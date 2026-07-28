use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use std::time::Instant;

use anyhow::{Context, Result};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

#[derive(Debug, Deserialize)]
struct PathArgs {
    path: PathBuf,
}

#[derive(Debug, Deserialize)]
struct ListArgs {
    path: PathBuf,
    #[serde(default)]
    offset: usize,
    #[serde(default = "default_list_limit")]
    limit: usize,
}

#[derive(Debug, Deserialize)]
struct ReadArgs {
    path: PathBuf,
    #[serde(default)]
    offset: u64,
    #[serde(default = "default_read_limit")]
    limit: u64,
}

#[derive(Debug, Deserialize)]
struct WriteArgs {
    path: PathBuf,
    content: String,
    #[serde(default)]
    expected_sha256: Option<String>,
    #[serde(default)]
    backup: bool,
}

#[derive(Debug, Deserialize)]
struct ReplaceArgs {
    path: PathBuf,
    old: String,
    new: String,
    #[serde(default)]
    expected_sha256: Option<String>,
    #[serde(default)]
    backup: bool,
}

#[derive(Debug, Deserialize)]
struct PatchArgs {
    root: PathBuf,
    patch: String,
}

#[derive(Debug, Deserialize)]
struct DeleteArgs {
    path: PathBuf,
    #[serde(default)]
    recursive: bool,
    #[serde(flatten)]
    extra: Map<String, Value>,
}

#[derive(Debug, Deserialize)]
struct MoveArgs {
    #[serde(alias = "source", alias = "src")]
    from: PathBuf,
    #[serde(alias = "destination", alias = "dst")]
    to: PathBuf,
    #[serde(default)]
    overwrite: bool,
    #[serde(flatten)]
    extra: Map<String, Value>,
}

#[derive(Debug, Deserialize)]
struct CopyArgs {
    #[serde(alias = "source", alias = "src")]
    from: PathBuf,
    #[serde(alias = "destination", alias = "dst")]
    to: PathBuf,
    #[serde(default)]
    recursive: bool,
    #[serde(default)]
    overwrite: bool,
    #[serde(flatten)]
    extra: Map<String, Value>,
}

#[derive(Debug, Deserialize)]
struct MkdirArgs {
    path: PathBuf,
    #[serde(default = "default_parents")]
    parents: bool,
    #[serde(flatten)]
    extra: Map<String, Value>,
}

fn default_read_limit() -> u64 {
    1024 * 1024
}

fn default_list_limit() -> usize {
    1_000
}

fn default_parents() -> bool {
    true
}

pub fn dispatch(capability: &str, args: Value) -> Result<Value> {
    match capability {
        "fs.stat" => stat(serde_json::from_value(args)?),
        "fs.list" => list(serde_json::from_value(args)?),
        "fs.read" => read(serde_json::from_value(args)?),
        "fs.hash" => hash_value(serde_json::from_value(args)?),
        "fs.write_atomic" => write_atomic(serde_json::from_value(args)?),
        "fs.replace" => replace(serde_json::from_value(args)?),
        "fs.apply_patch" => apply_patch(serde_json::from_value(args)?),
        "fs.delete" => delete(serde_json::from_value(args)?),
        "fs.move" => move_path(serde_json::from_value(args)?),
        "fs.copy" => copy(serde_json::from_value(args)?),
        "fs.mkdir" => mkdir(serde_json::from_value(args)?),
        _ => anyhow::bail!("unknown filesystem capability {capability}"),
    }
}

fn apply_patch(args: PatchArgs) -> Result<Value> {
    if args.patch.trim().is_empty() {
        anyhow::bail!("unified patch is empty");
    }
    let stage = std::env::temp_dir().join(format!("praxis-{}.patch", Uuid::new_v4()));
    fs::write(&stage, args.patch.as_bytes())?;
    let mut check_command = std::process::Command::new("git.exe");
    check_command
        .args(["-C"])
        .arg(&args.root)
        .args(["apply", "--check", "--whitespace=nowarn"])
        .arg(&stage);
    let check = command_output(&mut check_command).context("run git apply --check")?;
    if !check.status.success() {
        let _ = fs::remove_file(&stage);
        anyhow::bail!(
            "patch check failed: {}",
            String::from_utf8_lossy(&check.stderr)
        );
    }
    let mut apply_command = std::process::Command::new("git.exe");
    apply_command
        .args(["-C"])
        .arg(&args.root)
        .args(["apply", "--whitespace=nowarn"])
        .arg(&stage);
    let applied = command_output(&mut apply_command).context("run git apply")?;
    let _ = fs::remove_file(&stage);
    if !applied.status.success() {
        anyhow::bail!(
            "patch apply failed after check: {}",
            String::from_utf8_lossy(&applied.stderr)
        );
    }
    Ok(json!({
        "ok": true,
        "root": args.root,
        "patch_sha256": hex::encode(Sha256::digest(args.patch.as_bytes())),
    }))
}

fn command_output(command: &mut std::process::Command) -> std::io::Result<std::process::Output> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows::Win32::System::Threading::CREATE_NO_WINDOW;

        command.creation_flags(CREATE_NO_WINDOW.0);
    }
    command.output()
}

fn stat(args: PathArgs) -> Result<Value> {
    let metadata = fs::symlink_metadata(&args.path)
        .with_context(|| format!("stat {}", args.path.display()))?;
    Ok(json!({
        "ok": true,
        "path": args.path,
        "kind": if metadata.file_type().is_symlink() { "reparse" } else if metadata.is_dir() { "directory" } else if metadata.is_file() { "file" } else { "other" },
        "size": metadata.len(),
        "readonly": metadata.permissions().readonly(),
        "modified_unix_ms": metadata.modified().ok().and_then(|x| x.duration_since(std::time::UNIX_EPOCH).ok()).map(|x| x.as_millis()),
    }))
}

fn list(args: ListArgs) -> Result<Value> {
    const MAX_SCAN: usize = 100_000;
    let limit = args.limit.clamp(1, 4_096);
    let mut rows = Vec::new();
    for entry in
        fs::read_dir(&args.path).with_context(|| format!("list {}", args.path.display()))?
    {
        if rows.len() >= MAX_SCAN {
            anyhow::bail!(
                "directory contains more than {MAX_SCAN} entries; use rg or a narrower path"
            );
        }
        let entry = entry?;
        let metadata = entry.metadata()?;
        rows.push(json!({
            "name": entry.file_name().to_string_lossy(),
            "path": entry.path(),
            "kind": if metadata.is_dir() { "directory" } else if metadata.is_file() { "file" } else { "other" },
            "size": metadata.len(),
        }));
    }
    rows.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
    let total = rows.len();
    let start = args.offset.min(total);
    let end = start.saturating_add(limit).min(total);
    let entries = rows.drain(start..end).collect::<Vec<_>>();
    Ok(json!({
        "ok": true,
        "path": args.path,
        "offset": start,
        "limit": limit,
        "total": total,
        "next_offset": end,
        "eof": end == total,
        "entries": entries,
    }))
}

