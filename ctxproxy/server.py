"""Server HTTP del proxy (OpenAI-compatibile) davanti a Strata.

    python3 -m ctxproxy.server --upstream http://127.0.0.1:8095 --port 8096 --data ./data

Solo stdlib. Una richiesta alla volta (Strata ha una sola sequenza: serializzare qui evita che due client
intreccino ciclo recall e note).

Streaming: con `stream: true` il proxy chiede a Strata lo stream e inoltra al client ragionamento,
testo e tool call delta per delta. Le chiamate `strata_recall` (interne) non arrivano mai al client: il giro si
chiude lato proxy e il giro successivo continua lo stesso stream. Il ragionamento dei giri di recall arriva al
client (con una riga informativa) e il proxy lo ricorda per tenere stabile il prompt fisico al turno dopo.
Disconnessione del client (Esc in pi): il proxy chiude lo stream verso Strata, che annulla la generazione."""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import RECALL_NAME, Config, Journal, Manager, Store, TokenCounter, content_text, is_human_user
from .paging import GUARD_MSG, GUARD_NAMES, TOOLS_NAME, receipt_contaminated
from .upstream import Upstream, UpstreamError

LIMIT_MSG = ("strata_recall: limite di ricerche raggiunto per questa risposta. Non chiamare più "
             "strata_recall: rispondi ora con le informazioni già recuperate (o di' che non le hai).")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_./\-]{4,}|\d[\d.,:]{2,}")


class ClientGone(Exception):
    """Il client ha chiuso la connessione durante lo stream."""


