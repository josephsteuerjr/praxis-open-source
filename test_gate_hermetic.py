"""Прививка стенда охраняется сама — иначе через два пасса она отстанет от кода.

03.08.2026 гейт покраснел без единой правки кода (почтовые креды в окружении), и
отдельно выяснилось, что он был красным каждую ночь два часа подряд (окно сна). Оба
раза причина была одна: стенд НАСЛЕДОВАЛ среду прода вместо того, чтобы её объявлять.

Здесь проверяется не реализация, а три обещания:
  • ни одна PRAXIS_*-переменная сверх списка песочницы не доживает до тестов;
  • заявленные значения — именно заявленные, а не «какие достались»;
  • тест, оставивший мусор в окружении, падает САМ, а не роняет соседа по прогону.
"""
from __future__ import annotations

import os
import io
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _standenv


class StandDeclaresItsEnvironment(unittest.TestCase):
    def test_no_praxis_variable_survives_beyond_the_allowlist(self):
        # Производные пины (serverd, сессия) считаются от каталога песочницы,
        # поэтому в статических списках их нет — но объявлены они так же явно.
        declared = (set(_standenv.ENV_KEEP) | set(_standenv.ENV_PIN)
                    | set(_standenv._derived_pins(os.environ)))
        leaked = sorted(name for name in os.environ
                        if name.startswith("PRAXIS_") and name not in declared)
        self.assertEqual(leaked, [], f"среда прода дотекла до стенда: {leaked}")

    def test_pinned_values_are_the_declared_ones(self):
        for name, value in _standenv.ENV_PIN.items():
            self.assertEqual(os.environ.get(name), value, f"{name} не закреплён")

    def test_a_known_behaviour_gate_is_absent(self):
        """PRAXIS_CONTEXT_BUDGET стоял на проде и выбрасывал слои из её кадра.

        Тесты, утверждающие СОДЕРЖИМОЕ кадра, переменную не пиннят — то есть проверяли
        не то, что написано в ассерте, а то, что осталось после усечения по живому
        бюджету. Ветка усечения покрыта отдельно и явно (test_memory_v2).
        """
        self.assertIsNone(os.environ.get("PRAXIS_CONTEXT_BUDGET"))

    def test_sanitize_reports_what_it_took_away(self):
        """Прививка обязана называть снятое: молчаливая смена поведения — тот же дефект."""
        fake = {"PRAXIS_BASE": "/tmp/x", "PRAXIS_TEST": "1",
                "PRAXIS_EMAIL_PASS": "секрет", "PRAXIS_SLEEP_WINDOW": "4-6",
                "HOME": "/root"}
        dropped = _standenv.sanitize(fake)
        self.assertEqual(dropped, ["PRAXIS_EMAIL_PASS", "PRAXIS_SLEEP_WINDOW"])
        self.assertEqual(fake["PRAXIS_BASE"], "/tmp/x", "база песочницы не трогается")
        self.assertEqual(fake["HOME"], "/root", "не-PRAXIS переменные не наши")
        self.assertEqual(fake["PRAXIS_TZ"], _standenv.ENV_PIN["PRAXIS_TZ"])


class LeakDetectorIsInstalledAndWorks(unittest.TestCase):
    def test_it_is_installed(self):
        self.assertTrue(getattr(unittest.TestCase.run, "__praxis_env_guard__", False),
                        "детектор протечки окружения не встал")

    def test_installing_twice_is_refused(self):
        """Двойная обёртка дала бы двойной отчёт об одной протечке."""
        self.assertFalse(_standenv.install_leak_detector())

    def test_a_leaking_test_fails_itself_and_the_environment_is_restored(self):
        class Leaky(unittest.TestCase):
            def runTest(self):  # noqa: N802 - имя из unittest
                os.environ["PRAXIS_LEAK_PROBE"] = "1"

        result = unittest.TestResult()
        Leaky().run(result)
        self.assertEqual(len(result.failures), 1, "протечка прошла незамеченной")
        self.assertIn("PRAXIS_LEAK_PROBE", result.failures[0][1])
        self.assertNotIn("PRAXIS_LEAK_PROBE", os.environ,
                         "детектор обязан вернуть среду, а не только пожаловаться")

    def test_a_clean_test_is_not_accused(self):
        class Tidy(unittest.TestCase):
            def runTest(self):  # noqa: N802
                os.environ["PRAXIS_LEAK_PROBE"] = "1"
                del os.environ["PRAXIS_LEAK_PROBE"]

        result = unittest.TestResult()
        Tidy().run(result)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])