fn read(args: ReadArgs) -> Result<Value> {
    let limit = args.limit.clamp(1, 8 * 1024 * 1024);
    let mut file =
        File::open(&args.path).with_context(|| format!("read {}", args.path.display()))?;
    let size = file.metadata()?.len();
    if args.offset > size {
        anyhow::bail!("offset {} is beyond file size {}", args.offset, size);
    }
    file.seek(SeekFrom::Start(args.offset))?;
    let mut bytes = vec![0u8; limit.min(size - args.offset) as usize];
    file.read_exact(&mut bytes)?;
    Ok(json!({
        "ok": true,
        "path": args.path,
        "offset": args.offset,
        "size": size,
        "next_offset": args.offset + bytes.len() as u64,
        "eof": args.offset + bytes.len() as u64 == size,
        "text": String::from_utf8_lossy(&bytes),
        "lossy": std::str::from_utf8(&bytes).is_err(),
    }))
}

fn hash_value(args: PathArgs) -> Result<Value> {
    let (sha256, size) = sha256_file(&args.path)?;
    Ok(json!({"ok": true, "path": args.path, "sha256": sha256, "size": size}))
}

fn write_atomic(args: WriteArgs) -> Result<Value> {
    let before = existing_hash(&args.path)?;
    check_expected(before.as_deref(), args.expected_sha256.as_deref())?;
    let backup = if args.backup && args.path.exists() {
        Some(create_backup(&args.path)?)
    } else {
        None
    };
    atomic_write(&args.path, args.content.as_bytes())?;
    let (after, size) = sha256_file(&args.path)?;
    Ok(json!({
        "ok": true,
        "path": args.path,
        "before_sha256": before,
        "after_sha256": after,
        "size": size,
        "backup": backup,
    }))
}

fn replace(args: ReplaceArgs) -> Result<Value> {
    let before = existing_hash(&args.path)?;
    check_expected(before.as_deref(), args.expected_sha256.as_deref())?;
    let text = fs::read_to_string(&args.path)
        .with_context(|| format!("read text {}", args.path.display()))?;
    let count = text.matches(&args.old).count();
    if count != 1 {
        anyhow::bail!("exact replace requires one match, found {count}");
    }
    let backup = if args.backup {
        Some(create_backup(&args.path)?)
    } else {
        None
    };
    atomic_write(
        &args.path,
        text.replacen(&args.old, &args.new, 1).as_bytes(),
    )?;
    let (after, size) = sha256_file(&args.path)?;
    Ok(json!({
        "ok": true,
        "path": args.path,
        "before_sha256": before,
        "after_sha256": after,
        "size": size,
        "backup": backup,
    }))
}

// ── Дерево: удалить, переместить, скопировать, создать ───────────────────────────────
//
// ⚠ Десять файловых глаголов умели читать файл и писать файл — и ни один не менял дерево.
// Каждое «удали это» она делала запуском PowerShell: отдельная отслеживаемая операция со
// своим каталогом, которая остаётся в реестре навсегда (на этой машине таких записей уже
// 243), секунды вместо миллисекунд и мусор в учёте. Четыре глагола ниже закрывают дыру.
//
// Три правила, общие для всех четырёх:
//
//  1. Рекурсия — её явный выбор, а не наша догадка. «Удали пустой каталог» и «снеси
//     дерево» — разные намерения. Это не забор: отказ называет ровно то слово, которое
//     надо дописать, и повтор с ним проходит без всякого подтверждения.
//  2. Все пределы едут в ответе полем `limits` — даже когда до них не дошло. Молчаливый
//     предел хуже отсутствующего: об отсутствующий не спотыкаются вслепую.
//  3. Частичный результат называется частичным: `complete:false`, `failed[]` и `summary`,
//     который начинается со слова PARTIAL. Полагаться на внешнее поле `ok` здесь нельзя —
//     `body_client.call` собирает ответ как `{**frame, **result, "ok": frame["ok"]}`, то
//     есть наш `ok` перетирается статусом кадра. Поэтому правда живёт в `complete`,
//     `failed_count` и `summary`, которые никто не перетирает.

/// Сколько узлов дерева обходим за один вызов, прежде чем ответить частично.
const MAX_TREE_ENTRIES: usize = 200_000;
/// Сколько секунд работаем, прежде чем ответить частично.
///
/// ⚠ Число выбрано не с потолка: сервер зовёт тело с `timeout=60` (`body_client.call`).
/// Ответ, который не успел вернуться, для неё неотличим от «тело умерло» — а честное
/// «снесла 40 тысяч файлов из 300 тысяч, кончилось время» она может продолжить повтором.
const MAX_TREE_SECONDS: u64 = 45;
/// Сколько отказов перечисляем поимённо (счётчик `failed_count` — полный, без капа).
const MAX_FAILURES_REPORTED: usize = 20;
/// Сколько созданных каталогов перечисляем поимённо в `fs.mkdir`.
const MAX_CREATED_DIRS_REPORTED: usize = 64;

fn tree_limits() -> Value {
    json!({
        "max_entries": MAX_TREE_ENTRIES,
        "max_seconds": MAX_TREE_SECONDS,
        "max_failures_reported": MAX_FAILURES_REPORTED,
    })
}

/// Неизвестные аргументы не проглатываем и не роняем из-за них вызов: называем.
///
/// ⚠ Опечатка `recursiv: true` при молчаливом serde означала бы «рекурсия выключена» —
/// ровно тот молчаливый предел, которого в этом проекте быть не должно. Отвергать вызов
/// целиком тоже неправильно: тогда одно лишнее поле от диспетчера ломает глагол.
fn ignored_args(extra: &Map<String, Value>) -> Vec<String> {
    let mut names: Vec<String> = extra.keys().cloned().collect();
    names.sort();
    names
}

/// Бюджет обхода: узлы и время. Кончился — не идём дальше, но и не врём.
struct Budget {
    started: Instant,
    entries: usize,
    stopped: Option<String>,
}

impl Budget {
    fn new() -> Self {
        Self {
            started: Instant::now(),
            entries: 0,
            stopped: None,
        }
    }

    /// Оплатить один узел. `false` — бюджет исчерпан, причина уже записана.
    fn spend(&mut self) -> bool {
        if self.stopped.is_some() {
            return false;
        }
        if self.entries >= MAX_TREE_ENTRIES {
            self.stopped = Some(format!(
                "entry limit reached: {MAX_TREE_ENTRIES} nodes visited"
            ));
            return false;
        }
        let elapsed = self.started.elapsed().as_secs();
        if elapsed >= MAX_TREE_SECONDS {
            self.stopped = Some(format!(
                "time limit reached: {elapsed}s of {MAX_TREE_SECONDS}s allowed per call"
            ));
            return false;
        }
        self.entries += 1;
        true
    }

    fn elapsed_ms(&self) -> u128 {
        self.started.elapsed().as_millis()
    }
}

/// Что именно случилось с деревом — считаем по ходу, чтобы ответ говорил правду числами.
#[derive(Default)]
struct Tally {
    files: u64,
    dirs: u64,
    links: u64,
    bytes: u64,
    cleared_readonly: u64,
    overwritten: u64,
    skipped_existing: u64,
    skipped_reparse: u64,
    failed_total: u64,
    failed: Vec<Value>,
}

impl Tally {
    fn fail(&mut self, path: &Path, error: impl std::fmt::Display) {
        self.failed_total += 1;
        if self.failed.len() < MAX_FAILURES_REPORTED {
            self.failed
                .push(json!({"path": path, "error": error.to_string()}));
        }
    }

