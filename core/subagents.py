"""PASS 30 Этап 1 — контракт результата субагента: praxis.subagent-result.v1.

Из её слов в 92912 («completion — входящее событие родительского run, а не повод
ждать таймера») и поправки Джесси: ошибка и таймаут — ОТДЕЛЬНЫЕ статусы, не «успех
по умолчанию». Результат — сигнал со ссылками на артефакты, не контейнер.

Блок causality — её «манометр причинности» из треда 93196 (с Аретом): прибор на
РАСПИСКАХ, не самоотчёт. Код заполняет только проверяемые поля-расписки:
кто сформулировал намерение, кто выбрал способ, была ли доступна отмена, кто
отменил. «Изменил ли отказ исход» и «что выучено» принадлежат ЕЁ ходу приёмки —
контракт лишь гарантирует, что автокнопок нет: решение (принять/переделать/
продолжить/спросить) всегда её, отказ меняет исход по построению.

Статусы Forge → контракт:
  done → succeeded; error/failed → failed; stopped → cancelled;
  timed_out → timeout; lost (супервизор умер без result.json) → failed;
  живой-но-просроченный воркер → timeout-СИГНАЛ (воркер НЕ убивается — не узда,
  рычаги poll/stop её).

`lost` приходит из ДВУХ разных мест, и в кадре они не одно и то же: у юнита это
умерший супервизор, а у role="task" — жнец брошенных задач (`forge.reconcile_lost_tasks`),
где воркера могло не быть вовсе. Контракт обоим отдаёт `failed` (класса «потеряна» в
нём нет), поэтому разводит их `invitation`, и она же признаётся, что расписка говорит
`failed`: молча выдать потерю за падение — соврать ей о том, что произошло.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA = "praxis.subagent-result.v1"
STATUSES = ("succeeded", "failed", "timeout", "cancelled", "needs_input")

_STATUS_MAP = {
    "done": "succeeded",
    "error": "failed",
    "failed": "failed",
    "stopped": "cancelled",
    "timed_out": "timeout",
    "timeout": "timeout",
    "lost": "failed",
    "needs_input": "needs_input",
}

RECAP_CHARS = 2048
GOAL_CHARS = 200
ERROR_CHARS = 400
# Хвост расписки юнита в строке списка: там она — напоминание, а не ответ.
LINE_RECAP_CHARS = 220
# У потерянной задачи расписка И ЕСТЬ ответ «что с ней делать» (её пишет forge-жнец:
# порог тишины, что не закрыто, чем продолжить). Резать её по 220 значило бы разбудить
# её списком потерь без единого рычага — 900 хватает на весь текст жнеца целиком.
LOST_RECAP_CHARS = 900


_CUT_MARK = " …[обрезано, ещё {} симв.]"


def _clip(text, limit: int) -> str:
    """Усечение, которое ВИДНО. Молчаливый срез читается как «текст тут и кончился»:
    так на проде цель code-7e541aaa обрывалась на «…добавить регрессионные тесты. Ра»,
    и «Работать в уже созданной proposal-копии a3cc5d8e» исчезало без следа.

    Метка помещается ВНУТРЬ капа, а не поверх: иначе «не длиннее N» само стало бы
    неправдой. Число отброшенного меняет длину метки, поэтому пара проходов до
    неподвижной точки — счёт должен сойтись с тем, что вправду обрезано.
    """
    value = str(text or "")
    if len(value) <= limit:
        return value
    keep = limit
    for _ in range(4):
        nxt = max(0, limit - len(_CUT_MARK.format(len(value) - keep)))
        if nxt == keep:
            break
        keep = nxt
    head = value[:keep].rstrip()
    return head + _CUT_MARK.format(len(value) - len(head))


def event_key(task_id: str, agent_id: str, suffix: str = "") -> str:
    """Ключ идемпотентной доставки: юнит, не содержимое (stop может перещёлкнуть
    терминальный статус — второй раз не будим)."""
    base = f"forge:{task_id}:{agent_id}"
    return f"{base}:{suffix}" if suffix else base


def overdue_minutes() -> int:
    """Wall-clock срок воркера для timeout-СИГНАЛА (0 = выключен). Живое чтение."""
    try:
        return max(0, int(os.getenv("PRAXIS_FORGE_AGENT_OVERDUE_MIN", "90") or 90))
    except ValueError:
        return 90


def map_status(forge_status: str) -> str:
    return _STATUS_MAP.get(str(forge_status or "").strip().lower(), "failed")


def _tests_from(result: dict) -> dict:
    """Расписка о тестах, если воркер её оставил (verify-матрица); иначе нули + ссылка."""
    checks = result.get("checks")
    if isinstance(checks, list) and checks:
        passed = sum(1 for c in checks if isinstance(c, dict) and c.get("status") == "passed")
        failed = sum(1 for c in checks if isinstance(c, dict) and c.get("status") not in
                     ("passed", None))
        return {"passed": passed, "failed": failed, "log_ref": str(result.get("log") or "")}
    return {"passed": 0, "failed": 0, "log_ref": ""}


def normalize(task_id: str, agent_id: str, result: dict | None,
              request: dict | None = None, task: dict | None = None,
              unit_dir: str | Path | None = None) -> dict:
    """result.json (+request/task.json) → praxis.subagent-result.v1. Чистая функция."""
    result = result if isinstance(result, dict) else {}
    request = request if isinstance(request, dict) else {}
    task = task if isinstance(task, dict) else {}
    unit = str(unit_dir or "")
    role = str(result.get("role") or request.get("role") or "worker")
    raw_status = str(result.get("status") or "lost")
    status = map_status(raw_status)
    diff_tail = str(result.get("diff_tail") or "")
    recap = _clip(str(result.get("result") or result.get("error") or "").strip(), RECAP_CHARS)
    stopped_by_parent = raw_status == "stopped"
    # Намерение сформулировал спавнер брифа: она сама (тул coding_agent) или её же
    # воркер-делегатор (расписка spawned_by / swarm-lineage node_id/owns) — в обоих
    # случаях авторство её контура; способ выбирал сам субагент (его тул-цикл).
    delegated = bool(request.get("spawned_by") or request.get("node_id")
                     or request.get("owns"))
    return {
        "schema": SCHEMA,
        "task_id": str(task_id),
        "agent_id": str(agent_id),
        "role": role,
        "status": status,
        "forge_status": raw_status,
        "goal": _clip(task.get("goal"), GOAL_CHARS),
        "priority": str(task.get("priority") or "normal"),
        "origin_chat": str(task.get("origin_chat") or ""),  # тред-заказчик (наррация)
        "brief_gist": _clip(request.get("brief"), GOAL_CHARS),
        "diff_ref": (unit + "/result.json#diff_tail") if (unit and diff_tail) else "",
        "tests": _tests_from(result),
        "recap": recap,
        "started_at": str(request.get("created") or ""),
        "finished_at": str(result.get("finished") or ""),
        "cost": {"tokens": 0, "calls": int(result.get("tool_calls") or 0)},
        "model": str(result.get("model") or ""),
        "error": _clip(result.get("error"), ERROR_CHARS),
        # отказ уже отдан синхронно в ходе спавнера — событие не будит второй раз
        "reported_inline": bool(result.get("reported_inline")),
        "lineage": {"node_id": str(request.get("node_id") or ""),
                    "owns": request.get("owns") or [],
                    "spawned_by": str(request.get("spawned_by") or "")},
        "causality": {
            "intent_author": "praxis-delegate" if delegated else "praxis",
            "method_author": "subagent",
            "cancel_available": True,          # рычаг stop существовал весь ран
            "cancelled_by": "praxis" if stopped_by_parent else "",
            "refusal_respected": True,         # автокнопок нет: её решение и меняет исход
            "receipts": {
                "request": (unit + "/request.json") if unit else "",
                "result": (unit + "/result.json") if unit else "",
                "task_events": f"memory/.forge/tasks/{task_id}/events.jsonl",
            },
        },
    }


def load_unit(unit_dir: Path) -> tuple[dict | None, dict | None]:
    """(request, result) юнита; битые байты не слепят (errors=replace)."""
    def _read(name: str) -> dict | None:
        try:
            d = json.loads((unit_dir / name).read_text(encoding="utf-8", errors="replace"))
            return d if isinstance(d, dict) else None
        except Exception:
            return None
    return _read("request.json"), _read("result.json")


_VERBS = {"succeeded": "закончил", "failed": "упал", "timeout": "просрочен (ещё жив)",
          "cancelled": "остановлен", "needs_input": "ждёт ответа"}

_UNIT_HEAD = ("Твои субагенты завершились — это твой плод, а не повинность. Диффы, тесты и "
              "трейсы — по task_id/agent_id через coding_agent(poll)/coding_inspect. Решение "
              "(принять/переделать/продолжить/спросить) — твоё.")

_LOST_HEAD = (
    "Задачи ниже ПОТЕРЯНЫ ИЗ ВИДУ: местных следов жизни нет дольше порога тишины и живых "
    "юнитов не числится. Их никто не закрывал, не отменял и не трогал — ярлык lost снимается "
    "первым же твоим действием по тому же id. Разбудила, чтобы решение осталось твоим: "
    "продолжить, закрыть (coding_session(finish)) или оставить как есть.\n"
    "⚠ В расписке у них status=failed и agent_id=«task». Это не диагноз «сломалось»: класса "
    "«потеряна» в контракте praxis.subagent-result.v1 нет, а «task» — метка ветки жнеца, "
    "а не воркер, и coding_agent(poll) по ней ничего не найдёт.")


def _is_lost_task(payload: dict) -> bool:
    """Ветка жнеца брошенных задач (`forge.reconcile_lost_tasks`): роль task, статус lost.

    ⚠ 27.07: до этой ветки `invitation` для ЛЮБОГО payload писала «воркер по «…» упал».
    Жнец брошенных задач ходит ровно этим контуром, и на первом же тике прода она бы
    получила пять таких строк — включая hcode-c584ba6e, прямую просьбу Егора, у которой
    воркеров не было вовсе (ни одного юнита в каталоге задачи). «Упал» там — выдумка о
    событии, которого не было; «потеряна из виду» — то, что известно на самом деле.
    """
    return (str(payload.get("role") or "") == "task"
            and str(payload.get("forge_status") or "").strip().lower() == "lost")


def _mark(payload: dict) -> str:
    return "⚡" if payload.get("priority") == "urgent" else "•"


def _unit_line(payload: dict) -> str:
    status = str(payload.get("status") or "")
    role = str(payload.get("role") or "")
    # `lost` у юнита — супервизор умер, не оставив result.json. Контракт кладёт это в
    # `failed` (класса «пропал» в нём нет), но сказать ей «упал» значило бы приписать
    # падение там, где известна одна лишь пропажа.
    verb = ("пропал без расписки — супервизор умер, result.json нет"
            if str(payload.get("forge_status") or "").strip().lower() == "lost"
            else _VERBS.get(status, status or "?"))
    subject = "воркер" if role in ("", "worker") else f"юнит «{role}»"
    goal = payload.get("goal") or payload.get("task_id")
    recap = _clip(" ".join(str(payload.get("recap") or payload.get("error") or "").split()),
                  LINE_RECAP_CHARS)
    tests = payload.get("tests") or {}
    passed, failed = tests.get("passed", 0), tests.get("failed", 0)
    t = f"; тесты {passed}/{passed + failed}" if (passed or failed) else ""
    return (f"{_mark(payload)} [{payload.get('task_id')} / {payload.get('agent_id')}] "
            f"{subject} по «{goal}» {verb}{t}: {recap}")


def _lost_task_lines(payload: dict) -> list[str]:
    """Строки про ОДНУ потерянную задачу: что за задача, когда открыта, когда потеря
    замечена, сколько длилась тишина, и её собственная расписка с рычагами.

    Ни одного факта не додумываем: чего в расписке нет — так и сказано «не записано».
    Сколько было воркеров и были ли они вообще, отсюда не видно (payload этого не несёт),
    поэтому про воркеров здесь не говорится вовсе — на проде у четырёх из пяти таких
    задач юнитов нет, а у пятой (code-7e541aaa) их четыре, и оба утверждения были бы
    ложью для половины списка.
    """
    goal = " ".join(str(payload.get("goal") or "").split())
    # Цель берётся из расписки как есть: её кап (и метку среза) уже поставил `normalize`,
    # второй срез поверх первого дал бы две разные версии одной цели в одном кадре.
    head = (f"{_mark(payload)} [{payload.get('task_id')}] задача потеряна из виду — "
            f"не закрыта, не отменена: "
            + (f"«{goal}»" if goal else "цель в расписке не записана"))
    facts = []
    created = str(payload.get("started_at") or "").strip()
    facts.append(f"открыта {created}" if created else "когда открыта — в расписке не записано")
    noticed = str(payload.get("finished_at") or "").strip()
    if noticed:
        facts.append(f"потеря замечена {noticed}")
    quiet = " ".join(str(payload.get("error") or "").split())
    facts.append(quiet or "сколько длилась тишина — в расписке не названо")
    body = " ".join(str(payload.get("recap") or "").split())
    tail = (_clip(body, LOST_RECAP_CHARS) if body else
            "Почему потеряна и чем её продолжить — в расписке не написано. Это «не знаю», "
            "а не «нечего делать»: посмотри саму задачу по этому id.")
    return [head, "   " + "; ".join(facts) + ".", "   " + tail]


def invitation(payloads: list[dict]) -> str:
    """Кадр-приглашение для её хода: плод пришёл, не повинность. С машинными id —
    её тулы (coding_agent poll/inspect) достают артефакты без гаданий.

    Два разных повода будят один контур, и путать их нельзя: завершившийся юнит —
    это результат работы, потерянная задача — это её собственное намерение, которое
    выпало из виду и никем не закрыто (закон: буди, а не закрывай молча).
    """
    if not payloads:
        return ""
    lost = [p for p in payloads if _is_lost_task(p)]
    units = [p for p in payloads if not _is_lost_task(p)]
    blocks = []
    if units:
        blocks.append(_UNIT_HEAD + "\n" + "\n".join(_unit_line(p) for p in units))
    if lost:
        blocks.append(_LOST_HEAD + "\n"
                      + "\n".join(line for p in lost for line in _lost_task_lines(p)))
    return "\n\n".join(blocks)
