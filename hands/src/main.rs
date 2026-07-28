//! praxis-hands — компилируемый пол её кодинговых рук.
//!
//! Питон (workshop.py) остаётся её самоизменяемыми пальцами; сюда вынесено то, что
//! должно быть твёрдым: разрешение путей, зоны записи, тайм-ауты,
//! потолки вывода, защита от случайного усечения — и расписка о каждой операции.
//!
//! Контракт с питоном: аргументы — флаги, ответ — ОДНА JSON-строка в stdout.
//! Код возврата 0 всегда, когда бинарь отработал (ошибка операции живёт в поле `ok`);
//! ненулевой код — только если бинарь не понял, чего от него хотят.
//!
//! Зависимостей нет: собирается офлайн, за минуту, статически (musl).

mod rails;

use std::collections::HashMap;
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

// --------------------------------------------------------------------------- //
//  Крошечный JSON-писатель (читателя не нужно — питон только шлёт флаги)
// --------------------------------------------------------------------------- //

fn jesc(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn jobj(fields: &[(&str, String)]) -> String {
    let body: Vec<String> = fields.iter().map(|(k, v)| format!("{}:{}", jesc(k), v)).collect();
    format!("{{{}}}", body.join(","))
}

fn jbool(b: bool) -> String {
    (if b { "true" } else { "false" }).to_string()
}

fn jarr(items: &[String]) -> String {
    format!("[{}]", items.join(","))
}

// --------------------------------------------------------------------------- //
//  Расписка: каждая операция оставляет след (jsonl), даже неудачная
// --------------------------------------------------------------------------- //

fn now_ts() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn receipt(path: &Option<String>, op: &str, ok: bool, target: &str, note: &str) {
    let Some(p) = path else { return };
    let line = jobj(&[
        ("ts", now_ts().to_string()),
        ("op", jesc(op)),
        ("ok", jbool(ok)),
        // target тоже под потолком: exec приносит сюда целые команды-простыни
        ("target", jesc(&target.chars().take(200).collect::<String>())),
        ("note", jesc(&note.chars().take(200).collect::<String>())),
    ]);
    if let Some(dir) = Path::new(p).parent() {
        let _ = fs::create_dir_all(dir);
    }
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(p) {
        use std::io::Write;
        let _ = writeln!(f, "{}", line);
    }
}

// --------------------------------------------------------------------------- //
//  Пути: лексическая нормализация + джейл. Симлинки не разыменовываем намеренно —
//  бинарь не должен зависеть от состояния ФС, чтобы решение было предсказуемым.
// --------------------------------------------------------------------------- //

fn normalize(base: &Path, raw: &str) -> Option<PathBuf> {
    let p = Path::new(raw);
    let joined = if p.is_absolute() { p.to_path_buf() } else { base.join(p) };
    let mut out = PathBuf::new();
    for c in joined.components() {
        match c {
            Component::ParentDir => {
                if !out.pop() {
                    return None;
                }
            }
            Component::CurDir => {}
            c => out.push(c.as_os_str()),
        }
    }
    Some(out)
}

fn inside(root: &Path, cand: &Path) -> bool {
    cand.starts_with(root)
}

fn basename(p: &Path) -> String {
    p.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default()
}

/// Разрешение пути на ЗАПИСЬ — зеркало workshop._resolve_write.
/// С `root` (worktree предложения) корень — он; без него первый сегмент обязан быть
/// зоной записи (workspace/soul/memory). Имя файла ничего не запрещает.
fn resolve_write(base: &Path, root: &Option<String>, raw: &str) -> Result<PathBuf, String> {
    if raw.trim().is_empty() {
        return Err("пустой путь".into());
    }
    if let Some(r) = root {
        let wt = normalize(Path::new("."), r).ok_or("плохой корень предложения")?;
        if !wt.exists() {
            return Err(format!("нет worktree предложения: {}", r));
        }
        let cand = normalize(&wt, raw).ok_or("путь вылезает за корень")?;
        if !inside(&wt, &cand) {
            return Err("путь вне worktree предложения".into());
        }
        return Ok(cand);
    }
    let cand = normalize(base, raw).ok_or("путь вылезает за дом")?;
    if !inside(base, &cand) {
        return Err("путь вне дома".into());
    }
    let rel = cand.strip_prefix(base).map_err(|_| "путь вне дома".to_string())?;
    let first = rel.components().next().map(|c| c.as_os_str().to_string_lossy().to_string());
    match first {
        Some(f) if rails::WRITE_ZONES.contains(&f.as_str()) => Ok(cand),
        _ => Err("ядро — через предложение (proposal_id); без него пишу только в \
                  workspace/, soul/, memory/"
            .into()),
    }
}

fn resolve_read(base: &Path, raw: &str) -> Result<PathBuf, String> {
    let cand = normalize(base, raw).ok_or("путь вылезает за дом")?;
    if !inside(base, &cand) {
        return Err("путь вне дома".into());
    }
    Ok(cand)
}

// --------------------------------------------------------------------------- //
//  Потолок вывода: голова + хвост (зеркало workshop._cap_output)
// --------------------------------------------------------------------------- //

fn cap_output(s: &str, cap: usize) -> String {
    if s.chars().count() <= cap {
        return s.to_string();
    }
    let chars: Vec<char> = s.chars().collect();
    let head_n = (cap as f64 * 0.6) as usize;
    let tail_n = (cap as f64 * 0.35) as usize;
    let head: String = chars[..head_n].iter().collect();
    let tail: String = chars[chars.len() - tail_n..].iter().collect();
    let cut = chars.len() - head_n - tail_n;
    format!("{}\n… (вырезано {} символов) …\n{}", head, cut, tail)
}

// --------------------------------------------------------------------------- //
//  exec — запуск с тайм-аутом, джейлом cwd и потолком вывода
// --------------------------------------------------------------------------- //

/// Команда оболочки. На Windows — raw_arg: иначе std перекавычивает строку, и путь
/// с пробелами («C:\Program Files\…») приезжает в cmd покалеченным. Это не косметика:
/// на этом же бинаре потом будет стоять её windows-демон.
///
/// Внешняя пара кавычек — тоже не украшение: cmd /C съедает первую и последнюю кавычку
/// строки, поэтому команду, которая сама начинается с кавычки, надо обернуть ещё раз.
#[cfg(windows)]
fn shell_command(shell: &str) -> Command {
    use std::os::windows::process::CommandExt;
    let mut c = Command::new("cmd");
    c.raw_arg(format!("/C \"{}\"", shell));
    c
}

#[cfg(not(windows))]
fn shell_command(shell: &str) -> Command {
    let mut c = Command::new("bash");
    c.arg("-lc").arg(shell);
    c
}

/// Свою группу процессов — чтобы по тайм-ауту убить ВНУКОВ, а не только оболочку.
#[cfg(unix)]
fn detach_group(cmd: &mut Command) {
    use std::os::unix::process::CommandExt;
    cmd.process_group(0);
}

#[cfg(not(unix))]
fn detach_group(_cmd: &mut Command) {}

/// Убить дерево, а не одного родителя. Иначе внук (питон под `bash -lc`, под `cmd /C`)
/// переживает kill и держит пайпы — тайм-аут «срабатывает», а мы всё равно ждём его
/// до конца. Ровно этот баг ловит test_timeout_flag_actually_reaches_the_binary.
fn kill_tree(child: &mut std::process::Child) {
    let pid = child.id();
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/F", "/T", "/PID", &pid.to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    #[cfg(unix)]
    {
        // группа = pid ребёнка (detach_group), минус — «всей группе»
        let _ = Command::new("kill")
            .args(["-KILL", &format!("-{}", pid)])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn op_exec(base: &Path, a: &Args) -> String {
    let cwd_raw = a.get("cwd").unwrap_or_else(|| ".".into());
    let shell = match a.get("shell") {
        Some(s) => s,
        None => return jobj(&[("ok", jbool(false)), ("msg", jesc("нужен --shell"))]),
    };
    let timeout = a.num("timeout", 120);
    let cap = a.num("cap", 8000) as usize;

    let cwd = match normalize(base, &cwd_raw) {
        Some(p) if inside(base, &p) && p.is_dir() => p,
        _ => {
            return jobj(&[
                ("ok", jbool(false)),
                ("msg", jesc("рабочий каталог вне дома или не существует")),
            ])
        }
    };

    let mut cmd = shell_command(&shell);
    cmd.current_dir(&cwd).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    detach_group(&mut cmd);

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return jobj(&[("ok", jbool(false)), ("msg", jesc(&format!("не запустилось: {e}")))]),
    };
    // Пайпы читаем в отдельных нитях: полный буфер иначе намертво держит ребёнка.
    let mut so = child.stdout.take().expect("stdout piped");
    let mut se = child.stderr.take().expect("stderr piped");
    let t_out = thread::spawn(move || {
        let mut b = Vec::new();
        let _ = so.read_to_end(&mut b);
        b
    });
    let t_err = thread::spawn(move || {
        let mut b = Vec::new();
        let _ = se.read_to_end(&mut b);
        b
    });

    let start = Instant::now();
    let limit = Duration::from_secs(timeout as u64);
    let (code, killed) = loop {
        match child.try_wait() {
            Ok(Some(st)) => break (st.code().unwrap_or(-1), false),
            Ok(None) => {
                if start.elapsed() >= limit {
                    kill_tree(&mut child);
                    break (-1, true);
                }
                thread::sleep(Duration::from_millis(40));
            }
            Err(e) => return jobj(&[("ok", jbool(false)), ("msg", jesc(&format!("wait: {e}")))]),
        }
    };
    let out = String::from_utf8_lossy(&t_out.join().unwrap_or_default()).to_string();
    let err = String::from_utf8_lossy(&t_err.join().unwrap_or_default()).to_string();
    let mut all = out;
    if !err.trim().is_empty() {
        if !all.is_empty() {
            all.push('\n');
        }
        all.push_str(&err);
    }
    if killed {
        all.push_str(&format!("\n⏱ прервано по тайм-ауту ({timeout}с)"));
    }
    let ok = code == 0 && !killed;
    let dur_ms = start.elapsed().as_millis();
    receipt(&a.get("receipt"), "exec", ok, &shell,
            &format!("code={code} killed={killed} dur_ms={dur_ms}"));
    jobj(&[
        ("ok", jbool(ok)),
        ("code", code.to_string()),
        ("killed", jbool(killed)),
        ("out", jesc(&cap_output(&all, cap))),
    ])
}

// --------------------------------------------------------------------------- //
//  edit — ровно одно вхождение; 0 → превью, >1 → номера строк
// --------------------------------------------------------------------------- //

fn line_of(text: &str, byte_idx: usize) -> usize {
    text[..byte_idx].matches('\n').count() + 1
}

fn op_edit(base: &Path, a: &Args) -> String {
    let (file, old_f, new_f) = match (a.get("file"), a.get("old-file"), a.get("new-file")) {
        (Some(f), Some(o), Some(n)) => (f, o, n),
        _ => return jobj(&[("ok", jbool(false)), ("msg", jesc("нужны --file --old-file --new-file"))]),
    };
    let target = match resolve_write(base, &a.get("root"), &file) {
        Ok(p) => p,
        Err(e) => {
            receipt(&a.get("receipt"), "edit", false, &file, &e);
            return jobj(&[("ok", jbool(false)), ("msg", jesc(&format!("Не правлю: {e}")))]);
        }
    };
    if !target.is_file() {
        return jobj(&[
            ("ok", jbool(false)),
            ("msg", jesc(&format!("Нет файла {file} — новый файл создаётся fs_write."))),
        ]);
    }
    let old = fs::read_to_string(&old_f).unwrap_or_default();
    let new = fs::read_to_string(&new_f).unwrap_or_default();
    if old.is_empty() {
        return jobj(&[("ok", jbool(false)), ("msg", jesc("Пустой old — так не правлю."))]);
    }
    let text = match fs::read_to_string(&target) {
        Ok(t) => t,
        Err(e) => return jobj(&[("ok", jbool(false)), ("msg", jesc(&format!("не читается: {e}")))]),
    };
    let hits: Vec<usize> = text.match_indices(&old).map(|(i, _)| i).collect();
    if hits.is_empty() {
        let preview: String = text.chars().take(600).collect();
        receipt(&a.get("receipt"), "edit", false, &file, "0 вхождений");
        return jobj(&[
            ("ok", jbool(false)),
            ("msg", jesc("Вхождение не найдено (0 совпадений). Проверь точный текст через \
                          fs_read — пробелы/табуляция/кавычки должны совпадать байт в байт.")),
            ("preview", jesc(&preview)),
        ]);
    }
    if hits.len() > 1 {
        let lines: Vec<String> = hits.iter().take(5).map(|i| format!("строка {}", line_of(&text, *i))).collect();
        receipt(&a.get("receipt"), "edit", false, &file, &format!("{} вхождений", hits.len()));
        return jobj(&[
            ("ok", jbool(false)),
            ("msg", jesc(&format!(
                "Неоднозначно: {} совпадений (первые: {}). Расширь old несколькими строками \
                 контекста вокруг нужного места, чтобы вхождение стало уникальным.",
                hits.len(),
                lines.join(", ")
            ))),
            ("lines", jarr(&hits.iter().take(5).map(|i| line_of(&text, *i).to_string()).collect::<Vec<_>>())),
        ]);
    }
    let patched = text.replacen(&old, &new, 1);
    if let Err(e) = fs::write(&target, patched) {
        return jobj(&[("ok", jbool(false)), ("msg", jesc(&format!("не записалось: {e}")))]);
    }
    receipt(&a.get("receipt"), "edit", true, &file, "1 вхождение заменено");
    jobj(&[
        ("ok", jbool(true)),
        ("msg", jesc(&format!("Поправила {file}: 1 вхождение заменено."))),
    ])
}

// --------------------------------------------------------------------------- //
//  write — создание, осознанная перезапись, гард от случайного усечения
// --------------------------------------------------------------------------- //

fn op_write(base: &Path, a: &Args) -> String {
    let (file, content_f) = match (a.get("file"), a.get("content-file")) {
        (Some(f), Some(c)) => (f, c),
        _ => return jobj(&[("ok", jbool(false)), ("msg", jesc("нужны --file --content-file"))]),
    };
    let overwrite = a.flag("overwrite");
    let force = a.flag("force");
    let target = match resolve_write(base, &a.get("root"), &file) {
        Ok(p) => p,
        Err(e) => {
            receipt(&a.get("receipt"), "write", false, &file, &e);
            return jobj(&[("ok", jbool(false)), ("msg", jesc(&format!("Не пишу: {e}")))]);
        }
    };
    let content = fs::read_to_string(&content_f).unwrap_or_default();
    if target.exists() {
        if !overwrite {
            return jobj(&[
                ("ok", jbool(false)),
                ("msg", jesc(&format!(
                    "Файл {file} уже существует — правь точечно через fs_edit, \
                     или передай overwrite=true, если правда хочешь переписать целиком."
                ))),
            ]);
        }
        if !force {
            let old_len = fs::read_to_string(&target).map(|s| s.chars().count()).unwrap_or(0);
            let new_len = content.chars().count();
            if old_len > 0 && (new_len as f64) < (old_len as f64) * 0.7 {
                let pct = (new_len as f64 / old_len as f64 * 100.0).round() as i64;
                receipt(&a.get("receipt"), "write", false, &file, "shrink-guard");
                return jobj(&[
                    ("ok", jbool(false)),
                    ("msg", jesc(&format!(
                        "Стоп: новый текст {file} — {pct}% от старого ({old_len} → {new_len} симв.). \
                         Похоже на случайное усечение. Точечно — fs_edit; если перезапись \
                         осознанная — force=true."
                    ))),
                ]);
            }
        }
    }
    if let Some(dir) = target.parent() {
        let _ = fs::create_dir_all(dir);
    }
    if let Err(e) = fs::write(&target, &content) {
        return jobj(&[("ok", jbool(false)), ("msg", jesc(&format!("не записалось: {e}")))]);
    }
    receipt(&a.get("receipt"), "write", true, &file, &format!("{} симв.", content.chars().count()));
    jobj(&[
        ("ok", jbool(true)),
        ("msg", jesc(&format!("Записала {file} ({} симв.).", content.chars().count()))),
    ])
}

// --------------------------------------------------------------------------- //
//  outline — скелет python-файла (классы/функции), чтобы не читать 2000 строк
// --------------------------------------------------------------------------- //

fn op_outline(base: &Path, a: &Args) -> String {
    let file = match a.get("file") {
        Some(f) => f,
        None => return jobj(&[("ok", jbool(false)), ("msg", jesc("нужен --file"))]),
    };
    let target = match resolve_read(base, &file) {
        Ok(p) => p,
        Err(e) => return jobj(&[("ok", jbool(false)), ("msg", jesc(&e))]),
    };
    let text = match fs::read_to_string(&target) {
        Ok(t) => t,
        Err(e) => return jobj(&[("ok", jbool(false)), ("msg", jesc(&format!("не читается: {e}")))]),
    };
    let mut items: Vec<String> = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let indent = line.len() - line.trim_start().len();
        let t = line.trim_start();
        let kind = if t.starts_with("class ") {
            "class"
        } else if t.starts_with("def ") {
            "def"
        } else if t.starts_with("async def ") {
            "async def"
        } else {
            continue;
        };
        let rest = t.split_once(' ').map(|parts| parts.1).unwrap_or("");
        let name: String = rest.chars().take_while(|c| c.is_alphanumeric() || *c == '_').collect();
        if name.is_empty() {
            continue;
        }
        items.push(jobj(&[
            ("kind", jesc(kind)),
            ("name", jesc(&name)),
            ("line", (i + 1).to_string()),
            ("indent", indent.to_string()),
        ]));
        if items.len() >= 400 {
            break;
        }
    }
    receipt(&a.get("receipt"), "outline", true, &file, &format!("{} символов кода", items.len()));
    jobj(&[("ok", jbool(true)), ("items", jarr(&items))])
}

// --------------------------------------------------------------------------- //
//  search — литеральный поиск (регекспы остаются питону: у std их нет, и это честно)
// --------------------------------------------------------------------------- //

fn walk(dir: &Path, recursive: bool, name_glob: &str, out: &mut Vec<PathBuf>, budget: &mut usize) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        if *budget == 0 {
            return;
        }
        let p = e.path();
        let name = basename(&p);
        if p.is_dir() {
            if recursive && !rails::SKIP_DIRS.contains(&name.as_str()) {
                walk(&p, recursive, name_glob, out, budget);
            }
        } else if rails::glob_match(name_glob, &name) {
            out.push(p);
            *budget -= 1;
        }
    }
}

