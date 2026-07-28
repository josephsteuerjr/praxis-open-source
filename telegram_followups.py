"""Durable Telegram follow-up ledger: СЛЕД нити и ЗАКАЗАННЫЙ отчёт — разные вещи.

Каждое прямое сообщение кому-то, кроме Егора, оставляет здесь СЛЕД нити. След —
это её собственная память: через час, внутри пульса, Telegram отключён, и эта
запись — единственное свидетельство, что по этому поводу она уже говорила.
След Егору НЕ уходит никогда и гаснет по возрасту (`TRACE_TTL_SEC`).

ОТЧЁТ Егору («ответили — вот что») — отдельное свойство нити, `notify_owner`.
Его включает только заказчик: Егор словами («сообщи, когда ответит») или она
сама (`set_notice`, тул `watch_reply`). Заказанное обязательство по возрасту не
гаснет — потерять её намерение молча хуже, чем выполнить его поздно.

⚠ До 27.07.2026 этот докстринг утверждал обратное: «the owner need not remember
to say «сообщи, когда ответит»: that follow-up is the useful default». Дефолт
назначили в коде, Егора не спросили, в манифесте рельсов не назвали. Замер на
проде: 17 писем ему за две недели, из 11 owner-записей `wants_followup` не
проходит НИ ОДНА — он не заказал ни одного отчёта; шесть нитей она погасила
руками. Финальная капля — 27.07 02:32: Егор ответил ей реплаем в AbstractDL, и
его же реплика уехала ему в ЛС под заголовком «AbstractDL Chat ответил(а)».
Поэтому ответ самого Егора закрывает нить (она должна знать, что ответ пришёл),
но письмом ему не становится никогда.

JSON остаётся источником правды через рестарты.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path


BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STATE_PATH = BASE / "memory" / ".state" / "telegram_followups.json"
_LOCK = threading.RLock()
_VERSION = 1
_SETTLED_KEEP = 500

CONTEXT_LIMIT = 12          # сколько нитей влезает в ленту пульса


def _trace_ttl() -> float:
    """Срок жизни СЛЕДА нити, оставшейся без ответа.

    try/except обязателен: `telegram_followups` импортирует `mtproto_runner`, и
    сломанное значение в `.deploy.env` иначе роняет импорт вместе со всем
    раннером — ровно грабли `forge.py:128`. Битая переменная обязана пережиться
    молчаливым возвратом дефолта, а не падением контейнера.
    """
    try:
        value = float((os.environ.get("PRAXIS_FOLLOWUP_TRACE_TTL_SEC") or "").strip())
    except Exception:
        return 259200.0
    return value if value > 0 else 259200.0


# 72ч. Читается ОДИН раз на импорте — смена переменной требует рестарта раннера;
# это названо в рельсе `followup_notice`, чтобы предел не был молчаливым.
TRACE_TTL_SEC = _trace_ttl()

_EXPIRY_PATTERNS = (
    re.compile(
        r"(?is)(?:действ\w*|актив\w*|истеч\w*|протух\w*)"
        r".{0,40}?(\d+(?:[.,]\d+)?)\s*"
        r"(минут\w*|час\w*|дн(?:я|ей|и)?)"
    ),
    re.compile(
        r"(?is)(?:valid|active|expires?)\s+(?:for|in)\s+"
        r"(\d+(?:[.,]\d+)?)\s*(minutes?|hours?|days?)"
    ),
)


_FOLLOWUP_PATTERNS = (
    re.compile(
        r"(?is)(?:сообщи|скажи|дай\s+знать|отпишись|уведом\w*)"
        r".{0,100}(?:ответ\w*|отпиш\w*|реакци\w*)"
    ),
    re.compile(
        r"(?is)(?:когда|если).{0,60}(?:ответ\w*|отпиш\w*)"
        r".{0,80}(?:сообщи|скажи|дай\s+знать|отпишись|уведом\w*)"
    ),
    re.compile(r"(?is)(?:let\s+me\s+know|tell\s+me).{0,80}(?:repl\w*|respond\w*)"),
    re.compile(r"(?is)(?:when|if).{0,60}(?:they|he|she).{0,30}(?:repl\w*|respond\w*)"),
)

_NO_FOLLOWUP_PATTERNS = (
    re.compile(
        r"(?is)(?:не\s+(?:надо|нужно)?\s*(?:сообщать|отписываться|следить)|"
        r"без\s+(?:отч[её]та|follow[- ]?up))"
    ),
    re.compile(r"(?is)(?:don'?t|do\s+not)\s+(?:tell|notify|track|follow\s*up)"),
)


def wants_followup(text: str) -> bool:
    """Whether the owner's wording explicitly asks to hear about the answer."""
    value = str(text or "").strip()
    return bool(value and any(p.search(value) for p in _FOLLOWUP_PATTERNS))


