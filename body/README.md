# Praxis Body

`praxis-body` is the brainless Windows execution body of the single server-side Praxis agent.
`praxis-bridge` is its transport relay and artifact spool. Neither contains an LLM, memory,
Forge workers, or a task store.

## Workspace

- `crates/praxis-protocol` — `praxis.body.v1` wire types;
- `crates/praxis-body` — Windows identity, process/files/artifacts and outbound client;
- `crates/praxis-bridge` — WSS relay, durable frame spool, controller API and artifact CAS;
- `crates/praxis-tray` — GUI-subsystem notification icon and hidden interactive-session supervisor;
- `crates/praxis-system-router` — automatic LocalSystem service, sole WSS owner and router for the
  same executor/protocol;
- `../body_client.py` — synchronous server-side controller used by canonical Forge.
- `../computer_memory.py` — server-only receipts/maps/index; it is never linked into the body.

## Build and safe tests

```powershell
cd body
cargo check --workspace
cargo test --workspace
cargo build --release -p praxis-body
cargo build --release -p praxis-tray -p praxis-system-router
```

These unit tests do not launch a detached supervisor. Native Windows tests that exercise
`process.start`, Job Objects or parallel Forge checks must be run in an external user terminal,
never through the Codex inline runner on this machine.

## Development bridge

```powershell
$env:PRAXIS_BRIDGE_DEVICE_TOKEN='device-secret'
$env:PRAXIS_BRIDGE_CONTROLLER_TOKEN='controller-secret'
cargo run -p praxis-bridge -- --listen 127.0.0.1:9473 --state-dir state/bridge
```

Copy `body.example.json`, set the same device token, then:

```powershell
cargo run -p praxis-body -- connect --config .\body.local.json
```

The loopback bearer tokens are bootstrap credentials. Live production puts the bridge behind WSS;
the public Caddy surface exposes only device/CAS routes while the controller stays on the Docker
host boundary. mTLS enrollment remains the next transport hardening step.

For a VPN whose public hairpin route is unreliable, `dial_addresses` may contain an ordered list of
private and public `IP:port` destinations. The WebSocket tries them before ordinary OS DNS; artifact
HTTP resolution puts the same destinations first and appends direct DNS results. The configured
`bridge_ws_url` and `artifact_base_url` remain untouched, so WebSocket Host, HTTP Host, TLS SNI and
certificate validation still use the canonical DNS name. This is a dial override, not a TLS bypass.

```json
"dial_addresses": ["172.29.172.1:443", "203.0.113.10:443"]
```

The optional list is backward-compatible and may be omitted. While it is present, both canonical
URLs must share one DNS hostname and effective port, and every address must use that port. Zero-port,
unspecified, multicast, broadcast, link-local, duplicate and overlong lists fail config validation.

## Identity

```powershell
cargo run -p praxis-body -- identity
cargo run -p praxis-body -- capabilities --device-id windows-pc
```

`interactive` and `system` are wire-level choices. On Yegor's UAC-disabled workstation the actual
interactive token reports high integrity. `PraxisSystemRouter` runs as LocalSystem from boot, owns
the only outbound WSS connection and invokes the same body capability code under SYSTEM. After user
logon, the tray starts `praxis-body serve-interactive` without a console window; the service forwards
desktop-bound envelopes to it through an ACL- and token-bound named pipe. Neither process contains a
model, task store, memory or autonomous policy.

Authority is decided by the server before it creates an envelope. `praxis:self` is sovereign and may
use both interactive and SYSTEM execution. Only the human owner may grant or revoke access for other
humans; a trusted human cannot delegate, request SYSTEM, or use raw Telegram account operations.
Because Praxis herself also has full server shell/root and code hands, that division is an explicit
governance and audit invariant, not a cryptographic sandbox boundary enforced by this brainless body.

## Native desktop automation

The interactive session host provides typed Win32 operations. Everything in this list except
`desktop.window.read` is plain Win32 with no COM at all:

- `desktop.status`, paged `desktop.window.list` and guarded `desktop.window.activate`;
- `desktop.input.perform` for text, keys/hotkeys, mouse, click and wheel, optionally guarded by
  expected foreground HWND/PID;
- `desktop.screen.capture` v2 for desktop, region or window as lossless RGB PNG. The desktop
  provider marks the result as an image artifact; the shared runtime publishes it to CAS and the
  server/PWA can render or download it without a second capability-specific export call;
