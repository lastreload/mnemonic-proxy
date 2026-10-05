"""Effetto del template di Strata sulla cache a ogni nuovo messaggio utente (commento 1 della card t_00c5aef6).

Il template (chat_template.jinja, riga 119) tiene il ragionamento degli assistenti PRIMA dell'ultimo messaggio utente
solo se `preserve_thinking` è indefinito o vero. Con `preserve_thinking=false` a ogni nuovo messaggio utente il testo
renderizzato cambia dal primo assistente col ragionamento dopo il messaggio utente precedente: tutto il tratto
agentico da lì in poi va riletto.

Per ogni richiesta della sessione (storia grezza di pi, senza proxy) rende il prompt col template vero e conta i token
del prompt precedente che stanno dopo la prima differenza di testo. Confronta kwargs diversi.

    python3 tools/template_cache.py SESSION.jsonl --pi-request ../g3/pi-first-request.json --tokenizer ../cases/nibble2/tok
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctxproxy.core import TokenCounter, is_human_user  # noqa: E402
from ctxproxy.render import render_pieces  # noqa: E402
from tools.replay_dry import load, to_openai  # noqa: E402


def run(session, pi_req, tc, kw, limit=None):
    """Solo le coppie (richiesta prima, richiesta dopo) con un messaggio utente in mezzo: fra due richieste senza
    messaggio utente il template è a sola aggiunta per qualsiasi preserve_thinking (last_query_index non cambia)."""
    body = pi_req.get("body", pi_req)
    msgs = [body["messages"][0]]
    tools = body["tools"]
    snaps = []                      # (n messaggi al momento della richiesta)
    for r in load(session):
        m = r.get("message")
        if not m:
            continue
        if m["role"] == "assistant":
            snaps.append(len(msgs))
            if limit and len(snaps) >= limit:
                break
        msgs += to_openai(m, True)
    out = []
    for a, b in zip(snaps, snaps[1:]):
        if not any(is_human_user(x) for x in msgs[a:b]):
            out.append({"i": len(out), "lost": 0, "after_user": False})
            continue
        pa = [t for _, t in render_pieces(msgs[:a], tools, kw)]
        pb = [t for _, t in render_pieces(msgs[:b], tools, kw)]
        k = 0
        for x, y in zip(pa, pb):
            if x != y:
                break
            k += 1
        lost = tc.raw("".join(pa[k:-1])) if k < len(pa) - 1 else 0
        out.append({"i": len(out), "lost": lost, "after_user": True, "prompt_before": tc.raw("".join(pa))})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--pi-request", required=True)
    ap.add_argument("--tokenizer", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out")
    a = ap.parse_args()
    tc = TokenCounter(3.5, a.tokenizer)
    if not tc.exact:
        sys.exit("tokenizer non caricato")
    pi = json.load(open(a.pi_request))
    res = {}
    for label, kw in (("pi_attuale(indefinito)", {}), ("preserve_thinking=true", {"preserve_thinking": True}),
                      ("preserve_thinking=false", {"preserve_thinking": False})):
        s = run(a.session, pi, tc, kw, a.limit)
        u = [x for x in s if x["after_user"]]
        res[label] = {"richieste": len(s), "dopo_msg_utente": len(u),
                      "token_invalidati_totali": sum(x["lost"] for x in s),
                      "token_invalidati_dopo_msg_utente": sum(x["lost"] for x in u),
                      "max_su_un_msg_utente": max((x["lost"] for x in u), default=0),
                      "per_msg_utente": [x["lost"] for x in u]}
        print(label, json.dumps({k: v for k, v in res[label].items() if k != "per_msg_utente"}), flush=True)
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
