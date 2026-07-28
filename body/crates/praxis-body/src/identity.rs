use std::process::Command;

use praxis_body_protocol::{
    AdapterDescriptor, CapabilityDescriptor, CapabilityManifest, ExecutionContextCapability,
    ExecutionIdentity, ExecutionKind, IntegrityLevel, PROTOCOL,
};

use crate::{compose, desktop, uia};

fn command_output(command: &mut Command) -> std::io::Result<std::process::Output> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows::Win32::System::Threading::CREATE_NO_WINDOW;

        command.creation_flags(CREATE_NO_WINDOW.0);
    }
    command.output()
}

pub fn current() -> ExecutionIdentity {
    #[cfg(windows)]
    {
        windows_identity()
    }
    #[cfg(not(windows))]
    {
        let user = std::env::var("USER").unwrap_or_else(|_| "unknown".into());
        let root = unsafe { libc::geteuid() } == 0;
        ExecutionIdentity {
            kind: if root {
                ExecutionKind::System
            } else {
                ExecutionKind::Interactive
            },
            user_sid: Some(user),
            session_id: None,
            integrity: if root {
                IntegrityLevel::System
            } else {
                IntegrityLevel::Unknown
            },
            elevated: root,
        }
    }
}

#[cfg(windows)]
fn windows_identity() -> ExecutionIdentity {
    let mut user = Command::new("whoami.exe");
    user.args(["/user", "/fo", "csv", "/nh"]);
    let user_output = command_output(&mut user).ok();
    let mut groups = Command::new("whoami.exe");
    groups.args(["/groups", "/fo", "csv", "/nh"]);
    let groups_output = command_output(&mut groups).ok();
    let user_sid = user_output
        .as_ref()
        .and_then(|x| find_sid(&String::from_utf8_lossy(&x.stdout), "S-1-5-"));
    let groups = groups_output
        .as_ref()
        .map(|x| String::from_utf8_lossy(&x.stdout).to_string())
        .unwrap_or_default();
    let integrity = if groups.contains("S-1-16-20480") {
        IntegrityLevel::Protected
    } else if groups.contains("S-1-16-16384") {
        IntegrityLevel::System
    } else if groups.contains("S-1-16-12288") {
        IntegrityLevel::High
    } else if groups.contains("S-1-16-8192") || groups.contains("S-1-16-8448") {
        IntegrityLevel::Medium
    } else if groups.contains("S-1-16-4096") {
        IntegrityLevel::Low
    } else if groups.contains("S-1-16-0") {
        IntegrityLevel::Untrusted
    } else {
        IntegrityLevel::Unknown
    };
    let system = user_sid.as_deref() == Some("S-1-5-18") || integrity == IntegrityLevel::System;
    let elevated = matches!(
        integrity,
        IntegrityLevel::High | IntegrityLevel::System | IntegrityLevel::Protected
    );
    ExecutionIdentity {
        kind: if system {
            ExecutionKind::System
        } else {
            ExecutionKind::Interactive
        },
        user_sid,
        session_id: current_session_id(),
        integrity,
        elevated,
    }
}

#[cfg(windows)]
fn current_session_id() -> Option<u32> {
    use windows::Win32::System::RemoteDesktop::ProcessIdToSessionId;
    let mut session = 0u32;
    unsafe { ProcessIdToSessionId(std::process::id(), &mut session).ok()? };
    Some(session)
}

#[cfg(windows)]
fn find_sid(text: &str, prefix: &str) -> Option<String> {
    let start = text.find(prefix)?;
    let tail = &text[start..];
    let end = tail
        .find(|ch: char| !(ch.is_ascii_digit() || ch == '-' || ch == 'S'))
        .unwrap_or(tail.len());
    Some(tail[..end].to_string())
}

#[cfg(not(windows))]
fn python_adapter() -> AdapterDescriptor {
    AdapterDescriptor {
        name: "python-pywin32".into(),
        version: "unavailable".into(),
        capabilities: vec![],
        available: false,
    }
}

/// ⚠ Проба порождает python.exe. Манифест собирается на КАЖДЫЙ вызов возможностей —
/// самый частый глагол на этой машине, — и каждый раз платил спавном интерпретатора
/// (порядка полусекунды на ровном месте). Наличие pywin32 за время жизни процесса не
/// меняется, поэтому спрашиваем один раз.
#[cfg(windows)]
static PYTHON_ADAPTER: std::sync::OnceLock<AdapterDescriptor> = std::sync::OnceLock::new();

