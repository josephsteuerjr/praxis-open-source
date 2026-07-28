use std::time::Duration;

use anyhow::{Context, Result};
use praxis_body_protocol::{Envelope, Frame};
use serde::{Deserialize, Serialize};

#[cfg(windows)]
pub const MAX_LOCAL_MESSAGE: usize = 32 * 1024 * 1024;
const LOCAL_CONNECT_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Debug, Serialize, Deserialize)]
pub struct Request {
    pub token: String,
    #[serde(default)]
    pub ping: bool,
    #[serde(default)]
    pub envelope: Option<Envelope>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Response {
    pub ok: bool,
    #[serde(default)]
    pub frame: Option<Frame>,
    #[serde(default)]
    pub error: Option<String>,
}

#[cfg(windows)]
fn verify_server<T: std::os::windows::io::AsRawHandle>(
    pipe: &T,
    expected_sid: &str,
) -> Result<u32> {
    use std::ffi::c_void;
    use windows::Win32::Foundation::{CloseHandle, HANDLE, HLOCAL, LocalFree};
    use windows::Win32::Security::Authorization::ConvertStringSidToSidW;
    use windows::Win32::Security::{
        EqualSid, GetTokenInformation, PSID, TOKEN_QUERY, TOKEN_USER, TokenUser,
    };
    use windows::Win32::System::Pipes::GetNamedPipeServerProcessId;
    use windows::Win32::System::Threading::{
        OpenProcess, OpenProcessToken, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    use windows::core::PCWSTR;

    if expected_sid.trim().is_empty() {
        anyhow::bail!("expected named-pipe server SID is empty");
    }
    let handle = HANDLE(pipe.as_raw_handle());
    let mut server_pid = 0u32;
    unsafe { GetNamedPipeServerProcessId(handle, &mut server_pid)? };

    let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, server_pid)? };
    let mut token = HANDLE::default();
    let token_result = unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) };
    if let Err(error) = token_result {
        unsafe {
            let _ = CloseHandle(process);
        }
        return Err(error.into());
    }

    let result = (|| -> Result<bool> {
        let mut needed = 0u32;
        let _ = unsafe { GetTokenInformation(token, TokenUser, None, 0, &mut needed) };
        if needed < std::mem::size_of::<TOKEN_USER>() as u32 {
            anyhow::bail!("named-pipe server token returned no user SID");
        }
        let mut buffer = vec![0u8; needed as usize];
        unsafe {
            GetTokenInformation(
                token,
                TokenUser,
                Some(buffer.as_mut_ptr().cast::<c_void>()),
                needed,
                &mut needed,
            )?
        };
        let token_user = unsafe { &*(buffer.as_ptr().cast::<TOKEN_USER>()) };

        let mut wide: Vec<u16> = expected_sid.encode_utf16().collect();
        wide.push(0);
        let mut expected = PSID::default();
        unsafe { ConvertStringSidToSidW(PCWSTR(wide.as_ptr()), &mut expected)? };
        let matches = unsafe { EqualSid(token_user.User.Sid, expected).is_ok() };
        unsafe {
            let _ = LocalFree(Some(HLOCAL(expected.0)));
        }
        Ok(matches)
    })();

    unsafe {
        let _ = CloseHandle(token);
        let _ = CloseHandle(process);
    }
    if !result? {
        anyhow::bail!(
            "named-pipe server PID {server_pid} is not owned by expected SID {expected_sid}"
        );
    }
    Ok(server_pid)
}

