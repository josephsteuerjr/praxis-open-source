"""Механический кред-пол — единственное твёрдое (закон 3): креды не текут.

Это НЕ советник и НЕ оценка содержания — только форма токенов. Точный, быстрый,
не судит намерения. Ноль ложных срабатываний на git-SHA / expected_sha256 (её
честный инженерный материал) — намеренно нет hex-эвристики.

Общий модуль: им пользуется и наррация (Этап 2), и outbound-гард голоса — чтобы
у обоих был ОДИН пол, а не два расходящихся списка. Пришёл сюда из core/narration
(диагностика 23.07: LLM-судья ложно клеймил «credential» на SHA/receipt — этот
механический пол заменяет ту догадку).
"""
from __future__ import annotations

import re

_CRED_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws access key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "api key (sk-)"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack token"),
    # GitHub: classic (ghp_) + fine-grained/app/oauth (github_pat_, gho_/ghu_/ghs_/ghr_):
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "github token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), "github fine-grained pat"),
    (re.compile(r"\bgh[ousr]_[A-Za-z0-9]{30,}\b"), "github oauth/app token"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"), "jwt"),
    # Формы, которые в ЕЁ хозяйстве живут и уже текли (bot-token 17.07, z.ai в git):
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "telegram bot token"),
    (re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b"), "zai api key"),
    # НЕТ голой hex-эвристики: git-SHA и expected_sha256 — её честный инженерный материал
    # (z.ai-паттерн — не hex-эвристика: обязательная '.' + ровно-16 суффикс).
    # НЕТ env-присвоения ('PASSWORD=…'): она инфра-агент, обсуждает конфиги/env
    # постоянно — паттерн дал бы ложняки по её работе (та самая misfire). Prose-секрет
    # НЕ токен-формы — задокументированный остаток; реальный .env обычно уходит
    # скриншотом, а его ловит зрение-судья (PRIVACY_HOLD_CREDENTIAL по staged-image).
)


def credential_floor(*texts: str) -> str:
    """'' если чисто; иначе метка похожего на кред паттерна (пол, не совет).

    Принимает несколько строк (текст ответа + описание staged-медиа) — секрет в
    любой из них держит доставку."""
    for text in texts:
        value = str(text or "")
        if not value:
            continue
        for pattern, label in _CRED_PATTERNS:
            if pattern.search(value):
                return label
    return ""


# Промежуточный пасс D1: документ-вложение (.env/.pem/credentials.json) уходил мимо
# пола — метаданные сканировались, байты нет. Кап чтения держит память; бинарь
# (NUL в первом окне) СОЗНАТЕЛЬНО не покрыт: держать каждый архив/картинку-документом
# было бы забором по её нормальной работе. Секрет глубже капа в гигантском текстовом
# файле — задокументированный остаток, как prose-секрет в credential_floor.
DOCUMENT_SCAN_CAP = 512 * 1024


def _decode_text_head(head: bytes) -> str | None:
    """Разобрать head как человекочитаемый текст (utf-8/utf-16), иначе None (бинарь).

    UTF-16 важен: PowerShell-хост Егора по умолчанию пишет .env/конфиги в UTF-16,
    где после каждого ASCII-байта идёт \\x00 — наивная проверка «есть NUL → бинарь»
    пропускала бы такой человекочитаемый секрет (адверсарка round-1)."""
    if not head:
        return None
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):  # BOM UTF-16 LE/BE
        return head.decode("utf-16", "replace")
    if b"\x00" not in head:
        return head.decode("utf-8", "replace")
    # NUL без BOM: UTF-16 без метки чередует \x00 в чётных/нечётных позициях.
    zeros_even = head[0:4096:2].count(0)
    zeros_odd = head[1:4096:2].count(0)
    half = min(len(head), 4096) // 2
    if half and (zeros_even >= half * 0.8 or zeros_odd >= half * 0.8):
        endian = "utf-16-le" if zeros_odd >= zeros_even else "utf-16-be"
        return head.decode(endian, "replace")
    return None  # прочий NUL — настоящий бинарь, вне покрытия


# Файлы, которые являются кредом ЦЕЛИКОМ и по своей природе, а не по строке внутри.
# Сканировать их бессмысленно: сессия Telethon — это SQLite, ключ лежит в бинарном
# блобе, и текстовый пол по ней возвращает «чисто».
_CREDENTIAL_NAMES = (
    ".env", "llm.json", ".deploy.env", "praxis_device_auth.key",
)
_CREDENTIAL_SUFFIXES = (
    ".session", ".session-journal", ".pem", ".key", ".p12", ".pfx", ".keytab",
)
_CREDENTIAL_PARTS = ("credentials", "secrets")


def credential_by_identity(path) -> str:
    """Файл, который сам по себе кред. '' — не он.

    ⚠ Адверсарка 26.07 нашла дыру ценой всего дома: `praxis.session` — это SQLite с
    ключом авторизации её Telegram-аккаунта, то есть самый невосполнимый кред тут
    (номер одноразовый, сессию не перевыпустить). Текстовый пол честно отвечал по нему
    «чисто», потому что бинарь вне его покрытия, — и файл проходил обе отправки. Пока
    `send_file` был только у владельца, это был принятый остаток; с 26.07 рука есть в
    любом ходе, и остаток стал дырой.

    Опознание по ИМЕНИ, а не по содержимому: то, что целиком является ключом, нельзя
    отличить от мусора чтением первых килобайт.
    """
    raw = str(path or "").strip().replace("\\", "/")
    name = raw.rsplit("/", 1)[-1].casefold()
    if not name:
        return ""
    lowered = raw.casefold()
    if name in _CREDENTIAL_NAMES or name.startswith(".env"):
        return f"файл целиком является кредом: {name}"
    if any(name.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES):
        return f"файл целиком является кредом: {name}"
    if any(f"/{part}/" in f"/{lowered}" for part in _CREDENTIAL_PARTS):
        return f"файл лежит в каталоге кредов: {name}"
    return ""


def document_floor(path, *, cap: int = DOCUMENT_SCAN_CAP) -> str:
    """Кред-пол по файлу: '' — чисто. Иначе строка причины.

    Два слоя, и порядок важен: СНАЧАЛА содержимое — оно называет находку точно («private
    key», «bot token»), и такая причина полезнее ей самой. И только если содержимое
    молчит (бинарь, пусто, нечитаемо) — опознание по имени, потому что файл-ключ целиком
    прочтением не опознаётся.

    Только чтение и только метка наружу — содержимое никуда не копируется. Нечитаемый
    файл не держится здесь: его и отправить не выйдет.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(max(0, int(cap)))
    except OSError:
        head = b""
    text = _decode_text_head(head)
    if text is not None:
        found = credential_floor(text)
        if found:
            return found
    return credential_by_identity(path)
