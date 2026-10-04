#!/usr/bin/env python3
"""Dashboard live del "contesto virtuale" (solo stdlib, sola lettura).

Legge:
  - Strata  GET /status, GET /metrics           (HTTP, nessuna scrittura)
  - proxy   <data>/journal.jsonl                (coda incrementale del giornale)
            <data>/archive.sqlite               (sola lettura, per stimare la storia virtuale)
            <data>/live/last_request.json       (se il proxy gira con live_dump)
Serve la pagina (v2) su 127.0.0.1 (default 8097), che interroga /api/state ogni 1 s; /v1 = pagina precedente.
"""
import argparse
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_EVENTS = 50000


def http_json(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


class JournalTail:
    """Legge solo le righe nuove (complete) di journal.jsonl; gestisce troncamento/rotazione."""

    def __init__(self, path: str):
        self.path, self.off, self.ino, self.buf = path, 0, None, b""
        self.events: list[dict] = []
        self.base = 0  # indice assoluto del primo elemento di self.events

    def poll(self) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return
        if self.ino != st.st_ino or st.st_size < self.off:  # nuovo file: si riparte
            self.ino, self.off, self.buf = st.st_ino, 0, b""
            self.base += len(self.events)
            self.events = []
        if st.st_size == self.off:
            return
        with open(self.path, "rb") as f:
            f.seek(self.off)
            data = f.read()
        self.off += len(data)
        data = self.buf + data
        lines = data.split(b"\n")
        self.buf = lines.pop()  # riga parziale (scrittura in corso)
        for ln in lines:
            if not ln.strip():
                continue
            try:
                self.events.append(json.loads(ln))
            except ValueError:
                continue
        if len(self.events) > MAX_EVENTS:
            cut = len(self.events) - MAX_EVENTS
            self.events = self.events[cut:]
            self.base += cut

    def after(self, n: int) -> tuple[int, list[dict]]:
        """eventi con indice assoluto >= n -> (indice del primo restituito, eventi)."""
        start = max(n, self.base)
        return start, self.events[start - self.base:]


class VirtualEstimator:
    """Stima della storia virtuale (completa, lato client) di ogni conversazione nel tempo, dall'archivio.

    L'archivio registra ogni messaggio la prima volta che il proxy lo vede (created). Per l'istante t la storia
    virtuale ≈ somma, per ogni indice di messaggio, dei token dell'ultima versione vista entro t (messaggio +
    ragionamento). Gli argomenti delle tool call sono già contati nel messaggio assistant. È una stima: i rami
    più corti della storia precedente lasciano contati i messaggi oltre il ramo."""

    def __init__(self, path: str):
        self.path = path
        self.last_created = 0.0
        self.pending: list[tuple] = []  # (created, conv, idx, kind, tokens) ordinati
        self.cur: dict[str, dict] = {}  # conv -> {(idx, kind): tokens}
        self.tot: dict[str, int] = {}
        self.err = None

    def poll(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            db = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=1.0)
            try:
                rows = db.execute(
                    "SELECT created, conv, idx, role, tokens FROM archive WHERE created > ? "
                    "AND role != 'assistant-tool-args' ORDER BY created", (self.last_created,)).fetchall()
            finally:
                db.close()
            self.err = None
        except sqlite3.Error as e:
            self.err = str(e)
            return
        for c, conv, idx, role, tok in rows:
            self.pending.append((c, conv, idx, "r" if role == "assistant-reasoning" else "m", tok or 0))
        if rows:
            self.last_created = rows[-1][0]

    def at(self, ts: float, conv: str | None):
        """applica le righe con created <= ts e restituisce la stima per conv."""
        k = 0
        for k, (c, cv, idx, kind, tok) in enumerate(self.pending):
            if c > ts:
                break
            d = self.cur.setdefault(cv, {})
            self.tot[cv] = self.tot.get(cv, 0) - d.get((idx, kind), 0) + tok
            d[(idx, kind)] = tok
        else:
            k = len(self.pending)
        del self.pending[:k]
        return self.tot.get(conv) if conv else None


class State:
    def __init__(self, strata: str, data: str):
        self.strata, self.data = strata.rstrip("/"), data
        self.journal = JournalTail(os.path.join(data, "journal.jsonl"))
        self.virt = VirtualEstimator(os.path.join(data, "archive.sqlite"))
        self.series: list[dict] = []  # un punto per evento "request"
        self.done = 0                 # eventi del giornale già trasformati in punti
        self.offset: dict[str, int] = {}
        self.lock = threading.Lock()
        self.proxy = "http://127.0.0.1:8096"

    def refresh(self) -> None:
        with self.lock:
            self.journal.poll()
            self.virt.poll()
            start, evs = self.journal.after(self.done)
            for e in evs:
                if e.get("event") == "request":
                    v = e.get("virtual_tokens")
                    est = self.virt.at(e.get("ts", 0), e.get("conv"))
                    # l'archivio non contiene le definizioni degli strumenti né l'overhead del template: si calibra
                    # alla prima richiesta della conversazione (lì fisico = virtuale, nessun masking).
                    c = e.get("conv")
                    if est is not None and c not in self.offset and e.get("est_tokens"):
                        self.offset[c] = max(0, e["est_tokens"] - est)
                    if est is not None:
                        est += self.offset.get(c, 0)
                    self.series.append({
                        "ts": e.get("ts"), "conv": e.get("conv"), "round": e.get("round"),
                        "phys": e.get("prompt_tokens"), "phys_est": e.get("est_tokens"),
                        "virtual": v if v is not None else est, "virtual_src": "proxy" if v is not None else "stima",
                        "reused": e.get("reused"), "read": e.get("prompt_read"), "prompt_ms": e.get("prompt_ms"),
                        "out": e.get("completion_tokens"), "ms": e.get("ms"), "finish": e.get("finish"),
                        "messages": e.get("messages")})
            self.done = start + len(evs)
            if len(self.series) > MAX_EVENTS:
                del self.series[:len(self.series) - MAX_EVENTS]

    def live_request(self):
        p = os.path.join(self.data, "live", "last_request.json")
        try:
            st = os.stat(p)
        except FileNotFoundError:
            return None, None
        return st.st_mtime, p

    def snapshot(self, ev_after: int, pt_after: int, lr_mtime: float | None) -> dict:
        self.refresh()
        status, e1 = http_json(self.strata + "/status")
        metrics, e2 = http_json(self.strata + "/metrics")
        m = None
        if metrics:
            m = {k: metrics.get(k) for k in ("engine", "live", "requests", "totals", "hardware", "time")}
            if m.get("engine"):
                m["engine"] = {k: m["engine"].get(k) for k in
                               ("model", "max_context", "context", "kv", "kv_resident", "spec", "version",
                                "vram_free_mib", "conversation_cache_slots")}
        with self.lock:
            st_ev, evs = self.journal.after(ev_after)
            if len(evs) > 2000:
                st_ev += len(evs) - 2000
                evs = evs[-2000:]
            total_pts = len(self.series)
            pts = self.series[pt_after:] if pt_after <= total_pts else self.series[:]
            pt_start = pt_after if pt_after <= total_pts else 0
        out = {
            "now": time.time(), "strata_url": self.strata, "data_dir": self.data,
            "status": status, "metrics": m, "errors": {"status": e1, "metrics": e2, "archive": self.virt.err},
            "journal": {"exists": os.path.exists(self.journal.path), "start": st_ev, "events": evs,
                        "next": st_ev + len(evs)},
            "series": {"start": pt_start, "points": pts, "next": pt_start + len(pts)},
            "live_request": None,
        }
        mt, p = self.live_request()
        out["live_request_mtime"] = mt
        if mt is not None and mt != lr_mtime:
            try:
                with open(p, encoding="utf-8") as f:
                    out["live_request"] = json.load(f)
            except (OSError, ValueError) as e:
                out["errors"]["live_request"] = str(e)
                out["live_request_mtime"] = lr_mtime
        return out


def make_handler(state: State):
    page = os.path.join(HERE, "index.html")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, data: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path, _, q = self.path.partition("?")
            args = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)
            if path in ("/", "/index.html"):
                with open(page, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                def num(k, d=0.0):
                    try:
                        return float(args.get(k, d))
                    except ValueError:
                        return d
                lr = args.get("lr")
                snap = state.snapshot(int(num("ev")), int(num("pt")), float(lr) if lr not in (None, "", "null") else None)
                return self._send(200, json.dumps(snap, ensure_ascii=False).encode("utf-8"), "application/json")
            if path == "/health":
                return self._send(200, b'{"ok": true}', "application/json")
            if path == "/api/marks" and args.get("conv"):
                st, data = proxy_call(state.proxy + "/v1/strata/marks/" + args["conv"])
                return self._send(st, data, "application/json")
            self._send(404, b'{"error": "not found"}', "application/json")

        def do_POST(self):
            # 📌 / 🗑 dalla dashboard: inoltro al proxy (unica scrittura della dashboard, solo localhost)
            if self.path.split("?")[0] != "/api/marks":
                return self._send(404, b'{"error": "not found"}', "application/json")
            n = int(self.headers.get("Content-Length") or 0)
            st, data = proxy_call(state.proxy + "/v1/strata/marks", self.rfile.read(n) if n else b"{}")
            self._send(st, data, "application/json")

    return H


def proxy_call(url: str, body: bytes | None = None):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError as e:
        return 502, json.dumps({"error": "proxy non raggiungibile: %s" % e}).encode()


def main(argv=None):
    ap = argparse.ArgumentParser(description="dashboard live contesto virtuale (sola lettura)")
    ap.add_argument("--strata", default="http://127.0.0.1:8095")
    ap.add_argument("--data", required=True, help="data_dir del proxy (journal.jsonl, archive.sqlite, live/)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8097)
    ap.add_argument("--proxy", default="http://127.0.0.1:8096", help="ctx-proxy (per 📌 / 🗑)")
    a = ap.parse_args(argv)
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        ap.error("solo localhost (privacy: il prompt resta sulla macchina)")
    state = State(a.strata, a.data)
    state.proxy = a.proxy.rstrip("/")
    state.refresh()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(state))
    print("[ctx-dashboard] http://%s:%d  strata=%s data=%s" % (a.host, a.port, a.strata, a.data), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