class TheDoorIsCopiedWhole(unittest.TestCase):
    """Тестовое репо получает ТУ ЖЕ дверь, что живое.

    03.08.2026 список копируемых файлов отстал от того, что импортирует praxis_test.py,
    и девять тестов про её право на merge покраснели с сообщением «тесты: ? тестов, ЕСТЬ
    ПАДЕНИЯ» — то есть причина была не названа вовсе. Здесь она называется прямо.
    """

    def test_copy_door_takes_every_module_the_entrypoint_imports(self):
        source = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            names = _standenv.copy_door(source, Path(tmp))
            for expected in ("praxis_test.py", "_sandbox.py", "_standenv.py"):
                self.assertIn(expected, names)
            for name in names:
                self.assertTrue((Path(tmp) / name).is_file(), name)

    def test_an_incomplete_copy_is_refused_not_silently_shipped(self):
        source = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            real = shutil.copy2

            def skip_the_stand(src, dst, *a, **kw):
                if Path(src).name == "_standenv.py":
                    return dst
                return real(src, dst, *a, **kw)

            with mock.patch.object(_standenv.shutil, "copy2", skip_the_stand):
                with self.assertRaises(AssertionError):
                    _standenv.copy_door(source, Path(tmp))



class TheStandTestsTheBodyTheImageShips(unittest.TestCase):
    """Прививка не имеет права ТИХО отключать проверки.

    03.08.2026 первая её редакция сняла PRAXIS_HANDS (эту переменную ставит Dockerfile,
    а не .env) — и `setUpClass` пропустил весь класс про её файловый пол: двадцать пять
    тестов про shrink-guard, побег из дома, креды, квитанции и отпечаток рельсов бинаря.
    Прогон при этом сказал OK. Красноты нет, покрытия тоже — и заметить это можно было
    только сравнив СОСТАВ прогона до и после.
    """

    def test_the_image_variable_stays_in_the_allowlist(self):
        """Сторож решения: убрать её из списка снова — покраснеть здесь, а не молча."""
        self.assertIn("PRAXIS_HANDS", _standenv.ENV_KEEP,
                      "без неё класс про файловый пол пропустится, а гейт останется зелёным")

    def test_the_rust_floor_is_actually_reachable(self):
        """Сторож реальности: в контейнере пол обязан проверяться бинарём, а не питоном."""
        if not os.environ.get("PRAXIS_HANDS"):
            self.skipTest("PRAXIS_HANDS не задан образом — вне контейнера это нормально")
        import hands

        self.assertTrue(hands.available(),
                        "бинарь praxis-hands недостижим: пол проверяется только питоном, "
                        "и тесты про него пропускаются классом")