    fn failure_report(&self) -> Value {
        json!({
            "failed_count": self.failed_total,
            "failed": self.failed,
            "failed_truncated": self.failed_total as usize > self.failed.len(),
        })
    }
}

fn kind_of(metadata: &fs::Metadata) -> &'static str {
    let file_type = metadata.file_type();
    if file_type.is_symlink() {
        "reparse"
    } else if file_type.is_dir() {
        "directory"
    } else if file_type.is_file() {
        "file"
    } else {
        "other"
    }
}

/// Точка повторной обработки, которая ведёт в каталог (junction или symlink на папку).
///
/// ⚠ `FileType::is_dir()` для связи всегда false, поэтому без этой проверки junction
/// пришлось бы удалять как файл — а Windows такое не делает и вернула бы отказ в доступе.
fn is_directory_like(file_type: &fs::FileType) -> bool {
    if file_type.is_dir() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::FileTypeExt;
        file_type.is_symlink_dir()
    }
    #[cfg(not(windows))]
    {
        false
    }
}

/// Снять «только чтение» и повторить. Возвращает true, если атрибут действительно сняли.
///
/// ⚠ Живой случай: любой клон гита — файлы в `.git/objects` на Windows лежат read-only.
/// Проверено на этой машине (rustc 1.94): `fs::remove_file` такой файл сносит сам, молча,
/// а `fs::copy` ПОВЕРХ такого файла возвращает «отказано в доступе» (код 5). То есть
/// счётчик `cleared_readonly` почти всегда набивает копирование, а не удаление — и в
/// ответе он значит ровно «сколько раз атрибут снимали МЫ», а не «сколько было read-only».
/// Спрашивать её «снять атрибут?» было бы забором на ровном месте: она сказала «скопируй»,
/// права у неё полные. Наше дело — не спрятать, что атрибут пришлось снять.
fn clear_readonly(path: &Path) -> bool {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    let mut permissions = metadata.permissions();
    if !permissions.readonly() {
        return false;
    }
    permissions.set_readonly(false);
    fs::set_permissions(path, permissions).is_ok()
}

fn remove_entry(path: &Path, as_directory: bool, tally: &mut Tally) -> std::io::Result<()> {
    let once = |path: &Path| {
        if as_directory {
            fs::remove_dir(path)
        } else {
            fs::remove_file(path)
        }
    };
    match once(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
            if clear_readonly(path) {
                let retried = once(path);
                if retried.is_ok() {
                    tally.cleared_readonly += 1;
                }
                retried
            } else {
                Err(error)
            }
        }
        Err(error) => Err(error),
    }
}

fn copy_file_forgiving(source: &Path, target: &Path, tally: &mut Tally) -> std::io::Result<u64> {
    match fs::copy(source, target) {
        Ok(bytes) => Ok(bytes),
        Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
            if clear_readonly(target) {
                let retried = fs::copy(source, target);
                if retried.is_ok() {
                    tally.cleared_readonly += 1;
                }
                retried
            } else {
                Err(error)
            }
        }
        Err(error) => Err(error),
    }
}

fn delete(args: DeleteArgs) -> Result<Value> {
    let mut budget = Budget::new();
    let mut tally = Tally::default();
    let metadata = match fs::symlink_metadata(&args.path) {
        Ok(metadata) => metadata,
        // ⚠ «Нечего удалять» имеет право сказать ТОЛЬКО NotFound. Отказ в доступе или
        // сбойный том — это «не смогла посмотреть», и выдать его за «уже пусто» значит
        // соврать ровно тем способом, ради которого затевалась эта волна.
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(json!({
                "ok": true,
                "path": args.path,
                "existed": false,
                "recursive": args.recursive,
                "removed_files": 0,
                "removed_dirs": 0,
                "removed_links": 0,
                "removed_bytes": 0,
                "cleared_readonly": 0,
                "complete": true,
                "stopped_because": Value::Null,
                "failed_count": 0,
                "failed": [],
                "failed_truncated": false,
                "elapsed_ms": budget.elapsed_ms(),
                // Живой случай: две операции 13–14 июля били по `C:\Users\Egor` и
                // `C:\Users\Егор`, а на диске только `yegor`. Тихое «ок» на такую
                // опечатку — это ложь, из которой не выбраться.
                "summary": format!("nothing deleted: {} does not exist", args.path.display()),
                "ignored_args": ignored_args(&args.extra),
                "limits": tree_limits(),
            }));
        }
        Err(error) => {
            return Err(anyhow::Error::new(error))
                .with_context(|| format!("delete {}", args.path.display()));
        }
    };
    let kind = kind_of(&metadata);
    let file_type = metadata.file_type();
    if file_type.is_symlink() {
        // Саму связь снимаем, в цель не заходим: «удали эту папку» про junction никогда
        // не значит «вычисти чужой каталог на том конце».
        match remove_entry(&args.path, is_directory_like(&file_type), &mut tally) {
            Ok(()) => tally.links += 1,
            Err(error) => tally.fail(&args.path, error),
        }
    } else if file_type.is_dir() {
        if args.recursive {
            delete_tree(&args.path, &mut budget, &mut tally);
        } else {
            let mut entries = fs::read_dir(&args.path)
                .with_context(|| format!("delete {}", args.path.display()))?;
            if entries.next().is_some() {
                anyhow::bail!(
                    "{} is a directory and it is not empty: this call deletes an empty \
                     directory only. Repeat with recursive=true to delete the whole tree.",
                    args.path.display()
                );
            }
            drop(entries);
            match remove_entry(&args.path, true, &mut tally) {
                Ok(()) => tally.dirs += 1,
                Err(error) => tally.fail(&args.path, error),
            }
        }
    } else {
        let size = metadata.len();
        match remove_entry(&args.path, false, &mut tally) {
            Ok(()) => {
                tally.files += 1;
                tally.bytes += size;
            }
            Err(error) => tally.fail(&args.path, error),
        }
    }
    let complete = budget.stopped.is_none() && tally.failed_total == 0;
    let mut summary = format!(
        "deleted {} file(s), {} directory(ies), {} link(s), {} byte(s) under {}",
        tally.files,
        tally.dirs,
        tally.links,
        tally.bytes,
        args.path.display()
    );
    if tally.cleared_readonly > 0 {
        summary.push_str(&format!(
            "; cleared the read-only attribute on {} entry(ies) to do it",
            tally.cleared_readonly
        ));
    }
    if !complete {
        summary = format!(
            "PARTIAL: {summary}; {} entry(ies) could not be removed{}",
            tally.failed_total,
            budget
                .stopped
                .as_deref()
                .map(|reason| format!("; stopped early: {reason}"))
                .unwrap_or_default()
        );
    }
    Ok(json!({
        "ok": complete,
        "path": args.path,
        "existed": true,
        "kind": kind,
        "recursive": args.recursive,
        "removed_files": tally.files,
        "removed_dirs": tally.dirs,
        "removed_links": tally.links,
        "removed_bytes": tally.bytes,
        "cleared_readonly": tally.cleared_readonly,
        "complete": complete,
        "stopped_because": budget.stopped,
        "failed_count": tally.failed_total,
        "failed": tally.failed,
        "failed_truncated": tally.failed_total as usize > tally.failed.len(),
        "elapsed_ms": budget.elapsed_ms(),
        "summary": summary,
        "ignored_args": ignored_args(&args.extra),
        "limits": tree_limits(),
    }))
}

