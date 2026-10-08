# Author: Maurizio Verde — LastReload
"""Server MCP (Model Context Protocol) in sola lettura sull'archivio del proxy.

    python3 -m ctxproxy.mcp_server --db data/archive.sqlite                 # stdio (Claude Code, Codex, Hermes)
    python3 -m ctxproxy.mcp_server --db data/archive.sqlite --http 8133     # HTTP "streamable" su /mcp

Strumenti:
  recall         come lo strumento recall del proxy con recall_struct/recall_multi/recall_flex attivi (recall2.py): id,
                 query, queries, path, mode first/timeline/neighbors/output/reasoning/tree, filtri role/seg/from/to;
                 `conversation` facoltativo (di serie la conversazione più recente; con `id` quella del pezzo)
  conversations  elenco delle conversazioni archiviate (messaggi, token, segmenti, primo messaggio dell'utente)
  journal        eventi recenti del giornale del proxy (journal.jsonl accanto al database), filtrabili

Il database si apre in SOLA LETTURA (sqlite URI mode=ro): il proxy può continuare a scriverci. L'indice dei passaggi
di recall2 vive in tabelle TEMP di questa connessione (costruito alla prima ricerca su ogni conversazione), quindi
nessuna scrittura sul file. Protocollo: JSON-RPC 2.0, un messaggio per riga su stdio (initialize, tools/list,
tools/call, ping). Solo libreria standard.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import Config, Journal, Manager, Store, TokenCounter
from .recall2 import Recall2

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "mnemonic-archive", "version": "0.2.0"}
TEMP_SCHEMA = """
CREATE TEMP TABLE IF NOT EXISTS passages(pid INTEGER PRIMARY KEY, rid TEXT, conv TEXT, idx INT, role TEXT, off INT,
    len INT);
