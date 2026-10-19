# Author: Maurizio Verde — LastReload
"""0.3.8: real window from Ollama / LM Studio, engine truncation detected, estimate slack on generic engines.

    python3 -m pytest -q tests/test_engine_window.py
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import check, engines  # noqa: E402
from ctxproxy.core import Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.enginectx import GENERIC_SLACK, TruncWatch, pick  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402
from tests.test_proxy import TOOLS, history  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Base(unittest.TestCase):
    flavor = "ollama"
    n_ctx = 8192

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.eng = FakeEngine(flavor=self.flavor, n_ctx=self.n_ctx)
        self.url = self.eng.start()
        self.addCleanup(self.eng.stop)
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        self.logs: list = []

    def proxy(self, flavor_raw=None, **raw):
        cfg = Config.from_dict(raw)
        up = Upstream(self.url)
        j = Journal(self.jpath)
        self.engine = engines.setup(cfg, up, j, explicit=set(raw), log=self.logs.append)
        p = Proxy(cfg, up, Store(os.path.join(self.tmp.name, "a.sqlite")), j, TokenCounter(3.5))
        p.engine = self.engine
        self.addCleanup(p.store.db.close)
        self.cfg, self.p = cfg, p
        return p

    def events(self, name):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath) as f:
            return [e for e in map(json.loads, f) if e["event"] == name]

    def send(self, msgs, status=200, **kw):
        st, r, _ = self.p.chat({"model": self.eng.model, "messages": msgs, "tools": TOOLS, "max_tokens": 512, **kw})
        self.assertEqual(st, status, r)
        return r

    def check(self, *extra, cfg=None):
        argv = ["--upstream", self.url, "--port", str(free_port()), "--data", os.path.join(self.tmp.name, "data"),
                "--json", *extra]
        if cfg is not None:
            p = os.path.join(self.tmp.name, "cfg.json")
            with open(p, "w") as f:
                json.dump(cfg, f)
            argv += ["--config", p]
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            check.main(argv)
        res = json.loads(out.getvalue())
        self.assertEqual(res["exit_code"], cm.exception.code)
        return res["exit_code"], {(c["check"], c["status"]) for c in res["checks"]}, res["checks"]


class TestDetectOllama(Base):
    def test_window_read_from_api_ps(self):
        e = engines.detect(Upstream(self.url))
        self.assertEqual((e.kind, e.flavor, e.n_ctx, e.slot_save), ("openai", "ollama", 8192, False))
        self.assertEqual(e.model, "qwen3:4b-instruct-2507-q4_K_M")

    def test_window_and_thresholds_follow_the_engine(self):
        self.proxy()
        self.assertEqual(self.cfg.window, 8192)
        self.assertLess(self.cfg.mask_trigger, 8192)
        self.assertEqual(self.cfg.window_estimate_slack, GENERIC_SLACK)

    def test_not_loaded_model_is_loaded_and_read_before_the_first_request(self):
        self.eng.loaded, self.eng.load_ctx = False, 16384
        self.proxy()
        self.assertIsNone(self.engine.n_ctx)
        self.send(history(2, size=800) + [{"role": "user", "content": "e adesso?"}])
        self.assertEqual(self.eng.loads, 1)
        self.assertEqual(self.cfg.window, 16384)
        self.assertEqual(self.events("engine_load")[0]["status"], 200)
        self.assertEqual(self.events("engine_window")[-1]["n_ctx"], 16384)
        self.assertEqual(self.eng.truncated, [])

    def test_context_shrinks_window_follows(self):
        self.eng.n_ctx = 32768
        self.proxy()
        self.assertEqual(self.cfg.window, 32768)
        self.eng.n_ctx = 16384                    # model reloaded with less (OLLAMA_CONTEXT_LENGTH changed)
        self.send(history(24, size=3000) + [{"role": "user", "content": "e adesso?"}])
        self.assertEqual(self.cfg.window, 16384)
        self.assertEqual(self.eng.truncated, [], "the proxy never sends more than the engine context")
        self.assertEqual(self.events("engine_truncated"), [])

    def test_config_window_bigger_than_engine_is_refused(self):
        self.proxy(window=32768)
        big = engines.window_mismatch(self.cfg, self.engine, {"window"})
        self.assertEqual(len(big), 1)
        self.assertIn("OLLAMA_CONTEXT_LENGTH=32768", big[0][1])
        self.assertIn("8192", big[0][0])

    def test_config_window_lowered_if_engine_shrinks_later(self):
        self.eng.n_ctx = 32768
        self.proxy(window=32768)
        self.eng.n_ctx = 16384
        self.send(history(10, size=3000) + [{"role": "user", "content": "e adesso?"}])
        self.assertEqual(self.cfg.window, 16384)
        self.assertTrue(any("window lowered to 16384" in m for m in self.logs), self.logs)
        self.assertEqual(self.eng.truncated, [])

    @mock.patch.dict(os.environ, {engines.ALLOW_SMALL_WINDOW_ENV: ""})
    def test_default_context_refused_at_startup(self):
        self.proxy()                               # Ollama default-like 8192 < minimum window
        small = engines.window_problems(self.cfg, self.engine, set())
        self.assertTrue(small and "OLLAMA_CONTEXT_LENGTH=32768" in small[0][1], small)

    @mock.patch.dict(os.environ, {engines.ALLOW_SMALL_WINDOW_ENV: ""})
    def test_engine_too_small_refuses_the_request(self):
        self.eng.loaded, self.eng.load_ctx = False, 2048
        self.proxy()
        r = self.send([{"role": "user", "content": "ciao"}], status=400)
        self.assertIn("OLLAMA_CONTEXT_LENGTH", r["error"]["message"])
        self.assertEqual(self.events("engine_refused")[0]["status"], 400)

    def test_unknown_model_is_not_sent_blind(self):
        self.eng.loaded = False
        self.proxy()
        st, r, _ = self.p.chat({"model": "nope", "messages": [{"role": "user", "content": "ciao"}]})
        self.assertEqual(st, 503, r)
        self.assertEqual([x for x in self.eng.requests if x.get("model") == "nope" and x.get("max_tokens") != 1], [])


class TestDetectLMStudio(Base):
    flavor = "lmstudio"

    def test_window_read_from_loaded_context_length(self):
        e = engines.detect(Upstream(self.url))
        self.assertEqual((e.kind, e.flavor, e.n_ctx), ("openai", "lmstudio", 8192))

    def test_not_loaded_has_no_context(self):
        self.eng.loaded = False
        e = engines.detect(Upstream(self.url))
        self.assertEqual((e.flavor, e.n_ctx), ("lmstudio", None))

    def test_requests_fit_the_engine(self):
        self.eng.n_ctx = 16384
        self.proxy()
        msgs = history(4, size=3000)
        for k in range(6):
            msgs = msgs + [{"role": "user", "content": "passo %d" % k}]
            r = self.send(msgs)
            msgs = msgs + [r["choices"][0]["message"]]
            msgs += history(1, size=3000)[2:]
        self.assertEqual(self.eng.truncated, [])
        self.assertEqual(self.events("engine_truncated"), [])


class TestPick(unittest.TestCase):
    def test_pick(self):
        self.assertEqual(pick({"a:latest": 4096}, "a"), ("a:latest", 4096))
        self.assertEqual(pick({"a": 4096}, "other"), ("a", 4096), "one loaded model serves the request")
        self.assertEqual(pick({"a": 4096, "b": 32768}, "c"), ("a", 4096), "several: the smallest (safe)")
        self.assertEqual(pick({}, "a"), (None, 0))


class TestTruncation(Base):
    """Engine that does not tell its context (plain OpenAI-compatible) but is smaller than the window: cut in
    silence like Ollama -> engine_truncated, once per conversation in the log; answers unchanged."""
    flavor = "ollama"

    def setUp(self):
        super().setUp()

    def test_engine_truncated_logged_once_per_conversation(self):
        self.eng.n_ctx = 32768
        self.proxy(window=32768)
        self.eng.n_ctx = 4096
        # hide the context: the proxy believes 32768 (an unknown generic engine)
        self.engine.ctx_state = None
        msgs = history(16, size=3000)
        r = None
        for k in range(3):
            msgs = msgs + [{"role": "user", "content": "domanda %d" % k}]
            r = self.send(msgs)
            msgs = msgs + [r["choices"][0]["message"]]
        ev = self.events("engine_truncated")
        self.assertGreaterEqual(len(ev), 3, "every cut request is in the journal")
        self.assertTrue(all(e["prompt_tokens"] < 0.8 * e["est_tokens"] for e in ev))
        warn = [m for m in self.logs if "cut the prompt in silence" in m]
        self.assertEqual(len(warn), 1, "one warning per conversation")
        self.assertEqual(r["choices"][0]["message"]["content"], "ok", "answers unchanged")

    def test_no_calibration_on_cut_prompts(self):
        self.eng.n_ctx = 32768
        self.proxy(window=32768)
        self.eng.n_ctx = 4096
        self.engine.ctx_state = None
        before = self.p.tc.scale
        self.send(history(8, size=3000) + [{"role": "user", "content": "e adesso?"}])
        self.assertEqual(self.p.tc.scale, before)

    def test_whole_prompts_not_flagged(self):
        self.eng.n_ctx = 32768
        self.proxy()
        self.send(history(6, size=3000) + [{"role": "user", "content": "e adesso?"}])
        self.assertEqual(self.events("engine_truncated"), [])

    def test_ratio(self):
        self.assertTrue(TruncWatch.cut(13005, 3020))      # measured on AI395 (Ollama 4096)
        self.assertFalse(TruncWatch.cut(31319, 33354))    # whole prompt, estimate 6.5% low
        self.assertFalse(TruncWatch.cut(500, 10), "too small to judge")


class TestSlackOnlyGeneric(unittest.TestCase):
    def setUp(self):
        self.engs = []

    def tearDown(self):
        for e in self.engs:
            e.stop()

    def run_eng(self, **kw):
        e = FakeEngine(**kw)
        self.engs.append(e)
        return e.start()

    def test_slack(self):
        for flavor, slack in (("strata", 0.0), ("llama", 0.0), ("ds4", 0.0), ("openai", GENERIC_SLACK),
                              ("ollama", GENERIC_SLACK), ("lmstudio", GENERIC_SLACK)):
            cfg = Config()
            up = Upstream(self.run_eng(flavor=flavor, n_ctx=32768))
            e = engines.setup(cfg, up, None, explicit=set(), log=None)
            self.assertEqual(cfg.window_estimate_slack, slack, flavor)
            self.assertEqual(up.truncwatch is not None, e.kind == "openai", flavor)
            self.assertEqual(e.ctx_state is not None, flavor in ("ollama", "lmstudio"), flavor)

    def test_explicit_slack_wins(self):
        cfg = Config.from_dict({"window_estimate_slack": 0.05})
        engines.setup(cfg, Upstream(self.run_eng(flavor="openai")), None, explicit={"window_estimate_slack"}, log=None)
        self.assertEqual(cfg.window_estimate_slack, 0.05)

    def test_exact_tokenizer_no_slack(self):
        class Exact:
            exact = True
        cfg = Config()
        engines.setup(cfg, Upstream(self.run_eng(flavor="openai")), None, explicit=set(), counter=Exact(), log=None)
        self.assertEqual(cfg.window_estimate_slack, 0.0)

    def test_summary_unchanged_for_other_engines(self):
        e = engines.detect(Upstream(self.run_eng(flavor="llama")))
        self.assertNotIn("flavor", e.summary())


class TestCheck(Base):
    def test_window_bigger_than_engine_not_ready(self):
        code, st, items = self.check(cfg={"window": 32768})
        self.assertNotEqual(code, 0)
        self.assertIn(("context window", "fail"), st)
        fail = [i for i in items if i["status"] == "fail"]
        self.assertTrue(any("OLLAMA_CONTEXT_LENGTH=32768" in i.get("fix", "") for i in fail), fail)

    def test_window_from_engine_ok_with_tokenizer_warning(self):
        code, st, items = self.check(cfg={})
        self.assertIn(("context window", "ok"), st)
        self.assertIn(("token count", "warn"), st)
        self.assertTrue(any("no tokenizer: the window guard is estimated" in i["evidence"] for i in items))

    def test_live_full_window_ok(self):
        self.eng.n_ctx = 16384
        code, st, items = self.check("--live", cfg={})
        self.assertIn(("live: full window", "ok"), st, items)

    def test_live_full_window_cut_fails(self):
        self.eng.n_ctx = 16384
        self.eng.flavor = "openai_cut"     # answers like Ollama but hides /api: an unknown generic engine
        code, st, items = self.check("--live", cfg={"window": 32768})
        self.assertIn(("live: full window", "fail"), st, items)
        self.assertTrue(any("engine context smaller than window" in i["evidence"] for i in items))


class TestLMStudioCheck(Base):
    flavor = "lmstudio"

    def test_window_bigger_than_engine_not_ready(self):
        code, st, items = self.check(cfg={"window": 32768})
        self.assertIn(("context window", "fail"), st)
        self.assertTrue(any("lms load" in i.get("fix", "") for i in items if i["status"] == "fail"))


if __name__ == "__main__":
    unittest.main()
