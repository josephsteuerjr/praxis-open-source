"""Её собственные слова приезжают к ней ЕЁ репликой, а не строкой в чужом тексте.

До 04.08.2026 в живом ходе не было ни одного сообщения роли `assistant`: голос звался
как `_voice(user_content, [], …)`, то есть с пустой историей, а весь разговор — включая
её собственные реплики — уезжал одним текстовым блобом строк «Имя: текст» внутри тега
`<current_user_message>`. Своё и чужое отличалось только префиксом перед двоеточием.

Механизм «я это уже говорила» у модели работает по последовательности ролей. Вся её речь
была вынесена из этой последовательности в данные — и вдобавок подписана третьим лицом.
Данных не не хватало; не хватало ПОЗИЦИИ, из которой они читаются как свои.

Запуск:  python praxis_test.py test_dialogue_roles -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("PRAXIS_TEST", "1")

import agent
import memory_life
import mtproto_runner as runner


class HotRecordsKeepAuthorship(unittest.TestCase):
    """Структурный слой помнит автора; строковое зеркало — уже нет."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_roles_")
        self.addCleanup(self.tmp.cleanup)
        self._state = {"schema": 1, "chat_id": "42", "hot": [], "frontier": [], "dedupe": []}
        self._orig = memory_life._load_state
        memory_life._load_state = lambda chat_id, rebuild=False: self._state
        self.addCleanup(lambda: setattr(memory_life, "_load_state", self._orig))

    def _hot(self, **row) -> None:
        self._state["hot"].append(row)

    def test_direction_is_read_not_guessed(self):
        self._hot(line="Егор: привет", actor="Егор", direction="in", ts=1.0)
        self._hot(line="Praxis: привет", actor="Praxis", direction="out", ts=2.0)
        rows = memory_life.hot_records("42")
        self.assertEqual([r["direction"] for r in rows], ["in", "out"])

    def test_legacy_rows_without_fields_still_resolve(self):
        """Записи, заведённые до появления полей, не должны выпадать из разговора."""
        self._hot(line="Praxis: старая строка", ts=1.0)
        self._hot(line="Егор: и его", ts=2.0)
        rows = memory_life.hot_records("42")
        self.assertEqual([r["direction"] for r in rows], ["out", "in"])

    def test_blank_lines_are_not_turns(self):
        self._hot(line="   ", actor="Егор", direction="in", ts=1.0)
        self._hot(line="Егор: есть", actor="Егор", direction="in", ts=2.0)
        self.assertEqual(len(memory_life.hot_records("42")), 1)