CREATE INDEX IF NOT EXISTS temp.passages_rid ON passages(rid);
CREATE INDEX IF NOT EXISTS temp.passages_conv ON passages(conv, idx);
CREATE TEMP TABLE IF NOT EXISTS passages_done(rid TEXT, kind TEXT, PRIMARY KEY(rid, kind));
CREATE VIRTUAL TABLE IF NOT EXISTS temp.p_raw USING fts5(t, content='', tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS temp.p_norm USING fts5(t, content='', tokenize='unicode61 remove_diacritics 2');
"""

RECALL_DESC = (
    "Search the EXACT archive of past conversations kept by the context proxy (tool outputs, messages, reasoning, "
    "tool-call arguments). Use `query` (or `queries` for several variants at once) to search words, paths, function "
    "names, errors; `id` for the full text of a piece (with `offset` to continue); `path` for the history of a file "
    "(mode timeline, or first = first write); with `id`, mode neighbors/output/reasoning for nearby messages, the "
    "output of a call or the reasoning of the same turn; mode=tree for a map of the session. 'Strong' results = all "
    "words in the same passage. Read-only. Result headers and notes are in Italian.")


def recall_schema() -> dict:
    s = {"type": "string"}
    i = {"type": "integer"}
    return {"type": "object", "properties": {
        "conversation": {**s, "description": "conversation id (see conversations); default: the most recent"},
        "id": {**s, "description": "id of an archived piece, e.g. r1a2b3c4d5e6"},
        "query": {**s, "description": "text to search"},
        "queries": {"type": "array", "items": s, "description": "several search variants in one call"},
        "path": {**s, "description": "file path or name: history of operations"},
        "mode": {"type": "string", "enum": ["timeline", "first", "neighbors", "output", "reasoning", "tree"]},
        "order": {"type": "string", "enum": ["pertinenza", "tempo"]},
        "role": {**s, "description": "filter: user, assistant, reasoning, args, tool"},
        "seg": {**i, "description": "filter: segment"},
        "from": {**i, "description": "filter: from message number"},
        "to": {**i, "description": "filter: up to message number"},
        "n": {**i, "description": "with mode=neighbors: messages before and after"},
        "offset": {**i, "description": "with id: character offset to continue from"},
    }}


TOOLS = [
    {"name": "recall", "description": RECALL_DESC, "inputSchema": recall_schema(),
     "annotations": {"readOnlyHint": True}},
    {"name": "conversations", "description": "Conversations in the proxy's archive (most recent first): id, "
                                             "messages, tokens, segments, date, start of the user's first "
                                             "message. Read-only.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "how many (default 20)"},
         "query": {"type": "string", "description": "filter: text contained in the user's first message or in "
                                                    "the id"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "journal", "description": "Recent events of the proxy journal (requests, recall, masking, segments, "
                                       "saves...), most recent last. Read-only.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "how many events (default 30, max 500)"},
         "event": {"type": "string", "description": "filter by type (e.g. recall, request, autosave); several "
                                                    "types separated by commas"},
         "conversation": {"type": "string", "description": "filter by conversation"}}},
     "annotations": {"readOnlyHint": True}},
]


def open_readonly(path: str) -> Store:
    """Store sopra un database aperto in sola lettura (niente schema/migrazioni: nessuna scrittura)."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    uri = "file:%s?mode=ro" % os.path.abspath(path).replace("?", "%3f").replace("#", "%23")
    st = Store.__new__(Store)
    st.db = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
    st.lock = threading.RLock()
    names = {r[0] for r in st.db.execute("SELECT name FROM sqlite_master")}
    if "archive" not in names:
        raise ValueError("%s is not a proxy archive (no archive table)" % path)
    st.fts = "archive_fts" in names
    for tbl, ddl in (("fileops", "CREATE TEMP TABLE fileops(conv TEXT, idx INT, call_id TEXT, op TEXT, path TEXT, "
                                 "outcome TEXT, rid_args TEXT, rid_out TEXT, head TEXT)"),
                     ("segments", "CREATE TEMP TABLE segments(conv TEXT, seg INT, cut_idx INT, cut_h TEXT, "
                                  "notes_msg TEXT, kind TEXT, created REAL, pins_msg TEXT)"),
                     ("masks", "CREATE TEMP TABLE masks(h TEXT, conv TEXT, idx INT, rid TEXT, tokens INT, "
                               "created REAL, saved INT)")):
        if tbl not in names:            # archivi di versioni vecchie
            st.db.execute(ddl)
    st.db.executescript(TEMP_SCHEMA)
    return st


class Archive:
    def __init__(self, db: str, journal: str | None = None, max_tokens: int = 6000, tokenizer: str | None = None):
        self.path = db
        self.store = open_readonly(db)
        self.journal_path = journal if journal is not None else os.path.join(os.path.dirname(os.path.abspath(db)),
                                                                             "journal.jsonl")
        self.cfg = Config(recall_struct=True, recall_multi=True, recall_flex=True,
                          recall_struct_max_tokens=max_tokens, recall_max_tokens=max_tokens)
        self.mgr = Manager(self.cfg, self.store, TokenCounter(self.cfg.chars_per_token, tokenizer), Journal(None))
        self.r2 = Recall2.__new__(Recall2)
        # come Recall2.__init__, ma l'indice dei passaggi è già nelle tabelle TEMP (niente P_SCHEMA sul file)
        from .recall2 import PassageIndex, Structure
        self.r2.mgr, self.r2.cfg, self.r2.st, self.r2.tc = self.mgr, self.cfg, self.store, self.mgr.tc
        self.r2.pi = PassageIndex.__new__(PassageIndex)
        self.r2.pi.st = self.store
        self.r2.sx = Structure(self.store)
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- strumenti
    def latest_conv(self) -> str | None:
        with self.store.lock:
            r = self.store.db.execute("SELECT conv FROM archive GROUP BY conv ORDER BY max(created) DESC LIMIT 1"
                                      ).fetchone()
        return r[0] if r else None

    def recall(self, args: dict) -> str:
        args = dict(args or {})
        conv = str(args.pop("conversation", "") or "").strip()
        rid = str(args.get("id") or "").strip().removeprefix("recall:").removeprefix("id=")
        if not conv and rid:
            r = self.store.get(rid)
            conv = r[1] if r else ""
        conv = conv or self.latest_conv()
        if not conv:
            return "empty archive"
        with self.lock:
            out = self.r2.recall(conv, args, None, [])
        return "[conversation %s]\n%s" % (conv, out)

    def conversations(self, args: dict) -> str:
        limit = max(1, min(int((args or {}).get("limit") or 20), 500))
        q = str((args or {}).get("query") or "").strip().lower()
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT conv, count(*), coalesce(sum(tokens), 0), min(created), max(created), "
                "(SELECT content FROM archive b WHERE b.conv=a.conv AND b.role='user' ORDER BY idx LIMIT 1) "
                "FROM archive a GROUP BY conv ORDER BY max(created) DESC").fetchall()
            segs = dict(self.store.db.execute("SELECT conv, count(*) FROM segments GROUP BY conv").fetchall())
        out = []
        for conv, n, tok, t0, t1, first in rows:
            first = " ".join((first or "").split())
            if q and q not in conv.lower() and q not in first.lower():
                continue
            out.append({"conversation": conv, "messages": n, "tokens": tok, "segments": segs.get(conv, 0),
                        "first": _ts(t0), "last": _ts(t1), "first_user": first[:200]})
            if len(out) >= limit:
                break
        return json.dumps({"total": len(rows), "conversations": out}, ensure_ascii=False, indent=1)

    def journal(self, args: dict) -> str:
        args = args or {}
        limit = max(1, min(int(args.get("limit") or 30), 500))
        kinds = {k.strip() for k in str(args.get("event") or "").split(",") if k.strip()}
        conv = str(args.get("conversation") or "").strip()
        if not os.path.exists(self.journal_path):
            return "journal not found: %s" % self.journal_path
        out = []
        for line in _tail_lines(self.journal_path, max(limit * 50, 2000) if (kinds or conv) else limit):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if kinds and e.get("event") not in kinds:
                continue
            if conv and e.get("conv") != conv:
                continue
            for k in ("messages", "tools"):
                e.pop(k, None)
            out.append(e)
        out = out[-limit:]
        return "\n".join(json.dumps(e, ensure_ascii=False)[:2000] for e in out) or "no events"

    def call(self, name: str, args: dict) -> str:
        if name == "recall":
            return self.recall(args)
        if name == "conversations":
            return self.conversations(args)
        if name == "journal":
            return self.journal(args)
        raise KeyError(name)


