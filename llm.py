"""
Praxis — канал к моделям (PASS 8.0). Единственная точка вызова LLM во всём коде.

Два фреймворка (anthropic-протокол / openai-протокол), две ресурсные роли:
  * voice     — живой голос и основной агентный цикл;
  * evaluator — legacy-ключ вспомогательной модели: compact, memory/identity passes,
    privacy authority check и техническое второе мнение. Она не оценивает личность или речь.

Конфиг живёт в memory/llm.json (0600, gitignored, атомарная запись) и правится ТОЛЬКО
панелью (плитка «Мозг») и миграцией из env при первом старте. Горячий подхват: кэш по
mtime — панель (другой контейнер, тот же том) и голос видят правку без рестарта.

Фолбэк: на auth/429/5xx/timeout — один повтор на противоположном фреймворке, если там
есть ключ и у роли задан fallback_model. Событие — WARNING в лог + строка ей в дневник;
следующий успешный вызов на основном сбрасывает флаг. Смена ролей в конфиге — тоже
событие в дневник («мозг сменился») — её честное знание, на чём она думает.

Тестовый шов: _TEST_CLIENTS["anthropic"|"openai"] = фейк-клиент (None = «не настроено»);
сеть в тестах не нужна.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import os
import re
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("praxis-llm")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
REPO = Path(__file__).resolve().parent
MEM_DIR = BASE / "memory"
CONFIG_PATH = MEM_DIR / "llm.json"
JOURNAL_DIR = MEM_DIR / "journal"
USAGE_PATH = MEM_DIR / ".state" / "usage.json"   # PASS 9.1: расход токенов по ролям/дням
USAGE_KEEP_DAYS = 60

FRAMEWORKS = ("anthropic", "openai")
ROLES = ("voice", "evaluator")
_ROLE_RU = {"voice": "голос", "evaluator": "вспомогательная"}

DEFAULT_MAX_TOKENS = {"voice": 1024, "evaluator": 400}

# Ошибки, на которых имеет смысл фолбэк (по имени класса — одинаковы у обоих SDK).
_FALLBACK_ERRORS = {
    "AuthenticationError", "RateLimitError", "APITimeoutError",
    "APIConnectionError", "InternalServerError", "TimeoutError",
}


class EmptyResponseError(RuntimeError):
    """openai/relay вернул не-ответ, а не поднял исключение: пустой SSE-стрим (все чанки
    choices:[], ни текста, ни тула) ИЛИ error-контент с finish_reason='error' (codex-прокси
    так «маскирует» апстрим-4xx/5xx под обычный чанк). Для нас это транспортный сбой — фолбэк
    на другой фреймворк, а не пустая/утёкшая реплика. Найдено живым 06.07: голос на gpt-5.5/
    relay молчал в пустоту (10/10 пустых) или ронял в чат сырое «Error: 400», и фолбэк на glm
    не срабатывал, потому что _call_openai считал это успешным end_turn."""

# Тестовый шов: {"anthropic": фейк, "openai": фейк}. Значение None = «не настроено».
_TEST_CLIENTS: dict[str, object] = {}

_CLIENTS: dict[str, tuple[tuple, object]] = {}   # framework -> ((base_url, key), sdk-клиент)
_CACHE: dict = {"mtime": None, "cfg": None}
_STATE: dict[str, dict] = {r: {"on_fallback": False, "last_error": ""} for r in ROLES}


@dataclass
class LLMResponse:
    """Нормализованный ответ модели (anthropic-форма блоков, независимо от фреймворка)."""
    text: str = ""
    blocks: list = field(default_factory=list)   # [{"type":"text"|"tool_use", ...}]
    stop_reason: str = "end_turn"                # "tool_use" | "end_turn" | "max_tokens" | ...
    usage: dict = field(default_factory=dict)    # {"in": int, "out": int}
    framework: str = ""
    model: str = ""


@dataclass(frozen=True)
class Limits:
    # Bounded specialist loops only.  The main Praxis tool-loop does not consume this.
    max_tool_iters: int = 20


# --------------------------------------------------------------------------- #
#  Конфиг: миграция из env, атомарная запись, горячий подхват по mtime
# --------------------------------------------------------------------------- #

def _from_env() -> dict:
    """Собрать конфиг из env (миграция первого старта; .env остаётся бутстрапом транспорта)."""
    try:
        iters = max(1, min(300, int(os.getenv("PRAXIS_MAX_TOOL_ITERS", "20") or 20)))
    except ValueError:
        iters = 20
    return {
        "frameworks": {
            "anthropic": {"base_url": os.getenv("GLM_BASE_URL", "https://api.z.ai/api/anthropic"),
                          "api_key": os.getenv("GLM_API_KEY", "")},
            "openai": {"base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                       "api_key": os.getenv("OPENAI_API_KEY", "")},
        },
        "roles": {
            "voice": {"framework": "anthropic",
                      "model": os.getenv("GLM_VOICE_MODEL") or os.getenv("GLM_MODEL") or "glm-5.2",
                      "max_tokens": DEFAULT_MAX_TOKENS["voice"], "fallback_model": ""},
            "evaluator": {"framework": "anthropic",
                          "model": os.getenv("GLM_FIRSTPASS_MODEL")
                                   or os.getenv("GLM_GATEKEEPER_MODEL") or "glm-4.7",
                          "max_tokens": DEFAULT_MAX_TOKENS["evaluator"], "fallback_model": ""},
        },
        "limits": {"max_tool_iters": iters},
    }


def _normalize(cfg: dict) -> dict:
    """Дозаполнить пропуски дефолтами — конфиг руками/панелью не обязан быть полным."""
    base = _from_env()
    out = {"frameworks": {}, "roles": {}, "limits": dict(base["limits"])}
    for fw in FRAMEWORKS:
        cur = (cfg.get("frameworks") or {}).get(fw) or {}
        out["frameworks"][fw] = {"base_url": str(cur.get("base_url") or base["frameworks"][fw]["base_url"]),
                                 "api_key": str(cur.get("api_key") or "")}
    for role in ROLES:
        cur = (cfg.get("roles") or {}).get(role) or {}
        d = base["roles"][role]
        fw = cur.get("framework") if cur.get("framework") in FRAMEWORKS else d["framework"]
        try:
            mt = int(cur.get("max_tokens") or d["max_tokens"])
        except (TypeError, ValueError):
            mt = d["max_tokens"]
        out["roles"][role] = {"framework": fw, "model": str(cur.get("model") or d["model"]),
                              "max_tokens": mt, "fallback_model": str(cur.get("fallback_model") or "")}
    lim = cfg.get("limits") or {}
    try:
        out["limits"]["max_tool_iters"] = max(1, min(600, int(lim.get("max_tool_iters"))))  # 07.07: потолок 300->600, владелец хочет управлять сам
    except (TypeError, ValueError):
        pass
    # PASS 24 intentionally drops old evaluator/drift/window policy keys while normalizing.
    # They are neither hidden defaults nor compatibility vetoes.
    # PASS 9.1: опциональный прайс {"<model>": {"in_per_1m": X, "out_per_1m": Y}} — руками
    # в llm.json; без него панель честно показывает только токены. Пропускаем как есть,
    # чтобы запись конфига панелью не стирала блок.
    if isinstance(cfg.get("pricing"), dict):
        out["pricing"] = cfg["pricing"]
    # PASS 9.2: блок иммунитета (second_opinion — задел, дефолт false) — тоже пропускаем
    if isinstance(cfg.get("immune"), dict):
        out["immune"] = cfg["immune"]
    return out


def save_config(cfg: dict) -> None:
    """Атомарная запись конфига (tmp+replace), права 0600. Единственный писатель — панель/миграция."""
    cfg = _normalize(cfg)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:  # Windows/экзотика — не рельс, ключи и так вне git
        pass


def _journal(msg: str) -> None:
    """Строка ей в дневник (как selfdev): события канала — часть её честной непрерывности."""
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        p = JOURNAL_DIR / f"{_dt.date.today().isoformat()}.md"
        if not p.exists():
            p.write_text(f"# {_dt.date.today().isoformat()}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"- {_dt.datetime.now():%H:%M} (s2) [канал] {msg}\n")
    except Exception:
        log.debug("journal llm не удался", exc_info=True)


def _diff_roles(old: dict, new: dict) -> list[str]:
    out = []
    for role in ROLES:
        o, n = (old.get("roles") or {}).get(role) or {}, (new.get("roles") or {}).get(role) or {}
        if (o.get("model"), o.get("framework")) != (n.get("model"), n.get("framework")):
            out.append(f"{_ROLE_RU[role]} {o.get('model')} → {n.get('model')} ({n.get('framework')})")
    return out


def _config() -> dict:
    """Живой конфиг: перечитывается при смене mtime; нет файла — миграция из env (и запись)."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime_ns
    except OSError:
        mtime = None
    if mtime is None:
        if _CACHE["cfg"] is None or _CACHE["mtime"] is not None:
            _CACHE.update(mtime=None, cfg=_from_env())
            try:
                save_config(_CACHE["cfg"])
                _CACHE["mtime"] = CONFIG_PATH.stat().st_mtime_ns
                log.info("llm: конфиг мигрирован из env → %s", CONFIG_PATH)
            except OSError:
                log.warning("llm: конфиг из env не записался (работаю из памяти)", exc_info=True)
        return _CACHE["cfg"]
    if mtime == _CACHE["mtime"] and _CACHE["cfg"] is not None:
        return _CACHE["cfg"]
    try:
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        fresh = _normalize(stored)
    except Exception:
        log.warning("llm: llm.json не разобрался — работаю на последнем хорошем", exc_info=True)
        if _CACHE["cfg"] is None:
            _CACHE.update(mtime=mtime, cfg=_from_env())
        else:
            _CACHE["mtime"] = mtime
        return _CACHE["cfg"]
    if stored != fresh:
        # Normalisation is also the one-way migration boundary.  Retired
        # evaluator/drift/window policy must not linger in the durable file and
        # surprise a later process or operator merely because it is inert today.
        try:
            save_config(fresh)
            mtime = CONFIG_PATH.stat().st_mtime_ns
            log.info("llm: конфиг нормализован и устаревшие policy-поля удалены")
        except OSError:
            log.warning("llm: нормализованный конфиг не записался", exc_info=True)
    old = _CACHE["cfg"]
    if old is not None:
        for line in _diff_roles(old, fresh):
            _journal(f"мозг сменился: {line}")
            log.warning("llm: %s", line)
    _CACHE.update(mtime=mtime, cfg=fresh)
    return fresh


