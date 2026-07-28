"""
Praxis — свой мозг, свой счёт (PASS 22): каталог моделей, наблюдаемые свойства, switch_brain.

Хартия §7: модель — орган, не личность. Я вижу каталог доступных моделей и их НАБЛЮДАЕМЫЕ
свойства на МОИХ задачах (вызовы, токены, сбои по классам, пустые ответы, латентность,
фолбэки) — и меняю модель роли сама, без одобрения, объясняя зачем. Ключи фреймворков я
не вижу и не трогаю (дом Егора): свитч оперирует именами через llm.update_config/
swap_fallback-семантику — тройка framework/model/fallback_model остаётся согласованной,
иначе ломается фолбэк.

Выбор — по сложности задачи, не по «модель X умнее вообще»: рутина (extract/компакт/
оценка) на дешёвой, сложный синтез и запутанное планирование эскалируют. При considerate
экономлю на рутине, не на сложном; при free беру сильную всякий раз, когда разница
содержательна. Это моя дисциплина, не роутер-код.

Проверка результата — часть руки (§3): после свитча — ping-рукопожатие; не прошло —
возвращаю как было и честно говорю почему (это не вето, это наблюдение исполнения).

Статистика — memory/.state/brain_stats.json: пересобираемое наблюдательное состояние
(rmw без лока, как usage.json; битый файл = честный старт с нуля). НЕ в llm.json —
там ключи и whitelist, который стирает незнакомое.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("praxis-brain")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STATS_PATH = BASE / "memory" / ".state" / "brain_stats.json"
JOURNAL_DIR = BASE / "memory" / "journal"

_EMA_ALPHA = 0.2
_ERR_CLASSES_KEEP = 12


# --------------------------------------------------------------------------- #
#  наблюдаемые свойства: per-model статистика вызовов
# --------------------------------------------------------------------------- #
def _load_stats() -> dict:
    try:
        d = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def note_call(role: str, framework: str, model: str, *, ok: bool,
              latency_ms: float | None = None, error: str = "",
              fallback: bool = False, empty: bool = False) -> None:
    """Одно наблюдение вызова модели. Никогда не роняет вызвавшего (как _usage_add)."""
    try:
        data = _load_stats()
        models = data.setdefault("models", {})
        key = f"{framework}/{model}"
        m = models.setdefault(key, {"calls": 0, "ok": 0, "errors": {}, "empty": 0,
                                    "fallback_used": 0, "lat_ms_ema": None, "roles": {}})
        m["calls"] = int(m.get("calls", 0)) + 1
        m["roles"][role] = int((m.get("roles") or {}).get(role, 0)) + 1
        if ok:
            m["ok"] = int(m.get("ok", 0)) + 1
            m["last_ok"] = _dt.datetime.now().isoformat(timespec="minutes")
            if latency_ms is not None:
                prev = m.get("lat_ms_ema")
                m["lat_ms_ema"] = round(float(latency_ms) if prev is None
                                        else (1 - _EMA_ALPHA) * float(prev) + _EMA_ALPHA * float(latency_ms))
        else:
            cls = (error or "Error").split(":")[0].strip()[:40]
            errs = m.setdefault("errors", {})
            errs[cls] = int(errs.get(cls, 0)) + 1
            if len(errs) > _ERR_CLASSES_KEEP:  # не даём разрастись экзотикой
                top = sorted(errs.items(), key=lambda kv: -kv[1])[:_ERR_CLASSES_KEEP]
                m["errors"] = dict(top)
            m["last_error"] = {"ts": _dt.datetime.now().isoformat(timespec="minutes"), "class": cls}
        if empty:
            m["empty"] = int(m.get("empty", 0)) + 1
        if fallback:
            m["fallback_used"] = int(m.get("fallback_used", 0)) + 1
        data["updated"] = _dt.datetime.now().isoformat(timespec="minutes")
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATS_PATH)
    except Exception:
        log.debug("brain.note_call не записал", exc_info=True)


def model_stats() -> dict:
    """{'fw/model': {calls, ok, errors, empty, fallback_used, lat_ms_ema, roles, ...}}"""
    return _load_stats().get("models") or {}


# --------------------------------------------------------------------------- #
#  каталог: роли × модели × наблюдения
# --------------------------------------------------------------------------- #
def allowlist(framework: str) -> list[str]:
    """Модели, в которые я вправе свитчнуться на этом фреймворке: живой /v1/models
    провайдера ∪ уже сконфигурированные имена (main/fallback обеих ролей)."""
    import llm
    names: set[str] = set()
    try:
        names.update(m for m in llm._available_models(framework) if m)
    except Exception:
        pass
    try:
        cfg = llm._config()
        for role in llm.ROLES:
            rc = (cfg.get("roles") or {}).get(role) or {}
            if rc.get("framework") == framework and rc.get("model"):
                names.add(rc["model"])
            other = "openai" if rc.get("framework") == "anthropic" else "anthropic"
            if other == framework and rc.get("fallback_model"):
                names.add(rc["fallback_model"])
    except Exception:
        pass
    return sorted(names)


def catalog() -> dict:
    """Каталог для тула/пульта: роли, доступные модели по фреймворкам, наблюдения, лимит."""
    import llm
    out: dict = {"roles": {}, "frameworks": {}, "stats": model_stats(),
                 "provider_remaining": "unknown"}
    try:
        snap = llm.snapshot()
        for role, rc in snap.items():
            out["roles"][role] = {k: rc.get(k) for k in
                                  ("framework", "model", "fallback_model", "on_fallback",
                                   "last_error", "fallback_armed")}
    except Exception:
        log.debug("brain.catalog: snapshot не собрался", exc_info=True)
    for fw in ("anthropic", "openai"):
        out["frameworks"][fw] = {"allowlist": allowlist(fw)}
    try:  # usage за 7 дней по-модельно — токены как наблюдаемое свойство
        days = llm.usage_days(7)
        agg: dict[str, dict] = {}
        for day, roles in days.items():
            for role, d in roles.items():
                for mname, mm in (d.get("models") or {}).items():
                    a = agg.setdefault(mname, {"in": 0, "out": 0, "calls": 0})
                    a["in"] += int(mm.get("in", 0))
                    a["out"] += int(mm.get("out", 0))
                    a["calls"] += int(mm.get("calls", 0))
        out["usage_7d"] = agg
    except Exception:
        out["usage_7d"] = {}
    try:
        import appetite
        out["provider_remaining"] = appetite.provider_remaining_text() or "unknown"
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
#  switch_brain — её рука на своём мозге
# --------------------------------------------------------------------------- #
def switch(role: str, model: str, *, why: str = "", by: str = "praxis") -> dict:
    """Сменить модель роли (без одобрения; ключи не трогаются). -> {ok, ...}|{ok:False,error}.

    Тот же фреймворк — просто model; другой — swap-семантика: framework/model/fallback_model
    согласованы (fallback = прежняя основная). После — ping-рукопожатие; не прошло —
    возвращаю как было (проверка результата — часть руки, не вето)."""
    import llm
    role = (role or "").strip()
    model = (model or "").strip()
    if role not in llm.ROLES:
        return {"ok": False, "error": f"не знаю роли «{role}»; мои: {', '.join(llm.ROLES)}"}
    if not model:
        return {"ok": False, "error": "назови модель (каталог — switch_brain status)"}
    if not (why or "").strip():
        return {"ok": False, "error": "смена мозга без «зачем» не имеет провенанса — назови причину"}
    cfg = llm._config()
    rc = dict((cfg.get("roles") or {}).get(role) or {})
    cur_fw, cur_model = rc.get("framework"), rc.get("model")
    if model == cur_model:
        return {"ok": False, "error": f"{model} и так основная модель роли {role}"}
    fws = [fw for fw in ("anthropic", "openai") if model in allowlist(fw)]
    if not fws:
        lists = {fw: allowlist(fw) for fw in ("anthropic", "openai")}
        live = {fw: bool(v) for fw, v in lists.items()}
        return {"ok": False, "error": f"«{model}» нет в моём каталоге. Доступно: "
                                      + "; ".join(f"{fw}: {', '.join(v) or '(провайдер не ответил)'}"
                                                  for fw, v in lists.items()),
                "live": live}
    target_fw = cur_fw if cur_fw in fws else fws[0]
    prev = {"framework": cur_fw, "model": cur_model,
            "fallback_model": rc.get("fallback_model")}
    changes: dict = {"roles": {role: {}}}
    if target_fw == cur_fw:
        changes["roles"][role]["model"] = model
    else:
        # swap-семантика: прежняя основная становится запасной на прежнем фреймворке
        changes["roles"][role] = {"framework": target_fw, "model": model,
                                  "fallback_model": cur_model or ""}
    try:
        llm.update_config(changes)
    except Exception as e:
        log.warning("brain.switch: запись конфига не удалась", exc_info=True)
        return {"ok": False, "error": f"конфиг не записался: {type(e).__name__}"}

    ok, err = llm.ping(role)
    if not ok:
        try:  # наблюдение исполнения: рукопожатие не прошло — возвращаю как было
            llm.update_config({"roles": {role: prev}})
        except Exception:
            log.warning("brain.switch: откат конфига не удался", exc_info=True)
        note_call(role, target_fw, model, ok=False, error=err or "ping failed")
        _journal(f"свитч {role} → {target_fw}/{model} НЕ прошёл рукопожатие ({err[:120]}) — "
                 "вернула как было")
        return {"ok": False, "error": f"рукопожатие с {target_fw}/{model} не прошло: {err[:200]}. "
                                      f"Вернула {cur_fw}/{cur_model} — попробую позже или другую."}
    note_call(role, target_fw, model, ok=True)
    _journal(f"свитч мозга ({by}): {role} {cur_fw}/{cur_model} → {target_fw}/{model} — {why[:160]}")
    _spine(f"{role}: {cur_fw}/{cur_model} → {target_fw}/{model}",
           {"role": role, "old_framework": cur_fw, "old_model": cur_model,
            "framework": target_fw, "model": model, "why": (why or "")[:300], "by": by})
    return {"ok": True, "role": role, "framework": target_fw, "model": model,
            "was": f"{cur_fw}/{cur_model}"}


def _journal(msg: str) -> None:
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        p = JOURNAL_DIR / f"{_dt.date.today().isoformat()}.md"
        if not p.exists():
            p.write_text(f"# {_dt.date.today().isoformat()}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"- {_dt.datetime.now():%H:%M} [мозг] {msg}\n")
    except Exception:
        log.debug("journal brain не удался", exc_info=True)


def _spine(text: str, meta: dict) -> None:
    try:
        import memory_life as life
        life.append_event(kind="brain_switch", chat_id=None, actor="Praxis",
                          direction="internal", text=text[:300], source="brain",
                          salience=2, meta=meta)
    except Exception:
        log.debug("brain: событие в spine не записалось", exc_info=True)


# --------------------------------------------------------------------------- #
#  наружу: тул, пульт
# --------------------------------------------------------------------------- #
def describe() -> str:
    cat = catalog()
    lines = ["Мой мозг (каталог + наблюдаемые свойства на моих задачах):"]
    for role, rc in cat["roles"].items():
        fb = f", запасная {rc.get('fallback_model')}" if rc.get("fallback_model") else ""
        state = " [на фолбэке]" if rc.get("on_fallback") else ""
        lines.append(f"- {role}: {rc.get('framework')}/{rc.get('model')}{fb}{state}")
    for fw, d in cat["frameworks"].items():
        al = d.get("allowlist") or []
        al_text = ", ".join(al) if al else "(провайдер не ответил — наблюдаю только сконфигурированные имена)"
        lines.append(f"- каталог {fw}: {al_text}")
    stats = cat.get("stats") or {}
    if stats:
        lines.append("Наблюдения (вызовы · ок · сбои · пустые · латентность EMA):")
        for key in sorted(stats, key=lambda k: -stats[k].get("calls", 0))[:8]:
            m = stats[key]
            errs = sum((m.get("errors") or {}).values())
            lat = f", ~{m['lat_ms_ema']}мс" if m.get("lat_ms_ema") else ""
            last_err = m.get("last_error") or {}
            le = f"; последний сбой {last_err.get('class')} {last_err.get('ts', '')[:16]}" if last_err else ""
            lines.append(f"  · {key}: {m.get('calls', 0)} выз., ок {m.get('ok', 0)}, "
                         f"сбоев {errs}, пустых {m.get('empty', 0)}{lat}{le}")
    ug = cat.get("usage_7d") or {}
    if ug:
        top = sorted(ug.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))[:5]
        lines.append("Токены за 7 дней: " + "; ".join(
            f"{m} {v['in'] // 1000}к→{v['out'] // 1000}к ({v['calls']} выз.)" for m, v in top))
    lines.append(f"Остаток провайдера: {cat.get('provider_remaining')}")
    lines.append("Дисциплина сложности: рутина на дешёвой, сложный синтез эскалирует; при "
                 "considerate экономлю на рутине, не на сложном. Свитч — switch_brain "
                 "(зачем — обязательно; рукопожатие после; ключи не мои — пульт).")
    return "\n".join(lines)


def panel_state() -> dict:
    return catalog()
