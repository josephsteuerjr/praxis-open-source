//! Составные глаголы: два шага в ОДИН поход по сети.
//!
//! Живой случай, ради которого написан модуль: любой глагол тела стоит примерно 0.37 с
//! дороги туда-обратно, и эта цена не зависит от того, что глагол делает. Поэтому
//! «нажать и увидеть, что получилось» стоило минимум двух походов плюс снимок экрана
//! (ещё 2.4 с), а «дождаться, пока откроется окно» — по походу на каждый опрос: десять
//! человеческих движений превращались в двадцать с лишним вызовов.
//!
//! Здесь нет ни одной новой возможности: `desktop.input.perform_and_read` — это ровно
//! `desktop.input.perform` плюс `desktop.window.read`, а `desktop.window.wait` — ровно
//! тот же опрос, только его крутит тело, а не она. Ни один составной глагол ничего не
//! проверяет ЗА неё и ничего не запрещает: порядок «сначала посмотри, потом нажми» —
//! её решение, а не встроенная политика.

use std::path::Path;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use praxis_body_protocol::{AdapterDescriptor, CapabilityDescriptor};
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::{desktop, uia};

/// «Сделай и посмотри».
pub const ACT_AND_READ: &str = "desktop.input.perform_and_read";
/// «Подожди, пока».
pub const WAIT: &str = "desktop.window.wait";
pub const VERSION: u32 = 1;

/// Диспетчер спрашивает это ПЕРЕД общей веткой `starts_with("desktop.")` — имена
/// начинаются с того же префикса, и общая ветка увела бы их в `desktop::dispatch`,
/// где их нет, а падение выглядело бы как «такого глагола не существует».
pub fn handles(capability: &str) -> bool {
    capability == ACT_AND_READ || capability == WAIT
}

pub fn descriptors() -> Vec<CapabilityDescriptor> {
    vec![
        CapabilityDescriptor {
            name: ACT_AND_READ.into(),
            version: VERSION,
            // Ввод меняет чужой рабочий стол и стоит в одном ряду с
            // `desktop.input.perform`: mutating и durable у него те же.
            mutating: true,
            durable: true,
        },
        CapabilityDescriptor {
            name: WAIT.into(),
            version: VERSION,
            mutating: false,
            durable: false,
        },
    ]
}

/// Отдельный адаптер, потому что это отдельный механизм: не Win32 и не COM, а склейка
/// двух чужих глаголов. Если склейка когда-нибудь отвалится, в манифесте это будет видно
/// не по молчанию, а по строке.
pub fn adapter_descriptor() -> AdapterDescriptor {
    AdapterDescriptor {
        name: "composed-desktop-steps".into(),
        version: "1".into(),
        capabilities: vec![ACT_AND_READ.to_string(), WAIT.to_string()],
        // Составные шаги живут ровно там, где живут их части.
        available: cfg!(windows),
    }
}

// ─── пределы ────────────────────────────────────────────────────────────────────────────
//
// Закон проекта: ни одного молчаливого предела. Всё, что ниже, приезжает в ответе в поле
// `limits`, а всякая подтяжка к границе — в `notes`.

/// Пауза между действием и наблюдением. 120 мс — примерно столько окну нужно, чтобы
/// перерисоваться после клика; меньше — и читалка застаёт прошлый кадр.
const SETTLE_DEFAULT_MS: u64 = 120;
const SETTLE_CEILING_MS: u64 = 5_000;

const WAIT_TIMEOUT_DEFAULT_MS: u64 = 5_000;
const WAIT_TIMEOUT_FLOOR_MS: u64 = 100;
/// Серверная сторона ждёт ответ 60 с (`body_client.call(timeout=60)`). Ответ, который не
/// успел вернуться, для неё неотличим от «тело умерло», поэтому потолок ожидания заведомо
/// меньше: 45 с плюс последняя проба всё ещё укладываются в её терпение.
const WAIT_TIMEOUT_CEILING_MS: u64 = 45_000;

const POLL_DEFAULT_MS: u64 = 250;
const POLL_FLOOR_MS: u64 = 50;
const POLL_CEILING_MS: u64 = 5_000;

/// Срок ОДНОЙ пробы чтения окна. Пробе не нужен полный обход: ей нужно узнать, появилось
/// ли совпадение, и на следующем круге она попробует снова.
const PROBE_TIMEOUT_DEFAULT_MS: u64 = 1_200;
const PROBE_TIMEOUT_FLOOR_MS: u64 = 100;
/// Тот же потолок, что у `desktop.window.read`: выше него читалка всё равно подтянет к себе.
const PROBE_TIMEOUT_CEILING_MS: u64 = 20_000;

const PROBE_MAX_NODES_DEFAULT: u64 = 400;

/// Сколько окон запрашиваем у `desktop.window.list` на каждой пробе. Если окон больше,
/// это видно в `last_probe.windows_total` против `windows_listed`, а не по молчанию.
const WINDOW_LIST_LIMIT: u64 = 2_000;

/// Поимённо показываем не больше пяти сорванных проб, но их полное число — без капа.
const MAX_PROBE_ERRORS_REPORTED: usize = 5;

/// Просьбу вне границ не отвергаем (это был бы забор) и не проглатываем молча (это была
/// бы ложь): подтягиваем к границе и говорим об этом вслух. Тот же приём, что в `uia.rs`.
fn clamp(
    name: &str,
    requested: Option<u64>,
    default: u64,
    floor: u64,
    ceiling: u64,
    notes: &mut Vec<String>,
) -> u64 {
    let Some(requested) = requested else {
        return default;
    };
    let effective = requested.clamp(floor, ceiling);
    if effective != requested {
        notes.push(format!(
            "{name}={requested} is outside [{floor}, {ceiling}]; this call used {effective}"
        ));
    }
    effective
}