def web_search_backend() -> str | None:
    """Вернуть hosted-search backend, который реально умеет принять тул.

    z.ai использует Anthropic-shaped ``web_search_20250305``. Codex relay принимает
    Responses ``web_search`` через свой Chat-Completions compatibility edge, но это
    явная способность нашего деплоя: произвольному OpenAI-compatible URL такой тул
    посылать нельзя.
    """
    if os.getenv("PRAXIS_WEB_SEARCH", "0").lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        framework = _config()["roles"]["voice"]["framework"]
    except Exception:
        return None
    if framework == "anthropic":
        return "anthropic"
    relay_native = os.getenv("PRAXIS_OPENAI_NATIVE_WEB_SEARCH", "0").lower()
    if framework == "openai" and relay_native in ("1", "true", "yes", "on"):
        return "openai"
    return None


def web_search_available() -> bool:
    """Единый источник правды для реального вызова и панели способностей."""
    return web_search_backend() is not None


def role_max_tokens(role: str) -> int:
    """Потолок токенов роли из конфига (для вызовов, которым нужен «размер голоса»)."""
    rc = (_config().get("roles") or {}).get(role) or {}
    try:
        return int(rc.get("max_tokens") or DEFAULT_MAX_TOKENS.get(role, 1024))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS.get(role, 1024)


def limits() -> Limits:
    lim = _config().get("limits") or {}
    return Limits(max_tool_iters=int(lim.get("max_tool_iters", 20)))


def update_config(changes: dict) -> dict:
    """Слить изменения в конфиг (deep-merge по frameworks/roles/limits) и сохранить. -> новый конфиг.

    Путь ПИСАТЕЛЯ (панель/миграция/тесты): кэш обновляется напрямую, не через mtime —
    гранулярность файловых времён (Windows-тик ~15мс) не должна давать читать старое.
    Событие «мозг сменился» журналит ЧИТАТЕЛЬ (другой процесс) при mtime-перечитывании."""
    cfg = json.loads(json.dumps(_config()))  # глубокая копия
    for sect in ("frameworks", "roles"):
        for k, v in (changes.get(sect) or {}).items():
            if isinstance(v, dict):
                cfg.setdefault(sect, {}).setdefault(k, {}).update(v)
    if isinstance(changes.get("limits"), dict):
        cfg.setdefault("limits", {}).update(changes["limits"])
    save_config(cfg)
    fresh = _normalize(cfg)
    try:
        _CACHE.update(mtime=CONFIG_PATH.stat().st_mtime_ns, cfg=fresh)
    except OSError:
        _CACHE.update(cfg=fresh)
    return fresh


