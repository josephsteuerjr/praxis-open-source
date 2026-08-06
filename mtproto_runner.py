"""Praxis как пользователь Telegram (MTProto/Telethon) — перцепционная петля.

Входящее НЕ дёргает ответ по сообщению. Оно кладётся в per-chat буфер и взводит дебаунс;
всплеск склеивается в одну ситуацию. По тишине (дебаунс) с учётом кулдауна — ход ГОЛОСА
(PASS 8.1: привратник-perceive снесён, большая модель работает и в группе). В группе
голос получает presence-фрейм: тишина — его полноценный выбор, сентинел [молчу]; раннер
молчание не отправляет. Говорит в разговор (`send_message`), не цитатой.

По умолчанию группу будит только прямое обращение (@упоминание или реплай ей). Корневой
room-profile может явно включить engagement=reflective: тогда meaningful-фон склеивается
дебаунсом в один ambient-проход, а address всегда имеет приоритет. Топики не смешиваются.
Кост-гард группы: кулдаун (300с дефолт) + reflex + [молчу]. Вся фоновая
периодика (буферы/расписание/«сон»/сердцебиение) — один тик `_clock()`, «её часы».

Перед запуском нужен логин: python mtproto_login.py (см. README).
"""
from __future__ import annotations
import asyncio, datetime, hashlib, inspect, json, logging, mimetypes, os, re, tempfile, time, types
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

from telethon import TelegramClient, events
from dotenv import load_dotenv

import agent
import bufstore
import context_envelope
import formation
import group_context
import llm
import memory_life
import media as media_core
import owner_delivery
import perception
import selfdev
import reflex
import rooms
import social
import social_pulse
import telegram_contacts
import telegram_confirmation
import telegram_followups
import telegram_membership
import telegram_outbox
import telegram_registry
import telegram_routes
import telegram_topics
import unanswered
import workshop

try:  # герметичность: любой тест-запуск (unittest/pytest/PRAXIS_TEST) не читает боевой .env
    from _sandbox import _looks_like_test_run as _under_tests
except Exception:
    _under_tests = lambda: bool(os.environ.get("PRAXIS_TEST"))
if not _under_tests():
    load_dotenv(override=True)  # .env-тюнинг применяется и на простом рестарте (§9 пакета 2)
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION = os.getenv("TELEGRAM_SESSION", "praxis")
OWNER_ID = int(os.getenv("PRAXIS_OWNER_ID", "0"))
CONSOLIDATE_HOURS = float(os.getenv("PRAXIS_CONSOLIDATE_HOURS", "24"))  # ≤0 — сон выключен
# PASS 10.2: сон — по будильнику (sleep.due: окно PRAXIS_SLEEP_WINDOW + persist-метка),
# тик проверки раз в 30 мин; CONSOLIDATE_HOURS остался выключателем, не секундомером.
SLEEP_CHECK_SEC = float(os.getenv("PRAXIS_SLEEP_CHECK_SEC", "1800"))
_STARTED_AT = time.time()
# Legacy selector heartbeat is kept as a callable compatibility helper, but the clock has
# one autonomous wake source: durable social_pulse.  Two hourly jobs used to race and could
# open two independent task windows for the same hour.
HEARTBEAT_HOURS = float(os.getenv("PRAXIS_HEARTBEAT_HOURS", "0"))
DEBOUNCE_SEC = float(os.getenv("PRAXIS_DEBOUNCE_SEC", "4"))
COOLDOWN_DM = float(os.getenv("PRAXIS_COOLDOWN_DM", "8"))
COOLDOWN_GROUP = float(os.getenv("PRAXIS_COOLDOWN_GROUP", "300"))  # PASS 8.1: кост-гард групп
COOLDOWN_ADDRESSED = float(os.getenv("PRAXIS_COOLDOWN_ADDRESSED", "180"))
LAST_N = int(os.getenv("PRAXIS_LAST_N", "50"))
# Бюджет ленты для комнаты, которую Telegram НЕ делил форумными топиками (группа
# обсуждения канала, обычная супергруппа). Там лента должна покрывать разговор МЕСТА, а
# не хвост одной цепочки ответов: в AbstractDL при 14 000 символов в кадр влезало ~45
# строк, и все из общего чата — живое обсуждение под постом канала не попадало вовсе.
# Связывает именно бюджет символов, а не потолок сообщений (тот был 200 при 45 строках).
WHOLE_ROOM_CONTEXT_CHARS = int(os.getenv("PRAXIS_WHOLE_ROOM_CONTEXT_CHARS", "32000"))
# PASS 19: LAST_N остаётся legacy/ручкой fetch_context; живой hot-layer дышит 50↔100.
COMPACT_MARGIN = int(os.getenv("PRAXIS_COMPACT_MARGIN", "20"))
# Deep-room profiles may ask for a 500-message hot view.  Ordinary rooms still slice
# at memory_life.HOT_HARD_HI; the extra retention is inert until their root profile opts in.
BUF_MAXLEN = max(int(os.getenv("PRAXIS_BUF_MAXLEN", "160")),
                 memory_life.HOT_HARD_HI + 25, group_context.MAX_HOT + 25)
SCHED_TICK = float(os.getenv("PRAXIS_SCHED_TICK", "60"))  # период проверки due-задач (локально, без модели)
CLOCK_TICK = float(os.getenv("PRAXIS_CLOCK_TICK", "2"))   # PASS 4: удар «её часов» (и период флаша буферов)
# Кому позволен стоп-кран (Егор + хосты чужих пространств, напр. Хоуп). Owner всегда можно.
PANIC_IDS = {int(x) for x in os.getenv("PRAXIS_PANIC_IDS", "").replace(" ", "").split(",")
             if x.lstrip("-").isdigit()}