// ─── «сделай и посмотри» ────────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
// Опечатка в имени аргумента иначе означала бы «я попросила, а оно не применилось» —
// молчаливое ограничение в чистом виде.
#[serde(deny_unknown_fields)]
struct ActArgs {
    /// Ровно те же аргументы, что берёт `desktop.input.perform`. Разбирает их он сам:
    /// склейка не имеет права понимать ввод иначе, чем понимает его исполнитель ввода.
    input: Value,
    #[serde(default)]
    settle_ms: Option<u64>,
    /// Ровно те же аргументы, что берёт `desktop.window.read`. Пусто — читаем окно
    /// переднего плана, каким оно стало ПОСЛЕ действия.
    #[serde(default)]
    read: Option<Value>,
}

async fn act_and_read(args: Value, state_dir: &Path) -> Result<Value> {
    let args: ActArgs =
        serde_json::from_value(args).context("desktop.input.perform_and_read arguments")?;
    let mut notes: Vec<String> = Vec::new();
    let settle_ms = clamp(
        "settle_ms",
        args.settle_ms,
        SETTLE_DEFAULT_MS,
        0,
        SETTLE_CEILING_MS,
        &mut notes,
    );

    let started = Instant::now();
    // Ввод идёт первым и без всяких предварительных проверок: «сначала прочитай, потом
    // нажимай» — это политика, а глагол здесь только ради экономии дороги.
    let input = desktop::dispatch("desktop.input.perform", args.input, state_dir)?;
    let input_ms = started.elapsed().as_millis() as u64;

    if settle_ms > 0 {
        tokio::time::sleep(Duration::from_millis(settle_ms)).await;
    }

    let read_started = Instant::now();
    let read = uia::dispatch(uia::CAPABILITY, args.read.unwrap_or_else(|| json!({})));
    let read_ms = read_started.elapsed().as_millis() as u64;

    let (observed, observation) = match read {
        Ok(value) => (true, json!({"kind": "window_read", "read": value})),
        Err(error) => {
            // Действие УЖЕ произошло. Сказать «окно пустое» здесь было бы враньём:
            // правда в том, что наблюдения нет, а рабочий стол уже изменён.
            notes.push(
                "the input was performed and the reading was not: this answer describes an act without an observation"
                    .to_string(),
            );
            (
                false,
                json!({
                    "kind": "unavailable",
                    "capability": uia::CAPABILITY,
                    "error": format!("{error:#}"),
                    "meaning": "the input already happened; the window could not be read afterwards. This is not an empty window and not a window without matches.",
                }),
            )
        }
    };

    Ok(json!({
        "ok": true,
        "capability": ACT_AND_READ,
        "composed_of": ["desktop.input.perform", uia::CAPABILITY],
        "input": input,
        "settled_ms": settle_ms,
        "observed": observed,
        "observation": observation,
        "elapsed_ms": started.elapsed().as_millis() as u64,
        "input_ms": input_ms,
        "read_ms": read_ms,
        "round_trips_saved": 1,
        "limits": {
            "settle_ms": settle_ms,
            "settle_ms_floor": 0,
            "settle_ms_ceiling": SETTLE_CEILING_MS,
            "input_limits_in": "input.limits",
            "read_limits_in": "observation.read.limits",
        },
        "semantics": "one round trip, not one atomic act: the desktop is free to change between the input and the reading, and this verb never takes a screenshot — if the reading fails it says so and the screenshot stays your call",
        "notes": notes,
    }))
}

// ─── «подожди, пока» ────────────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WaitArgs {
    #[serde(rename = "for")]
    condition: String,
    #[serde(default)]
    title_contains: Option<String>,
    #[serde(default)]
    class_contains: Option<String>,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    foreground: Option<bool>,
    #[serde(default)]
    hwnd: Option<Value>,
    #[serde(default)]
    text_contains: Option<String>,
    #[serde(default)]
    visible_only: Option<bool>,
    #[serde(default)]
    timeout_ms: Option<u64>,
    #[serde(default)]
    poll_interval_ms: Option<u64>,
    #[serde(default)]
    probe_timeout_ms: Option<u64>,
    #[serde(default)]
    probe_max_nodes: Option<u64>,
    #[serde(default)]
    probe_max_depth: Option<u64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Condition {
    Window,
    WindowClosed,
    Text,
    ElementEnabled,
}

impl Condition {
    fn parse(raw: &str) -> Result<Self> {
        Ok(match raw.trim() {
            "window" => Self::Window,
            "window_closed" => Self::WindowClosed,
            "text" => Self::Text,
            "element_enabled" => Self::ElementEnabled,
            other => {
                bail!("for must be window, window_closed, text, or element_enabled, not {other:?}")
            }
        })
    }

    fn name(self) -> &'static str {
        match self {
            Self::Window => "window",
            Self::WindowClosed => "window_closed",
            Self::Text => "text",
            Self::ElementEnabled => "element_enabled",
        }
    }

    /// Условия про содержимое окна читают окно; условия про само окно обходятся списком.
    fn reads_window(self) -> bool {
        matches!(self, Self::Text | Self::ElementEnabled)
    }
}

#[derive(Debug, Clone, Default)]
struct WindowMatcher {
    title: String,
    class: String,
    pid: Option<u32>,
    foreground: bool,
}

impl WindowMatcher {
    fn is_empty(&self) -> bool {
        self.title.is_empty() && self.class.is_empty() && self.pid.is_none()
    }