def _ts(t):
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else None


def _tail_lines(path: str, n: int) -> list[str]:
    """Ultime n righe di un file anche grande (lettura a blocchi dalla fine)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        buf, pos = b"", end
        while pos > 0 and buf.count(b"\n") <= n:
            step = min(1 << 20, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
    lines = buf.decode("utf-8", "replace").splitlines()
    if pos > 0:
        lines = lines[1:]          # la prima può essere a metà
    return lines[-n:]


# -------------------------------------------------------------------- JSON-RPC
class McpServer:
    def __init__(self, archive: Archive):
        self.arc = archive

    def handle(self, msg: dict) -> dict | None:
        """Un messaggio JSON-RPC -> risposta (None per le notifiche)."""
        mid = msg.get("id")
        method = msg.get("method")
        if method is None:              # risposta del client a una nostra richiesta: non ne facciamo
            return None
        if mid is None:                 # notifica (notifications/initialized, cancelled...)
            return None
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                v = params.get("protocolVersion")
                return _ok(mid, {"protocolVersion": v if v in PROTOCOLS else PROTOCOLS[0],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": SERVER_INFO,
                                 "instructions": "Exact archive of the context proxy's conversations: use recall "
                                                 "to search, conversations for the list, journal for events. "
                                                 "Read-only."})
            if method == "ping":
                return _ok(mid, {})
            if method == "tools/list":
                return _ok(mid, {"tools": TOOLS})
            if method == "tools/call":
                name = params.get("name")
                if name not in {t["name"] for t in TOOLS}:
                    return _err(mid, -32602, "unknown tool: %s" % name)
                try:
                    text = self.arc.call(str(name), params.get("arguments") or {})
                    return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
                except Exception as e:  # noqa: BLE001  errore dello strumento: lo vede il modello
                    return _ok(mid, {"content": [{"type": "text", "text": "errore: %r" % e}], "isError": True})
            if method in ("resources/list", "prompts/list"):
                return _ok(mid, {method.split("/")[0]: []})
            if method == "resources/templates/list":
                return _ok(mid, {"resourceTemplates": []})
            return _err(mid, -32601, "method not supported: %s" % method)
        except Exception as e:  # noqa: BLE001
            return _err(mid, -32603, "internal error: %r" % e)

    def handle_any(self, data):
        """Messaggio singolo o batch (lista)."""
        if isinstance(data, list):
            out = [r for r in (self.handle(m) for m in data if isinstance(m, dict)) if r is not None]
            return out or None
        if not isinstance(data, dict):
            return _err(None, -32600, "invalid request")
        return self.handle(data)

    # ---------- stdio ----------
    def serve_stdio(self, inp=None, out=None):
        inp = inp or sys.stdin.buffer
        out = out or sys.stdout.buffer
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                resp = _err(None, -32700, "invalid JSON")
            else:
                resp = self.handle_any(data)
            if resp is not None:
                out.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
                out.flush()

    # ---------- HTTP (streamable, risposte JSON semplici) ----------
    def serve_http(self, host: str, port: int, path: str = "/mcp"):
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, st, obj=None, extra=None):
                data = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(st)
                if obj is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                # nessun flusso SSE dal server (non mandiamo notifiche): consentito dalla specifica
                self._send(405, extra={"Allow": "POST"})

            def do_DELETE(self):
                self._send(200 if self.path.split("?")[0] == path else 404)

            def do_POST(self):
                if self.path.split("?")[0] != path:
                    return self._send(404, {"error": "usa %s" % path})
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(n) or b"null")
                except ValueError:
                    return self._send(400, _err(None, -32700, "invalid JSON"))
                resp = srv.handle_any(data)
                if resp is None:
                    return self._send(202)
                self._send(200, resp)

        httpd = ThreadingHTTPServer((host, port), H)
        httpd.daemon_threads = True
        print("[mnemonic-mcp] http://%s:%d%s (read-only: %s)" % (host, port, path, self.arc.path),
              file=sys.stderr, flush=True)
        httpd.serve_forever()


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mnemonic-mcp",
                                 description="Read-only MCP server over the Mnemonic Proxy archive "
                                             "(tools: recall, conversations, journal).")
    ap.add_argument("--db", required=True, help="the proxy's archive.sqlite (opened read-only)")
    ap.add_argument("--journal", help="journal.jsonl (default: next to the database)")
    ap.add_argument("--max-tokens", type=int, default=6000, help="token cap of one recall result (default: 6000)")
    ap.add_argument("--tokenizer", help="HF tokenizer.json for token counts (optional)")
    ap.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP on /mcp instead of stdio")
    ap.add_argument("--host", default="127.0.0.1", help="bind address for --http (default: 127.0.0.1)")
    a = ap.parse_args(argv)
    srv = McpServer(Archive(a.db, a.journal, a.max_tokens, a.tokenizer))
    if a.http:
        srv.serve_http(a.host, a.http)
    else:
        srv.serve_stdio()


if __name__ == "__main__":
    main()