fn op_search(base: &Path, a: &Args) -> String {
    let literal = match a.get("literal") {
        Some(l) if !l.is_empty() => l,
        _ => return jobj(&[("ok", jbool(false)), ("msg", jesc("нужен --literal"))]),
    };
    let root_raw = a.get("root").unwrap_or_else(|| ".".into());
    let root = match resolve_read(base, &root_raw) {
        Ok(p) if p.is_dir() => p,
        _ => return jobj(&[("ok", jbool(false)), ("msg", jesc("плохой корень поиска"))]),
    };
    // Питон зовёт с glob вида `**/*.py` или `*.py`: рекурсия + маска имени.
    let raw_glob = a.get("glob").unwrap_or_else(|| "**/*.py".into());
    let recursive = raw_glob.starts_with("**/");
    let name_glob = raw_glob.rsplit('/').next().unwrap_or("*").to_string();
    let cap = a.num("cap", 60) as usize;
    let ignore_case = a.flag("ignore-case");
    let needle = if ignore_case { literal.to_lowercase() } else { literal.clone() };

    let mut files = Vec::new();
    let mut budget = 5000usize;
    walk(&root, recursive, &name_glob, &mut files, &mut budget);
    files.sort();

    let mut hits: Vec<String> = Vec::new();
    for f in &files {
        let Ok(text) = fs::read_to_string(f) else { continue };
        let rel = f.strip_prefix(&root).unwrap_or(f).to_string_lossy().replace('\\', "/");
        for (i, line) in text.lines().enumerate() {
            let hay = if ignore_case { line.to_lowercase() } else { line.to_string() };
            if hay.contains(&needle) {
                let clip: String = line.trim().chars().take(180).collect();
                hits.push(jesc(&format!("{}:{}: {}", rel, i + 1, clip)));
                if hits.len() >= cap {
                    break;
                }
            }
        }
        if hits.len() >= cap {
            break;
        }
    }
    receipt(&a.get("receipt"), "search", true, &literal, &format!("{} совпадений", hits.len()));
    jobj(&[
        ("ok", jbool(true)),
        ("files_seen", files.len().to_string()),
        ("capped", jbool(hits.len() >= cap)),
        ("hits", jarr(&hits)),
    ])
}

