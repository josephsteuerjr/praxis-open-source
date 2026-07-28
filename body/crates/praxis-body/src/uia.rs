//! Чтение окна ТЕКСТОМ: роли, надписи, состояния и координаты — вместо снимка экрана.
//!
//! Живой случай, ради которого написан модуль: чтобы узнать, что написано на кнопке,
//! у неё сегодня есть ровно один путь — `desktop.screen.capture` (2.4 секунды на 188 КБ)
//! плюс вызов зрения. Самый дешёвый вопрос на свете обходился дороже всего.
//!
//! Здесь и только здесь в теле живёт COM. Соседние модули о нём не знают: наружу торчат
//! `dispatch`, `handles`, `descriptors`, `adapter_descriptor` — обычные функции с JSON.
//! Апартамент инициализируется на СОБСТВЕННОМ потоке, который этот файл создаёт и хоронит
//! (см. `platform::read`), поэтому ни один чужой поток (в том числе рабочие потоки tokio,
//! которые переиспользуются файловыми глаголами) не оказывается втянут в COM.

use anyhow::{Context, Result, bail};
use praxis_body_protocol::{AdapterDescriptor, CapabilityDescriptor};
use serde::Deserialize;
use serde_json::{Map, Value, json};

/// Единственный глагол модуля.
pub const CAPABILITY: &str = "desktop.window.read";
pub const VERSION: u32 = 1;

/// Диспетчер спрашивает это ПЕРЕД веткой `starts_with("desktop.")`: имя глагола начинается
/// с того же префикса, и общая ветка увела бы его в `desktop::dispatch`, где его нет.
pub fn handles(capability: &str) -> bool {
    capability == CAPABILITY
}

pub fn descriptors() -> Vec<CapabilityDescriptor> {
    vec![CapabilityDescriptor {
        name: CAPABILITY.into(),
        version: VERSION,
        mutating: false,
        durable: false,
    }]
}

/// Отдельный адаптер, а не строка в `native-win32-desktop`. Тот адаптер честно назван
/// «типизированный Win32 без COM», и это остаётся правдой; читалка окна — другой механизм
/// с другой зависимостью, и в манифесте она обязана быть видна отдельно.
pub fn adapter_descriptor() -> AdapterDescriptor {
    AdapterDescriptor {
        name: "uia-window-reader".into(),
        version: "1".into(),
        capabilities: vec![CAPABILITY.to_string()],
        available: cfg!(windows),
    }
}

// ─── пределы ────────────────────────────────────────────────────────────────────────────
//
// Закон проекта: ни одного молчаливого предела. Всё, что ниже, приезжает в ответе в поле
// `limits` вместе со своим потолком, а всякая подрезка — в `truncated_by` и `notes`.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Limits {
    max_nodes: u64,
    max_depth: u64,
    max_children_per_node: u64,
    max_text_chars: u64,
    timeout_ms: u64,
}

/// Значения по умолчанию подобраны под смысл затеи: чтение обязано быть быстрее снимка
/// экрана (2.4 с). Поэтому дефолтный срок — 1800 мс: даже упёршись в него, глагол вернёт
/// ЧЕСТНЫЙ ЧАСТИЧНЫЙ ответ раньше, чем сделался бы скриншот. Нужен полный обход огромного
/// окна — она поднимает `timeout_ms` сама, потолок 20 с.
const DEFAULTS: Limits = Limits {
    max_nodes: 400,
    max_depth: 24,
    max_children_per_node: 128,
    max_text_chars: 240,
    timeout_ms: 1_800,
};

const CEILINGS: Limits = Limits {
    max_nodes: 5_000,
    max_depth: 64,
    max_children_per_node: 2_000,
    max_text_chars: 4_000,
    timeout_ms: 20_000,
};

const FLOORS: Limits = Limits {
    max_nodes: 1,
    max_depth: 0,
    max_children_per_node: 1,
    max_text_chars: 1,
    timeout_ms: 100,
};

/// Запас поверх `timeout_ms`, который ждёт вызывающая сторона: сам обход обязан уложиться
/// в свой срок, эта разница — только на дорогу до потока и обратно.
const WORKER_GRACE_MS: u64 = 750;

/// Только для запасного пути: `WM_GETTEXT` в чужой процесс может повиснуть на зависшем
/// окне, поэтому каждый текст спрашивается с таймаутом.
const WIN32_TEXT_TIMEOUT_MS: u32 = 150;

impl Limits {
    fn resolve(args: &ReadArgs, notes: &mut Vec<String>) -> Self {
        let mut limits = DEFAULTS;
        clamp(
            "max_nodes",
            args.max_nodes,
            &mut limits.max_nodes,
            FLOORS.max_nodes,
            CEILINGS.max_nodes,
            notes,
        );
        clamp(
            "max_depth",
            args.max_depth,
            &mut limits.max_depth,
            FLOORS.max_depth,
            CEILINGS.max_depth,
            notes,
        );
        clamp(
            "max_children_per_node",
            args.max_children_per_node,
            &mut limits.max_children_per_node,
            FLOORS.max_children_per_node,
            CEILINGS.max_children_per_node,
            notes,
        );
        clamp(
            "max_text_chars",
            args.max_text_chars,
            &mut limits.max_text_chars,
            FLOORS.max_text_chars,
            CEILINGS.max_text_chars,
            notes,
        );
        clamp(
            "timeout_ms",
            args.timeout_ms,
            &mut limits.timeout_ms,
            FLOORS.timeout_ms,
            CEILINGS.timeout_ms,
            notes,
        );
        limits
    }

    fn json(&self) -> Value {
        json!({
            "max_nodes": self.max_nodes,
            "max_nodes_ceiling": CEILINGS.max_nodes,
            "max_depth": self.max_depth,
            "max_depth_ceiling": CEILINGS.max_depth,
            "max_children_per_node": self.max_children_per_node,
            "max_children_per_node_ceiling": CEILINGS.max_children_per_node,
            "max_text_chars": self.max_text_chars,
            "max_text_chars_ceiling": CEILINGS.max_text_chars,
            "timeout_ms": self.timeout_ms,
            "timeout_ms_ceiling": CEILINGS.timeout_ms,
            "worker_grace_ms": WORKER_GRACE_MS,
            "win32_text_timeout_ms": WIN32_TEXT_TIMEOUT_MS,
        })
    }
}

/// Просьбу вне границ не отвергаем (это был бы забор) и не проглатываем молча
/// (это была бы ложь): подтягиваем к границе и говорим об этом вслух.
fn clamp(
    name: &str,
    requested: Option<u64>,
    target: &mut u64,
    floor: u64,
    ceiling: u64,
    notes: &mut Vec<String>,
) {
    let Some(requested) = requested else {
        return;
    };
    let effective = requested.clamp(floor, ceiling);
    if effective != requested {
        notes.push(format!(
            "{name}={requested} is outside [{floor}, {ceiling}]; this read used {effective}"
        ));
    }
    *target = effective;
}

// ─── аргументы ──────────────────────────────────────────────────────────────────────────

#[derive(Debug, Default, Deserialize)]
// Опечатка в имени аргумента иначе означала бы «я подняла кап, а он не поднялся» — тихое
// ограничение в чистом виде. Пусть лучше глагол назовёт неизвестное поле.
#[serde(deny_unknown_fields)]
struct ReadArgs {
    #[serde(default)]
    hwnd: Option<HwndArg>,
    #[serde(default)]
    backend: Option<String>,
    #[serde(default)]
    shape: Option<String>,
    #[serde(default)]
    text_contains: Option<String>,
    #[serde(default)]
    visible_only: Option<bool>,
    #[serde(default)]
    max_nodes: Option<u64>,
    #[serde(default)]
    max_depth: Option<u64>,
    #[serde(default)]
    max_children_per_node: Option<u64>,
    #[serde(default)]
    max_text_chars: Option<u64>,
    #[serde(default)]
    timeout_ms: Option<u64>,
}

/// Тот же разбор HWND, что у `desktop.window.activate`: она уже привыкла к «0x…» из
/// `desktop.window.list`, и читалка обязана принимать ровно то, что тот глагол отдал.
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
enum HwndArg {
    Number(u64),
    Text(String),
}

