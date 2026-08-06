"""Среда стенда ОБЪЯВЛЯЕТСЯ, а не наследуется.

⚠ 03.08.2026. Вердикт гейта двигала среда, а не код. Два случая пойманы живьём:

  • почтовые креды лежали в окружении контейнера → `capabilities`/`agent` выдавали
    владельцу `send_email`, и тест, кодирующий решение Егора от 26.07 («набор рук не
    зависит от того, кто заговорил»), покраснел в день, когда на сервере включили почту;

  • `PRAXIS_SLEEP_WINDOW` (по умолчанию 4–6 по её часам) → часовой пульс не поднимался,
    и тест ждал события, которого в окне быть не может. Гейт был красным КАЖДУЮ ночь
    два часа подряд.

Дверь Форжа судит по коду возврата, поэтому такая краснота не косметика: она закрывает
ей мёрж собственного кода. А симметричный случай страшнее — гейт может ПОЗЕЛЕНЕТЬ на
сломанном коде, если конфигурация сложится удачно.

Отсюда правило: **базис известен и пуст**, а тест, которому гейт важен, включает его САМ
и проверяет обе стороны. «Просто выключить всё» было бы ошибкой: тогда стенд перестал бы
покрывать конфигурацию, в которой прод реально живёт, — поэтому обе стороны, а не одна.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

#: Переменные, которые НЕ являются тюнингом прода: их ставит песочница или сам
#: ОБРАЗ (Dockerfile ENV). Снять такую — значит подменить тело, с которым живёт
#: контейнер, и проверять не то, что поедет в бой.
ENV_KEEP = frozenset({
    "PRAXIS_BASE",
    "PRAXIS_TEST",
    # ⚠ 03.08.2026, урок этой же прививки. PRAXIS_HANDS ставит Dockerfile
    # (ENV PRAXIS_HANDS=/usr/local/bin/praxis-hands), а не .env. Первая редакция
    # сняла его вместе со всем остальным → hands.available() стал False →
    # setUpClass пропустил ВЕСЬ класс TestWorkshopHands: двадцать пять тестов про
    # её файловый пол (shrink-guard, побег из дома, креды, квитанции, отпечаток
    # рельсов бинаря). Гейт при этом отрапортовал OK.
    #
    # Это ровно тот симметричный случай, ради которого прививка и делалась:
    # опасна не краснота, а ЗЕЛЁНОЕ при исчезнувшем покрытии. Ловится он не
    # тестом на поведение, а сравнением состава прогона — см. тесты ниже.
    "PRAXIS_HANDS",
})

#: Значения, которые стенд ЗАЯВЛЯЕТ. Не «как на проде» и не «как повезёт» — фиксировано.
ENV_PIN = {
    # Часы: и её пояс, и пояс процесса. Пока пояс наследовался, тесты про сутки,
    # тихие часы и окно сна зависели от того, в какую минуту их запустили.
    "PRAXIS_TZ": "Europe/Samara",
    "PRAXIS_TZ_OFFSET_H": "4",
    "TZ": "UTC",
    # Стенд не ходит в сеть за весами моделей.
    "PRAXIS_STT_LOCAL_FILES_ONLY": "1",
    # Владелец: несколько модулей читают его НА ИМПОРТЕ, поэтому объявить его должен
    # стенд. Прежде `test_telegram_membership` писал его в общий процесс на своём
    # импорте — а discover импортирует ВСЕ модули до первого теста, так что значение
    # доставалось и тем, кто про него не знал. Заодно настоящий telegram-id Егора
    # больше не попадает в прогон.
    "PRAXIS_OWNER_ID": "101",
}


def _derived_pins(env) -> dict:
    """Пины, зависящие от каталога песочницы, — считаются, а не пишутся руками.

    ⚠ 03.08.2026, сетевой аудит. Оба адреса ниже — не PRAXIS_*-тюнинг, а ВЫХОДЫ
    НАРУЖУ, которые стенд открывал молча.

    • PRAXIS_SERVERD_RUN. Переменной в окружении нет вовсе, `serverd_client`
      замораживает RUN_DIR на импорте и берёт дефолт /run/praxis-serverd — а он в
      контейнере СМОНТИРОВАН. Итог: `rails.registry()` и `capabilities.snapshot()`
      делали живой admin.status к root-демону хоста, около трёхсот вызовов за прогон,
      по 8 секунд срока. Вердикт двигало не поведение, а время: гейт краснел как
      «тесты не уложились», не называя причины. Уводим адрес в песочницу —
      `available()` честно False.
      Почему пин, а НЕ `if PRAXIS_TEST: return False` внутри клиента: тесты, которые
      проверяют serverd НАМЕРЕННО, патчат SOCK/TOKEN_FILE у себя и пин переживают, а
      забор внутри клиента сломал бы именно их.

    • TELEGRAM_SESSION. Вход стенда ставил её через setdefault — значит значение
      прода побеждало. А `mtproto_runner` строит TelegramClient НА УРОВНЕ МОДУЛЯ:
      SQLiteSession открывает её боевой файл сессии и держит auth-ключ в тест-процессе.
      По проводу ничего не уходило, но трогать её живую личность стенду нечего.
    """
    import tempfile

    base = Path(env.get("PRAXIS_BASE") or tempfile.gettempdir())
    return {
        "PRAXIS_SERVERD_RUN": str(base / "no-serverd"),
        "TELEGRAM_SESSION": str(base / "praxis_test_session"),
    }


def sanitize(environ: dict | None = None) -> list[str]:
    """Снять все PRAXIS_*, кроме нужных песочнице, и проставить заявленные значения.

    Возвращает имена снятых переменных — чтобы прогон мог сказать вслух, от чего он
    отвязался, а не молча изменил поведение.
    """
    env = os.environ if environ is None else environ
    dropped = sorted(name for name in list(env)
                     if name.startswith("PRAXIS_")
                     and not name.startswith("PRAXIS_TEST_")
                     and name not in ENV_KEEP
                     and name not in ENV_PIN)
    for name in dropped:
        del env[name]
    env.update(ENV_PIN)
    env.update(_derived_pins(env))
    return dropped


def door_modules(source: Path) -> list[str]:
    """Из чего состоит вход стенда: он сам и всё, что он импортирует локально.

    Читается из ЖИВОГО файла, а не перечисляется руками: список, набранный по
    памяти, однажды отстанет — 03.08.2026 так и вышло, когда появился этот модуль,
    и девять тестов про её право на merge покраснели по причине, не имеющей
    отношения к её коду.
    """
    text = (Path(source) / "praxis_test.py").read_text(encoding="utf-8")
    return ["praxis_test.py"] + [f"{name}.py"
                                 for name in re.findall(r"^import (_\w+)", text, re.M)]


def copy_door(source: Path, dest: Path) -> list[str]:
    """Поставить в тестовое репо ту же дверь, что у живого, и убедиться в этом."""
    names = door_modules(source)
    for name in names:
        shutil.copy2(Path(source) / name, Path(dest) / name)
    missing = [name for name in names if not (Path(dest) / name).is_file()]
    if missing:
        raise AssertionError(f"дверь скопирована неполно: {missing}")
    return names


_LEAK_HINT = ("Задавай состояние явно (patch.dict / setUp+tearDown): всё, что тест "
              "оставил в окружении, наследуют соседи по прогону, и падение уезжает "
              "к тому, кто ни при чём.")


def install_leak_detector() -> bool:
    """Тест, изменивший окружение и не вернувший его, падает сам — а не роняет соседа.

    Возвращает False, если детектор уже стоял: двойная обёртка дала бы двойной отчёт.
    """
    import unittest

    if getattr(unittest.TestCase.run, "__praxis_env_guard__", False):
        return False

    original = unittest.TestCase.run

    def run(self, result=None):  # noqa: ANN001 - подпись unittest
        before = dict(os.environ)
        try:  # журналу сети нужно имя теста, а знает его только эта обёртка
            import _netwatch

            _netwatch.CURRENT_TEST = self.id()
        except Exception:
            pass
        outcome = original(self, result)
        res = result if result is not None else outcome
        after = dict(os.environ)
        if after != before:
            names = sorted(set(before) ^ set(after)) or sorted(
                key for key in before if before[key] != after.get(key))
            os.environ.clear()
            os.environ.update(before)
            try:
                raise AssertionError(
                    "тест изменил окружение и не вернул его: "
                    + ", ".join(names) + ". " + _LEAK_HINT)
            except AssertionError:
                if res is not None and hasattr(res, "addFailure"):
                    res.addFailure(self, sys.exc_info())
        return outcome

    run.__praxis_env_guard__ = True
    run.__wrapped__ = original
    unittest.TestCase.run = run
    return True