class TheStandDoesNotReachOutside(unittest.TestCase):
    """Стенд не ходит наружу: ни к root-демону хоста, ни в её живую сессию.

    03.08.2026, сетевой аудит. Оба выхода были МОЛЧАЛИВЫМИ: rails.registry() и
    capabilities.snapshot() делали ~300 живых admin.status к root-демону по 8с срока
    каждый (вердикт двигало время, а не поведение), а TELEGRAM_SESSION ставилась через
    setdefault — то есть тест-процесс открывал её боевой файл сессии.
    """

    def test_the_host_daemon_is_out_of_reach(self):
        import serverd_client

        self.assertFalse(serverd_client.available(),
                         "стенд достаёт до root-демона хоста: ~300 RPC по 8с срока")
        # ⚠ `available()` отвечает на вопрос «лежат ли ПРЯМО СЕЙЧАС файлы сокета и
        # токена», а не «смотрит ли клиент в песочницу». На боксе без демона он был бы
        # False и без всякого пина — то есть сторож проходил бы, ничего не охраняя.
        run_dir = str(serverd_client.RUN_DIR)
        self.assertTrue(os.path.isabs(run_dir), run_dir)
        throwaway = (os.environ.get("PRAXIS_BASE") or "", tempfile.gettempdir())
        self.assertTrue(any(root and run_dir.startswith(root) for root in throwaway),
                        f"клиент смотрит в боевой каталог демона: {run_dir}")

    def test_the_telegram_session_is_disposable(self):
        """Сессия стенда — одноразовая, и это проверяется по форме пути.

        ⚠ Боевое значение — `praxis`, ОТНОСИТЕЛЬНОЕ имя: telethon разворачивает
        его от рабочего каталога, то есть в /app/praxis.session — её живой файл
        сессии. Вход стенда ставил переменную через setdefault, поэтому боевое
        значение побеждало, а `mtproto_runner` строит клиент НА УРОВНЕ МОДУЛЯ:
        любой импортёр открывал её сессию и держал auth-ключ в тест-процессе.

        Два модуля защищали себя сами (`test_truth_runner`,
        `test_direct_telegram_outbox` уводят сессию в свой mkdtemp), поэтому
        привязываться к каталогу песочницы нельзя — проверяем инвариант: путь
        АБСОЛЮТНЫЙ и лежит во временном каталоге. Боевое `praxis` не проходит
        уже по первому условию.
        """
        import tempfile

        session = os.environ.get("TELEGRAM_SESSION") or ""
        self.assertTrue(session, "сессия не задана вовсе")
        self.assertTrue(os.path.isabs(session),
                        f"относительный путь разворачивается от cwd в её живую "
                        f"сессию: {session!r}")
        throwaway = (os.environ.get("PRAXIS_BASE") or "", tempfile.gettempdir())
        self.assertTrue(any(root and session.startswith(root) for root in throwaway),
                        f"сессия не в одноразовом каталоге: {session!r}")


class TheNetworkJournalNamesWhereTheStandGoes(unittest.TestCase):
    """Фаза 1 забора: журнал без зубов. Поведение прогона не меняется, но выход виден.

    03.08.2026: за день нашлись три молчаливых выхода наружу, каждый отдельным аудитом.
    Журнал превращает разовый аудит в постоянный инструмент. ⚠ Он НЕ находит ложную
    зелень — он делает её опровержимой.
    """

    def test_it_is_armed(self):
        import _netwatch

        self.assertTrue(getattr(socket.socket.connect, "__praxis_net_watch__", False),
                        "журнал сети не встал")
        self.assertFalse(_netwatch.install(), "двойная обёртка удвоила бы журнал")

    def test_the_sandbox_unix_socket_is_not_outward(self):
        import _netwatch

        inside = os.path.join(os.environ.get("PRAXIS_BASE") or tempfile.gettempdir(), "s.sock")
        self.assertEqual(_netwatch.classify(socket.AF_UNIX, inside), "песочница")
        self.assertEqual(_netwatch.classify(socket.AF_UNIX, "/run/praxis-serverd/serverd.sock"),
                         "НАРУЖУ", "боевой демон хоста — это выход наружу")

    def test_loopback_is_named_but_not_called_safe(self):
        """⚠ На 127.0.0.1 висят мост тела (:9473) и реле мозга (:5011)."""
        import _netwatch

        self.assertEqual(_netwatch.classify(socket.AF_INET, ("127.0.0.1", 9473)), "loopback")
        self.assertEqual(_netwatch.classify(socket.AF_INET, ("93.184.216.34", 443)), "НАРУЖУ")
        self.assertEqual(_netwatch.classify(socket.AF_INET, ("api.z.ai", 443)), "НАРУЖУ")

    def test_a_real_connection_is_journaled_with_its_caller(self):
        import _netwatch

        before = len(_netwatch.ATTEMPTS)
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.socket()
        self.addCleanup(client.close)
        client.connect(server.getsockname())
        self.assertGreater(len(_netwatch.ATTEMPTS), before, "соединение не попало в журнал")
        row = _netwatch.ATTEMPTS[-1]
        self.assertEqual(row["kind"], "loopback")
        self.assertIn("test_gate_hermetic", row["test"], "журнал должен знать, чей это выход")

    def test_phase_one_has_no_teeth(self):
        """Фаза 1 не отказывает — иначе она поменяла бы поведение прогона."""
        import _netwatch

        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.socket()
        self.addCleanup(client.close)
        client.connect(server.getsockname())  # не бросает — это и проверяется
        self.assertIsInstance(_netwatch.report(io.StringIO()), str)


