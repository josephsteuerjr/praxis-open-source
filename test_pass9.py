"""
Тесты PASS 9 — 9.0 непрерывность приёма (buf_meta, boot-sweep кандидаты, [missed]-метка,
трекер неотвеченных ЛС) и 9.1 cost-meter (usage.json, ротация, STATE-строка, панель).
Герметичны (харнесс test_perceive.Base, модель — фейк).

Запуск:  python praxis_test.py test_pass9 -v
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import types
import unittest

import bufstore
import unanswered
import agent

from test_perceive import Base, FakeClient  # noqa: F401  (герметичный харнесс)
import llm


class BufBase(Base):
    """Base + bufstore/unanswered/llm-usage в том же tmp-дереве."""

    def setUp(self):
        super().setUp()
        mem = self.tmp / "memory"
        patch = {
            bufstore: dict(BASE=self.tmp, BUF_DIR=mem / ".buffers",
                           STATE_DIR=mem / ".state", META_PATH=mem / ".state" / "buf_meta.json"),
            unanswered: dict(BASE=self.tmp, STATE_DIR=mem / ".state",
                             PATH=mem / ".state" / "unanswered.json"),
            llm: dict(USAGE_PATH=mem / ".state" / "usage.json"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)


# --------------------------------------------------------------------------- #
#  buf_meta: время/автор последней строки буфера
# --------------------------------------------------------------------------- #

class TestBufMeta(BufBase):
    def test_update_and_load(self):
        bufstore.meta_update("777", author="Евгений", is_dm=True, name="Евгений", ts=1000.0)
        m = bufstore.meta_load()
        self.assertEqual(m["777"]["last_author"], "Евгений")
        self.assertEqual(m["777"]["last_ts"], 1000.0)
        self.assertTrue(m["777"]["is_dm"])
        self.assertEqual(m["777"]["name"], "Евгений")

    def test_update_keeps_known_fields(self):
        bufstore.meta_update("777", author="Евгений", is_dm=True, name="Евгений")
        bufstore.meta_update("777", author="Praxis")  # ответ: is_dm/name не передаются
        m = bufstore.meta_load()["777"]
        self.assertEqual(m["last_author"], "Praxis")
        self.assertTrue(m["is_dm"], "is_dm должен пережить апдейт без аргумента")
        self.assertEqual(m["name"], "Евгений")

    def test_atomic_no_tmp_left_and_corrupt_starts_fresh(self):
        bufstore.meta_update("1", author="x")
        self.assertFalse(bufstore.META_PATH.with_suffix(".json.tmp").exists(),
                         "tmp-файл должен замениться атомарно")
        bufstore.META_PATH.write_text("{битый json", encoding="utf-8")
        self.assertEqual(bufstore.meta_load(), {}, "битый файл — честный старт с нуля")
        bufstore.meta_update("2", author="y")  # запись поверх битого не падает
        self.assertIn("2", bufstore.meta_load())

    def test_dm_fallback_by_id_sign(self):
        bufstore.meta_update("-100500", author="кто-то")
        self.assertFalse(bufstore.meta_load()["-100500"]["is_dm"],
                         "у групп id отрицательный — фолбэк должен сказать «не DM»")


# --------------------------------------------------------------------------- #
#  boot-sweep: кого догонять после рестарта
# --------------------------------------------------------------------------- #

class TestMissedCandidates(BufBase):
    def _mk(self, cid, lines, ts=None, is_dm=None, name=""):
        bufstore.save(cid, lines)
        bufstore.meta_update(cid, author=lines[-1].split(":", 1)[0] if lines else "",
                             is_dm=is_dm, name=name, ts=ts)

    def test_finds_fresh_unanswered_dm(self):
        self._mk("777", ["Евгений: ты тут?"], ts=time.time() - 3600, is_dm=True, name="Евгений")
        out = bufstore.missed_dm_candidates(48.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["chat_id"], "777")
        self.assertEqual(out[0]["name"], "Евгений")
        self.assertAlmostEqual(out[0]["age_hours"], 1.0, delta=0.1)

    def test_praxis_last_line_not_candidate(self):
        self._mk("777", ["Евгений: ты тут?", "Praxis: тут"], ts=time.time(), is_dm=True)
        self.assertEqual(bufstore.missed_dm_candidates(48.0), [],
                         "последняя строка её — отвечено, догонять нечего")

    def test_group_not_candidate(self):
        self._mk("-100", ["вася: пракс, привет"], ts=time.time(), is_dm=False)
        self.assertEqual(bufstore.missed_dm_candidates(48.0), [], "sweep — только DM")

    def test_stale_not_candidate(self):
        self._mk("777", ["Евгений: ты тут?"], ts=time.time() - 49 * 3600, is_dm=True)
        self.assertEqual(bufstore.missed_dm_candidates(48.0), [],
                         "старше порога — поезд ушёл, человека не дёргаем")

    def test_no_meta_falls_back_to_mtime(self):
        bufstore.save("888", ["Аня: привет"])  # меты нет (старый деплой) — mtime свежий
        out = bufstore.missed_dm_candidates(48.0)
        self.assertEqual([c["chat_id"] for c in out], ["888"])

    def test_empty_buffer_ignored(self):
        bufstore.save("999", [])
        self.assertEqual(bufstore.missed_dm_candidates(48.0), [])


# --------------------------------------------------------------------------- #
#  [missed]-метка в presence-фрейме
# --------------------------------------------------------------------------- #

class TestMissedFrame(BufBase):
    def test_frame_carries_missed_line(self):
        ctx = agent.ChannelContext(chat_id="777", is_dm=True, owner=False, known=True,
                                   missed_hours=3.2)
        frame = agent._presence_frame(ctx)
        self.assertIn("[missed]", frame)
        self.assertIn("3 h ago", frame)
        self.assertIn("while you were offline", frame)
        self.assertIn("don't pretend you were here", frame)

    def test_frame_without_missed_is_clean(self):
        ctx = agent.ChannelContext(chat_id="777", is_dm=True)
        self.assertNotIn("[missed]", agent._presence_frame(ctx))

    def test_sub_hour_wording(self):
        ctx = agent.ChannelContext(chat_id="777", is_dm=True, missed_hours=0.4)
        self.assertIn("less than an h ago", agent._presence_frame(ctx))

    def test_voice_sees_missed_line(self):
        self._client("отвечаю")
        ctx = agent.ChannelContext(chat_id="777", is_dm=True, owner=True, missed_hours=5.0)
        agent.voice_turn("777", "Евгений: ты тут?", speaker="Евгений", ctx=ctx)
        sysm = self.fc.last["system"]
        text = "".join(b["text"] for b in sysm) if isinstance(sysm, list) else sysm
        self.assertIn("[missed]", text, "голос должен видеть честную метку о даунтайме")


# --------------------------------------------------------------------------- #
#  Трекер неотвеченных ЛС
# --------------------------------------------------------------------------- #

class TestUnanswered(BufBase):
    def test_add_entry_and_state_line(self):
        unanswered.note_incoming("777", "Евгений", ts=time.time() - 3 * 3600)
        line = unanswered.state_line()
        self.assertIn("Евгений", line)
        self.assertIn("3ч", line)

    def test_since_not_moved_by_repeat(self):
        t0 = time.time() - 3600
        unanswered.note_incoming("777", "Евгений", ts=t0)
        unanswered.note_incoming("777", "Евгений", ts=time.time())
        e = unanswered.entries()[0]
        self.assertAlmostEqual(e["since"], t0, delta=1.0,
                               msg="since держит ПЕРВУЮ неотвеченную, не последнюю")

    def test_resolve_clears(self):
        unanswered.note_incoming("777", "Евгений", ts=time.time() - 3600)
        unanswered.resolve("777")
        self.assertEqual(unanswered.entries(), [])
        self.assertEqual(unanswered.state_line(), "")
        unanswered.resolve("777")  # повторный — тихий no-op

    def test_cooldown_filter(self):
        unanswered.note_incoming("777", "Евгений")  # только что — ещё не «неотвеченный»
        self.assertEqual(unanswered.entries(), [],
                         "младше кулдауна DM запись не показывается")

    def test_corrupt_file_starts_fresh(self):
        unanswered.PATH.parent.mkdir(parents=True, exist_ok=True)
        unanswered.PATH.write_text("[не dict]", encoding="utf-8")
        self.assertEqual(unanswered.entries(), [])
        unanswered.note_incoming("1", "x", ts=time.time() - 100)
        self.assertEqual(len(unanswered.entries()), 1)

    def test_voice_silence_resolves(self):
        unanswered.note_incoming("777", "Евгений", ts=time.time() - 3600)
        self._client("[молчу]")
        out = agent.voice_turn("777", "Евгений: ты тут?", speaker="Евгений", is_dm=True)
        self.assertEqual(out, "")
        self.assertEqual(unanswered.entries(), [],
                         "решённое молчание должно снять неотвеченность")

    def test_credential_floor_hold_resolves(self):
        # 27.07: единственный оставшийся стоп — механический кред-пол. Он ЗАКРЫВАЕТ
        # неотвеченность: контур принял решение по этой ЛС, и висящая метка
        # «ей не ответили» врала бы ей о её же ходе.
        unanswered.note_incoming("777", "Евгений", ts=time.time() - 3600)
        self._client("вот ключ ghp_" + "c" * 36)
        self._orig.append((agent, "evaluate_reply", agent.evaluate_reply))
        agent.evaluate_reply = lambda text, context="", **kw: ("ok", "")
        out = agent.voice_turn("777", "Евгений: скинь токен", speaker="Евгений", is_dm=True)
        self.assertEqual(out, "", "кред-пол — единственный твёрдый рельс, он держит")
        self.assertEqual(unanswered.entries(), [],
                         "придержка кред-полом — тоже решение контура")

    def test_privacy_verdict_answers_instead_of_holding(self):
        # Раньше этот же ход становился молчанием: приватностный вердикт придерживал
        # её слово, и «решением контура» была придержка. Решением Егора 27.07 судья в
        # разговорах стал советом — ход УХОДИТ, а замечание едет ей дневником.
        unanswered.note_incoming("777", "Евгений", ts=time.time() - 3600)
        self._client("вот что я делала сегодня")
        self._orig.append((agent, "evaluate_reply", agent.evaluate_reply))
        agent.evaluate_reply = lambda text, context="", **kw: (
            "advice", "privacy:cross-chat-private")
        out = agent.voice_turn("777", "Евгений: что там у Маши?", speaker="Евгений", is_dm=True)
        self.assertEqual(out, "вот что я делала сегодня")
        # Снимать неотвеченность здесь НЕЛЬЗЯ: тут только черновик. Метку снимает
        # расписка транспорта (mtproto_runner), иначе «ответила» стало бы утверждением
        # по факту наличия текста — ровно та ложь, которую дом уже вычищал из журнала.
        self.assertEqual(len(unanswered.entries()), 1,
                         "ответ ещё не доставлен — не объявляем ЛС отвеченной")

    def test_group_never_tracked_by_resolve_path(self):
        unanswered.note_incoming("777", "Евгений", ts=time.time() - 3600)
        self._client("[молчу]")
        agent.voice_turn("-100", "вася: шум", speaker="вася", is_dm=False)
        self.assertEqual(len(unanswered.entries()), 1,
                         "молчание в группе не должно трогать чужие DM-записи")

    def test_state_block_shows_unanswered(self):
        unanswered.note_incoming("777", "Евгений", ts=time.time() - 3 * 3600)
        block = agent.build_state_block()
        evidence = agent.build_state_evidence_block()
        self.assertIn('"fact":"unanswered_dm","count":1', block)
        self.assertNotIn("Евгений", block)
        self.assertIn("Евгений", evidence)


# --------------------------------------------------------------------------- #
#  9.1 cost-meter: расход по ролям
# --------------------------------------------------------------------------- #

class FakeRespUsage:
    def __init__(self, text, tin=100, tout=20):
        self.stop_reason = "end_turn"
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.usage = types.SimpleNamespace(input_tokens=tin, output_tokens=tout)


class FakeClientUsage:
    def __init__(self, reply="ок", tin=100, tout=20):
        outer = self

        class _M:
            def create(_s, **kw):
                return FakeRespUsage(reply, outer.tin, outer.tout)
        self.tin, self.tout = tin, tout
        self.messages = _M()


class TestUsageMeter(BufBase):
    def _today(self):
        return _dt.date.today().isoformat()

    def test_chat_accumulates(self):
        llm.use_test_client(FakeClientUsage(tin=100, tout=20))
        llm.chat("voice", messages=[{"role": "user", "content": "x"}])
        llm.chat("voice", messages=[{"role": "user", "content": "y"}])
        llm.chat("evaluator", messages=[{"role": "user", "content": "z"}])
        day = llm._usage_load()[self._today()]
        base = {k: day["voice"][k] for k in ("in", "out", "calls", "fallback")}
        self.assertEqual(base, {"in": 200, "out": 40, "calls": 2, "fallback": 0})
        self.assertEqual(day["evaluator"]["calls"], 1)
        # PASS 18.5: по-модельный подразрез дня — те же вызовы, по фактической модели
        models = day["voice"].get("models") or {}
        self.assertEqual(sum(m["calls"] for m in models.values()), 2)
        self.assertEqual(sum(m["in"] for m in models.values()), 200)

    def test_rotation_drops_old_days(self):
        old = (_dt.date.today() - _dt.timedelta(days=llm.USAGE_KEEP_DAYS + 5)).isoformat()
        keep = (_dt.date.today() - _dt.timedelta(days=3)).isoformat()
        llm.USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        llm.USAGE_PATH.write_text(json.dumps({
            old: {"voice": {"in": 1, "out": 1, "calls": 1, "fallback": 0}},
            keep: {"voice": {"in": 2, "out": 2, "calls": 2, "fallback": 0}}}), encoding="utf-8")
        llm._usage_add("voice", {"in": 10, "out": 5})
        data = llm._usage_load()
        self.assertNotIn(old, data, "старше 60 дней должно ротироваться")
        self.assertIn(keep, data)
        self.assertEqual(data[self._today()]["voice"]["in"], 10)

    def test_corrupt_file_starts_fresh_and_never_raises(self):
        llm.USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        llm.USAGE_PATH.write_text("{битый", encoding="utf-8")
        llm._usage_add("voice", {"in": 7, "out": 3})  # не должен упасть
        self.assertEqual(llm._usage_load()[self._today()]["voice"]["in"], 7)

    def test_write_error_does_not_break_call(self):
        llm.use_test_client(FakeClientUsage())
        self._orig.append((llm, "_usage_load", llm._usage_load))
        llm._usage_load = lambda: (_ for _ in ()).throw(OSError("диск умер"))
        out = llm.chat("voice", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(out.text, "ок", "сбой cost-meter не должен ронять вызов модели")

    def test_fallback_counted(self):
        llm._usage_add("voice", {"in": 1, "out": 1}, fallback=True)
        llm._usage_add("voice", {"in": 1, "out": 1})
        d = llm._usage_load()[self._today()]["voice"]
        self.assertEqual((d["calls"], d["fallback"]), (2, 1))

    def test_usage_line_for_state(self):
        llm._usage_add("voice", {"in": 12300, "out": 4100})
        llm._usage_add("evaluator", {"in": 500, "out": 80})
        line = llm.usage_line()
        self.assertIn("голос", line)
        self.assertIn("12.3к→4.1к", line)
        self.assertIn("вспомогательная", line)
        block = agent.build_state_block()
        self.assertIn('"fact":"usage_today"', block)
        self.assertIn('"tokens_in":12300', block)

    def test_usage_line_empty_when_silent(self):
        self.assertEqual(llm.usage_line(), "")

    def test_usage_persists_cache_metrics(self):
        """cache_read/cache_creation накапливаются в daily usage и видны в usage_line."""
        llm._usage_add("voice", {"in": 1000, "out": 100, "cache_read": 800, "cache_creation": 200},
                        model="glm-5.2")
        data = llm._usage_load()
        day = data.get(self._today(), {})
        v = day.get("voice", {})
        self.assertEqual(v.get("cache_read"), 800)
        self.assertEqual(v.get("cache_creation"), 200)
        # model breakdown тоже хранит
        self.assertEqual(v.get("models", {}).get("glm-5.2", {}).get("cache_read"), 800)
        # 02.08: прибор показывает не сырую пару r/c, а то, что действительно означает
        # расход — долю кэша и СВЕЖИЙ вход (`in − cache_read`). Кэшированный префикс
        # провайдер уже держит; платится за остальное. Контракт этого теста — «метрики
        # видны в usage_line» — прежний, проверяется по смыслу, а не по слову «cache».
        line = llm.usage_line()
        self.assertIn("кэш 80%", line)
        self.assertIn("свежего входа 200", line)
        self.assertIn("записи в кэш 200", line)

    def test_usage_no_cache_metrics_when_absent(self):
        """Без cache-метрик usage_line молчит про кэш.

        Молчание тут значит «провайдер не сказал», а не «кэша нет» — путать эти два
        факта дороже, чем не показать число (02.08: реле теряло поле, и нули читались
        как «кэш не работает», хотя апстрим кэшировал на 85%).
        """
        llm._usage_add("evaluator", {"in": 500, "out": 50})
        line = llm.usage_line()
        self.assertNotIn("кэш", line)
        self.assertNotIn("свежего входа", line)

    def test_usage_days_window(self):
        old = (_dt.date.today() - _dt.timedelta(days=10)).isoformat()
        llm.USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        llm.USAGE_PATH.write_text(json.dumps({
            old: {"voice": {"in": 1, "out": 1, "calls": 1, "fallback": 0}}}), encoding="utf-8")
        llm._usage_add("voice", {"in": 2, "out": 2})
        week = llm.usage_days(7)
        self.assertNotIn(old, week, "окно панели — 7 дней")
        self.assertIn(self._today(), week)

    def test_pricing_preserved_and_money_only_when_priced(self):
        import panel
        # без прайса — только токены
        llm._usage_add("voice", {"in": 1_000_000, "out": 100_000})
        u = panel.llm_usage()
        self.assertFalse(u["priced"])
        self.assertEqual(u["cost"], {})
        # прайс задан руками в llm.json — деньги считаются и переживают update_config
        cfg = json.loads(json.dumps(llm._config()))
        model = cfg["roles"]["voice"]["model"]
        cfg["pricing"] = {model: {"in_per_1m": 1.0, "out_per_1m": 2.0}}
        llm.save_config(cfg)
        llm._CACHE.update(mtime=None, cfg=None)  # заставить перечитать
        llm.update_config({"limits": {"max_tool_iters": 21}})  # панельная запись не стирает прайс
        self.assertEqual(llm.pricing().get(model), {"in_per_1m": 1.0, "out_per_1m": 2.0})
        u = panel.llm_usage()
        self.assertTrue(u["priced"])
        today = _dt.date.today().isoformat()
        self.assertAlmostEqual(u["cost"][today]["voice"], 1.2, places=3)


# --------------------------------------------------------------------------- #
#  9.2 иммунитет: ревью её собственных правок
# --------------------------------------------------------------------------- #

import shutil

import _standenv
import subprocess
import tempfile
from pathlib import Path

import immune
import selfdev


def _sh(cwd, *args):
    return subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, timeout=30)


def _mk_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="immune_repo_"))
    _sh(d, "git", "init", "-q", "-b", "master")
    _sh(d, "git", "config", "user.name", "t")
    _sh(d, "git", "config", "user.email", "t@t")
    (d / ".gitignore").write_text(".env\n", encoding="utf-8")  # как в живом: decoy .env не трекается
    (d / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (d / "soul").mkdir()
    (d / "soul" / "note.md").write_text("# n\n", encoding="utf-8")
    (d / "test_smoke.py").write_text(
        "import unittest\nimport core\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_v(self):\n"
        "        self.assertIn(core.VALUE, (1, 2))\n", encoding="utf-8")
    # Та же дверь, что у живого репо; список её файлов читается из самой двери.
    # 03.08.2026 здесь был свой рукописный список — вторая копия одной и той же
    # логики, и отстала она вместе с первой.
    _standenv.copy_door(Path(__file__).resolve().parent, d)
    _sh(d, "git", "add", "-A")
    _sh(d, "git", "commit", "-q", "-m", "init")
    return d


class ImmuneBase(unittest.TestCase):
    """Свой tmp-репозиторий + пути иммунитета в нём (журнал/очередь/карточки изолированы)."""

    def setUp(self):
        self.repo = _mk_repo()
        self._orig = []
        mem = self.repo / "memory"
        patch = {
            immune: dict(BASE=self.repo, MEM_DIR=mem, STATE_DIR=mem / ".state",
                         QUEUE_PATH=mem / ".state" / "immune_queue.json",
                         CARDS_PATH=mem / ".state" / "immune_cards.json",
                         JOURNAL_DIR=mem / "journal"),
            selfdev: dict(REPO=self.repo),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        selfdev.LEDGER.unlink(missing_ok=True)
        selfdev.POLICY_FILE.unlink(missing_ok=True)
        selfdev.clear_restart_request()
        import llm
        llm.clear_test_clients()
        self.addCleanup(llm.clear_test_clients)

    def tearDown(self):
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        shutil.rmtree(self.repo, ignore_errors=True)

    def _stub_review(self, verdict, why=""):
        self._orig.append((immune, "review", immune.review))
        calls = []
        immune.review = lambda *a, **kw: (calls.append((a, kw)) or (verdict, why))
        return calls

    def _journal_text(self) -> str:
        out = ""
        jd = immune.JOURNAL_DIR
        if jd.exists():
            for f in sorted(jd.glob("*.md")):
                out += f.read_text(encoding="utf-8")
        return out


class TestImmuneReview(ImmuneBase):
    def test_big_diff_is_visible_advice_without_model(self):
        diff = "\n".join(f"+ line {i}" for i in range(immune.MAX_DIFF + 10))
        verdict, why = immune.review(diff, message="огромная правка")
        self.assertEqual(verdict, "warn")
        self.assertIn("крупнее окна рецензии", why)

    def test_empty_diff_ok(self):
        self.assertEqual(immune.review("")[0], "ok")

    def test_no_evaluator_is_honest_warn(self):
        verdict, why = immune.review("+ x = 1\n", message="мелочь")
        self.assertEqual(verdict, "warn", "нет канала — warn со следом, не слепой ok/red")
        self.assertIn("не смог", why)

    def test_model_verdicts_parse(self):
        import llm
        from test_perceive import FakeClient
        for reply, want in (("ok", "ok"), ("warn: вкусовщина", "warn"),
                            ("red: противоречит «Кем я не стану»", "red"),
                            ("что-то невнятное", "warn")):
            llm.use_test_client(FakeClient(reply))
            verdict, _ = immune.review("+ x = 1\n", message="правка")
            self.assertEqual(verdict, want, f"ответ {reply!r} должен дать {want}")

    def test_second_opinion_flag_default_off(self):
        self.assertFalse(immune.second_opinion_enabled(),
                         "second_opinion — задел, по умолчанию ВЫКЛ")


# PASS 16.4: submit требует её ревью — живая строка для тестов иммунитета
RV_16_4 = "прочитала дифф: меняется ровно заявленное, рисков не вижу, тесты держат"


class TestImmuneAutoZone(ImmuneBase):
    def _proposal(self, rel="soul/note.md", content="# n\nновая строка\n", title="заметка"):
        r = selfdev.begin("хочу лучше")
        self.assertTrue(r["ok"], r)
        (Path(r["path"]) / rel).write_text(content, encoding="utf-8")
        return r["id"], title

    def test_red_is_recorded_advice_but_does_not_take_her_decision(self):
        pid, title = self._proposal()
        self._stub_review("red", "противоречит инвариантам")
        msg = selfdev.submit(pid, title, review=RV_16_4)
        t = selfdev.get(pid)
        self.assertEqual(t["status"], "merged")
        self.assertEqual(t["zone"], "auto")
        self.assertFalse(t.get("notified"), "owner receipt должен уйти постфактум")
        self.assertEqual(t["immune"]["verdict"], "red")
        self.assertIn("новая строка", (self.repo / "soul" / "note.md").read_text(encoding="utf-8"))

    def test_ok_merges(self):
        pid, title = self._proposal()
        self._stub_review("ok")
        selfdev.submit(pid, title, review=RV_16_4)
        t = selfdev.get(pid)
        self.assertEqual(t["status"], "merged")
        self.assertEqual(t["immune"]["verdict"], "ok")

    def test_warn_merges_and_journals(self):
        pid, title = self._proposal()
        self._stub_review("warn", "вкусовщина")
        selfdev.submit(pid, title, review=RV_16_4)
        self.assertEqual(selfdev.get(pid)["status"], "merged", "warn не блокирует")
        # журнал selfdev пишет в песочницу PRAXIS_BASE — проверяем через леджер
        self.assertEqual(selfdev.get(pid)["immune"], {"verdict": "warn", "why": "вкусовщина"})

    def test_egor_apply_never_reviews(self):
        pid, title = self._proposal(rel="core.py", content="VALUE = 99\n")
        calls = self._stub_review("red", "не должно быть позвано")
        selfdev.submit(pid, title, review=RV_16_4)  # red tests: self-merge awaits explicit override
        self.assertEqual(calls, [], "красные тесты не доходят до merge review без override")
        res = selfdev.apply(pid, by="egor")
        self.assertTrue(res["ok"], res)
        self.assertEqual(calls, [], "мёрж руками Егора иммунитет не ревьюит")


class TestImmuneQueue(ImmuneBase):
    def _commit(self, author, msg, content):
        (self.repo / "soul" / "note.md").write_text(content, encoding="utf-8")
        _sh(self.repo, "git", "add", "-A")
        _sh(self.repo, "git", "-c", f"user.name={author}", "-c", "user.email=p@l",
            "commit", "-q", "-m", msg)
        return _sh(self.repo, "git", "rev-parse", "HEAD").stdout.strip()

    def test_live_selfcommit_reviewed_in_arrears(self):
        sha = self._commit("Praxis", "self-edit: soul/note.md", "# n\nживая правка\n")
        immune.enqueue(sha, "self-edit: soul/note.md")
        self._stub_review("warn", "спорно")
        n = immune.process_queue()
        self.assertEqual(n, 1)
        j = self._journal_text()
        self.assertIn("[иммунитет]", j)
        self.assertIn("warn", j)
        self.assertEqual(immune._load_json(immune.QUEUE_PATH, []), [], "очередь разобрана")
        self.assertEqual(immune.pending_cards(), [], "warn не рождает карточку")

    def test_red_live_commit_makes_card_no_rollback(self):
        sha = self._commit("Praxis", "write_skill: bad", "# n\nплохая правка\n")
        immune.enqueue(sha, "write_skill: bad")
        self._stub_review("red", "no_performance_layer-класс")
        immune.process_queue()
        cards = immune.pending_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("плохая правка", cards[0]["diff"])
        self.assertIn("no_performance_layer", cards[0]["why"])
        # отката НЕТ: HEAD остался её коммитом
        head = _sh(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(head, sha)
        immune.mark_card_sent(cards[0]["id"])
        self.assertEqual(immune.pending_cards(), [])

    def test_foreign_commits_ignored(self):
        sha = self._commit("Yegor", "ручная правка Егора", "# n\nего правка\n")
        immune.enqueue(sha, "ручная правка Егора")
        calls = self._stub_review("red", "не должно быть позвано")
        immune.process_queue()
        self.assertEqual(calls, [], "чужой автор — ревью не зовётся (иммунитет про её руки)")
        self.assertNotIn("[иммунитет]", self._journal_text())

    def test_enqueue_dedupes_and_none_safe(self):
        immune.enqueue(None, "мимо")
        immune.enqueue("abc123", "x")
        immune.enqueue("abc123", "x")
        self.assertEqual(len(immune._load_json(immune.QUEUE_PATH, [])), 1)


# --------------------------------------------------------------------------- #
#  9.3 inbox: файлы от Егора в мастерскую
# --------------------------------------------------------------------------- #

import asyncio
import os as _os

import workshop

# Раннер строит TelegramClient на импорте: безвредные креды + сессия во временном каталоге
_os.environ.setdefault("TELEGRAM_API_ID", "1")
_os.environ.setdefault("TELEGRAM_API_HASH", "test")
_os.environ.setdefault("TELEGRAM_SESSION",
                       str(Path(tempfile.gettempdir()) / "praxis_pass9_test_session"))

try:
    import mtproto_runner as _mr
    _RUNNER = True
except Exception:
    _mr = None
    _RUNNER = False


class InboxBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_inbox_"))
        self._orig = [(workshop, "BASE", workshop.BASE),
                      (workshop, "INBOX", workshop.INBOX)]
        workshop.BASE = self.tmp
        workshop.INBOX = self.tmp / "workspace" / "inbox"

    def tearDown(self):
        for m, k, v in reversed(self._orig):
            setattr(m, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestInboxAccept(InboxBase):
    def test_sanitize_strips_paths_and_junk(self):
        self.assertEqual(workshop.inbox_safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(workshop.inbox_safe_name("..\\..\\evil.exe"), "evil.exe")
        self.assertEqual(workshop.inbox_safe_name("отчёт за июль!.pdf"), "отчёт_за_июль_.pdf")
        self.assertEqual(workshop.inbox_safe_name(""), "file")
        self.assertLessEqual(len(workshop.inbox_safe_name("x" * 300)), 80)

    def test_accept_names_by_date(self):
        path, why = workshop.inbox_accept("отчёт.pdf", 1024, day="20260703")
        self.assertEqual(why, "")
        self.assertEqual(path.name, "20260703_отчёт.pdf")
        self.assertEqual(path.parent, workshop.INBOX)

    def test_collision_gets_suffix(self):
        p1, _ = workshop.inbox_accept("a.txt", 10, day="20260703")
        p1.write_text("x", encoding="utf-8")
        p2, _ = workshop.inbox_accept("a.txt", 10, day="20260703")
        self.assertNotEqual(p1, p2)
        self.assertEqual(p2.name, "20260703_a_2.txt")

    def test_live_paths_split_groups_and_private_chats(self):
        group, _ = workshop.inbox_accept(
            "a.txt", 10, day="20260703", chat_kind="group", chat_id="-10077",
            chat_label="Кухня")
        direct, _ = workshop.inbox_accept(
            "a.txt", 10, day="20260703", chat_kind="private", chat_id="42",
            chat_label="Вася")
        self.assertEqual(group.relative_to(workshop.INBOX).parts[:2],
                         ("groups", "Кухня_-10077"))
        self.assertEqual(direct.relative_to(workshop.INBOX).parts[:2],
                         ("private", "Вася_42"))

    def test_shared_inbox_reader_cannot_escape(self):
        path, _ = workshop.inbox_accept(
            "note.txt", 10, chat_kind="private", chat_id="42", chat_label="Вася")
        path.write_text("первая\nвторая", encoding="utf-8")
        out = workshop.inbox_read(path.relative_to(workshop.BASE).as_posix())
        self.assertIn("первая", out)
        self.assertIn("вторая", out)
        self.assertIn("путь должен быть", workshop.inbox_read("memory/llm.json"))

    def test_file_cap(self):
        path, why = workshop.inbox_accept("big.iso", (workshop.INBOX_FILE_MB + 1) * 1024 * 1024)
        self.assertIsNone(path)
        self.assertIn("больше лимита", why)

    def test_total_quota(self):
        workshop.INBOX.mkdir(parents=True, exist_ok=True)
        self._orig.append((workshop, "INBOX_TOTAL_MB", workshop.INBOX_TOTAL_MB))
        workshop.INBOX_TOTAL_MB = 1  # 1МБ квота для теста
        (workshop.INBOX / "old.bin").write_bytes(b"\0" * (1024 * 1024))
        path, why = workshop.inbox_accept("more.bin", 512 * 1024)
        self.assertIsNone(path)
        self.assertIn("квота", why)


@unittest.skipUnless(_RUNNER, "нет telethon — тесты раннера в контейнере")
class TestInboxRunner(InboxBase):
    def test_documents_archive_from_every_live_message(self):
        doc = types.SimpleNamespace(document=object(), photo=None)
        self.assertTrue(_mr._wants_inbox(True, True, doc))
        self.assertTrue(_mr._wants_inbox(True, False, doc), "чужая личка — тоже разговор")
        self.assertTrue(_mr._wants_inbox(False, True, doc), "файл владельца в группе принимаем")
        self.assertTrue(_mr._wants_inbox(False, False, doc, addressed=True))
        self.assertTrue(_mr._wants_inbox(False, False, doc, addressed=False),
                        "пассивный групповой документ тоже остаётся в inbox")
        nofile = types.SimpleNamespace(document=None, photo=None)
        self.assertFalse(_mr._wants_inbox(True, True, nofile))

    def test_download_makes_tag_with_path(self):
        saved = {}

        class Msg:
            file = types.SimpleNamespace(name="отчёт.pdf", ext=".pdf", size=2048)

            async def download_media(self, file):
                Path(file).parent.mkdir(parents=True, exist_ok=True)
                Path(file).write_bytes(b"pdf")
                saved["path"] = file
                return file

        tag = asyncio.run(_mr._inbox_download(Msg()))
        self.assertIn("[Файл: отчёт.pdf →", tag)
        self.assertIn("workspace/inbox/", tag)
        self.assertTrue(Path(saved["path"]).exists(), "файл должен лежать в inbox")

    def test_over_cap_honest_tag_no_download(self):
        class Msg:
            file = types.SimpleNamespace(name="big.iso", ext=".iso",
                                         size=(workshop.INBOX_FILE_MB + 5) * 1024 * 1024)

            async def download_media(self, file):
                raise AssertionError("сверх капа качать нельзя")

        tag = asyncio.run(_mr._inbox_download(Msg()))
        self.assertIn("не качаю", tag)
        self.assertIn("big.iso", tag)

    def test_photo_without_name_gets_ext(self):
        class Msg:
            file = types.SimpleNamespace(name=None, ext=".jpg", size=100)

            async def download_media(self, file):
                Path(file).parent.mkdir(parents=True, exist_ok=True)
                Path(file).write_bytes(b"jpg")
                return file

        tag = asyncio.run(_mr._inbox_download(Msg()))
        self.assertIn("photo.jpg", tag)


# --------------------------------------------------------------------------- #
#  9.4 задачи «напиши X» + прозрачность окон
# --------------------------------------------------------------------------- #

import heartbeat
import social
import tasks as tasks_mod


class TaskTargetBase(BufBase):
    def setUp(self):
        super().setUp()
        self._orig.append((heartbeat, "DECISIONS_PATH", heartbeat.DECISIONS_PATH))
        heartbeat.DECISIONS_PATH = self.tmp / "memory" / ".state" / "window_decisions.json"
        self._orig.append((tasks_mod, "TASKS", tasks_mod.TASKS))
        tasks_mod.TASKS = self.tmp / "memory" / "tasks.json"
        agent._TELETHON.pop("get_id", None)
        self.addCleanup(lambda: agent._TELETHON.pop("get_id", None))


class TestMessageTaskResolve(TaskTargetBase):
    def test_resolved_known_target_no_risky(self):
        agent._TELETHON["get_id"] = lambda ref: 555
        self._orig.append((social, "category", social.category))
        social.category = lambda sid: "known"
        out = agent.tool_remind_self("message", "поздравь с релизом", "in 2h", "@vasya")
        self.assertIn("Наметила #", out)
        self.assertNotIn("незнаком", out)
        t = tasks_mod.list_open()[-1]
        self.assertEqual(t["target_id"], 555, "id должен резолвиться при постановке")
        self.assertFalse(t.get("risky", False))

    def test_unresolved_honest_error_no_task(self):
        agent._TELETHON["get_id"] = lambda ref: (_ for _ in ()).throw(ValueError("нет такого"))
        out = agent.tool_remind_self("message", "привет", "in 1h", "@nosuch")
        self.assertIn("Не нахожу", out)
        self.assertEqual(tasks_mod.list_open(), [], "тихий провал в планировщике — запрещён")

    def test_stranger_gets_risky_mark(self):
        agent._TELETHON["get_id"] = lambda ref: 777000
        self._orig.append((social, "category", social.category))
        social.category = lambda sid: "unknown"
        out = agent.tool_remind_self("message", "напиши как договорились", "in 1h", "@stranger")
        self.assertIn("незнаком", out, "в подтверждении — честное слово о незнакомце")
        t = tasks_mod.list_open()[-1]
        self.assertTrue(t["risky"])
        self.assertIn("risky", agent.tool_my_agenda(), "пометка видна в моих намерениях")

    def test_no_bridge_honest_refusal(self):
        out = agent.tool_remind_self("message", "привет", "", "@vasya")
        self.assertIn("недоступен", out)
        self.assertEqual(tasks_mod.list_open(), [])

    def test_empty_target_refused(self):
        agent._TELETHON["get_id"] = lambda ref: 1
        out = agent.tool_remind_self("message", "привет", "", "")
        self.assertIn("нужен адресат", out)

    def test_other_kinds_untouched(self):
        out = agent.tool_remind_self("note", "не забыть про бэкап", "in 1h")
        self.assertIn("Наметила #", out)


# ⚠ Отсюда удалён класс TestWindowTransparency: он целиком проверял
# heartbeat.window_goal / record_decision / mark_window / WINDOW_DECIDE_SYS — контур,
# который не вызывался в проде ниоткуда и выброшен 25.07 по решению Егора.
# Цифра, которую стоит запомнить: мёртвый путь был покрыт дюжиной зелёных
# тестов. Покрытие говорило, что код работает, а не что он вызывается.



# --------------------------------------------------------------------------- #
#  9.5 сон v2: СВС (дубли досье, подрезка рёбер) + РЕМ (гипотезы) + мётла + отчёт
# --------------------------------------------------------------------------- #

import graph as graph_mod
import people as people_mod
import sleep as sleep_mod


class SleepBase(BufBase):
    def setUp(self):
        super().setUp()
        mem = self.tmp / "memory"
        patch = {
            sleep_mod: dict(BASE=self.tmp, TRASH_DIR=mem / ".trash",
                            DUPS_STATE=mem / ".state" / "sleep_dups.json"),
            graph_mod: dict(BASE=self.tmp, MEM_DIR=mem, GRAPH_MD=mem / "graph.md"),
            workshop: dict(BASE=self.tmp, INBOX=self.tmp / "workspace" / "inbox"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)

    def _dossier(self, slug, title, facts=(), aliases=""):
        body = {people_mod.FACTS: "\n".join(f"- [public] (s2) {f}" for f in facts)}
        if aliases:
            body[people_mod.ALIASES_KEY] = aliases
        people_mod.write(slug, title, body)

    def _journal_text(self):
        out = ""
        for f in sorted((self.tmp / "memory" / "journal").glob("*.md")):
            out += f.read_text(encoding="utf-8")
        return out


class TestSleepDossierDups(SleepBase):
    def test_dry_run_proposes_without_mutation(self):
        self._dossier("евгений", "Евгений", facts=["любит кофе", "живёт в Питере"])
        self._dossier("zhenya", "Женя", facts=["любит кофе"], aliases="Евгений")
        before_k = people_mod.read_text("евгений")
        before_a = people_mod.read_text("zhenya")
        merged, proposed = sleep_mod.svs_dossier_pass()
        self.assertEqual((merged, proposed), (0, 1), "первый заход — только предложение")
        self.assertEqual(people_mod.read_text("евгений"), before_k, "dry-run не мутирует")
        self.assertEqual(people_mod.read_text("zhenya"), before_a, "dry-run не мутирует")
        j = self._journal_text()
        self.assertIn("предлагаю слить", j)
        self.assertIn("евгений.md", j)

    def test_repeat_match_merges_with_backup(self):
        # keep выбирается по размеру файла — евгений.md заметно больше
        self._dossier("евгений", "Евгений",
                      facts=["любит кофе", "живёт в Питере", "строит дом", "не любит зумы"])
        self._dossier("zhenya", "Женя", facts=["любит кофе", "играет в шахматы"],
                      aliases="Евгений")
        sleep_mod.svs_dossier_pass()                      # dry-run
        merged, _ = sleep_mod.svs_dossier_pass()          # повтор → слияние
        self.assertEqual(merged, 1)
        self.assertFalse(people_mod.path_for("zhenya").exists(), "поглощённый файл удалён")
        trash = list(sleep_mod.TRASH_DIR.rglob("zhenya.md"))
        self.assertEqual(len(trash), 1, "бэкап поглощаемого обязателен")
        self.assertIn("шахматы", trash[0].read_text(encoding="utf-8"))
        keep = people_mod.read_text("евгений")
        self.assertIn("шахматы", keep, "уникальные факты absorb доехали")
        self.assertEqual(keep.count("любит кофе"), 1, "общие факты не задвоились")
        self.assertIn("Женя", ", ".join(people_mod.aliases("евгений")),
                      "имя absorb стало алиасом keep")
        self.assertIn("слила досье", self._journal_text())

    def test_scheduled_mode_never_merges_repeat_candidate(self):
        self._dossier("евгений", "Евгений",
                      facts=["любит кофе", "живёт в Питере", "строит дом"])
        self._dossier("zhenya", "Женя", facts=["любит кофе", "играет в шахматы"],
                      aliases="Евгений")
        sleep_mod.svs_dossier_pass(allow_merge=False)
        merged, _ = sleep_mod.svs_dossier_pass(allow_merge=False)
        self.assertEqual(merged, 0)
        self.assertTrue(people_mod.path_for("евгений").exists())
        self.assertTrue(people_mod.path_for("zhenya").exists())

    def test_low_confidence_never_automerges(self):
        self._dossier("маргарита", "Маргарита", facts=["факт а"])
        self._dossier("марго", "Марго", facts=["факт б"])  # префикс → 0.8 < 0.95
        sleep_mod.svs_dossier_pass()
        merged, _ = sleep_mod.svs_dossier_pass()
        self.assertEqual(merged, 0, "score < порога — только предложения, слияния нет")
        self.assertTrue(people_mod.path_for("марго").exists())

    def test_unrelated_names_not_candidates(self):
        self._dossier("антон", "Антон")
        self._dossier("хоуп", "Хоуп")
        self.assertEqual(sleep_mod.dossier_dup_candidates(), [])


class TestSleepEdgePrune(SleepBase):
    def test_exact_dup_lines_pruned_date_ignored(self):
        gm = graph_mod.GRAPH_MD
        gm.parent.mkdir(parents=True, exist_ok=True)
        gm.write_text(
            "# Граф памяти — связи не-людей\n\n"
            "- [[кофе]] ↔ [[утро]] — ритуал _(2026-06-01)_\n"
            "- [[кофе]] ↔ [[утро]] — ритуал _(2026-07-01)_\n"
            "- [[кофе]] ↔ [[утро]] — другая подпись _(2026-07-01)_\n", encoding="utf-8")
        removed = sleep_mod.prune_duplicate_edges()
        self.assertEqual(removed, 1, "точный повтор пары+подписи — один лишний")
        text = gm.read_text(encoding="utf-8")
        self.assertEqual(text.count("ритуал"), 1)
        self.assertIn("другая подпись", text, "разные подписи — не дубль")

    def test_graph_line_dup_of_person_link_pruned(self):
        self._dossier("антон", "Антон")
        people_mod.add_link("антон", "Антон", "стройка", "прораб")
        gm = graph_mod.GRAPH_MD
        gm.parent.mkdir(parents=True, exist_ok=True)
        gm.write_text("# Граф памяти — связи не-людей\n\n"
                      "- [[антон]] ↔ [[стройка]] — прораб _(2026-06-01)_\n", encoding="utf-8")
        removed = sleep_mod.prune_duplicate_edges()
        self.assertEqual(removed, 1, "дубль строки из файла человека в graph.md чистится")
        self.assertIn("[[стройка]]", people_mod.read_text("антон"), "первоисточник цел")


class TestSleepRem(SleepBase):
    def _seed_graph(self):
        gm = graph_mod.GRAPH_MD
        gm.parent.mkdir(parents=True, exist_ok=True)
        gm.write_text("# Граф памяти — связи не-людей\n\n"
                      "- [[кофе]] ↔ [[утро]] — ритуал _(2026-06-01)_\n"
                      "- [[стройка]] ↔ [[смета]] — торг _(2026-06-02)_\n", encoding="utf-8")

    def test_hypotheses_marked_and_self_line_to_journal(self):
        self._seed_graph()
        self._client(json.dumps({"links": [{"a": "кофе", "b": "смета", "why": "оба утренние"}],
                                 "self": "я оживаю к вечеру"}, ensure_ascii=False))
        added, selfish = sleep_mod.rem_pass()
        self.assertEqual(added, 1)
        self.assertTrue(selfish)
        g = graph_mod.GRAPH_MD.read_text(encoding="utf-8")
        self.assertIn("(сон, гипотеза)", g, "гипотеза несёт пометку")
        self.assertIn("оба утренние", g)
        j = self._journal_text()
        self.assertIn("гипотеза о себе", j)
        self.assertIn("я оживаю к вечеру", j)
        self.assertNotIn("я оживаю к вечеру",
                         (self.tmp / "soul" / "self.md").read_text(encoding="utf-8"),
                         "legacy self остаётся архивом; дневниковый кандидат не нормативен")

    def test_scheduled_rem_keeps_relation_episodic_and_graph_byte_identical(self):
        self._seed_graph()
        before = graph_mod.GRAPH_MD.read_bytes()
        self._client(json.dumps({"links": [
            {"a": "кофе", "b": "смета", "why": "оба утренние"},
        ]}, ensure_ascii=False))
        candidates, _ = sleep_mod.rem_pass(apply=False)
        self.assertEqual(candidates, 1)
        self.assertEqual(graph_mod.GRAPH_MD.read_bytes(), before)
        journal = self._journal_text()
        self.assertIn("episodic candidate, не claim", journal)
        self.assertIn("оба утренние", journal)

    def test_empty_graph_skips_without_model(self):
        class Boom:
            def __init__(self):
                class _M:
                    def create(_s, **kw):
                        raise AssertionError("пустой граф — модель звать нельзя")
                self.messages = _M()
        llm.use_test_client(Boom())
        self.assertEqual(sleep_mod.rem_pass(), (0, False))

    def test_budget_caps_call(self):
        self._seed_graph()
        self._orig.append((sleep_mod, "REM_MAX_TOKENS", sleep_mod.REM_MAX_TOKENS))
        sleep_mod.REM_MAX_TOKENS = 300
        self._client("{}")
        sleep_mod.rem_pass()
        self.assertLessEqual(self.fc.last["max_tokens"], 300, "бюджет фазы уважается")
        user = self.fc.last["messages"][0]["content"]
        self.assertLessEqual(len(user), 300 * 3 + 10, "вход порезан под бюджет")

    def test_max_three_hypotheses(self):
        self._seed_graph()
        links = [{"a": "кофе", "b": f"тема{i}", "why": f"гипотеза {i}"} for i in range(6)]
        self._client(json.dumps({"links": links}, ensure_ascii=False))
        added, _ = sleep_mod.rem_pass()
        self.assertLessEqual(added, 3)


class TestSleepSweepAndReport(SleepBase):
    def test_sweep_inbox_old_only(self):
        inbox = workshop.INBOX
        inbox.mkdir(parents=True, exist_ok=True)
        old = inbox / "20260501_старьё.pdf"
        fresh = inbox / "20260703_свежее.pdf"
        nested_dir = inbox / "groups" / "кухня_-1007"
        nested_dir.mkdir(parents=True)
        nested = nested_dir / "20260501_старьё.txt"
        old.write_bytes(b"x")
        fresh.write_bytes(b"y")
        nested.write_bytes(b"z")
        past = time.time() - 40 * 86400
        _os.utime(old, (past, past))
        _os.utime(nested, (past, past))
        n = sleep_mod.sweep_inbox()
        self.assertEqual(n, 2)
        self.assertFalse(old.exists())
        self.assertFalse(nested.exists())
        self.assertFalse(nested_dir.exists(), "empty per-chat folder is cleaned")
        self.assertTrue(fresh.exists(), "свежее мётла не трогает")

    def test_run_writes_report(self):
        for module, name in (
            (sleep_mod.consolidate, "run"),
            (sleep_mod.formation, "run"),
            (sleep_mod, "svs_dossier_pass"),
            (sleep_mod, "prune_duplicate_edges"),
            (sleep_mod, "unpark_expired_loops"),
            (sleep_mod, "rem_pass"),
        ):
            self._orig.append((module, name, getattr(module, name)))
        sleep_mod.consolidate.run = lambda *a, **kw: "Сведено дней: 0."
        seen = {}
        sleep_mod.formation.run = lambda depth, **kw: (
            seen.update(formation=(depth, kw)) or {"summary": "claims checked"}
        )
        sleep_mod.svs_dossier_pass = lambda *, allow_merge=True: (
            seen.update(allow_merge=allow_merge) or (0, 0)
        )
        sleep_mod.prune_duplicate_edges = lambda: (_ for _ in ()).throw(
            AssertionError("scheduled sleep must not rewrite graph directly")
        )
        sleep_mod.unpark_expired_loops = lambda: (_ for _ in ()).throw(
            AssertionError("scheduled sleep must not rewrite people directly")
        )
        sleep_mod.rem_pass = lambda *a, **kw: (
            seen.update(rem_apply=kw.get("apply")) or (1, False)
        )
        out = sleep_mod.run(depth="full")
        self.assertIn("сон:", out)
        self.assertEqual(seen["allow_merge"], False)
        self.assertEqual(seen["rem_apply"], False)
        self.assertEqual(seen["formation"][0], "full")
        self.assertIn("durable mutations — только через formation claims", out)
        j = self._journal_text()
        self.assertIn("[сон] сон: слито 0", j)
        self.assertIn("inbox −0 файлов", j)


if __name__ == "__main__":
    unittest.main(verbosity=2)
