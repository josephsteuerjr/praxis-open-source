"""Praxis — серверная обсерватория «Атланта» (mini-app @atlanta2_srv_bot).

Отдельный демон-контейнер (praxis-serverapp): показывает владельцу сервер целиком —
контейнеры, сети, порты, процессы, ядро, доступ, диски. Read-only по построению:
код примонтирован ro, докер — только через docker-socket-proxy (GET-запросы),
хост — через pid:host (/proc) и /:/rootfs:ro. Ничего не пишет и не исполняет.

Доступ — только владельцу: тот же HMAC-гейт Telegram WebApp initData, что у
почты/панели (mailroom_bot), но токеном своего бота (PRAXIS_SERVERAPP_BOT_TOKEN
из .deploy.env). Публичный HTTPS даёт host-Caddy: srv.203.0.113.10.nip.io → 8093.

Коллекторы — чистый stdlib, пути инжектируются (PRAXIS_HOST_PROC / PRAXIS_HOSTFS),
поэтому тестируются фикстурами без докера и без Linux (test_serverapp.py).
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import struct
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl

try:  # веб-слой опционален: коллекторы тестируются и там, где aiohttp нет
    import aiohttp
    from aiohttp import web
except Exception:  # pragma: no cover
    aiohttp = None
    web = None

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
try:  # герметичность: тест-запуск не читает боевой .env (та же логика, что в mailroom_bot)
    from _sandbox import _looks_like_test_run as _under_tests
except Exception:
    _under_tests = lambda: bool(os.environ.get("PRAXIS_TEST"))
if not _under_tests():
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env", override=True)
        load_dotenv(BASE / ".deploy.env", override=False)  # токен бота живёт здесь
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("praxis-serverapp")

TOKEN = os.getenv("PRAXIS_SERVERAPP_BOT_TOKEN", "")
OWNER_ID = int(os.getenv("PRAXIS_OWNER_ID", "0") or 0)
# PASS 17.A: «глаза» Праксис. Её контейнер спрашивает обсерваторию по внутренней сети —
# не docker.sock и не /proc хоста: ни одной новой привилегии, только чтение того же,
# что видит Егор. Токен пуст → эндпоинт отвечает 503 (fail-closed), а не открытой дверью.
AGENT_TOKEN = os.getenv("PRAXIS_AGENT_TOKEN", "")
PORT = int(os.getenv("PRAXIS_SERVERAPP_PORT", "8093") or 8093)
AUTH_MAX_AGE = int(os.getenv("PRAXIS_SERVERAPP_AUTH_MAX_AGE", "86400") or 86400)
DOCKER = os.getenv("PRAXIS_DOCKER_PROXY", "http://dockerproxy:2375").rstrip("/")
PROC = os.getenv("PRAXIS_HOST_PROC", "/proc")     # pid:host ⇒ /proc = хостовый
HOSTFS = os.getenv("PRAXIS_HOSTFS", "/rootfs")    # корень хоста, ro

CLK_TCK = 100          # USER_HZ: тики /proc/*/stat (стандарт на x86)
PAGE = 4096            # страница для /proc/*/statm

# F5: коллекторы (procfs/сокеты) могут зависнуть на host-задаче в непрерываемом D-state —
# такое чтение не убить даже SIGKILL. Держим их на ВЫДЕЛЕННОМ ограниченном пуле, а не на
# дефолтном пуле loop'а (которым пользуется getaddrinfo aiohttp и всё остальное): зависший
# тред-зомби не выест дефолтный пул и не заморозит обсерваторию, а упрётся в _POOL_SIZE.
_POOL_SIZE = int(os.getenv("PRAXIS_SERVERAPP_POOL", "16") or 16)
_POOL = ThreadPoolExecutor(max_workers=_POOL_SIZE, thread_name_prefix="collector")


def _to_thread(fn, *a, **k):
    """asyncio.to_thread, но на выделенном пуле коллекторов (не на дефолтном пуле loop'а)."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(_POOL, functools.partial(fn, *a, **k))


# --------------------------------------------------------------------------- #
#  Авторизация: Telegram WebApp initData (HMAC бот-токеном), owner-only
# --------------------------------------------------------------------------- #

def check_init_data(init_data: str, token: str, max_age: int = 0) -> dict | None:
    """Спека Telegram: secret = HMAC_SHA256("WebAppData", token); hash от
    отсортированных k=v через \\n. max_age>0 — отвергаем устаревшее (anti-replay)."""
    if not init_data or not token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv = pairs.pop("hash", None)
    if not recv:
        return None
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv):
        return None
    if max_age > 0:
        try:
            if time.time() - int(pairs.get("auth_date", "0")) > max_age:
                return None
        except Exception:
            return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


def _is_owner(init_data: str) -> bool:
    user = check_init_data(init_data, TOKEN, AUTH_MAX_AGE)
    return bool(user and OWNER_ID and user.get("id") == OWNER_ID)


# --------------------------------------------------------------------------- #
#  ATLAS v3: stateless owner session — делает Обсерваторию устанавливаемым PWA
#  без записи на диск. Токен подписан ключом из бот-токена (+ ротируемая соль),
#  проверяется без стора. Ротация соли = мгновенный отзыв всех сессий.
# --------------------------------------------------------------------------- #

SESSION_SALT = os.getenv("PRAXIS_SERVERAPP_SESSION_SALT", "atlas.v3")
SESSION_TTL = int(os.getenv("PRAXIS_SERVERAPP_SESSION_TTL", str(30 * 86400)) or 30 * 86400)


def _session_secret() -> bytes:
    return hmac.new(TOKEN.encode("utf-8"), b"atlas.session\0" + SESSION_SALT.encode("utf-8"),
                    hashlib.sha256).digest()


def mint_session(owner_id: int, *, now: float | None = None) -> str:
    """Подписанный владельческий токен: <owner>.<issued>.<nonce>.<mac>. Без стора."""
    issued = int(now if now is not None else time.time())
    nonce = secrets.token_urlsafe(9)
    body = f"{int(owner_id)}.{issued}.{nonce}"
    mac = hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{mac}"


