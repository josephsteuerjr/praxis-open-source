//! Deterministic native Windows desktop capabilities.
//!
//! This module contains no planning, memory, or autonomous policy. The server
//! chooses a typed operation; the interactive body executes it and returns a
//! structured receipt. Desktop capabilities intentionally refuse to pretend
//! that Session 0 is an interactive desktop.

use std::path::{Path, PathBuf};

#[cfg(any(windows, test))]
use std::fs::{self, File};
#[cfg(any(windows, test))]
use std::io::{self, BufWriter, Write};
#[cfg(any(windows, test))]
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail};
use praxis_body_protocol::{AdapterDescriptor, CapabilityDescriptor};
use serde_json::Value;
#[cfg(any(windows, test))]
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NativeDesktopCapability {
    pub name: &'static str,
    pub version: u32,
    pub mutating: bool,
    pub durable: bool,
}

pub const CAPABILITIES: &[NativeDesktopCapability] = &[
    NativeDesktopCapability {
        name: "desktop.status",
        version: 1,
        mutating: false,
        durable: false,
    },
    NativeDesktopCapability {
        name: "os.process.list",
        version: 1,
        mutating: false,
        durable: false,
    },
    NativeDesktopCapability {
        name: "desktop.window.list",
        version: 1,
        mutating: false,
        durable: false,
    },
    NativeDesktopCapability {
        name: "desktop.window.activate",
        version: 1,
        mutating: true,
        durable: true,
    },
    // v2: у мыши появились раздельные нажатие и отпускание плюс составное
    // перетаскивание. До этого пара «нажал+отпустил» была неразрывной, и целый
    // класс действий — потащить файл, ползунок, границу окна, выделить текст
    // мышью — был ей физически недоступен, о чём манифест молчал.
    NativeDesktopCapability {
        name: "desktop.input.perform",
        version: 2,
        mutating: true,
        durable: true,
    },
    NativeDesktopCapability {
        name: "desktop.screen.capture",
        version: 2,
        mutating: false,
        durable: true,
    },
    NativeDesktopCapability {
        name: "desktop.clipboard.read",
        version: 1,
        mutating: false,
        durable: false,
    },
    NativeDesktopCapability {
        name: "desktop.clipboard.write",
        version: 1,
        mutating: true,
        durable: true,
    },
];

/// A provider-owned file result which the shared runtime should publish through
/// the content-addressed artifact transport.  Adding another desktop capture
/// format stays inside this module; transport and journaling do not learn a new
/// capability name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArtifactOutput {
    pub path: PathBuf,
    pub name: Option<String>,
    pub media_type: String,
    pub presentation: String,
}

pub fn artifact_output(capability: &str, result: &Value) -> Result<Option<ArtifactOutput>> {
    if capability != "desktop.screen.capture" || result.get("ok") != Some(&Value::Bool(true)) {
        return Ok(None);
    }
    let path = PathBuf::from(
        result
            .get("path")
            .and_then(Value::as_str)
            .context("desktop capture result has no artifact path")?,
    );
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .map(str::to_string);
    Ok(Some(ArtifactOutput {
        path,
        name,
        media_type: result
            .get("mime")
            .and_then(Value::as_str)
            .unwrap_or("image/png")
            .to_string(),
        presentation: "image".into(),
    }))
}

pub fn descriptors() -> Vec<CapabilityDescriptor> {
    CAPABILITIES
        .iter()
        .map(|capability| CapabilityDescriptor {
            name: capability.name.into(),
            version: capability.version,
            mutating: capability.mutating,
            durable: capability.durable,
        })
        .collect()
}

pub fn adapter_descriptor() -> AdapterDescriptor {
    AdapterDescriptor {
        name: "native-win32-desktop".into(),
        version: "3".into(),
        capabilities: CAPABILITIES
            .iter()
            .map(|capability| capability.name.to_string())
            .collect(),
        available: cfg!(windows),
    }
}

#[cfg(any(windows, test))]
const MAX_CAPTURE_PIXELS: u64 = 40_000_000;
#[cfg(any(windows, test))]
const MAX_CAPTURE_RAW_BYTES: usize = 128 * 1024 * 1024;
#[cfg(any(windows, test))]
const MAX_CAPTURE_PNG_BYTES: usize = 128 * 1024 * 1024;

#[cfg(any(windows, test))]
fn capture_allocation(width: i32, height: i32) -> Result<usize> {
    if width <= 0 || height <= 0 {
        bail!("capture rectangle must have positive width and height")
    }
    let pixels = u64::try_from(width)?
        .checked_mul(u64::try_from(height)?)
        .context("capture pixel count overflow")?;
    if pixels > MAX_CAPTURE_PIXELS {
        bail!("capture rectangle is too large: {pixels} pixels (maximum {MAX_CAPTURE_PIXELS})")
    }
    let raw_bytes = usize::try_from(pixels)?
        .checked_mul(4)
        .context("capture raw BGRA size overflow")?;
    if raw_bytes > MAX_CAPTURE_RAW_BYTES {
        bail!("capture requires {raw_bytes} raw BGRA bytes (maximum {MAX_CAPTURE_RAW_BYTES})")
    }
    Ok(raw_bytes)
}

#[cfg(any(windows, test))]
struct SizeLimitedWriter<W> {
    inner: W,
    written: usize,
    limit: usize,
}

#[cfg(any(windows, test))]
impl<W> SizeLimitedWriter<W> {
    fn new(inner: W, limit: usize) -> Self {
        Self {
            inner,
            written: 0,
            limit,
        }
    }
}

#[cfg(any(windows, test))]
impl<W: Write> Write for SizeLimitedWriter<W> {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        if buffer.len() > self.limit.saturating_sub(self.written) {
            return Err(io::Error::other(format!(
                "PNG exceeds {} bytes",
                self.limit
            )));
        }
        let count = self.inner.write(buffer)?;
        self.written = self
            .written
            .checked_add(count)
            .ok_or_else(|| io::Error::other("PNG byte count overflow"))?;
        Ok(count)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

#[cfg(any(windows, test))]
fn write_png(path: &Path, width: i32, height: i32, bgra: &[u8]) -> Result<()> {
    let expected = capture_allocation(width, height)?;
    if bgra.len() != expected {
        bail!(
            "capture buffer has {} bytes; expected {expected}",
            bgra.len()
        )
    }

    let temporary = path.with_file_name(format!(".praxis-capture-{}.png", Uuid::new_v4()));
    let encoded = (|| -> Result<()> {
        let output = BufWriter::new(File::create(&temporary)?);
        let output = SizeLimitedWriter::new(output, MAX_CAPTURE_PNG_BYTES);
        let mut encoder = png::Encoder::new(output, u32::try_from(width)?, u32::try_from(height)?);
        encoder.set_color(png::ColorType::Rgb);
        encoder.set_depth(png::BitDepth::Eight);
        encoder.set_compression(png::Compression::Fast);
        let mut writer = encoder.write_header().context("write PNG header")?;
        {
            let mut stream = writer
                .stream_writer_with_size(64 * 1024)
                .context("start PNG stream")?;
            let bgra_row_bytes = usize::try_from(width)?
                .checked_mul(4)
                .context("capture BGRA row size overflow")?;
            let rgb_row_bytes = usize::try_from(width)?
                .checked_mul(3)
                .context("capture RGB row size overflow")?;
            let mut rgb_row = vec![0u8; rgb_row_bytes];
            for bgra_row in bgra.chunks_exact(bgra_row_bytes) {
                for (source, target) in bgra_row.chunks_exact(4).zip(rgb_row.chunks_exact_mut(3)) {
                    target.copy_from_slice(&[source[2], source[1], source[0]]);
                }
                stream.write_all(&rgb_row).context("write PNG pixels")?;
            }
            stream.finish().context("finish PNG pixel stream")?;
        }
        writer.finish().context("finish PNG")?;
        Ok(())
    })();
    if let Err(error) = encoded {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }

    let size = fs::metadata(&temporary)?.len();
    if size > MAX_CAPTURE_PNG_BYTES as u64 {
        let _ = fs::remove_file(&temporary);
        bail!("captured PNG exceeded {MAX_CAPTURE_PNG_BYTES} bytes")
    }
    let committed = crate::fsops::replace_path(&temporary, path);
    if committed.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    committed
}

#[cfg(any(windows, test))]
fn capture_name(requested: &str) -> String {
    let mut filtered: String = requested
        .chars()
        .filter(|value| value.is_alphanumeric() || matches!(value, '-' | '_' | '.'))
        .collect();
    for suffix in [".png", ".bmp"] {
        if filtered.to_ascii_lowercase().ends_with(suffix) {
            filtered.truncate(filtered.len() - suffix.len());
            break;
        }
    }
    let mut name = String::new();
    let mut utf16_units = 0usize;
    for value in filtered.trim_matches('.').chars() {
        let units = value.len_utf16();
        if utf16_units + units > 200 {
            break;
        }
        name.push(value);
        utf16_units += units;
    }
    let stem = name
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_uppercase();
    let reserved = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || stem
            .strip_prefix("COM")
            .and_then(|value| value.parse::<u8>().ok())
            .is_some_and(|value| (1..=9).contains(&value))
        || stem
            .strip_prefix("LPT")
            .and_then(|value| value.parse::<u8>().ok())
            .is_some_and(|value| (1..=9).contains(&value));
    if reserved {
        name.insert_str(0, "capture-");
    }
    if name.is_empty() {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        name = format!("capture-{}-{stamp}.png", std::process::id());
    } else {
        name.push_str(".png");
    }
    name
}

pub fn dispatch(capability: &str, args: Value, state_dir: &Path) -> Result<Value> {
    let Ok(handle) = tokio::runtime::Handle::try_current() else {
        return platform::dispatch(capability, args, state_dir);
    };
    if handle.runtime_flavor() != tokio::runtime::RuntimeFlavor::MultiThread {
        bail!("native desktop dispatch requires a multi-thread Tokio runtime")
    }
    let capability = capability.to_string();
    let state_dir = PathBuf::from(state_dir);
    tokio::task::block_in_place(|| {
        handle.block_on(async move {
            tokio::task::spawn_blocking(move || platform::dispatch(&capability, args, &state_dir))
                .await
                .context("native desktop worker stopped")?
        })
    })
}

#[cfg(not(windows))]
mod platform {
    use std::path::Path;

    use anyhow::{Result, bail};
    use serde_json::Value;

