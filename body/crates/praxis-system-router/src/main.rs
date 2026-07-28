#[cfg(windows)]
mod managed_child;

#[cfg(windows)]
mod service {
    use std::io::Write;
    use std::os::windows::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::{Command, Stdio};
    use std::sync::OnceLock;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::{Duration, Instant};

    use anyhow::{Context, Result};
    use praxis_body_protocol::{Envelope, Frame};
    use serde::{Deserialize, Serialize};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::windows::named_pipe::{NamedPipeServer, ServerOptions};
    use windows::Win32::Foundation::{HLOCAL, LocalFree};
    use windows::Win32::Security::Authorization::{
        ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
    };
    use windows::Win32::Security::{PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES};
    use windows::Win32::System::Services::*;
    use windows::Win32::System::Threading::CREATE_NO_WINDOW;
    use windows::core::{PCWSTR, PWSTR};

    use crate::managed_child::ManagedChild;

    static STOP: AtomicBool = AtomicBool::new(false);
    static SERVICE_NAME: OnceLock<String> = OnceLock::new();
    const MAX_LOCAL_MESSAGE: usize = 32 * 1024 * 1024;
    const PIPE_IO_TIMEOUT: Duration = Duration::from_secs(10);
    const DEFAULT_INVOKE_TIMEOUT: Duration = Duration::from_secs(5 * 60);
    const MAX_INVOKE_TIMEOUT: Duration = Duration::from_secs(30 * 60);

    #[derive(Debug, Clone, Deserialize)]
    struct Config {
        body_exe: PathBuf,
        body_config: PathBuf,
        pipe: String,
        token: String,
        allowed_user_sid: String,
        log: PathBuf,
        #[serde(default)]
        session_task: Option<String>,
    }

    #[derive(Debug, Serialize, Deserialize)]
    struct Request {
        token: String,
        #[serde(default)]
        ping: bool,
        #[serde(default)]
        envelope: Option<Envelope>,
    }

    #[derive(Debug, Serialize, Deserialize)]
    struct Response {
        ok: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        frame: Option<Frame>,
        #[serde(skip_serializing_if = "Option::is_none")]
        error: Option<String>,
    }

    fn argument_value(name: &str) -> Option<std::ffi::OsString> {
        let args: Vec<_> = std::env::args_os().collect();
        let at = args.iter().position(|value| value == name)?;
        args.get(at + 1).cloned()
    }

    fn configured_service_name() -> Result<String> {
        let name = argument_value("--service-name")
            .map(|value| value.to_string_lossy().into_owned())
            .unwrap_or_else(|| "PraxisSystemRouter".into());
        if name.trim().is_empty() || name.contains('\0') {
            anyhow::bail!("--service-name must be a non-empty Windows service name");
        }
        Ok(name)
    }

    fn read_config() -> Result<Config> {
        let path = argument_value("--config").context("--config is required")?;
        let config: Config = serde_json::from_slice(&std::fs::read(path)?)?;
        if !config.body_exe.is_file() {
            anyhow::bail!(
                "body executable does not exist: {}",
                config.body_exe.display()
            );
        }
        if !config.body_config.is_file() {
            anyhow::bail!(
                "body config does not exist: {}",
                config.body_config.display()
            );
        }
        if config.pipe.trim().is_empty()
            || config.token.is_empty()
            || config.allowed_user_sid.trim().is_empty()
        {
            anyhow::bail!("SYSTEM pipe, token and allowed_user_sid are required");
        }
        Ok(config)
    }

    fn log(path: &Path, line: &str) {
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
        {
            let _ = writeln!(file, "{} {line}", chrono::Utc::now().to_rfc3339());
        }
    }