def verify_session(token: str, *, now: float | None = None) -> int | None:
    """-> owner_id, если подпись верна и токен не протух; иначе None. Константное сравнение."""
    if not token or not TOKEN or not OWNER_ID:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    owner_s, issued_s, nonce, mac = parts
    body = f"{owner_s}.{issued_s}.{nonce}"
    calc = hmac.new(_session_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(calc, mac):
        return None
    try:
        owner = int(owner_s)
        issued = int(issued_s)
    except ValueError:
        return None
    if owner != OWNER_ID:
        return None
    ts = now if now is not None else time.time()
    if SESSION_TTL > 0 and (ts - issued) > SESSION_TTL:
        return None
    return owner


def _bearer(request) -> str:
    values = request.headers.getall("Authorization", [])
    if len(values) != 1:
        return ""
    parts = values[0].split(" ", 1)
    return parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""


def _request_is_owner(request) -> bool:
    """Owner via Telegram initData (webview) ИЛИ подписанная сессия (standalone PWA)."""
    init = request.headers.get("X-Telegram-Init-Data", "") or request.query.get("_auth", "")
    if init and _is_owner(init):
        return True
    tok = _bearer(request)
    return bool(tok and verify_session(tok) is not None)


def _is_agent(token: str) -> bool:
    """PASS 17.A: это она сама (её контейнер по внутренней сети). Fail-closed:
    пустой AGENT_TOKEN закрывает дверь для всех, а не открывает для всех.

    Сравниваем БАЙТЫ: compare_digest на не-ASCII строках бросает TypeError, а гейт,
    падающий от произвольного заголовка снаружи, — это не гейт (регресс держит тест)."""
    if not AGENT_TOKEN or not token:
        return False
    return hmac.compare_digest(token.encode("utf-8", "surrogateescape"),
                               AGENT_TOKEN.encode("utf-8", "surrogateescape"))


# --------------------------------------------------------------------------- #
#  Хост: /proc (pid:host) — CPU, память, аптайм, процессы, порты
# --------------------------------------------------------------------------- #

def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def cpu_times(proc: str = PROC) -> dict[str, list[int]]:
    """Строки cpu*/cpuN из /proc/stat → {'cpu': [...], 'cpu0': [...], ...}."""
    out: dict[str, list[int]] = {}
    for line in _read(f"{proc}/stat").splitlines():
        if line.startswith("cpu"):
            parts = line.split()
            out[parts[0]] = [int(x) for x in parts[1:]]
    return out


def cpu_percent_from(t0: dict, t1: dict) -> dict:
    """Два снимка cpu_times → {'total': pct, 'cores': [pct,..]}. idle = idle+iowait."""
    def pct(a: list[int], b: list[int]) -> float:
        d_total = sum(b) - sum(a)
        if d_total <= 0:
            return 0.0
        idle_a = a[3] + (a[4] if len(a) > 4 else 0)
        idle_b = b[3] + (b[4] if len(b) > 4 else 0)
        return round(max(0.0, min(100.0, (1 - (idle_b - idle_a) / d_total) * 100)), 1)
    cores = sorted(k for k in t0 if k != "cpu" and k in t1)
    return {"total": pct(t0.get("cpu", [0]), t1.get("cpu", [0])),
            "cores": [pct(t0[k], t1[k]) for k in cores]}


def meminfo(proc: str = PROC) -> dict:
    """kB из /proc/meminfo → total/available/used/swap (+used_pct)."""
    kv = {}
    for line in _read(f"{proc}/meminfo").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            kv[m.group(1)] = int(m.group(2)) * 1024
    total = kv.get("MemTotal", 0)
    avail = kv.get("MemAvailable", 0)
    used = max(0, total - avail)
    st, sf = kv.get("SwapTotal", 0), kv.get("SwapFree", 0)
    return {"total": total, "available": avail, "used": used,
            "used_pct": round(used / total * 100, 1) if total else 0.0,
            "buff_cache": kv.get("Buffers", 0) + kv.get("Cached", 0),
            "swap_total": st, "swap_used": max(0, st - sf)}


def loadavg(proc: str = PROC) -> dict:
    parts = _read(f"{proc}/loadavg").split()
    if len(parts) < 4:
        return {"m1": 0.0, "m5": 0.0, "m15": 0.0, "tasks": ""}
    return {"m1": float(parts[0]), "m5": float(parts[1]), "m15": float(parts[2]),
            "tasks": parts[3]}


def uptime_seconds(proc: str = PROC) -> float:
    try:
        return float(_read(f"{proc}/uptime").split()[0])
    except Exception:
        return 0.0


# --- процессы ---------------------------------------------------------------

_STAT_RE = re.compile(r"^(\d+) \((.*)\) (\S) (.+)$", re.S)


def _proc_stat(proc: str, pid: str) -> dict | None:
    """Одна строка /proc/PID/stat → pid, comm, state, ticks (utime+stime)."""
    m = _STAT_RE.match(_read(f"{proc}/{pid}/stat"))
    if not m:
        return None
    rest = m.group(4).split()
    try:  # rest[0] = ppid (поле 4); utime/stime = поля 14/15 → rest[10]/rest[11]
        return {"pid": int(m.group(1)), "comm": m.group(2), "state": m.group(3),
                "ppid": int(rest[0]), "ticks": int(rest[10]) + int(rest[11])}
    except Exception:
        return None


def _proc_uid(proc: str, pid: str) -> int:
    m = re.search(r"^Uid:\s+(\d+)", _read(f"{proc}/{pid}/status"), re.M)
    return int(m.group(1)) if m else -1


def _proc_rss(proc: str, pid: str) -> int:
    parts = _read(f"{proc}/{pid}/statm").split()
    return int(parts[1]) * PAGE if len(parts) > 1 else 0


def _pids(proc: str) -> list[str]:
    try:
        return [d for d in os.listdir(proc) if d.isdigit()]
    except Exception:
        return []


def processes_snapshot(proc: str = PROC, interval: float = 0.5,
                       _sleep=time.sleep) -> dict:
    """Два прохода по /proc/*/stat: топ по CPU и по памяти + счётчики состояний."""
    t0: dict[int, int] = {}
    for pid in _pids(proc):
        st = _proc_stat(proc, pid)
        if st:
            t0[st["pid"]] = st["ticks"]
    _sleep(interval)
    rows, states = [], {"R": 0, "S": 0, "D": 0, "Z": 0, "T": 0, "I": 0}
    uid_names = {u["uid"]: u["name"] for u in parse_passwd(HOSTFS)} if HOSTFS else {}
    for pid in _pids(proc):
        st = _proc_stat(proc, pid)
        if not st:
            continue
        states[st["state"]] = states.get(st["state"], 0) + 1
        dt = st["ticks"] - t0.get(st["pid"], st["ticks"])
        cpu = round(dt / (CLK_TCK * interval) * 100, 1) if interval > 0 else 0.0
        rss = _proc_rss(proc, pid)
        uid = _proc_uid(proc, pid)
        rows.append({"pid": st["pid"], "comm": st["comm"], "state": st["state"],
                     "cpu": cpu, "rss": rss,
                     "user": uid_names.get(uid, str(uid) if uid >= 0 else "?")})
    top_cpu = sorted(rows, key=lambda r: (-r["cpu"], -r["rss"]))[:20]
    top_mem = sorted(rows, key=lambda r: -r["rss"])[:20]
    return {"total": len(rows), "states": states, "top_cpu": top_cpu, "top_mem": top_mem}


# --- порты: /proc/1/net/{tcp,tcp6,udp,udp6} ----------------------------------

def _hex_ip4(h: str) -> str:
    b = bytes.fromhex(h)
    return ".".join(str(x) for x in b[::-1])


def _hex_ip6(h: str) -> str:
    # 4 группы по 32 бита, каждая little-endian (формат ядра в /proc/net/tcp6)
    import ipaddress
    raw = b"".join(bytes.fromhex(h[i:i + 8])[::-1] for i in range(0, 32, 8))
    addr = ipaddress.IPv6Address(raw)
    return str(addr.ipv4_mapped or addr)


def parse_net_listeners(net_dir: str) -> list[dict]:
    """Слушающие сокеты из procfs: tcp state 0A (LISTEN), udp state 07 (unconnected)."""
    out = []
    for fname, proto, want in (("tcp", "tcp", "0A"), ("tcp6", "tcp6", "0A"),
                               ("udp", "udp", "07"), ("udp6", "udp6", "07")):
        for line in _read(f"{net_dir}/{fname}").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != want:
                continue
            addr, port_hex = parts[1].rsplit(":", 1)
            ip = _hex_ip4(addr) if len(addr) == 8 else _hex_ip6(addr)
            out.append({"proto": proto.rstrip("6"), "v6": fname.endswith("6"),
                        "ip": ip, "port": int(port_hex, 16), "inode": int(parts[9])})
    return out


def socket_owners(proc: str, inodes: set[int]) -> dict[int, int]:
    """inode сокета → pid (скан /proc/*/fd; нужен CAP_SYS_PTRACE + pid:host)."""
    owners: dict[int, int] = {}
    left = set(inodes)
    for pid in _pids(proc):
        if not left:
            break
        try:
            fds = os.listdir(f"{proc}/{pid}/fd")
        except Exception:
            continue
        for fd in fds:
            try:
                tgt = os.readlink(f"{proc}/{pid}/fd/{fd}")
            except Exception:
                continue
            if tgt.startswith("socket:["):
                ino = int(tgt[8:-1])
                if ino in left:
                    owners[ino] = int(pid)
                    left.discard(ino)
    return owners


def _cmdline(proc: str, pid: int) -> str:
    return _read(f"{proc}/{pid}/cmdline").replace("\0", " ").strip()


def listening_ports(proc: str = PROC) -> list[dict]:
    """Порты + кто слушает; docker-proxy расшифровываем в контейнер по -container-ip."""
    socks = parse_net_listeners(f"{proc}/1/net")
    owners = socket_owners(proc, {s["inode"] for s in socks})
    # дедуп: один порт может висеть и v4 и v6 у одного процесса
    seen: dict[tuple, dict] = {}
    for s in socks:
        pid = owners.get(s["inode"], 0)
        comm = _read(f"{proc}/{pid}/comm").strip() if pid else ""
        cmd = _cmdline(proc, pid) if pid else ""
        target = ""
        if comm == "docker-proxy":
            m = re.search(r"-container-ip (\S+).*?-container-port (\d+)", cmd)
            if m:
                target = f"{m.group(1)}:{m.group(2)}"
        key = (s["proto"], s["port"], comm or s["ip"])
        cur = seen.get(key)
        if cur:
            cur["ips"].append(s["ip"])
        else:
            seen[key] = {"proto": s["proto"], "port": s["port"], "ips": [s["ip"]],
                         "pid": pid, "comm": comm, "cmd": cmd[:200], "target": target}
    return sorted(seen.values(), key=lambda r: (r["port"], r["proto"]))


# --------------------------------------------------------------------------- #
#  Хост: пропускная способность — /proc/1/net/dev (net-ns хоста), /proc/diskstats
# --------------------------------------------------------------------------- #

# виртуальные интерфейсы, которые не считаем «настоящим» трафиком сервера
_VIRT_IFACE = ("lo", "docker", "br-", "veth", "virbr", "tun", "tap", "cni",
               "flannel", "kube", "vnet", "ovs", "wg", "cali", "nerdctl")
# «целые» диски (без разделов/loop/dm): sda, vda, nvme0n1, xvda…
_WHOLE_DISK = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")


def parse_net_dev(path: str, phys_only: bool = True) -> dict[str, tuple[int, int]]:
    """/proc/1/net/dev → {iface: (rx_bytes, tx_bytes)}. Хостовый — через /proc/1."""
    out: dict[str, tuple[int, int]] = {}
    for line in _read(path).splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        f = rest.split()
        if len(f) < 16:
            continue
        if phys_only and (name == "lo" or name.startswith(_VIRT_IFACE)):
            continue
        out[name] = (int(f[0]), int(f[8]))   # rx bytes (поле 1), tx bytes (поле 9)
    return out


def parse_diskstats(path: str) -> dict[str, tuple[int, int]]:
    """/proc/diskstats → {dev: (read_bytes, write_bytes)}, только целые диски."""
    out: dict[str, tuple[int, int]] = {}
    for line in _read(path).splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        if not _WHOLE_DISK.match(name):
            continue
        out[name] = (int(f[5]) * 512, int(f[9]) * 512)  # сектора чтения/записи ×512
    return out


def counter_rate(prev: dict, cur: dict, dt: float) -> tuple[float, float]:
    """Сумма приростов двух пар-счётчиков по общим ключам, делённая на dt (>=0)."""
    a = b = 0
    for k, (ca, cb) in cur.items():
        if k in prev:
            da, db = ca - prev[k][0], cb - prev[k][1]
            if da >= 0:
                a += da
            if db >= 0:
                b += db
    return (a / dt if dt > 0 else 0.0, b / dt if dt > 0 else 0.0)


def quick_proc_counts(proc: str = PROC) -> dict:
    """Дёшево: сколько процессов всего и сколько в состоянии R (бегут)."""
    pids = _pids(proc)
    run = 0
    for p in pids:
        m = _STAT_RE.match(_read(f"{proc}/{p}/stat"))
        if m and m.group(3) == "R":
            run += 1
    return {"total": len(pids), "running": run}


class Sampler:
    """Фоновый сэмплер лёгких хост-метрик: ведёт короткую историю (по умолчанию
    150 точек × 2с = 5 мин), чтобы график был заполнен сразу при открытии, а не
    рос с нуля. Тяжёлое (docker-счётчик) — раз в несколько тиков."""

    def __init__(self, proc: str = PROC, session_getter=None, keep: int = 150):
        self.proc = proc
        self._sg = session_getter
        self.keep = keep
        self.hist = {k: deque(maxlen=keep) for k in
                     ("ts", "cpu", "mem", "load", "net_rx", "net_tx",
                      "disk_r", "disk_w")}
        self.cur: dict = {}
        self._prev_cpu = None
        self._prev_net = None
        self._prev_disk = None
        self._prev_mono = None
        self._tick = 0
        self._procs = {"total": 0, "running": 0}
        self._docker = {"total": 0, "running": 0}

    async def run(self, interval: float = 2.0):
        while True:
            try:
                await self.tick(interval)
            except Exception as e:                       # pragma: no cover
                log.warning("sampler tick: %s", e)
            await asyncio.sleep(interval)

    async def tick(self, interval: float = 2.0):
        now, mono = time.time(), time.monotonic()
        ct = cpu_times(self.proc)
        cpu = (cpu_percent_from(self._prev_cpu, ct)
               if self._prev_cpu else {"total": 0.0, "cores": []})
        self._prev_cpu = ct
        mem = meminfo(self.proc)
        ld = loadavg(self.proc)
        net = parse_net_dev(f"{self.proc}/1/net/dev")
        dsk = parse_diskstats(f"{self.proc}/diskstats")
        dt = (mono - self._prev_mono) if self._prev_mono else interval
        nrx, ntx = counter_rate(self._prev_net, net, dt) if self._prev_net else (0.0, 0.0)
        dr, dw = counter_rate(self._prev_disk, dsk, dt) if self._prev_disk else (0.0, 0.0)
        self._prev_net, self._prev_disk, self._prev_mono = net, dsk, mono
        self.hist["ts"].append(int(now))
        self.hist["cpu"].append(cpu["total"])
        self.hist["mem"].append(mem.get("used_pct", 0.0))
        self.hist["load"].append(ld.get("m1", 0.0))
        self.hist["net_rx"].append(round(nrx))
        self.hist["net_tx"].append(round(ntx))
        self.hist["disk_r"].append(round(dr))
        self.hist["disk_w"].append(round(dw))
        self.cur = {"cpu": cpu, "mem": mem, "load": ld,
                    "net": {"rx": round(nrx), "tx": round(ntx),
                            "ifaces": sorted(net.keys())},
                    "disk": {"read": round(dr), "write": round(dw),
                             "devs": sorted(dsk.keys())}}
        self._tick += 1
        if self._tick % 3 == 1:
            self._procs = quick_proc_counts(self.proc)
        if self._sg and self._tick % 3 == 2:
            try:
                cs = await _dget(self._sg(), "/containers/json?all=1")
                self._docker = {"total": len(cs),
                                "running": sum(1 for c in cs
                                               if c.get("State") == "running")}
            except Exception:
                pass

    def snapshot(self) -> dict:
        return {"t": int(time.time()), "interval": 2,
                "cpu": self.cur.get("cpu", {"total": 0.0, "cores": []}),
                "mem": self.cur.get("mem", {}), "load": self.cur.get("load", {}),
                "net": self.cur.get("net", {}), "disk": self.cur.get("disk", {}),
                "procs": self._procs, "docker": self._docker,
                "hist": {k: list(v) for k, v in self.hist.items()}}


# --------------------------------------------------------------------------- #
#  Хост: /rootfs — юзеры, группы, ssh, логины, диски, ось
# --------------------------------------------------------------------------- #

def parse_passwd(hostfs: str = HOSTFS) -> list[dict]:
    out = []
    for line in _read(f"{hostfs}/etc/passwd").splitlines():
        p = line.split(":")
        if len(p) < 7 or line.startswith("#"):
            continue
        try:
            uid = int(p[2])
        except ValueError:
            continue
        shell = p[6]
        human = uid == 0 or (1000 <= uid < 60000
                             and not shell.endswith(("nologin", "false", "sync")))
        out.append({"name": p[0], "uid": uid, "home": p[5], "shell": shell,
                    "human": human})
    return out


def parse_groups(hostfs: str = HOSTFS, names=("sudo", "docker", "adm", "wheel")) -> dict:
    got = {}
    for line in _read(f"{hostfs}/etc/group").splitlines():
        p = line.split(":")
        if len(p) >= 4 and p[0] in names:
            got[p[0]] = [m for m in p[3].split(",") if m]
    return got


def parse_sshd(hostfs: str = HOSTFS) -> dict:
    """Ключевые «двери» из sshd_config (эффективные строки, без Include-глубины)."""
    keys = {"permitrootlogin": None, "passwordauthentication": None,
            "pubkeyauthentication": None, "port": None}
    text = _read(f"{hostfs}/etc/ssh/sshd_config")
    for d in sorted(Path(f"{hostfs}/etc/ssh/sshd_config.d").glob("*.conf")
                    if Path(f"{hostfs}/etc/ssh/sshd_config.d").is_dir() else []):
        text = _read(str(d)) + "\n" + text  # .d-файлы главнее (первое вхождение)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        k = parts[0].lower()
        if k in keys and keys[k] is None and len(parts) > 1:
            keys[k] = parts[1].strip()
    return {"permit_root_login": keys["permitrootlogin"] or "yes*",
            "password_auth": keys["passwordauthentication"] or "yes*",
            "pubkey_auth": keys["pubkeyauthentication"] or "yes*",
            "port": keys["port"] or "22"}


def authorized_keys_count(hostfs: str, home: str) -> int:
    text = _read(f"{hostfs}{home}/.ssh/authorized_keys")
    return sum(1 for line in text.splitlines()
               if line.strip() and not line.strip().startswith("#"))


_UTMP_FMT = "<h2xi32s4s32s256s2hi2i4i20x"   # glibc utmp, 384 байта
_UTMP_SIZE = struct.calcsize(_UTMP_FMT)


def parse_utmp(path: str, only_user: bool = True, limit: int = 40) -> list[dict]:
    """Записи utmp/wtmp (логины). type 7 = USER_PROCESS, 2 = BOOT_TIME."""
    try:
        raw = Path(path).read_bytes()
    except Exception:
        return []
    out = []
    for off in range(0, len(raw) - _UTMP_SIZE + 1, _UTMP_SIZE):
        t, pid, line, _id, user, host, _e1, _e2, _sess, sec, _usec, *_ = \
            struct.unpack_from(_UTMP_FMT, raw, off)
        if only_user and t not in (7, 2):
            continue
        s = lambda b: b.split(b"\0", 1)[0].decode("utf-8", "replace")
        out.append({"type": t, "user": s(user) if t == 7 else "(загрузка)",
                    "line": s(line), "host": s(host), "ts": sec})
    return out[-limit:][::-1]


def os_release(hostfs: str = HOSTFS) -> dict:
    kv = {}
    for line in _read(f"{hostfs}/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k] = v.strip().strip('"')
    return kv


def kernel_facts(proc: str = PROC, hostfs: str = HOSTFS) -> dict:
    cpu_model, mhz, cores = "", 0.0, 0
    for line in _read(f"{proc}/cpuinfo").splitlines():
        if line.startswith("model name") and not cpu_model:
            cpu_model = line.split(":", 1)[1].strip()
        elif line.startswith("cpu MHz") and not mhz:
            try:
                mhz = float(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("processor"):
            cores += 1
    fn = _read(f"{proc}/sys/fs/file-nr").split()
    osr = os_release(hostfs)
    # hostname: UTS-неймспейс не шарится через pid:host — берём с диска хоста
    hostname = _read(f"{hostfs}/etc/hostname").strip() \
        or _read(f"{proc}/sys/kernel/hostname").strip()
    return {
        "kernel": _read(f"{proc}/sys/kernel/osrelease").strip(),
        "ostype": _read(f"{proc}/sys/kernel/ostype").strip(),
        "hostname": hostname,
        "version_line": _read(f"{proc}/version").strip(),
        "os_pretty": osr.get("PRETTY_NAME", "?"),
        "cpu_model": cpu_model, "cpu_mhz": round(mhz), "cpu_cores": cores,
        "pid_max": _read(f"{proc}/sys/kernel/pid_max").strip(),
        "threads": _read(f"{proc}/sys/kernel/threads-max").strip(),
        "swappiness": _read(f"{proc}/sys/vm/swappiness").strip(),
        "file_nr": {"open": int(fn[0]) if fn else 0,
                    "max": int(fn[2]) if len(fn) > 2 else 0},
        "entropy": _read(f"{proc}/sys/kernel/random/entropy_avail").strip(),
    }


def detect_virt(proc: str = PROC, hostfs: str = HOSTFS) -> dict:
    """Виртуализирован ли хост и чем. Читаем DMI (/sys), флаг hypervisor в cpuinfo.

    Смысл для наглядности: контейнеры — НЕ аппаратная виртуализация (общее ядро,
    namespaces/cgroups), а вот сам хост — гость KVM/QEMU поверх гипервизора и железа."""
    def sysdmi(name):
        return (_read(f"{hostfs}/sys/class/dmi/id/{name}").strip()
                or _read(f"/sys/class/dmi/id/{name}").strip())
    vendor = sysdmi("sys_vendor")
    product = sysdmi("product_name")
    bios = sysdmi("bios_vendor")
    chassis = sysdmi("chassis_vendor")
    blob = " ".join((vendor, product, bios, chassis)).lower()
    cpuinfo = _read(f"{proc}/cpuinfo")
    hyp_flag = " hypervisor" in cpuinfo or "\thypervisor" in cpuinfo \
        or bool(re.search(r"flags\s*:.*\bhypervisor\b", cpuinfo))
    kind, kindname = "bare", "физическое железо"
    if "qemu" in blob or "kvm" in blob:
        kind, kindname = "kvm", "KVM / QEMU"
    elif "vmware" in blob:
        kind, kindname = "vmware", "VMware"
    elif "microsoft" in blob or "hyper-v" in blob:
        kind, kindname = "hyperv", "Microsoft Hyper-V"
    elif "xen" in blob or _read(f"{hostfs}/sys/hypervisor/type").strip():
        kind, kindname = "xen", "Xen"
    elif "virtualbox" in blob or "innotek" in blob:
        kind, kindname = "vbox", "VirtualBox"
    elif "amazon" in blob or "ec2" in blob:
        kind, kindname = "kvm", "Amazon EC2 (Nitro/KVM)"
    elif hyp_flag:
        kind, kindname = "vm", "виртуальная машина (тип неизвестен)"
    return {"virtual": kind != "bare", "kind": kind, "kindname": kindname,
            "hypervisor_flag": hyp_flag, "vendor": vendor or "?",
            "product": product or "?", "bios": bios or "?"}


async def docker_info(session) -> dict:
    """Движок из /info и /version: версия, containerd/runc, драйвер, cgroup, охрана."""
    info = await _dget(session, "/info")
    ver = {}
    try:
        ver = await _dget(session, "/version")
    except Exception:
        pass
    comps = {c.get("Name", ""): c.get("Version", "")
             for c in (ver.get("Components") or [])}
    return {
        "engine": info.get("ServerVersion", ver.get("Version", "?")),
        "containerd": comps.get("containerd", ""),
        "runc": comps.get("runc", ""),
        "driver": info.get("Driver", ""),
        "cgroup": info.get("CgroupVersion", ""),
        "runtime": info.get("DefaultRuntime", ""),
        "security": info.get("SecurityOptions") or [],
        "ncpu": info.get("NCPU", 0),
        "mem_total": info.get("MemTotal", 0),
        "kernel": info.get("KernelVersion", ""),
        "os": info.get("OperatingSystem", ""),
        "arch": info.get("Architecture", ""),
        "containers": info.get("Containers", 0),
        "running": info.get("ContainersRunning", 0),
        "images": info.get("Images", 0),
        "name": info.get("Name", ""),
    }


def disks(proc: str = PROC, hostfs: str = HOSTFS) -> list[dict]:
    """Реальные ФС из /proc/1/mounts + statvfs через /rootfs."""
    seen_dev: dict[str, dict] = {}
    for line in _read(f"{proc}/1/mounts").splitlines():
        p = line.split()
        if len(p) < 3 or not p[0].startswith("/dev/"):
            continue
        dev, mnt, fstype = p[0], p[1].replace("\\040", " "), p[2]
        if fstype in ("squashfs", "iso9660"):
            continue
        if dev in seen_dev and len(seen_dev[dev]["mount"]) <= len(mnt):
            continue
        try:
            st = os.statvfs(hostfs + mnt if mnt != "/" else hostfs or "/")
        except Exception:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        seen_dev[dev] = {"dev": dev, "mount": mnt, "fstype": fstype, "total": total,
                         "used": total - free, "free": free,
                         "used_pct": round((1 - free / total) * 100, 1) if total else 0}
    return sorted(seen_dev.values(), key=lambda d: -d["total"])


# --------------------------------------------------------------------------- #
#  Docker через socket-proxy (только GET)
# --------------------------------------------------------------------------- #

async def _dget(session, path: str, *, raw: bool = False, timeout: float = 20.0):
    async with session.get(DOCKER + path,
                           timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        if r.status != 200:
            raise RuntimeError(f"docker {path} -> {r.status}")
        return await (r.read() if raw else r.json())


def shape_container(c: dict) -> dict:
    """Элемент /containers/json → компактная карточка."""
    name = (c.get("Names") or ["?"])[0].lstrip("/")
    ports = []
    for p in c.get("Ports") or []:
        ports.append({"private": p.get("PrivatePort"), "public": p.get("PublicPort"),
                      "proto": p.get("Type"), "ip": p.get("IP", "")})
    nets = {}
    for nname, n in ((c.get("NetworkSettings") or {}).get("Networks") or {}).items():
        nets[nname] = n.get("IPAddress", "")
    labels = c.get("Labels") or {}
    return {"id": (c.get("Id") or "")[:12], "name": name,
            "image": c.get("Image", ""), "state": c.get("State", ""),
            "status": c.get("Status", ""), "created": c.get("Created", 0),
            "ports": ports, "networks": nets,
            "compose": labels.get("com.docker.compose.project", "")}


def stats_percent(s: dict) -> dict:
    """JSON /containers/id/stats → {cpu_pct, mem_used, mem_limit, mem_pct}."""
    try:
        cs, ps = s["cpu_stats"], s["precpu_stats"]
        cpu_d = cs["cpu_usage"]["total_usage"] - ps["cpu_usage"]["total_usage"]
        sys_d = cs.get("system_cpu_usage", 0) - ps.get("system_cpu_usage", 0)
        ncpu = cs.get("online_cpus") or len(cs["cpu_usage"].get("percpu_usage") or [1])
        cpu = round(cpu_d / sys_d * ncpu * 100, 1) if sys_d > 0 else 0.0
    except Exception:
        cpu = 0.0
    mem = s.get("memory_stats") or {}
    used = mem.get("usage", 0) - (mem.get("stats") or {}).get("inactive_file", 0)
    limit = mem.get("limit", 0)
    return {"cpu_pct": max(0.0, cpu), "mem_used": max(0, used), "mem_limit": limit,
            "mem_pct": round(used / limit * 100, 1) if limit else 0.0}


def demux_logs(raw: bytes) -> str:
    """Логи docker: мультиплекс-фреймы [type,0,0,0,len4BE] либо сырой поток (tty)."""
    if not raw:
        return ""
    if raw[0] not in (0, 1, 2) or len(raw) < 8:
        return raw.decode("utf-8", "replace")
    out, i = [], 0
    while i + 8 <= len(raw):
        if raw[i] not in (0, 1, 2):     # не фрейм — значит, поток сырой
            return raw.decode("utf-8", "replace")
        n = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + n])
        i += 8 + n
    return b"".join(out).decode("utf-8", "replace")


def isolation(j: dict) -> dict:
    """Насколько «тонки стены» контейнера: привилегии, capabilities, доступ к хосту.

    Возвращает и факты, и производный `level` (tight/normal/loose/host) + причины —
    чтобы фронт честно раскрасил, кто изолирован строго, а кто дырявит хост."""
    hc = j.get("HostConfig") or {}
    caps = hc.get("CapAdd") or []
    secopt = hc.get("SecurityOpt") or []
    privileged = bool(hc.get("Privileged"))
    net_mode = hc.get("NetworkMode", "") or ""
    pid_mode = hc.get("PidMode", "") or ""
    ro_root = bool(hc.get("ReadonlyRootfs"))
    extra_hosts = hc.get("ExtraHosts") or []
    mounts = j.get("Mounts") or []
    sock = any("docker.sock" in (m.get("Source") or "") for m in mounts)
    apparmor_off = any("apparmor=unconfined" in s for s in secopt)
    seccomp_off = any("seccomp=unconfined" in s for s in secopt)
    reasons = []
    if privileged:
        reasons.append("privileged — почти как root на хосте")
    if sock:
        reasons.append("монтирует docker.sock — управляет всем докером")
    if pid_mode == "host":
        reasons.append("PID хоста — видит все процессы")
    if net_mode == "host":
        reasons.append("сеть хоста — без своего сетевого стека")
    for c in caps:
        reasons.append(f"capability {c}")
    if apparmor_off:
        reasons.append("AppArmor снят")
    if seccomp_off:
        reasons.append("seccomp снят")
    if privileged or sock:
        level = "host"        # фактически ключ от хоста
    elif net_mode == "host" or pid_mode == "host" or caps or apparmor_off or seccomp_off:
        level = "loose"       # прогрызена дырка в изоляции
    elif ro_root:
        level = "tight"       # дополнительно закалён (rootfs read-only)
        reasons.append("rootfs только для чтения")
    else:
        level = "normal"      # стандартные стены Docker: свои namespaces, apparmor+seccomp
    return {"level": level, "privileged": privileged, "caps": caps,
            "security_opt": secopt, "net_mode": net_mode, "pid_mode": pid_mode,
            "readonly_rootfs": ro_root, "extra_hosts": extra_hosts,
            "mounts_docker_sock": sock, "reasons": reasons}


def shape_inspect(j: dict) -> dict:
    st = j.get("State") or {}
    hc = (st.get("Health") or {})
    cfg = j.get("Config") or {}
    mounts = [{"src": m.get("Source", ""), "dst": m.get("Destination", ""),
               "rw": m.get("RW", True), "type": m.get("Type", "")}
              for m in j.get("Mounts") or []]
    cmd = cfg.get("Cmd") or cfg.get("Entrypoint") or []
    if not isinstance(cmd, str):
        cmd = " ".join(cmd)
    nets = {}
    for nname, n in ((j.get("NetworkSettings") or {}).get("Networks") or {}).items():
        nets[nname] = n.get("IPAddress", "")
    return {"id": (j.get("Id") or "")[:12],
            "name": (j.get("Name") or "?").lstrip("/"),
            "image": cfg.get("Image", ""),
            "cmd": cmd,
            "started": st.get("StartedAt", ""), "state": st.get("Status", ""),
            "pid": st.get("Pid", 0), "restarts": j.get("RestartCount", 0),
            "health": hc.get("Status", ""),
            "env_count": len(cfg.get("Env") or []),   # значения env не отдаём: секреты
            "mounts": mounts, "networks": nets,
            "isolation": isolation(j),
            "restart_policy": ((j.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", "")}


def reachability(target: dict, all_containers: list[dict]) -> dict:
    """К чему у контейнера есть доступ: соседи по сетям, хост-пути, шлюз, публикации.

    `target` — результат shape_inspect (нужны networks/mounts/isolation),
    `all_containers` — shape_container-карточки всех, чтобы найти соседей по сети."""
    iso = target.get("isolation") or {}
    my_nets = set((target.get("networks") or {}).keys())
    peers = []
    for c in all_containers:
        if c["name"] == target["name"]:
            continue
        shared = my_nets & set((c.get("networks") or {}).keys())
        if shared:
            peers.append({"name": c["name"], "state": c["state"],
                          "via": sorted(shared),
                          "ip": (c.get("networks") or {}).get(sorted(shared)[0], "")})
    host_paths = [{"src": m["src"], "dst": m["dst"], "rw": m["rw"]}
                  for m in target.get("mounts") or [] if m.get("type") == "bind"]
    published = [{"public": p["public"], "private": p["private"], "proto": p["proto"]}
                 for p in target.get("ports") or [] if p.get("public")]
    host_gateway = any("host-gateway" in h for h in iso.get("extra_hosts") or [])
    return {
        "networks": sorted(my_nets),
        "peers": sorted(peers, key=lambda p: p["name"]),
        "host_paths": host_paths,
        "host_gateway": host_gateway,     # достаёт loopback хоста (host.docker.internal)
        "docker_control": iso.get("mounts_docker_sock") or iso.get("privileged"),
        "published": published,
        "level": iso.get("level"),
    }


def build_netmap(containers: list[dict], networks: list[dict],
                 focus: str = "") -> dict:
    """Граф: контейнеры ↔ сети ↔ опубликованные порты (мир снаружи).

    `focus` — имя контейнера; если задан, узлы вне его достижимости помечаются
    dim, чтобы «подсветить», к чему у него есть доступ."""
    nodes, links = [], []
    nodes.append({"id": "host", "kind": "host", "label": "хост Атланта"})
    net_used = set()
    reach = set()
    focus_nets = set()
    if focus:
        for c in containers:
            if c["name"] == focus:
                focus_nets = set((c.get("networks") or {}).keys())
        for c in containers:
            if focus_nets & set((c.get("networks") or {}).keys()):
                reach.add("c:" + c["name"])
        reach |= {"n:" + n for n in focus_nets}
    for c in containers:
        nodes.append({"id": "c:" + c["name"], "kind": "container",
                      "label": c["name"], "state": c["state"],
                      "level": c.get("level", ""),
                      "focus": c["name"] == focus,
                      "reach": ("c:" + c["name"]) in reach if focus else True})
        for nname, ip in (c.get("networks") or {}).items():
            net_used.add(nname)
            links.append({"a": "c:" + c["name"], "b": "n:" + nname, "label": ip})
        for p in c.get("ports") or []:
            if p.get("public"):
                pid = f"p:{p['public']}/{p['proto']}"
                if not any(n["id"] == pid for n in nodes):
                    nodes.append({"id": pid, "kind": "port",
                                  "label": f":{p['public']}/{p['proto']}",
                                  "reach": True})
                    links.append({"a": pid, "b": "host", "label": ""})
                links.append({"a": "c:" + c["name"], "b": pid,
                              "label": f"→{p['private']}"})
    for n in networks:
        nname = n.get("Name", "")
        if nname in net_used or nname in ("bridge", "host"):
            nodes.append({"id": "n:" + nname, "kind": "network", "label": nname,
                          "driver": n.get("Driver", ""),
                          "internal": n.get("Internal", False),
                          "reach": ("n:" + nname) in reach if focus else True})
    known = {n["id"] for n in nodes}
    links = [l for l in links if l["a"] in known and l["b"] in known]
    return {"nodes": nodes, "links": links, "focus": focus}


# --------------------------------------------------------------------------- #
#  Сборные payload'ы
# --------------------------------------------------------------------------- #

def praxis_heartbeat() -> dict:
    """Кто хозяйка сервера: HEAD её репо (код примонтирован в /app ro)."""
    try:
        out = subprocess.run(["git", "-C", str(BASE), "log", "-1",
                              "--format=%h|%ct|%s"], capture_output=True,
                             text=True, timeout=10)
        h, ct, s = (out.stdout.strip().split("|", 2) + ["", ""])[:3]
        return {"head": h, "ts": int(ct or 0), "subject": s}
    except Exception:
        return {"head": "", "ts": 0, "subject": ""}


async def collect_overview(session) -> dict:
    async def _cpu():
        t0 = await _to_thread(cpu_times)
        await asyncio.sleep(0.45)
        return cpu_percent_from(t0, await _to_thread(cpu_times))

    async def _docker():
        cs = await _dget(session, "/containers/json?all=1")
        return {"total": len(cs),
                "running": sum(1 for c in cs if c.get("State") == "running")}

    def err(x, d):
        return d if isinstance(x, Exception) else x

    cpu, dock, mem, dsk, kf, ports, virt = await asyncio.gather(
        _cpu(), _docker(), _to_thread(meminfo), _to_thread(disks),
        _to_thread(kernel_facts), _to_thread(listening_ports),
        _to_thread(detect_virt), return_exceptions=True)
    kf = err(kf, {})
    users = [u for u in parse_passwd() if u["human"]]
    sess = [r for r in parse_utmp(f"{HOSTFS}/var/run/utmp") if r["type"] == 7]
    root = next((d for d in err(dsk, []) if d["mount"] == "/"), None)
    dock = err(dock, None) or {"total": 0, "running": 0,
                               "error": str(dock) if isinstance(dock, Exception) else ""}
    return {"now": int(time.time()),
            "host": {"hostname": kf.get("hostname", "?"),
                     "os": kf.get("os_pretty", "?"), "kernel": kf.get("kernel", "?"),
                     "uptime": uptime_seconds()},
            "virt": err(virt, {}),
            "cpu": {**err(cpu, {"total": 0.0, "cores": []}), "load": loadavg()},
            "mem": err(mem, {}), "disk_root": root, "docker": dock,
            "ports": len(err(ports, [])), "users": len(users), "sessions": len(sess),
            "praxis": praxis_heartbeat()}


async def _enrich_isolation(session, cards: list[dict]):
    """Дёшево дотянуть уровень изоляции каждому контейнеру (inspect без stats)."""
    sem = asyncio.Semaphore(5)

    async def one(c):
        async with sem:
            try:
                j = await _dget(session, f"/containers/{c['id']}/json")
                iso = isolation(j)
                c["level"] = iso["level"]
                c["iso_reasons"] = iso["reasons"]
            except Exception:
                c["level"] = ""
    await asyncio.gather(*(one(c) for c in cards))


async def collect_containers(session) -> dict:
    cards = [shape_container(c) for c in await _dget(session, "/containers/json?all=1")]
    # /stats?stream=false blocks ~1s per container (docker samples a CPU delta). With a
    # low semaphore this fan-out grew past _COLLECT_TIMEOUT as the box gained containers
    # and tripped the breaker permanently ("докер молчит"). Run all stats concurrently
    # with a per-call timeout so one hung container cannot eat the whole budget; a slow
    # or failing stat yields None (the card still lists, just without live %).
    running = [c for c in cards if c["state"] == "running"]
    sem = asyncio.Semaphore(min(max(len(running), 1), 16))

    async def _stat(c):
        async with sem:
            try:
                return stats_percent(await asyncio.wait_for(
                    _dget(session, f"/containers/{c['id']}/stats?stream=false", timeout=3.5),
                    timeout=4.0))
            except Exception:
                return None
    stats, _ = await asyncio.gather(
        asyncio.gather(*(_stat(c) for c in running)),
        _enrich_isolation(session, cards))
    live = dict(zip((c["id"] for c in running), stats))
    for c in cards:
        c["stats"] = live.get(c["id"])
    cards.sort(key=lambda c: (c["state"] != "running", c["name"]))
    return {"items": cards}


async def collect_container(session, cid: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{4,64}", cid):
        raise RuntimeError("bad id")
    j = await _dget(session, f"/containers/{cid}/json")
    insp = shape_inspect(j)
    out = dict(insp)
    # карточки всех — для соседей по сети (reachability)
    try:
        cards = [shape_container(c) for c in await _dget(session, "/containers/json?all=1")]
        me = next((c for c in cards if c["id"] == insp["id"]
                   or c["name"] == insp["name"]), None)
        insp_ports = me.get("ports") if me else []
        out["ports"] = insp_ports
        out["reach"] = reachability({**insp, "ports": insp_ports}, cards)
    except Exception:
        out["reach"] = None
    if insp["state"] == "running":
        try:
            out["stats"] = stats_percent(await _dget(
                session, f"/containers/{cid}/stats?stream=false"))
        except Exception:
            out["stats"] = None
        try:
            top = await _dget(session, f"/containers/{cid}/top")
            out["top"] = [dict(zip([t.lower() for t in top.get("Titles") or []], row))
                          for row in (top.get("Processes") or [])][:15]
        except Exception:
            out["top"] = []
    return out


async def collect_logs(session, cid: str, tail: int = 80) -> dict:
    if not re.fullmatch(r"[0-9a-f]{4,64}", cid):
        raise RuntimeError("bad id")
    tail = max(1, min(400, tail))
    raw = await _dget(session, f"/containers/{cid}/logs?stdout=1&stderr=1&tail={tail}",
                      raw=True)
    text = demux_logs(raw)
    if len(text) > 120_000:
        text = text[-120_000:]
    return {"text": text}


async def collect_netmap(session, focus: str = "") -> dict:
    cs, nets = await asyncio.gather(_dget(session, "/containers/json?all=1"),
                                    _dget(session, "/networks"))
    cards = [shape_container(c) for c in cs]
    await _enrich_isolation(session, cards)
    return build_netmap(cards, nets, focus=focus)


async def collect_stack(session) -> dict:
    """Слой виртуализации: железо → гипервизор → ядро → docker-движок → контейнеры."""
    virt = await _to_thread(detect_virt)
    kf = await _to_thread(kernel_facts)
    try:
        dinfo = await docker_info(session)
    except Exception as e:
        dinfo = {"error": str(e)}
    return {"virt": virt, "kernel": kf, "docker": dinfo,
            "uptime": uptime_seconds()}


async def collect_disks(session) -> dict:
    out = {"disks": await _to_thread(disks)}
    try:
        df = await _dget(session, "/system/df", timeout=40)
        imgs = df.get("Images") or []
        vols = df.get("Volumes") or []
        out["docker"] = {
            "images_count": len(imgs),
            "images_size": sum(i.get("Size", 0) for i in imgs),
            "containers_size": sum((c.get("SizeRw") or 0)
                                   for c in df.get("Containers") or []),
            "volumes_size": sum(((v.get("UsageData") or {}).get("Size") or 0)
                                for v in vols),
            "build_cache": sum((b.get("Size") or 0)
                               for b in df.get("BuildCache") or [])}
    except Exception as e:
        out["docker"] = {"error": str(e)}
    return out


def collect_access() -> dict:
    users = parse_passwd()
    humans = [u for u in users if u["human"]]
    for u in humans:
        u["keys"] = authorized_keys_count(HOSTFS, u["home"])
    logins = parse_utmp(f"{HOSTFS}/var/log/wtmp", limit=25)
    active = [r for r in parse_utmp(f"{HOSTFS}/var/run/utmp") if r["type"] == 7]
    return {"humans": humans, "system_count": len(users) - len(humans),
            "groups": parse_groups(), "sshd": parse_sshd(),
            "active": active, "logins": logins}


def collect_system() -> dict:
    return {**kernel_facts(), "uptime": uptime_seconds(), "load": loadavg(),
            "mem": meminfo()}


# --------------------------------------------------------------------------- #
#  PASS 17.A: «глаза» Праксис — read-only сводка о доме, в котором она живёт.
#
#  Обсерватория и так не умеет писать (GET-проксик докера, /rootfs:ro) — но «не умеет
#  писать» не значит «можно отдавать всё». Здесь второй забор: ЯВНЫЙ allowlist полей.
#  Наружу НЕ уходят: пользователи, sshd-конфиг, история логинов, env контейнеров,
#  командные строки процессов (в аргументах живут ключи). Тест держит этот список.
# --------------------------------------------------------------------------- #

# Что можно знать о контейнере: имя/состояние/образ/расход/уровень изоляции.
_AGENT_CONTAINER_KEYS = ("name", "state", "status", "image", "level")
# Что можно знать о порте: номер/протокол/кто слушает/куда ведёт. НЕ cmd.
_AGENT_PORT_KEYS = ("port", "proto", "comm", "target")


def shape_agent_status(overview: dict, containers: list[dict], ports: list[dict]) -> dict:
    """Чистая сборка сводки для неё (без сети — потому тестируется фикстурами).

    Порты делим на публичные (слушают не только loopback — это дверь наружу) и
    локальные (их считаем числом: ей важен факт, не перечисление)."""
    ov = overview or {}
    cs = []
    for c in containers or []:
        row = {k: c.get(k, "") for k in _AGENT_CONTAINER_KEYS}
        st = c.get("stats") or {}
        row["cpu_pct"] = st.get("cpu_pct", 0.0)
        row["mem_used"] = st.get("mem_used", 0)
        row["mem_pct"] = st.get("mem_pct", 0.0)
        cs.append(row)
    public, local = [], 0
    for p in ports or []:
        ips = p.get("ips") or []
        if all(ip.startswith("127.") or ip in ("::1",) for ip in ips) and ips:
            local += 1
            continue
        public.append({k: p.get(k, "") for k in _AGENT_PORT_KEYS})
    return {
        "now": ov.get("now", int(time.time())),
        "host": ov.get("host", {}),
        "virt": {"kindname": (ov.get("virt") or {}).get("kindname", "")},
        "cpu": ov.get("cpu", {}),
        "mem": ov.get("mem", {}),
        "disk_root": ov.get("disk_root") or {},
        "docker": ov.get("docker", {}),
        "praxis": ov.get("praxis", {}),      # её собственный HEAD — «на каком коде я живу»
        "containers": cs,
        "ports": {"public": public, "local_only": local},
    }


async def collect_agent_status(session, *, timeout: float = 6.0) -> dict:
    collectors = (
        collect_overview(session),
        collect_containers(session),
        _to_thread(listening_ports),
    )
    results = await asyncio.gather(
        *(asyncio.wait_for(collector, timeout=timeout) for collector in collectors),
        return_exceptions=True,
    )
    ov, cs, pts = results
    fallback = lambda value, default: default if isinstance(value, Exception) else value
    return shape_agent_status(
        fallback(ov, {}),
        (fallback(cs, {}) or {}).get("items", []),
        fallback(pts, []),
    )


# --------------------------------------------------------------------------- #
#  PASS 17.B: логи её сервисов. Логи — самое секретоносное, что есть на машине:
#  в них падают заголовки с ключами, строки .env, куски запросов. Поэтому здесь
#  ДВА забора: allowlist контейнеров (чужие проекты Егора не её дело) и маскировка
#  по контексту. Маскируем то, что названо ключом, а не всё длинное подряд:
#  git-sha и id контейнеров ей нужны целыми.
# --------------------------------------------------------------------------- #

_SECRET_PATTERNS = (
    # Authorization: Bearer <...> / Basic <...>
    re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-+/=]{8,})"),
    # key=value, где ключ пахнет секретом (в логах, .env-строках, query-параметрах)
    re.compile(r"(?i)\b([a-z_]*(?:api[_-]?key|secret|token|password|passwd|pwd|auth)"
               r"[a-z_]*)\s*[=:]\s*[\"']?([^\s\"'&,;]{6,})"),
    # известные префиксы ключей провайдеров
    re.compile(r"\b((?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})"),
)


def mask_secrets(text: str) -> str:
    """Заменить похожее на ключ на «***». Лучше лишний раз спрятать, чем один раз отдать."""
    if not text:
        return ""
    out = text
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)} ***", out)
    out = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}=***", out)
    out = _SECRET_PATTERNS[2].sub("***", out)
    return out


def agent_log_allowed(name: str) -> bool:
    """Только её сервисы и её запасной мозг. Список — из services.py (один источник)."""
    try:
        import services
        allowed = services.LOG_ALLOWED
    except Exception:      # обсерватория переживёт отсутствие модуля: fail-closed
        allowed = ()
    return (name or "") in allowed


async def _resolve_container(session, name: str) -> str:
    """Имя → id, и только для разрешённого имени (docker API принял бы что угодно)."""
    if not agent_log_allowed(name):
        raise RuntimeError(f"логи «{name}» не в моей зоне")
    for c in await _dget(session, "/containers/json?all=1"):
        if name in [n.lstrip("/") for n in (c.get("Names") or [])]:
            return (c.get("Id") or "")[:64]
    raise RuntimeError(f"контейнер «{name}» не найден")


async def collect_agent_logs(session, name: str, tail: int = 60) -> dict:
    cid = await _resolve_container(session, name)
    tail = max(1, min(200, int(tail or 60)))
    raw = await _dget(session, f"/containers/{cid}/logs?stdout=1&stderr=1&tail={tail}", raw=True)
    text = mask_secrets(demux_logs(raw))
    if len(text) > 20_000:
        text = text[-20_000:]
    return {"unit": name, "tail": tail, "text": text}


# --------------------------------------------------------------------------- #
#  Веб-слой
# --------------------------------------------------------------------------- #

_cache: dict[str, tuple[float, object]] = {}
_breaker: dict[str, float] = {}          # key -> monotonic, до которого фабрику НЕ зовём
_inflight: dict[str, asyncio.Lock] = {}  # key -> single-flight замок (один прогон фабрики)
_NEG_TTL = float(os.getenv("PRAXIS_SERVERAPP_NEG_TTL", "30") or 30)
_COLLECT_TIMEOUT = float(os.getenv("PRAXIS_SERVERAPP_TIMEOUT", "6") or 6)


def _stale(value):
    """Пометить деградированную (последнюю хорошую) выдачу флагом stale — для фронта."""
    return {**value, "stale": True} if isinstance(value, dict) else value


async def _cached(key: str, ttl: float, coro_factory, *,
                  timeout: float = _COLLECT_TIMEOUT, neg_ttl: float = _NEG_TTL):
    """Кэш + circuit-breaker + single-flight + жёсткий таймаут коллектора.

    F5: раньше кэш писался ТОЛЬКО на успехе и без single-flight — при зависшем procfs-чтении
    (host-задача в непрерываемом D-state) каждый опрос ре-спавнил новый тред в дефолтный пул
    до истощения, и вся обсерватория замерзала. Теперь: свежий хит отдаём сразу; при открытом
    breaker'е — последнее-хорошее (stale) без вызова фабрики; иначе один прогон под замком с
    таймаутом. На успехе кэшируем и гасим breaker; на ошибке/таймауте открываем breaker на
    neg_ttl и отдаём последнее-хорошее (stale) либо пробрасываем, если хорошего ещё нет. Так
    рождение новых тредов ограничено ~1 на ключ за окно neg_ttl, а зависшие треды бьются в
    выделенный _POOL, не трогая дефолтный пул (getaddrinfo/loop остаются живы).
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    if now < _breaker.get(key, 0.0):
        if hit is not None:
            return _stale(hit[1])
        raise RuntimeError(f"collector '{key}' degraded (breaker open)")
    lock = _inflight.setdefault(key, asyncio.Lock())
    async with lock:  # single-flight: параллельные опросы делят один прогон, не тред на каждого
        now = time.monotonic()
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        if now < _breaker.get(key, 0.0):
            if hit is not None:
                return _stale(hit[1])
            raise RuntimeError(f"collector '{key}' degraded (breaker open)")
        try:
            val = await asyncio.wait_for(coro_factory(), timeout=timeout)
        except Exception:  # CancelledError — BaseException, сюда НЕ попадёт (и правильно)
            _breaker[key] = time.monotonic() + neg_ttl
            if hit is not None:
                return _stale(hit[1])
            raise
        _cache[key] = (time.monotonic(), val)
        _breaker.pop(key, None)
        return val


def build_app():
    routes = web.RouteTableDef()

    def guard(handler):
        async def wrapped(request: web.Request):
            if not _request_is_owner(request):
                return web.json_response({"error": "forbidden"}, status=403)
            try:
                data = await handler(request)
            except Exception as e:
                log.warning("handler %s failed: %s", request.path, e)
                return web.json_response({"error": str(e)}, status=502)
            return web.json_response(data, dumps=lambda d: json.dumps(d, ensure_ascii=False))
        return wrapped

    @routes.get("/")
    async def index(request):
        p = BASE / "serverapp.html"
        if p.exists():
            return web.Response(text=p.read_text(encoding="utf-8"),
                                content_type="text/html",
                                headers={"Cache-Control": "no-cache",
                                         "Referrer-Policy": "no-referrer",
                                         "X-Content-Type-Options": "nosniff"})
        return web.Response(text="serverapp.html не найден", status=500)

    @routes.get("/api/session")
    async def api_session(request):
        # Владелец (initData webview ИЛИ уже-подписанная сессия) получает свежий токен.
        # Так standalone-PWA авторизуется вне Telegram и продлевает себя до протухания.
        if not _request_is_owner(request):
            return web.json_response({"error": "forbidden"}, status=403)
        return web.json_response(
            {"token": mint_session(OWNER_ID), "ttl": SESSION_TTL},
            headers={"Cache-Control": "no-store"})

    @routes.get("/favicon.ico")
    async def favicon(request):
        p = BASE / "serverapp_static" / "icon.svg"
        if p.is_file():
            return web.Response(body=p.read_bytes(), content_type="image/svg+xml",
                                headers={"Cache-Control": "public, max-age=86400"})
        return web.Response(status=204)

    @routes.get("/manifest.webmanifest")
    async def manifest(request):
        p = BASE / "serverapp_static" / "manifest.webmanifest"
        if p.is_file():
            return web.Response(body=p.read_bytes(),
                                content_type="application/manifest+json",
                                headers={"Cache-Control": "no-cache"})
        return web.Response(status=404)

    @routes.get("/sw.js")
    async def service_worker(request):
        p = BASE / "serverapp_static" / "sw.js"
        if p.is_file():
            return web.Response(body=p.read_bytes(), content_type="text/javascript",
                                headers={"Cache-Control": "no-cache",
                                         "Service-Worker-Allowed": "/"})
        return web.Response(status=404)

    _STATIC_TYPES = {".css": "text/css", ".js": "text/javascript",
                     ".svg": "image/svg+xml", ".png": "image/png",
                     ".webmanifest": "application/manifest+json",
                     ".woff2": "font/woff2"}

    @routes.get("/s/{name}")
    async def static_asset(request):
        name = re.sub(r"[^0-9a-zA-Z._-]", "", request.match_info["name"])
        p = BASE / "serverapp_static" / name
        ext = os.path.splitext(name)[1]
        if ext not in _STATIC_TYPES or not p.is_file():
            return web.Response(status=404)
        return web.Response(body=p.read_bytes(), content_type=_STATIC_TYPES[ext],
                            headers={"Cache-Control": "public, max-age=86400"})

    @routes.get("/vendor/{name}")
    async def vendor(request):
        # переиспользуем 3d-force-graph, уже вендоренный для её панели (panel_static/)
        name = re.sub(r"[^0-9a-zA-Z._-]", "", request.match_info["name"])
        p = BASE / "panel_static" / name
        if not name.endswith((".js", ".css")) or not p.is_file():
            return web.Response(status=404)
        ctype = "application/javascript" if name.endswith(".js") else "text/css"
        return web.Response(body=p.read_bytes(), content_type=ctype,
                            headers={"Cache-Control": "public, max-age=86400"})

    def agent_guard(request: web.Request):
        """-> None если пускаем, иначе готовый отказ. Fail-closed на пустом токене."""
        if not AGENT_TOKEN:
            return web.json_response({"error": "agent eyes disabled"}, status=503)
        if not _is_agent(request.headers.get("X-Praxis-Agent", "")):
            return web.json_response({"error": "forbidden"}, status=403)
        return None

    @routes.get("/api/agent/status")
    async def api_agent_status(request: web.Request):
        """Её глаза. Отдельная дверь: свой субъект (не Егор), свой гейт, свой allowlist."""
        deny = agent_guard(request)
        if deny is not None:
            return deny
        try:
            data = await _cached("ag", 4, lambda: collect_agent_status(request.app["session"]),
                                 timeout=8)  # > собственный внутренний 6s, чтоб не гасить его
        except Exception as e:
            log.warning("agent status failed: %s", e)
            return web.json_response({"error": str(e)}, status=502)
        return web.json_response(data, dumps=lambda d: json.dumps(d, ensure_ascii=False))

    @routes.get("/api/agent/logs")
    async def api_agent_logs(request: web.Request):
        """PASS 17.B: логи её сервисов — read-only, allowlist имён, секреты замаскированы."""
        deny = agent_guard(request)
        if deny is not None:
            return deny
        unit = request.query.get("unit", "")
        tail = int(request.query.get("tail", "60") or 60)
        try:
            data = await collect_agent_logs(request.app["session"], unit, tail)
        except Exception as e:
            log.warning("agent logs %s failed: %s", unit, e)
            return web.json_response({"error": str(e)}, status=502)
        return web.json_response(data, dumps=lambda d: json.dumps(d, ensure_ascii=False))

    def S(r):
        return r.app["session"]

    async def api_live(r):                       # быстрый live: сэмплер (кэш не нужен)
        s = r.app.get("sampler")
        return s.snapshot() if s else {"error": "sampler off"}

    async def api_overview(r):
        return await _cached("ov", 3, lambda: collect_overview(S(r)))

    async def api_stack(r):
        return await _cached("stk", 20, lambda: collect_stack(S(r)))

    async def api_containers(r):
        return await _cached("cs", 4, lambda: collect_containers(S(r)))

    async def api_container(r):
        return await collect_container(S(r), r.match_info["id"])

    async def api_logs(r):
        return await collect_logs(S(r), r.match_info["id"],
                                  int(r.query.get("tail", "80") or 80))

    async def api_netmap(r):
        focus = re.sub(r"[^0-9a-zA-Z_.-]", "", r.query.get("focus", ""))[:64]
        return await _cached("nm:" + focus, 5,
                             lambda: collect_netmap(S(r), focus=focus))

    async def api_ports(r):
        return {"items": await _cached(
            "pt", 5, lambda: _to_thread(listening_ports))}

    async def api_processes(r):
        return await _cached("pr", 4, lambda: _to_thread(processes_snapshot))

    async def api_system(r):
        return await _cached("sy", 10, lambda: _to_thread(collect_system))

    async def api_access(r):
        return await _cached("ac", 10, lambda: _to_thread(collect_access))

    async def api_disks(r):
        return await _cached("dk", 15, lambda: collect_disks(S(r)))

    for path, h in (("/api/live", api_live), ("/api/overview", api_overview),
                    ("/api/stack", api_stack), ("/api/containers", api_containers),
                    ("/api/container/{id}", api_container),
                    ("/api/container/{id}/logs", api_logs), ("/api/netmap", api_netmap),
                    ("/api/ports", api_ports), ("/api/processes", api_processes),
                    ("/api/system", api_system), ("/api/access", api_access),
                    ("/api/disks", api_disks)):
        routes.get(path)(guard(h))

    app = web.Application()
    app.add_routes(routes)

    async def _on_start(app):
        app["session"] = aiohttp.ClientSession()
        app["sampler"] = Sampler(session_getter=lambda: app["session"])
        app["sampler_task"] = asyncio.create_task(app["sampler"].run())
        app["restart_task"] = asyncio.create_task(_watch_restart_signal())

    async def _on_stop(app):
        for key in ("sampler_task", "restart_task"):
            t = app.get(key)
            if t:
                t.cancel()
        await app["session"].close()

    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_stop)
    return app


# --------------------------------------------------------------------------- #
#  PASS 17.B: она может попросить свои глаза перезапуститься (файл-сигнал).
#  Код здесь примонтирован ro — снести флаг нельзя, поэтому «свежесть» просьбы
#  определяется меткой времени: флаг новее старта процесса. После подъёма тот же
#  флаг старше нового старта, и цикл рестартов не заводится.
# --------------------------------------------------------------------------- #

STARTED_AT = time.time()


def _exit_process() -> None:      # вынесено для тестов
    os._exit(0)


async def _check_restart_signal(started_at: float = 0.0) -> bool:
    try:
        import services
    except Exception:
        return False
    reason = await asyncio.to_thread(services.restart_requested, "serverapp",
                                     started_at or STARTED_AT)
    if not reason:
        return False
    log.warning("RESTART signal from praxis: %s", reason[:160])
    _exit_process()
    return True


async def _watch_restart_signal() -> None:
    while True:
        try:
            await _check_restart_signal()
        except Exception:
            log.exception("watch-restart тик упал")
        await asyncio.sleep(5)


def main():
    if web is None:
        raise SystemExit("aiohttp не установлен")
    if not TOKEN:
        log.warning("PRAXIS_SERVERAPP_BOT_TOKEN пуст — все API-запросы будут 403")
    log.info("serverapp on :%s (docker=%s, proc=%s, hostfs=%s)",
             PORT, DOCKER, PROC, HOSTFS)
    web.run_app(build_app(), port=PORT)


if __name__ == "__main__":
    main()