def suppresses_followup(text: str) -> bool:
    """Whether the owner explicitly opted out of automatic answer tracking."""
    value = str(text or "").strip()
    return bool(value and any(pattern.search(value) for pattern in _NO_FOLLOWUP_PATTERNS))


def explicit_expiry_seconds(text: str) -> float | None:
    """Return a duration only when the message explicitly states its validity."""
    value = str(text or "")
    for pattern in _EXPIRY_PATTERNS:
        match = pattern.search(value)
        if not match:
            continue
        amount = float(match.group(1).replace(",", "."))
        unit = match.group(2).lower()
        if unit.startswith(("минут", "minute")):
            return amount * 60
        if unit.startswith(("час", "hour")):
            return amount * 3600
        return amount * 86400
    return None


def request_from_owner_buffer(lines, *, limit: int = 12,
                              explicit_only: bool = True) -> str:
    """Return the latest explicit owner request from a live owner-DM buffer.

    The active tool history intentionally excludes the message currently being
    processed.  The MTProto buffer already contains it, so it is the reliable
    source at send time.  Praxis-authored lines are never treated as authority.
    """
    for raw in reversed(list(lines or ())[-max(1, int(limit)):]):
        line = str(raw or "").strip()
        if not line or line.casefold().startswith("praxis:"):
            continue
        body = line.split(":", 1)[1].strip() if ":" in line else line
        if suppresses_followup(body):
            return ""
        return body if (not explicit_only or wants_followup(body)) else ""
    return ""


def _empty() -> dict:
    return {"version": _VERSION, "items": []}


