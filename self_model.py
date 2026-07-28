"""Compact, provenance-rich model of Praxis' current self (PASS 24).

The old ``soul/self.md`` is deliberately treated as quarantined legacy evidence.
A prompt read never opens it: it returns a fully provenance-validated
``CURRENT.md`` or no self text at all.  Migration and archival recall are explicit
operations.  They preserve the exact previous bytes in ``soul/self/history`` and
append provenance to ``memory/self/observations.jsonl``.

This module contains storage mechanics only.  It does not decide what Praxis is
or when a revision is warranted; those decisions belong to the server-side
agent/run that calls ``migrate`` or ``revise``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import memory_provenance
from process_liveness import is_process_alive


_LOG = logging.getLogger(__name__)

SCHEMA = "praxis.self.current.v1"
OBSERVATION_SCHEMA = "praxis.self.observation.v1"
MIN_CURRENT_CHARS = 120
MAX_CURRENT_CHARS = 12_000
_META_RE = re.compile(r"^<!--\s*praxis-self-current:\s*(\{.*\})\s*-->\s*$")
_BULLET_RE = re.compile(r"^-\s+_(\d{4}-\d{2}-\d{2})_\s+(.+)$")
_LOCAL_LOCK = threading.RLock()


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clean_refs(values: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for value in values or ():
        ref = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
        if ref and ref not in out:
            out.append(ref[:500])
    return out


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with tmp.open("xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


# ⚠ 26.07. Этот замок держит проекцию её желаний (desires.py:327/360/419/507/615) и
# авторские заметки (authored_notes.py:187). Раньше он воровался по голому mtime через 60с
# БЕЗ пульса — то есть у любого, кто честно работал дольше минуты, — и на выходе безусловно
# удалял ЧУЖОЙ замок, даже если его уже перехватили. Два писателя одновременно = порча,
# и молча. Все пределы ниже названы в тексте отказа — но это только половина закона 2.
# Вторая половина — рельс self_store_lock в rails.py (чужой файл, передано главному):
# пока его нет, эти числа существуют только в момент срабатывания.
SELF_LOCK_TIMEOUT_SEC = _env_float("PRAXIS_SELF_LOCK_TIMEOUT_SEC", 5.0, minimum=0.1)
SELF_LOCK_STALE_SEC = _env_float("PRAXIS_SELF_LOCK_STALE_SEC", 60.0, minimum=1.0)
SELF_LOCK_HEARTBEAT_SEC = _env_float("PRAXIS_SELF_LOCK_HEARTBEAT_SEC", 5.0, minimum=0.05)
# Замок создан, но JSON внутрь ещё не дописан: держателю дают этот вдох, прежде чем
# счесть замок бесхозным.
SELF_LOCK_UNREADABLE_GRACE_SEC = _env_float(
    "PRAXIS_SELF_LOCK_UNREADABLE_GRACE_SEC", 1.0, minimum=0.05)


# ⚠ 27.07, находка адверсария (та же, что в media.py). Возрастной порог опирается на пульс,
# а пульс — нить best-effort: ей могут не дать стартовать. Проба (scratchpad/self_steal.py,
# максимум одновременных держателей = 2) показала, что тогда второй писатель входит внутрь
# ЖИВОЙ транзакции — и проекция её желаний портится молча. /proc отвечает только «есть ли
# такой номер», но не «вышел ли `with`». Точное знание есть только у нас: пока держатель
# внутри, путь лежит здесь, и такой замок не отбирается никогда. Запись снимается в
# finally; протёкший файл в реестре не числится и стареет как чужой.
_HELD_LOCKS: dict[str, float] = {}
_HELD_LOCKS_GUARD = threading.Lock()
# О застрявшем СВОЁМ держателе говорим один раз за эпизод удержания, а не каждые 20мс
# опроса: иначе предупреждение тонет в собственном повторе и перестаёт читаться.
_STUCK_WARNED: dict[str, float] = {}


def _held_here(path: Path) -> float | None:
    """Момент (monotonic), когда ЭТОТ процесс взял замок и всё ещё внутри `with`."""

    with _HELD_LOCKS_GUARD:
        return _HELD_LOCKS.get(os.path.abspath(str(path)))


def _hold_here(path: Path) -> None:
    with _HELD_LOCKS_GUARD:
        _HELD_LOCKS[os.path.abspath(str(path))] = time.monotonic()


def _release_here(path: Path) -> None:
    with _HELD_LOCKS_GUARD:
        _HELD_LOCKS.pop(os.path.abspath(str(path)), None)
        _STUCK_WARNED.pop(os.path.abspath(str(path)), None)


def _first_stuck_report(path: Path, since: float) -> bool:
    """Первое ли это сообщение о том, что ИМЕННО этот эпизод удержания застрял."""

    key = os.path.abspath(str(path))
    with _HELD_LOCKS_GUARD:
        if _STUCK_WARNED.get(key) == since:
            return False
        _STUCK_WARNED[key] = since
        return True


def _lock_token(path: Path) -> str:
    """Токен, лежащий в замке прямо сейчас; '' — прочитать или разобрать не вышло."""

    try:
        record = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return str(record.get("token") or "")
    except (OSError, TypeError, ValueError):
        return ""


def _proc_started_at(pid: int) -> str:
    """Метка рождения процесса: 22-е поле /proc/<pid>/stat. '' — прочитать не вышло.

    Номер процесса тождества не доказывает: в контейнере номера маленькие и
    переиспользуются мгновенно (26.07 новый python после рестарта занял /proc/10 —
    номер мертвеца, державшего замок). Метка рождения перезапуск не переживает.
    Вне Linux честно вернём '' — «не знаю» должно выглядеть как «не знаю» (закон 3).

    ⚠ Живёт в трёх местах (здесь, media.py, forge.py). Настоящий дом — общая
    process_liveness, которая про метку рождения до сих пор не знает."""

    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        return raw.rsplit(")", 1)[1].split()[19]
    except Exception:
        return ""


def _owner_alive(pid: int, started_at: str = "") -> bool:
    """Жив ли ИМЕННО тот процесс, что взял замок, а не однофамилец по номеру."""

    if not is_process_alive(pid):
        return False
    if not started_at:
        return True          # замок старого образца или не-Linux: судим по номеру
    current = _proc_started_at(pid)
    return not current or current == started_at


class _LockHeartbeat:
    """Пульс держателя: пока он работает, mtime замка обновляется.

    Без пульса «протух через 60с» означало бы «отнять у того, кто просто долго пишет».
    С пульсом порог означает ровно то, что сказано в отказе: бездействие."""

    __slots__ = ("_path", "_period", "_token", "_stop", "_thread", "_warned",
                 "started", "lost")

    def __init__(self, path: Path, period: float = SELF_LOCK_HEARTBEAT_SEC, *,
                 token: str = "") -> None:
        self._path = Path(path)
        self._period = max(0.05, float(period))
        self._token = token
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = False
        self.started = False
        self.lost = False

    def start(self) -> "_LockHeartbeat":
        thread = threading.Thread(target=self._run, name="praxis-self-lock-heartbeat", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            # Нитей больше не дают. Уронить здесь исключение значило бы выйти из
            # __enter__, уже создав файл замка и не сняв его, — ровно тот вечный замок,
            # ради которого всё это. Работаем без пульса и говорим об этом.
            _LOG.warning("self store lock heartbeat could not start: the stale threshold "
                         "now measures how long the lock is held, not idleness")
            return self
        self._thread = thread
        self.started = True
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._period):
            # ⚠ 27.07: пульс бил по ПУТИ, а не по своему замку. После перехвата мы освежали
            # ЧУЖОЙ замок — и если бы перехватчик умер жёстко, держали бы его мертвеца
            # свежим до конца своей транзакции.
            if self._token and _lock_token(self._path) != self._token:
                self.lost = True
                _LOG.warning(
                    "self store lock %s is no longer ours: the heartbeat stops rather than "
                    "keep a stranger's lock looking alive; this write may have raced "
                    "another writer", self._path)
                return
            try:
                os.utime(self._path, None)
            except OSError as exc:
                # ⚠ Раньше здесь стоял `return`: пульс умирал НАВСЕГДА и МОЛЧА на первом же
                # ENOENT, и с этой секунды «60с бездействия» означало «60с удержания», а
                # отказ продолжал обещать пульс, которого уже нет.
                if not self._warned:
                    self._warned = True
                    _LOG.warning(
                        "self store lock heartbeat could not touch %s (%s); it keeps trying, "
                        "but until a touch succeeds the stale threshold measures how long "
                        "the lock is held, not idleness", self._path, exc)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)


def _lock_holder_note(path: Path) -> str:
    """Правда о держателе для текста отказа: кто держит и с какого времени (закон 3)."""

    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "holder unknown: the lock disappeared while it was being read"
    try:
        # ⚠ Не «last heartbeat»: пульс — нить best-effort, её может не быть вовсе, и тогда
        # это просто возраст файла. Называть возраст пульсом — враньё (закон 3).
        idle = max(0.0, time.time() - path.stat().st_mtime)
        idle_note = f"untouched for {idle:.1f}s"
    except OSError:
        idle_note = "for how long it has been untouched is unknown"
    try:
        record = json.loads(raw)
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return f"held by an unreadable token {raw[:64]!r}, {idle_note}"
    taken = str(record.get("at") or "unknown time")
    birth = str(record.get("started_at") or "")
    if birth:
        current = _proc_started_at(pid)
        if not current:
            identity = "its birth mark cannot be read from here, so identity is unproven"
        elif current == birth:
            identity = "identity confirmed by birth mark"
        else:
            identity = f"birth mark differs now ({current}) — the number was reused"
        return f"held by pid {pid} since {taken} ({identity}), {idle_note}"
    return (f"held by pid {pid} since {taken}, with no birth mark recorded, so a reused "
            f"process number cannot be told from the real holder, {idle_note}")


class _FileLock:
    """Small cross-platform inter-process lock with stale-owner recovery."""

    def __init__(self, path: Path, *, timeout: float = SELF_LOCK_TIMEOUT_SEC,
                 stale_after: float = SELF_LOCK_STALE_SEC,
                 heartbeat: float = SELF_LOCK_HEARTBEAT_SEC):
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self.heartbeat_period = heartbeat
        self.held = False
        # Замок отобрали, пока мы писали. Тишина об этом — та самая молчаливая порча,
        # ради которой всё и переделано; читается снаружи и попадает в лог.
        self.lost_to_takeover = False
        self._token = ""
        self._heartbeat: _LockHeartbeat | None = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        self.lost_to_takeover = False
        self._token = secrets.token_hex(8)
        payload = json.dumps({
            "pid": os.getpid(),
            # Без метки рождения «жив ли pid» отвечает «да» про постороннего процесса,
            # занявшего освободившийся номер, и замок становится вечным.
            "started_at": _proc_started_at(os.getpid()),
            "token": self._token,
            "at": _utc_now(),
        }).encode("utf-8")
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                # ⚠ 27.07: между O_EXCL и этой строкой замок мог уйти к соседу — он видел
                # созданный, но ещё пустой файл и по грейсу счёл его бесхозным. Молча
                # поехать дальше значило бы писать проекцию желаний вдвоём.
                if _lock_token(self.path) != self._token:
                    _LOG.warning("self store lock %s was taken over in the instant between "
                                 "creating it and confirming it (%s)",
                                 self.path, _lock_holder_note(self.path))
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"self store is busy: {self.path}: the lock we had just created "
                            f"was taken over before we could confirm it — "
                            f"{_lock_holder_note(self.path)}; waited {self.timeout:.1f}s")
                    time.sleep(0.02)
                    continue
                self.held = True
                _hold_here(self.path)
                self._heartbeat = _LockHeartbeat(
                    self.path, self.heartbeat_period, token=self._token).start()
                return self
            except FileExistsError:
                stale, reason = self._holder_is_stale()
                if stale:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    else:
                        _LOG.warning("self store lock taken over: %s", reason)
                        continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"self store is busy: {self.path}: {reason}; "
                        f"waited {self.timeout:.1f}s")
                time.sleep(0.02)

    def _holder_is_stale(self) -> tuple[bool, str]:
        """Протух ли держатель — и ровно та причина с ровно тем числом, что применено.

        ⚠ 27.07. Раньше отсюда возвращался голый флаг, а отказ во ВСЕХ ветках называл
        stale_after: в ветке нечитаемого токена реальный порог — секунда, то есть замок
        отбирали через полсекунды после обещания «ждать до 60с». Теперь вердикт и
        объяснение рождаются в одной точке и разойтись не могут (закон 3)."""

        note = _lock_holder_note(self.path)
        since = _held_here(self.path)
        if since is not None:
            # Наш же процесс внутри `with`. Отобрать = писать проекцию желаний вдвоём.
            held = max(0.0, time.monotonic() - since)
            overdue = ""
            if held > self.stale_after:
                overdue = (f" It is past the {self.stale_after:g}s stale threshold, so its own "
                           f"thread looks stuck — but a stuck thread of ours is a real "
                           f"deadlock, and naming it is better than two writers in one "
                           f"transaction.")
                if _first_stuck_report(self.path, since):
                    _LOG.warning(
                        "self store lock %s is held by this very process for %.1fs, past the "
                        "%ss threshold: our own holding thread is not finishing. The lock is "
                        "NOT taken over — that would mean two writers in one transaction",
                        self.path, held, f"{self.stale_after:g}")
            return False, (f"{note}; the holder is this very process and it is still inside "
                           f"its transaction (holding {held:.1f}s).{overdue} A live "
                           f"in-process holder is never taken over")
        try:
            record = json.loads(self.path.read_text(encoding="utf-8", errors="replace"))
            pid = int(record.get("pid"))
            started_at = str(record.get("started_at") or "")
        except (OSError, TypeError, ValueError):
            grace = SELF_LOCK_UNREADABLE_GRACE_SEC
            try:
                # Держатель мог умереть между O_EXCL и записью JSON. Дать вдох, потом
                # забрать бесхозный замок — иначе он вечен. Порог здесь СВОЙ.
                idle = max(0.0, time.time() - self.path.stat().st_mtime)
            except OSError:
                return False, f"{note}; the lock vanished while it was being judged"
            if idle > grace:
                return True, (f"{note}; its token is unreadable and the file has stood "
                              f"untouched {idle:.1f}s, past the {grace:g}s grace given to a "
                              f"holder that died between creating the lock and writing it")
            return False, (f"{note}; its token is unreadable, so it is taken over after "
                           f"{grace:g}s of standing still — not after {self.stale_after:g}s "
                           f"— and it has stood {idle:.1f}s")
        if not _owner_alive(pid, started_at):
            return True, (f"{note}; the process that took it is not there any more"
                          if started_at else
                          f"{note}; no process carries that number now")
        try:
            # Возрастной порог — второй выход из тупика: держатель формально жив, но
            # поток, взявший замок, умер вместе с задачей.
            idle = max(0.0, time.time() - self.path.stat().st_mtime)
        except OSError:
            return False, f"{note}; the lock vanished while it was being judged"
        if idle > self.stale_after:
            return True, (f"{note}; its process is alive but the lock has stood untouched "
                          f"{idle:.1f}s, past the {self.stale_after:g}s stale threshold")
        return False, (f"{note}; it is taken over only after {self.stale_after:g}s of standing "
                       f"still, and standing still is measured by the holder's heartbeat "
                       f"thread — which is best-effort and may not be running at all")

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.held:
            return
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None:
            heartbeat.stop()
        try:
            # ⚠ Раньше здесь стоял безусловный unlink: выходящий снимал ЧУЖОЙ замок,
            # если его собственный успели отобрать, и следующий писатель входил внутрь
            # чужой транзакции. Свой токен — единственное доказательство, что замок наш.
            try:
                record = json.loads(self.path.read_text(encoding="utf-8", errors="replace"))
                mine = str(record.get("token") or "") == self._token
            except (OSError, TypeError, ValueError):
                mine = False
            if mine:
                self.path.unlink(missing_ok=True)
            else:
                # Замок уже не наш: либо его перехватили, либо он исчез. И то и другое
                # значит, что запись могла идти одновременно с чужой — молчать нельзя.
                self.lost_to_takeover = True
                _LOG.warning(
                    "self store lock was taken over while held (%s): %s — this write may "
                    "have raced another writer", self.path, _lock_holder_note(self.path))
        finally:
            # Снимать запись реестра строго последним: пока она есть, свой же сосед не
            # тронет замок. Протёкший файл в реестре не числится и стареет как чужой.
            _release_here(self.path)
            self.held = False


# Shared durable-file primitives for sibling PASS 24 ledgers.  The aliases keep
# the compact implementation in one place without making callers depend on
# underscore-private names.
FileLock = _FileLock
atomic_write = _atomic_write
clean_refs = _clean_refs
utc_now = _utc_now


@dataclass(frozen=True)
class PromptSelf:
    """The exact self text selected for a prompt and where it came from."""

    text: str
    source: str
    path: str
    sha256: str
    revision: int | None
    provenance: dict[str, Any]


class SelfModel:
    def __init__(self, base: str | Path | None = None):
        self.base = Path(base or os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
        self.legacy_path = self.base / "soul" / "self.md"
        self.current_path = self.base / "soul" / "self" / "CURRENT.md"
        self.history_dir = self.base / "soul" / "self" / "history"
        self.observations_path = self.base / "memory" / "self" / "observations.jsonl"
        self.lock_path = self.base / "memory" / ".state" / "self-model.lock"

    @staticmethod
    def _split_current(raw: str) -> tuple[dict[str, Any], str]:
        lines = raw.replace("\r\n", "\n").splitlines()
        meta: dict[str, Any] = {}
        if lines:
            match = _META_RE.match(lines[0].strip())
            if match:
                try:
                    value = json.loads(match.group(1))
                    if isinstance(value, dict):
                        meta = value
                except (TypeError, ValueError):
                    meta = {}
                lines = lines[1:]
        return meta, "\n".join(lines).lstrip("\n").rstrip() + ("\n" if lines else "")

    def current_prompt_info(self) -> PromptSelf:
        """Read the prompt self without producing any filesystem side effect.

        This is deliberately fail closed.  Missing, corrupt or incompletely
        provenanced ``CURRENT.md`` yields an empty prompt result; neither
        ``soul/self.md`` nor a history snapshot is an availability fallback.
        """
        reason = "current_missing"
        raw_bytes = b""
        try:
            raw_bytes = self.current_path.read_bytes()
            raw = raw_bytes.decode("utf-8")
            meta, body = self._split_current(raw)
            revision_raw = meta.get("revision")
            required_strings = ("created_at", "by", "reason", "source", "source_sha256", "history")
            missing_meta = next(
                (key for key in required_strings if not str(meta.get(key) or "").strip()),
                "",
            )
            source_sha = str(meta.get("source_sha256") or "")
            history_rel = str(meta.get("history") or "").replace("\\", "/")
            history_token = Path(history_rel)
            history_valid = (
                bool(history_rel)
                and not history_token.is_absolute()
                and ".." not in history_token.parts
                and history_token.parent == Path("soul") / "self" / "history"
                and bool(re.fullmatch(r"[0-9]{4}", history_token.stem))
            )
            valid = (
                meta.get("schema") == SCHEMA
                and str(revision_raw).isdigit()
                and MIN_CURRENT_CHARS <= len(body.strip()) <= MAX_CURRENT_CHARS
                and not missing_meta
                and bool(re.fullmatch(r"[0-9a-f]{64}", source_sha))
                and isinstance(meta.get("evidence_refs"), list)
                and history_valid
            )
            if valid:
                try:
                    valid = _sha256((self.base / history_token).read_bytes()) == source_sha
                except OSError:
                    valid = False
            if valid:
                return PromptSelf(
                    text=body,
                    source="current",
                    path=self.current_path.relative_to(self.base).as_posix(),
                    sha256=_sha256(raw_bytes),
                    revision=int(revision_raw),
                    provenance=meta,
                )
            reason = "current_invalid"
        except FileNotFoundError:
            reason = "current_missing"
        except (OSError, UnicodeError):
            reason = "current_unreadable"
        return PromptSelf(
            text="",
            source="missing",
            path=self.current_path.relative_to(self.base).as_posix(),
            sha256=_sha256(raw_bytes) if raw_bytes else "",
            revision=None,
            provenance={
                "current_unavailable": True,
                "reason": reason,
                "legacy_quarantined": True,
            },
        )

    def current_prompt(self) -> str:
        return self.current_prompt_info().text

    def _history_paths(self) -> list[Path]:
        if not self.history_dir.exists():
            return []
        return sorted(
            (p for p in self.history_dir.glob("[0-9][0-9][0-9][0-9].md") if p.stem.isdigit()),
            key=lambda p: int(p.stem),
        )

    def history(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in self._history_paths():
            try:
                data = path.read_bytes()
            except OSError:
                continue
            meta, _ = self._split_current(data.decode("utf-8", errors="replace"))
            out.append({
                "version": int(path.stem),
                "path": path.relative_to(self.base).as_posix(),
                "sha256": _sha256(data),
                "bytes": len(data),
                "provenance": meta,
            })
        return out

    def _append_observation_locked(self, event: dict[str, Any]) -> dict[str, Any]:
        self.observations_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema": OBSERVATION_SCHEMA,
            "event_id": f"selfevt-{_dt.datetime.now(_dt.timezone.utc):%Y%m%dT%H%M%S}-{secrets.token_hex(5)}",
            "ts": _utc_now(),
            **event,
        }
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self._repair_torn_jsonl_locked(self.observations_path)
        fd = os.open(str(self.observations_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        return record

    @staticmethod
    def _repair_torn_jsonl_locked(path: Path) -> None:
        """Discard only an unterminated crash tail before the next durable append."""
        try:
            data = path.read_bytes()
        except OSError:
            return
        if not data or data.endswith(b"\n"):
            return
        boundary = data.rfind(b"\n") + 1
        with path.open("r+b") as fh:
            fh.truncate(boundary)
            fh.flush()
            os.fsync(fh.fileno())

    def record_observation(
        self,
        text: str,
        *,
        source: str,
        evidence_refs: Iterable[Any] = (),
        run_id: str = "",
        kind: str = "observation",
        meta: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        """Append evidence about self; never revise ``CURRENT.md`` implicitly.

        Diary-derived observations remain available as episodic evidence, but
        carry a machine-readable ``normative_eligible=false`` marker.  Automatic
        distillation must respect that marker as well as re-checking old rows by
        source/ref, so legacy data cannot bypass the boundary.
        """
        message = str(text or "").strip()
        origin = str(source or "").strip()
        if not message:
            raise ValueError("self observation text is required")
        if not origin:
            raise ValueError("self observation source is required")
        refs = _clean_refs(evidence_refs)
        metadata = dict(meta or {})
        metadata["normative_eligible"] = (
            metadata.get("normative_eligible") is not False
            and not bool(memory_provenance.untrusted_normative_inputs(source=origin, refs=refs))
        )
        event = {
            "kind": str(kind or "observation")[:60],
            "text": message[:4_000],
            "source": origin[:300],
            "evidence_refs": refs,
            "run_id": str(run_id or "")[:200],
            "meta": metadata,
            "dedupe_key": str(dedupe_key or "")[:300],
        }
        with _LOCAL_LOCK, _FileLock(self.lock_path):
            if event["dedupe_key"]:
                try:
                    lines = self.observations_path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                for line in reversed(lines):
                    try:
                        previous = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if (isinstance(previous, dict)
                            and previous.get("dedupe_key") == event["dedupe_key"]):
                        return {**previous, "deduplicated": True}
            return self._append_observation_locked(event)

    @staticmethod
    def _deterministic_compact(legacy: str) -> str:
        """Bounded evidence extraction used only when no authored compact is supplied."""
        conclusions: list[tuple[str, str]] = []
        for line in legacy.replace("\r\n", "\n").splitlines():
            match = _BULLET_RE.match(line.strip())
            if not match:
                continue
            date, body = match.groups()
            conclusion = body.rsplit("Вывод:", 1)[-1].strip() if "Вывод:" in body else body.strip()
            conclusion = re.sub(r"\s+", " ", conclusion)[:500]
            if conclusion and conclusion not in {x[1] for x in conclusions}:
                conclusions.append((date, conclusion))
        stable = conclusions[:4]
        recent = [item for item in conclusions[-5:] if item not in stable]
        lines = [
            "# Кто я сейчас",
            "",
            "Это компактная актуальная модель, детерминированно извлечённая из прежнего self; "
            "исходные наблюдения сохранены в истории.",
        ]
        if stable:
            lines += ["", "## Устойчивое"] + [f"- {text} _(источник: {date})_" for date, text in stable]
        if recent:
            lines += ["", "## Живые изменения"] + [f"- {text} _(источник: {date})_" for date, text in recent]
        if not conclusions:
            excerpt = re.sub(r"\s+", " ", legacy).strip()[:1_500]
            lines += ["", "## Сохранённое основание", excerpt or "Актуальная модель ещё не сформирована."]
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _validate_compact(text: str) -> str:
        body = str(text or "").replace("\r\n", "\n").strip()
        if len(body) < MIN_CURRENT_CHARS:
            raise ValueError(f"CURRENT is too short (<{MIN_CURRENT_CHARS} chars)")
        if len(body) > MAX_CURRENT_CHARS:
            raise ValueError(f"CURRENT is too large (>{MAX_CURRENT_CHARS} chars)")
        return body + "\n"

    @staticmethod
    def _current_document(body: str, meta: dict[str, Any]) -> bytes:
        marker = "<!-- praxis-self-current: " + json.dumps(
            meta, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ) + " -->\n"
        return (marker + body.lstrip("\n")).encode("utf-8")

    def migrate(
        self,
        *,
        reason: str,
        compact_text: str | None = None,
        evidence_refs: Iterable[Any] = (),
        run_id: str = "",
        by: str = "praxis",
        confidence: str = "inferred",
        trigger: str = "",
    ) -> dict[str, Any]:
        """Explicitly establish ``CURRENT.md`` while preserving legacy bytes.

        ``soul/self.md`` is read but never written.  Repeated migration is
        idempotent once a usable CURRENT exists.
        """
        why = str(reason or "").strip()
        if not why:
            return {"ok": False, "error": "migration reason is required"}
        refs = _clean_refs(evidence_refs)
        try:
            memory_provenance.require_normative_provenance(refs=refs)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        with _LOCAL_LOCK, _FileLock(self.lock_path):
            existing = self.current_prompt_info()
            if existing.source == "current":
                return {
                    "ok": True,
                    "migrated": False,
                    "reason": "CURRENT already exists",
                    "current": existing.path,
                    "sha256": existing.sha256,
                }
            if self.current_path.exists():
                return {
                    "ok": False,
                    "error": "CURRENT exists but is invalid; recover it explicitly before migration",
                }
            try:
                legacy_bytes = self.legacy_path.read_bytes()
                legacy = legacy_bytes.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                return {"ok": False, "error": f"legacy self is unavailable: {type(exc).__name__}"}
            if not legacy.strip():
                return {"ok": False, "error": "legacy self is empty"}

            body = self._validate_compact(
                compact_text if compact_text is not None else self._deterministic_compact(legacy)
            )
            history_path = self.history_dir / "0000.md"
            if history_path.exists():
                if history_path.read_bytes() != legacy_bytes:
                    return {"ok": False, "error": "history/0000.md exists with different legacy bytes"}
            else:
                _atomic_write(history_path, legacy_bytes)

            legacy_sha = _sha256(legacy_bytes)
            meta = {
                "schema": SCHEMA,
                "revision": 0,
                "created_at": _utc_now(),
                "by": str(by or "praxis")[:100],
                "reason": why[:500],
                "source": self.legacy_path.relative_to(self.base).as_posix(),
                "source_sha256": legacy_sha,
                "history": history_path.relative_to(self.base).as_posix(),
                "evidence_refs": refs,
                "run_id": str(run_id or "")[:200],
                "derivation": "authored" if compact_text is not None else "deterministic_extraction",
                "confidence": str(confidence or "inferred")[:40],
                "trigger": str(trigger or by or "praxis")[:100],
            }
            current_bytes = self._current_document(body, meta)
            _atomic_write(self.current_path, current_bytes)
            event = self._append_observation_locked({
                "kind": "migration",
                "text": why[:4_000],
                "source": self.legacy_path.relative_to(self.base).as_posix(),
                "evidence_refs": refs,
                "run_id": str(run_id or "")[:200],
                "meta": {
                    "revision": 0,
                    "legacy_sha256": legacy_sha,
                    "current_sha256": _sha256(current_bytes),
                    "history": history_path.relative_to(self.base).as_posix(),
                    "legacy_untouched": True,
                    "confidence": str(confidence or "inferred")[:40],
                    "trigger": str(trigger or by or "praxis")[:100],
                },
            })
            return {
                "ok": True,
                "migrated": True,
                "revision": 0,
                "current": self.current_path.relative_to(self.base).as_posix(),
                "history": history_path.relative_to(self.base).as_posix(),
                "legacy_sha256": legacy_sha,
                "current_sha256": _sha256(current_bytes),
                "event_id": event["event_id"],
            }

    def revise(
        self,
        new_text: str,
        *,
        reason: str,
        evidence_refs: Iterable[Any] = (),
        run_id: str = "",
        by: str = "praxis",
        confidence: str = "inferred",
        trigger: str = "",
        restored_version: int | None = None,
    ) -> dict[str, Any]:
        """Replace CURRENT atomically and archive its exact previous bytes.

        ``restored_version`` is reserved for :meth:`rollback`.  Keeping it in
        both CURRENT provenance and the append-only event makes a restore
        distinguishable from an ordinary authored revision after a restart.
        """
        why = str(reason or "").strip()
        if not why:
            return {"ok": False, "error": "revision reason is required"}
        try:
            body = self._validate_compact(new_text)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        refs = _clean_refs(evidence_refs)
        try:
            memory_provenance.require_normative_provenance(source=trigger, refs=refs)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        with _LOCAL_LOCK, _FileLock(self.lock_path):
            try:
                old_bytes = self.current_path.read_bytes()
            except OSError:
                return {"ok": False, "error": "CURRENT does not exist; call migrate explicitly first"}
            old_meta, old_body = self._split_current(old_bytes.decode("utf-8", errors="strict"))
            revision_raw = old_meta.get("revision")
            if (old_meta.get("schema") != SCHEMA or not str(revision_raw).isdigit()
                    or not MIN_CURRENT_CHARS <= len(old_body.strip()) <= MAX_CURRENT_CHARS):
                return {
                    "ok": False,
                    "error": "CURRENT is unprovenanced or outside the compact bounds; "
                             "repair/recover it before revision",
                }
            if body.strip() == old_body.strip():
                return {"ok": False, "error": "CURRENT text did not change"}

            versions = self._history_paths()
            next_version = (int(versions[-1].stem) + 1) if versions else 0
            archive_path = self.history_dir / f"{next_version:04d}.md"
            if archive_path.exists():
                return {"ok": False, "error": f"history collision at {archive_path.name}"}
            _atomic_write(archive_path, old_bytes)

            old_sha = _sha256(old_bytes)
            revision = max(next_version, int(old_meta.get("revision") or 0) + 1)
            meta = {
                "schema": SCHEMA,
                "revision": revision,
                "created_at": _utc_now(),
                "by": str(by or "praxis")[:100],
                "reason": why[:500],
                "source": self.current_path.relative_to(self.base).as_posix(),
                "source_sha256": old_sha,
                "history": archive_path.relative_to(self.base).as_posix(),
                "evidence_refs": refs,
                "run_id": str(run_id or "")[:200],
                "confidence": str(confidence or "inferred")[:40],
                "trigger": str(trigger or by or "praxis")[:100],
            }
            if restored_version is not None:
                meta["operation"] = "rollback"
                meta["restored_version"] = int(restored_version)
            current_bytes = self._current_document(body, meta)
            _atomic_write(self.current_path, current_bytes)
            event_meta = {
                "revision": revision,
                "previous_sha256": old_sha,
                "current_sha256": _sha256(current_bytes),
                "history": archive_path.relative_to(self.base).as_posix(),
                "confidence": str(confidence or "inferred")[:40],
                "trigger": str(trigger or by or "praxis")[:100],
            }
            if restored_version is not None:
                event_meta.update({
                    "operation": "rollback",
                    "restored_version": int(restored_version),
                })
            event = self._append_observation_locked({
                "kind": "rollback" if restored_version is not None else "revision",
                "text": why[:4_000],
                "source": str(by or "praxis")[:300],
                "evidence_refs": refs,
                "run_id": str(run_id or "")[:200],
                "meta": event_meta,
            })
            return {
                "ok": True,
                "revision": revision,
                "current": self.current_path.relative_to(self.base).as_posix(),
                "history": archive_path.relative_to(self.base).as_posix(),
                "previous_sha256": old_sha,
                "current_sha256": _sha256(current_bytes),
                "event_id": event["event_id"],
            }

    def rollback(
        self,
        version: int,
        *,
        reason: str,
        evidence_refs: Iterable[Any] = (),
        run_id: str = "",
        by: str = "praxis",
    ) -> dict[str, Any]:
        """Re-apply an archived compact version as a new revision.

        History is never moved or rewritten.  ``0000.md`` is the exact legacy
        corpus and may intentionally exceed the compact prompt bound; in that
        case it remains recall evidence rather than becoming CURRENT again.
        """
        try:
            number = int(version)
        except (TypeError, ValueError):
            return {"ok": False, "error": "history version must be an integer"}
        target = self.history_dir / f"{number:04d}.md"
        try:
            raw = target.read_bytes()
            _meta, body = self._split_current(raw.decode("utf-8", errors="strict"))
        except OSError:
            return {"ok": False, "error": f"history version {number} does not exist"}
        except UnicodeError:
            return {"ok": False, "error": f"history version {number} is not UTF-8"}
        refs = [*clean_refs(evidence_refs), target.relative_to(self.base).as_posix()]
        result = self.revise(
            body,
            reason=f"rollback to history/{number:04d}: {str(reason or '').strip()}",
            evidence_refs=refs,
            run_id=run_id,
            by=by,
            confidence="observed",
            trigger="rollback",
            restored_version=number,
        )
        if result.get("ok"):
            result["restored_version"] = number
        return result


def _store(base: str | Path | None = None) -> SelfModel:
    return SelfModel(base)


def current_prompt(base: str | Path | None = None) -> str:
    return _store(base).current_prompt()


def current_prompt_info(base: str | Path | None = None) -> PromptSelf:
    return _store(base).current_prompt_info()


def migrate(*, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _store(base).migrate(**kwargs)


def revise(new_text: str, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _store(base).revise(new_text, **kwargs)


def rollback(version: int, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _store(base).rollback(version, **kwargs)


def record_observation(text: str, *, base: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
    return _store(base).record_observation(text, **kwargs)


def history(base: str | Path | None = None) -> list[dict[str, Any]]:
    return _store(base).history()
