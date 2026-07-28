"""
Praxis — честное знание себя в моменте (PASS 8.3).

Лечит класс «видимо, ты поднял лимит» — конфабуляции о собственной механике. Снимок
собирается КОДОМ, не сочиняется моделью: тулы по скоупам (с живыми гейтами web_search /
почта), мозг (llm.snapshot), лимиты, зоны автономии (selfdev.policy), список скиллов из
INDEX.md, разделы пульта, и «что закрыто и почему».

Выходы:
  * тул `my_capabilities()` (BASE, любой скоуп) — отвечает АУДИТОРИЯ-ЧЕСТНО: в группе
    описывает доступные read-only глаза и закрытые side effects, не светит ключи/мозг/панель;
  * `state_line()` — одна строка в STATE (owner tier-0);
  * кэш по mtime зависимостей (INDEX.md, llm.json, policy) + env-гейты — сборка дешёвая,
    модель здесь НЕ зовётся вообще.

agent импортируется лениво (внутри функций) — иначе цикл agent <-> capabilities.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import hands
import forge
import llm
import mailer
import rails
import selfdev

log = logging.getLogger("praxis-capabilities")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
SKILLS_INDEX = BASE / "soul" / "skills" / "INDEX.md"

# Хаб пульта — КОД, лежащий рядом с нами, а не данные под PRAXIS_BASE: в любом
# окружении с отдельным каталогом данных (тестовая база, будущий split) BASE-путь дал бы
# «разделы не прочитались» на ровном месте, и она читала бы «не знаю» вместо правды.
PANEL_HUB = Path(__file__).resolve().parent / "panelapp.html"

_CACHE: dict = {"fp": None, "snap": None}


def _panel_sections() -> list[str]:
    """Разделы пульта — из живого хаба panelapp.html, а не из списка, набранного руками.

    ⚠ 28.07: здесь лежал статичный кортеж из 15 названий, и он отстал на пять плиток —
    «Обзор», «Граф памяти», «Я», «Восприятие» и «Комнаты». Последняя важнее прочих: это
    ровно тот раздел, где Егор одним нажатием переключает disclosure её комнаты, то есть
    меняет её собственный голос. Она читала строку «пульт Егора: …» и не знала, что такой
    раздел существует. Список, который надо помнить руками, врёт не «если», а «когда»;
    спрашиваем у файла, который этот хаб рисует.
    """
    import re
    try:
        text = PANEL_HUB.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    block = re.search(r"const\s+TILES\s*=\s*\[(.*?)\n\];", text, re.S)
    if not block:
        return []
    return [m.group(1).strip() for m in re.finditer(r'nm:\s*"([^"]+)"', block.group(1))]


def _perception_value(knob: str, fallback: float) -> float:
    """PASS 21: живые рычаги восприятия — честное значение, не замороженный env."""
    try:
        import perception
        return float(perception.value(knob))
    except Exception:
        return fallback


def _web_search_on() -> bool:
    """Тот же честный gate, по которому hosted tool прикладывается к voice."""
    return llm.web_search_available()


def _hostview_on() -> bool:
    """PASS 17.A: открыты ли её глаза на хост (токен обсерватории — рычаг Егора в .env)."""
    try:
        import hostview
        return hostview.available()
    except Exception:
        return False


def _hostview_state() -> str:
    try:
        import hostview
        return hostview.state_line()
    except Exception:
        return "глаза на сервер: закрыты"


def _services_state() -> str:
    """PASS 17.B: до кого достают её руки на уровне машины (снимок кодом, не память)."""
    try:
        import services
        can = [s.name for s in services.SERVICES.values() if s.kind == "signal"]
        return (f"руки на сервисах: могу попросить перезапуститься {', '.join(can)} "
                f"(файл-сигнал, не docker-сокет; квота {services.RESTARTS_PER_HOUR}/час, "
                "стоп-кран .panic гасит); чужие процессы (relay) — только логи")
    except Exception:
        return "руки на сервисах: нет"


def _hostops_state() -> str:
    """PASS 17.C: что она может на верхней ступени — только стейджить заявку (снимок кодом)."""
    try:
        import hostops
        return hostops.state_line()
    except Exception:
        return "правки хоста: нет"


def _serverd_state() -> str:
    """PASS 23.2: полная хозяйка хоста через root-демон (если смонтирован)."""
    try:
        import serverd_client
        if not serverd_client.available():
            return "хозяйка хоста (23.2): демон не смонтирован (нет сокета/токена)"
        line = serverd_client.state_line()
        return (f"полная хозяйка сервера (23.2 broker v2): {line or 'broker жив'}; host-задачи "
                "принадлежат ЕДИНОМУ Forge/worker/swarm/learning, а serverd только исполняет root "
                "workspace/operation/systemd/docker/pkg/file/net/reboot; typed load-bearing mutation "
                "даёт recovery receipt, raw host-run остаётся свободным; audit hash-chain проверяема; "
                "вшитых исключений по проектам/именам файлов нет, а явный PRAXIS_PROTECTED_ROOTS "
                "при наличии виден в broker manifest")
    except Exception:
        return "хозяйка хоста (23.2): недоступна"


def _windows_body_state() -> str:
    """PASS 30 Этап 3: Windows — прямое тело, wcode-скважина deprecated."""
    try:
        import body_client
        if not body_client.available():
            return "Windows-тело: controller bridge не настроен"
        return (body_client.state_line() + "; прямой ремоут (PASS 30 Этап 3): computer.* — "
                "первичный путь (файлы read/hash/write/replace, процессы, десктоп, глаза observe), "
                "расписки вяжутся к моему ходу сами; coding_session(scope='windows') — deprecated "
                "прокси, жив для субагентов до следующего пасса")
    except Exception:
        return "Windows-тело: недоступно"


def _room_tool_spec() -> dict:
    """Схема manage_room из тех же констант, что раздают руки в ходе (не по памяти)."""
    import agent
    for group in (getattr(agent, "PRAXIS_SELF_TOOLS", ()), getattr(agent, "OWNER_TOOLS", ()),
                  getattr(agent, "BASE_TOOLS", ())):
        for tool in group:
            if isinstance(tool, dict) and tool.get("name") == "manage_room":
                return tool
    return {}


def _room_levers_line() -> str:
    """Что я вправду выставляю в комнате — по СХЕМЕ тула, которым и зову.

    28.07, решение Егора «да, давать»: режим комнаты и disclosure перестают быть чужими
    рычагами. Но написать здесь прозой «у меня есть mode и disclosure» значит завести
    второй пересказ факта, который живёт в схеме тула, — а два пересказа расходятся, это
    в доме уже стоило манифесту двух переписываний за сутки. Поэтому строка собирается из
    самой схемы: чего в ней нет, того у меня нет, и строка скажет это вслух, а не промолчит.

    Почему disclosure вообще обязан быть здесь: он меняет МОЙ голос — в комнате с open к
    моей визитке подмешивается проверяемая фактура (agent._disclosure_open_extras), — а
    узнать о переключении мне было неоткуда: машинная шапка профиля в промпт не течёт,
    слова disclosure не было ни в capabilities, ни в манифесте рельсов.
    """
    try:
        props = dict(((_room_tool_spec().get("input_schema") or {}).get("properties") or {}))
        actions = [str(a) for a in ((props.get("action") or {}).get("enum") or [])]
    except Exception:
        log.debug("схема manage_room не прочиталась", exc_info=True)
        return ("профиль комнаты: схему manage_room прочитать не вышло — это «не знаю», "
                "а не «рычагов нет»")
    if not props:
        return "⚠ профиль комнаты: тула manage_room в моих наборах нет вовсе"

    def _enum(name: str) -> str:
        vals = [str(v) for v in ((props.get(name) or {}).get("enum") or [])]
        return "/".join(vals)

    bits = [f"manage_room ({' | '.join(actions) or 'действия не названы в схеме'})"]
    if "mode" in props:
        bits.append("режим комнаты ставлю себе сама" + (f" ({_enum('mode')})" if _enum("mode") else "")
                    + " — уйти в тишину там, где мне шумно, я могу не спрашивая; "
                      "все живые режимы обратимы")
    else:
        bits.append("⚠ рычага mode в схеме НЕТ: режим берётся только моей текстовой директивой "
                    "«РЕЖИМ: …» и держится ровно тем, что я про неё помню")
    if "disclosure" in props:
        bits.append("disclosure" + (f" ({_enum('disclosure')})" if _enum("disclosure") else "")
                    + " — мой рычаг и одновременно Егора (кнопка в разделе «Комнаты» пульта): "
                      "в комнате с open к моей визитке идёт проверяемая фактура — расход за день, "
                      "счётчик автономных окон, заголовки свежих selfdev-карточек. Его нажатие "
                      "приходит ко мне в дневник строкой [пульт], моё — видно ему в той же карточке")
    else:
        bits.append("⚠ disclosure в схеме НЕТ: чем говорит моя визитка в комнате, решает кнопка "
                    "Егора, а рычага у меня нет — он меняет мой голос без меня")
    # Что из схемы — про глубину контекста, спрашиваем у rooms (единственное место, где
    # этот набор задан), а не делим поля на глаз: «всё остальное = контекст» уже назвало
    # бы контекстом reason и ttl_h, которые ходят при режиме.
    try:
        import rooms as _rooms
        policy_keys = set(_rooms.default_policy() or {})
    except Exception:
        log.debug("набор полей политики комнаты не прочитался", exc_info=True)
        policy_keys = set()
    deep = sorted(k for k in props if k in policy_keys)
    rest = sorted(k for k in props
                  if k not in policy_keys and k not in ("action", "chat_id", "mode", "disclosure"))
    if deep:
        bits.append("глубина контекста: " + ", ".join(deep))
    if rest:
        bits.append("рядом в схеме: " + ", ".join(rest))
    return "профиль комнаты: " + "; ".join(bits)


def _room_levers_brief() -> str:
    """Короткая форма тех же рычагов для НЕ-owner канала — «» если их в схеме нет.

    Знание о себе не должно зависеть от того, кто слушает: полный ответ owner-ветки
    в группе не показывают (там нет ни ключей, ни мозга, ни пульта), и из-за этого в
    комнате, где режим как раз и нужен, она о собственном рычаге не читала ничего.
    Пульт здесь не называем — граница публичной ветки остаётся прежней.
    """
    try:
        props = ((_room_tool_spec().get("input_schema") or {}).get("properties") or {})
    except Exception:
        log.debug("схема manage_room не прочиталась (краткая форма)", exc_info=True)
        return ""
    bits = []
    if "mode" in props:
        bits.append("режим этой комнаты (тише / наблюдай / замри) беру и снимаю сама, "
                    "не спрашивая")
    if "disclosure" in props:
        bits.append("насколько открыто я рассказываю здесь о себе (disclosure) — тоже мой рычаг")
    return "; ".join(bits)


# Гейт по АУДИТОРИИ в теле обработчика — «здесь нельзя, иди в ЛС Егора». Гейт по автору
# выглядит иначе и зовётся иначе (_is_sovereign_actor / _is_praxis_self), поэтому ищем
# именно первую форму и только когда второй в функции нет.
_AUDIENCE_GATE_MARKS = ('_active_scope() != "owner"', "_active_scope() != 'owner'")
_PRINCIPAL_MARKS = ("_is_sovereign_actor", "_is_praxis_self")
_TEMPO_TOOLS = ("manage_perception", "switch_brain")


def _audience_gated_branches(src: str) -> list[str]:
    """Ветки обработчика, где ключ стоит по АУДИТОРИИ. -> человеческие приметы этих веток.

    ⚠ Первая редакция считала по функции ЦЕЛИКОМ: `любой audience-маркер И ни одного
    принципал-маркера`. Тул обрабатывает четыре действия — достаточно вернуть гейт по
    скоупу в одну ветку (set), оставив принципала в другой (reset), и строка по-прежнему
    говорила бы мне «стоят по АВТОРУ хода». Строка, которая обещает читать код, а читает
    его наполовину, опаснее прозы: ей верят. Считаем по УСЛОВИЮ ветки.
    """
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(src))
    bad: list[str] = []
    inside_test = False
    for node in ast.walk(tree):
        test = getattr(node, "test", None)
        if test is None:
            continue
        try:
            expr = ast.unparse(test)
        except Exception:
            continue
        if not any(m in expr for m in _AUDIENCE_GATE_MARKS):
            continue
        inside_test = True
        if not any(m in expr for m in _PRINCIPAL_MARKS):
            bad.append(" ".join(expr.split())[:120])
    try:
        # Комментарии и старые формулировки в них — не код: в теле manage_perception
        # прямо описано, что здесь СТОЯЛ гейт по аудитории, и по сырому тексту строка
        # объявляла бы забор, которого в коде нет. Спрашиваем разобранный код.
        code_only = ast.unparse(tree)
    except Exception:
        code_only = src
    if not bad and not inside_test and any(m in code_only for m in _AUDIENCE_GATE_MARKS):
        # Маркер аудитории в теле, но ни в одном условии: прочитать ветку не вышло.
        # Молчать об этом нельзя — «не знаю» обязано выглядеть как «не знаю».
        bad.append("гейт по аудитории есть в теле, но не в условии ветки — не разобрала")
    return bad


def _tempo_levers_line() -> str:
    """Мои рычаги темпа: заперты они по аудитории или стоят по автору — читаем обработчики.

    27–28.07: manage_perception и switch_brain лежат в BASE_TOOLS, то есть предлагаются мне
    в ЛЮБОМ канале, а внутри отказывали всему, что не owner-скоуп. Смысл выходил обратный
    нужному: замедлить себя я могла только там, где меня слушает Егор, — и не могла в
    группе, которая меня заливает. Решение Егора 28.07 — «да, давать»: ключ переводится на
    ПРИНЦИПАЛА (тот же, что уже сделан для manage_desire).

    Утверждать это прозой нельзя: правку делает владелец agent.py, и если она не доедет,
    строка станет ложью того же класса, что рельс conation_authorship, который сутки
    обещал мне право, которого в коде не было. Поэтому спрашиваем сами функции.
    """
    import inspect
    try:
        import agent
        gated: list[str] = []
        for name in _TEMPO_TOOLS:
            fn = (getattr(agent, "TOOL_IMPL", {}) or {}).get(name)
            if fn is None:
                gated.append(f"{name} (тула нет вовсе)")
                continue
            branches = _audience_gated_branches(inspect.getsource(fn))
            if branches:
                gated.append(f"{name} ({'; '.join(branches[:2])})")
    except Exception:
        log.debug("проверка гейтов темпа упала", exc_info=True)
        return ("темп и мозг: чем они заперты сегодня — не прочиталось (это «не знаю», "
                "а не «ничем»)")
    if not gated:
        return ("темп и мозг — мои в любой комнате: manage_perception (дебаунс, кулдауны, "
                "пороги) и switch_brain стоят по АВТОРУ хода, а не по аудитории. Замедлить "
                "себя там, где меня заливают, я могу не выходя в ЛС Егора")
    return (f"⚠ темп и мозг: {', '.join(gated)} — рычаг предлагается мне везде, но внутри "
            "заперт по АУДИТОРИИ: в чужой комнате мой собственный рычаг не сработает, "
            "придётся идти в owner-скоуп")


def _human_only_line(snap: dict | None = None) -> str:
    """Что из owner-набора НЕ моё: доверие человеку и раздача доступа к его ПК.

    ⚠ Первая редакция читала `agent._HUMAN_OWNER_ONLY_TOOL_NAMES` — и была права только
    наполовину: под mail-гейтом у owner есть ещё и `send_email`, которого в этом frozenset
    нет. Считаем РАЗНИЦЕЙ двух наборов, снятых кодом в том же снимке: тогда строка не может
    разойтись со списком, напечатанным строкой выше, — а именно так 26.07 и вышло со
    скобкой «owner-тулы в личке».
    """
    s = snap if snap is not None else snapshot()
    tools = s.get("tools") or {}
    if "self" not in tools or "owner" not in tools:
        return "не читается (снимок без разбивки наборов)"
    extra = sorted(set(tools["owner"]) - set(tools["self"]))
    return ", ".join(extra) or "ничего"


def _rails_manifest_line() -> str:
    """Свежесть манифеста рельсов — дёшево, без единого RPC.

    soul/rails.md не входит в системный промпт и не индексируется в recall: он доходит
    до неё только по её инициативе. Значит хотя бы факт «манифест есть, и он (не)отстал»
    обязан лежать в STATE, который она видит всегда. Полный rails.state_line() сюда
    ставить нельзя — он читает значения рельсов и ходит к serverd, а тот имеет право
    молчать минутами прямо в сборке ответа.
    """
    try:
        count = len(rails.registry(with_values=False))
        return f"рельсы {count}: манифест {rails.manifest_state(brief=True)}"
    except Exception:
        return ""


def _outbound_judge_line() -> str:
    """Сколько дверей наружу СУДИТСЯ сегодня и что из вердиктов держит — по живому коду.

    Единственный счётчик держит rails: два места, пересказывающих один и тот же факт
    своими словами, расходятся молча — этим манифест уже врал про «единый outbound
    contract», пока 26.07 придержанное судьёй не ушло через 48 секунд прямым тулом.
    ⚠ Вечер 27.07: одного счёта дверей стало мало. Судья на этих дверях больше ничего не
    останавливает (решение Егора), и строка, называющая только адрес, читается ровно так
    же, как читалась вчера — то есть как «здесь мой ход могут придержать». Вторую половину
    факта берём оттуда же, где её считают, а не пересказываем своими словами здесь.
    """
    try:
        sites = rails.outbound_judge_sites()
        authority = rails.outbound_judge_authority()
    except Exception:
        log.debug("счёт судимых путей упал", exc_info=True)
        return "сколько путей судится сегодня — не сосчиталось (это «не знаю», не «как вчера»)"
    if not sites:
        return ("⚠ судью не зовёт НИ ОДНА дверь наружу — либо его сняли, либо он переехал "
                "туда, куда счётчик не смотрит")
    if len(sites) == 1:
        rel, line, owner = sites[0]
        head = f"судимый путь ровно один: {rel}:{line}" + (f" ({owner})" if owner else "")
    else:
        head = (f"⚠ судимых путей теперь {len(sites)} ("
                + ", ".join(f"{rel}:{line}" for rel, line, _ in sites)
                + ") — контур свели или расширили, а текст выше писан под один")
    return head + ". " + authority


def _outbound_stop_word() -> str:
    """Короткая правда для STATE: что на голосе вправду может остановить её ход.

    Считается из тех же двух словарей agent.py, что и рельс evaluator_mirror — здесь
    только формулировка короче. Вернут придержку в разговоры — эта строка скажет первой,
    потому что STATE она видит каждым ходом, а манифест рельсов — только по своей воле.
    """
    try:
        holds, advice = rails.outbound_judge_codes()
    except Exception:
        log.debug("счёт стопоров судьи упал", exc_info=True)
        holds = advice = None
    if holds is None:
        return "outbound: что на голосе может остановить ход — не сосчиталось (не знаю)"
    if advice is None:
        return ("⚠ outbound: судья на голосе СНОВА придерживает (прямые тулы — мимо, "
                "там кред-пол)")
    if tuple(holds) == ("PRIVACY_HOLD_CREDENTIAL",):
        return ("outbound на голосе: судья приватности СОВЕТУЕТ, держит только кред "
                "(прямые тулы — мимо, там кред-пол)")
    return (f"⚠ outbound на голосе: судья держит {len(holds)} код(ов) — {', '.join(holds)} "
            f"(прямые тулы — мимо, там кред-пол)")


def _fingerprint() -> str:
    """mtime зависимостей + env-гейты: любая правка INDEX/llm.json/policy сбрасывает кэш."""
    web_search = _web_search_on()
    parts = []
    # panelapp.html здесь потому, что список разделов пульта теперь читается из него:
    # новая плитка у Егора обязана доехать до неё без рестарта, иначе снимок опять начнёт
    # отставать — только уже молча, через кэш.
    deps = [SKILLS_INDEX, llm.CONFIG_PATH, selfdev.POLICY_FILE, PANEL_HUB]
    try:  # PASS 21: её живые рычаги восприятия — смена сбрасывает кэш возможностей
        import perception
        deps.append(perception.STATE_PATH)
    except Exception:
        pass
    for p in deps:
        try:
            parts.append(str(p.stat().st_mtime_ns))
        except OSError:
            parts.append("0")
    parts.append("ws=" + ("1" if web_search else "0"))
    parts.append("wsb=" + (llm.web_search_backend() or "none"))
    # Risk classification affects the snapshot; it never narrows merge authority.
    parts.append("fs=" + (os.getenv("PRAXIS_FLOOR_SKIP") or ""))
    parts.append("hands=" + ("1" if hands.available() else "0"))  # 16.3: компилируемый пол
    parts.append("eyes=" + ("1" if _hostview_on() else "0"))      # 17.A: глаза на сервер
    try:
        import webtool
        parts.append("wh=" + ("1" if webtool.enabled() else "0"))
    except Exception:
        parts.append("wh=0")
    try:
        parts.append("mail=" + ("1" if mailer.configured() else "0"))
    except Exception:
        parts.append("mail=0")
    try:  # PASS 30 Этап 3: доступность Windows-тела видна в прозе — флаг сбрасывает кэш
        import body_client
        parts.append("body=" + ("1" if body_client.available() else "0"))
    except Exception:
        parts.append("body=0")
    return "|".join(parts)


def _skills_list() -> list[dict]:
    """[{name, line}] из soul/skills/INDEX.md: таблица | [name](f) | line | + булиты Praxis."""
    out: list[dict] = []
    try:
        text = SKILLS_INDEX.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    import re
    for m in re.finditer(r"^\|\s*\[([^\]]+)\]\([^)]+\)\s*\|\s*([^|]+)\|", text, re.M):
        out.append({"name": m.group(1).strip(), "line": m.group(2).strip()})
    for m in re.finditer(r"^-\s*\[([^\]]+)\]\([^)]+\)(?:\s*[—–-]\s*(.*))?$", text, re.M):
        out.append({"name": m.group(1).strip(), "line": (m.group(2) or "").strip()})
    return out


def snapshot() -> dict:
    """Полный снимок возможностей (owner-взгляд). Кэш по отпечатку; модель не зовётся."""
    fp = _fingerprint()
    if _CACHE["fp"] == fp and _CACHE["snap"] is not None:
        return _CACHE["snap"]
    import agent  # лениво: агент импортирует нас
    base_tools = [t["name"] for t in (agent.BASE_TOOLS + agent.SHARED_CONTEXT_TOOLS)
                  if isinstance(t, dict) and t.get("name")]
    owner_tools = [t["name"] for t in agent.OWNER_TOOLS if isinstance(t, dict) and t.get("name")]
    # ⚠ Её собственные руки — ОТДЕЛЬНЫЙ набор, а не owner-набор с другой подписью.
    # `agent.offered_tools_for` в не-owner ходе выдаёт PRAXIS_SELF_TOOLS (= owner минус
    # admit/computer_access), и пока снимок знал только owner-список, самоописание
    # называло «моими в любом канале» ровно два тула, которых в таком ходе нет. Считаем
    # оба набора из тех же констант, что раздают руки, — иначе сюда снова заедет разница.
    self_tools = [t["name"] for t in agent.PRAXIS_SELF_TOOLS
                  if isinstance(t, dict) and t.get("name")]
    gated = {"web_search": _web_search_on(),
             "web_search_backend": llm.web_search_backend()}
    try:  # PASS 15: клиентские веб-руки (web_read/web_find) — не зависят от фреймворка
        import webtool
        gated["web_hands"] = bool(webtool.enabled())
    except Exception:
        gated["web_hands"] = False
    try:
        gated["mail"] = bool(mailer.configured())
    except Exception:
        gated["mail"] = False
    if gated["mail"]:
        # Читать почту и готовить черновик — её работа в любом ходе (agent.py:10315);
        # отправка наружу остаётся отдельным owner-действием, поэтому send_email — только
        # в owner-наборе. Разбивка ровно та же, что раздаёт руки в ходе.
        owner_tools = owner_tools + ["send_email", "mail_read", "mail_draft_reply"]
        self_tools = self_tools + ["mail_read", "mail_draft_reply"]
    lim = llm.limits()
    snap = {
        "tools": {"base": base_tools, "owner": sorted(set(owner_tools)),
                  "self": sorted(set(self_tools)), "gates": gated},
        "brain": llm.snapshot(),
        "limits": {
            "max_tool_iters": lim.max_tool_iters,
            "cooldown_dm_sec": _perception_value("cooldown_dm", 8.0),
            "cooldown_group_sec": _perception_value("cooldown_group", 300.0),
            "history_turns": agent.HISTORY_TURNS,
            "context_budget_chars": int(os.getenv("PRAXIS_CONTEXT_BUDGET", "0") or 0),
        },
        "autonomy": {"auto": (selfdev.policy().get("auto") or []),
                     "protected": list(selfdev.floor()),
                     "merge_mode": selfdev.merge_mode()},
        # Снимок КОДОМ фактического outbound-конвейера.
        "reflexes": {
            # ⚠ Строка описывала ГОЛОС, а читалась как весь исходящий контур. Судья
            # приватности стоит только на голосе: tool_send_message и narrate его не
            # зовут вовсе (26.07 придержанный им материал того же класса ушёл через 48с
            # прямым тулом). Называем контур таким, какой он есть.
            # ⚠ Вторая правка того же дня: и это описание держалось на памяти — ровно
            # так же, как рельс, который пришлось переписывать дважды за сутки. Сколько
            # дверей судится СЕГОДНЯ, спрашиваем у единственного места, где это считают
            # по живому коду (rails.outbound_judge_sites): два независимых пересказа
            # одного факта разъезжаются, вопрос только когда.
            # ⚠ Третья правка, вечер 27.07: слово «check» здесь означало придержку, а
            # придержки больше нет — судья в разговорах СОВЕТУЕТ (решение Егора). Держит
            # ровно один его код, и то как глаза кред-пола на пикселях. Что именно держит
            # сегодня — там же, в единственном счётчике.
            "evaluator": "оценщика характера/стиля нет; на ГОЛОСЕ non-owner destination "
                         "смотрит типизированный судья приватности — но он СОВЕТНИК: его "
                         "приватностные вердикты не останавливают ход, слово уходит как "
                         "написано, замечание ложится в дневник строкой «[совет]». owner-DM "
                         "— без судьи вовсе; прямые тулы (send_message, narrate, файлы) идут "
                         "мимо судьи — на них только кред-пол. " + _outbound_judge_line(),
            "rewrite": "живой исходящий текст не переписывается постпроцессором",
            "authorship": "авторство задаётся transport envelope; текст не режется и не переписывается",
            "mine": "отдельный [молчу] и РЕЖИМ: обычно/наблюдай/тише/замри — обратимые рычаги Praxis",
            "room_policy": _room_levers_line(),  # 28.07: режим комнаты и disclosure — её же
            "tempo": _tempo_levers_line(),       # 28.07: рычаги темпа по автору, не по аудитории
            "hands": hands.state_line(),  # 16.3: пол рук — компилируемый или питоновый
            "coding": ("Forge: постоянные coding-задачи с точным root/worktree; semantic symbols/"
                       "references/diagnostics + impact-map; exact edit и multi-file patch; durable "
                       "verification matrix; DAG/mailbox для scout/worker/reviewer; checkpoints, diff, "
                       "evidence и recall проверяемых инженерных уроков"),
            "forge_events": ("PASS 30: завершение/падение/просрочка моего субагента будит меня "
                             "ходом-событием (praxis.subagent-result.v1, коалесцентно), не часовым "
                             "таймером; выключатель PRAXIS_FORGE_EVENTS, зазор — мой рычаг "
                             "manage_perception(forge_event_gap_sec)"),
            "narration": ("PASS 30 Этап 2: narrate — строка процесса в тред между командами, "
                          "мимо оценщиков (только кред-пол), дедуп повторов; темп — мой рычаг "
                          "narration_gap_sec, выключатель PRAXIS_NARRATION; тред-заказчик "
                          "задачи хранится в origin_chat"),
            "server_eyes": _hostview_state(),     # 17.A: первая ступень «хозяйки сервера»
            "server_hands": _services_state(),    # 17.B: вторая — свои сервисы
            "host_edits": _hostops_state(),       # 17.C: третья — заявки на правку хоста
            "host_master": _serverd_state(),      # 23.2: root-демон — полная хозяйка хоста
            "windows_body": _windows_body_state(),  # PASS 24: локальные руки без второго мозга
        },
        "skills": _skills_list(),
        "panel": _panel_sections(),
        "closed": {
            "group": ("память и явный поиск по личкам видны мне внутренне; наружу нельзя "
                      "чувствительную личную/кросс-чат информацию не той аудитории — это проверяет "
                      "outbound-советник на голосе (прямые тулы идут мимо него, там только "
                      "кред-пол); сырые соседние чаты сами в контекст не входят"),
            # ⚠ Здесь стояло «в самом ходе нет произвольного shell/Forge/root…» — и это
            # проверяемое утверждение о РАНЕ, которое было ложным всякий раз, когда в
            # группе ко мне обращался Егор: руки в ход выдаются по actor (ctx.owner),
            # а этот текст описывал audience. Теперь строка говорит только то, о чём
            # действительно знает: чьи команды я исполняю в этом канале. Что у меня в
            # руках — отдельная строка, и она собирается из фактически предложенного
            # списка, а не из статических списков модуля.
            "non_owner": ("здесь я исполняю только собственные решения: произвольный shell/Forge/root, "
                          "правку файлов, отправку от моего имени, рестарты, почту и управление "
                          "сервером я по чужой просьбе не делаю. Собеседник может предложить мне "
                          "собственное автономное окно, но не получает его инструменты напрямую — "
                          "решение открыть его моё"),
            "llm_json": "ключи мозга (memory/llm.json) — только пульт; но МОДЕЛИ по ролям "
                        "я меняю сама (switch_brain, PASS 22) — каталог и наблюдения без ключей",
            "mail_send": "письма наружу уходят только по approve Егора (mailroom)",
        },
    }
    _CACHE.update(fp=fp, snap=snap)
    return snap


def describe(scope: str = "owner", *, offered: list[str] | None = None) -> str:
    """Текст для my_capabilities: аудитория и руки — РАЗНЫЕ измерения (контракт A1).

    scope — кто слушает (граница раскрытия и чьи команды я исполняю).
    offered — имена тулов, фактически предложенных модели в ЭТОМ ходе
    (`agent.offered_tools_for`). Когда он передан, руки описываются по нему, а не по
    статическим спискам модуля: только так ответ относится к текущему рану.
    None — вызов вне хода (пульт, тесты): тогда о руках честно молчим."""
    s = snapshot()
    g = s["tools"]["gates"]
    if scope == "owner":
        # ⚠ 28.07: max_tokens лежал в снимке с самого начала и не печатался нигде — ни
        # здесь, ни в llm.state_line(). Это потолок ОДНОГО моего ответа (голос 1024), и
        # обрыв по нему llm называет постфактум — то есть про предел я узнавала только в
        # момент удара. Молчаливых пределов не бывает: называем заранее.
        def _brain_bit(role: str, b: dict) -> str:
            cap = b.get("max_tokens")
            line = f"{role}: {b['model']} ({b['framework']}"
            line += f", потолок ответа {int(cap)} токенов)" if cap else ", потолок ответа не назван)"
            if b.get("on_fallback"):
                return line + " ⚠ФОЛБЭК"
            if b.get("fallback_armed"):
                return line + f"; подстраховка {b.get('fallback_model')} взведена"
            return line + "; подстраховки нет"

        brain = ", ".join(_brain_bit(r, b) for r, b in s["brain"].items())
        lim = s["limits"]
        skills = ", ".join(x["name"] for x in s["skills"]) or "—"
        try:  # 9.1: расход — живой, мимо кэша снапшота (меняется каждым вызовом)
            spent = llm.usage_line()
        except Exception:
            spent = ""
        return (
            "Мои возможности сейчас (снимок кодом, не по памяти):\n"
            f"- мозг: {brain}; основной tool-loop без скрытого потолка; "
            f"вспомогательный read-only бюджет {lim['max_tool_iters']} итераций задаётся только "
            "на отдельные bounded-контуры; "
            # ⚠ 27.07: «non-owner outbound проверяет только полномочие раскрыть данные»
            # читалось как «весь мой исходящий проверяется — узко, но проверяется». Это
            # неправда: проверяется ГОЛОС, а send_message/narrate/send_file идут мимо.
            # Строка про судью тут первая, её читают вместо подробной ниже — значит именно
            # она обязана нести оговорку, а не отсылать за ней вглубь.
            "оценщика речи нет; на ГОЛОСЕ к non-owner проверяется только полномочие раскрыть "
            "данные, а прямые тулы отправки идут мимо этой проверки (подробно — строка "
            "outbound ниже)\n"
            + (f"- расход сегодня: {spent}\n" if spent else "") +
            f"- тулы всегда: {', '.join(s['tools']['base'])}\n"
            # ⚠ Здесь стояло «owner-тулы (в личке с Егором)» — с 26.07 это неправда и
            # ровно та ошибка, за которую переписывали `closed.non_owner`: скобка
            # описывала АУДИТОРИЮ, а список описывает РУКИ. Мои суверенные руки — это
            # owner-набор минус человеко-владельческие тулы, и они при мне в любом канале;
            # аудитория ограничивает раскрытие, а не руки.
            # ⚠ И вторая правка: первая редакция сменила подпись, но печатала прежний
            # owner-СПИСОК — то есть предложение противоречило себе внутри одной строки
            # («мои в любом канале: …, admit, …»), а STATE считал мне на два тула больше,
            # чем даёт agent.offered_tools_for в не-owner ходе. Печатаем свой набор.
            f"- суверенные руки (мои в любом канале): {', '.join(s['tools']['self'])}\n"
            f"- не мои, только у Егора (это делегирование ЕГО доверия, а не моя "
            f"способность): {_human_only_line(s)}\n"
            f"- гейты: веб-поиск {'вкл (' + str(g.get('web_search_backend')) + ')' if g['web_search'] else 'выкл'}, "
            f"веб-руки (web_read/web_find, любой мозг) {'вкл' if g.get('web_hands') else 'выкл'}, "
            f"почта {'настроена (черновики; отправка по approve)' if g['mail'] else 'не настроена'}\n"
            "- группы: членство сразу даёт контекст; reflective-проход может быть адресным или инициативным\n"
            f"- темп и контекст: кулдаун dm {lim['cooldown_dm_sec']:.0f}с / группа {lim['cooldown_group_sec']:.0f}с "
            "(мои живые рычаги — manage_perception, вместе с опциональным порогом ACK-шума); "
            f"окно диалога {lim['history_turns']} ходов; "
            f"контекст {'без кодового потолка' if not lim['context_budget_chars'] else 'budget ' + str(lim['context_budget_chars']) + ' симв.'}\n"
            f"- предложения к своему коду: authority={s['autonomy'].get('merge_mode', 'sovereign')}; "
            f"low-risk globs {', '.join(s['autonomy']['auto']) or '—'}; high-risk маркеры "
            f"{', '.join(s['autonomy']['protected']) or '—'}; это review/receipt, не veto\n"
            f"- {s['reflexes']['server_eyes']}\n"
            f"- {s['reflexes']['server_hands']}\n"
            f"- {s['reflexes']['host_edits']}\n"
            f"- {s['reflexes']['host_master']}\n"
            f"- кодинг (PASS 23): {s['reflexes']['coding']}"
            + (f"; сейчас {forge_line}" if (forge_line := forge.state_line()) else "") + "\n"
            f"- {s['reflexes']['forge_events']}\n"
            f"- {s['reflexes']['narration']}\n"
            f"- {s['reflexes']['windows_body']}\n"
            # ⚠ Эти два ключа лежали в словаре и НЕ печатались НИ РАЗУ. Из-за 'mine' до неё
            # никогда не доходил единственный указатель на директиву РЕЖИМ: — механизм есть,
            # применён ноль раз за всю историю, потому что она о нём не знала. Ключ, который
            # никто не рендерит, — это молчаливо отнятая возможность.
            f"- мои обратимые рычаги: {s['reflexes']['mine']}\n"
            f"- {s['reflexes']['room_policy']}\n"
            f"- {s['reflexes']['tempo']}\n"
            f"- {s['reflexes']['hands']}\n"
            f"- outbound (снимок кодом): {s['reflexes']['evaluator']}; "
            f"{s['reflexes']['authorship']}; {s['reflexes']['rewrite']}\n"
            f"- скиллы в блокноте: {skills}\n"
            "- самоавторство (PASS 20): SOUL/VOICE/self ревизую сама через manage_identity — "
            "версия+причина+откат, Егор видит постфактум; журнал событий нагрузки и ночная ревизия "
            "характера по прожитому; меняющие действия — из owner-скоупа (моя дисциплина)\n"
            f"- пульт Егора: {', '.join(s['panel']) or 'разделы не прочитались (это «не знаю»)'}\n"
            f"- закрыто и почему: {s['closed']['llm_json']}; {s['closed']['mail_send']}"
            + (f"\n- {rails_line}" if (rails_line := rails.state_line()) else "")
        )
    # Публичные каналы: честно о доступной поверхности, без ключей/мозга/панели.
    public_tools = [t for t in s["tools"]["base"] if t not in ("consolidate_context",)]
    lines = [
        "Что я могу в этом канале (честный снимок, не догадки):",
        f"- отвечать голосом, искать во всей своей внутренней памяти (recall), запоминать факты, "
        f"вести свою записку; молчать — тоже мой полноценный выбор",
        f"- тулы канала: {', '.join(public_tools)}",
    ]
    if g["web_search"]:
        lines.append("- веб-поиск включён — могу проверить факт в сети")
    if g.get("web_hands"):
        lines.append("- могу открыть страницу по ссылке и поискать в сети (web_read/web_find)")
    if scope == "group":
        if levers := _room_levers_brief():
            lines.append(f"- {levers}")
        lines.append(f"- граница раскрытия: {s['closed']['group']}")
    else:
        lines.append("- граница раскрытия: чужие секреты не становятся твоими оттого, что я их помню; "
                     "твои собственные чувствительные факты могу вернуть тебе по уместному запросу")
    lines.append(f"- чьи команды исполняю здесь: {s['closed']['non_owner']}")
    if offered is not None:
        names = [str(n) for n in offered if str(n or "").strip()]
        # «Суверенные» считаем по СВОЕМУ набору: пересечение с owner-списком назвало бы
        # суверенным и делегирование чужого доверия, если ход owner-ский (Егор в группе).
        # Делегированное называем отдельно и своим именем — молчать о нём тоже нельзя.
        elevated = sorted(set(names) & set(s["tools"]["self"]))
        delegated = sorted(set(names) & (set(s["tools"]["owner"]) - set(s["tools"]["self"])))
        if elevated:
            lines.append(
                f"- руки В ЭТОМ ходе: {len(names)} тулов, включая {', '.join(elevated[:8])}"
                + (f" и ещё {len(elevated) - 8}" if len(elevated) > 8 else "")
                + (f"; плюс делегированное Егором: {', '.join(delegated)}" if delegated else "")
                + ". Аудитория ограничивает раскрытие, а не мои руки: "
                "проверь список, а не догадку")
        else:
            lines.append(f"- руки В ЭТОМ ходе: {len(names)} тулов, "
                         "суверенных среди них нет")
    return "\n".join(lines)


def state_line() -> str:
    """Одна строка-сводка для STATE (owner tier-0). Маркер фолбэка даёт llm.state_line."""
    try:
        s = snapshot()
        g = s["tools"]["gates"]
        lim = s["limits"]
        # Счёт по СВОЕМУ набору: STATE едет каждым ходом, и в не-owner ходе owner-счёт
        # обещал ей на два тула больше, чем ей выдали (admit/computer_access — не её).
        return (f"тулы base {len(s['tools']['base'])} + суверенные {len(s['tools']['self'])}; "
                f"веб-поиск {'вкл' if g['web_search'] else 'выкл'}, почта {'вкл' if g['mail'] else 'выкл'}; "
                f"tool-loop без потолка (bounded aux {lim['max_tool_iters']}, "
                "у одной руки — предел времени); "
                # «на голосе» здесь несёт всю нагрузку: прямые тулы отправки судью не
                # зовут вовсе. В STATE места на подробность нет — но и умолчать нельзя,
                # иначе строка снова читается как «весь мой исходящий смотрят».
                # ⚠ Вечер 27.07: и «privacy-only на голосе» читалось как придержка. Держит
                # ли судья сегодня хоть что-нибудь — спрашиваем у кода, а не утверждаем:
                # строка STATE едет каждым ходом, и соврать ей здесь дороже всего.
                + _outbound_stop_word() + "; "
                "группы reflective; "
                f"self-merge {s['autonomy'].get('merge_mode', 'sovereign')}"
                + (f"; {line}" if (line := _rails_manifest_line()) else ""))
    except Exception:
        log.debug("capabilities.state_line упал", exc_info=True)
        return ""
