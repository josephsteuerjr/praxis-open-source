"""
Тесты серверной обсерватории (serverapp.py): HMAC-гейт initData, парсеры /proc
(cpu/mem/процессы/сокеты), /rootfs (passwd/groups/sshd/utmp/диски), докер-шейперы
(карточки/статы/логи/граф), веб-слой (403 без auth, 200 владельцу).

Коллекторы читают инжектированные пути — фикстуры строятся во временной папке,
ни докера, ни Linux не нужно. Запуск:  python praxis_test.py test_serverapp -v
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import struct
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import urlencode

_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import serverapp  # noqa: E402

TOKEN = "12345:TEST_TOKEN_abc"


def make_init_data(user_id: int = 777, token: str = TOKEN,
                   auth_date: int | None = None) -> str:
    pairs = {"auth_date": str(auth_date if auth_date is not None else int(time.time())),
             "query_id": "AAA", "user": json.dumps({"id": user_id, "first_name": "Т"})}
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class TestAuth(unittest.TestCase):
    def test_valid_signature(self):
        user = serverapp.check_init_data(make_init_data(), TOKEN)
        self.assertEqual(user["id"], 777)

    def test_tampered_rejected(self):
        bad = make_init_data().replace("777", "666")
        self.assertIsNone(serverapp.check_init_data(bad, TOKEN))

    def test_wrong_token_rejected(self):
        self.assertIsNone(serverapp.check_init_data(make_init_data(), "other:tok"))

    def test_expired_rejected(self):
        old = make_init_data(auth_date=int(time.time()) - 999)
        self.assertIsNone(serverapp.check_init_data(old, TOKEN, max_age=100))
        self.assertIsNotNone(serverapp.check_init_data(old, TOKEN, max_age=0))

    def test_empty(self):
        self.assertIsNone(serverapp.check_init_data("", TOKEN))
        self.assertIsNone(serverapp.check_init_data("hash=aa", ""))


class ProcFixture(unittest.TestCase):
    """Временное дерево, имитирующее /proc и /rootfs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = str(Path(self.tmp.name) / "proc")
        self.hostfs = str(Path(self.tmp.name) / "rootfs")
        for d in ("proc/sys/kernel/random", "proc/sys/vm", "proc/sys/fs",
                  "proc/1/net", "proc/42", "rootfs/etc/ssh/sshd_config.d",
                  "rootfs/var/log", "rootfs/var/run", "rootfs/root/.ssh"):
            (Path(self.tmp.name) / d).mkdir(parents=True, exist_ok=True)
        self._w("proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0\n"
                             "cpu0 50 0 50 400 0 0 0 0 0 0\n"
                             "cpu1 50 0 50 400 0 0 0 0 0 0\n")
        self._w("proc/meminfo", "MemTotal:       4000000 kB\n"
                                "MemFree:         500000 kB\n"
                                "MemAvailable:   2000000 kB\n"
                                "Buffers:         100000 kB\n"
                                "Cached:          900000 kB\n"
                                "SwapTotal:      1000000 kB\n"
                                "SwapFree:        750000 kB\n")
        self._w("proc/loadavg", "0.42 0.30 0.21 2/345 9999\n")
        self._w("proc/uptime", "360000.5 700000.0\n")
        self._w("proc/version", "Linux version 6.8.0-test (build) #1 SMP\n")
        self._w("proc/cpuinfo",
                "processor\t: 0\nmodel name\t: Test CPU @ 2.0GHz\ncpu MHz\t\t: 1996.25\n"
                "flags\t\t: fpu vme de pse tsc hypervisor lahf_lm\n\n"
                "processor\t: 1\nmodel name\t: Test CPU @ 2.0GHz\ncpu MHz\t\t: 1996.25\n"
                "flags\t\t: fpu vme de pse tsc hypervisor lahf_lm\n")
        self._w("proc/1/net/dev",
                "Inter-|   Receive                          |  Transmit\n"
                " face |bytes    packets errs drop fifo frame compressed multicast|"
                "bytes    packets errs drop fifo colls carrier compressed\n"
                "    lo:  1000 6 0 0 0 0 0 0 1000 6 0 0 0 0 0 0\n"
                "  eth0: 500000 100 0 0 0 0 0 0 200000 80 0 0 0 0 0 0\n"
                "docker0: 9999 9 0 0 0 0 0 0 9999 9 0 0 0 0 0 0\n")
        self._w("proc/diskstats",
                "   7       0 loop0 11 0 28 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
                "   8       0 sda 100 0 2000 50 200 10 8000 60 0 400 100 0 0 0 0\n"
                "   8       1 sda1 90 0 1800 45 190 9 7000 55 0 380 95 0 0 0 0\n")
        self._w("rootfs/sys/class/dmi/id/sys_vendor", "QEMU\n")
        self._w("rootfs/sys/class/dmi/id/product_name", "Standard PC (i440FX + PIIX, 1996)\n")
        self._w("rootfs/sys/class/dmi/id/bios_vendor", "SeaBIOS\n")
        self._w("rootfs/sys/class/dmi/id/chassis_vendor", "QEMU\n")
        self._w("rootfs/etc/hostname", "atlanta\n")
        self._w("proc/sys/kernel/osrelease", "6.8.0-test\n")
        self._w("proc/sys/kernel/ostype", "Linux\n")
        self._w("proc/sys/kernel/hostname", "atlanta\n")
        self._w("proc/sys/kernel/pid_max", "4194304\n")
        self._w("proc/sys/kernel/threads-max", "30000\n")
        self._w("proc/sys/kernel/random/entropy_avail", "256\n")
        self._w("proc/sys/vm/swappiness", "60\n")
        self._w("proc/sys/fs/file-nr", "4224\t0\t9223372036854775807\n")
        # процесс 42: comm со скобками и пробелом, 50+25 тиков, 250 страниц RSS
        self._w("proc/42/stat", "42 (web (srv)) S 1 42 42 0 -1 4194304 "
                                "100 0 0 0 50 25 0 0 20 0 1 0 12345 1000000 250 "
                                "18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 "
                                "0 0 0 0 0 0\n")
        self._w("proc/42/status", "Name:\tweb\nUid:\t1000\t1000\t1000\t1000\n")
        self._w("proc/42/statm", "1000 250 100 10 0 500 0\n")
        self._w("proc/42/comm", "web\n")
        self._w("rootfs/etc/passwd",
                "root:x:0:0:root:/root:/bin/bash\n"
                "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
                "yegor:x:1000:1000:Yegor:/home/yegor:/bin/bash\n"
                "svc:x:1001:1001::/home/svc:/usr/sbin/nologin\n")
        self._w("rootfs/etc/group",
                "root:x:0:\nsudo:x:27:yegor\ndocker:x:998:yegor,svc\nusers:x:100:\n")
        self._w("rootfs/etc/os-release",
                'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04 LTS"\nID=ubuntu\n')
        self._w("rootfs/etc/ssh/sshd_config",
                "# comment\nPort 22\nPermitRootLogin yes\n#PasswordAuthentication no\n")
        self._w("rootfs/root/.ssh/authorized_keys",
                "# ключи\nssh-ed25519 AAAA... a@b\nssh-rsa BBBB... c@d\n\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _w(self, rel: str, text: str):
        p = Path(self.tmp.name) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


class TestHostParsers(ProcFixture):
    def test_cpu_percent(self):
        t0 = serverapp.cpu_times(self.proc)
        t1 = {"cpu": [200, 0, 200, 1500, 0, 0, 0, 0, 0, 0],
              "cpu0": [100, 0, 100, 750, 0, 0, 0, 0, 0, 0],
              "cpu1": [100, 0, 100, 750, 0, 0, 0, 0, 0, 0]}
        out = serverapp.cpu_percent_from(t0, t1)
        self.assertAlmostEqual(out["total"], 22.2, places=1)
        self.assertEqual(len(out["cores"]), 2)

    def test_meminfo(self):
        m = serverapp.meminfo(self.proc)
        self.assertEqual(m["total"], 4000000 * 1024)
        self.assertEqual(m["used"], 2000000 * 1024)
        self.assertEqual(m["used_pct"], 50.0)
        self.assertEqual(m["swap_used"], 250000 * 1024)

    def test_loadavg_uptime(self):
        self.assertEqual(serverapp.loadavg(self.proc)["m1"], 0.42)
        self.assertEqual(serverapp.uptime_seconds(self.proc), 360000.5)

    def test_proc_stat_parens_in_comm(self):
        st = serverapp._proc_stat(self.proc, "42")
        self.assertEqual(st["comm"], "web (srv)")
        self.assertEqual(st["state"], "S")
        self.assertEqual(st["ppid"], 1)
        self.assertEqual(st["ticks"], 75)

    def test_processes_snapshot(self):
        with mock.patch.object(serverapp, "HOSTFS", self.hostfs):
            snap = serverapp.processes_snapshot(self.proc, interval=1.0,
                                                _sleep=lambda s: None)
        self.assertEqual(snap["total"], 1)
        row = snap["top_mem"][0]
        self.assertEqual(row["rss"], 250 * 4096)
        self.assertEqual(row["user"], "yegor")
        self.assertEqual(snap["states"]["S"], 1)

    def test_kernel_facts(self):
        with mock.patch.object(serverapp, "HOSTFS", self.hostfs):
            kf = serverapp.kernel_facts(self.proc, self.hostfs)
        self.assertEqual(kf["hostname"], "atlanta")
        self.assertEqual(kf["cpu_cores"], 2)
        self.assertEqual(kf["cpu_model"], "Test CPU @ 2.0GHz")
        self.assertEqual(kf["os_pretty"], "Ubuntu 24.04 LTS")
        self.assertEqual(kf["file_nr"]["open"], 4224)


class TestNetParsers(ProcFixture):
    def test_hex_ips(self):
        self.assertEqual(serverapp._hex_ip4("0100007F"), "127.0.0.1")
        self.assertEqual(serverapp._hex_ip4("00000000"), "0.0.0.0")
        self.assertEqual(serverapp._hex_ip6("0" * 24 + "01000000"), "::1")
        self.assertEqual(
            serverapp._hex_ip6("0000000000000000FFFF00000100007F"), "127.0.0.1")

    def test_listeners(self):
        self._w("proc/1/net/tcp",
                "  sl local rem st tx_rx tr tm retr uid timeout inode\n"
                "  0: 0100007F:1F90 00000000:0000 0A 0:0 00:0 00000000 0 0 111 1\n"
                "  1: 00000000:0016 00000000:0000 0A 0:0 00:0 00000000 0 0 222 1\n"
                "  2: 0100007F:AAAA 0200007F:BBBB 01 0:0 00:0 00000000 0 0 333 1\n")
        self._w("proc/1/net/udp",
                "  sl local rem st tx_rx tr tm retr uid timeout inode ref\n"
                " 10: 00000000:0035 00000000:0000 07 0:0 00:0 00000000 0 0 444 2\n")
        socks = serverapp.parse_net_listeners(f"{self.proc}/1/net")
        got = {(s["proto"], s["port"], s["ip"]) for s in socks}
        self.assertIn(("tcp", 8080, "127.0.0.1"), got)
        self.assertIn(("tcp", 22, "0.0.0.0"), got)
        self.assertIn(("udp", 53, "0.0.0.0"), got)
        self.assertEqual(len(socks), 3)  # ESTABLISHED (01) отсечён

    @unittest.skipIf(os.name == "nt", "нужны symlink'и procfs")
    def test_socket_owners_and_ports(self):
        self._w("proc/1/net/tcp",
                "  sl local rem st ...\n"
                "  0: 0100007F:1F90 00000000:0000 0A 0:0 00:0 00000000 0 0 111 1\n")
        fd = Path(self.proc) / "42" / "fd"
        fd.mkdir()
        os.symlink("socket:[111]", fd / "5")
        owners = serverapp.socket_owners(self.proc, {111})
        self.assertEqual(owners, {111: 42})
        self._w("proc/42/cmdline",
                "/usr/bin/docker-proxy\0-container-ip\x00172.18.0.2\x00"
                "-container-port\x008080\0")
        ports = serverapp.listening_ports(self.proc)
        self.assertEqual(ports[0]["port"], 8080)
        self.assertEqual(ports[0]["comm"], "web")


class TestThroughput(ProcFixture):
    def test_parse_net_dev_phys_only(self):
        net = serverapp.parse_net_dev(f"{self.proc}/1/net/dev")
        self.assertIn("eth0", net)
        self.assertNotIn("lo", net)         # виртуальные отброшены
        self.assertNotIn("docker0", net)
        self.assertEqual(net["eth0"], (500000, 200000))

    def test_parse_diskstats_whole_disk(self):
        d = serverapp.parse_diskstats(f"{self.proc}/diskstats")
        self.assertIn("sda", d)
        self.assertNotIn("sda1", d)         # раздел отброшен
        self.assertNotIn("loop0", d)
        self.assertEqual(d["sda"], (2000 * 512, 8000 * 512))

    def test_counter_rate(self):
        prev = {"eth0": (1000, 2000), "gone": (0, 0)}
        cur = {"eth0": (3000, 5000), "new": (10, 10)}   # new без prev — пропущен
        rx, tx = serverapp.counter_rate(prev, cur, 2.0)
        self.assertEqual(rx, 1000.0)        # (3000-1000)/2
        self.assertEqual(tx, 1500.0)        # (5000-2000)/2

    def test_counter_rate_handles_reset(self):
        # счётчик обнулился (reboot iface) → отрицательную дельту не считаем
        rx, tx = serverapp.counter_rate({"e": (9000, 9000)}, {"e": (100, 100)}, 1.0)
        self.assertEqual((rx, tx), (0, 0))

    def test_quick_proc_counts(self):
        c = serverapp.quick_proc_counts(self.proc)
        self.assertEqual(c["total"], 2)     # pid 1 (net-фикстура) + pid 42
        self.assertEqual(c["running"], 0)   # процесс 42 в состоянии S

    def test_sampler_two_ticks_make_rates(self):
        import asyncio
        s = serverapp.Sampler(proc=self.proc)

        async def go():
            await s.tick(2.0)               # первый тик: базы нет, rate=0
            # подкрутим счётчики, чтобы во втором тике появился прирост
            self._w("proc/1/net/dev",
                    "  eth0: 700000 100 0 0 0 0 0 0 300000 80 0 0 0 0 0 0\n")
            self._w("proc/diskstats",
                    "   8       0 sda 100 0 4000 50 200 10 12000 60 0 400 100 0 0 0 0\n")
            s._prev_mono -= 2.0             # эмулируем прошедшие 2с
            await s.tick(2.0)
        asyncio.run(go())
        snap = s.snapshot()
        self.assertTrue(len(snap["hist"]["net_rx"]) >= 2)
        self.assertGreater(snap["net"]["rx"], 0)      # 200000 байт за ~2с
        self.assertGreater(snap["disk"]["read"], 0)


class TestVirtAndStack(ProcFixture):
    def test_detect_kvm(self):
        v = serverapp.detect_virt(self.proc, self.hostfs)
        self.assertTrue(v["virtual"])
        self.assertEqual(v["kind"], "kvm")
        self.assertTrue(v["hypervisor_flag"])
        self.assertEqual(v["vendor"], "QEMU")

    def test_detect_bare_metal(self):
        for n in ("sys_vendor", "product_name", "bios_vendor", "chassis_vendor"):
            self._w(f"rootfs/sys/class/dmi/id/{n}", "Dell Inc.\n")
        self._w("proc/cpuinfo", "processor\t: 0\nflags\t\t: fpu vme de pse tsc\n")
        v = serverapp.detect_virt(self.proc, self.hostfs)
        self.assertFalse(v["virtual"])
        self.assertEqual(v["kind"], "bare")


class TestIsolationReach(unittest.TestCase):
    def _inspect(self, **hc):
        base = {"Id": "a" * 64, "Name": "/x", "State": {"Status": "running"},
                "Config": {"Image": "img"}, "Mounts": [], "HostConfig": hc,
                "NetworkSettings": {"Networks": {"praxis_default": {"IPAddress": "172.18.0.2"}}}}
        return base

    def test_isolation_tight_vs_host(self):
        tight = serverapp.isolation(self._inspect(ReadonlyRootfs=True))
        self.assertIn(tight["level"], ("tight", "normal"))
        priv = serverapp.isolation(self._inspect(Privileged=True))
        self.assertEqual(priv["level"], "host")
        self.assertTrue(any("privileged" in r for r in priv["reasons"]))

    def test_isolation_docker_sock(self):
        j = self._inspect()
        j["Mounts"] = [{"Source": "/var/run/docker.sock", "Destination": "/sock",
                        "RW": False, "Type": "bind"}]
        iso = serverapp.isolation(j)
        self.assertTrue(iso["mounts_docker_sock"])
        self.assertEqual(iso["level"], "host")

    def test_isolation_caps_and_apparmor(self):
        iso = serverapp.isolation(self._inspect(
            CapAdd=["SYS_PTRACE"], SecurityOpt=["apparmor=unconfined"],
            PidMode="host", ExtraHosts=["host.docker.internal:host-gateway"]))
        self.assertEqual(iso["level"], "loose")
        self.assertTrue(any("SYS_PTRACE" in r for r in iso["reasons"]))
        self.assertTrue(any("PID" in r for r in iso["reasons"]))

    def test_reachability_peers_and_host(self):
        target = {"name": "praxis", "networks": {"praxis_default": "172.18.0.2"},
                  "mounts": [{"src": "/opt/praxis", "dst": "/app", "rw": True, "type": "bind"}],
                  "ports": [], "isolation": {"level": "normal",
                  "extra_hosts": ["host.docker.internal:host-gateway"]}}
        others = [
            {"name": "praxis-mailbot", "state": "running",
             "networks": {"praxis_default": "172.18.0.3"}},
            {"name": "lonely", "state": "running", "networks": {"other": "172.20.0.2"}}]
        reach = serverapp.reachability(target, others)
        self.assertEqual([p["name"] for p in reach["peers"]], ["praxis-mailbot"])
        self.assertTrue(reach["host_gateway"])
        self.assertEqual(reach["host_paths"][0]["dst"], "/app")
        self.assertTrue(reach["host_paths"][0]["rw"])

    def test_build_netmap_focus_dims_unreachable(self):
        cards = [
            {"name": "praxis", "state": "running",
             "networks": {"praxis_default": "172.18.0.2"}, "ports": []},
            {"name": "mailbot", "state": "running",
             "networks": {"praxis_default": "172.18.0.3"}, "ports": []},
            {"name": "dasha", "state": "running",
             "networks": {"dasha_default": "172.20.0.2"}, "ports": []}]
        nets = [{"Name": "praxis_default", "Driver": "bridge"},
                {"Name": "dasha_default", "Driver": "bridge"}]
        g = serverapp.build_netmap(cards, nets, focus="praxis")
        by = {n["id"]: n for n in g["nodes"]}
        self.assertTrue(by["c:mailbot"]["reach"])       # сосед по сети — достижим
        self.assertFalse(by["c:dasha"]["reach"])        # чужая сеть — dim
        self.assertTrue(by["c:praxis"]["focus"])


class TestRootfsParsers(ProcFixture):
    def test_passwd(self):
        users = serverapp.parse_passwd(self.hostfs)
        humans = [u["name"] for u in users if u["human"]]
        self.assertEqual(humans, ["root", "yegor"])
        self.assertEqual(len(users), 4)

    def test_groups(self):
        g = serverapp.parse_groups(self.hostfs)
        self.assertEqual(g["sudo"], ["yegor"])
        self.assertEqual(g["docker"], ["yegor", "svc"])
        self.assertNotIn("users", g)

    def test_sshd_with_dropin_priority(self):
        base = serverapp.parse_sshd(self.hostfs)
        self.assertEqual(base["permit_root_login"], "yes")
        self.assertEqual(base["password_auth"], "yes*")  # дефолт, в конфиге закомментен
        self._w("rootfs/etc/ssh/sshd_config.d/50-cloud.conf",
                "PasswordAuthentication no\nPermitRootLogin prohibit-password\n")
        over = serverapp.parse_sshd(self.hostfs)
        self.assertEqual(over["password_auth"], "no")
        self.assertEqual(over["permit_root_login"], "prohibit-password")

    def test_authorized_keys(self):
        self.assertEqual(serverapp.authorized_keys_count(self.hostfs, "/root"), 2)
        self.assertEqual(serverapp.authorized_keys_count(self.hostfs, "/home/none"), 0)

    def test_utmp(self):
        def rec(t, user, line, host, sec):
            return struct.pack(serverapp._UTMP_FMT, t, 1234, line.encode(),
                               b"id", user.encode(), host.encode(),
                               0, 0, 0, sec, 0, 0, 0, 0, 0)
        raw = rec(2, "reboot", "~", "6.8.0", 1000) + \
            rec(7, "root", "pts/0", "203.0.113.7", 2000) + \
            rec(8, "", "pts/0", "", 2100)  # DEAD_PROCESS — отсекается
        p = Path(self.hostfs) / "var/log/wtmp"
        p.write_bytes(raw)
        out = serverapp.parse_utmp(str(p))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["user"], "root")       # свежие сверху
        self.assertEqual(out[0]["host"], "203.0.113.7")
        self.assertEqual(out[1]["user"], "(загрузка)")
        self.assertEqual(struct.calcsize(serverapp._UTMP_FMT), 384)

    def test_disks(self):
        self._w("proc/1/mounts",
                "/dev/sda1 / ext4 rw 0 0\n"
                "/dev/sda1 /snap ext4 rw 0 0\n"       # тот же dev — короткий mount
                "tmpfs /run tmpfs rw 0 0\n"
                "/dev/loop3 /snap/x squashfs ro 0 0\n")
        blocks = types.SimpleNamespace(f_blocks=1000, f_bavail=300, f_frsize=4096)
        with mock.patch.object(serverapp.os, "statvfs", create=True,
                               new=lambda p: blocks):
            out = serverapp.disks(self.proc, self.hostfs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["mount"], "/")
        self.assertEqual(out[0]["total"], 4096000)
        self.assertEqual(out[0]["used_pct"], 70.0)


