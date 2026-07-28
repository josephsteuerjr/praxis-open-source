"""Раннер перестаёт врать: расписка прямой отправки, обход комнат, резолв, пропуски.

Каждый тест здесь привязан к живому инциденту, а не к желаемому поведению:

* `run-20260723T063041453368Z-ac7b7431` — `RunConflict` на УЖЕ доставленном сообщении
  1193, потому что проекция расписки пересчитывалась живьём при повторе тула;
* `run-20260726T180824623461Z-ca66f57b` — две расписки (`results/0007`/`0012`), один
  текст, разный `followup_request`;
* 20.07 07:42–18:46 — в логе только `group backfill [-1003908850919]`, у живой
  `-1003959517654` за 11ч04м ни одного тика (`return` вместо `continue`);
* `parse_phone('-100500') -> '100500'` на прод-интерпретаторе 26.07 — числовая строка
  уходит в Telethon как номер телефона;
* `.state/group_context/-100500-*.json` и `groups/-100500-*/MAP.md` с mtime текущей
  минуты у комнаты, мёртвой с 06.07.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="praxis-truth-runner-session-"))
atexit.register(shutil.rmtree, _SESSION_DIR, True)
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test")
os.environ["TELEGRAM_SESSION"] = str(_SESSION_DIR / "telethon")
os.environ.setdefault("PRAXIS_TEST", "1")

import agent  # noqa: E402
import group_context  # noqa: E402
import media  # noqa: E402
import mtproto_runner as runner  # noqa: E402
import notes  # noqa: E402
import perception  # noqa: E402
import run_context  # noqa: E402
import run_manager  # noqa: E402
import telegram_followups  # noqa: E402
import telegram_outbox  # noqa: E402
import telegram_topics  # noqa: E402


class DirectOutboxProjectionTests(unittest.TestCase):
    """P0: расписка идемпотентна, значит её проекция не имеет права пересчитываться."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-runner-")
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.manager = run_manager.RunManager(self.base)
        self.spool = media.MediaSpool(self.base / "workspace" / "media")
        self._old_manager = agent._RUN_MANAGER
        self._old_spool = agent._MEDIA_SPOOL
        agent._RUN_MANAGER = self.manager
        agent._MEDIA_SPOOL = self.spool
        self.addCleanup(self._restore)
        self.outbox = telegram_outbox.TelegramOutbox(self.base / "outbox")
        self._old_outbox = runner._DIRECT_OUTBOX
        runner._DIRECT_OUTBOX = self.outbox
        self.addCleanup(self._restore_outbox)

    def _restore(self):
        agent._RUN_MANAGER = self._old_manager
        agent._MEDIA_SPOOL = self._old_spool

    def _restore_outbox(self):
        runner._DIRECT_OUTBOX = self._old_outbox

    def _run_with_started_send(self, *, run_id: str, call_id: str, text: str,
                               to: str) -> dict:
        channel = agent.ChannelContext(
            chat_id="555000100", room_id="555000100", principal_id="555000100",
            is_dm=True, owner=True, known=True, addressed=True,
            address_message_id=1245, address_kind="direct",
            reply_targets=((1245, "Yegor", "continue"),),
        )
        context = run_context.RunContext.create(
            run_id=run_id, kind="chat_turn", goal="напиши в абстракт",
            principal_id=str(channel.principal_id), scope=channel.scope,
            origin_chat_id=channel.chat_id,
            origin_message_ids=agent._run_origin_message_ids(channel),
            delivery_chat_id=channel.chat_id, model_profile="voice",
        )
        persisted = self.manager.create(
            context,
            agent._run_context_markdown(
                ctx=channel, kind=context.kind, goal=context.goal,
                conversation="immutable conversation", history=None,
                extra="immutable runtime frame",
            ),
        )
        self.manager.transition(persisted.run_id, "running", expected="pending")
        key = f"telegram-outbox:{run_id}:tool:{call_id}"
        self.manager.start_tool(
            run_id, call_id, "send_message", {"to": to, "text": text},
            side_effect=True, idempotency_key=key,
        )
        entry = self.outbox.prepare_text(
            key, peer_id=-1001341326876, text=text,
            run_id=run_id, call_id=call_id, purpose="tool:send_message",
        )
        return {"run_id": run_id, "call_id": call_id, "tool": "send_message",
                "idempotency_key": key, "entry": entry}

    def test_replay_reuses_the_stored_projection_instead_of_conflicting(self):
        started = self._run_with_started_send(
            run_id="run-truth-projection", call_id="call-truth-1",
            text="It’s Praxis, bitch — and I’m back.", to="@abstractDL",
        )
        execution = {k: started[k] for k in ("run_id", "call_id", "tool")}
        entry = started["entry"]
        first = {
            "target_label": "AbstractDL (@abstractDL, id 1341326876)",
            "target_user_id": None,
            "pulse_id": "pulse-42",
            "followup_request": "угу, выходи:)",
        }
        # Второй заход того же call_id — ровно то, что делает resume-исполнитель через
        # 3.1с: пульс уже не активен, буфер Егора уехал вперёд.
        drifted = dict(first, pulse_id="",
                       followup_request="Твой пост в абстракт не ушёл: @abstractDL — канал.")

        agent.run_direct_outbox_prepared(entry, **runner._durable_outbox_projection(
            execution, first))
        agent.run_direct_outbox_prepared(entry, **runner._durable_outbox_projection(
            execution, drifted))

        stored = agent._direct_outbox_intent(started["run_id"], started["call_id"])
        self.assertEqual(stored["projection"]["followup_request"],
                         first["followup_request"])
        self.assertEqual(stored["projection"]["pulse_id"], "pulse-42")
        receipts = [row for row in self.manager.iter_events(started["run_id"], strict=True)
                    if row.get("kind") == "direct_outbox_intent"]
        self.assertEqual(len(receipts), 1)

    def test_without_the_fix_the_same_replay_still_raises_run_conflict(self):
        """Контроль: тест выше не вакуумный — живой пересчёт по-прежнему конфликтует."""
        started = self._run_with_started_send(
            run_id="run-truth-conflict", call_id="call-truth-2",
            text="It’s Praxis, bitch — and I’m back.", to="@abstractDL",
        )
        entry = started["entry"]
        agent.run_direct_outbox_prepared(
            entry, target_label="AbstractDL (@abstractDL, id 1341326876)",
            target_user_id=None, pulse_id="pulse-42",
            followup_request="угу, выходи:)",
        )
        with self.assertRaises(run_manager.RunConflict):
            agent.run_direct_outbox_prepared(
                entry, target_label="AbstractDL (@abstractDL, id 1341326876)",
                target_user_id=None, pulse_id="",
                followup_request="Твой пост в абстракт не ушёл: @abstractDL — канал.",
            )

    def test_first_call_keeps_live_values_when_no_receipt_exists_yet(self):
        live = {"target_label": "Кто-то (id 7)", "target_user_id": 7,
                "pulse_id": "p1", "followup_request": "жду ответ"}
        with patch.object(agent, "_direct_outbox_intent", return_value=None):
            self.assertEqual(
                runner._durable_outbox_projection(
                    {"run_id": "r", "call_id": "c"}, live), live)

    def test_unreadable_receipt_does_not_swallow_the_live_projection(self):
        live = {"target_label": "Кто-то (id 7)", "target_user_id": 7,
                "pulse_id": "p1", "followup_request": "жду ответ"}
        with patch.object(agent, "_direct_outbox_intent",
                          side_effect=agent.DurableExecutionError("duplicated")):
            self.assertEqual(
                runner._durable_outbox_projection(
                    {"run_id": "r", "call_id": "c"}, live), live)

    def test_receipt_without_a_field_falls_back_to_the_live_value_for_it(self):
        live = {"target_label": "Кто-то (id 7)", "target_user_id": 7,
                "pulse_id": "p1", "followup_request": "жду ответ"}
        with patch.object(agent, "_direct_outbox_intent", return_value={
                "projection": {"pulse_id": "старый"}}):
            kept = runner._durable_outbox_projection({"run_id": "r", "call_id": "c"}, live)
        self.assertEqual(kept["pulse_id"], "старый")
        self.assertEqual(kept["target_user_id"], 7)
        self.assertEqual(kept["followup_request"], "жду ответ")

    def test_both_direct_send_doors_go_through_the_durable_projection(self):
        import inspect
        for name in ("_sync_send_message", "_sync_send_file"):
            source = inspect.getsource(getattr(runner, name))
            self.assertIn("_durable_outbox_projection", source, name)
            self.assertNotIn("run_direct_outbox_prepared(\n        entry, target_label",
                             source, name)


