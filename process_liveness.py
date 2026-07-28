"""Cross-platform process liveness checks that never signal Windows processes.

Здесь живёт КОРЕНЬ вопроса «жив ли тот, кто это начал». Долго корень отвечал только
на «занят ли такой номер», и из-за этого метку рождения процесса пришлось выписать
руками в трёх местах сразу — ``forge.py``, ``media.py``, ``self_model.py``: три
байт-в-байт одинаковых ``_proc_started_at``/``_owner_alive``. Теперь метка знает своё
место, и все трое могут звать отсюда.

Почему метка вообще нужна: в контейнере номера процессов маленькие и переиспользуются
мгновенно. 26.07 после рестарта praxis новый python занял /proc/10 — номер мертвеца,
державшего замок задачи. «Есть процесс с таким номером» отвечало «жив» про
постороннего, и замок становился вечным.
"""

from __future__ import annotations

import errno
import os
from typing import NamedTuple


# Вердикты тождества. Их четыре, а не два, ровно потому, что «не знаю» обязано
# выглядеть как «не знаю»: подпись под «мёртв» отбирает чужой замок, подпись под
# «жив» вешает свой навсегда, а сигнал по недоказанному номеру убивает постороннего.
GONE = "gone"          # номера нет (или это зомби) — обращаться не к кому
SAME = "same"          # метка совпала: это ТОТ САМЫЙ процесс, доказано
OTHER = "other"        # метка не совпала: номер переиспользован, это посторонний
UNPROVEN = "unproven"  # номер занят, но доказать тождество нечем (легаси-запись, не-Linux)


class Identity(NamedTuple):
    """Ответ о тождестве вместе с готовой правдой одной строкой.

    ``note`` — не лог, а текст для неё: любой отказ и любая деградация обязаны
    называть, чего мы не знаем (закон 3)."""

    verdict: str
    note: str

    @property
    def alive(self) -> bool:
        """Считать ли держателя живым. «Не доказано» — живым: хоронить без
        доказательства нельзя, это отобрало бы чужой рабочий замок."""

        return self.verdict in (SAME, UNPROVEN)

    @property
    def safe_to_signal(self) -> bool:
        """Можно ли слать сигнал по этому номеру.

        Здесь асимметрия с ``alive`` намеренная: ждать по недоказанному номеру
        безвредно, а вот SIGTERM по нему прилетит постороннему процессу. Поэтому
        подписываемся только под доказанным тождеством, а «не знаю» отдаём
        вызывающему словами (``note``), а не молчаливым отказом."""

        return self.verdict == SAME


def process_started_at(pid: int) -> str:
    """Метка рождения процесса: 22-е поле /proc/<pid>/stat. '' — прочитать не вышло.

    Это единственное, чего не переживает перезапуск: два процесса с одним номером
    почти наверняка родились в разные тики, а после рестарта контейнера —
    гарантированно. Вне Linux (/proc нет) честно возвращаем '' и судим по одному
    номеру, как раньше: соврать «не знаю» под видом факта нельзя.
    """

    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        return raw.rsplit(")", 1)[1].split()[19]
    except Exception:
        return ""


def _number_is_taken(pid: int) -> bool:
    """Занят ли номер — и только это. Тождества не доказывает."""

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        synchronize = 0x00100000
        wait_object_0 = 0x00000000
        error_access_denied = 5
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            # Access denied still proves that a protected process occupies the
            # PID.  Other errors do not justify treating a lock owner as live.
            return ctypes.get_last_error() == error_access_denied
        try:
            # A signalled process handle means the process has exited.  Any
            # other result is treated conservatively as alive.
            return kernel32.WaitForSingleObject(handle, 0) != wait_object_0
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        return exc.errno == errno.EPERM
    # PASS 30 Этап 1: зомби — НЕ живой. Осиротевший супервизор (спавнер умер/раннер
    # рестартовал) после смерти висит Z под чужим PID 1, которому его не реапить;
    # kill(pid, 0) на зомби проходит, и «running» становился вечным — тишина
    # вместо lost-события. /proc есть только на Linux; вне его — прежний ответ.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read(512)
        # поле state — после последней ')': "pid (comm) STATE ..."
        state = stat.rsplit(b")", 1)[-1].strip()[:1]
        return state != b"Z"
    except OSError:
        return True


def identify(pid: int, started_at: str = "") -> Identity:
    """Тот ли это процесс, что записал свой номер, — с готовой правдой о незнании.

    ``started_at`` — метка рождения, записанная в момент запуска. Пусто = запись
    старого образца (до 27.07) или не-Linux: тогда честный ответ «не доказано»,
    а не тихое «жив».
    """

    number = int(pid) if str(pid).lstrip("-").isdigit() else 0
    if not _number_is_taken(pid):
        return Identity(GONE, f"процесса с номером {number} нет (или он уже зомби)")
    started_at = str(started_at or "")
    if not started_at:
        return Identity(UNPROVEN,
                        f"номер {number} кем-то занят, но метки рождения в записи нет "
                        f"(запись старого образца) — что это тот же процесс, не доказано")
    current = process_started_at(number)
    if not current:
        return Identity(UNPROVEN,
                        f"номер {number} занят, но метку рождения прочитать не вышло "
                        f"(/proc недоступен) — тождество не доказано")
    if current == started_at:
        return Identity(SAME, f"номер {number} держит тот же процесс (метка {current})")
    return Identity(OTHER,
                    f"номер {number} переиспользован: занявший его процесс родился в тик "
                    f"{current}, а наш — в {started_at}; это посторонний")


def is_process_alive(pid: int, started_at: str = "") -> bool:
    """Жив ли ИМЕННО тот процесс, что записал этот номер.

    POSIX defines ``kill(pid, 0)`` as a non-signalling existence probe.  Windows
    does not: CPython implements ordinary ``os.kill`` signals there through
    ``TerminateProcess``.  Use a waitable process handle instead.

    Без ``started_at`` поведение прежнее — «занят ли номер». С меткой рождения
    ответ становится доказуемым; «не смогли доказать» здесь означает «живой»,
    потому что похоронить чужую работу дороже, чем подождать лишнего. Для сигнала
    это правило обратное — там нужен ``identify(...).safe_to_signal``.
    """

    return identify(pid, started_at).alive
