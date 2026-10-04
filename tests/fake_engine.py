"""Motore finto HTTP (solo stdlib) per i test: OpenAI /v1/chat/completions + /slots/0.

- Registra ogni richiesta ricevuta (self.requests) e il prompt "fisico" renderizzato in modo deterministico.
- Simula la cache a prefisso: cached_tokens = prefisso comune (in caratteri / 3.5) col prompt precedente;
  timings.prompt_n = token letti davvero. Così il giornale del proxy riceve reused/prompt_read come da Strata.
- `policy(body) -> message` decide la risposta (default: testo fisso). Le richieste di note e di sigillo sono
  riconosciute dal loro marcatore.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def render(body: dict) -> str:
    """Rendering deterministico del prompt (stand-in del template chat): ciò che conta è che due liste di messaggi
    uguali diano la stessa stringa e che un messaggio cambiato cambi la stringa da quel punto in poi."""
    out = ["<tools>" + json.dumps(body.get("tools") or [], sort_keys=True, ensure_ascii=False) + "</tools>"]
    for m in body.get("messages") or []:
        out.append("<|im_start|>%s\n%s%s%s<|im_end|>\n" % (
            m.get("role"), json.dumps(m.get("content"), ensure_ascii=False),
            json.dumps(m.get("tool_calls"), sort_keys=True, ensure_ascii=False) if m.get("tool_calls") else "",
            m.get("tool_call_id") or ""))
    return "".join(out)


class FakeEngine:
    def __init__(self, policy=None, cpt: float = 3.5):
        self.policy = policy or (lambda body: {"role": "assistant", "content": "ok"})
        self.requests: list[dict] = []
        self.prompts: list[str] = []
        self.slots: list[tuple] = []
        self.files: dict[str, str] = {}
        self.cancelled = 0
        self.cpt = cpt
        self.live = ""
        self.srv = None
        self.started = 1000
        self.n_requests = 0
        self.busy = False

    def restart(self):
        """Come un riavvio di Strata: la conversazione in memoria si perde, i file di sessione restano."""
        self.live = ""
        self.started += 1
        self.n_requests = 0

    def status(self) -> dict:
        return {"service": "strata", "loaded": True, "started": self.started,
                "activity": {"requests": self.n_requests, "in_flight": int(self.busy)}}

    def handle_chat(self, body: dict) -> dict:
        p = render(body)
        common = 0
        for a, b in zip(self.live, p):
            if a != b:
                break
            common += 1
        self.requests.append(body)
        self.prompts.append(p)
        self.n_requests += 1
        msg = self.policy(body)
        self.live = p
        pt = int(len(p) / self.cpt)
        cached = int(common / self.cpt)
        finish = "tool_calls" if msg.get("tool_calls") else "stop"
        ct = int(len(json.dumps(msg)) / self.cpt)
        return {"id": "chatcmpl-" + uuid.uuid4().hex[:8], "object": "chat.completion", "created": int(time.time()),
                "model": "fake", "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                          "prompt_tokens_details": {"cached_tokens": cached}},
                "timings": {"cache_n": cached, "prompt_n": pt - cached, "prompt_ms": (pt - cached) * 0.4}}

    def start(self, port: int = 0):
        eng = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, st, obj):
                d = json.dumps(obj).encode()
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(d)))
                self.end_headers()
                self.wfile.write(d)

            def do_GET(self):
                if self.path.startswith("/v1/status"):
                    return self._send(200, eng.status())
                self._send(200, {"status": "ok", "fake": True})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path.startswith("/slots/0"):
                    action = self.path.split("action=")[-1]
                    eng.slots.append((action, body.get("filename"), len(eng.live)))
                    if action == "save":
                        eng.files[body.get("filename")] = eng.live
                    elif action == "restore":
                        if body.get("filename") not in eng.files:
                            return self._send(404, {"error": {"message": "file non trovato"}})
                        eng.live = eng.files[body.get("filename")]
                    return self._send(200, {"id_slot": 0, "filename": body.get("filename"),
                                            "n_saved": int(len(eng.live) / eng.cpt),
                                            "n_restored": int(len(eng.live) / eng.cpt),
                                            "n_written": len(eng.live) * 10, "timings": {"save_ms": 1.0}})
                if body.get("stream"):
                    return self.do_POST_stream(body)
                self._send(200, eng.handle_chat(body))

            def do_POST_stream(self, body):
                r = eng.handle_chat(body)
                msg = dict(r["choices"][0]["message"])
                slow = msg.pop("_slow", 0)
                base = {k: r[k] for k in ("id", "object", "created", "model")}
                base["object"] = "chat.completion.chunk"

                def ch(delta, fin=None, **x):
                    return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": fin}], **x}

                parts = [ch({"role": "assistant", "content": ""})]
                for key in ("reasoning_content", "content"):
                    t = msg.get(key) or ""
                    for i in range(0, len(t), 10):
                        parts.append(ch({key: t[i:i + 10]}))
                for i, c in enumerate(msg.get("tool_calls") or []):
                    parts.append(ch({"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                                     "function": {"name": c["function"]["name"], "arguments": ""}}]}))
                    a = c["function"]["arguments"]
                    for k in range(0, len(a), 7):
                        parts.append(ch({"tool_calls": [{"index": i, "function": {"arguments": a[k:k + 7]}}]}))
                parts.append(ch({}, r["choices"][0]["finish_reason"], usage=r["usage"], timings=r["timings"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for p in parts:
                        self.wfile.write(b"data: " + json.dumps(p).encode() + b"\n\n")
                        self.wfile.flush()
                        if slow:
                            time.sleep(slow)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    eng.cancelled += 1
                self.close_connection = True

        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.srv.server_address[1]

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()


if __name__ == "__main__":
    # Stand-alone fake engine for smoke tests:  python3 -m tests.fake_engine --port 18095
    import argparse
    ap = argparse.ArgumentParser(description="fake OpenAI-compatible engine (Strata stand-in) for smoke tests")
    ap.add_argument("--port", type=int, default=18095)
    a = ap.parse_args()
    url = FakeEngine().start(a.port)
    print("[fake-engine] listening on %s" % url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