class ResolveEntityCandidateTests(unittest.IsolatedAsyncioTestCase):
    """P1: числовая строка уходит в Telethon как НОМЕР ТЕЛЕФОНА (contacts.GetContacts)."""

    def setUp(self):
        self._old_cache = dict(runner._entity_cache)
        runner._entity_cache.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        runner._entity_cache.clear()
        runner._entity_cache.update(self._old_cache)

    async def _calls_for(self, ref):
        get_entity = AsyncMock(side_effect=ValueError("no such peer"))
        client = types.SimpleNamespace(get_entity=get_entity)
        with patch.object(runner, "client", client):
            self.assertIsNone(await runner._resolve_entity(ref))
        return [call.args[0] for call in get_entity.await_args_list]

    async def test_negative_chat_id_never_reaches_telethon_as_a_string(self):
        # '-100500' -> parse_phone -> '100500' -> contacts.GetContactsRequest(0):
        # полная выгрузка адресной книги, и раньше ДВАЖДЫ за вызов.
        self.assertEqual(await self._calls_for("-100500"), [-100500])
        self.assertEqual(await self._calls_for(-100500), [-100500])
        self.assertEqual(await self._calls_for("-1001240718803"), [-1001240718803])

    async def test_positive_digits_keep_the_phone_book_hand_but_number_first(self):
        # Телефон в её адресной книге по-прежнему резолвится строкой — но только ПОСЛЕ
        # числа и ровно один раз, а не дважды.
        self.assertEqual(await self._calls_for("79991234567"),
                         [79991234567, "79991234567"])

    async def test_username_is_tried_exactly_once(self):
        self.assertEqual(await self._calls_for("@abstractDL"), ["@abstractDL"])

    async def test_successful_resolve_still_caches_by_reference_and_id(self):
        entity = types.SimpleNamespace(id=1341326876)
        client = types.SimpleNamespace(get_entity=AsyncMock(return_value=entity))
        with patch.object(runner, "client", client):
            self.assertIs(await runner._resolve_entity("-1001341326876"), entity)
        self.assertIs(runner._entity_cache["-1001341326876"], entity)
        self.assertIs(runner._entity_cache["1341326876"], entity)


