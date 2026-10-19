"""llama-server in router mode (0.3.7, issue #12): detection, per-model window, model switch and unload, saved files
bound to their model, unknown n_ctx refused. Against ctxproxy.fake_engine.FakeRouter (API verified on llama.cpp b11723).

    python3 -m unittest -v tests.test_router
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import engines  # noqa: E402
from ctxproxy.core import Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.router import OWNERS_FILE  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream, UpstreamError  # noqa: E402
from tests.fake_engine import FakeEngine, FakeRouter  # noqa: E402
from tests.test_proxy import TOOLS, history  # noqa: E402

BIG, SMALL = 65536, 16384


class RouterBase(unittest.TestCase):
    models = {"big": BIG, "small": SMALL}
    raw_over: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.rt = FakeRouter(self.models, strict_window=True)
        self.url = self.rt.start()
        self.addCleanup(self.rt.stop)
        self.slot_dir = os.path.join(self.tmp.name, "slots")
        os.makedirs(self.slot_dir)
        # no window/thresholds in the config: they come from each model's n_ctx (the usual llama-server setup)
        self.raw = {"slot_dir": self.slot_dir, "slot_save": True, "autosave": True, "autosave_idle_s": 180,
                    "autosave_min_tokens": 1000, "autorestore_min_gain": 500, "autosave_min_free_gb": 0,
                    "min_mask_tokens": 200, "engine_tokenize": True, "data_dir": self.tmp.name, **self.raw_over}
        self.cfg = Config.from_dict(self.raw)
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        up = Upstream(self.url)
        j = Journal(self.jpath)
        self.tc = TokenCounter(3.5)
        self.engine = engines.setup(self.cfg, up, j, explicit=set(self.raw), counter=self.tc, log=None)
        self.proxy = Proxy(self.cfg, up, Store(os.path.join(self.tmp.name, "a.sqlite")), j, self.tc)
        self.proxy.engine = self.engine
        self.saver = self.proxy.enable_autosave(start=False)
        # the engine counts the prompt exactly as the proxy does (strict window: any overflow -> HTTP 400); children
        # are started on load and take the counter then
        self.rt.counter = lambda body: self.proxy.wguard.count_body(body)

    def tearDown(self):
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def send(self, msgs, model="big", expect=200, **kw):
        st, r, _ = self.proxy.chat({"model": model, "messages": msgs, "tools": TOOLS, "max_tokens": 1024, **kw})
        self.assertEqual(st, expect, r)
        return r

    def events(self, name):
        with open(self.jpath) as f:
            return [e for e in map(json.loads, f) if e["event"] == name]

    def violations(self):
        return [v for ch in self.rt.children.values() for v in ch.violations]

    def tick(self):
        ev = self.saver.tick(now=time.time() + 200)
        if ev:
            with open(os.path.join(self.slot_dir, ev["file"]), "wb") as f:
                f.write(b"x" * 16)
        return ev


class TestDetect(unittest.TestCase):
    def router(self, **kw):
        rt = FakeRouter({"a": 32768, "b": 16384}, **kw)
        url = rt.start()
        self.addCleanup(rt.stop)
        return rt, Upstream(url)

    def test_detected_as_llama_router(self):
        rt, up = self.router()
        e = engines.detect(up)
        self.assertEqual((e.kind, e.router, e.slot_save, e.tokenize, e.detected), ("llama.cpp", True, True, True, True))
        self.assertIsNone(e.n_ctx, "no window before a request names a model")
        self.assertEqual(rt.loads, [], "detection does not load any model")
        self.assertIn("router", e.summary())

    def test_forced_llama_cpp_still_finds_the_router(self):
        _, up = self.router()
        e = engines.detect(up, "llama.cpp")
        self.assertTrue(e.router)
        self.assertFalse(e.detected)

    def test_no_slot_save_path(self):
        _, up = self.router(slot_save=False)
        e = engines.detect(up)
        self.assertTrue(e.router)
        self.assertFalse(e.slot_save)

    def test_single_model_llama_unchanged(self):
        eng = FakeEngine(flavor="llama", n_ctx=16384)
        url = eng.start()
        self.addCleanup(eng.stop)
        e = engines.detect(Upstream(url))
        self.assertEqual((e.kind, e.router, e.n_ctx), ("llama.cpp", False, 16384))
        self.assertNotIn("router", e.summary(), "summary of the other engines unchanged")


class TestWindowPerModel(RouterBase):
    def test_window_follows_the_requested_model(self):
        h = history(4)
        self.send(h, "big")
        self.assertEqual(self.cfg.window, BIG)
        self.send(h, "small")
        self.assertEqual(self.cfg.window, SMALL)
        self.send(h, "big")
        self.assertEqual(self.cfg.window, BIG)
        ev = self.events("router_model")
        self.assertEqual([(e["model"], e["n_ctx"], e["window"]) for e in ev],
                         [("big", BIG, BIG), ("small", SMALL, SMALL), ("big", BIG, BIG)])
        # thresholds scaled from the startup values (131072 default) as engines.apply does for one model
        self.assertEqual(ev[1]["scaled"]["mask_trigger"], 10000)
        self.assertEqual(self.cfg.mask_trigger, 40000)
        self.assertEqual([m for m, _ in self.rt.chats], ["big", "small", "big"], "each call goes to its model")
        self.assertEqual(self.violations(), [])

    def test_switch_to_a_smaller_model_never_overflows(self):
        h = history(40)      # ~40K tokens: fits the big model, not the small one
        self.send(h, "big")
        big_prompt = self.events("request")[-1]["prompt_tokens"]
        self.assertGreater(big_prompt, SMALL, "the test needs a prompt larger than the small window")
        h2 = h + [{"role": "assistant", "content": "fatto"}, {"role": "user", "content": "continua"}]
        self.send(h2, "small")
        req = self.events("request")[-1]
        self.assertLessEqual(req["prompt_tokens"] + 1024, SMALL, req)
        self.assertEqual(self.violations(), [], "the strict engine saw no request over its n_ctx")
        self.assertEqual(self.proxy.wguard.violations, 0)

    def test_internal_calls_go_to_the_same_model(self):
        self.send(history(3), "small")
        self.assertTrue(all(b.get("model") == "small" for m, b in self.rt.chats))

    def test_tokenize_per_model(self):
        self.send(history(2), "big")
        self.send(history(2), "small")
        tok = [m for meth, p, m in self.rt.calls if p == "/tokenize"]
        self.assertIn("big", tok)
        self.assertIn("small", tok)
        self.assertNotIn(None, tok)

    def test_alias_and_router_model(self):
        self.send(history(1), "nope", expect=400)
        self.assertIn("not served", self.events("router_refused")[-1]["error"])
        self.assertEqual(self.rt.chats, [], "refused before anything is sent")
        self.cfg.router_model = "small"
        self.send(history(1), "nope")
        self.assertEqual(self.rt.chats[-1][0], "small")


class TestUnload(RouterBase):
    def test_model_unloaded_between_two_requests(self):
        h = history(4)
        self.send(h, "big")
        self.rt.unload("big")
        self.send(h + [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}], "big")
        self.assertEqual(self.rt.loads, ["big", "big"], "loaded again before the request")
        st = self.events("router_model_state")
        self.assertEqual((st[-1]["model"], st[-1]["status"]), ("big", "unloaded"))
        self.assertEqual(self.events("router_n_ctx")[-1]["generation"], 2, "new load generation")
        self.assertEqual(self.events("strata_state_lost")[-1]["reason"], "router_unload",
                         "no warm-cache assumption after an unload (router reason, 0.3.7)")
        self.assertEqual(self.violations(), [])

    def test_engine_status_bound_to_model_and_generation(self):
        self.send(history(2), "big")
        s1 = self.proxy.up.status()
        self.assertEqual((s1["loaded"], s1["started"]), (True, "big#1"))
        self.rt.unload("big")
        s2 = self.proxy.up.status()
        self.assertFalse(s2["loaded"], "status read with autoload=false: an unloaded model is not loaded again")
        self.assertEqual(self.rt.loads, ["big"])

    def test_reloaded_with_another_n_ctx_unseen(self):
        """Unloaded and loaded again with another ctx-size between two requests (the proxy never saw it
        unloaded): meta.n_ctx in /models changed -> new generation, n_ctx read again, smaller window applied."""
        self.send(history(2), "big")
        self.rt.unload("big")
        self.rt.n_ctx["big"] = SMALL
        self.rt.load("big")
        self.send(history(40), "big")
        self.assertEqual(self.cfg.window, SMALL)
        self.assertEqual(self.events("router_n_ctx")[-1]["generation"], 2)
        self.assertEqual(self.violations(), [])

    def test_n_ctx_unknown_is_a_clear_error(self):
        self.rt.fail.add("small")
        r = self.send(history(1), "small", expect=503)
        self.assertIn("was not sent", r["error"]["message"])
        self.assertEqual(self.rt.chats, [])
        self.assertEqual(self.events("router_refused")[-1]["status"], 503)


class TestSavedFiles(RouterBase):
    def test_restore_refused_with_another_model(self):
        self.send(history(2), "big")
        up = self.proxy.up.up          # Tracker -> Upstream
        up.slot("save", "x-big.bin")
        self.assertEqual(self.rt.saved_by["x-big.bin"], "big")
        self.send(history(2), "small")
        with self.assertRaises(UpstreamError) as cm:
            up.slot("restore", "x-big.bin")
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.rt.restores, [], "the engine would have restored it: the proxy stops it")
        with open(os.path.join(self.tmp.name, OWNERS_FILE)) as f:
            self.assertEqual(json.load(f)["x-big.bin"]["model"], "big", "owner persisted in the data folder")

    def test_unknown_file_is_not_restored(self):
        self.send(history(2), "big")
        self.rt.files["foreign.bin"] = "x"
        with self.assertRaises(UpstreamError) as cm:
            self.proxy.up.up.slot("restore", "foreign.bin")
        self.assertEqual(cm.exception.status, 409)

    def test_autorestore_only_with_the_same_model(self):
        h = history(6)
        self.send(h, "big")
        ev = self.tick()
        self.assertIsNotNone(ev, "autosave of the big model's slot")
        self.assertEqual(self.rt.saved_by[ev["file"]], "big")
        h2 = h + [{"role": "assistant", "content": "r"}, {"role": "user", "content": "continua"}]
        self.send(h2, "small")
        self.assertEqual(self.rt.restores, [], "another model never restores big's file")
        self.assertFalse(self.events("autorestore"))
        self.assertEqual(self.events("autorestore_other_model")[-1]["model"], "small")
        self.rt.unload("big")
        self.send(h2 + [{"role": "assistant", "content": "r2"}, {"role": "user", "content": "ancora"}], "big")
        self.assertEqual(self.rt.restores, [("big", ev["file"], "big")], "same model, reloaded: restored")
        self.assertEqual(len(self.events("autorestore")), 1)
        self.assertEqual(self.violations(), [])

    def test_prune_keeps_other_models_files(self):
        h = history(6)
        self.send(h, "big")
        ev_big = self.tick()
        h2 = h + [{"role": "assistant", "content": "r"}, {"role": "user", "content": "continua"}]
        self.send(h2, "small")
        ev_small = self.tick()
        self.assertIsNotNone(ev_small)
        self.assertEqual(self.rt.saved_by[ev_small["file"]], "small")
        files = {r["file"] for r in self.proxy.store.autosaves(active_only=True)}
        self.assertIn(ev_big["file"], files, "small's save does not supersede big's file (prefix, other model)")
        self.assertFalse([e for e in self.events("autosave_prune") if e["file"] == ev_big["file"]])


class ProxyHTTP(RouterBase):
    """The proxy behind its real HTTP handler (streaming paths)."""

    def setUp(self):
        super().setUp()
        from http.server import ThreadingHTTPServer
        from ctxproxy.server import make_handler
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.proxy))
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def post_stream(self, path, body, timeout):
        """-> list of (seconds since the request, raw SSE line) until the server closes; socket.timeout propagates
        (a client with a read timeout shorter than the silence gives up)."""
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        t0 = time.time()
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/event-stream"))
        out = []
        try:
            while True:
                line = r.fp.readline()
                if not line:
                    break
                if line.strip():
                    out.append((time.time() - t0, line.decode().rstrip("\n")))
        finally:
            c.close()
        return out

    def wait_event(self, name, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            ev = self.events(name)
            if ev:
                return ev
            time.sleep(0.05)
        self.fail("no %s in the journal" % name)


class TestStreamingLoad(ProxyHTTP):
    """0.3.7 router mode: a stream request to a model that is not loaded gets SSE comments while it loads (on
    AI395 qwen took 73 s from the USB disk and a client with a 60 s read timeout gave up)."""

    def setUp(self):
        super().setUp()
        self.rt.load_delay = 4.0   # > the client read timeout below (3.0 s): without keep-alive it fails
        self.cfg.router_keepalive_s = 0.3

    def test_short_read_timeout_survives_a_slow_load(self):
        lines = self.post_stream("/v1/chat/completions", {"model": "big", "messages": history(2), "tools": TOOLS,
                                                          "max_tokens": 256, "stream": True}, timeout=3.0)
        comments = [(t, x) for t, x in lines if x.startswith(":")]
        data = [(t, x) for t, x in lines if x.startswith("data:")]
        self.assertGreaterEqual(len(comments), 3, lines)
        self.assertIn(": loading model big", [x for _, x in comments])
        self.assertLess(comments[0][0], 3.0, "first sign of life before the client's read timeout")
        self.assertLess(max(t for t, _ in comments), data[0][0], "comments only before the answer")
        self.assertEqual(data[-1][1], "data: [DONE]")
        self.assertTrue(any('"finish_reason"' in x for _, x in data))
        self.assertEqual(self.rt.loads, ["big"])
        self.assertEqual(len(self.rt.chats), 1)
        self.assertEqual(self.cfg.window, BIG, "window read from the n_ctx before the prompt was built")
        ka = self.events("router_load_keepalive")
        self.assertEqual(ka[-1]["model"], "big")
        self.assertGreaterEqual(ka[-1]["keepalives"], 3)
        self.assertEqual(self.violations(), [])

    def test_loaded_model_gets_no_comment(self):
        self.rt.load("big")
        lines = self.post_stream("/v1/chat/completions", {"model": "big", "messages": history(1), "tools": TOOLS,
                                                          "max_tokens": 256, "stream": True}, timeout=5)
        self.assertFalse([x for _, x in lines if x.startswith(": loading")], "nothing changes when it is loaded")
        self.assertFalse(self.events("router_load_keepalive"))

    def test_client_gone_during_the_load_nothing_generated(self):
        import socket
        import struct
        body = json.dumps({"model": "big", "messages": history(2), "tools": TOOLS, "max_tokens": 256,
                           "stream": True}).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.assertIn(b"200", s.recv(4096))
        time.sleep(0.4)
        linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)   # RST: the client is gone
        s.close()
        ev = self.wait_event("router_client_gone")[-1]
        self.assertEqual(ev["model"], "big")
        self.assertGreaterEqual(ev["load_s"], 3.9, "the load is waited for before the lock is released")
        self.assertTrue(self.proxy.lock.acquire(timeout=3), "lock released")
        self.proxy.lock.release()
        time.sleep(0.3)
        self.assertEqual(self.rt.chats, [], "nothing generated for a client that went away")
        self.assertFalse(self.events("request"))
        # the next request is served normally (model loaded, window known)
        self.send(history(2), "big")
        self.assertEqual(self.rt.loads, ["big"])

    def test_anthropic_stream_pings_while_loading(self):
        lines = self.post_stream("/v1/messages", {"model": "big", "max_tokens": 256, "stream": True,
                                                   "messages": [{"role": "user", "content": "ciao"}]}, timeout=3.0)
        evs = [x for _, x in lines if x.startswith("event:")]
        self.assertGreaterEqual(evs.count("event: ping"), 3, lines)
        self.assertEqual(evs[-1], "event: message_stop")
        self.assertEqual(len(self.rt.chats), 1)

    def test_responses_stream_comments_while_loading(self):
        lines = self.post_stream("/v1/responses", {"model": "big", "input": "ciao", "stream": True,
                                                    "max_output_tokens": 256}, timeout=3.0)
        self.assertIn(": loading model big", [x for _, x in lines])
        self.assertTrue(any("response.completed" in x for _, x in lines), lines)
        self.assertEqual(len(self.rt.chats), 1)

    def test_non_stream_unchanged(self):
        """Non-stream requests: no protocol change, the client's timeout must exceed the load time (README)."""
        r = self.send(history(1), "big")
        self.assertEqual(r["choices"][0]["message"]["role"], "assistant")
        self.assertFalse(self.events("router_load_keepalive"))