impl HwndArg {
    fn value(&self) -> Result<u64> {
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
                    value
                        .parse::<u64>()
                        .context("hwnd must be a positive integer or 0x-prefixed hexadecimal string")?
                }
            }
        };
        if value == 0 || value > usize::MAX as u64 {
            bail!("invalid hwnd")
        }
        Ok(value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Shape {
    Tree,
    Flat,
}

impl Shape {
    fn name(self) -> &'static str {
        match self {
            Self::Tree => "tree",
            Self::Flat => "flat",
        }
    }
}

#[derive(Debug, Clone)]
struct Plan {
    hwnd: Option<u64>,
    /// `true` — читать сразу обычным Win32, не поднимая UIA. Это не запрет и не режим
    /// безопасности, а рычаг: им проверяется запасной путь и им же можно взять дешёвое
    /// чтение, когда окно классическое, а UIA на нём тормозит.
    force_win32: bool,
    shape: Shape,
    needle: String,
    visible_only: bool,
    limits: Limits,
    notes: Vec<String>,
}

fn plan(args: Value) -> Result<Plan> {
    let args: ReadArgs = serde_json::from_value(args)
        .context("desktop.window.read arguments")?;
    let mut notes = Vec::new();
    let limits = Limits::resolve(&args, &mut notes);
    let shape = match args.shape.as_deref().map(str::trim).unwrap_or("tree") {
        "tree" => Shape::Tree,
        "flat" => Shape::Flat,
        other => bail!("shape must be tree or flat, not {other:?}"),
    };
    let force_win32 = match args.backend.as_deref().map(str::trim).unwrap_or("auto") {
        "auto" => false,
        "win32" => true,
        other => bail!("backend must be auto or win32, not {other:?}"),
    };
    Ok(Plan {
        hwnd: args.hwnd.as_ref().map(HwndArg::value).transpose()?,
        force_win32,
        shape,
        needle: args
            .text_contains
            .unwrap_or_default()
            .trim()
            .to_lowercase(),
        visible_only: args.visible_only.unwrap_or(false),
        limits,
        notes,
    })
}

// ─── прочитанное ────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct Rect {
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
}

impl Rect {
    fn is_empty(&self) -> bool {
        self.right <= self.left || self.bottom <= self.top
    }

    fn json(&self) -> Value {
        json!({
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.right - self.left,
            "height": self.bottom - self.top,
            // Точка, которой можно ткнуть мышью: без неё прочитанное некуда приложить,
            // и весь глагол теряет смысл. Это геометрический центр, а не «clickable point»
            // из UIA — за неё пришлось бы платить отдельным вызовом в чужой процесс.
            "center": {
                "x": self.left + (self.right - self.left) / 2,
                "y": self.top + (self.bottom - self.top) / 2,
            },
        })
    }
}

#[derive(Debug, Clone, Default)]
struct WindowInfo {
    hwnd: u64,
    title: String,
    class: String,
    pid: u32,
    rect: Option<Rect>,
    foreground: bool,
    resolved_from: &'static str,
}

impl WindowInfo {
    fn json(&self) -> Value {
        json!({
            "hwnd": format!("0x{:X}", self.hwnd),
            "title": self.title,
            "class": self.class,
            "pid": self.pid,
            "rect": self.rect.map(|rect| rect.json()),
            "foreground": self.foreground,
            "resolved_from": self.resolved_from,
        })
    }
}

#[derive(Debug, Clone, Default)]
struct Node {
    parent: Option<usize>,
    depth: u64,
    role: String,
    localized_role: Option<String>,
    name: Option<String>,
    value: Option<String>,
    automation_id: Option<String>,
    class: Option<String>,
    hwnd: Option<u64>,
    rect: Option<Rect>,
    enabled: Option<bool>,
    focused: Option<bool>,
    offscreen: Option<bool>,
    checked: Option<&'static str>,
    selected: Option<bool>,
    expanded: Option<&'static str>,
    text_truncated: bool,
    children_unread: Option<&'static str>,
}

impl Node {
    fn haystack(&self) -> String {
        let mut text = self.role.clone();
        for part in [
            self.name.as_deref(),
            self.value.as_deref(),
            self.automation_id.as_deref(),
            self.class.as_deref(),
            self.localized_role.as_deref(),
        ]
        .into_iter()
        .flatten()
        {
            text.push('\u{1}');
            text.push_str(part);
        }
        text.to_lowercase()
    }

    /// Ключа состояния НЕТ, когда элемент его не отдаёт. Отсутствие ключа значит
    /// «неизвестно», а не «false» — это разница между «кнопка не отмечена» и
    /// «у этой штуки вообще нет отметки», и врать здесь нельзя.
    fn json(&self, id: usize, children: Option<Vec<Value>>) -> Value {
        let mut object = Map::new();
        object.insert("id".into(), json!(id));
        object.insert("role".into(), json!(self.role));
        object.insert("depth".into(), json!(self.depth));
        if let Some(parent) = self.parent {
            object.insert("parent".into(), json!(parent));
        }
        for (key, value) in [
            ("name", self.name.as_ref()),
            ("value", self.value.as_ref()),
            ("automation_id", self.automation_id.as_ref()),
            ("class", self.class.as_ref()),
            ("localized_role", self.localized_role.as_ref()),
        ] {
            if let Some(value) = value {
                object.insert(key.into(), json!(value));
            }
        }
        if let Some(hwnd) = self.hwnd {
            object.insert("hwnd".into(), json!(format!("0x{hwnd:X}")));
        }
        if let Some(rect) = self.rect {
            object.insert("rect".into(), rect.json());
        }
        let mut state = Map::new();
        for (key, value) in [
            ("enabled", self.enabled),
            ("focused", self.focused),
            ("offscreen", self.offscreen),
            ("selected", self.selected),
        ] {
            if let Some(value) = value {
                state.insert(key.into(), json!(value));
            }
        }
        for (key, value) in [("checked", self.checked), ("expanded", self.expanded)] {
            if let Some(value) = value {
                state.insert(key.into(), json!(value));
            }
        }
        if !state.is_empty() {
            object.insert("state".into(), Value::Object(state));
        }
        if self.text_truncated {
            object.insert("text_truncated".into(), json!(true));
        }
        if let Some(reason) = self.children_unread {
            object.insert("children_unread".into(), json!(reason));
        }
        if let Some(children) = children {
            if !children.is_empty() {
                object.insert("children".into(), Value::Array(children));
            }
        }
        Value::Object(object)
    }
}

#[derive(Debug, Default)]
struct Walk {
    nodes: Vec<Node>,
    /// Элементы, которые обход УЖЕ УВИДЕЛ, но не успел прочитать. Это и есть честное
    /// «из скольких»: полное число узлов окна узнать нельзя, не прочитав их все.
    discovered_unread: u64,
    /// Узлы, чьих детей мы не раскрывали (упёрлись в глубину, ширину или срок).
    subtrees_unread: u64,
    skipped_offscreen: u64,
    depth_reached: u64,
    truncated_by: Vec<&'static str>,
    walk_ms: u64,
}

impl Walk {
    fn mark(&mut self, reason: &'static str) {
        if !self.truncated_by.contains(&reason) {
            self.truncated_by.push(reason);
        }
    }
}

/// Обрезка по единицам UTF-16 (как её видит Windows), а не по символам: иначе эмодзи
/// и суррогатные пары считались бы «одним», и предел был бы не тем, что заявлен.
fn clip_utf16(text: &str, limit: u64) -> (String, bool) {
    let limit = usize::try_from(limit).unwrap_or(usize::MAX);
    let mut result = String::new();
    let mut units = 0usize;
    for value in text.chars() {
        let width = value.len_utf16();
        if units + width > limit {
            return (result, true);
        }
        result.push(value);
        units += width;
    }
    (result, false)
}

