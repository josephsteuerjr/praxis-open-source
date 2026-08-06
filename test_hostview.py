"""
Тесты PASS 17.A — «глаза» Праксис на сервер (первая ступень «хозяйки сервера»).

Ступень называется «только смотреть», поэтому проверяем ровно три вещи:
  1. дверь закрыта по умолчанию (нет токена → 503, чужой токен → 403) — fail-closed;
  2. сквозь дверь не пролезает лишнее: пользователи, sshd, логины, env, командные
     строки процессов. Это не «мы посмотрели глазами», а тест против фикстуры;
  3. её ход не роняется никогда: нет токена / нет связи / мусор в ответе → честная
     строка, а не исключение.

serverapp тестируется без aiohttp и без докера: shape_agent_status — чистая функция.
"""

from __future__ import annotations

import asyncio
import json
import unittest
import urllib.error

import hostview
import serverapp


# --------------------------------------------------------------------------- #
#  Фикстуры: то, что обсерватория знает о хосте (включая чувствительное)
# --------------------------------------------------------------------------- #

OVERVIEW = {
    "now": 1783600000,
    "host": {"hostname": "atlanta", "os": "Ubuntu 24.04", "kernel": "6.8.0", "uptime": 123456.0},
    "virt": {"virtual": True, "kind": "kvm", "kindname": "KVM / QEMU", "vendor": "QEMU"},
    "cpu": {"total": 12.5, "cores": [10.0, 15.0], "load": {"m1": 0.4, "m5": 0.5, "m15": 0.6}},
    "mem": {"total": 8 * 1024 ** 3, "used": 5 * 1024 ** 3, "used_pct": 62.5},
    "disk_root": {"mount": "/", "total": 200 * 1024 ** 3, "free": 100 * 1024 ** 3, "used_pct": 50.0},
    "docker": {"total": 11, "running": 10},
    "praxis": {"head": "b8e2ce6", "ts": 1783600000, "subject": "pass 16.4"},
    # чувствительное, что обсерватория умеет отдавать Егору и НЕ должна отдавать в эту дверь:
    "users": 2, "sessions": 1,
}

CONTAINERS = [
    {"id": "abc123", "name": "praxis", "image": "praxis-praxis", "state": "running",
     "status": "Up 3 hours", "level": "normal", "compose": "praxis",
     "networks": {"praxis_default": "172.19.0.3"}, "ports": [],
     "stats": {"cpu_pct": 2.1, "mem_used": 180 * 1024 ** 2, "mem_limit": 0, "mem_pct": 4.5}},
    {"id": "def456", "name": "praxis-dockerproxy", "image": "tecnativa/docker-socket-proxy",
     "state": "running", "status": "Up 2 days", "level": "host",
     "iso_reasons": ["монтирует docker.sock — управляет всем докером"],
     "networks": {}, "ports": [], "stats": None},
    {"id": "0ff0ff", "name": "relay", "image": "relay", "state": "exited",
     "status": "Exited (1) 5 minutes ago", "level": "normal", "networks": {}, "ports": [],
     "stats": None},
]

PORTS = [
    {"proto": "tcp", "port": 22, "ips": ["0.0.0.0"], "pid": 700, "comm": "sshd",
     "cmd": "/usr/sbin/sshd -D -o SuperSecret=hunter2", "target": ""},
    {"proto": "tcp", "port": 8093, "ips": ["127.0.0.1"], "pid": 800, "comm": "docker-proxy",
     "cmd": "docker-proxy -container-ip 172.19.0.5 -container-port 8093",
     "target": "172.19.0.5:8093"},
    {"proto": "tcp", "port": 443, "ips": ["0.0.0.0", "::"], "pid": 900, "comm": "caddy",
     "cmd": "caddy run --config /etc/caddy/Caddyfile --envfile /etc/caddy/.env", "target": ""},
]


def _shape():
    return serverapp.shape_agent_status(OVERVIEW, CONTAINERS, PORTS)