    fn matches(&self, row: &Value) -> bool {
        if !self.title.is_empty() {
            let title = row
                .get("title")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_lowercase();
            if !title.contains(&self.title) {
                return false;
            }
        }
        if !self.class.is_empty() {
            let class = row
                .get("class")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_lowercase();
            if !class.contains(&self.class) {
                return false;
            }
        }
        if let Some(pid) = self.pid
            && row.get("pid").and_then(Value::as_u64) != Some(u64::from(pid))
        {
            return false;
        }
        if self.foreground && row.get("foreground") != Some(&Value::Bool(true)) {
            return false;
        }
        true
    }
}

#[derive(Debug, Clone)]
struct WaitPlan {
    condition: Condition,
    matcher: WindowMatcher,
    hwnd: Option<Value>,
    needle: String,
    visible_only: bool,
    visible_only_explicit: bool,
    timeout_ms: u64,
    poll_interval_ms: u64,
    probe_timeout_ms: u64,
    probe_max_nodes: u64,
    probe_max_depth: Option<u64>,
    notes: Vec<String>,
    ignored: Vec<&'static str>,
}

fn wait_plan(args: Value) -> Result<WaitPlan> {
    let args: WaitArgs = serde_json::from_value(args).context("desktop.window.wait arguments")?;
    let condition = Condition::parse(&args.condition)?;
    let mut notes = Vec::new();

    let matcher = WindowMatcher {
        title: args
            .title_contains
            .clone()
            .unwrap_or_default()
            .trim()
            .to_lowercase(),
        class: args
            .class_contains
            .clone()
            .unwrap_or_default()
            .trim()
            .to_lowercase(),
        pid: args.pid,
        foreground: args.foreground.unwrap_or(false),
    };
    let needle = args
        .text_contains
        .clone()
        .unwrap_or_default()
        .trim()
        .to_lowercase();

    if condition.reads_window() {
        if needle.is_empty() {
            bail!(
                "for={} needs text_contains: without it there is nothing to wait for",
                condition.name()
            )
        }
    } else if matcher.is_empty() {
        bail!(
            "for={} needs at least one of title_contains, class_contains, pid: without them every window matches and the answer would say nothing",
            condition.name()
        )
    }

    // Аргумент, который не участвует в выбранном условии, называется вслух. Иначе
    // «я же передала pid» превращается в молчаливое «а он не смотрелся».
    let mut ignored: Vec<&'static str> = Vec::new();
    if condition.reads_window() {
        if args.title_contains.is_some() {
            ignored.push("title_contains");
        }
        if args.class_contains.is_some() {
            ignored.push("class_contains");
        }
        if args.pid.is_some() {
            ignored.push("pid");
        }
        if args.foreground.is_some() {
            ignored.push("foreground");
        }
    } else {
        if args.text_contains.is_some() {
            ignored.push("text_contains");
        }
        if args.hwnd.is_some() {
            ignored.push("hwnd");
        }
        if args.probe_max_nodes.is_some() {
            ignored.push("probe_max_nodes");
        }
        if args.probe_max_depth.is_some() {
            ignored.push("probe_max_depth");
        }
        if args.probe_timeout_ms.is_some() {
            ignored.push("probe_timeout_ms");
        }
        if condition == Condition::WindowClosed && args.foreground.is_some() {
            ignored.push("foreground");
        }
    }

    let timeout_ms = clamp(
        "timeout_ms",
        args.timeout_ms,
        WAIT_TIMEOUT_DEFAULT_MS,
        WAIT_TIMEOUT_FLOOR_MS,
        WAIT_TIMEOUT_CEILING_MS,
        &mut notes,
    );
    let poll_interval_ms = clamp(
        "poll_interval_ms",
        args.poll_interval_ms,
        POLL_DEFAULT_MS,
        POLL_FLOOR_MS,
        POLL_CEILING_MS,
        &mut notes,
    );
    let probe_timeout_ms = clamp(
        "probe_timeout_ms",
        args.probe_timeout_ms,
        PROBE_TIMEOUT_DEFAULT_MS,
        PROBE_TIMEOUT_FLOOR_MS,
        PROBE_TIMEOUT_CEILING_MS,
        &mut notes,
    );

    Ok(WaitPlan {
        condition,
        matcher,
        hwnd: args.hwnd,
        needle,
        visible_only: args.visible_only.unwrap_or(true),
        visible_only_explicit: args.visible_only.is_some(),
        timeout_ms,
        poll_interval_ms,
        probe_timeout_ms,
        probe_max_nodes: args.probe_max_nodes.unwrap_or(PROBE_MAX_NODES_DEFAULT),
        probe_max_depth: args.probe_max_depth,
        notes,
        ignored,
    })
}

/// Итог ОДНОЙ пробы. `met` — условие увидено; `summary` — правда о том, что вообще
/// удалось разглядеть, включая частичность чтения.
#[derive(Debug, Clone)]
struct Probe {
    met: bool,
    matched: Option<Value>,
    summary: Value,
    truncated_by: Vec<String>,
}

fn evaluate_windows(plan: &WaitPlan, listing: &Value) -> Probe {
    let empty = Vec::new();
    let rows = listing
        .get("items")
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    let matched: Vec<&Value> = rows
        .iter()
        .filter(|row| plan.matcher.matches(row))
        .collect();
    let total = listing.get("total").and_then(Value::as_u64).unwrap_or(0);
    let returned = listing.get("returned").and_then(Value::as_u64).unwrap_or(0);
    let summary = json!({
        "kind": "window_list",
        "windows_matched": matched.len(),
        "windows_listed": returned,
        "windows_total": total,
        // Список окон тоже умеет быть частичным, и это обязано быть видно.
        "listing_truncated": total > returned,
        "visible_only": plan.visible_only,
    });
    let met = match plan.condition {
        Condition::WindowClosed => matched.is_empty(),
        _ => !matched.is_empty(),
    };
    // Для «окно закрылось» показывать нечего — совпадение и есть отсутствие совпадения.
    let matched_value = match plan.condition {
        Condition::WindowClosed => None,
        _ => matched.first().map(|row| (*row).clone()),
    };
    Probe {
        met,
        matched: matched_value,
        summary,
        truncated_by: if total > returned {
            vec!["window_list_limit".to_string()]
        } else {
            Vec::new()
        },
    }
}