def swap_fallback(role: str) -> dict:
    """06.07: поменять местами основную и запасную модели роли одним действием.

    chat() всегда считает fallback_model моделью ПРОТИВОПОЛОЖНОГО фреймворка от текущего —
    ручной свич framework в панели не переставлял model/fallback_model заодно, и после свича
    фолбэк бил в чужой протокол. Свап делает framework/model/fallback_model согласованными."""
    if role not in ROLES:
        raise ValueError(f"llm: неизвестная роль {role!r}")
    cfg = json.loads(json.dumps(_config()))
    rc = cfg["roles"][role]
    fb = (rc.get("fallback_model") or "").strip()
    if not fb:
        raise ValueError("нечего свапать — сначала задай fallback_model")
    other = "openai" if rc["framework"] == "anthropic" else "anthropic"
    rc["framework"], rc["model"], rc["fallback_model"] = other, fb, rc["model"]
    save_config(cfg)
    fresh = _normalize(cfg)
    try:
        _CACHE.update(mtime=CONFIG_PATH.stat().st_mtime_ns, cfg=fresh)
    except OSError:
        _CACHE.update(cfg=fresh)
    _journal(f"{_ROLE_RU[role]}: своп основной/запасной ({other}/{fb})")
    return fresh


# --------------------------------------------------------------------------- #
#  Клиенты SDK (кэш на фреймворк; пересоздаются при смене base_url/ключа)
# --------------------------------------------------------------------------- #

def _client_for(framework: str):
    """SDK-клиент фреймворка (или тестовый фейк). None — не настроен."""
    if framework in _TEST_CLIENTS:
        return _TEST_CLIENTS[framework]
    if os.environ.get("PRAXIS_TEST"):
        return None  # герметичность: в тестах сеть только через фейки (use_test_client)
    fw = (_config().get("frameworks") or {}).get(framework) or {}
    key, base_url = fw.get("api_key") or "", fw.get("base_url") or ""
    if not key:
        return None
    fp = (base_url, key)
    cached = _CLIENTS.get(framework)
    if cached and cached[0] == fp:
        return cached[1]
    if framework == "anthropic":
        from anthropic import Anthropic
        cli = Anthropic(api_key=key, base_url=base_url)
    else:
        from openai import OpenAI
        cli = OpenAI(api_key=key, base_url=base_url)
    _CLIENTS[framework] = (fp, cli)
    return cli


def configured(role: str = "voice") -> bool:
    """Есть ли живой канал для роли (ключ у её фреймворка / тестовый фейк)."""
    try:
        rc = (_config().get("roles") or {}).get(role) or {}
        return _client_for(rc.get("framework") or "anthropic") is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  Трансляция anthropic-формы <-> openai-формы
# --------------------------------------------------------------------------- #