class TestSaveEstimate(RouterBase):
    """0.3.7: disk needed for an autosave = tokens x bytes/token measured on the last save of the same model (x 1.25);
    20000 bytes/token until the model has saved once (on AI395: qwen 37-73 KB/token, gemma 7.6-9 KB/token)."""

    def setUp(self):
        super().setUp()
        self.rt.bytes_per_token = {"big": 70000, "small": 9000}

    def test_estimate_per_model(self):
        h = history(6)
        self.send(h, "big")
        e1 = self.tick()
        self.assertEqual((e1["est_bpt"], e1["est_from"]), (20000, "default"), "first save: the old value")
        self.assertEqual(e1["bytes"], e1["n_saved"] * 70000)
        h2 = h + [{"role": "assistant", "content": "r"}, {"role": "user", "content": "continua"}]
        self.send(h2, "big")
        e2 = self.tick()
        self.assertEqual((e2["est_bpt"], e2["est_from"]), (87500, e1["file"]), "big: 70000 x 1.25")
        self.assertEqual(e2["est_bytes"], int(e2["tokens"] * 87500))
        h3 = h2 + [{"role": "assistant", "content": "r2"}, {"role": "user", "content": "ancora"}]
        self.send(h3, "small")
        e3 = self.tick()
        self.assertEqual(e3["est_from"], "default", "small has no measure of its own: big's is not used")
        h4 = h3 + [{"role": "assistant", "content": "r3"}, {"role": "user", "content": "e poi"}]
        self.send(h4, "small")
        e4 = self.tick()
        self.assertEqual((e4["est_bpt"], e4["est_from"]), (11250, e3["file"]), "small: 9000 x 1.25")

    def test_disk_low_uses_the_measure(self):
        from collections import namedtuple
        from unittest import mock
        h = history(6)
        self.send(h, "big")
        e1 = self.tick()
        h2 = h + [{"role": "assistant", "content": "r"}, {"role": "user", "content": "continua"}]
        self.send(h2, "big")
        tokens = self.proxy.up.state["tokens"]
        du = namedtuple("du", "total used free")
        with mock.patch("ctxproxy.autosave.shutil.disk_usage", return_value=du(0, 0, tokens * 40000)):
            self.assertIsNone(self.tick(), "40 KB/token free: enough for the old 20000, not for big's 87500")
        skip = self.events("autosave_skip")[-1]
        self.assertEqual((skip["reason"], skip["est_bpt"], skip["est_from"]), ("disk_low", 87500, e1["file"]))