class TheFenceHasTeeth(unittest.TestCase):
    """Фаза 2: выход наружу больше не журналится молча, а отказывается.

    Allowlist написан по ФАКТИЧЕСКОМУ журналу фазы 1, а не по догадке: полный прогон
    показал ноль попыток наружу, 32 loopback на эфемерных портах (свои тест-серверы
    aiohttp) и 2 юникс-сокета в песочнице.
    """

    def test_the_refusal_is_not_an_oserror(self):
        """⚠ Самое важное свойство забора.

        Сделай отказ OSError — и `except (URLError, OSError, ...)` в hostview.py:52 его
        проглотит, вернёт None, тест позеленеет. Забор своими руками изготовил бы ложную
        зелень ровно той формы, ради которой ставился.
        """
        import _netwatch

        self.assertTrue(issubclass(_netwatch.StandWentOutside, BaseException))
        self.assertFalse(issubclass(_netwatch.StandWentOutside, Exception),
                         "отказ забора не должен ловиться `except Exception`")

    def test_an_outward_address_is_refused_before_the_wire(self):
        import _netwatch

        sock = socket.socket()
        self.addCleanup(sock.close)
        with self.assertRaises(_netwatch.StandWentOutside) as caught:
            sock.connect(("198.51.100.1", 80))          # RFC 5737: наружу и не существует
        text = str(caught.exception)
        self.assertIn("СТЕНД ПОШЁЛ НАРУЖУ", text)
        self.assertIn("198.51.100.1:80", text, "адресат должен быть назван")
        self.assertIn("test_gate_hermetic", text, "тест должен быть назван")
        self.assertIn("подмени шов у себя", text, "выход должен быть подсказан")

    def test_loopback_on_a_relay_port_is_refused_too(self):
        """⚠ loopback не синоним безопасного: за 9473 живёт мост тела."""
        import _netwatch

        sock = socket.socket()
        self.addCleanup(sock.close)
        with self.assertRaises(_netwatch.StandWentOutside) as caught:
            sock.connect(("127.0.0.1", 9473))
        self.assertIn("мост тела", str(caught.exception))

    def test_own_loopback_server_is_allowed(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.socket()
        self.addCleanup(client.close)
        client.connect(server.getsockname())            # не бросает — это и проверяется

    def test_the_sandbox_unix_socket_is_allowed(self):
        import _netwatch

        inside = os.path.join(os.environ.get("PRAXIS_BASE") or tempfile.gettempdir(), "x.sock")
        self.assertEqual(_netwatch._refusal(_netwatch.classify(socket.AF_UNIX, inside), inside), "")
        outside = "/run/praxis-serverd/serverd.sock"
        self.assertTrue(_netwatch._refusal(_netwatch.classify(socket.AF_UNIX, outside), outside))

    def test_teeth_can_be_taken_out_without_losing_the_journal(self):
        """Ручка стенда: журнал остаётся, отказ снимается."""
        import _netwatch

        before = len(_netwatch.ATTEMPTS)
        sock = socket.socket()
        self.addCleanup(sock.close)
        with mock.patch.object(_netwatch, "TEETH", False):
            with self.assertRaises(OSError):            # настоящая сеть, не забор
                sock.settimeout(0.05)
                sock.connect(("198.51.100.1", 80))
        self.assertGreater(len(_netwatch.ATTEMPTS), before, "журнал обязан остаться")




if __name__ == "__main__":
    unittest.main()
