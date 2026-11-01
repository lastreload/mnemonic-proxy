"""CPU-only regressions for per-model configuration profiles (issue #27)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ctxproxy import engines, server
from ctxproxy.core import Config, Journal, Store, TokenCounter, rid_for
from ctxproxy.profiles import ProfileProxy, load_profiles
from ctxproxy.server import Proxy, build_parser, load_config
from ctxproxy.upstream import Upstream, UpstreamError
from tests.fake_engine import FakeEngine
from tests.test_proxy import TOOLS, history, tool_round

try:
    import jinja2  # noqa: F401
    from tokenizers import Tokenizer, models, pre_tokenizers
    EXACT = True
except ImportError:
    EXACT = False


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def managed_requests(engine):
    """Exclude the one-token active-model probes from request-body assertions."""
    return [b for b in engine.requests if not (b.get("max_tokens") == 1 and b.get("messages") == [
        {"role": "user", "content": "hi"}])]


class ProfileBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.engine = FakeEngine(models={"alpha": 32768, "beta": 65536,
                                         "sharp-one": 32768, "sharp-two": 32768})
        self.url = self.engine.start()
        self.addCleanup(self.engine.stop)
        self.proxy = None

    def tearDown(self):
        if self.proxy is not None:
            self.proxy.close()

    def build(self, raw, mode="tools"):
        cfg = Config.from_dict(raw)
        up = Upstream(self.url)
        journal = Journal(os.path.join(self.tmp.name, "journal.jsonl"))
        counter = TokenCounter(3.5)
        legacy = Proxy(cfg, up, Store(os.path.join(self.tmp.name, "legacy.sqlite")), journal, counter, mode=mode)
        legacy.engine = engines.setup(cfg, up, journal, explicit=set(raw), counter=counter, log=None)
        specs, default = load_profiles(raw, cfg)
        self.proxy = ProfileProxy(legacy, SimpleNamespace(data=self.tmp.name), raw, specs, default)
        return self.proxy

    def send(self, model, messages=None, expect=200, **kw):
        st, response, _ = self.proxy.chat({"model": model, "messages": messages or history(1),
                                           "tools": TOOLS, "max_tokens": 128, **kw})
        self.assertEqual(st, expect, response)
        return response

    def events(self, name):
        path = os.path.join(self.tmp.name, "journal.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [e for e in map(json.loads, f) if e["event"] == name]


class TestSelectionAndWindows(ProfileBase):
    def test_exact_match_engine_window_fallback_and_override(self):
        self.build({"profiles": {
            "alpha": {"window": 16384, "mask_trigger": 12345},
            "beta": {"window": 20000, "window_override": True},
        }})
        self.send("alpha")
        self.assertEqual(self.proxy.contexts["alpha"].cfg.window, 32768,
                         "engine n_ctx wins over the profile fallback")
        self.assertEqual(self.proxy.contexts["alpha"].cfg.mask_trigger, 12345,
                         "an explicit profile threshold is not rescaled")
        self.send("beta")
        self.assertEqual(self.proxy.contexts["beta"].cfg.window, 20000)
        self.assertEqual([b["model"] for b in managed_requests(self.engine)], ["alpha", "beta"])
        events = self.events("model_profile")
        self.assertEqual([(e["model"], e["profile"]) for e in events], [("alpha", "alpha"), ("beta", "beta")])

    def test_generic_engine_uses_profile_window_fallback(self):
        self.engine.stop()
        self.engine = FakeEngine(flavor="openai", models={"alpha": 1, "beta": 1})
        self.url = self.engine.start()
        self.build({"profiles": {"alpha": {"window": 16384}, "beta": {"window": 32768}}})
        self.send("alpha")
        self.send("beta")
        self.assertEqual(self.proxy.contexts["alpha"].cfg.window, 16384)
        self.assertEqual(self.proxy.contexts["beta"].cfg.window, 32768)
        self.assertEqual(self.proxy.contexts["beta"].cfg.window_estimate_slack, 0.15,
                         "generic-engine estimated-count slack survives later requests")

    def test_unknown_is_400_before_engine_and_lists_profiles(self):
        self.build({"profiles": {"alpha": {}, "sharp-*": {}}})
        before = len(self.engine.requests)
        response = self.send("unknown", expect=400)
        self.assertEqual(response["error"]["code"], "unknown_model")
        self.assertIn("alpha", response["error"]["message"])
        self.assertIn("sharp-*", response["error"]["message"])
        self.assertEqual(len(self.engine.requests), before)
        response = self.send(None, expect=400)
        self.assertIn("missing", response["error"]["message"])
        self.assertEqual(len(self.engine.requests), before)
        st, response = self.proxy.count_tokens({"model": "unknown", "messages": []})
        self.assertEqual(st, 400)
        self.assertEqual(response["error"]["code"], "unknown_model")
        self.assertEqual(len(self.engine.requests), before, "count_tokens cannot bypass unknown-model validation")

    def test_default_is_explicit_and_never_reuses_previous_profile(self):
        self.build({"default_profile": "alpha", "profiles": {"alpha": {}, "beta": {}}})
        self.send("beta")
        self.send("not-configured")
        self.send(None)
        self.assertEqual([b["model"] for b in managed_requests(self.engine)], ["beta", "alpha", "alpha"])
        defaults = self.events("model_profile_default")
        self.assertEqual([e["requested"] for e in defaults], ["not-configured", None])

    def test_patterns_have_isolated_concrete_model_runtimes(self):
        self.build({"profiles": {"sharp-*": {"window": 16384}}})
        self.send("sharp-one")
        self.send("sharp-two")
        self.assertEqual(set(self.proxy.contexts), {"sharp-one", "sharp-two"})
        self.assertIsNot(self.proxy.contexts["sharp-one"].store, self.proxy.contexts["sharp-two"].store)
        self.assertIsNot(self.proxy.contexts["sharp-one"].tc, self.proxy.contexts["sharp-two"].tc)
        self.assertEqual([e["profile"] for e in self.events("model_profile")], ["sharp-*", "sharp-*"])

    def test_validation_rejects_bad_default_and_nested_profiles(self):
        for raw in ({"default_profile": "sharp-*", "profiles": {"sharp-*": {}}},
                    {"profiles": {"alpha": {"profiles": {}}}},
                    {"profiles": {"alpha": {"not_a_threshold": 1}}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                load_profiles(raw, Config.from_dict(raw))


class TestConversationIsolation(ProfileBase):
    def test_switch_back_retains_each_models_accounting_and_archive(self):
        self.build({"profiles": {"alpha": {}, "sharp-*": {}}})
        h = history(2)
        a = self.send("alpha", h)["strata_context"]
        b = self.send("sharp-one", h)["strata_context"]
        a2 = self.send("alpha", h + [{"role": "assistant", "content": "ok"},
                                      {"role": "user", "content": "continue"}])["strata_context"]
        self.assertNotEqual(a["conversation_id"], b["conversation_id"])
        self.assertEqual(a["conversation_id"], a2["conversation_id"])
        ca, cb = self.proxy.contexts["alpha"], self.proxy.contexts["sharp-one"]
        self.assertIsNot(ca.store, cb.store)
        self.assertFalse(ca.store.has_conv(b["conversation_id"]))
        self.assertFalse(cb.store.has_conv(a["conversation_id"]))
        rid = rid_for(1, h[1])
        self.assertEqual(ca.store.get(rid, a["conversation_id"])[5], "crea il gioco snake")
        self.assertEqual(cb.store.get(rid, b["conversation_id"])[5], "crea il gioco snake")
        self.assertIs(self.proxy.store_for(conv=a["conversation_id"]), ca.store)
        self.assertIs(self.proxy.store_for(rid=rid), ca.store)  # first matching concrete store

    def test_pattern_conversations_do_not_share_store_state(self):
        self.build({"profiles": {"sharp-*": {}}})
        h = history(1)
        one = self.send("sharp-one", h)["strata_context"]["conversation_id"]
        two = self.send("sharp-two", h)["strata_context"]["conversation_id"]
        self.assertNotEqual(one, two)
        self.assertNotEqual(self.proxy.contexts["sharp-one"].store.db.execute(
            "SELECT count(*) FROM archive").fetchone()[0], 0)
        self.assertFalse(self.proxy.contexts["sharp-one"].store.has_conv(two))

    def test_concurrent_requests_are_serialized_per_active_engine(self):
        seen = []
        def policy(body):
            seen.append((body.get("model"), self.engine.active_model))
            time.sleep(0.002)
            return {"role": "assistant", "content": "ok"}
        self.engine.policy = policy
        self.build({"profiles": {"alpha": {}, "beta": {}}})
        errors = []
        def run(model):
            try:
                for _ in range(4):
                    self.send(model)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=run, args=(m,)) for m in ("alpha", "beta")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertTrue(seen)
        self.assertTrue(all(requested == active for requested, active in seen), seen)


class TestInternalModelPropagation(ProfileBase):
    def test_recall_rounds_keep_originating_model(self):
        def policy(body):
            last = body["messages"][-1]
            if last.get("role") == "tool" and str(last.get("content", "")).startswith("[recall:"):
                return {"role": "assistant", "content": "found"}
            if last.get("role") == "user" and "remember" in str(last.get("content")):
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "r1", "type": "function", "function": {"name": "recall",
                                                                      "arguments": '{"query":"snake"}'}}]}
            return {"role": "assistant", "content": "ok"}
        self.engine.policy = policy
        self.build({"profiles": {"alpha": {}, "beta": {}}})
        h = history(1) + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "remember"}]
        start = len(self.engine.requests)
        response = self.send("beta", h)
        self.assertEqual(response["choices"][0]["message"]["content"], "found")
        calls = self.engine.requests[start:]
        self.assertGreaterEqual(len(calls), 3)  # activation + original + recall continuation
        self.assertTrue(all(b["model"] == "beta" for b in calls))

    def test_final_retries_keep_originating_model(self):
        self.engine.policy = lambda body: {"role": "assistant", "content": "", "tool_calls": [
            {"id": "r%d" % len(self.engine.requests), "type": "function",
             "function": {"name": "recall", "arguments": '{"query":"snake"}'}}]}
        self.build({"max_recall_rounds": 0, "profiles": {"alpha": {}, "beta": {}}})
        start = len(self.engine.requests)
        self.send("beta")
        calls = self.engine.requests[start:]
        self.assertGreaterEqual(len(calls), 3, "initial final call plus bounded final retries")
        self.assertTrue(all(b["model"] == "beta" for b in calls))

    def test_notes_seal_and_save_keep_model(self):
        self.build({"window": 32768, "mask_enabled": False, "tail_max": 12000, "reserve": 2000,
                    "default_response": 2000, "slot_save": True, "seal_experimental": True,
                    "profiles": {"alpha": {"window": 32768, "window_override": True}}})
        h = history(1)
        for k in range(1, 15):
            h += tool_round(k, 12000)
            start = len(self.engine.requests)
            self.send("alpha", h, max_tokens=256)
            self.assertTrue(all(b["model"] == "alpha" for b in self.engine.requests[start:]))
            if self.events("switch"):
                break
        self.assertTrue(self.events("notes"))
        self.assertTrue(self.events("seal"))
        self.assertTrue(self.events("save"))
        self.assertTrue(all(owner == "alpha" for owner in self.engine.saved_by.values()))

    def test_anchor_calls_and_saved_file_keep_model(self):
        self.build({"mask_trigger": 1000, "mask_target": 500, "min_mask_tokens": 50,
                    "keep_recent_tokens": 200, "min_age_turns": 1, "min_batch_tokens": 100,
                    "mask_anchor": True, "anchor_min_tokens": 100, "slot_save": True,
                    "segments_enabled": False, "profiles": {"alpha": {}}})
        self.send("alpha", history(5))
        self.assertTrue(self.events("anchor_save"))
        start = len(self.engine.requests)
        self.send("alpha", history(6))
        self.assertTrue(all(b["model"] == "alpha" for b in self.engine.requests[start:]))
        self.assertTrue(self.events("anchor_save"))
        self.assertTrue(all(owner == "alpha" for owner in self.engine.saved_by.values()))


class TestSavedStateIsolation(ProfileBase):
    def test_autosave_restore_and_cross_model_refusal(self):
        self.build({"slot_save": True, "autosave": True, "autosave_idle_s": 180,
                    "autosave_min_tokens": 100, "autorestore_min_gain": 100,
                    "autosave_min_free_gb": 0, "profiles": {"alpha": {}, "beta": {}}})
        h = history(6)
        self.send("alpha", h)
        ca = self.proxy.contexts["alpha"]
        ea = ca.autosaver.tick(now=time.time() + 200)
        self.assertIsNotNone(ea)
        self.send("beta", h)
        cb = self.proxy.contexts["beta"]
        eb = cb.autosaver.tick(now=time.time() + 200)
        self.assertIsNotNone(eb)
        self.assertNotEqual(ea["file"], eb["file"])
        self.assertEqual(self.engine.saved_by[ea["file"]], "alpha")
        self.assertEqual(self.engine.saved_by[eb["file"]], "beta")
        base_a = ca.up.up
        with self.assertRaises(UpstreamError) as caught:
            base_a.slot("restore", eb["file"])
        self.assertEqual(caught.exception.status, 409)
        self.assertNotIn(("alpha", eb["file"], "beta"), self.engine.restores,
                         "the fake engine would allow it; the proxy must refuse first")
        h2 = h + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "continue"}]
        self.send("alpha", h2)
        self.assertIn(("alpha", ea["file"], "alpha"), self.engine.restores)
        self.assertNotIn(("alpha", eb["file"], "beta"), self.engine.restores)
        owners = self.proxy.owners
        self.assertEqual(owners.get(ea["file"]), "alpha")
        self.assertEqual(owners.get(eb["file"]), "beta")


@unittest.skipUnless(EXACT, "install mnemonic-proxy[exact]")
class TestTokenizerAndTemplate(ProfileBase):
    def tokenizer(self, name, template=False):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path)
        tk = Tokenizer(models.WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]"))
        tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
        tk.save(os.path.join(path, "tokenizer.json"))
        if template:
            with open(os.path.join(path, "chat_template.jinja"), "w", encoding="utf-8") as f:
                f.write("{{ 'sharp ' * 100 }}{% for m in messages %}{{ m.role }} {{ m.content }} {% endfor %}assistant ")
        return os.path.join(path, "tokenizer.json")

    def test_tokenizer_and_pack_template_switch_together(self):
        plain = self.tokenizer("plain")
        sharp = self.tokenizer("sharp", template=True)
        self.build({"profiles": {"alpha": {"tokenizer": plain}, "sharp-*": {"tokenizer": sharp}}})
        self.send("alpha")
        self.send("sharp-one")
        a, s = self.proxy.contexts["alpha"], self.proxy.contexts["sharp-one"]
        self.assertTrue(a.tc.exact and s.tc.exact)
        self.assertIsNone(a.tc.prompt_template)
        self.assertIsNotNone(s.tc.prompt_template)
        self.assertIsNot(a.tc, s.tc)
        self.assertIsNot(a.tc.tk, s.tc.tk)
        body = {"messages": [{"role": "user", "content": "hello world"}], "max_tokens": 10}
        self.assertLess(a.wguard.count_body(body), s.wguard.count_body(body))
        summaries = {x["profile"]: x for x in self.proxy.profile_summary()}
        self.assertEqual(summaries["alpha"]["template"], "builtin-qwen")
        self.assertEqual(summaries["sharp-*"]["template"], "pack")


class TestLegacyRegression(unittest.TestCase):
    def test_no_profiles_forwarded_prompt_is_byte_identical_to_0319(self):
        engine = FakeEngine()
        url = engine.start()
        self.addCleanup(engine.stop)
        proxy = Proxy(Config(), Upstream(url), Store(), Journal(None), TokenCounter(3.5))
        self.addCleanup(proxy.store.db.close)
        req = {"model": "legacy-model", "messages": [{"role": "system", "content": "system"},
                                                     {"role": "user", "content": "hello"}],
               "tools": [{"type": "function", "function": {
                   "name": "bash", "description": "run", "parameters": {
                       "type": "object", "properties": {"command": {"type": "string"}}}}}],
               "max_tokens": 128, "temperature": 0}
        st, _, _ = proxy.chat(req)
        self.assertEqual(st, 200)
        raw = json.dumps(engine.requests[-1], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(),
                         "fa6e2446b2de309f129c4a3383aeb943e63ec508a7a7bc1113534cd2c7240265")


class TestCheckAndStartup(ProfileBase):
    def config(self, live=False):
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"profiles": {"alpha": {}, "beta": {}, "sharp-*": {}}}, f)
        argv = ["--upstream", self.url, "--port", str(free_port()), "--data",
                os.path.join(self.tmp.name, "check-data"), "--config", path]
        args = build_parser(prog="mnemonic-proxy check").parse_args(argv)
        args.live = live
        raw, cfg = load_config(args)
        return args, raw, cfg

    def test_check_lists_loaded_profiles(self):
        from ctxproxy.check import run_checks
        args, raw, cfg = self.config()
        report = run_checks(args, raw, cfg)
        lines = [i["evidence"] for i in report.items if i["check"] == "model profiles"]
        self.assertTrue(any(x.startswith("alpha:") for x in lines))
        self.assertTrue(any(x.startswith("beta:") for x in lines))
        self.assertTrue(any("pattern loaded" in x for x in lines))
        self.assertNotEqual(report.exit_code, 1, report.items)

    def test_check_live_lists_and_exercises_each_exact_model(self):
        from ctxproxy.check import run_checks
        args, raw, cfg = self.config(live=True)
        report = run_checks(args, raw, cfg, live=True)
        generations = [i for i in report.items if i["check"] == "live: generation"]
        self.assertEqual(len(generations), 2)
        self.assertNotEqual(report.exit_code, 1, report.items)
        self.assertIn("alpha", [b["model"] for b in self.engine.requests])
        self.assertIn("beta", [b["model"] for b in self.engine.requests])

    def test_startup_stderr_lists_profiles(self):
        args, raw, cfg = self.config()
        class Listener:
            daemon_threads = True
            def __init__(self, *a):
                pass
            def serve_forever(self):
                raise KeyboardInterrupt
            def server_close(self):
                pass
        err = io.StringIO()
        with patch.object(server, "ThreadingHTTPServer", Listener), contextlib.redirect_stderr(err):
            server.serve(args, raw, cfg)
        text = err.getvalue()
        self.assertIn("profile alpha:", text)
        self.assertIn("profile beta:", text)
        self.assertIn("profile sharp-*:", text)
        self.assertIn("engine when available, config fallback", text)

    def test_bad_profile_tokenizer_fails_before_upstream(self):
        raw = {"profiles": {"alpha": {"tokenizer": "missing.json"}}}
        cfg = Config.from_dict(raw)
        args = SimpleNamespace(data=self.tmp.name, tokenizer=None, upstream=self.url, api_key="")
        with patch("ctxproxy.core.load_tokenizer", side_effect=ValueError("bad tokenizer")), \
                patch.object(server, "Upstream") as upstream:
            with self.assertRaisesRegex(ValueError, "alpha.*estimated window guard"):
                server.serve(args, raw, cfg)
            upstream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
