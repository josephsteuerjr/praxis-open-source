#![cfg_attr(windows, windows_subsystem = "windows")]

#[cfg(windows)]
mod managed_child;

#[cfg(windows)]
mod app {
    use std::os::windows::process::CommandExt;
    use std::path::PathBuf;
    use std::process::{Command, Stdio};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex, OnceLock};
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use windows::Win32::Foundation::{
        CloseHandle, ERROR_ALREADY_EXISTS, GetLastError, HANDLE, HWND, LPARAM, LRESULT, POINT,
        WPARAM,
    };
    use windows::Win32::System::Threading::{CREATE_NO_WINDOW, CreateMutexW};
    use windows::Win32::UI::Shell::{
        NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_DELETE, NOTIFYICONDATAW, Shell_NotifyIconW,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        AppendMenuW, CreatePopupMenu, CreateWindowExW, DefWindowProcW, DestroyMenu,
        DispatchMessageW, GetCursorPos, GetMessageW, IDI_APPLICATION, LoadIconW, MF_GRAYED,
        MF_SEPARATOR, MF_STRING, MSG, PostQuitMessage, RegisterClassW, RegisterWindowMessageW,
        SetForegroundWindow, TPM_BOTTOMALIGN, TPM_LEFTALIGN, TPM_RETURNCMD, TrackPopupMenu,
        TranslateMessage, WM_DESTROY, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_USER, WNDCLASSW,
        WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_OVERLAPPED,
    };
    use windows::core::{PCWSTR, w};

    use crate::managed_child::ManagedChild;

    const TRAY_ID: u32 = 1;
    const WM_TRAY: u32 = WM_USER + 23;
    const MENU_OPEN: usize = 1001;
    const MENU_RECONNECT: usize = 1002;
    const MENU_EXIT: usize = 1003;
    static SUPERVISOR: OnceLock<Arc<Supervisor>> = OnceLock::new();
    static TASKBAR_CREATED: OnceLock<u32> = OnceLock::new();

    #[derive(Clone, Copy)]
    enum ChildMode {
        SessionHost,
        LegacyConnect,
    }

    impl ChildMode {
        fn subcommand(self) -> &'static str {
            match self {
                Self::SessionHost => "serve-interactive",
                Self::LegacyConnect => "connect",
            }
        }
    }

    struct Singleton(HANDLE);

    impl Drop for Singleton {
        fn drop(&mut self) {
            let _ = unsafe { CloseHandle(self.0) };
        }
    }

    struct Supervisor {
        body: PathBuf,
        config: PathBuf,
        state_dir: PathBuf,
        mode: ChildMode,
        stopping: AtomicBool,
        reconnect: AtomicBool,
        child: Mutex<Option<ManagedChild>>,
    }

    impl Supervisor {
        fn new(body: PathBuf, config: PathBuf, state_dir: PathBuf, mode: ChildMode) -> Arc<Self> {
            Arc::new(Self {
                body,
                config,
                state_dir,
                mode,
                stopping: AtomicBool::new(false),
                reconnect: AtomicBool::new(false),
                child: Mutex::new(None),
            })
        }

        fn spawn_loop(self: &Arc<Self>) {
            let this = self.clone();
            thread::spawn(move || {
                while !this.stopping.load(Ordering::SeqCst) {
                    let _ = std::fs::create_dir_all(&this.state_dir);
                    let stderr = std::fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(this.state_dir.join("body-stderr.log"));
                    let mut command = Command::new(&this.body);
                    command
                        .arg(this.mode.subcommand())
                        .arg("--config")
                        .arg(&this.config)
                        .stdin(Stdio::null())
                        .stdout(Stdio::null())
                        .creation_flags(CREATE_NO_WINDOW.0);
                    match stderr {
                        Ok(file) => {
                            command.stderr(Stdio::from(file));
                        }
                        Err(error) => {
                            this.log(&format!("body stderr log open failed: {error}"));
                            command.stderr(Stdio::null());
                        }
                    }
                    this.log(&format!(
                        "starting body mode={} executable={}",
                        this.mode.subcommand(),
                        this.body.display()
                    ));
                    let spawned = ManagedChild::spawn(&mut command, "interactive session host");
                    match spawned {
                        Ok(child) => {
                            this.log(&format!("body started pid={}", child.id()));
                            *this.child.lock().expect("child lock") = Some(child);
                        }
                        Err(error) => {
                            this.log(&format!("body spawn failed: {error}"));
                            thread::sleep(Duration::from_secs(5));
                            continue;
                        }
                    }
                    let mut termination_requested = false;
                    loop {
                        let stopping = this.stopping.load(Ordering::SeqCst);
                        let reconnecting = this.reconnect.swap(false, Ordering::SeqCst);
                        if (stopping || reconnecting) && !termination_requested {
                            let reason = if stopping {
                                "tray stopping"
                            } else {
                                "reconnect requested"
                            };
                            this.log(&format!("terminating body: {reason}"));
                            if let Some(child) = this.child.lock().expect("child lock").as_mut()
                                && let Err(error) = child.kill()
                            {
                                this.log(&format!("body terminate failed: {error}"));
                            }
                            termination_requested = true;
                        }
                        let wait = this
                            .child
                            .lock()
                            .expect("child lock")
                            .as_mut()
                            .map(ManagedChild::try_wait);
                        match wait {
                            Some(Ok(Some(status))) => {
                                this.log(&format!("body exited status={status}"));
                                break;
                            }
                            Some(Err(error)) => {
                                this.log(&format!("body status failed: {error}"));
                                break;
                            }
                            None => break,
                            Some(Ok(None)) => {}
                        }
                        thread::sleep(Duration::from_millis(500));
                    }
                    *this.child.lock().expect("child lock") = None;
                    if !this.stopping.load(Ordering::SeqCst) {
                        thread::sleep(Duration::from_secs(2));
                    }
                }
            });
        }

        fn request_reconnect(&self) {
            self.log("reconnect requested from tray");
            self.reconnect.store(true, Ordering::SeqCst);
        }
        fn stop(&self) {
            self.log("tray shutdown requested");
            self.stopping.store(true, Ordering::SeqCst);
            if let Some(child) = self.child.lock().expect("child lock").as_mut() {
                let _ = child.kill();
            }
        }
        fn open_state(&self) {
            let _ = Command::new("explorer.exe")
                .arg(&self.state_dir)
                .creation_flags(CREATE_NO_WINDOW.0)
                .spawn();
        }
        fn running(&self) -> bool {
            self.child.lock().expect("child lock").is_some()
        }
        fn log(&self, line: &str) {
            let _ = std::fs::create_dir_all(&self.state_dir);
            use std::io::Write;
            if let Ok(mut file) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(self.state_dir.join("tray.log"))
            {
                let timestamp = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|value| value.as_millis())
                    .unwrap_or_default();
                let _ = writeln!(file, "[{timestamp}] {line}");
            }
        }
    }

    fn wide(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(Some(0)).collect()
    }

    unsafe fn tray_data(hwnd: HWND, add: bool) {
        let mut data = NOTIFYICONDATAW {
            cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
            hWnd: hwnd,
            uID: TRAY_ID,
            uFlags: NIF_MESSAGE | NIF_ICON | NIF_TIP,
            uCallbackMessage: WM_TRAY,
            hIcon: unsafe { LoadIconW(None, IDI_APPLICATION).unwrap_or_default() },
            ..Default::default()
        };
        let label = if SUPERVISOR.get().is_some_and(|item| item.running()) {
            "Praxis Body — работает"
        } else {
            "Praxis Body — переподключается"
        };
        let encoded = wide(label);
        let take = encoded.len().min(data.szTip.len());
        data.szTip[..take].copy_from_slice(&encoded[..take]);
        if !unsafe { Shell_NotifyIconW(if add { NIM_ADD } else { NIM_DELETE }, &data) }.as_bool()
            && let Some(supervisor) = SUPERVISOR.get()
        {
            supervisor.log(&format!(
                "tray icon operation failed add={add}: {}",
                std::io::Error::last_os_error()
            ));
        }
    }

    unsafe fn show_menu(hwnd: HWND) {
        let Ok(menu) = (unsafe { CreatePopupMenu() }) else {
            return;
        };
        let status = wide(if SUPERVISOR.get().is_some_and(|item| item.running()) {
            "Работает"
        } else {
            "Переподключается"
        });
        let open = wide("Открыть состояние");
        let reconnect = wide("Переподключить");
        let exit = wide("Выход");
        let _ = unsafe { AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, PCWSTR(status.as_ptr())) };
        let _ = unsafe { AppendMenuW(menu, MF_SEPARATOR, 0, PCWSTR::null()) };
        let _ = unsafe { AppendMenuW(menu, MF_STRING, MENU_OPEN, PCWSTR(open.as_ptr())) };
        let _ = unsafe { AppendMenuW(menu, MF_STRING, MENU_RECONNECT, PCWSTR(reconnect.as_ptr())) };
        let _ = unsafe { AppendMenuW(menu, MF_STRING, MENU_EXIT, PCWSTR(exit.as_ptr())) };
        let mut point = POINT::default();
        let _ = unsafe { GetCursorPos(&mut point) };
        let _ = unsafe { SetForegroundWindow(hwnd) };
        let selected = unsafe {
            TrackPopupMenu(
                menu,
                TPM_BOTTOMALIGN | TPM_LEFTALIGN | TPM_RETURNCMD,
                point.x,
                point.y,
                None,
                hwnd,
                None,
            )
        }
        .0 as usize;
        let _ = unsafe { DestroyMenu(menu) };
        if let Some(supervisor) = SUPERVISOR.get() {
            match selected {
                MENU_OPEN => supervisor.open_state(),
                MENU_RECONNECT => supervisor.request_reconnect(),
                MENU_EXIT => {
                    supervisor.stop();
                    unsafe { PostQuitMessage(0) };
                }
                _ => {}
            }
        }
    }

    unsafe extern "system" fn wndproc(
        hwnd: HWND,
        message: u32,
        wparam: WPARAM,
        lparam: LPARAM,
    ) -> LRESULT {
        if TASKBAR_CREATED
            .get()
            .is_some_and(|registered| *registered == message)
        {
            if let Some(item) = SUPERVISOR.get() {
                item.log("taskbar recreated; restoring tray icon");
            }
            unsafe { tray_data(hwnd, true) };
            return LRESULT(0);
        }
        if message == WM_TRAY {
            match lparam.0 as u32 {
                WM_RBUTTONUP => unsafe { show_menu(hwnd) },
                WM_LBUTTONDBLCLK => {
                    if let Some(item) = SUPERVISOR.get() {
                        item.open_state();
                    }
                }
                _ => {}
            }
            return LRESULT(0);
        }
        if message == WM_DESTROY {
            unsafe { tray_data(hwnd, false) };
            if let Some(item) = SUPERVISOR.get() {
                item.stop();
            }
            unsafe { PostQuitMessage(0) };
            return LRESULT(0);
        }
        unsafe { DefWindowProcW(hwnd, message, wparam, lparam) }
    }

    fn parse() -> Result<(PathBuf, PathBuf, PathBuf, ChildMode), String> {
        let mut args = std::env::args_os().skip(1);
        let mut body = None;
        let mut config = None;
        let mut state = None;
        let mut mode = ChildMode::SessionHost;
        while let Some(arg) = args.next() {
            match arg.to_string_lossy().as_ref() {
                "--body" => body = args.next().map(PathBuf::from),
                "--config" => config = args.next().map(PathBuf::from),
                "--state-dir" => state = args.next().map(PathBuf::from),
                "--mode" => {
                    let value = args
                        .next()
                        .ok_or("--mode requires session-host or legacy-connect")?;
                    mode = match value.to_string_lossy().as_ref() {
                        "session-host" => ChildMode::SessionHost,
                        "legacy-connect" | "connect" => ChildMode::LegacyConnect,
                        _ => return Err("--mode requires session-host or legacy-connect".into()),
                    };
                }
                _ => {}
            }
        }
        Ok((
            body.ok_or("--body is required")?,
            config.ok_or("--config is required")?,
            state.ok_or("--state-dir is required")?,
            mode,
        ))
    }

    fn acquire_singleton() -> Result<Singleton, String> {
        let handle = unsafe { CreateMutexW(None, false, w!("Local\\PraxisBodyTray")) }
            .map_err(|error| format!("CreateMutexW: {error}"))?;
        if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
            let _ = unsafe { CloseHandle(handle) };
            return Err("Praxis tray is already running in this session".into());
        }
        Ok(Singleton(handle))
    }

    pub fn run() -> Result<(), String> {
        let (body, config, state, mode) = parse()?;
        let _singleton = acquire_singleton()?;
        let supervisor = Supervisor::new(body, config, state, mode);
        SUPERVISOR
            .set(supervisor.clone())
            .map_err(|_| "supervisor already set")?;
        supervisor.spawn_loop();
        unsafe {
            let taskbar_created = RegisterWindowMessageW(w!("TaskbarCreated"));
            if taskbar_created != 0 {
                let _ = TASKBAR_CREATED.set(taskbar_created);
            }
            let class = w!("PraxisBodyTrayWindow");
            let wc = WNDCLASSW {
                lpfnWndProc: Some(wndproc),
                lpszClassName: class,
                ..Default::default()
            };
            if RegisterClassW(&wc) == 0 {
                return Err(format!(
                    "RegisterClassW: {}",
                    std::io::Error::last_os_error()
                ));
            }
            let hwnd = CreateWindowExW(
                WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
                class,
                w!("Praxis Body"),
                WS_OVERLAPPED,
                0,
                0,
                0,
                0,
                None,
                None,
                None,
                None,
            )
            .map_err(|error| error.to_string())?;
            tray_data(hwnd, true);
            let mut message = MSG::default();
            while GetMessageW(&mut message, None, 0, 0).as_bool() {
                let _ = TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
        Ok(())
    }
}

#[cfg(windows)]
fn main() {
    if let Err(error) = app::run() {
        let root = std::env::var_os("PROGRAMDATA")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| std::path::PathBuf::from("C:\\ProgramData"))
            .join("Praxis\\Body");
        let _ = std::fs::create_dir_all(&root);
        let _ = std::fs::write(root.join("tray-startup-error.log"), error);
        std::process::exit(1);
    }
}

#[cfg(not(windows))]
fn main() {
    eprintln!("praxis-tray is Windows-only");
}