class TestDockerShapers(unittest.TestCase):
    def test_shape_container(self):
        c = serverapp.shape_container({
            "Id": "a" * 64, "Names": ["/praxis"], "Image": "praxis-praxis",
            "State": "running", "Status": "Up 2 hours", "Created": 1,
            "Ports": [{"PrivatePort": 8092, "PublicPort": 8092, "Type": "tcp",
                       "IP": "127.0.0.1"}],
            "NetworkSettings": {"Networks": {"praxis_default": {"IPAddress": "172.18.0.2"}}},
            "Labels": {"com.docker.compose.project": "praxis"}})
        self.assertEqual(c["id"], "a" * 12)
        self.assertEqual(c["name"], "praxis")
        self.assertEqual(c["networks"], {"praxis_default": "172.18.0.2"})
        self.assertEqual(c["ports"][0]["public"], 8092)

    def test_stats_percent(self):
        s = {"cpu_stats": {"cpu_usage": {"total_usage": 400}, "system_cpu_usage": 2000,
                           "online_cpus": 2},
             "precpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 1000},
             "memory_stats": {"usage": 1100, "limit": 4000,
                              "stats": {"inactive_file": 100}}}
        out = serverapp.stats_percent(s)
        self.assertEqual(out["cpu_pct"], 40.0)
        self.assertEqual(out["mem_used"], 1000)
        self.assertEqual(out["mem_pct"], 25.0)

    def test_demux_logs(self):
        framed = (b"\x01\x00\x00\x00\x00\x00\x00\x05hello" +
                  b"\x02\x00\x00\x00\x00\x00\x00\x03 er")
        self.assertEqual(serverapp.demux_logs(framed), "hello er")
        self.assertEqual(serverapp.demux_logs(b"plain tty log\n"), "plain tty log\n")
        self.assertEqual(serverapp.demux_logs(b""), "")

    def test_shape_inspect_hides_env_values(self):
        j = {"Id": "b" * 64, "Name": "/relay",
             "State": {"Status": "running", "StartedAt": "2026-07-01", "Pid": 5,
                       "Health": {"Status": "healthy"}},
             "Config": {"Image": "relay:local", "Cmd": ["./relay"],
                        "Env": ["SECRET=xxx", "PORT=5011"]},
             "Mounts": [{"Source": "/opt/praxis", "Destination": "/app", "RW": False,
                         "Type": "bind"}],
             "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
             "RestartCount": 3}
        out = serverapp.shape_inspect(j)
        self.assertEqual(out["env_count"], 2)
        self.assertNotIn("SECRET", json.dumps(out))
        self.assertEqual(out["health"], "healthy")
        self.assertFalse(out["mounts"][0]["rw"])

    def test_build_netmap(self):
        cs = [{"name": "praxis", "state": "running",
               "networks": {"net_a": "172.18.0.2"}, "ports": []},
              {"name": "mailbot", "state": "running",
               "networks": {"net_a": "172.18.0.3"},
               "ports": [{"private": 8092, "public": 8092, "proto": "tcp"}]}]
        nets = [{"Name": "net_a", "Driver": "bridge"},
                {"Name": "unused", "Driver": "bridge"}]
        g = serverapp.build_netmap(cs, nets)
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("host", ids)
        self.assertIn("c:praxis", ids)
        self.assertIn("n:net_a", ids)
        self.assertIn("p:8092/tcp", ids)
        self.assertNotIn("n:unused", ids)
        for l in g["links"]:
            self.assertIn(l["a"], ids)
            self.assertIn(l["b"], ids)

    def test_praxis_heartbeat_shape(self):
        hb = serverapp.praxis_heartbeat()
        self.assertEqual(set(hb), {"head", "ts", "subject"})


