"""Protocol evidence, not a version string or a gateway catalog (CPU-only HTTP fixtures)."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

from ctxproxy import engines
from ctxproxy.enginectx import detect_flavor, loaded_contexts
from ctxproxy.upstream import Upstream

VERSION = {"build_date": "2026-06-21T20:57:28Z", "commit": "316ad63f765365c7ea100a7270de12eeffc1190f", "version": "229"}
CATALOG = {"data": [{"id": x} for x in ("granite-4-h-small", "qwen3-coder", "qwen3-coder-next", "qwen3.6-35b-mtp")]}


@contextmanager
def gateway(ps=(404, b"404 page not found")):
    requests = []
    routes = {"/health": (200, {"status": "ok"}), "/v1/models": (200, CATALOG),
              "/api/version": (200, VERSION), "/api/ps": ps}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path))
            status, body = routes.get(self.path, (404, b"404 page not found"))
            body = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            requests.append((self.command, self.path))
            self.send_error(405)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class TestProtocolEvidence(unittest.TestCase):
    def up(self, ps):
        up = Mock()

        def raw(method, path, *args, **kwargs):
            self.assertEqual(method, "GET")
            if path == "/api/version":
                return 200, {}, json.dumps(VERSION).encode()
            if path == "/api/ps":
                if isinstance(ps, Exception):
                    raise ps
                return ps
            return 404, {}, b"not found"

        up.raw.side_effect = raw
        return up

    def test_version_without_coherent_ps_is_not_ollama(self):
        invalid = [(404, {}, b"404 page not found"), (500, {}, b"error"),
                   (200, {}, b"not JSON"), TimeoutError("simulated"),
                   *[(200, {}, json.dumps(x).encode()) for x in
                     ({}, {"models": None}, {"models": {}}, {"models": "oops"},
                      {"models": [None]}, {"models": [{}]}, {"models": [{"name": []}]})]]
        for ps in invalid:
            with self.subTest(ps=ps):
                self.assertIsNone(detect_flavor(self.up(ps)))
                self.assertIsNone(loaded_contexts(self.up(ps), "ollama"))

    def test_unsupported_ps_does_not_hide_lmstudio(self):
        up = self.up((404, {}, b"not found"))
        original = up.raw.side_effect

        def raw(method, path, *args, **kwargs):
            if path == "/api/v0/models":
                return 200, {}, b'{"data":[{"id":"qwen","state":"loaded","loaded_context_length":32768}]}'
            return original(method, path, *args, **kwargs)

        up.raw.side_effect = raw
        self.assertEqual(detect_flavor(up), "lmstudio")

    def test_real_ollama_empty_and_loaded(self):
        for models, expected in (([], {}), ([{"name": "qwen:latest", "context_length": 32768}],
                                           {"qwen:latest": 32768}),
                                 ([{"model": "qwen", "num_ctx": 16384}], {"qwen": 16384})):
            with self.subTest(models=models):
                ps = (200, {}, json.dumps({"models": models}).encode())
                self.assertEqual(detect_flavor(self.up(ps)), "ollama")
                self.assertEqual(loaded_contexts(self.up(ps), "ollama"), expected)

    def test_detect_distinguishes_empty_unknown_and_unreported_context(self):
        for ps, text in (((200, {}, b'{"models":[]}'), "no model loaded"),
                         ((200, {}, b'{"models":[{"name":"qwen"}]}'), "context not reported")):
            with self.subTest(ps=ps):
                eng = engines.detect(self.up(ps))
                self.assertEqual(eng.flavor, "ollama")
                self.assertIn(text, " ".join(eng.notes))
        # Protocol confirmed once; a later read can fail independently.
        up = self.up((200, {}, b'{"models":[]}'))
        original = up.raw.side_effect
        reads = 0

        def changing(method, path, *args, **kwargs):
            nonlocal reads
            if path == "/api/ps":
                reads += 1
                if reads > 1:
                    return 503, {}, b"unavailable"
            return original(method, path, *args, **kwargs)

        up.raw.side_effect = changing
        eng = engines.detect(up)
        self.assertEqual(eng.flavor, "ollama")
        self.assertIn("unavailable", " ".join(eng.notes))
        self.assertNotIn("no model loaded", " ".join(eng.notes))


class TestGatewayCheck(unittest.TestCase):
    def cli(self, url, folder, config=None):
        args = ["check", "--upstream", url, "--port", "0", "--data", folder, "--json"]
        if config is not None:
            path = os.path.join(folder, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config, f)
            args += ["--config", path]
        # Optional installed candidate entrypoint, launched outside the source tree.
        command = shlex.split(os.environ.get("MNEMONIC_CANDIDATE_COMMAND", "")) or [sys.executable, "-m", "ctxproxy.server"]
        env = os.environ.copy()
        if "MNEMONIC_CANDIDATE_COMMAND" in env:
            env.pop("PYTHONPATH", None)
        result = subprocess.run(command + args, cwd=folder if "MNEMONIC_CANDIDATE_COMMAND" in env else None,
                                env=env, text=True, capture_output=True, timeout=30)
        self.assertIn(result.returncode, (0, 1, 2), result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["exit_code"], result.returncode)
        return report

    def test_gateway_catalog_does_not_prove_loaded_or_context(self):
        with gateway() as (url, requests), tempfile.TemporaryDirectory() as folder:
            eng = engines.detect(Upstream(url))
            self.assertEqual((eng.kind, eng.flavor, eng.n_ctx), ("openai", None, None))
            for cfg in (None, {"window": 32768}):
                report = self.cli(url, folder, cfg)
                self.assertEqual(report["exit_code"], 2, report)
                items = report["checks"]
                self.assertFalse(any(i["check"] == "model loaded" and i["status"] == "ok" for i in items))
                self.assertTrue(any(i["check"] == "model availability" and "catalog" in i["evidence"]
                                    and i["status"] == "warn" for i in items))
                window = next(i for i in items if i["check"] == "context window")
                self.assertEqual(window["status"], "warn")
                self.assertIn("unverified", window["evidence"])
                self.assertIn("configured" if cfg else "default", window["evidence"])
                self.assertNotIn("OLLAMA_CONTEXT_LENGTH", json.dumps(report))
                self.assertNotIn("ollama run", json.dumps(report))
            self.assertTrue(requests)
            self.assertTrue(all(method == "GET" for method, _ in requests), requests)
            self.assertTrue(all(path in {"/health", "/v1/models", "/v1/status", "/props",
                                         "/api/version", "/api/ps", "/api/v0/models"}
                                for _, path in requests), requests)

    def test_malformed_and_error_ps_remain_generic_without_loading(self):
        for ps in ((200, {}), (200, b"not JSON"), (500, b"error")):
            with self.subTest(ps=ps), gateway(ps) as (url, requests), tempfile.TemporaryDirectory() as folder:
                report = self.cli(url, folder)
                self.assertEqual(report["exit_code"], 2)
                self.assertTrue(any(i["check"] == "model availability" for i in report["checks"]))
                self.assertTrue(all(method == "GET" for method, _ in requests), requests)

    def test_small_window_guard_still_fails(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {engines.ALLOW_SMALL_WINDOW_ENV: ""}):
            with gateway() as (url, requests), tempfile.TemporaryDirectory() as folder:
                report = self.cli(url, folder, {"window": 8192})
                self.assertEqual(report["exit_code"], 1)
                self.assertTrue(any(i["check"] == "context window" and i["status"] == "fail"
                                    for i in report["checks"]))
                self.assertTrue(all(method == "GET" for method, _ in requests), requests)

    def test_ollama_runtime_not_catalog_is_loaded_evidence(self):
        for models, status, text in (([], "warn", "no model loaded"),
                                     ([{"name": "qwen", "context_length": 32768}], "ok", "qwen"),
                                     ([{"name": "qwen"}], "ok", "qwen")):
            with self.subTest(models=models), gateway((200, {"models": models})) as (url, requests), \
                    tempfile.TemporaryDirectory() as folder:
                report = self.cli(url, folder)
                loaded = next(i for i in report["checks"] if i["check"] == "model loaded")
                self.assertEqual(loaded["status"], status)
                self.assertIn(text, loaded["evidence"])
                window = next(i for i in report["checks"] if i["check"] == "context window")
                self.assertEqual(window["status"], "ok" if models and "context_length" in models[0] else "warn")
                self.assertTrue(all(method == "GET" for method, _ in requests), requests)


@contextmanager
def lmstudio(models):
    """An LM Studio gateway: /api/v0/models with `state`, no /api/ps, no /api/version."""
    requests = []
    routes = {"/health": (200, {"status": "ok"}), "/v1/models": (200, CATALOG),
              "/api/v0/models": (200, {"data": models})}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path))
            status, body = routes.get(self.path, (404, b"404 page not found"))
            body = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            requests.append((self.command, self.path))
            self.send_error(405)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class TestRecognisedEngineEvidence(unittest.TestCase):
    """The same evidence rule for the other recognised flavour, and the guards it must not weaken."""

    cli = TestGatewayCheck.cli

    def test_lmstudio_runtime_is_the_loaded_state_evidence(self):
        for models, status, text in (
                ([{"id": "qwen", "state": "not-loaded"}], "warn", "no model loaded"),
                ([{"id": "qwen", "state": "loaded", "loaded_context_length": 32768}], "ok", "qwen")):
            with self.subTest(models=models), lmstudio(models) as (url, requests), \
                    tempfile.TemporaryDirectory() as folder:
                report = self.cli(url, folder)
                loaded = next(i for i in report["checks"] if i["check"] == "model loaded")
                self.assertEqual(loaded["status"], status)
                self.assertIn(text, loaded["evidence"])
                self.assertIn("/api/v0/models", loaded["evidence"])
                # LM Studio's own endpoint and commands; never Ollama's, and never the catalog as proof
                blob = json.dumps(report)
                for ollama_only in ("ollama run", "ollama ps", "/api/ps", "OLLAMA_CONTEXT_LENGTH"):
                    self.assertNotIn(ollama_only, blob)
                self.assertTrue(all(method == "GET" for method, _ in requests), requests)

    def test_a_configured_window_alone_is_never_called_verified(self):
        """Flavour recognised, nothing loaded: the configured window stays unverified (config is not evidence)."""
        with gateway((200, {"models": []})) as (url, _), tempfile.TemporaryDirectory() as folder:
            report = self.cli(url, folder, {"window": 32768})
            window = next(i for i in report["checks"] if i["check"] == "context window")
            self.assertEqual(window["status"], "warn")
            self.assertIn("not a verified runtime context", window["evidence"])
            self.assertEqual(report["exit_code"], 2, report)

    def test_the_known_context_mismatch_guard_still_fails(self):
        """A config window above the runtime context the engine reports is still a failure, not a warning."""
        with gateway((200, {"models": [{"name": "qwen", "context_length": 32768}]})) as (url, _), \
                tempfile.TemporaryDirectory() as folder:
            report = self.cli(url, folder, {"window": 65536})
            self.assertEqual(report["exit_code"], 1, report)
            self.assertTrue(any(i["check"] == "context window" and i["status"] == "fail"
                                for i in report["checks"]), report)

    def test_a_matching_runtime_context_is_still_verified(self):
        """The fix must not call a correct Ollama setup unverified: a measured context is still OK, never a failure."""
        with gateway((200, {"models": [{"name": "qwen", "context_length": 32768}]})) as (url, _), \
                tempfile.TemporaryDirectory() as folder:
            report = self.cli(url, folder, {"window": 32768})
            windows = [i for i in report["checks"] if i["check"] == "context window"]
            self.assertTrue(windows, report)
            self.assertTrue(all(i["status"] == "ok" for i in windows), report)
            self.assertIn("Ollama context=32768", windows[0]["evidence"])
            self.assertFalse([i for i in report["checks"] if i["status"] == "fail"], report)


if __name__ == "__main__":
    unittest.main()
