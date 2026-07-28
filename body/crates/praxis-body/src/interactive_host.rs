use std::sync::Arc;

use anyhow::Result;

use crate::runtime::Runtime;

#[cfg(windows)]
pub async fn serve(runtime: Arc<Runtime>) -> Result<()> {
    use std::io::Write;
    use std::time::Duration;

    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::windows::named_pipe::{NamedPipeServer, ServerOptions};
    use windows::Win32::Foundation::{HLOCAL, LocalFree};
    use windows::Win32::Security::Authorization::{
        ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
    };
    use windows::Win32::Security::{PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES};
    use windows::core::PCWSTR;

    use crate::local_router::{MAX_LOCAL_MESSAGE, Request, Response};

    const PIPE_IO_TIMEOUT: Duration = Duration::from_secs(10);

    fn log(runtime: &Runtime, message: &str) {
        let path = runtime.config.state_dir.join("session-host.log");
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
        {
            let _ = writeln!(file, "{} {message}", chrono::Utc::now().to_rfc3339());
        }
    }

    async fn respond(pipe: &mut NamedPipeServer, response: &Response) -> Result<()> {
        let raw = serde_json::to_vec(response)?;
        if raw.len() > MAX_LOCAL_MESSAGE {
            anyhow::bail!("interactive router response exceeds 32 MiB");
        }
        tokio::time::timeout(PIPE_IO_TIMEOUT, async {
            pipe.write_u32_le(raw.len() as u32).await?;
            pipe.write_all(&raw).await?;
            pipe.flush().await?;
            Ok::<(), anyhow::Error>(())
        })
        .await
        .map_err(|_| anyhow::anyhow!("interactive router response timed out"))??;
        Ok(())
    }

    async fn connection(mut pipe: NamedPipeServer, runtime: Arc<Runtime>) -> Result<()> {
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
            Err(_) => anyhow::bail!("interactive router header timed out"),
        };
        if length > MAX_LOCAL_MESSAGE {
            anyhow::bail!("interactive router request exceeds 32 MiB");
        }
        let mut raw = vec![0; length];
        tokio::time::timeout(PIPE_IO_TIMEOUT, pipe.read_exact(&mut raw))
            .await
            .map_err(|_| anyhow::anyhow!("interactive router request timed out"))??;
        let request: Request = serde_json::from_slice(&raw)?;
        if request.token != runtime.config.interactive_router_token {
            return respond(
                &mut pipe,
                &Response {
                    ok: false,
                    frame: None,
                    error: Some("invalid interactive router token".into()),
                },
            )
            .await;
        }
        if request.ping {
            return respond(
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
            return respond(
                &mut pipe,
                &Response {
                    ok: false,
                    frame: None,
                    error: Some("missing envelope".into()),
                },
            )
            .await;
        };
        let frame = runtime.handle(envelope).await;
        respond(
            &mut pipe,
            &Response {
                ok: true,
                frame: Some(frame),
                error: None,
            },
        )
        .await
    }

    fn secure_server(pipe: &str, first: bool, owner_sid: &str) -> Result<NamedPipeServer> {
        let sddl = format!("D:P(A;;GA;;;SY)(A;;GA;;;{owner_sid})");
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

    if runtime.config.interactive_router_token.is_empty() {
        anyhow::bail!("interactive_router_token is required for serve-interactive");
    }
    let current_sid = crate::identity::current().user_sid.unwrap_or_default();
    if runtime.config.interactive_user_sid.is_empty()
        || !current_sid.eq_ignore_ascii_case(&runtime.config.interactive_user_sid)
    {
        anyhow::bail!(
            "interactive host SID {current_sid:?} does not match configured owner SID {:?}",
            runtime.config.interactive_user_sid
        );
    }
    log(&runtime, "interactive session host starting");
    let mut first = true;
    loop {
        let server = secure_server(
            &runtime.config.interactive_router_pipe,
            first,
            &runtime.config.interactive_user_sid,
        )?;
        first = false;
        server.connect().await?;
        let next = runtime.clone();
        tokio::spawn(async move {
            if let Err(error) = connection(server, next.clone()).await {
                log(&next, &format!("interactive request failed: {error:#}"));
            }
        });
    }
}

#[cfg(not(windows))]
pub async fn serve(_runtime: Arc<Runtime>) -> Result<()> {
    anyhow::bail!("interactive session host is Windows-only")
}
