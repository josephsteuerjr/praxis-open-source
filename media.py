"""Typed, scope-isolated media storage for Praxis.

This module deliberately knows nothing about Telegram, an LLM provider, or a
delivery client.  It owns the boundary between untrusted media bytes and the
rest of the application:

* inbound rich media is limited to photos/audio, while guarded outbound delivery
  also accepts ordinary documents;
* the MIME type is derived from file magic, never from a filename/header;
* every stored path is bound to one privacy scope and one chat;
* per-kind and total-size caps are enforced before an object is exposed;
* outbound objects are validated and queued, but never sent here;
* stale spool files can be removed without following symlinks.

Typical integration::

    spool = MediaSpool()
    ref = spool.ingest_bytes(
        downloaded,
        kind="photo",
        filename="telegram.jpg",
        chat_id=chat_id,
        message_id=message_id,
        scope=scope,
        caption=caption,
    )
    turn = spool.envelope(text=caption, media=(ref,))

    outbound = spool.queue_outbound(
        generated_path,
        kind="audio",
        chat_id=chat_id,
        message_id=message_id,
        scope=scope,
        caption="",
    )
    # A caller may claim ``outbound`` and perform the actual send, then must
    # acknowledge its acceptance receipt with ``spool.discard(queue_id)``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from process_liveness import is_process_alive


_LOG = logging.getLogger(__name__)

MediaKind = Literal["photo", "audio", "document"]
ChatId = str | int
MessageId = str | int | None

MIB = 1024 * 1024
CAPTION_MAX_CHARS = 1024
ALLOWED_SCOPES = frozenset({"owner", "family", "known", "unknown", "group"})
ALLOWED_KINDS = frozenset({"photo", "audio", "document"})


def _env_mb(name: str, default: float) -> int:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(1, int(value * MIB))


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


PHOTO_MAX_BYTES = _env_mb("PRAXIS_MEDIA_PHOTO_MB", 20)
AUDIO_MAX_BYTES = _env_mb("PRAXIS_MEDIA_AUDIO_MB", 50)
DOCUMENT_MAX_BYTES = _env_mb("PRAXIS_MEDIA_DOCUMENT_MB", 250)
SPOOL_MAX_BYTES = _env_mb("PRAXIS_MEDIA_TOTAL_MB", 500)
MEDIA_TTL_SECONDS = _env_int("PRAXIS_MEDIA_TTL_HOURS", 24) * 3600
OUTBOUND_QUEUE_MAX = _env_int("PRAXIS_MEDIA_QUEUE_MAX", 64)
TURN_MEDIA_MAX = _env_int("PRAXIS_MEDIA_TURN_MAX", 10)
OUTBOX_RESULT_TTL_SECONDS = (
    _env_int("PRAXIS_MEDIA_OUTBOX_RESULT_DAYS", 30) * 24 * 3600
)
OUTBOX_RESULT_MAX = _env_int("PRAXIS_MEDIA_OUTBOX_RESULT_MAX", 5000)

# ⚠ 26.07, тот же корень, что запер задачу форжа под замком. В контейнере номера
# процессов маленькие и переиспользуются мгновенно: после рестарта praxis новый python
# занял /proc/10 — номер мертвеца, державшего замок. Проверка «есть процесс с таким
# номером» отвечала «жив» про совершенно постороннего, и замок леджера становился вечным:
# каждая операция с исходящими медиа падала бы «media outbox ledger is busy» до рестарта —
# то есть она молча переставала бы отправлять голос и картинки.
# Все пределы ниже названы в тексте отказа — но это только половина закона 2. Вторая
# половина — рельс media_outbox_ledger_lock в rails.py (чужой файл, передано главному):
# пока его нет, эти числа существуют только в момент срабатывания.
LEDGER_ABANDONED_SEC = _env_float("PRAXIS_MEDIA_LEDGER_ABANDONED_SEC", 60.0, minimum=1.0)
LEDGER_HEARTBEAT_SEC = _env_float("PRAXIS_MEDIA_LEDGER_HEARTBEAT_SEC", 5.0, minimum=0.05)
# Замок только что создан, но токен ещё не дописан: держателю дают этот вдох, прежде чем
# счесть замок бесхозным. Порог заведомо мал против десятисекундного окна ожидания.
LEDGER_UNREADABLE_GRACE_SEC = _env_float(
    "PRAXIS_MEDIA_LEDGER_UNREADABLE_GRACE_SEC", 1.0, minimum=0.05)


# ⚠ 27.07, находка адверсария. В контейнере praxis один процесс (pid 10, mtproto_runner),
# но в нём ДВА независимых MediaSpool с разными self._lock — agent.py:367 и
# mtproto_runner.py:286. Значит файловый замок арбитрирует не только процессы, но и потоки
# ВНУТРИ одного процесса. Возрастной порог опирается на пульс, а пульс — нить best-effort:
# ей могут не дать стартовать. Проба (scratchpad/self_steal.py, максимум одновременных
# держателей = 2) показала, что тогда сосед входит внутрь ЖИВОЙ транзакции и оба пишут
# .ledger/*.json — потерянные исходящие медиа, и молча.
# /proc про своих же не врёт, но и не знает, вышел ли `with`. Единственное точное знание —
# у нас самих: пока держатель внутри, путь лежит здесь. У такого замок не отбирается
# никогда, а отказ говорит почему. Запись снимается в finally, поэтому вечным это не
# станет; протёкший (не снятый) файл в реестре не числится и стареет как чужой.
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
    """Токен, лежащий в замке прямо сейчас; '' — прочитать не вышло."""

    try:
        return path.read_text(encoding="ascii", errors="ignore")
    except OSError:
        return ""


def _proc_started_at(pid: int) -> str:
    """Метка рождения процесса: 22-е поле /proc/<pid>/stat. '' — прочитать не вышло.

    Это единственное, чего не переживает перезапуск. Два процесса с одним номером
    почти наверняка родились в разные тики, а после рестарта контейнера — гарантированно.
    Вне Linux (/proc нет) честно вернём '' и будем судить по одному номеру, как раньше:
    соврать «не знаю» под видом факта нельзя (закон 3).

    ⚠ Живёт в трёх местах (здесь, self_model.py, forge.py). Настоящий дом — общая
    process_liveness.is_process_alive, которая про метку рождения до сих пор не знает."""

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
        return True          # токен старого образца или не-Linux: судим по номеру
    current = _proc_started_at(pid)
    return not current or current == started_at


class _LockHeartbeat:
    """Пульс держателя: пока он работает, mtime замка обновляется.

    Без пульса «возрастной порог» означал бы «отнять у того, кто просто долго работает».
    С пульсом порог означает ровно то, что сказано в отказе: бездействие. Молчащий mtime —
    это поток, умерший вместе с задачей, а не длинная честная транзакция."""

    __slots__ = ("_path", "_period", "_token", "_stop", "_thread", "_warned",
                 "started", "lost")

    def __init__(self, path: Path, period: float = LEDGER_HEARTBEAT_SEC, *,
                 token: str = "") -> None:
        self._path = Path(path)
        self._period = max(0.05, float(period))
        self._token = token
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = False
        self.started = False
        # Замок увели, пока мы его держали. Наружу — чтобы про это не молчали.
        self.lost = False

    def start(self) -> "_LockHeartbeat":
        thread = threading.Thread(target=self._run, name="praxis-lock-heartbeat", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            # Нитей больше не дают. Уронить здесь исключение значило бы выйти из
            # _ledger_guard, уже создав файл замка и не сняв его, — ровно тот вечный
            # замок, ради которого всё это. Работаем без пульса и говорим об этом.
            _LOG.warning("media outbox ledger heartbeat could not start: the abandoned "
                         "threshold now measures how long the lock is held, not idleness")
            return self
        self._thread = thread
        self.started = True
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._period):
            # ⚠ 27.07: пульс бил по ПУТИ, а не по своему замку. Если нас перехватили, а
            # перехватчик умер жёстко, мы бы держали его мертвеца свежим до конца своей
            # транзакции — ровно тот вечный замок, ради которого всё делалось.
            if self._token and _lock_token(self._path) != self._token:
                self.lost = True
                _LOG.warning(
                    "media outbox ledger lock %s is no longer ours: the heartbeat stops "
                    "rather than keep a stranger's lock looking alive; this ledger write "
                    "may have raced another writer", self._path)
                return
            try:
                os.utime(self._path, None)
            except OSError as exc:
                # ⚠ Раньше здесь стоял `return`: пульс умирал НАВСЕГДА и МОЛЧА на первом же
                # ENOENT (контендер на миг снёс замок, файл потом пересоздан). С этой
                # секунды «60с бездействия» означало «60с удержания», а отказ продолжал
                # обещать пульс, которого нет. Теперь — громко и с новой попыткой.
                if not self._warned:
                    self._warned = True
                    _LOG.warning(
                        "media outbox ledger heartbeat could not touch %s (%s); it keeps "
                        "trying, but until a touch succeeds the abandoned threshold "
                        "measures how long the lock is held, not idleness", self._path, exc)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)


class MediaError(ValueError):
    """Base class for rejected media operations."""


class MediaValidationError(MediaError):
    """The object or its on-disk representation is inconsistent."""


class UnsupportedMediaError(MediaValidationError):
    """The bytes do not match the declared guarded-delivery kind."""


class MediaTooLargeError(MediaValidationError):
    """A per-file or whole-spool cap would be exceeded."""


class MediaSecurityError(MediaValidationError):
    """A path, scope, or chat binding crossed a security boundary."""


class MediaQueueFullError(MediaValidationError):
    """The bounded outbound queue has no free slot."""


@dataclass(frozen=True, slots=True)
class MediaRef:
    """Immutable reference to one validated, locally spooled media object."""

    kind: MediaKind
    path: Path
    mime: str
    size: int
    chat_id: ChatId
    message_id: MessageId
    scope: str
    caption: str = ""
    sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "caption", str(self.caption or ""))
        object.__setattr__(self, "sha256", str(self.sha256 or "").lower())


@dataclass(frozen=True, slots=True)
class OutboundMedia:
    """A validated media delivery request, with no delivery side effects."""

    kind: MediaKind
    path: Path
    mime: str
    size: int
    target_chat_id: ChatId
    scope: str
    caption: str = ""
    reply_to_message_id: MessageId = None
    voice_note: bool = False
    queue_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = ""
    sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "caption", str(self.caption or ""))
        object.__setattr__(self, "queue_id", str(self.queue_id or ""))
        object.__setattr__(self, "run_id", str(self.run_id or ""))
        object.__setattr__(self, "sha256", str(self.sha256 or "").lower())

    @property
    def chat_id(self) -> ChatId:
        """Short read-only alias useful at the sender boundary."""

        return self.target_chat_id

    @property
    def message_id(self) -> MessageId:
        """Short read-only alias for the Telegram reply target."""

        return self.reply_to_message_id

    @classmethod
    def from_ref(
        cls,
        ref: MediaRef,
        *,
        caption: str | None = None,
        reply_to_message_id: MessageId = None,
        voice_note: bool = False,
    ) -> "OutboundMedia":
        return cls(
            kind=ref.kind,
            path=ref.path,
            mime=ref.mime,
            size=ref.size,
            target_chat_id=ref.chat_id,
            scope=ref.scope,
            caption=ref.caption if caption is None else caption,
            reply_to_message_id=(
                ref.message_id if reply_to_message_id is None else reply_to_message_id
            ),
            voice_note=voice_note,
            sha256=ref.sha256,
        )

    def as_ref(self) -> MediaRef:
        return MediaRef(
            kind=self.kind,
            path=self.path,
            mime=self.mime,
            size=self.size,
            chat_id=self.target_chat_id,
            message_id=self.reply_to_message_id,
            scope=self.scope,
            caption=self.caption,
            sha256=self.sha256,
        )


@dataclass(frozen=True, slots=True)
class TurnEnvelope:
    """Guardable response text/outbound media, plus optional inbound context."""

    text: str = ""
    outbound: tuple[OutboundMedia, ...] = ()
    media: tuple[MediaRef, ...] = ()
    # A live runner may retain inbound refs for a later attempt when the model
    # was unavailable.  This is transport state, not content and never sends.
    retry_media: bool = False
    # A durable run owns the turn but stopped at a safe checkpoint.  The live
    # trigger must be consumed without pretending that Praxis chose silence;
    # run recovery/resume will continue from the persisted checkpoint.
    deferred: bool = False
    # The voice path failed rather than choosing silence.  Transport consumes
    # the trigger without creating a false delivery/silence receipt; the
    # durable run remains the evidence and recovery/control surface.
    failed: bool = False
    boundary: bool = False
    # Durable server-side run which produced this envelope.  Transport uses it only to
    # append delivery receipts; it grants no authority and is never sent to Telegram.
    run_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "outbound", tuple(self.outbound or ()))
        object.__setattr__(self, "media", tuple(self.media or ()))
        object.__setattr__(self, "deferred", bool(self.deferred))
        object.__setattr__(self, "failed", bool(self.failed))
        object.__setattr__(self, "retry_media", bool(self.retry_media))
        object.__setattr__(self, "boundary", bool(self.boundary))
        object.__setattr__(self, "run_id", str(self.run_id or ""))

    @property
    def has_media(self) -> bool:
        return bool(self.media)

    @property
    def has_outbound(self) -> bool:
        return bool(self.outbound)


_MIME_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/avif": ".avif",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
}

_WINDOWS_RESERVED = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE
)
_QUEUE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SPOOL_NAME_PREFIX = re.compile(r"^msg-[0-9a-f]{10}-[0-9a-f]{12}-")
_OUTBOX_SCHEMA = "praxis.media.outbox.v1"
_OUTBOX_MAINTENANCE_SCHEMA = "praxis.media.outbox-maintenance.v1"
_TERMINAL_OUTBOX_STATES = frozenset({"delivered", "expired", "failed"})
_OUTBOX_STATES = frozenset({"pending", *_TERMINAL_OUTBOX_STATES})


def _fsync_directory(path: Path) -> None:
    """Best-effort durability for an atomic rename on POSIX.

    Windows does not allow opening a directory through ``os.open`` in the same
    way.  The file itself is always flushed; this extra barrier is used where
    the platform supports it.
    """

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    """Replace one JSON record atomically after flushing its bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True) + "\n").encode("utf-8")
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(temp), flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short outbox-ledger write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _fsync_directory(path.parent)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def safe_basename(name: str | os.PathLike[str] | None, *, fallback: str = "media") -> str:
    """Return a portable basename; directory components can never survive."""

    raw = unicodedata.normalize("NFKC", str(name or ""))
    raw = raw.replace("\x00", "").replace("\\", "/").rsplit("/", 1)[-1]
    raw = raw.strip().strip(".")
    raw = re.sub(r"[^\w.\- ]+", "_", raw, flags=re.UNICODE)
    raw = re.sub(r"\s+", "_", raw).strip(" ._")
    if not raw:
        raw = re.sub(r"[^\w.\-]+", "_", str(fallback or "media"), flags=re.UNICODE)
        raw = raw.strip(" ._") or "media"
    if _WINDOWS_RESERVED.match(raw):
        raw = "_" + raw
    if len(raw) > 96:
        suffix = Path(raw).suffix[:16]
        keep = max(1, 96 - len(suffix))
        raw = raw[:keep].rstrip(" ._") + suffix
    return raw or "media"


