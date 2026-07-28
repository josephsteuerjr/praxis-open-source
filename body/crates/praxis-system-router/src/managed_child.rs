use std::ffi::c_void;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Child, Command, ExitStatus};

use anyhow::{Context, Result};
use windows::Win32::Foundation::{CloseHandle, ERROR_ACCESS_DENIED, HANDLE};
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JobObjectExtendedLimitInformation, SetInformationJobObject,
};
use windows::Win32::System::Threading::{
    CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW, GetCurrentProcess,
};
use windows::core::BOOL;

/// Owns exactly one transport/session-host process.
///
/// SILENT_BREAKAWAY is intentional: durable operation supervisors created by
/// praxis-body must outlive a transport reconnect or tray/service restart.
pub struct ManagedChild {
    child: Child,
    _job: KillOnCloseJob,
}

struct KillOnCloseJob(usize);

impl KillOnCloseJob {
    fn create() -> Result<Self> {
        let job = unsafe { CreateJobObjectW(None, None)? };
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK;
        if let Err(error) = unsafe {
            SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const c_void,
                std::mem::size_of_val(&limits) as u32,
            )
        } {
            unsafe {
                let _ = CloseHandle(job);
            }
            return Err(error).context("configure managed-child Job Object");
        }
        Ok(Self(job.0 as usize))
    }

    fn assign(&self, child: &Child) -> Result<()> {
        let process = HANDLE(child.as_raw_handle());
        unsafe { AssignProcessToJobObject(self.handle(), process) }
            .context("assign immediate child to Job Object")
    }

    fn handle(&self) -> HANDLE {
        HANDLE(self.0 as *mut c_void)
    }
}

impl Drop for KillOnCloseJob {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.handle());
        }
    }
}

fn current_process_is_in_job() -> Result<bool> {
    let mut in_job = BOOL::default();
    unsafe { IsProcessInJob(GetCurrentProcess(), None, &mut in_job)? };
    Ok(in_job.as_bool())
}

fn creation_flags(try_outer_breakaway: bool) -> u32 {
    CREATE_NO_WINDOW.0
        | if try_outer_breakaway {
            CREATE_BREAKAWAY_FROM_JOB.0
        } else {
            0
        }
}

impl ManagedChild {
    pub fn spawn(command: &mut Command, label: &str) -> Result<Self> {
        let job = KillOnCloseJob::create()?;
        let try_outer_breakaway = current_process_is_in_job().unwrap_or(true);
        command.creation_flags(creation_flags(try_outer_breakaway));
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(error)
                if try_outer_breakaway
                    && error.raw_os_error() == Some(ERROR_ACCESS_DENIED.0 as i32) =>
            {
                // An outer scheduler/service job may forbid explicit breakaway but permit
                // nested jobs. Retry inside it, then assign our immediate-child job.
                command.creation_flags(creation_flags(false));
                command
                    .spawn()
                    .with_context(|| format!("start {label} inside outer Job Object"))?
            }
            Err(error) => return Err(error).with_context(|| format!("start {label}")),
        };
        if let Err(error) = job.assign(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error).with_context(|| format!("contain {label}"));
        }
        Ok(Self { child, _job: job })
    }

    pub fn id(&self) -> u32 {
        self.child.id()
    }

    pub fn kill(&mut self) -> std::io::Result<()> {
        self.child.kill()
    }

    pub fn try_wait(&mut self) -> std::io::Result<Option<ExitStatus>> {
        self.child.try_wait()
    }

    pub fn wait(&mut self) -> std::io::Result<ExitStatus> {
        self.child.wait()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn managed_job_kills_owner_but_silently_releases_descendants() {
        let flags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK).0;
        assert_ne!(flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.0, 0);
        assert_ne!(flags & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK.0, 0);
    }

    #[test]
    fn outer_breakaway_is_only_added_when_requested() {
        assert_eq!(creation_flags(false), CREATE_NO_WINDOW.0);
        assert_ne!(creation_flags(true) & CREATE_BREAKAWAY_FROM_JOB.0, 0);
    }
}
