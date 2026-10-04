"""Client HTTP minimale verso Strata (solo stdlib)."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request


class UpstreamError(RuntimeError):
    def __init__(self, status: int, body: bytes):
        super().__init__("upstream HTTP %d: %s" % (status, body[:300].decode("utf-8", "replace")))
        self.status, self.body = status, body


class Upstream:
    def __init__(self, base: str, api_key: str = "", timeout: float = 3600.0):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self, extra=None):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        h.update(extra or {})
        return h

    def raw(self, method: str, path: str, body: bytes | None = None, headers=None):
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=self._headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()

    def post_json(self, path: str, obj: dict) -> dict:
        st, _, body = self.raw("POST", path, json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        if st >= 400:
            raise UpstreamError(st, body)
        return json.loads(body)

    def chat(self, body: dict) -> dict:
        body = {k: v for k, v in body.items() if k not in ("stream", "stream_options")}
        return self.post_json("/v1/chat/completions", body)

    def chat_stream(self, body: dict) -> "StreamCall":
        """Apre /v1/chat/completions in streaming. Iterare l'oggetto dà i chunk JSON; close() chiude la connessione
        (Strata vede la disconnessione e annulla la generazione)."""
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        return StreamCall(self, body)

    def slot(self, action: str, filename: str) -> dict:
        return self.post_json("/slots/0?action=%s" % action, {"filename": filename})


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
