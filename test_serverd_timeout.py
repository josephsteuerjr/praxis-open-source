"""Срок ожидания демона-хозяйки: молчание сервера — это ОТВЕТ, а не вечность.

26.07. Её `coding_session` дважды уходил к praxis-serverd и не возвращался. Со стороны —
вечное зависание: ход жив, держит единый замок `_ONE_MIND`, а с ним и её саму; Егор пишет
в чат и не получает ответа. Разбор показал, что зависания как такового не было: срок в
клиенте стоял, но по умолчанию — 3600 секунд. Час молчания на привязку задачи, которая
занимает миллисекунды. Практически это неотличимо от вечности.

Свой срок передавал только `op.run`. Все остальные глаголы — inspect, edit, checkpoint,
op.start/poll/stop, admin.status — сидели на часе. Хуже: истёкший срок приходил как
`socket.timeout`, а тот в py3.12 — подкласс `OSError`, то есть уезжал в ветку «транспорт»
и вызов повторялся: два часа вместо одного, и «я не дождалась» под видом поломки связи.

Здесь проверяется то, что должно быть верно всегда:
  * матрёшка сроков: бюджет демона < срок клиента < потолок её руки (иначе честный ответ
    хоста не дойдёт до неё НИКОГДА — ход срубят раньше);
  * опрос состояния при сборке промпта не имеет права держать её мысль;
  * истёкший срок возвращается честной ошибкой с признанием НЕИЗВЕСТНОСТИ, называет свой
    срок и номер заявки, и НЕ ретраится;
  * снятый ретрай не открыл дорогу второй root-мутации: её собственный повтор идёт под той
    же заявкой, а не новой;
  * «не смогла спросить» не выдаётся за «демона нет»;
  * совет хозяйского советника доезжает до неё, а не выбрасывается по дороге.

Запуск:  python praxis_test.py test_serverd_timeout -v
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import tempfile
import threading
import types
import unittest
from unittest import mock

import serverd_client


class _Harness(unittest.TestCase):
    def setUp(self):
        # available() смотрит на сокет/токен реального хоста — здесь его нет.
        # ⚠ Патчи снимаются ТЕМ ЖЕ объектом, который запущен. Раньше `addCleanup` вешался на
        # второй, никогда не запущенный patcher: `stop()` на нём падал молча, подмены не
        # снимались и текли в чужие тесты (прогон `test_serverd_timeout test_pass23_2` →
        # failures=3, errors=2). Спасал только алфавитный порядок discover.
        for name, value in (("available", lambda: True), ("_token", lambda: "тест")):
            patcher = mock.patch.object(serverd_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Память о незакрытых заявках живёт в модуле — между тестами она не переезжает.
        serverd_client.forget_in_doubt()
        self.addCleanup(serverd_client.forget_in_doubt)

    def _seen(self, fn, reply=None):
        """Выполнить вызов, вернув (срок, который клиент дал сокету, аргументы запроса)."""
        seen = {}

        def fake_exchange(request, *, timeout, on_chunk=None):
            seen["timeout"] = timeout
            seen["request"] = request
            seen["calls"] = seen.get("calls", 0) + 1
            return dict(reply or {"ok": True})

        with mock.patch.object(serverd_client, "_exchange", fake_exchange):
            seen["out"] = fn()
        return seen

    def _mute_socket(self):
        """Демон, который принял запрос и замолчал навсегда."""
        class MuteSocket:
            def settimeout(self, _): pass
            def connect(self, _): pass
            def sendall(self, _): pass
            def recv(self, _): raise socket.timeout("timed out")
            def close(self): pass

        # Подменяем модуль сокетов целиком: на Windows нет AF_UNIX, и без этого тест
        # проверял бы платформу вместо логики.
        return types.SimpleNamespace(AF_UNIX=1, SOCK_STREAM=1, timeout=socket.timeout,
                                     socket=lambda *a, **k: MuteSocket())


class TestDeadlineTiers(_Harness):
    def test_fast_verb_does_not_hold_her_thought(self):
        """`admin.status` зовётся при каждой сборке промпта и идёт ВНЕ потолка руки."""
        waited = self._seen(lambda: serverd_client.status())["timeout"]
        self.assertEqual(waited, serverd_client.FAST_TIMEOUT_SEC)
        self.assertLessEqual(waited, serverd_client.DEFAULT_TIMEOUT_SEC,
                             "опрос состояния не имеет права стопорить мысль")

    def test_cheap_verb_gets_the_ordinary_deadline(self):
        for verb, args in (("op.poll", {}), ("op.list", {}),
                           ("workspace.inspect", {"action": "read", "path": "x"})):
            with self.subTest(verb=verb, args=args):
                waited = self._seen(lambda: serverd_client.call(verb, args))["timeout"]
                self.assertEqual(waited, serverd_client.DEFAULT_TIMEOUT_SEC)

    def test_semantic_inspect_is_not_cheap(self):
        """`status`/`orientation` зовут обход исходников — это не чтение одного файла.

        Худший ЗАКОННЫЙ случай `workspace.inspect` (git diff 120 + git_root 15 + sh 60) уже
        не влезает в обычный срок; срубить его на 180с — соврать «состояние НЕИЗВЕСТНО» на
        успешной работе.
        """
        for action in ("status", "orientation", "impact", "overview", "diff", "search"):
            with self.subTest(action=action):
                waited = self._seen(
                    lambda: serverd_client.workspace_inspect_result("/tmp", action))["timeout"]
                self.assertEqual(waited, serverd_client.LONG_TIMEOUT_SEC)

    def test_long_verbs_keep_their_room(self):
        cases = (
            ("host_ctl", lambda: serverd_client.host_ctl("pkg", action="install")),
            ("audit_verify", lambda: serverd_client.audit_verify()),
            ("checkpoint", lambda: serverd_client.checkpoint("/tmp")),
            ("edit", lambda: serverd_client.workspace_edit("/tmp", "write", path="a")),
        )
        for name, fn in cases:
            with self.subTest(name=name):
                self.assertEqual(self._seen(fn)["timeout"], serverd_client.LONG_TIMEOUT_SEC)

    def test_unknown_verb_waits_long_not_short(self):
        """Ошибаться безопаснее в сторону «подожду», чем «срублю её работу»."""
        waited = self._seen(lambda: serverd_client.call("workspace.something_new", {}))["timeout"]
        self.assertEqual(waited, serverd_client.LONG_TIMEOUT_SEC)

    def test_explicit_deadline_still_wins(self):
        waited = self._seen(lambda: serverd_client.call("op.poll", {}, timeout=42))["timeout"]
        self.assertEqual(waited, 42)


class TestNesting(unittest.TestCase):
    """Матрёшка: бюджет демона < срок клиента < потолок её руки."""

    def test_the_house_numbers(self):
        """Значения дома: 480 на демоне < 540 у клиента < 600 потолок руки."""
        for name in ("PRAXIS_TOOL_CEILING_SEC", "PRAXIS_SERVERD_TIMEOUT_SEC",
                     "PRAXIS_SERVERD_LONG_TIMEOUT_SEC", "PRAXIS_SERVERD_FAST_TIMEOUT_SEC"):
            if os.getenv(name):
                self.skipTest(f"{name} переопределён средой — числа дома проверять нечем")
        self.assertEqual(serverd_client.TOOL_CEILING_SEC, 600.0)
        self.assertEqual(serverd_client.LONG_TIMEOUT_SEC, 540.0)
        self.assertEqual(serverd_client.DEFAULT_TIMEOUT_SEC, 180.0)
        self.assertEqual(serverd_client.FAST_TIMEOUT_SEC, 8.0)
        self.assertEqual(serverd_client.SERVER_BUDGET_SEC, 480)

    def test_live_constants_are_nested(self):
        self.assertLessEqual(serverd_client.FAST_TIMEOUT_SEC, serverd_client.DEFAULT_TIMEOUT_SEC)
        self.assertLessEqual(serverd_client.DEFAULT_TIMEOUT_SEC, serverd_client.LONG_TIMEOUT_SEC)
        self.assertLess(serverd_client.SERVER_BUDGET_SEC, serverd_client.LONG_TIMEOUT_SEC)
        if serverd_client.TOOL_CEILING_SEC > 0:
            self.assertLess(serverd_client.LONG_TIMEOUT_SEC, serverd_client.TOOL_CEILING_SEC,
                            "иначе ход срубят раньше, чем демон ответит — и она не узнает исход")

    def test_a_removed_ceiling_is_not_mistaken_for_a_default_one(self):
        """`PRAXIS_TOOL_CEILING_SEC=0` в agent.py значит «потолка нет» — не 600."""
        with mock.patch.dict("os.environ", {"PRAXIS_TOOL_CEILING_SEC": "0"}):
            self.assertEqual(serverd_client._ceiling(), 0.0)
        with mock.patch.dict("os.environ", {"PRAXIS_TOOL_CEILING_SEC": "не число"}):
            self.assertEqual(serverd_client._ceiling(), 600.0)

    def test_boundary_long_is_clamped_under_the_ceiling(self):
        # Ровно граница: 900 при потолке 600 — то, что стояло в первой редакции правки.
        self.assertEqual(serverd_client._nested(900, 600, 180), 540)
        # Равенство потолку — тоже недостижимо, а значит зажимается.
        self.assertEqual(serverd_client._nested(600, 600, 180), 540)
        # На волосок ниже — оставляем как есть, чужой осознанный выбор не переписываем.
        self.assertEqual(serverd_client._nested(599, 600, 180), 599)
        # Потолок снят (agent: TOOL_CEILING_SEC <= 0) — зажимать не от чего.
        self.assertEqual(serverd_client._nested(900, 0, 180), 900)
        # Длинный не может оказаться короче обычного.
        self.assertEqual(serverd_client._nested(60, 600, 180), 180)
        # При домашнем потолке 600 длинный срок обязан покрывать честный худший случай
        # демона: `workspace.checkpoint` — это 60+120+60, `inspect diff` — 120+15+60.
        self.assertGreater(serverd_client._nested(540, 600, 180), 240)

    def test_run_budget_never_lets_the_daemon_outlive_the_client(self):
        """При ЛЮБОМ окружении: бюджет демона < моё ожидание ≤ длинный срок.

        Инвариант проверяется на подменённых константах, а не только на домашних 540/480:
        дефект жил именно там, где `SERVER_BUDGET_SEC` оставался «подстановкой по умолчанию»,
        а не потолком, и просимые 600с уходили демону целиком.
        """
        for long_v, budget_v in ((540.0, 480), (300.0, 240), (120.0, 60), (60.0, 1), (10.0, 9)):
            for asked in (0, 1, 30, 480, 600, 100000):
                with self.subTest(long=long_v, budget=budget_v, asked=asked):
                    with mock.patch.object(serverd_client, "LONG_TIMEOUT_SEC", long_v), \
                            mock.patch.object(serverd_client, "SERVER_BUDGET_SEC", budget_v):
                        budget, waiting = serverd_client._run_budget(asked)
                    self.assertGreaterEqual(budget, 1)
                    self.assertGreater(waiting, budget)
                    self.assertLessEqual(waiting, long_v)
                    if asked > 0:
                        self.assertLessEqual(budget, asked,
                                             "чужой достижимый срок не растягиваем")

    def test_run_budget_is_a_ceiling_not_only_a_default(self):
        budget, waiting = serverd_client._run_budget(0)
        self.assertEqual(budget, serverd_client.SERVER_BUDGET_SEC)
        self.assertEqual(serverd_client._run_budget(10 ** 6)[0], serverd_client.SERVER_BUDGET_SEC,
                         "бюджет демона — потолок; иначе он догорит после моей смерти")
        self.assertEqual(serverd_client._run_budget(30)[0], 30)
        self.assertLessEqual(waiting, serverd_client.LONG_TIMEOUT_SEC)

    def test_the_manifest_admits_the_budget_is_a_ceiling(self):
        """Закон 2: усечение 600→480 существует — значит оно в манифесте, а не только в коде."""
        row = {r["id"]: r for r in serverd_client.deadlines()}["serverd.server_budget"]
        self.assertEqual(row["seconds"], float(serverd_client.SERVER_BUDGET_SEC))
        self.assertIn("потолок", row["applies"])
        self.assertIn("op.start", row["applies"], "куда идти за работой длиннее")

    def test_broken_env_does_not_take_the_container_down(self):
        with mock.patch.dict("os.environ", {"PRAXIS_SERVERD_TIMEOUT_SEC": "3m"}):
            self.assertEqual(serverd_client._seconds("PRAXIS_SERVERD_TIMEOUT_SEC", 180.0), 180.0)
        with mock.patch.dict("os.environ", {"PRAXIS_SERVERD_TIMEOUT_SEC": "-5"}):
            self.assertEqual(serverd_client._seconds("PRAXIS_SERVERD_TIMEOUT_SEC", 180.0), 180.0)

    def test_every_deadline_is_named_for_the_manifest(self):
        """Закон 2: молчаливых пределов нет. Манифест берёт их отсюда, а не выдумывает."""
        rows = {row["id"]: row for row in serverd_client.deadlines()}
        self.assertEqual(rows["serverd.fast"]["seconds"], serverd_client.FAST_TIMEOUT_SEC)
        self.assertEqual(rows["serverd.default"]["seconds"], serverd_client.DEFAULT_TIMEOUT_SEC)
        self.assertEqual(rows["serverd.long"]["seconds"], serverd_client.LONG_TIMEOUT_SEC)
        for row in rows.values():
            self.assertTrue(row.get("applies"), f"срок {row['id']} без объяснения — молчаливый")


class TestSilenceIsAnAnswer(_Harness):
    def test_silence_comes_back_as_an_honest_answer(self):
        """Истёкший срок — ответ, а не исключение; и он НЕ выдаёт себя за провал."""
        with mock.patch.object(serverd_client, "socket", self._mute_socket()):
            out = serverd_client.call("workspace.inspect", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "timeout")
        self.assertNotEqual(out["code"], "transport",
                            "socket.timeout ⊂ OSError: молчание уезжало в ветку транспорта")
        self.assertIn("НЕИЗВЕСТНЫМ", out["error"],
                      "не выдаём молчание за доказанный провал: команда могла выполниться")
        self.assertIn(f"{serverd_client.LONG_TIMEOUT_SEC:.0f}с", out["error"],
                      "закон 2: срок обязан быть назван в том, что она видит")
        self.assertIn(out["request_id"], out["error"],
                      "по заявке она может спросить демона об исходе")
        self.assertEqual(out["deadline_s"], round(serverd_client.LONG_TIMEOUT_SEC, 1))

    def test_a_timeout_is_never_retried(self):
        """Ретрай по таймауту — это удвоенное молчание, о котором ей не сказали."""
        attempts = []

        def fake_exchange(request, *, timeout, on_chunk=None):
            attempts.append(request["request_id"])
            return {"ok": False, "code": "timeout", "request_id": request["request_id"],
                    "error": "молчит"}

        with mock.patch.object(serverd_client, "_exchange", fake_exchange):
            out = serverd_client.call("host.pkg", {"action": "install"})
        self.assertEqual(len(attempts), 1)
        self.assertNotIn("retried", out)

    def test_transport_break_is_still_retried_under_the_same_id(self):
        """Обрыв связи — другое дело: повтор тем же номером безопасен и нужен."""
        attempts = []

        def fake_exchange(request, *, timeout, on_chunk=None):
            attempts.append(request["request_id"])
            return {"ok": False, "code": "transport", "request_id": request["request_id"]}

        with mock.patch.object(serverd_client, "_exchange", fake_exchange):
            out = serverd_client.call("workspace.edit", {"action": "write"})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertTrue(out["retried"])

    def test_streaming_chunks_do_not_reset_the_deadline(self):
        """Со `stream=True` срок обязан быть на ВЕСЬ обмен, иначе он ей соврал."""
        clock = {"now": 0.0}
        seen = []

        class ChattySocket:
            def settimeout(self, value): seen.append(value)
            def connect(self, _): pass
            def sendall(self, _): pass

            def recv(self, _):
                clock["now"] += 100.0          # каждый чанк «стоит» 100 секунд
                if clock["now"] > 5000:        # чтобы поломка ловилась падением, а не зависанием
                    return b'{"type":"result","ok":true}\n'
                return b'{"type":"chunk","data":"..."}\n'

            def close(self): pass

        fake = types.SimpleNamespace(AF_UNIX=1, SOCK_STREAM=1, timeout=socket.timeout,
                                     socket=lambda *a, **k: ChattySocket())
        with mock.patch.object(serverd_client, "socket", fake), \
                mock.patch.object(serverd_client.time, "monotonic", lambda: clock["now"]):
            out = serverd_client.call("op.run", {"command": "x"}, stream=True,
                                      on_chunk=lambda _t: None, timeout=250)
        self.assertEqual(out["code"], "timeout")
        self.assertLess(seen[-1], seen[1], "остаток срока обязан убывать, а не сбрасываться")
        self.assertLessEqual(clock["now"], 350, "ждали примерно свои 250с, а не бесконечность")


class TestNoSecondMutation(_Harness):
    """Снятый ретрай не должен открывать дорогу второй root-мутации."""

    def _timeout_then(self, reply):
        state = {"ids": [], "replies": [None, reply]}

        def fake_exchange(request, *, timeout, on_chunk=None):
            state["ids"].append(request["request_id"])
            row = state["replies"].pop(0) if state["replies"] else {"ok": True}
            if row is None:                       # первая попытка: демон промолчал
                return serverd_client._timed_out(request["request_id"], timeout)
            row = dict(row)
            row.setdefault("request_id", request["request_id"])
            return row

        return state, fake_exchange

    def test_her_repeat_goes_under_the_same_request_id(self):
        state, fake = self._timeout_then({"ok": True, "text": "checkpoint 8f3a"})
        with mock.patch.object(serverd_client, "_exchange", fake):
            first = serverd_client.checkpoint("/srv/app", "спасательный")
            second = serverd_client.checkpoint("/srv/app", "спасательный")
        self.assertEqual(state["ids"][0], state["ids"][1],
                         "новый uuid = вторая мутация на хосте; повтор обязан спросить об исходе")
        self.assertIn("НЕИЗВЕСТНЫМ", first)
        self.assertIn("та же заявка", second, "закон 3: она должна знать, что это эхо, а не прогон")

    def test_a_resumed_read_is_not_dressed_as_an_echo(self):
        """Чтение демон пересчитывает заново — значит это НЕ эхо, и говорить так нельзя."""
        state, fake = self._timeout_then({"ok": True, "text": "a.py · 120B"})
        with mock.patch.object(serverd_client, "_exchange", fake):
            serverd_client.workspace_inspect_result("/tmp", "read", path="x")
            out = serverd_client.workspace_inspect_result("/tmp", "read", path="x")
        self.assertEqual(state["ids"][0], state["ids"][1],
                         "номер бережём и здесь: он ничего не стоит и ничего не ломает")
        self.assertEqual(out["text"], "a.py · 120B")
        self.assertNotIn("note", out)
        self.assertNotIn("resumed_note", out)

    def test_a_json_answer_survives_the_annotation(self):
        """`forge.py:1652,1921` разбирают `text` как JSON — приписка сломала бы план и impact."""
        out = serverd_client._annotate({"ok": True, "text": '{"changed": ["a.py"]}'},
                                       "признание", verb="workspace.edit")
        self.assertEqual(json.loads(out["text"]), {"changed": ["a.py"]})
        self.assertEqual(out["resumed_note"], "признание",
                         "признание не теряется, а живёт в своём ключе")
        prose = serverd_client._annotate({"ok": True, "text": "готово"}, "признание",
                                         verb="host.systemctl")
        self.assertIn("признание", prose["text"])

    def test_a_daemon_note_is_not_clobbered(self):
        """`brokerops._budgeted` кладёт в `note` СВОЁ признание об усечении обхода.

        Затереть его своим — обменять одно молчание на другое: она перестала бы знать, что
        ответ демона неполный.
        """
        out = serverd_client._annotate({"ok": True, "text": "", "note": "обход усечён"},
                                       "признание", verb="host.file")
        self.assertEqual(out["note"], "обход усечён")
        self.assertEqual(out["resumed_note"], "признание")
        # Обе приписки доезжают до неё, а не вытесняют друг друга.
        printed = "\n".join(serverd_client._with_note([], out))
        self.assertIn("обход усечён", printed)
        self.assertIn("признание", printed)

    def test_a_resumed_host_verb_speaks_even_when_the_host_prints_nothing(self):
        """`systemctl restart` на успехе печатает ПУСТО (`hostverbs.py:145-148`).

        Именно здесь признание терялось: оно уезжало в ключ `note`, которого не читает никто,
        а `tool_host_ctl` (`agent.py:3337-3352`) показывал ей сохранённую расписку из `_cached`
        — «ok (exit 0)» и снимок before/after пятнадцатиминутной давности — как состояние
        хоста СЕЙЧАС. Проверяем то поле, которое агент реально печатает.
        """
        state, fake = self._timeout_then({"ok": True, "exit": 0, "text": "",
                                          "before": "active", "after": "active"})
        with mock.patch.object(serverd_client, "_exchange", fake):
            serverd_client.host_ctl("systemctl", action="restart", unit="praxis-mailbot")
            out = serverd_client.host_ctl("systemctl", action="restart", unit="praxis-mailbot")
        self.assertEqual(state["ids"][0], state["ids"][1])
        self.assertIn("та же заявка", str(out.get("text") or ""),
                      "агент печатает text — признание обязано быть там, а не в мёртвом ключе")

    def test_a_resumed_run_with_empty_output_says_so(self):
        """Тот же провал на `op.run`: пустой вывод — и эхо подавалось как свежий прогон."""
        state, fake = self._timeout_then({"ok": True, "body": "", "status": "finished",
                                          "exit": 0, "id": "op-1"})
        with mock.patch.object(serverd_client, "_exchange", fake):
            serverd_client.run("/srv/app", "systemctl restart nginx", timeout=120)
            out = serverd_client.run("/srv/app", "systemctl restart nginx", timeout=120)
        self.assertEqual(state["ids"][0], state["ids"][1])
        self.assertIn("(empty output)", out)
        self.assertIn("та же заявка", out,
                      "закон 3: старая расписка не имеет права выглядеть свежим фактом")

    def test_a_resumed_start_does_not_look_like_a_fresh_launch(self):
        state, fake = self._timeout_then({"ok": True, "id": "op-7", "supervisor_pid": 42,
                                          "cwd": "/srv/app", "log": "/var/log/op-7"})
        with mock.patch.object(serverd_client, "_exchange", fake):
            serverd_client.process("/srv/app", "start", command="make", name="build")
            out = serverd_client.process("/srv/app", "start", command="make", name="build")
        self.assertEqual(state["ids"][0], state["ids"][1])
        self.assertIn("та же заявка", out)

    def test_a_different_call_is_not_hijacked(self):
        state, fake = self._timeout_then({"ok": True, "text": "ok"})
        with mock.patch.object(serverd_client, "_exchange", fake):
            serverd_client.checkpoint("/srv/app", "спасательный")
            serverd_client.checkpoint("/srv/other", "другой корень")
        self.assertNotEqual(state["ids"][0], state["ids"][1],
                            "чужая заявка = id_conflict у демона и потерянная работа")

    def test_the_third_call_is_a_real_repeat(self):
        """Заборов нет: спросить об исходе можно один раз, дальше — её осознанный повтор."""
        state, fake = self._timeout_then({"ok": True, "text": "ok"})
        with mock.patch.object(serverd_client, "_exchange", fake):
            serverd_client.checkpoint("/srv/app", "спасательный")
            serverd_client.checkpoint("/srv/app", "спасательный")
            serverd_client.checkpoint("/srv/app", "спасательный")
        self.assertEqual(state["ids"][0], state["ids"][1])
        self.assertNotIn(state["ids"][2], state["ids"][:2])

    def test_a_stale_request_is_forgotten(self):
        """Окно памяти конечно и названо: через него повтор снова становится повтором."""
        state, fake = self._timeout_then({"ok": True, "text": "ok"})
        clock = {"now": 1000.0}
        with mock.patch.object(serverd_client, "_exchange", fake), \
                mock.patch.object(serverd_client.time, "monotonic", lambda: clock["now"]):
            serverd_client.checkpoint("/srv/app", "спасательный")
            clock["now"] += serverd_client.IN_DOUBT_WINDOW_SEC + 1
            serverd_client.checkpoint("/srv/app", "спасательный")
        self.assertNotEqual(state["ids"][0], state["ids"][1])

    def test_an_explicit_request_id_is_untouched(self):
        """Форж-матрица уже носит свой ключ идемпотентности — не подменяем его."""
        state, fake = self._timeout_then({"ok": True})
        with mock.patch.object(serverd_client, "_exchange", fake):
            serverd_client.call("op.run", {"command": "pytest"}, request_id="verify-42-lint")
            serverd_client.call("op.run", {"command": "pytest"}, request_id="verify-42-lint")
        self.assertEqual(state["ids"], ["verify-42-lint", "verify-42-lint"])


class TestStateLine(_Harness):
    def test_silence_is_not_reported_as_absence(self):
        """«Не смогла спросить» ≠ «демона нет». Эта строка едет в манифест и в блок состояния."""
        with mock.patch.object(serverd_client, "socket", self._mute_socket()):
            line = serverd_client.state_line()
        self.assertIn("НЕИЗВЕСТНО", line)
        self.assertNotIn("unavailable", line)
        self.assertIn(f"{serverd_client.FAST_TIMEOUT_SEC:.0f}с", line)

    def test_a_real_refusal_still_reads_as_unavailable(self):
        with mock.patch.object(serverd_client, "_exchange",
                               lambda *a, **k: {"ok": False, "code": "auth", "error": "плохой токен"}):
            line = serverd_client.state_line()
        self.assertIn("unavailable", line)
        self.assertIn("плохой токен", line, "причина отказа не должна теряться по дороге")

    def test_a_live_broker_reads_as_alive(self):
        reply = {"ok": True, "protocol": serverd_client.PROTOCOL,
                 "operations": [{"status": "running"}, {"status": "done"}],
                 "audit": {"ok": True}}
        with mock.patch.object(serverd_client, "_exchange", lambda *a, **k: dict(reply)):
            line = serverd_client.state_line()
        self.assertIn("operations running 1", line)
        self.assertIn("audit verified", line)


class TestAdviceReachesHer(_Harness):
    def test_run_no_longer_drops_the_advice(self):
        """Совет советника демона выбрасывался здесь — до неё он не доезжал никогда."""
        reply = {"ok": True, "body": "готово", "status": "finished", "exit": 0,
                 "advice": "эта команда трогает /etc — сделай checkpoint"}
        out = self._seen(lambda: serverd_client.run("/srv/app", "apt-get upgrade", timeout=120),
                         reply=reply)["out"]
        self.assertIn("советник:", out)
        self.assertIn("/etc", out)

    def test_a_run_without_its_own_deadline_names_the_budget(self):
        """`coding_run(timeout=0)` обещает «без предела» — на host-пути это неправда."""
        seen = self._seen(lambda: serverd_client.run("/srv/app", "make", timeout=0),
                          reply={"ok": True, "body": "готово"})
        self.assertEqual(seen["request"]["args"]["timeout"], serverd_client.SERVER_BUDGET_SEC,
                         "бюджет обязан быть передан демону явно, иначе он догорит после нас")
        self.assertLessEqual(seen["timeout"], serverd_client.LONG_TIMEOUT_SEC)
        self.assertGreater(seen["timeout"], seen["request"]["args"]["timeout"],
                           "клиент обязан пережить бюджет демона, иначе исход не услышан")
        self.assertIn("[срок]", seen["out"])
        self.assertIn(str(serverd_client.SERVER_BUDGET_SEC), seen["out"])

    def test_the_client_always_outlives_the_daemon(self):
        """Инвариант, а не числа: бюджет демона < моё ожидание ≤ длинный срок < потолок руки.

        ⚠ Здесь стоял тест, который ЗАЩИЩАЛ дефект: он требовал `args["timeout"] == 600` при
        `seen["timeout"] == 540`, то есть кодифицировал «демон считает дольше, чем я слушаю»
        как норму. На этом и держалась инверсия: команда, идущая 545с, честно доводилась
        демоном и рапортовалась, а она получала «не ответил за 540с, состояние НЕИЗВЕСТНО».
        До правки (`max(60, timeout+60)`) клиент ждал 660 и ответ доходил — это был регресс.
        """
        for asked in (0, 30, 120, 480, 540, 600, 1800):
            with self.subTest(asked=asked):
                seen = self._seen(
                    lambda: serverd_client.run("/srv/app", "make -j4", timeout=asked),
                    reply={"ok": True, "body": "готово"})
                budget = seen["request"]["args"]["timeout"]
                waited = seen["timeout"]
                self.assertGreater(waited, budget,
                                   "клиент ушёл раньше демона — честный частичный ответ "
                                   "(status=timed_out + хвост лога + op id) слушать некому")
                self.assertGreaterEqual(waited - budget, 30,
                                        "run_wait (brokerops.py:598) добивает процесс ещё 30с "
                                        "после бюджета — их надо пережить")
                self.assertLessEqual(waited, serverd_client.LONG_TIMEOUT_SEC)
                if serverd_client.TOOL_CEILING_SEC > 0:
                    self.assertLess(waited, serverd_client.TOOL_CEILING_SEC)

    def test_an_asked_deadline_is_never_silently_shortened(self):
        """Закон 2: зажим 600→480 существует, значит он обязан быть назван в её ответе."""
        seen = self._seen(lambda: serverd_client.run("/srv/app", "pytest", timeout=600),
                          reply={"ok": True, "body": "готово"})
        budget = seen["request"]["args"]["timeout"]
        self.assertLess(budget, 600, "иначе демон переживёт клиента")
        self.assertIn("[срок]", seen["out"], "молчаливого усечения не бывает")
        self.assertIn("600", seen["out"], "она обязана видеть, сколько просила")
        self.assertIn(str(budget), seen["out"], "и сколько реально получила")
        self.assertIn("op.start", seen["out"], "и куда идти за работой длиннее")

    def test_a_deadline_that_fits_is_left_alone_and_unremarked(self):
        """Заборов нет: достижимый срок — её выбор, переписывать и комментировать нечего."""
        seen = self._seen(lambda: serverd_client.run("/srv/app", "pytest", timeout=120),
                          reply={"ok": True, "body": "готово"})
        self.assertEqual(seen["request"]["args"]["timeout"], 120)
        self.assertNotIn("[срок]", seen["out"])

    def test_the_live_composition_with_forge_keeps_the_nesting(self):
        """Реальный путь целиком: `coding_run` → `forge._remote_run_deadline` → этот клиент.

        Отказ жил именно в композиции: каждый слой по отдельности выглядел разумно, а вместе
        `forge` отдавал 600 (её срок «влезает в потолок руки»), а клиент зажимал ТОЛЬКО своё
        ожидание до 540 — и переворачивал матрёшку.
        """
        try:
            import forge
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"forge не импортируется в этом окружении: {exc}")
        for asked in (0, 120, 600, 1800):
            with self.subTest(asked=asked):
                given, _note = forge._remote_run_deadline(asked)
                seen = self._seen(
                    lambda: serverd_client.run("/srv/app", "make", timeout=given),
                    reply={"ok": True, "body": "готово"})
                self.assertGreater(seen["timeout"], seen["request"]["args"]["timeout"],
                                   f"coding_run(timeout={asked}) выворачивает матрёшку")

    def test_a_short_run_keeps_its_small_deadline(self):
        seen = self._seen(lambda: serverd_client.run("/srv/app", "ls", timeout=30),
                          reply={"ok": True, "body": "готово"})
        self.assertEqual(seen["timeout"], min(90.0, serverd_client.LONG_TIMEOUT_SEC))


class TestDetachedMatrix(unittest.TestCase):
    """Матрица проверок живёт в отдельном процессе — потолок её руки сюда не достаёт.

    Но и здесь час молчания был молчаливым: демону предела не передавали вовсе, клиент ждал
    ровно 3600 и в отчёте об этом сроке не было ни слова.
    """

    def _run_one(self, check, calls):
        import forge_verify
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            fake = types.SimpleNamespace(
                call=lambda verb, args, **kw: (calls.append((verb, args, kw)),
                                               {"ok": True, "body": "готово", "exit": 0})[1])
            with mock.patch.dict("sys.modules", {"serverd_client": fake}):
                row = forge_verify._run_one(0, check, root, root / "m", 0, {"checks": {}},
                                            threading.Lock(), "host", "m1", None)
            # Лог читаем ДО того, как временный каталог исчезнет вместе с ним.
            return row, pathlib.Path(row["log"]).read_text(encoding="utf-8")

    def test_a_check_without_a_deadline_tells_both_sides(self):
        calls = []
        row, log = self._run_one({"id": "lint", "command": "ruff ."}, calls)
        verb, args, kwargs = calls[0]
        self.assertEqual(verb, "op.run")
        self.assertEqual(args["timeout"], forge_verify_deadline(),
                         "демон обязан знать предел, иначе он догорит после смерти клиента")
        self.assertGreater(kwargs["timeout"], args["timeout"], "клиент переживает бюджет демона")
        self.assertEqual(row["deadline_s"], forge_verify_deadline())
        self.assertIn("[срок]", log, "закон 2: подставленный предел обязан быть назван")

    def test_a_check_with_its_own_deadline_keeps_it(self):
        calls = []
        row, log = self._run_one({"id": "tests", "command": "pytest", "timeout": 1800}, calls)
        self.assertEqual(calls[0][1]["timeout"], 1800, "чужой осознанный срок не переписываем")
        self.assertEqual(calls[0][2]["timeout"], 1860)
        self.assertEqual(row["deadline_s"], 1800)
        self.assertNotIn("[срок]", log)


class TestTheDaemonsConfessionReachesHer(_Harness):
    """27.07: демон честно признавался, а клиент выбрасывал признание.

    `workspace_inspect` на УСПЕХЕ отдавал только `text` — то есть `note` («ответ ЧАСТИЧНЫЙ —
    файлы: 3000 из ≥5000»), `budget_note` (чей это был срок) и `limits.note` («посмотрела N
    из M») до неё не доезжали вовсе. Выживало лишь то, что демон успел вшить ВНУТРЬ текста:
    ориентировка и search. `impact`, `checks`, `model`, `overview`, `symbols`, `references`,
    `diagnostics`, `review` молчали целиком — а это самые дорогие её обходы.
    """

    def _inspect(self, action, reply):
        return self._seen(lambda: serverd_client.workspace_inspect("/opt/praxis", action),
                          reply=reply)["out"]

    def test_prose_inspect_no_longer_swallows_the_truncation(self):
        out = self._inspect("status", {
            "ok": True, "text": "HOST ROOT: /opt/praxis\ngit: none",
            "truncated": True,
            "note": "ответ ЧАСТИЧНЫЙ — файлы: 3000 из ≥5000; упёрлась в кап файлов",
            "limits": {"complete": False, "stopped_by": "file-cap",
                       "note": "ответ ЧАСТИЧНЫЙ — файлы: 3000 из ≥5000; упёрлась в кап файлов"},
        })
        self.assertIn("HOST ROOT", out, "данные никуда не деваются")
        self.assertIn("ЧАСТИЧНЫЙ", out, "усечение обязано быть названо в том, что она видит")
        self.assertIn("3000 из ≥5000", out)
        self.assertEqual(out.count("3000 из ≥5000"), 1, "одно признание, а не эхо трёх ключей")

    def test_a_json_inspect_keeps_parsing_and_still_confesses(self):
        """`forge.py:1734,2016` разбирают ответ `json.loads` — приписка хвостом сломала бы его.

        Сломанный разбор — это «Remote verification plan не разобрался» и `impact = None`,
        то есть обмен одного молчания на другое.
        """
        out = self._inspect("impact", {
            "ok": True, "text": json.dumps({"changed": ["agent.py"], "truncated": True}),
            "note": "ответ ЧАСТИЧНЫЙ — файлы: 12000 из ≥40000; упёрлась в кап файлов",
            "limits": {"complete": False, "stopped_by": "file-cap", "stages": [
                {"stage": "файлы", "taken": 12000, "seen": 40000, "seen_exact": False}]},
        })
        parsed = json.loads(out)
        self.assertEqual(parsed["changed"], ["agent.py"], "данные разбираются как раньше")
        self.assertIn("ЧАСТИЧНЫЙ", parsed["serverd_note"])
        self.assertIn("12000 из ≥40000", parsed["serverd_note"])

    def test_a_complete_answer_still_says_how_much_it_saw(self):
        """«Посмотрела N из M» — признание и на полном ответе: иначе полноту не отличить."""
        out = self._inspect("checks", {
            "ok": True, "text": json.dumps({"checks": [{"id": "lint"}]}),
            "limits": {"complete": True, "stopped_by": "",
                       "note": "ответ полный — файлы: 120 из 120; бюджет 12000 файлов / 25.0с "
                               "не связывал, потрачено 1.4с"},
        })
        parsed = json.loads(out)
        self.assertEqual(parsed["checks"], [{"id": "lint"}])
        self.assertIn("120 из 120", parsed["serverd_note"])

    def test_the_budget_note_is_not_lost_when_it_is_the_only_confession(self):
        """`broker._with_budget_note` объясняет, ЧЕЙ был срок и чем его сдвинуть."""
        out = self._inspect("overview", {
            "ok": True, "text": json.dumps({"files": 3}),
            "budget_note": "считала по ТВОЕМУ сроку 60с, а не по своему (300с).",
        })
        self.assertIn("ТВОЕМУ сроку", json.loads(out)["serverd_note"])

    def test_the_daemon_is_not_made_to_repeat_itself(self):
        """Ту же приписку демон вшивает внутрь ориентировки — второй раз она шум.

        Хвост «потрачено N.Nс» пересчитывается на возврате и отличается дословно, поэтому
        сравнение идёт по началу до первой `;`, а не по всей строке.
        """
        out = self._inspect("status", {
            "ok": True,
            "text": ("HOST ROOT: /opt/praxis\nбюджет обхода: ответ полный — файлы: 120 из 120; "
                     "бюджет 12000 файлов / 25.0с не связывал, потрачено 1.0с"),
            "limits": {"complete": True, "note": "ответ полный — файлы: 120 из 120; бюджет "
                                                 "12000 файлов / 25.0с не связывал, потрачено 1.7с"},
        })
        self.assertEqual(out.count("120 из 120"), 1)
        self.assertNotIn("потрачено 1.7с", out)

    def test_a_clean_answer_is_left_exactly_as_it_was(self):
        """Чтение одного файла бюджета не тратит — дописывать туда нечего."""
        out = self._inspect("read", {"ok": True, "text": "a.py · sha256=ab\n    1\timport os"})
        self.assertEqual(out, "a.py · sha256=ab\n    1\timport os")

    def test_a_failed_inspect_keeps_the_daemons_words(self):
        out = self._inspect("search", {"ok": False, "error": "bad regex: nothing to repeat",
                                       "note": "обход остановлен на 12000 файлах"})
        self.assertIn("bad regex", out)
        self.assertIn("12000 файлах", out)

    def test_broken_json_falls_back_to_prose_instead_of_swallowing(self):
        """Обрезанный демоном JSON (`_cap`) уже не разбирается — молчать всё равно нельзя."""
        out = self._inspect("model", {"ok": True, "text": '{"languages": {"py": 12',
                                      "note": "ответ ЧАСТИЧНЫЙ — упёрлась в кап символов"})
        self.assertIn("ЧАСТИЧНЫЙ", out)


class TestEveryErrorCarriesItsRequestId(_Harness):
    """Без номера заявки она не может спросить демона об исходе — а печатается только `error`."""

    def _error_of(self, reply):
        seen = self._seen(lambda: serverd_client.call("workspace.edit", {"action": "write"}),
                          reply=reply)
        return seen["out"]["error"], seen["request"]["request_id"]

    def test_transport_eof_and_daemon_refusals_all_name_the_request(self):
        for reply in ({"ok": False, "code": "transport", "error": "serverd transport: OSError"},
                      {"ok": False, "code": "eof", "error": "serverd closed without a terminal frame"},
                      {"ok": False, "code": "denied", "error": "not a file: /opt/praxis/x"},
                      {"ok": False, "code": "busy", "error": "все 3 обработчика заняты"}):
            with self.subTest(code=reply["code"]):
                error, rid = self._error_of(reply)
                self.assertIn(rid, error, "иначе исход мутации спросить нечем")

    def test_a_silent_refusal_still_names_the_request(self):
        error, rid = self._error_of({"ok": False, "code": "denied"})
        self.assertIn(rid, error)

    def test_the_number_is_not_said_twice(self):
        """У истёкшего срока номер уже в тексте — второй раз он шум."""
        with mock.patch.object(serverd_client, "socket", self._mute_socket()):
            out = serverd_client.call("workspace.inspect", {})
        self.assertEqual(out["error"].count(out["request_id"]), 1)

    def test_a_success_is_not_touched(self):
        seen = self._seen(lambda: serverd_client.call("op.list", {}), reply={"ok": True})
        self.assertNotIn("error", seen["out"])

    def test_an_unsent_request_says_so_instead_of_inventing_a_number(self):
        """Сокета нет — заявки не было; выдумывать номер здесь было бы враньём."""
        with mock.patch.object(serverd_client, "available", lambda: False):
            out = serverd_client.call("host.pkg", {"action": "install"})
        self.assertIn("не отправлялась", out["error"])
        self.assertIn("ничего не запускалось", out["error"])


class TestPkgAdmitsTheInvertedNesting(_Harness):
    """`hostverbs.pkg` держит 900с, а клиент ждёт 540 — единственная нарочная инверсия.

    Убрать её нельзя (оборванный `apt-get install -y` = полусобранный dpkg на живом хосте),
    значит она обязана быть СКАЗАНА: и до того, как ужалит, и в момент, когда ужалила.
    """

    def _pkg(self, reply, **kwargs):
        return self._seen(lambda: serverd_client.host_ctl("pkg", action="install",
                                                          names="nginx", **kwargs),
                          reply=reply)["out"]

    def test_the_inversion_is_named_before_it_bites(self):
        out = self._pkg({"ok": True, "exit": 0, "text": "nginx уже стоит"})
        self.assertIn("nginx уже стоит", out["text"], "ответ хоста не подменяется")
        self.assertIn(str(serverd_client.PKG_SERVER_BUDGET_SEC), out["text"])
        self.assertIn(f"{serverd_client.LONG_TIMEOUT_SEC:.0f}с", out["text"])
        self.assertIn("не знаю", out["text"], "«не знаю» ≠ «провалилось»")

    def test_a_timeout_names_the_way_to_learn_the_outcome(self):
        # Молчание берём настоящее (`_timed_out`), чтобы номер заявки был тот же, что уехал
        # демону, а не выдуманный фикстурой.
        def fake_exchange(request, *, timeout, on_chunk=None):
            return serverd_client._timed_out(request["request_id"], timeout)

        with mock.patch.object(serverd_client, "_exchange", fake_exchange):
            out = serverd_client.host_ctl("pkg", action="install", names="nginx")
        self.assertIn("action=query", out["error"], "дешёвый способ узнать исход")
        self.assertIn(out["request_id"], out["error"], "и номер, под которым спросить демона")
        self.assertEqual(out["error"].count(out["request_id"]), 2,
                         "номер называют оба признания — своё молчание и оговорка про pkg")
        self.assertIn("продолжится без меня", out["error"])
        self.assertNotIn("[срок]", str(out.get("text") or ""),
                         "признание кладётся в видимое поле один раз, а не в оба")

    def test_an_unmounted_broker_is_not_warned_about_a_deadline(self):
        """Заявки не было — предела, о котором предупреждать, тоже нет."""
        with mock.patch.object(serverd_client, "available", lambda: False):
            out = serverd_client.host_ctl("pkg", action="install", names="nginx")
        self.assertNotIn("[срок]", str(out.get("text") or ""))
        self.assertIn("не отправлялась", out["error"])

    def test_her_own_deadline_is_the_one_that_gets_named(self):
        """Свой срок она передаёт демону сама — врать ей про 900 в этом случае нельзя."""
        out = self._pkg({"ok": True, "text": ""}, timeout=1200)
        self.assertIn("1200", out["text"])
        self.assertNotIn(str(serverd_client.PKG_SERVER_BUDGET_SEC), out["text"])

    def test_no_inversion_no_warning(self):
        """Граница: предупреждение существует ровно пока бюджет демона БОЛЬШЕ моего срока."""
        for long_v, warned in ((float(serverd_client.PKG_SERVER_BUDGET_SEC) - 1, True),
                               (float(serverd_client.PKG_SERVER_BUDGET_SEC), False),
                               (float(serverd_client.PKG_SERVER_BUDGET_SEC) + 1, False)):
            with self.subTest(long=long_v):
                with mock.patch.object(serverd_client, "LONG_TIMEOUT_SEC", long_v):
                    out = self._pkg({"ok": True, "text": "готово"})
                self.assertEqual("[срок]" in out["text"], warned)

    def test_other_host_verbs_are_not_given_a_pkg_warning(self):
        out = self._seen(lambda: serverd_client.host_ctl("systemctl", action="restart",
                                                         unit="praxis-mailbot"),
                         reply={"ok": True, "text": "", "exit": 0})["out"]
        self.assertNotIn("[срок]", str(out.get("text") or ""))

    def test_the_manifest_admits_the_inversion_too(self):
        """Закон 2 часть (б): предел назван не только в ответе, но и в манифесте рельсов."""
        row = {r["id"]: r for r in serverd_client.deadlines()}["serverd.long"]
        self.assertIn("host.pkg", row["applies"])
        self.assertIn(str(serverd_client.PKG_SERVER_BUDGET_SEC), row["applies"])
        self.assertIn("action=query", row["applies"], "и способ узнать исход потом")


class TestProcessBranchesKeepTheNote(_Harness):
    """Приписка демона обязана доезжать на КАЖДОЙ ветке, а не там, где о ней вспомнили."""

    def test_every_branch_prints_the_daemon_note(self):
        cases = (
            ("start", {"ok": True, "id": "op-1", "supervisor_pid": 4, "cwd": "/srv",
                       "log": "/l", "note": "пределы: живых host-операций 3 из 24"}),
            ("poll", {"ok": True, "id": "op-1", "body": "", "note": "пределы: 3 из 24"}),
            ("stop", {"ok": True, "id": "op-1", "text": "остановила", "note": "пределы: 3 из 24"}),
            ("list", {"ok": True, "operations": [], "note": "пределы: 3 из 24"}),
        )
        for action, reply in cases:
            with self.subTest(action=action):
                out = self._seen(lambda: serverd_client.process("/srv", action, command="make",
                                                                operation_id="op-1"),
                                 reply=reply)["out"]
                self.assertIn("3 из 24", out)

    def test_a_failed_branch_keeps_both_the_reason_and_the_note(self):
        for action in ("start", "poll", "list"):
            with self.subTest(action=action):
                out = self._seen(lambda: serverd_client.process("/srv", action, command="make",
                                                                operation_id="op-1"),
                                 reply={"ok": False, "error": "unknown operation op-1",
                                        "note": "пределы: 3 из 24"})["out"]
                self.assertIn("unknown operation", out)
                self.assertIn("3 из 24", out)


class TestAHostRefusalKeepsItsExplanation(_Harness):
    """27.07: «отказал без объяснения» — при том, что объяснение лежало в соседнем ключе.

    `hostverbs` на ненулевом коде возвращает `ok:False` и кладёт причину ХОЗЯИНА в `text`
    (вывод systemctl/docker/apt); ключа `error` у него нет вовсе (`hostverbs.py:145-150`).
    Первая редакция `_with_rid` дописывала выдуманный `error` — и `tool_host_ctl`
    (`agent.py`) по непустому `error` выходил рано, выбрасывая `text`/`exit`/`было`/`стало`.
    Живьём это превращало «Failed to restart nginx.service: Unit nginx.service not found»
    в «[serverd] praxis-serverd отказал без объяснения»: объяснение было и было потеряно.
    """

    FAILED_UNIT = {
        "ok": False, "verb": "systemctl", "action": "restart", "unit": "nginx", "exit": 5,
        "text": "Failed to restart nginx.service: Unit nginx.service not found.",
        "before": {"active": "inactive"}, "after": {"active": "inactive"},
    }

    def test_the_hosts_own_words_are_not_replaced_by_a_shrug(self):
        seen = self._seen(lambda: serverd_client.host_ctl("systemctl", action="restart",
                                                          unit="nginx"),
                          reply=dict(self.FAILED_UNIT))
        out, rid = seen["out"], seen["request"]["request_id"]
        self.assertIn("Unit nginx.service not found", out["text"])
        self.assertFalse(str(out.get("error") or "").strip(),
                         "выдуманный error перебивал живое объяснение и рубил ответ руки")
        self.assertNotIn("без объяснения", json.dumps(out, ensure_ascii=False))
        self.assertIn(rid, out["text"], "номер заявки кладётся туда, где объяснение")
        self.assertEqual(out["exit"], 5, "код возврата не теряется по дороге")

    def test_the_number_is_appended_to_the_explanation_exactly_once(self):
        already = serverd_client._with_rid(
            {"ok": False, "text": "Unit nginx.service not found. [заявка abc123]"}, "abc123")
        self.assertEqual(already["text"].count("abc123"), 1)
        self.assertFalse(already.get("error"))

    def test_a_refusal_with_nothing_at_all_still_confesses_it_has_no_reason(self):
        """Граница: «без объяснения» — правда ровно тогда, когда объяснения ВПРАВДУ нет."""
        for body in ("", "   ", None):
            with self.subTest(body=body):
                out = serverd_client._with_rid({"ok": False, "text": body}, "rid-1")
                self.assertIn("отказал без объяснения", out["error"])
                self.assertIn("rid-1", out["error"])

    def test_an_explanation_and_a_daemon_error_live_side_by_side(self):
        out = serverd_client._with_rid(
            {"ok": False, "error": "broker denied", "text": "Unit not found"}, "rid-2")
        self.assertIn("rid-2", out["error"])
        self.assertEqual(out["text"], "Unit not found",
                         "номер уже в error — второй раз в text он шум")

    def test_a_success_is_never_annotated(self):
        # `workspace.inspect impact` отдаёт JSON в `text` (`forge.py:1734` его разбирает):
        # приписка сюда сломала бы разбор, а не добавила правды.
        payload = {"ok": True, "exit": 0, "text": '{"changed": ["agent.py"]}'}
        out = serverd_client._with_rid(dict(payload), "rid-3")
        self.assertEqual(out, payload)
        self.assertEqual(json.loads(out["text"])["changed"], ["agent.py"])

    def test_a_frame_without_ok_at_all_is_read_as_a_refusal(self):
        """Все обёртки считают кадр без `ok` отказом — приписка обязана быть той же."""
        out = serverd_client._with_rid({"text": "нет такого юнита"}, "rid-4")
        self.assertIn("rid-4", out["text"])
        self.assertIn("нет такого юнита", out["text"])

    def test_the_daemons_postscript_lands_at_the_very_END_of_a_long_output(self):
        """Приписка про срок дописывается в ХВОСТ — и голый `text[:4000]` съедал её всегда.

        Здесь фиксируется место приписки; то, что она переживает рез руки, проверяет
        `test_truth_agent.HostOutputTruthTests`.
        """
        if serverd_client.LONG_TIMEOUT_SEC >= serverd_client.PKG_SERVER_BUDGET_SEC:
            self.skipTest("инверсии нет в этой среде — предупреждать не о чем")
        apt = "\n".join(f"Get:{i} http://deb.debian.org/debian bookworm/main amd64 nginx"
                        for i in range(200))
        self.assertGreater(len(apt), 4000)
        out = self._seen(lambda: serverd_client.host_ctl("pkg", action="install", names="nginx"),
                         reply={"ok": True, "exit": 0, "text": apt})["out"]
        self.assertGreater(out["text"].index("[срок]"), 4000,
                           "признание живёт за капом 4000 — достать его можно только с хвоста")
        self.assertTrue(out["text"].rstrip().endswith("неизвестность, а не провал."))


def forge_verify_deadline() -> int:
    import forge_verify
    return forge_verify.DETACHED_HOST_DEADLINE_SEC


if __name__ == "__main__":
    unittest.main()