@unittest.skipIf(serverapp.web is None, "aiohttp недоступен")
class TestWebLayer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        self._patches = [mock.patch.object(serverapp, "TOKEN", TOKEN),
                         mock.patch.object(serverapp, "OWNER_ID", 777)]
        for p in self._patches:
            p.start()
        serverapp._cache.clear()
        serverapp._breaker.clear()   # F5: открытый breaker не должен течь между тестами
        serverapp._inflight.clear()
        self.client = TestClient(TestServer(serverapp.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for p in self._patches:
            p.stop()

    async def test_api_forbidden_without_auth(self):
        r = await self.client.get("/api/overview")
        self.assertEqual(r.status, 403)

    async def test_api_forbidden_for_stranger(self):
        r = await self.client.get("/api/system",
                                  params={"_auth": make_init_data(user_id=666)})
        self.assertEqual(r.status, 403)

    async def test_api_ok_for_owner(self):
        with mock.patch.object(serverapp, "collect_system",
                               new=lambda: {"ok": True, "кто": "я"}):
            r = await self.client.get("/api/system",
                                      params={"_auth": make_init_data(user_id=777)})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["ok"], True)

    async def test_handler_error_becomes_502(self):
        def boom():
            raise RuntimeError("нет докера")
        with mock.patch.object(serverapp, "collect_system", new=boom):
            r = await self.client.get("/api/system",
                                      params={"_auth": make_init_data(user_id=777)})
        self.assertEqual(r.status, 502)

    async def test_index_served_without_auth(self):
        r = await self.client.get("/")
        self.assertIn(r.status, (200, 500))  # 500 если html ещё не рядом

    async def test_favicon_no_auth(self):
        r = await self.client.get("/favicon.ico")
        self.assertEqual(r.status, 204)

    async def test_vendor_serves_lib_no_auth(self):
        # 3d-force-graph переиспользуется из panel_static/ (публично, как favicon)
        r = await self.client.get("/vendor/3d-force-graph.min.js")
        if (serverapp.BASE / "panel_static" / "3d-force-graph.min.js").is_file():
            self.assertEqual(r.status, 200)
            self.assertIn("javascript", r.headers.get("Content-Type", ""))
        else:
            self.assertEqual(r.status, 404)

    async def test_vendor_rejects_nonstatic(self):
        for bad in ("/vendor/serverapp.py", "/vendor/.env", "/vendor/secret.txt"):
            r = await self.client.get(bad)
            self.assertEqual(r.status, 404, bad)

    async def test_live_endpoint_owner(self):
        # сэмплер стартует в on_startup; хотя бы снимок-заглушка должен вернуться
        r = await self.client.get("/api/live",
                                  params={"_auth": make_init_data(user_id=777)})
        self.assertEqual(r.status, 200)
        body = await r.json()
        self.assertIn("hist", body)
        self.assertIn("cpu", body)


class TestCachedBreaker(unittest.IsolatedAsyncioTestCase):
    """F5: _cached = circuit-breaker + single-flight + stale-serve + жёсткий таймаут;
    коллекторы на выделенном пуле _POOL, а не на дефолтном пуле loop'а."""

    async def asyncSetUp(self):
        serverapp._cache.clear()
        serverapp._breaker.clear()
        serverapp._inflight.clear()

    async def test_cached_serves_stale_on_timeout_and_opens_breaker(self):
        calls = {"n": 0}

        async def good():
            return {"v": 1}

        self.assertEqual(await serverapp._cached("k", 0, good), {"v": 1})

        async def hang():
            calls["n"] += 1
            await asyncio.Event().wait()  # виснет навсегда (как D-state procfs-чтение)

        val = await serverapp._cached("k", 0, hang, timeout=0.05)
        self.assertEqual(val, {"v": 1, "stale": True})              # последнее-хорошее, помечено
        self.assertGreater(serverapp._breaker.get("k", 0), time.monotonic())  # breaker открыт
        before = calls["n"]
        val = await serverapp._cached("k", 0, hang, timeout=0.05)
        self.assertEqual(val, {"v": 1, "stale": True})
        self.assertEqual(calls["n"], before, "breaker должен подавить повторный вызов фабрики")

    async def test_cached_breaker_bounds_new_factory_calls(self):
        async def seed():
            return {"ok": 1}
        await serverapp._cached("k", 0, seed)
        calls = {"n": 0}

        async def hang():
            calls["n"] += 1
            await asyncio.Event().wait()

        for _ in range(10):
            await serverapp._cached("k", 0, hang, timeout=0.02, neg_ttl=30)
        self.assertLessEqual(calls["n"], 1, "breaker ограничивает рождение тредов ~1 за окно")

    async def test_cached_single_flight_one_factory_call(self):
        calls = {"n": 0}

        async def slow():
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return {"v": calls["n"]}

        results = await asyncio.gather(*[serverapp._cached("k", 10, slow) for _ in range(8)])
        self.assertEqual(calls["n"], 1, "single-flight: одна фабрика на всех параллельных")
        self.assertTrue(all(r == results[0] for r in results))

    async def test_to_thread_runs_on_dedicated_collector_pool(self):
        import concurrent.futures
        import threading
        self.assertIsInstance(serverapp._POOL, concurrent.futures.ThreadPoolExecutor)
        name = await serverapp._to_thread(lambda: threading.current_thread().name)
        self.assertTrue(name.startswith("collector"), name)

    async def test_cached_reraises_when_no_lastgood(self):
        async def boom():
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            await serverapp._cached("k", 0, boom, timeout=1)   # нет хорошего -> проброс
        with self.assertRaises(RuntimeError):
            await serverapp._cached("k", 0, boom, timeout=1)   # breaker открыт, всё равно ошибка


class TestAtlasSession(unittest.TestCase):
    """ATLAS v3: stateless owner session token (подпись бот-токеном, без стора) —
    делает Обсерваторию устанавливаемым PWA. Verify константен, TTL и owner проверяются."""

    def setUp(self):
        self._tok, self._own = serverapp.TOKEN, serverapp.OWNER_ID
        serverapp.TOKEN, serverapp.OWNER_ID = "12345:TEST", 777

    def tearDown(self):
        serverapp.TOKEN, serverapp.OWNER_ID = self._tok, self._own

    def test_mint_and_verify_roundtrip(self):
        tok = serverapp.mint_session(777)
        self.assertEqual(serverapp.verify_session(tok), 777)

    def test_tamper_rejected(self):
        tok = serverapp.mint_session(777)
        body, mac = tok.rsplit(".", 1)
        self.assertIsNone(serverapp.verify_session(body + "." + ("0" * len(mac))))
        self.assertIsNone(serverapp.verify_session(tok.replace("777", "666", 1)))

    def test_wrong_owner_and_expiry(self):
        self.assertIsNone(serverapp.verify_session(serverapp.mint_session(999)))
        old = serverapp.mint_session(777, now=time.time() - serverapp.SESSION_TTL - 10)
        self.assertIsNone(serverapp.verify_session(old))

    def test_secret_bound_to_bot_token_and_salt(self):
        tok = serverapp.mint_session(777)
        serverapp.TOKEN = "99999:OTHER"
        self.assertIsNone(serverapp.verify_session(tok), "смена бот-токена рвёт все сессии")
        serverapp.TOKEN = "12345:TEST"
        self.assertEqual(serverapp.verify_session(tok), 777)

    def test_malformed_tokens(self):
        for bad in ("", "a.b.c", "x.y.z.w.v", "not-a-token"):
            self.assertIsNone(serverapp.verify_session(bad))


class TestCollectContainers(unittest.IsolatedAsyncioTestCase):
    """«Докер молчит»-регресс: per-container /stats?stream=false блокирует ~1с; при низком
    семафоре веер по многим контейнерам перелетал _COLLECT_TIMEOUT и залипал breaker.
    Теперь статы идут параллельно + best-effort, а список отдаётся всегда."""

    def _fake_dget(self, *, running: int, stall_ids=frozenset(), stat_delay=1.0):
        cards = []
        for i in range(running):
            cards.append({"Id": f"{i:012d}" + "0" * 52, "Names": [f"/c{i}"],
                          "Image": "img", "State": "running", "Status": "Up",
                          "Created": 1, "Ports": [], "NetworkSettings": {}, "Labels": {}})
        cards.append({"Id": "dead" + "0" * 60, "Names": ["/stopped"], "Image": "img",
                      "State": "exited", "Status": "Exited", "Created": 1,
                      "Ports": [], "NetworkSettings": {}, "Labels": {}})

        async def fake(session, path, *, raw=False, timeout=20.0):
            if path == "/containers/json?all=1":
                return cards
            if path.endswith("/json"):
                return {"Id": "x", "State": {}, "Config": {}, "HostConfig": {}, "Mounts": []}
            if "/stats" in path:
                cid = path.split("/")[2][:12]
                if cid in stall_ids:
                    await asyncio.Event().wait()          # виснет как D-state
                await asyncio.sleep(stat_delay)
                return {"cpu_stats": {"cpu_usage": {"total_usage": 2}, "system_cpu_usage": 10,
                                      "online_cpus": 1},
                        "precpu_stats": {"cpu_usage": {"total_usage": 1}, "system_cpu_usage": 5},
                        "memory_stats": {"usage": 100, "limit": 1000, "stats": {}}}
            raise RuntimeError("unexpected " + path)
        return fake

    async def test_stats_fan_out_is_parallel_within_budget(self):
        with mock.patch.object(serverapp, "_dget", self._fake_dget(running=15)):
            t0 = time.monotonic()
            out = await asyncio.wait_for(serverapp.collect_containers(None), timeout=5.0)
            elapsed = time.monotonic() - t0
        self.assertEqual(len(out["items"]), 16)                 # 15 running + 1 exited
        self.assertLess(elapsed, 3.0, f"parallel stats должны укладываться (<3с), было {elapsed:.1f}")
        running = [c for c in out["items"] if c["state"] == "running"]
        self.assertTrue(all(c["stats"] is not None for c in running))

    async def test_hung_stat_does_not_blank_the_list(self):
        stall = {"000000000003"}
        with mock.patch.object(serverapp, "_dget",
                               self._fake_dget(running=6, stall_ids=stall)):
            out = await asyncio.wait_for(serverapp.collect_containers(None), timeout=6.0)
        self.assertEqual(len(out["items"]), 7)                  # список полон несмотря на висяк
        by_id = {c["id"]: c for c in out["items"]}
        self.assertIsNone(by_id["000000000003"]["stats"])       # висяк -> None, не срыв
        self.assertIsNone(by_id["dead00000000"]["stats"])       # не-running -> None
        others = [c for c in out["items"]
                  if c["state"] == "running" and c["id"] != "000000000003"]
        self.assertTrue(all(c["stats"] is not None for c in others))


if __name__ == "__main__":
    unittest.main()