/// Обход снизу вверх, итеративно.
///
/// ⚠ Не рекурсией: в её рабочих деревьях лежат `node_modules` и `target`, на которых
/// рекурсивный обход съедает стек — а падение тела здесь означало бы наполовину снесённое
/// дерево вообще без ответа о том, что успело исчезнуть.
fn delete_tree(root: &Path, budget: &mut Budget, tally: &mut Tally) {
    let mut stack: Vec<(PathBuf, bool)> = vec![(root.to_path_buf(), false)];
    while let Some((path, children_visited)) = stack.pop() {
        if children_visited {
            match remove_entry(&path, true, tally) {
                Ok(()) => tally.dirs += 1,
                Err(error) => tally.fail(&path, error),
            }
            continue;
        }
        if !budget.spend() {
            return;
        }
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) => {
                tally.fail(&path, error);
                continue;
            }
        };
        let file_type = metadata.file_type();
        if file_type.is_symlink() {
            match remove_entry(&path, is_directory_like(&file_type), tally) {
                Ok(()) => tally.links += 1,
                Err(error) => tally.fail(&path, error),
            }
        } else if file_type.is_dir() {
            match fs::read_dir(&path) {
                Ok(entries) => {
                    stack.push((path.clone(), true));
                    for entry in entries {
                        match entry {
                            Ok(entry) => stack.push((entry.path(), false)),
                            Err(error) => tally.fail(&path, error),
                        }
                    }
                }
                Err(error) => tally.fail(&path, error),
            }
        } else {
            let size = metadata.len();
            match remove_entry(&path, false, tally) {
                Ok(()) => {
                    tally.files += 1;
                    tally.bytes += size;
                }
                Err(error) => tally.fail(&path, error),
            }
        }
    }
}

/// Сравнимый ключ пути: канонизируем ближайшего существующего предка и досыпаем хвост.
///
/// Нужен ровно для одного вопроса — «а не внутрь ли себя мы копируем». `canonicalize`
/// целиком не годится: назначения обычно ещё нет на диске.
fn compare_key(path: &Path) -> PathBuf {
    let mut existing = path.to_path_buf();
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(canonical) = fs::canonicalize(&existing) {
            let mut key = canonical;
            for part in tail.iter().rev() {
                key.push(part);
            }
            return normalized_case(&key);
        }
        match existing.file_name() {
            Some(name) => {
                tail.push(name.to_os_string());
                if !existing.pop() {
                    break;
                }
            }
            None => break,
        }
    }
    normalized_case(path)
}

fn normalized_case(path: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        // Windows не различает регистр: `C:\Work` и `c:\work` — один каталог, и копия
        // «внутрь себя» через разный регистр закрутилась бы бесконечно.
        PathBuf::from(path.to_string_lossy().to_lowercase())
    }
    #[cfg(not(windows))]
    {
        path.to_path_buf()
    }
}

fn ensure_distinct(from: &Path, to: &Path) -> Result<()> {
    let from_key = compare_key(from);
    let to_key = compare_key(to);
    if from_key == to_key {
        anyhow::bail!(
            "source and destination are the same path: {}",
            from.display()
        );
    }
    if to_key.starts_with(&from_key) {
        anyhow::bail!(
            "destination {} is inside the source {}: copying a tree into itself never ends",
            to.display(),
            from.display()
        );
    }
    Ok(())
}

/// `mv a.txt C:\dir` — привычка оболочки: назначение-каталог значит «положи внутрь».
/// Мы так и делаем, но обязаны вернуть ИТОГОВЫЙ путь, иначе она не знает, куда легло.
fn resolve_destination(from: &Path, to: &Path) -> Result<(PathBuf, bool)> {
    let into_directory = fs::metadata(to).map(|x| x.is_dir()).unwrap_or(false);
    if into_directory {
        let name = from
            .file_name()
            .with_context(|| format!("source {} has no file name to keep", from.display()))?;
        Ok((to.join(name), true))
    } else {
        Ok((to.to_path_buf(), false))
    }
}

/// Разные тома. Windows отвечает ERROR_NOT_SAME_DEVICE(17), unix — EXDEV(18).
fn is_cross_volume(error: &std::io::Error) -> bool {
    #[cfg(windows)]
    {
        matches!(error.raw_os_error(), Some(17))
    }
    #[cfg(not(windows))]
    {
        matches!(error.raw_os_error(), Some(18))
    }
}