def delivery_basename(path: str | os.PathLike[str], *, fallback: str = "document.bin") -> str:
    """Return the user-visible filename, without Praxis' private spool prefix.

    The prefix makes on-disk names collision-proof and binds evidence to one
    stored object.  It is an implementation detail and must never become the
    Telegram document name shown to a person.
    """

    name = safe_basename(Path(path).name, fallback=fallback)
    visible = _SPOOL_NAME_PREFIX.sub("", name, count=1)
    return safe_basename(visible, fallback=fallback)


def contained_path(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> Path:
    """Resolve *path* below *root* or raise on traversal/symlink escape."""

    boundary = Path(root).expanduser().resolve()
    raw = Path(path).expanduser()
    candidate = raw if raw.is_absolute() else boundary / raw
    # abspath collapses ``..`` without following symlinks, so lexical escapes are
    # rejected separately from resolved symlink escapes.
    lexical = Path(os.path.abspath(candidate))
    try:
        lexical.relative_to(boundary)
    except ValueError as exc:
        raise MediaSecurityError("media path is outside the spool") from exc
    try:
        resolved = lexical.resolve(strict=False)
        resolved.relative_to(boundary)
    except (OSError, ValueError) as exc:
        raise MediaSecurityError("media path escapes the spool") from exc
    return resolved


def normalize_scope(scope: str) -> str:
    value = str(scope or "").strip().lower()
    if value not in ALLOWED_SCOPES:
        raise MediaSecurityError(f"unsupported media scope: {scope!r}")
    return value


def media_kind(mime: str | None) -> MediaKind | None:
    if mime in _MIME_EXTENSIONS:
        if str(mime).startswith("image/"):
            return "photo"
        if str(mime).startswith("audio/"):
            return "audio"
    return None


def _header(source: bytes | bytearray | memoryview | str | os.PathLike[str]) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source[:128])
    with Path(source).open("rb") as handle:
        return handle.read(128)


