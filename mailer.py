"""
Praxis — почта (SMTP send + IMAP read). Без новых зависимостей (stdlib smtplib/imaplib).

Креды из env (держать в .deploy.env, gitignored):
  PRAXIS_EMAIL_ADDR, PRAXIS_EMAIL_PASS
Хосты (дефолты Mailfence):
  PRAXIS_SMTP_HOST=smtp.mailfence.com  PRAXIS_SMTP_PORT=465 (SSL)
  PRAXIS_IMAP_HOST=imap.mailfence.com  PRAXIS_IMAP_PORT=993 (SSL)

Почта — отдельный КАНАЛ. Исходящее по умолчанию owner-направлено (она шлёт, когда Егор просит);
автономная отправка — за env PRAXIS_EMAIL_AUTONOMOUS. Входящее от незнакомца не должно тянуть
приватку (scope как у unknown) — это гейтит вызывающий код в agent.py.
"""
from __future__ import annotations

import email as _email
import imaplib
import logging
import os
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage

log = logging.getLogger("praxis-mail")

# модульные ссылки — чтобы легко мокать в тестах
SMTP_SSL = smtplib.SMTP_SSL
IMAP4_SSL = imaplib.IMAP4_SSL


def _cfg() -> dict:
    return {
        "addr": os.getenv("PRAXIS_EMAIL_ADDR", "").strip(),
        "pw": os.getenv("PRAXIS_EMAIL_PASS", ""),
        "smtp_host": os.getenv("PRAXIS_SMTP_HOST", "smtp.mailfence.com"),
        "smtp_port": int(os.getenv("PRAXIS_SMTP_PORT", "465") or 465),
        "imap_host": os.getenv("PRAXIS_IMAP_HOST", "imap.mailfence.com"),
        "imap_port": int(os.getenv("PRAXIS_IMAP_PORT", "993") or 993),
    }


def configured() -> bool:
    c = _cfg()
    return bool(c["addr"] and c["pw"])


def _decode(s: str | None) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


# Кап причины в ответе. Обрезка сама по себе законна, молчаливая обрезка — нет: полная
# причина всегда лежит в логе praxis-mail (`log.warning(..., exc_info=True)`), и ответ
# обязан сказать, что хвост там. Та же форма, что `agent._clip_reason`.
_REASON_CHARS = 400

# Ошибки, при которых сервер ЯВНО отказался принять письмо: адресат/отправитель отвергнут,
# команда не поддержана, DATA отбита кодом. Тут «не отправилось» — правда. Ошибки логина и
# соединения сюда НЕ входят: их и так ловит стадия, а слова «сервер отказался принять
# письмо» про неверный пароль были бы такой же неточностью, только с другой стороны.
_REFUSED = (
    smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
    smtplib.SMTPNotSupportedError, smtplib.SMTPDataError,
)


def _clip_reason(text: object, limit: int = _REASON_CHARS) -> str:
    """Причина целиком до границы слова; обрыв помечен явно."""
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    head = value[:limit]
    space = head.rfind(" ")
    if space > limit // 2:
        head = head[:space]
    return head.rstrip(" ,;:.—-") + f" […обрезано, полная причина в логе praxis-mail; кап {limit} симв.]"