fn move_path(args: MoveArgs) -> Result<Value> {
    let mut budget = Budget::new();
    let mut tally = Tally::default();
    let source = fs::symlink_metadata(&args.from)
        .with_context(|| format!("move source {}", args.from.display()))?;
    let source_kind = kind_of(&source);
    let (destination, resolved_into_directory) = resolve_destination(&args.from, &args.to)?;
    ensure_distinct(&args.from, &destination)?;
    let existing = fs::symlink_metadata(&destination).ok();
    let mut overwritten = false;
    if let Some(existing) = &existing {
        if !args.overwrite {
            anyhow::bail!(
                "destination {} already exists ({}). Repeat with overwrite=true to replace it.",
                destination.display(),
                kind_of(existing)
            );
        }
        if is_directory_like(&existing.file_type()) {
            // Не забор, а отказ угадывать: `overwrite` — про замену файла. Снести дерево
            // под назначением — отдельное намерение, у него есть своё имя (fs.delete
            // recursive=true), и подразумевать его флагом было бы подменой смысла.
            anyhow::bail!(
                "destination {} is an existing directory: overwrite=true replaces a file, \
                 it will not delete a directory tree for you. Delete it first with \
                 fs.delete recursive=true, or move to a path that does not exist.",
                destination.display()
            );
        }
        overwritten = true;
        if source.file_type().is_dir() {
            // rename каталога поверх существующего файла не умеет ни один API: убираем
            // файл заранее. Отдельным шагом — чтобы отказ на нём был назван отказом.
            remove_entry(&destination, false, &mut tally)
                .with_context(|| format!("replace {}", destination.display()))?;
        }
    }
    let bytes_hint = if source.file_type().is_dir() {
        0
    } else {
        source.len()
    };
    let strategy;
    let mut cross_volume = false;
    let mut source_removed = true;
    match fs::rename(&args.from, &destination) {
        Ok(()) => {
            strategy = "rename";
            budget.spend();
            if source.file_type().is_dir() {
                tally.dirs += 1;
            } else {
                tally.files += 1;
                tally.bytes += bytes_hint;
            }
        }
        Err(error) if is_cross_volume(&error) => {
            // ⚠ Тут «мгновенно» превращается в минуты. Молчать об этом нельзя: она
            // планирует по времени, и «переместила» без пояснения означало бы, что
            // копирование 40 ГБ выглядит как сбой связи, а не как честная работа.
            strategy = "copy_and_delete";
            cross_volume = true;
            copy_tree(
                &args.from,
                &destination,
                args.overwrite,
                &mut budget,
                &mut tally,
            );
            if tally.failed_total == 0 && budget.stopped.is_none() {
                let mut removal = Tally::default();
                delete_source_after_copy(&args.from, &source, &mut budget, &mut removal);
                tally.cleared_readonly += removal.cleared_readonly;
                source_removed = removal.failed_total == 0;
                tally.failed_total += removal.failed_total;
                for failure in removal.failed {
                    if tally.failed.len() < MAX_FAILURES_REPORTED {
                        tally.failed.push(failure);
                    }
                }
            } else {
                // Копия не удалась целиком — источник не трогаем. Половина данных на
                // новом месте плюс снесённый оригинал — единственный по-настоящему
                // невозвратный исход, и его мы не устраиваем.
                source_removed = false;
            }
        }
        Err(error) => {
            return Err(anyhow::Error::new(error)).with_context(|| {
                format!(
                    "move {} -> {}",
                    args.from.display(),
                    destination.display()
                )
            });
        }
    }
    let complete = budget.stopped.is_none() && tally.failed_total == 0 && source_removed;
    let mut summary = if cross_volume {
        format!(
            "moved {} to {} ACROSS VOLUMES: this was a copy of {} file(s) ({} byte(s)) \
             followed by deleting the source, not an instant rename",
            args.from.display(),
            destination.display(),
            tally.files,
            tally.bytes
        )
    } else {
        format!(
            "renamed {} to {} in place ({} byte(s))",
            args.from.display(),
            destination.display(),
            tally.bytes
        )
    };
    if overwritten {
        summary.push_str("; an existing file at the destination was replaced");
    }
    if resolved_into_directory {
        summary.push_str("; the destination was an existing directory, so the name was kept");
    }
    if tally.skipped_reparse > 0 {
        summary.push_str(&format!(
            "; {} reparse point(s) (junction/symlink) were not followed and were left behind",
            tally.skipped_reparse
        ));
    }
    if !complete {
        summary = format!(
            "PARTIAL: {summary}; {} failure(s){}{}",
            tally.failed_total,
            if source_removed {
                ""
            } else {
                "; the source is STILL THERE"
            },
            budget
                .stopped
                .as_deref()
                .map(|reason| format!("; stopped early: {reason}"))
                .unwrap_or_default()
        );
    }
    let failures = tally.failure_report();
    Ok(json!({
        "ok": complete,
        "from": args.from,
        "to": destination,
        "requested_to": args.to,
        "destination_resolved_into_directory": resolved_into_directory,
        "kind": source_kind,
        "strategy": strategy,
        "cross_volume": cross_volume,
        "overwritten": overwritten,
        "moved_files": tally.files,
        "moved_dirs": tally.dirs,
        "moved_links": tally.links,
        "bytes": tally.bytes,
        "skipped_reparse": tally.skipped_reparse,
        "cleared_readonly": tally.cleared_readonly,
        "source_removed": source_removed,
        "complete": complete,
        "stopped_because": budget.stopped,
        "failed_count": failures["failed_count"],
        "failed": failures["failed"],
        "failed_truncated": failures["failed_truncated"],
        "elapsed_ms": budget.elapsed_ms(),
        "summary": summary,
        "ignored_args": ignored_args(&args.extra),
        "limits": tree_limits(),
    }))
}

fn delete_source_after_copy(
    source: &Path,
    metadata: &fs::Metadata,
    budget: &mut Budget,
    tally: &mut Tally,
) {
    if metadata.file_type().is_dir() {
        delete_tree(source, budget, tally);
    } else {
        match remove_entry(source, is_directory_like(&metadata.file_type()), tally) {
            Ok(()) => {}
            Err(error) => tally.fail(source, error),
        }
    }
}

fn copy(args: CopyArgs) -> Result<Value> {
    let mut budget = Budget::new();
    let mut tally = Tally::default();
    let source = fs::symlink_metadata(&args.from)
        .with_context(|| format!("copy source {}", args.from.display()))?;
    let source_kind = kind_of(&source);
    if source.file_type().is_dir() && !args.recursive {
        anyhow::bail!(
            "{} is a directory: this call copies one file. Repeat with recursive=true to \
             copy the whole tree.",
            args.from.display()
        );
    }
    let (destination, resolved_into_directory) = resolve_destination(&args.from, &args.to)?;
    ensure_distinct(&args.from, &destination)?;
    let existed = fs::symlink_metadata(&destination).is_ok();
    if existed && !args.overwrite && !source.file_type().is_dir() {
        anyhow::bail!(
            "destination {} already exists. Repeat with overwrite=true to replace it.",
            destination.display()
        );
    }
    copy_tree(
        &args.from,
        &destination,
        args.overwrite,
        &mut budget,
        &mut tally,
    );
    let complete = budget.stopped.is_none() && tally.failed_total == 0;
    let mut summary = format!(
        "copied {} file(s), {} new directory(ies), {} byte(s) from {} to {}",
        tally.files,
        tally.dirs,
        tally.bytes,
        args.from.display(),
        destination.display()
    );
    if tally.overwritten > 0 {
        summary.push_str(&format!(
            "; {} existing file(s) were overwritten",
            tally.overwritten
        ));
    }
    if tally.skipped_existing > 0 {
        summary.push_str(&format!(
            "; {} file(s) already existed and were LEFT AS THEY WERE because overwrite=false",
            tally.skipped_existing
        ));
    }
    if tally.skipped_reparse > 0 {
        summary.push_str(&format!(
            "; {} reparse point(s) (junction/symlink) were not followed and were NOT copied",
            tally.skipped_reparse
        ));
    }
    if resolved_into_directory {
        summary.push_str("; the destination was an existing directory, so the name was kept");
    }
    if !complete {
        summary = format!(
            "PARTIAL: {summary}; {} failure(s){}",
            tally.failed_total,
            budget
                .stopped
                .as_deref()
                .map(|reason| format!("; stopped early: {reason}"))
                .unwrap_or_default()
        );
    }
    let failures = tally.failure_report();
    Ok(json!({
        "ok": complete,
        "from": args.from,
        "to": destination,
        "requested_to": args.to,
        "destination_resolved_into_directory": resolved_into_directory,
        "kind": source_kind,
        "recursive": args.recursive,
        "overwrite": args.overwrite,
        "copied_files": tally.files,
        "created_dirs": tally.dirs,
        "bytes": tally.bytes,
        "overwritten_files": tally.overwritten,
        "skipped_existing": tally.skipped_existing,
        "skipped_reparse": tally.skipped_reparse,
        "cleared_readonly": tally.cleared_readonly,
        "complete": complete,
        "stopped_because": budget.stopped,
        "failed_count": failures["failed_count"],
        "failed": failures["failed"],
        "failed_truncated": failures["failed_truncated"],
        "elapsed_ms": budget.elapsed_ms(),
        "summary": summary,
        "ignored_args": ignored_args(&args.extra),
        "limits": tree_limits(),
    }))
}

