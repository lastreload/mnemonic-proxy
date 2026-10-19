# Author: Maurizio Verde — LastReload
"""Client HTTP minimale verso Strata (solo stdlib)."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


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

    def _guard(self, body: dict) -> dict:
        return self.window_guard.enforce(body) if self.window_guard is not None else body

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

    def chat(self, body: dict) -> dict:
        body = self._guard(self._model({**self.extra_body,
                                        **{k: v for k, v in body.items() if k not in ("stream", "stream_options")}}))
        return self.post_json("/v1/chat/completions", body)

    def chat_stream(self, body: dict) -> "StreamCall":
        """Apre /v1/chat/completions in streaming. Iterare l'oggetto dà i chunk JSON; close() chiude la connessione
        (Strata vede la disconnessione e annulla la generazione)."""
        body = self._guard(self._model({**self.extra_body, **body, "stream": True,
                                        "stream_options": {"include_usage": True}}))
        return StreamCall(self, body)

    def slot_file_ok(self, filename: str) -> bool:
        """Router mode: may this saved file be restored with the current model? Always True otherwise."""
        return self.router is None or self.router.restore_refusal(filename) is None

    def slot(self, action: str, filename: str) -> dict:
        """Strata e llama-server hanno la stessa forma: POST /slots/{id}?action=save|restore {"filename"} ->
        {id_slot, filename, n_saved|n_restored, n_written|n_read, timings.save_ms|restore_ms}."""
        if self.router is not None:
            return self._router_slot(action, filename)
        st, _, body = self.raw("POST", "/slots/%d?action=%s" % (self.slot_id, action),
                               json.dumps({"filename": filename}, ensure_ascii=False).encode("utf-8"))
        if st >= 400:
            from .engines import slot_error_status
            raise UpstreamError(slot_error_status(self.engine, action, st, body, self.slot_dir, filename), body)
        return json.loads(body)

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
                yield json.loads(data)
            except ValueError:
                continue

    def close(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