    fn launch_session_task(config: &Config) {
        let Some(name) = config.session_task.as_deref() else {
            return;
        };
        match Command::new("schtasks.exe")
            .args(["/Run", "/TN", name])
            .creation_flags(CREATE_NO_WINDOW.0)
            .output()
        {
            Ok(output) if output.status.success() => {
                log(&config.log, &format!("requested session task {name}"));
            }
            Ok(output) => log(
                &config.log,
                &format!(
                    "session task {name} was not started: {}",
                    String::from_utf8_lossy(&output.stderr).trim()
                ),
            ),
            Err(error) => log(
                &config.log,
                &format!("session task {name} launch failed: {error}"),
            ),
        }
    }

    async fn write_response(pipe: &mut NamedPipeServer, response: &Response) -> Result<()> {
        let raw = serde_json::to_vec(response)?;
        if raw.len() > MAX_LOCAL_MESSAGE {
            anyhow::bail!("SYSTEM pipe response exceeds 32 MiB");
        }
        tokio::time::timeout(PIPE_IO_TIMEOUT, async {
            pipe.write_u32_le(raw.len() as u32).await?;
            pipe.write_all(&raw).await?;
            pipe.flush().await
        })
        .await
        .context("SYSTEM pipe response write timed out")??;
        Ok(())
    }

    fn invoke_timeout(envelope: &Envelope) -> Duration {
        let praxis_body_protocol::Frame::Invoke {
            deadline: Some(deadline),
            ..
        } = &envelope.frame
        else {
            return DEFAULT_INVOKE_TIMEOUT;
        };
        deadline
            .to_owned()
            .signed_duration_since(chrono::Utc::now())
            .to_std()
            .unwrap_or(Duration::from_millis(1))
            .saturating_add(Duration::from_secs(2))
            .clamp(Duration::from_secs(1), MAX_INVOKE_TIMEOUT)
    }