class TestRouterReasons(RouterBase):
    """0.3.7: the state lost through a model change or an unload has its own reason in router mode."""

    def test_model_change_and_unload(self):
        h = history(4)
        self.send(h, "big")
        h2 = h + [{"role": "assistant", "content": "r"}, {"role": "user", "content": "continua"}]
        self.send(h2, "small")
        self.assertEqual(self.events("strata_state_lost")[-1]["reason"], "router_model_change")
        h3 = h2 + [{"role": "assistant", "content": "r2"}, {"role": "user", "content": "ancora"}]
        self.send(h3, "small")
        self.rt.unload("small")
        self.send(h3 + [{"role": "assistant", "content": "r3"}, {"role": "user", "content": "e poi"}], "small")
        self.assertIn(self.events("strata_state_lost")[-1]["reason"], ("router_unload",))
        reasons = {e["reason"] for e in self.events("strata_state_lost")}
        self.assertFalse(reasons & {"strata_restart", "unloaded"}, reasons)

    def test_restore_reason(self):
        h = history(6)
        self.send(h, "big")
        self.assertIsNotNone(self.tick())
        self.rt.unload("big")
        self.send(h + [{"role": "assistant", "content": "r"}, {"role": "user", "content": "continua"}], "big")
        ev = self.events("autorestore")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[-1]["reason"], "router_unload")


