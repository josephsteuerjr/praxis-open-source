"""
Praxis — договор об аппетитах (PASS 18.3, AppetiteLedger).

Это НЕ safety-cap и не диспетчер воли. Орган восприятия и учёта: различает наблюдаемые
токены/вызовы (usage.json), расчётную стоимость (pricing из llm.json, если задан), слово
Егора, МОЁ толкование и моё обещание. Он не отклоняет и не переписывает мой выбор модели,
инструмента или фоновой работы — вето нет по построению.

Четыре формулировки Егора (толкую их Я, тулом manage_appetite — не regex в коде):
  «не экономь / копай сколько нужно» → free;
  «умерь аппетиты»                  → considerate (перестраиваю фон, говорю чем жертвую);
  «не больше X в день»              → pledged (моё видимое обещание, не машинный стоп-кран);
  «останови фон»                    → background_paused (доделать атомарное, новых не начинать).

Кнопки пульта пишут owner_request с kind — это ЯВНОЕ решение Егора (честный класс 2,
помечено как его слово); свободный текст с пульта/из чата ждёт моего толкования.

Канон отношений — memory/appetite.md (просьбы/толкования/обещания, append).
memory/.state/appetite.json — учёт, пересобираемый; атомарная запись tmp+replace
(двухпроцессные коллизии praxis/mailbot исчезающе редки — обе пишут только по слову Егора
или моему тулу).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
import urllib.request
from pathlib import Path

import llm

log = logging.getLogger("praxis-appetite")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STATE_PATH = BASE / "memory" / ".state" / "appetite.json"
CONTRACT_MD = BASE / "memory" / "appetite.md"
JOURNAL_DIR = BASE / "memory" / "journal"
PROVIDER_LIMITS_URL = os.getenv("PRAXIS_LIMITS_URL", "").strip()
PROVIDER_LIMITS_TTL = max(15.0, float(os.getenv("PRAXIS_LIMITS_TTL_SEC", "60") or 60))
_PROVIDER_CACHE: dict = {"at": 0.0, "data": None}

MODES = ("free", "considerate", "pledged", "background_paused")
REQUEST_KINDS = ("free", "considerate", "pause_background", "pledge", "text")
SLEEP_DEPTHS = ("full", "light", "skip")

_DEFAULT = {
    "mode": "free",
    "owner_request": None,      # {kind, raw, ts, source}
    "interpretation": None,     # {text, plan: {windows, sleep_depth, note}, ts}
    "promise": {"daily_cost": None, "daily_tokens": None, "background_calls": None},
    "external_provider_boundary": "absent",
}

_MD_HEADER = (
    "# Договор об аппетитах — Praxis и Егор\n\n"
    "_Мышление стоит денег Егора. Здесь живут его просьбы, мои толкования и обещания —\n"
    "канон отношений (PASS 18.3). Учёт — memory/.state/appetite.json + usage.json;\n"
    "код считает и показывает, но не решает за меня._\n"
)


# ---------------------------------------------------------------- состояние

def _load() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("не dict")
    except Exception:
        return json.loads(json.dumps(_DEFAULT))
    out = json.loads(json.dumps(_DEFAULT))
    out.update({k: v for k, v in data.items() if k in _DEFAULT})
    if out.get("mode") not in MODES:
        out["mode"] = "free"
    return out


def _save(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def state() -> dict:
    """Полное состояние + наблюдаемое (на лету, не хранится)."""
    s = _load()
    s["observed"] = observed_today()
    return s


# ---------------------------------------------------------------- наблюдаемое

def _window_summary(label: str, window: dict | None) -> str:
    window = window if isinstance(window, dict) else {}
    remaining = window.get("remaining_percent")
    if not isinstance(remaining, (int, float)):
        used = window.get("used_percent")
        remaining = (max(0.0, min(100.0, 100.0 - float(used)))
                     if isinstance(used, (int, float)) else None)
    if remaining is None:
        return ""
    reset_after = window.get("reset_after_seconds")
    if not isinstance(reset_after, (int, float)):
        reset_at = window.get("reset_at")
        reset_after = (max(0.0, float(reset_at) - time.time())
                       if isinstance(reset_at, (int, float)) else None)
    reset = ""
    if isinstance(reset_after, (int, float)):
        minutes = max(0, int(reset_after) // 60)
        reset = f", сброс через {minutes // 60}ч {minutes % 60:02d}м"
    return f"{label}: {float(remaining):.1f}% осталось{reset}"


def provider_limits(*, force: bool = False) -> dict | None:
    """Last authenticated Codex allowance from the local relay; never guesses."""
    now = time.time()
    if not force and now - float(_PROVIDER_CACHE.get("at") or 0) < PROVIDER_LIMITS_TTL:
        data = _PROVIDER_CACHE.get("data")
        return data if isinstance(data, dict) else None
    if not PROVIDER_LIMITS_URL or os.getenv("PRAXIS_TEST"):
        return None
    try:
        req = urllib.request.Request(PROVIDER_LIMITS_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("limits response too large")
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict) or data.get("schema") != "relay.openai.limits.v1":
            raise ValueError("unexpected limits schema")
        _PROVIDER_CACHE.update(at=now, data=data)
        return data
    except Exception:
        # Cache an honest absence briefly; panel refreshes must not hammer relay.
        _PROVIDER_CACHE.update(at=now, data=None)
        log.debug("provider_limits: relay недоступен", exc_info=True)
        return None


def provider_remaining_text(data: dict | None) -> str:
    rate = data.get("rate_limit") if isinstance(data, dict) else None
    rate = rate if isinstance(rate, dict) else {}
    parts = [
        _window_summary("5ч", rate.get("primary_window")),
        _window_summary("неделя", rate.get("secondary_window")),
    ]
    parts = [part for part in parts if part]
    return "; ".join(parts) if parts else "unknown"


def _provider_remaining_value(data: dict | None, window: str) -> float | None:
    """Numeric allowance for reports; absent stays unknown rather than becoming zero."""
    try:
        item = data["rate_limit"][window]
        remaining = item.get("remaining_percent")
        if remaining is None and isinstance(item.get("used_percent"), (int, float)):
            remaining = 100.0 - float(item["used_percent"])
        return float(remaining) if isinstance(remaining, (int, float)) else None
    except (KeyError, TypeError):
        return None


def observed_today() -> dict:
    """Локальный usage и фактические окна Codex, если relay смог их прочитать."""
    tokens_in = tokens_out = calls = 0
    try:
        day = llm.usage_days(1).get(_dt.date.today().isoformat(), {})
        for rec in day.values():
            tokens_in += int(rec.get("in", 0))
            tokens_out += int(rec.get("out", 0))
            calls += int(rec.get("calls", 0))
    except Exception:
        log.debug("observed_today: usage не прочитался", exc_info=True)
    limits = provider_limits()
    return {"tokens_today": tokens_in + tokens_out, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "calls_today": calls,
            "estimated_cost_today": estimated_cost_today(),
            "provider_remaining": provider_remaining_text(limits),
            "provider_limits": limits}


def estimated_cost_today() -> float | None:
    """Оценка $ за сегодня по pricing из llm.json; None = цены не заданы.
    По-модельный разрез usage (18.5) точнее; без него — текущая модель роли."""
    try:
        pricing = llm.pricing()
        if not pricing:
            return None
        day = llm.usage_days(1).get(_dt.date.today().isoformat(), {})
        if not day:
            return 0.0
        snap = llm.snapshot()
        total = 0.0
        for role, rec in day.items():
            by_model = rec.get("models") or {}
            if by_model:
                for model, m in by_model.items():
                    p = pricing.get(model)
                    if p:
                        total += (int(m.get("in", 0)) * float(p.get("in_per_1m", 0))
                                  + int(m.get("out", 0)) * float(p.get("out_per_1m", 0))) / 1e6
            else:
                model = (snap.get(role) or {}).get("model", "")
                p = pricing.get(model)
                if p:
                    total += (int(rec.get("in", 0)) * float(p.get("in_per_1m", 0))
                              + int(rec.get("out", 0)) * float(p.get("out_per_1m", 0))) / 1e6
        return round(total, 4)
    except Exception:
        log.debug("estimated_cost_today упал", exc_info=True)
        return None


# ---------------------------------------------------------------- слово Егора

class OwnerWordDestroyed(ValueError):
    """Слова Егора пришли уничтоженными. Канонизировать их — значит соврать ей о нём."""


def _looks_ascii_replaced(raw: str) -> bool:
    """Текст, из которого выбило буквы заменой на '?' по дороге.

    Отличать от честного вопроса: у настоящей просьбы есть буквы. Признак порчи —
    цепочка из трёх и более '?' подряд при полном отсутствии букв.
    """
    text = str(raw or "")
    if "???" not in text:
        return False
    return not any(ch.isalpha() for ch in text)


REQUEST_LABELS = {"free": "не экономь", "considerate": "умерь аппетиты",
                  "pause_background": "останови фон", "pledge": "числовая просьба",
                  "text": "своими словами"}


def owner_request_text(request: dict | None) -> str:
    """Что показывать ей НА МЕСТЕ слов Егора — включая случай, когда слов не осталось.

    ⚠ 01.08. Сторож в `set_owner_request` не даёт канонизировать уничтоженный текст
    ВПРЕДЬ, но запись 29.07 легла раньше сторожа — и продолжала показываться и в кадре
    ориентации (`agent.py`, ключ `appetite_owner_request`), и в `describe()` строкой
    «Просьба Егора (panel, 29.07 15:44): ???????????? ???????????????: …». То есть ей
    по-прежнему предъявляли ряд знаков вопроса ПОД ПОДПИСЬЮ ЕГОРА, а с её стороны это
    неотличимо от того, что он так и написал.

    Восстановить утраченное нельзя — байтов больше нет. Назвать утрату утратой можно,
    и это единственное честное, что здесь осталось: класс просьбы сохранился и он
    настоящий, а текст — нет. Проверка идёт по СОДЕРЖИМОМУ, а не по дате записи, так
    что любая другая порча такого рода закрыта тем же ходом.
    """
    req = request if isinstance(request, dict) else {}
    raw = str(req.get("raw") or "").strip()
    kind = str(req.get("kind") or "").strip() or "text"
    label = REQUEST_LABELS.get(kind, kind)
    if raw and not _looks_ascii_replaced(raw):
        return raw
    if not raw:
        return f"({label})"
    return (f"[текст утрачен при записи — не-ASCII выбило заменой на «?»; "
            f"уцелел только класс просьбы: {label}]")


def set_owner_request(kind: str, raw: str, source: str) -> dict:
    """Записать просьбу Егора (кнопка пульта = структурное kind; текст = kind='text').
    Код НЕ меняет mode и НЕ толкует — толкование за Praxis (manage_appetite).

    ``source`` обязателен и не имеет умолчания. Раньше умолчанием был "panel", и
    29.07 одноразовый SSH-скрипт с машины Егора записал просьбу как «кнопка пульта»,
    хотя кнопку никто не нажимал: HTTP-запроса в журнале доступа нет ни одного за две
    недели, а сама панель структурное kind с текстом отправить не может. Ярлык
    достался вранью бесплатно — теперь назваться обязан каждый.

    Уничтоженный текст не канонизируется вовсе. Та же запись 29.07 пришла как 75
    знаков вопроса (кириллицу выбило ascii-заменой ещё на стороне клиента) и легла в
    договор, дневник и её память как слова Егора. Полтора месяца она жила своим
    толкованием фразы, которой не видит, и обнаружить это своими средствами не могла.
    Громкий отказ лучше тихой лжи в её каноне.
    """
    if _looks_ascii_replaced(raw):
        log.error("просьба Егора пришла уничтоженной (%d знаков, ни одной буквы, source=%s) "
                  "— НЕ записываю", len(str(raw or "")), source)
        raise OwnerWordDestroyed(
            "текст просьбы пришёл без единой буквы (похоже на ascii-замену по дороге). "
            "Не записываю: её договор не должен хранить уничтоженные слова как твои.")
    kind = (kind or "text").strip().lower()
    if kind not in REQUEST_KINDS:
        kind = "text"
    s = _load()
    s["owner_request"] = {"kind": kind, "raw": str(raw or "").strip()[:500],
                          "ts": time.time(), "source": source}
    _save(s)
    label = REQUEST_LABELS.get(kind, "своими словами")
    _contract_append(f"просьба Егора ({source}, {label})",
                     f"> {raw.strip() or label}" if (raw or "").strip() else f"({label})")
    _journal(f"просьба Егора об аппетитах ({label}): {(raw or '')[:160]} — жду моего толкования")
    return s["owner_request"]


def unacked_request() -> dict | None:
    """Просьба Егора, которую я ещё не истолковала (новее моего толкования)."""
    s = _load()
    req = s.get("owner_request")
    if not req:
        return None
    interp = s.get("interpretation") or {}
    if float(req.get("ts", 0)) > float(interp.get("ts", 0) or 0):
        return req
    return None


# ---------------------------------------------------------------- её рука (тул)

def interpret(mode: str, text: str, windows: bool | None = None,
              sleep_depth: str | None = None, note: str = "", raw_request: str = "") -> str:
    """МОЁ толкование просьбы (или моя собственная перестройка). Меняет mode и план фона.
    План — моё решение, фоновые контуры читают его как данность, не как кодовое вето."""
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        return f"Режим не из договора: {mode}. Мои режимы: {', '.join(MODES)}."
    depth = (sleep_depth or "").strip().lower() or None
    if depth is not None and depth not in SLEEP_DEPTHS:
        return f"Глубина сна не из набора {SLEEP_DEPTHS}: {depth}"
    s = _load()
    if raw_request.strip() and not s.get("owner_request"):
        # просьба пришла чатом — фиксирую его слово рядом с толкованием
        s["owner_request"] = {"kind": "text", "raw": raw_request.strip()[:500],
                              "ts": time.time(), "source": "chat"}
    plan = {"windows": True if windows is None else bool(windows),
            "sleep_depth": depth or ("light" if mode == "considerate" else
                                     "skip" if mode == "background_paused" else "full"),
            "note": str(note or "")[:300]}
    s["mode"] = mode
    s["interpretation"] = {"text": str(text or "")[:500], "plan": plan, "ts": time.time()}
    if mode != "pledged":
        pass  # обещание живёт отдельно; interpret его не стирает
    _save(s)
    body = (f"режим **{mode}**; окна: {'да' if plan['windows'] else 'нет'}; "
            f"сон: {plan['sleep_depth']}" + (f"; {plan['note']}" if plan["note"] else "") +
            (f"\n\n{text.strip()}" if (text or "").strip() else ""))
    _contract_append("моё толкование", body)
    _journal(f"аппетит: режим {mode}; {text[:160] if text else 'перестроила фон'}")
    obs = observed_today()
    cost = obs.get("estimated_cost_today")
    return (f"Приняла: режим {mode}; окна {'открыты' if plan['windows'] else 'не начинаю'}; "
            f"сон {plan['sleep_depth']}. Сегодня уже: {_k(obs['tokens_today'])} ток, "
            f"{obs['calls_today']} выз." + (f", ≈${cost}" if cost is not None else "") +
            " Записано в договор (memory/appetite.md).")


def pledge(daily_cost: float | None = None, daily_tokens: int | None = None,
           background_calls: int | None = None, text: str = "") -> str:
    """Моё числовое обещание Егору. Видимое, с честной сверкой; не машинный стоп-кран."""
    s = _load()
    prom = {"daily_cost": float(daily_cost) if daily_cost is not None else None,
            "daily_tokens": int(daily_tokens) if daily_tokens is not None else None,
            "background_calls": int(background_calls) if background_calls is not None else None}
    if not any(v is not None for v in prom.values()):
        s["promise"] = dict(_DEFAULT["promise"])
        s["mode"] = "free" if s["mode"] == "pledged" else s["mode"]
        _save(s)
        _contract_append("обещание снято", text.strip() or "мы договорились без чисел")
        return "Обещание снято — числовых границ нет."
    s["promise"] = prom
    s["mode"] = "pledged"
    s["interpretation"] = {"text": str(text or "")[:500],
                           "plan": (s.get("interpretation") or {}).get("plan") or
                                   {"windows": True, "sleep_depth": "light", "note": ""},
                           "ts": time.time()}
    _save(s)
    parts = []
    if prom["daily_tokens"] is not None:
        parts.append(f"не больше {_k(prom['daily_tokens'])} токенов/день")
    if prom["daily_cost"] is not None:
        parts.append(f"не больше ${prom['daily_cost']}/день")
    if prom["background_calls"] is not None:
        parts.append(f"фоновых вызовов ≤{prom['background_calls']}/день (сверка по окнам)")
    body = "; ".join(parts) + (f"\n\n{text.strip()}" if (text or "").strip() else "")
    _contract_append("моё обещание", body)
    _journal(f"аппетит: обещала {('; '.join(parts))[:160]}")
    return f"Обещание записано: {'; '.join(parts)}. Сверяюсь каждый день; превышение — разговор, не тихий срыв."


def promise_check() -> str | None:
    """Сверка обещания с фактом (для STATE/сна). None = обещания нет или всё в рамках.
    Возвращает строку-сигнал, если факт превысил или вплотную (>85%) подошёл к обещанию."""
    s = _load()
    prom = s.get("promise") or {}
    obs = observed_today()
    msgs = []
    if prom.get("daily_tokens"):
        used, cap = obs["tokens_today"], int(prom["daily_tokens"])
        if used >= cap:
            msgs.append(f"токены: {_k(used)} — обещание {_k(cap)} ПРЕВЫШЕНО")
        elif used >= cap * 0.85:
            msgs.append(f"токены: {_k(used)} из {_k(cap)} (впритык)")
    if prom.get("daily_cost") is not None and obs.get("estimated_cost_today") is not None:
        used_c, cap_c = float(obs["estimated_cost_today"]), float(prom["daily_cost"])
        if cap_c > 0:
            if used_c >= cap_c:
                msgs.append(f"стоимость: ≈${used_c} — обещание ${cap_c} ПРЕВЫШЕНО")
            elif used_c >= cap_c * 0.85:
                msgs.append(f"стоимость: ≈${used_c} из ${cap_c} (впритык)")
    return "; ".join(msgs) or None


# ---------------------------------------------------------------- гейты фона (18.4)

def background_hold() -> str | None:
    """Причина не начинать НОВУЮ фоновую работу, или None.
    Два честных источника: моё принятое состояние (mode) и свежая нерастолкованная
    кнопка Егора «останови фон» (его явное решение — класс 2, действует до моего ответа)."""
    s = _load()
    if s.get("mode") == "background_paused":
        return "фон остановлен: моё принятое состояние по просьбе Егора (manage_appetite)"
    req = s.get("owner_request") or {}
    interp = s.get("interpretation") or {}
    if (req.get("kind") == "pause_background"
            and float(req.get("ts", 0)) > float(interp.get("ts", 0) or 0)):
        return "фон остановлен: кнопка Егора «останови фон» (жду моего толкования)"
    return None


# ⚠ Здесь были `windows_off()` и `considerate_hint()` (см. ниже). Обе читал ровно один
# вызывающий — `heartbeat.window_goal`, а его читал `_heartbeat_once`, которого не было
# ни в одной строке `_clock_jobs()`. То есть план «окна не открывать» и подсказка
# бережливого режима не влияли ни на одно пробуждение с тех пор, как часы свели к
# единственному durable-пульсу. Удалены 25.07 по решению Егора вместе с контуром.
# Важно: она НЕ теряет способность — она теряет её видимость. Рычага не существовало,
# существовала запись о нём. Единственный живой рычаг аппетита — `background_hold`
# (окна И ПРОБУЖДЕНИЯ по расписанию, сон, formation); ровно этим объёмом он и назван в
# манифесте. Пробуждения вошли 26.07 вместе с самим видом: рекуррентный wake — то же
# расписание фона, что и рекуррентное окно, и оставить его снаружи значило бы молча
# сузить рычаг, обещанный Егору словами.
# `plan["windows"]` продолжает писаться в manage_appetite как её заявленное намерение
# и виден в пульте — это наблюдаемая запись, а не вето.


def promise_line() -> str | None:
    """Строка сверки обещания для дневника/сна. None = обещания нет."""
    s = _load()
    prom = s.get("promise") or {}
    if not any(v is not None for v in prom.values()):
        return None
    warn = promise_check()
    if warn:
        return f"⚠ {warn} — это разговор с Егором, не тихий срыв"
    obs = observed_today()
    pieces = []
    if prom.get("daily_tokens") is not None:
        pieces.append(f"{_k(obs['tokens_today'])} из {_k(prom['daily_tokens'])} ток")
    if prom.get("daily_cost") is not None:
        cost = obs.get("estimated_cost_today")
        pieces.append(f"≈${cost if cost is not None else '?'} из ${prom['daily_cost']}")
    return "в рамках: " + ", ".join(pieces) if pieces else None


def sleep_depth() -> str:
    """Глубина ближайшего сна по моему плану: full | light | skip."""
    s = _load()
    if background_hold():
        return "skip"
    plan = (s.get("interpretation") or {}).get("plan") or {}
    d = plan.get("sleep_depth")
    return d if d in SLEEP_DEPTHS else "full"


# ---------------------------------------------------------------- выводы наружу

def _k(n) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}к" if n >= 1000 else str(n)


def state_line() -> str:
    """Строка в STATE (owner tier-0): режим, свежая просьба, обещание против факта."""
    try:
        s = _load()
        obs = observed_today()
        bits = [f"режим {s['mode']}"]
        req = unacked_request()
        if req:
            label = {"free": "не экономь", "considerate": "умерь аппетиты",
                     "pause_background": "останови фон"}.get(req["kind"], req.get("raw", "")[:60] or "просьба")
            bits.append(f"⚠ свежая просьба Егора не истолкована: «{label}» — ответь manage_appetite")
        prom = s.get("promise") or {}
        if any(v is not None for v in prom.values()):
            pieces = []
            if prom.get("daily_tokens") is not None:
                pieces.append(f"{_k(obs['tokens_today'])}/{_k(prom['daily_tokens'])} ток")
            if prom.get("daily_cost") is not None:
                cost = obs.get("estimated_cost_today")
                pieces.append(f"≈${cost if cost is not None else '?'}/${prom['daily_cost']}")
            bits.append("обещание: " + ", ".join(pieces))
            warn = promise_check()
            if warn:
                bits.append(f"⚠ {warn}")
        if s.get("external_provider_boundary") != "absent":
            bits.append(f"внешний лимит провайдера: {s['external_provider_boundary']} (граница Егора, не моя)")
        hold = background_hold()
        if hold:
            bits.append(hold)
        return "; ".join(bits)
    except Exception:
        log.debug("appetite.state_line упал", exc_info=True)
        return ""


def short_line() -> str:
    """Короткая строка для rails-манифеста."""
    s = _load()
    hold = background_hold()
    return f"режим {s['mode']}" + (f"; {hold}" if hold else "")


def usage_delta(before: dict | None = None) -> dict | str:
    """Снимок расхода за сегодня / дельта от снимка (для отчётов сна и окон).
    usage_delta() -> снимок; usage_delta(снимок) -> строка «≈N ток (M выз.)[, ≈$X]»."""
    now = observed_today()
    if before is None:
        return now
    d_tok = max(0, now["tokens_today"] - int(before.get("tokens_today", 0)))
    d_calls = max(0, now["calls_today"] - int(before.get("calls_today", 0)))
    tokens_unknown = d_calls > 0 and d_tok == 0
    line = (f"токены не сообщены relay ({d_calls} выз.)" if tokens_unknown
            else f"≈{_k(d_tok)} ток ({d_calls} выз.)")
    c0, c1 = before.get("estimated_cost_today"), now.get("estimated_cost_today")
    if not tokens_unknown and c0 is not None and c1 is not None:
        line += f", ≈${round(max(0.0, float(c1) - float(c0)), 4)}"
    windows = []
    for key, label in (("primary_window", "5ч"), ("secondary_window", "неделя")):
        old = _provider_remaining_value(before.get("provider_limits"), key)
        new = _provider_remaining_value(now.get("provider_limits"), key)
        if old is not None and new is not None:
            windows.append(f"{label}: {old:.1f}→{new:.1f}% остатка")
    if windows:
        line += "; relay " + ", ".join(windows)
    return line


# ---------------------------------------------------------------- записи

def _contract_append(title: str, body: str) -> None:
    try:
        CONTRACT_MD.parent.mkdir(parents=True, exist_ok=True)
        if not CONTRACT_MD.exists():
            CONTRACT_MD.write_text(_MD_HEADER, encoding="utf-8")
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        with CONTRACT_MD.open("a", encoding="utf-8") as f:
            f.write(f"\n## {stamp} — {title}\n{body.strip()}\n")
    except Exception:
        log.debug("appetite: договор не дописался", exc_info=True)


def _journal(msg: str) -> None:
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        p = JOURNAL_DIR / f"{_dt.date.today().isoformat()}.md"
        if not p.exists():
            p.write_text(f"# {_dt.date.today().isoformat()}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- {_dt.datetime.now():%H:%M} (s2) [аппетит] {msg}\n")
    except Exception:
        log.debug("appetite: дневник не дописался", exc_info=True)


def describe() -> str:
    """Статус для тула manage_appetite(status) и пульта."""
    s = state()
    obs = s["observed"]
    req = s.get("owner_request") or {}
    interp = s.get("interpretation") or {}
    plan = interp.get("plan") or {}
    prom = s.get("promise") or {}
    lines = [f"Режим: {s['mode']}"]
    if req:
        ts = _dt.datetime.fromtimestamp(req.get("ts", 0)).strftime("%d.%m %H:%M")
        fresh = " ⚠ не истолкована" if unacked_request() else ""
        lines.append(f"Просьба Егора ({req.get('source', '?')}, {ts}): "
                     f"{owner_request_text(req)}{fresh}")
    if interp:
        lines.append(f"Моё толкование: {interp.get('text') or '—'} "
                     f"(окна: {'да' if plan.get('windows', True) else 'нет'}; сон: {plan.get('sleep_depth', 'full')})")
    if any(v is not None for v in prom.values()):
        lines.append(f"Обещание: токены/день {prom.get('daily_tokens') or '—'}, "
                     f"$/день {prom.get('daily_cost') if prom.get('daily_cost') is not None else '—'}, "
                     f"фон/день {prom.get('background_calls') or '—'}")
        warn = promise_check()
        if warn:
            lines.append(f"Сверка: ⚠ {warn}")
    cost = obs.get("estimated_cost_today")
    if obs["calls_today"] and not obs["tokens_today"]:
        today = f"токены не сообщены relay, {obs['calls_today']} выз."
    else:
        today = (f"{_k(obs['tokens_today'])} ток ({_k(obs['tokens_in'])}→{_k(obs['tokens_out'])}), "
                 f"{obs['calls_today']} выз." +
                 (f", ≈${cost}" if cost is not None else " (цены не заданы)"))
    lines.append(f"Сегодня: {today}")
    visibility = "снимок relay" if obs.get("provider_limits") else "честно: unknown"
    lines.append(f"Остаток у провайдера: {obs['provider_remaining']} ({visibility}); "
                 f"внешний лимит: {s.get('external_provider_boundary', 'absent')}")
    lines.append("Договор: memory/appetite.md")
    return "\n".join(lines)