def _load(path: Path = STATE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return _empty()
    return {"version": _VERSION, "items": [x for x in data["items"] if isinstance(x, dict)]}


def _save(data: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _owes_owner_notice(item: dict) -> bool:
    """Нить, по которой Егору ЕЩЁ должны письмо.

    Один предикат на три места (`_compact`, `pending_notifications`, `context`):
    разъехавшись, они дали бы «отвечено, отчёта не будет» — запись, защищённую от
    вытеснения навсегда, и леджер без границы.
    """
    return bool(
        item.get("status") == "answered"
        and item.get("notify_owner")
        and not item.get("notified_at")
        and not item.get("notice_skipped")
    )


def _compact(items: list[dict], *, settled_keep: int = _SETTLED_KEEP) -> list[dict]:
    """Bound settled history without ever dropping an unresolved obligation."""
    protected = {
        index for index, item in enumerate(items)
        if item.get("status") == "pending" or _owes_owner_notice(item)
    }
    settled = [index for index in range(len(items)) if index not in protected]
    keep_settled = max(0, int(settled_keep) - len(protected))
    selected = protected | set(settled[-keep_settled:] if keep_settled else ())
    return [item for index, item in enumerate(items) if index in selected]


def _expire_pending(items: list[dict], *, now: float) -> bool:
    changed = False
    for item in items:
        expires_at = item.get("expires_at")
        if item.get("status") != "pending" or not isinstance(expires_at, (int, float)):
            continue
        if float(expires_at) <= now:
            item["status"] = "expired"
            item["expired_at"] = now
            changed = True
    return changed


class FollowUpLedger:
    """Small synchronized facade, with an injectable path for tests."""

    def __init__(self, path: str | Path = STATE_PATH):
        self.path = Path(path)

    def create(
        self,
        *,
        target_ref: str,
        target_label: str,
        target_peer_id: str | int,
        target_user_id: str | int | None,
        sent_message_id: str | int,
        request_text: str,
        sent_at: float | None = None,
        idempotency_key: str = "",
        notify_owner: bool = False,
        notice_source: str = "",
        sent_excerpt: str = "",
    ) -> dict:
        now = float(sent_at if sent_at is not None else time.time())
        receipt = str(idempotency_key or "").strip()
        peer = str(target_peer_id)
        sent_mid = int(sent_message_id)
        expiry_seconds = explicit_expiry_seconds(request_text)
        item = {
            "id": "tgfu_" + uuid.uuid4().hex[:16],
            "status": "pending",
            "created_at": now,
            "target_ref": str(target_ref),
            "target_label": str(target_label),
            "target_peer_id": peer,
            "target_user_id": (str(target_user_id) if target_user_id is not None else None),
            "sent_message_id": sent_mid,
            "sent_at": now,
            "request_text": str(request_text or "")[:1000],
            # ЧТО она отправила, а не только ПОЧЕМУ. 27.07 02:29 она отправила
            # поправку второй раз реплаем на собственное #94144: id своей реплики
            # она взяла отсюда, а текста её не было нигде — в леджере не было поля.
            "sent_excerpt": str(sent_excerpt or "")[:500],
            # Отчёт Егору — не свойство механизма, а решение заказчика.
            "notify_owner": bool(notify_owner),
            "notice_source": str(notice_source or "")[:40],
            "notice_skipped": "",
            # У СЛЕДА срок жизни есть и он назван; у ЗАКАЗАННОГО обязательства его
            # нет — заказ по возрасту не гаснет (потерять её намерение молча хуже,
            # чем выполнить поздно). Явно названный в тексте срок бьёт оба случая.
            "expires_at": (now + expiry_seconds if expiry_seconds is not None
                           else (None if notify_owner else now + TRACE_TTL_SEC)),
            "response": None,
            "notified_at": None,
            "idempotency_key": receipt,
        }
        with _LOCK:
            state = _load(self.path)
            for existing in state["items"]:
                try:
                    same_message = (
                        str(existing.get("target_peer_id")) == peer
                        and int(existing.get("sent_message_id")) == sent_mid
                    )
                except (TypeError, ValueError):
                    same_message = False
                if ((receipt and str(existing.get("idempotency_key") or "") == receipt)
                        or same_message):
                    return dict(existing)
            state["items"].append(item)
            state["items"] = _compact(state["items"])
            _save(state, self.path)
        return dict(item)

    def snapshot(self, *, status: str | None = None) -> list[dict]:
        """Read the persisted ledger without expiring or rewriting any entries."""
        with _LOCK:
            items = _load(self.path)["items"]
        if status is not None:
            items = [x for x in items if x.get("status") == status]
        return [dict(x) for x in items]

    def list(self, *, status: str | None = None) -> list[dict]:
        with _LOCK:
            state = _load(self.path)
            if _expire_pending(state["items"], now=time.time()):
                state["items"] = _compact(state["items"])
                _save(state, self.path)
            items = state["items"]
        if status is not None:
            items = [x for x in items if x.get("status") == status]
        return [dict(x) for x in items]

    def observe_incoming(
        self,
        *,
        peer_id: str | int,
        sender_id: str | int | None,
        message_id: str | int,
        text: str,
        reply_to_message_id: str | int | None = None,
        received_at: float | None = None,
        sender_name: str = "",
        sender_is_owner: bool = False,
    ) -> dict | None:
        """Resolve one pending follow-up from a concrete incoming message.

        In a DM, a later incoming message from the addressed user is an answer.
        In a group/channel, only a Telegram reply to the exact sent message is
        accepted; unrelated room traffic must not produce a false notification.

        `sender_name` и `sender_is_owner` приходят снаружи: модуль не знает
        OWNER_ID и не должен его знать, а раннер имя ответившего и так печатает
        в лог («получен ответ #94244 от Yegor Kosyrev») — до 27.07 оно просто не
        доезжало ни до письма, ни до записи.
        """
        now = float(received_at if received_at is not None else time.time())
        peer = str(peer_id)
        sender = str(sender_id) if sender_id is not None else None
        mid = int(message_id)
        reply_mid = int(reply_to_message_id) if reply_to_message_id is not None else None
        with _LOCK:
            state = _load(self.path)
            expired = _expire_pending(state["items"], now=now)
            # Telegram may replay an update after reconnect.  One concrete incoming
            # message must never settle a second obligation on the next replay.
            for existing in state["items"]:
                response = existing.get("response") or {}
                try:
                    already_observed = (
                        str(response.get("peer_id")) == peer
                        and int(response.get("message_id")) == mid
                    )
                except (TypeError, ValueError):
                    already_observed = False
                if already_observed:
                    return None
            candidates = []
            for index, item in enumerate(state["items"]):
                if item.get("status") != "pending" or str(item.get("target_peer_id")) != peer:
                    continue
                sent_mid = int(item.get("sent_message_id") or 0)
                target_user = item.get("target_user_id")
                is_dm = bool(target_user)
                if is_dm:
                    if sender != str(target_user) or mid <= sent_mid:
                        continue
                    exact_reply = int(reply_mid == sent_mid)
                elif reply_mid != sent_mid:
                    continue
                else:
                    exact_reply = 1
                candidates.append((exact_reply, float(item.get("sent_at") or 0), index))
            if not candidates:
                if expired:
                    state["items"] = _compact(state["items"])
                    _save(state, self.path)
                return None
            _, _, index = max(candidates)
            item = state["items"][index]
            item["status"] = "answered"
            item["response"] = {
                "peer_id": peer,
                "sender_id": sender,
                # Отвечает ЧЕЛОВЕК, а не чат: заголовок письма брал `target_label`
                # и выходило «AbstractDL Chat ответил(а)». Пусто здесь — честное
                # «не знаю кто», выдумывать метку чата вместо имени нельзя.
                "sender_name": str(sender_name or "")[:120],
                "message_id": mid,
                "reply_to_message_id": reply_mid,
                "text": str(text or "")[:4000],
                "received_at": now,
            }
            if sender_is_owner:
                # 27.07 02:32: Егор ответил ей реплаем в AbstractDL — и его же слова
                # уехали ему в ЛС под заголовком «AbstractDL Chat ответил(а)». Нить
                # ответом ЗАКРЫТА (она должна знать, что ответ пришёл), но пересылать
                # человеку его собственную реплику нечего и никогда не будет.
                # Пишем здесь, а не в тике доставки: факт истинен ровно сейчас, без
                # окна между матчем и отправкой и без знания OWNER_ID внутри модуля.
                item["notice_skipped"] = "ответил сам Егор"
                item["notice_skipped_at"] = now
            _save(state, self.path)
            return dict(item)

    def pending_notifications(self) -> list[dict]:
        """Только ЗАКАЗАННЫЕ отчёты: след нити — её память, а не почта Егору.

        До 27.07 здесь стояло «любая отвеченная нить» — отсюда и брались 17 писем
        ему за две недели, ни одного из которых он не просил.
        """
        return [x for x in self.list(status="answered") if _owes_owner_notice(x)]

    def cancel(self, followup_id: str) -> bool:
        with _LOCK:
            state = _load(self.path)
            changed = False
            for item in state["items"]:
                if item.get("id") == followup_id and item.get("status") in ("pending", "answered"):
                    item["status"] = "cancelled"
                    item["cancelled_at"] = time.time()
                    changed = True
                    break
            if changed:
                state["items"] = _compact(state["items"])
                _save(state, self.path)
            return changed

    def set_notice(self, followup_id: str, on: bool, *, source: str = "praxis") -> dict | None:
        """Включить/выключить отчёт Егору по конкретной нити.

        Её рычаг: раньше она могла только ОТМЕНЯТЬ чужое решение (`cancel`) — шесть
        раз и отменяла. Теперь она автор нити, а не проситель: завести отчёт там,
        где он нужен ей, и снять там, где не нужен. Возвращает None, если нити нет
        или она уже закрыта — молча «получилось» отвечать нельзя.
        """
        with _LOCK:
            state = _load(self.path)
            for item in state["items"]:
                if item.get("id") != followup_id:
                    continue
                if item.get("status") not in ("pending", "answered"):
                    return None
                item["notify_owner"] = bool(on)
                item["notice_source"] = (str(source or "")[:40] if on else "")
                if on:
                    item["notice_skipped"] = ""
                    # Обязательство, в отличие от следа, по возрасту не гаснет.
                    # Явно названный в тексте срок («код действует 30 минут») —
                    # единственное, что остаётся сильнее заказа.
                    if explicit_expiry_seconds(item.get("request_text") or "") is None:
                        item["expires_at"] = None
                _save(state, self.path)
                return dict(item)
            return None

    def mark_notified(self, followup_id: str, *, at: float | None = None) -> bool:
        with _LOCK:
            state = _load(self.path)
            changed = False
            for item in state["items"]:
                if (item.get("id") == followup_id
                        and item.get("status") == "answered"
                        and isinstance(item.get("response"), dict)
                        and not item.get("notified_at")):
                    item["notified_at"] = float(at if at is not None else time.time())
                    item["status"] = "notified"
                    changed = True
                    break
            if changed:
                state["items"] = _compact(state["items"])
                _save(state, self.path)
            return changed

    def context(self, *, limit: int = CONTEXT_LIMIT) -> str:
        """Compact factual context for Praxis's hourly social pulse."""
        all_items = self.list()
        cap = max(1, int(limit))
        unresolved = [
            item for item in all_items
            if item.get("status") == "pending" or _owes_owner_notice(item)
        ]
        # Old obligations must not disappear behind a stream of settled history.
        # Fill any remaining budget with the newest settled facts.
        items = unresolved[:cap]
        if len(items) < cap:
            unresolved_ids = {str(item.get("id") or "") for item in unresolved}
            settled = [
                item for item in all_items
                if str(item.get("id") or "") not in unresolved_ids
                and item.get("status") != "expired"
            ]
            items.extend(settled[-(cap - len(items)):])
        if not items:
            return "Нет активных Telegram follow-up нитей."
        rows = []
        now = time.time()
        for item in items:
            age_h = max(0.0, (now - float(item.get("sent_at") or now)) / 3600)
            row = (
                f"- {item.get('id')} [{item.get('status')}] {item.get('target_label')} "
                f"peer={item.get('target_peer_id')} message_id={item.get('sent_message_id')} "
                f"{age_h:.1f}ч назад"
            )
            # Inside the hourly pulse the Telegram client is disconnected, so this
            # ledger is the only record of what a thread already covered.  Without it
            # a wake re-derives "I should tell them X" and sends the same thing again.
            # Поэтому гист — это в первую очередь ЕЁ отправленный текст: `request_text`
            # у owner-веток содержит слова Егора («им отправить»), и анти-повтор на
            # них не работает вовсе. `sent_text` читаем тоже: соседняя бригада вводит
            # то же поле под своим именем — пусть смена имени не гасит след молча.
            gist = str(
                item.get("sent_excerpt") or item.get("sent_text")
                or item.get("request_text") or ""
            ).strip()
            if gist:
                row += f"; повод отправки: {gist[:240]}"
            # Правило 2 в строке, которую она читает: кто заказал отчёт и уйдёт ли он.
            if item.get("notify_owner"):
                source = str(item.get("notice_source") or "").strip()
                row += "; отчёт Егору: " + {
                    "owner": "заказан им словами",
                    "praxis": "мой — я включила сама",
                }.get(source, f"заказан ({source})" if source else "заказан")
            else:
                row += "; отчёт Егору: нет — это мой след"
            skipped = str(item.get("notice_skipped") or "").strip()
            if skipped:
                row += f"; письма не будет: {skipped}"
            response = item.get("response") or {}
            if response:
                row += (
                    f"; ответ message_id={response.get('message_id')}: "
                    f"{str(response.get('text') or '')[:240]}"
                )
            rows.append(row)
        # Все четыре предела названы здесь и только здесь: раннер отдаёт текст
        # целиком именно потому, что режет и объявляет их леджер (правило 2 —
        # молчаливого усечения быть не должно ни на одном слое).
        return (
            f"Telegram follow-up ledger (факты; показываю {len(items)} нитей из "
            f"{len(all_items)}, не больше {cap}; тексты режу до 240 симв. в строке "
            f"и до 500 в самой записи; след без ответа гаснет через "
            f"{TRACE_TTL_SEC / 3600:.0f}ч, заказанный отчёт не гаснет никогда):\n"
            + "\n".join(rows)
        )


LEDGER = FollowUpLedger()
