"""`check` against an engine that wants an API key (Unsloth's `unsloth run`, gateways).

Found with Unsloth 2026.11.3: detection already sent `--api-key`, but the reachability probe did not, so `check`
printed "/v1/models 401" and could not list the loaded model from it. Every probe must send the key.

Small local HTTP server, no model, CPU, under a second.
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import check  # noqa: E402

KEY = "sk-unsloth-test-0000"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _AuthEngine(BaseHTTPRequestHandler):
    """llama-server-like answers, 401 without `Authorization: Bearer KEY` (like Unsloth's API)."""

    def log_message(self, format, *args):  # noqa: A002 (quiet)
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + KEY:
            return self._send(401, {"error": {"message": "Invalid API key", "code": 401}})
        if self.path.startswith("/v1/models"):
            return self._send(200, {"object": "list", "data": [{"id": "tiny-model", "object": "model"}]})
        if self.path.startswith("/props"):
            return self._send(200, {"default_generation_settings": {"n_ctx": 32768}, "model_path": "/m/tiny-model.gguf",
                                    "total_slots": 1})
        if self.path.startswith("/slots"):
            return self._send(200, [{"id": 0, "n_ctx": 32768, "is_processing": False}])
        return self._send(404, {"detail": "API endpoint not found"})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + KEY:
            return self._send(401, {"error": {"message": "Invalid API key", "code": 401}})
        return self._send(404, {"detail": "API endpoint not found"})


class CheckSendsApiKey(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _AuthEngine)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def run_check(self, *extra):
        argv = ["--upstream", self.url, "--port", str(free_port()), "--data", os.path.join(self.tmp.name, "data"),
                "--json", *extra]
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            check.main(argv)
        return {c["check"]: c for c in json.loads(out.getvalue())["checks"]}

    def test_reachability_probe_sends_the_key(self):
        res = self.run_check("--api-key", KEY)
        self.assertIn("/v1/models 200", json.dumps(res["engine reachable"]))
        self.assertIn("llama.cpp", json.dumps(res["engine type"]))
        self.assertIn("32768", json.dumps(res["context window"]))

    def test_without_the_key_the_engine_says_401(self):
        res = self.run_check()
        self.assertIn("/v1/models 401", json.dumps(res["engine reachable"]))


if __name__ == "__main__":
    unittest.main()