fn role_name(control_type: i32) -> String {
    let known = match control_type {
        0 => "unknown",
        50000 => "button",
        50001 => "calendar",
        50002 => "check_box",
        50003 => "combo_box",
        50004 => "edit",
        50005 => "hyperlink",
        50006 => "image",
        50007 => "list_item",
        50008 => "list",
        50009 => "menu",
        50010 => "menu_bar",
        50011 => "menu_item",
        50012 => "progress_bar",
        50013 => "radio_button",
        50014 => "scroll_bar",
        50015 => "slider",
        50016 => "spinner",
        50017 => "status_bar",
        50018 => "tab",
        50019 => "tab_item",
        50020 => "text",
        50021 => "tool_bar",
        50022 => "tool_tip",
        50023 => "tree",
        50024 => "tree_item",
        50025 => "custom",
        50026 => "group",
        50027 => "thumb",
        50028 => "data_grid",
        50029 => "data_item",
        50030 => "document",
        50031 => "split_button",
        50032 => "window",
        50033 => "pane",
        50034 => "header",
        50035 => "header_item",
        50036 => "table",
        50037 => "title_bar",
        50038 => "separator",
        50039 => "semantic_zoom",
        50040 => "app_bar",
        // Неизвестный код показываем числом, а не выдаём за «custom»: пусть лучше она
        // увидит непонятное, чем поверит в понятное и неверное.
        _ => "",
    };
    if known.is_empty() {
        format!("control_type_{control_type}")
    } else {
        known.to_string()
    }
}

/// Роль по имени оконного класса — только для запасного Win32-пути, где никакой роли
/// не сообщают вообще.
fn win32_role(class: &str) -> &'static str {
    let class = class.to_ascii_lowercase();
    if class.contains("richedit") || class == "edit" {
        "edit"
    } else if class.contains("button") {
        "button"
    } else if class.contains("static") {
        "text"
    } else if class.contains("combobox") {
        "combo_box"
    } else if class.contains("listbox") || class.contains("syslistview") {
        "list"
    } else if class.contains("systreeview") {
        "tree"
    } else if class.contains("systabcontrol") {
        "tab"
    } else if class.contains("progress") {
        "progress_bar"
    } else if class.contains("scrollbar") {
        "scroll_bar"
    } else if class.contains("toolbarwindow") {
        "tool_bar"
    } else if class.contains("msctls_statusbar") {
        "status_bar"
    } else if class == "#32770" {
        "dialog"
    } else {
        "window"
    }
}

// ─── сборка ответа ──────────────────────────────────────────────────────────────────────

struct Rendered<'a> {
    plan: &'a Plan,
    window: &'a WindowInfo,
    walk: &'a Walk,
    backend: &'static str,
    backend_detail: &'static str,
    fallback_reason: Option<String>,
    elapsed_ms: u64,
    extra_notes: Vec<String>,
    workers_live: u64,
}

fn render(rendered: Rendered<'_>) -> Value {
    let Rendered {
        plan,
        window,
        walk,
        backend,
        backend_detail,
        fallback_reason,
        elapsed_ms,
        extra_notes,
        workers_live,
    } = rendered;

    let children_of = children_of(&walk.nodes);
    let keep = keep_flags(plan, &walk.nodes, &children_of);
    let kept = keep.iter().filter(|value| **value).count();

    let mut object = Map::new();
    object.insert("ok".into(), json!(true));
    object.insert("capability".into(), json!(CAPABILITY));
    object.insert("backend".into(), json!(backend));
    object.insert("backend_detail".into(), json!(backend_detail));
    object.insert(
        "fallback_reason".into(),
        fallback_reason.map(Value::String).unwrap_or(Value::Null),
    );
    object.insert("window".into(), window.json());
    object.insert("shape".into(), json!(plan.shape.name()));

    match plan.shape {
        Shape::Tree => {
            let root = (!walk.nodes.is_empty() && keep[0])
                .then(|| tree_json(0, &walk.nodes, &children_of, &keep))
                .unwrap_or(Value::Null);
            object.insert("root".into(), root);
        }
        Shape::Flat => {
            let items: Vec<Value> = walk
                .nodes
                .iter()
                .enumerate()
                .filter(|(index, _)| keep[*index])
                .map(|(index, node)| node.json(index, None))
                .collect();
            object.insert("items".into(), Value::Array(items));
        }
    }

    object.insert("nodes_read".into(), json!(walk.nodes.len()));
    object.insert("nodes_returned".into(), json!(kept));
    object.insert("discovered_unread".into(), json!(walk.discovered_unread));
    object.insert("subtrees_unread".into(), json!(walk.subtrees_unread));
    object.insert("skipped_offscreen".into(), json!(walk.skipped_offscreen));
    object.insert("depth_reached".into(), json!(walk.depth_reached));
    object.insert("truncated".into(), json!(!walk.truncated_by.is_empty()));
    object.insert("truncated_by".into(), json!(walk.truncated_by));
    // Полное число элементов окна известно ТОЛЬКО когда обход дошёл до конца: посчитать
    // их, не читая, нельзя — счёт стоит ровно столько же, сколько чтение.
    object.insert("total_known".into(), json!(walk.truncated_by.is_empty()));
    object.insert("limits".into(), plan.limits.json());
    object.insert("elapsed_ms".into(), json!(elapsed_ms));
    object.insert("walk_ms".into(), json!(walk.walk_ms));
    object.insert("uia_workers_live".into(), json!(workers_live));
    object.insert(
        "coordinates".into(),
        json!("screen pixels on the virtual desktop, same space desktop.input.perform takes"),
    );
    object.insert(
        "state_semantics".into(),
        json!("a missing state key means the element does not expose it; it does not mean false"),
    );
    object.insert(
        "ids".into(),
        json!("ids index this answer only and are not stable across reads; use hwnd or automation_id to re-find an element"),
    );

    let mut notes = plan.notes.clone();
    notes.extend(extra_notes);
    if !plan.needle.is_empty() {
        object.insert(
            "filter".into(),
            json!({
                "text_contains": plan.needle,
                "matched_or_ancestor": kept,
                "of_nodes_read": walk.nodes.len(),
                "rule": match plan.shape {
                    Shape::Tree => "tree keeps matching elements and their ancestors as the path",
                    Shape::Flat => "flat keeps only matching elements",
                },
            }),
        );
    }
    if walk.truncated_by.contains(&"max_nodes") {
        notes.push(format!(
            "stopped at max_nodes={}: {} more elements were already discovered and left unread, and there may be more behind them",
            plan.limits.max_nodes, walk.discovered_unread
        ));
    }
    if walk.truncated_by.contains(&"timeout") {
        notes.push(format!(
            "stopped at timeout_ms={}: this reading is partial, not the whole window",
            plan.limits.timeout_ms
        ));
    }
    if walk.truncated_by.contains(&"max_depth") {
        notes.push(format!(
            "{} subtrees were left unopened at max_depth={}",
            walk.subtrees_unread, plan.limits.max_depth
        ));
    }
    if walk.truncated_by.contains(&"max_children_per_node") {
        notes.push(format!(
            "at least one element has more than max_children_per_node={} children; the rest of that list was not read",
            plan.limits.max_children_per_node
        ));
    }
    if walk.skipped_offscreen > 0 {
        notes.push(format!(
            "visible_only=true skipped {} offscreen elements together with everything under them",
            walk.skipped_offscreen
        ));
    }
    if elapsed_ms > 1_000 && backend == "uia" {
        // Догадка названа догадкой: причина может быть и другой, но эта — самая частая,
        // и знать о ней полезнее, чем гадать над секундой на ровном месте.
        notes.push(
            "this read took over a second; one common cause is a first read of a Chromium/Electron window, which wakes its accessibility engine — later reads of the same window are usually much faster"
                .to_string(),
        );
    }
    object.insert("notes".into(), json!(notes));
    Value::Object(object)
}

fn children_of(nodes: &[Node]) -> Vec<Vec<usize>> {
    let mut children = vec![Vec::new(); nodes.len()];
    for (index, node) in nodes.iter().enumerate() {
        if let Some(parent) = node.parent {
            if parent < children.len() {
                children[parent].push(index);
            }
        }
    }
    children
}