    pub fn dispatch(_capability: &str, _args: Value, _state_dir: &Path) -> Result<Value> {
        bail!("native desktop capabilities require an interactive Windows session")
    }
}

#[cfg(windows)]
mod platform {
    use std::ffi::c_void;
    use std::fs;
    use std::mem::size_of;
    use std::path::Path;
    use std::ptr;
    use std::thread;
    use std::time::Duration;

    use anyhow::{Context, Result, anyhow, bail};
    use serde::Deserialize;
    use serde_json::{Value, json};
    use windows::Win32::Foundation::{
        CloseHandle, GlobalFree, HANDLE, HGLOBAL, HWND, LPARAM, POINT, RECT,
    };
    use windows::Win32::Graphics::Gdi::{
        BI_RGB, BITMAPINFO, BitBlt, CAPTUREBLT, CreateCompatibleBitmap, CreateCompatibleDC,
        DIB_RGB_COLORS, DeleteDC, DeleteObject, GetDC, GetDIBits, HBITMAP, HGDIOBJ, ReleaseDC,
        SRCCOPY, SelectObject,
    };
    use windows::Win32::System::DataExchange::{
        CloseClipboard, EmptyClipboard, GetClipboardData, GetClipboardSequenceNumber,
        IsClipboardFormatAvailable, OpenClipboard, SetClipboardData,
    };
    use windows::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW,
        TH32CS_SNAPPROCESS,
    };
    use windows::Win32::System::Memory::{
        GMEM_MOVEABLE, GlobalAlloc, GlobalLock, GlobalSize, GlobalUnlock,
    };
    use windows::Win32::System::RemoteDesktop::ProcessIdToSessionId;
    use windows::Win32::System::Threading::{
        AttachThreadInput, GetCurrentProcessId, GetCurrentThreadId, GetProcessTimes, OpenProcess,
        PROCESS_QUERY_LIMITED_INFORMATION, QueryFullProcessImageNameW,
    };
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE, KEYBD_EVENT_FLAGS, KEYBDINPUT,
        KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, MOUSE_EVENT_FLAGS, MOUSEEVENTF_ABSOLUTE,
        MOUSEEVENTF_HWHEEL, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MIDDLEDOWN,
        MOUSEEVENTF_MIDDLEUP, MOUSEEVENTF_MOVE, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP,
        MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_WHEEL, MOUSEINPUT, SendInput, VIRTUAL_KEY,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        BringWindowToTop, EnumWindows, GetClassNameW, GetCursorPos, GetForegroundWindow,
        GetSystemMetrics, GetWindowRect, GetWindowTextLengthW, GetWindowTextW,
        GetWindowThreadProcessId, IsIconic, IsWindow, IsWindowVisible, SM_CXVIRTUALSCREEN,
        SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SW_RESTORE, SetForegroundWindow,
        ShowWindowAsync,
    };
    use windows::core::{BOOL, PWSTR};

    use super::{capture_allocation, capture_name, write_png};

    const CF_UNICODETEXT: u32 = 13;
    const DEFAULT_PAGE: usize = 2_048;
    const MAX_PAGE: usize = 20_000;
    const DEFAULT_CLIPBOARD_CHARS: usize = 1_000_000;
    const MAX_CLIPBOARD_CHARS: usize = 1_000_000;
    const MAX_INPUT_EVENTS: usize = 512;
    const MAX_INPUT_TEXT_UTF16_UNITS: usize = 16_384;
    const MAX_HOTKEY_KEYS: usize = 32;
    const MAX_CLICK_COUNT: u32 = 64;
    const MAX_INPUT_RECORDS: usize = 65_536;
    // body_client waits 60 seconds; leave room for the native call and reply.
    const MAX_TOTAL_INPUT_DELAY_MS: u64 = 30_000;
    // Перетаскивание. Проводник, ползунки и выделение текста не считают за drag
    // «телепорт» курсора: им нужны промежуточные MOUSEMOVE и живая пауза после
    // нажатия. Поэтому один шаг drag разворачивается в несколько отправок с
    // паузами — и у каждой паузы есть свой потолок, а их сумма едет в тот же
    // общий бюджет MAX_TOTAL_INPUT_DELAY_MS, а не мимо него.
    const MAX_DRAG_STEPS: u32 = 256;
    const DEFAULT_DRAG_STEPS: u32 = 24;
    const MAX_DRAG_PAUSE_MS: u64 = 5_000;
    const DEFAULT_DRAG_HOLD_MS: u64 = 60;
    const DEFAULT_DRAG_STEP_DELAY_MS: u64 = 8;
    const DEFAULT_DRAG_SETTLE_MS: u64 = 60;

    #[derive(Debug, Clone, Deserialize)]
    #[serde(untagged)]
    enum HwndArg {
        Number(u64),
        Text(String),
    }

    impl HwndArg {
        fn value(&self) -> Result<HWND> {
            let value = match self {
                Self::Number(value) => *value,
                Self::Text(value) => {
                    let value = value.trim();
                    if let Some(hex) = value
                        .strip_prefix("0x")
                        .or_else(|| value.strip_prefix("0X"))
                    {
                        u64::from_str_radix(hex, 16)
                            .context("hwnd must be a positive integer or hexadecimal string")?
                    } else {
                        value.parse::<u64>().context(
                            "hwnd must be a positive integer or 0x-prefixed hexadecimal string",
                        )?
                    }
                }
            };
            if value == 0 || value > usize::MAX as u64 {
                bail!("invalid hwnd")
            }
            Ok(HWND(value as usize as *mut c_void))
        }
    }

    #[derive(Debug, Default, Deserialize)]
    struct PageArgs {
        #[serde(default)]
        offset: usize,
        #[serde(default = "default_page")]
        limit: usize,
    }

    fn default_page() -> usize {
        DEFAULT_PAGE
    }

    #[derive(Debug, Default, Deserialize)]
    struct ProcessListArgs {
        #[serde(flatten)]
        page: PageArgs,
        #[serde(default)]
        name_contains: String,
        #[serde(default)]
        session_id: Option<u32>,
    }

    #[derive(Debug, Deserialize)]
    struct WindowListArgs {
        #[serde(flatten)]
        page: PageArgs,
        #[serde(default = "yes")]
        visible_only: bool,
        #[serde(default)]
        pid: Option<u32>,
        #[serde(default)]
        title_contains: String,
    }

    impl Default for WindowListArgs {
        fn default() -> Self {
            Self {
                page: PageArgs::default(),
                visible_only: true,
                pid: None,
                title_contains: String::new(),
            }
        }
    }

    fn yes() -> bool {
        true
    }

    #[derive(Debug, Deserialize)]
    struct ActivateArgs {
        hwnd: HwndArg,
        #[serde(default)]
        expected_pid: Option<u32>,
        #[serde(default = "yes")]
        restore: bool,
        #[serde(default = "activate_timeout")]
        timeout_ms: u64,
    }

    fn activate_timeout() -> u64 {
        1_500
    }

    #[derive(Debug, Deserialize)]
    struct InputArgs {
        #[serde(default)]
        expected_foreground: Option<HwndArg>,
        #[serde(default)]
        expected_pid: Option<u32>,
        events: Vec<InputEvent>,
        #[serde(default)]
        inter_event_delay_ms: u64,
    }

    #[derive(Debug, Deserialize)]
    #[serde(tag = "type", rename_all = "snake_case")]
    pub(super) enum InputEvent {
        Text {
            text: String,
        },
        Hotkey {
            keys: Vec<KeyArg>,
        },
        Key {
            key: KeyArg,
            #[serde(default = "press_action")]
            action: String,
        },
        Mouse {
            x: i32,
            y: i32,
            #[serde(default)]
            relative: bool,
        },
        Click {
            #[serde(default = "left_button")]
            button: String,
            #[serde(default)]
            x: Option<i32>,
            #[serde(default)]
            y: Option<i32>,
            #[serde(default = "one")]
            count: u32,
        },
        Wheel {
            delta: i32,
            #[serde(default)]
            horizontal: bool,
            #[serde(default)]
            x: Option<i32>,
            #[serde(default)]
            y: Option<i32>,
        },
        /// Зажать кнопку и оставить зажатой до следующих шагов ЭТОЙ ЖЕ пачки.
        /// Из вызова зажатие не выходит: см. `HeldButtons`.
        MouseDown {
            #[serde(default = "left_button")]
            button: String,
            #[serde(default)]
            x: Option<i32>,
            #[serde(default)]
            y: Option<i32>,
        },
        /// Отпустить кнопку. Отпускание кнопки, которая не была зажата, — не ошибка:
        /// это её ручной способ снять залипание, оставшееся от оборванного вызова.
        MouseUp {
            #[serde(default = "left_button")]
            button: String,
            #[serde(default)]
            x: Option<i32>,
            #[serde(default)]
            y: Option<i32>,
        },
        /// «Нажал → провёл → отпустил» одним шагом: файл в проводнике, ползунок,
        /// граница окна, выделение текста по диагонали.
        ///
        /// Начало (`x`,`y`) обязательно и явно: курсор мог быть сдвинут предыдущим
        /// шагом этой же пачки, и «тащить от того места, где он сейчас» означало бы
        /// считать интерполяцию от точки, которой она не видела.
        Drag {
            #[serde(default = "left_button")]
            button: String,
            x: i32,
            y: i32,
            to_x: i32,
            to_y: i32,
            #[serde(default = "default_drag_steps")]
            steps: u32,
            #[serde(default = "default_drag_hold_ms")]
            hold_ms: u64,
            #[serde(default = "default_drag_step_delay_ms")]
            step_delay_ms: u64,
            #[serde(default = "default_drag_settle_ms")]
            settle_ms: u64,
        },
    }

    #[derive(Debug, Clone, Deserialize)]
    #[serde(untagged)]
    pub(super) enum KeyArg {
        Number(u16),
        Text(String),
    }

    /// Кнопка мыши как состояние, а не как неразрывная пара флагов.
    /// Пока нажатие и отпускание жили только внутри `click_inputs`, зажатия
    /// не существовало — а значит, не существовало и перетаскивания.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub(super) enum MouseButton {
        Left,
        Right,
        Middle,
    }

    impl MouseButton {
        fn parse(raw: &str) -> Result<Self> {
            match raw.trim().to_ascii_lowercase().as_str() {
                "left" => Ok(Self::Left),
                "right" => Ok(Self::Right),
                "middle" => Ok(Self::Middle),
                _ => bail!("button must be left, right, or middle"),
            }
        }

        fn name(self) -> &'static str {
            match self {
                Self::Left => "left",
                Self::Right => "right",
                Self::Middle => "middle",
            }
        }

        fn down(self) -> MOUSE_EVENT_FLAGS {
            match self {
                Self::Left => MOUSEEVENTF_LEFTDOWN,
                Self::Right => MOUSEEVENTF_RIGHTDOWN,
                Self::Middle => MOUSEEVENTF_MIDDLEDOWN,
            }
        }

        fn up(self) -> MOUSE_EVENT_FLAGS {
            match self {
                Self::Left => MOUSEEVENTF_LEFTUP,
                Self::Right => MOUSEEVENTF_RIGHTUP,
                Self::Middle => MOUSEEVENTF_MIDDLEUP,
            }
        }
    }

    fn press_action() -> String {
        "press".into()
    }

    fn left_button() -> String {
        "left".into()
    }

    fn one() -> u32 {
        1
    }

    fn default_drag_steps() -> u32 {
        DEFAULT_DRAG_STEPS
    }

    fn default_drag_hold_ms() -> u64 {
        DEFAULT_DRAG_HOLD_MS
    }

    fn default_drag_step_delay_ms() -> u64 {
        DEFAULT_DRAG_STEP_DELAY_MS
    }

    fn default_drag_settle_ms() -> u64 {
        DEFAULT_DRAG_SETTLE_MS
    }

    #[derive(Debug, Deserialize)]
    struct CaptureArgs {
        #[serde(default = "desktop_target")]
        target: String,
        #[serde(default)]
        hwnd: Option<HwndArg>,
        #[serde(default)]
        x: Option<i32>,
        #[serde(default)]
        y: Option<i32>,
        #[serde(default)]
        width: Option<i32>,
        #[serde(default)]
        height: Option<i32>,
        #[serde(default)]
        name: String,
    }

    #[derive(Debug, Clone, Copy)]
    struct CaptureRegion {
        left: i32,
        top: i32,
        width: i32,
        height: i32,
    }

    fn desktop_target() -> String {
        "desktop".into()
    }

    #[derive(Debug, Deserialize)]
    struct ClipboardReadArgs {
        #[serde(default = "default_clipboard_chars")]
        limit_chars: usize,
    }

    fn default_clipboard_chars() -> usize {
        DEFAULT_CLIPBOARD_CHARS
    }

    #[derive(Debug, Deserialize)]
    struct ClipboardWriteArgs {
        text: String,
    }

    #[derive(Debug)]
    struct ProcessDetails {
        path: Option<String>,
        created_filetime: Option<u64>,
        session_id: Option<u32>,
    }

    struct HandleGuard(HANDLE);

    impl Drop for HandleGuard {
        fn drop(&mut self) {
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }

    struct ClipboardGuard;

    impl Drop for ClipboardGuard {
        fn drop(&mut self) {
            unsafe {
                let _ = CloseClipboard();
            }
        }
    }

    /// Temporarily join the input queues involved in foreground arbitration.
    ///
    /// A tray/background process is normally forbidden from stealing foreground even when the
    /// owner explicitly asked it to activate a window.  Joining the existing foreground and
    /// target queues is the documented Win32 handshake; it avoids a blind synthetic Alt press
    /// and is always undone before returning.
    struct ThreadInputAttachments {
        current: u32,
        attached: Vec<u32>,
    }

    impl ThreadInputAttachments {
        fn new() -> Self {
            Self {
                current: unsafe { GetCurrentThreadId() },
                attached: Vec::new(),
            }
        }

        fn attach(&mut self, other: u32) -> bool {
            if other == 0 || other == self.current || self.attached.contains(&other) {
                return other == self.current || self.attached.contains(&other);
            }
            if unsafe { AttachThreadInput(self.current, other, true) }.as_bool() {
                self.attached.push(other);
                true
            } else {
                false
            }
        }
    }

    impl Drop for ThreadInputAttachments {
        fn drop(&mut self) {
            for other in self.attached.iter().rev() {
                unsafe {
                    let _ = AttachThreadInput(self.current, *other, false);
                }
            }
        }
    }

    struct WindowCollector {
        foreground: HWND,
        visible_only: bool,
        pid: Option<u32>,
        title_contains: String,
        rows: Vec<Value>,
    }

    pub fn dispatch(capability: &str, args: Value, state_dir: &Path) -> Result<Value> {
        match capability {
            "desktop.status" => desktop_status(),
            "os.process.list" => process_list(serde_json::from_value(args)?),
            "desktop.window.list" => window_list(serde_json::from_value(args)?),
            "desktop.window.activate" => window_activate(serde_json::from_value(args)?),
            "desktop.input.perform" => input_perform(serde_json::from_value(args)?),
            "desktop.screen.capture" => screen_capture(serde_json::from_value(args)?, state_dir),
            "desktop.clipboard.read" => clipboard_read(serde_json::from_value(args)?),
            "desktop.clipboard.write" => clipboard_write(serde_json::from_value(args)?),
            _ => bail!("unknown native desktop capability {capability}"),
        }
    }

    fn desktop_status() -> Result<Value> {
        let foreground = unsafe { GetForegroundWindow() };
        let mut cursor = POINT::default();
        let cursor = unsafe { GetCursorPos(&mut cursor) }
            .ok()
            .map(|_| json!({"x": cursor.x, "y": cursor.y}));
        let session_id = process_session(unsafe { GetCurrentProcessId() });
        Ok(json!({
            "ok": true,
            "interactive": session_id.is_some_and(|value| value != 0),
            "session_id": session_id,
            "foreground": window_row(foreground, 0),
            "cursor": cursor,
            "virtual_screen": virtual_screen(),
            "clipboard_sequence": unsafe { GetClipboardSequenceNumber() },
        }))
    }

    fn process_list(args: ProcessListArgs) -> Result<Value> {
        let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)? };
        let _snapshot = HandleGuard(snapshot);
        let mut entry = PROCESSENTRY32W {
            dwSize: size_of::<PROCESSENTRY32W>() as u32,
            ..Default::default()
        };
        let mut rows = Vec::new();
        let mut more = unsafe { Process32FirstW(snapshot, &mut entry) }.is_ok();
        let needle = args.name_contains.to_lowercase();
        while more {
            let name = utf16_z(&entry.szExeFile);
            let details = process_details(entry.th32ProcessID);
            let matches_name = needle.is_empty()
                || name.to_lowercase().contains(&needle)
                || details
                    .path
                    .as_deref()
                    .is_some_and(|value| value.to_lowercase().contains(&needle));
            let matches_session = args
                .session_id
                .is_none_or(|value| details.session_id == Some(value));
            if matches_name && matches_session {
                rows.push(json!({
                    "pid": entry.th32ProcessID,
                    "parent_pid": entry.th32ParentProcessID,
                    "threads": entry.cntThreads,
                    "name": name,
                    "path": details.path,
                    "session_id": details.session_id,
                    "created_filetime": details.created_filetime,
                }));
            }
            more = unsafe { Process32NextW(snapshot, &mut entry) }.is_ok();
        }
        rows.sort_by_key(|row| row["pid"].as_u64().unwrap_or_default());
        Ok(page(rows, args.page))
    }

    fn window_list(args: WindowListArgs) -> Result<Value> {
        let mut collector = WindowCollector {
            foreground: unsafe { GetForegroundWindow() },
            visible_only: args.visible_only,
            pid: args.pid,
            title_contains: args.title_contains.to_lowercase(),
            rows: Vec::new(),
        };
        unsafe {
            EnumWindows(
                Some(enum_window),
                LPARAM(&mut collector as *mut WindowCollector as isize),
            )?;
        }
        let mut result = page(collector.rows, args.page);
        result["foreground_hwnd"] = Value::String(hwnd_hex(collector.foreground));
        Ok(result)
    }

    unsafe extern "system" fn enum_window(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let collector = unsafe { &mut *(lparam.0 as *mut WindowCollector) };
        let visible = unsafe { IsWindowVisible(hwnd) }.as_bool();
        if collector.visible_only && !visible {
            return BOOL(1);
        }
        let mut pid = 0u32;
        unsafe { GetWindowThreadProcessId(hwnd, Some(&mut pid)) };
        if collector.pid.is_some_and(|expected| expected != pid) {
            return BOOL(1);
        }
        let title = window_text(hwnd);
        if !collector.title_contains.is_empty()
            && !title.to_lowercase().contains(&collector.title_contains)
        {
            return BOOL(1);
        }
        let z_order = collector.rows.len();
        if let Some(mut row) = window_row(hwnd, z_order) {
            row["visible"] = Value::Bool(visible);
            row["foreground"] = Value::Bool(hwnd == collector.foreground);
            collector.rows.push(row);
        }
        BOOL(1)
    }

    fn window_activate(args: ActivateArgs) -> Result<Value> {
        let hwnd = args.hwnd.value()?;
        require_window(hwnd, args.expected_pid)?;
        if args.timeout_ms > 15_000 {
            bail!("activation timeout must not exceed 15000ms")
        }
        let foreground_before = unsafe { GetForegroundWindow() };
        let foreground_thread = if foreground_before.0.is_null() {
            0
        } else {
            unsafe { GetWindowThreadProcessId(foreground_before, None) }
        };
        let target_thread = unsafe { GetWindowThreadProcessId(hwnd, None) };
        let mut attachments = ThreadInputAttachments::new();
        let attached_foreground = attachments.attach(foreground_thread);
        let attached_target = attachments.attach(target_thread);
        if args.restore && unsafe { IsIconic(hwnd) }.as_bool() {
            unsafe {
                let _ = ShowWindowAsync(hwnd, SW_RESTORE);
            }
        }
        let mut raised = unsafe { BringWindowToTop(hwnd) }.is_ok();
        let mut requested = unsafe { SetForegroundWindow(hwnd) }.as_bool();
        let mut attempts = 1u32;
        let timeout = args.timeout_ms;
        let mut waited = 0u64;
        while unsafe { GetForegroundWindow() } != hwnd && waited < timeout {
            thread::sleep(Duration::from_millis(25));
            waited += 25;
            if waited.is_multiple_of(250) {
                raised |= unsafe { BringWindowToTop(hwnd) }.is_ok();
                requested |= unsafe { SetForegroundWindow(hwnd) }.as_bool();
                attempts += 1;
            }
        }
        let actual = unsafe { GetForegroundWindow() };
        Ok(json!({
            "ok": actual == hwnd,
            "requested_hwnd": hwnd_hex(hwnd),
            "foreground_before": hwnd_hex(foreground_before),
            "foreground_hwnd": hwnd_hex(actual),
            "set_foreground_returned": requested,
            "brought_to_top": raised,
            "current_thread": attachments.current,
            "foreground_thread": foreground_thread,
            "target_thread": target_thread,
            "attached_foreground": attached_foreground,
            "attached_target": attached_target,
            "attempts": attempts,
            "waited_ms": waited,
        }))
    }

    /// Одна отправка в `SendInput` плюс пауза после неё.
    ///
    /// Шаг ввода больше не равен одной отправке: перетаскивание разворачивается в
    /// «нажал → n промежуточных сдвигов → отпустил» с живыми паузами внутри шага.
    /// `press`/`release` — то, что эта пачка делает с состоянием кнопок; по ним
    /// ведётся ведомость зажатого.
    struct PreparedChunk {
        inputs: Vec<INPUT>,
        pause_ms: u64,
        press: Option<MouseButton>,
        release: Option<MouseButton>,
        clamped_moves: usize,
    }

    /// Всё, что известно о пачке ДО первой отправки. Живой ход и тест считают это
    /// одной и той же функцией, чтобы числа в ответе не расходились с реальностью.
    pub(super) struct PlanTotals {
        pub(super) batches: usize,
        pub(super) records: usize,
        pub(super) pause_ms: u64,
        pub(super) clamped_moves: usize,
    }

    /// Ведомость зажатых кнопок.
    ///
    /// Зажатие переживает отдельную отправку — в этом весь смысл раздельных
    /// `mouse_down`/`mouse_up`. Но именно поэтому любой обрыв между ними (отказ
    /// `ensure_foreground` на следующем шаге, UIPI на середине пачки, паника)
    /// оставил бы рабочий стол Егора с намертво зажатой кнопкой и неуправляемым.
    /// Ведомость гасит это на выходе из обработчика, а `Drop` — последний рубеж.
    #[derive(Default)]
    struct HeldButtons {
        held: Vec<MouseButton>,
    }

    impl HeldButtons {
        fn press(&mut self, button: MouseButton) {
            if !self.held.contains(&button) {
                self.held.push(button);
            }
        }

        fn release(&mut self, button: MouseButton) {
            self.held.retain(|value| *value != button);
        }

        /// Отпускает всё, что осталось зажатым, и возвращает имена — чтобы ответ
        /// назвал вслух правку состояния мыши, которую сделал не её шаг, а тело.
        fn release_all(&mut self) -> Vec<&'static str> {
            if self.held.is_empty() {
                return Vec::new();
            }
            let names: Vec<&'static str> = self.held.iter().map(|button| button.name()).collect();
            let inputs: Vec<INPUT> = self
                .held
                .iter()
                .rev()
                .map(|button| mouse_input(0, 0, 0, button.up()))
                .collect();
            self.held.clear();
            // Отпускаем без права на отказ: если и это не пустил UIPI, сказать об
            // этом можно только текстом ответа — но не удержанием кнопки.
            let _ = send_inputs(&inputs);
            names
        }
    }

    impl Drop for HeldButtons {
        fn drop(&mut self) {
            let _ = self.release_all();
        }
    }

    fn input_perform(args: InputArgs) -> Result<Value> {
        if args.events.is_empty() {
            bail!("events must not be empty")
        }
        if args.events.len() > MAX_INPUT_EVENTS {
            bail!(
                "too many input events: {} (maximum {MAX_INPUT_EVENTS})",
                args.events.len()
            )
        }
        // Prepare and validate the entire batch before the first mutation.  A
        // malformed late event must not leave a half-applied automation step.
        let chunks = prepare_input_events(&args.events, args.inter_event_delay_ms)?;
        let totals = plan_totals(&chunks)?;
        if totals.pause_ms > MAX_TOTAL_INPUT_DELAY_MS {
            bail!(
                "total input delay is {}ms (maximum {MAX_TOTAL_INPUT_DELAY_MS}ms)",
                totals.pause_ms
            )
        }
        let before = ensure_foreground(args.expected_foreground.as_ref(), args.expected_pid)?;
        let mut held = HeldButtons::default();
        let mut paused_ms = 0u64;
        let outcome = (|| -> Result<()> {
            for (index, chunk) in chunks.iter().enumerate() {
                ensure_foreground(args.expected_foreground.as_ref(), args.expected_pid)?;
                // Помечаем зажатой ДО отправки: `SendInput` умеет вставить нажатие и
                // упереться на отпускании, и тогда кнопка уже зажата, а мы бы о ней
                // не знали.
                if let Some(button) = chunk.press {
                    held.press(button);
                }
                send_inputs(&chunk.inputs)?;
                if let Some(button) = chunk.release {
                    held.release(button);
                }
                if chunk.pause_ms > 0 && index + 1 < chunks.len() {
                    thread::sleep(Duration::from_millis(chunk.pause_ms));
                    paused_ms = paused_ms.saturating_add(chunk.pause_ms);
                }
            }
            Ok(())
        })();
        let auto_released = held.release_all();
        if let Err(error) = outcome {
            if auto_released.is_empty() {
                return Err(error);
            }
            // Ошибку показывают целиком (`{:#}` — вся цепочка), плюс правду о том,
            // что стол оставлен без зажатых кнопок: иначе она бы гадала, чинить ли
            // мышь вручную.
            return Err(anyhow!(
                "{error:#}; released still-held mouse buttons before returning: {}",
                auto_released.join(", ")
            ));
        }
        let after = unsafe { GetForegroundWindow() };
        Ok(json!({
            "ok": true,
            "events": args.events.len(),
            "input_batches": totals.batches,
            "input_records": totals.records,
            "planned_pause_ms": totals.pause_ms,
            "paused_ms": paused_ms,
            "clamped_moves": totals.clamped_moves,
            "buttons_auto_released": auto_released,
            "buttons_held_at_exit": Vec::<&str>::new(),
            "foreground_before": hwnd_hex(before),
            "foreground_after": hwnd_hex(after),
            "virtual_screen": virtual_screen(),
            "limits": input_limits(),
        }))
    }

    /// Пределы ввода едут в КАЖДОМ ответе. Молчаливый предел — та же ложь, просто
    /// отложенная до момента удара: она узнавала о капе из текста ошибки.
    fn input_limits() -> Value {
        json!({
            "max_events": MAX_INPUT_EVENTS,
            "max_input_records": MAX_INPUT_RECORDS,
            "max_text_utf16_units": MAX_INPUT_TEXT_UTF16_UNITS,
            "max_hotkey_keys": MAX_HOTKEY_KEYS,
            "max_click_count": MAX_CLICK_COUNT,
            "max_total_delay_ms": MAX_TOTAL_INPUT_DELAY_MS,
            "max_drag_steps": MAX_DRAG_STEPS,
            "default_drag_steps": DEFAULT_DRAG_STEPS,
            "max_drag_pause_ms": MAX_DRAG_PAUSE_MS,
            "default_drag_pauses_ms": {
                "hold": DEFAULT_DRAG_HOLD_MS,
                "step_delay": DEFAULT_DRAG_STEP_DELAY_MS,
                "settle": DEFAULT_DRAG_SETTLE_MS,
            },
            "coordinates": "absolute x/y are clamped into virtual_screen; clamped_moves says how many were pulled to the edge",
            "button_hold": "a held mouse button never survives the call: whatever this batch leaves down is released before returning and named in buttons_auto_released",
            // Раньше сторож фокуса срабатывал раз на шаг; теперь шаг drag — это 26 отправок,
            // и сторож стоит перед КАЖДОЙ. Само по себе это правильно (стол мог уехать
            // посреди перетаскивания), но пока об этом молчали, отказ «foreground changed»
            // посреди drag выглядел бы необъяснимым: она передала сторожа один раз, а
            // сорвалось на двадцатой отправке.
            "focus_guard": "expected_foreground/expected_pid are re-checked before EVERY batch, so a drag is aborted mid-way if the foreground moves; the held button is released and named in the error",
        })
    }

    fn plan_totals(chunks: &[PreparedChunk]) -> Result<PlanTotals> {
        let mut records = 0usize;
        let mut pause_ms = 0u64;
        let mut clamped_moves = 0usize;
        for chunk in chunks {
            records = records
                .checked_add(chunk.inputs.len())
                .context("input record count overflow")?;
            pause_ms = pause_ms
                .checked_add(chunk.pause_ms)
                .context("input delay overflow")?;
            clamped_moves = clamped_moves
                .checked_add(chunk.clamped_moves)
                .context("clamped move count overflow")?;
        }
        Ok(PlanTotals {
            batches: chunks.len(),
            records,
            pause_ms,
            clamped_moves,
        })
    }

    /// Разложить пачку в отправки, ничего не отправляя. Тесты ходят сюда, чтобы
    /// проверять арифметику пауз и капы, не трогая настоящую мышь Егора.
    #[cfg(test)]
    pub(super) fn plan_summary(
        events: &[InputEvent],
        inter_event_delay_ms: u64,
    ) -> Result<PlanTotals> {
        plan_totals(&prepare_input_events(events, inter_event_delay_ms)?)
    }

    fn prepare_input_events(
        events: &[InputEvent],
        inter_event_delay_ms: u64,
    ) -> Result<Vec<PreparedChunk>> {
        let mut total = 0usize;
        let mut chunks: Vec<PreparedChunk> = Vec::with_capacity(events.len());
        for (index, event) in events.iter().enumerate() {
            let produced = event_chunks(event)?;
            for chunk in &produced {
                total = total
                    .checked_add(chunk.inputs.len())
                    .context("input record count overflow")?;
                if total > MAX_INPUT_RECORDS {
                    bail!("input batch expands to {total} records (maximum {MAX_INPUT_RECORDS})")
                }
            }
            chunks.extend(produced);
            if index + 1 < events.len()
                && inter_event_delay_ms > 0
                && let Some(last) = chunks.last_mut()
            {
                last.pause_ms = last
                    .pause_ms
                    .checked_add(inter_event_delay_ms)
                    .context("input delay overflow")?;
            }
        }
        Ok(chunks)
    }

    /// Что шаг делает с состоянием кнопок: (что нажал, что отпустил).
    /// Один источник правды и для живой ведомости, и для предсказания `net_held`.
    fn event_button_effect(event: &InputEvent) -> Result<(Option<MouseButton>, Option<MouseButton>)> {
        Ok(match event {
            InputEvent::Click { button, .. } | InputEvent::Drag { button, .. } => {
                let button = MouseButton::parse(button)?;
                (Some(button), Some(button))
            }
            InputEvent::MouseDown { button, .. } => (Some(MouseButton::parse(button)?), None),
            InputEvent::MouseUp { button, .. } => (None, Some(MouseButton::parse(button)?)),
            _ => (None, None),
        })
    }

    /// Что останется зажатым, если все шаги дойдут до стола целиком. Живой цикл
    /// ведёт ту же ведомость по фактическим отправкам; здесь она считается наперёд,
    /// чтобы тест мог проверить арифметику без единого касания настоящей мыши.
    #[cfg(test)]
    pub(super) fn net_held(events: &[InputEvent]) -> Result<Vec<&'static str>> {
        let mut held = HeldButtons::default();
        for event in events {
            let (press, release) = event_button_effect(event)?;
            if let Some(button) = press {
                held.press(button);
            }
            if let Some(button) = release {
                held.release(button);
            }
        }
        // Забираем список, не отпуская: `Drop` не должен слать ввод из подсчёта.
        Ok(std::mem::take(&mut held.held)
            .into_iter()
            .map(|button| button.name())
            .collect())
    }

    fn event_chunks(event: &InputEvent) -> Result<Vec<PreparedChunk>> {
        if let InputEvent::Drag {
            button,
            x,
            y,
            to_x,
            to_y,
            steps,
            hold_ms,
            step_delay_ms,
            settle_ms,
        } = event
        {
            return drag_chunks(
                MouseButton::parse(button)?,
                (*x, *y),
                (*to_x, *to_y),
                *steps,
                (*hold_ms, *step_delay_ms, *settle_ms),
            );
        }
        let (press, release) = event_button_effect(event)?;
        let mut clamped_moves = 0usize;
        let inputs = event_inputs(event, &mut clamped_moves)?;
        Ok(vec![PreparedChunk {
            inputs,
            pause_ms: 0,
            press,
            release,
            clamped_moves,
        }])
    }

    fn drag_chunks(
        button: MouseButton,
        from: (i32, i32),
        to: (i32, i32),
        steps: u32,
        pauses: (u64, u64, u64),
    ) -> Result<Vec<PreparedChunk>> {
        if !(1..=MAX_DRAG_STEPS).contains(&steps) {
            bail!("drag steps must be between 1 and {MAX_DRAG_STEPS}")
        }
        let (hold_ms, step_delay_ms, settle_ms) = pauses;
        for (name, value) in [
            ("hold_ms", hold_ms),
            ("step_delay_ms", step_delay_ms),
            ("settle_ms", settle_ms),
        ] {
            if value > MAX_DRAG_PAUSE_MS {
                bail!("drag {name} is {value}ms (maximum {MAX_DRAG_PAUSE_MS}ms per pause)")
            }
        }
        let mut chunks = Vec::with_capacity(steps as usize + 2);
        let mut clamped_moves = 0usize;
        let mut press = vec![absolute_move(from.0, from.1, &mut clamped_moves)?];
        press.push(mouse_input(0, 0, 0, button.down()));
        chunks.push(PreparedChunk {
            inputs: press,
            pause_ms: hold_ms,
            press: Some(button),
            release: None,
            clamped_moves,
        });
        for step in 1..=steps {
            // Линейная интерполяция в i64: экраны бывают с отрицательным началом,
            // а произведение размаха на номер шага легко вылезает из i32.
            let point = |start: i32, end: i32| -> i32 {
                let span = i64::from(end) - i64::from(start);
                (i64::from(start) + span * i64::from(step) / i64::from(steps)) as i32
            };
            let mut clamped_moves = 0usize;
            let input = absolute_move(point(from.0, to.0), point(from.1, to.1), &mut clamped_moves)?;
            chunks.push(PreparedChunk {
                inputs: vec![input],
                pause_ms: if step == steps { settle_ms } else { step_delay_ms },
                press: None,
                release: None,
                clamped_moves,
            });
        }
        chunks.push(PreparedChunk {
            inputs: vec![mouse_input(0, 0, 0, button.up())],
            pause_ms: 0,
            press: None,
            release: Some(button),
            clamped_moves: 0,
        });
        Ok(chunks)
    }

    fn event_inputs(event: &InputEvent, clamped_moves: &mut usize) -> Result<Vec<INPUT>> {
        match event {
            InputEvent::Text { text } => text_inputs(text),
            InputEvent::Hotkey { keys } => hotkey_inputs(keys),
            InputEvent::Key { key, action } => key_action_inputs(key, action),
            InputEvent::Mouse { x, y, relative } => {
                if *relative {
                    Ok(vec![mouse_input(*x, *y, 0, MOUSEEVENTF_MOVE)])
                } else {
                    Ok(vec![absolute_move(*x, *y, clamped_moves)?])
                }
            }
            InputEvent::Click {
                button,
                x,
                y,
                count,
            } => click_inputs(button, *x, *y, *count, clamped_moves),
            InputEvent::Wheel {
                delta,
                horizontal,
                x,
                y,
            } => wheel_inputs(*delta, *horizontal, *x, *y, clamped_moves),
            InputEvent::MouseDown { button, x, y } => {
                button_edge_inputs(button, *x, *y, true, clamped_moves)
            }
            InputEvent::MouseUp { button, x, y } => {
                button_edge_inputs(button, *x, *y, false, clamped_moves)
            }
            // Перетаскивание — единственный шаг, который разворачивается больше чем
            // в одну отправку, поэтому его собирает `event_chunks`, а не эта функция.
            InputEvent::Drag { .. } => bail!("drag is expanded into batches by event_chunks"),
        }
    }

    fn button_edge_inputs(
        button: &str,
        x: Option<i32>,
        y: Option<i32>,
        press: bool,
        clamped_moves: &mut usize,
    ) -> Result<Vec<INPUT>> {
        if x.is_some() != y.is_some() {
            bail!("mouse button x and y must be supplied together")
        }
        let button = MouseButton::parse(button)?;
        let mut inputs = Vec::with_capacity(2);
        if let (Some(x), Some(y)) = (x, y) {
            inputs.push(absolute_move(x, y, clamped_moves)?);
        }
        inputs.push(mouse_input(
            0,
            0,
            0,
            if press { button.down() } else { button.up() },
        ));
        Ok(inputs)
    }

    fn text_inputs(text: &str) -> Result<Vec<INPUT>> {
        let units: Vec<_> = text.encode_utf16().collect();
        if units.len() > MAX_INPUT_TEXT_UTF16_UNITS {
            bail!(
                "input text has {} UTF-16 units (maximum {MAX_INPUT_TEXT_UTF16_UNITS})",
                units.len()
            )
        }
        let capacity = units
            .len()
            .checked_mul(2)
            .context("input text record count overflow")?;
        let mut inputs = Vec::with_capacity(capacity);
        for unit in units {
            inputs.push(keyboard_input(0, unit, KEYEVENTF_UNICODE));
            inputs.push(keyboard_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP));
        }
        Ok(inputs)
    }

    fn hotkey_inputs(keys: &[KeyArg]) -> Result<Vec<INPUT>> {
        if keys.is_empty() {
            bail!("hotkey keys must not be empty")
        }
        if keys.len() > MAX_HOTKEY_KEYS {
            bail!("hotkey has {} keys (maximum {MAX_HOTKEY_KEYS})", keys.len())
        }
        let keys = keys.iter().map(key_value).collect::<Result<Vec<_>>>()?;
        let mut inputs = Vec::with_capacity(keys.len() * 2);
        for key in &keys {
            inputs.push(keyboard_input(*key, 0, KEYBD_EVENT_FLAGS(0)));
        }
        for key in keys.iter().rev() {
            inputs.push(keyboard_input(*key, 0, KEYEVENTF_KEYUP));
        }
        Ok(inputs)
    }

    fn key_action_inputs(key: &KeyArg, action: &str) -> Result<Vec<INPUT>> {
        let key = key_value(key)?;
        match action.trim().to_ascii_lowercase().as_str() {
            "down" => Ok(vec![keyboard_input(key, 0, KEYBD_EVENT_FLAGS(0))]),
            "up" => Ok(vec![keyboard_input(key, 0, KEYEVENTF_KEYUP)]),
            "press" => Ok(vec![
                keyboard_input(key, 0, KEYBD_EVENT_FLAGS(0)),
                keyboard_input(key, 0, KEYEVENTF_KEYUP),
            ]),
            _ => bail!("key action must be press, down, or up"),
        }
    }

    fn click_inputs(
        button: &str,
        x: Option<i32>,
        y: Option<i32>,
        count: u32,
        clamped_moves: &mut usize,
    ) -> Result<Vec<INPUT>> {
        if x.is_some() != y.is_some() {
            bail!("click x and y must be supplied together")
        }
        let button = MouseButton::parse(button)?;
        if !(1..=MAX_CLICK_COUNT).contains(&count) {
            bail!("click count must be between 1 and {MAX_CLICK_COUNT}")
        }
        let mut inputs = Vec::with_capacity(count as usize * 2 + usize::from(x.is_some()));
        if let (Some(x), Some(y)) = (x, y) {
            inputs.push(absolute_move(x, y, clamped_moves)?);
        }
        for _ in 0..count {
            inputs.push(mouse_input(0, 0, 0, button.down()));
            inputs.push(mouse_input(0, 0, 0, button.up()));
        }
        Ok(inputs)
    }

    fn wheel_inputs(
        delta: i32,
        horizontal: bool,
        x: Option<i32>,
        y: Option<i32>,
        clamped_moves: &mut usize,
    ) -> Result<Vec<INPUT>> {
        if x.is_some() != y.is_some() {
            bail!("wheel x and y must be supplied together")
        }
        let mut inputs = Vec::with_capacity(2);
        if let (Some(x), Some(y)) = (x, y) {
            inputs.push(absolute_move(x, y, clamped_moves)?);
        }
        inputs.push(mouse_input(
            0,
            0,
            delta as u32,
            if horizontal {
                MOUSEEVENTF_HWHEEL
            } else {
                MOUSEEVENTF_WHEEL
            },
        ));
        Ok(inputs)
    }

    fn send_inputs(inputs: &[INPUT]) -> Result<()> {
        if inputs.is_empty() {
            return Ok(());
        }
        let sent = unsafe { SendInput(inputs, size_of::<INPUT>() as i32) } as usize;
        if sent != inputs.len() {
            bail!(
                "SendInput inserted {sent} of {} records (the target may be blocked by UIPI)",
                inputs.len()
            )
        }
        Ok(())
    }

    fn keyboard_input(vk: u16, scan: u16, flags: KEYBD_EVENT_FLAGS) -> INPUT {
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: VIRTUAL_KEY(vk),
                    wScan: scan,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }

    fn mouse_input(dx: i32, dy: i32, data: u32, flags: MOUSE_EVENT_FLAGS) -> INPUT {
        INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT {
                    dx,
                    dy,
                    mouseData: data,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }

    fn absolute_move(x: i32, y: i32, clamped_moves: &mut usize) -> Result<INPUT> {
        let (left, top, width, height) = virtual_screen_tuple();
        if width <= 0 || height <= 0 {
            bail!("virtual desktop has invalid dimensions")
        }
        let (nx, pulled_x) = normalize_axis(x, left, width);
        let (ny, pulled_y) = normalize_axis(y, top, height);
        if pulled_x || pulled_y {
            // Координата за пределами виртуального экрана раньше молча подтягивалась
            // к краю: она метила в точку по чужому скриншоту, получала край и не
            // узнавала об этом ниоткуда. Теперь такие сдвиги считаются и едут в ответ.
            *clamped_moves = clamped_moves.saturating_add(1);
        }
        Ok(mouse_input(
            nx,
            ny,
            0,
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        ))
    }

    /// Возвращает нормализованную ось и признак «пришлось подтянуть к краю».
    fn normalize_axis(value: i32, origin: i32, length: i32) -> (i32, bool) {
        if length <= 1 {
            return (0, i64::from(value) != i64::from(origin));
        }
        let relative = i64::from(value).saturating_sub(i64::from(origin));
        let clamped = relative.clamp(0, i64::from(length - 1));
        (
            ((clamped * 65_535) / i64::from(length - 1)) as i32,
            clamped != relative,
        )
    }

    fn key_value(key: &KeyArg) -> Result<u16> {
        if let KeyArg::Number(value) = key {
            if *value == 0 {
                bail!("virtual key must be nonzero")
            }
            return Ok(*value);
        }
        let KeyArg::Text(raw) = key else {
            unreachable!()
        };
        let key = raw.trim().to_ascii_lowercase();
        let value = match key.as_str() {
            "backspace" => 0x08,
            "tab" => 0x09,
            "enter" | "return" => 0x0D,
            "shift" => 0x10,
            "ctrl" | "control" => 0x11,
            "alt" => 0x12,
            "pause" => 0x13,
            "caps_lock" | "capslock" => 0x14,
            "escape" | "esc" => 0x1B,
            "space" => 0x20,
            "page_up" | "pageup" => 0x21,
            "page_down" | "pagedown" => 0x22,
            "end" => 0x23,
            "home" => 0x24,
            "left" => 0x25,
            "up" => 0x26,
            "right" => 0x27,
            "down" => 0x28,
            "print_screen" | "printscreen" => 0x2C,
            "insert" => 0x2D,
            "delete" | "del" => 0x2E,
            "win" | "meta" | "left_win" => 0x5B,
            "right_win" => 0x5C,
            _ if key.len() == 1 => {
                let byte = key.as_bytes()[0];
                if byte.is_ascii_alphanumeric() {
                    byte.to_ascii_uppercase() as u16
                } else {
                    bail!("unsupported named key {raw:?}; pass a numeric virtual-key code")
                }
            }
            _ if key.starts_with('f') => {
                let number = key[1..].parse::<u16>().unwrap_or_default();
                if (1..=24).contains(&number) {
                    0x6F + number
                } else {
                    bail!("function key must be f1 through f24")
                }
            }
            _ => bail!("unsupported named key {raw:?}; pass a numeric virtual-key code"),
        };
        Ok(value)
    }

    fn ensure_foreground(expected: Option<&HwndArg>, expected_pid: Option<u32>) -> Result<HWND> {
        let actual = unsafe { GetForegroundWindow() };
        if actual.0.is_null() {
            bail!("there is no foreground window in the interactive desktop")
        }
        if let Some(expected) = expected {
            let expected = expected.value()?;
            if actual != expected {
                bail!(
                    "foreground changed: expected {}, actual {}",
                    hwnd_hex(expected),
                    hwnd_hex(actual)
                )
            }
        }
        if let Some(expected_pid) = expected_pid {
            let mut actual_pid = 0;
            unsafe { GetWindowThreadProcessId(actual, Some(&mut actual_pid)) };
            if actual_pid != expected_pid {
                bail!("foreground pid changed: expected {expected_pid}, actual {actual_pid}")
            }
        }
        Ok(actual)
    }

    fn screen_capture(args: CaptureArgs, state_dir: &Path) -> Result<Value> {
        let (region, target_hwnd) = match args.target.to_ascii_lowercase().as_str() {
            "desktop" => {
                let (left, top, width, height) = virtual_screen_tuple();
                (
                    CaptureRegion {
                        left,
                        top,
                        width,
                        height,
                    },
                    None,
                )
            }
            "region" => (
                CaptureRegion {
                    left: args.x.context("region requires x")?,
                    top: args.y.context("region requires y")?,
                    width: args.width.context("region requires width")?,
                    height: args.height.context("region requires height")?,
                },
                None,
            ),
            "window" => {
                let hwnd = args.hwnd.context("window capture requires hwnd")?.value()?;
                require_window(hwnd, None)?;
                let mut rect = RECT::default();
                unsafe { GetWindowRect(hwnd, &mut rect)? };
                (
                    CaptureRegion {
                        left: rect.left,
                        top: rect.top,
                        width: rect.right - rect.left,
                        height: rect.bottom - rect.top,
                    },
                    Some(hwnd),
                )
            }
            _ => bail!("capture target must be desktop, region, or window"),
        };
        if region.width <= 0 || region.height <= 0 {
            bail!("capture rectangle must have positive width and height")
        }
        let image_bytes = capture_allocation(region.width, region.height)?;
        let directory = state_dir.join("desktop").join("captures");
        fs::create_dir_all(&directory)?;
        let name = capture_name(&args.name);
        let path = directory.join(name);
        capture_png(region, &path, image_bytes)?;
        let size = fs::metadata(&path)?.len();
        Ok(json!({
            "ok": true,
            "path": path,
            "format": "png",
            "mime": "image/png",
            "size": size,
            "left": region.left,
            "top": region.top,
            "width": region.width,
            "height": region.height,
            "target_hwnd": target_hwnd.map(hwnd_hex),
            "visible_desktop_capture": true,
        }))
    }

    fn capture_png(region: CaptureRegion, path: &Path, image_bytes: usize) -> Result<()> {
        let screen = unsafe { GetDC(None) };
        if screen.0.is_null() {
            bail!("GetDC returned no desktop device context")
        }
        let memory = unsafe { CreateCompatibleDC(Some(screen)) };
        if memory.0.is_null() {
            unsafe {
                ReleaseDC(None, screen);
            }
            bail!("CreateCompatibleDC failed")
        }
        let bitmap = unsafe { CreateCompatibleBitmap(screen, region.width, region.height) };
        if bitmap.0.is_null() {
            unsafe {
                let _ = DeleteDC(memory);
                ReleaseDC(None, screen);
            }
            bail!("CreateCompatibleBitmap failed")
        }
        let old = unsafe { SelectObject(memory, HGDIOBJ(bitmap.0)) };
        let result = capture_selected_png(screen, memory, bitmap, region, path, image_bytes);
        unsafe {
            SelectObject(memory, old);
            let _ = DeleteObject(HGDIOBJ(bitmap.0));
            let _ = DeleteDC(memory);
            ReleaseDC(None, screen);
        }
        result
    }

    fn capture_selected_png(
        screen: windows::Win32::Graphics::Gdi::HDC,
        memory: windows::Win32::Graphics::Gdi::HDC,
        bitmap: HBITMAP,
        region: CaptureRegion,
        path: &Path,
        image_bytes: usize,
    ) -> Result<()> {
        unsafe {
            BitBlt(
                memory,
                0,
                0,
                region.width,
                region.height,
                Some(screen),
                region.left,
                region.top,
                SRCCOPY | CAPTUREBLT,
            )?;
        }
        let mut pixels = vec![0u8; image_bytes];
        let mut info = BITMAPINFO::default();
        info.bmiHeader.biSize = size_of::<windows::Win32::Graphics::Gdi::BITMAPINFOHEADER>() as u32;
        info.bmiHeader.biWidth = region.width;
        info.bmiHeader.biHeight = -region.height;
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB.0;
        info.bmiHeader.biSizeImage = u32::try_from(image_bytes)?;
        let rows = unsafe {
            GetDIBits(
                memory,
                bitmap,
                0,
                region.height as u32,
                Some(pixels.as_mut_ptr().cast()),
                &mut info,
                DIB_RGB_COLORS,
            )
        };
        if rows != region.height {
            bail!("GetDIBits returned {rows} of {} rows", region.height)
        }
        write_png(path, region.width, region.height, &pixels)
    }

    fn clipboard_read(args: ClipboardReadArgs) -> Result<Value> {
        let _clipboard = open_clipboard()?;
        let sequence = unsafe { GetClipboardSequenceNumber() };
        if unsafe { IsClipboardFormatAvailable(CF_UNICODETEXT) }.is_err() {
            return Ok(json!({
                "ok": true,
                "available": false,
                "format": "unicode_text",
                "clipboard_sequence": sequence,
            }));
        }
        let handle = unsafe { GetClipboardData(CF_UNICODETEXT)? };
        let global = HGLOBAL(handle.0);
        let bytes = unsafe { GlobalSize(global) };
        if bytes < 2 {
            return Ok(json!({
                "ok": true,
                "available": true,
                "format": "unicode_text",
                "text": "",
                "chars": 0,
                "truncated": false,
                "clipboard_sequence": sequence,
            }));
        }
        let pointer = unsafe { GlobalLock(global) } as *const u16;
        if pointer.is_null() {
            bail!("GlobalLock failed for clipboard text")
        }
        let units = unsafe { std::slice::from_raw_parts(pointer, bytes / 2) };
        let length = units
            .iter()
            .position(|unit| *unit == 0)
            .unwrap_or(units.len());
        let limit = args.limit_chars.clamp(1, MAX_CLIPBOARD_CHARS).min(length);
        let text = String::from_utf16_lossy(&units[..limit]);
        unsafe {
            let _ = GlobalUnlock(global);
        }
        Ok(json!({
            "ok": true,
            "available": true,
            "format": "unicode_text",
            "text": text,
            "chars": length,
            "truncated": limit < length,
            "clipboard_sequence": sequence,
        }))
    }

    fn clipboard_write(args: ClipboardWriteArgs) -> Result<Value> {
        let mut units: Vec<u16> = args.text.encode_utf16().collect();
        if units.len() > MAX_CLIPBOARD_CHARS {
            bail!(
                "clipboard text has {} UTF-16 units (maximum {MAX_CLIPBOARD_CHARS})",
                units.len()
            )
        }
        units.push(0);
        let bytes = units
            .len()
            .checked_mul(size_of::<u16>())
            .context("clipboard text is too large")?;
        let global = unsafe { GlobalAlloc(GMEM_MOVEABLE, bytes)? };
        let pointer = unsafe { GlobalLock(global) } as *mut u16;
        if pointer.is_null() {
            unsafe {
                let _ = GlobalFree(Some(global));
            }
            bail!("GlobalLock failed for clipboard allocation")
        }
        unsafe {
            ptr::copy_nonoverlapping(units.as_ptr(), pointer, units.len());
            let _ = GlobalUnlock(global);
        }
        let result = (|| -> Result<()> {
            let _clipboard = open_clipboard()?;
            unsafe {
                EmptyClipboard()?;
                SetClipboardData(CF_UNICODETEXT, Some(HANDLE(global.0)))?;
            }
            Ok(())
        })();
        if let Err(error) = result {
            unsafe {
                let _ = GlobalFree(Some(global));
            }
            return Err(error);
        }
        Ok(json!({
            "ok": true,
            "format": "unicode_text",
            "chars": units.len() - 1,
            "clipboard_sequence": unsafe { GetClipboardSequenceNumber() },
        }))
    }

    fn open_clipboard() -> Result<ClipboardGuard> {
        for _ in 0..40 {
            if unsafe { OpenClipboard(None) }.is_ok() {
                return Ok(ClipboardGuard);
            }
            thread::sleep(Duration::from_millis(25));
        }
        bail!("clipboard remained busy for 1 second")
    }

    fn process_details(pid: u32) -> ProcessDetails {
        let session_id = process_session(pid);
        let Ok(handle) = (unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) })
        else {
            return ProcessDetails {
                path: None,
                created_filetime: None,
                session_id,
            };
        };
        let _handle = HandleGuard(handle);
        let mut path_buffer = vec![0u16; 32_768];
        let mut length = path_buffer.len() as u32;
        let path = unsafe {
            QueryFullProcessImageNameW(
                handle,
                Default::default(),
                PWSTR(path_buffer.as_mut_ptr()),
                &mut length,
            )
        }
        .ok()
        .map(|_| String::from_utf16_lossy(&path_buffer[..length as usize]));
        let mut created = Default::default();
        let mut exited = Default::default();
        let mut kernel = Default::default();
        let mut user = Default::default();
        let created_filetime =
            unsafe { GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user) }
                .ok()
                .map(|_| {
                    (u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime)
                });
        ProcessDetails {
            path,
            created_filetime,
            session_id,
        }
    }

    fn process_session(pid: u32) -> Option<u32> {
        let mut session = 0;
        unsafe { ProcessIdToSessionId(pid, &mut session) }
            .ok()
            .map(|_| session)
    }

    fn window_row(hwnd: HWND, z_order: usize) -> Option<Value> {
        if hwnd.0.is_null() || !unsafe { IsWindow(Some(hwnd)) }.as_bool() {
            return None;
        }
        let mut pid = 0;
        let thread_id = unsafe { GetWindowThreadProcessId(hwnd, Some(&mut pid)) };
        let details = process_details(pid);
        let mut rect = RECT::default();
        let rect_value = unsafe { GetWindowRect(hwnd, &mut rect) }.ok().map(|_| {
            json!({
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
                "width": rect.right - rect.left,
                "height": rect.bottom - rect.top,
            })
        });
        let hwnd_text = hwnd_hex(hwnd);
        Some(json!({
            "hwnd": hwnd_text,
            "fingerprint": format!("{}:{pid}:{}", hwnd_hex(hwnd), details.created_filetime.unwrap_or_default()),
            "pid": pid,
            "thread_id": thread_id,
            "session_id": details.session_id,
            "process_path": details.path,
            "process_created_filetime": details.created_filetime,
            "title": window_text(hwnd),
            "class": window_class(hwnd),
            "rect": rect_value,
            "visible": unsafe { IsWindowVisible(hwnd) }.as_bool(),
            "minimized": unsafe { IsIconic(hwnd) }.as_bool(),
            "z_order": z_order,
        }))
    }

    fn window_text(hwnd: HWND) -> String {
        let length = unsafe { GetWindowTextLengthW(hwnd) }.max(0) as usize;
        let mut buffer = vec![0u16; length.saturating_add(1).max(2)];
        let copied = unsafe { GetWindowTextW(hwnd, &mut buffer) }.max(0) as usize;
        String::from_utf16_lossy(&buffer[..copied.min(buffer.len())])
    }

    fn window_class(hwnd: HWND) -> String {
        let mut buffer = vec![0u16; 512];
        let copied = unsafe { GetClassNameW(hwnd, &mut buffer) }.max(0) as usize;
        String::from_utf16_lossy(&buffer[..copied.min(buffer.len())])
    }

    fn require_window(hwnd: HWND, expected_pid: Option<u32>) -> Result<()> {
        if !unsafe { IsWindow(Some(hwnd)) }.as_bool() {
            bail!("window {} no longer exists", hwnd_hex(hwnd))
        }
        if let Some(expected_pid) = expected_pid {
            let mut actual_pid = 0;
            unsafe { GetWindowThreadProcessId(hwnd, Some(&mut actual_pid)) };
            if actual_pid != expected_pid {
                bail!("window pid changed: expected {expected_pid}, actual {actual_pid}")
            }
        }
        Ok(())
    }

    fn page(rows: Vec<Value>, args: PageArgs) -> Value {
        let total = rows.len();
        let limit = args.limit.clamp(1, MAX_PAGE);
        let items: Vec<_> = rows.into_iter().skip(args.offset).take(limit).collect();
        json!({
            "ok": true,
            "total": total,
            "offset": args.offset,
            "limit": limit,
            "returned": items.len(),
            "next_offset": (args.offset + items.len() < total).then_some(args.offset + items.len()),
            "items": items,
        })
    }

    fn virtual_screen() -> Value {
        let (left, top, width, height) = virtual_screen_tuple();
        json!({
            "left": left,
            "top": top,
            "width": width,
            "height": height,
            "right": left + width,
            "bottom": top + height,
        })
    }

    fn virtual_screen_tuple() -> (i32, i32, i32, i32) {
        unsafe {
            (
                GetSystemMetrics(SM_XVIRTUALSCREEN),
                GetSystemMetrics(SM_YVIRTUALSCREEN),
                GetSystemMetrics(SM_CXVIRTUALSCREEN),
                GetSystemMetrics(SM_CYVIRTUALSCREEN),
            )
        }
    }

    fn hwnd_hex(hwnd: HWND) -> String {
        format!("0x{:X}", hwnd.0 as usize)
    }

    fn utf16_z(value: &[u16]) -> String {
        let length = value
            .iter()
            .position(|unit| *unit == 0)
            .unwrap_or(value.len());
        String::from_utf16_lossy(&value[..length])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capability_names_are_unique_and_versioned() {
        let mut names: Vec<_> = CAPABILITIES.iter().map(|value| value.name).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), CAPABILITIES.len());
        for descriptor in descriptors() {
            // input.perform v2 — раздельные нажатие/отпускание мыши и перетаскивание.
            let expected = u32::from(matches!(
                descriptor.name.as_str(),
                "desktop.screen.capture" | "desktop.input.perform"
            )) + 1;
            assert_eq!(descriptor.version, expected, "{}", descriptor.name);
        }
        assert_eq!(adapter_descriptor().version, "3");
    }

    #[test]
    fn capture_declares_a_provider_owned_image_artifact() {
        let output = artifact_output(
            "desktop.screen.capture",
            &serde_json::json!({"ok": true, "path": "capture.png", "mime": "image/png"}),
        )
        .unwrap()
        .unwrap();
        assert_eq!(output.path, PathBuf::from("capture.png"));
        assert_eq!(output.media_type, "image/png");
        assert_eq!(output.presentation, "image");
        assert!(
            artifact_output("desktop.status", &serde_json::json!({"ok": true}))
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn adapter_is_only_advertised_as_available_on_windows() {
        assert_eq!(adapter_descriptor().available, cfg!(windows));
    }

    #[cfg(windows)]
    #[test]
    fn native_read_surfaces_smoke() {
        let state = std::env::temp_dir().join("praxis-desktop-read-smoke");
        for (capability, args) in [
            ("desktop.status", serde_json::json!({})),
            ("os.process.list", serde_json::json!({"limit": 8})),
            (
                "desktop.window.list",
                serde_json::json!({"limit": 8, "visible_only": false}),
            ),
        ] {
            let result = dispatch(capability, args, &state).unwrap();
            assert_eq!(result["ok"], true, "{capability}: {result}");
        }
    }

    #[cfg(windows)]
    #[test]
    fn input_resource_limits_fail_before_touching_the_desktop() {
        let state = std::env::temp_dir().join("praxis-desktop-bounds");
        let too_many_events = serde_json::json!({
            "events": (0..513).map(|_| serde_json::json!({
                "type": "mouse", "x": 0, "y": 0, "relative": true
            })).collect::<Vec<_>>()
        });
        let error = dispatch("desktop.input.perform", too_many_events, &state).unwrap_err();
        assert!(error.to_string().contains("too many input events"));

        let oversized_text = serde_json::json!({
            "events": [{"type": "text", "text": "x".repeat(16_385)}]
        });
        let error = dispatch("desktop.input.perform", oversized_text, &state).unwrap_err();
        assert!(error.to_string().contains("UTF-16 units"));

        let too_many_records = serde_json::json!({
            "events": (0..3).map(|_| serde_json::json!({
                "type": "text", "text": "x".repeat(16_384)
            })).collect::<Vec<_>>()
        });
        let error = dispatch("desktop.input.perform", too_many_records, &state).unwrap_err();
        assert!(error.to_string().contains("input batch expands"));

        let excessive_delay = serde_json::json!({
            "inter_event_delay_ms": 30_001,
            "events": [
                {"type": "key", "key": "a"},
                {"type": "key", "key": "b"}
            ]
        });
        let error = dispatch("desktop.input.perform", excessive_delay, &state).unwrap_err();
        assert!(error.to_string().contains("total input delay"));

        let excessive_clicks = serde_json::json!({
            "events": [{"type": "click", "button": "left", "count": 65}]
        });
        let error = dispatch("desktop.input.perform", excessive_clicks, &state).unwrap_err();
        assert!(error.to_string().contains("click count"));
    }

    #[cfg(windows)]
    fn input_events(value: serde_json::Value) -> Vec<platform::InputEvent> {
        serde_json::from_value(value).expect("input events must parse")
    }

    /// Ведомость зажатого. Это тот самый счёт, по которому обработчик отпускает
    /// кнопку на выходе: если он ошибётся, мышь Егора останется зажатой.
    #[cfg(windows)]
    #[test]
    fn a_held_mouse_button_is_counted_until_something_releases_it() {
        let held = |value| platform::net_held(&input_events(value)).unwrap();

        // Одинокое нажатие — ровно тот случай, ради которого есть авто-отпускание.
        assert_eq!(
            held(serde_json::json!([{"type": "mouse_down", "button": "left"}])),
            vec!["left"]
        );
        assert!(
            held(serde_json::json!([
                {"type": "mouse_down", "button": "left"},
                {"type": "mouse", "x": 10, "y": 10},
                {"type": "mouse_up", "button": "left"}
            ]))
            .is_empty()
        );
        // Разные кнопки живут независимо: отпустили левую — правая всё ещё зажата.
        assert_eq!(
            held(serde_json::json!([
                {"type": "mouse_down", "button": "left"},
                {"type": "mouse_down", "button": "right"},
                {"type": "mouse_up", "button": "left"}
            ])),
            vec!["right"]
        );
        // Составные шаги замкнуты сами на себя.
        assert!(held(serde_json::json!([{"type": "click", "count": 3}])).is_empty());
        assert!(
            held(serde_json::json!([
                {"type": "drag", "x": 0, "y": 0, "to_x": 20, "to_y": 20}
            ]))
            .is_empty()
        );
        // Отпустить незажатое — не ошибка: это её способ снять залипание,
        // оставшееся от вызова, который оборвался раньше.
        assert!(held(serde_json::json!([{"type": "mouse_up", "button": "middle"}])).is_empty());
    }

    /// Перетаскивание разворачивается в отправки с паузами, и эти паузы едут в тот
    /// же общий бюджет задержки, а не мимо него.
    #[cfg(windows)]
    #[test]
    fn drag_expands_into_batches_whose_pauses_land_in_the_declared_delay_budget() {
        let plan = |value, delay| platform::plan_summary(&input_events(value), delay).unwrap();

        let single = plan(
            serde_json::json!([{
                "type": "drag", "x": 0, "y": 0, "to_x": 100, "to_y": 50,
                "steps": 10, "hold_ms": 50, "step_delay_ms": 7, "settle_ms": 30
            }]),
            0,
        );
        // нажатие + 10 сдвигов + отпускание
        assert_eq!(single.batches, 12);
        // 50 (после нажатия) + 9x7 (между сдвигами) + 30 (перед отпусканием)
        assert_eq!(single.pause_ms, 143);

        // Пауза между шагами добавляется ровно один раз на стык, поверх внутренних.
        let paired = plan(
            serde_json::json!([
                {"type": "drag", "x": 0, "y": 0, "to_x": 100, "to_y": 50,
                 "steps": 10, "hold_ms": 50, "step_delay_ms": 7, "settle_ms": 30},
                {"type": "key", "key": "a"}
            ]),
            200,
        );
        assert_eq!(paired.pause_ms, 343);
        assert_eq!(paired.batches, 13);

        // Умолчания названы в ответе и должны совпадать с тем, что реально считается.
        let defaults = plan(
            serde_json::json!([{"type": "drag", "x": 0, "y": 0, "to_x": 10, "to_y": 10}]),
            0,
        );
        assert_eq!(defaults.batches, 26);
        assert_eq!(defaults.pause_ms, 60 + 23 * 8 + 60);
    }

    /// Границы новых капов — с обеих сторон, но без единого касания настоящей мыши:
    /// принятая сторона доказывается тем, что подготовка спотыкается уже о ДРУГОЙ,
    /// заведомо более поздний предел.
    #[cfg(windows)]
    #[test]
    fn drag_limits_are_checked_at_the_boundary_before_the_desktop_is_touched() {
        let state = std::env::temp_dir().join("praxis-desktop-drag-bounds");
        let drag = |extra: serde_json::Value| {
            let mut step = serde_json::json!({
                "type": "drag", "x": 0, "y": 0, "to_x": 10, "to_y": 10,
                "steps": 1, "hold_ms": 0, "step_delay_ms": 0, "settle_ms": 0
            });
            let object = step.as_object_mut().unwrap();
            for (key, value) in extra.as_object().unwrap() {
                object.insert(key.clone(), value.clone());
            }
            step
        };
        // Второй шаг заведомо непроходим, поэтому «принято» видно по тому, КАКАЯ
        // жалоба пришла, и ни одна мышь при этом не двигается.
        let wall = serde_json::json!({"type": "text", "text": "x".repeat(16_385)});
        let refused = |first: serde_json::Value| {
            dispatch(
                "desktop.input.perform",
                serde_json::json!({"events": [first, wall.clone()]}),
                &state,
            )
            .unwrap_err()
            .to_string()
        };

        assert!(refused(drag(serde_json::json!({"steps": 256}))).contains("UTF-16 units"));
        assert!(
            refused(drag(serde_json::json!({"steps": 257})))
                .contains("drag steps must be between 1 and 256")
        );
        assert!(
            refused(drag(serde_json::json!({"steps": 0})))
                .contains("drag steps must be between 1 and 256")
        );
        assert!(refused(drag(serde_json::json!({"hold_ms": 5_000}))).contains("UTF-16 units"));
        assert!(
            refused(drag(serde_json::json!({"hold_ms": 5_001})))
                .contains("drag hold_ms is 5001ms")
        );
        assert!(
            refused(drag(serde_json::json!({"settle_ms": 5_001})))
                .contains("drag settle_ms is 5001ms")
        );
        assert!(
            refused(drag(serde_json::json!({"step_delay_ms": 5_001})))
                .contains("drag step_delay_ms is 5001ms")
        );

        // Сумма внутренних пауз упирается в общий бюджет: 200x150 = 30000 проходит,
        // 201-я миллисекунда на шаге — уже нет.
        let events = input_events(serde_json::json!([drag(
            serde_json::json!({"steps": 200, "step_delay_ms": 150, "settle_ms": 150})
        )]));
        assert_eq!(platform::plan_summary(&events, 0).unwrap().pause_ms, 30_000);
        let over = dispatch(
            "desktop.input.perform",
            serde_json::json!({"events": [drag(
                serde_json::json!({"steps": 200, "step_delay_ms": 151, "settle_ms": 151})
            )]}),
            &state,
        )
        .unwrap_err()
        .to_string();
        assert!(over.contains("total input delay is 30200ms"), "{over}");
    }

    /// Подтягивание координаты к краю виртуального экрана было молчаливым.
    #[cfg(windows)]
    #[test]
    fn out_of_screen_coordinates_are_counted_instead_of_being_pulled_in_silence() {
        let state = std::env::temp_dir().join("praxis-desktop-clamp");
        let status = dispatch("desktop.status", serde_json::json!({}), &state).unwrap();
        let screen = &status["virtual_screen"];
        let (left, top) = (
            screen["left"].as_i64().unwrap() as i32,
            screen["top"].as_i64().unwrap() as i32,
        );

        let inside = platform::plan_summary(
            &input_events(serde_json::json!([
                {"type": "mouse_down", "x": left, "y": top},
                {"type": "mouse_up", "x": left, "y": top}
            ])),
            0,
        )
        .unwrap();
        assert_eq!(inside.clamped_moves, 0);

        let outside = platform::plan_summary(
            &input_events(serde_json::json!([
                {"type": "mouse_down", "x": left - 1, "y": top},
                {"type": "mouse", "x": left, "y": top - 1},
                {"type": "mouse_up", "x": left, "y": top}
            ])),
            0,
        )
        .unwrap();
        assert_eq!(outside.clamped_moves, 2);
    }

    #[test]
    fn capture_has_independent_pixel_and_raw_byte_limits() {
        let error = capture_allocation(10_000, 4_001).unwrap_err();
        assert!(error.to_string().contains("pixels"));

        let error = capture_allocation(9_000, 4_000).unwrap_err();
        assert!(error.to_string().contains("raw BGRA bytes"));
    }

    #[test]
    fn capture_name_is_windows_component_safe() {
        let name = capture_name(&("𐐀".repeat(300) + ".bmp"));
        assert!(name.encode_utf16().count() <= 204);
        assert!(name.ends_with(".png"));
        assert_eq!(capture_name("Praxis.Screen.BMP"), "Praxis.Screen.png");
        assert_eq!(capture_name("Praxis.Screen.png"), "Praxis.Screen.png");
        assert_ne!(capture_name("CON"), "CON.png");
    }

    #[test]
    fn png_encoder_preserves_bgra_colors_as_lossless_rgb() {
        let directory = std::env::temp_dir().join(format!("praxis-png-test-{}", Uuid::new_v4()));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("capture.png");
        fs::write(&path, b"old capture").unwrap();
        let bgra = [
            0, 0, 255, 0, // red; the unused GDI alpha byte must not make it transparent
            255, 128, 0, 37, // blue with a green component
        ];
        write_png(&path, 2, 1, &bgra).unwrap();

        let bytes = fs::read(&path).unwrap();
        assert_eq!(&bytes[..8], b"\x89PNG\r\n\x1a\n");
        let decoder = png::Decoder::new(std::io::BufReader::new(File::open(&path).unwrap()));
        let mut reader = decoder.read_info().unwrap();
        let mut decoded = vec![0; reader.output_buffer_size().unwrap()];
        let info = reader.next_frame(&mut decoded).unwrap();
        assert_eq!((info.width, info.height), (2, 1));
        assert_eq!(info.color_type, png::ColorType::Rgb);
        assert_eq!(&decoded[..info.buffer_size()], &[255, 0, 0, 0, 128, 255]);
        assert_eq!(fs::read_dir(&directory).unwrap().count(), 1);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn png_output_writer_enforces_its_byte_limit_before_overflow() {
        let mut output = SizeLimitedWriter::new(Vec::new(), 3);
        output.write_all(b"abc").unwrap();
        let error = output.write_all(b"d").unwrap_err();
        assert!(error.to_string().contains("PNG exceeds 3 bytes"));
        assert_eq!(output.inner, b"abc");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn desktop_dispatch_refuses_to_block_a_current_thread_runtime() {
        let state = std::env::temp_dir().join("praxis-desktop-current-thread");
        let error = dispatch("desktop.status", serde_json::json!({}), &state).unwrap_err();
        assert!(error.to_string().contains("multi-thread Tokio runtime"));
    }

    #[cfg(windows)]
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn desktop_dispatch_runs_from_tokio_via_the_blocking_pool() {
        let state = std::env::temp_dir().join("praxis-desktop-blocking-pool");
        let result = dispatch("desktop.status", serde_json::json!({}), &state).unwrap();
        assert_eq!(result["ok"], true);
    }
}
