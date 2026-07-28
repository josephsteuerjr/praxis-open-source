"""Small, dependency-free routing contract for Telegram forum topics.

Telegram authorization and room policy belong to the root peer.  Conversation state,
however, belongs to a concrete forum topic (``top_msg_id``).  Keeping those identities
separate prevents one topic from replacing another topic's wake/buffer while still
using the real peer id for Telethon delivery and access checks.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_STATE_SEP = "__topic__"
_SELECTOR_RE = re.compile(r"^(?P<peer>.+?)#(?:topic|thread):(?P<topic>[1-9]\d*)$", re.I)


@dataclass(frozen=True, slots=True)
class TopicRoute:
    """Root Telegram peer plus an optional forum/comment-thread root message id."""

    peer_id: str
    topic_id: int | None = None

    def __post_init__(self) -> None:
        peer = str(self.peer_id).strip()
        if not peer:
            raise ValueError("peer_id is empty")
        topic = None if self.topic_id is None else int(self.topic_id)
        if topic is not None and topic <= 0:
            raise ValueError("topic_id must be positive")
        object.__setattr__(self, "peer_id", peer)
        object.__setattr__(self, "topic_id", topic)

    @property
    def conversation_id(self) -> str:
        """Filesystem-safe, restart-stable key for all per-conversation state."""

        if self.topic_id is None:
            return self.peer_id
        return f"{self.peer_id}{_STATE_SEP}{self.topic_id}"

    @property
    def selector(self) -> str:
        """Human/tool-facing explicit route accepted by read/send helpers."""

        if self.topic_id is None:
            return self.peer_id
        return f"{self.peer_id}#topic:{self.topic_id}"


def is_topic_opener(message) -> bool:
    return type(getattr(message, "action", None)).__name__ == "MessageActionTopicCreate"


def topic_opener_title(message) -> str:
    """Return the title of a ``MessageActionTopicCreate`` service message.

    Kept structural so persisted routing and hermetic tests do not require a live
    Telethon import.  Telegram uses the service message's own id as the forum root.
    """

    action = getattr(message, "action", None)
    if not is_topic_opener(message):
        return ""
    return str(getattr(action, "title", "") or "").strip()


def thread_root_for_message(message) -> int | None:
    """Reply-chain root of one message — a *behavioural* identity, never a storage key.

    Conversation state belongs to the room (see ``route_for_message``); pacing and
    addressing still belong to the branch, so cooldown, supersede and reply targeting
    can stay per-thread while she reads one continuous room.  Telegram gives the root
    as ``reply_to_top_id`` from the second level down, and only ``reply_to_msg_id`` on
    the first reply; a message that answers nothing opens its own chain.
    """

    header = getattr(message, "reply_to", None)
    for owner, name in ((header, "reply_to_top_id"), (message, "reply_to_top_id"),
                        (header, "reply_to_msg_id"), (message, "reply_to_msg_id"),
                        (message, "id")):
        if owner is None:
            continue
        value = getattr(owner, name, None)
        if value is None or isinstance(value, bool):
            continue
        try:
            root = int(value)
        except (TypeError, ValueError):
            continue
        if root > 0:
            return root
    return None


def route_for_message(peer_id: str | int, message, *, is_private: bool = False,
                      is_forum: bool | None = None) -> TopicRoute:
    """Extract a stable topic root from a Telethon-like message.

    For an ordinary reply inside a topic, ``reply_to_msg_id`` is the immediate target
    and ``reply_to_top_id`` is the topic root.  For a reply to the topic opener itself,
    Telegram may omit ``reply_to_top_id``; then the immediate id is the root.  Private
    chats deliberately stay byte-compatible even if a synthetic test object happens to
    carry a similar header.

    ``is_forum`` is the *chat's* own nature, not the message's.  Only a real Telegram
    forum has topics that are separate places; in an ordinary supergroup a reply chain
    is a thread of one room, and splitting conversation state on it costs her the
    conversation: Telegram omits ``reply_to_top_id`` on the first reply, so the chain's
    root and its first answer file under the bare peer while every later reply files
    under ``__topic__<root>``.  One human conversation then holds two tails, and the
    message a thread is *about* is unreachable from that thread (observed 23.07.2026 in
    -1001240718803: «у меня в топик попал только твой ответ, без сообщения #93708»).
    ``None`` means the caller does not know, and keeps the pre-existing header-driven
    behaviour so persisted-route recovery and hermetic tests stay byte-compatible.
    """

    peer = str(peer_id)
    if is_private:
        return TopicRoute(peer)
    if is_forum is False:
        # An ordinary supergroup is one room.  Reply chains stay a *behavioural* identity
        # (wake, cooldown, reply addressing) computed per message; they never become a
        # storage key, so buffers, life spine, notes and promises keep one tail per room.
        return TopicRoute(peer)
    if is_topic_opener(message):
        try:
            opener_id = int(getattr(message, "id"))
        except (TypeError, ValueError):
            opener_id = 0
        if opener_id > 0:
            return TopicRoute(peer, opener_id)
    header = getattr(message, "reply_to", None)
    # Telethon exposes the header on ``message.reply_to``.  Small adapters and a
    # few older Message builds expose the projected fields directly instead, so
    # accept both shapes.  This is deliberately structural: importing Telethon
    # here would make persisted-route recovery depend on a live client package.
    top = getattr(header, "reply_to_top_id", None) if header is not None else None
    if top is None:
        top = getattr(message, "reply_to_top_id", None)
    forum = bool(getattr(header, "forum_topic", False)) if header is not None else False
    forum = forum or bool(getattr(message, "forum_topic", False))
    try:
        parsed_top = 0 if isinstance(top, bool) else int(top)
    except (TypeError, ValueError):
        parsed_top = 0
    if parsed_top <= 0 and forum:
        top = (getattr(header, "reply_to_msg_id", None) if header is not None
               else getattr(message, "reply_to_msg_id", None))
    # ``reply_to_top_id`` is also used for Telegram discussion/comment threads.  They
    # need the same isolation and delivery semantics even when forum_topic is absent.
    if top is None:
        return TopicRoute(peer)
    try:
        topic = 0 if isinstance(top, bool) else int(top)
    except (TypeError, ValueError):
        return TopicRoute(peer)
    return TopicRoute(peer, topic) if topic > 0 else TopicRoute(peer)


def parse_selector(value: str | int) -> tuple[str, int | None]:
    """Parse ``<peer>#topic:<top_msg_id>`` without changing ordinary references."""

    raw = str(value).strip()
    match = _SELECTOR_RE.fullmatch(raw)
    if not match:
        return raw, None
    return match.group("peer").strip(), int(match.group("topic"))


def measure_split(peer_id: str | int, messages) -> dict:
    """Датчик расщепления комнаты: во сколько ключей разговора разъезжается одна комната.

    Пункт 5 начинается отсюда, а не с правки маршрутизации. Утверждение «обычная
    супергруппа расщепляется на псевдо-темы» до сих пор было выводом из чтения кода;
    датчик делает его ИЗМЕРЕНИЕМ и, главное, оставляет постоянный регрессионный прибор:
    после починки то же число обязано схлопнуться, иначе починки не было.

    Идея подсмотрена у Арета (`deminded/mycelium-commons`): у них выгрузка нити дала 23
    сообщения против 142 в ленте того же диапазона — независимое полевое подтверждение
    того же поведения Telegram с другой стороны провода. Полноту не заверяет тот же
    орган, который выгружал.

    Считаем два маршрута на одних и тех же сообщениях: сегодняшний production
    (`is_forum=None`) и контрфактический (`is_forum=False`). Разница — это ровно то,
    что изменила бы правка, выраженное в числах, а не в аргументах. Ничего не пишет и
    ничего не меняет.
    """
    peer = str(peer_id)
    live: dict[str, int] = {}
    flat: dict[str, int] = {}
    total = 0
    for msg in messages or ():
        if getattr(msg, "id", None) is None:
            continue
        total += 1
        a = route_for_message(peer, msg, is_private=False).conversation_id
        b = route_for_message(peer, msg, is_private=False, is_forum=False).conversation_id
        live[a] = live.get(a, 0) + 1
        flat[b] = flat.get(b, 0) + 1
    largest = max(live.values()) if live else 0
    return {
        "peer_id": peer,
        "messages": total,
        "keys_live": len(live),
        "keys_if_not_forum": len(flat),
        "largest_branch": largest,
        # Доля комнаты, видимая с самого крупного ключа. Это и есть «23 из 142»:
        # столько она читает, просыпаясь на этой ветке.
        "largest_branch_share": (round(largest / total, 3) if total else 0.0),
        "singleton_keys": sum(1 for n in live.values() if n == 1),
        "top_keys": dict(sorted(live.items(), key=lambda kv: kv[1], reverse=True)[:10]),
    }


def route_from_conversation_id(value: str | int) -> TopicRoute:
    """Reverse a persisted state key; malformed suffixes remain ordinary peer ids."""

    raw = str(value).strip()
    peer, sep, suffix = raw.rpartition(_STATE_SEP)
    if sep and peer and suffix.isdigit() and int(suffix) > 0:
        return TopicRoute(peer, int(suffix))
    return TopicRoute(raw)


def route_from_reference(value: str | int) -> TopicRoute:
    """Accept an explicit selector, a persisted state key, or an ordinary peer.

    Human-facing tools use ``<peer>#topic:<id>`` while durable queues and buffers
    use :attr:`TopicRoute.conversation_id`.  Centralising that translation keeps a
    selector from ever reaching Telethon as though it were an entity username.
    """

    peer, topic = parse_selector(value)
    if topic is not None:
        return TopicRoute(peer, topic)
    return route_from_conversation_id(peer)
