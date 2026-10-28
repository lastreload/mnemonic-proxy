# Author: Maurizio Verde — LastReload
"""Server HTTP del proxy (OpenAI-compatibile) davanti a Strata.

    python3 -m ctxproxy.server --upstream http://127.0.0.1:8095 --port 8096 --data ./data

Solo stdlib. Una richiesta alla volta (Strata ha una sola sequenza: serializzare qui evita che due client
intreccino ciclo recall e note).

Streaming: con `stream: true` il proxy chiede a Strata lo stream e inoltra al client ragionamento,
testo e tool call delta per delta. Le chiamate `recall` (interne; alias storico `strata_recall`) non arrivano mai al client: il giro si
chiude lato proxy e il giro successivo continua lo stesso stream. Il ragionamento dei giri di recall arriva al
client (con una riga informativa) e il proxy lo ricorda per tenere stabile il prompt fisico al turno dopo.
Disconnessione del client (Esc in pi): il proxy chiude lo stream verso Strata, che annulla la generazione."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import answerfix, compat
from .core import (LEGACY_RECALL_NAME, RECALL_NAME, Config, Journal, Manager, Store, TokenCounter, content_text,
                   is_human_user)
from .paging import guard_msg, is_guarded, receipt_contaminated
from .render import template_kwargs
from .upstream import Upstream, UpstreamError
from .window import (ALL_BANDS, CALL_ALLOWANCE, FINAL_NOTE_EN, FINAL_NOTE_IT, LIMIT_NOTE_EN, LIMIT_NOTE_IT,
                     NO_ROOM_EN, NO_ROOM_IT, NOTICE_MIN, RESULT_MIN, RESULT_R, SEARCH_BANDS, SearchState, WindowGuard,
                     _text as _txt, clean, conclusion, idents, pieces, remove_piece, removal_entry, removal_order,
                     set_mt, sources_from_meta)

LIMIT_MSG = ("{rn}: limite di ricerche raggiunto per questa risposta. Non chiamare più "
             "{rn}: rispondi ora con le informazioni già recuperate (o di' che non le hai).")
LIMIT_MSG_EN = ("{rn}: search limit reached for this answer. Do not call {rn} again: answer now with the "
                "information already retrieved (or say that you do not have it).")
# 0.3.6: the last new final attempt (thinking off) answers the internal calls of the previous attempt with this
ANSWER_NOW_IT = ("{rn}: la ricerca è chiusa e {rn} non è disponibile. Non chiamare strumenti: rispondi ora alla "
                 "domanda, nel formato richiesto, con le informazioni già recuperate (o di' che non le hai).")
ANSWER_NOW_EN = ("{rn}: the search is closed and {rn} is not available. Do not call any tool: answer the question "
                 "now, in the requested format, with the information already retrieved (or say that you do not have "
                 "it).")
FINAL_ATTEMPTS = 2          # 0.3.6: new final attempts after a final round of internal calls only, never more
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_./\-]{4,}|\d[\d.,:]{2,}")


class ClientGone(Exception):
    """Il client ha chiuso la connessione durante lo stream."""


class Comment(str):
    """emit(Comment(text)): an SSE comment line (`: text`) for the client, like emit(None) but with a reason
    (0.3.7 router mode: `loading model <id>` while the model loads). Clients ignore it."""

    def __new__(cls, text):
        return super().__new__(cls, " ".join(str(text).split()))


class Proxy:
    def __init__(self, cfg: Config, upstream: Upstream, store: Store, journal: Journal, counter: TokenCounter,
                 mode: str = "tools"):
        self.cfg, self.up, self.store, self.journal, self.tc = cfg, upstream, store, journal, counter
        self.mode = mode
        self.mgr = Manager(cfg, store, counter, journal)
        self.lock = threading.Lock()
        self.autosaver = None
        self.engine = None          # engines.Engine | None (impostato da main; None = Strata)
        # 0.3.3: window guard on the innermost Upstream (wrappers - Tracker, ArchivingUpstream - delegate to it)
        self.wguard = WindowGuard(cfg, self.mgr, journal)
        base = upstream
        while hasattr(base, "up") and base.__class__.__name__ in ("Tracker", "ArchivingUpstream"):
            base = base.up
        if isinstance(base, Upstream):
            base.window_guard = self.wguard

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
        router = getattr(self.engine or getattr(self.up, "engine", None), "router_state", None)
        if router is not None:
            # 0.3.7 router mode: model, n_ctx and window are settled before anything is built or sent
            with self.lock:
                err = self._router_select(router, req, emit)
                if err is not None:
                    return err
                return self._chat_off(req, emit) if self.mode == "off" else self._chat_managed(req, hint, emit)
        gw = getattr(self.engine, "ctx_state", None) if self.engine is not None else None
        if gw is not None and self.mode != "off":
            # 0.3.8 Ollama / LM Studio: the real context of the model is read before the prompt is built
            with self.lock:
                err = self._router_select(gw, req, emit, event="engine_refused")
                if err is not None:
                    return err
                return self._chat_managed(req, hint, emit)
        if self.mode == "off":
            return self._chat_off(req, emit)
        with self.lock:
            return self._chat_managed(req, hint, emit)

    def _router_select(self, router, req: dict, emit=None, event: str = "router_refused"):
        from .enginectx import EngineCtxError
        from .router import RouterError
        try:
            if emit is None:
                router.select(req)
            else:
                self._router_select_alive(router, req, emit)
        except (RouterError, EngineCtxError) as e:
            self.journal.log(event, model=req.get("model"), status=e.status, error=e.message[:300])
            return e.status, {"error": {"message": e.message, "type": "router_error" if event == "router_refused"
                                        else "engine_context_error", "code": e.status}}, {}
        return None

    def _router_select_alive(self, router, req: dict, emit):
        """Streaming: select() (which may load the model: 70+ s from a slow disk) runs in a thread while this one
        sends `: loading model <id>` every `router_keepalive_s`, so a client with a read timeout does not give up.
        The window is still settled before anything is built. A keep-alive that cannot be written = the client is
        gone: nothing is generated; the thread is joined first (the load itself cannot be stopped and the router
        state must be settled before the lock is released), then ClientGone."""
        from .router import KEEPALIVE_S
        box: dict = {}

        def run():
            try:
                router.select(req)
            except BaseException as e:  # noqa: BLE001
                box["e"] = e
        t0 = time.time()
        t = threading.Thread(target=run, daemon=True, name="router-select")
        t.start()
        every = float(getattr(self.cfg, "router_keepalive_s", KEEPALIVE_S) or KEEPALIVE_S)
        sent, model = 0, None
        while True:
            t.join(every)
            if not t.is_alive():
                break
            model = router.loading or model or req.get("model")
            try:
                emit(Comment("loading model %s" % model))
                sent += 1
            except ClientGone:
                waited = round(time.time() - t0, 1)
                t.join()
                self.journal.log("router_client_gone", model=model, waited_s=waited, keepalives=sent,
                                 load_s=round(time.time() - t0, 1), error=str(box.get("e") or "")[:200] or None)
                raise
        if sent:
            self.journal.log("router_load_keepalive", model=model, keepalives=sent, s=round(time.time() - t0, 1))
        if "e" in box:
            raise box["e"]

    def _chat_off(self, req: dict, emit=None):
        """mode off: plain pass-through (one round, no prompt management)."""
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
        if p.response_limit is not None:
            body = set_mt(body, p.response_limit)
        resp = None
        msgs_c = req.get("messages") or []
        last_user = max((i for i, m in enumerate(msgs_c) if is_human_user(m)), default=None)
        lu_text = content_text(msgs_c[last_user].get("content"))[:300] if last_user is not None else ""
        lu_full = content_text(msgs_c[last_user].get("content")) if last_user is not None else ""
        if self.cfg.recall_leaf_select:
            # selettore a sotto-blocco foglia: guida = ultima domanda dell'utente (intera), dichiarata nell'estratto
            self.mgr.question = content_text(msgs_c[last_user].get("content")) if last_user is not None else ""
        stream_state = {"sent_role": False, "client_calls": 0} if emit else None
        guard_on = bool(getattr(self.cfg, "window_guard", True))
        phys0 = list(phys)          # P0: prompt prepared before the loop (masking, segments)
        loop: list[dict] = []       # what the loop adds (calls, reasoning, results): removable, see window.py
        floor = self._answer_floor(body)
        final = False
        tries = made = 0            # 0.3.5/0.3.6: new final attempts tried / made (at most FINAL_ATTEMPTS)
        no_think = False            # 0.3.6: the last attempt goes with the reasoning off (T not applied)
        st = SearchState()
        if guard_on:
            self._check_floor(p, phys0, body, floor, st)
        for rnd in range(self.cfg.max_recall_rounds + 3):
            tools_r = p.tools
            if guard_on:
                final, tools_r = self._plan_round(p, phys0, loop, body, rnd, floor, lu_full, st)
                phys = phys0 + clean(loop)
            self._live(p, phys, rnd, body)
            if stream_state is not None:
                stream_state["internal"] = self._internal_names(p, phys)
            self.wguard.where = "final" if final else ("answer" if rnd == 0 else "recall_round")
            try:
                if emit:
                    resp = self._round({**answerfix.stream_body(self, p, body, phys, rnd, tools_r, no_think),
                                        "messages": phys, "tools": tools_r}, emit, stream_state)
                else:
                    resp = answerfix.chat(self, p, body, phys, rnd, tools_r, no_think)
            except UpstreamError as e:
                self.journal.log("upstream_error", conv=p.conv, status=e.status, error=str(e)[:300])
                return e.status, _err_body(e), {}
            except ClientGone:
                self.journal.log("client_disconnect", conv=p.conv, round=rnd, req=rid_req,
                                 ms=round((time.time() - t0) * 1000))
                raise
            finally:
                self.wguard.where = ""
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
            if guard_on and final and not others and tries < FINAL_ATTEMPTS \
                    and not (msg.get("content") or "").strip():
                # 0.3.5: the final round (no internal tools offered) returned ONLY internal calls - the model saw
                # recall used earlier and called it anyway. Instead of an empty answer: a new final attempt, with
                # the calls answered by the closing notice (nothing searched), if it fits with F.
                # 0.3.6: if that attempt too returns only internal calls, a LAST one with the reasoning off and the
                # explicit instruction to answer now in the requested format (Flash Next at 64K reasoned up to the
                # budget and called recall again: AgentLongBench confirmation run). Never more than two. When attempt
                # 1 does not fit, attempt 2 is tried at once (it can go without the calls: see _final_retry).
                ok = False
                while tries < FINAL_ATTEMPTS and not ok:
                    tries += 1
                    nbody = self._no_think_body(body) if tries == FINAL_ATTEMPTS else body
                    ok = self._final_retry(p, phys0, loop, nbody, rnd, floor, lu_full, st, msg, mine, tools_r,
                                           attempt=tries, ref_body=body)
                if ok:
                    made += 1
                    if tries == FINAL_ATTEMPTS:
                        body, no_think = nbody, True
                    if emit:
                        if tries == FINAL_ATTEMPTS:
                            info = ("\n[gestore del contesto: ricerca chiusa, %s non eseguita; ultimo tentativo di "
                                    "risposta, senza ragionamento]\n" if p.lang == "it" else
                                    "\n[context manager: search closed, %s not run; last answer attempt, reasoning "
                                    "off]\n") % p.recall_name
                        else:
                            info = ("\n[gestore del contesto: ricerca chiusa, %s non eseguita; nuovo tentativo di "
                                    "risposta]\n" if p.lang == "it" else
                                    "\n[context manager: search closed, %s not run; new answer attempt]\n") \
                                % p.recall_name
                        self._emit_delta(emit, stream_state, resp, {"reasoning_content": info})
                        strip_c.append(resp.get("_sent_content", ""))
                        strip_r.append(resp.get("_sent_reasoning", "") + info)
                    phys = phys0 + clean(loop)
                    continue
            if others or rnd > self.cfg.max_recall_rounds or final:
                # chiamate miste (o troppi giri, o giro finale per spazio): al client vanno solo le sue; la recall
                # non eseguita sparisce.
                msg["tool_calls"] = others or None
                if not others:
                    msg.pop("tool_calls", None)
                    resp["choices"][0]["finish_reason"] = "stop"
                self.journal.log("recall_dropped", conv=p.conv, count=len(mine), mixed=bool(others), round=rnd,
                                 names=sorted({(c.get("function") or {}).get("name") for c in mine}), final=final,
                                 retry=made > 0, retry_no_room=tries > made, attempts=made, thinking_off=no_think)
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
            if guard_on and not final:
                # S of INV-3 (0.3.5): complete generation of this internal round, measured before any clean-up
                st.saw_generation((resp.get("usage") or {}).get("completion_tokens"),
                                  resp["choices"][0].get("finish_reason") or "")
            done = []                    # calls that got a result (the assistant message keeps only these)
            # Only batches with distinct explicit ids share a budget. Once active, include other recall
            # results too: an interleaved query must not consume a later id's share. Single-id and query-only
            # rounds retain the 0.3.13 path, including its byte-for-byte result rendering.
            batch_calls = []
            ids = set()
            for call in mine:
                if call in blocked or (call.get("function") or {}).get("name") not in p.recall_aliases:
                    continue
                batch_calls.append(call)
                args = (call.get("function") or {}).get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except (ValueError, TypeError):
                    continue
                if isinstance(args, dict) and isinstance(args.get("id"), str) and args["id"]:
                    ids.add(args["id"])
            batch_left = len(batch_calls) if guard_on and len(ids) > 1 else 0
            for c in mine:
                ts = time.time()
                a = (c.get("function") or {}).get("arguments")
                cname = (c.get("function") or {}).get("name")
                meta: list = []
                if guard_on and st.force_final:
                    # 0.3.5: the search is closed (no room): the other calls of this round do not search; they leave
                    # the assistant message so the tool protocol stays coherent
                    self.journal.log("recall_skipped", conv=p.conv, round=rnd, name=cname, args=str(a)[:300],
                                     reason=st.reason)
                    continue
                if c in blocked:
                    out = guard_msg(p.lang).replace("{rn}", p.recall_name)
                    if guard_on:
                        out = self._fit_other(p, phys0, loop, new, body, rnd, floor, lu_full, out, st)
                        if out is None:
                            continue
                    new.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
                    done.append(c)
                    self.journal.log("receipt_guard", conv=p.conv, round=rnd, name=cname, args=str(a)[:500])
                    if emit:
                        info = ("\n[gestore del contesto: chiamata %s bloccata (copia di una ricevuta)]\n"
                                if p.lang == "it" else
                                "\n[context manager: call %s blocked (copy of a receipt)]\n") % cname
                        self._emit_delta(emit, stream_state, resp, {"reasoning_content": info})
                        strip_r[-1] += info
                    continue
                if cname not in p.recall_aliases:
                    out = self._tools_call(p, phys + new, cname, a, rnd)
                    if guard_on:
                        out = self._fit_other(p, phys0, loop, new, body, rnd, floor, lu_full, out, st)
                        if out is None:
                            continue
                    new.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
                    done.append(c)
                    if emit:
                        info = "\n[%s: definizioni caricate]\n" % cname
                        self._emit_delta(emit, stream_state, resp, {"reasoning_content": info})
                        strip_r[-1] += info
                    continue
                src = None
                note = None
                if rnd >= self.cfg.max_recall_rounds:
                    # ultimo giro: niente nuova ricerca, il modello deve rispondere con ciò che ha (G3). With the guard
                    # on this round is already final (0.3.5): only the guard-off path gets here.
                    out = (LIMIT_MSG if p.lang == "it" else LIMIT_MSG_EN).replace("{rn}", p.recall_name)
                elif guard_on:
                    limit = None
                    if batch_left and c in batch_calls:
                        limit = self._batch_result_cap(p, phys0, loop, new, body, rnd, floor, lu_full,
                                                       st, batch_left)
                        batch_left -= 1
                    out = self.mgr.recall(p.conv, a, max_idx=last_user, meta=meta,
                                          max_tokens=self.cfg.recall_max_tokens if limit is None else limit)
                    got = self._fit_result(p, phys0, loop, new, body, rnd, floor, lu_full, a, out, meta,
                                           last_user, st, result_limit=limit)
                    if got is None:
                        continue                  # not even the "no room" notice fits: the call leaves the group
                    out, src, note = got
                else:
                    out = self.mgr.recall(p.conv, a, max_idx=last_user, meta=meta)
                    # ricontrollo del budget: il risultato non deve far uscire il prompt dalla finestra
                    resp_budget = int(body.get("max_tokens") or self.cfg.default_response)
                    room = self.cfg.window - self.cfg.reserve - resp_budget \
                        - self.mgr.estimate(phys + new, p.tools)[0] - 64
                    need = self.tc.count(out)
                    if need > max(room, 0):
                        # 0.3.0 (t_5de89914): a finestra piccola prima si accorcia lo spazio della risposta (fino a
                        # default_response), poi si taglia il risultato. Prima il risultato veniva tagliato a zero.
                        floor = min(resp_budget, int(self.cfg.default_response))
                        give = min(need - max(room, 0), resp_budget - floor) if room + resp_budget - floor > 0 else 0
                        if give > 0:
                            body["max_tokens"] = resp_budget - give
                            room += give
                            self.journal.log("response_shrunk", conv=p.conv, round=rnd, max_tokens=resp_budget - give,
                                             was=resp_budget)
                    if need > max(room, 0) and self.cfg.recall_leaf_select:
                        # selettore foglia: si rifà la scelta dentro lo spazio rimasto (cornice e coda comprese),
                        # togliendo i blocchi meno pertinenti invece di tagliare il testo a metà
                        meta.clear()
                        out = self.mgr.recall(p.conv, a, max_idx=last_user, meta=meta, max_tokens=max(room, 0))
                        need = self.tc.count(out)
                    if need > max(room, 0):
                        out = self.mgr.fit_tokens(out, max(room, 0), exact=self.cfg.recall_leaf_select)
                        self.journal.log("recall_trimmed", conv=p.conv, room=room, round=rnd)
                tmsg = {"role": "tool", "tool_call_id": c.get("id"), "content": out}
                if guard_on:
                    tmsg["_src"] = src or []
                    if note:
                        tmsg["_final_note"] = note
                new.append(tmsg)
                done.append(c)
                rec = self.journal.log("recall", conv=p.conv, req=rid_req, round=rnd, last_user=lu_text,
                                       args=a, results=meta, chars=len(out), tokens=self.tc.count(out),
                                       ms=round((time.time() - ts) * 1000, 2))
                recalls.append({k: rec[k] for k in ("args", "tokens")})
                recall_logs.append((rec, out))
                if emit:
                    info = "\n[%s %s: %d risultati]\n" % (cname, _short(a), len(meta))
                    self._emit_delta(emit, stream_state, resp, {"reasoning_content": info})
                    strip_r[-1] += info
            if guard_on and len(done) < len(mine):
                # 0.3.5: calls without a result leave the assistant message (tool protocol coherent); a group left
                # without any result is cancelled as a whole (the round's cost, never older evidence)
                if done:
                    new[0] = {**new[0], "tool_calls": done}
                else:
                    self.journal.log("recall_group_cancelled", conv=p.conv, round=rnd, calls=len(mine),
                                     reason=st.reason)
                    new = []
                self.journal.log("recall_dropped", conv=p.conv, count=len(mine) - len(done), mixed=False,
                                 round=rnd, names=sorted({(c.get("function") or {}).get("name") for c in mine
                                                          if c not in done}), final=True, reason=st.reason)
            if guard_on:
                loop += new
                phys = phys0 + clean(loop)
            else:
                phys += new
                internal += new
        if guard_on:
            internal = clean(loop)
        # controllo post-risposta (K) e pulizia del ragionamento di T/K (answerfix.py)
        resp = answerfix.guard(self, p, body, phys, resp, emit, tools=tools_r if guard_on else "_p")
        answerfix.finish(self, p, resp)
        # Never promote private reasoning or guess an answer when a closed search ends without public text.
        msg = resp["choices"][0]["message"]
        missing_answer = final and not msg.get("tool_calls") and not content_text(msg.get("content")).strip()
        if missing_answer:
            notice = ("[gestore del contesto: ricerca chiusa; il modello non ha prodotto una risposta finale "
                      "testuale. Nessuna risposta è stata dedotta dal ragionamento privato.]" if p.lang == "it" else
                      "[context manager: search closed; the model did not produce a final text answer. "
                      "No answer was inferred from private reasoning.]")
            msg["content"] = notice
            self.journal.log("final_answer_missing", conv=p.conv, round=rnd, attempts=made,
                             thinking_off=no_think, reason="no_public_text")
            if emit:
                self._emit_delta(emit, stream_state, resp, {"content": notice})
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
               "final_answer_missing": bool(missing_answer),
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

    # ---------- 0.3.3 window guard: recall loop (window.py) ----------
    def _answer_floor(self, body) -> int:
        """F: answer tokens the client call always gets: the client's max_tokens (or default_response), lowered to
        response_floor when that is set, never more than a quarter of the window."""
        mt = int(body.get("max_completion_tokens") or body.get("max_tokens") or self.cfg.default_response)
        rf = int(self.cfg.response_floor or 0)
        f = min(mt, rf) if rf > 0 else mt
        return max(1, min(f, max(1, int(self.cfg.window) // 4)))

    def _final_tools(self, p, phys):
        """Final call for lack of space: the client's tools only (the proxy's own are not offered)."""
        if not getattr(self.cfg, "final_drop_internal_tools", True) or not p.tools:
            return p.tools
        names = self._internal_names(p, phys)
        out = [t for t in p.tools if ((t or {}).get("function") or {}).get("name") not in names]
        return out or None

    def _wcount(self, phys, tools, body) -> int:
        return self.wguard.count(phys, tools, template_kwargs(body))

    def _check_floor(self, p, phys0, body, floor, st) -> None:
        """F (0.3.5): explicit check P0 + F + margin <= W before the loop. prepare() tries a segment switch but does
        not certify it; when it does not hold the answer cannot get F: no search at all (the round is final), the
        answer gets what is left (enforce lowers max_tokens; below 1 token the request is refused), journaled."""
        W, M = int(self.cfg.window), self.wguard.margin
        P0 = self._wcount(phys0, self._final_tools(p, phys0), body)
        if P0 + floor + M > W:
            st.floor_short = True
            st.close("floor_short")
            self.wguard._log(action="floor_short", where="prepare", conv=p.conv, prompt=P0, floor=floor,
                             room=W - M - P0)

    def _frame_tokens(self, p, phys, body) -> int:
        """H of INV-3: what one more result adds besides its text (the tool message frame) + the closing note."""
        empty = {"role": "tool", "tool_call_id": "x", "content": ""}
        try:
            frame = max(0, self._wcount(phys + [empty], p.tools, body) - self._wcount(phys, p.tools, body))
        except Exception:  # noqa: BLE001
            frame = 16
        note = FINAL_NOTE_IT if p.lang == "it" else FINAL_NOTE_EN
        return frame + self.tc.count(note)

    def _remove(self, p, loop, question, bands, cause, sink, st=None, fits=None) -> bool:
        """Remove ONE piece of `loop` from the given bands (first of removal_order); False when none is left."""
        policy = getattr(self.cfg, "recall_removal", "value")
        order = removal_order(loop, self.tc.count, question, policy, rn=p.recall_name, lang=p.lang, bands=bands)
        if not order:
            return False
        pc = order[0]
        remove_piece(loop, pc, p.recall_name, p.lang, self.tc.count)
        e = removal_entry(pc, cause, self.tc.count(pc["text"]))
        sink.append(e)
        if st is not None and pc["kind"] == "result" and pc.get("copy") is None:
            st.evidence_lost.append(e)
        return True

    def _plan_round(self, p, phys0, loop, body, rnd, floor, question, state=None):
        """Before each round of the recall loop (0.3.5, window.py INV-3/INV-4). Mutates `loop` and returns (final,
        tools for the round).
        Search round (rnd < max_recall_rounds, search not closed): P + max(S, CALL_ALLOWANCE + R + F) + H + margin
        <= W after removing ONLY verified copies and finished reasoning; otherwise final.
        Final round: P + F + margin <= W, reached by removing bands 0-1, then reducing/cancelling the NEWEST group
        (the search round that just ran: its results were never seen by the model), and only as an emergency C2
        (whole older rounds, those without protected pieces first), journaled with cause and sources."""
        st = state if state is not None else SearchState()
        W, M = int(self.cfg.window), self.wguard.margin
        policy = getattr(self.cfg, "recall_removal", "value")
        r_search, r_final, r_group, r_c2 = [], [], [], []

        def fits(extra, tools):
            return self._wcount(phys0 + clean(loop), tools, body) + extra + M <= W

        search_ok = rnd < self.cfg.max_recall_rounds and not st.force_final
        reason = "" if search_ok else (st.reason or "round_limit") if st.force_final else "round_limit"
        final = not search_ok
        extra = 0
        if search_ok:
            H = self._frame_tokens(p, phys0 + clean(loop), body)
            extra = max(st.s_est, CALL_ALLOWANCE + RESULT_R + floor) + H
            while not fits(extra, p.tools):
                if not self._remove(p, loop, question, SEARCH_BANDS, "search", r_search):
                    final, reason = True, "space"
                    break
        tools = p.tools
        if final:
            # closed for space or for the round limit: no internal tools (a recall there would be dropped and the
            # answer left empty); the model is told why
            tools = self._final_tools(p, phys0 + clean(loop))
            note = ""
            if loop and loop[-1].get("role") == "tool" and not loop[-1].get("_final_note") and rnd > 0:
                if reason == "round_limit":
                    note = LIMIT_NOTE_IT if p.lang == "it" else LIMIT_NOTE_EN
                else:
                    note = FINAL_NOTE_IT if p.lang == "it" else FINAL_NOTE_EN
            need = floor + (self.tc.count(note) if note else 0)
            while not fits(need, tools):
                if not self._remove(p, loop, question, SEARCH_BANDS, "final", r_final):
                    break
            if not fits(need, tools):
                self._shrink_newest(p, loop, question, r_group, fits, need, tools)
            if note and loop and loop[-1].get("role") == "tool" and not loop[-1].get("_final_note"):
                loop[-1] = {**loop[-1], "content": _txt(loop[-1].get("content")) + note, "_final_note": note}
            if not fits(floor, tools):
                # emergency (an estimate gone wrong, or P0 itself too big): results that are not copies, the ones
                # without the question's identifiers first; then C2, whole rounds. Every loss is journaled.
                while not fits(floor, tools):
                    if not self._remove(p, loop, question, (2, 3), "final_emergency", r_c2, st):
                        break
            if not fits(floor, tools):
                # C2 (emergency): whole rounds (call + results); rounds without protected pieces first, oldest first
                self._c2(p, loop, question, r_c2, fits, floor, tools, st)
        st.final_reason = reason if final else ""
        removed = r_search + r_final + r_group + r_c2
        if removed or final:
            P = self._wcount(phys0 + clean(loop), tools, body)
            if removed or (final and rnd > 0):
                self.wguard._log(action="final" if final else "remove", where="round", conv=p.conv, round=rnd,
                                 policy=policy, reason=reason or None,
                                 removed=[[e["kind"], e["round"], e["tokens"]] for e in removed],
                                 protected=[[e["kind"], e["round"], e["tokens"]] for e in removed if e["protected"]],
                                 removed_search=r_search, removed_final=r_final, newest_group=r_group, c2=r_c2,
                                 noncopy_search=sum(1 for e in r_search if e["kind"] == "result" and not e["copy"]),
                                 evidence_lost=len(st.evidence_lost), s_est=st.s_est, extra=extra or None,
                                 prompt=P, floor=floor, fits=P + floor + M <= W)
        return final, tools

    def _no_think_body(self, body: dict) -> dict:
        """0.3.6: the body of the last final attempt: reasoning off the way Strata (and llama.cpp) honour it -
        chat_template_kwargs.enable_thinking=false, which wins over reasoning_effort in the template (render.py
        template_kwargs, as Strata's frontend); no thinking budget (it would have nothing to bound).
        tool_choice=none forbids even tools remembered from earlier messages; schemas stay stable."""
        ctk = body.get("chat_template_kwargs") if isinstance(body.get("chat_template_kwargs"), dict) else {}
        out = {**body, "tool_choice": "none", "chat_template_kwargs": {**ctk, "enable_thinking": False}}
        out.pop("reasoning_budget_tokens", None)
        return out

    def _final_retry(self, p, phys0, loop, body, rnd, floor, question, st, msg, mine, tools,
                     attempt: int = 1, ref_body=None) -> bool:
        """0.3.5: a final round answered with internal calls only (recall not offered, called anyway). Adds to the
        loop the assistant message with those calls (reasoning reduced to its conclusion line, as for a finished
        round) and, for each call, a short result with the closing notice (FINAL_NOTE for space, LIMIT_MSG for the
        round limit); nothing is searched. The new attempt is a normal final call (client tools only) and must pass
        the same check P + F + margin <= W - when the final call itself could not give F (floor_short), it must give
        at least the room the first attempt had. To pay for the added cost only verified copies and finished
        reasoning are removed (bands 0-1, never evidence); if it still does not fit the loop is restored and False is
        returned (an explicit failure notice is returned if no public text remains). Journal: final_recall_retry.
        0.3.6: attempt 2 (the last) is made only after attempt 1 returned internal calls only: its calls are answered
        with ANSWER_NOW (answer now, in the requested format, no tools) and `body` is the reasoning-off body, so the
        same check P + F + margin <= W is made with the template the engine will render; the answer room `need` is the
        one of the previous final call (`ref_body`, its own template), never less. If the calls + ANSWER_NOW do not
        fit even after removing bands 0-1, the last attempt goes BARE: the calls leave (as in a cancelled group), the
        prompt is the one of the previous final call with the reasoning off - ANSWER_NOW is appended to the last
        result only if it fits - and the same check is made (bands 0-1 only). Journal: attempt, thinking_off, bare."""
        W, M = int(self.cfg.window), self.wguard.margin
        reason = st.final_reason or st.reason or "round_limit"
        before = self._wcount(phys0 + clean(loop), tools, body)
        before_ref = before if ref_body is None or ref_body is body else \
            self._wcount(phys0 + clean(loop), tools, ref_body)
        need = max(1, min(floor, W - M - before_ref))
        if attempt >= FINAL_ATTEMPTS:
            text = (ANSWER_NOW_IT if p.lang == "it" else ANSWER_NOW_EN).replace("{rn}", p.recall_name)
        elif reason == "round_limit":
            text = (LIMIT_MSG if p.lang == "it" else LIMIT_MSG_EN).replace("{rn}", p.recall_name)
        else:
            text = (FINAL_NOTE_IT if p.lang == "it" else FINAL_NOTE_EN).strip()
        snap = list(loop)
        amsg = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": list(mine)}
        r = msg.get("reasoning_content")
        if isinstance(r, str) and r.strip():
            amsg["reasoning_content"] = conclusion(r)
            amsg["_reasoning_cut"] = True
        loop += [amsg] + [{"role": "tool", "tool_call_id": c.get("id"), "content": text, "_keep": True,
                           "_final_note": text} for c in mine]
        removed: list = []

        def fits():
            return self._wcount(phys0 + clean(loop), tools, body) + need + M <= W
        while not fits():
            if not self._remove(p, loop, question, SEARCH_BANDS, "final_retry", removed, st):
                break
        ok = fits()
        bare = False
        if not ok and attempt >= FINAL_ATTEMPTS:
            # 0.3.6: the last attempt without the calls (no added cost but the reasoning-off template)
            loop[:] = snap
            removed = []
            while not fits():
                if not self._remove(p, loop, question, SEARCH_BANDS, "final_retry", removed, st):
                    break
            ok = bare = fits()
            if ok and loop and loop[-1].get("role") == "tool":
                last = loop[-1]
                loop[-1] = {**last, "content": _txt(last.get("content")) + "\n[" + text + "]",
                            "_final_note": (last.get("_final_note") or "") + "\n[" + text + "]"}
                if not fits():
                    loop[-1] = last
        after = self._wcount(phys0 + clean(loop), tools, body) if ok else before
        if ok:
            st.close(reason)              # the next round is final for the same reason (never a search again)
        else:
            loop[:] = snap                # nothing removed to pay for an attempt that is not made
        self.journal.log("final_recall_retry", conv=p.conv, round=rnd, reason=reason, used=ok, calls=len(mine),
                         attempt=attempt, thinking_off=template_kwargs(body).get("enable_thinking") is False,
                         bare=bare,
                         names=sorted({(c.get("function") or {}).get("name") for c in mine}),
                         prompt_before=before, prompt=after, need=need, floor=floor,
                         removed=[[e["kind"], e["round"], e["tokens"], e["band"]] for e in removed] if ok else [],
                         evidence_lost=len(st.evidence_lost))
        return ok

    def _shrink_newest(self, p, loop, question, sink, fits, need, tools) -> None:
        """The newest group (last assistant call + its results) does not fit with F: its results become receipts
        (newest first), then the group is cancelled as a whole - the cost of the search that was just admitted,
        never an older piece of evidence (Sol, correction 2)."""
        j = max((k for k, m in enumerate(loop) if m.get("role") == "assistant"), default=None)
        if j is None:
            return
        for pc in sorted([pc for pc in pieces(loop) if pc["i"] > j and pc["kind"] == "result"],
                         key=lambda x: -x["i"]):
            if fits(need, tools):
                return
            pc["band"] = None
            remove_piece(loop, pc, p.recall_name, p.lang, self.tc.count)
            sink.append(removal_entry(pc, "newest_group_receipt", self.tc.count(pc["text"])))
        if not fits(need, tools):
            n = sum(self.tc.count(_txt(m.get("content"))) for m in loop[j:])
            del loop[j:]
            sink.append({"kind": "group", "round": sum(1 for m in loop if m.get("role") == "assistant"),
                         "tokens": n, "band": None, "copy": False, "protected": False,
                         "cause": "newest_group_cancelled", "sources": []})

    def _c2(self, p, loop, question, sink, fits, floor, tools, st) -> None:
        """Emergency C2: drop whole rounds until the final call fits (P -> P0). Rounds without protected pieces go
        first (oldest first), then the others; every drop is recorded with what it carried."""
        q = idents(question)
        while loop and not fits(floor, tools):
            starts = [k for k, m in enumerate(loop) if m.get("role") == "assistant"] or [0]
            groups = [(s0, (starts + [len(loop)])[n + 1]) for n, s0 in enumerate(starts)]
            if starts[0] > 0:
                groups.insert(0, (0, starts[0]))

            def prot(g):
                return any(m.get("role") == "tool" and not m.get("_receipt") and idents(_txt(m.get("content"))) & q
                           for m in loop[g[0]:g[1]])
            g = next((g for g in groups if not prot(g)), groups[0])
            res = [m for m in loop[g[0]:g[1]] if m.get("role") == "tool" and not m.get("_receipt")]
            e = {"kind": "round", "round": sum(1 for m in loop[:g[0]] if m.get("role") == "assistant"),
                 "tokens": sum(self.tc.count(_txt(m.get("content"))) for m in loop[g[0]:g[1]]), "band": None,
                 "copy": False, "protected": prot(g), "cause": "c2_emergency",
                 "sources": sorted({s[1] for m in res for s in (m.get("_src") or []) if len(s) > 1})[:8],
                 "results": len(res)}
            del loop[g[0]:g[1]]
            sink.append(e)
            if res:
                st.evidence_lost.append(e)

    def _result_cap(self, p, phys0, loop, new, body, floor) -> int:
        """Room for one more result now: W - margin - F - closing note - P(with an empty result)."""
        W, M = int(self.cfg.window), self.wguard.margin
        empty = {"role": "tool", "tool_call_id": "x", "content": ""}
        note = FINAL_NOTE_IT if p.lang == "it" else FINAL_NOTE_EN
        base = self._wcount(phys0 + clean(loop + new) + [empty], p.tools, body)
        return max(0, W - M - floor - self.tc.count(note) - base)

    def _free_search(self, p, phys0, loop, new, body, rnd, floor, question, need, where):
        """Free bands 0-1 (verified copies, finished reasoning - this round's included) of loop + new until the
        room for a result reaches `need` (Argon: need, not a smaller want). Never a non-copy result."""
        cap = self._result_cap(p, phys0, loop, new, body, floor)
        removed = []
        while cap < need:
            both = loop + new
            if not self._remove(p, both, question, SEARCH_BANDS, where, removed):
                break
            loop[:] = both[:len(loop)]
            new[:] = both[len(loop):]
            cap = self._result_cap(p, phys0, loop, new, body, floor)
        if removed:
            self.wguard._log(action="remove", where="result", conv=p.conv, round=rnd,
                             policy=getattr(self.cfg, "recall_removal", "value"),
                             removed=[[e["kind"], e["round"], e["tokens"]] for e in removed],
                             protected=[[e["kind"], e["round"], e["tokens"]] for e in removed if e["protected"]],
                             removed_search=removed,
                             noncopy_search=sum(1 for e in removed if e["kind"] == "result" and not e["copy"]),
                             cap=cap, need=need)
        return cap

    def _batch_result_cap(self, p, phys0, loop, new, body, rnd, floor, question, state, remaining):
        """Share current result room, reusing unused shares, without changing window/floor invariants.

        Reserve RESULT_R per pending id (including this one), plus conservative message frames. If those
        shares and the next round's INV-3 reserve fit, keep that round too. Otherwise spend the reserve on
        this batch, fairly; tiny windows still go through the normal no-room path. Only bands 0-1 may be freed.
        """
        H = self._frame_tokens(p, phys0 + clean(loop + new), body)
        frames = (remaining - 1) * H
        future = (max(state.s_est, CALL_ALLOWANCE + RESULT_R + floor) + H - floor
                  if rnd + 1 < self.cfg.max_recall_rounds else 0)
        minimum = min(RESULT_R, self.cfg.recall_max_tokens)
        room = self._free_search(p, phys0, loop, new, body, rnd, floor, question,
                                 remaining * minimum + frames + future, "batch")
        reserve = future if room >= remaining * minimum + frames + future else 0
        cap = min(self.cfg.recall_max_tokens, max(0, room - frames - reserve) // remaining)
        self.journal.log("recall_batch_budget", conv=p.conv, round=rnd, remaining=remaining,
                         room=room, frames=frames, future=reserve, cap=cap)
        return cap

    def _fit_result(self, p, phys0, loop, new, body, rnd, floor, question, a, out, meta, last_user, state=None,
                    result_limit=None):
        """A recall result never takes the room the answer floor (and the closing note) needs (0.3.5):
        cap = W - margin - F - note - P(with an empty result). If the result does not fit, only verified copies and
        finished reasoning (this round's too) are removed, up to what the result needs; then the leaf selector
        (whole blocks); then, with cap >= NOTICE_MIN, a strict cut (the sources become `cut`); below that the
        result is replaced by a short "no room" notice that fits the cap (meta cleared, search closed), or - when
        not even the notice fits - None: the call leaves the group.
        Returns (text, sources, closing note or None) or None."""
        st = state if state is not None else SearchState()
        need = self.tc.count(out)
        cap = self._free_search(p, phys0, loop, new, body, rnd, floor, question, need, "result")
        if result_limit is not None:
            cap = min(cap, result_limit)  # a retry must not consume another id's or the next round's share
        if need <= cap:
            return out, sources_from_meta(meta), None
        # Retry id paging (and the optional leaf selector) in the actual remaining window budget.
        if cap >= NOTICE_MIN:
            meta.clear()
            out = self.mgr.recall(p.conv, a, max_idx=last_user, meta=meta, max_tokens=cap)
            need = self.tc.count(out)
            if need <= cap:
                return out, sources_from_meta(meta), None
        if cap >= NOTICE_MIN:
            note = "\n[...tagliato per spazio]" if p.lang == "it" else "\n[...cut for space]"
            cut = self.wguard.fit_text(out, cap, note)
            self.journal.log("recall_trimmed", conv=p.conv, room=cap, round=rnd, guard=True)
            if cut:
                return cut, sources_from_meta(meta, cut=True), None
        meta.clear()
        st.close("no_room_for_result")
        notice = (NO_ROOM_IT if p.lang == "it" else NO_ROOM_EN).format(rn=p.recall_name, n=need)
        self.journal.log("recall_no_room", conv=p.conv, round=rnd, cap=cap, need=need,
                         notice=self.tc.count(notice) <= cap)
        if self.tc.count(notice) > cap:
            return None
        return notice, [], notice

    def _fit_other(self, p, phys0, loop, new, body, rnd, floor, question, out, state):
        """Results that are not recall output (tool definitions loaded by the pager, receipt-guard notices) pass the
        same check (0.3.5): they fit whole after freeing bands 0-1, or the call leaves the group and the search is
        closed (a cut definition would be useless)."""
        need = self.tc.count(out)
        cap = self._free_search(p, phys0, loop, new, body, rnd, floor, question, need, "other")
        if need <= cap:
            return out
        state.close("no_room_for_tool_result")
        self.journal.log("recall_no_room", conv=p.conv, round=rnd, cap=cap, need=need, other=True)
        return None

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
        chiamate recall. -> risposta non-stream ricostruita (+ _sent_content/_sent_reasoning)."""
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
                                        "args": "", "mine": name in state.get("internal", (RECALL_NAME, LEGACY_RECALL_NAME)),
                                        "cidx": None, "held": self.cfg.receipt_guard and is_guarded(name)}
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
        except (ClientGone, ConnectionError):   # ConnectionError: also ConnectionAbortedError (Windows)
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
        """Nomi di chiamata gestiti dal proxy in questo giro: recall, tools (e i loro alias storici) e (con tools_paging) gli
        strumenti del catalogo NON ancora caricati nel prompt fisico (la chiamata non va al client: il proxy
        risponde con la definizione e chiede di ripetere la chiamata)."""
        names = set(p.recall_aliases)
        if p.catalog:
            names |= set(p.tools_aliases)
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
        if name in p.tools_aliases:
            q = str(a.get("query") or "")
            names = pager.search(p.catalog, q, a.get("names"), limit=self.cfg.tools_load_max)
            out = pager.result(p.catalog, names, loaded, q, name=p.tools_name, lang=p.lang)
            self.journal.log("tools_search", conv=p.conv, round=rnd, query=q, names_asked=a.get("names"),
                             found=names, loaded_new=[n for n in names if n not in loaded],
                             tokens=self.tc.count(out))
            return out
        # chiamata diretta a uno strumento del catalogo non caricato: definizione + richiesta di ripetere
        if p.lang == "it":
            note = ("La chiamata a %s NON è stata eseguita: la sua definizione non era ancora caricata. Eccola: "
                    "ripeti la chiamata con i parametri corretti." % name)
        else:
            note = ("The call to %s was NOT executed: its definition was not loaded yet. Here it is: "
                    "repeat the call with the correct parameters." % name)
        out = pager.result(p.catalog, [name], loaded, name, note, name=p.tools_name, lang=p.lang)
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
                return 400, {"error": {"message": "missing pin text"}}
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
                return 400, {"error": {"message": "no user message before index %d" % idx}}
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
            compat.replace(tmp, os.path.join(d, "last_request.json"))
        except (OSError, TypeError, ValueError) as e:  # l'osservabilità non deve mai rompere la richiesta
            self.journal.log("live_dump_error", error=repr(e)[:300])

    def _log_request(self, kind, p, resp, t0, rnd, nmsg):
        u = resp.get("usage") or {}
        tm = resp.get("timings") or {}
        est = p.est_tokens if p else None
        real = u.get("prompt_tokens")
        tw = getattr(self.up, "truncwatch", None)
        if p and rnd == 0 and real and not (tw is not None and tw.cut(est, real)):
            # 0.3.8: a prompt cut by the engine is not a calibration sample (it would teach a ratio of 0.2-0.5)
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
        if p and rnd == 0 and any(e.get("event") == "mask" for e in p.events or []):
            # 0.3.3, measure only: after a mask, prefix the proxy expected the engine to reuse vs what it reused
            # (hybrid engines restart from the live session or the longest checkpoint that is a prefix)
            real_reused = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
            if real_reused is None:
                real_reused = tm.get("cache_n")
            exp = inv.get("expected_reused")
            self.journal.log("mask_reuse", conv=p.conv, seg=p.seg, first_diff=inv.get("first_diff"),
                             expected_reused=exp, real_reused=real_reused, prompt_tokens=real,
                             gap=(exp - real_reused) if exp is not None and real_reused is not None else None)


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
                    return self._send(404, {"error": {"message": "id not found"}})
                return self._send(200, dict(zip(("rid", "conv", "idx", "role", "name", "content", "tokens"), r)))
            if parts[:3] == ["v1", "strata", "conversations"] and len(parts) == 4:
                c = parts[3]
                segs = [{"seg": s, "cut_index": ci, "kind": k, "notes": n}
                        for s, ci, _, n, k in proxy.store.segments(c)]
                return self._send(200, {"conversation_id": c, **proxy.store.stats(c), "segment_list": segs})
            if parts[:3] == ["v1", "strata", "journal"]:
                return self._send(200, {"events": proxy.journal.mem[-200:]})
            if parts[:3] == ["v1", "strata", "engine"] or parts[:2] == ["v1", "engine"] and len(parts) == 2:
                eng = getattr(proxy, "engine", None) or getattr(proxy.up, "engine", None)
                return self._send(200, eng.summary() if eng else {"kind": "strata", "detected": False})
            if parts == ["health"]:
                return self._health()
            if parts[:3] == ["v1", "strata", "marks"] and len(parts) == 4:
                return self._send(200, proxy.marks(parts[3]))
            return self._passthrough("GET")

        def _health(self):
            """Proxy health + the engine's /health (llama-server: 503 while loading the model). 200 only when the
            engine answers 200 (or has no /health but serves /v1/models)."""
            from . import __version__
            eng = getattr(proxy, "engine", None) or getattr(proxy.up, "engine", None)
            up = {}
            try:
                st, _, data = proxy.up.raw("GET", "/health")
                if st == 404:
                    st, _, _ = proxy.up.raw("GET", "/v1/models")
                    data = b""
                try:
                    up = json.loads(data) if data else {}
                except ValueError:
                    up = {"raw": data[:200].decode("utf-8", "replace")}
                up = up if isinstance(up, dict) else {"raw": up}
                up["http_status"] = st
            except Exception as e:  # noqa: BLE001
                st, up = 0, {"error": repr(e)[:200]}
            status = "ok" if st == 200 else "loading" if st == 503 else "engine_unreachable" if st == 0 else "error"
            return self._send(200 if status == "ok" else 503, {
                "status": status, "service": "mnemonic-proxy", "version": __version__,
                "engine": eng.kind if eng else "strata", "upstream": up})

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
                        elif isinstance(obj, str):
                            self.wfile.write(b": " + obj.encode("utf-8") + b"\n\n")
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
            except ConnectionError:   # BrokenPipe, ConnectionReset, ConnectionAborted (Windows)
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


SUBCOMMANDS = """subcommands:
  mnemonic-proxy demo     offline demo (no model, no GPU): archive, masking, recall -> PASS/FAIL
  mnemonic-proxy check    is this setup ready? same options as the server, plus --json and --live
  mnemonic-proxy forget   offline deletion of one conversation; --list or --conv ID [--dry-run]
"""


def build_parser(prog="mnemonic-proxy", description=None):
    """Server options; `check` uses the same ones, so it reads the configuration the server would run with."""
    from . import __version__
    ap = argparse.ArgumentParser(
        prog=prog, epilog=SUBCOMMANDS if prog == "mnemonic-proxy" else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=description or (
            "Mnemonic Proxy %s: transparent proxy that lets local coding agents work for hours "
            "(verbatim archive, structured recall, segments with handoff notes, saved engine state)." % __version__))
    ap.add_argument("--upstream", default="http://127.0.0.1:8095",
                    help="base URL of the inference engine (Strata, llama-server, any OpenAI-compatible server)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1, local only)")
    ap.add_argument("--port", type=int, default=8096, help="listen port (default: 8096)")
    ap.add_argument("--data", default="./data", help="data folder: archive.sqlite, journal.jsonl (default: ./data)")
    ap.add_argument("--mode", choices=["tools", "off"], default="tools",
                    help="tools = managed context (default); off = plain pass-through")
    ap.add_argument("--config", help="JSON file with Config fields (window, thresholds, feature flags)")
    ap.add_argument("--tokenizer", help="HF tokenizer.json for exact token counts (optional; default chars/3.5)")
    ap.add_argument("--api-key", default="", help="API key sent to the upstream engine (optional)")
    ap.add_argument("--engine", choices=["auto", "strata", "llama.cpp", "ds4", "openai"], default=None,
                    help="engine type (default: from the config file, otherwise auto-detected)")
    ap.add_argument("--version", action="version", version="mnemonic-proxy " + __version__)
    return ap


def load_config(a) -> tuple[dict, Config]:
    """-> (fields written in the config file / on the command line, Config). Relative `slot_dir` and
    `kv_archive_dir` are resolved against the current directory, like `--data`."""
    raw_cfg = {}
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            raw_cfg = json.load(f)
        if not isinstance(raw_cfg, dict):
            raise ValueError("%s: expected a JSON object" % a.config)
    if a.engine:
        raw_cfg["engine"] = a.engine
    cfg = Config.from_dict(raw_cfg)
    for k in ("slot_dir", "kv_archive_dir"):
        if getattr(cfg, k):
            setattr(cfg, k, os.path.abspath(os.path.expanduser(getattr(cfg, k))))
    return raw_cfg, cfg


def main(argv=None):
    from . import __version__
    argv = list(sys.argv[1:] if argv is None else argv)
    compat.console_safe()
    if argv and argv[0] == "demo":
        from .demo import main as demo_main
        return demo_main(argv[1:])
    if argv and argv[0] == "check":
        from .check import main as check_main
        return check_main(argv[1:])
    if argv and argv[0] == "forget":
        from .forget import main as forget_main
        raise SystemExit(forget_main(argv[1:]))
    a = build_parser().parse_args(argv)
    raw_cfg, cfg = load_config(a)
    os.makedirs(a.data, exist_ok=True)
    from .datalock import DataLock
    try:
        with DataLock(a.data):
            return serve(a, raw_cfg, cfg)
    except ValueError as e:
        raise SystemExit("mnemonic-proxy: %s" % e) from None


def serve(a, raw_cfg, cfg):
    """Run only while main owns the data directory's maintenance lock."""
    if (cfg.slot_save or cfg.live_dump) and not cfg.data_dir:
        cfg.data_dir = os.path.abspath(a.data)
    counter = TokenCounter(cfg.chars_per_token, a.tokenizer)
    if a.tokenizer is not None and not counter.exact:
        raise ValueError("cannot load the explicitly requested --tokenizer: %s. "
                         "Install 'mnemonic-proxy[exact]' in this interpreter and check the tokenizer path/format; "
                         "omit --tokenizer only if estimated counting is intended. Refusing to start with an "
                         "estimated window guard." % counter.kind)
    up = Upstream(a.upstream, a.api_key)
    journal = Journal(os.path.join(a.data, "journal.jsonl"))
    from .engines import setup as setup_engine, window_mismatch, window_problems
    engine = setup_engine(cfg, up, journal, explicit=set(raw_cfg), counter=counter,
                          log=lambda m: print(m, flush=True))
    small = window_problems(cfg, engine, set(raw_cfg))
    if engine.router_state is not None:
        # 0.3.7: which model saved which slot file must survive a proxy restart (data folder, always)
        from .router import OWNERS_FILE, SlotOwners
        engine.router_state.owners = SlotOwners(os.path.join(os.path.abspath(a.data), OWNERS_FILE))
    big = window_mismatch(cfg, engine, set(raw_cfg))     # 0.3.8 Ollama / LM Studio: they would cut in silence
    if small:
        journal.log("refused_small_window", problems=[e for e, _ in small + big])
        raise SystemExit("mnemonic-proxy: refusing to start, the context window is too small.\n" + "\n".join(
            "  %s\n  -> %s" % p for p in small + big))
    if big:
        journal.log("refused_window_over_engine", problems=[e for e, _ in big])
        raise SystemExit("mnemonic-proxy: refusing to start, the config window is bigger than the engine context.\n"
                         + "\n".join("  %s\n  -> %s" % p for p in big))
    proxy = Proxy(cfg, up, Store(os.path.join(a.data, "archive.sqlite")), journal, counter, mode=a.mode)
    proxy.engine = engine
    if cfg.autosave:
        proxy.enable_autosave()
    if cfg.kv_archive:
        from .kvarchive import enable as enable_kv_archive, zstd
        if zstd is None:
            raise SystemExit("mnemonic-proxy: kv_archive requires Python >= 3.14 (compression.zstd); "
                             "running on Python %d.%d. Set \"kv_archive\": false or upgrade Python."
                             % sys.version_info[:2])
        enable_kv_archive(proxy)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(proxy))
    # Request threads stay daemonic: with HTTP/1.1 keep-alive an idle client connection would otherwise block
    # shutdown forever. The data lock is released by the OS when the process exits.
    srv.daemon_threads = True
    print("[mnemonic-proxy] %s:%d -> %s (engine %s, saved state %s) mode=%s window=%s tokens=%s anchor=%s" % (
        a.host, a.port, a.upstream, engine.kind + (" router" if engine.router else ""),
        "yes" if engine.slot_save else "no", a.mode,
        "per model (n_ctx)" if engine.router else ("%s (read from %s before each request)" % (
            cfg.window, engine.flavor) if engine.flavor else cfg.window), proxy.tc.kind, cfg.mask_anchor), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for worker in (proxy.autosaver, getattr(proxy, "archiver", None)):
            if worker is not None:
                worker.stop()
                if worker.th is not None:
                    worker.th.join()
        srv.server_close()
        proxy.store.db.close()


if __name__ == "__main__":
    main()