class Proxy:
    def __init__(self, cfg: Config, upstream: Upstream, store: Store, journal: Journal, counter: TokenCounter,
                 mode: str = "tools"):
        self.cfg, self.up, self.store, self.journal, self.tc = cfg, upstream, store, journal, counter
        self.mode = mode
        self.mgr = Manager(cfg, store, counter, journal)
        self.lock = threading.Lock()
        self.autosaver = None
        self.engine = None          # engines.Engine | None (impostato da main; None = Strata)

    def enable_autosave(self, start: bool = True):
        """Avvolge l'upstream nel Tracker (ripristino automatico) e avvia il thread di autosalvataggio."""
        from .autosave import AutoSaver, Tracker
        if not isinstance(self.up, Tracker):
            self.up = Tracker(self.up, self.store, self.journal, self.cfg)
        self.autosaver = AutoSaver(self)
        if start:
            self.autosaver.start()
        return self.autosaver

    # ------------------------------------------------------------------
    def chat(self, req: dict, hint: str | None = None, emit=None) -> tuple[int, dict, dict]:
        """-> (status, corpo risposta non-stream, header extra). Con `emit` (callable(chunk_dict|None)) la risposta
        viene anche trasmessa in streaming chunk per chunk; None = keep-alive."""
        if self.mode == "off":
            t0 = time.time()
            try:
                r = self._round(req, emit, None) if emit else self.up.chat(req)
            except UpstreamError as e:
                return e.status, _err_body(e), {}
            self._log_request("off", None, r, t0, 0, 0)
            if emit:
                r.pop("_sent_content", None)
                r.pop("_sent_reasoning", None)
                fin = {k: r.get(k) for k in ("id", "created", "model")}
                emit({**fin, "object": "chat.completion.chunk", "usage": r.get("usage"), "timings": r.get("timings"),
                      "choices": [{"index": 0, "delta": {}, "finish_reason": r["choices"][0].get("finish_reason")}]})
            return 200, r, {}
        with self.lock:
            return self._chat_managed(req, hint, emit)

    def _prepare(self, req, hint, emit):
        """prepare() può durare (pacchetto mask + ancora, switch con note): in streaming tiene viva la connessione."""
        if emit is None:
            return self.mgr.prepare(req, upstream=self.up, hint=hint)
        box = {}

        def run():
            try:
                box["p"] = self.mgr.prepare(req, upstream=self.up, hint=hint)
            except BaseException as e:  # noqa: BLE001
                box["e"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        while t.is_alive():
            t.join(2.0)
            if t.is_alive():
                emit(None)  # se il client è andato via solleva ClientGone; prepare finisce comunque da solo
        if "e" in box:
            raise box["e"]
        return box["p"]

    def _chat_managed(self, req, hint, emit=None):
        t0 = time.time()
        rid_req = "q" + uuid.uuid4().hex[:10]
        p = self._prepare(req, hint, emit)
        if hasattr(self.up, "ctx"):
            self.up.ctx = (p.conv, p.seg)
        phys = list(p.messages)
        internal: list[dict] = []
        recalls, recall_logs = [], []
        strip_c, strip_r = [], []
        body = {k: v for k, v in req.items() if k not in ("messages", "tools", "stream", "stream_options")}
        resp = None
        msgs_c = req.get("messages") or []
        last_user = max((i for i, m in enumerate(msgs_c) if is_human_user(m)), default=None)
        lu_text = content_text(msgs_c[last_user].get("content"))[:300] if last_user is not None else ""
        stream_state = {"sent_role": False, "client_calls": 0} if emit else None
        for rnd in range(self.cfg.max_recall_rounds + 2):
            self._live(p, phys, rnd, body)
            if stream_state is not None:
                stream_state["internal"] = self._internal_names(p, phys)
            try:
                if emit:
                    resp = self._round({**body, "messages": phys, "tools": p.tools}, emit, stream_state)
                else:
                    resp = self.up.chat({**body, "messages": phys, "tools": p.tools})
            except UpstreamError as e:
                self.journal.log("upstream_error", conv=p.conv, status=e.status, error=str(e)[:300])
                return e.status, _err_body(e), {}
            except ClientGone:
                self.journal.log("client_disconnect", conv=p.conv, round=rnd, req=rid_req,
                                 ms=round((time.time() - t0) * 1000))
                raise
            self._log_request("managed", p, resp, t0, rnd, len(phys))
            self._live(p, phys, rnd, body, resp)
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            internal_names = self._internal_names(p, phys)
            blocked = [c for c in calls if self.cfg.receipt_guard and receipt_contaminated(
                (c.get("function") or {}).get("name"), (c.get("function") or {}).get("arguments"))]
            mine = [c for c in calls if (c.get("function") or {}).get("name") in internal_names or c in blocked]
            if not mine:
                break
            others = [c for c in calls if c not in mine]
            if others or rnd > self.cfg.max_recall_rounds:
                # chiamate miste (o troppi giri): al client vanno solo le sue; la recall non eseguita sparisce.
                msg["tool_calls"] = others or None
                if not others:
                    msg.pop("tool_calls", None)
                    resp["choices"][0]["finish_reason"] = "stop"
                self.journal.log("recall_dropped", conv=p.conv, count=len(mine), mixed=bool(others), round=rnd,
                                 names=sorted({(c.get("function") or {}).get("name") for c in mine}))
                break
            # ciclo tool lato server: assistant(recall) + risultati, poi di nuovo a Strata
            amsg = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": mine}
            if msg.get("reasoning_content"):
                amsg["reasoning_content"] = msg["reasoning_content"]
            new = [amsg]
            if emit:
                # ciò che il client ha già visto di questo giro: verrà tolto dal suo messaggio al turno dopo
                strip_c.append(resp.get("_sent_content", ""))
                strip_r.append(resp.get("_sent_reasoning", ""))
            for c in mine:
                ts = time.time()
                a = (c.get("function") or {}).get("arguments")
                cname = (c.get("function") or {}).get("name")
                meta: list = []
                if c in blocked:
                    out = GUARD_MSG
                    new.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
                    self.journal.log("receipt_guard", conv=p.conv, round=rnd, name=cname, args=str(a)[:500])
                    if emit:
                        info = "\n[gestore del contesto: chiamata %s bloccata (copia di una ricevuta)]\n" % cname
                        self._emit_delta(emit, stream_state, resp, {"reasoning_content": info})
                        strip_r[-1] += info
                    continue
                if cname != RECALL_NAME:
                    out = self._tools_call(p, phys + new, cname, a, rnd)
                    new.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
                    if emit:
                        info = "\n[%s: definizioni caricate]\n" % (TOOLS_NAME if cname == TOOLS_NAME else cname)
                        self._emit_delta(emit, stream_state, resp, {"reasoning_content": info})
                        strip_r[-1] += info
                    continue
                if rnd == self.cfg.max_recall_rounds:
                    # ultimo giro: niente nuova ricerca, il modello deve rispondere con ciò che ha (G3)
                    out = LIMIT_MSG
                else:
                    out = self.mgr.recall(p.conv, a, max_idx=last_user, meta=meta)
                    # ricontrollo del budget: il risultato non deve far uscire il prompt dalla finestra
                    room = self.cfg.window - self.cfg.reserve - int(body.get("max_tokens") or
                                                                     self.cfg.default_response) \
                        - self.mgr.estimate(phys + new, p.tools)[0] - 64
                    if self.tc.count(out) > max(room, 0):
                        out = self.mgr.fit_tokens(out, max(room, 0))
                        self.journal.log("recall_trimmed", conv=p.conv, room=room, round=rnd)
                new.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
                rec = self.journal.log("recall", conv=p.conv, req=rid_req, round=rnd, last_user=lu_text,
                                       args=a, results=meta, chars=len(out), tokens=self.tc.count(out),
                                       ms=round((time.time() - ts) * 1000, 2))
                recalls.append({k: rec[k] for k in ("args", "tokens")})
                recall_logs.append((rec, out))
                if emit:
                    info = "\n[strata_recall %s: %d risultati]\n" % (_short(a), len(meta))
                    self._emit_delta(emit, stream_state, resp, {"reasoning_content": info})
                    strip_r[-1] += info
            phys += new
            internal += new
        if internal and p.hs:
            self.mgr.remember_internal(p.conv, p.hs[-1], internal, "".join(strip_c), "".join(strip_r))
        if recall_logs:
            self._recall_use(p.conv, rid_req, recall_logs, resp["choices"][0]["message"])
        st = self.store.stats(p.conv)
        u = resp.get("usage") or {}
        ctx = {"conversation_id": p.conv, "mode": self.mode, "segment": p.seg,
               "active_tokens": u.get("prompt_tokens"), "active_tokens_est": p.est_tokens,
               "virtual_tokens": p.virtual_tokens, "masked": p.masked, "masked_tokens": p.masked_tokens,
               "masked_saved": p.masked_saved,
               "archived": st["archived"], "recalls": recalls,
               "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
               "prompt_read": (resp.get("timings") or {}).get("prompt_n"),
               "invalidated_suffix_tokens": (p.invalidated or {}).get("tokens", 0),
               "invalidated_cause": (p.invalidated or {}).get("cause"),
               "auto_recall": next(({"injected": e.get("injected"), "tokens": e.get("tokens"),
                                     "ids": [x["rid"] for x in e.get("pieces") or []]}
                                    for e in p.events if e.get("event") == "auto_recall"), None),
               "events": [e["event"] for e in p.events]}
        resp["strata_context"] = ctx
        for k in ("_sent_content", "_sent_reasoning"):
            resp.pop(k, None)
        hdr = {"X-Strata-Context": "seg=%d active=%s virtual=%d masked=%d recalls=%d cached=%s" % (
            p.seg, u.get("prompt_tokens"), p.virtual_tokens, p.masked, len(recalls), ctx["cached_tokens"])}
        if emit:
            fin = {"id": resp.get("id"), "object": "chat.completion.chunk", "created": resp.get("created"),
                   "model": resp.get("model"),
                   "choices": [{"index": 0, "delta": {}, "finish_reason": resp["choices"][0].get("finish_reason")}],
                   "usage": resp.get("usage"), "strata_context": ctx}
            if resp.get("timings"):
                fin["timings"] = resp["timings"]
            emit(fin)
        return 200, resp, hdr

    # ---------- streaming ----------
    def _emit_delta(self, emit, state, resp, delta):
        if state is None:
            return
        base = {"id": resp.get("id") or "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion.chunk",
                "created": resp.get("created") or int(time.time()), "model": resp.get("model")}
        if not state["sent_role"]:
            emit({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                       "finish_reason": None}]})
            state["sent_role"] = True
        emit({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})

    def _round(self, body, emit, state):
        """Un giro in streaming da Strata. Inoltra subito ragionamento/testo/tool call del client; trattiene le
        chiamate strata_recall. -> risposta non-stream ricostruita (+ _sent_content/_sent_reasoning)."""
        if state is None:
            state = {"sent_role": False, "client_calls": 0, "final": True}
        call = self.up.chat_stream(body)
        content, reasoning = [], []
        sent_c, sent_r = [], []
        calls: dict[int, dict] = {}      # indice upstream -> {id, name, args, mine, cidx}
        finish, usage, timings, meta = None, None, None, {}
        last_ka = time.monotonic()
        try:
            for ch in call:
                if not meta:
                    meta = {k: ch.get(k) for k in ("id", "created", "model")}
                if ch.get("usage"):
                    usage = ch["usage"]
                if ch.get("timings"):
                    timings = ch["timings"]
                choice = (ch.get("choices") or [None])[0]
                if not choice:
                    continue
                d = choice.get("delta") or {}
                out = {}
                if d.get("reasoning_content"):
                    reasoning.append(d["reasoning_content"])
                    out["reasoning_content"] = d["reasoning_content"]
                    sent_r.append(d["reasoning_content"])
                if d.get("content"):
                    content.append(d["content"])
                    out["content"] = d["content"]
                    sent_c.append(d["content"])
                fwd_calls = []
                for tc in d.get("tool_calls") or []:
                    i = tc.get("index", 0)
                    f = tc.get("function") or {}
                    c = calls.get(i)
                    if c is None:
                        name = f.get("name") or ""
                        c = calls[i] = {"id": tc.get("id"), "name": name,
                                        "args": "", "mine": name in state.get("internal", (RECALL_NAME,)),
                                        "cidx": None, "held": self.cfg.receipt_guard and name in GUARD_NAMES}
                        if not c["mine"] and not c["held"]:
                            c["cidx"] = state["client_calls"]
                            state["client_calls"] += 1
                    c["args"] += f.get("arguments") or ""
                    if not c["mine"] and not c["held"]:
                        x = {"index": c["cidx"], "function": {"arguments": f.get("arguments") or ""}}
                        if tc.get("id"):
                            x["id"] = tc["id"]
                            x["type"] = "function"
                        if f.get("name"):
                            x["function"]["name"] = f["name"]
                        fwd_calls.append(x)
                if fwd_calls:
                    out["tool_calls"] = fwd_calls
                if out:
                    self._emit_delta(emit, state, meta, out)
                    last_ka = time.monotonic()
                elif time.monotonic() - last_ka > 2.0:
                    emit(None)   # keep-alive (e verifica che il client ci sia ancora)
                    last_ka = time.monotonic()
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        except (ClientGone, BrokenPipeError, ConnectionResetError):
            call.close()
            raise ClientGone()
        finally:
            call.close()
        # chiamate con effetti trattenute dalla guardia: inoltrate intere solo se pulite
        for _, c in sorted(calls.items()):
            if c.get("held") and not receipt_contaminated(c["name"], c["args"]):
                c["cidx"] = state["client_calls"]
                state["client_calls"] += 1
                self._emit_delta(emit, state, meta, {"tool_calls": [
                    {"index": c["cidx"], "id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"]}}]})
        msg = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        tcs = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"]}}
               for _, c in sorted(calls.items())]
        if tcs:
            msg["tool_calls"] = tcs
        return {**meta, "object": "chat.completion",
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": usage or {}, "timings": timings or {},
                "_sent_content": "".join(sent_c), "_sent_reasoning": "".join(sent_r)}

    # ---------- strumenti su richiesta (paging.py) ----------
    def _internal_names(self, p, phys) -> set:
        """Nomi di chiamata gestiti dal proxy in questo giro: strata_recall, strata_tools e (con tools_paging) gli
        strumenti del catalogo NON ancora caricati nel prompt fisico (la chiamata non va al client: il proxy
        risponde con la definizione e chiede di ripetere la chiamata)."""
        names = {RECALL_NAME}
        if p.catalog:
            names.add(TOOLS_NAME)
            names |= set(p.catalog) - self.mgr.pager.loaded_in(phys)
        return names

    def _tools_call(self, p, phys, name, args, rnd) -> str:
        pager = self.mgr.pager
        loaded = pager.loaded_in(phys)
        try:
            a = json.loads(args) if isinstance(args, str) and args.strip() else (args or {})
        except ValueError:
            a = {"query": str(args)}
        a = a if isinstance(a, dict) else {"query": str(a)}
        if name == TOOLS_NAME:
            q = str(a.get("query") or "")
            names = pager.search(p.catalog, q, a.get("names"), limit=self.cfg.tools_load_max)
            out = pager.result(p.catalog, names, loaded, q)
            self.journal.log("tools_search", conv=p.conv, round=rnd, query=q, names_asked=a.get("names"),
                             found=names, loaded_new=[n for n in names if n not in loaded],
                             tokens=self.tc.count(out))
            return out
        # chiamata diretta a uno strumento del catalogo non caricato: definizione + richiesta di ripetere
        note = ("La chiamata a %s NON è stata eseguita: la sua definizione non era ancora caricata. Eccola: "
                "ripeti la chiamata con i parametri corretti." % name)
        out = pager.result(p.catalog, [name], loaded, name, note)
        self.journal.log("tool_not_loaded", conv=p.conv, round=rnd, name=name, args=str(args)[:500],
                         tokens=self.tc.count(out))
        return out

    # ---------- giornale recall: uso dei pezzi ----------
    # ---------- pin / usa e getta dalla dashboard ----------
    def marks(self, conv: str) -> dict:
        pins = [{"id": i, "index": x, "text": t, "created": c, "active": bool(a)}
                for i, x, t, c, a in self.store.pins(conv, active_only=False)]
        users = {i: c for i, c in self.store.user_messages(conv)}
        drops = [{"index": i, "text": (users.get(i) or "")[:300]} for i in sorted(self.store.drops(conv))]
        used = sum(self.tc.count(p["text"]) for p in pins if p["active"])
        return {"conversation_id": conv, "pins": pins, "drops": drops, "pins_tokens": used,
                "users": [{"index": i, "text": (c or "")[:240]} for i, c in sorted(users.items())][-200:],
                "pins_max_tokens": self.cfg.pins_max_tokens, "over_limit": used > self.cfg.pins_max_tokens}

    def mark_action(self, d: dict):
        """{"conv", "action": pin|unpin|update_pin|drop|undrop, "index"?, "id"?, "text"?}.
        pin su un messaggio qualsiasi: testo dato oppure il testo archiviato del messaggio.
        drop su un messaggio qualsiasi: vale per lo scambio che lo contiene (dal messaggio utente precedente)."""
        conv, act = d["conv"], d["action"]
        if act == "pin":
            idx = int(d["index"])
            text = (d.get("text") or "").strip()
            if not text:
                users = dict(self.store.user_messages(conv))
                text = (users.get(idx) or "").strip()
            if not text:
                return 400, {"error": {"message": "testo del punto fermo mancante"}}
            pid = self.store.add_pin(conv, idx, text)
            self.journal.log("pin", conv=conv, id=pid, index=idx, source="dashboard", text=text,
                             tokens=self.tc.count(text))
            return 200, {"ok": True, "id": pid, **self.marks(conv)}
        if act in ("unpin", "update_pin"):
            pid = int(d["id"])
            ok = self.store.update_pin(pid, text=d.get("text") if act == "update_pin" else None,
                                       active=False if act == "unpin" else None)
            self.journal.log(act, conv=conv, id=pid, source="dashboard")
            return (200 if ok else 404), {"ok": ok, **self.marks(conv)}
        if act in ("drop", "undrop"):
            idx = int(d["index"])
            users = sorted(i for i, _ in self.store.user_messages(conv) if i <= idx)
            if not users:
                return 400, {"error": {"message": "nessun messaggio utente prima dell'indice %d" % idx}}
            u = users[-1]
            if act == "drop":
                self.store.add_drop(conv, u)
            else:
                self.store.del_drop(conv, u)
            self.journal.log("drop_mark" if act == "drop" else "drop_unmark", conv=conv, index=u, source="dashboard")
            return 200, {"ok": True, "index": u, **self.marks(conv)}
        return 400, {"error": {"message": "azione sconosciuta %r" % act}}

    def _recall_use(self, conv, rid_req, recall_logs, final_msg):
        """Euristica economica: identificatori/numeri dei pezzi restituiti che ricompaiono nella risposta finale
        (testo, ragionamento, argomenti delle chiamate). Base per un futuro selettore addestrato."""
        try:
            ans = " ".join([final_msg.get("content") or "", final_msg.get("reasoning_content") or ""] +
                           [(c.get("function") or {}).get("arguments") or "" for c in final_msg.get("tool_calls") or []])
            ans_ids = set(IDENT.findall(ans))
            for rec, out in recall_logs:
                q = set(IDENT.findall(str(rec.get("args") or ""))) | set(IDENT.findall(rec.get("last_user") or ""))
                got = set(IDENT.findall(out)) - q
                hit = sorted(got & ans_ids)
                self.journal.log("recall_use", conv=conv, req=rid_req, round=rec.get("round"),
                                 results=[r.get("rid") for r in rec.get("results") or []],
                                 used=bool(hit), overlap=len(hit), sample=hit[:15])
        except Exception as e:  # noqa: BLE001
            self.journal.log("recall_use_error", error=repr(e)[:200])

    def _live(self, p, phys, rnd, body, resp=None):
        """live_dump: scrive in modo atomico data_dir/live/last_request.json col prompt fisico appena inviato a
        Strata e, a risposta arrivata, la risposta (prima: response.done=false). Solo file locale, mai via rete."""
        if not (self.cfg.live_dump and self.cfg.data_dir):
            return
        try:
            d = os.path.join(self.cfg.data_dir, "live")
            os.makedirs(d, exist_ok=True)
            r = {"done": False}
            if resp is not None:
                ch = (resp.get("choices") or [{}])[0]
                m, u = ch.get("message") or {}, resp.get("usage") or {}
                r = {"done": True, "finish_reason": ch.get("finish_reason"), "content": m.get("content"),
                     "reasoning_content": m.get("reasoning_content"), "tool_calls": m.get("tool_calls"),
                     "completion_tokens": u.get("completion_tokens"), "prompt_tokens": u.get("prompt_tokens"),
                     "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                     "prompt_read": (resp.get("timings") or {}).get("prompt_n")}
            rec = {"ts": round(time.time(), 3), "conv": p.conv, "seg": p.seg, "round": rnd, "messages": phys,
                   "n_messages": len(phys), "tools": [(t.get("function") or {}).get("name") for t in p.tools or []],
                   "est_tokens": p.est_tokens, "virtual_tokens": p.virtual_tokens, "masked": p.masked,
                   "masked_tokens": p.masked_tokens, "events": [e.get("event") for e in p.events],
                   "max_tokens": body.get("max_tokens") or body.get("max_completion_tokens"),
                   "prompt_tokens": r.get("prompt_tokens"), "response": r}
            tmp = os.path.join(d, ".last_request.%d.tmp" % threading.get_ident())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
            os.replace(tmp, os.path.join(d, "last_request.json"))
        except (OSError, TypeError, ValueError) as e:  # l'osservabilità non deve mai rompere la richiesta
            self.journal.log("live_dump_error", error=repr(e)[:300])

    def _log_request(self, kind, p, resp, t0, rnd, nmsg):
        u = resp.get("usage") or {}
        tm = resp.get("timings") or {}
        est = p.est_tokens if p else None
        real = u.get("prompt_tokens")
        if p and rnd == 0 and real:
            self.tc.calibrate(p.est_tokens, real)
        inv = (p.invalidated or {}) if p and rnd == 0 else {}
        self.journal.log("request", mode=kind, conv=p.conv if p else None, round=rnd, messages=nmsg,
                         prompt_tokens=real, est_tokens=est,
                         virtual_tokens=p.virtual_tokens if p else None, seg=p.seg if p else None,
                         reused=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                         prompt_read=tm.get("prompt_n"), prompt_ms=tm.get("prompt_ms"),
                         completion_tokens=u.get("completion_tokens"), ms=round((time.time() - t0) * 1000),
                         finish=resp["choices"][0].get("finish_reason"),
                         invalidated_suffix_tokens=inv.get("tokens", 0) if p else None,
                         invalidated_cause=inv.get("cause") if p else None)


def _short(a) -> str:
    try:
        d = json.loads(a) if isinstance(a, str) else (a or {})
        s = d.get("id") or d.get("query") or ""
    except (ValueError, AttributeError):
        s = str(a)
    s = str(s).replace("\n", " ")
    return repr(s[:60])


def _err_body(e: UpstreamError) -> dict:
    try:
        return json.loads(e.body)
    except ValueError:
        return {"error": {"message": str(e), "code": e.status}}


def sse_from_response(r: dict):
    """Risposta completa in chunk SSE OpenAI (usato solo se lo streaming vero non è disponibile)."""
    base = {"id": r.get("id") or "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion.chunk",
            "created": r.get("created") or int(time.time()), "model": r.get("model")}
    msg = r["choices"][0]["message"]

    def chunk(delta, finish=None, **extra):
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}

    yield chunk({"role": "assistant"})
    if msg.get("reasoning_content"):
        yield chunk({"reasoning_content": msg["reasoning_content"]})
    if msg.get("content"):
        yield chunk({"content": msg["content"]})
    for i, c in enumerate(msg.get("tool_calls") or []):
        yield chunk({"tool_calls": [{"index": i, "id": c.get("id"), "type": "function",
                                     "function": c.get("function")}]})
    extra = {"usage": r.get("usage")}
    for k in ("timings", "strata_context"):
        if r.get(k) is not None:
            extra[k] = r[k]
    yield chunk({}, r["choices"][0].get("finish_reason"), **extra)