fn copy_tree(from: &Path, to: &Path, overwrite: bool, budget: &mut Budget, tally: &mut Tally) {
    let mut stack: Vec<(PathBuf, PathBuf)> = vec![(from.to_path_buf(), to.to_path_buf())];
    while let Some((source, target)) = stack.pop() {
        if !budget.spend() {
            return;
        }
        let metadata = match fs::symlink_metadata(&source) {
            Ok(metadata) => metadata,
            Err(error) => {
                tally.fail(&source, error);
                continue;
            }
        };
        let file_type = metadata.file_type();
        if file_type.is_symlink() {
            // ⚠ Связь не разворачиваем. Скопировать ЦЕЛЬ вместо связи — подмена смысла:
            // на месте ярлыка окажется его содержимое, и она об этом не узнает. Считаем
            // и называем в ответе (`skipped_reparse`), а не делаем вид, что скопировали.
            tally.skipped_reparse += 1;
            continue;
        }
        if file_type.is_dir() {
            let target_existed = fs::metadata(&target).map(|x| x.is_dir()).unwrap_or(false);
            if let Err(error) = fs::create_dir_all(&target) {
                tally.fail(&target, error);
                continue;
            }
            if !target_existed {
                tally.dirs += 1;
            }
            match fs::read_dir(&source) {
                Ok(entries) => {
                    for entry in entries {
                        match entry {
                            Ok(entry) => {
                                stack.push((entry.path(), target.join(entry.file_name())))
                            }
                            Err(error) => tally.fail(&source, error),
                        }
                    }
                }
                Err(error) => tally.fail(&source, error),
            }
        } else {
            let target_existed = fs::symlink_metadata(&target).is_ok();
            if target_existed && !overwrite {
                tally.skipped_existing += 1;
                continue;
            }
            if let Some(parent) = target.parent()
                && !parent.as_os_str().is_empty()
                && let Err(error) = fs::create_dir_all(parent)
            {
                tally.fail(parent, error);
                continue;
            }
            match copy_file_forgiving(&source, &target, tally) {
                Ok(bytes) => {
                    tally.files += 1;
                    tally.bytes += bytes;
                    if target_existed {
                        tally.overwritten += 1;
                    }
                }
                Err(error) => tally.fail(&target, error),
            }
        }
    }
}

fn mkdir(args: MkdirArgs) -> Result<Value> {
    let started = Instant::now();
    if let Ok(metadata) = fs::symlink_metadata(&args.path) {
        if is_directory_like(&metadata.file_type()) {
            return Ok(json!({
                "ok": true,
                "path": args.path,
                "existed": true,
                "parents": args.parents,
                "created": [],
                "created_count": 0,
                "created_truncated": false,
                "complete": true,
                "failed_count": 0,
                "failed": [],
                "elapsed_ms": started.elapsed().as_millis(),
                "summary": format!("{} already exists as a directory; nothing was created", args.path.display()),
                "ignored_args": ignored_args(&args.extra),
                "limits": json!({"max_created_reported": MAX_CREATED_DIRS_REPORTED}),
            }));
        }
        anyhow::bail!(
            "{} already exists and it is a {}, not a directory",
            args.path.display(),
            kind_of(&metadata)
        );
    }
    // Считаем недостающую цепочку САМИ, а не зовём create_dir_all вслепую: только так
    // ответ может сказать, какие именно каталоги родились на диске этим вызовом.
    let mut missing: Vec<PathBuf> = Vec::new();
    let mut cursor = args.path.clone();
    loop {
        if cursor.exists() {
            break;
        }
        missing.push(cursor.clone());
        match cursor.parent() {
            Some(parent) if !parent.as_os_str().is_empty() => cursor = parent.to_path_buf(),
            _ => break,
        }
    }
    missing.reverse();
    if !args.parents && missing.len() > 1 {
        anyhow::bail!(
            "parent directory {} does not exist: this call creates one level. Repeat with \
             parents=true to create the whole chain ({} missing level(s)).",
            missing[missing.len() - 2].display(),
            missing.len() - 1
        );
    }
    let mut created: Vec<PathBuf> = Vec::new();
    let mut tally = Tally::default();
    for path in &missing {
        match fs::create_dir(path) {
            Ok(()) => created.push(path.clone()),
            Err(error) => {
                tally.fail(path, error);
                break;
            }
        }
    }
    let complete = tally.failed_total == 0;
    let reported: Vec<&PathBuf> = created.iter().take(MAX_CREATED_DIRS_REPORTED).collect();
    let summary = if complete {
        format!(
            "created {} directory(ies), deepest is {}",
            created.len(),
            args.path.display()
        )
    } else {
        format!(
            "PARTIAL: created {} directory(ies) and then failed before reaching {}",
            created.len(),
            args.path.display()
        )
    };
    Ok(json!({
        "ok": complete,
        "path": args.path,
        "existed": false,
        "parents": args.parents,
        "created": reported,
        "created_count": created.len(),
        "created_truncated": created.len() > reported.len(),
        "complete": complete,
        "failed_count": tally.failed_total,
        "failed": tally.failed,
        "elapsed_ms": started.elapsed().as_millis(),
        "summary": summary,
        "ignored_args": ignored_args(&args.extra),
        "limits": json!({"max_created_reported": MAX_CREATED_DIRS_REPORTED}),
    }))
}

