"""Раскладка гейта по процессам: тот же набор тестов, но не в одну очередь.

Зачем. Полный прогон — 2822 теста, и unittest гонит их СТРОГО последовательно: на
четырёхъядерной машине занято 20% процессора, шесть минут стены. За сегодняшние сутки
гейт гонялся полтора десятка раз; это часы ожидания на ровном месте.

Как. Модули раскладываются по N процессам, каждый со своей одноразовой базой
(`_sandbox.activate_if_testing` уже даёт её на процесс — суффикс случайный, проверено).
Процесс на ШАРД, а не на модуль: импорт `agent`/`llm`/`telethon` стоит секунды, и платить
его на каждый модуль дороже, чем выиграть на параллели.

⚠ Главное, чего здесь боимся. Раскладка меняет то, КТО С КЕМ делит процесс, — а значит
может и вскрыть, и спрятать межтестовую протечку. Сегодня такая уже ловилась дважды
(`test_serverd_timeout` вешал `addCleanup` на незапущенный patcher и течь шла в соседей;
`test_rails_truth` не возвращал `core.subagents` в `sys.modules`). Поэтому здесь есть
`--verify`: прогнать последовательно и параллельно и сравнить МНОЖЕСТВА падений. Если они
разошлись — это находка, а не повод «ну оно же зелёное».

Балансировка. После каждого прогона длительности модулей пишутся в
`.praxis_test_durations.json`, и следующая раскладка ведёт по ним «жадным» разложением
(самые долгие — первыми, каждый в самый свободный шард). Первый прогон идёт вслепую,
поровну по числу модулей.

Запуск:
    python praxis_test_parallel.py                 # столько процессов, сколько ядер минус один
    python praxis_test_parallel.py --jobs 4
    python praxis_test_parallel.py --verify        # + контрольный последовательный прогон
    python praxis_test_parallel.py test_turns test_rails_truth
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent
DURATIONS = REPO / ".praxis_test_durations.json"
RUNNER = REPO / "praxis_test.py"

# «Ran 2822 tests in 370.123s» и хвост «FAILED (failures=1, skipped=1)» / «OK».
_RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.M)
_OUTCOME = re.compile(r"^(OK|FAILED)(?:\s+\((.*)\))?\s*$", re.M)
_FAILNAME = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.M)


def discover_modules() -> list[str]:
    """Имена тест-модулей — те же, что взял бы `discover`."""
    return sorted(
        p.stem for p in REPO.glob("test_*.py")
        if p.is_file() and not p.name.startswith("test__")
    )


def _load_durations() -> dict[str, float]:
    try:
        return json.loads(DURATIONS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def shard(modules: list[str], jobs: int) -> list[list[str]]:
    """Жадная раскладка по известным длительностям: длинные первыми, каждый — в самый лёгкий шард.

    Без истории раскладка идёт поровну по счёту — это заведомо хуже, но честно: первый
    прогон и не может знать, что `test_pass23_2` идёт минуту, а `test_turns` секунду.
    """
    known = _load_durations()
    ordered = sorted(modules, key=lambda m: -known.get(m, 0.0))
    buckets: list[list[str]] = [[] for _ in range(jobs)]
    weights = [0.0] * jobs
    for name in ordered:
        i = weights.index(min(weights))
        buckets[i].append(name)
        # Неизвестному модулю даём средний вес, иначе все безвестные слипнутся в один шард.
        weights[i] += known.get(name, (sum(known.values()) / len(known)) if known else 1.0)
    return [b for b in buckets if b]


def run_shard_here(names: list[str]) -> int:
    """Дочерний вход: собрать набор из перечисленных модулей и прогнать.

    ⚠ Здесь стоял `praxis_test.py -q <модули>`, и это была ошибка с ценой в 750 тестов.
    `unittest` при перечислении ИМЁН роняет ВЕСЬ прогон на первой же ошибке импорта, а
    `discover` записывает её одним падением и идёт дальше. `test_multimodal` импортирует
    `mtproto_runner`, тот на уровне модуля строит клиент Telegram — и в дочернем процессе
    без ключей шард умирал целиком, отчитываясь нулём тестов. Суммарное число молча
    падало с 2891 до 2139, а «падений стало меньше» читалось как хорошая новость.
    Повторяем поведение discover: плохой импорт — это ОДНО падение, а не немота шарда.
    """
    import praxis_test  # noqa: F401 — импорт активирует песочницу ДО импорта тестов
    import unittest

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in names:
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except Exception as exc:  # noqa: BLE001 — любой отказ импорта равен падению теста
            message = f"{type(exc).__name__}: {exc}"

            class _ImportFailure(unittest.TestCase):
                pass

            def _fail(self, _name=name, _msg=message):
                self.fail(f"модуль {_name} не импортировался: {_msg}")

            setattr(_ImportFailure, f"test_import_{name}", _fail)
            _ImportFailure.__qualname__ = _ImportFailure.__name__ = name
            suite.addTest(_ImportFailure(f"test_import_{name}"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def run_shard(names: list[str], index: int) -> dict:
    started = time.monotonic()
    env = dict(os.environ)
    # Каждому процессу — своя одноразовая база. PRAXIS_BASE намеренно НЕ ставим:
    # _sandbox игнорирует унаследованный и заводит свой, а пиннинг сделал бы шарды
    # соседями по одному каталогу — ровно то, от чего изоляция и защищает.
    env.pop("PRAXIS_BASE", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--run-shard", ",".join(names)],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    ran = _RAN.search(blob)
    outcome = _OUTCOME.search(blob)
    return {
        "index": index,
        "modules": names,
        "tests": int(ran.group(1)) if ran else 0,
        "seconds": round(time.monotonic() - started, 1),
        "ok": bool(outcome and outcome.group(1) == "OK"),
        "detail": outcome.group(2) if (outcome and outcome.group(2)) else "",
        "failures": sorted(set(_FAILNAME.findall(blob))),
        "returncode": proc.returncode,
        "blob": blob,
    }


def run_serial(names: list[str]) -> dict:
    return run_shard(names, -1)


def _save_durations(results: list[dict]) -> None:
    """Длительность шарда делим по модулям поровну — грубо, но сходится за пару прогонов."""
    known = _load_durations()
    for row in results:
        if not row["modules"]:
            continue
        share = row["seconds"] / len(row["modules"])
        for name in row["modules"]:
            known[name] = round((known.get(name, share) + share) / 2, 2)
    try:
        DURATIONS.write_text(json.dumps(known, ensure_ascii=False, indent=1, sort_keys=True),
                             encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    if "--run-shard" in sys.argv:
        return run_shard_here(sys.argv[sys.argv.index("--run-shard") + 1].split(","))

    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("modules", nargs="*", help="тест-модули; пусто — все")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--verify", action="store_true",
                    help="прогнать ЕЩЁ и последовательно, сравнить множества падений")
    args = ap.parse_args()

    modules = args.modules or discover_modules()
    jobs = max(1, min(args.jobs, len(modules)))
    buckets = shard(modules, jobs)

    print(f"модулей {len(modules)}, процессов {len(buckets)}", file=sys.stderr)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(buckets)) as pool:
        results = list(pool.map(lambda pair: run_shard(*pair),
                                [(names, i) for i, names in enumerate(buckets)]))
    wall = time.monotonic() - started
    _save_durations(results)

    total = sum(r["tests"] for r in results)
    failures = sorted({f for r in results for f in r["failures"]})
    print()
    for r in sorted(results, key=lambda x: -x["seconds"]):
        mark = "OK " if r["ok"] else "КРАСНЫЙ"
        print(f"  шард {r['index']}: {r['tests']:>4} тестов за {r['seconds']:>6.1f}с  {mark}"
              f"  ({len(r['modules'])} модулей, дольше всех: {r['modules'][0] if r['modules'] else '—'})")
    print(f"\nВСЕГО {total} тестов за {wall:.1f}с стены")
    if failures:
        print(f"ПАДЕНИЙ {len(failures)}:")
        for name in failures:
            print("  ", name)
    else:
        print("падений нет")

    # Сумма по шардам обязана сойтись с числом модулей: молчаливо потерянный шард
    # выглядел бы как «стало меньше падений».
    covered = sum(len(r["modules"]) for r in results)
    if covered != len(modules):
        print(f"⚠ ПОКРЫТО {covered} модулей из {len(modules)} — раскладка потеряла часть",
              file=sys.stderr)
        return 2
    mute = [r for r in results if not r["tests"] and r["modules"]]
    if mute:
        # ⚠ Немой шард ОПАСНЕЕ красного: его тесты не выполнились, а итог выглядит
        # как «меньше падений». Именно так 28.07 потерялись 750 тестов и вылезло
        # мнимое «множества разошлись». Это отказ прогона, а не заметка в логе.
        for r in mute:
            print(f"\n⚠ ШАРД {r['index']} НЕ ВЫПОЛНИЛ НИ ОДНОГО ТЕСТА "
                  f"({len(r['modules'])} модулей, код {r['returncode']}):", file=sys.stderr)
            print(r["blob"][-1200:], file=sys.stderr)
        print("ПРОГОН НЕДЕЙСТВИТЕЛЕН: часть набора не выполнялась", file=sys.stderr)
        return 2

    if args.verify:
        print("\n— контрольный последовательный прогон —", file=sys.stderr)
        ser = run_serial(modules)
        same = set(ser["failures"]) == set(failures)
        print(f"последовательно: {ser['tests']} тестов за {ser['seconds']}с, "
              f"падений {len(ser['failures'])}")
        if not same:
            only_par = sorted(set(failures) - set(ser["failures"]))
            only_ser = sorted(set(ser["failures"]) - set(failures))
            print("⚠ МНОЖЕСТВА ПАДЕНИЙ РАЗОШЛИСЬ — это находка, а не шум:")
            if only_par:
                print("  падает ТОЛЬКО параллельно (шарды поссорились):", *only_par)
            if only_ser:
                print("  падает ТОЛЬКО последовательно (соседство по процессу прячет "
                      "или создаёт дефект):", *only_ser)
            return 3
        if ser["tests"] != total:
            print(f"⚠ РАЗНОЕ ЧИСЛО ТЕСТОВ: параллельно {total}, последовательно {ser['tests']}")
            return 3
        print(f"сошлось: то же число тестов и те же падения; "
              f"ускорение {ser['seconds'] / max(wall, 0.1):.1f}×")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