#[cfg(windows)]
fn python_adapter() -> AdapterDescriptor {
    PYTHON_ADAPTER.get_or_init(probe_python_adapter).clone()
}

#[cfg(windows)]
fn probe_python_adapter() -> AdapterDescriptor {
    let mut command = Command::new("python.exe");
    command.args([
        "-c",
        "import sys,pythoncom,win32com.client;print(sys.version.split()[0])",
    ]);
    let probe = command_output(&mut command);
    let (available, version) = match probe {
        Ok(output) if output.status.success() => (
            true,
            String::from_utf8_lossy(&output.stdout).trim().to_string(),
        ),
        _ => (false, "unavailable".into()),
    };
    AdapterDescriptor {
        name: "python-pywin32".into(),
        version,
        // ⚠ Здесь стояло vec!["office.com", "win32.api"]. Единственным читателем этих строк
        // во всей коробке была сама эта строка: диспетчер таких глаголов не знает, ни один
        // обработчик их не принимает. То есть манифест объявлял ей две возможности, которых
        // нет — а объявленная и неисполнимая возможность хуже отсутствующей: на неё
        // рассчитывают. Адаптер остаётся (факт «pywin32 есть» полезен сам по себе),
        // обещаний больше нет.
        capabilities: vec![],
        available,
    }
}

pub fn manifest(device_id: &str) -> CapabilityManifest {
    manifest_with_routes(device_id, false, false)
}

pub fn manifest_with_routes(
    device_id: &str,
    interactive_router_available: bool,
    system_router_available: bool,
) -> CapabilityManifest {
    let identity = current();
    let interactive_available =
        identity.kind == ExecutionKind::Interactive || interactive_router_available;
    let system_available = identity.kind == ExecutionKind::System || system_router_available;
    let capability = |name: &str, mutating: bool, durable: bool| CapabilityDescriptor {
        name: name.into(),
        version: 1,
        mutating,
        durable,
    };
    let mut capabilities = vec![
        capability("body.status", false, false),
        capability("fs.stat", false, false),
        capability("fs.list", false, false),
        capability("fs.read", false, false),
        capability("fs.hash", false, false),
        capability("fs.write_atomic", true, true),
        capability("fs.replace", true, true),
        capability("fs.apply_patch", true, true),
        capability("fs.export", false, true),
        capability("fs.import", true, true),
        // Четыре глагола ниже маршрутизировались общей веткой `fs.*` с того дня, как их
        // написали, — то есть работали, о чём манифест молчал. Необъявленная исполнимая
        // возможность так же невидима, как объявленная неисполнимая: она о ней не узнает.
        capability("fs.delete", true, true),
        capability("fs.move", true, true),
        capability("fs.copy", true, true),
        capability("fs.mkdir", true, true),
        capability("process.start", true, true),
        capability("process.status", false, true),
        capability("process.cancel", true, true),
        capability("process.list", false, true),
        capability("adapter.list", false, false),
    ];
    capabilities.extend(desktop::descriptors());
    capabilities.extend(uia::descriptors());
    capabilities.extend(compose::descriptors());
    CapabilityManifest {
        protocol: PROTOCOL.into(),
        body_version: env!("CARGO_PKG_VERSION").into(),
        device_id: device_id.into(),
        hostname: hostname(),
        os: std::env::consts::OS.into(),
        arch: std::env::consts::ARCH.into(),
        execution_contexts: vec![
            ExecutionContextCapability {
                kind: ExecutionKind::Interactive,
                available: interactive_available,
                identity: (identity.kind == ExecutionKind::Interactive).then(|| identity.clone()),
                unavailable_reason: (!interactive_available)
                    .then(|| "no interactive Praxis session host is connected".into()),
            },
            ExecutionContextCapability {
                kind: ExecutionKind::System,
                available: system_available,
                identity: (identity.kind == ExecutionKind::System).then(|| identity.clone()),
                unavailable_reason: (!system_available)
                    .then(|| "Praxis Body Service is not available".into()),
            },
        ],
        capabilities,
        adapters: vec![
            desktop::adapter_descriptor(),
            uia::adapter_descriptor(),
            compose::adapter_descriptor(),
            python_adapter(),
        ],
    }
}

fn hostname() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| {
            let mut command = Command::new(if cfg!(windows) {
                "hostname.exe"
            } else {
                "hostname"
            });
            command_output(&mut command)
                .ok()
                .filter(|output| output.status.success())
                .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
        })
        .unwrap_or_else(|| "unknown".into())
}