def _all_keys(obj) -> set[str]:
    """Все ключи структуры рекурсивно. Сверять надо поля, а не подстроки: имя процесса
    «sshd» на 22-м порту — полезный факт, а вот поле `sshd` (конфиг ssh) — чужой секрет."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            keys |= _all_keys(v)
    return keys


# --------------------------------------------------------------------------- #
#  1. Дверь: fail-closed
# --------------------------------------------------------------------------- #

class TestAgentDoorIsClosedByDefault(unittest.TestCase):
    def setUp(self):
        self._orig = serverapp.AGENT_TOKEN

    def tearDown(self):
        serverapp.AGENT_TOKEN = self._orig

    def test_no_token_means_nobody_gets_in(self):
        serverapp.AGENT_TOKEN = ""
        self.assertFalse(serverapp._is_agent(""), "пустой токен не должен открывать дверь")
        self.assertFalse(serverapp._is_agent("что-угодно"),
                         "без настроенного токена дверь закрыта для всех — это fail-closed")

    def test_wrong_token_refused(self):
        serverapp.AGENT_TOKEN = "правильный-секрет"
        self.assertFalse(serverapp._is_agent("почти-правильный"))
        self.assertFalse(serverapp._is_agent(""))
        self.assertTrue(serverapp._is_agent("правильный-секрет"))

    def test_owner_gate_is_a_different_door(self):
        """Гейт Егора (HMAC initData) и гейт агента — разные субъекты, не путаются."""
        serverapp.AGENT_TOKEN = "секрет"
        self.assertFalse(serverapp._is_owner("секрет"), "токен агента не делает тебя Егором")

    def test_non_ascii_never_crashes_the_gate(self):
        """Регресс: compare_digest на не-ASCII строках бросает TypeError. Гейт, падающий
        от произвольного заголовка снаружи, — это не гейт, а способ уронить обсерваторию."""
        serverapp.AGENT_TOKEN = "секрет-с-кириллицей"
        self.assertTrue(serverapp._is_agent("секрет-с-кириллицей"))
        self.assertFalse(serverapp._is_agent("другой-секрет"))
        serverapp.AGENT_TOKEN = "ascii-token"
        self.assertFalse(serverapp._is_agent("кириллица-снаружи"))
        self.assertFalse(serverapp._is_agent("💥"))


# --------------------------------------------------------------------------- #
#  2. Allowlist: чувствительное не проходит
# --------------------------------------------------------------------------- #

class TestNothingSensitiveLeaks(unittest.TestCase):
    def test_no_users_sshd_logins(self):
        keys = _all_keys(_shape())
        for forbidden in ("users", "sessions", "sshd", "logins", "authorized_keys",
                          "humans", "groups", "active", "env", "cmd", "pid", "networks"):
            self.assertNotIn(forbidden, keys, f"поле «{forbidden}» не должно уходить в эту дверь")

    def test_no_command_lines(self):
        """В аргументах процессов живут ключи и пути к секретам — cmd не отдаём никогда."""
        blob = json.dumps(_shape(), ensure_ascii=False)
        self.assertNotIn("hunter2", blob, "секрет из командной строки sshd не должен утечь")
        self.assertNotIn("envfile", blob)
        self.assertNotIn("Caddyfile", blob)

    def test_loopback_ports_are_counted_not_listed(self):
        p = _shape()["ports"]
        self.assertEqual([r["port"] for r in p["public"]], [22, 443])
        self.assertEqual(p["local_only"], 1, "127.0.0.1-сокет — число, не строка")

    def test_container_fields_are_the_allowlist(self):
        c = _shape()["containers"][0]
        self.assertEqual(set(c), set(serverapp._AGENT_CONTAINER_KEYS)
                         | {"cpu_pct", "mem_used", "mem_pct"})
        self.assertNotIn("networks", c, "внутренние IP соседей ей в этой сводке не нужны")

    def test_useful_facts_survive(self):
        """Забор не должен вырезать то, ради чего всё затевалось."""
        d = _shape()
        self.assertEqual(d["host"]["hostname"], "atlanta")
        self.assertEqual(d["docker"], {"total": 11, "running": 10})
        self.assertEqual(d["praxis"]["head"], "b8e2ce6", "«на каком коде я живу» — важный факт")
        names = [c["name"] for c in d["containers"]]
        self.assertEqual(names, ["praxis", "praxis-dockerproxy", "relay"])
        self.assertEqual(d["virt"]["kindname"], "KVM / QEMU")


class TestAgentStatusCollection(unittest.IsolatedAsyncioTestCase):
    async def test_hung_collector_degrades_to_partial_status(self):
        original_overview = serverapp.collect_overview
        original_containers = serverapp.collect_containers
        original_ports = serverapp.listening_ports

        async def hung_overview(session):
            await asyncio.sleep(1)

        async def healthy_containers(session):
            return {"items": CONTAINERS}

        try:
            serverapp.collect_overview = hung_overview
            serverapp.collect_containers = healthy_containers
            serverapp.listening_ports = lambda: PORTS
            status = await serverapp.collect_agent_status(None, timeout=0.01)
        finally:
            serverapp.collect_overview = original_overview
            serverapp.collect_containers = original_containers
            serverapp.listening_ports = original_ports

        self.assertEqual(status["host"], {})
        self.assertEqual(
            [row["name"] for row in status["containers"]],
            ["praxis", "praxis-dockerproxy", "relay"],
        )
        self.assertEqual(status["ports"]["local_only"], 1)


# --------------------------------------------------------------------------- #
#  3. Её ход не роняется, и текст читаем человеком
# --------------------------------------------------------------------------- #

class TestHerEyesNeverThrow(unittest.TestCase):
    def setUp(self):
        self._token = hostview.os.environ.get("PRAXIS_AGENT_TOKEN")
        self._fetch = hostview.fetch

    def tearDown(self):
        hostview.fetch = self._fetch
        if self._token is None:
            hostview.os.environ.pop("PRAXIS_AGENT_TOKEN", None)
        else:
            hostview.os.environ["PRAXIS_AGENT_TOKEN"] = self._token

    def test_no_token_is_an_honest_sentence(self):
        hostview.os.environ.pop("PRAXIS_AGENT_TOKEN", None)
        self.assertFalse(hostview.available())
        out = hostview.describe("overview")
        self.assertIn("Глаза закрыты", out)
        self.assertIn("PRAXIS_AGENT_TOKEN", out)
        self.assertIn("закрыты", hostview.state_line())

    def test_state_line_does_not_lie_about_her_hands(self):
        """17.B дала ей перезапуск своих сервисов — строка о себе обязана это учитывать."""
        hostview.os.environ["PRAXIS_AGENT_TOKEN"] = "секрет"
        line = hostview.state_line()
        self.assertNotIn("перезапускать", line, "она умеет перезапускать свои сервисы")
        self.assertIn("хост править не умею", line)

    def test_dead_observatory_is_an_honest_sentence(self):
        hostview.os.environ["PRAXIS_AGENT_TOKEN"] = "секрет"
        hostview.fetch = lambda: None
        # ⚠ Слово «обсерватория» истинно и в ветке «молчит», и в ветке «глаза нет» —
        # ассерт не различал их. Спрашиваем различающий текст и ставим анти-страж:
        # упоминание переменной токена значит, что мы провалились не в ту ветку.
        self.assertTrue(hostview.available(), "токен поставлен — глаза есть")
        out = hostview.describe("overview")
        self.assertIn("обсерватория", out.lower())
        self.assertNotIn("PRAXIS_AGENT_TOKEN", out, "это ветка «глаз нет», а не «молчит»")
        self.assertNotIn("Traceback", out)

    def test_url_error_becomes_none_not_exception(self):
        hostview.os.environ["PRAXIS_AGENT_TOKEN"] = "секрет"
        orig = hostview.urllib.request.urlopen

        def boom(*a, **kw):
            raise urllib.error.URLError("нет связи")

        hostview.urllib.request.urlopen = boom
        try:
            self.assertIsNone(hostview.fetch(), "сетевая ошибка → None → честная строка")
        finally:
            hostview.urllib.request.urlopen = orig

    def test_overview_text_is_readable(self):
        hostview.os.environ["PRAXIS_AGENT_TOKEN"] = "секрет"
        hostview.fetch = _shape
        out = hostview.describe("overview")
        self.assertIn("atlanta", out)
        self.assertIn("KVM / QEMU", out)
        self.assertIn("1д 10ч", out, "аптайм — по-человечески, не 123456.0")
        self.assertIn("10 живых из 11", out)
        self.assertIn("b8e2ce6", out)

    def test_containers_show_who_died_and_who_is_loose(self):
        hostview.os.environ["PRAXIS_AGENT_TOKEN"] = "секрет"
        hostview.fetch = _shape
        out = hostview.describe("containers")
        self.assertIn("relay", out)
        self.assertIn("Exited", out, "упавший сервис обязан быть видим")
        self.assertIn("○", out)
        self.assertIn("доступ к докеру/хосту", out, "level=host — честно назван риском")

    def test_ports_separate_world_from_loopback(self):
        hostview.os.environ["PRAXIS_AGENT_TOKEN"] = "секрет"
        hostview.fetch = _shape
        out = hostview.describe("ports")
        self.assertIn("22/tcp", out)
        self.assertIn("sshd", out)
        self.assertIn("loopback", out)
        self.assertNotIn("hunter2", out)

    def test_unknown_section_is_explained(self):
        hostview.os.environ["PRAXIS_AGENT_TOKEN"] = "секрет"
        self.assertIn("Не знаю раздел", hostview.describe("всё-подряд"))

    def test_human_units(self):
        self.assertEqual(hostview.human_bytes(1536), "1.5 КБ")
        self.assertEqual(hostview.human_bytes(0), "0 Б")
        self.assertEqual(hostview.human_uptime(90), "1м")
        self.assertEqual(hostview.human_uptime(3700), "1ч 1м")
        self.assertEqual(hostview.human_uptime(None), "?")


class TestLogsAreMaskedAndFenced(unittest.TestCase):
    """PASS 17.B: логи — самое секретоносное на машине. Два забора: allowlist и маска."""

    def test_bearer_and_api_keys_are_masked(self):
        raw = (
            "INFO calling z.ai\n"
            "DEBUG headers: Authorization: Bearer 8f3c1e77a9b24d5e0c11aa77bb33cc99.AbCdEf\n"
            "DEBUG GLM_API_KEY=8f3c1e77a9b24d5e0c11aa77bb33cc99\n"
            "DEBUG openai key sk-proj-Ab12Cd34Ef56Gh78Ij90KlMn\n"
            "WARN password: hunter2secret\n"
        )
        out = serverapp.mask_secrets(raw)
        for secret in ("8f3c1e77a9b24d5e0c11aa77bb33cc99", "sk-proj-Ab12Cd34Ef56Gh78Ij90KlMn",
                       "hunter2secret", "AbCdEf"):
            self.assertNotIn(secret, out, f"«{secret}» не должен доехать до неё")
        self.assertIn("***", out)
        self.assertIn("calling z.ai", out, "полезные строки лога маска не съедает")

    def test_shas_and_ids_survive(self):
        """git-sha и id контейнеров ей нужны целыми — маскируем по контексту, не всё длинное."""
        raw = "launch runner @ 63d70af\ncontainer 4f2a1b9c8d3e started\nRan 1054 tests"
        self.assertEqual(serverapp.mask_secrets(raw), raw)

    def test_empty_is_empty(self):
        self.assertEqual(serverapp.mask_secrets(""), "")

    def test_log_allowlist_is_enforced_in_the_observatory(self):
        self.assertTrue(serverapp.agent_log_allowed("relay"))
        self.assertTrue(serverapp.agent_log_allowed("praxis-serverapp"))
        for foreign in ("dasha-bot", "vroom", "amnezia-awg2", "praxis-dockerproxy", ""):
            self.assertFalse(serverapp.agent_log_allowed(foreign))

    def test_client_refuses_foreign_units_without_a_request(self):
        import hostview as hv
        called = []
        orig = hv._get
        hv._get = lambda p: called.append(p)
        try:
            out = hv.logs("dasha-bot")
        finally:
            hv._get = orig
        self.assertIn("не в моей зоне", out)
        self.assertEqual(called, [], "чужой юнит не должен даже дойти до сети")


class TestToolIsOwnerOnlyAndReadOnly(unittest.TestCase):
    def test_registered_for_owner_and_scout(self):
        import agent
        self.assertIn("server_status", agent.TOOL_IMPL)
        self.assertIn("server_status", [t["name"] for t in agent.OWNER_TOOLS])
        self.assertNotIn("server_status", [t["name"] for t in agent.BASE_TOOLS],
                         "чужим каналам машина Егора не показывается")
        self.assertNotIn("server_status", [t["name"] for t in agent.FAMILY_TOOLS])
        self.assertIn("server_status", agent._SCOUT_TOOL_NAMES,
                      "разведчику можно: он read-only по построению")

    def test_tool_description_promises_no_writes(self):
        import agent
        desc = agent.SERVER_STATUS_TOOL["description"].lower()
        self.assertIn("read-only", desc)
        self.assertIn("cannot restart", desc)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