// --------------------------------------------------------------------------- //
//  guard — «а можно ли?» без действия. Тесты сверяют вердикт с питоном.
// --------------------------------------------------------------------------- //

fn op_guard(base: &Path, a: &Args) -> String {
    let path = a.get("path").unwrap_or_default();
    let op = a.get("op").unwrap_or_else(|| "write".into());
    let verdict = match op.as_str() {
        "read" => resolve_read(base, &path).map(|_| ()),
        "floor" => {
            let on = rails::on_floor(&path.replace('\\', "/"));
            return jobj(&[("ok", jbool(!on)), ("on_floor", jbool(on))]);
        }
        _ => resolve_write(base, &a.get("root"), &path).map(|_| ()),
    };
    match verdict {
        Ok(()) => jobj(&[("ok", jbool(true))]),
        Err(e) => jobj(&[("ok", jbool(false)), ("msg", jesc(&e))]),
    }
}

// --------------------------------------------------------------------------- //
//  Разбор аргументов
// --------------------------------------------------------------------------- //

struct Args {
    kv: HashMap<String, String>,
    flags: Vec<String>,
}

impl Args {
    fn parse(it: impl Iterator<Item = String>) -> Args {
        let (mut kv, mut flags) = (HashMap::new(), Vec::new());
        let argv: Vec<String> = it.collect();
        let mut i = 0;
        while i < argv.len() {
            let a = &argv[i];
            if let Some(name) = a.strip_prefix("--") {
                if i + 1 < argv.len() && !argv[i + 1].starts_with("--") {
                    kv.insert(name.to_string(), argv[i + 1].clone());
                    i += 2;
                    continue;
                }
                flags.push(name.to_string());
            }
            i += 1;
        }
        Args { kv, flags }
    }
    fn get(&self, k: &str) -> Option<String> {
        self.kv.get(k).cloned()
    }
    fn flag(&self, k: &str) -> bool {
        self.flags.iter().any(|f| f == k) || self.kv.get(k).map(|v| v == "true").unwrap_or(false)
    }
    fn num(&self, k: &str, dflt: i64) -> i64 {
        self.get(k).and_then(|v| v.parse().ok()).unwrap_or(dflt)
    }
}