- Unicode `desktop.clipboard.read/write` and paged `os.process.list`;
- `desktop.window.read` — the window as text (see below). This one does use COM.

Session 0 never pretends to own a user desktop: desktop/input calls require the interactive session
host. Office COM/pywin32 is a separate later adapter, not a prerequisite for this native surface.
Window activation uses a temporary `AttachThreadInput` handshake with the current foreground and
target threads, detaches immediately, and returns the actual foreground HWND. Input then checks
`expected_foreground`/PID again and fails closed if focus changed.

## Reading a window as text (`desktop.window.read`)

Until this verb existed, the only way to learn what a button said was `desktop.screen.capture`
(about 2.4 seconds for a 188 KB PNG) plus a vision call. `desktop.window.read` walks the UI
Automation control view of one window — by `hwnd`, or the foreground window when `hwnd` is
omitted — and returns roles, labels, values, states and screen rectangles as JSON, with a
`center` point per element so the reading can be handed straight to `desktop.input.perform`.

COM enters the body here and nowhere else. `crates/praxis-body/src/uia.rs` owns the whole
dependency: `CoInitializeEx(COINIT_MULTITHREADED)` runs on a dedicated thread that this module
creates per call and joins or abandons itself, so no tokio worker thread — which file and process
verbs reuse — is ever pulled into an apartment. MTA rather than STA because an STA thread must
pump a message loop for incoming calls to arrive at all, and this one runs none; MTA is also what
Microsoft recommends for UI Automation clients. The extra `windows` crate features
(`Win32_System_Com`, `Win32_UI_Accessibility`) are declared in `crates/praxis-body/Cargo.toml`,
not in the workspace list, so the COM surface is visible in the dependency manifest.

Plain Win32 stays as the named fallback and never silently becomes silence: when UI Automation
cannot be created, errors, or does not answer inside `timeout_ms`, the verb still returns
`ok: true` with `backend: "win32"` and a `fallback_reason` string, having walked the classic child
windows with timed `WM_GETTEXT`. That fallback sees far less — Chromium, Electron, WPF and UWP
surfaces are mute to it, which is exactly why UI Automation is the primary path — and it says so
in `notes` instead of pretending the window was empty. `backend: "win32"` in the arguments takes
that path deliberately, which is both how the fallback is exercised and a cheap read for classic
windows.

Measured on the live workstation (debug build, one window at a time): an Electron window,
266 elements, 615 ms; a classic Win32 application window, 86-93 elements, 700-926 ms; a XAML Quick
Settings flyout, 41 elements, 99-159 ms — against 2.4 s for a 188 KB screenshot plus a vision call.

The two backends are not ranked, they are different: the XAML flyout read through the forced Win32
fallback yields exactly one element, the window itself, which is the whole reason COM is here; the
classic Win32 window read through the same fallback yields 73 elements in 3 ms against 700 ms
through UI Automation. Automatic stays UI Automation because it is the one that is never mute.

Every limit is named in the answer under `limits`, together with its ceiling: `max_nodes` (400,
ceiling 5000), `max_depth` (24/64), `max_children_per_node` (128/2000), `max_text_chars` (240/4000,
counted in UTF-16 units) and `timeout_ms` (1800/20000). A request outside a range is pulled to the
border and the change is stated in `notes` rather than refused or silently swallowed. When the walk
stops early, `truncated_by` names the cap, `total_known` goes false and `discovered_unread` reports
how many elements had already been found and left unread. A missing state key means the element
does not expose that state; it never means `false`.

## Live deployment

- server unit: `praxis-body-bridge.service`, user `praxis-body`, listen `172.17.0.1:9473`;
- public device endpoint: `wss://body.203.0.113.10.nip.io`;
- Windows install: `C:\ProgramData\Praxis\Body`;
- release inputs are copied and SHA-256 verified as versioned executables under
  `C:\ProgramData\Praxis\Body\bin`; SCM and Task Scheduler never point back into a checkout or
  Downloads directory. The install root, binaries and configs have protected explicit ACLs;
- both local named-pipe addresses are cryptographically randomized on every install. Their servers
  use explicit SYSTEM + installing-owner SID DACLs in addition to separate bearer tokens;
- `PraxisSystemRouter` is an automatic LocalSystem service and the sole WSS owner. It starts before
  user logon, supervises `praxis-body connect` with `CREATE_NO_WINDOW`, keeps the service journal in
  `C:\ProgramData\Praxis\Body\state` and serves SYSTEM operations;