class _BackfillHarness(unittest.IsolatedAsyncioTestCase):
    """Общий стенд обхода комнат: чистые глобалы, живой транспорт, свой файл пропусков."""

    def setUp(self):
        self._old_misses = dict(runner._backfill_resolve_misses)
        self._old_cursor = runner._backfill_cursor
        self._old_online = runner._backfill_was_online
        runner._backfill_resolve_misses.clear()
        runner._backfill_cursor = 0
        runner._backfill_was_online = True
        # Транспорт для этих тестов ЖИВ: настоящий `client` в тестах не подключён, и без
        # подмены бэкфилл честно вышел бы на первой же строке «связи нет».
        self._online = patch.object(runner, "_backfill_transport_online", return_value=True)
        self._online.start()
        self.addCleanup(self._online.stop)
        # Отсрочки теперь ложатся в perception — под тестом это должен быть свой файл,
        # а не живое кольцо пропусков в memory/.state.
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-backfill-")
        self.addCleanup(self._temp.cleanup)
        self.skips = Path(self._temp.name) / "perception_skips.jsonl"
        self._old_skips_path = perception.SKIPS_PATH
        perception.SKIPS_PATH = self.skips
        perception._LAST_SKIP.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        runner._backfill_resolve_misses.clear()
        runner._backfill_resolve_misses.update(self._old_misses)
        runner._backfill_cursor = self._old_cursor
        runner._backfill_was_online = self._old_online
        perception.SKIPS_PATH = self._old_skips_path
        perception._LAST_SKIP.clear()

    def _skip_rows(self) -> list[dict]:
        if not self.skips.exists():
            return []
        return [json.loads(line) for line in
                self.skips.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _patches(self, rooms_list, *, resolvable, due=None):
        resolve = AsyncMock(side_effect=lambda peer: (
            types.SimpleNamespace(id=abs(int(peer)))
            if str(peer) in resolvable else None))
        backfill = AsyncMock(return_value=None)
        return resolve, backfill, (
            patch.object(runner.rooms, "list_rooms", return_value=list(rooms_list)),
            patch.object(runner.rooms, "room_policy", return_value={"backfill_limit": 200}),
            patch.object(runner.group_context, "backfill_due",
                         side_effect=lambda peer, limit: (
                             True if due is None else str(peer) in due)),
            patch.object(runner, "_meta_for_peer", return_value=(None, None)),
            patch.object(runner, "_resolve_entity", resolve),
            patch.object(runner, "_backfill_group_context", backfill),
        )


class BackfillHeadOfLineTests(_BackfillHarness):
    """P2: `return` вместо `continue` — вечный блокировщик впереди по алфавиту."""

    async def test_dead_room_no_longer_blocks_the_rooms_behind_it(self):
        # Порядок сортировки живой: '8' (0x38) > '-' (0x2D), поэтому положительный
        # peer_id всегда оказывается ПОСЛЕ мёртвой комнаты.
        resolve, backfill, patches = self._patches(
            ["-100500", "555000100"], resolvable={"555000100"})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
        self.assertEqual(backfill.await_count, 1)
        self.assertEqual(str(backfill.await_args.args[0]), "555000100")

    async def test_a_resolve_miss_is_remembered_and_not_retried_before_the_ttl(self):
        resolve, backfill, patches = self._patches(["-100500"], resolvable=set())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
            self.assertEqual(resolve.await_count, 1)
            await runner._group_context_backfill_once()
            self.assertEqual(resolve.await_count, 1)  # промах закэширован
            # Граница ПЕРВОЙ отсрочки — минута, а не четверть часа.
            first = runner.BACKFILL_MISS_BACKOFF_SEC
            runner._backfill_resolve_misses["-100500"] = {"ts": time.time() - first + 5, "n": 1}
            await runner._group_context_backfill_once()
            self.assertEqual(resolve.await_count, 1)
            runner._backfill_resolve_misses["-100500"] = {"ts": time.time() - first - 5, "n": 1}
            await runner._group_context_backfill_once()
            self.assertEqual(resolve.await_count, 2)
            # ...и второй промах подряд стоит уже вдвое дороже: на 61-й секунде рано.
            self.assertEqual(runner._backfill_resolve_misses["-100500"]["n"], 2)
            runner._backfill_resolve_misses["-100500"] = {"ts": time.time() - first - 5, "n": 2}
            await runner._group_context_backfill_once()
            self.assertEqual(resolve.await_count, 2)

    def test_miss_backoff_grows_from_a_minute_and_is_capped_at_the_ttl(self):
        self.assertEqual(runner._backfill_miss_ttl(1), runner.BACKFILL_MISS_BACKOFF_SEC)
        self.assertEqual(runner._backfill_miss_ttl(2), runner.BACKFILL_MISS_BACKOFF_SEC * 2)
        self.assertEqual(runner._backfill_miss_ttl(4), runner.BACKFILL_MISS_BACKOFF_SEC * 8)
        # Потолок: сколько бы промахов ни было, дороже 15 минут комната не стоит.
        self.assertEqual(runner._backfill_miss_ttl(50), runner.BACKFILL_MISS_TTL_SEC)
        self.assertLessEqual(runner._backfill_miss_ttl(5), runner.BACKFILL_MISS_TTL_SEC)

    async def test_a_dead_transport_never_poisons_the_miss_cache(self):
        # Зонд до починки: 20 нерезолвимых комнат при мёртвом клиенте укладывались в
        # промах-кэш по 5 за тик (5/20 → 10/20 → 15/20 → 20/20), и после восстановления
        # связи `_backfill_group_context` не звался ещё 15 минут.
        peers = [f"-100{n}" for n in range(20)]
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        with patch.object(runner, "_backfill_transport_online", return_value=False):
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                for _ in range(5):
                    await runner._group_context_backfill_once()
        self.assertEqual(resolve.await_count, 0)
        self.assertEqual(runner._backfill_resolve_misses, {})
        # Связь вернулась — первая же комната обрабатывается в ТОТ ЖЕ тик.
        resolve, backfill, patches = self._patches(peers, resolvable=set(peers))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
        self.assertEqual(backfill.await_count, 1)

    async def test_reconnect_forgets_misses_that_were_really_about_the_network(self):
        peers = ["-1001", "-1002"]
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
        self.assertEqual(set(runner._backfill_resolve_misses), set(peers))
        # Обрыв, затем возврат связи: отсрочки, снятые вокруг обрыва, забываются целиком.
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        with patch.object(runner, "_backfill_transport_online", return_value=False):
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                await runner._group_context_backfill_once()
        resolve, backfill, patches = self._patches(peers, resolvable=set(peers))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
        self.assertEqual(backfill.await_count, 1)

    async def test_a_tick_held_entirely_by_the_miss_cache_is_not_silent(self):
        # До починки такой тик не писал НИ ОДНОЙ строки: комнаты пустые, объяснения нет.
        peers = ["-1001", "-1002"]
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
            with self.assertLogs(runner.log, level="INFO") as captured:
                await runner._group_context_backfill_once()
        self.assertEqual(resolve.await_count, 2)  # второй тик резолв не трогал
        held = [line for line in captured.output if "под отсрочкой" in line]
        self.assertEqual(len(held), 1)
        for peer in peers:
            self.assertIn(peer, held[0])

    async def test_resolve_cap_is_named_out_loud_and_rotates_to_the_rest(self):
        peers = [f"-100{n}" for n in range(10)]
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertLogs(runner.log, level="INFO") as captured:
                await runner._group_context_backfill_once()
        self.assertEqual(resolve.await_count, runner.BACKFILL_RESOLVES_PER_TICK)
        cap_lines = [line for line in captured.output if "кап резолвов" in line]
        self.assertEqual(len(cap_lines), 1)
        self.assertIn(f"резолвила {runner.BACKFILL_RESOLVES_PER_TICK} комнат из 10",
                      cap_lines[0])
        # Отложенные названы поимённо, а не спрятаны за многоточием.
        for peer in peers[runner.BACKFILL_RESOLVES_PER_TICK:]:
            self.assertIn(peer, cap_lines[0])

    async def test_cursor_gives_every_room_a_turn_across_ticks(self):
        peers = [f"-100{n}" for n in range(10)]
        seen: set[str] = set()
        for _ in range(len(peers)):
            runner._backfill_resolve_misses.clear()
            resolve, backfill, patches = self._patches(peers, resolvable=set())
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                await runner._group_context_backfill_once()
            seen.update(str(call.args[0]) for call in resolve.await_args_list)
        self.assertEqual(seen, set(peers))

    async def test_cap_is_still_named_when_a_room_did_get_backfilled(self):
        # Успешно обработанная комната не отменяет того, что до других в этот тик руки
        # не дошли: иначе кап снова стал бы молчаливым.
        peers = [f"-100{n}" for n in range(10)]
        warm = peers[7]
        backfill = AsyncMock(return_value=None)
        resolve = AsyncMock(return_value=None)
        with (
            patch.object(runner.rooms, "list_rooms", return_value=peers),
            patch.object(runner.rooms, "room_policy", return_value={"backfill_limit": 200}),
            patch.object(runner.group_context, "backfill_due", return_value=True),
            patch.object(runner, "_meta_for_peer", side_effect=lambda peer: (
                (str(peer), {"entity": types.SimpleNamespace(id=7)})
                if str(peer) == warm else (None, None))),
            patch.object(runner, "_resolve_entity", resolve),
            patch.object(runner, "_backfill_group_context", backfill),
        ):
            with self.assertLogs(runner.log, level="INFO") as captured:
                await runner._group_context_backfill_once()
        self.assertEqual(backfill.await_count, 1)
        cap_lines = [line for line in captured.output if "кап резолвов" in line]
        self.assertEqual(len(cap_lines), 1)
        for peer in peers[runner.BACKFILL_RESOLVES_PER_TICK:7]:
            self.assertIn(peer, cap_lines[0])

    async def test_the_sixtieth_room_is_not_stranded_behind_the_window(self):
        # Срез стоял ДО курсора (`rooms.list_rooms()[:50]`), поэтому курсор крутился по
        # модулю пятидесяти и комната с индексом ≥50 не становилась головой окна НИКОГДА.
        # 60 комнат: если срез снова уедет вперёд курсора, последние десять сюда не
        # попадут ни за один тик — тест покраснеет на «-100059».
        peers = sorted(f"-100{n:03d}" for n in range(60))
        self.assertGreater(len(peers), runner.BACKFILL_ROOMS_PER_TICK)
        last = peers[-1]
        served: list[str] = []
        resolve, backfill, patches = self._patches(peers, resolvable=set(peers))
        backfill.side_effect = lambda peer, entity, limit=None: served.append(str(peer))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            for _ in range(len(peers)):
                await runner._group_context_backfill_once()
        # Каждый тик обрабатывает голову окна, а голова едет по кругу полного списка:
        # за len(rooms) тиков свой тик получают ВСЕ, включая шестидесятую.
        self.assertIn(last, served)
        self.assertEqual(sorted(served), peers)
        # ...и не позже, чем через len(rooms) тиков — иначе «достанется следующему тику»
        # снова было бы обещанием без срока.
        self.assertLessEqual(served.index(last) + 1, len(peers))

    async def test_the_room_window_cap_is_named_out_loud_and_where_she_reads_it(self):
        # Второй кап (ширина окна) молчал вовсе: он и был тем самым спящим ружьём.
        peers = sorted(f"-100{n:03d}" for n in range(60))
        outside = len(peers) - runner.BACKFILL_ROOMS_PER_TICK
        resolve, backfill, patches = self._patches(peers, resolvable=set(peers))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertLogs(runner.log, level="INFO") as captured:
                await runner._group_context_backfill_once()
                await runner._group_context_backfill_once()
        window = [line for line in captured.output if "вне окна" in line]
        self.assertEqual(len(window), 2)  # в логе — каждый тик
        self.assertIn(f"окно обхода {runner.BACKFILL_ROOMS_PER_TICK} комнат из {len(peers)}",
                      window[0])
        self.assertIn(f"вне окна в этот тик {outside}", window[0])
        # Ей — та же правда, и ровно одной записью: detail стабилен между тиками, иначе
        # вращение окна рождало бы новую строку в кольце пропусков каждую минуту.
        rows = [row for row in self._skip_rows() if "вне окна" in row["detail"]]
        self.assertEqual(len(rows), 1, [row["detail"] for row in rows])
        self.assertEqual(rows[0]["class"], "отложила")
        self.assertIn(str(runner.BACKFILL_ROOMS_PER_TICK), rows[0]["detail"])
        self.assertIn(f"{outside} комнат вне окна", rows[0]["detail"])
        self.assertIn(f"через {len(peers)} тиков", rows[0]["detail"])

    async def test_a_list_that_fits_the_window_says_nothing_about_it(self):
        # Невакуумность: пока комнат меньше окна (сегодня их три) — ни строки лишнего шума.
        peers = ["-1001", "-1002", "-1003"]
        resolve, backfill, patches = self._patches(peers, resolvable=set(peers))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with self.assertLogs(runner.log, level="INFO") as captured:
                runner.log.info("маркер")  # assertLogs требует хотя бы одну запись
                await runner._group_context_backfill_once()
        self.assertEqual([line for line in captured.output if "вне окна" in line], [])
        self.assertEqual([row for row in self._skip_rows()
                          if "вне окна" in row["detail"]], [])

    async def test_one_failing_room_does_not_end_the_pass(self):
        peers = ["-100500", "555000100"]
        backfill = AsyncMock(return_value=None)
        resolve = AsyncMock(side_effect=lambda peer: types.SimpleNamespace(id=1))

        def due(peer, limit):
            if str(peer) == "-100500":
                raise RuntimeError("архив недоступен")
            return True

        with (
            patch.object(runner.rooms, "list_rooms", return_value=peers),
            patch.object(runner.rooms, "room_policy", return_value={"backfill_limit": 200}),
            patch.object(runner.group_context, "backfill_due", side_effect=due),
            patch.object(runner, "_meta_for_peer", return_value=(None, None)),
            patch.object(runner, "_resolve_entity", resolve),
            patch.object(runner, "_backfill_group_context", backfill),
        ):
            await runner._group_context_backfill_once()
        self.assertEqual(backfill.await_count, 1)
        self.assertEqual(str(backfill.await_args.args[0]), "555000100")


class BackfillSkipVisibilityTests(_BackfillHarness):
    """Закон 2: пределы бэкфилла обязаны быть названы ЕЙ, а не только в log.info.

    Ровно тот критерий, который `_note_one_mind_defer` применил к `_ONE_MIND`: гейт,
    живущий одной строкой лога контейнера, — молчаливое ограничение. На вопрос «почему в
    этой комнате пусто» `manage_perception("skips")` обязан отвечать.
    """

    async def test_a_resolve_miss_names_the_room_and_the_delay_where_she_reads_it(self):
        resolve, backfill, patches = self._patches(["-100500"], resolvable=set())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
        rows = [row for row in self._skip_rows() if row["stage"] == "group_backfill"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["class"], "отложила")
        self.assertEqual(rows[0]["chat"], "-100500")
        # Срок назван числом, и это ПЕРВАЯ отсрочка — минута, а не 15.
        self.assertIn("1 мин", rows[0]["detail"])
        self.assertIn("промах 1", rows[0]["detail"])

    async def test_the_resolve_cap_is_named_where_she_reads_it_too(self):
        peers = [f"-100{n}" for n in range(10)]
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
        cap = [row for row in self._skip_rows()
               if row["stage"] == "group_backfill" and "кап" in row["detail"]]
        self.assertEqual(len(cap), 1)
        self.assertIn(str(runner.BACKFILL_RESOLVES_PER_TICK), cap[0]["detail"])
        self.assertEqual(cap[0]["class"], "отложила")

    async def test_rooms_held_by_the_cache_are_named_where_she_reads_it(self):
        peers = ["-1001", "-1002"]
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
            await runner._group_context_backfill_once()
        held = [row for row in self._skip_rows() if "под отсрочкой" in row["detail"]]
        self.assertEqual(len(held), 1)
        for peer in peers:
            self.assertIn(peer, held[0]["detail"])

    async def test_the_held_notice_stops_repeating_once_the_set_is_stable(self):
        # Курсор крутится каждый тик, поэтому порядок комнат в обходе всякий раз другой.
        # Если бы detail шёл в порядке обхода, perception не схлопнул бы записи и она
        # получала бы новую строку КАЖДУЮ минуту про один и тот же факт — шум, из-за
        # которого кольцо пропусков перестают читать. Меняется факт (комнат стало
        # больше) — новая запись законна; не меняется ничего — записи быть не должно.
        peers = [f"-100{n}" for n in range(8)]
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            # Два тика набирают все 8 комнат в промах-кэш (кап 5 за тик), третий —
            # первый, где НИЧЕГО больше не меняется; с него и начинаем считать.
            for _ in range(3):
                await runner._group_context_backfill_once()
            self.assertEqual(len(runner._backfill_resolve_misses), len(peers))
            before = len([row for row in self._skip_rows()
                          if "под отсрочкой" in row["detail"]])
            for _ in range(4):
                await runner._group_context_backfill_once()
        after = [row for row in self._skip_rows() if "под отсрочкой" in row["detail"]]
        self.assertEqual(len(after), before, [row["detail"] for row in after])

    async def test_a_dead_transport_says_so_instead_of_going_quiet(self):
        peers = ["-1001"]
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        with patch.object(runner, "_backfill_transport_online", return_value=False):
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                await runner._group_context_backfill_once()
        rows = [row for row in self._skip_rows() if row["stage"] == "group_backfill"]
        self.assertEqual(len(rows), 1)
        self.assertIn("связи нет", rows[0]["detail"])
        self.assertNotIn("chat", rows[0])  # это про весь обход, а не про комнату

    async def test_her_own_closed_window_is_not_reported_as_a_network_failure(self):
        # Закон 3: «я закрыла связь сама» и «связь пропала» — разные факты.
        peers = ["-1001"]
        resolve, backfill, patches = self._patches(peers, resolvable=set())
        runner._EXPECT_DISCONNECT.set()
        self.addCleanup(runner._EXPECT_DISCONNECT.clear)
        with patch.object(runner, "_backfill_transport_online", return_value=False):
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                await runner._group_context_backfill_once()
        rows = [row for row in self._skip_rows() if row["stage"] == "group_backfill"]
        self.assertEqual(len(rows), 1)
        self.assertIn("моим же окном", rows[0]["detail"])
        self.assertNotIn("связи нет", rows[0]["detail"])

    async def test_a_healthy_tick_records_no_skip_at_all(self):
        # Контроль невакуумности: записи появляются только когда что-то ДЕЙСТВИТЕЛЬНО
        # отложено, иначе кольцо пропусков превратилось бы в шум и перестало читаться.
        peers = ["-1001"]
        resolve, backfill, patches = self._patches(peers, resolvable={"-1001"})
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await runner._group_context_backfill_once()
        self.assertEqual(backfill.await_count, 1)
        self.assertEqual(self._skip_rows(), [])


class OneMindSkipVisibilityTests(unittest.IsolatedAsyncioTestCase):
    """Закон 2: отложенное пробуждение обязано быть видно ей, а не только в логе."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-skips-")
        self.addCleanup(self._temp.cleanup)
        self.skips = Path(self._temp.name) / "perception_skips.jsonl"
        self._old_path = perception.SKIPS_PATH
        perception.SKIPS_PATH = self.skips
        perception._LAST_SKIP.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        perception.SKIPS_PATH = self._old_path
        perception._LAST_SKIP.clear()

    def _rows(self) -> list[dict]:
        if not self.skips.exists():
            return []
        return [json.loads(line) for line in
                self.skips.read_text(encoding="utf-8").splitlines() if line.strip()]

    async def test_deferred_task_window_writes_the_reason_where_sleep_window_writes_it(self):
        async with runner._ONE_MIND:
            self.assertIsNone(await runner._task_window("ночная ревизия"))
        rows = self._rows()
        self.assertEqual([row["stage"] for row in rows], ["one_mind:task_window"])
        self.assertEqual(rows[0]["class"], "отложила")
        self.assertIn("ночная ревизия", rows[0]["detail"])

    async def test_deferred_wake_writes_its_own_reason(self):
        async with runner._ONE_MIND:
            self.assertIsNone(await runner._wake_pass("разбуди меня со связью"))
        rows = self._rows()
        self.assertEqual([row["stage"] for row in rows], ["one_mind:wake_pass"])
        self.assertIn("разбуди меня со связью", rows[0]["detail"])

    async def test_an_open_lock_records_nothing(self):
        with patch.object(runner.agent, "wake_turn", return_value=None):
            self.assertTrue(await runner._wake_pass("обычный подъём"))
        self.assertEqual(self._rows(), [])


class ProjectionFreshnessTests(unittest.TestCase):
    """P2: холостой тик безусловно переписывал фикстуру и делал её самой свежей комнатой."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-groupctx-")
        self.addCleanup(self._temp.cleanup)
        base = Path(self._temp.name)
        self._saved = (group_context.BASE, group_context.STATE_DIR,
                       group_context.GROUPS_DIR)
        group_context.BASE = base
        group_context.STATE_DIR = base / ".state" / "group_context"
        group_context.GROUPS_DIR = base / "groups"
        self.addCleanup(self._restore)

    def _restore(self):
        (group_context.BASE, group_context.STATE_DIR,
         group_context.GROUPS_DIR) = self._saved

    def test_a_room_without_an_archive_is_built_once_and_then_left_alone(self):
        peer = "-100500"
        first = group_context.projection(peer)
        self.assertEqual(first.get("message_count", 0), 0)
        target = group_context.projection_path(peer)
        md = group_context.projection_markdown_path(peer)
        stamps = (target.stat().st_mtime_ns, md.stat().st_mtime_ns)
        for _ in range(5):
            group_context.projection(peer)
        self.assertEqual((target.stat().st_mtime_ns, md.stat().st_mtime_ns), stamps)

    def test_an_appearing_archive_still_invalidates_the_cached_projection(self):
        peer = "-1003959517654"
        group_context.projection(peer)
        target = group_context.projection_path(peer)
        before = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(int(before.get("archive_size") or 0), 0)
        group_context.observe_message(
            peer_id=peer, topic_id=None, message_id=11, sender_id=7,
            sender_name="Кто-то", reply_to_message_id=None,
            timestamp="2026-07-26T22:00:00+00:00",
            text="первое сообщение комнаты",
        )
        after = group_context.projection(peer)
        self.assertEqual(after.get("message_count"), 1)
        self.assertGreater(int(after.get("archive_size") or 0), 0)

    def test_backfill_due_no_longer_rewrites_the_map_on_every_tick(self):
        peer = "-100500"
        self.assertTrue(group_context.backfill_due(peer, 200))
        md = group_context.projection_markdown_path(peer)
        stamp = md.stat().st_mtime_ns
        for _ in range(5):
            self.assertTrue(group_context.backfill_due(peer, 200))
        self.assertEqual(md.stat().st_mtime_ns, stamp)