def sniff_mime(
    source: bytes | bytearray | memoryview | str | os.PathLike[str],
) -> str | None:
    """Identify a supported MIME from magic bytes, independent of extension."""

    head = _header(source)
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brands = head[8:64]
        if any(brand in brands for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1")):
            return "image/heic"
        if any(brand in brands for brand in (b"avif", b"avis")):
            return "image/avif"
        if any(brand in brands for brand in (b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B ")):
            return "audio/mp4"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"#!AMR\n") or head.startswith(b"#!AMR-WB\n"):
        return "audio/amr"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
        return "audio/aac"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    return None


def _id_text(value: ChatId | MessageId, label: str, *, allow_none: bool = False) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise MediaValidationError(f"{label} is required")
    if isinstance(value, bool):
        raise MediaValidationError(f"{label} must not be boolean")
    text = str(value).strip()
    if not text or len(text) > 256 or any(ord(ch) < 32 for ch in text):
        raise MediaValidationError(f"invalid {label}")
    return text


def _caption(value: str | None, *, truncate: bool) -> str:
    text = str(value or "")
    if len(text) > CAPTION_MAX_CHARS:
        if not truncate:
            raise MediaValidationError(
                f"caption exceeds {CAPTION_MAX_CHARS} characters"
            )
        text = text[:CAPTION_MAX_CHARS]
    return text


def _canonical_name(filename: str | os.PathLike[str] | None, mime: str, kind: MediaKind) -> str:
    if kind == "document":
        # Documents are intentionally not renamed from their visible extension.
        # Telegram must receive ``report.docx`` rather than an opaque ``document.bin``.
        return safe_basename(filename, fallback="document.bin")
    ext = _MIME_EXTENSIONS[mime]
    base = safe_basename(filename, fallback=kind + ext)
    stem = base[: -len(Path(base).suffix)] if Path(base).suffix else base
    stem = stem.rstrip(" ._") or kind
    return safe_basename(stem + ext, fallback=kind + ext)


def _chat_token(scope: str, chat_id: ChatId) -> str:
    chat = _id_text(chat_id, "chat_id")
    digest = hashlib.sha256(f"{scope}\0{chat}".encode("utf-8")).hexdigest()[:20]
    return f"chat-{digest}"


class MediaSpool:
    """Bounded media spool with an atomic, restart-safe outbound outbox.

    The in-memory deque is only a validated cache.  One JSON record per queue
    id under ``outbox-ledger/`` is the source of truth.  A pending record is
    written before :meth:`enqueue` returns; :meth:`discard` turns it into a
    durable delivery tombstone instead of forgetting it from RAM.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        photo_max_bytes: int = PHOTO_MAX_BYTES,
        audio_max_bytes: int = AUDIO_MAX_BYTES,
        document_max_bytes: int = DOCUMENT_MAX_BYTES,
        max_total_bytes: int = SPOOL_MAX_BYTES,
        ttl_seconds: int = MEDIA_TTL_SECONDS,
        max_queue: int = OUTBOUND_QUEUE_MAX,
        max_turn_media: int = TURN_MEDIA_MAX,
        outbox_result_ttl_seconds: int = OUTBOX_RESULT_TTL_SECONDS,
        outbox_result_max: int = OUTBOX_RESULT_MAX,
    ) -> None:
        if root is None:
            base = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
            root = base / "workspace" / "media"
        raw_root = Path(root).expanduser()
        if raw_root.exists() and raw_root.is_symlink():
            raise MediaSecurityError("media spool root must not be a symlink")
        raw_root.mkdir(parents=True, exist_ok=True)
        self.root = raw_root.resolve()
        self.photo_max_bytes = self._positive(photo_max_bytes, "photo_max_bytes")
        self.audio_max_bytes = self._positive(audio_max_bytes, "audio_max_bytes")
        self.document_max_bytes = self._positive(document_max_bytes, "document_max_bytes")
        self.max_total_bytes = self._positive(max_total_bytes, "max_total_bytes")
        self.ttl_seconds = self._positive(ttl_seconds, "ttl_seconds")
        self.max_queue = self._positive(max_queue, "max_queue")
        self.max_turn_media = self._positive(max_turn_media, "max_turn_media")
        self.outbox_result_ttl_seconds = self._positive(
            outbox_result_ttl_seconds, "outbox_result_ttl_seconds")
        self.outbox_result_max = self._positive(outbox_result_max, "outbox_result_max")
        self._queue: deque[OutboundMedia] = deque()
        self._claimed: set[str] = set()
        self._lock = threading.RLock()
        ledger = self.root / "outbox-ledger"
        if ledger.exists() and ledger.is_symlink():
            raise MediaSecurityError("outbox ledger must not be a symlink")
        ledger.mkdir(parents=True, exist_ok=True)
        self._ledger_dir = contained_path(self.root, ledger)
        self._maintenance_path = contained_path(self._ledger_dir, ".maintenance")
        try:
            os.chmod(self._ledger_dir, 0o700)
        except OSError:
            pass
        self._ledger_errors: list[dict] = []
        with self._lock, self._ledger_guard():
            self._reload_ledger_locked()

    def _ledger_holder_note(self, lock_path: Path) -> str:
        """Правда о держателе для текста отказа: кто держит и с какого времени.

        Отказ обязан говорить, кого именно ждали и насколько мы уверены, что это он же
        (закон 3). «Не знаю» здесь и выглядит как «не знаю»."""

        try:
            raw = lock_path.read_text(encoding="ascii", errors="ignore").strip()
        except OSError:
            return "holder unknown: the lock disappeared while it was being read"
        try:
            # ⚠ Не «last heartbeat»: пульс — нить best-effort, её может не быть вовсе, и
            # тогда это просто возраст файла. Называть возраст пульсом — враньё (закон 3).
            idle = max(0.0, time.time() - lock_path.stat().st_mtime)
            idle_note = f"untouched for {idle:.1f}s"
        except OSError:
            idle_note = "for how long it has been untouched is unknown"
        parts = raw.split(":")
        try:
            owner = int(parts[0])
        except (IndexError, ValueError):
            return f"held by an unreadable token {raw[:64]!r}, {idle_note}"
        if len(parts) >= 3 and parts[1]:
            birth = parts[1]
            current = _proc_started_at(owner)
            if not current:
                identity = "its birth mark cannot be read from here, so identity is unproven"
            elif current == birth:
                identity = "identity confirmed by birth mark"
            else:
                identity = f"birth mark differs now ({current}) — the number was reused"
            return (f"held by pid {owner} since process tick {birth} ({identity}), {idle_note}")
        return (f"held by pid {owner} with a legacy token carrying no birth mark, so a "
                f"reused process number cannot be told from the real holder, {idle_note}")

    def _ledger_verdict(self, lock_path: Path) -> tuple[bool, str]:
        """Отбирать ли замок — и ровно та причина с ровно тем числом, что применено.

        ⚠ 27.07. Раньше решение возвращало голый флаг, а текст отказа во ВСЕХ ветках
        называл LEDGER_ABANDONED_SEC. В ветке нечитаемого токена реальный порог — секунда:
        ей говорили «отберут после 60с», а отбирали через полсекунды; в логе кражи по
        переиспользованию номера стояло «бездействие 60с» при свежем mtime. Поэтому
        вердикт и объяснение рождаются в одной точке и расходиться не могут (закон 3)."""

        note = self._ledger_holder_note(lock_path)
        since = _held_here(lock_path)
        if since is not None:
            # Наш же процесс внутри `with`. Отобрать = писать в леджер вдвоём.
            held = max(0.0, time.monotonic() - since)
            overdue = ""
            if held > LEDGER_ABANDONED_SEC:
                overdue = (f" it is past the {LEDGER_ABANDONED_SEC:g}s abandoned threshold, so "
                           f"its own thread looks stuck — but a stuck thread of ours is a real "
                           f"deadlock, and naming it is better than two writers in one "
                           f"transaction;")
                if _first_stuck_report(lock_path, since):
                    _LOG.warning(
                        "media outbox ledger lock %s is held by this very process for %.1fs, "
                        "past the %ss threshold: our own holding thread is not finishing. The "
                        "lock is NOT taken over — that would mean two writers in one ledger "
                        "transaction", lock_path, held, f"{LEDGER_ABANDONED_SEC:g}")
            return False, (
                f"{note}; the holder is this very process and it is still inside its "
                f"transaction (holding {held:.1f}s).{overdue} A live in-process holder is "
                f"never taken over")
        try:
            raw = lock_path.read_text(encoding="ascii", errors="ignore")
            parts = raw.strip().split(":")
            owner = int(parts[0])
            started_at = parts[1] if len(parts) >= 3 else ""
        except (OSError, IndexError, ValueError):
            grace = LEDGER_UNREADABLE_GRACE_SEC
            try:
                # Держатель мог умереть между O_EXCL и записью токена. Вдох — и забираем
                # бесхозный замок, иначе он вечен. Порог здесь СВОЙ, не шестидесятисекундный.
                idle = max(0.0, time.time() - lock_path.stat().st_mtime)
            except OSError:
                return True, f"{note}; the lock vanished while it was being judged"
            if idle > grace:
                return True, (f"{note}; its token is unreadable and the file has stood "
                              f"untouched {idle:.1f}s, past the {grace:g}s grace given to a "
                              f"holder that died between creating the lock and writing its "
                              f"token")
            return False, (f"{note}; its token is unreadable, so it is taken over after "
                           f"{grace:g}s of standing still — not after "
                           f"{LEDGER_ABANDONED_SEC:g}s — and it has stood {idle:.1f}s")
        if not _owner_alive(owner, started_at):
            return True, (f"{note}; the process that took it is not there any more"
                          if started_at else
                          f"{note}; no process carries that number now")
        try:
            idle = max(0.0, time.time() - lock_path.stat().st_mtime)
        except OSError:
            return True, f"{note}; the lock vanished while it was being judged"
        if idle > LEDGER_ABANDONED_SEC:
            return True, (f"{note}; its process is alive but the lock has stood untouched "
                          f"{idle:.1f}s, past the {LEDGER_ABANDONED_SEC:g}s abandoned "
                          f"threshold")
        return False, (f"{note}; it is taken over only after {LEDGER_ABANDONED_SEC:g}s of "
                       f"standing still, and standing still is measured by the holder's "
                       f"heartbeat thread — which is best-effort and may not be running "
                       f"at all")

    @contextlib.contextmanager
    def _ledger_guard(self, timeout: float = 10.0):
        """Serialize ledger mutations across threads and service processes."""

        lock_path = self._ledger_dir / ".lock"
        # Метка рождения прямо в токене: номер процесса тождества не доказывает (см.
        # комментарий у LEDGER_ABANDONED_SEC). Формат pid:starttime:uuid, старый pid:uuid
        # читаем по-прежнему — замки от прошлой версии не должны становиться вечными.
        token = f"{os.getpid()}:{_proc_started_at(os.getpid())}:{uuid.uuid4().hex}"
        wait_budget = max(0.1, float(timeout))
        deadline = time.monotonic() + wait_budget
        while True:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            try:
                fd = os.open(str(lock_path), flags, 0o600)
                try:
                    os.write(fd, token.encode("ascii"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                # ⚠ 27.07: между O_EXCL и этой строкой замок мог уйти к соседу — он видел
                # созданный, но ещё пустой файл и по грейсу счёл его бесхозным. Молча
                # поехать дальше значило бы писать в леджер вдвоём.
                if _lock_token(lock_path) == token:
                    break
                _LOG.warning("media outbox ledger lock was taken over in the instant between "
                             "creating it and confirming it (%s)",
                             self._ledger_holder_note(lock_path))
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "media outbox ledger is busy: the lock we had just created was taken "
                        f"over before we could confirm it — {self._ledger_holder_note(lock_path)}"
                        f"; waited {wait_budget:.1f}s")
                time.sleep(0.02)
                continue
            except FileExistsError:
                # A lock path is control state, never a user-supplied pointer.
                # Unlink the link itself without inspecting its target.
                if lock_path.is_symlink():
                    try:
                        lock_path.unlink()
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "media outbox ledger symlink is busy: a symlink stands where "
                                f"the lock file must be and cannot be removed; waited "
                                f"{wait_budget:.1f}s")
                        time.sleep(0.02)
                    continue
                # ⚠ Возрастной путь раньше был ТОЛЬКО в ветке нечитаемого токена. Пока
                # номер жив (пусть и чужой), замок не отбирался никогда — и весь
                # исходящий медиа-тракт стоял до рестарта. Теперь выходов из тупика три
                # (мёртвый/переиспользованный номер, бездействие, нечитаемый токен), и
                # каждый называет СВОЙ порог — вердикт и объяснение из одной точки.
                stale, reason = self._ledger_verdict(lock_path)
                if stale:
                    # Кража замка — событие, о котором нельзя молчать: это либо мертвец,
                    # либо брошенная транзакция, и в обоих случаях след должен остаться.
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    else:
                        _LOG.warning("media outbox ledger lock taken over: %s", reason)
                        continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"media outbox ledger is busy: {reason}; waited {wait_budget:.1f}s")
                time.sleep(0.02)
        _hold_here(lock_path)
        heartbeat = _LockHeartbeat(lock_path, LEDGER_HEARTBEAT_SEC, token=token).start()
        try:
            yield
        finally:
            heartbeat.stop()
            try:
                # Windows scanners/contenders can briefly hold a sharing handle on
                # the lock between our token check and unlink.  Retry that narrow
                # race; leaving a live-pid lock behind would otherwise block every
                # later outbox operation until the service exits.
                for _attempt in range(50):
                    try:
                        if lock_path.read_text(encoding="ascii", errors="ignore") != token:
                            # Замок уже не наш. Раньше здесь молча выходили; но это значит,
                            # что кто-то писал в леджер одновременно с нами.
                            _LOG.warning(
                                "media outbox ledger lock %s was taken over while we held it "
                                "(%s) — this ledger write may have raced another writer",
                                lock_path, self._ledger_holder_note(lock_path))
                            break
                        lock_path.unlink()
                        _fsync_directory(self._ledger_dir)
                        break
                    except FileNotFoundError:
                        break
                    except OSError:
                        time.sleep(0.01)
            finally:
                # Снимать запись строго последним: пока она есть, свой же сосед не тронет
                # замок. Протёкший файл (unlink не удался) в реестре не числится и стареет
                # как чужой — вечным он не станет.
                _release_here(lock_path)

    def _record_path(self, queue_id: str) -> Path:
        value = str(queue_id or "")
        if not _QUEUE_ID.fullmatch(value):
            raise MediaValidationError("invalid outbound queue_id")
        return contained_path(self._ledger_dir, f"{value}.json")

    def _item_payload(self, item: OutboundMedia) -> dict:
        path = contained_path(self.root, item.path)
        return {
            "kind": item.kind,
            "path": path.relative_to(self.root).as_posix(),
            "mime": item.mime,
            "size": int(item.size),
            "target_chat_id": item.target_chat_id,
            "scope": item.scope,
            "caption": item.caption,
            "reply_to_message_id": item.reply_to_message_id,
            "voice_note": item.voice_note,
            "queue_id": item.queue_id,
            "run_id": item.run_id,
            "sha256": item.sha256,
        }

    def _item_from_payload(self, payload: dict) -> OutboundMedia:
        if not isinstance(payload, dict):
            raise MediaValidationError("outbox item must be an object")
        item = OutboundMedia(
            kind=str(payload.get("kind") or ""),  # type: ignore[arg-type]
            path=contained_path(self.root, str(payload.get("path") or "")),
            mime=str(payload.get("mime") or ""),
            size=int(payload.get("size")),
            target_chat_id=payload.get("target_chat_id"),
            scope=str(payload.get("scope") or ""),
            caption=str(payload.get("caption") or ""),
            reply_to_message_id=payload.get("reply_to_message_id"),
            voice_note=payload.get("voice_note"),
            queue_id=str(payload.get("queue_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            sha256=str(payload.get("sha256") or ""),
        )
        return self._validate_outbound_locked(item)

    def _read_record_locked(self, path: Path) -> dict:
        # Never follow an attacker- or accident-created ledger symlink.  The
        # record name is untrusted until its embedded queue id has been checked.
        if path.is_symlink():
            raise MediaSecurityError(f"outbox record must not be a symlink: {path.name}")
        try:
            direct = Path(os.path.abspath(path))
            direct.relative_to(self._ledger_dir)
        except (OSError, ValueError) as exc:
            raise MediaSecurityError(f"outbox record escapes ledger: {path.name}") from exc
        if direct.parent != self._ledger_dir or direct.suffix.lower() != ".json":
            raise MediaSecurityError(f"invalid outbox record path: {path.name}")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise MediaValidationError(f"invalid outbox record {path.name}: {exc}") from exc
        if not isinstance(record, dict) or record.get("schema") != _OUTBOX_SCHEMA:
            raise MediaValidationError(f"invalid outbox schema in {path.name}")
        queue_id = str(record.get("queue_id") or "")
        if self._record_path(queue_id) != path.resolve():
            raise MediaSecurityError(f"outbox queue id/path mismatch in {path.name}")
        if str(record.get("state") or "") not in _OUTBOX_STATES:
            raise MediaValidationError(f"invalid outbox state in {path.name}")
        return record

    def _write_record_locked(self, record: dict) -> None:
        _atomic_json(self._record_path(str(record.get("queue_id") or "")), record)

    def _terminal_record_locked(self, record: dict, state: str, *, reason: str,
                                now: float | None = None,
                                receipt: dict | None = None) -> dict:
        if state not in _TERMINAL_OUTBOX_STATES:
            raise ValueError("terminal outbox state required")
        current = time.time() if now is None else float(now)
        terminal = dict(record)
        terminal["state"] = state
        terminal["updated_at"] = current
        terminal["result"] = {
            "status": state, "reason": str(reason), "at": current,
            **(dict(receipt or {}) if state == "delivered" else {}),
        }
        self._write_record_locked(terminal)
        return terminal

    def _write_maintenance_locked(self, result: dict) -> None:
        snapshot = {
            "schema": _OUTBOX_MAINTENANCE_SCHEMA,
            "updated_at": time.time(),
            "last_prune": result,
        }
        _atomic_json(self._maintenance_path, snapshot)

    def _prune_terminal_locked(self, *, now: float) -> tuple[Path, ...]:
        """Bound terminal history by age and count without touching live intent."""

        terminal: list[tuple[float, str, Path]] = []
        errors: list[dict[str, str]] = []
        examined = 0
        for path in sorted(self._ledger_dir.glob("*.json")):
            try:
                record = self._read_record_locked(path)
            except MediaValidationError as exc:
                errors.append({"path": path.name, "error": str(exc)})
                continue
            if record.get("state") not in _TERMINAL_OUTBOX_STATES:
                continue
            examined += 1
            try:
                updated_at = float(record.get("updated_at"))
                if not math.isfinite(updated_at) or updated_at < 0:
                    raise ValueError("updated_at must be a finite non-negative number")
            except (TypeError, ValueError) as exc:
                errors.append({"path": path.name, "error": str(exc)})
                continue
            terminal.append((updated_at, str(record.get("queue_id") or ""), path))

        cutoff = now - self.outbox_result_ttl_seconds
        # Newest first makes the count cap deterministic even if wall-clock
        # resolution produces equal timestamps.
        terminal.sort(key=lambda row: (row[0], row[1]), reverse=True)
        candidates: list[tuple[Path, str]] = []
        retained_by_age = 0
        for index, (updated_at, _queue_id, path) in enumerate(terminal):
            if updated_at < cutoff:
                candidates.append((path, "age"))
            elif retained_by_age >= self.outbox_result_max:
                candidates.append((path, "count"))
            else:
                retained_by_age += 1

        removed: list[Path] = []
        by_reason = {"age": 0, "count": 0}
        for path, reason in candidates:
            try:
                path.unlink()
                removed.append(path)
                by_reason[reason] += 1
            except OSError as exc:
                errors.append({"path": path.name, "error": str(exc)})
        if removed:
            _fsync_directory(self._ledger_dir)

        self._write_maintenance_locked({
            "examined_terminal": examined,
            "retained_terminal": max(0, examined - len(removed)),
            "pruned": len(removed),
            "pruned_by_age": by_reason["age"],
            "pruned_by_count": by_reason["count"],
            "retention_seconds": self.outbox_result_ttl_seconds,
            "max_results": self.outbox_result_max,
            "errors": errors[:100],
            "error_count": len(errors),
        })
        return tuple(removed)

    def _reload_ledger_locked(self) -> list[MediaValidationError]:
        """Refresh the validated cache and durably expose invalid pending rows."""

        restored: list[tuple[float, str, OutboundMedia]] = []
        errors: list[MediaValidationError] = []
        self._ledger_errors = []
        for path in sorted(self._ledger_dir.glob("*.json")):
            try:
                record = self._read_record_locked(path)
            except MediaValidationError as exc:
                errors.append(exc)
                self._ledger_errors.append({
                    "schema": _OUTBOX_SCHEMA, "state": "corrupt",
                    "path": path.name, "error": str(exc),
                })
                continue
            if record.get("state") != "pending":
                continue
            try:
                item = self._item_from_payload(record.get("item") or {})
                if item.queue_id != str(record.get("queue_id") or ""):
                    raise MediaValidationError("outbox item/record queue id mismatch")
            except (MediaError, OSError, TypeError, ValueError) as exc:
                error = (exc if isinstance(exc, MediaValidationError)
                         else MediaValidationError(str(exc)))
                errors.append(error)
                self._terminal_record_locked(
                    record, "expired", reason=f"restore validation failed: {error}")
                continue
            restored.append((float(record.get("created_at") or 0.0), item.queue_id, item))
        restored.sort(key=lambda row: (row[0], row[1]))
        self._queue = deque(item for _created, _queue_id, item in restored)
        live_ids = {item.queue_id for item in self._queue}
        self._claimed.intersection_update(live_ids)
        return errors

    def _enqueue_locked(self, item: OutboundMedia, *, reload: bool = True) -> OutboundMedia:
        if reload:
            self._reload_ledger_locked()
        self._require_queue_space_locked()
        self._validate_outbound_locked(item)
        path = self._record_path(item.queue_id)
        if path.exists() or any(existing.queue_id == item.queue_id for existing in self._queue):
            raise MediaValidationError("duplicate outbound queue_id")
        now = time.time()
        record = {
            "schema": _OUTBOX_SCHEMA,
            "queue_id": item.queue_id,
            "state": "pending",
            "created_at": now,
            "updated_at": now,
            "item": self._item_payload(item),
            "result": None,
        }
        # Disk is authoritative: only publish the item in RAM after this replace.
        self._write_record_locked(record)
        self._queue.append(item)
        return item

    @staticmethod
    def _positive(value: int, label: str) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise MediaValidationError(f"{label} must be an integer") from exc
        if number <= 0:
            raise MediaValidationError(f"{label} must be positive")
        return number

    def _kind_cap(self, kind: MediaKind) -> int:
        if kind == "photo":
            return self.photo_max_bytes
        if kind == "audio":
            return self.audio_max_bytes
        if kind == "document":
            return self.document_max_bytes
        raise UnsupportedMediaError(f"unsupported media kind: {kind!r}")

    @staticmethod
    def _check_kind_mime(kind: str, mime: str | None) -> tuple[MediaKind, str]:
        if kind not in ALLOWED_KINDS:
            raise UnsupportedMediaError(f"unsupported media kind: {kind!r}")
        if kind == "document":
            # Unknown magic is expected for source trees, archives, office files and
            # build artifacts.  Recognised image/audio bytes must still use their rich
            # media kind so callers cannot accidentally change presentation semantics.
            if media_kind(mime) is not None:
                raise UnsupportedMediaError(
                    f"declared document contains recognised rich-media MIME {mime!r}"
                )
            return "document", str(mime or "application/octet-stream")
        actual_kind = media_kind(mime)
        if actual_kind is None:
            raise UnsupportedMediaError("unknown or unsupported media magic")
        if actual_kind != kind:
            raise UnsupportedMediaError(
                f"declared kind {kind!r} does not match detected MIME {mime!r}"
            )
        return kind, str(mime)  # type: ignore[return-value]

    def check_size(self, kind: MediaKind, size: int) -> None:
        """Cheap pre-download cap check; the actual file is checked again later."""

        probe_mime = (
            "image/jpeg" if kind == "photo"
            else "audio/ogg" if kind == "audio"
            else "application/octet-stream"
        )
        self._check_kind_mime(kind, probe_mime)
        try:
            value = int(size)
        except (TypeError, ValueError) as exc:
            raise MediaValidationError("media size must be an integer") from exc
        if value < 0:
            raise MediaValidationError("media size must not be negative")
        cap = self._kind_cap(kind)
        if value > cap:
            raise MediaTooLargeError(f"{kind} is {value} bytes; cap is {cap}")

    def _managed_files(self):
        for direction in ("inbound", "outbound"):
            top = self.root / direction
            if not top.exists() or top.is_symlink():
                continue
            for path in top.rglob("*"):
                if path.is_symlink():
                    continue
                try:
                    if path.is_file():
                        yield path
                except OSError:
                    continue

    def used_bytes(self) -> int:
        with self._lock:
            total = 0
            for path in self._managed_files():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
            return total

    def _check_capacity_locked(self, incoming_size: int) -> None:
        used = self.used_bytes()
        if used + incoming_size > self.max_total_bytes:
            raise MediaTooLargeError(
                f"media spool would exceed {self.max_total_bytes} bytes "
                f"({used} already used, {incoming_size} incoming)"
            )

    def _scoped_dir(self, direction: str, scope: str, chat_id: ChatId) -> Path:
        if direction not in ("inbound", "outbound"):
            raise MediaSecurityError("invalid media spool direction")
        scope = normalize_scope(scope)
        folder = contained_path(
            self.root,
            Path(direction) / scope / _chat_token(scope, chat_id),
        )
        # Refuse an attacker-created symlink at any managed component.  The root
        # itself is resolved and trusted by __init__.
        cursor = self.root
        for part in folder.relative_to(self.root).parts:
            cursor = cursor / part
            if cursor.exists() and cursor.is_symlink():
                raise MediaSecurityError("symlinks are not allowed in the media spool")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _destination(
        self,
        *,
        direction: str,
        scope: str,
        chat_id: ChatId,
        message_id: MessageId,
        filename: str | os.PathLike[str] | None,
        mime: str,
        kind: MediaKind,
    ) -> Path:
        folder = self._scoped_dir(direction, scope, chat_id)
        msg = _id_text(message_id, "message_id", allow_none=True) or "generated"
        msg_token = hashlib.sha256(msg.encode("utf-8")).hexdigest()[:10]
        name = _canonical_name(filename, mime, kind)
        return contained_path(
            self.root,
            folder / f"msg-{msg_token}-{uuid.uuid4().hex[:12]}-{name}",
        )

    def _make_ref(
        self,
        path: Path,
        *,
        kind: MediaKind,
        mime: str,
        chat_id: ChatId,
        message_id: MessageId,
        scope: str,
        caption: str | None,
    ) -> MediaRef:
        return MediaRef(
            kind=kind,
            path=path,
            mime=mime,
            size=path.stat().st_size,
            chat_id=chat_id,
            message_id=message_id,
            scope=normalize_scope(scope),
            caption=_caption(caption, truncate=True),
            sha256=_file_sha256(path),
        )

    def _store_bytes(
        self,
        data: bytes | bytearray | memoryview,
        *,
        direction: str,
        kind: MediaKind,
        filename: str | os.PathLike[str] | None,
        chat_id: ChatId,
        message_id: MessageId,
        scope: str,
        caption: str | None,
    ) -> MediaRef:
        raw = bytes(data)
        mime = sniff_mime(raw)
        kind, mime = self._check_kind_mime(kind, mime)
        _id_text(chat_id, "chat_id")
        _id_text(message_id, "message_id", allow_none=True)
        scope = normalize_scope(scope)
        self.check_size(kind, len(raw))
        with self._lock:
            self._check_capacity_locked(len(raw))
            dest = self._destination(
                direction=direction,
                scope=scope,
                chat_id=chat_id,
                message_id=message_id,
                filename=filename,
                mime=mime,
                kind=kind,
            )
            temp = contained_path(
                self.root, dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
            )
            try:
                with temp.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, dest)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            ref = self._make_ref(
                dest,
                kind=kind,
                mime=mime,
                chat_id=chat_id,
                message_id=message_id,
                scope=scope,
                caption=caption,
            )
            return self._validate_ref_locked(ref)

    def _store_path(
        self,
        source: str | os.PathLike[str],
        *,
        direction: str,
        kind: MediaKind,
        filename: str | os.PathLike[str] | None,
        chat_id: ChatId,
        message_id: MessageId,
        scope: str,
        caption: str | None,
        move: bool,
    ) -> MediaRef:
        raw_source = Path(source).expanduser()
        if raw_source.is_symlink():
            raise MediaSecurityError("media source must not be a symlink")
        try:
            src = raw_source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise MediaValidationError("media source does not exist") from exc
        if not src.is_file():
            raise MediaValidationError("media source is not a regular file")
        mime = sniff_mime(src)
        kind, mime = self._check_kind_mime(kind, mime)
        _id_text(chat_id, "chat_id")
        _id_text(message_id, "message_id", allow_none=True)
        scope = normalize_scope(scope)
        advertised_size = src.stat().st_size
        self.check_size(kind, advertised_size)
        with self._lock:
            self._check_capacity_locked(advertised_size)
            dest = self._destination(
                direction=direction,
                scope=scope,
                chat_id=chat_id,
                message_id=message_id,
                filename=filename or src.name,
                mime=mime,
                kind=kind,
            )
            temp = contained_path(
                self.root, dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
            )
            try:
                shutil.copyfile(src, temp, follow_symlinks=False)
                copied_size = temp.stat().st_size
                self.check_size(kind, copied_size)
                if copied_size != advertised_size:
                    raise MediaValidationError("media source changed while being copied")
                copied_mime = sniff_mime(temp) or (
                    "application/octet-stream" if kind == "document" else None
                )
                if copied_mime != mime:
                    raise MediaValidationError("media source changed while being copied")
                # The temporary file now contributes to disk usage but was not in
                # the pre-copy total; advertised_size is therefore the correct delta.
                os.replace(temp, dest)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            if move:
                try:
                    src.unlink()
                except OSError:
                    # Storage succeeded; move semantics are best effort so the
                    # validated spool object is never lost because source cleanup failed.
                    pass
            ref = self._make_ref(
                dest,
                kind=kind,
                mime=mime,
                chat_id=chat_id,
                message_id=message_id,
                scope=scope,
                caption=caption,
            )
            return self._validate_ref_locked(ref)

    def ingest_bytes(
        self,
        data: bytes | bytearray | memoryview,
        *,
        kind: MediaKind,
        filename: str | os.PathLike[str] | None,
        chat_id: ChatId,
        message_id: MessageId,
        scope: str,
        caption: str = "",
    ) -> MediaRef:
        """Validate and atomically place inbound bytes in the scoped spool."""

        return self._store_bytes(
            data,
            direction="inbound",
            kind=kind,
            filename=filename,
            chat_id=chat_id,
            message_id=message_id,
            scope=scope,
            caption=caption,
        )

    def ingest_path(
        self,
        source: str | os.PathLike[str],
        *,
        kind: MediaKind,
        chat_id: ChatId,
        message_id: MessageId,
        scope: str,
        caption: str = "",
        filename: str | os.PathLike[str] | None = None,
        move: bool = False,
    ) -> MediaRef:
        """Validate and atomically copy/move an inbound file into the spool."""

        return self._store_path(
            source,
            direction="inbound",
            kind=kind,
            filename=filename,
            chat_id=chat_id,
            message_id=message_id,
            scope=scope,
            caption=caption,
            move=move,
        )

    def _path_binding(self, ref: MediaRef) -> tuple[Path, tuple[str, ...]]:
        raw = Path(ref.path)
        if raw.is_symlink():
            raise MediaSecurityError("media references may not point to symlinks")
        path = contained_path(self.root, raw)
        rel = path.relative_to(self.root)
        parts = rel.parts
        if len(parts) != 4 or parts[0] not in ("inbound", "outbound"):
            raise MediaSecurityError("media path is not in a managed scoped directory")
        # No managed path component may be a symlink, even when it resolves back
        # inside the spool.
        cursor = self.root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise MediaSecurityError("symlinks are not allowed in the media spool")
        return path, parts

    def _validate_ref_locked(
        self,
        ref: MediaRef,
        *,
        expected_scope: str | None = None,
        expected_chat_id: ChatId | None = None,
    ) -> MediaRef:
        if not isinstance(ref, MediaRef):
            raise MediaValidationError("expected MediaRef")
        kind, declared_mime = self._check_kind_mime(ref.kind, ref.mime)
        scope = normalize_scope(ref.scope)
        if scope != ref.scope:
            raise MediaSecurityError("media scope must be canonical")
        _id_text(ref.chat_id, "chat_id")
        _id_text(ref.message_id, "message_id", allow_none=True)
        _caption(ref.caption, truncate=False)
        path, parts = self._path_binding(ref)
        if parts[1] != scope or parts[2] != _chat_token(scope, ref.chat_id):
            raise MediaSecurityError("media path is bound to another scope or chat")
        if expected_scope is not None and scope != normalize_scope(expected_scope):
            raise MediaSecurityError("media is not visible in the requested scope")
        if expected_chat_id is not None:
            expected = _id_text(expected_chat_id, "expected_chat_id")
            actual = _id_text(ref.chat_id, "chat_id")
            if actual != expected:
                raise MediaSecurityError("media belongs to another chat")
        if not path.is_file():
            raise MediaValidationError("spooled media file is missing")
        stat = path.stat()
        if stat.st_size != ref.size:
            raise MediaValidationError("spooled media size no longer matches its reference")
        self.check_size(kind, stat.st_size)
        if ref.sha256:
            if not _SHA256.fullmatch(ref.sha256):
                raise MediaValidationError("invalid spooled media sha256")
            if _file_sha256(path) != ref.sha256:
                raise MediaValidationError("spooled media sha256 no longer matches its reference")
        elif kind == "document":
            raise MediaValidationError("guarded document requires sha256")
        detected = sniff_mime(path) or (
            "application/octet-stream" if kind == "document" else None
        )
        if detected != declared_mime:
            raise MediaValidationError(
                f"spooled media magic is {detected!r}, reference says {declared_mime!r}"
            )
        if kind != "document" and path.suffix.lower() != _MIME_EXTENSIONS[declared_mime]:
            raise MediaValidationError("spooled media extension is not canonical for its MIME")
        return ref

    def validate_ref(
        self,
        ref: MediaRef,
        *,
        expected_scope: str | None = None,
        expected_chat_id: ChatId | None = None,
    ) -> MediaRef:
        """Re-check bytes, path containment, metadata, and scope/chat binding."""

        with self._lock:
            return self._validate_ref_locked(
                ref,
                expected_scope=expected_scope,
                expected_chat_id=expected_chat_id,
            )

    def _validate_outbound_locked(
        self,
        item: OutboundMedia,
        *,
        expected_scope: str | None = None,
        expected_chat_id: ChatId | None = None,
    ) -> OutboundMedia:
        if not isinstance(item, OutboundMedia):
            raise MediaValidationError("expected OutboundMedia")
        if not _QUEUE_ID.fullmatch(item.queue_id):
            raise MediaValidationError("invalid outbound queue_id")
        if not isinstance(item.voice_note, bool):
            raise MediaValidationError("voice_note must be boolean")
        if item.voice_note and item.kind != "audio":
            raise MediaValidationError("voice_note is valid only for audio")
        self._validate_ref_locked(
            item.as_ref(),
            expected_scope=expected_scope,
            expected_chat_id=expected_chat_id,
        )
        return item

    def validate_outbound(
        self,
        item: OutboundMedia,
        *,
        expected_scope: str | None = None,
        expected_chat_id: ChatId | None = None,
    ) -> OutboundMedia:
        with self._lock:
            return self._validate_outbound_locked(
                item,
                expected_scope=expected_scope,
                expected_chat_id=expected_chat_id,
            )

    def validate_turn(
        self,
        envelope: TurnEnvelope,
        *,
        expected_scope: str | None = None,
        expected_chat_id: ChatId | None = None,
    ) -> TurnEnvelope:
        """Validate a model turn and reject mixed-scope/mixed-chat attachments."""

        if not isinstance(envelope, TurnEnvelope):
            raise MediaValidationError("expected TurnEnvelope")
        if not envelope.text.strip() and not envelope.media and not envelope.outbound:
            raise MediaValidationError("turn has neither text nor media")
        media_count = len(envelope.media) + len(envelope.outbound)
        if media_count > self.max_turn_media:
            raise MediaValidationError(
                f"turn has {media_count} media objects; cap is {self.max_turn_media}"
            )
        with self._lock:
            scopes: set[str] = set()
            chats: set[str] = set()
            for ref in envelope.media:
                self._validate_ref_locked(
                    ref,
                    expected_scope=expected_scope,
                    expected_chat_id=expected_chat_id,
                )
                scopes.add(ref.scope)
                chats.add(str(ref.chat_id))
            for item in envelope.outbound:
                self._validate_outbound_locked(
                    item,
                    expected_scope=expected_scope,
                    expected_chat_id=expected_chat_id,
                )
                scopes.add(item.scope)
                chats.add(str(item.target_chat_id))
            if len(scopes) > 1 or len(chats) > 1:
                raise MediaSecurityError("one turn may not mix media from different scopes/chats")
        return envelope

    def envelope(
        self,
        text: str = "",
        outbound: tuple[OutboundMedia, ...] | list[OutboundMedia] = (),
        media: tuple[MediaRef, ...] | list[MediaRef] = (),
        *,
        boundary: bool = False,
        run_id: str = "",
        expected_scope: str | None = None,
        expected_chat_id: ChatId | None = None,
    ) -> TurnEnvelope:
        turn = TurnEnvelope(
            text=text, outbound=tuple(outbound), media=tuple(media), boundary=boundary,
            run_id=run_id)
        return self.validate_turn(
            turn,
            expected_scope=expected_scope,
            expected_chat_id=expected_chat_id,
        )

    def _require_queue_space_locked(self) -> None:
        if len(self._queue) >= self.max_queue:
            raise MediaQueueFullError(f"outbound media queue cap is {self.max_queue}")

    def enqueue(self, item: OutboundMedia) -> OutboundMedia:
        """Durably enqueue an existing request before returning it to a sender."""

        with self._lock, self._ledger_guard():
            return self._enqueue_locked(item)

    def resolve_outbound(
        self,
        source: MediaRef | str | os.PathLike[str],
        *,
        kind: MediaKind | None = None,
        target_chat_id: ChatId | None = None,
        reply_to_message_id: MessageId = None,
        scope: str | None = None,
        caption: str | None = None,
        filename: str | os.PathLike[str] | None = None,
        move: bool = False,
        voice_note: bool = False,
    ) -> OutboundMedia:
        """Resolve/copy and validate an outbound source without queueing/sending."""

        with self._lock:
            created_path: Path | None = None
            if isinstance(source, MediaRef):
                ref = self._validate_ref_locked(source)
                if kind is not None and kind != ref.kind:
                    raise MediaValidationError("outbound kind differs from MediaRef")
                target_scope = ref.scope if scope is None else normalize_scope(scope)
                target_chat = ref.chat_id if target_chat_id is None else target_chat_id
                if target_scope != ref.scope or str(target_chat) != str(ref.chat_id):
                    raise MediaSecurityError(
                        "implicit forwarding to another scope/chat is not allowed"
                    )
                item = OutboundMedia.from_ref(
                    ref,
                    caption=caption,
                    reply_to_message_id=reply_to_message_id,
                    voice_note=voice_note,
                )
            else:
                if kind is None or target_chat_id is None or scope is None:
                    raise MediaValidationError(
                        "kind, target_chat_id, and scope are required for an outbound path"
                    )
                ref = self._store_path(
                    source,
                    direction="outbound",
                    kind=kind,
                    filename=filename,
                    chat_id=target_chat_id,
                    message_id=reply_to_message_id,
                    scope=scope,
                    caption=caption,
                    move=move,
                )
                created_path = ref.path
                item = OutboundMedia.from_ref(ref, voice_note=voice_note)
            try:
                self._validate_outbound_locked(item)
            except Exception:
                if created_path is not None:
                    try:
                        created_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            return item

    def resolve_outbound_bytes(
        self,
        data: bytes | bytearray | memoryview,
        *,
        kind: MediaKind,
        filename: str | os.PathLike[str] | None,
        target_chat_id: ChatId,
        reply_to_message_id: MessageId,
        scope: str,
        caption: str = "",
        voice_note: bool = False,
    ) -> OutboundMedia:
        """Bytes variant of :meth:`resolve_outbound`, also with no side effects."""

        with self._lock:
            ref = self._store_bytes(
                data,
                direction="outbound",
                kind=kind,
                filename=filename,
                chat_id=target_chat_id,
                message_id=reply_to_message_id,
                scope=scope,
                caption=caption,
            )
            item = OutboundMedia.from_ref(ref, voice_note=voice_note)
            try:
                self._validate_outbound_locked(item)
            except Exception:
                try:
                    ref.path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return item

    def queue_outbound(
        self,
        source: MediaRef | str | os.PathLike[str],
        *,
        kind: MediaKind | None = None,
        target_chat_id: ChatId | None = None,
        reply_to_message_id: MessageId = None,
        scope: str | None = None,
        caption: str | None = None,
        filename: str | os.PathLike[str] | None = None,
        move: bool = False,
        voice_note: bool = False,
    ) -> OutboundMedia:
        """Resolve and queue one outbound object; still performs no send."""

        with self._lock, self._ledger_guard():
            self._reload_ledger_locked()
            self._require_queue_space_locked()
            item = self.resolve_outbound(
                source,
                kind=kind,
                target_chat_id=target_chat_id,
                reply_to_message_id=reply_to_message_id,
                scope=scope,
                caption=caption,
                filename=filename,
                move=move,
                voice_note=voice_note,
            )
            try:
                return self._enqueue_locked(item, reload=False)
            except Exception:
                if not isinstance(source, MediaRef):
                    try:
                        item.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise

    def queue_outbound_bytes(
        self,
        data: bytes | bytearray | memoryview,
        *,
        kind: MediaKind,
        filename: str | os.PathLike[str] | None,
        target_chat_id: ChatId,
        reply_to_message_id: MessageId,
        scope: str,
        caption: str = "",
        voice_note: bool = False,
    ) -> OutboundMedia:
        """Resolve and queue provider bytes, without invoking a sender."""

        with self._lock, self._ledger_guard():
            self._reload_ledger_locked()
            self._require_queue_space_locked()
            item = self.resolve_outbound_bytes(
                data,
                kind=kind,
                filename=filename,
                target_chat_id=target_chat_id,
                reply_to_message_id=reply_to_message_id,
                scope=scope,
                caption=caption,
                voice_note=voice_note,
            )
            try:
                return self._enqueue_locked(item, reload=False)
            except Exception:
                try:
                    item.path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

    def pending(
        self,
        *,
        scope: str | None = None,
        chat_id: ChatId | None = None,
    ) -> tuple[OutboundMedia, ...]:
        """Return an immutable queue snapshot, optionally restricted by channel."""

        wanted_scope = normalize_scope(scope) if scope is not None else None
        wanted_chat = _id_text(chat_id, "chat_id") if chat_id is not None else None
        with self._lock, self._ledger_guard():
            self._reload_ledger_locked()
            return tuple(
                item
                for item in self._queue
                if (wanted_scope is None or item.scope == wanted_scope)
                and (wanted_chat is None or str(item.chat_id) == wanted_chat)
            )

    def peek(self) -> OutboundMedia | None:
        with self._lock, self._ledger_guard():
            self._reload_ledger_locked()
            return self._queue[0] if self._queue else None

    def dequeue(self) -> OutboundMedia | None:
        """Claim the next request locally without erasing its durable intent.

        A sender must call :meth:`discard` only after it has an acceptance
        receipt.  An unacknowledged claim becomes pending again after restart.
        """

        with self._lock, self._ledger_guard():
            errors = self._reload_ledger_locked()
            if errors:
                raise errors[0]
            for item in self._queue:
                if item.queue_id not in self._claimed:
                    self._validate_outbound_locked(item)
                    self._claimed.add(item.queue_id)
                    return item
            return None

    def discard(self, queue_id: str, *, receipt: dict | None = None) -> bool:
        """Acknowledge a sender receipt with a durable delivered tombstone."""

        wanted = str(queue_id or "")
        with self._lock, self._ledger_guard():
            path = self._record_path(wanted)
            if not path.is_file():
                return False
            record = self._read_record_locked(path)
            if record.get("state") == "delivered":
                return False
            # A late, real receipt is stronger evidence than an earlier TTL or
            # missing-file observation, so expired -> delivered is intentional.
            self._terminal_record_locked(
                record, "delivered", reason="sender acceptance receipt acknowledged",
                receipt=receipt,
            )
            self._queue = deque(item for item in self._queue if item.queue_id != wanted)
            self._claimed.discard(wanted)
            return True

    def fail(self, queue_id: str, *, reason: str) -> bool:
        """Terminally stop a pending upload that must never be retried."""

        wanted = str(queue_id or "")
        with self._lock, self._ledger_guard():
            path = self._record_path(wanted)
            if not path.is_file():
                return False
            record = self._read_record_locked(path)
            if record.get("state") != "pending":
                return False
            self._terminal_record_locked(record, "failed", reason=str(reason or "stopped"))
            self._queue = deque(item for item in self._queue if item.queue_id != wanted)
            self._claimed.discard(wanted)
            return True

    def outbox_results(self, state: str | None = None) -> tuple[dict, ...]:
        """Return durable delivered/expired/corrupt records for recovery and UI."""

        wanted = str(state or "").strip().lower()
        allowed = {*_TERMINAL_OUTBOX_STATES, "corrupt"}
        if wanted and wanted not in allowed:
            raise MediaValidationError(
                "outbox result state must be delivered, expired, failed, or corrupt")
        with self._lock, self._ledger_guard():
            self._reload_ledger_locked()
            rows: list[dict] = []
            for path in sorted(self._ledger_dir.glob("*.json")):
                try:
                    record = self._read_record_locked(path)
                except MediaValidationError:
                    continue
                if record.get("state") in _TERMINAL_OUTBOX_STATES:
                    rows.append(json.loads(json.dumps(record, ensure_ascii=False)))
            rows.extend(json.loads(json.dumps(row, ensure_ascii=False))
                        for row in self._ledger_errors)
            if wanted:
                rows = [row for row in rows if row.get("state") == wanted]
            rows.sort(key=lambda row: (float(row.get("updated_at") or 0.0),
                                       str(row.get("queue_id") or row.get("path") or "")))
            return tuple(rows)

    def outbox_maintenance(self) -> dict:
        """Return the last bounded-prune result, including any unlink/read errors."""

        with self._lock, self._ledger_guard():
            if self._maintenance_path.is_symlink():
                raise MediaSecurityError("outbox maintenance state must not be a symlink")
            if not self._maintenance_path.is_file():
                return {}
            try:
                snapshot = json.loads(self._maintenance_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError) as exc:
                raise MediaValidationError(f"invalid outbox maintenance state: {exc}") from exc
            if (not isinstance(snapshot, dict)
                    or snapshot.get("schema") != _OUTBOX_MAINTENANCE_SCHEMA):
                raise MediaValidationError("invalid outbox maintenance schema")
            return json.loads(json.dumps(snapshot, ensure_ascii=False))

    def cleanup(
        self,
        *,
        now: float | None = None,
        ttl_seconds: int | None = None,
    ) -> tuple[Path, ...]:
        """Delete expired media and prune old terminal outbox receipts.

        Symlinks encountered below ``inbound/`` or ``outbound/`` are unlinked as
        unsafe spool artifacts; their targets are never followed or deleted.
        Pending delivery intent is never pruned.  Returned paths include any
        terminal receipt records removed by age/count retention.
        """

        current = time.time() if now is None else float(now)
        ttl = self.ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if ttl < 0:
            raise MediaValidationError("ttl_seconds must not be negative")
        removed: list[Path] = []
        with self._lock, self._ledger_guard():
            self._reload_ledger_locked()
            for direction in ("inbound", "outbound"):
                top = self.root / direction
                if not top.exists():
                    continue
                if top.is_symlink():
                    top.unlink()
                    removed.append(top)
                    continue
                paths = list(top.rglob("*"))
                for path in paths:
                    try:
                        if path.is_symlink():
                            path.unlink()
                            removed.append(path)
                            continue
                        if not path.is_file():
                            continue
                        if current - path.stat().st_mtime > ttl:
                            for item in tuple(self._queue):
                                if item.path == path:
                                    record_path = self._record_path(item.queue_id)
                                    if record_path.is_file():
                                        record = self._read_record_locked(record_path)
                                        self._terminal_record_locked(
                                            record, "expired",
                                            reason="managed media exceeded TTL", now=current)
                                    self._claimed.discard(item.queue_id)
                            path.unlink()
                            removed.append(path)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        continue
                for folder in sorted(
                    (path for path in paths if path.is_dir() and not path.is_symlink()),
                    key=lambda item: len(item.parts),
                    reverse=True,
                ):
                    try:
                        folder.rmdir()
                    except OSError:
                        pass
            removed_keys = {str(path) for path in removed}
            self._queue = deque(
                item
                for item in self._queue
                if str(item.path) not in removed_keys and item.path.is_file()
            )
            # External deletion or invalid bytes are also converted into a
            # visible expired result by the authoritative reload.
            self._reload_ledger_locked()
            removed.extend(self._prune_terminal_locked(now=current))
        return tuple(removed)


__all__ = [
    "ALLOWED_KINDS",
    "ALLOWED_SCOPES",
    "AUDIO_MAX_BYTES",
    "CAPTION_MAX_CHARS",
    "MEDIA_TTL_SECONDS",
    "OUTBOX_RESULT_MAX",
    "OUTBOX_RESULT_TTL_SECONDS",
    "OUTBOUND_QUEUE_MAX",
    "PHOTO_MAX_BYTES",
    "SPOOL_MAX_BYTES",
    "TURN_MEDIA_MAX",
    "MediaError",
    "MediaQueueFullError",
    "MediaRef",
    "MediaSecurityError",
    "MediaSpool",
    "MediaTooLargeError",
    "MediaValidationError",
    "OutboundMedia",
    "TurnEnvelope",
    "UnsupportedMediaError",
    "contained_path",
    "media_kind",
    "normalize_scope",
    "safe_basename",
    "delivery_basename",
    "sniff_mime",
]
