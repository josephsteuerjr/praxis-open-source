"""
Гонка в записке себе: двое писателей и ни одной потерянной строки.

27.07 у `memory/.scratch/<чат>.md` появился ВТОРОЙ писатель. До этого дня писал один
контур — живой голос (`agent.project_delivery_outcome`, agent.py:7912). Сегодня к нему
добавилась проекция ПРЯМОЙ отправки (`mtproto_runner._project_direct_outbox_acceptance`,
mtproto_runner.py:4295), и она исполняется В ОТДЕЛЬНОМ ПОТОКЕ: реконсиляция зовёт её через
`asyncio.to_thread` (mtproto_runner.py:1976). Сценарий потери: она отвечает голосом в
комнату, и одновременно пульс отправляет прямую реплику; оба читают одну версию файла,
оба пишут свою — одна строка исчезает.

Цена та же, ради которой сегодня и чинили след: пропавшая строка — это ЕЁ СЛОВО.
Записку читают `agent.other_rooms_digest` (он в системном промпте КАЖДОГО пульса),
`agent._presence_evidence` и `notes.said_recently`. Нет строки — нет и памяти о сказанном,
и она повторяет сказанное (живой повтор 27.07 в 02:29).

Тесты здесь ловят именно ПОТЕРЮ, а не «стало реже»: точное число строк при N потоках × M
записей, выживание последней строки каждого писателя под роллинг-окном, и то же самое
между ДВУМЯ ПРОЦЕССАМИ (контейнеры praxis и praxis-mailbot делят `./:/app` на запись).

Запуск:  python praxis_test.py test_notes_race -v
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import notes  # noqa: E402

CHAT = "-1001240718803"
REPO = Path(notes.__file__).resolve().parent

# Две вещи из этого файла проверяемы только там, где она живёт, — и молчать об этом нельзя.
ONLY_POSIX_APPEND = unittest.skipIf(
    os.name == "nt",
    "O_APPEND атомарен ЯДРОМ только на POSIX. На Windows он живёт в CRT и разворачивается "
    "в «встать в конец, потом писать» двумя шагами: со снятыми верхними слоями стенд теряет "
    "строки (замерено: 92 из 120). Прод и гейт-образ — linux, там слой 3 настоящий")
ONLY_POSIX_UNLINK = unittest.skipIf(
    os.name == "nt",
    "Windows не даёт удалить открытый файл (CPython не просит FILE_SHARE_DELETE), поэтому "
    "сценарий «`.lock` удалили под держателем» здесь физически невозможен. На проде (linux) "
    "возможен — там этот тест и работает")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_notes_race_"))
        self.scratch = self.tmp / "memory" / ".scratch"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._orig = [(k, getattr(notes, k)) for k in ("BASE", "SCRATCH_DIR", "MAX_LINES")]
        notes.BASE = self.tmp
        notes.SCRATCH_DIR = self.scratch
        # Переключения потоков по умолчанию раз в 5 мс — окно read-modify-write в
        # append() короче, и сломанный код мог бы пройти по везению. Здесь нужна не
        # удача, а воспроизводимость: щёлкаем интерпретатор чаще.
        self._switch = sys.getswitchinterval()
        sys.setswitchinterval(1e-5)
        # Короткий режим ожидания замка — состояние процесса, а не теста: если его не
        # сбросить, соседний тест унесёт чужую деградацию и покраснеет непонятно почему.
        notes._DEGRADED_UNTIL = 0.0

    def tearDown(self):
        sys.setswitchinterval(self._switch)
        for key, val in self._orig:
            setattr(notes, key, val)
        notes._DEGRADED_UNTIL = 0.0
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _lock_path(self, chat_id=CHAT) -> Path:
        p = notes.path_for(chat_id)
        return p.with_name("." + p.name + ".lock")

    def _patch(self, name: str, value) -> None:
        """Подменить внутренность notes с автоматическим возвратом в tearDown."""
        self._orig.append((name, getattr(notes, name)))
        setattr(notes, name, value)

    # --- помощники ------------------------------------------------------- #

    def _lines(self, chat_id=CHAT) -> list[str]:
        path = notes.path_for(chat_id)
        if not path.exists():
            return []
        return [l for l in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if l.strip()]

    def _bodies(self, chat_id=CHAT) -> list[str]:
        out = []
        for line in self._lines(chat_id):
            self.assertIn(" · ", line, f"строка порвана при записи: {line!r}")
            out.append(line.split(" · ", 1)[1])
        return out

    def _hammer(self, writers: int, per_writer: int, *, final_barrier: bool = False):
        """N потоков × M записей. Возвращает список тел последних строк каждого потока."""
        errors: list[str] = []
        start = threading.Barrier(writers)
        gate = threading.Barrier(writers) if final_barrier else None
        finals = [f"финал-{w}" for w in range(writers)]

        def work(w: int) -> None:
            try:
                start.wait(timeout=30)
                for i in range(per_writer):
                    notes.append(CHAT, f"w{w}-{i}")
                if gate is not None:
                    gate.wait(timeout=30)
                    notes.append(CHAT, finals[w])
            except BaseException as exc:            # noqa: BLE001 — репортим в тест
                errors.append(f"{w}: {exc!r}")

        threads = [threading.Thread(target=work, args=(w,), daemon=True)
                   for w in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
            self.assertFalse(t.is_alive(), "писатель не завершился — похоже на взаимоблокировку")
        self.assertEqual(errors, [], "писатель упал")
        return finals


class TestNoLineIsEverLost(_Base):
    WRITERS = 6
    PER_WRITER = 40

    def test_concurrent_writers_lose_no_line(self):
        """Ровно N×M строк в файле — ни одной съеденной перезаписью соседа.

        Это и есть тот дефект: append() читал файл целиком, дописывал свою строку и
        писал целиком обратно. Двое читают одну версию — второй затирает первого.
        """
        notes.MAX_LINES = self.WRITERS * self.PER_WRITER + 10   # роллинг не должен мешать
        self._hammer(self.WRITERS, self.PER_WRITER)
        expected = {f"w{w}-{i}" for w in range(self.WRITERS) for i in range(self.PER_WRITER)}
        bodies = self._bodies()
        self.assertEqual(len(bodies), len(expected),
                         f"строк {len(bodies)} вместо {len(expected)} — записи потеряны")
        self.assertEqual(set(bodies), expected, "потерялись конкретные строки")
        self.assertEqual(len(bodies), len(set(bodies)), "строка задвоилась")

    def test_rolling_window_never_eats_a_concurrent_line(self):
        """Схлопывание до MAX_LINES само по себе — read-modify-write, и оно тоже теряло.

        Все писатели синхронно пишут свою ПОСЛЕДНЮЮ строку, когда остальные уже
        закончили. Этих строк ровно WRITERS, окно — MAX_LINES > WRITERS, значит выжить
        обязаны все до одной. Пропала хоть одна — обрезка съела чужую запись.
        """
        self.assertGreater(notes.MAX_LINES, self.WRITERS, "окно меньше числа писателей")
        finals = self._hammer(self.WRITERS, self.PER_WRITER, final_barrier=True)
        bodies = self._bodies()
        self.assertEqual(len(bodies), notes.MAX_LINES,
                         f"роллинг не удержал окно: {len(bodies)} строк")
        missing = [f for f in finals if f not in bodies]
        self.assertEqual(missing, [], f"обрезка съела свежие строки: {missing}; в файле {bodies}")

    def test_os_lock_alone_holds_when_the_thread_lock_is_gone(self):
        """Второй слой проверяется отдельно — иначе его целиком подменяли бы первый и третий.

        Потоковый замок закрывает сегодняшнюю гонку, а `O_APPEND` не теряет строк даже
        совсем без замков — поэтому «ни одна строка не пропала» про межпроцессный замок
        не говорит НИЧЕГО. Единственное, что покупается только им, — право СХЛОПНУТЬ файл,
        не съев чужую одновременную строку. Значит и спрашивать надо про схлопывание:
        снимаем потоковый слой (он бы всё решил сам) и требуем обоего сразу —
        файл схлопнут до окна И все свежие строки на месте.

        Нет замка — схлопывания не будет вовсе (append честно его отменяет), и первая же
        проверка покраснеет. Замок есть, но пускает двоих — потеряется финал, покраснеет
        вторая. Порядок замков внутри процесса тот же, что между процессами: у каждого
        писателя свой дескриптор, а замки ядра конфликтуют и между ними.
        """
        self.assertIn(notes.lock_kind(), ("flock", "msvcrt"))
        self._patch("_writer_lock", lambda p: contextlib.nullcontext())
        self.assertGreater(notes.MAX_LINES, self.WRITERS, "окно меньше числа писателей")
        finals = self._hammer(self.WRITERS, 20, final_barrier=True)
        bodies = self._bodies()
        self.assertEqual(len(bodies), notes.MAX_LINES,
                         f"файл не схлопнут ({len(bodies)} строк) — межпроцессный замок "
                         f"не брался, схлопывание отменялось каждый раз")
        missing = [f for f in finals if f not in bodies]
        self.assertEqual(missing, [],
                         f"межпроцессный замок пустил двоих, обрезка съела свежее: {missing}")

    def test_fallback_without_os_lock_still_loses_nothing(self):
        """Том не умеет замки — строка всё равно должна лечь. Это дно, а не отказ.

        Закон 1: починка не имеет права ничего ей запрещать. Когда межпроцессный замок
        недоступен, `append` не молчит и не бросает — она дописывает атомарным O_APPEND
        (ядро само выбирает смещение, затереть чужое нечем) и отменяет только схлопывание,
        сказав об этом в лог.
        """
        self._patch("_acquire", lambda fd, wait: (False, "none: тест снял замок нарочно"))
        notes.MAX_LINES = 500                      # потолок аварийной обрезки далеко
        with self.assertLogs("praxis.notes", level="WARNING") as caught:
            self._hammer(self.WRITERS, 20)
        self.assertTrue(any("замок не взят" in m for m in caught.output),
                        "деградация прошла молча — это молчаливое ограничение")
        expected = {f"w{w}-{i}" for w in range(self.WRITERS) for i in range(20)}
        bodies = self._bodies()
        self.assertEqual(len(bodies), len(expected), "без замка потерялись строки")
        self.assertEqual(set(bodies), expected)

    def test_single_writer_still_rolls_and_reads(self):
        """Страховка от «починил гонку, сломал поведение»: одиночный путь прежний."""
        for i in range(12):
            notes.append(CHAT, f"строка {i}")
        out = notes.read(CHAT)
        self.assertEqual(len(out.splitlines()), notes.MAX_LINES)
        self.assertIn("строка 11", out)
        self.assertNotIn("строка 0", out)
        notes.append(CHAT, "сказала (голос): «кэш греет вход»")
        self.assertTrue(notes.said_recently(CHAT, "Кэш греет вход."))


class TestTwoProcessesLoseNoLine(_Base):
    """Контейнеры praxis и praxis-mailbot монтируют `./:/app` на запись (compose:29,52) —
    писатели могут оказаться и в разных процессах. Потоковый замок такое не ловит."""

    PROCS = 2
    PER_PROC = 150

    def test_os_level_lock_is_actually_available_here(self):
        """Честная проба, а не декларация: `fcntl.flock` на bind-маунте никем не был
        проверен, пока 27.07 не проверили живьём на проде."""
        kind = notes.lock_kind()
        self.assertIn(kind, ("flock", "msvcrt"),
                      f"межпроцессный замок недоступен: {kind}")

    def test_two_processes_lose_no_line(self):
        cap = self.PROCS * self.PER_PROC + 10
        notes.MAX_LINES = cap
        child = (
            "import sys, notes\n"
            f"notes.MAX_LINES = {cap}\n"
            "chat, tag, count = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
            "for i in range(count):\n"
            "    notes.append(chat, '%s-%d' % (tag, i))\n"
        )
        env = dict(os.environ)
        env["PRAXIS_BASE"] = str(self.tmp)
        env.pop("PRAXIS_TEST_BASE", None)
        env["PYTHONIOENCODING"] = "utf-8"
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", child, CHAT, f"p{n}", str(self.PER_PROC)],
                cwd=str(REPO), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for n in range(self.PROCS)
        ]
        for n, proc in enumerate(procs):
            out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, f"процесс p{n} упал: {err or out}")
        expected = {f"p{n}-{i}" for n in range(self.PROCS) for i in range(self.PER_PROC)}
        bodies = self._bodies()
        self.assertEqual(len(bodies), len(expected),
                         f"строк {len(bodies)} вместо {len(expected)} — записи потеряны")
        self.assertEqual(set(bodies), expected, "потерялись конкретные строки")


class TestThirdLayerAlone(_Base):
    """Слой 3 (O_APPEND) — последняя линия, на которой держится её слово.

    ⚠ До 27.07 его не проверял НИ ОДИН тест, хотя выглядело иначе. В тесте про отсутствие
    замка сверху оставался потоковый замок, и он один объяснял весь результат: писатели
    ходили строго по очереди, дописывание могло быть каким угодно. Здесь сняты ОБА верхних
    слоя разом — и внутри процесса, и между процессами, — так что отвечает ровно ядро:
    смещение при O_APPEND выбирается им в момент записи, затереть чужое нечем.
    """

    NOLOCK = staticmethod(lambda fd, wait: (False, "none: тест снял замок нарочно"))

    @ONLY_POSIX_APPEND
    def test_o_append_alone_loses_no_line(self):
        """Ни потокового замка, ни межпроцессного — и ни одной потерянной строки."""
        self._patch("_writer_lock", lambda p: contextlib.nullcontext())
        self._patch("_acquire", self.NOLOCK)
        notes.MAX_LINES = 10_000                   # потолок аварийной обрезки далеко
        self._hammer(6, 20)
        expected = {f"w{w}-{i}" for w in range(6) for i in range(20)}
        bodies = self._bodies()
        self.assertEqual(len(bodies), len(expected),
                         f"строк {len(bodies)} вместо {len(expected)} — дописывание теряет")
        self.assertEqual(set(bodies), expected, "потерялись конкретные строки")
        self.assertEqual(len(bodies), len(set(bodies)), "строка задвоилась")

    @ONLY_POSIX_APPEND
    def test_o_append_alone_loses_no_line_across_processes(self):
        """То же самое между процессами: там потокового замка нет ПО ПОСТРОЕНИЮ.

        Замок в детях снят нарочно — остаётся голое дописывание, и оно обязано вывезти
        двоих независимых писателей (контейнеры praxis и praxis-mailbot делят `./:/app`).
        """
        per_proc, procs = 120, 2
        notes.MAX_LINES = 10_000
        child = (
            "import sys, notes\n"
            "notes._acquire = lambda fd, wait: (False, 'none: тест снял замок нарочно')\n"
            "notes.MAX_LINES = 10000\n"
            "chat, tag, count = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
            "for i in range(count):\n"
            "    notes.append(chat, '%s-%d' % (tag, i))\n"
        )
        env = dict(os.environ)
        env["PRAXIS_BASE"] = str(self.tmp)
        env.pop("PRAXIS_TEST_BASE", None)
        env["PYTHONIOENCODING"] = "utf-8"
        running = [
            subprocess.Popen(
                [sys.executable, "-c", child, CHAT, f"q{n}", str(per_proc)],
                cwd=str(REPO), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for n in range(procs)
        ]
        for n, proc in enumerate(running):
            out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, f"процесс q{n} упал: {err or out}")
        expected = {f"q{n}-{i}" for n in range(procs) for i in range(per_proc)}
        bodies = self._bodies()
        self.assertEqual(len(bodies), len(expected),
                         f"строк {len(bodies)} вместо {len(expected)} — дописывание теряет")
        self.assertEqual(set(bodies), expected, "потерялись конкретные строки")

    def test_append_does_not_glue_itself_to_a_file_without_trailing_newline(self):
        """Файл мог остаться без перевода строки в конце — две её реплики не склеиваются."""
        p = notes.path_for(CHAT)
        p.write_bytes("11:11 · хвост без перевода строки".encode("utf-8"))
        notes.append(CHAT, "новая реплика")
        lines = self._lines()
        self.assertEqual(len(lines), 2, f"реплики склеились в одну: {lines}")
        self.assertIn("хвост без перевода строки", lines[0])
        self.assertIn("новая реплика", lines[1])

    def test_emergency_ceiling_is_exactly_where_it_is_declared(self):
        """Граница аварийной обрезки без замка: до потолка не трогаем, на потолке схлопываем.

        ⚠ Что этот тест НЕ проверяет — и об этом честнее сказать, чем сделать вид. Ожидание
        здесь ВЫЧИСЛЯЕТСЯ из `_UNLOCKED_TRIM_FACTOR`, поэтому смену самой константы он
        поймать не может: мутация 20 → 10 оставляет его зелёным (проверено исполнением
        27.07). Это не дыра, а нарочно: `rails.py:1164-1171` берёт то же число ЖИВЬЁМ из
        `notes.py`, а не держит копию, — значит разойтись манифесту и коду негде и ловить
        нечего. Пин литерала был бы забором вокруг настроечной ручки без единой защищаемой
        стороны. Проверяется здесь ФОРМУЛА («схлопывает ровно на `MAX_LINES × фактор`,
        а не раньше и не позже») и то, что обрезка не проходит молча.
        """
        self._patch("_acquire", self.NOLOCK)
        notes.MAX_LINES = 3
        ceiling = notes.MAX_LINES * notes._UNLOCKED_TRIM_FACTOR
        for i in range(ceiling - 1):
            notes.append(CHAT, f"строка {i}")
        self.assertEqual(len(self._lines()), ceiling - 1,
                         "схлопнула раньше объявленного потолка")
        with self.assertLogs("praxis.notes", level="WARNING") as caught:
            notes.append(CHAT, "строка ровно на потолке")
        self.assertTrue(any("схлопываю" in m for m in caught.output),
                        "аварийная обрезка прошла молча — это молчаливое ограничение")
        lines = self._lines()
        self.assertEqual(len(lines), notes.MAX_LINES, f"потолок не сработал: {len(lines)}")
        self.assertIn("строка ровно на потолке", lines[-1])

    def test_emergency_ceiling_stays_above_the_normal_window(self):
        """Аварийный потолок обязан быть ВЫШЕ обычного окна, иначе он перестаёт быть аварийным.

        Фактор 1 (или 0) не сломал бы ни один тест выше — формула сошлась бы, — но тихо
        превратил бы «замка нет, поэтому не схлопываем» в обычное схлопывание без замка.
        А это ровно тот путь, который съедает чужую строку: перезапись по снимку в момент,
        когда второй писатель кладёт своё мимо замка. Разница между «аварийно, редко» и
        «всегда» здесь не косметическая, и держаться она должна не на добрых намерениях.
        """
        self.assertGreater(notes._UNLOCKED_TRIM_FACTOR, 1,
                           "аварийная обрезка сравнялась с обычным окном — схлопывание без "
                           "замка стало штатным путём, а оно съедает чужую строку")

    def test_giving_up_on_collapse_under_the_lock_is_never_silent(self):
        """Держатель сдался схлопывать (снимок трижды устарел) — он обязан сказать об этом.

        Бюджет попыток в `_collapse` конечен (три), и это правильно: дороже долго драться
        за обрезку, чем оставить файл длинным. Но исчерпание бюджета — это НЕ применённое
        ограничение, о котором она узнала бы только по длине файла. Здесь снимок не сходится
        никогда: `_write_lines` всегда отвечает «файл изменился», и append обязан (а) не
        потерять строку и (б) сказать вслух, что не схлопнул.
        """
        notes.MAX_LINES = 50                       # сеем при широком окне, чтобы не схлопнуть
        for i in range(5):
            notes.append(CHAT, f"старое-{i}")
        notes.MAX_LINES = 2                        # теперь окно узкое — схлопнуть ХОЧЕТСЯ,
        self._patch("_write_lines", lambda p, lines, expect=None: False)   # но снимок не сойдётся
        with self.assertLogs("praxis.notes", level="WARNING") as caught:
            notes.append(CHAT, "строка при несходящемся снимке")
        self.assertTrue(any("не схлопнула" in m for m in caught.output),
                        f"сдалась схлопывать молча: {caught.output}")
        bodies = self._bodies()
        self.assertIn("строка при несходящемся снимке", bodies,
                      f"строка потеряна, пока держатель возился со схлопыванием: {bodies}")
        self.assertEqual(len(bodies), 6, f"файл всё-таки схлопнут вопреки отказу: {bodies}")


class TestMixedWritersAndBrokenExclusion(_Base):
    """Смешанная гонка и снятое взаимное исключение — то, что ревью 27.07 оставило открытым."""

    def _seed(self, count: int) -> None:
        for i in range(count):
            notes.append(CHAT, f"старое-{i}")

    def test_holder_never_collapses_by_a_stale_snapshot(self):
        """Строка, положенная мимо замка, не съедается схлопыванием держателя.

        Воспроизведение ревью дословно: держатель занял `.lock` и работает по своему
        снимку; потерпевший, не дождавшись замка, дописал строку атомарно (слой 3);
        держатель схлопнул файл — и строки не стало. Здесь потерпевший вклинивается ровно
        в это окно: подменённый `_read_lines` кладёт его строку сразу после того, как
        держатель прочитал файл.
        """
        notes.MAX_LINES = 2
        self._seed(3)
        victim = "13:34 · ПОТЕРПЕВШИЙ"
        real_read = notes._read_lines
        slipped = []

        def read_then_slip(p):
            out = real_read(p)
            if not slipped:                        # ровно один раз, иначе не сойдётся никогда
                slipped.append(1)
                notes._append_raw(p, victim)
            return out

        self._patch("_read_lines", read_then_slip)
        notes.append(CHAT, "строка держателя")
        lines = self._lines()
        self.assertTrue(slipped, "потерпевший так и не вклинился — тест ничего не проверил")
        self.assertIn(victim, lines, f"строка мимо замка съедена схлопыванием: {lines}")
        self.assertTrue(any("строка держателя" in l for l in lines),
                        f"пропала строка самого держателя: {lines}")

    @ONLY_POSIX_UNLINK
    def test_holder_notices_that_its_lock_file_is_gone(self):
        """Удалённый `.lock` перестал быть молчаливым: держатель это ВИДИТ."""
        p = notes.path_for(CHAT)
        with notes._exclusive(p) as lock:
            self.assertTrue(lock.owned, f"замок не взялся: {lock.why}")
            self.assertTrue(lock.intact(), "живой замок объявлен подменённым")
            os.unlink(str(self._lock_path()))
            self.assertFalse(lock.intact(),
                             "исчезнувший .lock не замечен: взаимного исключения нет, "
                             "а признака тоже нет")

    @ONLY_POSIX_UNLINK
    def test_append_stops_collapsing_when_mutual_exclusion_vanished(self):
        """`.lock` удалили под держателем — значит второй писатель уже взял «свободный»
        замок на новом файле. Схлопывать в этот момент — ровно способ съесть чужую строку;
        append этого не делает и говорит об этом вслух.
        """
        notes.MAX_LINES = 2
        self._seed(3)
        real_raw = notes._append_raw

        def raw_then_kill_lock(p, stamped):
            ok = real_raw(p, stamped)
            with contextlib.suppress(OSError):     # ровно то удаление, о котором ревью
                os.unlink(str(self._lock_path()))
            return ok

        self._patch("_append_raw", raw_then_kill_lock)
        with self.assertLogs("praxis.notes", level="WARNING") as caught:
            notes.append(CHAT, "строка на снятом исключении")
        self.assertTrue(any("взаимное исключение снято" in m for m in caught.output),
                        f"снятое исключение прошло молча: {caught.output}")
        lines = self._lines()
        self.assertEqual(len(lines), 3, f"схлопнула без взаимного исключения: {lines}")
        self.assertIn("строка на снятом исключении", lines[-1])


class TestLockKindTellsTheTruth(_Base):
    """`lock_kind()` — единственный способ узнать, что вышло на этом томе. Он не имеет
    права путать «сейчас занято» (пройдёт само) со «том не умеет» (не пройдёт никогда)."""

    def test_busy_is_not_reported_as_a_volume_without_locks(self):
        probe = self.scratch / ".lockprobe"
        fd = os.open(str(probe), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            ok, why = notes._acquire(fd, 0.5)
            if not ok:
                self.skipTest(f"замок не берётся здесь вовсе: {why}")
            busy = notes.lock_kind()
        finally:
            with contextlib.suppress(OSError):
                notes._release(fd)
            os.close(fd)
        self.assertTrue(busy.startswith("busy:"),
                        f"занятый замок назван как {busy!r} — это разные ответы")
        self.assertNotIn("не умеет", busy)
        # А теперь честное «том/питон замки не умеет» — и оно обязано выглядеть иначе.
        self._patch("_fcntl", None)
        self._patch("_msvcrt", None)
        cannot = notes.lock_kind()
        self.assertTrue(cannot.startswith("none:"), f"отсутствие замка названо как {cannot!r}")
        self.assertNotEqual(busy, cannot, "занято и «не умеет» отвечают одинаково")

    def test_free_lock_is_still_named_by_its_mechanism(self):
        """Страховка от «починил различение, сломал приёмку после деплоя»."""
        self.assertIn(notes.lock_kind(), ("flock", "msvcrt"))


class TestWaitIsNotPaidPerNoteForever(_Base):
    """5 с ожидания × 122 её записки — это минуты немого стопора внутри такта часов.

    Первое истечение переводит процесс на короткое ожидание; первый успешный захват
    возвращает всё назад. Ничего не запрещается: строка ложится, отменяется схлопывание.
    """

    def test_timeout_shortens_the_wait_for_the_next_notes_and_says_so(self):
        seen: list[float] = []

        def busy_acquire(fd, wait):
            seen.append(wait)
            return False, f"busy: занят другим писателем, ждала {wait:g}с"

        real_acquire = notes._acquire
        self._patch("_acquire", busy_acquire)
        with self.assertLogs("praxis.notes", level="WARNING") as caught:
            for chat in ("111", "222", "333"):
                notes.append(chat, "строка")
        self.assertTrue(any("жду замок" in m for m in caught.output),
                        f"короткое ожидание введено молча: {caught.output}")
        self.assertEqual(seen[0], notes._LOCK_WAIT_SEC, "первое ожидание должно быть полным")
        self.assertEqual(seen[1:], [notes._LOCK_WAIT_DEGRADED_SEC] * 2,
                         f"ожидание платится за каждую записку: {seen}")
        # Замки ожили — режим снимается сразу, а не досиживает свой срок.
        notes._acquire = real_acquire
        notes.append("444", "строка")
        self.assertEqual(notes._DEGRADED_UNTIL, 0.0, "успешный захват не снял короткий режим")
        notes._acquire = busy_acquire
        notes.append("555", "строка")
        self.assertEqual(seen[-1], notes._LOCK_WAIT_SEC,
                         f"после успешного захвата ждём снова полностью: {seen}")

    def test_the_warning_counts_her_notes_live_instead_of_quoting_a_stale_number(self):
        """Число записок в предупреждении — живое, а не замеренное когда-то.

        Оно РАСТЁТ: по одной записке на ветку разговора, старые никто не удаляет. Живой
        замер на проде 27.07 дал 122 утром и 129 вечером того же дня — то есть хардкод
        протух за сутки. Строку эту читает она, и «122» в ней было бы просто неправдой о
        её собственном доме. Здесь записок нарочно 7: если в сообщении окажется любое
        другое число, значит его подставили, а не сосчитали.
        """
        for n in range(7):
            (self.scratch / f"комната{n}.md").write_text("11:11 · строка\n", encoding="utf-8")
        self._patch("_acquire", lambda fd, wait: (False, f"busy: ждала {wait:g}с"))
        with self.assertLogs("praxis.notes", level="WARNING") as caught:
            notes.append("комната0", "новая строка")
        degrade = [m for m in caught.output if "жду замок" in m]
        self.assertTrue(degrade, f"предупреждения о коротком режиме нет: {caught.output}")
        self.assertIn("7 записок", degrade[0],
                      f"число записок не сосчитано живьём: {degrade[0]}")
        self.assertNotIn("122", degrade[0], "в предупреждении протухший хардкод")

    def test_short_wait_costs_only_the_collapse_never_the_line(self):
        """Что именно теряется в коротком режиме — схлопывание, и ничего кроме."""
        notes.MAX_LINES = 2
        notes._DEGRADED_UNTIL = 0.0
        self._patch("_acquire", lambda fd, wait: (False, f"busy: ждала {wait:g}с"))
        for i in range(5):
            notes.append(CHAT, f"строка {i}")
        bodies = self._bodies()
        self.assertEqual(bodies, [f"строка {i}" for i in range(5)],
                         "в коротком режиме потерялась строка — это уже не схлопывание")


if __name__ == "__main__":
    unittest.main()