class _DirectSendTraceHarness(unittest.TestCase):
    """Стенд прямой отправки: свои архив, записка, буфер и леджер — живая память не тронута."""

    OWNER = 555000100
    GROUP = -1001240718803
    SAID = ("Поправка к моей реплике выше: я неточно сказала «у меня на Opus 5». "
            "Мой рабочий движок — gpt-5.6-sol, а Opus 5 у меня в терминале.")

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-trace-")
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self._saved = (group_context.GROUPS_DIR, group_context.STATE_DIR,
                       notes.SCRATCH_DIR)
        group_context.GROUPS_DIR = self.base / "groups"
        group_context.STATE_DIR = self.base / ".state" / "group_context"
        notes.SCRATCH_DIR = self.base / ".scratch"
        self.addCleanup(self._restore_paths)
        # ⚠ Без этого архив комнаты под тестами выключен (`_group_archive_enabled`), и
        # тест на архив был бы зелёным при ЛЮБОЙ реализации.
        self._env = patch.dict(os.environ, {"PRAXIS_TEST_GROUP_ARCHIVE": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.ledger = telegram_followups.FollowUpLedger(self.base / "followups.json")
        for target, attr, value in (
            (runner, "OWNER_ID", self.OWNER),
            (runner, "_self_id", 7770001),
            (runner.telegram_followups, "LEDGER", self.ledger),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        for module, name in (("telegram_contacts", "mark_outbound"),
                             ("social_pulse", "note_outbound"),
                             ("unanswered", "resolve")):
            p = patch.object(getattr(runner, module), name)
            p.start()
            self.addCleanup(p.stop)
        self._touched: list[str] = []
        self.addCleanup(self._restore_buffers)

    def _restore_paths(self):
        (group_context.GROUPS_DIR, group_context.STATE_DIR,
         notes.SCRATCH_DIR) = self._saved

    def _restore_buffers(self):
        for convo in self._touched:
            runner._buf.pop(convo, None)
            runner._meta.pop(convo, None)

    def _accepted(self, *, peer_id, text, message_id=94243, topic_id=None,
                  reply_to=None, projection=None, kind="text",
                  at=1785000000.0) -> tuple[dict, dict]:
        """Расписка и принятая запись очереди ровно той формы, что даёт telegram_outbox."""
        call_id = f"call-{message_id}"
        key = f"telegram-outbox:run-truth:tool:{call_id}"
        identity = {"key": key, "peer_id": peer_id, "random_id": 4242,
                    "run_id": "run-truth", "call_id": call_id}
        proof = {"entry": identity, "projection": dict(projection or {})}
        entry = {"id": "e1", "key": key, "kind": kind, "state": "accepted",
                 "peer_id": peer_id, "topic_id": topic_id, "reply_to": reply_to,
                 "random_id": 4242, "payload": {"text": text},
                 "receipt": {"message_id": message_id, "random_id": 4242},
                 "purpose": "tool:send_message", "updated_at": at}
        self._touched.append(str(peer_id))
        return proof, entry

    def _archived(self, peer_id) -> list[dict]:
        return list(group_context.iter_records(str(peer_id)))


class DirectSendLeavesTheSameTraceAsVoiceTests(_DirectSendTraceHarness):
    """P0 27.07 02:29: она отправила поправку второй раз, реплаем на собственное #94144.

    Номер своей реплики она знала (леджер лежал в кадре), а ТЕКСТА не было нигде: прямая
    отправка оставляла один след из четырёх — буфер комнаты, который живёт только внутри
    хода по этой комнате. Пульс в комнату не заходит. На проде 22 из 22 её прямых
    сообщений в группы отсутствуют в `memory/groups/*/archive.jsonl` (у голоса там 591),
    а в `.scratch/-1001240718803.md` того дня стоят 17:20 и 18:45 — и дырка на 18:44.
    """

    def test_direct_group_send_leaves_note_archive_and_buffer(self):
        proof, entry = self._accepted(peer_id=self.GROUP, text=self.SAID)
        result = runner._project_direct_outbox_acceptance(proof, entry)
        self.assertTrue(result.startswith("projected:"), result)
        convo = str(self.GROUP)

        self.assertIn(f"Praxis: {self.SAID}", list(runner._buf[convo]))

        # Записка — БУКВА В БУКВУ та же, что пишет голос: её читают other_rooms_digest,
        # _presence_evidence и said_recently, и они узнают только эту форму.
        scratch = notes.path_for(convo).read_text(encoding="utf-8")
        self.assertIn(f"сказала (голос): «{self.SAID[:notes.SAID_GIST_CHARS]}»", scratch)
        self.assertTrue(notes.said_recently(convo, self.SAID))

        rows = [row for row in self._archived(self.GROUP)
                if row.get("message_id") == 94243]
        self.assertEqual(len(rows), 1, self._archived(self.GROUP))
        self.assertTrue(rows[0]["outgoing"])
        self.assertEqual(rows[0]["sender_name"], "Praxis")
        self.assertEqual(rows[0]["sender_id"], 7770001)
        self.assertEqual(rows[0]["text"], self.SAID)

    def test_a_topic_send_lands_in_its_own_topic_and_its_own_scratch(self):
        # Границы веток: комната одна, разговоры разные. Записка обязана лечь в ключ
        # ветки (иначе said_recently смешает темы), а архив — в свой topic_id.
        proof, entry = self._accepted(peer_id=self.GROUP, text=self.SAID,
                                      topic_id=15, reply_to=15, message_id=94250)
        runner._project_direct_outbox_acceptance(proof, entry)
        convo = telegram_topics.TopicRoute(str(self.GROUP), 15).conversation_id
        self._touched.append(convo)
        self.assertNotEqual(convo, str(self.GROUP))
        self.assertTrue(notes.path_for(convo).exists())
        rows = [row for row in self._archived(self.GROUP)
                if row.get("message_id") == 94250]
        self.assertEqual(rows[0]["topic_id"], 15)

    def test_direct_dm_send_writes_note_but_no_group_archive(self):
        # 52 из 74 её прямых отправок — в ЛС. У ЛС архива комнаты нет и быть не должно.
        peer = 555000333
        proof, entry = self._accepted(peer_id=peer, text=self.SAID, message_id=1271)
        runner._project_direct_outbox_acceptance(proof, entry)
        self.assertTrue(notes.said_recently(str(peer), self.SAID))
        self.assertIn(f"Praxis: {self.SAID}", list(runner._buf[str(peer)]))
        self.assertEqual(self._archived(peer), [])

    def test_a_broken_note_costs_neither_the_delivery_nor_the_other_two_traces(self):
        # Мутация, которую этот тест ловит: слить три try в один. Тогда падение записки
        # уносит с собой архив — и её слово снова выпадает из истории комнаты.
        proof, entry = self._accepted(peer_id=self.GROUP, text=self.SAID)
        with patch.object(notes, "append", side_effect=OSError("диск полон")):
            with self.assertLogs(runner.log, level="ERROR") as captured:
                result = runner._project_direct_outbox_acceptance(proof, entry)
        self.assertTrue(result.startswith("projected:"), result)
        self.assertTrue(any("заметка о прямой отправке" in line
                            for line in captured.output), captured.output)
        self.assertIn(f"Praxis: {self.SAID}", list(runner._buf[str(self.GROUP)]))
        self.assertEqual(len([row for row in self._archived(self.GROUP)
                              if row.get("message_id") == 94243]), 1)

    def test_a_broken_archive_costs_neither_the_delivery_nor_the_note(self):
        proof, entry = self._accepted(peer_id=self.GROUP, text=self.SAID)
        with patch.object(runner.group_context, "observe_message",
                          side_effect=OSError("архив недоступен")):
            with self.assertLogs(runner.log, level="ERROR") as captured:
                result = runner._project_direct_outbox_acceptance(proof, entry)
        self.assertTrue(result.startswith("projected:"), result)
        self.assertTrue(any("архив комнаты не принял" in line
                            for line in captured.output), captured.output)
        self.assertTrue(notes.said_recently(str(self.GROUP), self.SAID))

    def test_an_empty_text_leaves_nothing_and_says_nothing(self):
        # Невакуумность: следы появляются только там, где было сказанное слово.
        proof, entry = self._accepted(peer_id=self.GROUP, text="   ", message_id=94260)
        runner._project_direct_outbox_acceptance(proof, entry)
        self.assertFalse(notes.path_for(str(self.GROUP)).exists())
        self.assertEqual(self._archived(self.GROUP), [])


class DirectSendTraceIdempotencyTests(unittest.TestCase):
    """Двух записок «сказала (голос)» быть не может: иначе повтор расписки после
    переподключения вернулся бы к ней как «я это говорила дважды»."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-trace-once-")
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.manager = run_manager.RunManager(self.base)
        self.spool = media.MediaSpool(self.base / "workspace" / "media")
        self._old = (agent._RUN_MANAGER, agent._MEDIA_SPOOL, runner._DIRECT_OUTBOX,
                     notes.SCRATCH_DIR, agent._TELETHON.get(
                         "project_direct_outbox_acceptance"))
        agent._RUN_MANAGER = self.manager
        agent._MEDIA_SPOOL = self.spool
        self.outbox = telegram_outbox.TelegramOutbox(self.base / "outbox")
        runner._DIRECT_OUTBOX = self.outbox
        notes.SCRATCH_DIR = self.base / ".scratch"
        agent._TELETHON["project_direct_outbox_acceptance"] = (
            runner._project_direct_outbox_acceptance)
        self.addCleanup(self._restore)
        for target, attr, value in ((runner, "OWNER_ID", 555000100),
                                    (runner, "_self_id", 7770001)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        for module, name in (("telegram_contacts", "mark_outbound"),
                             ("social_pulse", "note_outbound"),
                             ("unanswered", "resolve")):
            p = patch.object(getattr(runner, module), name)
            p.start()
            self.addCleanup(p.stop)
        self.ledger = telegram_followups.FollowUpLedger(self.base / "followups.json")
        p = patch.object(runner.telegram_followups, "LEDGER", self.ledger)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(runner._buf.pop, "-1001240718803", None)

    def _restore(self):
        (agent._RUN_MANAGER, agent._MEDIA_SPOOL, runner._DIRECT_OUTBOX,
         notes.SCRATCH_DIR, restore_callback) = self._old
        if restore_callback is None:
            agent._TELETHON.pop("project_direct_outbox_acceptance", None)
        else:
            agent._TELETHON["project_direct_outbox_acceptance"] = restore_callback

    def test_a_replayed_acceptance_does_not_write_a_second_note(self):
        run_id, call_id = "run-truth-note-once", "call-note-1"
        text = "Поправка: мой рабочий движок — gpt-5.6-sol."
        channel = agent.ChannelContext(
            chat_id="555000100", room_id="555000100", principal_id="555000100",
            is_dm=True, owner=True, known=True, addressed=False,
        )
        context = run_context.RunContext.create(
            run_id=run_id, kind="task_window", goal="часовой импульс",
            principal_id=str(channel.principal_id), scope=channel.scope,
            origin_chat_id=channel.chat_id, origin_message_ids=(),
            delivery_chat_id=channel.chat_id, model_profile="voice",
        )
        self.manager.create(context, agent._run_context_markdown(
            ctx=channel, kind=context.kind, goal=context.goal,
            conversation="immutable conversation", history=None,
            extra="immutable runtime frame",
        ))
        self.manager.transition(run_id, "running", expected="pending")
        key = f"telegram-outbox:{run_id}:tool:{call_id}"
        self.manager.start_tool(run_id, call_id, "send_message",
                                {"to": "@abstractDL", "text": text},
                                side_effect=True, idempotency_key=key)
        entry = self.outbox.prepare_text(
            key, peer_id=-1001240718803, text=text, run_id=run_id, call_id=call_id,
            purpose="tool:send_message")
        agent.run_direct_outbox_prepared(
            entry, target_label="AbstractDL Chat", target_user_id=None,
            pulse_id="pulse-1", followup_request="")
        entry = self.outbox.mark_accepted(key, message_id=94243)

        self.assertTrue(agent.project_direct_outbox_acceptance(entry))
        self.assertTrue(agent.project_direct_outbox_acceptance(entry))

        scratch = notes.path_for("-1001240718803").read_text(encoding="utf-8")
        self.assertEqual(scratch.count("сказала (голос)"), 1, scratch)
        self.assertEqual(len(self.ledger.list()), 1)


class FollowUpTraceIsHerMemoryNotOwnerMailTests(_DirectSendTraceHarness):
    """След нити ≠ отчёт Егору.

    27.07 02:32 Егору в ЛС уехала его же реплика из AbstractDL под заголовком «AbstractDL
    Chat ответил(а)». На проде: 32 записи леджера, заказано словами 0, писем ему 17 (89%
    его личного ящика от неё), шесть она погасила руками.
    """

    def test_a_plain_direct_send_leaves_a_trace_that_never_becomes_owner_mail(self):
        proof, entry = self._accepted(peer_id=self.GROUP, text=self.SAID,
                                      projection={"target_label": "AbstractDL Chat"})
        runner._project_direct_outbox_acceptance(proof, entry)
        items = self.ledger.list()
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0].get("notify_owner"))
        self.assertEqual(items[0].get("notice_source"), "")
        # По этой строке она через час узнаёт, ЧТО именно уже сказала.
        self.assertEqual(items[0].get("sent_excerpt"), self.SAID)
        self.ledger.observe_incoming(
            peer_id=self.GROUP, sender_id=1240, message_id=94244,
            text="принял", reply_to_message_id=94243, sender_name="Арет")
        self.assertEqual(self.ledger.pending_notifications(), [])

    def test_the_owner_s_own_words_are_what_orders_the_report(self):
        proof, entry = self._accepted(
            peer_id=self.GROUP, text=self.SAID, message_id=94244,
            projection={"target_label": "AbstractDL Chat",
                        "followup_request": "Пракс, поправь там же и сообщи, когда ответят."})
        runner._project_direct_outbox_acceptance(proof, entry)
        item = self.ledger.list()[0]
        self.assertTrue(item.get("notify_owner"))
        self.assertEqual(item.get("notice_source"), "owner")
        self.ledger.observe_incoming(
            peer_id=self.GROUP, sender_id=1240, message_id=94245,
            text="принял", reply_to_message_id=94244, sender_name="Арет")
        self.assertEqual([x["id"] for x in self.ledger.pending_notifications()],
                         [item["id"]])

    def test_her_own_dm_to_the_owner_leaves_no_thread_at_all(self):
        # Егору её слово видно и так — след здесь был бы бухгалтерией ради бухгалтерии.
        proof, entry = self._accepted(peer_id=self.OWNER, text=self.SAID,
                                      message_id=1271)
        runner._project_direct_outbox_acceptance(proof, entry)
        self.assertEqual(self.ledger.list(), [])

    def test_a_ledger_failure_costs_the_trace_loudly_but_not_the_delivery(self):
        proof, entry = self._accepted(peer_id=self.GROUP, text=self.SAID)
        with patch.object(self.ledger, "create", side_effect=OSError("файл занят")):
            with self.assertLogs(runner.log, level="ERROR") as captured:
                result = runner._project_direct_outbox_acceptance(proof, entry)
        self.assertTrue(result.startswith("projected:"), result)
        self.assertTrue(any("след нити не завёлся" in line for line in captured.output),
                        captured.output)


class OwnerReportIsOrderedNotAssumedTests(unittest.TestCase):
    """`explicit_only=False` выключал единственный гейт: любая последняя фраза Егора
    («им отправить», «[Голосовое]») читалась как заказ «доложи мне ответ». Из 11 реальных
    owner-записей прода явную просьбу не содержит НИ ОДНА."""

    OWNER = 555000100

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-order-")
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.outbox = telegram_outbox.TelegramOutbox(self.base / "outbox")
        self._old_outbox = runner._DIRECT_OUTBOX
        runner._DIRECT_OUTBOX = self.outbox
        self.addCleanup(self._restore)
        self.addCleanup(runner._buf.pop, str(self.OWNER), None)

    def _restore(self):
        runner._DIRECT_OUTBOX = self._old_outbox

    def _projection_for(self, *, owner_lines, pulse_id="", active_chat=None,
                        text="Поправка: мой движок — gpt-5.6-sol.",
                        call_id="call-order-1"):
        """Прогнать прямую отправку в группу и вернуть проекцию её расписки."""
        captured: dict = {}
        entity = types.SimpleNamespace(id=1240718803, title="AbstractDL Chat",
                                       megagroup=True)
        runner._buf[str(self.OWNER)] = list(owner_lines)
        execution = {"run_id": "run-order", "call_id": call_id, "tool": "send_message"}
        key = f"telegram-outbox:run-order:tool:{call_id}"
        prepared = self.outbox.prepare_text(
            key, peer_id=-1001240718803, text=text, run_id="run-order",
            call_id=call_id, purpose="tool:send_message")
        accepted = dict(prepared, state="accepted",
                        receipt={"message_id": 94243,
                                 "random_id": prepared["random_id"]})
        calls = {"n": 0}

        def _threadsafe(fn, timeout=None):
            # первый заход — резолв адресата, второй — сама отправка
            calls["n"] += 1
            return entity if calls["n"] == 1 else accepted

        with (
            patch.object(runner, "OWNER_ID", self.OWNER),
            patch.object(runner.agent, "current_tool_execution", return_value=execution),
            patch.object(runner.agent, "_active_chat",
                         return_value=(str(self.OWNER) if active_chat is None
                                       else active_chat)),
            patch.object(runner.agent, "_direct_outbox_intent", return_value=None),
            patch.object(runner.agent, "run_direct_outbox_prepared",
                         side_effect=lambda entry, **kw: captured.update(kw)),
            patch.object(runner.agent, "project_direct_outbox_acceptance",
                         return_value=True),
            patch.object(runner.social_pulse, "active_id", return_value=pulse_id),
            patch.object(runner.social_pulse, "allow_outbound", return_value=(True, "")),
            patch.object(runner, "_threadsafe_result", side_effect=_threadsafe),
        ):
            runner._sync_send_message("-1001240718803", text)
        return captured

    def test_a_plain_owner_line_does_not_order_a_report(self):
        # Живые строки из его буфера на проде: ни одна не проходит wants_followup.
        for line in ("Егор: им отправить", "Егор: [Голосовое]",
                     "Егор: предложи Вике помощь с эксельками, если ей нужно"):
            with self.subTest(line=line):
                projection = self._projection_for(owner_lines=[line])
                self.assertEqual(projection.get("followup_request"), "", line)

    def test_explicit_words_still_order_it(self):
        # Закон 1: способность не отнята — просто у отчёта появился заказчик.
        projection = self._projection_for(
            owner_lines=["Егор: спроси у Арета и сообщи, когда он ответит"])
        self.assertIn("сообщи", projection.get("followup_request", ""))

    def test_her_own_pulse_initiative_no_longer_orders_a_report_for_him(self):
        # 21 запись из 32 родилась здесь: каждая её реплика из пульса становилась письмом.
        projection = self._projection_for(
            owner_lines=["Егор: спроси у Арета и сообщи, когда он ответит"],
            pulse_id="pulse-1", active_chat="")
        self.assertEqual(projection.get("followup_request"), "")

    def test_a_turn_that_is_not_in_his_dm_orders_nothing_either(self):
        projection = self._projection_for(
            owner_lines=["Егор: спроси у Арета и сообщи, когда он ответит"],
            active_chat="-1001240718803")
        self.assertEqual(projection.get("followup_request"), "")


class FollowUpLetterNamesThePersonTests(unittest.IsolatedAsyncioTestCase):
    """27.07: письмо в ЛС Егора называлось «AbstractDL Chat ответил(а)». Чат не отвечает —
    отвечает человек, и имя было под рукой: строкой выше раннер печатает «получен ответ
    #94244 от Yegor Kosyrev»."""

    async def _title_for(self, item) -> str:
        emitted: dict = {}

        def _emit(kind, **kwargs):
            emitted.update(kwargs)
            return {"id": "delivery-1", "status": "queued", "transports": ()}

        with (
            patch.object(runner, "OWNER_ID", 555000100),
            patch.object(runner.telegram_followups.LEDGER, "pending_notifications",
                         return_value=[item]),
            patch.object(runner.telegram_followups.LEDGER, "mark_notified"),
            patch.object(runner.owner_delivery.LEDGER, "emit", side_effect=_emit),
        ):
            await runner._followups_once()
        return str(emitted.get("title") or "")

    def _item(self, **response):
        return {"id": "tgfu_x", "target_label": "AbstractDL Chat (@abstractdl_chat, id 1240718803)",
                "target_user_id": None, "sent_message_id": 94243,
                "response": dict({"peer_id": "-1001240718803", "message_id": 94244,
                                  "text": "принял"}, **response)}

    async def test_a_group_answer_names_the_human_and_the_room(self):
        title = await self._title_for(self._item(sender_name="Арет", sender_id=1240))
        self.assertTrue(title.startswith("Арет ответил(а) в AbstractDL Chat"), title)

    async def test_a_dm_answer_names_only_the_human(self):
        item = dict(self._item(sender_name="Виктория", sender_id=555000333),
                    target_user_id="555000333")
        self.assertEqual(await self._title_for(item), "Виктория ответил(а)")

    async def test_an_old_dm_record_without_a_name_still_names_the_person(self):
        # Записи до 27.07 имени не несут вовсе. В ЛС отвечает ровно адресат, поэтому
        # метка нити там И ЕСТЬ человек — подставить её не ложь.
        item = dict(self._item(sender_id=555000333), target_user_id="555000333",
                    target_label="Вика (@vika_test, id 555000333)")
        self.assertEqual(await self._title_for(item),
                         "Вика (@vika_test, id 555000333) ответил(а)")

    async def test_an_unknown_name_says_the_id_and_never_the_room(self):
        title = await self._title_for(self._item(sender_id=555000333))
        self.assertTrue(title.startswith("id 555000333 ответил(а) в"), title)

    async def test_a_nameless_idless_answer_admits_it_does_not_know(self):
        # Закон 3: «не знаю кто» обязано выглядеть как «не знаю кто», а не как метка чата.
        title = await self._title_for(self._item())
        self.assertTrue(title.startswith("не знаю кто ответил(а)"), title)
        self.assertNotIn("AbstractDL Chat ответил", title)


class SheCanAskForTheReportHerselfTests(unittest.TestCase):
    """Закон 1: автоматизм снят — значит рука обязана остаться. Раньше она могла только
    ГАСИТЬ чужое решение (шесть раз и гасила)."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-watch-")
        self.addCleanup(self._temp.cleanup)
        self.ledger = telegram_followups.FollowUpLedger(
            Path(self._temp.name) / "followups.json")
        for target, attr, value in (
            (runner.telegram_followups, "LEDGER", self.ledger),
            (runner, "_telegram_account_gate", lambda: None),
        ):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.item = self.ledger.create(
            target_ref="-1001240718803", target_label="AbstractDL Chat",
            target_peer_id=-1001240718803, target_user_id=None,
            sent_message_id=94243, request_text="", sent_excerpt="Поправка…",
            idempotency_key="k1")

    def test_watch_turns_a_trace_into_an_ordered_report_and_unwatch_turns_it_back(self):
        answer = runner._sync_followups("watch", self.item["id"])
        self.assertIn("включила", answer)
        self.ledger.observe_incoming(
            peer_id=-1001240718803, sender_id=1240, message_id=94244, text="принял",
            reply_to_message_id=94243, sender_name="Арет")
        self.assertEqual([x["id"] for x in self.ledger.pending_notifications()],
                         [self.item["id"]])
        self.assertIn("выключила", runner._sync_followups("unwatch", self.item["id"]))
        self.assertEqual(self.ledger.pending_notifications(), [])

    def test_an_unknown_thread_is_named_as_such_instead_of_a_silent_ok(self):
        self.assertIn("Не нашла живую нить", runner._sync_followups("watch", "tgfu_нет"))

    def test_the_help_line_names_both_new_hands(self):
        answer = runner._sync_followups("что-то")
        self.assertIn("watch", answer)
        self.assertIn("unwatch", answer)


class IncomingCarriesWhoAnsweredTests(unittest.TestCase):
    """Раннер знает имя и «это сам Егор» — в модуле леджера OWNER_ID неизвестен вовсе."""

    def test_the_incoming_handler_passes_the_name_and_the_owner_flag(self):
        import inspect
        source = inspect.getsource(runner.on_new)
        self.assertIn("sender_name=name", source)
        self.assertIn("sender_is_owner=bool(is_owner)", source)


if __name__ == "__main__":
    unittest.main()