fn evaluate_read(plan: &WaitPlan, read: &Value) -> Probe {
    let empty = Vec::new();
    let items = read
        .get("items")
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    // «enabled» отсутствует, когда элемент этого о себе не говорит. Считать молчание
    // за «выключено» — врать; считать за «включено» — врать опаснее. Поэтому такие
    // элементы идут отдельным счётчиком и условие на них НЕ срабатывает.
    let mut enabled: Option<&Value> = None;
    let mut without_state = 0u64;
    for item in items {
        match item.get("state").and_then(|state| state.get("enabled")) {
            Some(Value::Bool(true)) => {
                if enabled.is_none() {
                    enabled = Some(item);
                }
            }
            Some(Value::Bool(false)) => {}
            _ => without_state += 1,
        }
    }
    let truncated_by: Vec<String> = read
        .get("truncated_by")
        .and_then(Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(|value| value.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    let summary = json!({
        "kind": "window_read",
        "backend": read.get("backend").cloned().unwrap_or(Value::Null),
        "window": read.get("window").cloned().unwrap_or(Value::Null),
        "nodes_read": read.get("nodes_read").cloned().unwrap_or(Value::Null),
        "matched": items.len(),
        "matched_without_enabled_state": without_state,
        "read_truncated": !truncated_by.is_empty(),
        "truncated_by": truncated_by.clone(),
        "total_known": read.get("total_known").cloned().unwrap_or(Value::Null),
        // Пределы самой пробы приезжают целиком: у пробы есть свои капы (глубина, длина
        // надписей, срок), и молчать о них здесь значило бы спрятать их за словом «проба».
        "read_limits": read.get("limits").cloned().unwrap_or(Value::Null),
        "read_notes": read.get("notes").cloned().unwrap_or(Value::Null),
    });
    let (met, matched) = match plan.condition {
        Condition::ElementEnabled => (enabled.is_some(), enabled.cloned()),
        _ => (!items.is_empty(), items.first().cloned()),
    };
    let matched = matched.map(|element| {
        json!({
            "element": element,
            "window": read.get("window").cloned().unwrap_or(Value::Null),
            "backend": read.get("backend").cloned().unwrap_or(Value::Null),
        })
    });
    Probe {
        met,
        matched,
        summary,
        truncated_by,
    }
}

#[derive(Debug, Clone, Default)]
struct WaitOutcome {
    met: bool,
    waited_ms: u64,
    polls: u64,
    evaluated: u64,
    matched: Option<Value>,
    last_probe: Option<Value>,
    last_truncated_by: Vec<String>,
    errors: Vec<Value>,
    error_count: u64,
}

fn render_wait(plan: &WaitPlan, outcome: &WaitOutcome) -> Value {
    let mut notes = plan.notes.clone();
    let reason = if outcome.met {
        format!(
            "condition {} was observed on poll {} after {}ms",
            plan.condition.name(),
            outcome.polls,
            outcome.waited_ms
        )
    } else {
        // Истечение срока — это «не дождалась», а не «условия нет». Разница между этими
        // двумя предложениями и есть всё, ради чего написан глагол.
        let mut reason = format!(
            "did not observe {} within timeout_ms={} (waited {}ms over {} polls, {} of which actually evaluated the condition); this is a timeout, not evidence that the condition is false",
            plan.condition.name(),
            plan.timeout_ms,
            outcome.waited_ms,
            outcome.polls,
            outcome.evaluated
        );
        if outcome.evaluated == 0 {
            reason.push_str(
                "; every probe failed, so the condition was never checked even once — see probe_errors",
            );
            notes.push(
                "not one probe succeeded: this answer says nothing about the condition itself"
                    .to_string(),
            );
        } else if !outcome.last_truncated_by.is_empty() {
            reason.push_str(&format!(
                "; the last probe was partial (truncated_by={:?}), so \"not found\" means \"not found in the part that was read\"",
                outcome.last_truncated_by
            ));
            notes.push(
                "raise probe_max_nodes / probe_timeout_ms if the element could be deeper than the part that was read"
                    .to_string(),
            );
        }
        reason
    };
    if outcome.error_count > 0 {
        notes.push(format!(
            "{} of {} probes failed; {} of them are named in probe_errors",
            outcome.error_count,
            outcome.polls,
            outcome.errors.len()
        ));
    }
    if !plan.ignored.is_empty() {
        notes.push(format!(
            "for={} does not use these arguments, they were ignored: {}",
            plan.condition.name(),
            plan.ignored.join(", ")
        ));
    }
    if !plan.condition.reads_window() && !plan.visible_only_explicit {
        notes.push(
            "visible_only defaults to true: a hidden window counts as absent, and for=window_closed a window that only became invisible counts as closed"
                .to_string(),
        );
    }

    json!({
        "ok": true,
        "capability": WAIT,
        "for": plan.condition.name(),
        "met": outcome.met,
        "timed_out": !outcome.met,
        "waited_ms": outcome.waited_ms,
        "polls": outcome.polls,
        "condition_evaluated": outcome.evaluated,
        "match": outcome.matched.clone().unwrap_or(Value::Null),
        "last_probe": outcome.last_probe.clone().unwrap_or(Value::Null),
        "probe_errors": outcome.errors.clone(),
        "probe_error_count": outcome.error_count,
        "probe_errors_truncated": (outcome.errors.len() as u64) < outcome.error_count,
        "reason": reason,
        "limits": {
            "timeout_ms": plan.timeout_ms,
            "timeout_ms_floor": WAIT_TIMEOUT_FLOOR_MS,
            "timeout_ms_ceiling": WAIT_TIMEOUT_CEILING_MS,
            "poll_interval_ms": plan.poll_interval_ms,
            "poll_interval_ms_floor": POLL_FLOOR_MS,
            "poll_interval_ms_ceiling": POLL_CEILING_MS,
            "probe_timeout_ms": plan.probe_timeout_ms,
            "probe_timeout_ms_floor": PROBE_TIMEOUT_FLOOR_MS,
            "probe_timeout_ms_ceiling": PROBE_TIMEOUT_CEILING_MS,
            "probe_max_nodes": plan.probe_max_nodes,
            // Не задана — значит действует умолчание самой читалки окна, и оно приезжает
            // целиком в `last_probe.read_limits`, а не остаётся неназванным.
            "probe_max_depth": plan.probe_max_depth,
            "probe_limits_in": "last_probe.read_limits",
            "window_list_limit": WINDOW_LIST_LIMIT,
            "max_probe_errors_reported": MAX_PROBE_ERRORS_REPORTED,
            "first_probe": "immediate: the first probe runs before the first sleep, so an already-true condition costs one poll",
            "budget": "timeout_ms covers the whole wait; the last probe may overrun it by at most probe_timeout_ms, and waited_ms says what actually elapsed",
            // Найдено живой пробой: `for=text` с timeout_ms=1500 отдал частичное чтение,
            // и по одному лишь `limits.probe_timeout_ms` (1200) причину было не собрать —
            // фактический срок последней пробы был 100 мс, потому что столько осталось от
            // общего бюджета. Число видно в `last_probe.read_limits.timeout_ms`, но пока
            // не сказано, ОТКУДА оно, «частичная проба» выглядит необъяснимой.
            "probe_budget_squeeze": "each probe gets min(probe_timeout_ms, remaining wait) but never less than probe_timeout_ms_floor, so the last probe of a short wait can be much shorter than probe_timeout_ms; the value it actually got is last_probe.read_limits.timeout_ms",
        },
        "semantics": "met=false together with timed_out=true means the condition was not observed inside the named budget; it is not a claim that the condition is false. The truth of this answer is met, not ok.",
        "notes": notes,
    })
}

async fn wait_for(args: Value, state_dir: &Path) -> Result<Value> {
    let plan = wait_plan(args)?;
    let started = Instant::now();
    let deadline = started + Duration::from_millis(plan.timeout_ms);
    let mut outcome = WaitOutcome::default();

    loop {
        outcome.polls += 1;
        let remaining = deadline
            .saturating_duration_since(Instant::now())
            .as_millis() as u64;
        let probe = probe_once(&plan, state_dir, remaining);
        match probe {
            Ok(probe) => {
                outcome.evaluated += 1;
                outcome.last_truncated_by = probe.truncated_by.clone();
                outcome.last_probe = Some(probe.summary);
                if probe.met {
                    outcome.met = true;
                    outcome.matched = probe.matched;
                    break;
                }
            }
            Err(error) => {
                outcome.error_count += 1;
                if outcome.errors.len() < MAX_PROBE_ERRORS_REPORTED {
                    outcome.errors.push(json!({
                        "at_ms": started.elapsed().as_millis() as u64,
                        "poll": outcome.polls,
                        "error": format!("{error:#}"),
                    }));
                }
            }
        }
        let now = Instant::now();
        if now >= deadline {
            break;
        }
        let sleep_ms = plan
            .poll_interval_ms
            .min(deadline.saturating_duration_since(now).as_millis() as u64);
        if sleep_ms > 0 {
            tokio::time::sleep(Duration::from_millis(sleep_ms)).await;
        }
    }

    outcome.waited_ms = started.elapsed().as_millis() as u64;
    Ok(render_wait(&plan, &outcome))
}

fn probe_once(plan: &WaitPlan, state_dir: &Path, remaining_ms: u64) -> Result<Probe> {
    if plan.condition.reads_window() {
        // Проба не обязана успеть прочитать всё окно: ей нужно узнать, появилось ли
        // совпадение. Срок пробы никогда не длиннее остатка общего срока — иначе
        // «подожди 1 секунду» уходило бы в перебор на время одного чтения.
        let budget = plan
            .probe_timeout_ms
            .min(remaining_ms.max(PROBE_TIMEOUT_FLOOR_MS));
        let mut read_args = Map::new();
        if let Some(hwnd) = plan.hwnd.clone() {
            read_args.insert("hwnd".into(), hwnd);
        }
        read_args.insert("shape".into(), json!("flat"));
        read_args.insert("text_contains".into(), json!(plan.needle));
        read_args.insert("visible_only".into(), json!(plan.visible_only));
        read_args.insert("max_nodes".into(), json!(plan.probe_max_nodes));
        if let Some(depth) = plan.probe_max_depth {
            read_args.insert("max_depth".into(), json!(depth));
        }
        read_args.insert("timeout_ms".into(), json!(budget));
        let read = uia::dispatch(uia::CAPABILITY, Value::Object(read_args))?;
        return Ok(evaluate_read(plan, &read));
    }

    let mut list_args = Map::new();
    list_args.insert("visible_only".into(), json!(plan.visible_only));
    list_args.insert("limit".into(), json!(WINDOW_LIST_LIMIT));
    if !plan.matcher.title.is_empty() {
        // У `desktop.window.list` тот же регистронезависимый «содержит», что и здесь,
        // поэтому фильтр отдаём ему: так по проводу едет меньше строк.
        list_args.insert("title_contains".into(), json!(plan.matcher.title));
    }
    if let Some(pid) = plan.matcher.pid {
        list_args.insert("pid".into(), json!(pid));
    }
    let listing = desktop::dispatch("desktop.window.list", Value::Object(list_args), state_dir)?;
    Ok(evaluate_windows(plan, &listing))
}

// ─── вход ───────────────────────────────────────────────────────────────────────────────

pub async fn dispatch(capability: &str, args: Value, state_dir: &Path) -> Result<Value> {
    match capability {
        ACT_AND_READ => act_and_read(args, state_dir).await,
        WAIT => wait_for(args, state_dir).await,
        other => bail!("unknown composed capability {other}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan_of(args: Value) -> WaitPlan {
        wait_plan(args).expect("plan")
    }

    #[test]
    fn the_module_declares_exactly_what_it_handles() {
        let declared: Vec<String> = descriptors()
            .into_iter()
            .map(|descriptor| descriptor.name)
            .collect();
        assert_eq!(declared.len(), 2);
        for name in &declared {
            assert!(handles(name), "{name} is declared but not handled");
        }
        assert!(!handles("desktop.window.read"));
        assert!(!handles("desktop.input.perform"));
        let adapter = adapter_descriptor();
        assert_eq!(adapter.capabilities, declared);
    }

    #[test]
    fn an_unknown_argument_is_named_instead_of_silently_dropped() {
        let error = wait_plan(json!({"for": "window", "titel_contains": "Блокнот"}))
            .expect_err("typo must not pass");
        // Опечатка, съеденная молча, читается как «я попросила, а тело не послушалось».
        // Имя поля живёт в ПРИЧИНЕ ошибки, поэтому и сверяем всю цепочку `{:#}` — ровно
        // ту, что теперь уезжает в кадр ошибки из `runtime::handle`.
        let chain = format!("{error:#}");
        assert!(chain.contains("titel_contains"), "{chain}");

        let error = serde_json::from_value::<ActArgs>(json!({"input": {}, "setle_ms": 10}))
            .expect_err("typo must not pass")
            .to_string();
        assert!(error.contains("setle_ms"), "{error}");
    }

    #[test]
    fn a_condition_without_its_subject_is_refused_by_name() {
        let error = wait_plan(json!({"for": "text"}))
            .expect_err("no needle")
            .to_string();
        assert!(error.contains("text_contains"), "{error}");
        let error = wait_plan(json!({"for": "window"}))
            .expect_err("no matcher")
            .to_string();
        assert!(error.contains("title_contains"), "{error}");
        let error = wait_plan(json!({"for": "wibble"}))
            .expect_err("bad condition")
            .to_string();
        assert!(error.contains("window_closed"), "{error}");
    }

    #[test]
    fn limits_are_pulled_to_the_boundary_and_the_pull_is_declared() {
        let plan = plan_of(json!({
            "for": "window",
            "title_contains": "x",
            "timeout_ms": 600_000,
            "poll_interval_ms": 1,
        }));
        assert_eq!(plan.timeout_ms, WAIT_TIMEOUT_CEILING_MS);
        assert_eq!(plan.poll_interval_ms, POLL_FLOOR_MS);
        let notes = plan.notes.join("\n");
        assert!(notes.contains("timeout_ms=600000"), "{notes}");
        assert!(notes.contains("poll_interval_ms=1"), "{notes}");

        // Граница ровно по границе не подтягивается и молчит.
        let exact = plan_of(json!({
            "for": "window",
            "title_contains": "x",
            "timeout_ms": WAIT_TIMEOUT_CEILING_MS,
            "poll_interval_ms": POLL_FLOOR_MS,
        }));
        assert!(exact.notes.is_empty(), "{:?}", exact.notes);

        let rendered = render_wait(&plan, &WaitOutcome::default());
        assert_eq!(
            rendered["limits"]["timeout_ms"],
            json!(WAIT_TIMEOUT_CEILING_MS)
        );
        assert_eq!(
            rendered["limits"]["timeout_ms_ceiling"],
            json!(WAIT_TIMEOUT_CEILING_MS)
        );
    }

    #[test]
    fn a_window_matcher_needs_every_named_part_to_agree() {
        let plan = plan_of(json!({
            "for": "window",
            "title_contains": "Блокнот",
            "class_contains": "notepad",
            "foreground": true,
        }));
        let listing = json!({
            "ok": true, "total": 3, "returned": 3,
            "items": [
                {"title": "Блокнот", "class": "Notepad", "pid": 10, "foreground": false},
                {"title": "чужое", "class": "Notepad", "pid": 11, "foreground": true},
                {"title": "БЛОКНОТ — файл", "class": "NotepadMainClass", "pid": 12, "foreground": true},
            ],
        });
        let probe = evaluate_windows(&plan, &listing);
        assert!(probe.met);
        assert_eq!(probe.matched.as_ref().unwrap()["pid"], json!(12));
        assert_eq!(probe.summary["windows_matched"], json!(1));
    }

    #[test]
    fn a_truncated_window_list_is_called_truncated() {
        let plan = plan_of(json!({"for": "window", "title_contains": "нет такого"}));
        let listing = json!({"ok": true, "total": 4000, "returned": 2000, "items": []});
        let probe = evaluate_windows(&plan, &listing);
        assert!(!probe.met);
        assert_eq!(probe.summary["listing_truncated"], json!(true));
        assert_eq!(probe.truncated_by, vec!["window_list_limit".to_string()]);
    }

    #[test]
    fn window_closed_is_met_by_the_absence_of_a_match() {
        let plan = plan_of(json!({"for": "window_closed", "title_contains": "Сохранить"}));
        let present = json!({"ok": true, "total": 1, "returned": 1,
            "items": [{"title": "Сохранить как", "class": "#32770", "pid": 4}]});
        assert!(!evaluate_windows(&plan, &present).met);
        let gone = json!({"ok": true, "total": 0, "returned": 0, "items": []});
        let probe = evaluate_windows(&plan, &gone);
        assert!(probe.met);
        assert!(probe.matched.is_none());
    }

    #[test]
    fn a_silent_element_is_not_reported_as_available() {
        let plan = plan_of(json!({"for": "element_enabled", "text_contains": "сохранить"}));
        // У первого элемента ключа `enabled` НЕТ — это «не сказано», а не «выключено»
        // и уж точно не «доступно». Условие на нём срабатывать не должно.
        let read = json!({
            "backend": "uia",
            "window": {"hwnd": "0x1"},
            "nodes_read": 12,
            "truncated_by": [],
            "total_known": true,
            "items": [
                {"id": 3, "role": "button", "name": "Сохранить", "state": {"focused": false}},
                {"id": 4, "role": "menu_item", "name": "Сохранить как", "state": {"enabled": false}},
            ],
        });
        let probe = evaluate_read(&plan, &read);
        assert!(!probe.met);
        assert_eq!(probe.summary["matched"], json!(2));
        assert_eq!(probe.summary["matched_without_enabled_state"], json!(1));

        let mut ready = read.clone();
        ready["items"][0]["state"]["enabled"] = json!(true);
        let probe = evaluate_read(&plan, &ready);
        assert!(probe.met);
        assert_eq!(probe.matched.as_ref().unwrap()["element"]["id"], json!(3));
    }

    #[test]
    fn plain_text_waiting_does_not_care_about_the_enabled_state() {
        let plan = plan_of(json!({"for": "text", "text_contains": "готово"}));
        let read =
            json!({"items": [{"id": 1, "role": "text", "name": "Готово"}], "truncated_by": []});
        assert!(evaluate_read(&plan, &read).met);
        let empty = json!({"items": [], "truncated_by": []});
        assert!(!evaluate_read(&plan, &empty).met);
    }

    #[test]
    fn a_timeout_says_it_did_not_wait_long_enough_and_never_says_the_condition_is_false() {
        let plan = plan_of(json!({"for": "text", "text_contains": "готово", "timeout_ms": 1500}));
        let outcome = WaitOutcome {
            met: false,
            waited_ms: 1512,
            polls: 6,
            evaluated: 6,
            ..Default::default()
        };
        let rendered = render_wait(&plan, &outcome);
        let reason = rendered["reason"].as_str().unwrap();
        assert!(reason.contains("timeout_ms=1500"), "{reason}");
        assert!(reason.contains("1512ms"), "{reason}");
        assert!(
            reason.contains("not evidence that the condition is false"),
            "{reason}"
        );
        assert_eq!(rendered["met"], json!(false));
        assert_eq!(rendered["timed_out"], json!(true));
        assert_eq!(rendered["condition_evaluated"], json!(6));
    }

    #[test]
    fn a_partial_last_reading_makes_not_found_say_where_it_did_not_look() {
        let plan = plan_of(json!({"for": "text", "text_contains": "готово"}));
        let outcome = WaitOutcome {
            met: false,
            waited_ms: 5001,
            polls: 12,
            evaluated: 12,
            last_truncated_by: vec!["max_nodes".into(), "timeout".into()],
            ..Default::default()
        };
        let reason = render_wait(&plan, &outcome)["reason"]
            .as_str()
            .unwrap()
            .to_string();
        assert!(reason.contains("max_nodes"), "{reason}");
        assert!(
            reason.contains("not found in the part that was read"),
            "{reason}"
        );
    }

    #[test]
    fn probes_that_all_failed_are_not_reported_as_an_absent_condition() {
        let plan = plan_of(json!({"for": "window", "title_contains": "Блокнот"}));
        let outcome = WaitOutcome {
            met: false,
            waited_ms: 5000,
            polls: 3,
            evaluated: 0,
            errors: vec![json!({"poll": 1, "error": "no foreground window"})],
            error_count: 3,
            ..Default::default()
        };
        let rendered = render_wait(&plan, &outcome);
        let reason = rendered["reason"].as_str().unwrap();
        assert!(reason.contains("never checked even once"), "{reason}");
        assert_eq!(rendered["probe_error_count"], json!(3));
        assert_eq!(rendered["probe_errors_truncated"], json!(true));
        let notes = rendered["notes"].to_string();
        assert!(
            notes.contains("says nothing about the condition itself"),
            "{notes}"
        );
    }

    #[test]
    fn arguments_the_condition_does_not_use_are_named_out_loud() {
        let plan = plan_of(json!({
            "for": "text",
            "text_contains": "готово",
            "pid": 42,
            "title_contains": "Блокнот",
        }));
        assert!(plan.ignored.contains(&"pid"));
        assert!(plan.ignored.contains(&"title_contains"));
        let notes = render_wait(&plan, &WaitOutcome::default())["notes"].to_string();
        assert!(notes.contains("pid"), "{notes}");
        assert!(notes.contains("ignored"), "{notes}");
    }

    #[test]
    fn the_act_verb_settle_pause_is_clamped_and_declared() {
        let mut notes = Vec::new();
        assert_eq!(
            clamp(
                "settle_ms",
                Some(9_000),
                SETTLE_DEFAULT_MS,
                0,
                SETTLE_CEILING_MS,
                &mut notes
            ),
            SETTLE_CEILING_MS
        );
        assert!(notes.join("\n").contains("settle_ms=9000"));
        let mut quiet = Vec::new();
        assert_eq!(
            clamp(
                "settle_ms",
                None,
                SETTLE_DEFAULT_MS,
                0,
                SETTLE_CEILING_MS,
                &mut quiet
            ),
            SETTLE_DEFAULT_MS
        );
        assert!(quiet.is_empty());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_failed_reading_after_a_successful_act_is_not_an_empty_window() {
        // Вызов без `events` падает у самого `desktop.input.perform`, поэтому проверяем
        // вторую половину закона отдельно: ответ, в котором действие состоялось, а
        // наблюдения нет, обязан говорить об этом прямо. Здесь ввод заведомо не пройдёт
        // (пустая пачка), и весь глагол честно возвращает ошибку, а не выдуманный успех.
        let error = dispatch(
            ACT_AND_READ,
            json!({"input": {"events": []}}),
            Path::new("."),
        )
        .await
        .expect_err("empty input must not look like a performed act");
        let text = format!("{error:#}");
        assert!(!text.contains("\"observed\": true"), "{text}");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn an_unknown_composed_name_is_refused_by_name() {
        let error = dispatch("desktop.window.dance", json!({}), Path::new("."))
            .await
            .expect_err("unknown");
        assert!(error.to_string().contains("desktop.window.dance"));
    }

    /// Живая проба: сдвиг мыши на ноль пикселей (заведомо безобидный ввод) плюс чтение
    /// окна переднего плана — ровно то, ради чего глагол написан. Держим её выключенной
    /// по умолчанию: на машине без рабочего стола переднего плана просто нет.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "нужен живой рабочий стол: шлёт настоящий ввод и читает окно переднего плана"]
    async fn live_one_call_both_acts_and_observes() {
        let answer = dispatch(
            ACT_AND_READ,
            json!({
                "input": {"events": [{"type": "mouse", "x": 0, "y": 0, "relative": true}]},
                "read": {"shape": "flat", "max_nodes": 40},
            }),
            Path::new("."),
        )
        .await
        .expect("act and read");
        assert_eq!(answer["input"]["ok"], json!(true));
        assert_eq!(answer["observed"], json!(true));
        assert_eq!(answer["observation"]["kind"], json!("window_read"));
        assert!(
            answer["observation"]["read"]["window"]["hwnd"].is_string(),
            "{answer}"
        );
        assert_eq!(answer["limits"]["settle_ms"], json!(SETTLE_DEFAULT_MS));
    }

    /// Живая проба второй половины: условие, которое УЖЕ выполнено, обязано стоить одну
    /// пробу и вернуться сразу. Заголовок берём у первого же настоящего окна — так тест
    /// не зависит от того, что именно открыто на машине.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "нужен живой рабочий стол хотя бы с одним видимым окном"]
    async fn live_a_condition_that_already_holds_costs_one_poll() {
        let listing =
            desktop::dispatch("desktop.window.list", json!({"limit": 50}), Path::new("."))
                .expect("window list");
        let title = listing["items"]
            .as_array()
            .and_then(|items| {
                items
                    .iter()
                    .filter_map(|row| row["title"].as_str())
                    .find(|title| title.len() > 4)
            })
            .expect("at least one titled window")
            .to_string();
        let answer = dispatch(
            WAIT,
            json!({"for": "window", "title_contains": title, "timeout_ms": 2000}),
            Path::new("."),
        )
        .await
        .expect("wait");
        assert_eq!(answer["met"], json!(true), "{answer}");
        assert_eq!(answer["polls"], json!(1), "{answer}");
        assert!(answer["match"]["hwnd"].is_string(), "{answer}");
    }

    /// Живая проба текстового ожидания: важно не «нашлось», а что проба ДОШЛА до чтения
    /// окна и условие было проверено по-настоящему — иначе честный таймаут был бы
    /// неотличим от сорванных проб.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "нужен живой рабочий стол с окном на переднем плане"]
    async fn live_text_waiting_really_reads_the_window_before_giving_up() {
        let answer = dispatch(
            WAIT,
            json!({
                "for": "text",
                "text_contains": "такой-надписи-в-окне-нет-27072026",
                "timeout_ms": 700,
                "poll_interval_ms": 150,
                "probe_max_nodes": 60,
            }),
            Path::new("."),
        )
        .await
        .expect("timeout is an answer");
        assert_eq!(answer["met"], json!(false), "{answer}");
        assert!(
            answer["condition_evaluated"].as_u64().unwrap() >= 1,
            "probes never reached the window reader: {answer}"
        );
        assert_eq!(answer["probe_error_count"], json!(0), "{answer}");
        assert_eq!(
            answer["last_probe"]["kind"],
            json!("window_read"),
            "{answer}"
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[cfg_attr(
        not(windows),
        ignore = "ожидание опирается на desktop.window.list, которого нет вне Windows"
    )]
    async fn waiting_for_a_window_that_will_never_appear_times_out_honestly() {
        let started = Instant::now();
        let answer = dispatch(
            WAIT,
            json!({
                "for": "window",
                "title_contains": "окна-с-таким-именем-не-существует-27072026",
                "timeout_ms": 400,
                "poll_interval_ms": 100,
            }),
            Path::new("."),
        )
        .await
        .expect("timeout is an answer, not a failure");
        // Срок назван и соблюдён: не «висим до серверного таймаута».
        assert!(started.elapsed() < Duration::from_secs(5));
        assert_eq!(answer["met"], json!(false));
        assert_eq!(answer["timed_out"], json!(true));
        assert_eq!(answer["limits"]["timeout_ms"], json!(400));
        assert!(answer["polls"].as_u64().unwrap() >= 1);
        assert!(
            answer["reason"]
                .as_str()
                .unwrap()
                .contains("timeout_ms=400")
        );
    }
}