# PASS 10.7: новичок-протокол — сколько сообщений предыстории читать при входе в группу
BACKFILL_N = int(os.getenv("PRAXIS_BACKFILL_N", "200"))
# PASS 9.0: непрерывность приёма — сообщение в даунтайм не теряется молча (кейс Евгения).
MISSED_DM_HOURS = float(os.getenv("PRAXIS_MISSED_DM_HOURS", "48"))
MISSED_SWEEP_DELAY = float(os.getenv("PRAXIS_MISSED_SWEEP_DELAY", "25"))  # после полного подъёма
SEEN_IDS_KEEP = 300  # сторож дедупа msg_id на чат (catch_up может доиграть уже виденное)
# PASS 9.2: как часто разбирать очередь иммунитета (0 — выключить заботу)
IMMUNE_MINUTES = float(os.getenv("PRAXIS_IMMUNE_MINUTES", "15"))
# PASS 12.1: как часто проверять простаивающие важные сообщения в окно отсутствия владельца
# (тик локален и дёшев: без активного окна absence.due() сразу пуст; 0 — выключить заботу).
ABSENCE_TICK = float(os.getenv("PRAXIS_ABSENCE_TICK", "300"))
# PASS 13.1 compatibility: timeout for an explicitly announced transport reconnect.
# PASS 24 task runs never use this state: Telethon stays connected while they execute.
RECONNECT_TIMEOUT_SEC = float(os.getenv("PRAXIS_RECONNECT_TIMEOUT_SEC", "1800"))
# HOTFIX 07.07: сколько ждать фонового прогрева кэша диалогов при ХОЛОДНОМ резолве по имени.
# Свой бюджет, не доля чужого: раньше холодный iter_dialogs() платился из общих 30 секунд
# _sync_send_message — первый «напиши Евгению» после рестарта умирал TimeoutError'ом.
DIALOG_WARMUP_WAIT_SEC = float(os.getenv("PRAXIS_DIALOG_WARMUP_WAIT_SEC", "90"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("praxis-mt")
import logsink  # noqa: E402  (после basicConfig: файловый хвост для панели)
logsink.attach("praxis")

# catch_up=True (PASS 9.0): пропущенные в даунтайм апдейты доезжают после реконнекта —
# сообщение, пришедшее между рестартами, больше не теряется молча.
client = TelegramClient(SESSION, API_ID, API_HASH, catch_up=True)
_self_id = None

# HOTFIX 07.07 (вечер): луп main() — захватывается при старте, ЕДИНСТВЕННЫЙ живой.
# НЕЛЬЗЯ брать client.loop из воркер-треда: с telethon>=1.39 это динамический
# get_running_loop(), который в треде без лупа МОЛЧА создаёт новый мёртвый луп
# (new_event_loop + set_event_loop) — run_coroutine_threadsafe планирует корутину
# в никуда, и .result() честно выедает весь таймаут-бюджет. Так 07.07 умерли ВСЕ
# sync-тулы Telethon разом: send_message (120с TimeoutError), get_id («[не нашла]» =
# скрытый 20с-таймаут), read_chat, search_chats... — при живом приёме апдейтов
# (он на настоящем лупе). В свежем клиенте (проба) те же вызовы шли за 0.2с.
_LOOP: asyncio.AbstractEventLoop | None = None
_TELEGRAM_DISPATCHER: telegram_registry.TelegramAccountDispatcher | None = None
_TELEGRAM_CONFIRMATIONS: telegram_confirmation.ConfirmationStore | None = None
_TELEGRAM_CRITICAL_CHALLENGES: telegram_confirmation.CriticalChallengeStore | None = None


def _main_loop() -> asyncio.AbstractEventLoop:
    """Луп, на котором реально живёт Telethon. Для sync-обёрток тулов (воркер-треды)."""
    if _LOOP is None:
        raise RuntimeError("луп Telethon ещё не захвачен (main() не стартовал)")
    return _LOOP


def _threadsafe_result(coro_factory, timeout: float):
    """Run on Telethon's captured loop without leaking or ghosting a coroutine.

    Resolve the loop before creating the coroutine. If the synchronous caller times out,
    propagate cancellation so a tool cannot report failure and then send something later.
    """
    loop = _main_loop()
    coro = coro_factory()
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:
        coro.close()
        raise
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise


# PASS 13.1: run_until_disconnected() в main() возвращается на ЛЮБОЙ disconnect(), намеренный
# или нет — раньше это было неразличимо, и любой намеренный disconnect (_task_window) мог
# ронять процесс на удаче тайминга. Явные флаги намерения — см. _supervise_connection().
_EXPECT_DISCONNECT = asyncio.Event()  # legacy/explicit transport recovery; never a work mode
_SHUTDOWN = asyncio.Event()           # кто-то хочет, чтобы main() реально закончился (_control_once)
# Она ОДНА: единый когнитивный проход за раз. Живой ход по чату (_run_pass) и автономное
# окно (_task_window) держат этот замок, поэтому параллельных «я» не бывает — ни два чата
# разом, ни окно поверх живого разговора. Окно, увидев занятость, откладывается до следующего
# тика (не блокирует часы); живой ход дожидается (при закрытом на окно Telethon это и не
# наступает). Устраняет диссоциацию, где пульс и разговор одновременно били в мозг.
class _OneMind(asyncio.Lock):
    """Её единый замок плюс один честный вопрос: «возьмётся ли он сейчас, не засыпая?».

    Сам asyncio.Lock такого вопроса не отвечает: ``locked()`` говорит только про владельца
    и ничего — про очередь, а между ``release()`` и тем, как очередной ждущий реально
    возьмёт замок, ``locked()`` уже False, тогда как ``acquire()`` встанет в хвост.
    Заботам, которых зовут прямо из часов (отсутствие, ночь), засыпать нельзя — с ними
    встанет весь тик.

    Считаем СВОЮ глубину очереди вокруг родного ``acquire()``: поведение замка не
    трогаем, добавляем только наблюдение. Так ответ не зависит от приватных полей и не
    может тихо испортиться на другой версии Python.
    """

    def __init__(self) -> None:
        super().__init__()
        self._parked = 0

    async def acquire(self) -> bool:  # type: ignore[override]
        self._parked += 1
        try:
            return await super().acquire()
        finally:
            self._parked -= 1

    def free_now(self) -> bool:
        """True — захват вернётся не отдав управление (никто не держит и не ждёт)."""
        return not self.locked() and self._parked == 0


_ONE_MIND = _OneMind()
_buf: dict[str, deque] = defaultdict(lambda: deque(maxlen=BUF_MAXLEN))
_meta: dict[str, dict] = {}


@dataclass(frozen=True)
class GroupWake:
    """Immutable provenance for an addressed or reflective group pass."""

    message_id: int | None
    message_ts: float
    kind: str
    speaker: str
    sender_id: int | None
    owner: bool
    known: bool
    family: bool
    context_snapshot: str
    reply_targets_snapshot: tuple
    media_snapshot: tuple[media_core.MediaRef, ...]
    addressed: bool = True
    query: str = ""
    # Та же лента, но с авторством: ((её ли это строка, строка для роли), …).
    # Замораживается ВМЕСТЕ с текстом, одним чтением архива. Пустой кортеж —
    # честное «авторства не знаю» (запасной путь по строковому буферу).
    turns_snapshot: tuple = ()


_group_wakes: dict[str, GroupWake] = {}
_seen_ids: dict[str, deque] = defaultdict(lambda: deque(maxlen=SEEN_IDS_KEEP))  # 9.0: дедуп catch_up
_recent_msgs: dict[str, deque] = defaultdict(lambda: deque(maxlen=12))  # 15: (msg_id, автор, гист) для ОТВЕТ->#id
# PASS 16.2: недавние отправители на чат — (ts, имя, id). Честный источник для get_id
# в классе «айди спамера»: отправитель БЫЛ в апдейте, но резолв по диалогам/участникам
# его не видит (забанен/ушёл). RAM: с рестарта; глубже по времени — тул read_log.
_recent_senders: dict[str, deque] = defaultdict(lambda: deque(maxlen=40))
_BOUNDARY_REPLY_TTL_S = 6 * 60 * 60
# chat -> (sent message id, provocateur sender id, timestamp). A direct reply to an explicit
# boundary is context, but does not earn another defensive pass.
_boundary_replies: dict[str, deque] = defaultdict(lambda: deque(maxlen=8))
_pending_media: dict[str, deque] = defaultdict(
    lambda: deque(maxlen=max(2, media_core.TURN_MEDIA_MAX * 2)))
_missed: dict[str, float] = {}            # 9.0: chat_id -> возраст (ч) для честной [missed]-метки
_chat_desc: dict[str, dict] = {}          # §2: ленивая осознанность чата (title/kind/size), кэш на чат
_debounce: dict[str, asyncio.Task] = {}
_deferred: dict[str, asyncio.Task] = {}   # §8: отложенный проход на остаток кулдауна
_last_pass: dict[str, float] = defaultdict(float)
_passing: set[str] = set()
_dm_rearm: set[str] = set()  # PASS 21: ЛС-триггеры, сгоревшие об идущий ход, — перевзвести
_supersede_gen: dict[str, int] = {}  # PASS 29: бамп поколения == создан новый пасс-преемник (см. _arm)
_buf_dirty: set[str] = set()              # §1: чаты с несохранённым буфером
_compacting: set[str] = set()             # §6: чтобы не свернуть один чат дважды разом
_MEDIA_SPOOL: media_core.MediaSpool | None = None
_MEDIA_SENDING: set[str] = set()
_MEDIA_ACCEPTED: dict[str, dict] = {}
_TEXT_SENDING: set[str] = set()
_MEMBERSHIP_LEDGER: telegram_membership.MembershipLedger | None = None
_DIRECT_OUTBOX: telegram_outbox.TelegramOutbox | None = None
_DIRECT_OUTBOX_RECONCILED: set[str] = set()
_SOCIAL_PULSE_TASK: asyncio.Task | None = None
_FORGE_EVENTS_TASK: asyncio.Task | None = None   # PASS 30 Этап 1: single-flight насоса событий
_SLEEP_TASK: asyncio.Task | None = None          # 26.07: ночь ждёт замок, но не в часах


def _one_mind_is_free() -> bool:
    """Возьмётся ли замок БЕЗ ожидания. False — кто-то держит его или уже стоит в очереди.

    Одного ``locked()`` мало, и это не педантизм: у asyncio.Lock честная очередь, и между
    ``release()`` и тем, как очередной ждущий реально возьмёт замок, ``locked()`` уже
    False — а ``acquire()`` всё равно встанет в хвост, то есть ЗАСНЁТ. Для заботы, которую
    зовут прямо из часов, это означало бы вставший тик: буферы, планировщик, outbox.

    ⚠ Здесь читалось приватное поле ``Lock._waiters``. Праксис назвала это хрупким, и была
    права: молчаливая деградация при переименовании поля — ровно тот случай, когда защита
    исчезает, а выглядит работающей. Теперь глубину очереди считаем сами, через публичный
    API замка; честность очереди, пробуждение и порядок остаются его.
    """
    return _ONE_MIND.free_now()
_FORGE_EVENT_LAST = {"ts": 0.0}                  # зазор между forge-event ходами (её рычаг)
_CLOCK_STARTUP_DUE = frozenset({
    "durable_resume", "social_pulse", "computer_inventory",
    "forge_events",   # догнать события, случившиеся при даунтайме, сразу после подъёма
    # Реконсайлер предложений с периодом 30 минут ГОЛОДАЛ: каждый перезапуск отодвигал
    # его срок на полный период, а из последних 39 запусков 16 случились быстрее чем
    # через полчаса после предыдущего (её самомёрж перезапускает её дважды, плюс выкаты).
    # Итог: за 13 дней он отработал один раз (метки updated у building стоят на 19.07),
    # и всё это время реестр предложений врал о себе, потому что поправить его было
    # некому. Он тоже дешёвая догоняющая проверка сохранённого состояния — ей место здесь.
    "selfdev_reconcile",
})
_TURN_TOPIC_ROUTE: ContextVar[telegram_topics.TopicRoute | None] = ContextVar(
    "praxis_telegram_topic_route", default=None)
_topic_titles: dict[tuple[str, int], str] = {}


def _media_spool() -> media_core.MediaSpool:
    global _MEDIA_SPOOL
    if _MEDIA_SPOOL is None:
        _MEDIA_SPOOL = media_core.MediaSpool()
    return _MEDIA_SPOOL


def _membership_ledger() -> telegram_membership.MembershipLedger:
    global _MEMBERSHIP_LEDGER
    if _MEMBERSHIP_LEDGER is None:
        _MEMBERSHIP_LEDGER = telegram_membership.MembershipLedger()
    return _MEMBERSHIP_LEDGER


def _direct_outbox() -> telegram_outbox.TelegramOutbox:
    global _DIRECT_OUTBOX
    if _DIRECT_OUTBOX is None:
        _DIRECT_OUTBOX = telegram_outbox.TelegramOutbox()
    return _DIRECT_OUTBOX


def _route_from_state(chat_id: str | int) -> telegram_topics.TopicRoute:
    """Conversation id -> root peer/topic; ordinary chat ids remain unchanged."""

    return telegram_topics.route_from_conversation_id(chat_id)


def _route_from_reference(value: str | int) -> telegram_topics.TopicRoute:
    """Tool/queue address -> root Telethon peer plus isolated conversation key."""

    return telegram_topics.route_from_reference(value)


def _meta_for_peer(peer_id: str | int) -> tuple[str, dict] | tuple[None, None]:
    """Find usable live metadata for a root peer, including topic-scoped entries."""

    peer = str(peer_id)
    direct = _meta.get(peer)
    if isinstance(direct, dict):
        return peer, direct
    # Dicts preserve insertion order; the last matching topic is the freshest one.
    for conversation_id, meta in reversed(list(_meta.items())):
        if isinstance(meta, dict) and str(meta.get("peer_id") or "") == peer:
            return conversation_id, meta
    return None, None


def _meta_for_delivery(target: str | int, reply_to=None) -> tuple[str | None, dict | None]:
    """Find metadata without collapsing a durable topic address to its root peer.

    New queue records carry a conversation id, so their topic is unambiguous after
    restart even before an incoming update repopulates ``_meta``.  The recent-reply
    lookup remains only as backwards compatibility for old root-peer queue records.
    """

    raw = str(target)
    direct = _meta.get(raw)
    if isinstance(direct, dict):
        return raw, direct
    route = _route_from_reference(raw)
    conversation_id = route.conversation_id
    routed = _meta.get(conversation_id)
    if isinstance(routed, dict):
        return conversation_id, routed
    # A persisted topic route is already authoritative.  Borrowing another
    # topic's fresh metadata would silently reroute a restart retry across
    # threads in the same group; resolve the root entity without metadata.
    if route.topic_id is not None:
        return conversation_id, None
    peer = route.peer_id
    if reply_to is not None:
        try:
            wanted = int(reply_to)
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None:
            for conversation_id, ring in reversed(list(_recent_msgs.items())):
                meta = _meta.get(conversation_id)
                if not isinstance(meta, dict) or str(meta.get("peer_id") or conversation_id) != peer:
                    continue
                if any(mid == wanted for mid, _author, _gist in ring):
                    return conversation_id, meta
    return _meta_for_peer(peer)


def _cooldown(is_dm: bool, room_mode: str = "normal", *, addressed: bool = False) -> float:
    # PASS 21: темп — её живой рычаг (manage_perception), env остался дефолтом
    try:
        if is_dm:
            return float(perception.value("cooldown_dm"))
        knob = "cooldown_addressed" if addressed else "cooldown_group"
        base = float(perception.value(knob))
    except Exception:
        base = COOLDOWN_DM if is_dm else (COOLDOWN_ADDRESSED if addressed else COOLDOWN_GROUP)
        if is_dm:
            return base
    if room_mode == "quiet":  # 10.3: в тихой комнате она вдвое реже
        base *= 2
    return base


def _buf_push(chat_id: str, line: str, *, author: str = "",
              is_dm: bool | None = None, name: str | None = None,
              source_id: str | int | None = None, ts: float | None = None,
              record_life: bool = True) -> None:
    """Добавить строку в буфер и пометить чат к персисту (§1).

    PASS 9.0: заодно фиксируем время/автора последней строки в buf_meta.json —
    буфер строк времени не хранит, а boot-sweep после рестарта должен знать возраст."""
    _buf[chat_id].append(line)
    _buf_dirty.add(chat_id)
    if record_life and not _under_tests():
        try:
            actor = author or line.split(":", 1)[0]
            direction = "out" if actor.strip().casefold() == "praxis" else "in"
            dedupe = (f"telegram:{chat_id}:{source_id}:{direction}"
                      if source_id is not None else "")
            memory_life.record_message(
                chat_id, line, actor=actor, direction=direction, source="telegram",
                source_id=source_id, is_dm=is_dm, ts=ts, dedupe_key=dedupe)
        except Exception:
            # Event spine failing must be loud in logs, but never eat a Telegram message.
            log.exception("life event не записался [%s]", chat_id)
    try:
        bufstore.meta_update(chat_id, author=author or line.split(":", 1)[0],
                             is_dm=is_dm, name=name)
    except Exception:
        log.debug("buf_meta не записалась [%s]", chat_id, exc_info=True)


def _telegram_handle(entity) -> str | None:
    """Доступный для @-упоминания username из Telethon entity.

    У новых аккаунтов Telegram основной ``.username`` может быть пустым, а живой
    публичный ник лежит в списке ``.usernames``. Не берём неактивные исторические
    ники: ими нельзя надёжно упомянуть человека.
    """
    primary = str(getattr(entity, "username", "") or "").strip().lstrip("@")
    if primary:
        return primary
    for item in getattr(entity, "usernames", None) or ():
        if not getattr(item, "active", False):
            continue
        handle = str(getattr(item, "username", "") or "").strip().lstrip("@")
        if handle:
            return handle
    return None


def _sender_label(sender) -> str:
    """Имя автора с доступным @-ником, чтобы голос мог адресовать его в Telegram."""
    name = " ".join(filter(None, [getattr(sender, "first_name", None),
                                  getattr(sender, "last_name", None)]))
    handle = _telegram_handle(sender)
    if name:
        return f"{name} (@{handle})" if handle else name
    return f"@{handle}" if handle else "кто-то"


def _sender_name(m) -> str:
    """Имя автора Telethon-сообщения; её собственные — 'Praxis'."""
    if getattr(m, "out", False):
        return "Praxis"
    return _sender_label(getattr(m, "sender", None))


def _media_tag(m) -> str:
    """Медиа-конверт (PASS_3 §2): структурный тег, чтобы она видела, ЧТО пришло, даже без текста.

    Порядок проверок важен: стикеры/гифки — тоже документы с видео-атрибутом."""
    try:
        if getattr(m, "photo", None) is not None:
            return "[Изображение]"
        if getattr(m, "voice", None) is not None:
            return "[Голосовое]"
        if getattr(m, "video_note", None) is not None:
            return "[Видеосообщение]"
        if getattr(m, "sticker", None) is not None:
            return "[Стикер]"
        if getattr(m, "gif", None) is not None:
            return "[GIF]"
        if getattr(m, "video", None) is not None:
            return "[Видео]"
        if getattr(m, "audio", None) is not None:
            return "[Аудио]"
        if getattr(m, "document", None) is not None:
            fname = getattr(getattr(m, "file", None), "name", None)
            return f"[Документ: {fname}]" if fname else "[Документ]"
        if getattr(m, "media", None) is not None:
            return "[Медиа]"
    except Exception:
        pass
    return ""


def _typed_media_kind(m) -> str | None:
    """Только то, что реально понимает новый тракт: фото и голос/аудио (не видео/GIF)."""
    if getattr(m, "photo", None) is not None:
        return "photo"
    if getattr(m, "voice", None) is not None or getattr(m, "audio", None) is not None:
        return "audio"
    return None


class _CappedMediaSink:
    """Synchronous file-like sink Telethon can stream into without a RAM-sized blob."""

    def __init__(self, path: Path, limit: int):
        self.path = path
        self.limit = int(limit)
        self.total = 0
        self._file = path.open("wb")

    def write(self, chunk) -> int:
        data = bytes(chunk)
        if self.total + len(data) > self.limit:
            raise media_core.MediaTooLargeError(
                f"download exceeds {self.limit} byte media cap")
        written = self._file.write(data)
        self.total += written
        return written

    def flush(self) -> None:
        self._file.flush()

    def fileno(self) -> int:
        return self._file.fileno()

    def close(self) -> None:
        if not self._file.closed:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


async def _capture_typed_media(msg, *, chat_id: str, scope: str,
                               caption: str = "") -> tuple[media_core.MediaRef | None, str]:
    """Stream through a hard cap, verify magic/quota, then bind to the scoped spool."""
    kind = _typed_media_kind(msg)
    if kind is None:
        return None, ""
    f = getattr(msg, "file", None)
    size = int(getattr(f, "size", 0) or 0)
    name = getattr(f, "name", None) or f"telegram{getattr(f, 'ext', '') or ''}"
    temp_path: Path | None = None
    try:
        spool = _media_spool()
        if size:
            spool.check_size(kind, size)
        cap = spool.photo_max_bytes if kind == "photo" else spool.audio_max_bytes
        fd, raw_temp = tempfile.mkstemp(prefix="praxis-media-", suffix=".part")
        os.close(fd)
        temp_path = Path(raw_temp)
        with _CappedMediaSink(temp_path, cap) as sink:
            got = await msg.download_media(file=sink)
            # Hermetic adapters may return bytes instead of honoring a file-like sink.
            if sink.total == 0 and isinstance(got, (bytes, bytearray, memoryview)):
                sink.write(got)
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            return None, f"[{('Изображение' if kind == 'photo' else 'Аудио')}: скачать не вышло]"
        ref = spool.ingest_path(
            temp_path, kind=kind, filename=name, chat_id=chat_id,
            message_id=getattr(msg, "id", None), scope=scope, caption=caption or "",
            move=True)
        temp_path = None
        return ref, ""
    except media_core.MediaTooLargeError:
        log.info("медиа сверх лимита [%s]: %s (%d)", chat_id, name, size)
        return None, f"[{('Изображение' if kind == 'photo' else 'Аудио')}: слишком большое]"
    except media_core.MediaError as e:
        log.info("медиа отклонено [%s]: %s", chat_id, str(e)[:160])
        return None, f"[{('Изображение' if kind == 'photo' else 'Аудио')}: формат не поддержан]"
    except Exception:
        log.warning("скачивание мультимедиа упало [%s]", chat_id, exc_info=True)
        return None, f"[{('Изображение' if kind == 'photo' else 'Аудио')}: скачать не вышло]"
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _format_messages(msgs) -> list[str]:
    """Telethon-сообщения -> строки 'Имя: текст' в хронологии (старые сверху). Её — 'Praxis'.

    Медиа получают структурный тег; реплаи — маркер '(в ответ <кому>)', если цель в выборке."""
    msgs = list(msgs)
    by_id = {}
    for m in msgs:
        mid = getattr(m, "id", None)
        if mid is not None:
            by_id[mid] = _sender_name(m)
    lines = []
    for m in msgs:
        text = getattr(m, "message", None) or ""
        tag = _media_tag(m)
        if not text and not tag:
            continue
        rid = getattr(m, "reply_to_msg_id", None)
        mark = (f" (в ответ {by_id[rid]})" if rid in by_id else " (в ответ)") if rid else ""
        body = " ".join(x for x in (tag, text) if x)
        lines.append(f"{_sender_name(m)}{mark}: {body}")
    return lines


async def _last_n_text(chat_id: str) -> str:
    """PASS 19 hot-layer: persisted local buffer breathes 50↔100 after provenance compaction.

    The incoming handler has already observed and appended the current message before this call,
    so the buffer is the exact live slice. Telegram remains the cold-start fallback.
    """
    local = list(_buf[chat_id])[-memory_life.HOT_HARD_HI:]
    if local:
        return "\n".join(local)
    meta = _meta.get(chat_id, {})
    entity = meta.get("entity", None)
    if entity is not None:
        try:
            limit = min(memory_life.HOT_HI, max(memory_life.HOT_LO, LAST_N))
            topic_id = meta.get("topic_id")
            kwargs = {"reply_to": int(topic_id)} if topic_id is not None else {}
            msgs = await client.get_messages(entity, limit=limit, **kwargs)
            lines = _format_messages(reversed(list(msgs)))  # get_messages отдаёт новые сверху
            if lines:
                return "\n".join(lines[-limit:])
        except Exception:
            log.warning("live-fetch контекста упал [%s] — фолбэк на буфер", chat_id, exc_info=True)
    return ""


def _dialogue_roles_on() -> bool:
    """Рычаг отката без передеплоя. По умолчанию ВКЛЮЧЕНО.

    Это не ограничитель её восприятия: и с ролями, и без них в модель уезжает один и тот
    же разговор. Разница только в том, чьей репликой приезжают её собственные слова.
    """
    return os.getenv("PRAXIS_DIALOGUE_ROLES", "1").strip().lower() not in ("0", "false", "no", "off")


def _strip_author_prefix(line: str, actor: str) -> str:
    """«Praxis: текст» -> «текст». Префикс — служебный, и в её собственной реплике ему
    не место: она не подписывает свои слова своим именем, когда говорит."""
    head = f"{actor}: "
    return line[len(head):] if actor and line.startswith(head) else line


def _turns_to_dialogue(turns) -> tuple[list[dict], str]:
    """Лента с авторством -> (история ролями, то-на-что-она-отвечает-сейчас).

    Одно правило на личку и на группу: граница проходит по ЕЁ ПОСЛЕДНЕЙ реплике.
    Всё до неё включительно — разговор, всё после — то, что пришло и ждёт ответа.
    Если она здесь ещё не говорила либо последней говорила она сама — ролей нет и
    вызывающий идёт прежним путём: пустое user-сообщение хуже сплошного текста.
    """
    rows = [(bool(is_self), str(text)) for is_self, text in turns if str(text).strip()]
    if not rows:
        return [], ""
    last_self = -1
    for i, (is_self, _text) in enumerate(rows):
        if is_self:
            last_self = i
    if last_self < 0:
        return [], ""
    history: list[dict] = []
    run_role, run_lines = "", []

    def flush() -> None:
        if run_lines:
            history.append({"role": run_role, "content": "\n\n".join(run_lines).strip()})

    for is_self, text in rows[:last_self + 1]:
        role = "assistant" if is_self else "user"
        if role != run_role:
            flush()
            run_role, run_lines = role, []
        run_lines.append(text)
    flush()
    if not history:
        return [], ""
    current = "\n".join(text for _is_self, text in rows[last_self + 1:])
    if not current.strip():
        return [], ""
    return history, current


def _group_dialogue(turns) -> tuple[list[dict], str]:
    """Групповая лента ролями. Пустой снимок авторства -> прежний путь."""
    if not _dialogue_roles_on() or not turns:
        return [], ""
    try:
        return _turns_to_dialogue([(is_self, role_line)
                                   for is_self, _line, role_line in turns])
    except Exception:
        log.warning("роли группового разговора не собрались — иду сплошным текстом",
                    exc_info=True)
        return [], ""


def _dm_dialogue(chat_id: str) -> tuple[list[dict], str]:
    """Разговор в личке ролями: (история, то-на-что-она-отвечает-сейчас).

    Граница проведена там, где она и лежит по смыслу: всё до её последней реплики
    включительно — история, всё после — то, что пришло и ждёт ответа. Если она в этом
    чате ещё не говорила, истории нет и весь разговор остаётся текущим сообщением —
    ровно нынешнее поведение.

    Возвращает ([], "") если ролей не собрать: вызывающий тогда работает по-старому.
    """
    if not _dialogue_roles_on():
        return [], ""
    try:
        rows = memory_life.hot_records(chat_id, memory_life.HOT_HARD_HI)
    except Exception:
        log.warning("роли разговора не собрались [%s] — иду сплошным текстом", chat_id,
                    exc_info=True)
        return [], ""
    if not rows:
        return [], ""
    # ⚠ Подпись снимается с ОБЕИХ сторон, и это не симметрия ради красоты.
    # 03.08 21:44 она ответила Егору в личке, говоря о нём в ТРЕТЬЕМ ЛИЦЕ: «вы сейчас
    # чините ровно то, от чего Егору стало не по себе» — обращаясь при этом к нему.
    # Причина ровно здесь: свою реплику я от подписи освободил, а его — нет, и роль
    # `user` приезжала с текстом «Yegor Kosyrev (@tatarskiy_e4pochmak): …». Пока весь
    # разговор был одним блобом, имя читалось как стенограмма. Когда роль `user` СТАЛА
    # собеседником, имя перед его же словами превращает его в пересказываемое третье
    # лицо: кадр говорит «мне докладывают слова Егора», а не «Егор говорит мне».
    # В личке собеседник ровно один, и кто он — уже сказано ролью, `speaker` и рамкой
    # присутствия. В ГРУППЕ подпись остаётся: там говорящих много, и без имени реплика
    # безадресна.
    return _turns_to_dialogue([
        (row["direction"] == "out",
         _strip_author_prefix(row["line"], row["actor"]))
        for row in rows
    ])


async def _chat_descriptor(event, chat_id: str) -> dict:
    """§2: осознанность чата — {title, kind, size}, кэш на чат (без API-запроса на каждое сообщение).

    title/size тянем один раз при первом сообщении из чата; для больших групп число участников
    берём дешёвым GetParticipants(limit=0).total (счётчик, не выкачивая список)."""
    d = _chat_desc.get(chat_id)
    if d is not None:
        return d
    kind = "dm" if event.is_private else "group"
    title = size = None
    try:
        chat = await event.get_chat()
        title = getattr(chat, "title", None)  # у групп/каналов; в личке None
        size = getattr(chat, "participants_count", None)
        if kind == "group" and not size:
            try:
                size = (await client.get_participants(chat, limit=0)).total
            except Exception:
                size = None
    except Exception:
        log.debug("chat descriptor [%s] не получен", chat_id, exc_info=True)
    d = {"title": title, "kind": kind, "size": size}
    _chat_desc[chat_id] = d
    return d


def _wants_inbox(is_private: bool, is_owner: bool, msg, addressed: bool = False) -> bool:
    """Archive every Telegram document that reaches a live/admitted chat.

    Addressing controls whether Praxis speaks, not whether she was allowed to notice an
    attachment.  Photos/audio already take the typed media path below and are therefore
    excluded here to avoid downloading the same bytes twice.
    """
    if _typed_media_kind(msg) is not None:
        return False
    has_file = (getattr(msg, "document", None) is not None or
                getattr(msg, "photo", None) is not None)
    return bool(has_file)


async def _inbox_download(msg, *, scope: str = "", chat_id: str | int | None = None,
                          chat_kind: str = "", chat_label: str = "") -> str | None:
    """Скачать Telegram-файл в scoped workspace/inbox. -> буфер-тег ('[Файл: имя → путь]' или честный
    отказ по капам) | None при неожиданном сбое (остаётся обычный медиа-тег)."""
    try:
        f = getattr(msg, "file", None)
        name = getattr(f, "name", None) or ("photo" + str(getattr(f, "ext", None) or ".jpg"))
        size = int(getattr(f, "size", 0) or 0)
        path, why = workshop.inbox_accept(
            name, size, scope=scope, chat_id=chat_id,
            chat_kind=chat_kind, chat_label=chat_label)
        if path is None:
            return f"[Файл: {name} — {why}]"
        got = await msg.download_media(file=str(path))
        if not got:
            return f"[Файл: {name} — скачать не вышло]"
        rel = Path(str(got)).resolve().relative_to(workshop.BASE.resolve()).as_posix()
        log.info("inbox: сохранила Telegram-файл %s → %s", name, rel)
        return f"[Файл: {name} → {rel}]"
    except Exception:
        log.exception("inbox: скачивание Telegram-файла упало")
        return None


class _DeadRoomFilter(logging.Filter):
    """PASS 10.8: banned/private-канал в catch-up тракте Telethon (прод: одна и та же
    строка каждый час, комната обезличена). Первое появление: mode=dead в профиле
    + одна строка в журнал; повторы для уже мёртвой комнаты глушатся из лога."""

    _RE = re.compile(r"Account is now banned in (\d+)|"
                     r"channel (\d+).{0,40}(?:private|forbidden)", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            m = self._RE.search(record.getMessage())
        except Exception:
            return True
        if not m:
            return True
        cid = "-100" + (m.group(1) or m.group(2))
        try:
            if rooms.profile_read(cid)["mode"] == "dead":
                return False  # уже знаем — не засорять лог
            rooms.set_mode(cid, "dead", reason="Telegram: канал недоступен (banned/private)",
                           set_by="praxis")
            agent.tool_journal(f"[комната] {cid} мертва — Telegram отдаёт banned/private; "
                               "перестаю её слушать", salience=2)
            log.warning("комната %s помечена dead — дальнейший banned-спам глушится", cid)
        except Exception:
            log.debug("dead-room фильтр не смог пометить %s", cid, exc_info=True)
        return True  # первую строку показать честно


def _install_dead_room_filter() -> None:
    """Вешаем фильтр на хендлеры root-логгера: telethon-логгеры — дети root, записи
    проходят через его хендлеры (фильтры логгеров не наследуются, хендлеров — да)."""
    f = _DeadRoomFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)


@client.on(events.ChatAction)
async def on_chat_action(event) -> None:
    """A real Telegram membership event admits the room to Praxis's perception."""
    try:
        if not (getattr(event, "user_added", False) or getattr(event, "created", False)):
            return
        uids = set(getattr(event, "user_ids", None) or [])
        uid = getattr(event, "user_id", None)
        if uid is not None:
            uids.add(uid)
        if not _self_id or _self_id not in uids:
            return
        await _newcomer(event)
    except Exception:
        log.exception("новичок-протокол упал")


async def _newcomer(event) -> None:
    """Вход в новую группу: room-профиль mode=observer, предыстория (её же аккаунтом,
    ровно то, что видит любой новый участник) → компакт-сводка в профиль (НЕ в буфер,
    НЕ в журнал), затем один проход «осмотрись» (нормы + опц. one-shot приветствие)
    и inbox-карточка владельцу."""
    chat_id = str(event.chat_id)
    owner_added = False
    try:
        adder = await event.get_added_by()
        owner_added = bool(
            adder is not None and OWNER_ID != 0 and getattr(adder, "id", 0) == OWNER_ID
        )
        if rooms.add_room(chat_id):
            log.info("новичок-протокол [%s]: реальное членство добавлено в каталог", chat_id)
    except AttributeError:
        pass  # событие без get_added_by (нечем проверить — allowlist не трогаем)
    except Exception:
        log.debug("get_added_by не удался [%s]", chat_id, exc_info=True)
    title = None
    ent = None
    try:
        ent = await event.get_chat()
        title = getattr(ent, "title", None)
    except Exception:
        log.debug("get_chat в новичок-протоколе не удался", exc_info=True)
    await _initialize_joined_room(chat_id, ent or event.chat_id, title=title,
                                  allow=True,
                                  set_by="owner" if owner_added else "praxis")


async def _initialize_joined_room(chat_id: str, entity, *, title: str | None = None,
                                  allow: bool = False, set_by: str = "praxis") -> None:
    """Initialize a room joined either by ChatAction or a sovereign link tool.

    This is deliberately idempotent: Telethon may deliver the ChatAction after
    ImportChatInviteRequest already returned.  Rejoining an explicitly departed
    room revives it while preserving its accumulated room memory.
    """
    set_by = "owner" if set_by == "owner" else "praxis"
    if allow:
        rooms.add_room(chat_id)
    prof = rooms.profile_read(chat_id)
    if prof["exists"] and prof["structured"]:
        if prof.get("mode") == "dead":
            rooms.set_mode(
                chat_id, "normal", reason="вернулась", set_by=set_by,
            )
            rooms.owner_card(chat_id, "join", f"снова вошла в «{title or chat_id}»; режим normal")
        else:
            log.info("новичок-протокол [%s]: профиль уже есть — не сбрасываю (re-add)", chat_id)
        return
    rooms.set_mode(chat_id, "normal", reason="", set_by=set_by)
    rooms.profile_update(chat_id, engagement="reflective")
    log.info("новичок-протокол [%s]: вошла в «%s», режим normal/reflective", chat_id, title or "?")
    lines: list[str] = []
    try:
        msgs = await client.get_messages(entity, limit=BACKFILL_N)
        for m in reversed(list(msgs or [])):
            t = getattr(m, "message", "") or ""
            if t.strip():
                lines.append(f"{_sender_name(m)}: {t}")
    except Exception:
        log.info("новичок-протокол [%s]: история недоступна (скрыта/приватна)", chat_id)
    if lines:
        summ = await asyncio.to_thread(agent.backfill_summary, lines)
        text = ("_(прочитанное, не пережитое)_\n" + summ) if summ else "истории не видно"
    else:
        text = "истории не видно"
    rooms.section_set(chat_id, "Сводка предыстории", text)
    norms, greeting = await asyncio.to_thread(agent.lookaround, chat_id, title)
    if norms:
        rooms.section_set(chat_id, "Нормы и атмосфера", norms)
    greeted = rooms.profile_read(chat_id)["header"].get("greeted") == "yes"
    if greeting and not greeted:
        try:
            await client.send_message(entity, greeting)
            rooms.profile_update(chat_id, greeted="yes")
            _buf_push(chat_id, f"Praxis: {greeting}", author="Praxis", is_dm=False)
            log.info("новичок-протокол [%s]: поздоровалась (один раз)", chat_id)
        except Exception:
            log.exception("приветствие не ушло [%s]", chat_id)
    two = " / ".join([l for l in text.splitlines() if l.strip()][:2])[:200]
    rooms.owner_card(chat_id, "join",
                     f"вошла в «{title or chat_id}», профиль готов, режим normal/reflective: {two}")


async def _forum_topic_catalog(entity, peer_id: str, *, limit: int = 500) -> list[dict]:
    """Read Telegram's forum map with this runner's already-connected client."""

    from telethon.tl.functions.messages import GetForumTopicsRequest

    cap = max(1, min(500, int(limit)))
    rows: list[dict] = []
    seen: set[int] = set()
    offset_date = None
    offset_id = offset_topic = 0
    while len(rows) < cap:
        page_size = min(100, cap - len(rows))
        result = await client(GetForumTopicsRequest(
            peer=entity, offset_date=offset_date, offset_id=offset_id,
            offset_topic=offset_topic, limit=page_size,
        ))
        page = list(getattr(result, "topics", None) or ())
        fresh = []
        for item in page:
            try:
                topic_id = int(getattr(item, "id"))
            except (TypeError, ValueError):
                continue
            if topic_id <= 0 or topic_id in seen:
                continue
            seen.add(topic_id)
            title = str(getattr(item, "title", "") or f"topic #{topic_id}")
            date = getattr(item, "date", None)
            top_message = getattr(item, "top_message", None)
            row = {
                "topic_id": topic_id, "title": title,
                "date": date, "top_message": top_message,
            }
            rows.append(row)
            fresh.append(item)
            _topic_titles[(str(peer_id), topic_id)] = title
            await asyncio.to_thread(
                group_context.record_topic, peer_id, topic_id, title,
                timestamp=date, message_id=topic_id,
            )
        if len(page) < page_size or not fresh:
            break
        last = fresh[-1]
        offset_date = getattr(last, "date", None)
        offset_id = int(getattr(last, "top_message", 0) or 0)
        offset_topic = int(getattr(last, "id", 0) or 0)
    return rows


async def _backfill_group_context(peer_id: str, entity, *, limit: int) -> dict:
    """Archive a bounded newest slice, idempotently and without any model call.

    Repeating after a crash is the resume protocol: canonical message ids de-duplicate,
    and the archive's message count remains the hard ceiling.
    """

    peer = str(peer_id)
    cap = max(0, min(rooms.BACKFILL_MAX, int(limit)))
    before = await asyncio.to_thread(group_context.archived_message_count, peer)
    if cap <= 0 or before >= cap:
        return {"peer_id": peer, "limit": cap, "before": before, "added": 0,
                "complete": before >= cap if cap else True}
    # ⚠ Здесь `except Exception` сплющивал ТРИ разных факта в один пустой список:
    # «Telegram говорит, что это не форум» (CHANNEL_FORUM_MISSING — прямой и durable
    # ответ), «сеть/флуд» (не говорит ничего) и «форум без тем». Ответ на самый важный
    # вопрос про комнату приходил сюда бесплатно и выбрасывался в локальную переменную.
    try:
        topics = await _forum_topic_catalog(entity, peer, limit=min(500, cap))
        await asyncio.to_thread(
            lambda: telegram_routes.observe(peer, kind="get_forum_topics_ok",
                                            detail=f"тем: {len(topics)}"))
    except Exception as exc:
        topics = []
        missing = type(exc).__name__ == "ChannelForumMissingError" or \
            "CHANNEL_FORUM_MISSING" in str(exc)
        try:
            await asyncio.to_thread(
                lambda: telegram_routes.observe(
                    peer,
                    kind="channel_forum_missing" if missing else "rpc_unavailable",
                    detail=type(exc).__name__))
        except Exception:
            log.debug("реестр маршрутов не принял свидетельство [%s]", peer, exc_info=True)
        # Non-forum groups legitimately reject GetForumTopics.  Their root history is
        # still useful and follows the same canonical path.
        log.info("group backfill [%s]: forum topic list unavailable (%s)",
                 peer, type(exc).__name__)

    messages = []
    async for item in client.iter_messages(entity, limit=cap):
        messages.append(item)
    # Свидетельство об ИСТОРИИ, а не о «сейчас». Прямой ответ Telegram
    # (GetForumTopics) говорит только про текущее состояние комнаты и потому не
    # покрывает старые ключи — а их в AbstractDL 279. Зато мы только что прошли
    # диапазон сообщений: у настоящего форума в нём есть служебные «создана тема»
    # (в Грибнице их 18), у обычной супергруппы — ни одного (на 4138 сообщений).
    # Отдаём реестру именно диапазон: тогда исторические ключи получают ответ, а не
    # «не знаю», и слой B наконец начинает работать на том, ради чего писался.
    ids = [int(m.id) for m in messages if getattr(m, "id", None) is not None]
    if ids:
        openers = sum(1 for m in messages if telegram_topics.is_topic_opener(m))
        try:
            await asyncio.to_thread(
                lambda: telegram_routes.observe(
                    peer,
                    kind=("topic_opener_seen" if openers else "no_topic_openers_in_range"),
                    since_message_id=min(ids), until_message_id=max(ids),
                    detail=f"пройдено {len(ids)}, openers {openers}"))
        except Exception:
            log.debug("реестр маршрутов: историческое свидетельство не записалось [%s]",
                      peer, exc_info=True)
    added = 0
    count = before
    _archived_any = False
    for msg in reversed(messages):
        if count >= cap:
            break
        mid = getattr(msg, "id", None)
        if mid is None:
            continue
        route = telegram_topics.route_for_message(peer, msg, is_private=False)
        opener = telegram_topics.topic_opener_title(msg)
        topic_title = opener
        if route.topic_id is not None and not topic_title:
            topic_title = _topic_titles.get((peer, int(route.topic_id)), "")
        if opener and route.topic_id is not None:
            await asyncio.to_thread(
                group_context.record_topic, peer, route.topic_id, opener,
                timestamp=getattr(msg, "date", None), message_id=mid,
            )
        sender = getattr(msg, "sender", None)
        sender_id = getattr(msg, "sender_id", None) or getattr(sender, "id", None)
        name = _sender_name(msg)
        try:
            accepted = await asyncio.to_thread(
                group_context.observe_message,
                peer_id=peer, topic_id=route.topic_id, message_id=mid,
                sender_id=sender_id, sender_name=name,
                reply_to_message_id=getattr(msg, "reply_to_msg_id", None),
                timestamp=getattr(msg, "date", None),
                edited_at=getattr(msg, "edit_date", None),
                text=getattr(msg, "message", "") or "",
                topic_title=topic_title, media=_media_tag(msg),
                outgoing=bool(getattr(msg, "out", False)),
            )
        except (TypeError, ValueError):
            continue
        if accepted:
            added += 1
            count += 1
            _archived_any = True
    # Границы мест. Реестр уже знает, форум ли комната; но вердикт «форум» сам по себе
    # её не чинит: в General Грибницы 631 сообщение плюс 437 в 37 наших псевдоветках.
    # Кто из веток настоящая тема Telegram, а кто наш артефакт, видно прямо в архиве —
    # по тому, лежит ли корневое сообщение ветки в ней самой. Считаем это здесь, один
    # раз на обход, чтобы на чтении остался просмотр за O(1).
    if _archived_any or before:
        try:
            mapping = await asyncio.to_thread(group_context.branch_containers, peer)
            if mapping:
                await asyncio.to_thread(telegram_routes.observe_branches, peer, mapping)
                log.info("реестр маршрутов [%s]: наших псевдоветок %d", peer, len(mapping))
        except Exception:
            log.debug("границы мест не посчитались [%s]", peer, exc_info=True)
    # Пункт 5, ДАТЧИК. Обе стороны считаются на одних и тех же сообщениях, которые мы
    # только что прошли: сегодняшний маршрут и тот, что был бы при is_forum=False.
    # Ничего не меняет; журнал лежит в .state (прибор, не память).
    try:
        split = await asyncio.to_thread(telegram_topics.measure_split, peer, messages)
        if split.get("messages"):
            context_envelope.record_probe("route_split", split)
            if split["keys_live"] > split["keys_if_not_forum"]:
                log.warning(
                    "route split [%s]: %d сообщений в %d ключах (было бы %d); "
                    "самая крупная ветка держит %.0f%% комнаты",
                    peer, split["messages"], split["keys_live"],
                    split["keys_if_not_forum"], 100 * split["largest_branch_share"])
    except Exception:
        log.debug("датчик расщепления не отработал [%s]", peer, exc_info=True)
    projection = await asyncio.to_thread(group_context.rebuild_projection, peer)
    complete = int(projection.get("message_count") or 0) >= cap or len(messages) < cap
    await asyncio.to_thread(
        group_context.mark_backfill, peer, limit=cap,
        scanned=len(messages), complete=complete,
    )
    log.info("group backfill [%s]: %d -> %d/%d; topics=%d",
             peer, before, projection.get("message_count", 0), cap, len(topics))
    return {
        "peer_id": peer, "limit": cap, "before": before, "added": added,
        "message_count": int(projection.get("message_count") or 0),
        "topics": len(topics), "complete": complete,
    }


def _consume_boundary_reply(chat_id: str, reply_to_id: int | None,
                            sender_id: int | None, *, is_owner: bool) -> bool:
    if is_owner or reply_to_id is None or sender_id is None:
        return False
    now = time.time()
    live = deque(maxlen=8)
    matched = False
    for sent_id, provocateur_id, created_at in _boundary_replies.get(chat_id, ()):
        if now - created_at > _BOUNDARY_REPLY_TTL_S:
            continue
        if not matched and sent_id == reply_to_id and provocateur_id == sender_id:
            matched = True
            continue
        live.append((sent_id, provocateur_id, created_at))
    if live:
        _boundary_replies[chat_id] = live
    else:
        _boundary_replies.pop(chat_id, None)
    return matched


def _should_wake(is_private: bool, addressed: bool, decision: str,
                  room_mode: str = "normal") -> bool:
    """Wake contract: DM/address always wins; reflective rooms batch ambient flow."""
    if is_private or addressed:
        return True
    if decision == "ignore":
        return False
    return str(room_mode or "normal").casefold() == "reflective"


def _room_policy_for_state(chat_id: str | int) -> dict:
    """Resolve a conversation key to its root peer before reading room policy."""

    try:
        peer_id = _route_from_state(chat_id).peer_id
        return rooms.room_policy(peer_id)
    except Exception:
        log.debug("deep-room profile не прочитался [%s]", chat_id, exc_info=True)
        return rooms.default_policy()


def _group_archive_enabled() -> bool:
    """Keep hermetic runner fixtures off the real memory tree unless explicitly opted in."""

    return not _under_tests() or os.getenv("PRAXIS_TEST_GROUP_ARCHIVE") == "1"


def _group_context_snapshot(chat_id: str, policy: dict) -> str:
    """Freeze one topic only; a deep profile reads its larger canonical archive tail."""
    return _group_context_frozen(chat_id, policy)[0]


def _group_context_frozen(chat_id: str, policy: dict) -> tuple[str, tuple]:
    """Тот же снимок ленты, но ВМЕСТЕ с авторством: (текст, строки-записи).

    Текст — байт в байт прежний: на нём стоят расписки, прожитый ход и исходящая
    граница. Записи нужны, чтобы разложить ту же ленту по ролям, ничего не пересобирая:
    снимок группы заморожен на момент пробуждения, и второй проход по архиву показал бы
    модели уже другой разговор.

    Пустой кортеж записей — честное «авторство неизвестно»: так возвращается запасной
    путь по строковому буферу, где своё от чужого отличается только префиксом. Роли в
    таком проходе не собираются вовсе, и ход идёт как раньше.
    """
    limit = int(policy.get("context_hot") or 0)
    if limit > 0 and _group_archive_enabled():
        route = _route_from_state(chat_id)
        # Слой B: спрашиваем реестр и читаем комнату целиком там, где Telegram ПРЯМО
        # ответил, что форума нет. Никакого «по умолчанию» и никакого угадывания:
        # `unknown` ведёт себя ровно как раньше. Хранение не трогается — меняется
        # только то, сколько своей комнаты она видит, просыпаясь в ветке.
        #
        # ⚠ Границы места считает `telegram_routes.read_scope`, а не этот код. Прежде
        # решение было переписано на месте: спрашивалось про `topic_id` вместо `peer_id`
        # и только при непустой ветке — на корневом ключе слой B не включался вовсе.
        # Читателей этого решения теперь трое; разойтись они могут только молча.
        scope = telegram_routes.read_scope(route.peer_id, route.topic_id)
        summary_chars = int(policy.get("context_summary_chars") or 7000)
        # ⚠ Связывает БЮДЖЕТ СИМВОЛОВ, а не потолок сообщений: при 14 000 в кадр влезало
        # ~45 строк при потолке в 200 сообщений. Поэтому «длиннее» — это про символы.
        # В не-форуме лента должна покрывать РАЗГОВОР места, а не хвост одной ветки:
        # там веток нет, есть одна комната, и в оживлённой комнате 45 строк — это
        # десять минут. Решение Егора 04.08.
        max_chars = max(8_000, summary_chars * 2)
        if scope.whole_room:
            max_chars = max(WHOLE_ROOM_CONTEXT_CHARS, summary_chars * 3)
        try:
            rows = group_context.context_rows(
                route.peer_id, topic_id=route.topic_id, limit=limit,
                max_chars=max_chars,
                whole_room=scope.whole_room, members=scope.members,
                thread_word=scope.thread_word,
            )
            archived = "\n".join(row["line"] for row in rows)
            if archived:
                return archived, tuple(
                    (bool(row["self"]), str(row["line"]), str(row["role_line"]))
                    for row in rows)
        except Exception:
            log.exception("group archive context не собрался [%s]", chat_id)
    return "\n".join(list(_buf[chat_id])[-(limit or memory_life.HOT_HARD_HI):]), ()


def _group_trigger_snapshot(chat_id: str, *, mid, message_ts: float,
                            kind: str, addressed: bool, query: str,
                            name: str, sender_id, is_owner: bool,
                            known: bool, family: bool) -> GroupWake:
    policy = _room_policy_for_state(chat_id)
    # Авторство замораживается ВМЕСТЕ с лентой, одним чтением архива: пробуждение может
    # ждать в кулдауне минуты, и второй проход показал бы модели уже другой разговор.
    frozen_text, frozen_turns = _group_context_frozen(chat_id, policy)
    return GroupWake(
        message_id=int(mid) if mid is not None else None,
        message_ts=float(message_ts),
        kind=str(kind),
        addressed=bool(addressed),
        query=str(query or "")[:4000],
        speaker=name,
        sender_id=int(sender_id) if sender_id is not None else None,
        owner=bool(is_owner) if addressed else False,
        known=bool(known),
        family=bool(family),
        context_snapshot=frozen_text,
        turns_snapshot=frozen_turns,
        media_snapshot=tuple(_pending_media.get(chat_id, ())),
        reply_targets_snapshot=tuple(_recent_msgs[chat_id]),
    )


def _addressed_trigger_snapshot(chat_id: str, *, mid, message_ts: float,
                                kind: str, name: str, sender_id,
                                is_owner: bool, known: bool, family: bool) -> GroupWake:
    """Freeze the exact group situation that earned a future model pass.

    Cooldown may delay a turn for minutes. Without this envelope, a sticky
    ``addressed=True`` is later combined with a moving Telegram tail, another
    speaker, newer media, and newer reply targets. The model then appears to
    answer an unrelated message. Only a newer explicit address may replace the
    snapshot; background traffic cannot mutate it.
    """
    return _group_trigger_snapshot(
        chat_id, mid=mid, message_ts=message_ts, kind=kind,
        addressed=True, query="", name=name, sender_id=sender_id,
        is_owner=is_owner, known=known, family=family,
    )


def _note_address_overwritten(chat_id: str, old: GroupWake, new: GroupWake) -> None:
    """Канарейка: одно обращение вытеснило другое, ещё не отработанное.

    Пробуждение на комнату одно. Пока держится кулдаун, второе обращение перезаписывает
    первое, и проход пойдёт по последнему. Сообщение остаётся в контексте комнаты, но
    перестаёт быть обращением: вместе с ним уходят `speaker`, `kind` и `owner` — то есть
    обращение Егора, вытесненное чужим, идёт в проход БЕЗ его полномочий.

    Здесь ничего не чинится, только считается. Какой ход правильный, когда двое обратились
    подряд, — вопрос не механический; чинить его вслепую значит выбрать за неё. Сколько это
    стоит на живом потоке, скажет журнал (`canary.dropped_addresses`).
    """
    try:
        meta = {"dropped": "addressed"}
        if old.owner and not new.owner:
            meta["owner_lost"] = "1"
        perception.note_skip(
            "group_wake", "отложила", chat_id=chat_id,
            detail=(f"обращение #{old.message_id} ({old.kind}, {old.speaker or '?'}) "
                    f"вытеснено #{new.message_id} до прохода"),
            meta=meta,
        )
    except Exception:
        log.debug("канарейка вытесненного обращения не записалась", exc_info=True)


def _install_group_wake(chat_id: str, wake: GroupWake) -> bool:
    """Install a generation without letting ambient traffic replace an address."""

    current = _group_wakes.get(chat_id)
    if current is not None and current.addressed and not wake.addressed:
        return False
    if (current is not None and current.addressed and wake.addressed
            and current.message_id != wake.message_id):
        _note_address_overwritten(chat_id, current, wake)
    _group_wakes[chat_id] = wake
    return True


def _edit_revision_source_id(message_id: int, edited_at, *, text: str = "",
                             media: str = "") -> str:
    """Stable identity for one Telegram revision, including its visible payload.

    Telegram exposes ``edit_date`` only to whole-second precision in common update
    paths.  Two real edits can therefore share both message id and timestamp.  The
    visible payload digest keeps those revisions distinct while duplicate delivery
    of the same update retains exactly the same source id.
    """

    if isinstance(edited_at, datetime.datetime):
        stamp = edited_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        revision = stamp.astimezone(datetime.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    else:
        revision = str(edited_at or "").strip()
    payload = json.dumps(
        {"media": str(media or ""), "text": str(text or "")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    payload_sha = hashlib.sha256(payload).hexdigest()[:20]
    return f"{int(message_id)}:edit:{revision}:{payload_sha}"


@client.on(getattr(events, "MessageEdited", events.NewMessage)(incoming=True))
async def on_edited(event) -> None:
    """Archive an admitted group edit without turning it into a new voice turn.

    Telegram edits retain their original message id.  ``edit_date`` plus a visible
    payload digest is therefore the revision identity in both the canonical group
    archive and the life spine (Telegram may collapse multiple edits into one second).
    This path intentionally does not perform media downloads, contact/authority
    mutations, follow-up matching, panic handling, or any model/wake work.
    """

    if bool(getattr(event, "is_private", False)):
        return
    msg = event.message
    mid = getattr(msg, "id", None)
    edited_at = getattr(msg, "edit_date", None)
    if mid is None or edited_at is None or not _group_archive_enabled():
        return

    route = telegram_topics.route_for_message(
        event.chat_id, msg, is_private=False)
    peer_id = route.peer_id
    chat_id = route.conversation_id
    if rooms.is_frozen(peer_id):
        return

    sender = await event.get_sender()
    if sender is not None and getattr(sender, "is_self", False):
        return
    sender_id = (getattr(event, "sender_id", None)
                 or getattr(sender, "id", None))
    is_owner = OWNER_ID != 0 and sender_id == OWNER_ID
    if not rooms.is_allowed(peer_id, is_owner):
        return
    try:
        room_mode = rooms.effective_mode(peer_id)
    except Exception:
        log.debug("edited message room mode unavailable [%s]", chat_id,
                  exc_info=True)
        room_mode = "normal"
    if room_mode in ("frozen", "dead"):
        return

    name = _sender_label(sender)
    text = str(getattr(msg, "message", "") or "")
    media = _media_tag(msg)
    reply_to_mid = getattr(msg, "reply_to_msg_id", None)
    try:
        message_ts = float(msg.date.timestamp())
    except Exception:
        message_ts = time.time()

    raw_topic_title = ""
    if route.topic_id is not None:
        cache_key = (peer_id, int(route.topic_id))
        raw_topic_title = _topic_titles.get(cache_key, "")
        if not raw_topic_title:
            try:
                raw_topic_title = await asyncio.to_thread(
                    group_context.topic_title, peer_id, route.topic_id)
            except Exception:
                log.debug("edited message topic title unavailable [%s]", chat_id,
                          exc_info=True)
        if raw_topic_title:
            _topic_titles[cache_key] = raw_topic_title

    try:
        added = await asyncio.to_thread(
            group_context.observe_message,
            peer_id=peer_id, topic_id=route.topic_id, message_id=mid,
            sender_id=sender_id, sender_name=name,
            reply_to_message_id=reply_to_mid, timestamp=message_ts,
            edited_at=edited_at, text=text, topic_title=raw_topic_title,
            media=media,
        )
    except Exception:
        log.exception("group edit archive failed [%s] #%s", chat_id, mid)
        return
    body = " ".join(part for part in (media, text) if part).strip()
    if not body:
        body = "[empty message after edit]"
    reply_mark = (f", reply to #{reply_to_mid}"
                  if reply_to_mid is not None else "")
    source_id = _edit_revision_source_id(
        int(mid), edited_at, text=text, media=media,
    )
    revision = source_id.split(":edit:", 1)[-1]
    marker = f"[edited #{mid} at {revision}{reply_mark}]"
    # The archive is written first.  If the process died before this buffer/life
    # append, Telethon replay sees ``added=False`` but can still repair the missing
    # projection.  A persisted exact marker and memory_life's dedupe key prevent an
    # ordinary duplicate delivery from creating a second revision line.
    if any(marker in prior for prior in _buf[chat_id]):
        return
    try:
        edit_ts = float(edited_at.timestamp())
    except Exception:
        edit_ts = time.time()
    live_meta = _meta.get(chat_id) or {}
    _buf_push(
        chat_id, f"{name} {marker}: {body}",
        author=name, is_dm=False,
        name=str(live_meta.get("title") or raw_topic_title or name),
        source_id=source_id, ts=edit_ts,
    )
    log.info("EDIT [%s] %s (id=%s): #%s archived=%s",
             chat_id, name, sender_id, mid, added)


def mid_of(message) -> int | None:
    """Идентификатор сообщения или None. Нужен реестру маршрутов как граница эпохи."""
    value = getattr(message, "id", None)
    return int(value) if isinstance(value, int) else None


def _note_room_mode_skip(peer_id, chat_id, mode: str, *, stage: str) -> None:
    profile = {}
    try:
        profile = rooms.profile_read(peer_id)
    except Exception:
        log.debug("room profile provenance упал [%s]", chat_id, exc_info=True)
    effective = str(profile.get("mode") or mode)
    # Провенанс режима уже лежал в meta — и всё равно КЛАСС был жёстко «запретил_егор»,
    # включая случай mode_set_by == "praxis". То есть собственную границу она читала как
    # чужой запрет, а именно от этой подмены авторства словарь классов и защищает.
    set_by = str(profile.get("mode_set_by") or "")
    klass = "мой_ритм" if set_by == "praxis" else "запретил_егор"
    perception.note_skip(
        stage, klass, chat_id=chat_id,
        detail=f"режим {effective}",
        meta={
            "mode": effective,
            "mode_set_by": profile.get("mode_set_by"),
            "mode_reason": profile.get("mode_reason"),
            "mode_until": profile.get("mode_until"),
        },
    )


@client.on(events.NewMessage(incoming=True))
async def on_new(event) -> None:
    msg = event.message
    text = msg.message or ""
    media = _media_tag(msg)
    is_private = bool(event.is_private)
    opener_title = "" if is_private else telegram_topics.topic_opener_title(msg)
    if opener_title and not text and not media:
        media = f"[Создан топик: {opener_title}]"
    if not text and not media:
        return
    sender = await event.get_sender()
    if sender is not None and getattr(sender, "is_self", False):
        return
    name = _sender_label(sender)
    route = telegram_topics.route_for_message(
        event.chat_id, msg, is_private=bool(is_private))
    peer_id = route.peer_id
    # All conversational state is topic-local.  Access/room policy below deliberately
    # continues to use peer_id: a topic is not a second Telegram room or authority.
    chat_id = route.conversation_id
    # Пункт 5, ТЕНЬ. Природа комнаты лежит прямо здесь и бесплатно: `event.chat` —
    # синхронное свойство, объект уже пришёл в контейнере апдейта, сети не будет.
    # Маршрутизацию НЕ трогаем — только копим свидетельства, чтобы перекладка ключа
    # однажды делалась по знанию. Объект с флагом `min` документирован как ненадёжный,
    # поэтому идёт как слабое свидетельство и не смеет перебить прямой ответ Telegram.
    if not is_private:
        try:
            chat_obj = getattr(event, "chat", None)
            if telegram_topics.is_topic_opener(msg):
                await asyncio.to_thread(
                    lambda: telegram_routes.observe(
                        peer_id, kind="topic_opener_seen", message_id=mid_of(msg),
                        detail=opener_title[:80]))
            elif chat_obj is not None:
                flag = getattr(chat_obj, "forum", None)
                kind = ("update_min_entity" if getattr(chat_obj, "min", False)
                        else ("entity_forum_flag" if flag is not None else "legacy_chat"))
                await asyncio.to_thread(
                    lambda: telegram_routes.observe(
                        peer_id, kind=kind, forum=bool(flag) if flag is not None else False,
                        message_id=mid_of(msg), detail=type(chat_obj).__name__))
            # ⚠ 03.08.2026, долг с 28.07. Природа «могу ли я сюда писать» лежит здесь
            # ровно так же бесплатно: `broadcast` — поле того же объекта, сети не будет.
            # Живьём 03.08 в 14:08 она ответила в вещательный канал AbstractDL, и узнала
            # об этом ПОСЛЕ — из отказа доставки и из поправки Егора вручную. Знание было
            # доступно ДО, и просто не спрашивалось.
            if chat_obj is not None and getattr(chat_obj, "broadcast", None) is not None:
                await asyncio.to_thread(
                    lambda: telegram_routes.note_writing(
                        peer_id, broadcast=bool(getattr(chat_obj, "broadcast", False))))
                await _note_linked_discussion(chat_obj, peer_id)
        except Exception:
            log.debug("реестр маршрутов: живое свидетельство не записалось [%s]",
                      peer_id, exc_info=True)
        # ⚠ ВЕЩАТЕЛЬНЫЙ КАНАЛ ЕЙ ЗАКРЫТ. Решение Егора 06.08: она живёт в ЧАТЕ канала, а
        # не в самом канале. Запись канала и так приезжает в связанное обсуждение
        # авторством канала — то есть ровно так, как её видит человек. Отдельное место
        # для той же записи даёт ей второй экземпляр одного события и комнату, в которой
        # у неё нет голоса.
        #
        # Это уже стоило живьём: 03.08 в 14:08 она ответила в вещательный канал и узнала
        # об этом ПОСЛЕ, из отказа доставки. Знание было доступно ДО и не спрашивалось.
        #
        # ⚑ КАЛИТКА СТОИТ ПОСЛЕ НАБЛЮДЕНИЯ НАМЕРЕННО: выше по этому же блоку записаны
        # природа места и адрес связанного обсуждения. Закрыть вход, не узнав, куда
        # ведёт дверь, значило бы оставить её без адреса чата навсегда.
        #
        # Закрывается только ПРЯМОЕ `broadcast=True` от Telegram. Молчание про природу
        # места не закрывает ничего: закрывают знанием, а не подозрением.
        #
        # Рычаг возврата — её условие приёмки: `PRAXIS_BROADCAST_INTAKE=1` открывает вход
        # обратно. Читается на КАЖДОМ событии, а не на импорте: рычаг, ради которого надо
        # перезапускать её, — это не рычаг, а ещё один повод не трогать.
        if (getattr(getattr(event, "chat", None), "broadcast", False)
                and os.getenv("PRAXIS_BROADCAST_INTAKE", "0").strip() not in ("1", "true", "yes")):
            log.info("канал [%s]: вход ей закрыт, её место — связанное обсуждение",
                     peer_id)
            return
    sender_id = getattr(event, "sender_id", None) or getattr(sender, "id", None)
    is_owner = OWNER_ID != 0 and sender_id == OWNER_ID
    if sender is not None:
        try:
            telegram_contacts.observe(sender, aliases=(name,), interacted=True)
            if sender_id is not None:
                _entity_cache[str(sender_id)] = sender
            handle = _telegram_handle(sender)
            if handle:
                _entity_cache[handle.casefold()] = sender
        except Exception:
            log.debug("адресная книга: отправитель не сохранился", exc_info=True)

    # PASS 9.0: catch_up может доиграть уже виденный апдейт — msg_id-сторож против дублей буфера
    mid = getattr(msg, "id", None)
    if mid is not None:
        if mid in _seen_ids[chat_id]:
            return
        _seen_ids[chat_id].append(mid)

    # Стоп-кран: /panic от владельца или хоста чужого пространства (PRAXIS_PANIC_IDS). Только стоп.
    if text.strip().lower() in ("/panic", "паника") and (is_owner or sender_id in PANIC_IDS):
        log.warning("PANIC от id=%s", sender_id)
        try:
            await asyncio.to_thread(agent.panic, f"by {sender_id}")
            await client.disconnect()
        except Exception:
            log.exception("panic")
        return

    if rooms.is_frozen(peer_id):
        _note_room_mode_skip(peer_id, chat_id, "frozen", stage="frozen")
        return  # пульт: чат заморожен — до неё не доходит (не бан)
    room_mode = "normal"
    room_policy = rooms.default_policy()
    if not is_private:
        if not rooms.is_allowed(peer_id, is_owner):
            perception.note_skip("departed", "явный leave", chat_id=chat_id)
            return
        # 10.3: режим комнаты из профиля (TTL-спуск ленивый); frozen/dead — как freeze
        try:
            room_mode = rooms.effective_mode(peer_id)
        except Exception:
            log.debug("effective_mode упал [%s]", chat_id, exc_info=True)
        if room_mode in ("frozen", "dead"):
            _note_room_mode_skip(peer_id, chat_id, room_mode, stage="room_mode")
            return
        try:
            room_policy = rooms.room_policy(peer_id)
        except Exception:
            log.debug("room_policy упал [%s]", chat_id, exc_info=True)

    cat = social.category(sender_id)
    known = cat in ("owner", "known")
    fam = bool(is_private and not is_owner and known and social.is_family(sender_id))  # 10.10
    # Незнакомец — полноценный разговор, не заявка владельцу. Первый контакт только
    # отмечается в её собственной памяти; заморозить неприятный чат она может сама.
    admission = None
    if is_private and not is_owner and cat == "unknown":
        today = datetime.date.today().isoformat()
        first, count = social.note_unknown(sender_id, today)
        admission = {"first": first, "count": count, "over_cap": False}

    mentioned = bool(getattr(msg, "mentioned", False))
    replied, reply_mark = False, ""
    reply_to_mid = getattr(msg, "reply_to_msg_id", None)
    if msg.is_reply:
        try:
            r = await msg.get_reply_message()
            if r is not None:
                replied = bool(_self_id and r.sender_id == _self_id)
                # PASS 15: она видит, НА ЧТО ответили — короткий гист цитируемого
                # (скобки/переносы срезаны: маркер должен сниматься _REPLY_MARKER_RE).
                tgt = re.sub(r"[()\n\r]", " ", (getattr(r, "message", "") or ""))
                tgt = re.sub(r"\s+", " ", tgt).strip()[:50]
                who = "Praxis" if replied else _sender_name(r)
                reply_mark = f" (в ответ {who}" + (f": «{tgt}»" if tgt else "") + ")"
        except Exception:
            pass
    named = agent._named(text)
    addressed = mentioned or replied or named
    # Все документы из допущенных чатов — в мастерскую, даже когда сообщение не было
    # адресовано Praxis. Addressing решает, просыпается ли голос, но не стирает её обзор.
    # Каталоги человеческие: workspace/inbox/groups/<чат> и private/<личка>.
    download_scope = ("group" if not is_private else "owner" if is_owner else
                      "family" if fam else "known" if known else "unknown")
    desc = await _chat_descriptor(event, peer_id)  # нужен и каталогу inbox, и мета хода
    if _wants_inbox(is_private, is_owner, msg, addressed):
        inbox_tag = await _inbox_download(
            msg, scope=download_scope, chat_id=chat_id,
            chat_kind="private" if is_private else "group",
            chat_label=name if is_private else str(desc.get("title") or ""))
        if inbox_tag:
            media = inbox_tag
    # Фото и аудио получают отдельный типизированный тракт. Скачиваем только ПОСЛЕ
    # room/access-гейтов выше; ref привязан к этому scope/chat и не попадёт в live-history другого.
    media_scope = download_scope
    ref, media_error = await _capture_typed_media(
        msg, chat_id=chat_id, scope=media_scope, caption=text)
    if ref is not None:
        _pending_media[chat_id].append(ref)
    elif media_error:
        media = media_error
    body = " ".join(x for x in (media, text) if x)  # медиа-конверт: тег + подпись
    try:
        message_ts = float(msg.date.timestamp()) if getattr(msg, "date", None) is not None else time.time()
    except Exception:
        message_ts = time.time()
    raw_topic_title = ""
    if route.topic_id is not None:
        cache_key = (peer_id, int(route.topic_id))
        raw_topic_title = opener_title or _topic_titles.get(cache_key, "")
        if not raw_topic_title:
            try:
                raw_topic_title = await asyncio.to_thread(
                    group_context.topic_title, peer_id, route.topic_id)
            except Exception:
                log.debug("topic title не прочитался [%s]", chat_id, exc_info=True)
        if raw_topic_title:
            _topic_titles[cache_key] = raw_topic_title
    topic_title = (
        f"{desc.get('title')} · {raw_topic_title or f'topic #{route.topic_id}'}"
        if route.topic_id is not None else desc.get("title")
    )
    buffer_name = name if route.topic_id is None else str(topic_title or name)
    _buf_push(chat_id, f"{name}{reply_mark}: {body}", author=name, is_dm=is_private,
              name=buffer_name,
              source_id=mid, ts=message_ts)
    # The archive sees every admitted group update, not only messages that wake the
    # model.  It is exact-topic and append-only; projection/search failures are loud
    # but must never make the live Telegram update disappear.
    if not is_private and mid is not None and _group_archive_enabled():
        try:
            if opener_title and route.topic_id is not None:
                await asyncio.to_thread(
                    group_context.record_topic,
                    peer_id, route.topic_id, opener_title,
                    timestamp=message_ts, message_id=mid,
                )
            await asyncio.to_thread(
                group_context.observe_message,
                peer_id=peer_id, topic_id=route.topic_id, message_id=mid,
                sender_id=sender_id, sender_name=name,
                reply_to_message_id=reply_to_mid, timestamp=message_ts,
                edited_at=getattr(msg, "edit_date", None),
                text=text, topic_title=raw_topic_title, media=media,
            )
        except Exception:
            log.exception("group archive не записал [%s] #%s", chat_id, mid)
    if mid is not None:
        try:
            # Имя и «это сам Егор» известны ровно здесь и уже посчитаны выше: в модуле
            # леджера OWNER_ID неизвестен вовсе. 27.07 02:31 раннер напечатал в лог
            # «получен ответ #94244 от Yegor Kosyrev» — и это знание никуда дальше не
            # ехало: в письмо шла метка ЧАТА, а то, что ответил сам адресат письма, не
            # проверял никто.
            matched = telegram_followups.LEDGER.observe_incoming(
                peer_id=peer_id, sender_id=sender_id, message_id=mid, text=body,
                reply_to_message_id=reply_to_mid, received_at=message_ts,
                sender_name=name, sender_is_owner=bool(is_owner),
            )
            if matched:
                log.info("FOLLOW-UP %s: получен ответ #%s от %s",
                         matched.get("id"), mid, name)
        except Exception:
            # Follow-up bookkeeping must never make a live incoming update disappear.
            log.exception("follow-up ledger не записал входящее [%s] #%s", chat_id, mid)
    # Passive group traffic also ages into memory; compaction cannot depend on
    # Praxis being addressed and producing an outgoing turn.
    if not _under_tests():
        asyncio.create_task(_maybe_compact(chat_id))
    if mid is not None:  # PASS 15: карта адресных ответов (ОТВЕТ->#id)
        _recent_msgs[chat_id].append((int(mid), name, body[:60]))
    if sender_id is not None:  # PASS 16.2: кэш отправителей для get_id (спамер-класс)
        _recent_senders[chat_id].append((time.time(), name, sender_id))
    # §2: прямое обращение — упоминание/реплай ей. Для группы его provenance живёт
    # отдельно в immutable GroupWake; rolling _meta остаётся только снимком последнего сообщения.
    # 9.6: голое имя из adressed УБРАНО (Егор: «на имя — ещё осторожнее») — оно не будит
    # и не поднимает флаг «к тебе обратились»; имя остаётся просто словом в буфере.
    suppress_boundary_reply = bool(
        not is_private and replied and _consume_boundary_reply(
            chat_id, reply_to_mid, sender_id, is_owner=is_owner))
    _meta[chat_id] = {"entity": event.chat_id, "peer_id": peer_id,
                      "topic_id": route.topic_id, "sender_id": sender_id,
                      "origin_message_id": (int(mid) if mid is not None else None),
                      "origin_text": str(text or ""),
                      "is_dm": is_private, "is_owner": is_owner,
                      "known": known, "family": fam, "name": name,
                      "title": topic_title, "size": desc.get("size"),
                      "addressed": bool(addressed),
                      "addressed_mid": (int(mid) if (addressed and mid is not None) else None),
                      "room_mode": room_mode, "room_policy": room_policy}
    log.info("MSG [%s] %s (id=%s, %s): %r", "DM" if is_private else chat_id, name, sender_id, cat, body[:60])

    # Незнакомец остаётся самостоятельным разговором Praxis: адрес уже сохранён в книге.
    if is_private and not is_owner and cat == "unknown":
        adm = admission or {}
        if adm["first"]:
            agent.tool_journal(f"[новый] {name} (id={sender_id}): {body[:200]}")

    # reflex — 0-токенный пре-фильтр: одиночный шум (смайл/«ок»/стикер) не будит проход,
    # но остаётся в буфере как контекст для следующего прохода.
    # Boundary provenance remains observable, but it has no authority to decide that a
    # person's next reply is "bait" or suppress Praxis before she sees it.
    if suppress_boundary_reply:
        log.info("reply to prior boundary [%s] #%s — visible to the normal voice pass",
                 chat_id, reply_to_mid)
    decision = reflex.triage(text, is_private=is_private, mentioned=mentioned,
                             replied_to_self=replied, named=named, media=media)
    engagement = (str(room_policy.get("engagement") or "reflective")
                  if room_mode == "normal" else "addressed")
    if not _should_wake(is_private, addressed, decision, engagement):
        # PASS 21: причина видна. ЛС-игнор (стикер-шум) — «не сочла важным»; групповой
        # неадресованный поток — не пропуск, а среда: копится в буфере, здесь только счётчик.
        if is_private:
            perception.note_skip("reflex", "не_сочла_важным", chat_id=chat_id,
                                 detail=media or f"шум, len={len(text)}")
        else:
            perception.note_ambient(chat_id)
        return
    if not is_private:
        msg_date = getattr(msg, "date", None)
        try:
            message_ts = float(msg_date.timestamp())
        except (AttributeError, TypeError, ValueError, OSError):
            message_ts = time.time()
        kind = ("mention+reply" if mentioned and replied else
                "mention" if mentioned else "reply" if replied else
                "name" if named else "ambient")
        wake = await asyncio.to_thread(
            _group_trigger_snapshot,
            chat_id, mid=mid, message_ts=message_ts, kind=kind,
            addressed=bool(addressed), query=body, name=name,
            sender_id=sender_id, is_owner=is_owner, known=known, family=fam,
        )
        if not _install_group_wake(chat_id, wake):
            perception.note_ambient(chat_id)
            return
    # PASS 9.0: чужая реплика в ЛС, которую она собралась разобрать, — кандидат в неотвеченные.
    # Запись переживает рестарт (armed-дебаунс — нет); снимет её ответ или решённое молчание.
    if is_private:
        try:
            unanswered.note_incoming(chat_id, name)
        except Exception:
            log.debug("unanswered.note_incoming упал [%s]", chat_id, exc_info=True)
    _arm(chat_id)


def _arm(chat_id: str) -> None:
    """Взвести/перевзвести дебаунс — всплеск склеивается в одну ситуацию."""
    # PASS 29: инкремент поколения == запланирован новый пасс-преемник. Бамп и отмена
    # текущего пасса синхронны (без await между ними), поэтому «gen изменился» строго
    # эквивалентно «преемник существует» и не может разойтись с реальностью. Это
    # единственный в процессе отменитель живого хода — значит отмена БЕЗ бампа = shutdown.
    _supersede_gen[chat_id] = _supersede_gen.get(chat_id, 0) + 1
    t = _debounce.get(chat_id)
    if t and not t.done():
        t.cancel()
    _debounce[chat_id] = asyncio.create_task(_debounced(chat_id))


async def _debounced(chat_id: str) -> None:
    try:
        try:
            wait = float(perception.value("debounce_sec"))  # PASS 21: живой рычаг
        except Exception:
            wait = DEBOUNCE_SEC
        await asyncio.sleep(wait)
    except asyncio.CancelledError:
        return
    await _run_pass(chat_id)


def _defer_pass(chat_id: str, delay: float) -> None:
    """§8: не ронять накопленное в кулдауне — перепланировать проход на остаток кулдауна."""
    t = _deferred.get(chat_id)
    if t and not t.done():
        return  # уже отложен — один таймер на чат

    async def _later():
        try:
            await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            return
        await _run_pass(chat_id)

    _deferred[chat_id] = asyncio.create_task(_later())


def _telegram_media_random_id(queue_id: str) -> int:
    """Stable signed int64 so retrying one durable queue id cannot duplicate a message."""
    raw = hashlib.sha256(f"praxis-media\0{queue_id}".encode("utf-8")).digest()[:8]
    value = int.from_bytes(raw, "big", signed=True)
    return value or 1


def _telegram_text_random_id(delivery_key: str) -> int:
    """Stable signed int64 for one logical text delivery or chunk."""
    raw = hashlib.sha256(f"praxis-text\0{delivery_key}".encode("utf-8")).digest()[:8]
    value = int.from_bytes(raw, "big", signed=True)
    return value or 1


async def _send_message_idempotent(entity, message: str, *, delivery_key: str,
                                   reply_to=None, random_id: int | None = None):
    """Telethon ``send_message`` with a caller-owned stable MTProto random id.

    Telegram de-duplicates retries carrying the same ``random_id``.  The public
    method does not expose that field, so the live client uses the equivalent
    raw request; small hermetic clients keep their public-method fallback.
    """
    random_id = (_telegram_text_random_id(delivery_key)
                 if random_id is None else int(random_id))
    if random_id == 0:
        raise ValueError("Telegram random_id must be non-zero")
    response_parser = inspect.getattr_static(type(client), "_get_response_message", None)
    if not (hasattr(client, "_parse_message_text")
            and response_parser is not None
            and hasattr(client, "get_input_entity") and callable(client)):
        kwargs = {"reply_to": reply_to} if reply_to is not None else {}
        sent = await client.send_message(entity, message, **kwargs)
        return sent, random_id

    from telethon.tl import functions as tl_functions, types as tl_types

    input_entity = await client.get_input_entity(entity)
    parsed, formatting_entities = await client._parse_message_text(str(message), ())
    if not parsed:
        raise ValueError("Telegram message cannot be empty")
    input_reply = (tl_types.InputReplyToMessage(reply_to_msg_id=int(reply_to))
                   if reply_to is not None else None)
    request = tl_functions.messages.SendMessageRequest(
        peer=input_entity,
        message=parsed,
        entities=formatting_entities,
        no_webpage=False,
        reply_to=input_reply,
        random_id=random_id,
    )
    response = await client(request)
    # Telegram may use this compact response for ordinary user dialogs.  The
    # runner only needs the accepted message id, not a fully hydrated Message.
    if isinstance(response, tl_types.UpdateShortSentMessage):
        sent = types.SimpleNamespace(id=response.id, message=parsed)
    else:
        if isinstance(response_parser, staticmethod):
            sent = response_parser.__func__(request, response, input_entity)
        else:
            sent = client._get_response_message(request, response, input_entity)
    if sent is None:
        raise RuntimeError("Telegram accepted no identifiable message")
    return sent, random_id


async def _send_file_idempotent(entity, item: media_core.OutboundMedia, *, reply_to=None,
                                random_id: int | None = None,
                                visible_filename: str | None = None):
    """Telethon send_file equivalent with a stable MTProto random_id.

    Uploading bytes may repeat after a crash; the visible SendMediaRequest does
    not.  Small hermetic test clients fall back to their public send_file stub.
    """
    random_id = (_telegram_media_random_id(item.queue_id)
                 if random_id is None else int(random_id))
    if random_id == 0:
        raise ValueError("Telegram random_id must be non-zero")
    force_document = item.kind == "document"
    if not (hasattr(client, "_file_to_media") and hasattr(client, "_parse_message_text")
            and hasattr(client, "_get_response_message")
            and hasattr(client, "get_input_entity") and callable(client)):
        send_kwargs = {
            "caption": item.caption[:900] or None,
            "reply_to": reply_to,
            "voice_note": bool(item.voice_note),
        }
        # Preserve the old photo/audio fallback call shape for small adapters;
        # only document delivery requires the additional Telethon flag.
        if force_document:
            send_kwargs["force_document"] = True
        sent = await client.send_file(entity, str(item.path), **send_kwargs)
        return sent, random_id

    from telethon.tl import functions, types

    input_entity = await client.get_input_entity(entity)
    conversion_kwargs = {
        "voice_note": bool(item.voice_note),
        "force_document": force_document,
    }
    if force_document:
        # The spool's collision-proof msg-<hash>-<nonce> prefix is private
        # implementation state.  Set Telegram's visible filename explicitly
        # so the recipient sees the requested artifact name only.
        conversion_kwargs["attributes"] = [types.DocumentAttributeFilename(
            file_name=(str(visible_filename).strip() if visible_filename
                       else media_core.delivery_basename(item.path)),
        )]
        conversion_kwargs["mime_type"] = item.mime
    _handle, input_media, _image = await client._file_to_media(
        str(item.path), **conversion_kwargs,
    )
    if not input_media:
        raise TypeError(f"cannot convert outbound media {item.path}")
    caption, formatting_entities = await client._parse_message_text(
        item.caption[:900], ())
    input_reply = (types.InputReplyToMessage(reply_to_msg_id=int(reply_to))
                   if reply_to is not None else None)
    request = functions.messages.SendMediaRequest(
        peer=input_entity,
        media=input_media,
        message=caption,
        entities=formatting_entities,
        reply_to=input_reply,
        random_id=random_id,
    )
    response = await client(request)
    return client._get_response_message(request, response, input_entity), random_id


def _direct_tool_execution(expected_tool: str | tuple) -> dict[str, object]:
    """Require the durable tool identity which owns a direct Telegram mutation.

    PASS 30 Этап 2: у моста может быть несколько законных хозяев (send_message И
    narrate) — контракт расширен до множества имён, идентичность по-прежнему
    обязана совпасть с реально исполняемым тулом."""

    expected = (expected_tool,) if isinstance(expected_tool, str) else tuple(expected_tool)
    label = "/".join(expected)
    execution = agent.current_tool_execution()
    if not isinstance(execution, dict):
        raise agent.DurableExecutionError(
            f"{label} requires a durable run/call identity"
        )
    run_id = str(execution.get("run_id") or "").strip()
    call_id = str(execution.get("call_id") or "").strip()
    tool = str(execution.get("tool") or "").strip()
    if not run_id or not call_id or tool not in expected:
        raise agent.DurableExecutionError(
            f"{label} has no matching durable run/call identity"
        )
    result = dict(execution)
    result["run_id"] = run_id
    result["call_id"] = call_id
    result["tool"] = tool
    return result


def _direct_tool_key(execution: dict[str, object]) -> str:
    key = str(execution.get("idempotency_key") or "").strip()
    if key:
        return key
    return (
        f"telegram-outbox:{execution['run_id']}:tool:{execution['call_id']}"
    )


def _one_sent_message_id(sent) -> int:
    rows = list(sent) if isinstance(sent, (list, tuple)) else [sent]
    ids = [int(getattr(row, "id")) for row in rows
           if getattr(row, "id", None) is not None]
    if len(ids) != 1 or ids[0] <= 0:
        raise RuntimeError("Telegram accepted no unique message id")
    return ids[0]


def _direct_outbox_result(entry: dict, *, label: str = "") -> str:
    """Deterministic tool receipt derived only from the durable acceptance row."""

    receipt = dict(entry.get("receipt") or {})
    message_id = receipt.get("message_id") or "?"
    peer_id = entry.get("peer_id")
    topic_id = entry.get("topic_id")
    selector = (telegram_topics.TopicRoute(str(peer_id), int(topic_id)).selector
                if topic_id is not None else str(peer_id))
    destination = str(label or selector)
    if entry.get("kind") == "file":
        filename = str((entry.get("payload") or {}).get("visible_filename") or "file")
        return (
            f"Отправила файл → {destination} "
            f"(chat_id={selector}, message_id={message_id}): {filename}"
        )
    text = str((entry.get("payload") or {}).get("text") or "")
    return (
        f"Отправила → {destination} "
        f"(chat_id={selector}, message_id={message_id}): {text[:60]}"
    )


async def _send_direct_outbox_entry(entry: dict, *, entity=None) -> dict:
    """Send one already-durable intent and persist acceptance before returning."""

    if entry.get("state") == "accepted":
        return entry
    if str(entry.get("purpose") or "").startswith("tool:"):
        prepared = await asyncio.to_thread(agent.direct_outbox_prepared, dict(entry))
        if not prepared:
            raise agent.DurableExecutionError(
                "direct Telegram tool entry has no exact pre-network run proof")
    peer_id = int(entry["peer_id"])
    if entity is None:
        entity = await _resolve_entity(peer_id)
    if entity is None:
        raise RuntimeError(f"Telegram peer is unavailable: {peer_id}")
    reply_to = entry.get("reply_to")
    if reply_to is None:
        reply_to = entry.get("topic_id")
    payload = dict(entry.get("payload") or {})
    if entry.get("kind") == "text":
        sent, random_id = await _send_message_idempotent(
            entity,
            str(payload.get("text") or ""),
            delivery_key=str(entry["key"]),
            reply_to=reply_to,
            random_id=int(entry["random_id"]),
        )
    elif entry.get("kind") == "file":
        item = media_core.OutboundMedia(
            kind="document",
            path=Path(str(payload["staged_path"])),
            mime=str(payload["mime"]),
            size=int(payload["size"]),
            target_chat_id=peer_id,
            scope=("group" if peer_id < 0 else "known"),
            caption=str(payload.get("caption") or ""),
            reply_to_message_id=reply_to,
            queue_id=str(entry["id"]),
            run_id=str(entry.get("run_id") or ""),
            sha256=str(payload.get("sha256") or ""),
        )
        sent, random_id = await _send_file_idempotent(
            entity,
            item,
            reply_to=reply_to,
            random_id=int(entry["random_id"]),
            visible_filename=str(payload.get("visible_filename") or "document.bin"),
        )
    else:
        raise RuntimeError(f"unsupported Telegram outbox kind: {entry.get('kind')}")
    message_id = _one_sent_message_id(sent)
    return await asyncio.to_thread(
        _direct_outbox().mark_accepted,
        str(entry["key"]),
        message_id=message_id,
        random_id=random_id,
    )


def _record_direct_outbox_failure(key: str, error: BaseException) -> tuple[bool, dict]:
    """Отказ Telegram в журнал: (постоянный?, новое состояние записи).

    ⚠ 26.07, живой инцидент. Она отправила пост в «@abstractDL» — а это КАНАЛ, не чат
    обсуждения, и прав писать там у неё нет. Telegram ответил ChatAdminRequiredError, то
    есть «нет и не будет». Журнал записал это как обычную неудачу, и часы резюма стали
    поднимать тот же ран каждые 45 секунд: 18:08:49, 18:09:02, 18:09:47, 18:10:35 и
    дальше без конца. Дверь закрыта навсегда, а стук не прекращался.

    Разница между «сейчас не вышло» и «здесь нельзя» — не оттенок, а два разных мира:
    первое стоит повторить, второе надо СКАЗАТЬ ей, чтобы она исправила адрес сама. Список
    постоянных отказов уже есть в agent и уже используется на другом пути доставки; берём
    его, а не заводим второй — иначе два пути разойдутся, а это ровно тот класс беды,
    который весь этот день и разбирали.
    """
    outbox = _direct_outbox()
    if agent._is_permanent_delivery_error(error):
        reason = (f"permanent Telegram refusal: {type(error).__name__}: "
                  f"{str(error)[:300]}")
        return True, outbox.dead_letter(key, reason)
    return False, outbox.record_retry(key, error)


async def _retry_direct_outbox_entry(entry: dict, error: BaseException) -> dict:
    permanent, state = await asyncio.to_thread(
        _record_direct_outbox_failure, str(entry["key"]), error,
    )
    if permanent:
        log.warning("direct Telegram outbox: постоянный отказ, больше не повторяю [%s]: %s",
                    entry.get("key"), error)
    return state


async def _reconcile_direct_outbox_entry(entry: dict) -> bool:
    """Project a transport receipt into its run ledger; never invoke the model."""

    key = str(entry.get("key") or "")
    if key in _DIRECT_OUTBOX_RECONCILED:
        return True
    purpose = str(entry.get("purpose") or "")
    if not purpose.startswith("tool:"):
        _DIRECT_OUTBOX_RECONCILED.add(key)
        return True
    reconcile = getattr(agent, "run_direct_outbox_accepted", None)
    if not callable(reconcile):
        return False
    try:
        reconciled = await asyncio.to_thread(reconcile, dict(entry))
    except Exception:
        log.exception("direct Telegram outbox reconciliation failed [%s]", key)
        return False
    if reconciled:
        if (entry.get("kind") == "file" and OWNER_ID
                and int(entry.get("peer_id") or 0) == int(OWNER_ID)):
            payload = dict(entry.get("payload") or {})
            run_id = str(entry.get("run_id") or "")
            filename = str(payload.get("visible_filename") or "document.bin")[:240]
            await asyncio.to_thread(
                owner_delivery.LEDGER.emit,
                "file_ready",
                title=f"Файл готов: {filename}",
                body=str(payload.get("caption") or "")[:1200],
                outcome="success",
                thread_key=f"run:{run_id}" if run_id else "telegram-file",
                correlation={
                    "run_id": run_id,
                    "message_id": str((entry.get("receipt") or {}).get("message_id") or ""),
                    "sha256": str(payload.get("sha256") or ""),
                },
                reason="Telegram принял файл под стабильным random_id.",
                provenance={"source": "direct_telegram_outbox", "source_id": key},
                expectation="Файл уже в Telegram; полный след и артефакты доступны в run.",
                action={
                    "label": "Открыть run", "domain": "praxis",
                    "action": "run.open", "run_id": run_id,
                } if run_id else {},
                result=filename,
                dedupe_key=f"direct-file:{key}",
                transports=("pwa",),
            )
        _DIRECT_OUTBOX_RECONCILED.add(key)
        return True
    return False


async def _direct_outbox_once() -> None:
    """Replay due direct/scheduled sends with their original random ids, no LLM."""

    outbox = _direct_outbox()
    pending = await asyncio.to_thread(
        outbox.pending, due_only=True, verify_files=True,
    )
    for entry in pending[:100]:
        try:
            accepted = await _send_direct_outbox_entry(entry)
        except Exception as exc:
            try:
                state = await _retry_direct_outbox_entry(entry, exc)
                log.warning(
                    "direct Telegram outbox retry [%s] state=%s attempts=%s: %s",
                    entry.get("key"), state.get("state"), state.get("attempts"), exc,
                )
            except Exception:
                log.exception("direct Telegram outbox retry journal failed [%s]", entry.get("key"))
            continue
        await _reconcile_direct_outbox_entry(accepted)

    accepted_rows = await asyncio.to_thread(outbox.accepted, verify_files=False)
    for entry in accepted_rows:
        if str(entry.get("key") or "") in _DIRECT_OUTBOX_RECONCILED:
            continue
        await _reconcile_direct_outbox_entry(entry)


async def _send_turn_media(entity, item: media_core.OutboundMedia, *,
                           ctx: agent.ChannelContext, reply_to=None,
                           state_chat_id: str | None = None) -> dict | None:
    """Return a Telegram acceptance receipt; persistence failures never cause a resend."""
    if item.run_id:
        try:
            started = await asyncio.to_thread(
                agent.run_delivery_media_started, item.run_id, item.queue_id,
            )
        except Exception:
            log.exception("media tool-start receipt не записался [%s]", item.queue_id)
            return None
        if not started:
            log.error("media tool-start receipt отсутствует [%s]", item.queue_id)
            return None
    try:
        _media_spool().validate_outbound(
            item, expected_scope=ctx.scope, expected_chat_id=ctx.chat_id)
        media_reply_to = (item.reply_to_message_id
                          if item.reply_to_message_id is not None else reply_to)
        sent, random_id = await _send_file_idempotent(
            entity, item, reply_to=media_reply_to,
        )
    except Exception as exc:
        # ⚠ Классификация постоянного отказа снимается ЗДЕСЬ, с самого исключения.
        # Раньше наверх уезжала строка «queued for retry», а `_is_permanent_delivery_error`
        # по построению отвечает False на строку — то есть медийный шов не мог отличить
        # «сеть моргнула» от «сюда файлы нельзя» в принципе. 03.08 это стоило 728 отказов
        # за тринадцать часов и потерянной работы, о которой она не знала.
        permanent = agent._is_permanent_delivery_error(exc)
        log.log(logging.ERROR if not permanent else logging.WARNING,
                "исходящее медиа не отправилось [%s]%s", ctx.chat_id,
                " — маршрут отказал НАВСЕГДА, повторять не буду" if permanent else "",
                exc_info=not permanent)
        if permanent:
            log.warning("файл остался у неё: %s (%s)", item.path, type(exc).__name__)
        if item.run_id:
            try:
                await asyncio.to_thread(
                    agent.run_delivery_media_result, item.run_id, item.queue_id,
                    ok=False,
                    error=(f"{type(exc).__name__}: {exc}"[:200] if permanent
                           else "Telegram media upload failed; queued for retry"),
                    permanent=permanent,
                    chat_id=str(ctx.chat_id or ""),
                    path=str(getattr(item, "path", "") or ""),
                    caption=str(getattr(item, "caption", "") or ""),
                )
            except Exception:
                log.exception("media failure receipt не записался [%s]", item.queue_id)
        return None

    receipt = {
        "message_id": getattr(sent, "id", None),
        "random_id": random_id,
        "accepted_at": time.time(),
    }
    try:
        tag = {
            "photo": "[Изображение]",
            "audio": "[Аудио]",
            "document": "[Файл]",
        }.get(item.kind, "[Медиа]")
        _buf_push(str(state_chat_id or ctx.chat_id),
                  f"Praxis: {tag}" + (f" {item.caption}" if item.caption else ""),
                  author="Praxis", is_dm=ctx.is_dm,
                  source_id=str(receipt["message_id"] or ""), ts=receipt["accepted_at"])
    except Exception:
        log.exception("принятое Telegram media не попало в hot buffer [%s]", item.queue_id)
    log.info("МЕДИА(%s) [%s] -> %s id=%s", item.kind, ctx.chat_id, item.path.name,
             receipt["message_id"])
    return receipt


async def _attempt_queued_media(entity, item: media_core.OutboundMedia, *,
                                ctx: agent.ChannelContext, reply_to=None,
                                finalize_recovered: bool = True,
                                state_chat_id: str | None = None) -> bool:
    """One in-flight upload per queue id; acknowledge the queue only after success."""
    if item.queue_id in _MEDIA_SENDING:
        return False
    _MEDIA_SENDING.add(item.queue_id)
    try:
        spool = _media_spool()
        receipt = _MEDIA_ACCEPTED.get(item.queue_id)
        if receipt is None:
            receipt = await _send_turn_media(
                entity, item, ctx=ctx, reply_to=reply_to,
                state_chat_id=state_chat_id,
            )
        if receipt is None:
            return False

        # Telegram has accepted the stable random_id.  From this point onward
        # no bookkeeping exception is allowed to turn acceptance into a resend.
        acknowledged = False
        for delay in (0.0, 0.05, 0.2):
            if delay:
                await asyncio.sleep(delay)
            try:
                changed = await asyncio.to_thread(
                    spool.discard, item.queue_id, receipt=receipt,
                )
                if changed or any(
                        row.get("queue_id") == item.queue_id
                        for row in await asyncio.to_thread(spool.outbox_results, "delivered")):
                    acknowledged = True
                    break
            except Exception:
                log.exception("media acceptance tombstone не записался [%s]", item.queue_id)
        if acknowledged:
            _MEDIA_ACCEPTED.pop(item.queue_id, None)
        else:
            _MEDIA_ACCEPTED[item.queue_id] = dict(receipt)
            log.critical("Telegram принял media, но outbox ack пока не записан [%s]",
                         item.queue_id)

        run_receipt = not bool(item.run_id)
        if item.run_id:
            try:
                await asyncio.to_thread(
                    agent.run_delivery_media_result, item.run_id, item.queue_id,
                    ok=True, message_id=receipt.get("message_id"),
                )
                run_receipt = True
            except Exception:
                # The outbox tombstone is now the recovery source for this
                # secondary run receipt.  Keep the staged bytes until the
                # tombstone has been projected into the run WAL; never retry
                # Telegram because of this bookkeeping gap.
                log.exception("media run receipt не записался после acceptance [%s]",
                              item.queue_id)
        if acknowledged and run_receipt:
            try:
                item.path.unlink(missing_ok=True)
            except OSError:
                pass
        if (acknowledged and run_receipt and finalize_recovered and item.run_id
                and not any(pending.run_id == item.run_id for pending in spool.pending())):
            await asyncio.to_thread(
                agent.run_delivery_finalize_recovered, item.run_id, media_count=1,
            )
        return True
    finally:
        _MEDIA_SENDING.discard(item.queue_id)


async def _queue_and_send_media(entity, item: media_core.OutboundMedia, *,
                                ctx: agent.ChannelContext, reply_to=None,
                                state_chat_id: str | None = None) -> bool:
    """Persist the retry intent in the bounded in-process queue before first upload."""
    # The original address is durable routing provenance.  Without persisting it, a
    # failed forum upload retries into General after restart.
    if item.reply_to_message_id is None and reply_to is not None:
        item = replace(item, reply_to_message_id=reply_to)
    spool = _media_spool()
    if not any(pending.queue_id == item.queue_id for pending in spool.pending()):
        try:
            spool.enqueue(item)
        except media_core.MediaError:
            log.exception("исходящее медиа не встало в retry-очередь [%s]", ctx.chat_id)
            return False
    return await _attempt_queued_media(
        entity, item, ctx=ctx, reply_to=reply_to, finalize_recovered=False,
        state_chat_id=state_chat_id,
    )


def _consume_pending_media(chat_id: str, refs: tuple[media_core.MediaRef, ...]) -> None:
    """Detach only refs from a terminally processed attempt; keep newer concurrent arrivals."""
    queue = _pending_media.get(chat_id)
    if not queue:
        return
    for ref in refs:
        try:
            queue.remove(ref)
        except ValueError:
            pass
    if not queue:
        _pending_media.pop(chat_id, None)


TELEGRAM_TEXT_CHUNK_UTF16 = 3800


def _utf16_units(text: str) -> int:
    """Telegram measures message length in UTF-16 code units, not Python chars."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in (text or ""))


def _split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_CHUNK_UTF16) -> tuple[str, ...]:
    """Losslessly split text below Telegram's UTF-16 limit.

    Prefer a paragraph, then newline, then whitespace boundary in the latter half
    of the safe window. A pathological unbroken token falls back to the exact
    code-point boundary; surrogate pairs are never split because Python exposes
    a non-BMP character as one code point.
    """
    text = str(text or "")
    if not text:
        return ()
    if limit < 1:
        raise ValueError("Telegram text limit must be positive")
    chunks: list[str] = []
    start = 0
    while start < len(text):
        units = 0
        hard_end = start
        while hard_end < len(text):
            width = 2 if ord(text[hard_end]) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            hard_end += 1
        if hard_end == start:
            raise ValueError("one character exceeds the Telegram text limit")
        split_at = hard_end
        if hard_end < len(text):
            floor = start + max(1, (hard_end - start) // 2)
            paragraph = text.rfind("\n\n", floor, hard_end)
            if paragraph >= floor:
                split_at = paragraph + 2
            else:
                newline = text.rfind("\n", floor, hard_end)
                if newline >= floor:
                    split_at = newline + 1
                else:
                    for pos in range(hard_end - 1, floor - 1, -1):
                        if text[pos].isspace():
                            split_at = pos + 1
                            break
        chunks.append(text[start:split_at])
        start = split_at
    assert "".join(chunks) == text
    assert all(_utf16_units(chunk) <= limit for chunk in chunks)
    return tuple(chunks)


async def _await_despite_cancellation(awaitable):
    """Return an awaitable's result and whether cancellation arrived meanwhile."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _voice_turn_offloaded(*args, **kwargs):
    return await asyncio.to_thread(agent.voice_turn_envelope, *args, **kwargs)


async def _voice_turn_with_typing(entity, *args, **kwargs):
    async with client.action(entity, "typing"):
        return await _voice_turn_offloaded(*args, **kwargs)


async def _run_pass(chat_id: str) -> None:
    """Ход по чату (PASS 8.1): и личка, и группа — голос. Путь: reflex (в on_new) → voice →
    audience-aware finalizer (в owner-DM без оценки речи) → send | [молчу]. Фокус-окно она открывает
    сама тулом focus — планировщик часов подхватит намерение на ближайшем тике."""
    if chat_id in _passing:
        # PASS 21: раньше ЛС-триггер здесь ТЕРЯЛСЯ (асимметрия с group-rearm в finally) —
        # теперь помечаем и перевзводим после хода; причина пропуска видна.
        if _meta.get(chat_id, {}).get("is_dm", True):
            _dm_rearm.add(chat_id)
            perception.note_skip("busy", "отложила", chat_id=chat_id,
                                 detail="ход уже идёт — вернусь после него")
        return
    meta = _meta.get(chat_id, {})
    is_dm = meta.get("is_dm", True)
    persisted_route = _route_from_state(chat_id)
    peer_id = str(meta.get("peer_id") or persisted_route.peer_id)
    topic_id = meta.get("topic_id", persisted_route.topic_id)
    route = telegram_topics.TopicRoute(peer_id, topic_id)
    wake = None if is_dm else _group_wakes.get(chat_id)
    # Отложенный таймер несёт только chat_id и может пережить уже завершённый/заменённый
    # ход. Без живого immutable wake он не имеет права брать свежий хвост Telegram.
    if not is_dm and wake is None:
        log.debug("устаревший групповой проход без wake [%s] — no-op", chat_id)
        perception.note_skip("stale_wake", "отложила", chat_id=chat_id,
                             detail="проход пережил свой wake")
        return
    elapsed = time.time() - _last_pass[chat_id]
    addressed = bool(meta.get("addressed", False)) if is_dm else bool(wake.addressed)
    cd = _cooldown(is_dm, meta.get("room_mode", "normal"), addressed=addressed)
    if elapsed < cd:
        _defer_pass(chat_id, cd - elapsed + 0.05)  # transport retry, не task и не loop
        perception.note_skip("cooldown", "отложила", chat_id=chat_id,
                             detail=(f"transport retry через {cd - elapsed:.0f}с; "
                                     "после него актуальность решается заново"))
        return
    _passing.add(chat_id)
    armed_gen = _supersede_gen.get(chat_id, 0)  # PASS 29: снимок ДО первого await
    addressed_mid = meta.get("addressed_mid") if is_dm else (
        wake.message_id if wake.addressed else None)
    terminal = False
    superseded_abandon = False  # PASS 29: этот черновик брошен в пользу преемника
    cancellation_seen = False
    delivery_run_id = ""
    delivery_message_ids: list[str] = []
    # Она одна: этот живой ход держит единый когнитивный проход, пока идёт. Второй чат или
    # автономное окно не побегут параллельно (окно, увидев замок, отложится). Освобождаем
    # в finally.
    #
    # ⚠ Ожидание замка — снаружи try, поэтому отмена ЗДЕСЬ не дошла бы до finally и
    # оставила бы chat_id в `_passing` навсегда: дальше каждый проход этого чата выходит
    # на первой же строке, и комната глохнет насовсем. Раньше сюда было не попасть — при
    # закрытом на окно Telethon новое сообщение не приходило, а значит и `_arm` не
    # отменял дебаунс-таск (об этом и говорил прежний комментарий). 26.07 пульс перестал
    # рвать связь, и ждать замка стало можно минутами ПОД живым входящим потоком: второе
    # сообщение в тот же чат отменяет таск ровно на этом await. Держим свою уборку сами.
    try:
        await _ONE_MIND.acquire()
    except BaseException:
        _passing.discard(chat_id)
        raise
    try:
        _last_pass[chat_id] = time.time()
        # Тот же разговор, но ролями: её реплики поедут в модель как ЕЁ реплики, а не
        # строками «Praxis: …» / «[…; Praxis [id …]] …» внутри чужого текста. Сплошная
        # склейка при этом никуда не девается — на ней стоят расписки, исходящая граница
        # и прожитый ход.
        turn_history: list[dict] = []
        turn_current = ""
        if is_dm:
            last_n = await _last_n_text(chat_id)
            turn_history, turn_current = await asyncio.to_thread(_dm_dialogue, chat_id)
            turn_media = tuple(_pending_media.get(chat_id, ()))
            speaker = meta.get("name")
            owner = meta.get("is_owner", False)
            known = meta.get("known", True)
            family = meta.get("family", False)
            reply_targets = tuple(_recent_msgs[chat_id])
            address_kind = None
            address_age_sec = None
        else:
            # Кулдаун может длиться минуты: исходный wake остаётся неизменным, но новый
            # разговор после него виден отдельно для свежего решения об актуальности.
            current_context, current_turns = _group_context_frozen(
                chat_id, meta.get("room_policy") or _room_policy_for_state(chat_id))
            last_n = wake.context_snapshot
            group_turns = tuple(wake.turns_snapshot)
            if current_context and current_context != wake.context_snapshot:
                # ⚠ Здесь приклеивался ВЕСЬ свежий снимок комнаты — то есть ранние
                # сообщения уезжали в её кадр по второму разу, до +20 000 символов
                # дословного дубля на ход (замер 03.08). Показываем ДОБАВИВШЕЕСЯ.
                #
                # Сравнение идёт по записям и по их содержимому, а не по префиксу строк:
                # у ленты есть служебные вставки (корень ветки, пометка обреза), и
                # пометка МЕНЯЕТСЯ вместе с числом показанных сообщений — на префиксе
                # это разошлось бы каждый раз. Отредактированное сообщение при этом
                # честно приезжает ещё раз: оно и правда стало другим.
                known_lines = {line for _s, line, _r in group_turns}
                extra = tuple(item for item in current_turns
                              if item[1] not in known_lines)
                if not group_turns and not current_turns:
                    # Авторства нет ни там, ни там (запасной путь по буферу) — прежнее
                    # поведение: показываем свежий снимок целиком.
                    extra = ()
                    addition = current_context
                else:
                    addition = "\n".join(line for _s, line, _r in extra)
                if addition.strip():
                    note = ("\n\n---\n[После исходной реплики разговор продолжился; "
                            "это контекст для новой проверки актуальности, а не новая задача.]\n")
                    last_n += note + addition.lstrip("\n")
                    if group_turns and extra:
                        group_turns = group_turns + ((False, note.strip(), note.strip()),) + extra
            turn_history, turn_current = _group_dialogue(group_turns)
            turn_media = wake.media_snapshot
            speaker = wake.speaker if wake.addressed else None
            owner = wake.owner
            known = wake.known if wake.addressed else True
            family = wake.family
            reply_targets = wake.reply_targets_snapshot
            address_kind = wake.kind
            address_age_sec = max(0.0, time.time() - wake.message_ts)
            log.info("%s проход [%s] закреплён за #%s (возраст %.1fс)",
                     "адресный" if wake.addressed else "reflective ambient",
                     chat_id, wake.message_id, address_age_sec)
        entity = meta.get("entity", peer_id)
        # Пункт 4, ТЕНЬ. Настоящий отправитель известен прямо здесь и прямо здесь же
        # ниже затирается на praxis:self, когда пробуждение не адресное. Меряем факт до
        # подмены; поведение не меняется ни на строку — конверт никем не читается.
        _raw_actor = (meta.get("sender_id") if is_dm
                      else (wake.sender_id if wake is not None else None))
        _synth = bool(not is_dm and wake is not None and not wake.addressed)
        _origin_mid = (meta.get("origin_message_id") if is_dm
                       else (wake.message_id if wake is not None else None))
        _envelope = context_envelope.measure(
            chat_id=chat_id, room_id=peer_id, is_dm=is_dm, owner=owner,
            praxis_self=_synth, actor_raw=_raw_actor, synthesized=_synth,
            triggers=((context_envelope.Trigger(
                principal_id=str(_raw_actor), message_id=_origin_mid,
                ts=time.time(), kind=("dm" if is_dm else (
                    "addressed" if (wake is not None and wake.addressed) else "ambient"))),)
                if _raw_actor is not None else ()),
            delegation_ref=("wake:not_addressed" if _synth else ""),
            origin_message_id=_origin_mid,
            origin_addressed=bool(is_dm or (wake is not None and wake.addressed)),
        )
        # §2: единый объект канала (scope + осознанность чата) — один источник правды на весь ход
        ctx = agent.ChannelContext(
            # Agent-local state (notes, runs, inbound/outbound media guards) is
            # conversation-scoped.  The runner keeps the real peer/topic route and
            # is the only layer that translates it into Telethon delivery arguments.
            chat_id=chat_id, room_id=peer_id,
            principal_id=(meta.get("sender_id") if is_dm else
                          wake.sender_id if wake.addressed else agent.PRAXIS_SELF_PRINCIPAL),
            origin_message_id=(meta.get("origin_message_id") if is_dm else wake.message_id),
            # ⚠ В группе это поле оставалось ПУСТЫМ, хотя текст обращения лежит рядом, в
            # снимке пробуждения. Из-за пустоты поиск по её памяти звался всем контекстом
            # комнаты целиком: замер 26.07 — 40104 символа вместо 170, 21.2с вместо 3.8с,
            # и находки мимо (всплывала чужая архитектурная выкладка вместо самой темы).
            # Склейка комнаты это ухудшила: запрос вырос с ветки до всего места.
            origin_text=(str(meta.get("origin_text") or "") if is_dm
                         else str((wake.query if wake is not None else "") or "")),
            is_dm=is_dm, owner=owner,
            known=known, family=family,  # 10.10
            addressed=addressed,
            address_message_id=addressed_mid if not is_dm else None,
            address_kind=address_kind,
            address_age_sec=address_age_sec,
            title=meta.get("title"), size=meta.get("size"),
            missed_hours=_missed.pop(chat_id, None),  # 9.0: честная метка «я была офлайн»
            reply_targets=reply_targets,  # 15: карта адресных ответов на момент триггера
            envelope=_envelope,           # пункт 4: теневой замер, никем не читается
        )
        topic_orient = ""
        if route.topic_id is not None:
            room_policy = meta.get("room_policy") or _room_policy_for_state(chat_id)
            # ⚠ Здесь ей БЕЗУСЛОВНО сообщалось «Telegram forum topic … isolated from
            # every other topic in the same group». В обычной супергруппе ложна каждая
            # часть: форума нет, «тема» — это одна цепочка ответов в той же комнате, а
            # «изоляция» — артефакт расщеплённого ключа, а не Telegram. Измерено 25.07:
            # в AbstractDL 4138 сообщений разложены на 279 таких «тем». Реестр уже
            # знает природу комнаты — читаем его и говорим ей то, что есть. Ключ при
            # этом не меняется: это правка ФРАЗЫ, а не маршрута.
            forum_status, _epoch = telegram_routes.status_at(
                route.peer_id, route.topic_id)
            topic_orient = telegram_routes.orientation_line(
                route.peer_id, route.topic_id, route.selector, forum_status)
            # agent prompt assembly injects this topic's canonical continuity once.
            try:
                cross = await asyncio.to_thread(
                    group_context.orientation_bundle,
                    route.peer_id, current_topic=route.topic_id,
                    query=(wake.query if wake is not None else ""),
                    cross_topics=room_policy.get("cross_topics", "off"),
                    max_chars=max(3000, int(room_policy.get("context_summary_chars") or 7000)),
                    # Карта идёт строкой ниже ориентации и обязана говорить то же самое.
                    # Но карта — про КОМНАТУ: каждая ветка называется по своей природе,
                    # иначе настоящая тема форума превращается в «reply thread», то есть
                    # единственная граница, которую Telegram провёл, объявляется нашей.
                    not_a_forum=(forum_status == telegram_routes.FALSE),
                    artifacts=telegram_routes.artifacts_of(route.peer_id),
                )
            except Exception:
                cross = ""
                log.exception("cross-topic orientation не собрался [%s]", chat_id)
            if cross:
                topic_orient += "\n\n" + cross
        topic_token = _TURN_TOPIC_ROUTE.set(route)
        try:
            if is_dm:
                envelope, cancellation_seen = await _await_despite_cancellation(
                    _voice_turn_with_typing(
                        entity, chat_id, last_n, speaker,
                        ctx=ctx, orient=topic_orient, media_refs=turn_media,
                        history=turn_history, current_text=turn_current,
                    )
                )
            else:
                # в группе без «печатает…»: тишина ([молчу]) — частый честный исход, не изображаем набор
                envelope, cancellation_seen = await _await_despite_cancellation(
                    _voice_turn_offloaded(
                        chat_id, last_n, speaker,
                        ctx=ctx, orient=topic_orient, media_refs=turn_media,
                        history=turn_history, current_text=turn_current,
                    )
                )
        finally:
            _TURN_TOPIC_ROUTE.reset(topic_token)
        if envelope.deferred:
            delivery_run_id = str(envelope.run_id or "")
            terminal = True
            log.warning("durable turn deferred at checkpoint [%s]", delivery_run_id or chat_id)
            if cancellation_seen:
                raise asyncio.CancelledError
            return
        if envelope.failed:
            delivery_run_id = str(envelope.run_id or "")
            terminal = True
            log.error("durable turn failed before delivery [%s]", delivery_run_id or chat_id)
            if cancellation_seen:
                raise asyncio.CancelledError
            return
        if envelope.retry_media:
            if cancellation_seen:
                raise asyncio.CancelledError
            return  # same immutable input/media will be re-armed in finally
        delivery_run_id = str(envelope.run_id or "")
        reply = str(envelope.text or "")
        directed = None
        if reply:
            # PASS 15: её выбор адресата (ОТВЕТ->#id) главнее; фолбэк — в группе при прямом
            # обращении её ответ уходит телеграм-реплаем на обратившееся сообщение.
            reply, directed = agent.split_reply_directive(
                reply, {m for m, _a, _g in reply_targets})
        reply_to = directed if directed is not None else (
            addressed_mid if (addressed and not is_dm) else route.topic_id)
        has_text = bool(reply.strip())
        has_media = bool(envelope.outbound)
        if not has_text and not has_media:
            if delivery_run_id:
                _completed, silent_cancelled = await _await_despite_cancellation(
                    asyncio.to_thread(
                        agent.run_delivery_completed, delivery_run_id, silent=True,
                    )
                )
                cancellation_seen = cancellation_seen or silent_cancelled
                # The silent decision is durably completed and owned; never re-arm the
                # wake to re-author it, even if a cancellation was observed mid-commit.
                terminal = True
            if cancellation_seen and not terminal:
                # Hermetic caller with no durable run: retain and re-arm the same wake.
                raise asyncio.CancelledError
            terminal = True  # [молчу]/empty voice is a completed decision
            return

        # A model-authored output is now owned by this durable delivery, even
        # when it is media-only. Never re-arm the same group wake and ask the
        # model to invent the output again after a transport failure.
        chunks = _split_telegram_text(reply) if has_text else ()
        if delivery_run_id:
            delivery_kwargs = {
                "chat_id": chat_id,
                "text_chars": len(reply) if has_text else 0,
                "media_count": len(envelope.outbound),
                "media_queue_ids": [str(item.queue_id) for item in envelope.outbound],
            }
            if chunks:
                delivery_kwargs["text_plan"] = {
                    "schema": agent._TELEGRAM_TEXT_PLAN_SCHEMA,
                    "conversation_id": str(chat_id),
                    "peer_id": str(route.peer_id),
                    "topic_id": route.topic_id,
                    "chunks": [{
                        "index": index,
                        "text": chunk,
                        "sha256": hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                        "delivery_key": f"run:{delivery_run_id}:chunk:{index}",
                        "reply_to": (reply_to if index == 0 else route.topic_id),
                    } for index, chunk in enumerate(chunks)],
                }
                _TEXT_SENDING.add(delivery_run_id)
            try:
                started, handoff_cancelled = await _await_despite_cancellation(
                    asyncio.to_thread(
                        agent.run_delivery_started, delivery_run_id, **delivery_kwargs,
                    )
                )
                cancellation_seen = cancellation_seen or handoff_cancelled
                if not started:
                    raise RuntimeError(
                        "durable Telegram delivery intent was not accepted for this run")
                if cancellation_seen:
                    # Nothing has reached Telegram yet.  Two indistinguishable cancels
                    # land here: (a) a newer trigger ran on_new→_arm, which bumped
                    # _supersede_gen AND scheduled a fresh pass that will re-author to the
                    # current state; (b) shutdown/teardown cancelled us directly, with no
                    # successor.  The gen snapshot tells them apart exactly (see _arm).
                    superseded = _supersede_gen.get(chat_id, 0) != armed_gen
                    # Abandon the interrupted draft TERMINALLY — so no recovery clock ever
                    # mails the reply she never consciously sent (the «автоотбойник») —
                    # ONLY when a fresh reply is durably guaranteed, never trading the
                    # disliked bounce for a silent DROP:
                    #   • DMs: the superseding message is durable (unanswered.note_incoming
                    #     + _missed_dm_sweep re-author after a crash) → always drop-safe.
                    #   • Groups: the successor is an in-memory pass with no persisted wake,
                    #     so abandoning is drop-safe only while the process lives to run it.
                    #     During shutdown we keep deployed recovery semantics (boot
                    #     text_outbox replays her authored reply) rather than risk a drop.
                    if superseded and (is_dm or not _SHUTDOWN.is_set()):
                        abandoned, _late = await _await_despite_cancellation(
                            asyncio.to_thread(
                                agent.run_delivery_superseded, delivery_run_id,
                                reason="superseded by a newer trigger before send",
                            )
                        )
                        if abandoned:
                            # Run is now 'cancelled': text_outbox + resume skip it, so the
                            # persisted text_plan is UNREPLAYABLE.  The already-armed
                            # successor owns the re-author; the finally must not
                            # replay/consume anything the successor owns.
                            superseded_abandon = True
                            terminal = True
                        else:
                            # Terminalisation did not stick (rare) — keep deployed recovery
                            # semantics so her authored reply is still delivered; never drop.
                            terminal = bool(delivery_run_id) and not has_media
                    else:
                        # No successor, or a group held for durability during shutdown:
                        # deployed behaviour — text recovers via text_outbox, media re-arms.
                        terminal = bool(delivery_run_id) and not has_media
                    raise asyncio.CancelledError
            except Exception as exc:
                # No Telegram call has happened.  The authored model output is
                # already durable, so consume the trigger and let run recovery
                # prepare/replay delivery instead of asking the model again.
                await asyncio.to_thread(
                    agent.run_delivery_blocked, delivery_run_id,
                    reason=("Telegram delivery intent did not persist before send: "
                            f"{type(exc).__name__}: {exc}"),
                )
                terminal = True
                return
        # Once a durable run owns the authored output, recovery can finish the
        # exact delivery without asking the model again.  Hermetic/legacy
        # callers without a run id do not have that safety net: a failure before
        # the first accepted side effect must retain and re-arm the same wake.
        if cancellation_seen and not delivery_run_id:
            raise asyncio.CancelledError
        terminal = bool(delivery_run_id)

        sent_chunks: list[str] = []
        sent_ids: list[str] = []
        send_error: BaseException | None = None
        provocateur_id = wake.sender_id if wake is not None else None
        for index, chunk in enumerate(chunks):
            # Every chunk must stay in the forum topic. Only the first chunk is a
            # semantic reply to the addressed message; the rest attach to topic root.
            chunk_reply_to = reply_to if index == 0 else route.topic_id
            try:
                text_delivery_key = (
                    f"run:{delivery_run_id}:chunk:{index}"
                    if delivery_run_id else
                    "turn:" + hashlib.sha256((
                        f"{chat_id}\0{addressed_mid}\0{reply}\0{index}"
                    ).encode("utf-8", errors="replace")).hexdigest()
                )
                sent, _random_id = await _send_message_idempotent(
                    entity, chunk, delivery_key=text_delivery_key,
                    reply_to=chunk_reply_to,
                )
            except BaseException as exc:
                send_error = exc
                break
            if delivery_run_id:
                try:
                    await asyncio.to_thread(
                        agent.run_delivery_text_chunk_accepted,
                        delivery_run_id, index=index,
                        delivery_key=text_delivery_key,
                        message_id=getattr(sent, "id", None),
                    )
                except BaseException as exc:
                    # Telegram may already have accepted the chunk, but its
                    # stable key makes the durable retry safe.  Never advance
                    # to another chunk without first persisting this receipt.
                    send_error = exc
                    break
            if index == 0 and envelope.boundary and not is_dm and provocateur_id is not None:
                sent_id = getattr(sent, "id", None)
                if sent_id is not None:
                    _boundary_replies[chat_id].append(
                        (int(sent_id), int(provocateur_id), time.time()))
            if (not is_dm and getattr(sent, "id", None) is not None
                    and _group_archive_enabled()):
                try:
                    sent_date = getattr(sent, "date", None)
                    sent_ts = (sent_date.timestamp() if sent_date is not None else time.time())
                    await asyncio.to_thread(
                        group_context.observe_message,
                        peer_id=route.peer_id, topic_id=route.topic_id,
                        message_id=int(sent.id), sender_id=_self_id,
                        sender_name="Praxis", reply_to_message_id=chunk_reply_to,
                        timestamp=sent_ts, text=chunk,
                        topic_title=_topic_titles.get(
                            (route.peer_id, int(route.topic_id)), ""
                        ) if route.topic_id is not None else "",
                        outgoing=True,
                    )
                except Exception:
                    log.exception("group archive не записал исходящее [%s] #%s",
                                  chat_id, getattr(sent, "id", None))
            sent_chunks.append(chunk)
            if getattr(sent, "id", None) is not None:
                sent_ids.append(str(sent.id))
                delivery_message_ids.append(str(sent.id))

        sent_reply = "".join(sent_chunks)
        if sent_reply:
            # A visible prefix is already accepted.  Re-running the model would
            # duplicate it even when a later chunk fails.
            terminal = True
            sent_meta = ({"source_id": ",".join(sent_ids), "ts": time.time()}
                         if sent_ids else {})
            _buf_push(chat_id, f"Praxis: {sent_reply}", author="Praxis", is_dm=is_dm,
                      **sent_meta)
            log.info("ГОЛОС(%s) [%s]%s chunks=%d/%d -> %r",
                     "DM" if is_dm else "GRP", chat_id,
                     f" reply_to=#{reply_to}" if reply_to else "",
                     len(sent_chunks), len(chunks), sent_reply[:80])
            if is_dm:
                try:
                    await asyncio.to_thread(unanswered.resolve, chat_id)
                except Exception:
                    log.debug("unanswered.resolve упал [%s]", chat_id, exc_info=True)
            if delivery_run_id and send_error is None:
                try:
                    await asyncio.to_thread(
                        agent.run_delivery_text_accepted, delivery_run_id,
                        text=sent_reply, message_ids=sent_ids,
                    )
                except Exception:
                    # Telegram already accepted the visible prefix. Logging
                    # failure must not cause a second send.
                    log.exception("text acceptance receipt не записался [%s]",
                                  delivery_run_id)

        # If the first text chunk is transport-uncertain, do not add a second
        # kind of side effect. A partial accepted text prefix does keep its
        # already-authored media handoff, matching the previous exact behavior.
        may_handoff_media = send_error is None or bool(sent_reply)
        media_results: list[bool] = []
        if may_handoff_media:
            for item in envelope.outbound:
                media_results.append(
                    await _queue_and_send_media(
                        entity, item, ctx=ctx, reply_to=reply_to,
                        state_chat_id=chat_id,
                    )
                )

        if envelope.outbound and not terminal:
            # A failed upload is still terminal for the authored turn iff the
            # retry intent reached the durable media spool.  If enqueue itself
            # failed, keep the original wake so a legacy caller can try again.
            queued_ids = {pending.queue_id for pending in _media_spool().pending()}
            terminal = (
                len(media_results) == len(envelope.outbound)
                and all(
                    accepted or item.queue_id in queued_ids
                    for item, accepted in zip(envelope.outbound, media_results)
                )
            )
        elif send_error is None and not envelope.outbound:
            terminal = True

        if delivery_run_id and send_error is None:
            if len(media_results) == len(envelope.outbound) and all(media_results):
                await asyncio.to_thread(
                    agent.run_delivery_finalize_recovered, delivery_run_id,
                    media_count=len(media_results),
                )
            else:
                await asyncio.to_thread(
                    agent.run_delivery_blocked, delivery_run_id,
                    reason="one or more Telegram media uploads are queued for retry",
                )
        if send_error is not None:
            raise send_error
    except Exception as exc:
        if delivery_run_id:
            await asyncio.to_thread(
                agent.run_delivery_failed, delivery_run_id, exc,
                observed_message_ids=delivery_message_ids,
            )
        log.exception("ход упал [%s]", chat_id)
    finally:
        _ONE_MIND.release()
        if delivery_run_id:
            _TEXT_SENDING.discard(delivery_run_id)
        if terminal and not superseded_abandon:
            # Consume only media actually presented to this terminal turn; newer
            # concurrent arrivals remain queued. A newer wake generation survives.
            # PASS 29: a superseded/abandoned draft must NOT touch the successor's media,
            # wake or addressing — the fresh pass owns them; hence the extra guard.
            _consume_pending_media(chat_id, turn_media)
            if is_dm and chat_id in _meta:
                _meta[chat_id]["addressed"] = False
                _meta[chat_id]["addressed_mid"] = None
            if (not is_dm and wake is not None
                    and _group_wakes.get(chat_id) is wake):
                _group_wakes.pop(chat_id, None)
        _passing.discard(chat_id)
        # Новый настоящий address мог прийти, пока голос работал в thread; или тот же
        # wake должен повториться после retry_media/ошибки. Его debounce мог уже сгореть
        # об _passing, поэтом любой оставшийся wake generation-safe перевзводим.
        if not is_dm:
            current_wake = _group_wakes.get(chat_id)
            if current_wake is not None:
                _arm(chat_id)
        elif chat_id in _dm_rearm:
            # PASS 21: ЛС-триггер, сгоревший об _passing, больше не теряется
            _dm_rearm.discard(chat_id)
            _arm(chat_id)
        asyncio.create_task(_maybe_compact(chat_id))  # §6: сворачивание фоном, вне пути ответа


async def _maybe_compact(chat_id: str) -> None:
    """PASS 19: fold only an episode-aware, provenance-backed hot prefix.

    Сворачивается МЕСТО: замок берётся по нему же, иначе две ветки одной комнаты
    запускали бы свёртку одного и того же разговора одновременно — двойные вызовы
    модели на одну работу.
    """
    place = memory_life.place_key(chat_id)
    if place in _compacting:
        return
    _compacting.add(place)
    try:
        result = await asyncio.to_thread(memory_life.compact_if_due, place)
        fold = int(result.get("folded") or 0)
        if not fold:
            if (result.get("plan") or {}).get("reason") == "open_episode":
                log.info("compact [%s]: жду границу незаконченного эпизода (%d сообщений)",
                         place, len(_buf[chat_id]))
            return
        buf = _buf[chat_id]
        folded_lines = result.get("folded_lines")
        cut = fold
        if isinstance(folded_lines, list) and folded_lines:
            # Кольцо буфера и горячее кольцо места — ПАРАЛЛЕЛЬНЫЕ последовательности с
            # разной ёмкостью (BUF_MAXLEN=525 против HOT_HARD_HI=125), без курсора и без
            # id событий. Прежняя проверка закреплялась по НУЛЕВОМУ индексу и молча
            # предполагала, что свёрнутый блок лежит в начале буфера. Стоит буферу хоть
            # раз оказаться длиннее горячего кольца — совпадение головы ложно НАВСЕГДА:
            # состояние поглощающее, перезакрепиться нечем. Замер 31.07: свёрнутый блок
            # стоял на 449-й позиции, успешных срезов за 8 часов — ноль, а старое уходило
            # слепым вытеснением deque вместо осознанной свёртки — в трёх горячих местах,
            # включая личку Егора.
            # Ищем блок ЦЕЛИКОМ: совпадение всего свёрнутого куска подделать нечем, а
            # голову — можно. Не нашли — не режем, как и раньше.
            lines = list(buf)
            n = len(folded_lines)
            cut = next((i + n for i in range(len(lines) - n + 1)
                        if lines[i:i + n] == folded_lines), -1)
        if cut < 0:
            # Компакт цел в любом случае (сырой JSONL никуда не делся), а вот срезать
            # то, чего в буфере нет, нельзя — можно снести непредставленное сообщение.
            # Штатная причина расхождения одна: свёртка охватила несколько веток одной
            # комнаты, а этот буфер — только одна из них.
            level = log.info if place != str(chat_id) else log.error
            level("compact [%s]: свёртки места %s нет в локальном буфере — не режем",
                  chat_id, place)
            return
        for _ in range(min(cut, len(buf))):
            buf.popleft()
        _buf_dirty.add(chat_id)
        log.info("compact [%s]: %d событий → %s; hot=%s; причина=%s",
                 place, fold, result.get("compact_id"), result.get("hot"),
                 (result.get("plan") or {}).get("reason"))
    except Exception:
        log.exception("compact-триггер упал [%s]", chat_id)
    finally:
        _compacting.discard(place)


_PULSE_RETRY_AT = 0.0  # эпоха, когда отложенное пульсовое окно просится обратно; 0 — не просится


def _pulse_retry_sec() -> float:
    """Через сколько отложенное живым ходом пульсовое окно пробует снова.

    Тик часов — 2 секунды, а каждая попытка пересобирает контекст пульса, поэтому
    «на следующем тике» здесь было бы молотьбой."""
    try:
        return max(0.0, float(os.getenv("PRAXIS_PULSE_RETRY_SEC", "120")))
    except (TypeError, ValueError):
        return 120.0


def _request_pulse_retry(now: float | None = None) -> float:
    """Заявка на возврат отложенного окна. Ставит её ТОЛЬКО тот, кого реально отложили.

    Часы могли бы вместо этого сверяться с durable-сроком пульса — но «пульс due» верно
    и тогда, когда он сам решил не открываться: в окне сна `_social_pulse_once`
    возвращается ДО `begin()`, и срок остаётся due всю ночь. Часы бы будили её каждые
    две минуты до утра и засыпали её же perception-skips. Явная заявка отличает
    «меня отложили» от «я не пошла», а обратный скачок системных часов не превращает
    это в вечный холостой цикл."""
    global _PULSE_RETRY_AT
    _PULSE_RETRY_AT = float(now if now is not None else time.time()) + _pulse_retry_sec()
    return _PULSE_RETRY_AT


def _clear_pulse_retry() -> None:
    global _PULSE_RETRY_AT
    _PULSE_RETRY_AT = 0.0


async def _note_one_mind_defer(stage: str, goal: str) -> None:
    """Отложенное пробуждение обязано быть ВИДНЫМ ей, а не только в логе.

    Контракт R1 (CONTRACTS.md, закон 2): гейт может существовать — молча нет. `_ONE_MIND`
    откладывает её собственное окно/будильник, и до сих пор это жило одной строкой
    `log.info`, которую она не читает; `manage_perception("skips")` знал ровно один
    источник пропусков — окно сна (`_social_pulse_once`), а `rails.py:176-182` при этом
    утверждает, что иных гейтов нет. Пишем причину туда же, куда ложатся остальные
    пропуски восприятия: класс «отложила» — потому что это именно отсрочка, намерение
    остаётся due и вернётся следующим тиком, а не съедено.

    Повторы схлопывает сам perception (одинаковые stage+detail в окне 10 минут), так что
    занятый час не превращается в сотню строк.
    """
    try:
        await asyncio.to_thread(
            lambda: perception.note_skip(
                f"one_mind:{stage}", "отложила",
                detail=f"занята живым ходом, вернусь как освободится: {(goal or '')[:80]}"))
    except Exception:
        log.debug("skip отложенного пробуждения не записался", exc_info=True)


async def _task_window(goal: str, *, mailbox_index: str | None = None,
                       on_open=None, on_run=None,
                       keep_transport: bool = False) -> bool | None:
    """Её долгий run над собой под единым замком. Два режима связи, и они разные по смыслу.

    ПО УМОЛЧАНИЮ (её ретрит: focus, rest, кодинг, намеченное окно) — старый канон,
    восстановленный в PASS 13.1: Telethon ЗАКРЫТ на всё окно. Она не прерывается входящими
    и не двоится; backlog приходит одной ситуацией через буфер+дебаунс на reconnect;
    отправки durable и уходят заботами ``direct_outbox``/``text_outbox`` после
    переподключения. Это её недоступность по её выбору, а не побочность.

    keep_transport=True (часовой пульс) — БЕЗ разрыва. Пульс социальный насквозь: открытые
    нити, почта, фолоуапы, «хочу ли я кому-то написать», — и всё это он делал с закрытым
    Telegram, то есть работа, ради которой он существует, была в нём невозможна, а она
    недоступна час за часом.

    Почему это не возврат регрессии PASS 25 («Telethon continuously online» породил
    ПАРАЛЛЕЛЬНЫЕ пробуждения): от неё защищает не disconnect, а замок. ``_ONE_MIND``
    появился позже, и всякий входящий проход его ЖДЁТ (``_pass``:
    ``await _ONE_MIND.acquire()``), а не бежит рядом; тем же замком и на живой связи уже
    год работает forge-контур. Политика пульса «кому можно писать»
    (``social_pulse.allow_outbound``) стоит на шве тула, до постановки в outbox, поэтому
    живая связь её тоже не обходит.

    Что меняется для человека: раньше его сообщение в час пульса не приходило вовсе —
    теперь приходит сразу и отвечается следующим ходом.

    -> True/False по исходу, None если замок занят живым ходом (пульс тогда отложится)."""
    if _ONE_MIND.locked():
        # Она одна: идёт живой ход — окно не открываем поверх него. Часы не блокируем.
        # Формулировка нарочно без числа: у этой функции два вызывающих с разными
        # сроками возврата (пульс — по заявке `_request_pulse_retry`, разовые
        # focus/rest/coding — со своего тика планировщика), и обещать одному срок
        # другого значит соврать ей в её же журнале.
        log.info("ТАСК-ОКНО отложено: занята живым ходом, вернусь как освободится — %s",
                 (goal or "")[:80])
        await _note_one_mind_defer("task_window", goal)
        return None
    async with _ONE_MIND:
        log.info("ТАСК-ОКНО открываю: %s", (goal or "")[:80])
        # Окно РЕАЛЬНО открылось (замок взят) — только теперь помечаем намерение сработавшим,
        # атомарно и ДО disconnect (который останавливает loop). Отложенное окно (замок был
        # занят живым ходом выше) сюда не доходит и намерение НЕ съедает: одноразовый focus/rest
        # остаётся due и вернётся на следующем тике, а не теряется тихо.
        if on_open is not None:
            try:
                await on_open()
            except Exception:
                log.exception("mark-on-open таск-окна упал")
        ok = False
        if keep_transport:
            # Окно без разрыва связи. Замок _ONE_MIND по-прежнему держит «её одну», но
            # входящее ДОХОДИТ и ждёт своей очереди, а не проваливается в час немоты.
            #
            # Рамке отдаём НАБЛЮДЁННОЕ состояние, а не намерение. «Я не рвал связь» и
            # «связь есть» — разные утверждения: сокет мог отвалиться сам за секунду до
            # этого. Сказать ей «Telegram открыт», не посмотрев, значило бы повторить ту
            # самую ложь в рамке, ради которой всё это и переписывалось.
            try:
                live = "connected" if client.is_connected() else "disconnected"
            except Exception:
                # Не смогли посмотреть — так и скажем. Опрос состояния не имеет права
                # уронить её ход, а «unknown» в рамке честнее любого из двух ответов.
                log.debug("не смог прочитать состояние транспорта", exc_info=True)
                live = "unknown"
            try:
                await asyncio.to_thread(agent.task_window, goal,
                                        mailbox_index=mailbox_index,
                                        transport=live, on_run=on_run)
                ok = True
            except Exception:
                log.exception("таск-окно (без разрыва) упало")
            return ok
        _EXPECT_DISCONNECT.set()  # 13.1: намеренный disconnect; main() должен его пережить
        try:
            await client.disconnect()
        except Exception:
            log.exception("disconnect перед окном")
        try:
            await asyncio.to_thread(agent.task_window, goal, mailbox_index=mailbox_index,
                                    transport="closed_for_window", on_run=on_run)
            ok = True
        except Exception:
            log.exception("таск-окно упало")
        finally:
            # restart_self уже вышел бы из процесса (bootguard поднимет); иначе переподключаемся
            # сами, и накопленный backlog приезжает одной ситуацией. _EXPECT_DISCONNECT снимаем
            # ТОЛЬКО после успешного connect — иначе пусть main() ждёт/сдастся по таймауту, а не
            # примет сбой за работу.
            try:
                await client.connect()
                log.info("ТАСК-ОКНО закрыто, переподключилась; backlog придёт одной ситуацией")
                _EXPECT_DISCONNECT.clear()
            except Exception:
                log.exception("reconnect после окна")
    return ok


async def _wake_pass(goal: str, *, on_open=None, on_run=None) -> bool | None:
    """kind=wake: её будильник поднимает ОБЫЧНЫЙ ход — Telethon НЕ закрываем.

    Отличие от ``_task_window`` ровно одно и оно всё: здесь нет disconnect. Поэтому пока
    она разбужена, входящие ДОХОДЯТ (буфер живой), чтение соседних диалогов работает, а
    отправка уходит сразу, а не ждёт reconnect.

    Точная мера доступности: живой ход человека не пропускается, а ЖДЁТ замок
    (``_ONE_MIND.acquire()`` в ``_pass``), и на следующем тике пробуждение видит замок
    занятым и отходит. Значит очередь честная: написавший во время пробуждения получает
    ответ следующим же ходом, а не после часа тишины. Это ровно та цена, что у любого
    живого хода, и она несравнима с окном, где сообщение не приходит вовсе.

    Дисциплина «не более раза» — та же, что у окна: намерение метится сработавшим при
    РЕАЛЬНОМ подъёме (замок взят), а отложенное (замок занят живым ходом) остаётся due и
    вернётся на следующем тике, а не теряется тихо."""
    if _ONE_MIND.locked():
        log.info("ПРОБУЖДЕНИЕ отложено: занята живым ходом, вернусь на следующем тике — %s",
                 (goal or "")[:80])
        await _note_one_mind_defer("wake_pass", goal)
        return None
    async with _ONE_MIND:
        log.info("ПРОБУЖДЕНИЕ: %s", (goal or "")[:80])
        if on_open is not None:
            try:
                await on_open()
            except Exception:
                log.exception("mark-on-open пробуждения упал")
        try:
            await asyncio.to_thread(agent.wake_turn, goal, on_run=on_run)
            return True
        except Exception:
            log.exception("пробуждение упало")
            return False


async def _room_participants_search(q: str, limit: int = 5) -> list[str]:
    """Тёзки среди участников ЕЁ комнат (НЕ глобальный поиск Telegram) — информация для
    ответа владельцу, не адресация: отправка по-прежнему только диалоги/точный адрес."""
    out: list[str] = []
    for cid in list(rooms.allowed_chats())[:6]:
        try:
            parts = await client.get_participants(int(cid), search=q, limit=limit)
        except Exception:
            continue
        out.extend(_ent_label(p) for p in parts)
        if len(out) >= limit:
            break
    return out[:limit]


def _recent_senders_hits(q: str, limit: int = 5) -> list[str]:
    """PASS 16.2: поиск по кэшу недавних отправителей (имя/подстрока или точный id).
    Публично видимые отправители её же чатов — не глобальный поиск и не приватка."""
    ql = (q or "").strip().lstrip("@").lower()
    if not ql:
        return []
    out, now = [], time.time()
    for cid, ring in list(_recent_senders.items()):
        for ts, nm, sid in reversed(list(ring)):
            if ql in (nm or "").lower() or ql == str(sid):
                age = max(0, int((now - ts) / 60))
                out.append(f"{nm} (id={sid}) — писал(а) в {cid} ~{age}м назад")
                break
    return out[:limit]


def _sync_get_id(name_or_username: str):
    """Sync-обёртка для тула get_id. Имя без личного диалога — поиск по участникам ОБЩИХ
    комнат (кейс «id Анны для Хоуп»: человек писал в группу, но лички с ним нет).
    PASS 16.2: + кэш недавних отправителей — забаненный спамер в участниках уже не
    числится, но его id пришёл в апдейте и не должен «исчезать» для неё (инцидент 09.07:
    «видишь айди спамера? Я нет»). Глубже RAM-кэша — тул read_log."""
    async def _coro():
        try:
            ent = await _resolve_entity(name_or_username)
        except ResolveDenied as e:
            fresh = _recent_senders_hits(str(name_or_username))
            if fresh:
                return ("Диалога нет, но среди недавних отправителей моих чатов: "
                        + "; ".join(fresh) + ". (кэш с рестарта; глубже — read_log)")
            hits = await _room_participants_search(str(name_or_username))
            if hits:
                return ("Личного диалога нет, но среди участников общих комнат: "
                        + "; ".join(hits) + ". Для отправки нужен точный @username/id.")
            return str(e)
        if ent is None:
            fresh = _recent_senders_hits(str(name_or_username))
            if fresh:
                return ("В диалогах не нашла, но среди недавних отправителей: "
                        + "; ".join(fresh) + ". (кэш с рестарта; глубже — read_log)")
        return _ent_label(ent) if ent is not None else None
    return _threadsafe_result(_coro, 20)


def _sync_resolve_id(ref):
    """PASS 12.0.b: тот же богатый резолвер, что и на отправке (_resolve_entity), но отдаёт id.
    Раньше постановка задачи звала только get_entity (одна попытка) — и отбивалась там, где
    отправка бы дорезолвила через int()/перебор диалогов. Теперь оба конца резолвят одинаково."""
    async def _coro():
        ent = await _resolve_entity(ref)
        return getattr(ent, "id", None) if ent is not None else None
    return _threadsafe_result(_coro, 40)


def _sync_search_chats(query: str) -> str:
    async def _coro():
        q, out = query.lower(), []
        async for d in client.iter_dialogs():
            if q in (d.name or "").lower():
                out.append(f"{d.name}: {d.id}")
                if len(out) >= 10:
                    break
        return "\n".join(out) or "(ничего не нашла)"
    return _threadsafe_result(_coro, 30)


def _sync_search_private_messages(query: str, limit: int = 20) -> str:
    """Search message text across Praxis's Telegram DMs, never groups/channels."""
    async def _coro():
        from telethon.tl.types import PeerUser

        q = str(query or "").strip()
        if not q:
            return "Нужна непустая строка поиска."
        cap = max(1, min(40, int(limit or 20)))
        rows = []
        # Telegram global search may return groups as well; inspect a wider window and
        # keep only PeerUser messages.  This does not inject results into ordinary turns:
        # bytes reach the model only after an explicit tool call.
        async for msg in client.iter_messages(None, search=q, limit=cap * 8):
            if not isinstance(getattr(msg, "peer_id", None), PeerUser):
                continue
            try:
                chat = await msg.get_chat()
            except Exception:
                chat = None
            try:
                sender = await msg.get_sender()
            except Exception:
                sender = None
            chat_label = _ent_label(chat) if chat is not None else f"DM {getattr(msg, 'chat_id', '?')}"
            author = "Praxis" if getattr(msg, "sender_id", None) == _self_id else _sender_label(sender)
            body = re.sub(r"\s+", " ", (getattr(msg, "message", None) or _media_tag(msg) or "")).strip()
            if not body:
                continue
            when = getattr(msg, "date", None)
            stamp = when.strftime("%Y-%m-%d %H:%M") if when is not None else "?"
            rows.append(f"{stamp} · {chat_label} · {author}: {body[:500]}")
            if len(rows) >= cap:
                break
        if not rows:
            return "(в личках ничего не нашла)"
        return ("[PRIVATE CROSS-CHAT SEARCH — внутренний материал; не цитируй чувствительные "
                "личные сведения аудитории без права их получить]\n" + "\n".join(rows))
    return _threadsafe_result(_coro, 60)


_entity_cache: dict[str, object] = {}   # ref (lowercase str) → entity
_dialog_name_cache: list[tuple[str, object]] | None = None  # [(name.lower(), entity), ...]
_dialog_warmup_task: asyncio.Task | None = None  # HOTFIX 07.07: прогрев кэша (фон, с коннекта)


async def _build_dialog_cache() -> None:
    """Один проход iter_dialogs → кэш имён диалогов. Собирает в локальный список и публикует
    атомарно в конце: параллельный резолв не видит полусобранный кэш."""
    global _dialog_name_cache
    t0 = time.time()
    cache: list[tuple[str, object]] = []
    try:
        async for d in client.iter_dialogs():
            if d.name:
                cache.append((d.name.lower(), d.entity))
                # также закэшировать по id диалога
                _entity_cache[str(d.id)] = d.entity
                dialog_date = getattr(d, "date", None)
                dialog_seen = (float(dialog_date.timestamp()) if dialog_date is not None else 0.0)
                telegram_contacts.observe(d.entity, aliases=(d.name,), dialog=True,
                                           seen_at=dialog_seen, persist=False)
    except Exception:
        if not cache:
            log.exception("скан диалогов упал, не собрав ничего — следующий резолв попробует заново")
            _dialog_name_cache = None
            return
        log.exception("скан диалогов оборвался — кэш имён будет частичным (%d)", len(cache))
    # Полный список контактов именно аккаунта Praxis. Если API-слой/тестовый клиент
    # этого не умеет, диалоги и увиденные отправители всё равно дают рабочую книгу.
    imported = 0
    try:
        from telethon.tl.functions.contacts import GetContactsRequest
        result = await client(GetContactsRequest(hash=0))
        for ent in getattr(result, "users", ()) or ():
            # Being in the contact list is a strong address signal, but not evidence that the
            # conversation happened now. Preserve real message/dialog recency.
            if telegram_contacts.observe(ent, contact=True, seen_at=0.0, persist=False):
                imported += 1
    except Exception:
        log.debug("импорт Telegram contacts недоступен — остаются диалоги/отправители", exc_info=True)
    for ident, alias in social.known_ids().items():
        telegram_contacts.add_alias(ident, str(alias), persist=False)
    telegram_contacts.save()
    _dialog_name_cache = cache
    log.info("адресная книга прогрета: %d диалогов + %d контактов, всего %d за %.1fс",
             len(cache), imported, telegram_contacts.count(), time.time() - t0)


def _start_dialog_warmup() -> asyncio.Task:
    """HOTFIX 07.07: греть кэш диалогов фоном сразу после connect — не лениво на первом резолве
    по имени, где холодный iter_dialogs() конкурировал за таймаут-бюджет живого запроса owner'а."""
    global _dialog_warmup_task
    if _dialog_warmup_task is None or _dialog_warmup_task.done():
        _dialog_warmup_task = asyncio.create_task(_build_dialog_cache())
    return _dialog_warmup_task


async def _ensure_dialog_cache() -> None:
    """Тёплый кэш → мгновенно. Холодный → ждать прогрев СВОИМ бюджетом (DIALOG_WARMUP_WAIT_SEC);
    прогрев ещё не запускался (ранний вызов) — запустить здесь же. Не успел — честный TimeoutError
    с внятным словом, не молчаливое «не нашла»."""
    if _dialog_name_cache is not None:
        return
    task = _start_dialog_warmup()
    t0 = time.time()
    try:
        # shield: таймаут ожидания не убивает сам прогрев — он дозреет в фоне для следующих
        await asyncio.wait_for(asyncio.shield(task), timeout=DIALOG_WARMUP_WAIT_SEC)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"кэш диалогов ещё греется (ждала {DIALOG_WARMUP_WAIT_SEC:.0f}с) — "
            "резолв по имени пока недоступен, по id/@username работает") from None
    waited = time.time() - t0
    if waited > 1:
        log.info("холодный резолв по имени ждал прогрева кэша %.1fс", waited)
    if _dialog_name_cache is None:  # прогрев завершился, но упал, ничего не собрав
        raise TimeoutError("скан диалогов не удался — резолв по имени пока недоступен")


class ResolveDenied(Exception):
    """Осознанный отказ резолва — НЕ сбой канала: неоднозначное имя или адресат вне её
    видимости. str(e) — готовый честный ответ тула (с кандидатами и что уточнить)."""


def _ent_label(ent) -> str:
    """Человекочитаемая метка entity: «Получатель (@example, id 234567890)» — для квитанций
    и списков кандидатов, чтобы было видно, КТО именно зарезолвился."""
    name = (" ".join(filter(None, (getattr(ent, "first_name", None), getattr(ent, "last_name", None))))
            or getattr(ent, "title", None) or getattr(ent, "name", None) or "?")
    bits = []
    handle = _telegram_handle(ent)
    if handle:
        bits.append(f"@{handle}")
    bits.append(f"id {getattr(ent, 'id', '?')}")
    return f"{name} ({', '.join(bits)})"


async def _resolve_entity(ref):
    """Резолв entity. Видимость (07.07, вечер): ТОЧНЫЙ адрес (id/@username) — Telegram-резолв,
    как раньше; ИМЯ — только среди СВОИХ диалогов. Раньше имя проваливалось в get_entity →
    session-БД, где лежат ВСЕ, кого она когда-либо видела в группах (одних точных «Евгениев»
    там несколько) — фактически глобальный поиск, и письмо могло уйти постороннему тёзке.
    Неоднозначность — не молчаливое первое совпадение, а ResolveDenied со списком кандидатов."""
    ref_s = str(ref).strip()
    cache_key = ref_s.lower().lstrip("@")

    # 1. Быстрый кэш
    if cache_key in _entity_cache:
        return _entity_cache[cache_key]

    # 2. Точный адрес: id / @username — get_entity (быстро, адресат назван явно)
    if isinstance(ref, int) or ref_s.startswith("@") or ref_s.lstrip("-").isdigit():
        # Кандидаты строятся ОДИН раз, числом вперёд и без дублей.
        # Числовая СТРОКА, отданная Telethon как есть, до всякого поиска чата уходит в
        # `utils.parse_phone` — и там она НОМЕР ТЕЛЕФОНА. Проверено на прод-интерпретаторе
        # 26.07: parse_phone('-100500') -> '100500', parse_phone('-1001240718803') ->
        # '1001240718803'. Дальше `_get_entity_from_string` делает
        # `contacts.GetContactsRequest(0)` — ПОЛНУЮ выгрузку адресной книги, и так на
        # каждый промах. Хуже: `for cand in (ref, ref_s)` при уже-строковом `ref` давал
        # два одинаковых кандидата, то есть две выгрузки за вызов; третий заход
        # `get_entity(int(ref_s))` дублировал их снова. С 20.07 по мёртвой -100500 это
        # 4865 холостых тиков backfill — заявка на FloodWait по contacts.GetContacts, а
        # такт часов последовательный и с `await`: flood-wait подвесил бы вместе с
        # резолвом буферы, schedule, outbox и forge_events.
        # Строковый кандидат сохранён — по нему Telethon умеет находить человека из
        # адресной книги по номеру телефона, и эту руку я не отнимаю. Но ОТРИЦАТЕЛЬНАЯ
        # числовая строка телефоном не бывает никогда: '-' там означает «это чат», и
        # parse_phone его просто съедает. Такой кандидат выбрасывается — это снятие
        # заведомо ложного запроса, а не сужение адресации.
        cands: list = []
        for cand in (ref, ref_s):
            if isinstance(cand, str) and cand.lstrip("-").isdigit():
                try:
                    numeric = int(cand)
                except ValueError:
                    numeric = None
                if numeric is not None:
                    if numeric not in cands:
                        cands.append(numeric)
                    if cand.startswith("-"):
                        continue
            if cand not in cands:
                cands.append(cand)
        for cand in cands:
            try:
                ent = await client.get_entity(cand)
                if ent is not None:
                    _entity_cache[cache_key] = ent
                    _entity_cache[str(getattr(ent, "id", "")).lower()] = ent
                    return ent
            except Exception:
                pass
        return None

    # 3. Имя — постоянная адресная книга (contacts + dialogs + seen senders + known aliases).
    await _ensure_dialog_cache()
    q = ref_s.lower()
    book = telegram_contacts.candidates(ref_s)
    for row in book:
        ident = str(row.get("id") or "")
        ent = _entity_cache.get(ident)
        if ent is None:
            try:
                ent = await client.get_entity(int(ident))
            except Exception:
                ent = None
        if ent is not None:
            _entity_cache[cache_key] = ent
            _entity_cache[ident] = ent
            if len(book) > 1:
                log.info("резолв %r: выбрала %s по адресу/свежести среди %d кандидатов",
                         ref, _ent_label(ent), len(book))
            return ent

    # Legacy in-memory fallback for chats/groups and for a warmup that could not persist.
    matches = [(name_lower, ent) for name_lower, ent in (_dialog_name_cache or []) if q in name_lower]
    if not matches:
        # 01.08.2026: тот же ключ имени, что в адресной книге и в досье людей —
        # иначе «Егор» не находит диалог с «Yegor Kosyrev», хотя это один человек и
        # один открытый диалог. Складываются только написания одного имени; поиск
        # по подстроке сохранён, чтобы группы резолвились как раньше.
        q_key = telegram_contacts.identity_key(ref_s)
        if q_key:
            matches = [(name_lower, ent) for name_lower, ent in (_dialog_name_cache or [])
                       if q_key in telegram_contacts.identity_key(name_lower)]
    if matches:
        ent = matches[0][1]
        _entity_cache[cache_key] = ent
        if len(matches) > 1:
            log.info("резолв %r: выбрала самый свежий диалог %s среди %d",
                     ref, _ent_label(ent), len(matches))
        return ent
    raise ResolveDenied(
        f"«{ref_s}» пока нет в моей адресной книге, контактах или диалогах. Нужен один "
        "первичный @username/id; после первого контакта имя запомню сама.")


_TG_INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def _membership_target(raw: str) -> tuple[str, str]:
    """Normalize Telegram membership refs to (invite|public|entity, value)."""
    from urllib.parse import parse_qs, unquote, urlparse

    value = str(raw or "").strip()
    if not value:
        raise ValueError("нужна invite/public ссылка, @username или chat_id")
    parsed = urlparse(value)
    if parsed.scheme.casefold() == "tg" and parsed.netloc.casefold() == "join":
        invite = (parse_qs(parsed.query).get("invite") or [""])[0].strip()
        if not _TG_INVITE_RE.fullmatch(invite):
            raise ValueError("не распознала invite hash в tg://join")
        return "invite", invite
    if parsed.scheme.casefold() in ("http", "https"):
        host = (parsed.netloc or "").casefold().split(":", 1)[0]
        if host not in ("t.me", "telegram.me", "www.t.me", "www.telegram.me"):
            raise ValueError("это не ссылка t.me/telegram.me")
        parts = [unquote(x).strip() for x in parsed.path.split("/") if x.strip()]
        if not parts:
            raise ValueError("в Telegram-ссылке нет группы или invite hash")
        if parts[0] == "joinchat" and len(parts) >= 2:
            invite = parts[1]
            if not _TG_INVITE_RE.fullmatch(invite):
                raise ValueError("не распознала invite hash")
            return "invite", invite
        if parts[0].startswith("+"):
            invite = parts[0][1:]
            if not _TG_INVITE_RE.fullmatch(invite):
                raise ValueError("не распознала invite hash")
            return "invite", invite
        if parts[0] in ("c", "s") or len(parts) > 1:
            raise ValueError("нужна ссылка на саму группу/канал, не на сообщение")
        return "public", "@" + parts[0].lstrip("@")
    if value.startswith("+") and _TG_INVITE_RE.fullmatch(value[1:]):
        return "invite", value[1:]
    if value.startswith("@"):
        return "public", value
    return "entity", value


async def _note_linked_discussion(chat_obj, peer_id) -> None:
    """Узнать, какое обсуждение связано с каналом. Один RPC и только когда её ещё нет.

    `broadcast` приходит бесплатно в объекте апдейта, а `linked_chat_id` — только в
    полном описании канала, то есть за отдельный запрос. Поэтому: спрашиваем один раз на
    канал и записываем durable; дальше берём из записи. Ошибка тут ничего не стоит — без
    адреса фраза честно скажет, что связанного обсуждения мы не знаем, вместо того чтобы
    отправить её наугад.
    """
    if not getattr(chat_obj, "broadcast", False):
        return
    try:
        if telegram_routes.writing_of(peer_id).get("linked_chat_id"):
            return
        from telethon.tl.functions.channels import GetFullChannelRequest

        full = await client(GetFullChannelRequest(channel=chat_obj))
        linked = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
        if not linked:
            return
        title = ""
        for candidate in (getattr(full, "chats", None) or ()):
            try:
                if int(getattr(candidate, "id", 0)) == int(linked):
                    title = str(getattr(candidate, "title", "") or "")
                    break
            except (TypeError, ValueError):
                continue
        await asyncio.to_thread(
            lambda: telegram_routes.note_writing(
                peer_id, linked_chat_id=_marked_peer_id_from_id(int(linked)),
                linked_title=title))
        log.info("канал [%s]: связанное обсуждение %s (%s)", peer_id, linked, title or "?")
    except Exception:
        log.debug("связанное обсуждение канала не узналось [%s]", peer_id, exc_info=True)


def _marked_peer_id_from_id(ident: int) -> str:
    """Голый channel id -> тот вид адреса, которым она пользуется (-100…)."""
    return str(-(1_000_000_000_000 + int(ident)))


def _entity_kind(ent) -> str:
    from telethon.tl.types import Channel, Chat, User

    if isinstance(ent, Channel):
        return "channel"
    if isinstance(ent, Chat):
        return "chat"
    if isinstance(ent, User):
        return "user"
    name = type(ent).__name__.casefold()
    if "channel" in name or hasattr(ent, "megagroup") or hasattr(ent, "broadcast"):
        return "channel"
    if name.endswith("chat") or getattr(ent, "is_group", False):
        return "chat"
    return "user"


def _marked_peer_id(ent) -> int:
    """Telethon-compatible dialog id (-100… for channel/supergroup)."""
    from telethon import utils

    try:
        return int(utils.get_peer_id(ent))
    except Exception:
        ident = int(getattr(ent, "id"))
        kind = _entity_kind(ent)
        if kind == "channel":
            return -(1_000_000_000_000 + ident)
        if kind == "chat":
            return -ident
        return ident


def _telegram_account_gate() -> str | None:
    """The owner and Praxis herself may act; trusted humans cannot delegate account power."""
    if not agent._is_sovereign_actor():
        return "Отказ: Telegram-аккаунтом управляет только владелец или сама Praxis."
    return None


def _telegram_account_principal(value: object = None) -> str | None:
    """Чья расписка ляжет под операцию на аккаунте.

    ⚠ Решение Егора 26.07 (вариант 1): в её ходе действует ОНА, кто бы ни заговорил.
    Прежде здесь проходили только Егор и её фоновый ход, поэтому стоило человеку
    заговорить — и её собственный аккаунт становился для неё закрыт. Пропуск даёт
    `_telegram_account_gate` (он и есть решение о праве); здесь остаётся честная
    подпись: действие принадлежит ей, а не собеседнику.
    """
    raw = (agent._active_principal() if value is None else str(value or "").strip())
    if raw == agent.PRAXIS_SELF_PRINCIPAL:
        return raw
    principal = agent._stable_numeric_principal(raw)
    if OWNER_ID and principal == str(OWNER_ID):
        return principal
    return agent.PRAXIS_SELF_PRINCIPAL if agent._is_praxis_self() else None


def _begin_membership_transaction(action: str, target: str) -> dict:
    """Capture live human authority and fsync intent before scheduling MTProto."""
    denied = _telegram_account_gate()
    if denied:
        raise PermissionError(denied)
    target = str(target or "").strip()
    _membership_target(target)  # reject malformed/message links without creating intent noise
    principal = _telegram_account_principal()
    if principal is None:
        raise PermissionError("Telegram membership доступен только владельцу или самой Praxis")
    return _membership_ledger().begin(action, target, principal)


async def _telegram_registry_entity_resolver(value, expected_type: str,
                                             field: str, request_name: str):
    """Resolve registry scalar references through this process' live Telethon client.

    Telethon's ``get_input_entity`` gives an ``InputPeer*``.  Some TL constructors
    require the narrower ``InputChannel``/``InputUser`` wrapper, while dialog and
    notification constructors require one additional envelope.  Keeping this here
    (rather than in :mod:`telegram_registry`) makes the registry session-agnostic and
    guarantees that raw calls use the same connected client and entity cache as the
    ordinary Praxis Telegram hands.
    """
    from telethon import utils
    from telethon.tl import types

    peer = await client.get_input_entity(value)
    expected = str(expected_type or "")
    if expected in {"InputChannel", "TypeInputChannel"}:
        return utils.get_input_channel(peer)
    if expected in {"InputUser", "TypeInputUser"}:
        return utils.get_input_user(peer)
    if expected in {"InputDialogPeer", "TypeInputDialogPeer"}:
        return types.InputDialogPeer(peer=utils.get_input_peer(peer))
    if expected in {"InputNotifyPeer", "TypeInputNotifyPeer"}:
        return types.InputNotifyPeer(peer=utils.get_input_peer(peer))
    return utils.get_input_peer(peer)


def _install_telegram_dispatcher() -> telegram_registry.TelegramAccountDispatcher:
    """Bind installed TL schema dispatch to the already-connected account client."""
    global _TELEGRAM_DISPATCHER, _TELEGRAM_CONFIRMATIONS, _TELEGRAM_CRITICAL_CHALLENGES
    try:
        confirmation_ttl = max(
            30, min(900, int(os.getenv("PRAXIS_TELEGRAM_CONFIRM_TTL_SEC", "300"))),
        )
    except ValueError:
        confirmation_ttl = 300
    _TELEGRAM_CONFIRMATIONS = telegram_confirmation.ConfirmationStore(
        owner_id=OWNER_ID, ttl_seconds=confirmation_ttl,
        confirmable_principals=(agent.PRAXIS_SELF_PRINCIPAL,),
    )
    _TELEGRAM_CRITICAL_CHALLENGES = telegram_confirmation.CriticalChallengeStore(
        owner_id=OWNER_ID, ttl_seconds=confirmation_ttl,
        initiator_principals=(agent.PRAXIS_SELF_PRINCIPAL,),
    )
    _TELEGRAM_DISPATCHER = telegram_registry.TelegramAccountDispatcher(
        caller=client,
        entity_resolver=_telegram_registry_entity_resolver,
        owner_id=OWNER_ID,
        confirmation_verifier=_TELEGRAM_CONFIRMATIONS.verify_and_consume,
        sovereign_principals=(agent.PRAXIS_SELF_PRINCIPAL,),
    )
    meta = _TELEGRAM_DISPATCHER.registry.metadata
    log.info(
        "Telethon TL registry: %d requests, layer=%s, version=%s, fingerprint=%s",
        meta["request_count"], meta["tl_layer"], meta["telethon_version"],
        str(meta["fingerprint"])[:12],
    )
    return _TELEGRAM_DISPATCHER


async def _telegram_account_async(*, action: str, target: str = "", query: str = "",
                                  request: str = "", params: dict | None = None,
                                  confirm: str = "", challenge_id: str = "",
                                  scope: str = "",
                                  namespace: str = "", risk: str = "",
                                  offset: int = 0, limit: int = 25,
                                  _principal: str | int = "unknown",
                                  _origin: dict | None = None,
                                  _execution: dict | None = None) -> dict:
    """Compact list/search/describe/call adapter over the live dispatcher."""
    dispatcher = _TELEGRAM_DISPATCHER
    if dispatcher is None:
        raise RuntimeError("Telethon registry dispatcher ещё не инициализирован")
    if not OWNER_ID or str(_principal) not in {str(OWNER_ID), agent.PRAXIS_SELF_PRINCIPAL}:
        raise PermissionError("raw MTProto dispatcher доступен только владельцу или самой Praxis")
    action = str(action or "").strip().lower()

    def owner_origin() -> telegram_confirmation.OwnerOrigin:
        if str(_principal) != str(OWNER_ID):
            raise PermissionError(
                "account-critical подтверждение даёт только владелец из личного Telegram-чата"
            )
        try:
            origin = telegram_confirmation.OwnerOrigin.from_mapping(_origin or {})
        except (TypeError, ValueError, OverflowError) as exc:
            raise PermissionError(
                "для account-critical действия нет точного immutable origin owner-сообщения"
            ) from exc
        if (origin.principal_id != str(OWNER_ID)
                or origin.chat_id != str(OWNER_ID) or not origin.is_dm):
            raise PermissionError(
                "account-critical действие разрешено только владельцу из личного Telegram-чата"
            )
        return origin

    critical_store = _TELEGRAM_CRITICAL_CHALLENGES
    proof_store = _TELEGRAM_CONFIRMATIONS
    if action in {"confirm", "pending_confirmations", "cancel_confirmation"}:
        if critical_store is None or proof_store is None:
            raise RuntimeError("Telegram critical confirmation stores не инициализированы")
        selector = str(challenge_id or target or request or "").strip()
        if action == "pending_confirmations":
            active = [
                item for item in critical_store.list()
                if item.get("status") in {"pending", "in_doubt"}
                and (
                    str(_principal) == str(OWNER_ID)
                    or item.get("requested_by") == agent.PRAXIS_SELF_PRINCIPAL
                )
            ]
            return {"action": action, "items": active, "count": len(active)}
        origin = owner_origin()
        if not selector:
            raise ValueError(f"telegram_account {action}: challenge_id обязателен")
        if action == "cancel_confirmation":
            cancelled = critical_store.cancel(selector, origin=origin)
            return {
                "action": action, "challenge_id": selector,
                "cancelled": bool(cancelled),
            }

        # The model sees only the challenge id.  The exact phrase is compared with
        # immutable raw Telegram text captured by the runner, never with a tool arg.
        claimed = critical_store.claim(selector, origin=origin)
        # Recompute the keyed binding from the authenticated envelope in this
        # process.  The commitment key is deliberately not durable, so a plain
        # verifier for a password/code can never be recovered from the ledger.
        binding = dispatcher.confirmation_binding(
            str(claimed["request_name"]), dict(claimed["parameters"]),
            principal=str(claimed["principal"]),
        )
        try:
            proof = proof_store.issue(binding, principal=_principal)
        except Exception as exc:
            critical_store.finish(
                selector, error=f"proof issue failed: {type(exc).__name__}: {exc}",
            )
            raise
        try:
            receipt = await dispatcher.handle(
                "call",
                {
                    "name": binding.request_name,
                    "parameters": dict(claimed["parameters"]),
                    "mode": "raw",
                },
                principal=claimed["principal"],
                confirmation=proof,
                delivery_context={
                    "chat_id": origin.chat_id,
                    "run_id": origin.run_id,
                    "origin_message_id": origin.message_id,
                    "challenge_id": selector,
                    "requested_by": claimed["principal"],
                    "confirmed_by": str(_principal),
                    "request_origin": claimed.get("origin"),
                },
            )
        except Exception:
            # Claim is already fsynced.  We cannot know whether an unexpected crash
            # happened before or after Telethon accepted the mutation, so it remains
            # in_doubt and can never be replayed automatically.
            raise
        challenge = critical_store.finish(selector, receipt=receipt)
        safe_receipt = dict((challenge.get("terminal") or {}).get("receipt") or {})
        return {
            "action": action,
            "requested_by": claimed["principal"],
            "confirmed_by": str(_principal),
            "challenge": challenge,
            # Never feed submitted/serialized auth parameters back into the durable
            # model/run log.  The challenge ledger exposes only opaque receipt identity.
            "receipt": {"action": "call", "receipt": safe_receipt},
        }

    mapped = {
        "registry_list": "list",
        "registry_search": "search",
        "list": "list",
        "search": "search",
        "describe": "describe",
        "call": "call",
    }.get(action)
    if mapped is None:
        raise ValueError(
            "registry action должен быть list | search | describe | call | confirm | "
            "pending_confirmations | cancel_confirmation"
        )

    arguments: dict = {}
    if mapped == "list":
        arguments = {"offset": max(0, int(offset)), "limit": int(limit)}
        if scope:
            arguments["scope"] = str(scope)
        if namespace:
            arguments["namespace"] = str(namespace)
        if risk:
            arguments["risk"] = str(risk)
    elif mapped == "search":
        arguments = {"query": str(query or target), "limit": int(limit)}
        if scope:
            arguments["scope"] = str(scope)
    elif mapped == "describe":
        arguments = {"name": str(request or target or query)}
    else:
        arguments = {
            "name": str(request or target),
            "parameters": dict(params or {}),
            "mode": "raw",
        }

        # `confirm` is a legacy model-controlled field.  It is intentionally ignored.
        # Critical dispatch is split into two durable runs and proof never crosses the
        # model boundary.
        try:
            descriptor = dispatcher.registry.get(arguments["name"])
        except Exception:
            descriptor = None
        if descriptor is not None and descriptor.requires_confirmation:
            if critical_store is None or proof_store is None:
                raise RuntimeError("Telegram critical confirmation stores не инициализированы")
            execution = dict(_execution or {})
            execution_run = str(execution.get("run_id") or "").strip()
            call_id = str(execution.get("call_id") or "").strip()
            if (not execution_run or not call_id
                    or execution.get("tool") != "telegram_account"):
                raise PermissionError(
                    "account-critical вызов не связан с точным durable tool intent"
                )
            if str(_principal) == agent.PRAXIS_SELF_PRINCIPAL:
                intent_origin = telegram_confirmation.CriticalIntentOrigin.background(
                    run_id=execution_run,
                    call_id=call_id,
                    principal_id=agent.PRAXIS_SELF_PRINCIPAL,
                    confirmation_owner_id=str(OWNER_ID),
                )
            else:
                origin = owner_origin()
                if execution_run != origin.run_id:
                    raise PermissionError(
                        "account-critical owner-вызов не связан с точным durable run"
                    )
                intent_origin = telegram_confirmation.CriticalIntentOrigin.from_owner(
                    origin, call_id=call_id,
                )
            stable_key = str(execution.get("idempotency_key") or "").strip()
            if not stable_key:
                stable_key = f"telegram-critical:{execution_run}:tool:{call_id}"
            binding = dispatcher.confirmation_binding(
                descriptor.name, arguments["parameters"], principal=_principal,
            )
            challenge = critical_store.prepare(
                binding, arguments["parameters"], origin=intent_origin,
                idempotency_key=stable_key,
            )
            return {
                "action": "challenge",
                "challenge": challenge,
                "instruction": (
                    "Владелец должен прислать exact_phrase отдельным новым сообщением "
                    "в личный чат; затем можно вызвать action=confirm с challenge_id."
                ),
            }

    current = agent.run_context.current_run()
    origin_chat_id = (_origin or {}).get("chat_id") if isinstance(_origin, dict) else None
    execution_run_id = ((_execution or {}).get("run_id")
                        if isinstance(_execution, dict) else None)
    delivery_context = {
        "chat_id": origin_chat_id or agent._active_chat(),
        "run_id": execution_run_id or (current.run_id if current is not None else None),
    }
    return await dispatcher.handle(
        mapped,
        arguments,
        principal=_principal,
        delivery_context=delivery_context,
    )


async def _resolve_membership_entity(target: str):
    kind, value = _membership_target(target)
    if kind == "invite":
        from telethon.tl.functions.messages import CheckChatInviteRequest

        checked = await client(CheckChatInviteRequest(value))
        ent = getattr(checked, "chat", None)
        if ent is None:
            raise ValueError("по invite-ссылке я ещё не состою; сначала нужен join")
        return ent
    ent = await _resolve_entity(value)
    if ent is None:
        raise ValueError(f"не нашла Telegram-группу: {target}")
    return ent


def _membership_entity_facts(ent) -> dict:
    chat_id = _marked_peer_id(ent)
    title = getattr(ent, "title", None) or getattr(ent, "name", None) or str(chat_id)
    return {
        "chat_id": chat_id,
        "entity_id": int(getattr(ent, "id")),
        "entity_kind": _entity_kind(ent),
        "title": str(title),
    }


async def _apply_membership_acceptance(
    action: str, result: dict, ent=None, *, principal_id: object
) -> bool:
    """Project a recorded Telegram acceptance into root-room state idempotently."""
    set_by = (
        "praxis" if str(principal_id or "") == agent.PRAXIS_SELF_PRINCIPAL else "owner"
    )
    if action == "join":
        if result.get("status") == "request_sent":
            return False  # keep polling: approval has not established membership yet
        chat_id = str(result.get("chat_id") or "")
        if not chat_id:
            raise ValueError("accepted join has no root chat_id")
        if ent is not None:
            await _initialize_joined_room(
                chat_id, ent, title=str(result.get("title") or chat_id), allow=True,
                set_by=set_by,
            )
            _entity_cache[chat_id] = ent
        else:
            # Recovery must not collapse a forum topic into a conversation id: membership
            # and room admission always use the marked root peer stored in the receipt.
            rooms.add_room(chat_id)
            profile = rooms.profile_read(chat_id)
            if not profile.get("exists") or not profile.get("structured"):
                rooms.set_mode(chat_id, "observer", reason="восстановила подтверждённый вход",
                               set_by=set_by)
            elif profile.get("mode") == "dead":
                rooms.set_mode(chat_id, "observer", reason="восстановила подтверждённый вход",
                               set_by=set_by)
            rooms.owner_card(chat_id, "join", "восстановила подтверждённый Telegram-вход")
        return True

    chat_id = str(result.get("chat_id") or "")
    if not chat_id:
        raise ValueError("accepted leave has no root chat_id")
    rooms.remove_room(chat_id)
    rooms.set_mode(chat_id, "dead", reason="вышла из Telegram", set_by=set_by)
    _entity_cache.pop(chat_id, None)
    return True


def _membership_transaction(action: str, target: str, principal_id: object,
                            transaction_id: str | None) -> dict:
    if transaction_id is None:
        raise PermissionError("membership requires a durable sovereign intent")
    ledger = _membership_ledger()
    state = ledger.get(transaction_id)
    if state is None:
        raise KeyError(f"unknown membership transaction {transaction_id}")
    principal = _telegram_account_principal(state.get("principal_id"))
    supplied = _telegram_account_principal(principal_id)
    if principal is None or supplied != principal:
        raise PermissionError("membership transaction actor is no longer valid")
    if (state.get("action"), state.get("target"), state.get("principal_id")) != (
            action, target, principal):
        raise PermissionError("membership transaction provenance mismatch")
    return state


async def _join_chat_async(target: str, *, principal_id: object = None,
                           transaction_id: str | None = None, recovery: bool = False) -> dict:
    from telethon.errors import InviteRequestSentError, UserAlreadyParticipantError
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

    target = str(target or "").strip()
    kind, value = _membership_target(target)  # local validation before creating an intent
    state = _membership_transaction("join", target, principal_id, transaction_id)
    tx_id = state["id"]
    ledger = _membership_ledger()
    if state["status"] == "applied":
        return dict(state.get("result") or {})

    ent = None
    mutation_started = False
    try:
        # Telegram accepted but the process died before room projection.  Apply without
        # repeating the network mutation; a pending invite is polled read-only below.
        if state["status"] == "accepted" and state.get("result", {}).get("status") != "request_sent":
            result = dict(state["result"])
            try:
                ent = await _resolve_entity(result.get("chat_id"))
            except Exception:
                ent = None
            if await _apply_membership_acceptance(
                "join", result, ent, principal_id=state["principal_id"],
            ):
                ledger.applied(tx_id)
            return result

        already = False
        if kind == "invite":
            checked = await client(CheckChatInviteRequest(value))
            ent = getattr(checked, "chat", None)
            if ent is not None:
                already = True
            elif state["status"] == "accepted":
                # A join-request acceptance is not yet membership.  Do not send a second
                # request; leave it pending for the next clock tick.
                return dict(state["result"])
            else:
                mutation_started = True
                try:
                    updates = await client(ImportChatInviteRequest(value))
                except UserAlreadyParticipantError:
                    checked = await client(CheckChatInviteRequest(value))
                    ent = getattr(checked, "chat", None)
                    already = True
                except InviteRequestSentError:
                    result = {
                        "status": "request_sent", "chat_id": None,
                        "title": getattr(checked, "title", None) or "?",
                        "detail": "заявка на вступление отправлена администраторам",
                    }
                    ledger.accepted(tx_id, result)
                    return result
                else:
                    chats = list(getattr(updates, "chats", None) or ())
                    ent = chats[0] if chats else None
            if ent is None:
                raise RuntimeError("Telegram подтвердил invite, но не вернул chat entity")
        else:
            ent = await _resolve_entity(value)
            if ent is None:
                raise ValueError(f"не нашла Telegram-группу: {target}")
            if _entity_kind(ent) not in ("channel", "chat"):
                raise ValueError("это Telegram-пользователь, а не группа/канал")
            ledger.prepared(tx_id, _membership_entity_facts(ent))
            mutation_started = True
            try:
                updates = await client(JoinChannelRequest(ent))
                chats = list(getattr(updates, "chats", None) or ())
                if chats:
                    ent = chats[0]
            except UserAlreadyParticipantError:
                already = True
        if _entity_kind(ent) not in ("channel", "chat"):
            raise ValueError("invite ведёт не в группу/канал")
        facts = _membership_entity_facts(ent)
        ledger.prepared(tx_id, facts)
        result = dict(facts)
        result.pop("entity_kind", None)
        result["status"] = "already_joined" if already else "joined"
        ledger.accepted(tx_id, result)  # acceptance is durable before local room writes
    except Exception as exc:
        current = ledger.get(tx_id) or {}
        if current.get("status") != "accepted":
            if isinstance(exc, (ValueError, ResolveDenied)) and not mutation_started:
                ledger.failed(tx_id, exc)
            else:
                ledger.in_doubt(tx_id, exc)
        raise

    try:
        if await _apply_membership_acceptance(
            "join", result, ent, principal_id=state["principal_id"],
        ):
            ledger.applied(tx_id)
    except Exception as exc:
        result = dict(result)
        result["projection"] = "pending"
        result["projection_error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("membership join принят Telegram, room projection ждёт retry [%s]", tx_id)
    return result


async def _leave_chat_async(target: str, *, principal_id: object = None,
                            transaction_id: str | None = None, recovery: bool = False) -> dict:
    from telethon.errors import UserNotParticipantError
    from telethon.tl.functions.channels import LeaveChannelRequest
    from telethon.tl.functions.messages import DeleteChatUserRequest
    from telethon.tl.types import InputUserSelf

    target = str(target or "").strip()
    target_kind, _ = _membership_target(target)
    state = _membership_transaction("leave", target, principal_id, transaction_id)
    tx_id = state["id"]
    ledger = _membership_ledger()
    if state["status"] == "applied":
        return dict(state.get("result") or {})
    if state["status"] == "accepted":
        result = dict(state["result"])
        if await _apply_membership_acceptance(
            "leave", result, principal_id=state["principal_id"],
        ):
            ledger.applied(tx_id)
        return result

    ent = None
    mutation_started = False
    try:
        try:
            ent = await _resolve_membership_entity(target)
        except ValueError:
            prepared = dict(state.get("prepared") or {})
            if not (recovery and target_kind == "invite" and prepared.get("chat_id")):
                raise
            # The initial run proved membership and durably prepared the root entity.
            # If the same invite now says no membership, the desired leave is already true.
            result = {
                "status": "already_left",
                "chat_id": prepared["chat_id"],
                "entity_id": prepared.get("entity_id"),
                "title": prepared.get("title") or str(prepared["chat_id"]),
            }
            ledger.accepted(tx_id, result)
        else:
            facts = _membership_entity_facts(ent)
            kind = facts["entity_kind"]
            if kind not in {"channel", "chat"}:
                raise ValueError("это Telegram-пользователь, а не группа/канал")
            ledger.prepared(tx_id, facts)
            mutation_started = True
            try:
                if kind == "channel":
                    await client(LeaveChannelRequest(ent))
                else:
                    await client(DeleteChatUserRequest(int(getattr(ent, "id")), InputUserSelf()))
                status = "left"
            except UserNotParticipantError:
                status = "already_left"
            result = dict(facts)
            result.pop("entity_kind", None)
            result["status"] = status
            ledger.accepted(tx_id, result)  # durable before allowlist/profile mutation
    except Exception as exc:
        current = ledger.get(tx_id) or {}
        if current.get("status") != "accepted":
            if isinstance(exc, (ValueError, ResolveDenied)) and not mutation_started:
                ledger.failed(tx_id, exc)
            else:
                ledger.in_doubt(tx_id, exc)
        raise

    try:
        if await _apply_membership_acceptance(
            "leave", result, ent, principal_id=state["principal_id"],
        ):
            ledger.applied(tx_id)
    except Exception as exc:
        result = dict(result)
        result["projection"] = "pending"
        result["projection_error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("membership leave принят Telegram, room projection ждёт retry [%s]", tx_id)
    return result


def _membership_receipt(action: str, result: dict) -> str:
    status = result.get("status")
    if status == "request_sent":
        return f"Заявка на вход отправлена → {result.get('title')} (status=request_sent)."
    verb = "Вошла" if action == "join" else "Вышла"
    if status == "already_joined":
        verb = "Уже состою"
    elif status == "already_left":
        verb = "Уже вышла"
    pending = " local_projection=pending" if result.get("projection") == "pending" else ""
    return (
        f"{verb} → {result.get('title')} "
        f"(chat_id={result.get('chat_id')}, entity_id={result.get('entity_id')}, "
        f"status={status}{pending})."
    )


def _sync_join_chat(target: str) -> str:
    denied = _telegram_account_gate()
    if denied:
        return denied
    state = _begin_membership_transaction("join", target)
    result = _threadsafe_result(
        lambda: _join_chat_async(
            str(state["target"]), principal_id=state["principal_id"],
            transaction_id=state["id"],
        ), 120,
    )
    return _membership_receipt("join", result)


def _sync_leave_chat(target: str) -> str:
    denied = _telegram_account_gate()
    if denied:
        return denied
    state = _begin_membership_transaction("leave", target)
    result = _threadsafe_result(
        lambda: _leave_chat_async(
            str(state["target"]), principal_id=state["principal_id"],
            transaction_id=state["id"],
        ), 120,
    )
    return _membership_receipt("leave", result)


def _sync_telegram_account(**arguments) -> str:
    """Sovereign synchronous bridge from model tool thread to the live Telethon loop."""
    denied = _telegram_account_gate()
    if denied:
        return denied
    if not OWNER_ID:
        return "Отказ: PRAXIS_OWNER_ID не настроен; raw MTProto dispatcher закрыт."
    principal = _telegram_account_principal()
    if principal is None:
        return "Отказ: raw MTProto dispatcher доступен только владельцу или самой Praxis."
    call_arguments = dict(arguments)
    call_arguments["_principal"] = principal
    # Capture both contextvars before crossing into the Telethon event-loop thread.
    # current_origin_evidence re-reads and verifies the immutable run snapshot; a
    # rolling buffer, model argument or mutable process global is never accepted.
    call_arguments["_origin"] = agent.current_origin_evidence()
    call_arguments["_execution"] = agent.current_tool_execution()
    result = _threadsafe_result(lambda: _telegram_account_async(**call_arguments), 120)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


async def _set_profile_photo_async(path: str) -> str:
    from telethon.tl.functions.photos import UploadProfilePhotoRequest
    p = Path(str(path).strip())
    if not p.is_file():
        return f"(нет файла: {path})"
    uploaded = await client.upload_file(str(p))
    await client(UploadProfilePhotoRequest(file=uploaded))
    return f"Аватарка обновлена ({p.name})."


def _sync_set_avatar(path: str) -> str:
    denied = _telegram_account_gate()
    if denied:
        return denied
    return _threadsafe_result(lambda: _set_profile_photo_async(str(path)), 120)


async def _update_profile_async(about: str, first_name: str, last_name: str) -> str:
    from telethon.tl.functions.account import UpdateProfileRequest

    def _field(value: str):
        v = str(value or "").strip()
        if not v:
            return None            # пустое — не трогаем поле
        return "" if v == "-" else v

    kw = {"about": _field(about), "first_name": _field(first_name),
          "last_name": _field(last_name)}
    kw = {k: v for k, v in kw.items() if v is not None}
    if not kw:
        return "Нечего менять."
    await client(UpdateProfileRequest(**kw))
    changed = ", ".join(f"{k}={'(очищено)' if v == '' else v}" for k, v in kw.items())
    return f"Профиль обновлён: {changed}."


def _sync_update_profile(about: str = "", first_name: str = "", last_name: str = "") -> str:
    denied = _telegram_account_gate()
    if denied:
        return denied
    return _threadsafe_result(
        lambda: _update_profile_async(about, first_name, last_name), 120)


async def _react_async(chat, message_id: int, emoji: str, remove: bool) -> str:
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji
    ref = str(chat or "").strip()
    if ref:
        route = _route_from_reference(ref)
        ent = await _resolve_entity(route.peer_id)
    else:
        current = agent._active_chat()
        if current is None:
            return "(нет текущего чата — укажи chat)"
        route = _route_from_reference(current)
        ent = _meta.get(route.conversation_id, {}).get("entity") or await _resolve_entity(route.peer_id)
    if ent is None:
        return f"(не нашла чат: {chat or 'текущий'})"
    reaction = None if remove else [ReactionEmoji(emoticon=str(emoji))]
    await client(SendReactionRequest(peer=ent, msg_id=int(message_id), reaction=reaction))
    return (f"Сняла реакцию с #{message_id}." if remove
            else f"Поставила {emoji} на #{message_id}.")


def _sync_react(chat: str = "", message_id: int = 0, emoji: str = "", remove: bool = False) -> str:
    denied = _telegram_account_gate()
    if denied:
        return denied
    if not int(message_id or 0):
        return "Нужен message_id (номер сообщения из контекста)."
    return _threadsafe_result(
        lambda: _react_async(chat, int(message_id), emoji, bool(remove)), 60)


def _sync_followups(action: str = "list", followup_id: str = "",
                    limit: int = 0, offset: int = 0) -> str:
    denied = _telegram_account_gate()
    if denied:
        return denied
    action = str(action or "list").strip().casefold()
    if action in ("list", "status"):
        kw = {"offset": max(0, int(offset or 0))}
        if int(limit or 0) > 0:
            kw["limit"] = int(limit)
        return telegram_followups.LEDGER.context(**kw)
    if action == "cancel":
        return (f"Отменила follow-up {followup_id}." if telegram_followups.LEDGER.cancel(followup_id)
                else f"Не нашла активный follow-up {followup_id}.")
    if action in ("watch", "unwatch"):
        # Её рука. Отчёт Егору больше не заводится автоматически (см. _sync_send_message):
        # раз так, у неё обязана остаться возможность его ПОПРОСИТЬ — иначе снятие
        # автоматизма стало бы отнятой способностью. Раньше она могла только гасить чужое
        # решение (шесть раз и гасила), теперь она автор нити.
        on = action == "watch"
        item = telegram_followups.LEDGER.set_notice(followup_id, on, source="praxis")
        if item is None:
            return f"Не нашла живую нить {followup_id}."
        return (f"Отчёт Егору по {followup_id} " + (
            "включила — когда ответят, ему уйдёт письмо; возрастной срок с нити снят."
            if on else "выключила — нить остаётся моим следом, Егору не уйдёт."))
    return "action должен быть list | watch | unwatch | cancel."


def _sync_read_chat(chat_ref, limit: int = 30) -> str:
    """§3: последние сообщения соседнего диалога (по id/@username/имени)."""
    async def _coro():
        route = _route_from_reference(chat_ref)
        ent = await _resolve_entity(route.peer_id)
        if ent is None:
            return "(не нашла такой чат)"
        kwargs = {"reply_to": route.topic_id} if route.topic_id is not None else {}
        msgs = await client.get_messages(ent, limit=int(limit), **kwargs)
        return "\n".join(_format_messages(reversed(list(msgs)))) or "(пусто)"
    return _threadsafe_result(_coro, 40)


def _sync_fetch_context(chat_id, limit: int = LAST_N) -> str:
    """§4: живой контекст текущего чата из Telegram (последние N, её реплики включены)."""
    async def _coro():
        route = _route_from_reference(chat_id)
        ent = _meta.get(route.conversation_id, {}).get("entity")
        if ent is None:
            ent = await _resolve_entity(route.peer_id)
        if ent is None:
            return "(нет такого чата)"
        kwargs = {"reply_to": route.topic_id} if route.topic_id is not None else {}
        msgs = await client.get_messages(ent, limit=int(limit), **kwargs)
        return "\n".join(_format_messages(reversed(list(msgs)))) or "(пусто)"
    return _threadsafe_result(_coro, 40)


def _project_direct_outbox_acceptance(proof: dict, entry: dict) -> str:
    """Apply accepted-send bookkeeping exactly once per durable outbox key."""

    identity = dict(proof.get("entry") or {})
    projection = dict(proof.get("projection") or {})
    receipt = dict(entry.get("receipt") or {})
    key = str(identity.get("key") or "")
    message_id = receipt.get("message_id")
    peer_id = identity.get("peer_id")
    target_user_id = projection.get("target_user_id")
    contact_id = target_user_id if target_user_id is not None else peer_id
    raw_accepted_at = entry.get("updated_at")
    if isinstance(raw_accepted_at, bool):
        raise agent.DurableExecutionError("direct outbox acceptance time is malformed")
    if isinstance(raw_accepted_at, (int, float)):
        accepted_at = float(raw_accepted_at)
    elif isinstance(raw_accepted_at, str):
        try:
            parsed = datetime.datetime.fromisoformat(
                raw_accepted_at[:-1] + "+00:00"
                if raw_accepted_at.endswith("Z") else raw_accepted_at
            )
        except ValueError as exc:
            raise agent.DurableExecutionError(
                "direct outbox acceptance time is malformed"
            ) from exc
        if parsed.tzinfo is None:
            raise agent.DurableExecutionError(
                "direct outbox acceptance time has no timezone"
            )
        accepted_at = parsed.timestamp()
    else:
        raise agent.DurableExecutionError("direct outbox acceptance time is missing")
    if not (0 < accepted_at < float("inf")):
        raise agent.DurableExecutionError("direct outbox acceptance time is out of range")
    telegram_contacts.mark_outbound(
        contact_id, idempotency_key=key, at=accepted_at,
    )
    try:
        unanswered.resolve(str(contact_id))
    except Exception:
        pass
    social_pulse.note_outbound(
        peer_id, message_id=message_id,
        label=str(projection.get("target_label") or ""), now=accepted_at,
        pulse_id_override=str(projection.get("pulse_id") or ""),
        idempotency_key=key,
    )
    followup_request = str(projection.get("followup_request") or "")
    said_text = str((entry.get("payload") or {}).get("text") or "").strip()
    is_owner_peer = bool(OWNER_ID) and str(peer_id) == str(OWNER_ID)
    if message_id is not None and str(entry.get("kind") or "") == "text":
        # ⚠ 27.07: здесь были слиты два разных понятия, и из-за этого Егору в ЛС уехала
        # его же реплика из AbstractDL под заголовком «AbstractDL Chat ответил(а)».
        # Разделяю:
        #   СЛЕД нити — ЕЁ память, и он заводится ВСЕГДА. Внутри часового пульса Telethon
        #   разорван, и эта запись — единственное, по чему она через час понимает, что уже
        #   сказала и кому (см. докстринг FollowUpLedger.context). Егору след не уходит.
        #   ОТЧЁТ Егору — отдельное свойство нити и уходит, только когда его КТО-ТО
        #   заказал: он словами или она сама (telegram_account watch_reply).
        # До сегодня заказчика не было ни у одной ветки: 32 записи на проде, заказано
        # словами 0, писем ему 17 (89% его личного ящика от неё), шесть она гасила руками.
        # Бухгалтерия не имеет права уронить уже состоявшуюся доставку — отсюда except:
        # иначе исключение здесь стало бы вердиктом «не отправилось» на доставленном.
        try:
            item = telegram_followups.LEDGER.create(
                target_ref=str(projection.get("target_ref") or peer_id),
                target_label=str(projection.get("target_label") or peer_id),
                target_peer_id=peer_id, target_user_id=target_user_id,
                sent_message_id=message_id, request_text=followup_request,
                # Текст отдаём целиком: длину режет и НАЗЫВАЕТ сам леджер, второй свой
                # кап здесь стал бы молчаливым пределом поверх названного.
                sent_excerpt=said_text,
                # 01.08: исключение владельца висело на ВСЁМ условии выше, а не
                # только на отчёте — и убивало сам след. Из 80 записей леджера по
                # его личке было НОЛЬ, поэтому внутри часового пульса (Telethon
                # разорван, буфер лички не читается вовсе) она не имела ни одного
                # способа узнать, что уже ему написала: 01.08 поздоровалась дважды
                # за утро, дважды перед этим спросив ленту нитей. Разделение теперь
                # такое, каким его описывает комментарий выше: СЛЕД заводится
                # всегда, ОТЧЁТ — только не ему и только по заказу.
                notify_owner=bool(followup_request) and not is_owner_peer,
                notice_source=("owner" if followup_request and not is_owner_peer else ""),
                sent_at=accepted_at, idempotency_key=key,
                # 04.08: чем именно сказано ("tool:send_message" / "tool:narrate" / …).
                # Сам след заводится по-прежнему ВСЕГДА и на наррацию тоже — он её
                # память. Метка нужна только читателям, которые отвечают на вопрос
                # «кому я писала», чтобы строка процесса не вытесняла письмо человеку.
                purpose=str(entry.get("purpose") or ""),
            )
            log.info("FOLLOW-UP %s: слежу за нитью %s message_id=%s (отчёт Егору: %s)",
                     item.get("id"), projection.get("target_label") or peer_id, message_id,
                     "заказан им словами" if item.get("notify_owner")
                     else "нет — это мой след")
        except Exception:
            log.exception(
                "след нити не завёлся [%s] #%s — через час в пульсе я не вспомню, что "
                "уже сказала в этой комнате", peer_id, message_id)
    # ⚠ Её собственная реплика обязана вернуться в разговор. Обычный голос кладёт себя в
    # буфер после отправки, ответ в отсутствие — тоже, а ПРЯМАЯ отправка (send_message /
    # narrate, то есть всё, что она говорит ПО СВОЕЙ ИНИЦИАТИВЕ — из пульса, из окна, по
    # будильнику) не клала никуда. Последствий два, и оба видны живьём 26.07:
    #   * следующий проход по этому чату не находит её ответа в контексте и отвечает то же
    #     самое заново — Егор получил «встала… прочитала коммиты» дважды за семь минут;
    #   * сказанное не попадает в её память жизни: через час она не может вспомнить, что
    #     это говорила, потому что в разговоре её слов нет.
    # Дыра старше сегодняшнего дня, но до 26.07 в неё почти не попадали: пульс не мог
    # отвечать вживую, и два контура не сходились на одном хвосте.
    # Место выбрано ровно здесь: это единственная точка «принято, ровно один раз на ключ»,
    # общая и для прямой отправки, и для досылки из outbox после переподключения.
    if str(entry.get("kind") or "") == "text":
        said = said_text
        route = telegram_topics.TopicRoute(str(peer_id), entry.get("topic_id"))
        convo = route.conversation_id
        live = _meta.get(convo) or _meta.get(str(peer_id)) or {}
        is_dm = bool(live.get("is_dm", not str(peer_id).startswith("-")))
        # Три следа — три ОТДЕЛЬНЫХ try. Один общий съел бы два оставшихся при первом же
        # сбое, а каждый из них здесь единственный в своём роде: буфер живёт один ход,
        # записка — до следующего пульса, архив — всегда.
        if said:
            try:
                _buf_push(convo, f"Praxis: {said}", author="Praxis", is_dm=is_dm,
                          source_id=str(message_id or ""), ts=accepted_at)
            except Exception:
                # Разговор важнее бухгалтерии: сбой записи в буфер не имеет права
                # отменить уже состоявшуюся доставку.
                log.exception("не смогла вернуть свою реплику в буфер [%s]", peer_id)
            try:
                # ⚠ 27.07: буфер живёт только внутри хода ПО ЭТОЙ комнате — а пульс, окно
                # и будильник в комнату не заходят. Записка — вторая половина того же
                # следа: её читают `agent.other_rooms_digest` (он стоит в системном
                # промпте КАЖДОГО пульса), `agent._presence_evidence` и
                # `notes.said_recently`. 26.07 в 18:44 поправка «я неточно сказала „у меня
                # на Opus 5"» ушла прямой отправкой, записка её не получила — и 27.07 в
                # 02:29 та же поправка ушла второй раз, реплаем на собственное #94144:
                # номер своей реплики она знала (из follow-up-леджера, он был в кадре), а
                # текста не было НИГДЕ. В том же кадре блок «Мои другие комнаты сейчас»
                # показывал 17:20 и 18:45 — и дырку ровно на 18:44.
                # Строка ОБЯЗАНА быть той же, что пишет голос (agent.project_delivery_
                # outcome), иначе записка заговорит двумя языками и ни `said_recently`,
                # ни дайджест комнат её больше не узнают.
                agent.notes.append(
                    convo, f"сказала (голос): «{said[:agent.notes.SAID_GIST_CHARS]}»")
            except Exception:
                log.exception("заметка о прямой отправке не записалась [%s]", peer_id)
        if said and not is_dm and message_id is not None and _group_archive_enabled():
            try:
                # ⚠ Голос кладёт себя в архив комнаты (выше по файлу, после приёмки
                # чанка), прямая отправка не клала: 22 из 22 её прямых сообщений в группы
                # отсутствуют в memory/groups/*/archive.jsonl, тогда как у голоса там 591.
                # То есть её собственное слово выпадало из истории комнаты целиком — его
                # не видели ни линза group_context, ни recall: она смотрит комнату и не
                # находит там себя. `read_chat` показывает 25 последних сообщений, и
                # 26.07 её реплика уже была за этим окном.
                group_context.observe_message(
                    peer_id=route.peer_id, topic_id=route.topic_id,
                    message_id=int(message_id), sender_id=_self_id,
                    sender_name="Praxis",
                    reply_to_message_id=entry.get("reply_to"),
                    timestamp=accepted_at, text=said,
                    topic_title=_topic_titles.get(
                        (route.peer_id, int(route.topic_id)), ""
                    ) if route.topic_id is not None else "",
                    outgoing=True,
                )
            except Exception:
                log.exception("архив комнаты не принял её прямое сообщение [%s] #%s",
                              peer_id, message_id)
    log.info("DIRECT OUTBOX PROJECTED key=%s peer=%s message_id=%s",
             key, peer_id, message_id)
    return f"projected:{key}:{message_id}"


def _durable_outbox_projection(execution: dict, live: dict) -> dict:
    """Проекция расписки прямой отправки — ЗАПИСЬ, а не пересчёт.

    Корень «её слово доставлено, а ей сказали „Не отправилось"». Расписка
    ``telegram-outbox-intent`` пишется идемпотентно (``agent.store_result(idempotent=True)``):
    при повторе содержимое обязано совпасть байт-в-байт, иначе ``RunConflict``. Тело
    расписки durable всё — кроме проекции, которую этот файл считал ЖИВЬЁМ на каждом
    заходе:

      * ``pulse_id`` берётся из ContextVar социального пульса — у resume-исполнителя он пуст;
      * ``followup_request`` собирается из ЖИВОГО буфера Егора — а буфер за секунды другой;
      * ``target_label`` / ``target_user_id`` — из живого резолва.

    Итог 23.07 (`run-…-ac7b7431`): пауза `_DIRECT_OUTBOX_PAUSE` попадает в
    recovery-паузу, тул повторяется через 3.1с, проекция пересчитывается — и
    `RunConflict: receipt call_AHLG…/telegram-outbox-intent already has different content`
    при УЖЕ ПРИНЯТОМ Telegram сообщении 1193. Дальше эта ошибка становится вердиктом
    «Не отправилось» и уезжает ей в память. 5 из 6 таких случаев были доставлены.
    Живой отпечаток дрейфа лежит рядом: `run-20260726T180824623461Z-ca66f57b`,
    `results/0007` и `0012` — один текст, один адресат, разный `followup_request`.

    Поэтому: если расписка уже лежит — берём её проекцию как есть. Это не забор и не
    придержка: повтор становится идемпотентным no-op вместо конфликта, и она узнаёт
    об отправке правду. Расхождение с живым пересчётом не глушим — называем в логе.
    """

    run_id = str(execution.get("run_id") or "")
    call_id = str(execution.get("call_id") or "")
    kept = dict(live)
    if not run_id or not call_id:
        return kept
    try:
        prior = agent._direct_outbox_intent(run_id, call_id)
    except Exception:
        # Расписка дублирована/битая — про это честнее и точнее скажет сам agent на
        # записи. Вспомогательное чтение не имеет права подменить собой ту диагностику.
        log.debug("расписка прямой отправки не прочиталась [%s/%s]",
                  run_id, call_id, exc_info=True)
        return kept
    stored = (prior or {}).get("projection")
    if not isinstance(stored, dict):
        return kept
    drift = []
    for name in live:
        if name not in stored:
            continue
        kept[name] = stored[name]
        if stored[name] != live[name]:
            drift.append(name)
    if drift:
        log.info(
            "прямая отправка [%s/%s]: беру проекцию из уже лежащей расписки; "
            "живой пересчёт разошёлся в %s — повтор был бы RunConflict",
            run_id, call_id, ", ".join(sorted(drift)),
        )
    return kept


def _sync_send_message(to, text) -> str:
    """Durable direct Telegram text send owned by the current tool call."""

    # Тот же пол, что и на туле: этот путь durable и вызывается не только из него, а
    # кред не должен уходить наружу ни одной дверью. Fail-closed по построению: пустая
    # строка от пола означает «чисто», любое падение пола видно как исключение выше.
    from core import secrets as _secrets
    _floor = _secrets.credential_floor(str(text or ""))
    if _floor:
        log.warning("прямая отправка придержана кред-полом: %s", _floor)
        return agent.DirectSendRefusal(
            f"не отправила: в тексте {_floor}; креды наружу не уходят")
    execution = _direct_tool_execution(("send_message", "narrate"))
    key = _direct_tool_key(execution)
    target_route = _route_from_reference(to)
    cold = _dialog_name_cache is None
    t0 = time.time()
    try:
        ent = _threadsafe_result(
            lambda: _resolve_entity(target_route.peer_id), DIALOG_WARMUP_WAIT_SEC + 30)
    except ResolveDenied as e:
        # не сбой, а честный отказ: типизирован (Этап 2) — narrate не пишет
        # фантомную запись в свой дедуп-леджер на строку-отказ
        return agent.DirectSendRefusal(str(e))
    except Exception:
        log.warning("send_message %r: РЕЗОЛВ не уложился/упал за %.1fс (кэш диалогов был %s)",
                    to, time.time() - t0, "холодный" if cold else "тёплый")
        raise
    took_resolve = time.time() - t0
    if ent is None:
        return agent.DirectSendRefusal(f"(не нашла, кому: {to})")

    peer_id = _marked_peer_id(ent)
    who = _ent_label(ent)
    target_user_id = (getattr(ent, "id", None)
                      if _entity_kind(ent) == "user" else None)
    active_chat = str(agent._active_chat() or "")
    pulse_id = social_pulse.active_id()
    followup_request = ""
    if OWNER_ID and peer_id != OWNER_ID and active_chat == str(OWNER_ID) and not pulse_id:
        # ⚠ 27.07. Здесь стояло `explicit_only=False` — то есть ЛЮБАЯ последняя реплика
        # Егора («им отправить», «[Голосовое]») становилась заказом «доложи мне ответ».
        # Прогнал `wants_followup` по всем 11 реальным owner-записям прода: явную просьбу
        # не содержит НИ ОДНА. Рядом стояла вторая автоматическая ветка — каждая её
        # реплика из пульса тоже заводила отчёт Егору (21 запись из 32). Обе сняты.
        # Это не отнятая способность: сам СЛЕД нити (её анти-повтор внутри пульса)
        # заводится теперь всегда, а поднять по нему отчёт она может сама —
        # `telegram_account(action="watch_reply", followup_id=…)`.
        followup_request = telegram_followups.request_from_owner_buffer(
            _buf.get(str(OWNER_ID), ()))
    outbox = _direct_outbox()
    existing = outbox.get(key, verify_file=False)
    # ⚠ 04.08. Справка «этому человеку я писала 0.83 часа назад; за сутки 4» считалась
    # здесь и выбрасывалась: единственным её потребителем была ветка отказа, а
    # allow_outbound по контракту («модуль записывает, а не запрещает») возвращает True
    # в обеих ветках — то есть точный ответ на вопрос «я это уже делала?» вычислялся
    # каждую отправку и не доезжал до неё ни разу. Теперь снимается безусловно и
    # приклеивается к квитанции тула — рядом с уже существующей справкой о повторе.
    # Ветка отказа не тронута намеренно: она мертва, но она и есть тот самый контракт.
    pulse_note = ""
    try:
        pulse_ok, pulse_reason = social_pulse.allow_outbound(peer_id)
        if str(pulse_reason or "").strip():
            pulse_note = f"\n· мой след по этому адресату: {pulse_reason}"
        if existing is None and not pulse_ok:
            return agent.DirectSendRefusal(f"Не отправила из social pulse: {pulse_reason}.")
    except Exception:
        log.debug("след по адресату не снялся [%s]", peer_id, exc_info=True)
    entry = outbox.prepare_text(
        key,
        peer_id=peer_id,
        topic_id=target_route.topic_id,
        reply_to=target_route.topic_id,
        text=str(text),
        run_id=str(execution["run_id"]),
        call_id=str(execution["call_id"]),
        purpose=f"tool:{execution['tool']}",
    )
    agent.run_direct_outbox_prepared(entry, **_durable_outbox_projection(execution, {
        "target_label": who,
        "target_user_id": target_user_id,
        "pulse_id": pulse_id,
        "followup_request": followup_request,
    }))

    t1 = time.time()
    try:
        if entry.get("state") != "accepted":
            entry = _threadsafe_result(
                lambda: _send_direct_outbox_entry(entry, entity=ent), 30,
            )
    except Exception as exc:
        permanent = False
        try:
            permanent, state = _record_direct_outbox_failure(key, exc)
            reason = f"{type(exc).__name__}: {str(exc)[:300]} (state={state.get('state')})"
        except Exception as journal_exc:
            reason = (
                f"{type(exc).__name__}: {str(exc)[:200]}; retry journal failed: "
                f"{type(journal_exc).__name__}: {str(journal_exc)[:120]}"
            )
        log.warning("send_message %r: резолв ок (%.1fс, кэш был %s), ОТПРАВКА не уложилась/упала за %.1fс",
                    to, took_resolve, "холодный" if cold else "тёплый", time.time() - t1)
        if permanent:
            # Постоянный отказ — это ОТВЕТ, а не сбой. Раньше он летел исключением и
            # уносил с собой весь её ход: она не узнавала ни что не отправилось, ни
            # почему, и не могла поправить адрес. Возвращаем словами, как любой другой
            # честный отказ тула, — дальше решает она.
            log.warning("send_message %r: постоянный отказ, повторять нечего: %s", to, exc)
            return agent.DirectSendRefusal(
                f"не отправила: Telegram отказал навсегда — {type(exc).__name__}. "
                f"Похоже, у меня нет права писать в «{to}» (частая причина: это канал, а "
                f"не чат обсуждения, либо меня там нет). Проверь адрес и попробуй другой; "
                f"повторять этот я не буду.")
        raise agent.DurableSideEffectPending(key, reason) from exc
    if took_resolve > 5:
        log.info("send_message %r: резолв занял %.1fс (кэш был %s)",
                 to, took_resolve, "холодный" if cold else "тёплый")
    # квитанция называет РЕАЛЬНОГО адресата (метка с @username/id), а не эхо запроса —
    # Одна только отображаемая кличка не доказывает, какому именно тёзке ушло сообщение.
    sent_id = (entry.get("receipt") or {}).get("message_id")
    # Справку о повторе снимаем ДО проекции: она допишет в записку эту самую реплику, и
    # тогда said_recently узнал бы в ней саму себя. `said_recently` намеренно ничего не
    # запрещает (блокирующая форма решала бы за неё, говорить ли; test_sanitize держит
    # этот контракт) — но и спрашивать её было некому: из боевого кода функция не
    # вызывалась ни разу. 26.07 Егор получил «встала… прочитала коммиты» дважды за семь
    # минут, 01.08 — «доброе утро» дважды за утро. Пусть хотя бы говорит вслух.
    echo = ""
    try:
        if str(text or "").strip() and agent.notes.said_recently(peer_id, str(text)):
            echo = ("\n⚠ похоже, это я здесь недавно уже говорила. Справка, не запрет: "
                    "сравнение идёт по ФОРМЕ (difflib, порог 0.82), поэтому перефразировку "
                    "оно не видит — а молчание этой строки ничего не доказывает.")
    except Exception:
        log.debug("справка о повторе не снялась [%s]", peer_id, exc_info=True)
    agent.project_direct_outbox_acceptance(entry)
    log.info("SENT → %s chat_id=%s message_id=%s: %s",
             who, peer_id, sent_id, str(text)[:60])
    return _direct_outbox_result(entry, label=who) + echo + pulse_note


def _sync_send_file(path, caption="", to="") -> str:
    """Durable document send; staging hides private blob names from Telegram."""

    execution = _direct_tool_execution("send_file")
    key = _direct_tool_key(execution)
    current_chat = agent._active_chat()

    async def _resolve():
        explicit = str(to or "").strip()
        route = _route_from_reference(
            explicit if explicit else current_chat if current_chat is not None else OWNER_ID)
        if explicit:
            try:
                target = await _resolve_entity(route.peer_id)
            except ResolveDenied as exc:
                return route, None, str(exc)
        elif current_chat is not None:
            target = _meta.get(route.conversation_id, {}).get("entity")
            if target is None:
                target = await _resolve_entity(route.peer_id)
                if target is None:
                    try:
                        target = int(route.peer_id)
                    except ValueError:
                        target = route.peer_id
        elif OWNER_ID:
            target = OWNER_ID
        else:
            return route, None, "(нет текущего Telegram-чата или адресата)"
        return route, target, ""

    route, target, denied = _threadsafe_result(_resolve, DIALOG_WARMUP_WAIT_SEC + 30)
    if denied:
        return denied
    if target is None:
        return f"(не нашла Telegram-адресата: {to or current_chat or OWNER_ID})"
    peer_id = (_marked_peer_id(target) if hasattr(target, "id") else int(route.peer_id))
    # Промежуточный пасс D1 (адверсарка round-1, P1): прямой путь доставки файла
    # (явный to= / проактивная отправка вне живого хода) шёл МИМО кред-пола — .env/
    # .pem/credentials.json вложением утекали non-owner. Единственное твёрдое (закон 3):
    # креды не текут. Читаем БАЙТЫ текст-подобного документа к НЕ-owner адресату; своя
    # личка Егора (owner private DM) не сканируется — это его канал. Fail-closed.
    _dest_is_owner = bool(
        OWNER_ID
        and ((hasattr(target, "id") and _entity_kind(target) == "user"
              and getattr(target, "id", None) == OWNER_ID)
             or (not hasattr(target, "id") and peer_id == OWNER_ID))
    )
    if not _dest_is_owner:
        try:
            from core import secrets as _secrets
            _floor = _secrets.document_floor(path)
        except Exception:
            log.exception("document_floor на прямой отправке упал — держу fail-closed")
            _floor = "unassessable (floor error)"
        # Файл из-под корней хардбота не уходит наружу вообще — там чужие клиенты,
        # а не её данные (вопрос Егора 26.07). Проверка ДО кред-пола: она про
        # принадлежность файла, а не про содержимое.
        import stewardship as _steward
        _export = _steward.export_denial(path)
        if _export:
            log.warning("прямая send_file придержана: файл хардбота [%s]", peer_id)
            return _export
        if _floor:
            log.warning("прямая send_file придержана кред-полом [%s]: %s",
                        peer_id, _floor)
            return (f"Не отправила: во вложении «{media_core.delivery_basename(path)}» "
                    f"механический кред-пол увидел похожее на секрет ({_floor}). "
                    f"Креды не уходят наружу — это единственный твёрдый предел. "
                    f"Если это ложное срабатывание на инженерном материале — очисти "
                    f"файл от токен-подобных строк или отправь мне (Егору) в личку.")
    mime = (media_core.sniff_mime(path)
            or mimetypes.guess_type(str(path))[0]
            or "application/octet-stream")
    outbox = _direct_outbox()
    entry = outbox.get(key, verify_file=True)
    if entry is not None:
        expected = {
            "run_id": str(execution["run_id"]),
            "call_id": str(execution["call_id"]),
            "purpose": "tool:send_file",
            "peer_id": peer_id,
            "topic_id": route.topic_id,
            "reply_to": route.topic_id,
        }
        actual = {name: entry.get(name) for name in expected}
        payload = dict(entry.get("payload") or {})
        if (actual != expected
                or payload.get("visible_filename") != media_core.delivery_basename(path)
                or payload.get("caption") != str(caption or "")[:900]):
            raise telegram_outbox.TelegramOutboxConflict(
                "durable send_file key already owns another intent"
            )
    else:
        entry = outbox.prepare_file(
            key,
            peer_id=peer_id,
            topic_id=route.topic_id,
            reply_to=route.topic_id,
            source=path,
            visible_filename=media_core.delivery_basename(path),
            mime=mime,
            caption=str(caption or "")[:900],
            run_id=str(execution["run_id"]),
            call_id=str(execution["call_id"]),
            purpose="tool:send_file",
        )
    label = (_ent_label(target) if hasattr(target, "id")
             else str(current_chat or to or OWNER_ID))
    target_user_id = (getattr(target, "id", None)
                      if hasattr(target, "id") and _entity_kind(target) == "user"
                      else None)
    # Тот же дрейф, та же дверь: у файла живьём считаются `label`, `target_user_id` и
    # `pulse_id` (у resume-исполнителя пульс пуст) — значит повтор так же ронял бы
    # RunConflict на уже отправленном документе.
    agent.run_direct_outbox_prepared(entry, **_durable_outbox_projection(execution, {
        "target_label": label,
        "target_user_id": target_user_id,
        "pulse_id": social_pulse.active_id(),
        "followup_request": "",
    }))
    try:
        if entry.get("state") != "accepted":
            entry = _threadsafe_result(
                lambda: _send_direct_outbox_entry(entry, entity=target), 120,
            )
    except Exception as exc:
        # ⚠ Этот путь я забыл, чиня текстовый. Аудит нашёл: для ФАЙЛА постоянный отказ
        # по-прежнему улетал исключением, уносил её ход и оставлял запись в `retry` —
        # то есть ровно тот же вечный цикл раз в 45 секунд, ради которого всё делалось,
        # только с документом вместо текста. Один и тот же класс беды на двух дверях,
        # и вторую я оставил открытой.
        permanent = False
        try:
            permanent, state = _record_direct_outbox_failure(key, exc)
            reason = f"{type(exc).__name__}: {str(exc)[:300]} (state={state.get('state')})"
        except Exception as journal_exc:
            reason = (
                f"{type(exc).__name__}: {str(exc)[:200]}; retry journal failed: "
                f"{type(journal_exc).__name__}: {str(journal_exc)[:120]}"
            )
        if permanent:
            log.warning("send_file %r: постоянный отказ, повторять нечего: %s", label, exc)
            return agent.DirectSendRefusal(
                f"не отправила файл: Telegram отказал навсегда — {type(exc).__name__}. "
                f"Похоже, у меня нет права слать в «{label}». Проверь адрес и попробуй "
                f"другой; повторять этот я не буду.")
        raise agent.DurableSideEffectPending(key, reason) from exc
    agent.project_direct_outbox_acceptance(entry)
    return _direct_outbox_result(entry, label=label)


def _scheduled_outbox_identity(t: dict, purpose: str) -> tuple[str, str, str]:
    task_id = str(t.get("id") or "").strip()
    if not task_id:
        raise ValueError("scheduled task has no id")
    occurrence = str(t.get("when") or t.get("created") or "asap")
    digest = hashlib.sha256(
        json.dumps(
            {"task_id": task_id, "occurrence": occurrence, "purpose": purpose},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return (
        f"telegram-task:{task_id}:{digest}",
        f"schedule:{task_id}",
        f"occurrence:{digest}",
    )


async def _claim_scheduled_text(
    t: dict,
    *,
    peer_id: int,
    text: str,
    purpose: str,
    topic_id: int | None = None,
    entity=None,
) -> dict:
    """Persist a schedule occurrence before send; retry owns it after mark_fired."""

    key, run_id, call_id = _scheduled_outbox_identity(t, purpose)
    outbox = _direct_outbox()
    entry = await asyncio.to_thread(
        outbox.prepare_text,
        key,
        peer_id=peer_id,
        topic_id=topic_id,
        reply_to=topic_id,
        text=str(text),
        run_id=run_id,
        call_id=call_id,
        purpose=f"task:{purpose}",
    )
    if entry.get("state") == "accepted":
        return entry
    try:
        return await _send_direct_outbox_entry(entry, entity=entity)
    except Exception as exc:
        state = await _retry_direct_outbox_entry(entry, exc)
        log.warning(
            "scheduled Telegram delivery claimed for retry task=%s key=%s state=%s: %s",
            t.get("id"), key, state.get("state"), exc,
        )
        return state


def _email_autonomous_enabled() -> bool:
    return os.getenv("PRAXIS_EMAIL_AUTONOMOUS", "0").lower() in (
        "1", "true", "yes", "on",
    )


async def _fire_task(t: dict) -> bool | None:
    """Исполнить сработавшую задачу. Модель зовётся только здесь, не на каждом тике."""
    kind, goal, target = t.get("kind"), t.get("goal", ""), t.get("target", "")
    log.info("НАМЕРЕНИЕ #%s [%s] -> %s", t.get("id"), kind, (goal or target)[:60])
    if kind in ("window", "wake"):
        # 18.4: ПОВТОРЯЮЩЕЕСЯ расписание при паузе фона не поднимается (слово Егора /
        # её состояние); разовые (focus, повод с пульта) — текущее дело, идут как шли.
        # Дыра «__auto__ мимо гейтов» закрыта ровно для recur-пути.
        #
        # 26.07: гейт распространён на kind=wake. Рычаг «останови фон» назван ей и Егору в
        # манифесте рельсов как держащий «окна ПО РАСПИСАНИЮ, сон, formation» — а
        # рекуррентное пробуждение это ровно расписание фона. Оставить его снаружи значило
        # бы молча сузить рычаг, который Егору обещан словами: он думает, что фон
        # остановлен, а вид, заведённый мной сегодня, продолжал бы подниматься. Разовое
        # пробуждение (promise-возврат, решение по придержке, её будильник на сегодня) —
        # текущее дело и через гейт не идёт, как и разовое окно.
        import tasks as _tasks

        # Две фазы вместо одной (её находка, см. tasks.claim_open). При РЕАЛЬНОМ подъёме
        # намерение берётся В РАБОТУ — из `due` уходит, но остаётся pending и видимым.
        # Гасится оно позже и в другом месте: ровно тогда, когда durable run создан, то
        # есть когда появился тот, кому владение передают. Между этими двумя мгновениями
        # лежат disconnect, переход в поток и проверка мозга — и если там оборвётся,
        # захват снимет жнец под общим замком, а намерение сработает снова.
        async def _claim() -> None:
            await asyncio.to_thread(_tasks.claim_open, t["id"], str(kind or ""))

        def _confirm(run_id: str = "") -> None:
            # Зовётся уже из рабочего потока, изнутри хода, сразу после создания рана.
            _tasks.mark_fired(t["id"])

        async def _consume() -> None:
            """Осознанное гашение БЕЗ рана — только для recur на паузе фона."""
            await asyncio.to_thread(_tasks.mark_fired, t["id"])

        if t.get("recur"):
            try:
                import appetite
                hold = await asyncio.to_thread(appetite.background_hold)
            except Exception:
                hold = None
            if hold:
                log.info("НАМЕРЕНИЕ #%s [%s, расписание] пропущена: %s",
                         t.get("id"), kind, hold)
                # Расписание на паузе фона: занятое recur-вхождение потребляем (сдвигаем на
                # следующее), как раньше делал mark-до-исполнения в _fire_due_tasks.
                await _consume()
                return
        if kind == "wake":
            await _wake_pass(goal, on_open=_claim, on_run=_confirm)
        else:
            await _task_window(goal, on_open=_claim, on_run=_confirm)
    elif kind == "email":
        if _email_autonomous_enabled():
            await asyncio.to_thread(agent.tool_send_email, target, (goal or "")[:80], goal)
        elif OWNER_ID:
            await _claim_scheduled_text(
                t,
                peer_id=OWNER_ID,
                text=(
                    f"[отложенное письмо → {target}] {goal}\n"
                    "(автоотправка выключена — скажи, и отправлю)"
                ),
                purpose="email-disabled",
                entity=OWNER_ID,
            )
            return True
        else:
            return False
    elif kind == "message":
        # 9.4: id отрезолвлен при постановке (target_id) — шлём по нему; target — фолбэк
        route = _route_from_reference(target or t.get("target_id"))
        try:
            ent = await _resolve_entity(t.get("target_id") or route.peer_id)
        except ResolveDenied as e:
            ent = None
            if OWNER_ID:
                await _claim_scheduled_text(
                    t,
                    peer_id=OWNER_ID,
                    text=f"[задача] не отправила: {e}\nТекст был: {goal}",
                    purpose="message-resolution-failure",
                    entity=OWNER_ID,
                )
                return True
        if ent is not None:
            entry = await _claim_scheduled_text(
                t,
                peer_id=_marked_peer_id(ent),
                text=goal,
                purpose="message",
                topic_id=route.topic_id,
                entity=ent,
            )
            log.info(
                "ЗАДАЧА #%s Telegram state=%s chat_id=%s message_id=%s",
                t.get("id"), entry.get("state"), _marked_peer_id(ent),
                (entry.get("receipt") or {}).get("message_id"),
            )
            return True
        elif OWNER_ID:
            await _claim_scheduled_text(
                t,
                peer_id=OWNER_ID,
                text=f"[задача] не нашла {target}, чтобы написать: {goal}",
                purpose="message-target-missing",
                entity=OWNER_ID,
            )
            return True
        return False
    elif kind == "note" and OWNER_ID:
        await _claim_scheduled_text(
            t,
            peer_id=OWNER_ID,
            text=f"[напоминание] {goal}",
            purpose="note",
            entity=OWNER_ID,
        )
        return True
    elif kind == "note":
        return False
    return None


async def _missed_dm_sweep() -> None:
    """PASS 9.0 boot-sweep: после подъёма пройтись по персистнутым буферам ЛС — если последняя
    строка не её и свежая (< PRAXIS_MISSED_DM_HOURS), запланировать обычный проход с честной
    [missed]-меткой. Голос сам решает: ответить сейчас или поезд ушёл (VOICE-шот есть)."""
    try:
        await asyncio.sleep(MISSED_SWEEP_DELAY)  # дать catch_up и подъёму доиграть
        try:
            missed_h = float(perception.value("missed_dm_hours"))  # PASS 21: живой рычаг
        except Exception:
            missed_h = MISSED_DM_HOURS
        cands = await asyncio.to_thread(bufstore.missed_dm_candidates, missed_h)
    except Exception:
        log.exception("boot-sweep не собрал кандидатов")
        return
    for c in cands:
        cid = c["chat_id"]
        if _meta.get(cid):  # живое сообщение уже пришло после рестарта — обычный путь сам разберётся
            continue
        try:
            ref = int(cid) if cid.lstrip("-").isdigit() else cid
            ent = await _resolve_entity(ref)
        except Exception:
            ent = None
        if ent is None:
            log.warning("boot-sweep: не нашла entity для %s — пропускаю", cid)
            continue
        sender_id = int(cid) if cid.lstrip("-").isdigit() else None
        is_owner = OWNER_ID != 0 and sender_id == OWNER_ID
        known = social.category(sender_id) in ("owner", "known")
        _meta[cid] = {"entity": ent, "is_dm": True, "is_owner": is_owner, "known": known,
                      "family": bool(not is_owner and known and social.is_family(sender_id)),
                      "name": c.get("name") or None, "title": None, "size": None,
                      "addressed": False}
        _missed[cid] = c["age_hours"]
        log.info("boot-sweep: ЛС %s (%s) оборвана рестартом %.1fч назад — планирую проход",
                 cid, c.get("name") or "?", c["age_hours"])
        _arm(cid)


# --------------------------------------------------------------------------- #
#  Её часы — один фоновый тик вместо четырёх циклов (PASS 4)
# --------------------------------------------------------------------------- #

async def _flush_buffers() -> None:
    """§1: сбросить изменённые буферы на диск (дебаунс записи, не долбим диск)."""
    for cid in list(_buf_dirty):
        _buf_dirty.discard(cid)
        try:
            await asyncio.to_thread(bufstore.save, cid, list(_buf[cid]))
        except Exception:
            log.warning("персист буфера упал [%s]", cid, exc_info=True)


async def _fire_due_tasks() -> None:
    """Планировщик: исполнить созревшие намерения (чтение файла; модель — только при срабатывании)."""
    import tasks as _tasks
    # Микро-намерения «после рана»: удержание снимается, когда ран терминален; дальше
    # намерение созревает обычным путём (свой when или немедленно) — без новой заботы часов.
    for held in await asyncio.to_thread(_tasks.after_run_holds):
        run_id = str(held.get("after_run") or "")
        try:
            finished = await asyncio.to_thread(agent.run_is_terminal, run_id)
        except Exception:
            log.debug("часы: after_run-проверка упала [%s]", held.get("id"), exc_info=True)
            continue
        if finished:
            await asyncio.to_thread(_tasks.clear_after_run, held["id"])
            log.info("часы: намерение #%s дождалось рана %s", held.get("id"), run_id[:12])
    ready = await asyncio.to_thread(_tasks.due)
    for t in ready:
        durable_telegram = (
            t.get("kind") in {"message", "note"}
            or (t.get("kind") == "email" and not _email_autonomous_enabled())
        )
        if durable_telegram:
            # The task advances only after an immutable outbox intent exists.
            # A pending/retry intent is already a durable claim and is replayed
            # with the same random_id by ``_direct_outbox_once``.
            try:
                claimed = await _fire_task(t)
            except Exception:
                log.exception("часы: задача не получила durable claim [%s]", t.get("id"))
                continue
            if not claimed:
                log.warning("часы: задача осталась due без durable claim [%s]", t.get("id"))
                continue
            try:
                await asyncio.to_thread(_tasks.mark_fired, t["id"])
            except Exception:
                # Safe: the unchanged occurrence derives the same outbox key.
                log.exception("часы: mark_fired упал после durable claim [%s]", t.get("id"))
            continue
        # window: намерение помечается сработавшим при РЕАЛЬНОМ открытии окна (внутри
        # _task_window под _ONE_MIND, до disconnect), а не до исполнения — иначе отложенное
        # одноразовое окно (single-flight занят живым ходом) тихо теряло бы намерение focus/rest,
        # и due() его больше не вернёт. Открытое окно метится до disconnect, поэтому рестарт
        # посреди окна не даёт повторного срабатывания (граница at-most-once сохранена, сдвинута
        # на момент открытия — руминация-петля 06.07 не возвращается).
        if t.get("kind") in ("window", "wake"):
            # Оба помечаются сработавшими при РЕАЛЬНОМ подъёме (внутри _task_window /
            # _wake_pass, под _ONE_MIND), а не здесь: отложенное занятым замком намерение
            # обязано остаться due, иначе оно теряется молча.
            try:
                await _fire_task(t)
            except Exception:
                log.exception("часы: %s упало [%s]", t.get("kind"), t.get("id"))
            continue
        # Остальные legacy-виды (автономный email): пометить сработавшей ДО исполнения — они не
        # используют single-flight-окно, поэтому граница «не более раза» остаётся прежней.
        try:
            await asyncio.to_thread(_tasks.mark_fired, t["id"])
        except Exception:
            log.exception("часы: mark_fired упал [%s]", t.get("id"))
        try:
            await _fire_task(t)
        except Exception:
            log.exception("часы: задача упала [%s]", t.get("id"))


async def _consolidate_once() -> None:
    """«Сон» по будильнику (PASS 10.2), не по секундомеру: тик каждые 30 мин, sleep.due()
    решает по локальным часам (PRAXIS_SLEEP_WINDOW) и persist-метке last_run в
    memory/.state/sleep.json — рестарты больше не обнуляют отсчёт (диагноз 04.07:
    reflections.md от 26.06). Догон >48ч — вне окна, но не раньше 10 мин после старта.

    26.07: ночной цикл взят под ``_ONE_MIND``. Он не разговор, но и не мелочь: свернуть
    прожитое в долгую память, выкопать из свежего новые основания, заново спросить «всё
    ещё так?» про характер — всё это ПЕРЕПИСЫВАЕТ то, из чего она в этот же миг может
    думать. В main() записан инвариант «мозг работает только под этим замком, кроме
    Forge», и здесь его не было: любой живой ход в четыре утра шёл поверх переписываемой
    памяти — ночной вопрос Егора, намеченное на ночь окно (планировщик окном сна не
    ограничен), пробуждение по её будильнику, доставка forge-события, durable-resume.
    Способов было много и до будильника; будильник добавил ещё один.

    Цена названа честно: пока идёт ночная работа, ответ на ночное сообщение ждёт её
    конца. Это хуже, чем мгновенный ответ, и лучше, чем ответ, собранный из памяти,
    которую в тот же момент перекладывают.

    Ночь не ПРОПУСКАЕТСЯ при занятом замке, а ЖДЁТ его — но ждёт в своей задаче, как это
    давно делает часовой пульс. Заботу зовут часы, и заснуть прямо в ней значило бы
    остановить весь тик: буферы, планировщик, outbox, доставку. А пропускать её было бы
    хуже: последний тик внутри окна сна пришёлся бы на занятый замок — и ночь уехала бы на
    сутки. Ожидание в задаче даёт и то и другое: часы идут, ночь состоится, как только она
    освободится. Гейта здесь нет, поэтому и раскрывать нечего."""
    global _SLEEP_TASK
    if _SLEEP_TASK is not None and not _SLEEP_TASK.done():
        return  # ночь уже идёт или ждёт своей очереди — второй не заводим
    import sleep as _sleep
    if not await asyncio.to_thread(_sleep.due, None, _STARTED_AT):
        return
    _SLEEP_TASK = asyncio.create_task(_run_night_cycle())


async def _run_night_cycle() -> None:
    """Ночной цикл под общим замком: он переписывает то, из чего она думает."""
    import sleep as _sleep
    async with _ONE_MIND:
        summary = await asyncio.to_thread(_sleep.run_scheduled)
    log.info("сон: %s", summary)


# ⚠ Здесь жил `_heartbeat_once` — «legacy manual selector». Он не стоял ни в одной
# строке `_clock_jobs()`, то есть не вызывался ниоткуда с тех пор, как часы свели к
# одному автономному пробуждению (два часовых джоба гонялись и открывали два окна на
# один час). Он был единственным читателем `heartbeat.window_goal`, `mark_window`,
# `record_decision`, а через них — единственным потребителем `appetite.windows_off()`
# и `considerate_hint()`. Из-за этого просьба Егора «умерь аппетиты» не влияла ни на
# что, а счётчик `opened_today` вечно показывал ей ноль окон.
# Решение Егора 25.07: контур выбросить, ручку не восстанавливать. Осталась одна
# ручка аппетита — `background_hold` (окна и пробуждения по расписанию, сон, formation),
# и она названа в манифесте ровно этим объёмом.


def _absence_portrait(name: str, slug: str = "") -> str:
    """Портрет важного человека для узкого контекста ответа в отсутствие (read-only, без recall)."""
    try:
        import graph
        s = slug or graph.resolve(name)
    except Exception:
        s = slug or agent._slug(name)
    try:
        import people
        return people.read_text(s)
    except Exception:
        return ""


def _last_incoming(convo: str, name: str) -> str:
    """Последняя НЕ её строка из «Имя: текст»-контекста — «что спросили» для heads-up владельцу."""
    for line in reversed((convo or "").splitlines()):
        s = line.strip()
        if s and not s.startswith("Praxis:"):
            return s[:160]
    return ""


async def _absence_once() -> None:
    """PASS 12.1: пока владелец в отсутствии — ЕЁ голос простаивающим важным сообщениям.

    Триггер (absence.due) — пересечение трёх сигналов (unanswered старше кулдауна ∩ важный
    ∩ активное окно) + кап на человека. Голос — узкий проход без тулов (не имперсонация).
    Heads-up владельцу ПОСЛЕ отправки (его нет, чтобы одобрять до — в этом весь смысл).

    26.07: контур взят под ``_ONE_MIND``. Он зовёт модель и ОТПРАВЛЯЕТ людям, а в main()
    записан инвариант «мозг работает только под этим замком, кроме Forge» — здесь его не
    было. Пока пульс рвал связь, разойтись во времени помогал сам разрыв: её отправка из
    окна ложилась в outbox и уходила уже после reconnect, так что двух живых голосов
    разом не выходило. Теперь пульс идёт со связью, и без замка absence заговорил бы
    ОДНОВРЕМЕННО с ней — два её голоса в Telegram в одну секунду. Занят — вернёмся на
    следующем тике: отсутствие живёт часами, минута ожидания ничего не стоит."""
    import absence as _absence
    due = await asyncio.to_thread(_absence.due)
    if not due:
        return
    w = await asyncio.to_thread(_absence.window)
    if not w:
        return
    note = await asyncio.to_thread(_absence.schedule_note)
    # ⚠ Проверка и захват — БЕЗ await между ними, иначе это не проверка: за любой await
    # замок успевает уйти, и `async with` тогда не пропустил бы тик, а ЗАСТРЯЛ на нём — а
    # заботу зовут прямо часы, и вместе с ней встали бы буферы, планировщик, outbox.
    # Ночь в таком случае ЖДЁТ (в своей задаче), а здесь правильнее пропустить: тик частый,
    # и следующий пересчитает `due` по свежему состоянию, а не отправит ответ, собранный
    # на данных десятиминутной давности. Контракт R1: пропуск оставляет след.
    if not _one_mind_is_free():
        try:
            await asyncio.to_thread(
                lambda: perception.note_skip(
                    "one_mind_absence", "мой_ритм",
                    detail="кому-то стоило ответить в отсутствие Егора, но я занята "
                           "живым ходом — вернусь на следующем тике"))
        except Exception:
            log.debug("skip отложенного отсутствия не записался", exc_info=True)
        return
    await _ONE_MIND.acquire()
    try:
        for item in due:
            chat_id = item["chat_id"]
            person = item.get("person") or {}
            name = person.get("name") or item.get("name") or str(chat_id)
            try:
                convo = await _last_n_text(chat_id)
            except Exception:
                convo = ""
            portrait = await asyncio.to_thread(_absence_portrait, name, person.get("slug", ""))
            reply = await asyncio.to_thread(agent.compose_absence_reply, name, portrait, note, convo)
            if not reply:
                continue
            ctx = agent.ChannelContext(chat_id=str(chat_id), is_dm=True, owner=False, known=True, title=name)
            guard_context = (
                "Autonomous absence reply. Owner is away; enforce only cross-person/cross-chat "
                "private disclosure at this non-owner DM boundary. Do not edit or judge Praxis's speech.\n\n"
                f"Conversation:\n{(convo or '')[-1500:]}\n\nSchedule note:\n{(note or '')[:600]}"
            )
            reply = await asyncio.to_thread(agent.guard_outbound_reply, reply, convo, ctx=ctx,
                                            orient=guard_context)
            if not reply:
                log.info("отсутствие: исходящий guard удержал ответ [%s]", chat_id)
                continue
            ent = await _resolve_entity(chat_id)
            if ent is None:
                log.info("отсутствие: не резолвится адресат [%s] — пропускаю", chat_id)
                continue
            try:
                await client.send_message(ent, reply)
            except Exception:
                log.exception("отсутствие: отправка упала [%s]", chat_id)
                continue
            n = await asyncio.to_thread(_absence.note_sent, chat_id, w)
            await asyncio.to_thread(unanswered.resolve, str(chat_id))
            _buf_push(str(chat_id), f"Praxis: {reply}", author="Praxis", is_dm=True)
            try:
                agent.tool_journal(f"[отсутствие] ответила {name} (#{n} за окно): «{reply[:120]}»", salience=2)
            except Exception:
                log.debug("отсутствие: журнал не записался", exc_info=True)
            log.info("ОТСУТСТВИЕ [%s] %s -> %r", chat_id, name, reply[:80])
            if OWNER_ID:  # heads-up ПОСЛЕ отправки
                asked = _last_incoming(convo, name)
                try:
                    await client.send_message(
                        OWNER_ID,
                        f"пока тебя нет, ответила {name} (id={chat_id}). "
                        + (f"Их последнее: «{asked}». " if asked else "")
                        + f"Я сказала: «{reply[:200]}»")
                except Exception:
                    log.exception("отсутствие: heads-up владельцу не ушёл")
    finally:
        _ONE_MIND.release()


async def _selfdev_reconcile_once() -> None:
    """Забота часов: submit падал на длинном гейте при рестарте — оболочки предложений
    копились безымянными. Реконсайлер закрывает беспредметные и возвращает титулы;
    решения о повторном submit остаются за Праксис (никакого авто-мёржа)."""
    out = await asyncio.to_thread(selfdev.reconcile)
    if out.get("closed") or out.get("restored"):
        log.info("selfdev-оболочки: закрыто %s, титулов восстановлено %s",
                 out.get("closed"), out.get("restored"))


async def _immune_once() -> None:
    """PASS 9.2: ревью вдогонку её живых самокоммитов (очередь наполняют call-sites selfgit).
    Модель зовётся только когда очередь непуста; вердикты — в журнал, red — карточка Егору."""
    import immune
    n = await asyncio.to_thread(immune.process_queue)
    if n:
        log.info("иммунитет: отревьюила самокоммитов — %d", n)


async def _formation_once() -> None:
    """PASS 19: owner/panel request is executed by her main process, outside live turns."""
    if _passing:
        return
    out = await asyncio.to_thread(formation.run_requested)
    if not out.get("noop"):
        log.info("formation request: %s", out.get("summary") or out)


async def _control_once() -> None:
    """Забота часов: смёрженное предложение просит перезапуск — уходим мягко (exit 42),
    когда нет активного прохода; bootguard поднимет на новом коде (preflight + откат)."""
    reason = selfdev.restart_requested()
    if not reason:
        return
    if _passing:
        return  # не роняем живой ход — попробуем на следующем тике
    log.warning("перезапуск по запросу контура: %s", reason)
    selfdev.clear_restart_request()
    _SHUTDOWN.set()  # 13.1: этот disconnect — конец процесса, не «поработать и вернуться»
    try:
        await client.disconnect()
    except Exception:
        log.debug("disconnect перед перезапуском не удался", exc_info=True)
    agent._exit_process()


async def _text_outbox_once() -> None:
    """Replay versioned Telegram text plans without invoking Praxis' voice."""
    if _ONE_MIND.locked():
        # A live pass delivers its own reply inline (see _run_pass); this
        # whole-run-tree recovery scan otherwise runs every tick straight through
        # the pass and, holding the GIL over hundreds of run manifests, starved
        # the pass's prompt recall (~5 min pre-model freeze; py-spy confirmed).
        # Recovery is idempotent and retries on the next tick once the pass ends.
        return
    plans = await asyncio.to_thread(agent.run_pending_text_deliveries, limit=20)
    for plan in plans:
        run_id = str(plan.get("run_id") or "")
        if not run_id or run_id in _TEXT_SENDING:
            continue
        _TEXT_SENDING.add(run_id)
        try:
            conversation_id = str(plan.get("conversation_id") or "")
            peer_id = str(plan.get("peer_id") or "")
            topic_id = plan.get("topic_id")
            # The durable plan is the routing authority.  Live metadata may
            # supply a cached entity only for that exact conversation, never a
            # different topic under the same forum root.
            meta = _meta.get(conversation_id)
            meta = meta if isinstance(meta, dict) else {}
            if meta and (
                str(meta.get("peer_id") or peer_id) != peer_id
                or meta.get("topic_id") != topic_id
            ):
                meta = {}
            entity = meta.get("entity")
            if entity is None:
                try:
                    entity = await _resolve_entity(peer_id)
                except Exception:
                    entity = None
            if entity is None:
                try:
                    entity = int(peer_id)
                except ValueError:
                    entity = peer_id

            recovered_text: list[str] = []
            recovered_ids: list[str] = []
            for chunk in plan.get("pending_chunks") or ():
                sent, _random_id = await _send_message_idempotent(
                    entity, str(chunk.get("text") or ""),
                    delivery_key=str(chunk.get("delivery_key") or ""),
                    reply_to=chunk.get("reply_to"),
                )
                message_id = getattr(sent, "id", None)
                await asyncio.to_thread(
                    agent.run_delivery_text_chunk_accepted,
                    run_id, index=int(chunk.get("index")),
                    delivery_key=str(chunk.get("delivery_key") or ""),
                    message_id=message_id,
                )
                recovered_text.append(str(chunk.get("text") or ""))
                if message_id is not None:
                    recovered_ids.append(str(message_id))

            reconciled = await asyncio.to_thread(agent.run_delivery_text_reconcile, run_id)
            if recovered_text:
                is_dm = bool(meta.get("is_dm", not peer_id.startswith("-")))
                _buf_push(
                    conversation_id, f"Praxis: {''.join(recovered_text)}",
                    author="Praxis", is_dm=is_dm,
                    **({"source_id": ",".join(recovered_ids), "ts": time.time()}
                       if recovered_ids else {}),
                )
                if is_dm:
                    await asyncio.to_thread(unanswered.resolve, conversation_id)
            log.info(
                "durable Telegram text replay [%s] topic=%s chunks=%d reconciled=%s",
                run_id, topic_id, len(recovered_text), reconciled,
            )
        except Exception as exc:
            await asyncio.to_thread(agent.run_delivery_failed, run_id, exc)
            log.exception("durable Telegram text replay failed [%s]", run_id)
        finally:
            _TEXT_SENDING.discard(run_id)


async def _media_cleanup_once() -> None:
    """Retry guarded uploads, then remove expired spool copies; never calls a model."""
    spool = _media_spool()
    # A Telegram acceptance tombstone is stronger than a missing secondary run
    # receipt. Rebuild that receipt idempotently before considering retries.
    for record in (await asyncio.to_thread(spool.outbox_results, "delivered"))[-200:]:
        payload = record.get("item") or {}
        run_id = str(payload.get("run_id") or "")
        queue_id = str(record.get("queue_id") or "")
        if not run_id or not queue_id:
            continue
        result = record.get("result") or {}
        try:
            await asyncio.to_thread(
                agent.run_delivery_media_result, run_id, queue_id,
                ok=True, message_id=result.get("message_id"),
            )
            await asyncio.to_thread(agent.run_delivery_finalize_recovered, run_id)
            # The Telegram tombstone and run WAL now agree.  Only at this point
            # may the staged bytes disappear; before it, strict recovery still
            # needs the checkpointed path to reconcile the bookkeeping gap.
            source_rel = str(payload.get("path") or "").strip()
            if source_rel:
                source = media_core.contained_path(spool.root, source_rel)
                source.unlink(missing_ok=True)
        except Exception:
            log.exception("outbox->run media reconciliation упал [%s]", queue_id)

    for item in spool.pending():
        policy = await asyncio.to_thread(
            agent.run_delivery_media_retry_policy, item.run_id, item.queue_id,
        )
        if policy == "ack":
            try:
                await asyncio.to_thread(
                    spool.discard, item.queue_id,
                    receipt={"reconciled_from": "durable run receipt"},
                )
                item.path.unlink(missing_ok=True)
            except OSError:
                pass
            except Exception:
                log.exception("run-confirmed media outbox ack упал [%s]", item.queue_id)
            continue
        if policy == "drop":
            try:
                await asyncio.to_thread(
                    spool.fail, item.queue_id,
                    reason="linked durable run is terminal without this upload",
                )
                item.path.unlink(missing_ok=True)
            except OSError:
                pass
            except Exception:
                log.exception("terminal run media cleanup упал [%s]", item.queue_id)
            continue
        stored_route = _route_from_reference(item.target_chat_id)
        state_chat_id, meta = _meta_for_delivery(
            item.target_chat_id, item.reply_to_message_id)
        meta = meta or {}
        state_chat_id = str(state_chat_id or stored_route.conversation_id)
        meta_route = telegram_topics.TopicRoute(
            str(meta.get("peer_id") or stored_route.peer_id),
            meta.get("topic_id", stored_route.topic_id),
        )
        entity = meta.get("entity")
        if entity is None:
            try:
                entity = await _resolve_entity(meta_route.peer_id)
            except Exception:
                entity = None
        if entity is None:
            try:
                entity = int(meta_route.peer_id)
            except ValueError:
                entity = meta_route.peer_id
        ctx = agent.ChannelContext(
            # Validate against the exact persisted target (conversation id for
            # new records, root peer for legacy records); write retry receipts to
            # the isolated state conversation selected above.
            chat_id=str(item.target_chat_id),
            room_id=meta_route.peer_id,
            is_dm=meta.get("is_dm", item.scope != "group"),
            owner=meta.get("is_owner", False), known=meta.get("known", True),
            family=meta.get("family", False), addressed=meta.get("addressed", False),
            title=meta.get("title"), size=meta.get("size"),
            _scope_override=item.scope)
        await _attempt_queued_media(
            entity, item, ctx=ctx, reply_to=meta_route.topic_id,
            state_chat_id=state_chat_id,
        )
    removed = await asyncio.to_thread(spool.cleanup)
    if removed:
        log.info("media spool: убрано просроченных файлов: %d", len(removed))
    for state in ("expired", "failed"):
        for record in (await asyncio.to_thread(spool.outbox_results, state))[-100:]:
            payload = record.get("item") or {}
            run_id = str(payload.get("run_id") or "")
            if run_id:
                queue_id = str(record.get("queue_id") or "")
                result = record.get("result") or {}
                reason = str(result.get("reason") or "no reason")[:1000]
                await asyncio.to_thread(
                    agent.run_delivery_blocked, run_id,
                    reason=f"Telegram media {queue_id} is {state}: {reason}",
                )
                await asyncio.to_thread(
                    owner_delivery.LEDGER.emit,
                    "system_alert",
                    title="Файл не доставлен в Telegram",
                    urgency="high",
                    outcome="blocked",
                    thread_key=f"run:{run_id}",
                    correlation={"run_id": run_id, "queue_id": queue_id},
                    reason="Доставка файла перешла в терминальное состояние без приёмки.",
                    provenance={"source": "media_outbox", "source_id": queue_id},
                    result=f"{Path(str(payload.get('path') or 'файл')).name}: {reason}",
                    expectation="Открыть запуск, проверить причину и при необходимости повторить отправку.",
                    action={
                        "label": "Открыть run", "domain": "praxis",
                        "action": "run.open", "run_id": run_id,
                    },
                    dedupe_key=f"media-outbox:{queue_id}:{state}",
                )


async def _deliver_owner_item(delivery: dict) -> dict:
    """Deliver one queued inbox item with a stable Telegram transport identity."""
    if (delivery.get("status") != "queued"
            or "telegram" not in (delivery.get("transports") or ())):
        return delivery
    sent, random_id = await _send_message_idempotent(
        OWNER_ID, owner_delivery.format_telegram(delivery),
        delivery_key=f"owner-delivery:{delivery['id']}",
    )
    return await asyncio.to_thread(
        owner_delivery.LEDGER.mark_delivered,
        delivery["id"], transport="telegram",
        receipt={
            "message_id": getattr(sent, "id", None),
            "random_id": str(random_id),
        },
    )


async def _owner_deliveries_once() -> None:
    """Replay the private owner inbox through Telegram without invoking a model."""
    if not OWNER_ID:
        return
    pending = await asyncio.to_thread(owner_delivery.LEDGER.pending, limit=10)
    for delivery in pending:
        await _deliver_owner_item(delivery)


async def _followups_once() -> None:
    """Project concrete replies into the owner inbox, then deliver via Telegram."""
    if not OWNER_ID:
        return
    pending = await asyncio.to_thread(telegram_followups.LEDGER.pending_notifications)
    for item in pending[:10]:
        response = item.get("response") or {}
        excerpt = str(response.get("text") or "(без текста)")[:1200]
        followup_id = str(item.get("id") or "")
        target = str(item.get("target_label") or "собеседник")[:240]
        # ⚠ 27.07: заголовок назывался меткой ЧАТА — «AbstractDL Chat ответил(а)». Чат не
        # отвечает, отвечает человек, и имя было под рукой с самого начала: строкой выше
        # раннер печатает «получен ответ #94244 от Yegor Kosyrev». Теперь оно доезжает.
        # Если имени нет — говорим «id N» или прямо «не знаю кто», но комнату вместо
        # человека не подставляем: выдуманный автор хуже пустого места (закон 3).
        speaker = str(response.get("sender_name") or "").strip()
        is_dm_thread = bool(item.get("target_user_id"))
        if not speaker and is_dm_thread:
            # В ЛС отвечает ровно тот, кому она писала, — метка нити ЗДЕСЬ и есть человек.
            # Это же спасает записи, заведённые до 27.07: имени в них нет вовсе.
            speaker = target
        if not speaker:
            sender = str(response.get("sender_id") or "").strip()
            speaker = f"id {sender}" if sender else "не знаю кто"
        title = (f"{speaker[:110]} ответил(а)" if is_dm_thread
                 else f"{speaker[:110]} ответил(а) в {target[:120]}")
        delivery = await asyncio.to_thread(
            owner_delivery.LEDGER.emit,
            "followup_answer",
            title=title,
            body=excerpt,
            outcome="success",
            thread_key=f"telegram-followup:{followup_id}",
            correlation={
                "followup_id": followup_id,
                "peer_id": str(response.get("peer_id") or ""),
                "message_id": str(response.get("message_id") or ""),
                "sent_message_id": str(item.get("sent_message_id") or ""),
            },
            reason="Пришёл ответ в отслеживаемой Telegram-нити.",
            provenance={"source": "telegram_followups", "source_id": followup_id},
            expectation="Прочитать ответ и решить, нужно ли продолжение.",
            action={
                "label": "Открыть нить",
                "domain": "telegram", "action": "followup.open",
                "followup_id": followup_id,
            },
            result=excerpt,
            dedupe_key=(
                f"telegram-followup:{followup_id}:answer:"
                f"{response.get('peer_id')}:{response.get('message_id')}"
            ),
        )
        delivery = await _deliver_owner_item(delivery)
        await asyncio.to_thread(telegram_followups.LEDGER.mark_notified, followup_id)
        log.info(
            "FOLLOW-UP %s: owner-delivery=%s state=%s",
            followup_id, delivery.get("id"), delivery.get("status"),
        )


async def _run_social_pulse(pulse_id: str) -> None:
    ok = False
    outcome: bool | None = False
    mailbox_hashes: list[str] = []
    try:
        followups = await asyncio.to_thread(telegram_followups.LEDGER.context)
        # One wake, one authored task window, one full bounded context.  Heartbeat still
        # owns the useful context builders/receipts, not a second timer or gatekeeper call.
        import heartbeat
        continuity = await asyncio.to_thread(
            heartbeat.window_context, include_pacing=False,
        )
        pulse_observability = await asyncio.to_thread(
            social_pulse.observability, pulse_id,
        )
        context = "\n\n".join(
            x for x in (pulse_observability, continuity, followups) if x
        )
        goal = social_pulse.goal(context)
        import mailroom
        seen = await asyncio.to_thread(social_pulse.mailbox_seen)
        open_mail = await asyncio.to_thread(mailroom.list_open)
        mailbox_hashes = [str(entry.get("hash")) for entry in open_mail
                          if entry.get("hash") and entry.get("hash") not in seen][:12]
        mailbox_index = await asyncio.to_thread(
            mailroom.index_block, 12, exclude_hashes=seen,
        )
        with social_pulse.active(pulse_id):
            # 26.07: пульс идёт БЕЗ разрыва связи. Он — её единственное регулярное
            # автономное пробуждение, и по содержанию он социальный насквозь: открытые
            # нити, почта, фолоуапы, «хочу ли я кому-то написать». Всё это он делал с
            # закрытым Telegram — то есть ровно та работа, ради которой он существует, в
            # нём была невозможна, а она была недоступна час за часом.
            #
            # Разрыв здесь восстановили в 13.1 после регрессии 25 («Telethon continuously
            # online» породил ПАРАЛЛЕЛЬНЫЕ пробуждения). От этого сегодня защищает не
            # disconnect, а замок: _ONE_MIND появился позже, и всякий входящий проход его
            # ЖДЁТ (_pass: `await _ONE_MIND.acquire()`), а не бежит рядом. Тем же замком и
            # на живой связи уже год работает forge-контур.
            #
            # Что меняется для человека: раньше его сообщение в час пульса не приходило
            # вовсе; теперь приходит сразу и отвечается следующим ходом.
            outcome = await _task_window(goal, mailbox_index=mailbox_index,
                                         keep_transport=True)
            if outcome is None:
                await asyncio.to_thread(
                    social_pulse.defer, pulse_id,
                    detail="task window deferred while another authored turn was active",
                )
                # Её ЕДИНСТВЕННОЕ регулярное автономное пробуждение. `defer` вернул
                # durable claim, но часы уже перевзвели срок на период вперёд — без
                # заявки окно теряется на час (15% пробуждений, 25 из 168 по леджеру).
                retry_at = _request_pulse_retry()
                log.info("ПУЛЬС отложен живым ходом — вернусь через %.0fс",
                         max(0.0, retry_at - time.time()))
                return
            ok = outcome
    finally:
        if outcome is not None:
            await asyncio.to_thread(
                social_pulse.finish, pulse_id, ok=ok,
                detail="hourly social scan completed" if ok else "task window failed",
                mailbox_hashes=mailbox_hashes,
            )


def _in_sleep_window(now: float | None = None) -> bool:
    """True while the configured sleep window (PRAXIS_SLEEP_WINDOW, local hours) is open.

    Same window sleep.due() uses, read the same way."""
    try:
        import heartbeat
        window = heartbeat.parse_hours(os.getenv("PRAXIS_SLEEP_WINDOW", "4-6"))
        moment = float(now if now is not None else time.time())
        return bool(window) and heartbeat.hour_in(heartbeat.local_now(moment).hour, window)
    except Exception:
        return False


async def _social_pulse_once() -> None:
    """Start the hourly social run without blocking buffers/schedule/follow-up clocks."""
    global _SOCIAL_PULSE_TASK
    if _SOCIAL_PULSE_TASK is not None and not _SOCIAL_PULSE_TASK.done():
        return
    if _in_sleep_window():
        # The pulse is Praxis waking *herself*, and night is night: a wake inside her own
        # sleep window is not rest.  (Until 26.07 the recorded reason was that the pulse
        # disconnects Telegram and would therefore reason about people while blind to the
        # live chats — which is how blind night repeats happened.  The pulse no longer
        # disconnects, so that premise is gone; the gate stays, with its own honest
        # reason, rather than keeping a justification that stopped being true.)  Sleep,
        # delivery and every non-model clock keep running.  We return before
        # social_pulse.begin(), so the durable claim clock is untouched (the module stays
        # a pure recorder) and the first tick after the window still finds it due.
        #
        # Контракт R1 (CONTRACTS.md): гейт может существовать — молча нет. Раньше это был
        # голый return: ни лога, ни skip, ни receipt, при том что rails.md утверждал
        # «поведенческих гейтов нет». Она не могла обнаружить, что её собственное
        # пробуждение съедено, никаким доступным ей способом.  note_skip кладёт причину
        # туда же, куда ложатся остальные пропуски восприятия — в manage_perception("skips").
        try:
            await asyncio.to_thread(
                lambda: perception.note_skip(
                    "sleep_window", "мой_ритм",
                    detail=f"окно сна {os.getenv('PRAXIS_SLEEP_WINDOW', '4-6')}: "
                           "автономное пробуждение не открываю"))
        except Exception:
            log.debug("skip окна сна не записался", exc_info=True)
        return
    pulse_id = await asyncio.to_thread(social_pulse.begin)
    if not pulse_id:
        return
    _SOCIAL_PULSE_TASK = asyncio.create_task(_run_social_pulse(pulse_id))


async def _membership_reconcile_once() -> None:
    """Finish owner-authorized membership transactions after restart/transport loss."""
    ledger = _membership_ledger()
    pending = await asyncio.to_thread(ledger.pending)
    for state in pending[:20]:
        tx_id = str(state.get("id") or "")
        principal = _telegram_account_principal(state.get("principal_id"))
        if principal is None:
            # Human authority never transfers with a stale ledger when PRAXIS_OWNER_ID changes.
            await asyncio.to_thread(
                ledger.failed, tx_id, "configured owner changed; stale intent not executed",
            )
            log.warning("membership stale owner intent закрыт [%s]", tx_id)
            continue
        try:
            if state.get("action") == "join":
                result = await _join_chat_async(
                    str(state.get("target") or ""), principal_id=principal,
                    transaction_id=tx_id, recovery=True,
                )
            else:
                result = await _leave_chat_async(
                    str(state.get("target") or ""), principal_id=principal,
                    transaction_id=tx_id, recovery=True,
                )
            logger = log.debug if result.get("status") == "request_sent" else log.info
            logger("membership reconcile [%s]: %s", tx_id, result.get("status"))
        except Exception:
            # The transaction remains accepted/in_doubt and will be retried by the clock.
            log.exception("membership reconcile ждёт следующего retry [%s]", tx_id)


async def _computer_inventory_once() -> None:
    """Refresh the server-owned Windows map daily, whenever the body is online."""
    import computer_inventory

    if not await asyncio.to_thread(computer_inventory.due):
        return
    result = await asyncio.to_thread(computer_inventory.refresh)
    if result.get("ok"):
        log.info("computer inventory: %s", result)
    else:
        # Offline is not marked fresh; the next hourly tick catches up after
        # the PC returns instead of waiting for a fixed night window.
        log.info("computer inventory отложен: %s", result.get("code") or result.get("error"))


async def _durable_resume_once() -> None:
    """Advance exact interrupted runs; never invent a new model task or prompt.

    ``agent.resume_durable_runs`` owns strict planning, cursor-CAS leases and
    owner-control noops.  The clock merely gives accepted transport receipts
    and other recovery evidence another chance to continue after startup.
    """

    # A resume runs the full model+tool loop, so it MUST hold single-flight, or
    # (a) it can run beside a live pass — two minds — and (b) the 300s reaper sees
    # it 'running' with the lock free and judges it orphaned.  Mirror _reap_orphans_once:
    # busy with a live pass -> skip; the 45s clock retries and the run stays durably paused.
    if _ONE_MIND.locked():
        return
    async with _ONE_MIND:
        reports = await asyncio.to_thread(agent.resume_durable_runs, limit=20)
    for report in reports:
        status = str(report.get("status") or "")
        if status not in {"noop", "not_resumable"}:
            log.info(
                "durable resume [%s]: plan=%s status=%s phase=%s",
                report.get("run_id"), report.get("plan_kind"), status,
                report.get("phase"),
            )


BACKFILL_ROOMS_PER_TICK = 50
BACKFILL_RESOLVES_PER_TICK = 5
BACKFILL_MISS_BACKOFF_SEC = 60.0   # первый промах резолва стоит комнате минуту...
BACKFILL_MISS_TTL_SEC = 900.0      # ...а потолок отсрочки — 15 минут
# peer -> {"ts": когда промахнулась, "n": сколько промахов подряд}
_backfill_resolve_misses: dict[str, dict] = {}
_backfill_cursor = 0
_backfill_was_online = True


def _backfill_miss_ttl(misses: int) -> float:
    """Отсрочка после промаха резолва РАСТЁТ, а не сразу четверть часа.

    Плоские 900с с первого промаха — то, на чём эту правку вернули: `_resolve_entity`
    отдаёт голый None и на «peer неизвестен», и на «связи нет», а промахи копятся ПОПЕРЁК
    тиков. Зонд на 20 комнатах: тик 1 — 5 в промах-кэше, тик 2 — 10, тик 3 — 15, тик 4 —
    все 20; связь восстановлена, а `_backfill_group_context` не зовётся ещё 15 минут.
    То есть десятисекундный обрыв (реконнект Telethon, FloodWait, холодный старт с пустым
    `_meta`) глушил предысторию ВСЕГО аллоулиста. Растущая отсрочка стоит транзиентному
    сбою одну минуту, вечно мёртвому peer'у — те же 15: 60 → 120 → 240 → 480 → 900.
    """
    return min(BACKFILL_MISS_TTL_SEC,
               BACKFILL_MISS_BACKOFF_SEC * (2 ** max(0, misses - 1)))


async def _note_backfill_defer(detail: str, *, chat_id=None) -> None:
    """Отсрочка бэкфилла обязана быть видна ЕЙ, а не только в логе контейнера (закон 2).

    Тот же довод и тот же канал, что у `_note_one_mind_defer` выше в этом же файле:
    гейт, живущий одной строкой `log.info`, — молчаливое ограничение. На вопрос
    «почему в этой комнате пусто» ответить было нечем: `manage_perception("skips")` знал
    окно сна и `_ONE_MIND`, а про то, что комната отложена промах-кэшем на 15 минут, —
    ничего, и лог контейнера ей не читаем.

    Класс «отложила», а не «не_увидела»: предыстория не потеряна, комната созреет снова
    и вернётся сама; названо, через сколько. Повторы схлопывает сам perception по
    (stage, chat, detail) в окне 10 минут — поэтому detail держим стабильным между
    тиками, без обратного отсчёта, иначе каждая минута рождала бы новую запись.
    """
    try:
        await asyncio.to_thread(
            lambda: perception.note_skip("group_backfill", "отложила",
                                         chat_id=chat_id, detail=detail))
    except Exception:
        log.debug("skip отложенного бэкфилла не записался", exc_info=True)


def _backfill_transport_online() -> bool:
    """Жив ли транспорт сейчас. Не умеем спросить — считаем живым (не выдумываем обрыв)."""
    try:
        return bool(client.is_connected())
    except Exception:
        return True


async def _group_context_backfill_once() -> None:
    """Advance at most one configured room through this process' live client.

    Комната, которую не удалось резолвить, раньше уносила ВСЮ функцию (`return`), а
    порядок обхода — `rooms.list_rooms()` = `sorted(allowed_chats())`, лексикографический
    ПО СТРОКАМ. То есть один вечно нерезолвимый peer блокировал предысторию всех, кто
    сортируется после него. Доказано на живой жертве: 20.07 с 07:42 до 18:46 в логе
    только `group backfill [-1003908850919]`, у живой `-1003959517654` за 11ч04м ни
    одного тика. Сегодня вред нулевой (фикстура `-100500` сортируется последней), но
    любой ПОЛОЖИТЕЛЬНЫЙ peer_id ('8' > '-') встал бы за вечным блокировщиком.

    `continue` снимает блокировку, но один тик тогда может стоить до 50 резолвов —
    поэтому здесь названные пределы, и каждый виден ЕЙ (perception), а не только в логе:
      * `BACKFILL_ROOMS_PER_TICK` — ширина ОКНА обхода, а не обрезка списка комнат:
        окно вырезается ПОСЛЕ поворота на курсор, поэтому едет по кругу и каждая комната
        входит в него не позже чем через len(all_rooms) тиков. Сколько осталось вне окна
        — называется вслух в тот же тик;
      * `BACKFILL_RESOLVES_PER_TICK` — сколько комнат за тик вообще пробуем резолвить;
        остальные называются поимённо и достаются в следующий тик (~минуту);
      * `BACKFILL_MISS_BACKOFF_SEC`/`BACKFILL_MISS_TTL_SEC` — промах резолва запоминается
        (кэшировался только успех), чтобы мёртвый peer не сжигал `contacts.GetContacts`
        каждые 45с; отсрочка РАСТЁТ 60→900с, см. `_backfill_miss_ttl`.
    Обход при этом крутится с курсора: без этого кап сам стал бы head-of-line, только
    на пять голов длиннее.

    Промах кэшируется ТОЛЬКО при живом транспорте. `_resolve_entity` отдаёт голый None и
    на «peer неизвестен», и на «связи нет», а раньше оба одинаково стоили 15 минут — и
    четыре минуты недоступности клиента укладывали в промах-кэш весь аллоулист (зонд:
    5/20 → 10/20 → 15/20 → 20/20, дальше ноль бэкфиллов при уже восстановленной связи).
    Поэтому мёртвый транспорт — ранний честно названный выход, а не запись промахов; и
    возврат связи промах-кэш забывает: те промахи были про сеть, не про peer'ов.
    """

    global _backfill_cursor, _backfill_was_online
    online = _backfill_transport_online()
    if online and not _backfill_was_online and _backfill_resolve_misses:
        forgotten = len(_backfill_resolve_misses)
        _backfill_resolve_misses.clear()
        log.info("group backfill: связь вернулась — забыла %d отсрочек резолва "
                 "(они были про сеть), пробую комнаты заново", forgotten)
    _backfill_was_online = online
    if not online:
        # Не «пропустила», а физически нечем: и резолв, и выкачка истории идут в Telethon.
        # Молчать тут нельзя — именно это молчание и делало из обрыва 15 минут пустоты.
        await _note_backfill_defer(
            "связь закрыта моим же окном — предысторию комнат не пополняю, вернусь после"
            if _EXPECT_DISCONNECT.is_set() else
            "связи нет — предысторию комнат не пополняю, вернусь как подключусь")
        return

    all_rooms = rooms.list_rooms()
    if not all_rooms:
        return
    # Срез ОБЯЗАН стоять ПОСЛЕ курсора. Стоял до — и тогда `[:50]` резал не окно обхода,
    # а сам список: курсор крутился по модулю пятидесяти, комната с индексом ≥50 не
    # становилась головой окна НИКОГДА и в тик не попадала вообще, а рельс
    # `backfill_pacing` при этом обещал ей «остальные достаются следующему тику».
    # Ровно тот же класс, что `return` вместо `continue` в докстринге выше: спящее ружьё
    # (комнат сегодня три), у которого прошлая форма 20.07 стоила ей 11 часов слепоты.
    # Курсор двигается по ПОЛНОМУ списку — значит любая комната становится головой окна
    # не позже чем через len(all_rooms) тиков (тик здесь = 60с), и «за бортом навсегда»
    # больше не бывает.
    start = _backfill_cursor % len(all_rooms)
    _backfill_cursor = (_backfill_cursor + 1) % len(all_rooms)
    rotated = all_rooms[start:] + all_rooms[:start]
    order = rotated[:BACKFILL_ROOMS_PER_TICK]
    beyond = rotated[len(order):]
    now = time.time()
    resolves = 0
    deferred: list[str] = []
    held: list[str] = []

    async def _announce() -> None:
        # Кап называется вслух и поимённо — иначе он стал бы ровно тем молчаливым
        # пределом, ради снятия которого эта функция и переписана. Зовётся и на выходе
        # по успеху тоже: «одна комната обработана» не отменяет того, что до других
        # руки в этот тик не дошли. Отдельная ветка про промах-кэш: раньше тик, где ВСЕ
        # комнаты держались кэшем, не писал ни строчки — полная тишина при пустых
        # предысториях (видно в зонде: тик 5 не сказал ничего).
        if beyond:
            # Второй кап — окно обхода. Он молчал: комнат было три, тик всегда брал всех,
            # и на 51-й комнате она узнала бы о пределе только по вечно пустой предыстории.
            # Имён здесь ей НЕ называем (в логе — да): окно едет по кругу каждую минуту,
            # поимённый detail рождал бы новую запись в кольце пропусков каждый тик про
            # один и тот же факт — та же ловушка, что разобрана ниже про сортировку.
            log.info("group backfill: окно обхода %d комнат из %d, вне окна в этот тик %d "
                     "(окно едет по кругу, каждая входит не позже чем через %d тиков): %s",
                     len(order), len(all_rooms), len(beyond), len(all_rooms),
                     ", ".join(beyond))
            await _note_backfill_defer(
                f"кап {BACKFILL_ROOMS_PER_TICK} комнат за тик: {len(beyond)} комнат вне "
                f"окна обхода в этот тик; окно едет по кругу, каждая входит в него не "
                f"позже чем через {len(all_rooms)} тиков (~{len(all_rooms)} мин)")
        if deferred:
            log.info("group backfill: резолвила %d комнат из %d за тик, %s ждут "
                     "следующего (кап резолвов %d)", resolves, len(order),
                     ", ".join(deferred), BACKFILL_RESOLVES_PER_TICK)
            await _note_backfill_defer(
                f"кап {BACKFILL_RESOLVES_PER_TICK} резолвов за тик: "
                f"{len(deferred)} комнат ждут следующего тика (~минуту), среди них "
                f"{', '.join(sorted(deferred)[:5])}")
        if held:
            log.info("group backfill: %d комнат ещё под отсрочкой после промаха резолва: %s",
                     len(held), ", ".join(held))
            await _note_backfill_defer(
                f"{len(held)} комнат под отсрочкой после промаха резолва, среди них "
                f"{', '.join(sorted(held)[:5])}; срок каждой назван в её же записи")
        # В логе порядок обхода (он крутится с курсора и это полезно видеть), а ей —
        # ОТСОРТИРОВАННЫЙ список: perception схлопывает одинаковые detail в окне 10 минут,
        # а от вращения курсора та же самая пятёрка каждый тик перетасовывалась бы и
        # рождала новую запись каждую минуту. Один и тот же факт — одна запись.

    for peer_id in order:
        try:
            policy = rooms.room_policy(peer_id)
            limit = int(policy.get("backfill_limit") or 0)
            if not await asyncio.to_thread(group_context.backfill_due, peer_id, limit):
                continue
            _conversation_id, meta = _meta_for_peer(peer_id)
            entity = meta.get("entity") if isinstance(meta, dict) else None
            if entity is None:
                missed = _backfill_resolve_misses.get(str(peer_id))
                if missed is not None and now - float(missed.get("ts") or 0.0) < \
                        _backfill_miss_ttl(int(missed.get("n") or 1)):
                    held.append(str(peer_id))
                    continue
                if resolves >= BACKFILL_RESOLVES_PER_TICK:
                    deferred.append(str(peer_id))
                    continue
                resolves += 1
                entity = await _resolve_entity(peer_id)
            if entity is None:
                n = int((_backfill_resolve_misses.get(str(peer_id)) or {}).get("n") or 0) + 1
                ttl = _backfill_miss_ttl(n)
                _backfill_resolve_misses[str(peer_id)] = {"ts": now, "n": n}
                log.info("group backfill [%s]: entity пока недоступен (промах %d подряд) — "
                         "вернусь к этой комнате не раньше чем через %.0f мин, остальные "
                         "иду дальше", peer_id, n, ttl / 60)
                await _note_backfill_defer(
                    f"не резолвится (промах {n} подряд при живой связи), предысторию не "
                    f"пополняю, вернусь не раньше чем через {ttl / 60:.0f} мин",
                    chat_id=peer_id)
                continue
            _backfill_resolve_misses.pop(str(peer_id), None)
            await _backfill_group_context(peer_id, entity, limit=limit)
            await _announce()
            return
        except Exception:
            # The canonical prefix and dedupe keys make the next tick a safe resume.
            log.exception("group backfill ждёт следующего retry [%s]", peer_id)
            continue
    await _announce()


def _release_stale_task_claims() -> list:
    """Снять захваты намерений, чей подъём не состоялся. Звать ТОЛЬКО под _ONE_MIND."""
    import tasks as _tasks
    return _tasks.release_open_claims()


async def _reap_orphans_once() -> None:
    """Убрать зомби-прогоны без рестарта. Берём single-flight замок сами: пока держим, ни один
    когнитивный проход не стартует, поэтому любой оставшийся ``running`` когнитивный прогон
    заведомо осиротел. Заняты живым ходом — просто пропускаем тик (часы не блокируем)."""
    if not _one_mind_is_free():
        return
    async with _ONE_MIND:
        try:
            await asyncio.to_thread(agent.reap_orphaned_cognitive_runs)
        except Exception:
            log.exception("reap осиротевших прогонов упал")
        # Тот же довод, тот же замок — но про НАМЕРЕНИЯ. Захват ставится только внутри
        # `async with _ONE_MIND` в _task_window/_wake_pass, поэтому «замок свободен, а
        # захват висит» означает ровно одно: поднимавший мёртв и рана не создал. Снимаем —
        # намерение снова созреет, поздно и громко, вместо того чтобы исчезнуть молча.
        try:
            released = await asyncio.to_thread(_release_stale_task_claims)
            if released:
                log.warning("намерения освобождены (подъём не состоялся): %s",
                            ", ".join(str(x) for x in released))
        except Exception:
            log.exception("снятие зависших захватов намерений упало")


async def _forge_wake_once() -> None:
    """Urgent-темп пробуждения-на-готово: воркер urgent-Forge-задачи завершился ->
    немедленное окно-приглашение (в пределах тика). Normal-завершения сюда НЕ попадают —
    они всплывут в её ближайшем часовом окне (heartbeat.window_context). Приглашение,
    не повинность; идемпотентно через wake_seen (mark_seen только urgent-ключей).

    PASS 30 Этап 1: при живом событийном контуре (PRAXIS_FORGE_EVENTS=1, дефолт) этот
    поллер СПИТ — завершения будят её ходом через core.events (_forge_events_once).
    Выключатель = откат на старый путь без деплоя (тень органа, §0.c-2)."""
    try:
        from core import events as core_events
        if core_events.enabled():
            return
    except Exception:
        pass
    try:
        import forge
        import tasks
        fresh = await asyncio.to_thread(forge.pending_completions, False)
    except Exception:
        return
    urgent = [c for c in fresh if isinstance(c, dict) and c.get("priority") == "urgent"]
    if not urgent:
        return
    try:
        await asyncio.to_thread(forge.mark_seen, [c.get("key") for c in urgent])
        goal = forge.wake_invitation(urgent)[:800]
        await asyncio.to_thread(
            lambda: tasks.add("window", goal, when="in 0m", author="praxis"))
        log.info("forge: urgent-завершение (%d) -> немедленное окно", len(urgent))
    except Exception:
        log.debug("forge urgent wake add failed", exc_info=True)


async def _run_forge_event_pass() -> None:
    """Доставка forge-событий ОДНИМ её ходом (коалесцированно), под _ONE_MIND.

    Порядок (урок скептиков Этапа 1 — не повторить гэп старого пути с двух сторон):
    durable-журнал уже записан продюсером → мозг жив? → bump_attempts (крашеустойчивый
    счёт) → ход → ТОЛЬКО ПОТОМ mark_delivered. Мёртвый мозг = события ждут, не текут;
    краш посреди хода = повторная доставка (макс MAX_DELIVERY_ATTEMPTS, ядовитое
    событие гасится ГРОМКО, не молча). Telethon НЕ закрываем: это не окно."""
    global _FORGE_EVENT_LAST
    from core import events as core_events
    if _ONE_MIND.locked():
        return  # занята живым ходом; события никуда не денутся — следующий тик
    async with _ONE_MIND:
        try:
            if not await asyncio.to_thread(agent.llm.configured):
                return  # мозг недоступен: не потреблять, вернёмся когда оживёт
            events = await asyncio.to_thread(core_events.undelivered, {"subagent_result"})
            if not events:
                return
            _FORGE_EVENT_LAST["ts"] = time.time()

            def _key(e: dict) -> str:
                return str(e.get("dedup_key") or e.get("id") or "")

            counts = await asyncio.to_thread(core_events.bump_attempts,
                                             [_key(e) for e in events])
            poisoned = [e for e in events
                        if counts.get(_key(e), 1) > core_events.MAX_DELIVERY_ATTEMPTS]
            if poisoned:
                log.warning("forge-события гашу после %d неудачных доставок (не молча): %s",
                            core_events.MAX_DELIVERY_ATTEMPTS,
                            ", ".join(_key(e) for e in poisoned))
            import forge
            already_shown = await asyncio.to_thread(forge._wake_load_seen)

            def _quiet(e: dict) -> bool:
                p = e.get("payload") or {}
                # её собственный stop / отказ, отданный синхронно в её же ходе, —
                # расписка уже есть; показанное старым путём за время отката env —
                # не показываем второй раз.
                # ⚠ 23.07: РОЛЕВОГО фильтра здесь БОЛЬШЕ НЕТ. Паритет со старым
                # путём (будить только worker) стоил 15 часов простоя на задаче
                # hcode-e99e7d35: её конвейер — worker→reviewer→worker→reviewer, и
                # именно ревьюер выносит BLOCKING-вердикт. После воркер-события она
                # порождала следующий шаг в ТУ ЖЕ минуту; после ревьюер-завершения
                # (и после упавшего ревьюера!) — тишина по 4-6 часов. План говорит
                # «завершение/падение/таймаут субагента будит», без оговорок по роли.
                return ((p.get("causality") or {}).get("cancelled_by") == "praxis"
                        or bool(p.get("reported_inline"))
                        or f"{p.get('task_id')}:{p.get('agent_id')}" in already_shown)

            quiet = [e for e in events if e not in poisoned and _quiet(e)]
            loud = [e for e in events if e not in poisoned and e not in quiet]
            turn_ok = True
            if loud:
                try:
                    await asyncio.to_thread(agent.forge_event_turn, loud)
                    log.info("forge-события доставлены ходом: %d (тихо: %d)",
                             len(loud), len(quiet))
                except Exception:
                    # Ход упал (расписка held=error уже в turns) — loud НЕ помечаем:
                    # придут снова, попытка учтена bump_attempts, кап гасит громко.
                    turn_ok = False
                    log.warning("forge_event ход упал — события ждут повторной доставки",
                                exc_info=True)
            delivered_now = (loud if turn_ok else []) + quiet + poisoned
            await asyncio.to_thread(core_events.mark_delivered,
                                    [_key(e) for e in delivered_now])
            # мост к старому пути: при выключении контура часовой фолбэк не покажет
            # уже прожитое второй раз. ТОЛЬКО терминальные (базовый ключ без суффикса):
            # overdue-СИГНАЛ не хоронит будущее реальное завершение юнита.
            try:
                def _is_terminal_unit_event(e: dict) -> bool:
                    p = e.get("payload") or {}
                    return (str(e.get("dedup_key") or "")
                            == f"forge:{p.get('task_id')}:{p.get('agent_id')}")

                bridge = []
                for e in delivered_now:
                    p = e.get("payload") or {}
                    if _is_terminal_unit_event(e):
                        bridge.append(f"{p.get('task_id')}:{p.get('agent_id')}")
                    elif str(e.get("dedup_key") or "").endswith(":overdue"):
                        # durable-гвард overdue: журнал забудет ключ на компакте,
                        # wake_seen помнит (старый путь суффиксные ключи не читает)
                        bridge.append(f"{p.get('task_id')}:{p.get('agent_id')}:overdue")
                await asyncio.to_thread(forge.mark_seen, bridge)
            except Exception:
                pass
            await asyncio.to_thread(core_events.compact)
        except Exception:
            log.exception("forge-event доставка упала")


async def _forge_events_once() -> None:
    """PASS 30 Этап 1: события субагентов будят её. Тик дёшев: реконсайлер
    рейт-лимитится сам (60с), пустой журнал отсеивается mtime-гвардом без парсинга;
    ход — в create_task (часы не блокируются), single-flight, зазор — её рычаг."""
    global _FORGE_EVENTS_TASK
    try:
        from core import events as core_events
        if not core_events.enabled():
            return
        import forge
        await asyncio.to_thread(forge.reconcile_subagent_events)
        try:
            mtime = core_events.JOURNAL.stat().st_mtime_ns
        except OSError:
            return  # журнала ещё нет — эмитов не было
        if mtime == _FORGE_EVENT_LAST.get("empty_mtime"):
            return  # с прошлого «пусто» журнал не менялся — не парсим зря
        pending = await asyncio.to_thread(core_events.undelivered, {"subagent_result"}, 1)
        if not pending:
            _FORGE_EVENT_LAST["empty_mtime"] = mtime
            return
    except Exception:
        log.debug("forge_events тик упал", exc_info=True)
        return
    gap = float(perception.value("forge_event_gap_sec"))
    if gap > 0 and time.time() - _FORGE_EVENT_LAST["ts"] < gap:
        return  # коалесценция: события подождут зазор и придут одним ходом
    # Вежливость к живому диалогу: человек уже в дебаунсе/проходе — его ход первее,
    # плод подождёт пару тиков (события durable, не теряются).
    if _passing or any(not t.done() for t in _debounce.values()):
        return
    if _FORGE_EVENTS_TASK is not None and not _FORGE_EVENTS_TASK.done():
        return
    _FORGE_EVENTS_TASK = asyncio.create_task(_run_forge_event_pass())


def _clock_jobs() -> dict:
    """Таблица забот часов: имя -> (период в секундах, корутина). Период <= 0 — выключена."""
    return {
        "control": (max(CLOCK_TICK, 5.0), _control_once),
        "buffers": (CLOCK_TICK, _flush_buffers),
        "schedule": (SCHED_TICK, _fire_due_tasks),
        "sleep": (SLEEP_CHECK_SEC if CONSOLIDATE_HOURS > 0 else 0.0, _consolidate_once),
        "immune": (IMMUNE_MINUTES * 60, _immune_once),  # 9.2: ревью самокоммитов вдогонку
        "formation": (15.0, _formation_once),            # 19: ручной запрос из пульта
        "absence": (ABSENCE_TICK, _absence_once),       # 12.1: ответ простаивающим важным в окно
        "membership": (60.0, _membership_reconcile_once),  # durable owner join/leave
        "direct_outbox": (15.0, _direct_outbox_once),  # tool/task sends; same random_id; no model
        # Strict WAL resume; audited owner control stays authoritative.
        "durable_resume": (45.0, _durable_resume_once),
        "text_outbox": (15.0, _text_outbox_once),      # stable-id replay; exact topic; no model
        "media": (60.0, _media_cleanup_once),           # фото/аудио: retry + TTL, без модели
        "owner_delivery": (15.0, _owner_deliveries_once),  # typed inbox -> Telegram transport
        "followups": (60.0, _followups_once),           # реальные ответы на owner-просьбы
        "social_pulse": (
            social_pulse.interval_hours() * 3600, _social_pulse_once
        ),                                               # PASS 24: раз в час осмотреть нити
        "computer_inventory": (3600.0, _computer_inventory_once),
        "group_context_backfill": (60.0, _group_context_backfill_once),
        "reap_orphans": (300.0, _reap_orphans_once),   # зомби-прогоны без рестарта; без модели
        # Оболочки предложений после рестартов: закрыть беспредметные, вернуть титулы.
        "selfdev_reconcile": (1800.0, _selfdev_reconcile_once),
        "forge_wake": (max(CLOCK_TICK, 30.0), _forge_wake_once),  # urgent Forge-завершения -> немедленное окно
        # PASS 30 Этап 1: завершения субагентов будят её ходом (события, не поллинг)
        "forge_events": (max(CLOCK_TICK, 5.0), _forge_events_once),
    }


def _clock_initial_deadlines(now: float, jobs: dict) -> dict[str, float]:
    """Set explicit restart semantics for each care.

    Only cheap persisted-state catch-up checks are due in the startup pass.
    Everything else retains the historical full-period delay.  The durable social pulse
    is armed at its own persisted boundary (last start + interval): begin() would
    decline an early startup attempt, and the now+period re-arm in _clock_pass would
    then push the window a full period past the original boundary — a restart minutes
    before her hourly window used to eat that window.  Restart still cannot
    manufacture a second wake-up: begin() keeps the CAS on last_started_at.
    """

    deadlines = {
        name: (now if name in _CLOCK_STARTUP_DUE else now + period)
        for name, (period, _care) in jobs.items()
        if period > 0
    }
    if "social_pulse" in deadlines:
        try:
            deadlines["social_pulse"] = social_pulse.next_due_at(now=now)
        except Exception:
            log.exception("часы: social_pulse.next_due_at упал — оставляю startup-due")
    return deadlines


async def _clock_pass(now: float, next_at: dict, jobs: dict) -> list[str]:
    """Один удар часов: выполнить созревшие заботы, перевзвести их сроки. -> имена сработавших.

    Упавшая забота логируется и не мешает остальным; её срок всё равно перевзводится.

    Пульс с заявкой на возврат (`_request_pulse_retry`) обслуживается раньше своего
    срока: окно, отложенное живым ходом, иначе ждало бы ЦЕЛЫЙ ПЕРИОД, хотя её леджер
    считает такое окно всего лишь отложенным."""
    fired = []
    for name, (period, care) in jobs.items():
        if period <= 0:
            continue
        due_at = float(next_at.get(name, 0.0))
        if name == "social_pulse" and _PULSE_RETRY_AT:
            due_at = min(due_at, _PULSE_RETRY_AT)
        if now < due_at:
            continue
        next_at[name] = now + period
        if name == "social_pulse":
            # Попытка делается сейчас; если её снова отложат — заявка встанет заново.
            _clear_pulse_retry()
        fired.append(name)
        try:
            await care()
        except Exception:
            log.exception("часы: забота «%s» упала", name)
    return fired


async def _clock() -> None:
    """Единая периодика вместо _buf_flusher/_scheduler/_consolidator/_heartbeat.

    Сам тик локальный и бесплатный: модель зовётся только внутри созревшей заботы.
    На старте сразу проверяются только durable resume, social-pulse due-state и
    computer-inventory due-state. Остальные заботы получают полный период;
    «сон» не наступает из-за самого рестарта; social pulse сверяется с durable due-state."""
    jobs = _clock_jobs()
    now = time.time()
    next_at = _clock_initial_deadlines(now, jobs)
    await _clock_pass(now, next_at, jobs)
    while True:
        await asyncio.sleep(CLOCK_TICK)
        await _clock_pass(time.time(), next_at, jobs)


async def _wait_reconnected(timeout: float | None = None) -> bool:
    """PASS 13.1: поллит возврат клиента в сеть после намеренного disconnect. -> дождалась ли.

    Work runs never enter this state; they leave Telethon connected."""
    deadline = time.time() + (RECONNECT_TIMEOUT_SEC if timeout is None else timeout)
    while time.time() < deadline:
        if client.is_connected() or not _EXPECT_DISCONNECT.is_set():
            return True
        await asyncio.sleep(0.1)
    return False


async def _supervise_connection() -> None:
    """PASS 13.1: держит процесс живым, пока Telethon не отключится по-настоящему.

    `run_until_disconnected()` returns on every disconnect. Work runs no longer disconnect at all.
    `_SHUTDOWN` is a real exit; `_EXPECT_DISCONNECT` remains only for an explicit transport-recovery
    path. If neither flag is set, the disconnect is a real network/client failure and bootguard
    can restart the process. No work state is encoded in the socket lifecycle.
    """
    while not _SHUTDOWN.is_set():
        await client.run_until_disconnected()
        if _SHUTDOWN.is_set():
            break
        if _EXPECT_DISCONNECT.is_set():
            log.info("клиент отключился намеренно -- жду переподключения")
            if await _wait_reconnected():
                continue
            log.warning("не переподключилась за %.0fс после намеренного disconnect -- считаю сбоем, выхожу",
                        RECONNECT_TIMEOUT_SEC)
            break
        log.warning("клиент отключился без явного намерения -- настоящий выход")
        break


def _bridge_canary() -> None:
    """HOTFIX 07.07 (вечер): проверка sync-моста тулов тем же путём, каким ходят все
    sync-обёртки (воркер-тред -> run_coroutine_threadsafe -> луп main). Мёртвый мост —
    знать при старте одной строкой лога, а не при первом «напиши Евгению» через 2 минуты."""
    try:
        _threadsafe_result(lambda: asyncio.sleep(0), 10)
        log.info("sync-мост тулов к лупу жив")
    except Exception:
        log.error("sync-мост тулов к лупу МЁРТВ — все Telethon-тулы будут таймаутить", exc_info=True)


def _warm_media() -> None:
    """Держать whisper (и локальный piper) резидентно С БУТА, а не лениво с первой голосовой.
    Модели грузятся один раз в общий кэш media_audio backend'а; keep_loaded держит их дальше.
    Строго best-effort: НЕ роняет старт раннера — при сбое модели подхватятся лениво."""
    try:
        import media_audio
        result = media_audio.warm()
        log.info("прогрев медиа-моделей на буте (whisper+piper резидентно): %s", result)
    except Exception:
        log.warning("прогрев медиа-моделей упал (не критично — подхватится лениво)", exc_info=True)


async def _start_shared_stt():
    """Expose the same process-local Whisper object over an authenticated UDS."""

    try:
        import media_audio
        import stt_rpc

        backend = media_audio.get_default_backend().stt
        return await stt_rpc.start_from_env(backend)
    except Exception as exc:
        # The endpoint is optional and must not take the Telegram runtime down.
        # Keep OS paths and any secret-adjacent exception text out of the log.
        log.error("shared STT unavailable error_type=%s", type(exc).__name__)
        return None


async def main() -> None:
    global _LOOP
    _LOOP = asyncio.get_running_loop()  # единственный живой луп — для sync-обёрток тулов
    try:
        import goals as _legacy_goals
        migration = _legacy_goals.decommission_legacy()
        if migration.get("migrated"):
            log.info("legacy moral goals archived: %s", migration["migrated"])
    except Exception:
        # Automatic recall excludes these paths independently, so an archival I/O
        # problem cannot re-author Praxis; keep the evidence and make it visible.
        log.exception("legacy moral goals could not be archived")
    if not API_ID or not API_HASH:
        raise SystemExit("Нет TELEGRAM_API_ID/TELEGRAM_API_HASH в .env")
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Не залогинен. Сначала: python mtproto_login.py send  -> потом ... code <код>")
    _start_dialog_warmup()  # HOTFIX 07.07: кэш имён диалогов греется фоном с самого коннекта
    asyncio.create_task(asyncio.to_thread(_bridge_canary))
    asyncio.create_task(asyncio.to_thread(_warm_media))  # whisper+piper резидентно С БУТА, не лениво
    global _self_id
    me = await client.get_me()
    _self_id = me.id
    _install_telegram_dispatcher()
    agent._TELETHON["get_id"] = _sync_get_id
    agent._TELETHON["resolve_entity"] = _sync_resolve_id  # PASS 12.0.b: единый резолвер постановки=отправки
    agent._TELETHON["search_chats"] = _sync_search_chats
    agent._TELETHON["search_private_messages"] = _sync_search_private_messages
    agent._TELETHON["read_chat"] = _sync_read_chat
    agent._TELETHON["fetch_context"] = _sync_fetch_context
    agent._TELETHON["send_message"] = _sync_send_message
    agent._TELETHON["send_file"] = _sync_send_file
    agent._TELETHON["project_direct_outbox_acceptance"] = (
        _project_direct_outbox_acceptance
    )
    # PASS 24 Telegram surface.  Owner turns and Praxis-self receive it; the sync
    # functions repeat the actor check so trusted humans cannot delegate it.
    agent._TELETHON["join_chat"] = _sync_join_chat
    agent._TELETHON["leave_chat"] = _sync_leave_chat
    agent._TELETHON["followups"] = _sync_followups
    agent._TELETHON["telegram_account"] = _sync_telegram_account
    # Её лицо, слова о себе и жесты — тот же sovereign-гейт, что и остальной аккаунт.
    agent._TELETHON["set_profile_photo"] = _sync_set_avatar
    agent._TELETHON["update_profile"] = _sync_update_profile
    agent._TELETHON["send_reaction"] = _sync_react
    # PASS 30.0.e: честный сенсор транспорта для тулов и окон — синхронный и thread-safe.
    # «Закрыт намеренно (моё окно)» и «отвалился» — разные состояния, не одно молчание.
    agent._TELETHON["transport_state"] = lambda: {
        "connected": bool(client.is_connected()),
        "intentional_window": _EXPECT_DISCONNECT.is_set(),
    }
    # Replay append-only run WAL before accepting new work. Interrupted read-only work becomes
    # paused; unversioned side effects stay in_doubt. Versioned Telegram text plans are retried
    # below only with their persisted route/chunks and stable MTProto random-id keys.
    recovered_runs = await asyncio.to_thread(agent.recover_durable_state)
    if recovered_runs:
        log.warning("восстановлено durable runs: %d", len(recovered_runs))
    # §1: восстановить буферы переписки с диска (restart-proof бэкстоп)
    restored = bufstore.load_all()
    restored_meta = bufstore.meta_load()
    for cid, lines in restored.items():
        _buf[cid].extend(lines[-BUF_MAXLEN:])
        try:
            meta = restored_meta.get(cid) if isinstance(restored_meta.get(cid), dict) else {}
            migration = await asyncio.to_thread(
                memory_life.bootstrap_legacy, cid, lines[-BUF_MAXLEN:], last_ts=meta.get("last_ts"))
            if not migration.get("already"):
                log.info("PASS19 bootstrap [%s]: events=%s legacy_compact=%s",
                         cid, migration.get("events"), migration.get("legacy_compact"))
        except Exception:
            log.exception("PASS19 bootstrap не удался [%s]", cid)
    if restored:
        log.info("восстановлено буферов: %d (%s)", len(restored), ", ".join(list(restored)[:5]))
        for cid in restored:
            asyncio.create_task(_maybe_compact(cid))
    # Reconcile accepted/in-doubt account membership before accepting fresh turns.
    await _membership_reconcile_once()
    # Direct tool and scheduled sends own immutable intents.  Replay them with
    # their original MTProto random ids before accepting fresh model work.
    await _direct_outbox_once()
    # Project delivered media tombstones and retry already-owned transport only
    # after Telegram routes/buffers are hydrated, before any model continuation.
    await _media_cleanup_once()
    # Buffers are in place; replay authored text before accepting fresh turns.
    await _text_outbox_once()
    # Single invariant: the brain only ever runs under _ONE_MIND (except Forge).
    # Uncontended here — the clock/supervisor start below — but held for uniformity.
    async with _ONE_MIND:
        resumed_runs = await asyncio.to_thread(agent.resume_durable_runs, limit=20)
    if resumed_runs:
        log.warning("обработано executable durable resumes: %d", len(resumed_runs))
    shared_stt = await _start_shared_stt()
    try:
        log.info("Praxis на связи как @%s (id %s). мозг: %s; owner=%s комнат=%d "
                 "last_n=%d дебаунс=%.0fs кулдаун dm/grp=%.0f/%.0f",
                 me.username, me.id, llm.state_line() or "не настроен",
                 OWNER_ID or "—", len(rooms.allowed_chats()), LAST_N, DEBOUNCE_SEC, COOLDOWN_DM, COOLDOWN_GROUP)
        _install_dead_room_filter()  # 10.8: banned/private-каналы → mode=dead, лог не спамится
        asyncio.create_task(_clock())  # PASS 4: буферы/расписание/«сон»/сердцебиение — один тик
        asyncio.create_task(_missed_dm_sweep())  # PASS 9.0: догнать ЛС, оборванные рестартом
        await _supervise_connection()
    finally:
        if shared_stt is not None:
            await shared_stt.stop()


if __name__ == "__main__":
    # НЕ client.loop.run_until_complete: с telethon>=1.39 client.loop — динамический
    # get_running_loop(), у которого столько же «лупов», сколько тредов его спрашивало.
    # asyncio.run — честный один луп на весь процесс (см. _main_loop).
    asyncio.run(main())