fn keep_flags(plan: &Plan, nodes: &[Node], children: &[Vec<usize>]) -> Vec<bool> {
    if plan.needle.is_empty() {
        return vec![true; nodes.len()];
    }
    let mut keep: Vec<bool> = nodes
        .iter()
        .map(|node| node.haystack().contains(&plan.needle))
        .collect();
    if plan.shape == Shape::Flat {
        return keep;
    }
    // В дереве совпавший узел без предков нельзя ни показать, ни найти повторно —
    // поэтому путь до него сохраняется целиком.
    for index in (0..nodes.len()).rev() {
        if children[index].iter().any(|child| keep[*child]) {
            keep[index] = true;
        }
    }
    keep
}

fn tree_json(index: usize, nodes: &[Node], children: &[Vec<usize>], keep: &[bool]) -> Value {
    let kids: Vec<Value> = children[index]
        .iter()
        .filter(|child| keep[**child])
        .map(|child| tree_json(*child, nodes, children, keep))
        .collect();
    nodes[index].json(index, Some(kids))
}

// ─── вход ───────────────────────────────────────────────────────────────────────────────

/// Та же оговорка про рантайм, что у `desktop::dispatch`: обход UIA блокирующий, и на
/// однопоточном рантайме `block_on` внутри `block_on` встал бы намертво.
pub fn dispatch(capability: &str, args: Value) -> Result<Value> {
    let Ok(handle) = tokio::runtime::Handle::try_current() else {
        return run(capability, args);
    };
    if handle.runtime_flavor() != tokio::runtime::RuntimeFlavor::MultiThread {
        bail!("desktop.window.read requires a multi-thread Tokio runtime")
    }
    let capability = capability.to_string();
    tokio::task::block_in_place(|| {
        handle.block_on(async move {
            tokio::task::spawn_blocking(move || run(&capability, args))
                .await
                .context("window reader worker stopped")?
        })
    })
}

fn run(capability: &str, args: Value) -> Result<Value> {
    if !handles(capability) {
        bail!("unknown window reader capability {capability}")
    }
    let plan = plan(args)?;
    platform::read(&plan)
}

#[cfg(not(windows))]
mod platform {
    use anyhow::{Result, bail};
    use serde_json::Value;

    pub fn read(_plan: &super::Plan) -> Result<Value> {
        bail!("desktop.window.read requires an interactive Windows session")
    }
}

#[cfg(windows)]
mod platform {
    use std::collections::VecDeque;
    use std::ffi::c_void;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::mpsc::{self, RecvTimeoutError};
    use std::thread;
    use std::time::{Duration, Instant};

    use anyhow::{Context, Result, bail};
    use serde_json::Value;
    use windows::Win32::Foundation::{HWND, LPARAM, RECT, WPARAM};
    use windows::Win32::System::Com::{
        CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED, CoCreateInstance, CoInitializeEx,
        CoUninitialize,
    };
    use windows::Win32::UI::Accessibility::{
        AutomationElementMode_Full, CUIAutomation, ExpandCollapseState_Collapsed,
        ExpandCollapseState_Expanded, ExpandCollapseState_LeafNode,
        ExpandCollapseState_PartiallyExpanded, IUIAutomation, IUIAutomationElement,
        IUIAutomationExpandCollapsePattern,
        IUIAutomationSelectionItemPattern, IUIAutomationTogglePattern, IUIAutomationValuePattern,
        ToggleState_Indeterminate, ToggleState_Off, ToggleState_On, TreeScope_Element,
        UIA_AutomationIdPropertyId, UIA_BoundingRectanglePropertyId, UIA_ClassNamePropertyId,
        UIA_ControlTypePropertyId, UIA_ExpandCollapsePatternId, UIA_HasKeyboardFocusPropertyId,
        UIA_IsEnabledPropertyId, UIA_IsOffscreenPropertyId, UIA_LocalizedControlTypePropertyId,
        UIA_NamePropertyId, UIA_NativeWindowHandlePropertyId, UIA_PATTERN_ID, UIA_PROPERTY_ID,
        UIA_ProcessIdPropertyId, UIA_SelectionItemPatternId, UIA_TogglePatternId,
        UIA_ValuePatternId,
    };
    use windows::Win32::UI::Input::KeyboardAndMouse::IsWindowEnabled;
    use windows::Win32::UI::WindowsAndMessaging::{
        GW_CHILD, GW_HWNDNEXT, GetClassNameW, GetForegroundWindow, GetWindow, GetWindowRect,
        GetWindowTextLengthW, GetWindowTextW, GetWindowThreadProcessId, IsWindow, IsWindowVisible,
        SMTO_ABORTIFHUNG, SendMessageTimeoutW, WM_GETTEXT, WM_GETTEXTLENGTH,
    };

    use super::{
        Node, Plan, Rect, Rendered, WINDOW_READ_UIA_DETAIL, WINDOW_READ_WIN32_DETAIL,
        WIN32_TEXT_TIMEOUT_MS, WORKER_GRACE_MS, Walk, WindowInfo, clip_utf16, render, role_name,
        win32_role,
    };

    /// Живые потоки UIA. Растёт только тогда, когда предыдущее чтение не ответило в срок и
    /// было брошено: такой поток застрял в вызове к зависшему окну и когда-нибудь выйдет
    /// сам. Число едет в ответе — брошенный поток не должен быть невидимым.
    static UIA_WORKERS: AtomicU64 = AtomicU64::new(0);

    const CACHED_PROPERTIES: &[UIA_PROPERTY_ID] = &[
        UIA_NamePropertyId,
        UIA_ControlTypePropertyId,
        UIA_LocalizedControlTypePropertyId,
        UIA_BoundingRectanglePropertyId,
        UIA_IsEnabledPropertyId,
        UIA_IsOffscreenPropertyId,
        UIA_HasKeyboardFocusPropertyId,
        UIA_AutomationIdPropertyId,
        UIA_ClassNamePropertyId,
        UIA_NativeWindowHandlePropertyId,
        UIA_ProcessIdPropertyId,
    ];

    const CACHED_PATTERNS: &[UIA_PATTERN_ID] = &[
        UIA_ValuePatternId,
        UIA_TogglePatternId,
        UIA_SelectionItemPatternId,
        UIA_ExpandCollapsePatternId,
    ];

    /// Апартамент COM. Единственное место в теле, где вызывается `CoInitializeEx`.
    ///
    /// MTA, а не STA: STA обязывает поток крутить оконный цикл сообщений, иначе входящие
    /// вызовы просто не доставляются и клиент виснет; наш поток никакого цикла не крутит.
    /// Microsoft для клиентов UI Automation и рекомендует MTA. Ветка `RPC_E_CHANGED_MODE`
    /// оставлена честности ради: если поток уже чей-то STA, мы НЕ трогаем чужой апартамент
    /// и не вызываем `CoUninitialize` — но на своём собственном потоке этого не бывает.
    struct ComApartment {
        owned: bool,
    }

