# Author: Maurizio Verde — LastReload
"""Client HTTP minimale verso Strata (solo stdlib)."""
from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .savedstate import SavedState


class UpstreamError(RuntimeError):
    def __init__(self, status: int, body: bytes):
        super().__init__("upstream HTTP %d: %s" % (status, body[:300].decode("utf-8", "replace")))
        self.status, self.body = status, body


class Upstream:
    def __init__(self, base: str, api_key: str = "", timeout: float = 3600.0):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.engine = None          # engines.Engine (None = Strata, comportamento storico)
        self.slot_id = 0
        self.slot_dir = ""          # per riconoscere un file mancante (llama-server risponde 400)
        self.extra_body: dict = {}  # campi aggiunti a ogni richiesta chat (llama.cpp: id_slot, cache_prompt)
        self.window_guard = None    # window.WindowGuard: every chat request passes through enforce() (0.3.3)
        self.router: Any = None     # 0.3.7: router.Router (llama-server router mode), None otherwise
        self.router_model = None    # model selected for the request in progress (router mode)
        self.truncwatch: Any = None  # 0.3.8: enginectx.TruncWatch (generic engines), None otherwise
        self.watch_ctx = None        # (conv, seg) of the request in progress, for truncwatch (set by the proxy)
        self.saved_state = SavedState(self)  # process-local circuit breaker, keyed by concrete model id

    def _count(self):
        g = self.window_guard
        return getattr(g, "last_count", None) if g is not None else None

    def _watch(self, est, usage):
        """0.3.8: prompt cut by the engine? (journal + one warning per conversation; the answer is unchanged)"""
        if self.truncwatch is None or est is None:
            return
        try:
            self.truncwatch.observe(est, usage, getattr(self.window_guard, "where", "") or "", self.watch_ctx)
        except Exception:  # noqa: BLE001  observability must never break a request
            pass

    def _guard(self, body: dict) -> dict:
        if self.window_guard is None:
            return body
        self.window_guard.last_count = None      # set again by enforce() (stale when the guard is off)
        return self.window_guard.enforce(body)

    def _model(self, body: dict) -> dict:
        """Router mode: every call goes to the model whose window was applied (internal calls included)."""
        return {**body, "model": self.router_model} if self.router is not None and self.router_model else body

    def set_engine(self, engine, slot_dir: str = ""):
        self.engine = engine
        self.slot_id = engine.slot_id
        self.slot_dir = slot_dir
        self.extra_body = {"id_slot": engine.slot_id, "cache_prompt": True} if engine.kind == "llama.cpp" else {}

    def _headers(self, extra=None):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        h.update(extra or {})
        return h

    def raw(self, method: str, path: str, body: bytes | None = None, headers=None, timeout: float | None = None):
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=self._headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()

    def post_json(self, path: str, obj: dict) -> dict:
        st, _, body = self.raw("POST", path, json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        if st >= 400:
            raise UpstreamError(st, body)
        return json.loads(body)

    def _context_retry(self, error: UpstreamError, body: dict) -> dict:
        """One pre-generation rejection can supply the count the server actually used.

        Do not guess from arbitrary 400s, retry transport failures, or change messages.
        Hidden server-side history/template settings cannot be counted by a tokenizer alone.
        """
        g = self.window_guard
        if (error.status != 400 or g is None or not getattr(g.cfg, "window_guard", True)
                or not g.cfg.window):
            raise error
        try:
            obj = json.loads(error.body)
            message = obj["error"]["message"]
        except (ValueError, TypeError, KeyError):
            raise error
        if not isinstance(message, str):
            raise error
        match = re.fullmatch(
            r"request exceeds the context window: prompt \((\d+) tokens\) "
            r"(?:\+ max tokens \((\d+)\) exceeds the context \((\d+)\)|"
            r"leaves no room to answer in the context \((\d+)\)); requests are never truncated(?:\..*)?",
            message, re.DOTALL)
        if match is None:
            raise error
        from .window import WindowExceeded, mt_of, set_mt
        prompt, requested, win, empty_win = match.groups()
        prompt, window = int(prompt), int(win or empty_win)
        if window < 1 or (requested is not None and int(requested) != mt_of(body)):
            raise error
        # Never expand the configured window, nor shrink a generation to fit a
        # second time: the caller retries exactly once outside this function.
        window = min(window, int(g.cfg.window))
        room = window - max(8, g.margin) - prompt
        g._log(action="engine_rejected", where=g.where or "call", prompt=prompt,
               counted_prompt=g.last_count, engine_window=int(win or empty_win),
               max_tokens=mt_of(body), retry=bool(room > 0 and room < mt_of(body)))
        if room < 1:
            g.refused += 1
            g._log(action="refused", where=g.where or "call", prompt=prompt,
                   max_tokens=mt_of(body), reason="engine_prompt_exceeds_window")
            raise WindowExceeded(prompt, window, max(8, g.margin), g.where or "call") from error
        if room >= mt_of(body):
            raise error
        g.last_count = prompt
        g._log(action="engine_retry", where=g.where or "call", prompt=prompt,
               max_tokens=room, was=mt_of(body))
        return g.cap_budget(set_mt(body, room))

    def _chat_once_with_context_retry(self, body: dict, send):
        try:
            return send(body)
        except UpstreamError as error:
            corrected = self._context_retry(error, body)
        return send(corrected)  # bounded: a second error is returned, never retried

    def chat(self, body: dict) -> dict:
        self.saved_state.select(body.get("model"))
        body = self._guard(self._model({**self.extra_body,
                                        **{k: v for k, v in body.items() if k not in ("stream", "stream_options")}}))
        r = self._chat_once_with_context_retry(body, lambda b: self.post_json("/v1/chat/completions", b))
        self._watch(self._count(), r.get("usage") if isinstance(r, dict) else None)
        return r

    def chat_stream(self, body: dict) -> "StreamCall":
        """Apre /v1/chat/completions in streaming. Iterare l'oggetto dà i chunk JSON; close() chiude la connessione
        (Strata vede la disconnessione e annulla la generazione)."""
        self.saved_state.select(body.get("model"))
        body = self._guard(self._model({**self.extra_body, **body, "stream": True,
                                        "stream_options": {"include_usage": True}}))
        sc = self._chat_once_with_context_retry(body, lambda b: StreamCall(self, b))
        if self.truncwatch is not None:
            sc.est = self._count()
        return sc

    def slot_file_ok(self, filename: str) -> bool:
        """Router mode: may this saved file be restored with the current model? Always True otherwise."""
        return self.router is None or self.router.restore_refusal(filename) is None

    def slot(self, action: str, filename: str) -> dict:
        """Strata e llama-server hanno la stessa forma: POST /slots/{id}?action=save|restore {"filename"} ->
        {id_slot, filename, n_saved|n_restored, n_written|n_read, timings.save_ms|restore_ms}.

        Every slot call passes through the same per-model circuit breaker.  A
        suspended model fails locally without another HTTP request.
        """
        key = self.saved_state.key()        # fixed once for require, observe and end_probe
        probe = self.saved_state.require(action, key=key)
        ok = False
        try:
            if self.router is not None:
                result = self._router_slot(action, filename)
            else:
                st, _, body = self.raw("POST", "/slots/%d?action=%s" % (self.slot_id, action),
                                       json.dumps({"filename": filename}, ensure_ascii=False).encode("utf-8"))
                if st >= 400:
                    from .engines import slot_error_status
                    raise UpstreamError(slot_error_status(self.engine, action, st, body, self.slot_dir, filename),
                                        body)
                result = json.loads(body)
            ok = True
            return result
        except UpstreamError as error:
            self.saved_state.observe(error, action, filename, key=key)
            raise
        finally:
            if probe is not None:
                self.saved_state.end_probe(probe, ok)

    def _router_slot(self, action: str, filename: str) -> dict:
        """Router mode: the slot of the current model ("model" in the body, autoload=false: a model that has been
        unloaded is not loaded again just to save an empty slot). A restore is refused (409) unless the file was
        saved by the same model; the engine itself would restore another model's file."""
        m = self.router_model
        if not m:
            raise UpstreamError(409, b'{"error": {"message": "router mode: no model selected yet"}}')
        if action == "restore":
            why = self.router.restore_refusal(filename)
            if why:
                raise UpstreamError(409, json.dumps({"error": {"message": "router mode: not restored: " + why}})
                                    .encode("utf-8"))
        st, _, body = self.raw("POST", "/slots/%d?action=%s&autoload=false" % (self.slot_id, action),
                               json.dumps({"filename": filename, "model": m}, ensure_ascii=False).encode("utf-8"))
        if st >= 400:
            from .engines import slot_error_status
            raise UpstreamError(slot_error_status(self.engine, action, st, body, self.slot_dir, filename), body)
        if action == "save":
            self.router.owners.record(filename, m)
        return json.loads(body)


class StreamCall:
    def __init__(self, up: Upstream, body: dict):
        self.up, self.est = up, None     # est: 0.3.8 prompt count for the truncation watch (generic engines)
        u = urllib.parse.urlsplit(up.base)
        self.conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=up.timeout)
        prefix = u.path.rstrip("/")
        self.conn.request("POST", prefix + "/v1/chat/completions", json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          up._headers({"Accept": "text/event-stream"}))
        self.resp = self.conn.getresponse()
        if self.resp.status >= 400:
            data = self.resp.read()
            self.close()
            raise UpstreamError(self.resp.status, data)

    def __iter__(self):
        for line in self.resp:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if self.est is not None and isinstance(obj, dict) and obj.get("usage"):
                self.up._watch(self.est, obj.get("usage"))
            yield obj

    def close(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