fn main() {
    let mut argv = std::env::args().skip(1);
    let Some(sub) = argv.next() else {
        eprintln!("praxis-hands <exec|edit|write|outline|search|guard|version> --флаги");
        std::process::exit(2);
    };
    let a = Args::parse(argv);
    if sub == "version" {
        // rails_fp — рукопожатие рельсов: мост сверяет отпечаток с rails.rs репо,
        // бинарь, собранный под старые таблицы, виден сразу (state_line + тест в гейте).
        println!("{}", jobj(&[
            ("ok", jbool(true)),
            ("version", jesc(env!("CARGO_PKG_VERSION"))),
            ("rails_fp", jesc(rails::FINGERPRINT)),
        ]));
        return;
    }
    let base_raw = a.get("base").unwrap_or_else(|| ".".into());
    let base = match normalize(Path::new("."), &base_raw) {
        Some(b) => b,
        None => {
            eprintln!("плохой --base");
            std::process::exit(2);
        }
    };
    let out = match sub.as_str() {
        "exec" => op_exec(&base, &a),
        "edit" => op_edit(&base, &a),
        "write" => op_write(&base, &a),
        "outline" => op_outline(&base, &a),
        "search" => op_search(&base, &a),
        "guard" => op_guard(&base, &a),
        other => {
            eprintln!("не знаю команду: {other}");
            std::process::exit(2);
        }
    };
    println!("{}", out);
}