    impl ComApartment {
        fn enter() -> Self {
            let hr = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) };
            // S_OK и S_FALSE оба требуют парного CoUninitialize; всё остальное — нет.
            Self { owned: hr.is_ok() }
        }
    }

    impl Drop for ComApartment {
        fn drop(&mut self) {
            if self.owned {
                unsafe { CoUninitialize() };
            }
        }
    }

    pub fn read(plan: &Plan) -> Result<Value> {
        let started = Instant::now();
        let window = resolve_window(plan)?;
        if plan.force_win32 {
            return fallback(
                plan,
                &window,
                "backend=win32 was asked for, so UI Automation was not consulted at all"
                    .to_string(),
                started,
            );
        }

        let (sender, receiver) = mpsc::channel();
        let job = plan.clone();
        let handle = window.hwnd;
        UIA_WORKERS.fetch_add(1, Ordering::SeqCst);
        // COM поднимается на СВОЁМ потоке и умирает вместе с ним. Так апартамент не
        // протекает в рабочие потоки tokio, которые переиспользуются файловыми глаголами.
        let spawned = thread::Builder::new()
            .name("praxis-uia-read".into())
            .spawn(move || {
                let _apartment = ComApartment::enter();
                let outcome = uia_walk(handle, &job);
                let _ = sender.send(outcome);
                UIA_WORKERS.fetch_sub(1, Ordering::SeqCst);
            });
        if let Err(error) = spawned {
            UIA_WORKERS.fetch_sub(1, Ordering::SeqCst);
            return fallback(
                plan,
                &window,
                format!("could not start the uia worker thread: {error}"),
                started,
            );
        }

        let grace = Duration::from_millis(plan.limits.timeout_ms + WORKER_GRACE_MS);
        match receiver.recv_timeout(grace) {
            Ok(Ok(walk)) => Ok(render(Rendered {
                plan,
                window: &window,
                walk: &walk,
                backend: "uia",
                backend_detail: WINDOW_READ_UIA_DETAIL,
                fallback_reason: None,
                elapsed_ms: started.elapsed().as_millis() as u64,
                extra_notes: Vec::new(),
                workers_live: UIA_WORKERS.load(Ordering::SeqCst),
            })),
            Ok(Err(error)) => fallback(
                plan,
                &window,
                format!("UI Automation did not come up: {error:#}"),
                started,
            ),
            Err(RecvTimeoutError::Timeout) => fallback(
                plan,
                &window,
                format!(
                    "UI Automation did not answer within timeout_ms={} plus worker_grace_ms={}; the target window is probably busy and its reader thread is still running",
                    plan.limits.timeout_ms, WORKER_GRACE_MS
                ),
                started,
            ),
            Err(RecvTimeoutError::Disconnected) => fallback(
                plan,
                &window,
                "the uia worker thread ended without an answer".to_string(),
                started,
            ),
        }
    }

    /// Немота UIA не должна становиться немотой обоими ртами: обычный Win32 читает меньше
    /// (Chrome, WPF и UWP для него немые), но то, что он читает, — правда, и причина
    /// перехода названа.
    fn fallback(
        plan: &Plan,
        window: &WindowInfo,
        reason: String,
        started: Instant,
    ) -> Result<Value> {
        let walk = win32_walk(window, plan);
        Ok(render(Rendered {
            plan,
            window,
            walk: &walk,
            backend: "win32",
            backend_detail: WINDOW_READ_WIN32_DETAIL,
            fallback_reason: Some(reason),
            elapsed_ms: started.elapsed().as_millis() as u64,
            extra_notes: vec![
                "this is the plain Win32 reading: it sees classic child windows only, so Chromium, Electron, WPF and UWP surfaces look empty here even when they are full"
                    .to_string(),
                "checked/selected/expanded are absent on this path because Win32 does not report them; absent means unknown, not false"
                    .to_string(),
            ],
            workers_live: UIA_WORKERS.load(Ordering::SeqCst),
        }))
    }

    fn resolve_window(plan: &Plan) -> Result<WindowInfo> {
        let (hwnd, resolved_from) = match plan.hwnd {
            Some(value) => (HWND(value as usize as *mut c_void), "argument"),
            None => (unsafe { GetForegroundWindow() }, "foreground"),
        };
        if hwnd.0.is_null() {
            bail!("there is no foreground window in the interactive desktop")
        }
        if !unsafe { IsWindow(Some(hwnd)) }.as_bool() {
            bail!("window 0x{:X} no longer exists", hwnd.0 as usize)
        }
        let mut pid = 0u32;
        unsafe { GetWindowThreadProcessId(hwnd, Some(&mut pid)) };
        Ok(WindowInfo {
            hwnd: hwnd.0 as usize as u64,
            title: window_title(hwnd),
            class: window_class(hwnd),
            pid,
            rect: window_rect(hwnd),
            foreground: unsafe { GetForegroundWindow() } == hwnd,
            resolved_from,
        })
    }

    fn uia_walk(handle: u64, plan: &Plan) -> Result<Walk> {
        let started = Instant::now();
        let deadline = started + Duration::from_millis(plan.limits.timeout_ms);
        let hwnd = HWND(handle as usize as *mut c_void);

        let automation: IUIAutomation =
            unsafe { CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER) }
                .context("CoCreateInstance(CUIAutomation)")?;
        let cache = unsafe { automation.CreateCacheRequest() }.context("CreateCacheRequest")?;
        let view = unsafe { automation.ControlViewCondition() }.context("ControlViewCondition")?;
        unsafe {
            cache.SetTreeScope(TreeScope_Element)?;
            cache.SetTreeFilter(&view)?;
            // Full, а не None: по этим же элементам мы продолжаем шагать ходоком, а элемент
            // без полной ссылки умеет только отдать своё содержимое и никуда не ведёт.
            cache.SetAutomationElementMode(AutomationElementMode_Full)?;
            for property in CACHED_PROPERTIES {
                cache.AddProperty(*property)?;
            }
            for pattern in CACHED_PATTERNS {
                cache.AddPattern(*pattern)?;
            }
        }
        let root = unsafe { automation.ElementFromHandleBuildCache(hwnd, &cache) }
            .context("ElementFromHandle")?;
        // Ходок по control view: это то дерево, которое видит человек. Сырое (raw) дерево
        // вдвое-втрое толще и состоит наполовину из невидимых обёрток.
        let walker = unsafe { automation.ControlViewWalker() }.context("ControlViewWalker")?;

        let mut walk = Walk::default();
        let mut queue: VecDeque<(IUIAutomationElement, u64, Option<usize>)> = VecDeque::new();
        queue.push_back((root, 0, None));

        while !queue.is_empty() {
            if walk.nodes.len() as u64 >= plan.limits.max_nodes {
                walk.mark("max_nodes");
                break;
            }
            if Instant::now() >= deadline {
                walk.mark("timeout");
                break;
            }
            let (element, depth, parent) = queue.pop_front().expect("queue is not empty");
            let mut node = node_from(&element, depth, parent, plan);
            if plan.visible_only && node.offscreen == Some(true) {
                walk.skipped_offscreen += 1;
                continue;
            }
            walk.depth_reached = walk.depth_reached.max(depth);
            let index = walk.nodes.len();

            if depth + 1 > plan.limits.max_depth {
                node.children_unread = Some("max_depth");
                walk.subtrees_unread += 1;
                walk.mark("max_depth");
            } else {
                let mut child = unsafe { walker.GetFirstChildElementBuildCache(&element, &cache) }
                    .ok();
                let mut seen = 0u64;
                while let Some(current) = child {
                    if seen >= plan.limits.max_children_per_node {
                        node.children_unread = Some("max_children_per_node");
                        walk.subtrees_unread += 1;
                        walk.mark("max_children_per_node");
                        break;
                    }
                    if Instant::now() >= deadline {
                        node.children_unread = Some("timeout");
                        walk.subtrees_unread += 1;
                        walk.mark("timeout");
                        break;
                    }
                    let next =
                        unsafe { walker.GetNextSiblingElementBuildCache(&current, &cache) }.ok();
                    queue.push_back((current, depth + 1, Some(index)));
                    child = next;
                    seen += 1;
                }
            }
            walk.nodes.push(node);
        }

        walk.discovered_unread = queue.len() as u64;
        walk.walk_ms = started.elapsed().as_millis() as u64;
        Ok(walk)
    }

    fn node_from(
        element: &IUIAutomationElement,
        depth: u64,
        parent: Option<usize>,
        plan: &Plan,
    ) -> Node {
        let mut truncated = false;
        let mut text = |value: Option<String>| -> Option<String> {
            let value = value?;
            if value.is_empty() {
                return None;
            }
            let (clipped, cut) = clip_utf16(&value, plan.limits.max_text_chars);
            truncated |= cut;
            Some(clipped)
        };

        let name = text(unsafe { element.CachedName() }.ok().map(|v| v.to_string()));
        let localized_role = text(
            unsafe { element.CachedLocalizedControlType() }
                .ok()
                .map(|v| v.to_string()),
        );
        let automation_id = text(
            unsafe { element.CachedAutomationId() }
                .ok()
                .map(|v| v.to_string()),
        );
        let class = text(unsafe { element.CachedClassName() }.ok().map(|v| v.to_string()));
        let value = text(
            unsafe { element.GetCachedPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId) }
                .ok()
                .and_then(|pattern| unsafe { pattern.CachedValue() }.ok())
                .map(|v| v.to_string()),
        );

        let checked = unsafe {
            element.GetCachedPatternAs::<IUIAutomationTogglePattern>(UIA_TogglePatternId)
        }
        .ok()
        .and_then(|pattern| unsafe { pattern.CachedToggleState() }.ok())
        // Сравнение, а не match по константам: имена этих констант не в верхнем регистре,
        // и в позиции образца компилятор справедливо предупреждает — такой образец легко
        // спутать со свежей привязкой, которая совпала бы с чем угодно.
        .and_then(|state| {
            if state == ToggleState_On {
                Some("on")
            } else if state == ToggleState_Off {
                Some("off")
            } else if state == ToggleState_Indeterminate {
                Some("mixed")
            } else {
                None
            }
        });
        let selected = unsafe {
            element.GetCachedPatternAs::<IUIAutomationSelectionItemPattern>(
                UIA_SelectionItemPatternId,
            )
        }
        .ok()
        .and_then(|pattern| unsafe { pattern.CachedIsSelected() }.ok())
        .map(|value| value.as_bool());
        let expanded = unsafe {
            element.GetCachedPatternAs::<IUIAutomationExpandCollapsePattern>(
                UIA_ExpandCollapsePatternId,
            )
        }
        .ok()
        .and_then(|pattern| unsafe { pattern.CachedExpandCollapseState() }.ok())
        .and_then(|state| {
            if state == ExpandCollapseState_Expanded {
                Some("expanded")
            } else if state == ExpandCollapseState_Collapsed {
                Some("collapsed")
            } else if state == ExpandCollapseState_PartiallyExpanded {
                Some("partially_expanded")
            } else if state == ExpandCollapseState_LeafNode {
                Some("leaf")
            } else {
                None
            }
        });

        Node {
            parent,
            depth,
            role: role_name(
                unsafe { element.CachedControlType() }
                    .map(|value| value.0)
                    .unwrap_or_default(),
            ),
            localized_role,
            name,
            value,
            automation_id,
            class,
            hwnd: unsafe { element.CachedNativeWindowHandle() }
                .ok()
                .map(|value| value.0 as usize as u64)
                .filter(|value| *value != 0),
            rect: unsafe { element.CachedBoundingRectangle() }
                .ok()
                .map(rect_of)
                .filter(|rect| !rect.is_empty()),
            enabled: unsafe { element.CachedIsEnabled() }
                .ok()
                .map(|value| value.as_bool()),
            focused: unsafe { element.CachedHasKeyboardFocus() }
                .ok()
                .map(|value| value.as_bool()),
            offscreen: unsafe { element.CachedIsOffscreen() }
                .ok()
                .map(|value| value.as_bool()),
            checked,
            selected,
            expanded,
            text_truncated: truncated,
            children_unread: None,
        }
    }

    fn win32_walk(window: &WindowInfo, plan: &Plan) -> Walk {
        let started = Instant::now();
        let deadline = started
            + Duration::from_millis(plan.limits.timeout_ms)
            + Duration::from_millis(WORKER_GRACE_MS);
        let mut walk = Walk::default();
        let mut queue: VecDeque<(HWND, u64, Option<usize>)> = VecDeque::new();
        queue.push_back((HWND(window.hwnd as usize as *mut c_void), 0, None));

        while !queue.is_empty() {
            if walk.nodes.len() as u64 >= plan.limits.max_nodes {
                walk.mark("max_nodes");
                break;
            }
            if Instant::now() >= deadline {
                walk.mark("timeout");
                break;
            }
            let (hwnd, depth, parent) = queue.pop_front().expect("queue is not empty");
            let visible = unsafe { IsWindowVisible(hwnd) }.as_bool();
            if plan.visible_only && !visible {
                walk.skipped_offscreen += 1;
                continue;
            }
            walk.depth_reached = walk.depth_reached.max(depth);
            let index = walk.nodes.len();
            let class = window_class(hwnd);
            let (name, name_cut) = clip_utf16(&window_title(hwnd), plan.limits.max_text_chars);
            let (class_text, class_cut) = clip_utf16(&class, plan.limits.max_text_chars);
            let mut node = Node {
                parent,
                depth,
                role: win32_role(&class).to_string(),
                name: (!name.is_empty()).then_some(name),
                class: (!class_text.is_empty()).then_some(class_text),
                hwnd: Some(hwnd.0 as usize as u64),
                rect: window_rect(hwnd),
                enabled: Some(unsafe { IsWindowEnabled(hwnd) }.as_bool()),
                offscreen: Some(!visible),
                text_truncated: name_cut || class_cut,
                ..Node::default()
            };

            if depth + 1 > plan.limits.max_depth {
                node.children_unread = Some("max_depth");
                walk.subtrees_unread += 1;
                walk.mark("max_depth");
            } else {
                let mut child = unsafe { GetWindow(hwnd, GW_CHILD) }.ok();
                let mut seen = 0u64;
                while let Some(current) = child {
                    if seen >= plan.limits.max_children_per_node {
                        node.children_unread = Some("max_children_per_node");
                        walk.subtrees_unread += 1;
                        walk.mark("max_children_per_node");
                        break;
                    }
                    queue.push_back((current, depth + 1, Some(index)));
                    child = unsafe { GetWindow(current, GW_HWNDNEXT) }.ok();
                    seen += 1;
                }
            }
            walk.nodes.push(node);
        }

        walk.discovered_unread = queue.len() as u64;
        walk.walk_ms = started.elapsed().as_millis() as u64;
        walk
    }

    fn rect_of(rect: RECT) -> Rect {
        Rect {
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
        }
    }

    fn window_rect(hwnd: HWND) -> Option<Rect> {
        let mut rect = RECT::default();
        unsafe { GetWindowRect(hwnd, &mut rect) }
            .ok()
            .map(|_| rect_of(rect))
            .filter(|rect| !rect.is_empty())
    }

    fn window_class(hwnd: HWND) -> String {
        let mut buffer = vec![0u16; 512];
        let copied = unsafe { GetClassNameW(hwnd, &mut buffer) }.max(0) as usize;
        String::from_utf16_lossy(&buffer[..copied.min(buffer.len())])
    }

    /// `GetWindowTextW` в чужом процессе отдаёт только заголовок окна: надпись на кнопке
    /// живёт за `WM_GETTEXT`, а его нельзя слать без срока — зависшее окно повесило бы и нас.
    fn window_title(hwnd: HWND) -> String {
        let direct = {
            let length = unsafe { GetWindowTextLengthW(hwnd) }.max(0) as usize;
            let mut buffer = vec![0u16; length.saturating_add(1).max(2)];
            let copied = unsafe { GetWindowTextW(hwnd, &mut buffer) }.max(0) as usize;
            String::from_utf16_lossy(&buffer[..copied.min(buffer.len())])
        };
        if !direct.is_empty() {
            return direct;
        }
        let mut length = 0usize;
        unsafe {
            SendMessageTimeoutW(
                hwnd,
                WM_GETTEXTLENGTH,
                WPARAM(0),
                LPARAM(0),
                SMTO_ABORTIFHUNG,
                WIN32_TEXT_TIMEOUT_MS,
                Some(&mut length),
            )
        };
        if length == 0 || length > 8_192 {
            return String::new();
        }
        let mut buffer = vec![0u16; length + 1];
        let mut copied = 0usize;
        unsafe {
            SendMessageTimeoutW(
                hwnd,
                WM_GETTEXT,
                WPARAM(buffer.len()),
                LPARAM(buffer.as_mut_ptr() as isize),
                SMTO_ABORTIFHUNG,
                WIN32_TEXT_TIMEOUT_MS,
                Some(&mut copied),
            )
        };
        String::from_utf16_lossy(&buffer[..copied.min(buffer.len())])
    }
}

