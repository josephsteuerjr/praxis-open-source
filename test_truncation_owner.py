"""
Обрыв по max_tokens носит ВЛАДЕЛЬЦА — и чужой обрыв не попадает в её биографию.

Живой дефект (опись 26.07 + десант 27.07): `turns._TRUNCATED` был ОДНИМ словарём на
процесс. Отметку об обрыве клал llm, а забирал первый же читатель — `guard_outbound_reply`.
Значит обрыв судьи приватности (`llm.chat("evaluator")` внутри её же хода), обрыв сжатия
или ночного прохода записывался в кольцо ходов как «ЕЁ ответ оборван потолком»: чужая
жизнь в её памяти. agent.py снимал отметку у судьи руками — это лечило один известный
симптом, а не класс.

Здесь проверяется ровно это: чей обрыв — тот и забирает; незабранный обрыв не исчезает
молча; срок годности отметки не съедает законный длинный ход.

Запуск:  python praxis_test.py test_truncation_owner -v
"""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path

import llm
import turns
from test_llm import Base as LLMBase, FakeAnthropic, FakeAnthResp, FakeStreamOpenAI, _chunk


class TurnsBase(unittest.TestCase):
    """Изоляция turns: файл кольца и слоты обрыва — процесс-глобалы."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_cut_"))
        st = self.tmp / "memory" / ".state"
        self._orig = [(turns, k, getattr(turns, k)) for k in ("BASE", "STATE_DIR", "PATH",
                                                              "ARCHIVE_DIR")]
        turns.BASE, turns.STATE_DIR = self.tmp, st
        turns.PATH = st / "turns.jsonl"
        turns.ARCHIVE_DIR = self.tmp / "memory" / "self"
        turns._reset()
        self.addCleanup(turns._reset)
        self.addCleanup(lambda: [setattr(m, k, v) for m, k, v in self._orig])
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _turn(self, out="её ответ", **fields):
        t = turns.begin(kind="chat", chat_id="777", scope="owner", who="Егор",
                        gist_in="Егор: привет")
        t["out"] = out
        t.update(fields)
        turns.record(t)
        return turns.recent()[-1]


class TestOwnedTruncation(TurnsBase):
    def test_the_judges_cut_is_not_claimed_by_her_turn(self):
        # Судья приватности оборвался потолком. Её собственный ответ — целый.
        turns.note_truncated(model="judge-model", chars=7, owner="evaluator")

        claimed = turns.take_truncation()          # так забирает guard_outbound_reply
        self.assertEqual(claimed, {},
                         "обрыв судьи не должен доставаться её ходу")

        row = self._turn(out="целая фраза")
        self.assertEqual(row.get("why"), "",
                         "причина хода не должна говорить об обрыве её фразы")
        line = turns.format_line(row)
        self.assertNotIn("фраза не закончена", line)

    def test_her_own_cut_still_reaches_her_turn(self):
        # Обратная сторона: перестараться нельзя — её собственный обрыв обязан дойти.
        turns.note_truncated(model="voice-model", chars=512)
        claimed = turns.take_truncation()
        self.assertEqual(claimed.get("model"), "voice-model")
        self.assertEqual(claimed.get("chars"), 512)
        self.assertEqual(turns.take_truncation(), {}, "одноразово: один обрыв — один ход")

    def test_two_cuts_in_one_turn_are_told_apart(self):
        # Настоящий порядок живого хода: сначала обрывается её фраза, потом — судья,
        # который её судит. На одном слоте второй затирал первый, и guard забирал ЧУЖОЙ.
        turns.note_truncated(model="voice-model", chars=512)
        turns.note_truncated(model="judge-model", chars=7, owner="evaluator")

        claimed = turns.take_truncation()
        self.assertEqual(claimed.get("model"), "voice-model",
                         "guard обязан забрать обрыв ЕЁ фразы, а не судьи")

        row = self._turn(out="полуфраза", why="ответ оборван потолком max_tokens")
        line = turns.format_line(row)
        self.assertIn("вспомогательная модель", line,
                      "обрыв судьи — тоже факт хода, он не должен исчезать")
        self.assertIn("не её фраза", line, "и он обязан быть назван чужим")

    def test_an_unclaimed_cut_of_this_turn_is_written_down_not_dropped(self):
        turns.note_truncated(model="judge-model", chars=7, owner="evaluator")
        self._turn(out="целая фраза")
        disk = turns.PATH.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(disk), 1)
        self.assertIn("оборвано потолком max_tokens", disk[0])
        self.assertNotIn("оборвано потолком", turns.format_line(self._turn(out="следующий")),
                         "уже записанный обрыв не должен клеиться ко второму ходу")

    def test_a_cut_nobody_can_claim_surfaces_in_state(self):
        # Фоновая роль (сон, консолидация) зовёт модель в СВОЁМ потоке: записи хода у неё
        # нет, забрать отметку некому. Молча выбросить её нельзя — она едет в STATE.
        worker = threading.Thread(
            target=lambda: turns.note_truncated(model="rem-model", chars=40))
        worker.start()
        worker.join()

        row = self._turn(out="её ход в главном потоке")
        self.assertNotIn("оборвано потолком", turns.format_line(row),
                         "чужой ПОТОК не имеет права дописываться в её ход")

        # Отметка протухает — и именно в этот момент становится фактом, а не мусором.
        self.assertEqual(turns.take_truncation(max_age_sec=0.0), {})
        line = turns.unclaimed_line()
        self.assertIn("без читателя", line)
        self.assertIn("голос", line)
        self.assertIn(line, turns.state_line(),
                      "STATE — единственный канал tier-0 этого модуля; факт обязан дойти")

    def test_a_complete_answer_cancels_the_role_own_stale_mark(self):
        # Настоящий срок годности отметки — «до следующего полного ответа той же роли».
        turns.note_truncated(model="voice-model", chars=512)
        turns.clear_truncation()
        self.assertEqual(turns.take_truncation(), {},
                         "после полного ответа роли прежний обрыв — не про этот ход")
        self.assertIn("без читателя", turns.unclaimed_line(),
                      "снятая отметка тоже факт: роль обрывалась, и этого никто не прочёл")


class TestTruncationAge(TurnsBase):
    """Точка 3 брифа: не съедает ли max_age_sec законный случай."""

    def _age(self, seconds: float, owner: str = "voice") -> None:
        turns.note_truncated(model="voice-model", chars=512, owner=owner)
        key = (threading.get_ident(), owner)
        turns._TRUNCATED[key]["ts"] = time.time() - seconds

    def test_a_long_but_legal_turn_keeps_its_cut(self):
        # Между оборванным ответом модели и guard'ом лежит тул-цикл: ОДИН тул имеет право
        # занять TOOL_CEILING_SEC=600с (agent.py:9658), итераций до 20. Прежние 300с
        # протухали посреди её же хода — и ход записывался как «промолчала сама».
        self._age(900.0)
        self.assertEqual(turns.take_truncation().get("chars"), 512,
                         "15 минут — это законный длинный ход, а не протухшая отметка")

    def test_the_age_backstop_holds_on_both_sides_of_its_edge(self):
        default = 3600.0
        self._age(default - 1.0)
        self.assertEqual(turns.take_truncation().get("chars"), 512, "до границы — забирается")

        self._age(default + 1.0)
        self.assertEqual(turns.take_truncation(), {}, "за границей — не забирается")
        self.assertIn("без читателя", turns.unclaimed_line(),
                      "но и за границей она не исчезает молча")


class TestLLMWritesTheOwner(LLMBase):
    """Сторона писателя: llm кладёт отметку от имени роли, а пинг — вообще не кладёт."""

    def setUp(self):
        super().setUp()
        st = self.tmp / "memory" / ".state"
        self._t_orig = [(turns, k, getattr(turns, k)) for k in ("BASE", "STATE_DIR", "PATH",
                                                                "ARCHIVE_DIR")]
        turns.BASE, turns.STATE_DIR = self.tmp, st
        turns.PATH = st / "turns.jsonl"
        turns.ARCHIVE_DIR = self.tmp / "memory" / "self"
        turns._reset()
        self.addCleanup(turns._reset)
        self.addCleanup(lambda: [setattr(m, k, v) for m, k, v in self._t_orig])

    @staticmethod
    def _cut_stream(text="PRIVACY_"):
        """Живой прод-путь: relay/openai отдаёт SSE, finish_reason='length' = обрыв."""
        return FakeStreamOpenAI([_chunk(content=text), _chunk(finish="length")])

    def test_the_judges_cut_on_the_openai_path_never_becomes_hers(self):
        # Тот самый живой случай: судья приватности зовётся ВНУТРИ её хода, в её потоке,
        # и его обрыв на едином слоте доставался первому читателю — её записи.
        self._write_cfg(evaluator={"framework": "openai", "model": "gpt-x"})
        llm.use_test_client(self._cut_stream(), "openai")
        llm.chat("evaluator", messages=[{"role": "user", "content": "судить"}])

        self.assertEqual(turns.take_truncation(), {},
                         "её ход не должен получить обрыв судьи")
        self.assertEqual(turns.take_truncation(owner="evaluator").get("chars"), 8,
                         "а сам судья — должен, это его факт")

    def test_a_cut_on_the_anthropic_path_is_named_at_all(self):
        # Отметка ставилась внутри _call_openai — значит anthropic-путь (боевой канал
        # z.ai) обрыва не замечал вообще.
        self._write_cfg()
        llm.use_test_client(FakeAnthropic([FakeAnthResp(text="полу", stop="max_tokens")]))
        llm.chat("voice", messages=[{"role": "user", "content": "привет"}])
        self.assertEqual(turns.take_truncation().get("chars"), 4)

    def test_a_full_answer_clears_the_previous_cut_of_the_same_role(self):
        self._write_cfg(voice={"framework": "openai", "model": "gpt-x"})
        llm.use_test_client(self._cut_stream(text="полу"), "openai")
        llm.chat("voice", messages=[{"role": "user", "content": "раз"}])
        llm.use_test_client(
            FakeStreamOpenAI([_chunk(content="целая фраза"), _chunk(finish="stop")]), "openai")
        llm.chat("voice", messages=[{"role": "user", "content": "два"}])
        self.assertEqual(turns.take_truncation(), {},
                         "второй, целый ответ роли отменяет прежнюю отметку")

    def test_a_channel_ping_does_not_mark_her_next_turn_as_cut(self):
        # ping() ходит к модели мимо chat() с max_tokens=1 — то есть КАЖДАЯ проверка
        # канала (brain.py:208) кончалась stop_reason='max_tokens'.
        self._write_cfg(voice={"framework": "openai", "model": "gpt-x"})
        llm.use_test_client(self._cut_stream(text="о"), "openai")

        ok, err = llm.ping("voice")
        self.assertTrue(ok, err)
        self.assertEqual(turns.take_truncation(), {},
                         "пинг на 1 токен — не оборванная фраза и не биография")


if __name__ == "__main__":
    unittest.main()