def send(to: str, subject: str, body: str) -> str:
    """Отправить письмо. -> человекочитаемый статус.

    ⚠ Раньше ЛЮБОЕ исключение становилось строкой «Не отправилось: <тип>: <160 символов>».
    Две лжи в одной строке (та же болезнь, что чинили в `agent._direct_send_outcome` и
    `agent._clip_reason`): обрезка посреди слова, о которой нигде не сказано, и вердикт
    «не отправилось» там, где это НЕ известно. Обрыв соединения или таймаут ПОСЛЕ того,
    как письмо ушло в DATA, — это «не знаю, ушло ли»: сервер мог принять его и не успеть
    ответить. Повторить вслепую по такому вердикту = второе письмо живому человеку.
    """
    c = _cfg()
    if not configured():
        return "Почта не настроена (нет PRAXIS_EMAIL_ADDR/PASS)."
    to = (to or "").strip()
    if "@" not in to:
        return f"Не похоже на адрес: {to!r}."
    msg = EmailMessage()
    msg["From"] = c["addr"]
    msg["To"] = to
    msg["Subject"] = subject or "(без темы)"
    msg.set_content(body or "")
    # На какой стадии упало: до передачи письма отправки не было по построению.
    stage = "соединение с SMTP"
    try:
        ctx = ssl.create_default_context()
        with SMTP_SSL(c["smtp_host"], c["smtp_port"], context=ctx, timeout=30) as s:
            stage = "логин"
            s.login(c["addr"], c["pw"])
            stage = "передача письма"
            s.send_message(msg)
            # Сервер принял DATA. Всё, что упадёт дальше (QUIT/закрытие), доставки уже
            # не отменяет — и не имеет права выглядеть как «не отправилось».
            stage = "закрытие соединения"
    except Exception as e:
        log.warning("send упал на стадии «%s»", stage, exc_info=True)
        reason = _clip_reason(f"{type(e).__name__}: {e}")
        if stage == "закрытие соединения":
            return (f"Отправлено → {to}: «{msg['Subject']}» (сервер принял письмо). "
                    f"Соединение закрылось с ошибкой уже после этого ({reason}) — "
                    f"на доставку это не влияет, повторять НЕ надо.")
        if stage != "передача письма" or isinstance(e, _REFUSED):
            where = ("сервер отказался принять письмо" if isinstance(e, _REFUSED)
                     else f"упало на стадии «{stage}», до передачи письма")
            return f"Не отправилось ({where}): {reason}"
        # Связь оборвалась/истекло время УЖЕ в передаче: приёмку сервер не подтвердил, но
        # и отказа не дал. Это незнание, и выглядеть оно обязано как незнание.
        return (f"НЕ ЗНАЮ, ушло ли письмо → {to}: связь оборвалась на передаче ({reason}). "
                f"Сервер мог принять письмо и не успеть ответить. Прежде чем повторять — "
                f"загляни в «Отправленные» или спроси адресата: повтор может дать второе письмо.")
    return f"Отправлено → {to}: «{msg['Subject']}»."


def _extract_body(m) -> str:
    if m.is_multipart():
        for part in m.walk():
            disp = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", "ignore")
        return ""
    payload = m.get_payload(decode=True) or b""
    return payload.decode(m.get_content_charset() or "utf-8", "ignore")


def fetch(limit: int = 5, unseen_only: bool = False) -> list[dict]:
    """Последние письма из INBOX. -> [{from, subject, date, body, msg_id}], новые сверху.

    msg_id (Message-ID) нужен mailroom для дедупа входящих между поллами."""
    c = _cfg()
    if not configured():
        return []
    out: list[dict] = []
    M = None
    try:
        ctx = ssl.create_default_context()
        M = IMAP4_SSL(c["imap_host"], c["imap_port"], ssl_context=ctx)
        M.login(c["addr"], c["pw"])
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN" if unseen_only else "ALL")
        ids = (data[0].split() if (data and data[0]) else [])[-int(limit):]
        for i in reversed(ids):
            typ, msgdata = M.fetch(i, "(RFC822)")
            if typ != "OK" or not msgdata or not msgdata[0]:
                continue
            m = _email.message_from_bytes(msgdata[0][1])
            out.append({
                "from": _decode(m.get("From")),
                "subject": _decode(m.get("Subject")),
                "date": m.get("Date", ""),
                "body": (_extract_body(m) or "").strip()[:1500],
                "msg_id": (m.get("Message-ID") or m.get("Message-Id") or "").strip(),
            })
    except Exception:
        log.warning("fetch упал", exc_info=True)
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass
    return out