#[cfg(windows)]
async fn exchange(
    pipe_name: &str,
    expected_sid: &str,
    request: &Request,
    connect_wait: Duration,
    io_wait: Option<Duration>,
) -> Result<Response> {
    use std::time::Instant;

    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::windows::named_pipe::ClientOptions;

    async fn inner(
        pipe_name: &str,
        expected_sid: &str,
        request: &Request,
        connect_wait: Duration,
    ) -> Result<Response> {
        let deadline = Instant::now() + connect_wait;
        let mut pipe = loop {
            match ClientOptions::new().open(pipe_name) {
                Ok(pipe) => break pipe,
                Err(error)
                    if Instant::now() < deadline
                        && matches!(error.raw_os_error(), Some(2 | 231 | 233)) =>
                {
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
                Err(error) => {
                    return Err(error).with_context(|| format!("open local router {pipe_name}"));
                }
            }
        };
        verify_server(&pipe, expected_sid)
            .with_context(|| format!("authenticate local router {pipe_name}"))?;

        let raw = serde_json::to_vec(request)?;
        if raw.len() > MAX_LOCAL_MESSAGE {
            anyhow::bail!("local router request exceeds 32 MiB");
        }
        pipe.write_u32_le(raw.len() as u32).await?;
        pipe.write_all(&raw).await?;
        pipe.flush().await?;
        let length = pipe.read_u32_le().await? as usize;
        if length > MAX_LOCAL_MESSAGE {
            anyhow::bail!("local router response exceeds 32 MiB");
        }
        let mut raw = vec![0; length];
        pipe.read_exact(&mut raw).await?;
        Ok(serde_json::from_slice(&raw)?)
    }

    if let Some(io_wait) = io_wait {
        tokio::time::timeout(
            connect_wait.saturating_add(io_wait),
            inner(pipe_name, expected_sid, request, connect_wait),
        )
        .await
        .with_context(|| format!("local router {pipe_name} timed out"))?
    } else {
        // Once the authenticated executor has admitted an Invoke, a caller-side deadline must
        // not synthesize a terminal router Error. The target Runtime owns terminal completion;
        // replay may need this pipe to carry a result produced after the old socket disappeared.
        inner(pipe_name, expected_sid, request, connect_wait).await
    }
}

#[cfg(not(windows))]
async fn exchange(
    _pipe_name: &str,
    _expected_sid: &str,
    _request: &Request,
    _connect_wait: Duration,
    _io_wait: Option<Duration>,
) -> Result<Response> {
    anyhow::bail!("local routers are Windows-only")
}

#[cfg(windows)]
pub fn available(pipe: &str, expected_sid: &str) -> bool {
    use tokio::net::windows::named_pipe::ClientOptions;

    if expected_sid.trim().is_empty() {
        return false;
    }
    ClientOptions::new()
        .open(pipe)
        .ok()
        .is_some_and(|client| verify_server(&client, expected_sid).is_ok())
}

#[cfg(not(windows))]
pub fn available(_pipe: &str, _expected_sid: &str) -> bool {
    false
}

fn validate_correlated_frame(frame: &Frame, request_id: &str, operation_id: &str) -> Result<()> {
    let matches = match frame {
        Frame::Accepted {
            request_id: response_request,
            operation_id: response_operation,
            ..
        }
        | Frame::Progress {
            request_id: response_request,
            operation_id: response_operation,
            ..
        }
        | Frame::Result {
            request_id: response_request,
            operation_id: response_operation,
            ..
        }
        | Frame::Cancelled {
            request_id: response_request,
            operation_id: response_operation,
            ..
        } => response_request == request_id && response_operation == operation_id,
        Frame::Error {
            request_id: Some(response_request),
            operation_id: Some(response_operation),
            ..
        } => response_request == request_id && response_operation == operation_id,
        _ => false,
    };
    if !matches {
        anyhow::bail!("local router returned an uncorrelated frame");
    }
    Ok(())
}

pub async fn invoke(
    pipe: String,
    expected_sid: String,
    token: String,
    envelope: Envelope,
) -> Result<Frame> {
    let (request_id, operation_id) = match &envelope.frame {
        Frame::Invoke {
            request_id,
            operation_id,
            ..
        } => (request_id.clone(), operation_id.clone()),
        _ => anyhow::bail!("local router accepts only invoke envelopes"),
    };
    let response = exchange(
        &pipe,
        &expected_sid,
        &Request {
            token,
            ping: false,
            envelope: Some(envelope),
        },
        LOCAL_CONNECT_TIMEOUT,
        None,
    )
    .await?;
    if !response.ok {
        anyhow::bail!(
            "{}",
            response
                .error
                .unwrap_or_else(|| "local router rejected request".into())
        );
    }
    let frame = response.frame.context("local router returned no frame")?;
    validate_correlated_frame(&frame, &request_id, &operation_id)?;
    Ok(frame)
}

#[cfg(test)]
mod tests {
    use super::*;
    use praxis_body_protocol::{ExecutionIdentity, ExecutionKind, IntegrityLevel, OperationStatus};

    #[test]
    fn rejects_uncorrelated_local_frames() {
        let frame = Frame::Accepted {
            request_id: "other".into(),
            operation_id: "op-1".into(),
            status: OperationStatus::Starting,
            identity: ExecutionIdentity {
                kind: ExecutionKind::Interactive,
                user_sid: None,
                session_id: Some(1),
                integrity: IntegrityLevel::Unknown,
                elevated: false,
            },
        };
        assert!(validate_correlated_frame(&frame, "req-1", "op-1").is_err());
    }
}