// --------------------------------------------------------------------------- //
//  Тесты рельсов: пол компилируемый — пусть и проверяется компилятором
// --------------------------------------------------------------------------- //

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floor_matches_globs() {
        assert!(rails::on_floor("bootguard.py"));
        assert!(rails::on_floor("docker-compose.deploy.yml"));
        assert!(rails::on_floor("companion/selfdev.py"));
        assert!(!rails::on_floor("agent.py"));
    }

    #[test]
    fn normalize_blocks_escape() {
        let base = Path::new("/app");
        assert!(normalize(base, "../etc/passwd").map(|p| inside(base, &p)) != Some(true));
        assert_eq!(normalize(base, "workspace/x.py"), Some(PathBuf::from("/app/workspace/x.py")));
    }

    #[test]
    fn write_zones_enforced() {
        let base = Path::new("/app");
        assert!(resolve_write(base, &None, "agent.py").is_err());
        assert!(resolve_write(base, &None, "workspace/p/x.py").is_ok());
        assert!(resolve_write(base, &None, "memory/.env").is_ok());
    }

    #[test]
    fn cap_output_keeps_head_and_tail() {
        let s = "x".repeat(100);
        let out = cap_output(&s, 20);
        assert!(out.contains("вырезано"));
        assert!(out.chars().count() < s.chars().count() + 40);
    }
}