def system_text(system) -> str:
    """system-блоки (или строка) -> одна строка (cache_control отбрасывается)."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(str(b.get("text", "")) for b in system if isinstance(b, dict))
    return str(system or "")


def _openai_strict_schema(schema: dict | None) -> dict:
    """OpenAI strict-mode (в т.ч. codex-relay) требует на КАЖДОЙ object-схеме: additionalProperties:
    false (ловили на recall, tools[0]) И required, перечисляющий ВСЕ ключи properties (ловили на
    remember, tools[1] — open_loop и другие опциональные поля не входили в required). Наши
    input_schema писаны под anthropic (там ничего из этого не нужно, required — только по-настоящему
    обязательные) — транслируем сюда, не трогая исходник. Поле, не бывшее required у anthropic,
    оборачивается в {anyOf: [<исходная схема>, {type: null}]} — модель явно шлёт null вместо
    пропуска ключа, сохраняя опциональность по смыслу; agent._voice отфильтровывает None перед
    вызовом тула, так что для самой реализации тула ничего не меняется. Рекурсивно — на случай
    вложенных object/array-схем."""
    s = dict(schema) if isinstance(schema, dict) else {"type": "object", "properties": {}}
    if s.get("type") == "object":
        props = s.get("properties") if isinstance(s.get("properties"), dict) else {}
        orig_required = set(s.get("required") or [])
        new_props = {}
        for k, v in props.items():
            fixed = _openai_strict_schema(v) if isinstance(v, dict) else v
            if k not in orig_required and isinstance(fixed, dict):
                fixed = {"anyOf": [fixed, {"type": "null"}]}
            new_props[k] = fixed
        s = dict(s, properties=new_props, required=list(new_props.keys()),
                **{"additionalProperties": False})
    items = s.get("items")
    if isinstance(items, dict):
        s = dict(s, items=_openai_strict_schema(items))
    return s


def tools_to_openai(tools: list | None) -> list | None:
    """Перевести функции и hosted web-search конкретного Codex relay."""
    if not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "web_search":
            item = {"type": "web_search"}
            context_size = t.get("search_context_size")
            if context_size is not None:
                if context_size not in ("low", "medium", "high"):
                    raise ValueError("web_search.search_context_size must be low, medium, or high")
                item["search_context_size"] = context_size
            external = t.get("external_web_access")
            if external is not None:
                if not isinstance(external, bool):
                    raise ValueError("web_search.external_web_access must be boolean")
                item["external_web_access"] = external
            max_uses = t.get("max_uses")
            if max_uses is not None:
                if isinstance(max_uses, bool) or not isinstance(max_uses, int) or not 1 <= max_uses <= 10:
                    raise ValueError("web_search.max_uses must be an integer from 1 to 10")
                item["max_uses"] = max_uses
            out.append(item)
            continue
        if "input_schema" not in t:
            continue  # provider-specific server tool (например z.ai search)
        out.append({"type": "function", "function": {
            "name": t.get("name", ""), "description": t.get("description", ""),
            "parameters": _openai_strict_schema(t.get("input_schema"))}})
    return out or None


def tools_to_anthropic(tools: list | None) -> list | None:
    """Оставить Anthropic/function tools, отфильтровав relay-only hosted search.

    Последний tool в массиве получает cache_control: ephemeral, чтобы весь блок
    инструментных схем попал во второй cache breakpoint (первый — system-промпт).
    Это ~20-25k токенов, которые иначе платятся каждый ход заново.
    PRAXIS_PROMPT_CACHE=0 отключает (совместимо с _system в agent.py).
    """
    if not tools:
        return None
    out = [t for t in tools
           if isinstance(t, dict) and t.get("type") != "web_search"]
    if not out:
        return None
    if os.getenv("PRAXIS_PROMPT_CACHE", "1").lower() not in ("0", "false", "no"):
        last = dict(out[-1])
        last["cache_control"] = {"type": "ephemeral"}
        out[-1] = last
    return out


_VISION_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_VISION_MAX_BYTES = int(float(os.getenv("PRAXIS_IMAGE_LLM_MB", "20")) * 1024 * 1024)


def _image_payload(block: dict) -> tuple[str, str]:
    """Канонический image-блок -> (mime, base64), единый для обоих адаптеров.

    Живой раннер передаёт проверенный MediaRef как {type:image,path,mime}. Форму source
    принимаем для совместимости с уже готовыми Anthropic-блоками и тестовыми вызовами.
    """
    source = block.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        mime = str(source.get("media_type") or block.get("mime") or "").lower()
        data = str(source.get("data") or "")
        if mime not in _VISION_MIME or not data:
            raise ValueError("неподдерживаемый или пустой image-блок")
        if len(data) > ((_VISION_MAX_BYTES + 2) // 3) * 4 + 8:
            raise ValueError("изображение превышает локальный лимит")
        return mime, data
    path = Path(str(block.get("path") or ""))
    mime = str(block.get("mime") or block.get("media_type") or "").lower()
    if mime not in _VISION_MIME:
        raise ValueError(f"формат изображения {mime or '?'} не поддерживается моделью")
    try:
        size = path.stat().st_size
    except OSError as e:
        raise ValueError("файл изображения недоступен") from e
    if size <= 0 or size > _VISION_MAX_BYTES:
        raise ValueError("изображение пустое или превышает локальный лимит")
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


def messages_to_anthropic(messages: list) -> list:
    """Наша история -> Anthropic; локальные image/path превращаются в base64 source."""
    out: list[dict] = []
    for m in messages or []:
        content = m.get("content", "")
        if not isinstance(content, list):
            out.append(dict(m))
            continue
        blocks = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image":
                mime, data = _image_payload(b)
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": data}})
            else:
                blocks.append(b)
        out.append(dict(m, content=blocks))
    return out


def messages_to_openai(messages: list) -> list:
    """Наша история (anthropic-форма, включая tool_use/tool_result) -> openai-сообщения."""
    out: list[dict] = []
    for m in messages or []:
        role, content = m.get("role", "user"), m.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            texts, calls = [], []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(str(b.get("text", "")))
                elif b.get("type") == "tool_use":
                    calls.append({"id": str(b.get("id", "")), "type": "function",
                                  "function": {"name": str(b.get("name", "")),
                                               "arguments": json.dumps(b.get("input") or {},
                                                                       ensure_ascii=False)}})
            msg: dict = {"role": "assistant", "content": "\n".join(t for t in texts if t) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        # user: tool_result-блоки становятся role=tool; image-блоки — Chat Completions
        # image_url с data URL. Текст без картинки оставляем строкой для совместимости.
        texts, multimodal = [], []
        has_image = False
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                out.append({"role": "tool", "tool_call_id": str(b.get("tool_use_id", "")),
                            "content": str(b.get("content", ""))})
            elif b.get("type") == "text":
                text = str(b.get("text", ""))
                texts.append(text)
                multimodal.append({"type": "text", "text": text})
            elif b.get("type") == "image":
                mime, data = _image_payload(b)
                image_url: dict = {"url": f"data:{mime};base64,{data}"}
                detail = str(b.get("detail") or "").lower()
                if detail in ("low", "high", "original", "auto"):
                    image_url["detail"] = detail
                multimodal.append({"type": "image_url", "image_url": image_url})
                has_image = True
        if has_image:
            out.append({"role": role, "content": multimodal})
        elif texts:
            out.append({"role": role, "content": "\n".join(texts)})
    return out


def blocks_from_openai(msg) -> list:
    """openai message -> блоки anthropic-формы (text + tool_use, JSON-аргументы распарсены)."""
    blocks: list[dict] = []
    text = getattr(msg, "content", None)
    if text:
        blocks.append({"type": "text", "text": str(text)})
    for tc in getattr(msg, "tool_calls", None) or []:
        fn = getattr(tc, "function", None)
        raw = getattr(fn, "arguments", "") or ""
        try:
            args = json.loads(raw) if raw.strip() else {}
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}
        blocks.append({"type": "tool_use", "id": str(getattr(tc, "id", "")),
                       "name": str(getattr(fn, "name", "")), "input": args})
    return blocks


# PASS: z.ai's web_search_20250305 emulation does NOT behave like real Anthropic's server tool
# (a single clean final text block). It returns the whole exchange inline as one response with
# content = [text(narrates the call, e.g. "🔍 Z.ai Built-in Tool... Executing on server..."),
# server_tool_use, text(dumps the RAW search-result JSON as "Output: ..."), tool_result,
# text(the actual clean synthesized answer)] — three "text"-typed blocks, two of them scaffolding.
# Naively joining every text block (as real Anthropic responses safely allow) leaked raw JSON into
# replies (observed live 05.07, see soul/skills/web_search_reader.md). Any block type besides
# text/tool_use means "opaque server-side work happened here" — text at or before the LAST such
# block is narration/result-dump, never her voice; only text strictly after it is genuine reply.
# When no such block is present (the overwhelming common case, incl. every existing client tool_use
# turn), this is a no-op and every text block is kept exactly as before.
_KNOWN_BLOCK_TYPES = ("text", "tool_use")


def _blocks_from_anthropic(resp) -> list:
    raw = getattr(resp, "content", None) or []
    last_opaque = -1
    for i, b in enumerate(raw):
        if getattr(b, "type", None) not in _KNOWN_BLOCK_TYPES:
            last_opaque = i
    blocks = []
    for i, b in enumerate(raw):
        t = getattr(b, "type", None)
        if t == "text":
            if i <= last_opaque:
                continue  # server-tool narration/raw-result scaffolding — not her voice
            blocks.append({"type": "text", "text": b.text})
        elif t == "tool_use":
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return blocks


def text_of(resp) -> str:
    """Текст ответа: понимает LLMResponse и сырые ответы anthropic-SDK (легаси-вызовы)."""
    if isinstance(resp, LLMResponse):
        return resp.text
    return "".join(b["text"] for b in _blocks_from_anthropic(resp)
                   if b["type"] == "text").strip()


# --------------------------------------------------------------------------- #
#  PASS 9.1: cost-meter — расход по ролям, копится в usage.json (STATE + панель «Расход»)
# --------------------------------------------------------------------------- #

def _usage_load() -> dict:
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}  # битый/нет — честный старт с нуля (это счётчик, не память)


def _usage_add(role: str, usage: dict, fallback: bool = False, model: str = "") -> None:
    """Прибавить успешный вызов. Ошибки записи НЕ роняют вызов модели — только debug-лог.

    PASS 18.5: model — по-модельный подразрез дня (день→роль→models→{in,out,calls}):
    панель ценит расход по ФАКТИЧЕСКОЙ модели, а не по текущей задним числом. Старые
    записи без models живы — формат обратносовместим.
    Известный допуск: read-modify-write из двух процессов (praxis и mailbot) без лока —
    редкие потерянные инкременты терпим, счётчик наблюдательный."""
    try:
        data = _usage_load()
        day = _dt.date.today().isoformat()
        cutoff = (_dt.date.today() - _dt.timedelta(days=USAGE_KEEP_DAYS)).isoformat()
        for k in [k for k in data if isinstance(k, str) and k < cutoff]:
            del data[k]  # ротация: 60 дней достаточно для панели и трендов
        d = data.setdefault(day, {}).setdefault(role, {"in": 0, "out": 0, "calls": 0, "fallback": 0})
        u_in = int((usage or {}).get("in", 0) or 0)
        u_out = int((usage or {}).get("out", 0) or 0)
        d["in"] = int(d.get("in", 0)) + u_in
        d["out"] = int(d.get("out", 0)) + u_out
        d["calls"] = int(d.get("calls", 0)) + 1
        d["fallback"] = int(d.get("fallback", 0)) + (1 if fallback else 0)
        u_cr = int((usage or {}).get("cache_read", 0) or 0)
        u_cc = int((usage or {}).get("cache_creation", 0) or 0)
        if u_cr or u_cc:
            d["cache_read"] = int(d.get("cache_read", 0)) + u_cr
            d["cache_creation"] = int(d.get("cache_creation", 0)) + u_cc
        if model:
            m = d.setdefault("models", {}).setdefault(str(model), {"in": 0, "out": 0, "calls": 0})
            m["in"] += u_in
            m["out"] += u_out
            m["calls"] += 1
            if u_cr or u_cc:
                m["cache_read"] = m.get("cache_read", 0) + u_cr
                m["cache_creation"] = m.get("cache_creation", 0) + u_cc
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(USAGE_PATH)
    except Exception:
        log.debug("llm: usage не записался", exc_info=True)


def usage_days(n: int = 7) -> dict:
    """Расход за последние n дней: {день: {роль: {in,out,calls,fallback}}} (для панели)."""
    data = _usage_load()
    cutoff = (_dt.date.today() - _dt.timedelta(days=max(0, n - 1))).isoformat()
    return {day: v for day, v in sorted(data.items()) if isinstance(day, str) and day >= cutoff}


def usage_line() -> str:
    """Одна строка для STATE: расход основного и вспомогательного каналов за сегодня."""
    today = _usage_load().get(_dt.date.today().isoformat()) or {}
    if not today:
        return ""

    def _k(v: int) -> str:
        return f"{v / 1000:.1f}к" if v >= 1000 else str(v)

    parts = []
    for role in ROLES:
        d = today.get(role)
        if not isinstance(d, dict) or not d.get("calls"):
            continue
        fb = f", фолбэков {d['fallback']}" if d.get("fallback") else ""
        # 02.08: не сырая пара r/c, а величина, которая действительно означает расход.
        # Кэшированный префикс провайдер уже держит; платит она за СВЕЖИЙ вход —
        # `in − cache_read`. На живом прогоне это 42к на ход при 85% кэша ≈ 6.3к свежего.
        # До этой ночи `cache_read` через gpt не приходил вовсе, и строка молчала —
        # молчание читалось как «кэша нет», хотя означало «нам не сказали».
        cr = int(d.get("cache_read", 0))
        cc = int(d.get("cache_creation", 0))
        u_in = int(d.get("in", 0))
        if cr or cc:
            share = f", кэш {100 * cr / u_in:.0f}%" if u_in else ""
            fresh = f", свежего входа {_k(max(0, u_in - cr))}" if u_in else ""
            cache = f"{share}{fresh}"
            if cc:
                cache += f", записи в кэш {_k(cc)}"
        else:
            cache = ""
        parts.append(f"{_ROLE_RU[role]} {_k(int(d.get('in', 0)))}→{_k(int(d.get('out', 0)))} ток "
                     f"({d['calls']} выз.{fb}{cache})")
    return ", ".join(parts)


def pricing() -> dict:
    """Прайс из llm.json (опциональный, руками). Нет цен — панель показывает только токены."""
    p = _config().get("pricing")
    return p if isinstance(p, dict) else {}


# --------------------------------------------------------------------------- #
#  Вызовы фреймворков
# --------------------------------------------------------------------------- #

_OPENAI_COMPLETION_TOKENS_RE = re.compile(r"^(o\d|gpt-5)")


def _call_anthropic(cli, model: str, *, system, messages, tools, max_tokens, thinking) -> LLMResponse:
    kw: dict = {"model": model, "max_tokens": max_tokens,
                "messages": messages_to_anthropic(messages)}
    if system:
        kw["system"] = system
    anthropic_tools = tools_to_anthropic(tools)
    if anthropic_tools:
        kw["tools"] = anthropic_tools
    if thinking:
        kw["thinking"] = {"type": "enabled", "budget_tokens": int(thinking)}
        kw["max_tokens"] = max(int(max_tokens), int(thinking) + 1024)
    # PASS 10.0: всегда стримом — SDK кидает «Streaming is required for operations that
    # may take longer than 10 minutes» на больших max_tokens (ночные окна умирали, кап
    # сгорал впустую). get_final_message() возвращает тот же Message (blocks/usage/
    # stop_reason на месте). Фейки без .stream (легаси-тесты) идут через create.
    streamer = getattr(getattr(cli, "messages", None), "stream", None)
    if callable(streamer):
        with streamer(**kw) as st:
            resp = st.get_final_message()
    else:
        resp = cli.messages.create(**kw)
    usage = getattr(resp, "usage", None)
    _usage = {"in": int(getattr(usage, "input_tokens", 0) or 0),
              "out": int(getattr(usage, "output_tokens", 0) or 0)}
    # Anthropic cache metrics — видимость hit-rate и реальной экономии
    _cr = getattr(usage, "cache_read_input_tokens", None)
    _cc = getattr(usage, "cache_creation_input_tokens", None)
    if _cr:
        _usage["cache_read"] = int(_cr)
    if _cc:
        _usage["cache_creation"] = int(_cc)
    return LLMResponse(
        text=text_of(resp), blocks=_blocks_from_anthropic(resp),
        stop_reason=str(getattr(resp, "stop_reason", None) or "end_turn"),
        usage=_usage,
        framework="anthropic", model=model)


_OPENAI_STOP = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}


def _openai_reasoning_effort(thinking) -> str | None:
    """Бюджет thinking (токены, как у anthropic) -> ступень reasoning_effort релея.

    Релей по умолчанию шлёт effort=none (скорость), но когда код ЯВНО просит
    подумать (консолидация, ночные проходы) — глубина должна вернуться, иначе
    thinking на openai-пути так и остался бы молча съеденным."""
    if not thinking:
        return None
    budget = int(thinking)
    if budget <= 2048:
        return "low"
    if budget <= 8192:
        return "medium"
    return "high"


def _call_openai(cli, model: str, *, system, messages, tools, max_tokens, thinking) -> LLMResponse:
    msgs = messages_to_openai(messages)
    sys_text = system_text(system)
    if sys_text:
        msgs = [{"role": "system", "content": sys_text}] + msgs
    # PASS: локальный relay (codex/ChatGPT-прокси на host.docker.internal:5011) ВСЕГДА отдаёт SSE —
    # не-стриминговый create() возвращал пусто. Просим stream и агрегируем; реальный OpenAI тоже
    # умеет stream, так что путь общий. Фейки/не-стриминговые сервера отдают единый объект (ветка ниже).
    kw: dict = {"model": model, "messages": msgs, "stream": True,
                "stream_options": {"include_usage": True}}
    effort = _openai_reasoning_effort(thinking)
    if effort:
        # неизвестное SDK поле — только через extra_body; релей примет reasoning_effort
        # per-request поверх своего дефолта (none), чужой сервер молча проигнорирует
        kw["extra_body"] = {"reasoning_effort": effort}
    if _OPENAI_COMPLETION_TOKENS_RE.match(model or ""):
        kw["max_completion_tokens"] = max_tokens   # reasoning-модели не берут max_tokens
    else:
        kw["max_tokens"] = max_tokens
    ot = tools_to_openai(tools)
    if ot:
        kw["tools"] = ot
    resp = cli.chat.completions.create(**kw)
    # единый объект с .choices[0].message — не-стрим (фейк-клиент/обычный сервер): прежний разбор
    choices = getattr(resp, "choices", None)
    if choices and getattr(choices[0], "message", None) is not None:
        return _openai_from_completion(resp, model)
    out = _openai_from_stream(resp, model)  # итератор чанков (relay/реальный OpenAI stream)
    # Здоровье канала: релей на сбое апстрима отдаёт либо error-контент (finish_reason='error'),
    # либо совсем пустой стрим (ни текста, ни тула). И то, и другое — не ответ; поднимаем
    # fallbackable-сбой, чтобы chat() ушёл на glm, а не вернул пустоту/строку "Error: 400".
    if out.stop_reason == "error" or (not out.blocks and not out.text.strip()):
        raise EmptyResponseError(out.text[:200] or "пустой ответ (ни текста, ни инструмента)")
    return out


def _note_truncation(out: "LLMResponse", role: str) -> None:
    """Ответ, обрезанный потолком max_tokens, обязан быть НАЗВАН — и назван С ВЛАДЕЛЬЦЕМ.

    ⚠ `stop_reason == "max_tokens"` объявлен в схеме ответа с самого начала, но не читала
    его ни одна строка кода. Фраза, оборванная на полуслове, уходила в Telegram как
    законченная; а если обрыв случался до первого готового блока — ход записывался как
    «промолчала сама», байт-в-байт как настоящее решение промолчать. На проде потолок
    сейчас щедрый (voice 32768), но дефолт в коде — 1024, и потерять `llm.json` значит
    получить обрыв на каждом длинном ответе, ничего об этом не узнав.

    ⚠ Отметка идёт ОТ ИМЕНИ РОЛИ. Слот в turns был один на процесс и доставался первому
    читателю: обрыв судьи приватности (та же `evaluator`, тот же поток, внутри её же
    хода) записывался в кольцо как «её ответ оборван». Владельца теперь несёт отметка.

    ⚠ И зовётся это из `chat()`, а не из `_call_openai`. `ping()` ходит к модели мимо
    `chat()` с `max_tokens=1` — то есть КАЖДАЯ проверка канала (`brain.py:208`) кончалась
    `stop_reason='max_tokens'` и клала отметку об обрыве на 1 символ, которую следующий
    её ход забирал как обрыв собственной фразы. Пинг — не ответ, и биографии у него нет.

    Полный ответ роли ОТМЕНЯЕТ её прежнюю отметку: это честный срок годности вместо
    гадания по часам (см. turns.clear_truncation).
    """
    truncated = str(getattr(out, "stop_reason", "")) == "max_tokens"
    if truncated:
        log.warning("ответ оборван потолком max_tokens (роль %s, %s): %d символов, "
                    "блоков %d — это НЕ законченная фраза",
                    role, getattr(out, "model", "?"),
                    len(out.text or ""), len(out.blocks or ()))
    try:
        import turns
        if truncated:
            turns.note_truncated(model=str(getattr(out, "model", "") or ""),
                                 chars=len(out.text or ""), owner=role)
        else:
            turns.clear_truncation(owner=role)
    except Exception:
        log.debug("обрыв ответа не записался в прожитый ход", exc_info=True)


def _openai_from_completion(resp, model: str) -> LLMResponse:
    choice = (getattr(resp, "choices", None) or [None])[0]
    msg = getattr(choice, "message", None)
    finish = str(getattr(choice, "finish_reason", None) or "stop")
    blocks = blocks_from_openai(msg) if msg is not None else []
    usage = getattr(resp, "usage", None)
    return LLMResponse(
        text="\n".join(b["text"] for b in blocks if b["type"] == "text").strip(),
        blocks=blocks, stop_reason=_OPENAI_STOP.get(finish, finish),
        usage={"in": int(getattr(usage, "prompt_tokens", 0) or 0),
               "out": int(getattr(usage, "completion_tokens", 0) or 0),
               **({"cache_read": _openai_cached_tokens(usage)}
                  if _openai_cached_tokens(usage) else {})},
        framework="openai", model=model)


def _openai_cached_tokens(usage) -> int:
    """Кэшированный префикс из openai-совместимого usage, или 0.

    02.08.2026. Кэш на этом пути АВТОМАТИЧЕСКИЙ — провайдер сам кэширует совпадающий
    префикс, включить его нельзя, его зарабатывают стабильностью байтов. Отчитывается он
    единственным полем `usage.prompt_tokens_details.cached_tokens`, а мы читали только
    антропиковские имена (`cache_read`/`cache_creation`) — поэтому в учёте по всем ходам
    через gpt стояли нули, и это читалось как «кэш не работает». Означало же оно
    «не сообщено»: отличить одно от другого было нечем, и на этом я построил неверный
    вывод для Егора. Реле деталь тоже теряло — исправлено тем же заходом.
    """
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    if details is None:
        return 0
    value = getattr(details, "cached_tokens", None)
    if value is None and isinstance(details, dict):
        value = details.get("cached_tokens")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _openai_from_stream(stream, model: str) -> LLMResponse:
    """Собрать ответ из SSE-чанков openai-протокола (delta.content + delta.tool_calls)."""
    parts: list[str] = []
    tools_acc: dict[int, dict] = {}
    finish = "stop"
    u_in = u_out = u_cached = 0
    for chunk in stream:
        u = getattr(chunk, "usage", None)
        if u is not None:
            u_in = int(getattr(u, "prompt_tokens", 0) or 0) or u_in
            u_out = int(getattr(u, "completion_tokens", 0) or 0) or u_out
            u_cached = _openai_cached_tokens(u) or u_cached
        chs = getattr(chunk, "choices", None) or []
        if not chs:
            continue
        ch = chs[0]
        d = getattr(ch, "delta", None)
        if d is not None:
            c = getattr(d, "content", None)
            if c:
                parts.append(c)
            for tc in getattr(d, "tool_calls", None) or []:
                idx = int(getattr(tc, "index", 0) or 0)
                slot = tools_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] += fn.arguments
        if getattr(ch, "finish_reason", None):
            finish = ch.finish_reason
    text = "".join(parts).strip()
    blocks: list[dict] = [{"type": "text", "text": text}] if text else []
    for idx in sorted(tools_acc):
        s = tools_acc[idx]
        if not s["name"]:
            continue
        try:
            args = json.loads(s["args"]) if (s["args"] or "").strip() else {}
        except Exception:
            args = {}
        blocks.append({"type": "tool_use", "id": s["id"] or f"call_{idx}",
                       "name": s["name"], "input": args if isinstance(args, dict) else {}})
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else _OPENAI_STOP.get(finish, finish)
    return LLMResponse(text=text, blocks=blocks, stop_reason=stop,
                       usage={"in": u_in, "out": u_out,
                              **({"cache_read": u_cached} if u_cached else {})},
                       framework="openai", model=model)


# --------------------------------------------------------------------------- #
#  Ротация наименований моделей: провайдеры переименовывают/выкатывают модели
#  (relay крутит gpt-5.x, z.ai — glm-*). Если заданное имя пропало из списка —
#  берём ближайшее живое (та же семья, старшая версия), не роняя ход. Кэш на TTL.
# --------------------------------------------------------------------------- #

_MODELS_CACHE: dict[str, tuple[float, list[str]]] = {}   # framework -> (expiry_epoch, [ids])
_MODELS_TTL = float(os.getenv("PRAXIS_MODELS_TTL", "600"))


def _models_url(base_url: str) -> str:
    """.../v1/models для любого base_url (снимаем хвостовой /v1, ставим один раз)."""
    b = (base_url or "").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b + "/v1/models"


def _available_models(framework: str) -> list[str]:
    """Список id моделей провайдера (кэш TTL). Под стендом сеть не трогаем вовсе."""
    # ⚠ 03.08.2026. У соседа по файлу (`_client_for`) шов PRAXIS_TEST есть, а здесь его
    # не было — и `panel.brain_catalog()` под тестом уходил ДВУМЯ живыми HTTPS-запросами
    # на api.z.ai и api.openai.com. Фейк-клиент спасал только там, где его успели
    # зарегистрировать; в `test_panel_organs` его нет вовсе. Ключей в окружении гейта
    # тоже нет (форж отдаёт подпроцессу белый список), так что запрос шёл с пустым
    # бирером на публичный адрес: стенд стучался наружу и ждал таймаута.
    if os.environ.get("PRAXIS_TEST") or framework in _TEST_CLIENTS:
        return []
    now = _time.time()
    hit = _MODELS_CACHE.get(framework)
    if hit and hit[0] > now:
        return hit[1]
    ids: list[str] = []
    try:
        fw = (_config().get("frameworks") or {}).get(framework) or {}
        base, key = fw.get("base_url") or "", fw.get("api_key") or ""
        if base:
            import urllib.request
            req = urllib.request.Request(_models_url(base), headers={
                "Authorization": f"Bearer {key}", "x-api-key": key,
                "anthropic-version": "2023-06-01"})
            data = json.loads(urllib.request.urlopen(req, timeout=6).read())
            ids = [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]
    except Exception as e:
        log.debug("llm: список моделей %s не получить (%s)", framework, type(e).__name__)
        ids = []
    _MODELS_CACHE[framework] = (now + _MODELS_TTL, ids)
    return ids


def _pick_replacement(model: str, avail: list[str]) -> str:
    """Замена пропавшей модели: предпочесть ту же семью (буквенный префикс), старшую версию."""
    def fam(m: str) -> str:
        mm = re.match(r"^([a-zA-Z]+)", m or "")
        return mm.group(1).lower() if mm else ""

    def ver(m: str) -> list[int]:
        return [int(x) for x in re.findall(r"\d+", m or "")]

    pool = [m for m in avail if fam(m) == fam(model)] or avail
    return max(pool, key=ver) if pool else ""


def _resolve_model(framework: str, model: str) -> str:
    """Имя модели с учётом ротации: заданное живо — оставить; пропало — ближайшее живое."""
    avail = _available_models(framework)
    if not avail:
        return model
    # Compatibility migration: an early UI exposed the generic gpt-5.6 alias,
    # but the Codex backend only serves concrete sol/terra/luna slugs. Prefer
    # sol deterministically when the relay's live model list proves it exists,
    # even if a stale discovery cache still advertises the rejected generic id.
    if framework == "openai" and model == "gpt-5.6" and "gpt-5.6-sol" in avail:
        pick = "gpt-5.6-sol"
    elif model in avail:
        return model
    else:
        pick = _pick_replacement(model, avail)
    if pick and pick != model:
        log.warning("llm: модель %s пропала у %s — ротация имени на %s", model, framework, pick)
        try:
            _journal(f"модель {model} пропала у {framework}, взяла {pick} (ротация имён провайдера)")
        except Exception:
            pass
        return pick
    return model


def _call(framework: str, model: str, **kw) -> LLMResponse:
    cli = _client_for(framework)
    if cli is None:
        raise RuntimeError(f"llm: фреймворк {framework} не настроен (нет ключа)")
    model = _resolve_model(framework, model)  # ротация наименований провайдера
    if framework == "anthropic":
        return _call_anthropic(cli, model, **kw)
    return _call_openai(cli, model, **kw)


def _fallbackable(e: Exception) -> bool:
    if isinstance(e, EmptyResponseError):
        return True  # вырожденный ответ релея — тоже повод уйти на другой фреймворк
    name = type(e).__name__
    if name in _FALLBACK_ERRORS:
        return True
    try:
        if int(getattr(e, "status_code", 0) or 0) >= 500:
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(e, TimeoutError)


def chat(role: str, *, system=None, messages: list, tools: list | None = None,
         max_tokens: int | None = None, thinking: int | None = None) -> LLMResponse:
    """Вызов модели по роли. Фолбэк на противоположный фреймворк — один повтор, честно в дневник."""
    if role not in ROLES:
        raise ValueError(f"llm: неизвестная роль {role!r}")
    cfg = _config()
    rc = cfg["roles"][role]
    fw, model = rc["framework"], rc["model"]
    mt = int(max_tokens or rc.get("max_tokens") or DEFAULT_MAX_TOKENS[role])
    st = _STATE[role]
    t0 = _time.time()
    try:
        resp = _call(fw, model, system=system, messages=messages, tools=tools,
                     max_tokens=mt, thinking=thinking)
        # Обрыв потолком — факт этой роли. Раньше он ставился внутри `_call_openai`, то
        # есть anthropic-путь обрыва не замечал вовсе, а `ping()` замечал лишний.
        _note_truncation(resp, role)
        if st["on_fallback"]:
            log.info("llm: %s вернулась на основной канал (%s/%s)", _ROLE_RU[role], fw, model)
        st["on_fallback"], st["last_error"] = False, ""
        # 9.1: расход копится; сбой записи вызова не роняет. PASS 22: по ФАКТИЧЕСКОМУ имени
        # (после ротации _resolve_model конфигное имя может врать в by-model разрезе).
        _usage_add(role, resp.usage, model=(resp.model or model))
        _brain_note(role, fw, resp.model or model, ok=True,
                    latency_ms=(_time.time() - t0) * 1000)
        return resp
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:120]}"
        st["last_error"] = err
        _brain_note(role, fw, model, ok=False, error=err,
                    empty=isinstance(e, EmptyResponseError))
        if not _fallbackable(e):
            raise
        other = "openai" if fw == "anthropic" else "anthropic"
        fb_model = (rc.get("fallback_model") or "").strip()
        if not fb_model or _client_for(other) is None:
            raise
        log.warning("llm: %s упал (%s) — фолбэк на %s/%s", _ROLE_RU[role], err, other, fb_model)
        t1 = _time.time()
        try:
            resp = _call(other, fb_model, system=system, messages=messages, tools=tools,
                         max_tokens=mt, thinking=None)
        except Exception as e2:
            _brain_note(role, other, fb_model, ok=False,
                        error=f"{type(e2).__name__}: {str(e2)[:80]}",
                        empty=isinstance(e2, EmptyResponseError))
            raise
        _note_truncation(resp, role)   # фолбэк-модель обрывается ровно так же
        if not st["on_fallback"]:  # событие — один раз на уход, не на каждый вызов
            _journal(f"{_ROLE_RU[role]} упал ({type(e).__name__}), ушла на фолбэк {other}/{fb_model}")
        st["on_fallback"] = True
        _usage_add(role, resp.usage, fallback=True, model=(resp.model or fb_model))
        _brain_note(role, other, resp.model or fb_model, ok=True,
                    latency_ms=(_time.time() - t1) * 1000, fallback=True)
        return resp


def _brain_note(role: str, framework: str, model: str, **kw) -> None:
    """PASS 22: наблюдение вызова в per-model статистику (brain.py). Никогда не роняет вызов."""
    try:
        import brain
        brain.note_call(role, framework, model, **kw)
    except Exception:
        log.debug("brain note не записался", exc_info=True)


def ping(role: str) -> tuple[bool, str]:
    """Проверка ОСНОВНОГО канала роли (без фолбэка): 1-токенный вызов. -> (ok, err)."""
    try:
        rc = _config()["roles"][role]
    except KeyError:
        return (False, f"нет роли {role}")
    try:
        resp = _call(rc["framework"], rc["model"], system="", messages=[{"role": "user", "content": "ping"}],
                     tools=None, max_tokens=1, thinking=None)
        _usage_add(role, resp.usage, model=rc["model"])  # 18.5: пинг — тоже расход, не мимо счётчика
        return (True, "")
    except Exception as e:
        return (False, f"{type(e).__name__}: {str(e)[:200]}")


def snapshot() -> dict:
    """Для STATE/панели: роль -> {framework, model, fallback_model, fallback_armed, on_fallback, last_error}."""
    cfg = _config()
    out = {}
    for role in ROLES:
        rc = cfg["roles"][role]
        other = "openai" if rc["framework"] == "anthropic" else "anthropic"
        armed = bool((rc.get("fallback_model") or "").strip()
                     and (cfg["frameworks"].get(other) or {}).get("api_key"))
        st = _STATE[role]
        out[role] = {"framework": rc["framework"], "model": rc["model"],
                     "max_tokens": rc.get("max_tokens"),
                     "fallback_model": rc.get("fallback_model") or "",
                     "fallback_armed": armed,
                     "on_fallback": bool(st["on_fallback"]),
                     "last_error": st["last_error"]}
    return out


# --------------------------------------------------------------------------- #
#  Тестовые хелперы (сети в тестах нет; фейк эмулирует anthropic-SDK .messages.create)
# --------------------------------------------------------------------------- #

def use_test_client(fake, framework: str = "anthropic") -> None:
    """Подставить фейковый SDK-клиент для тестов (None = «канал не настроен»)."""
    _TEST_CLIENTS[framework] = fake


def clear_test_clients() -> None:
    _TEST_CLIENTS.clear()


def state_line() -> str:
    """Одна строка для STATE: основной и вспомогательный модельные каналы."""
    try:
        snap = snapshot()
    except Exception:
        return ""
    parts = []
    for role in ROLES:
        s = snap[role]
        mark = " ⚠ фолбэк" if s["on_fallback"] else ""
        parts.append(f"{_ROLE_RU[role]}={s['model']} ({s['framework']}){mark}")
    return ", ".join(parts)