class TestSingleModelUnchanged(unittest.TestCase):
    """Outside router mode the journal is the same as before: no estimate fields, the old reasons."""

    def test_autosave_and_reasons(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        eng = FakeEngine(flavor="llama", n_ctx=BIG)
        url = eng.start()
        self.addCleanup(eng.stop)
        slot_dir = os.path.join(tmp.name, "slots")
        os.makedirs(slot_dir)
        raw = {"slot_dir": slot_dir, "slot_save": True, "autosave": True, "autosave_idle_s": 180,
               "autosave_min_tokens": 1000, "autosave_min_free_gb": 0, "data_dir": tmp.name}
        cfg = Config.from_dict(raw)
        jpath = os.path.join(tmp.name, "j.jsonl")
        up, j, tc = Upstream(url), Journal(jpath), TokenCounter(3.5)
        e = engines.setup(cfg, up, j, explicit=set(raw), counter=tc, log=None)
        store = Store(os.path.join(tmp.name, "a.sqlite"))
        self.addCleanup(store.db.close)
        p = Proxy(cfg, up, store, j, tc)
        p.engine = e
        saver = p.enable_autosave(start=False)
        st, r, _ = p.chat({"model": "x", "messages": history(6), "tools": TOOLS, "max_tokens": 1024})
        self.assertEqual(st, 200, r)
        ev = saver.tick(now=time.time() + 200)
        self.assertIsNotNone(ev)
        self.assertFalse([k for k in ev if k.startswith("est_")], ev)
        p.up._set({**p.up.state, "started": "other"})
        p.up.check(where="test")
        with open(jpath) as f:
            lost = [x for x in map(json.loads, f) if x["event"] == "strata_state_lost"]
        self.assertEqual(lost[-1]["reason"], "strata_restart")


if __name__ == "__main__":
    unittest.main()
