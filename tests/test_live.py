"""Test: ancora di masking, streaming vero, disconnessione, giornale recall.

    python3 -m unittest -v tests.test_live
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import RECALL_NAME  # noqa: E402
from ctxproxy.server import make_handler  # noqa: E402
from tests.test_proxy import TOOLS, Base, assert_tool_pairs, history, tool_round  # noqa: E402


class TestAnchor(Base):
    cfg_over = {"mask_anchor": True, "slot_save": True, "anchor_min_tokens": 500, "min_batch_tokens": 3000,
                "segments_enabled": False}

    def test_anchor_limits_reread_after_mask(self):
        h = history(1)
        for k in range(1, 70):
            h += tool_round(k, 3500)
            self.send(h)
        masks = self.events("mask")
        saves = self.events("anchor_save")
        restores = self.events("anchor_restore")
        self.assertGreaterEqual(len(masks), 3, "servono più pacchetti per provare l'ancora")
        self.assertEqual(len(saves), len(masks))
        self.assertEqual(len(restores), len(masks) - 1, "dal secondo pacchetto in poi si riparte dall'ancora")
        self.assertFalse(self.events("anchor_stale"))
        # frontiera monotona: ogni pacchetto parte dalla frontiera del precedente o dopo
        for a, b in zip(masks, masks[1:]):
            self.assertGreaterEqual(b["first_index"], a["frontier_index"])
            self.assertLessEqual(b["floor_index"], a["frontier_index"])   # ancora = confine user/assistant ≤ frontiera
            self.assertGreater(b["floor_index"], a["first_index"])
        # dopo un pacchetto, la richiesta vera riparte dall'ancora appena salvata (non dal primo pezzo mascherato)
        reqs = self.events("request")
        for s in saves:
            nxt = next(r for r in reqs if r["ts"] >= s["ts"])
            self.assertGreaterEqual(nxt["reused"], 0.95 * s["prompt_tokens"], (s, nxt))
        # e la lettura dell'ancora nuova riparte dall'ancora vecchia ripristinata
        for a, b in zip(saves, saves[1:]):
            self.assertGreaterEqual(b["cached"], 0.95 * a["prompt_tokens"], (a, b))
        # una richiesta identica dopo tutto ciò resta stabile
        self.send(h)
        self.send(h)
        self.assertEqual(self.eng.prompts[-1], self.eng.prompts[-2])
        assert_tool_pairs(self, self.eng.requests[-1]["messages"])

    def test_without_anchor_reread_is_large(self):
        """Riferimento: senza ancora la richiesta dopo il pacchetto rilegge dal primo pezzo mascherato."""
        self.proxy.cfg.mask_anchor = False
        h = history(1)
        for k in range(1, 40):
            h += tool_round(k, 3500)
            self.send(h)
        masks = self.events("mask")
        self.assertTrue(masks)
        reqs = self.events("request")
        big = [next(r for r in reqs if r["ts"] >= m["ts"])["prompt_read"] for m in masks]
        self.assertTrue(all(b > 3000 for b in big), big)


class StreamPolicy:
    """Risposte scriptate per il motore finto in modalità stream."""


class TestStreaming(Base):
    def policy(self, body):
        msgs = body["messages"]
        last = msgs[-1]
        if last.get("role") == "user" and "cerca" in str(last.get("content")):
            return {"role": "assistant", "content": "", "reasoning_content": "devo cercare",
                    "tool_calls": [{"id": "rc9", "type": "function",
                                    "function": {"name": RECALL_NAME, "arguments": '{"query":"riga 1 del"}'}}]}
        if last.get("role") == "tool" and str(last.get("content")).startswith("[recall:"):
            return {"role": "assistant", "content": "trovato nel file f1", "reasoning_content": "ora rispondo"}
        if last.get("role") == "user" and "lento" in str(last.get("content")):
            return {"role": "assistant", "content": "x" * 400, "reasoning_content": "pensiero " * 40,
                    "_slow": 0.05}
        if last.get("role") == "user" and "strumento" in str(last.get("content")):
            return {"role": "assistant", "content": "eseguo", "reasoning_content": "uso bash",
                    "tool_calls": [{"id": "b1", "type": "function",
                                    "function": {"name": "bash", "arguments": '{"command":"ls -la"}'}}]}
        return {"role": "assistant", "content": "ok", "reasoning_content": "facile"}

    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.proxy))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def stream(self, msgs):
        body = json.dumps({"model": "m", "messages": msgs, "tools": TOOLS, "stream": True,
                           "stream_options": {"include_usage": True}}).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.headers["Content-Type"], "text/event-stream")
            lines = [l for l in r.read().decode().split("\n") if l.startswith("data: ")]
        self.assertEqual(lines[-1], "data: [DONE]")
        return [json.loads(l[6:]) for l in lines[:-1]]

    @staticmethod
    def collect(chunks):
        content = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c.get("choices"))
        reasoning = "".join(c["choices"][0]["delta"].get("reasoning_content") or "" for c in chunks if c.get("choices"))
        calls = {}
        for c in chunks:
            for tc in (c.get("choices") or [{}])[0].get("delta", {}).get("tool_calls") or []:
                d = calls.setdefault(tc["index"], {"id": None, "name": "", "args": ""})
                d["id"] = tc.get("id") or d["id"]
                d["name"] += (tc.get("function") or {}).get("name") or ""
                d["args"] += (tc.get("function") or {}).get("arguments") or ""
        fin = [c["choices"][0]["finish_reason"] for c in chunks if c.get("choices") and c["choices"][0].get("finish_reason")]
        return content, reasoning, calls, fin

    def test_stream_passthrough_tokens(self):
        chunks = self.stream(history(2) + [{"role": "user", "content": "lento"}])
        content, reasoning, calls, fin = self.collect(chunks)
        self.assertEqual(content, "x" * 400)
        self.assertEqual(reasoning, "pensiero " * 40)
        self.assertEqual(fin, ["stop"])
        self.assertGreater(len(chunks), 20, "streaming vero: molti delta, non un blocco unico")
        # l'upstream è stato chiamato in streaming
        self.assertTrue(self.eng.requests[-1].get("stream"))
        self.assertIn("strata_context", chunks[-1])
        self.assertIn("usage", chunks[-1])

    def test_stream_client_tool_call(self):
        chunks = self.stream(history(2) + [{"role": "user", "content": "usa uno strumento"}])
        content, reasoning, calls, fin = self.collect(chunks)
        self.assertEqual(fin, ["tool_calls"])
        self.assertEqual(calls[0]["name"], "bash")
        self.assertEqual(json.loads(calls[0]["args"]), {"command": "ls -la"})
        self.assertEqual(calls[0]["id"], "b1")

    def test_stream_recall_hidden(self):
        h = history(3) + [{"role": "user", "content": "cerca nel primo file"}]
        chunks = self.stream(h)
        content, reasoning, calls, fin = self.collect(chunks)
        self.assertEqual(calls, {}, "la chiamata interna strata_recall non deve arrivare al client")
        self.assertIn("trovato nel file f1", content)
        self.assertEqual(fin, ["stop"])
        rec = self.events("recall")
        self.assertEqual(len(rec), 1)
        self.assertIn("results", rec[0])
        self.assertTrue(rec[0]["results"])
        self.assertIn("last_user", rec[0])
        # la richiesta successiva reinserisce gli eventi interni: prefisso stabile
        prev = self.eng.prompts[-1]
        msg = {"role": "assistant", "content": content, "reasoning_content": reasoning}
        self.stream(h + [msg, {"role": "user", "content": "grazie"}])
        self.assertTrue(self.eng.prompts[-1].startswith(prev[:len(prev) - 10]))

    def test_disconnect_cancels_upstream(self):
        import socket
        body = json.dumps({"model": "m", "messages": history(2) + [{"role": "user", "content": "lento"}],
                           "tools": TOOLS, "stream": True}).encode()
        s = socket.create_connection(self.srv.server_address)
        s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        got = s.recv(2048)
        self.assertIn(b"200", got.split(b"\r\n")[0])
        s.close()   # Esc in pi
        deadline = time.time() + 10
        while time.time() < deadline and not self.events("client_disconnect"):
            time.sleep(0.1)
        self.assertTrue(self.events("client_disconnect"))
        deadline = time.time() + 10
        while time.time() < deadline and not self.eng.cancelled:
            time.sleep(0.1)
        self.assertTrue(self.eng.cancelled, "lo stream verso il motore deve essere chiuso")
        # il proxy resta utilizzabile
        chunks = self.stream(history(2) + [{"role": "user", "content": "di nuovo"}])
        self.assertEqual(self.collect(chunks)[0], "ok")


if __name__ == "__main__":
    unittest.main()
