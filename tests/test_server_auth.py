"""Il proxy dietro una chiave API (`--client-key-file`): 401 senza chiave, 200 con Authorization o x-api-key.
   python3 -m unittest -v tests.test_server_auth"""
from __future__ import annotations

import http.client
import io
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.server import authorized, main, make_handler  # noqa: E402
from tests.test_proxy import TOOLS, Base, history  # noqa: E402

KEY = "sekret"


def serve(proxy, client_key=""):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(proxy, client_key))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def call(base, path, body=None, **hdr):
    req = urllib.request.Request(base + path, data=body, headers=hdr,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()


class AuthTests(Base):
    def serve_with_key(self):
        return serve(self.proxy, KEY)

    def test_open_proxy_needs_no_key(self):
        srv, base = serve(self.proxy)
        try:
            self.assertEqual(call(base, "/health")[0], 200)
            body = json.dumps({"model": "m", "messages": history(1), "tools": TOOLS}).encode()
            self.assertEqual(call(base, "/v1/chat/completions", body, **{"Content-Type": "application/json"})[0], 200)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_key_required_refuses_every_path(self):
        srv, base = self.serve_with_key()
        try:
            chat = json.dumps({"model": "m", "messages": history(1), "tools": TOOLS}).encode()
            paths = [("/health", None), ("/v1/engine", None), ("/v1/strata/journal", None),
                     ("/v1/strata/conversations/conv1", None), ("/v1/strata/marks/conv1", None),
                     ("/v1/chat/completions", chat), ("/v1/messages", chat), ("/v1/responses", chat),
                     ("/v1/strata/marks", b'{"conv": "conv1"}'), ("/v1/strata/saved-state/reset", b"{}")]
            for path, body in paths:
                st, data = call(base, path, body)
                self.assertEqual(st, 401, "%s ha risposto %s: %s" % (path, st, data))
                self.assertEqual(json.loads(data)["error"]["type"], "authentication_error", path)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_both_client_headers_pass(self):
        srv, base = self.serve_with_key()
        try:
            chat = json.dumps({"model": "m", "messages": history(1), "tools": TOOLS}).encode()
            for hdr in ({"Authorization": "Bearer " + KEY}, {"Authorization": "bearer " + KEY},
                        {"x-api-key": KEY}):
                self.assertEqual(call(base, "/health", None, **hdr)[0], 200, hdr)
                self.assertEqual(call(base, "/v1/chat/completions", chat,
                                      **{"Content-Type": "application/json", **hdr})[0], 200, hdr)
            # il body di una richiesta rifiutata è stato letto: la connessione keep-alive resta allineata
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
            conn.request("POST", "/v1/chat/completions", chat, {"Content-Type": "application/json"})
            refused = conn.getresponse()
            self.assertEqual(refused.status, 401)
            refused.read()               # body consumato: solo così la connessione resta riutilizzabile
            conn.request("GET", "/health", None, {"Authorization": "Bearer " + KEY})
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_wrong_key_refused(self):
        srv, base = self.serve_with_key()
        try:
            for hdr in ({"Authorization": "Bearer wrong"}, {"Authorization": KEY},
                       {"Authorization": "Bearer " + KEY.upper()}, {"x-api-key": "wrong"}, {}):
                self.assertEqual(call(base, "/health", None, **hdr)[0], 401, hdr)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_authorized(self):
        self.assertTrue(authorized({}, ""))                      # proxy aperto: niente chiave
        self.assertFalse(authorized({}, KEY))
        self.assertTrue(authorized({"Authorization": "Bearer " + KEY}, KEY))
        self.assertTrue(authorized({"Authorization": "bearer " + KEY}, KEY))
        self.assertTrue(authorized({"Authorization": "Bearer  " + KEY + " "}, KEY))
        self.assertTrue(authorized({"x-api-key": KEY}, KEY))
        self.assertFalse(authorized({"Authorization": "Basic " + KEY}, KEY))
        self.assertFalse(authorized({"Authorization": "Bearer "}, KEY))
        # intestazione non ASCII: confrontato in bytes, non un TypeError

    def test_non_ascii_header(self):
        self.assertFalse(authorized({"Authorization": "Bearer caffè"}, KEY))
        self.assertFalse(authorized({"x-api-key": "café"}, KEY))

        self.assertFalse(authorized({"Authorization": "Bearer "}, KEY))


class CliTests(Base):
    """`--client-key-file` attiva il controllo sulla chiave dei clienti; un bind non loopback senza chiave
    parte aperto con un warning (`--host 0.0.0.0` funziona come prima)."""

    def run_main(self, *extra):
        from contextlib import redirect_stderr
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        out = io.StringIO()
        argv = ["--upstream", self.url, "--port", str(port), "--data", os.path.join(self.tmp.name, "d"), *extra]
        with mock.patch("ctxproxy.server.ThreadingHTTPServer"), redirect_stderr(out):
            try:
                main(argv)
            except SystemExit as e:
                return str(e.code), out.getvalue()
        return None, out.getvalue()

    def test_non_loopback_without_a_key_starts_open_with_a_warning(self):
        code, err = self.run_main("--host", "0.0.0.0")
        self.assertIsNone(code)
        self.assertIn("warning", err)
        self.assertIn("--client-key-file", err)
        self.assertIn("auth=open", err)

    def test_loopback_stays_open(self):
        code, err = self.run_main("--host", "localhost")
        self.assertIsNone(code)
        self.assertIn("auth=open", err)

    def test_client_key_file_is_the_key_checked(self):
        path = os.path.join(self.tmp.name, "client.key")
        with open(path, "w", encoding="utf-8") as f:
            f.write("client-secret\n")
        srv, base = serve(self.proxy, "client-secret")
        try:
            self.assertEqual(call(base, "/health", None, **{"Authorization": "Bearer client-secret"})[0], 200)
            self.assertEqual(call(base, "/health", None, **{"Authorization": "Bearer " + KEY})[0], 401)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_client_key_file_alone_activates_the_check(self):
        path = os.path.join(self.tmp.name, "client.key")
        with open(path, "w", encoding="utf-8") as f:
            f.write("client-secret\n")
        code, err = self.run_main("--client-key-file", path)
        self.assertIsNone(code)
        self.assertIn("api key required", err)

    def test_empty_client_key_file_refuses(self):
        path = os.path.join(self.tmp.name, "client.key")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n")
        msg, _ = self.run_main("--client-key-file", path)
        self.assertIn("--client-key-file is empty", msg)

    def test_missing_client_key_file_refuses(self):
        msg, _ = self.run_main("--client-key-file", os.path.join(self.tmp.name, "nope.key"))
        self.assertIn("cannot read --client-key-file", msg)

    def test_api_key_file_is_the_key_sent_upstream(self):
        from types import SimpleNamespace
        from ctxproxy.server import resolve_keys
        path = os.path.join(self.tmp.name, "engine.key")
        with open(path, "w", encoding="utf-8") as f:
            f.write("engine-secret\n")
        a = SimpleNamespace(api_key="inline", api_key_file=path, client_key_file=None)
        resolve_keys(a)
        self.assertEqual(a.api_key, "engine-secret")
        self.assertEqual(a.client_key, "")

    def test_api_key_file_refuses(self):
        path = os.path.join(self.tmp.name, "engine.key")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n")
        msg, _ = self.run_main("--api-key-file", path)
        self.assertIn("--api-key-file is empty", msg)
        msg, _ = self.run_main("--api-key-file", os.path.join(self.tmp.name, "nope.key"))
        self.assertIn("cannot read --api-key-file", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)