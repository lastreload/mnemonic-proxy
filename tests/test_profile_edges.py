"""Independent CPU regressions for profile lifecycle and public HTTP paths."""
import contextlib
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from ctxproxy import server
from ctxproxy.check import run_checks
from ctxproxy.core import Config, rid_for
from ctxproxy.profiles import load_profiles
from ctxproxy.upstream import UpstreamError
from tests.test_model_profiles import ProfileBase, free_port
from tests.test_proxy import history


class TestProfileEdges(ProfileBase):
    def test_missing_model_does_not_match_wildcard(self):
        self.build({"profiles": {"*": {}}})
        for bad in (None, "", 123, {}, []):
            with self.subTest(model=bad):
                self.send(bad, expect=400)
        self.assertFalse(self.engine.requests)

    def test_profile_estimate_override(self):
        self.build({"profiles": {"alpha": {"chars_per_token": 2}, "beta": {"chars_per_token": 5}}})
        self.send("alpha")
        self.send("beta")
        self.assertEqual(self.proxy.contexts["alpha"].tc.raw("x" * 100), 50)
        self.assertEqual(self.proxy.contexts["beta"].tc.raw("x" * 100), 20)

    def test_fallback_window_does_not_inflate_default_thresholds(self):
        self.build({"profiles": {"alpha": {"window": 16384}, "beta": {"window": 262144}}})
        self.send("alpha")
        self.send("beta")
        self.assertEqual(self.proxy.contexts["alpha"].cfg.mask_trigger, 20000)
        self.assertEqual(self.proxy.contexts["beta"].cfg.mask_trigger, 40000)
        self.send("alpha")
        self.assertEqual(self.proxy.contexts["alpha"].cfg.mask_trigger, 20000)

    def test_child_lock_supports_locked_on_all_supported_python_versions(self):
        self.build({"profiles": {"alpha": {}}})
        self.send("alpha")
        lock = self.proxy.contexts["alpha"].lock
        self.assertFalse(lock.locked())
        with lock:
            self.assertTrue(lock.locked())
        self.assertIsNot(lock, self.proxy.lock)

    def test_activate_valid_model_from_too_small_unrelated_model(self):
        self.engine.models["alpha"] = 8192
        self.engine.n_ctx = 8192
        self.build({"profiles": {"beta": {"window": 65536}}})
        self.send("beta")
        self.assertEqual(self.engine.active_model, "beta")
        self.assertEqual(self.proxy.contexts["beta"].cfg.window, 65536)

    def test_external_model_switch_is_detected_before_window_refresh(self):
        self.build({"profiles": {"alpha": {}, "beta": {}}})
        self.send("beta")
        self.engine.active_model = "alpha"
        self.engine.n_ctx = 32768
        self.engine.live = ""
        self.send("beta")
        self.assertEqual(self.proxy.contexts["beta"].cfg.window, 65536)
        self.assertEqual(self.engine.active_model, "beta")

    def test_empty_or_malformed_profiles_never_enable_legacy_silently(self):
        for profiles in ({}, [], None, "alpha"):
            raw = {"profiles": profiles}
            cfg = Config.from_dict(raw)
            args = SimpleNamespace(data=self.tmp.name, tokenizer=None, upstream=self.url, api_key="", config=None)
            with self.subTest(profiles=profiles), patch.object(server, "Upstream") as upstream:
                with self.assertRaises(ValueError):
                    server.serve(args, raw, cfg)
                upstream.assert_not_called()
                report = run_checks(args, raw, cfg)
                self.assertEqual(report.exit_code, 1)

    def test_startup_does_not_validate_unrelated_active_model_window(self):
        self.engine.n_ctx = 8192
        raw = {"profiles": {"beta": {"window": 65536}}}
        cfg = Config.from_dict(raw)
        args = SimpleNamespace(data=self.tmp.name, tokenizer=None, upstream=self.url, api_key="", mode="tools",
                               host="127.0.0.1", port=free_port())
        class Listener:
            def __init__(self, *a):
                pass
            def serve_forever(self):
                raise KeyboardInterrupt
            def server_close(self):
                pass
        with patch.object(server, "ThreadingHTTPServer", Listener), contextlib.redirect_stderr(io.StringIO()):
            server.serve(args, raw, cfg)

    def test_check_static_defers_unrelated_window_and_live_reads_target_window(self):
        self.engine.models["alpha"] = 8192
        self.engine.n_ctx = 8192
        raw = {"profiles": {"beta": {"window": 16384}}}
        args = SimpleNamespace(data=self.tmp.name, tokenizer=None, upstream=self.url, api_key="", config=None,
                               host="127.0.0.1", port=free_port())
        static = run_checks(args, raw, Config.from_dict(raw))
        self.assertNotEqual(static.exit_code, 1, static.items)
        self.assertEqual(self.engine.active_model, "alpha")
        self.assertFalse(self.engine.requests, "static check must not load a model")
        live = run_checks(args, raw, Config.from_dict(raw), live=True)
        self.assertNotEqual(live.exit_code, 1, live.items)
        self.assertTrue(any("65536" in i["evidence"] for i in live.items if i["check"] == "context window"))
        self.assertEqual(self.engine.active_model, "beta")

    def test_saved_ownership_and_archive_survive_restart_without_loading_model(self):
        raw = {"slot_save": True, "profiles": {"alpha": {}, "beta": {}}}
        self.build(raw)
        h = history(2)
        conv = self.send("alpha", h)["strata_context"]["conversation_id"]
        child = self.proxy.contexts["alpha"]
        child.up.slot("save", "alpha-test.bin")
        self.proxy.close()
        self.proxy = None
        self.build(raw)
        count = len(self.engine.requests)
        self.assertTrue(self.proxy.store_for(conv=conv).has_conv(conv))
        self.assertIsNotNone(self.proxy.store_for(rid=rid_for(1, h[1])).get(rid_for(1, h[1])))
        self.assertEqual(self.proxy.marks(conv)["conversation_id"], conv)
        self.assertFalse(self.proxy.contexts, "archive reads must not instantiate a model runtime")
        self.assertEqual(len(self.engine.requests), count)
        self.send("beta")
        with self.assertRaises(UpstreamError) as caught:
            self.proxy.contexts["beta"].up.slot("restore", "alpha-test.bin")
        self.assertEqual(caught.exception.status, 409)
        resumed = self.send("alpha", h)["strata_context"]["conversation_id"]
        self.assertEqual(resumed, conv)
        self.proxy.contexts["alpha"].up.slot("restore", "alpha-test.bin")
        self.assertIn(("alpha", "alpha-test.bin", "alpha"), self.engine.restores)

    def test_public_http_unknown_streaming_and_model_switch(self):
        self.build({"profiles": {"alpha": {}, "beta": {}}})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.proxy))
        srv.daemon_threads = True
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/v1/chat/completions" % srv.server_port
            def call(model, stream=False, path="/v1/chat/completions"):
                body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hello"}],
                                   "max_tokens": 128, "stream": stream}).encode()
                endpoint = "http://127.0.0.1:%d%s" % (srv.server_port, path)
                req = urllib.request.Request(endpoint, body, {"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status, r.read()
                except urllib.error.HTTPError as e:
                    return e.code, e.read()
            for model in ("alpha", "beta", "alpha"):
                st, body = call(model)
                self.assertEqual(st, 200, body)
                self.assertEqual(json.loads(body)["model"], model)
            for path in ("/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens", "/v1/responses"):
                for stream in (False, True):
                    st, body = call("unknown", stream, path)
                    self.assertEqual(st, 400, body)
                    self.assertIn(b"configured profiles", body)
            self.proxy.default_profile = "alpha"
            st, body = call("unknown")
            self.assertEqual(st, 200, body)
            self.assertEqual(json.loads(body)["model"], "alpha")
            st, body = call("beta", True)
            self.assertEqual(st, 200, body)
            self.assertIn(b"data: [DONE]", body)
            self.assertIn(b'"model": "beta"', body)
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join()
