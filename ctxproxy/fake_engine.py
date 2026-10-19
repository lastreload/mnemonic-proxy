# Author: Maurizio Verde — LastReload
"""Motore finto HTTP (solo stdlib) per i test, la demo e `check` in CI: OpenAI /v1/chat/completions + /slots/0.

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
import urllib.error
import urllib.parse
import urllib.request
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
        # 0.3.3 strict window (worst-case engine for the window guard): `strict_window` = True rejects with HTTP 400
        # every chat request with prompt + max_tokens > n_ctx (and one without max_tokens: the engine would run to
        # the window). `counter(body)` -> prompt tokens (default: the proxy's exact count is passed in by tests).
        self.strict_window = False
        self.counter = None
        self.violations: list = []
        self.checked = 0

    def window_check(self, body: dict) -> str | None:
        self.checked += 1
        p = int(self.counter(body)) if self.counter else int(len(render(body)) / self.cpt)
        mt = body.get("max_completion_tokens") or body.get("max_tokens")
        if not isinstance(mt, int) or mt <= 0 or p + mt > self.n_ctx:
            v = {"prompt": p, "max_tokens": mt, "n_ctx": self.n_ctx,
                 "reasoning_budget_tokens": body.get("reasoning_budget_tokens")}
            self.violations.append(v)
            return "request (%d tokens) exceeds the available context size (%d tokens)" % (p + (mt or 0), self.n_ctx)
        rb = body.get("reasoning_budget_tokens")
        if isinstance(rb, int) and rb > 0 and rb >= mt:
            self.violations.append({"prompt": p, "max_tokens": mt, "reasoning_budget_tokens": rb, "inv": 2})
            return "reasoning_budget_tokens %d >= max_tokens %d" % (rb, mt)
        return None

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
                "context": {"native": self.n_ctx, "max_positions": self.n_ctx},
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
        if self.counter is not None:               # strict window: report the same exact count it enforces
            pt = int(self.counter(body))
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
                    if self.path.startswith("/v1/models"):
                        return self._send(200, {"object": "list", "data": [{"id": "fake-0.6B.gguf"}]})
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
                if eng.strict_window and self.path.startswith("/v1/chat"):
                    err = eng.window_check(body)
                    if err:
                        return self._send(400, {"error": {"message": err, "type": "exceed_context_size_error"}})
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
                except ConnectionError:   # Windows: ConnectionAbortedError (WinError 10053) too
                    eng.cancelled += 1
                self.close_connection = True

        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.srv.server_address[1]

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()


class FakeRouter:
    """llama-server in router mode (0.3.7, issue #12), as verified on llama.cpp b11723: several models, each one a
    child FakeEngine(flavor="llama") started on load and stopped on unload (its in-memory state is lost), slot files
    shared by all models (the real engine restores another model's file without complaining).

    - GET /props without model: the router's own props (role router, n_ctx 0); GET /props|/slots with ?model=;
      POST bodies carry "model"; missing -> 400 "model name is missing from the request".
    - autoload: on by default, `?autoload=false` (GET and POST) -> 400 "model is not loaded".
    - GET /models (= /v1/models): status.value unloaded|loaded, status.args with --ctx-size/--slot-save-path,
      meta.n_ctx when loaded; `context_length` is the preset value (`preset_ctx`), which can differ from the real slot
      n_ctx (the real server caps -c to the training context).
    - POST /models/load|unload {"model"}; `fail` = models whose load fails (status failed, 503 on autoload).
    - models_max: loading one more model unloads the least recently used one.
    """

    def __init__(self, models: dict, slot_save: bool = True, cpt: float = 3.5, policy=None, preset_ctx=None,
                 fail=(), models_max: int = 0, strict_window: bool = False, counter=None):
        self.n_ctx = dict(models)                 # model id -> real slot n_ctx
        self.preset_ctx = dict(preset_ctx or models)
        self.slot_save, self.cpt, self.policy = slot_save, cpt, policy
        self.fail, self.models_max = set(fail), models_max
        self.strict_window, self.counter = strict_window, counter
        self.children: dict[str, FakeEngine] = {}
        self.urls: dict[str, str] = {}
        self.failed: set = set()
        self.files: dict[str, str] = {}           # shared slot directory
        self.saved_by: dict[str, str] = {}        # file -> model that saved it (engine side)
        self.restores: list = []                  # (model, file, saved_by)
        self.loads: list = []                     # models loaded, in order
        self.calls: list = []                     # (method, path, model)
        self.chats: list = []                     # (model, body)
        self.lru: list = []
        self.load_delay: float | dict = 0         # seconds per load (or model -> seconds), see load()
        self.loading: set = set()
        self.bytes_per_token: dict = {}           # model -> n_written per saved token (default: the child's own)
        self.srv = None
        self.mu = threading.RLock()

    # ---- model lifecycle ----
    def load(self, mid: str) -> str | None:
        """-> error message or None. `load_delay` = seconds a load takes (status "loading" meanwhile)."""
        with self.mu:
            if mid in self.children:
                return None
            if mid in self.fail:
                self.failed.add(mid)
                return "model %s failed to load" % mid
            delay = self.load_delay.get(mid, 0) if isinstance(self.load_delay, dict) else self.load_delay
            if delay:
                self.loading.add(mid)
        if delay:
            time.sleep(delay)
        with self.mu:
            self.loading.discard(mid)
            if mid in self.children:
                return None
            if self.models_max and len(self.children) >= self.models_max:
                self.unload(self.lru[0])
            ch = FakeEngine(policy=self.policy, cpt=self.cpt, flavor="llama", slot_save=self.slot_save,
                            n_ctx=self.n_ctx[mid])
            ch.files = self.files
            ch.strict_window, ch.counter = self.strict_window, self.counter
            self.urls[mid] = ch.start(0)
            self.children[mid] = ch
            self.failed.discard(mid)
            self.loads.append(mid)
            self.lru.append(mid)
            return None

    def unload(self, mid: str) -> bool:
        with self.mu:
            ch = self.children.pop(mid, None)
            if ch is None:
                return False
            ch.stop()
            self.urls.pop(mid, None)
            if mid in self.lru:
                self.lru.remove(mid)
            return True

    def touch(self, mid: str):
        with self.mu:
            if mid in self.lru:
                self.lru.remove(mid)
                self.lru.append(mid)

    def entry(self, mid: str) -> dict:
        args = ["llama-server", "--port", "0", "--alias", mid, "--ctx-size", str(self.preset_ctx[mid]),
                "--parallel", "1"] + (["--slot-save-path", "/slots"] if self.slot_save else [])
        st = {"value": "loaded" if mid in self.children else "loading" if mid in self.loading else "unloaded",
              "args": args}
        if mid in self.failed:
            st.update(failed=True, exit_code=1)
        e = {"id": mid, "aliases": [], "object": "model", "owned_by": "llamacpp", "status": st,
             "context_length": self.preset_ctx[mid]}
        if mid in self.children:
            e["meta"] = {"n_ctx": self.n_ctx[mid], "n_ctx_train": self.n_ctx[mid]}
        return e

    def start(self, port: int = 0):
        rt = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, st, obj, ctype="application/json"):
                d = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(st)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(d)))
                self.end_headers()
                self.wfile.write(d)

            def _err(self, st, msg):
                return self._send(st, {"error": {"code": st, "message": msg, "type": "invalid_request_error"}})

            def _route(self, method, path, query, model, body: bytes | None):
                rt.calls.append((method, path, model))
                if not model:
                    return self._err(400, "model name is missing from the request")
                if model not in rt.n_ctx:
                    return self._err(400, "model '%s' not found" % model)
                if model not in rt.children:
                    if (query.get("autoload") or ["true"])[0] == "false":
                        return self._err(400, "model is not loaded")
                    err = rt.load(model)
                    if err:
                        return self._send(503, {"error": {"code": 503, "message": err, "type": "unavailable_error"}})
                rt.touch(model)
                if method == "POST" and path.startswith("/slots/"):
                    action = (query.get("action") or [""])[0]
                    fn = json.loads(body or b"{}").get("filename")
                    if action == "save":
                        rt.saved_by[fn] = model
                    elif action == "restore" and fn in rt.files:
                        rt.restores.append((model, fn, rt.saved_by.get(fn)))
                if method == "POST" and path.startswith("/v1/chat"):
                    rt.chats.append((model, json.loads(body or b"{}")))
                q = [(k, v) for k, vs in query.items() for v in vs if k not in ("model", "autoload")]
                url = rt.urls[model] + path + ("?" + urllib.parse.urlencode(q) if q else "")
                req = urllib.request.Request(url, data=body, method=method,
                                             headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        st, ctype, data = r.status, r.headers.get("Content-Type") or "application/json", r.read()
                except urllib.error.HTTPError as e:
                    st, ctype, data = e.code, "application/json", e.read()
                if method == "POST" and path.startswith("/slots/") and st == 200 \
                        and (query.get("action") or [""])[0] == "save" and model in rt.bytes_per_token:
                    d = json.loads(data)
                    d["n_written"] = int(d.get("n_saved") or 0) * rt.bytes_per_token[model]
                    data = json.dumps(d).encode()
                self.send_response(st)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if ctype.startswith("text/event-stream"):
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(u.query)
                model = (query.get("model") or [None])[0]
                if u.path == "/health":
                    return self._send(200, {"status": "ok"})
                if u.path in ("/models", "/v1/models"):
                    with rt.mu:
                        return self._send(200, {"object": "list", "data": [rt.entry(m) for m in rt.n_ctx]})
                if u.path == "/props" and not model:
                    return self._send(200, {"role": "router", "max_instances": rt.models_max or 4,
                                            "models_autoload": True, "model_alias": "llama-server",
                                            "model_path": "none",
                                            "default_generation_settings": {"params": None, "n_ctx": 0}})
                if u.path in ("/props", "/slots", "/metrics"):
                    return self._route("GET", u.path, query, model, None)
                return self._err(404, "File Not Found")

            def do_POST(self):
                u = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(u.query)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                model = body.get("model") if isinstance(body, dict) else None
                if u.path == "/models/load":
                    if model not in rt.n_ctx:
                        return self._send(404, {"error": {"code": 404, "message": "File Not Found"}})
                    if model in rt.children:
                        return self._err(400, "model is already running")
                    err = rt.load(model)
                    return self._send(200, {"success": True}) if not err else self._err(500, err)
                if u.path == "/models/unload":
                    return self._send(200, {"success": True}) if rt.unload(model) else \
                        self._err(400, "model is not running")
                return self._route("POST", u.path, query, model, raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.srv.server_address[1]

    def stop(self):
        for m in list(self.children):
            self.unload(m)
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()


if __name__ == "__main__":
    # Stand-alone fake engine for smoke tests:  python3 -m ctxproxy.fake_engine --port 18095
    import argparse
    ap = argparse.ArgumentParser(description="fake OpenAI-compatible engine (Strata stand-in) for smoke tests")
    ap.add_argument("--port", type=int, default=18095)
    ap.add_argument("--flavor", choices=["strata", "llama", "ds4", "openai", "router"], default="strata",
                    help="engine to imitate (llama = llama-server with --slot-save-path; router = llama-server in "
                         "router mode, see --models)")
    ap.add_argument("--n-ctx", type=int, default=131072)
    ap.add_argument("--models", default="big=65536,small=16384",
                    help="router flavor: model=n_ctx,... (each model a llama-server, loaded on demand)")
    ap.add_argument("--no-slot-save", action="store_true", help="llama flavor: as if started without --slot-save-path")
    a = ap.parse_args()
    if a.flavor == "router":
        ms = {k: int(v) for k, v in (p.split("=", 1) for p in a.models.split(",") if p)}
        url = FakeRouter(ms, slot_save=not a.no_slot_save).start(a.port)
    else:
        url = FakeEngine(flavor=a.flavor, n_ctx=a.n_ctx, slot_save=not a.no_slot_save).start(a.port)
    print("[fake-engine] listening on %s" % url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