- Task Scheduler task `Praxis Body` starts only `praxis-tray.exe` (Highest, Interactive, AtLogon).
  The tray supervises `praxis-body serve-interactive` with `CREATE_NO_WINDOW`; it does not open a
  second WSS connection;
- service configs are `service-body.json` and `system-router.json`; user/session config remains the
  installer input body JSON. They are ACLed to the logged-in owner and SYSTEM;
- interactive runtime state: `%LOCALAPPDATA%\Praxis\Body`;
- public `/v1/controller/*` is deliberately 404; server Praxis uses `host.docker.internal`.

Current installed/source SHA-256 evidence:

- body: `17C9032B35CF6E24751BBC2FBDEC21F8B93F3A16845B2D1E76CA5321CDE90CD9`;
- tray: `AFB805C18B5AADDAA8BCD90CAC7B2EF7C93394457E96ECD531E18A89621EC8DE`;
- system router: `EF2A0C84846FF5DEF344E7D99E1DEDDB9031EA86997C65915BBA3BE76FA05397`.

The installed hash-named executables match the release artifacts. The LocalSystem service is
Automatic and running; the Highest/Interactive tray task is running; all managed processes have a
zero main-window handle. Body source inputs are unchanged through deployed server head `905e09d`.
Both service and interactive configs use ordered dial fallback from the tunnel endpoint to the
external endpoint.

The exact-live body canary reports interactive high/session 1 and SYSTEM/session 0, both elevated;
UTF-8 process output is exact, and bidirectional file import/edit/export preserves the visible name,
size and SHA-256. Canary files were removed from Windows. Canary script SHA-256:
`341ef0d91bfe416a11eb10d68484090893af93a7f30c520930f4c81cfaeddf99`.

An earlier isolated scroll canary passed position `0 -> 18` and a `760x520` PNG with SHA-256
`e473d9c27b53895d4ef77fa9129aade45978aebb27e035aa53b3ec44d11b89c7`. The desktop was locked after
the exact-final reinstall (`foreground=null`, `cursor=null`), so that scroll result is supporting
evidence rather than a claim that the exact-final binary was re-scrolled. Exact Linux bridge
is deployed with server head `905e09d` at SHA-256
`43660e0514432a1719f146dced45bb3006e63c431ed7bb80ec84e88a99c337b2`. `praxis-body-bridge` is
enabled/active; the final runtime evidence and body canary are green.

## Operator commands and logs

Run service-control commands from an elevated PowerShell. SCM is configured `Automatic`; pre-login
behavior was validated by stopping the AtLogon half and exercising SYSTEM through the service. A
physical cold reboot was not part of this gate. The interactive host appears when the owner logs on.

```powershell
Get-Service PraxisSystemRouter
Get-ScheduledTask -TaskName 'Praxis Body' | Select-Object TaskName, State

# Restore either half without opening a console window.
Restart-Service PraxisSystemRouter
Start-ScheduledTask -TaskName 'Praxis Body'

# Service/WSS and interactive-session diagnostics.
Get-Content 'C:\ProgramData\Praxis\Body\state\service.log' -Tail 100
Get-Content 'C:\ProgramData\Praxis\Body\state\connection.log' -Tail 100
Get-Content "$env:LOCALAPPDATA\Praxis\Body\tray.log" -Tail 100
Get-Content "$env:LOCALAPPDATA\Praxis\Body\session-host.log" -Tail 100
Get-Content "$env:LOCALAPPDATA\Praxis\Body\body-stderr.log" -Tail 100
```

If the tray cannot parse its startup arguments, it writes
`C:\ProgramData\Praxis\Body\tray-startup-error.log`; an early service-config failure goes to
`C:\ProgramData\Praxis\Body\service-startup-error.log`. The tray menu can open the interactive state
directory and restart only the session host; it does not own or restart the WSS transport.

Server-side recovery must never probe a Windows PID with `os.kill(pid, 0)`: CPython may terminate the
process instead of performing a POSIX-style existence check. Use `process_liveness.is_process_alive`,
which opens the process with `SYNCHRONIZE` and performs a zero-time wait.

## Evidence and memory

The body never writes Praxis memory. Server Forge stores full receipts/logs privately, appends
normalized `memory/computer/events/*.jsonl`, renders grep-friendly device/task Markdown cards and
indexes them in a disposable SQLite database. `coding_inspect(action="observations")` reads the task
evidence. Finishing a task creates one `computer_episode`; polling noise is suppressed.