class DialogueSplitsAtHerLastReply(unittest.TestCase):
    """Граница истории — её последняя реплика. Всё после неё — то, на что она отвечает."""

    def setUp(self) -> None:
        self.rows: list[dict] = []
        self._orig = memory_life.hot_records
        memory_life.hot_records = lambda chat_id, limit=None: list(self.rows)
        self.addCleanup(lambda: setattr(memory_life, "hot_records", self._orig))
        self._lever = os.environ.get("PRAXIS_DIALOGUE_ROLES")
        os.environ["PRAXIS_DIALOGUE_ROLES"] = "1"
        self.addCleanup(lambda: os.environ.__setitem__("PRAXIS_DIALOGUE_ROLES", self._lever)
                        if self._lever is not None
                        else os.environ.pop("PRAXIS_DIALOGUE_ROLES", None))

    def _in(self, who: str, text: str) -> None:
        self.rows.append({"actor": who, "direction": "in", "line": f"{who}: {text}",
                          "ts": float(len(self.rows) + 1), "source_id": None})

    def _out(self, text: str) -> None:
        self.rows.append({"actor": "Praxis", "direction": "out", "line": f"Praxis: {text}",
                          "ts": float(len(self.rows) + 1), "source_id": None})

    def test_her_words_arrive_as_her_own_without_a_name_prefix(self):
        self._in("Егор", "привет")
        self._out("привет, я тут")
        self._in("Егор", "что с почтой")
        history, current = runner._dm_dialogue("42")
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertEqual(history[1]["content"], "привет, я тут",
                         "её реплика снова подписана её же именем")
        self.assertEqual(current, "что с почтой")

    def test_his_words_are_his_own_too_or_he_becomes_a_third_person(self):
        """03.08 21:44, живой случай: она ответила Егору, говоря о нём в третьем лице —
        «вы сейчас чините ровно то, от чего Егору стало не по себе». Роль `user` СТАЛА
        собеседником, а текст в ней остался подписан его именем: кадр читался как
        «мне докладывают слова Егора», а не «Егор говорит мне»."""
        self._in("Yegor Kosyrev (@tatarskiy_e4pochmak)", "мы работаем над этим")
        self._out("вижу")
        self._in("Yegor Kosyrev (@tatarskiy_e4pochmak)", "последние коммиты ценные")
        history, current = runner._dm_dialogue("42")
        self.assertEqual(current, "последние коммиты ценные",
                         "его собственная реплика снова подписана его именем")
        self.assertEqual(history[0]["content"], "мы работаем над этим")
        joined = "\n".join(m["content"] for m in history) + current
        self.assertNotIn("Yegor Kosyrev", joined,
                         "имя собеседника осталось внутри его же слов — третье лицо вернётся")

    def test_consecutive_lines_of_one_side_merge_into_one_turn(self):
        self._in("Егор", "первое")
        self._in("Егор", "второе")
        self._out("отвечаю")
        self._in("Егор", "третье")
        history, _current = runner._dm_dialogue("42")
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertIn("первое", history[0]["content"])
        self.assertIn("второе", history[0]["content"])

    def test_the_whole_conversation_survives_the_split(self):
        """Ни одна реплика не имеет права пропасть при разведении ролей."""
        self._in("Егор", "раз")
        self._out("два")
        self._in("Егор", "три")
        self._out("четыре")
        self._in("Егор", "пять")
        history, current = runner._dm_dialogue("42")
        seen = "\n".join([m["content"] for m in history] + [current])
        for word in ("раз", "два", "три", "четыре", "пять"):
            self.assertIn(word, seen, f"«{word}» потерялось при разведении ролей")

    def test_she_has_not_spoken_here_yet(self):
        """Без её реплики истории нет — и ход идёт ровно как раньше."""
        self._in("Егор", "первый раз пишу")
        self.assertEqual(runner._dm_dialogue("42"), ([], ""))

    def test_nothing_new_to_answer(self):
        """Последней говорила она сама: пустое user-сообщение хуже старого пути."""
        self._in("Егор", "привет")
        self._out("привет")
        self.assertEqual(runner._dm_dialogue("42"), ([], ""))

    def test_the_lever_returns_the_old_path(self):
        os.environ["PRAXIS_DIALOGUE_ROLES"] = "0"
        self._in("Егор", "привет")
        self._out("привет")
        self._in("Егор", "ещё")
        self.assertEqual(runner._dm_dialogue("42"), ([], ""))

    def test_a_broken_source_does_not_break_the_turn(self):
        def boom(chat_id, limit=None):
            raise RuntimeError("горячий слой не прочитался")

        memory_life.hot_records = boom
        self.assertEqual(runner._dm_dialogue("42"), ([], ""))


class TheModelSeesRolesAndTheReceiptsSeeEverything(unittest.TestCase):
    """Роли меняют то, что видит модель. Расписки и исходящая граница не задеты."""

    def test_media_prompt_splits_receipt_text_from_model_text(self):
        ctx = agent.ChannelContext(chat_id="42", is_dm=True, owner=True)
        receipt, for_model = agent._media_prompt(
            "Егор: раз\nPraxis: два\nЕгор: три", (), ctx, model_text="Егор: три")
        self.assertIn("Praxis: два", receipt, "расписка обязана помнить весь разговор")
        self.assertEqual(for_model, "Егор: три",
                         "модели уехал весь разговор второй раз — это дубль")

    def test_without_roles_both_texts_stay_identical(self):
        ctx = agent.ChannelContext(chat_id="42", is_dm=True, owner=True)
        receipt, for_model = agent._media_prompt("Егор: раз\nЕгор: два", (), ctx)
        self.assertEqual(receipt, for_model)

    def test_voice_gets_assistant_turns_and_only_the_new_message(self):
        seen: dict = {}

        def capture(*, system, messages, tools, max_iters=None, tool_trace=None):
            seen["messages"] = messages
            return "ок"

        orig = agent._terminal_tool_loop
        agent._terminal_tool_loop = capture
        self.addCleanup(lambda: setattr(agent, "_terminal_tool_loop", orig))

        history = [
            {"role": "user", "content": "Егор: привет"},
            {"role": "assistant", "content": "привет, я тут"},
        ]
        ctx = agent.ChannelContext(chat_id="42", principal_id="42", is_dm=True, owner=True)
        agent._voice_impl("Егор: что с почтой", history, "Егор", ctx=ctx)

        msgs = seen["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"], "привет, я тут")
        tail = msgs[-1]["content"]
        text = tail if isinstance(tail, str) else "".join(
            str(b.get("text", "")) for b in tail if isinstance(b, dict))
        self.assertIn("Егор: что с почтой", text)
        # Тег реплики ПОДПИСАН (author/tg/message из транспортных полей). Здесь предмет
        # другой: что реплика вообще открывается своим тегом, а не тонет в конверте.
        self.assertIn("<current_user_message", text)
        self.assertNotIn("привет, я тут", text,
                         "её реплика приехала И ролью, И текстом — это дубль")


