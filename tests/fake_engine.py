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
    def __init__(self, policy=None, cpt: float = 3.5, flavor: str = "strata", slot_save: bool = True,
                 n_slots: int = 1, n_ctx: int = 131072):
        """flavor: strata (/v1/status) | llama (llama-server: /props, /slots, /tokenize, 400 per file mancante,
        id_task che cresce di più di 1 per richiesta, 501 sulle azioni slot senza --slot-save-path) | ds4
        (ds4-server: /v1/models con owned_by "ds4.c", niente slot, id `call_<hex>` generati dal motore, 400 su un
        risultato di strumento con id sconosciuto, conteggio delle chiamate rimandate con id noto/sconosciuto) | openai
        (solo /v1/chat/completions)."""
        self.flavor, self.slot_save, self.n_slots, self.n_ctx = flavor, slot_save, n_slots, n_ctx
        self.id_task = 0
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
        self.ds4_memory: dict = {}      # flavor ds4: id -> argomenti campionati (exact DSML tool replay)
        self.ds4_live_ids: set = set()
        self.ds4_replay = {"mem": 0, "canonical": 0, "missing_ids": []}
        self.rejected: list = []

    def restart(self):
        """Come un riavvio di Strata: la conversazione in memoria si perde, i file di sessione restano."""
        self.live = ""
        self.started += 1
        self.n_requests = 0
        self.id_task = 0

    def slots_list(self) -> list:
        return [{"id": i, "n_ctx": self.n_ctx // self.n_slots, "is_processing": bool(self.busy) and i == 0,
                 "id_task": self.id_task if i == 0 else -1, "n_prompt_tokens": int(len(self.live) / self.cpt)}
                for i in range(self.n_slots)]

    def status(self) -> dict:
        return {"service": "strata", "loaded": True, "started": self.started,
                "activity": {"requests": self.n_requests, "in_flight": int(self.busy)}}

    # ---- ds4-server (flavor="ds4"): comportamento verificato su ds4_server.c ----
    def ds4_check(self, body: dict) -> str | None:
        """Come anthropic_validate_tool_results / responses_validate_tool_outputs: un risultato di strumento con un
        id che non è né nella storia rimandata (chiamata assistant precedente) né nello stato vivo -> errore
        «replay full history». (Il vero ds4 lo fa su /v1/messages e /v1/responses; qui anche su chat, più severo.)"""
        seen = set()
        for m in body.get("messages") or []:
            if m.get("role") == "assistant":
                seen.update(c.get("id") for c in m.get("tool_calls") or [])
            elif m.get("role") == "tool":
                cid = m.get("tool_call_id")
                if cid not in seen and cid not in self.ds4_live_ids:
                    return ("continuation state is not available for tool_call_id %s; retry by replaying the full "
                            "messages history" % cid)
        return None

    def ds4_after(self, body: dict, msg: dict) -> dict:
        """Ripresa per id: per ogni tool call rimandata, id noto = testo campionato esatto (mem), id sconosciuto =
        rendering canonico (il prefisso può cambiare). Poi id nuovi `call_<hex>` alle chiamate del modello."""
        for m in body.get("messages") or []:
            for c in (m.get("tool_calls") or []) if m.get("role") == "assistant" else []:
                if c.get("id") in self.ds4_memory:
                    self.ds4_replay["mem"] += 1
                else:
                    self.ds4_replay["canonical"] += 1
                    self.ds4_replay["missing_ids"].append(c.get("id"))
        calls = []
        for c in msg.get("tool_calls") or []:
            c = dict(c)
            if not c.get("id"):
                c["id"] = "call_" + uuid.uuid4().hex
            self.ds4_memory[c["id"]] = (c.get("function") or {}).get("arguments")
            calls.append(c)
        self.ds4_live_ids = {c["id"] for c in calls}
        return {**msg, "tool_calls": calls} if calls else msg

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
        self.id_task += 3
        msg = self.policy(body)
        if self.flavor == "ds4":
            msg = self.ds4_after(body, msg)
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
                if eng.flavor == "llama":
                    if self.path.startswith("/props"):
                        return self._send(200, {"default_generation_settings": {"n_ctx": eng.n_ctx // eng.n_slots},
                                                "total_slots": eng.n_slots, "model_path": "/m/fake-0.6B.gguf"})
                    if self.path.startswith("/slots"):
                        return self._send(200, eng.slots_list())
                    if self.path.startswith("/health"):
                        return self._send(200, {"status": "ok"})
                    return self._send(404, {"error": {"code": 404, "message": "File Not Found"}})
                if eng.flavor == "openai":
                    if self.path.startswith("/v1/models"):
                        return self._send(200, {"data": [{"id": "fake"}]})
                    return self._send(404, {"error": {"message": "not found"}})
                if eng.flavor == "ds4":
                    if self.path == "/v1/models":
                        mk = lambda i: {"id": i, "object": "model", "created": 1767225600, "owned_by": "ds4.c",  # noqa
                                        "name": "Qwen3.8-Flash-Next", "context_length": eng.n_ctx,
                                        "top_provider": {"context_length": eng.n_ctx}}
                        return self._send(200, {"object": "list", "data": [
                            mk("qwen3.8-flash-next"), mk("qwen3.8-flash-next-chat"),
                            mk("qwen3.8-flash-next-reasoner")]})
                    return self._send(404, {"error": {"message": "unknown endpoint"}})
                if self.path.startswith("/v1/status"):
                    return self._send(200, eng.status())
                self._send(200, {"status": "ok", "fake": True})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if eng.flavor == "openai" and not self.path.startswith("/v1/chat"):
                    return self._send(404, {"error": {"message": "not found"}})
                if eng.flavor == "ds4":
                    if not self.path.startswith("/v1/chat/completions"):
                        eng.slots.append(("unexpected", self.path, len(eng.live)))
                        return self._send(404, {"error": {"message": "unknown endpoint"}})
                    err = eng.ds4_check(body)
                    if err:
                        eng.rejected.append(err)
                        return self._send(400, {"error": {"message": err, "type": "invalid_request_error"}})
                if eng.flavor == "llama" and self.path.startswith("/tokenize"):
                    return self._send(200, {"tokens": list(range(int(len(body.get("content") or "") / eng.cpt)))})
                if eng.flavor == "llama" and self.path.startswith("/slots/"):
                    sid = int(self.path.split("/")[2].split("?")[0])
                    action = self.path.split("action=")[-1]
                    if not eng.slot_save:
                        return self._send(501, {"error": {"code": 501, "message": "This server does not support "
                                                          "slots action. Start it with `--slot-save-path`"}})
                    if action not in ("save", "restore", "erase"):
                        return self._send(400, {"error": {"code": 400, "message": "Invalid action"}})
                    if sid != 0:
                        return self._send(400, {"error": {"code": 400, "message": "Invalid slot ID"}})
                    fn = body.get("filename")
                    eng.slots.append((action, fn, len(eng.live)))
                    if action == "save":
                        eng.files[fn] = eng.live
                        return self._send(200, {"id_slot": sid, "filename": fn, "n_saved": int(len(eng.live) / eng.cpt),
                                                "n_written": len(eng.live) * 10, "timings": {"save_ms": 1.0}})
                    if action == "restore":
                        if fn not in eng.files:
                            return self._send(400, {"error": {"code": 400, "message": "Unable to restore slot: No "
                                                              "available space in KV cache or invalid slot save file"}})
                        eng.live = eng.files[fn]
                        return self._send(200, {"id_slot": sid, "filename": fn,
                                                "n_restored": int(len(eng.live) / eng.cpt),
                                                "n_read": len(eng.live) * 10, "timings": {"restore_ms": 1.0}})
                    eng.live = ""
                    return self._send(200, {"id_slot": sid, "n_erased": 1})
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