    async fn connection(mut pipe: NamedPipeServer, config: Config) -> Result<()> {
        let length = match tokio::time::timeout(PIPE_IO_TIMEOUT, pipe.read_u32_le()).await {
            Ok(Ok(length)) => length as usize,
            Ok(Err(error))
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::UnexpectedEof | std::io::ErrorKind::BrokenPipe
                ) =>
            {
                return Ok(());
            }
            Ok(Err(error)) => return Err(error.into()),
            Err(_) => anyhow::bail!("SYSTEM pipe header read timed out"),
        };
        if length > MAX_LOCAL_MESSAGE {
            anyhow::bail!("request exceeds 32 MiB");
        }
        let mut raw = vec![0; length];
        tokio::time::timeout(PIPE_IO_TIMEOUT, pipe.read_exact(&mut raw))
            .await
            .context("SYSTEM pipe body read timed out")??;
        let request: Request = serde_json::from_slice(&raw)?;
        if request.token != config.token {
            return write_response(
                &mut pipe,
                &Response {
                    ok: false,
                    frame: None,
                    error: Some("invalid router token".into()),
                },
            )
            .await;
        }
        if request.ping {
            return write_response(
                &mut pipe,
                &Response {
                    ok: true,
                    frame: None,
                    error: None,
                },
            )
            .await;
        }
        let Some(envelope) = request.envelope else {
            return write_response(
                &mut pipe,
                &Response {
                    ok: false,
                    frame: None,
                    error: Some("missing envelope".into()),
                },
            )
            .await;
        };
        let input = serde_json::to_vec(&envelope)?;
        let child_timeout = invoke_timeout(&envelope);
        let mut command = tokio::process::Command::new(&config.body_exe);
        command
            .arg("invoke-envelope")
            .arg("--config")
            .arg(&config.body_config)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        command.as_std_mut().creation_flags(CREATE_NO_WINDOW.0);
        let mut child = command.spawn().context("start SYSTEM body helper")?;
        let mut stdin = child.stdin.take().context("body stdin")?;
        tokio::time::timeout(PIPE_IO_TIMEOUT, stdin.write_all(&input))
            .await
            .context("SYSTEM body helper input timed out")??;
        drop(stdin);
        let output = match tokio::time::timeout(child_timeout, child.wait_with_output()).await {
            Ok(output) => output.context("wait for SYSTEM body helper")?,
            Err(_) => {
                let message = format!(
                    "SYSTEM body helper timed out after {}s",
                    child_timeout.as_secs()
                );
                log(&config.log, &message);
                return write_response(
                    &mut pipe,
                    &Response {
                        ok: false,
                        frame: None,
                        error: Some(message),
                    },
                )
                .await;
            }
        };
        if !output.status.success() {
            let error = String::from_utf8_lossy(&output.stderr).trim().to_string();
            log(&config.log, &format!("body helper failed: {error}"));
            return write_response(
                &mut pipe,
                &Response {
                    ok: false,
                    frame: None,
                    error: Some(error),
                },
            )
            .await;
        }
        let frame: Frame = serde_json::from_slice(&output.stdout)?;
        write_response(
            &mut pipe,
            &Response {
                ok: true,
                frame: Some(frame),
                error: None,
            },
        )
        .await
    }

    async fn stop_requested() {
        while !STOP.load(Ordering::SeqCst) {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    fn secure_server(pipe: &str, first: bool, allowed_user_sid: &str) -> Result<NamedPipeServer> {
        let sddl = format!("D:P(A;;GA;;;SY)(A;;GA;;;{allowed_user_sid})");
        let mut wide: Vec<u16> = sddl.encode_utf16().collect();
        wide.push(0);
        let mut descriptor = PSECURITY_DESCRIPTOR::default();
        unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                PCWSTR(wide.as_ptr()),
                SDDL_REVISION_1,
                &mut descriptor,
                None,
            )?
        };
        let mut attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor.0,
            ..Default::default()
        };
        let mut options = ServerOptions::new();
        options.first_pipe_instance(first);
        let created = unsafe {
            options.create_with_security_attributes_raw(
                pipe,
                (&mut attributes as *mut SECURITY_ATTRIBUTES).cast(),
            )
        };
        unsafe {
            let _ = LocalFree(Some(HLOCAL(descriptor.0)));
        }
        Ok(created?)
    }

    async fn serve_router(config: Config) -> Result<()> {
        log(
            &config.log,
            &format!("SYSTEM router listening on {}", config.pipe),
        );
        let mut first = true;
        while !STOP.load(Ordering::SeqCst) {
            let server = secure_server(&config.pipe, first, &config.allowed_user_sid)?;
            first = false;
            tokio::select! {
                result = server.connect() => {
                    result?;
                    let next = config.clone();
                    tokio::spawn(async move {
                        if let Err(error) = connection(server, next.clone()).await {
                            log(&next.log, &format!("SYSTEM request failed: {error:#}"));
                        }
                    });
                }
                _ = stop_requested() => break,
            }
        }
        Ok(())
    }

    async fn supervise_connection(config: Config) -> Result<()> {
        let connection_log = config
            .log
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join("connection.log");
        let mut failures = 0u32;
        while !STOP.load(Ordering::SeqCst) {
            let stdout = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&connection_log)?;
            let stderr = stdout.try_clone()?;
            let mut command = Command::new(&config.body_exe);
            command
                .arg("connect")
                .arg("--config")
                .arg(&config.body_config)
                .stdin(Stdio::null())
                .stdout(Stdio::from(stdout))
                .stderr(Stdio::from(stderr));
            let mut child = ManagedChild::spawn(&mut command, "Praxis WSS connection body")
                .with_context(|| format!("start {} connect", config.body_exe.display()))?;
            let child_started = Instant::now();
            log(
                &config.log,
                &format!("connection body started pid={}", child.id()),
            );
            let exit = loop {
                if STOP.load(Ordering::SeqCst) {
                    let _ = child.kill();
                    break child.wait().ok();
                }
                if let Some(status) = child.try_wait()? {
                    break Some(status);
                }
                tokio::time::sleep(Duration::from_millis(500)).await;
            };
            if STOP.load(Ordering::SeqCst) {
                log(&config.log, "connection body stopped with service");
                break;
            }
            if child_started.elapsed() >= Duration::from_secs(60) {
                failures = 0;
            }
            failures = failures.saturating_add(1);
            log(
                &config.log,
                &format!("connection body exited {exit:?}; restart #{failures}"),
            );
            let delay = (1u64 << failures.min(5)).min(30);
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(delay)) => {}
                _ = stop_requested() => break,
            }
        }
        Ok(())
    }

    async fn serve(config: Config) -> Result<()> {
        launch_session_task(&config);
        let router = serve_router(config.clone());
        let connection = supervise_connection(config);
        tokio::try_join!(router, connection)?;
        Ok(())
    }

    unsafe extern "system" fn handler(control: u32) {
        if control == SERVICE_CONTROL_STOP || control == SERVICE_CONTROL_SHUTDOWN {
            STOP.store(true, Ordering::SeqCst);
        }
    }

    fn set_status(handle: SERVICE_STATUS_HANDLE, state: SERVICE_STATUS_CURRENT_STATE, exit: u32) {
        let controls = if state == SERVICE_RUNNING {
            SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
        } else {
            0
        };
        let status = SERVICE_STATUS {
            dwServiceType: SERVICE_WIN32_OWN_PROCESS,
            dwCurrentState: state,
            dwControlsAccepted: controls,
            dwWin32ExitCode: exit,
            dwWaitHint: if state == SERVICE_START_PENDING {
                10_000
            } else {
                0
            },
            ..Default::default()
        };
        let _ = unsafe { SetServiceStatus(handle, &status) };
    }

    unsafe extern "system" fn service_main(_argc: u32, _argv: *mut PWSTR) {
        let service_name = SERVICE_NAME
            .get()
            .map(String::as_str)
            .unwrap_or("PraxisSystemRouter");
        let service_name: Vec<u16> = service_name.encode_utf16().chain(Some(0)).collect();
        let Ok(handle) =
            (unsafe { RegisterServiceCtrlHandlerW(PCWSTR(service_name.as_ptr()), Some(handler)) })
        else {
            return;
        };
        set_status(handle, SERVICE_START_PENDING, 0);
        let config = match read_config() {
            Ok(config) => config,
            Err(error) => {
                let fallback = std::env::var_os("PROGRAMDATA")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"))
                    .join(r"Praxis\Body\service-startup-error.log");
                log(&fallback, &format!("service config failed: {error:#}"));
                set_status(handle, SERVICE_STOPPED, 1);
                return;
            }
        };
        log(&config.log, "Praxis Body Service starting");
        set_status(handle, SERVICE_RUNNING, 0);
        let result: Result<()> = match tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
        {
            Ok(runtime) => runtime.block_on(serve(config.clone())),
            Err(error) => Err(error.into()),
        };
        if let Err(error) = &result {
            log(&config.log, &format!("service failed: {error:#}"));
        }
        set_status(handle, SERVICE_STOPPED, u32::from(result.is_err()));
    }

    pub fn run() -> Result<()> {
        let configured_name = configured_service_name()?;
        SERVICE_NAME
            .set(configured_name.clone())
            .map_err(|_| anyhow::anyhow!("Windows service name was already initialized"))?;
        let mut name: Vec<u16> = configured_name.encode_utf16().chain(Some(0)).collect();
        let table = [
            SERVICE_TABLE_ENTRYW {
                lpServiceName: PWSTR(name.as_mut_ptr()),
                lpServiceProc: Some(service_main),
            },
            SERVICE_TABLE_ENTRYW::default(),
        ];
        unsafe {
            StartServiceCtrlDispatcherW(table.as_ptr())?;
        }
        Ok(())
    }
}

#[cfg(windows)]
fn main() {
    if let Err(error) = service::run() {
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}

#[cfg(not(windows))]
fn main() {
    eprintln!("praxis-system-router is Windows-only");
}