const WINDOW_READ_UIA_DETAIL: &str =
    "IUIAutomation control view, properties and patterns read from one cache request, walked on a dedicated MTA thread";
const WINDOW_READ_WIN32_DETAIL: &str =
    "plain Win32 child-window walk with timed WM_GETTEXT; no COM involved";

#[cfg(test)]
mod tests {
    use super::*;

    fn test_plan(shape: Shape, needle: &str) -> Plan {
        Plan {
            hwnd: None,
            force_win32: false,
            shape,
            needle: needle.to_string(),
            visible_only: false,
            limits: DEFAULTS,
            notes: Vec::new(),
        }
    }

    fn node(parent: Option<usize>, depth: u64, role: &str, name: &str) -> Node {
        Node {
            parent,
            depth,
            role: role.to_string(),
            name: (!name.is_empty()).then(|| name.to_string()),
            ..Node::default()
        }
    }

    fn sample_walk() -> Walk {
        Walk {
            nodes: vec![
                node(None, 0, "window", "Notepad"),
                node(Some(0), 1, "menu_bar", "Menu"),
                node(Some(1), 2, "menu_item", "Save"),
                node(Some(0), 1, "edit", "Document"),
            ],
            depth_reached: 2,
            ..Walk::default()
        }
    }

    fn rendered(plan: &Plan, walk: &Walk) -> Value {
        render(Rendered {
            plan,
            window: &WindowInfo {
                hwnd: 0x1234,
                title: "Notepad".into(),
                class: "Notepad".into(),
                pid: 42,
                rect: Some(Rect {
                    left: 0,
                    top: 0,
                    right: 100,
                    bottom: 50,
                }),
                foreground: true,
                resolved_from: "foreground",
            },
            walk,
            backend: "uia",
            backend_detail: WINDOW_READ_UIA_DETAIL,
            fallback_reason: None,
            elapsed_ms: 17,
            extra_notes: Vec::new(),
            workers_live: 0,
        })
    }