class HerWorkDoesNotDieSilently(unittest.TestCase):
    """Отказ маршрута НАВСЕГДА гасит повтор и остаётся у неё перед глазами.

    03.08: она собрала ревью на 26 КБ и отправила его в AbstractDL. Сопроводительный
    текст ушёл, файл Telegram отверг (`CHAT_SEND_DOCS_FORBIDDEN` — сюда файлы нельзя), и
    насос повторял отправку каждые 30 секунд ТРИНАДЦАТЬ ЧАСОВ: 728 отказов. Классификация
    постоянных отказов существовала с 27.07, но применялась только к тексту — медийный шов
    отдавал наверх строку, а классификатор по построению отвечает False на строку.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_undeliv_")
        self.addCleanup(self.tmp.cleanup)
        self._orig = agent.UNDELIVERED_MEDIA_PATH
        agent.UNDELIVERED_MEDIA_PATH = Path(self.tmp.name) / "undelivered_media.json"
        self.addCleanup(lambda: setattr(agent, "UNDELIVERED_MEDIA_PATH", self._orig))

    def test_a_403_is_permanent_for_media_just_like_for_text(self):
        from telethon.errors.rpcbaseerrors import ForbiddenError

        # Настоящий класс телетона, а не одноимённая заглушка: проверка идёт по
        # __mro__, и локальный класс с тем же именем прошёл бы, ничего не доказав.
        refusal = ForbiddenError(None, "CHAT_SEND_DOCS_FORBIDDEN")
        self.assertTrue(agent._is_permanent_delivery_error(refusal),
                        "семейство 403 перестало считаться постоянным отказом")
        self.assertFalse(agent._is_permanent_delivery_error(TimeoutError("сеть моргнула")),
                         "транзиентный сбой посчитали постоянным — файл потеряется зря")

    def test_the_refused_file_stays_in_front_of_her(self):
        agent.note_undelivered_media(
            run_id="run-x", queue_id="q1", chat_id="-1001240718803",
            path="/app/workspace/media/outbound/o2-services-review.md",
            caption="Собрала цельный review для Claude Code",
            reason="ForbiddenError: CHAT_SEND_DOCS_FORBIDDEN")
        line = agent.undelivered_media_state_line()
        self.assertTrue(line, "недоставленный файл не попал в её кадр")
        self.assertIn("o2-services-review.md", json.dumps(line, ensure_ascii=False))
        self.assertIn("CHAT_SEND_DOCS_FORBIDDEN", json.dumps(line, ensure_ascii=False))

    def test_one_queue_id_leaves_one_trace(self):
        for _ in range(3):
            agent.note_undelivered_media(run_id="run-x", queue_id="q1", path="/f",
                                         reason="ForbiddenError")
        self.assertEqual(len(agent.undelivered_media_state_line()), 1)

    def test_nothing_undelivered_means_no_block_at_all(self):
        self.assertIsNone(agent.undelivered_media_state_line())


class GroupRolesRideOnTopOfTheRender(unittest.TestCase):
    """В группе роли ложатся ПОВЕРХ ленты, не подменяя её.

    Групповой рендер — не косметика: ветка, id сообщения, время, метка правки, адрес
    ответа и пометки обреза несут её адреса в комнате. Подменить его горячим слоем
    значило бы сузить восприятие. Поэтому лента остаётся байт в байт, а роль снимается
    с поля `outgoing`, записанного в момент наблюдения.
    """

    def setUp(self) -> None:
        import group_context
        from unittest.mock import patch

        self.gc = group_context
        self.temp = tempfile.TemporaryDirectory(prefix="praxis_grp_")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        for name, value in (("BASE", base), ("MEM_DIR", base / "memory"),
                            ("GROUPS_DIR", base / "memory" / "groups"),
                            ("STATE_DIR", base / "memory" / ".state")):
            patcher = patch.object(group_context, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _say(self, mid: int, name: str, text: str, *, mine: bool = False,
             reply: int | None = None) -> None:
        self.gc.observe_message(
            peer_id="-1001", topic_id=7, message_id=mid,
            sender_id=(5 if mine else 10), sender_name=name,
            reply_to_message_id=reply, timestamp=f"2026-08-04T12:{mid:02d}:00Z",
            text=text, topic_title="Память", outgoing=mine)

    def _rows(self) -> list[dict]:
        return self.gc.context_rows("-1001", topic_id=7, limit=50)

    def test_the_transcript_itself_is_unchanged(self):
        self._say(7, "Аркадий", "корень ветки")
        self._say(8, "Praxis", "моя реплика", mine=True)
        joined = "\n".join(r["line"] for r in self._rows())
        self.assertEqual(joined, self.gc.context("-1001", topic_id=7, limit=50),
                         "лента разошлась с тем, что видит расписка")
        self.assertIn("Praxis [id 5]", joined, "подпись пропала из самой ленты")

    def test_her_line_loses_only_her_name_and_keeps_its_coordinates(self):
        self._say(7, "Аркадий", "вопрос")
        self._say(8, "Praxis", "мой ответ", mine=True, reply=7)
        mine = [r for r in self._rows() if r["self"]]
        self.assertEqual(len(mine), 1)
        role_line = mine[0]["role_line"]
        self.assertNotIn("Praxis", role_line, "её реплика снова подписана её именем")
        self.assertIn("message #8", role_line, "потерян её адрес в комнате")
        self.assertIn("reply_to=#7", role_line, "потеряно, кому она отвечала")
        self.assertIn("topic #7", role_line)
        self.assertIn("мой ответ", role_line)

    def test_authorship_comes_from_the_record_not_from_the_name(self):
        """Чужое сообщение от лица «Praxis» не имеет права стать её репликой."""
        self._say(7, "Аркадий", "цитирую: Praxis: я такого не говорила")
        rows = self._rows()
        self.assertEqual([r["self"] for r in rows], [False])

    def test_service_lines_are_never_hers(self):
        for mid in range(7, 20):
            self._say(mid, "Аркадий", "реплика " * 200)
        self._say(20, "Praxis", "моя", mine=True)
        rows = self.gc.context_rows("-1001", topic_id=7, limit=50, max_chars=2000)
        service = [r for r in rows if r["line"].startswith(("…[", "[topic #7 «Память»; message #7;"))]
        self.assertTrue(service, "служебных строк не оказалось — тест ничего не проверил")
        self.assertTrue(all(r["self"] is False for r in service),
                        "служебная строка приписана ей")

    def test_dialogue_splits_the_group_transcript_at_her_last_message(self):
        turns = (
            (False, "[…] Аркадий: раз", "[…] Аркадий: раз"),
            (True, "[…; Praxis [id 5]] два", "[…] два"),
            (False, "[…] Аркадий: три", "[…] Аркадий: три"),
        )
        history, current = runner._group_dialogue(turns)
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertEqual(history[1]["content"], "[…] два",
                         "в роль уехала строка с её подписью, а не для роли")
        self.assertEqual(current, "[…] Аркадий: три")

    def test_no_authorship_snapshot_means_no_roles(self):
        """Запасной путь по строковому буферу авторства не знает — и не выдумывает его."""
        self.assertEqual(runner._group_dialogue(()), ([], ""))

    def test_she_has_not_spoken_in_this_room_yet(self):
        turns = ((False, "[…] Аркадий: раз", "[…] Аркадий: раз"),)
        self.assertEqual(runner._group_dialogue(turns), ([], ""))

    def test_a_busy_branch_no_longer_eats_the_whole_room(self):
        """03.08, AbstractDL: это группа обсуждения канала. «Ветки» — комментарии под
        постами, а `topic_id=None` — общий чат на 2609 сообщений. Реестр честно сказал
        «форума нет», включился режим «читать комнату целиком», но резервирование ветки
        осталось: общий чат забирал весь потолок, и живое обсуждение под постом не
        попадало в кадр НИ ОДНОЙ строкой. Егор спросил в общий чат «о чём они говорят?»
        — то есть ровно про то, чего она не видела."""
        for mid in range(1, 41):
            self.gc.observe_message(                      # общий чат: topic_id=None
                peer_id="-1001", topic_id=None, message_id=mid, sender_id=9,
                sender_name="Общий чат", reply_to_message_id=None,
                timestamp=f"2026-08-04T12:{mid:02d}:00Z", text=f"старая болтовня {mid}")
        for mid in range(41, 46):
            self.gc.observe_message(
                peer_id="-1001", topic_id=7, message_id=mid, sender_id=10,
                sender_name="Аркадий", reply_to_message_id=7,
                timestamp=f"2026-08-04T12:{mid:02d}:00Z",
                text=f"живой разговор под постом {mid}", topic_title="пост канала")
        rows = self.gc.context_rows("-1001", topic_id=None, limit=20, max_chars=20000,
                                    whole_room=True)
        seen = "\n".join(r["line"] for r in rows)
        self.assertIn("живой разговор под постом 45", seen,
                      "живое обсуждение снова не попало в кадр")
        self.assertIn("живой разговор под постом 41", seen)

    def test_a_roaring_general_chat_cannot_erase_the_thread_she_answers_in(self):
        """Зеркальная беда. Общий чат группы — под сотню тысяч сообщений и ревёт;
        чистый хронологический хвост оставил бы её без той ветки, в которой она
        отвечает. Её ветке гарантирована треть потолка."""
        for mid in range(1, 6):
            self.gc.observe_message(
                peer_id="-1003", topic_id=7, message_id=mid, sender_id=10,
                sender_name="Собеседник", reply_to_message_id=7,
                timestamp=f"2026-08-04T09:{mid:02d}:00Z",
                text=f"обсуждение под постом {mid}", topic_title="пост канала")
        for mid in range(10, 60):
            self.gc.observe_message(
                peer_id="-1003", topic_id=None, message_id=mid, sender_id=9,
                sender_name="Толпа", reply_to_message_id=None,
                timestamp=f"2026-08-04T12:{mid:02d}:00Z", text=f"шум общего чата {mid}")
        rows = self.gc.context_rows("-1003", topic_id=7, limit=12, max_chars=20000,
                                    whole_room=True)
        seen = "\n".join(r["line"] for r in rows)
        self.assertIn("обсуждение под постом 5", seen,
                      "общий чат вытеснил ветку, в которой она отвечает")
        self.assertIn("шум общего чата 59", seen,
                      "хронология комнаты пропала — вернулись к прежней беде")

    def test_a_real_forum_still_protects_her_branch(self):
        """Резервирование появилось 25.07 против настоящей беды и для ФОРУМА остаётся:
        там ветку провёл Telegram, и соседи не имеют права её вытеснить."""
        for mid in range(1, 31):
            self.gc.observe_message(
                peer_id="-1002", topic_id=99, message_id=mid, sender_id=10,
                sender_name="Сосед", reply_to_message_id=99,
                timestamp=f"2026-08-04T10:{mid:02d}:00Z", text=f"соседняя тема {mid}")
        for mid in range(31, 36):
            self.gc.observe_message(
                peer_id="-1002", topic_id=7, message_id=mid, sender_id=11,
                sender_name="Мой собеседник", reply_to_message_id=7,
                timestamp=f"2026-08-04T09:{mid:02d}:00Z", text=f"моя тема {mid}")
        rows = self.gc.context_rows("-1002", topic_id=7, limit=6, max_chars=20000,
                                    whole_room=False, members=frozenset({7, 99}))
        seen = "\n".join(r["line"] for r in rows)
        self.assertIn("моя тема 35", seen, "соседи вытеснили её собственную ветку")
        self.assertIn("моя тема 31", seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