def make_handler(proxy: Proxy):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):  # meno rumore
            pass

        def _send(self, status, obj, headers=None, raw=None, ctype="application/json"):
            data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _passthrough(self, method, body=None):
            st, hdr, data = proxy.up.raw(method, self.path, body)
            self._send(st, None, raw=data, ctype=hdr.get("Content-Type", "application/json"))

        def do_GET(self):
            parts = self.path.split("?")[0].strip("/").split("/")
            if parts[:3] == ["v1", "strata", "archive"] and len(parts) == 4:
                r = proxy.store.get(parts[3])
                if not r:
                    return self._send(404, {"error": {"message": "id non trovato"}})
                return self._send(200, dict(zip(("rid", "conv", "idx", "role", "name", "content", "tokens"), r)))
            if parts[:3] == ["v1", "strata", "conversations"] and len(parts) == 4:
                c = parts[3]
                segs = [{"seg": s, "cut_index": ci, "kind": k, "notes": n}
                        for s, ci, _, n, k in proxy.store.segments(c)]
                return self._send(200, {"conversation_id": c, **proxy.store.stats(c), "segment_list": segs})
            if parts[:3] == ["v1", "strata", "journal"]:
                return self._send(200, {"events": proxy.journal.mem[-200:]})
            if parts[:3] == ["v1", "strata", "engine"]:
                eng = getattr(proxy, "engine", None) or getattr(proxy.up, "engine", None)
                return self._send(200, eng.summary() if eng else {"kind": "strata", "detected": False})
            if parts[:3] == ["v1", "strata", "marks"] and len(parts) == 4:
                return self._send(200, proxy.marks(parts[3]))
            return self._passthrough("GET")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            path = self.path.split("?")[0].rstrip("/")
            if path.startswith("/v1/strata/marks"):
                try:
                    st, out = proxy.mark_action(json.loads(body or b"{}"))
                except (ValueError, KeyError, TypeError) as e:
                    st, out = 400, {"error": {"message": "richiesta non valida: %s" % e}}
                return self._send(st, out)
            if path in ("/v1/messages", "/v1/messages/count_tokens", "/messages", "/messages/count_tokens"):
                from . import api_anthropic
                return api_anthropic.handle(self, proxy, path, body)
            if path in ("/v1/responses", "/responses"):
                from . import api_responses
                return api_responses.handle(self, proxy, path, body)
            if path not in ("/v1/chat/completions", "/chat/completions"):
                return self._passthrough("POST", body)
            try:
                req = json.loads(body or b"{}")
            except ValueError:
                return self._send(400, {"error": {"message": "JSON non valido"}})
            hint = self.headers.get("X-Strata-Conversation") or req.get("prompt_cache_key")
            if not req.get("stream"):
                try:
                    st, resp, hdr = proxy.chat(req, hint)
                except Exception as e:  # noqa: BLE001
                    proxy.journal.log("proxy_error", error=repr(e)[:500])
                    return self._send(500, {"error": {"message": "proxy: %r" % e}})
                return self._send(st, resp, hdr)
            return self._stream(req, hint)

        def _stream(self, req, hint):
            """Header SSE subito, poi i chunk man mano che arrivano da Strata. Gli errori dopo l'header viaggiano
            come evento SSE {"error": ...} (l'SDK OpenAI li solleva come APIError)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            wlock = threading.Lock()

            def emit(obj):
                try:
                    with wlock:
                        if obj is None:
                            self.wfile.write(b": keep-alive\n\n")
                        else:
                            self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    raise ClientGone()

            try:
                st, resp, _ = proxy.chat(req, hint, emit=emit)
                if st != 200:
                    emit({"error": {"message": json.dumps(resp, ensure_ascii=False)[:2000], "code": st}})
                emit_done = b"data: [DONE]\n\n"
                with wlock:
                    self.wfile.write(emit_done)
                    self.wfile.flush()
            except ClientGone:
                pass
            except (BrokenPipeError, ConnectionResetError):
                proxy.journal.log("client_disconnect", where="done")
            except Exception as e:  # noqa: BLE001
                proxy.journal.log("proxy_error", error=repr(e)[:500])
                try:
                    emit({"error": {"message": "proxy: %r" % e, "code": 500}})
                    with wlock:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                except ClientGone:
                    pass

    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description="proxy contesto virtuale davanti a Strata")
    ap.add_argument("--upstream", default="http://127.0.0.1:8095")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--data", default="./data", help="cartella per archive.sqlite e journal.jsonl")
    ap.add_argument("--mode", choices=["tools", "off"], default="tools")
    ap.add_argument("--config", help="JSON con i campi di Config (soglie, finestra, flag)")
    ap.add_argument("--tokenizer", help="tokenizer.json HF (facoltativo; altrimenti caratteri/3.5)")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--engine", choices=["auto", "strata", "llama.cpp", "openai"], default=None,
                    help="tipo di motore (di serie: dalla configurazione, altrimenti rilevato)")
    a = ap.parse_args(argv)
    raw_cfg = json.load(open(a.config)) if a.config else {}
    if a.engine:
        raw_cfg["engine"] = a.engine
    cfg = Config.from_dict(raw_cfg)
    os.makedirs(a.data, exist_ok=True)
    if (cfg.slot_save or cfg.live_dump) and not cfg.data_dir:
        cfg.data_dir = os.path.abspath(a.data)
    up = Upstream(a.upstream, a.api_key)
    journal = Journal(os.path.join(a.data, "journal.jsonl"))
    counter = TokenCounter(cfg.chars_per_token, a.tokenizer)
    from .engines import setup as setup_engine
    engine = setup_engine(cfg, up, journal, explicit=set(raw_cfg), counter=counter,
                          log=lambda m: print(m, flush=True))
    proxy = Proxy(cfg, up, Store(os.path.join(a.data, "archive.sqlite")), journal, counter, mode=a.mode)
    proxy.engine = engine
    if cfg.autosave:
        proxy.enable_autosave()
    if cfg.kv_archive:
        from .kvarchive import enable as enable_kv_archive
        enable_kv_archive(proxy)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(proxy))
    srv.daemon_threads = True
    print("[strata-context] %s:%d -> %s (%s, salvataggi %s) mode=%s window=%d token=%s anchor=%s" % (
        a.host, a.port, a.upstream, engine.kind, "sì" if engine.slot_save else "no", a.mode, cfg.window,
        proxy.tc.kind, cfg.mask_anchor), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