    #[test]
    fn the_module_declares_exactly_one_verb() {
        assert!(handles(CAPABILITY));
        assert!(!handles("desktop.window.list"));
        let descriptors = descriptors();
        assert_eq!(descriptors.len(), 1);
        assert_eq!(descriptors[0].name, CAPABILITY);
        assert!(!descriptors[0].mutating);
        let adapter = adapter_descriptor();
        assert_eq!(adapter.capabilities, vec![CAPABILITY.to_string()]);
        assert_eq!(adapter.available, cfg!(windows));
    }

    #[test]
    fn an_unknown_capability_is_refused_by_name() {
        let error = run("desktop.window.list", json!({})).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("unknown window reader capability desktop.window.list"),
            "{error}"
        );
    }

    #[test]
    fn every_named_cap_ships_its_ceiling_and_nothing_else_hides() {
        let limits = DEFAULTS.json();
        let object = limits.as_object().unwrap();
        for name in [
            "max_nodes",
            "max_depth",
            "max_children_per_node",
            "max_text_chars",
            "timeout_ms",
        ] {
            let value = object[name].as_u64().unwrap();
            let ceiling = object[&format!("{name}_ceiling")].as_u64().unwrap();
            assert!(value <= ceiling, "{name} {value} > {ceiling}");
        }
        // Пять капов со своими потолками плюс два справочных срока. Если кто-то добавит
        // ограничение и забудет назвать его здесь — этот счёт разойдётся.
        assert_eq!(object.len(), 12);
        assert!(object.contains_key("worker_grace_ms"));
        assert!(object.contains_key("win32_text_timeout_ms"));
    }

    #[test]
    fn a_cap_out_of_range_is_pulled_to_the_border_and_said_out_loud() {
        let stretched = plan(json!({"max_nodes": 999_999, "timeout_ms": 0})).unwrap();
        assert_eq!(stretched.limits.max_nodes, CEILINGS.max_nodes);
        assert_eq!(stretched.limits.timeout_ms, FLOORS.timeout_ms);
        assert!(
            stretched
                .notes
                .iter()
                .any(|note| note.contains("max_nodes=999999") && note.contains("5000")),
            "{:?}",
            stretched.notes
        );
        assert!(
            stretched
                .notes
                .iter()
                .any(|note| note.contains("timeout_ms=0") && note.contains("100")),
            "{:?}",
            stretched.notes
        );
        // Просьба внутри границ проходит молча и без правок.
        let quiet = plan(json!({"max_nodes": 42})).unwrap();
        assert_eq!(quiet.limits.max_nodes, 42);
        assert!(quiet.notes.is_empty());
    }

    #[test]
    fn a_misspelled_argument_is_named_instead_of_ignored() {
        let error = plan(json!({"max_node": 10})).unwrap_err();
        assert!(error.to_string().contains("desktop.window.read arguments"));
        assert!(format!("{error:#}").contains("max_node"), "{error:#}");
    }

    #[test]
    fn shape_and_hwnd_are_parsed_the_same_way_window_list_prints_them() {
        assert_eq!(plan(json!({"shape": "flat"})).unwrap().shape, Shape::Flat);
        assert_eq!(plan(json!({})).unwrap().shape, Shape::Tree);
        let error = plan(json!({"shape": "list"})).unwrap_err();
        assert!(error.to_string().contains("shape must be tree or flat"));

        assert_eq!(plan(json!({"hwnd": "0x1F4"})).unwrap().hwnd, Some(500));
        assert_eq!(plan(json!({"hwnd": "500"})).unwrap().hwnd, Some(500));
        assert_eq!(plan(json!({"hwnd": 500})).unwrap().hwnd, Some(500));
        assert!(plan(json!({"hwnd": 0})).unwrap_err().to_string().contains("invalid hwnd"));
    }

    #[test]
    fn the_plain_win32_path_can_be_asked_for_by_name() {
        assert!(!plan(json!({})).unwrap().force_win32);
        assert!(!plan(json!({"backend": "auto"})).unwrap().force_win32);
        assert!(plan(json!({"backend": "win32"})).unwrap().force_win32);
        let error = plan(json!({"backend": "uia"})).unwrap_err();
        assert!(error.to_string().contains("backend must be auto or win32"));
    }

    #[test]
    fn text_is_clipped_by_utf16_units_not_by_characters() {
        // Суррогатная пара занимает две единицы UTF-16: при лимите 3 влезает одна пара
        // и один обычный символ, а не две пары.
        let (text, cut) = clip_utf16("𐐀𐐀", 3);
        assert_eq!(text, "𐐀");
        assert!(cut);
        let (text, cut) = clip_utf16("𐐀a", 3);
        assert_eq!(text, "𐐀a");
        assert!(!cut);
        let (text, cut) = clip_utf16("abc", 3);
        assert_eq!(text, "abc");
        assert!(!cut);
    }

    #[test]
    fn every_uia_control_type_has_a_word_and_a_stranger_keeps_its_number() {
        for id in 50_000..=50_040 {
            let role = role_name(id);
            assert!(
                !role.starts_with("control_type_"),
                "control type {id} has no name"
            );
        }
        assert_eq!(role_name(0), "unknown");
        assert_eq!(role_name(50_041), "control_type_50041");
        assert_eq!(role_name(50_000), "button");
        assert_eq!(role_name(50_002), "check_box");
    }

    #[test]
    fn win32_classes_map_to_readable_roles() {
        assert_eq!(win32_role("Button"), "button");
        assert_eq!(win32_role("RichEdit50W"), "edit");
        assert_eq!(win32_role("Edit"), "edit");
        assert_eq!(win32_role("Static"), "text");
        assert_eq!(win32_role("#32770"), "dialog");
        assert_eq!(win32_role("Chrome_WidgetWin_1"), "window");
    }

    #[test]
    fn a_tree_answer_keeps_parent_links_and_child_order() {
        let walk = sample_walk();
        let value = rendered(&test_plan(Shape::Tree, ""), &walk);
        assert_eq!(value["shape"], "tree");
        assert_eq!(value["nodes_returned"], 4);
        assert_eq!(value["window"]["hwnd"], "0x1234");
        let root = &value["root"];
        assert_eq!(root["role"], "window");
        assert_eq!(root["children"][0]["name"], "Menu");
        assert_eq!(root["children"][0]["children"][0]["name"], "Save");
        assert_eq!(root["children"][1]["name"], "Document");
        assert_eq!(root["children"][1]["parent"], 0);
    }

    #[test]
    fn coordinates_come_with_a_point_that_can_actually_be_clicked() {
        let mut walk = sample_walk();
        walk.nodes[3].rect = Some(Rect {
            left: 10,
            top: 20,
            right: 110,
            bottom: 60,
        });
        let value = rendered(&test_plan(Shape::Flat, ""), &walk);
        let edit = &value["items"][3];
        assert_eq!(edit["rect"]["width"], 100);
        assert_eq!(edit["rect"]["height"], 40);
        assert_eq!(edit["rect"]["center"]["x"], 60);
        assert_eq!(edit["rect"]["center"]["y"], 40);
    }

    #[test]
    fn a_filter_keeps_the_path_in_a_tree_and_only_the_hits_when_flat() {
        let walk = sample_walk();
        let tree = rendered(&test_plan(Shape::Tree, "save"), &walk);
        assert_eq!(tree["nodes_returned"], 3);
        assert_eq!(tree["root"]["children"][0]["children"][0]["name"], "Save");
        assert_eq!(tree["root"]["children"].as_array().unwrap().len(), 1);
        assert_eq!(tree["filter"]["text_contains"], "save");

        let flat = rendered(&test_plan(Shape::Flat, "save"), &walk);
        assert_eq!(flat["nodes_returned"], 1);
        assert_eq!(flat["items"].as_array().unwrap().len(), 1);
        assert_eq!(flat["items"][0]["name"], "Save");

        // Ничего не нашлось — это пусто, а не «не смогла прочитать».
        let empty = rendered(&test_plan(Shape::Tree, "нетакого"), &walk);
        assert_eq!(empty["nodes_returned"], 0);
        assert_eq!(empty["root"], Value::Null);
        assert_eq!(empty["nodes_read"], 4);
    }

    #[test]
    fn a_whole_reading_says_so_and_a_cut_one_says_how_much_is_left() {
        let plan = test_plan(Shape::Tree, "");
        let whole = rendered(&plan, &sample_walk());
        assert_eq!(whole["total_known"], true);
        assert_eq!(whole["truncated"], false);
        assert_eq!(whole["truncated_by"].as_array().unwrap().len(), 0);
        assert_eq!(whole["notes"].as_array().unwrap().len(), 0);

        let mut cut = sample_walk();
        cut.mark("max_nodes");
        cut.discovered_unread = 212;
        let value = rendered(&plan, &cut);
        assert_eq!(value["total_known"], false);
        assert_eq!(value["truncated"], true);
        assert_eq!(value["truncated_by"][0], "max_nodes");
        assert_eq!(value["discovered_unread"], 212);
        let notes = value["notes"].as_array().unwrap();
        assert!(
            notes.iter().any(|note| {
                let note = note.as_str().unwrap();
                note.contains("max_nodes=400") && note.contains("212 more")
            }),
            "{notes:?}"
        );
    }

    #[test]
    fn an_unknown_state_is_absent_and_never_printed_as_false() {
        let mut walk = sample_walk();
        walk.nodes[2].enabled = Some(false);
        walk.nodes[2].checked = Some("on");
        let value = rendered(&test_plan(Shape::Flat, ""), &walk);
        let save = &value["items"][2];
        assert_eq!(save["state"]["enabled"], false);
        assert_eq!(save["state"]["checked"], "on");
        assert!(save["state"].get("selected").is_none());
        assert!(save["state"].get("expanded").is_none());
        // Элемент, не сказавший о себе ничего, приезжает вовсе без блока состояния.
        assert!(value["items"][1].get("state").is_none());
        assert!(
            value["state_semantics"]
                .as_str()
                .unwrap()
                .contains("does not mean false")
        );
    }

    #[test]
    fn a_truncated_label_is_marked_on_the_node_that_lost_it() {
        let mut walk = sample_walk();
        walk.nodes[3].text_truncated = true;
        walk.nodes[3].children_unread = Some("max_depth");
        let value = rendered(&test_plan(Shape::Flat, ""), &walk);
        assert_eq!(value["items"][3]["text_truncated"], true);
        assert_eq!(value["items"][3]["children_unread"], "max_depth");
        assert!(value["items"][0].get("text_truncated").is_none());
    }

    #[test]
    fn a_fallback_answer_is_an_answer_and_names_why_it_is_the_lesser_one() {
        let plan = test_plan(Shape::Tree, "");
        let walk = Walk {
            nodes: vec![node(None, 0, "window", "Быстрые настройки")],
            ..Walk::default()
        };
        let value = render(Rendered {
            plan: &plan,
            window: &WindowInfo {
                hwnd: 0x1234,
                title: "Быстрые настройки".into(),
                class: "ControlCenterWindow".into(),
                pid: 42,
                rect: None,
                foreground: true,
                resolved_from: "foreground",
            },
            walk: &walk,
            backend: "win32",
            backend_detail: WINDOW_READ_WIN32_DETAIL,
            fallback_reason: Some("UI Automation did not answer within timeout_ms=1800".into()),
            elapsed_ms: 2,
            extra_notes: vec!["Chromium, Electron, WPF and UWP surfaces look empty here".into()],
            workers_live: 1,
        });
        // Обвал UIA — не отказ: она получает то, что удалось прочитать, причину и
        // предупреждение, что тишина этого пути может быть неправдой про окно.
        assert_eq!(value["ok"], true);
        assert_eq!(value["backend"], "win32");
        assert_eq!(value["nodes_returned"], 1);
        assert_eq!(value["uia_workers_live"], 1);
        assert!(
            value["fallback_reason"]
                .as_str()
                .unwrap()
                .contains("did not answer"),
        );
        assert!(
            value["notes"]
                .as_array()
                .unwrap()
                .iter()
                .any(|note| note.as_str().unwrap().contains("look empty here")),
            "{:?}",
            value["notes"]
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn dispatch_refuses_to_block_a_current_thread_runtime() {
        let error = dispatch(CAPABILITY, json!({})).unwrap_err();
        assert!(
            error.to_string().contains("multi-thread Tokio runtime"),
            "{error}"
        );
    }

    #[cfg(not(windows))]
    #[test]
    fn without_windows_the_verb_says_why_instead_of_returning_nothing() {
        let error = run(CAPABILITY, json!({})).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("requires an interactive Windows session"),
            "{error}"
        );
    }

    /// Живой замер. Не входит в гейт: требует рабочего стола с окном на переднем плане.
    /// Гонять руками: `cargo test -p praxis-body uia::tests::live -- --ignored --nocapture`.
    #[cfg(windows)]
    #[ignore = "нужен живой рабочий стол с окном на переднем плане"]
    #[test]
    fn live_reading_of_the_foreground_window() {
        let mut slowest = std::time::Duration::ZERO;
        for (label, args) in [
            ("tree", json!({})),
            ("tree again (warm)", json!({})),
            ("flat + filter", json!({"shape": "flat", "text_contains": "close"})),
            ("shallow", json!({"max_depth": 2})),
            ("forced win32 fallback", json!({"backend": "win32"})),
        ] {
            let started = std::time::Instant::now();
            let value = run(CAPABILITY, args).unwrap();
            slowest = slowest.max(started.elapsed());
            println!(
                "{label}: backend={} nodes={}/{} depth={} ms={} walk_ms={} truncated={} title={}",
                value["backend"],
                value["nodes_returned"],
                value["nodes_read"],
                value["depth_reached"],
                value["elapsed_ms"],
                value["walk_ms"],
                value["truncated_by"],
                value["window"]["title"],
            );
            assert_eq!(value["ok"], true);
        }
        let value = run(CAPABILITY, json!({})).unwrap();
        println!("{}", serde_json::to_string_pretty(&value).unwrap());
        assert!(
            slowest < std::time::Duration::from_secs(3),
            "чтение окна заняло {slowest:?} — затея имеет смысл только быстрее снимка экрана"
        );
    }
}