pub fn sha256_file(path: &Path) -> Result<(String, u64)> {
    let mut file = File::open(path).with_context(|| format!("hash {}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut size = 0u64;
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        size += read as u64;
    }
    Ok((hex::encode(hasher.finalize()), size))
}

pub fn existing_hash(path: &Path) -> Result<Option<String>> {
    if path.exists() {
        Ok(Some(sha256_file(path)?.0))
    } else {
        Ok(None)
    }
}

fn check_expected(actual: Option<&str>, expected: Option<&str>) -> Result<()> {
    if let Some(expected) = expected
        && actual != Some(expected)
    {
        anyhow::bail!(
            "hash conflict: expected {}, actual {}",
            expected,
            actual.unwrap_or("<missing>")
        );
    }
    Ok(())
}

fn create_backup(path: &Path) -> Result<PathBuf> {
    let parent = path.parent().context("file has no parent")?;
    let name = path
        .file_name()
        .map(|x| x.to_string_lossy())
        .unwrap_or_default();
    let backup = parent.join(format!(".{name}.praxis-backup-{}", Uuid::new_v4()));
    fs::copy(path, &backup)?;
    Ok(backup)
}

pub fn atomic_write(path: &Path, content: &[u8]) -> Result<()> {
    let parent = path.parent().context("destination has no parent")?;
    fs::create_dir_all(parent)?;
    let name = path
        .file_name()
        .map(|x| x.to_string_lossy())
        .unwrap_or_default();
    let stage = parent.join(format!(".{name}.praxis-part-{}", Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&stage)?;
    file.write_all(content)?;
    file.sync_all()?;
    drop(file);
    replace_path(&stage, path).with_context(|| {
        format!(
            "commit atomic write {} -> {}",
            stage.display(),
            path.display()
        )
    })?;
    Ok(())
}

#[cfg(windows)]
pub fn replace_path(stage: &Path, destination: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows::Win32::Storage::FileSystem::{
        MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
    };
    use windows::core::PCWSTR;

    let wide = |value: &Path| {
        value
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect::<Vec<u16>>()
    };
    let source = wide(stage);
    let target = wide(destination);
    unsafe {
        MoveFileExW(
            PCWSTR(source.as_ptr()),
            PCWSTR(target.as_ptr()),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )?;
    }
    Ok(())
}

#[cfg(not(windows))]
pub fn replace_path(stage: &Path, destination: &Path) -> Result<()> {
    fs::rename(stage, destination)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expected_hash_prevents_stale_write() {
        let dir = std::env::temp_dir().join(format!("praxis-fs-{}", Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("value.txt");
        fs::write(&path, "old").unwrap();
        let error = dispatch(
            "fs.write_atomic",
            json!({"path": path, "content": "new", "expected_sha256": "deadbeef"}),
        )
        .unwrap_err();
        assert!(error.to_string().contains("hash conflict"));
        assert_eq!(fs::read_to_string(dir.join("value.txt")).unwrap(), "old");
        let _ = fs::remove_dir_all(dir);
    }

    fn scratch(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("praxis-{tag}-{}", Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn text(value: &Value, key: &str) -> String {
        value[key].as_str().unwrap_or_default().to_string()
    }

    /// Живой случай 13–14 июля: две операции били по `C:\Users\Egor` и `C:\Users\Егор`,
    /// а на диске только `yegor`. Тихое «удалила» на такой промах — ложь, из которой
    /// не выбраться: она считает файл снесённым и идёт дальше.
    #[test]
    fn deleting_a_missing_path_says_it_was_missing_and_not_that_it_removed_something() {
        let dir = scratch("delete-missing");
        let value = dispatch(
            "fs.delete",
            json!({"path": dir.join("Egor").join("note.txt")}),
        )
        .unwrap();
        assert_eq!(value["existed"], false);
        assert_eq!(value["removed_files"], 0);
        assert_eq!(value["removed_dirs"], 0);
        assert!(
            text(&value, "summary").contains("does not exist"),
            "{value}"
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn deleting_a_full_directory_without_recursive_names_the_word_that_unlocks_it() {
        let dir = scratch("delete-tree-guard");
        fs::write(dir.join("a.txt"), "a").unwrap();
        let error = dispatch("fs.delete", json!({"path": &dir}))
            .unwrap_err()
            .to_string();
        assert!(error.contains("recursive=true"), "{error}");
        assert!(dir.exists(), "отказ не должен ничего сносить");
        // Граница: пустой каталог уходит и без слова recursive — различение, не забор.
        let empty = dir.join("empty");
        fs::create_dir(&empty).unwrap();
        let value = dispatch("fs.delete", json!({"path": &empty})).unwrap();
        assert_eq!(value["removed_dirs"], 1);
        assert!(!empty.exists());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_recursive_delete_counts_exactly_what_it_removed() {
        let dir = scratch("delete-tree");
        let root = dir.join("root");
        fs::create_dir_all(root.join("sub")).unwrap();
        fs::write(root.join("a.txt"), "abc").unwrap();
        fs::write(root.join("sub").join("b.txt"), "de").unwrap();
        let value = dispatch("fs.delete", json!({"path": &root, "recursive": true})).unwrap();
        assert_eq!(value["complete"], true, "{value}");
        assert_eq!(value["removed_files"], 2, "{value}");
        assert_eq!(value["removed_dirs"], 2, "{value}");
        assert_eq!(value["removed_bytes"], 5, "{value}");
        assert_eq!(value["failed_count"], 0);
        assert!(!root.exists());
        let _ = fs::remove_dir_all(dir);
    }

    /// Любой клон гита: `.git/objects` лежит read-only. Удаление такого файла Windows
    /// делает молча сама, а вот копирование ПОВЕРХ него отвечает «отказано в доступе» —
    /// и вот там мы снимаем атрибут и обязаны об этом сказать. Спрашивать «снять?» —
    /// забор; снять и промолчать — вранье.
    #[cfg(windows)]
    #[test]
    fn a_read_only_destination_is_overwritten_and_the_answer_admits_the_attribute_was_cleared() {
        let dir = scratch("copy-readonly");
        let from = dir.join("s.txt");
        let to = dir.join("object");
        fs::write(&from, "new").unwrap();
        fs::write(&to, "old").unwrap();
        let mut permissions = fs::metadata(&to).unwrap().permissions();
        permissions.set_readonly(true);
        fs::set_permissions(&to, permissions).unwrap();

        let value =
            dispatch("fs.copy", json!({"from": &from, "to": &to, "overwrite": true})).unwrap();
        assert_eq!(value["copied_files"], 1, "{value}");
        assert_eq!(value["overwritten_files"], 1, "{value}");
        assert_eq!(value["cleared_readonly"], 1, "{value}");
        assert_eq!(fs::read_to_string(&to).unwrap(), "new");

        // Удаление read-only файла на Windows проходит без нашей помощи — тогда и
        // счётчик обязан остаться нулём, а не приписывать нам чужую работу.
        let value = dispatch("fs.delete", json!({"path": &to})).unwrap();
        assert_eq!(value["removed_files"], 1, "{value}");
        assert_eq!(value["cleared_readonly"], 0, "{value}");
        assert!(!to.exists());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn unknown_arguments_come_back_by_name_instead_of_silently_defaulting() {
        let dir = scratch("delete-typo");
        let path = dir.join("a.txt");
        fs::write(&path, "a").unwrap();
        let value = dispatch("fs.delete", json!({"path": &path, "recursiv": true})).unwrap();
        assert_eq!(value["ignored_args"], json!(["recursiv"]), "{value}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn every_tree_verb_carries_its_limits_in_the_answer() {
        let dir = scratch("limits");
        fs::write(dir.join("a.txt"), "a").unwrap();
        let answers = vec![
            dispatch("fs.mkdir", json!({"path": dir.join("made")})).unwrap(),
            dispatch(
                "fs.copy",
                json!({"from": dir.join("a.txt"), "to": dir.join("b.txt")}),
            )
            .unwrap(),
            dispatch(
                "fs.move",
                json!({"from": dir.join("b.txt"), "to": dir.join("c.txt")}),
            )
            .unwrap(),
            dispatch("fs.delete", json!({"path": dir.join("c.txt")})).unwrap(),
        ];
        for answer in answers {
            let limits = answer["limits"].as_object().expect("нет блока limits");
            assert!(!limits.is_empty(), "пустой блок пределов: {answer}");
            assert!(
                answer["summary"].as_str().is_some(),
                "нет человеческого итога: {answer}"
            );
        }
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_move_refuses_to_replace_silently_and_replaces_when_told() {
        let dir = scratch("move-overwrite");
        let from = dir.join("a.txt");
        let to = dir.join("b.txt");
        fs::write(&from, "new").unwrap();
        fs::write(&to, "old").unwrap();
        let error = dispatch("fs.move", json!({"from": &from, "to": &to}))
            .unwrap_err()
            .to_string();
        assert!(error.contains("overwrite=true"), "{error}");
        assert_eq!(fs::read_to_string(&to).unwrap(), "old");
        let value =
            dispatch("fs.move", json!({"from": &from, "to": &to, "overwrite": true})).unwrap();
        assert_eq!(value["overwritten"], true, "{value}");
        assert_eq!(value["strategy"], "rename", "{value}");
        assert_eq!(value["cross_volume"], false, "{value}");
        assert_eq!(value["source_removed"], true, "{value}");
        assert_eq!(fs::read_to_string(&to).unwrap(), "new");
        assert!(!from.exists());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_move_into_an_existing_directory_reports_where_it_actually_landed() {
        let dir = scratch("move-into-dir");
        let from = dir.join("a.txt");
        let box_dir = dir.join("box");
        fs::write(&from, "a").unwrap();
        fs::create_dir(&box_dir).unwrap();
        let value = dispatch("fs.move", json!({"from": &from, "to": &box_dir})).unwrap();
        assert_eq!(value["destination_resolved_into_directory"], true, "{value}");
        assert_eq!(value["to"], json!(box_dir.join("a.txt")), "{value}");
        assert!(box_dir.join("a.txt").exists());
        let _ = fs::remove_dir_all(dir);
    }

    /// Перенос через границу тома — это копирование со сносом источника, а не мгновенное
    /// переименование. Второй том в тесте не поднять, поэтому проверяем оба звена, из
    /// которых собран этот путь: распознавание кода ошибки и саму подмену.
    #[test]
    fn the_cross_volume_path_is_recognised_by_its_error_code_and_moves_the_whole_tree() {
        let same_device = if cfg!(windows) { 17 } else { 18 };
        assert!(is_cross_volume(&std::io::Error::from_raw_os_error(
            same_device
        )));
        assert!(!is_cross_volume(&std::io::Error::from_raw_os_error(5)));

        let dir = scratch("move-xvol");
        let from = dir.join("src");
        let to = dir.join("dst");
        fs::create_dir_all(from.join("sub")).unwrap();
        fs::write(from.join("a.txt"), "abc").unwrap();
        fs::write(from.join("sub").join("b.txt"), "de").unwrap();
        let mut budget = Budget::new();
        let mut tally = Tally::default();
        copy_tree(&from, &to, false, &mut budget, &mut tally);
        assert_eq!(tally.files, 2);
        assert_eq!(tally.bytes, 5);
        let metadata = fs::symlink_metadata(&from).unwrap();
        let mut removal = Tally::default();
        delete_source_after_copy(&from, &metadata, &mut budget, &mut removal);
        assert_eq!(removal.failed_total, 0);
        assert!(!from.exists(), "источник обязан исчезнуть после копии");
        assert_eq!(fs::read_to_string(to.join("sub").join("b.txt")).unwrap(), "de");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn copying_a_directory_without_recursive_names_the_word_that_unlocks_it() {
        let dir = scratch("copy-guard");
        let from = dir.join("src");
        fs::create_dir(&from).unwrap();
        let error = dispatch("fs.copy", json!({"from": &from, "to": dir.join("dst")}))
            .unwrap_err()
            .to_string();
        assert!(error.contains("recursive=true"), "{error}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_recursive_copy_says_what_it_kept_and_what_it_overwrote() {
        let dir = scratch("copy-tree");
        let from = dir.join("src");
        let into = dir.join("dst");
        fs::create_dir_all(from.join("sub")).unwrap();
        fs::write(from.join("a.txt"), "one").unwrap();
        fs::write(from.join("sub").join("b.txt"), "two").unwrap();
        fs::create_dir_all(into.join("src")).unwrap();
        fs::write(into.join("src").join("a.txt"), "old").unwrap();

        let kept = dispatch(
            "fs.copy",
            json!({"from": &from, "to": &into, "recursive": true}),
        )
        .unwrap();
        assert_eq!(kept["destination_resolved_into_directory"], true, "{kept}");
        assert_eq!(kept["copied_files"], 1, "{kept}");
        assert_eq!(kept["skipped_existing"], 1, "{kept}");
        assert_eq!(kept["overwritten_files"], 0, "{kept}");
        assert!(
            text(&kept, "summary").contains("LEFT AS THEY WERE"),
            "{kept}"
        );
        assert_eq!(
            fs::read_to_string(into.join("src").join("a.txt")).unwrap(),
            "old"
        );

        let replaced = dispatch(
            "fs.copy",
            json!({"from": &from, "to": &into, "recursive": true, "overwrite": true}),
        )
        .unwrap();
        assert_eq!(replaced["copied_files"], 2, "{replaced}");
        assert_eq!(replaced["overwritten_files"], 2, "{replaced}");
        assert_eq!(replaced["skipped_existing"], 0, "{replaced}");
        assert_eq!(
            fs::read_to_string(into.join("src").join("a.txt")).unwrap(),
            "one"
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn copying_a_tree_into_itself_is_named_instead_of_looping_forever() {
        let dir = scratch("copy-self");
        let from = dir.join("src");
        fs::create_dir(&from).unwrap();
        fs::write(from.join("a.txt"), "a").unwrap();
        let error = dispatch(
            "fs.copy",
            json!({"from": &from, "to": from.join("inner"), "recursive": true}),
        )
        .unwrap_err()
        .to_string();
        assert!(error.contains("inside the source"), "{error}");
        // Файл сам на себя: назначение не каталог, значит путь совпадает буквально —
        // и это отдельная фраза, чтобы она не гадала, что за «внутри себя».
        let same = dispatch(
            "fs.copy",
            json!({"from": from.join("a.txt"), "to": from.join("a.txt")}),
        )
        .unwrap_err()
        .to_string();
        assert!(same.contains("same path"), "{same}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn mkdir_lists_exactly_the_directories_it_created_and_repeats_without_lying() {
        let dir = scratch("mkdir");
        let deep = dir.join("a").join("b").join("c");
        let value = dispatch("fs.mkdir", json!({"path": &deep})).unwrap();
        assert_eq!(value["existed"], false, "{value}");
        assert_eq!(value["created_count"], 3, "{value}");
        assert_eq!(
            value["created"],
            json!([dir.join("a"), dir.join("a").join("b"), &deep]),
            "{value}"
        );
        assert!(deep.is_dir());

        let again = dispatch("fs.mkdir", json!({"path": &deep})).unwrap();
        assert_eq!(again["existed"], true, "{again}");
        assert_eq!(again["created_count"], 0, "{again}");

        let occupied = dir.join("file.txt");
        fs::write(&occupied, "x").unwrap();
        let error = dispatch("fs.mkdir", json!({"path": &occupied}))
            .unwrap_err()
            .to_string();
        assert!(error.contains("not a directory"), "{error}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn mkdir_without_parents_names_the_missing_level_and_still_makes_one() {
        let dir = scratch("mkdir-parents");
        let deep = dir.join("a").join("b");
        let error = dispatch("fs.mkdir", json!({"path": &deep, "parents": false}))
            .unwrap_err()
            .to_string();
        assert!(error.contains("parents=true"), "{error}");
        assert!(!dir.join("a").exists(), "отказ не должен ничего создавать");
        // Граница: один уровень без parents проходит.
        let value = dispatch(
            "fs.mkdir",
            json!({"path": dir.join("a"), "parents": false}),
        )
        .unwrap();
        assert_eq!(value["created_count"], 1, "{value}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn exact_replace_refuses_ambiguous_text() {
        let dir = std::env::temp_dir().join(format!("praxis-replace-{}", Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("value.txt");
        fs::write(&path, "x x").unwrap();
        let error =
            dispatch("fs.replace", json!({"path": path, "old": "x", "new": "y"})).unwrap_err();
        assert!(error.to_string().contains("found 2"));
        let _ = fs::remove_dir_all(dir);
    }
}
