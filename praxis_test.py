"""Hermetic unittest entrypoint for a checkout that may sit beside live memory.

Usage mirrors unittest's CLI::

    python praxis_test.py -q test_turns test_heartbeat
    python praxis_test.py discover -q

The disposable Praxis base is selected before unittest imports a test module, and
the checkout's own `memory/`/`soul/`/`workspace/` are fenced off for the rest of
the process — see `_sandbox` for the 06.07.2026 incident both layers answer for.
A bare `python -m unittest …` no longer bypasses this (`_sandbox` self-arms on
import), but this entrypoint is still the only one that gets the redirection in
*before* the first module computes its BASE.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["PRAXIS_TEST"] = "1"

import _sandbox

TEST_BASE = _sandbox.activate_if_testing()
if not TEST_BASE:  # pragma: no cover - a broken isolation boundary must be loud
    raise RuntimeError("Praxis test sandbox did not activate")

# Не «переменная поставлена», а «база действительно другая»: 06.07 прогон шёл по
# /app, где чекаут и её живая база — один и тот же каталог, и молчаливой разницы
# между ними не было ни в одном признаке.
_REPO = Path(__file__).resolve().parent
if Path(TEST_BASE).resolve() == _REPO:
    raise RuntimeError(f"PRAXIS_BASE указывает на сам чекаут ({_REPO}) — изоляции нет")
if not _sandbox.install_live_tree_guard():  # pragma: no cover - see above
    raise RuntimeError("забор живого дерева не встал — прогон отменён")

# ⚠ 28.07. `mtproto_runner` строит клиент Telegram НА УРОВНЕ МОДУЛЯ, и без ключей импорт
# падает. Держалось это на случайности: несколько тест-модулей ставили заглушку каждый для
# себя (`test_agent_reaper`, `test_runner_reconnect` и другие), а `unittest` импортирует по
# алфавиту — и `test_agent_reaper` успевал раньше `test_multimodal`. Зависимость держалась
# на порядке букв, нигде не была записана и разваливалась, как только модули разъезжались
# по процессам: сам по себе `test_multimodal` давал 1 тест (ошибка импорта) вместо 48.
# Заглушка живёт здесь — в единственном входе, который и так готовит песочницу.
# `setdefault`: настоящее окружение, если оно есть, сильнее.
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("TELEGRAM_SESSION",
                      str(Path(TEST_BASE) / "praxis_test_session"))

# Прививка среды: стенд ОБЪЯВЛЯЕТ конфигурацию, а не наследует её.
# 03.08.2026 вердикт гейта двигали переменные прода: почтовые креды выдавали
# владельцу руку сверх объявленного множества, а PRAXIS_SLEEP_WINDOW делал часовой
# пульс неподъёмным — гейт краснел каждую ночь два часа подряд и закрывал ей дверь
# Форжа. Правило и обе его стороны — в _standenv.
import _standenv

DROPPED_ENV = _standenv.sanitize()
_standenv.install_leak_detector()

if __name__ == "__main__":
    import unittest

    print(f"praxis_test: PRAXIS_BASE={TEST_BASE}; живое дерево "
          f"{_REPO}/{{{','.join(_sandbox.LIVE_SUBTREES)}}} закрыто на запись",
          file=sys.stderr)
    print(f"praxis_test: среда объявлена — снято {len(DROPPED_ENV)} PRAXIS_*-переменных прода, "
          f"закреплено {len(_standenv.ENV_PIN)}", file=sys.stderr)
    unittest.main(module=None)
