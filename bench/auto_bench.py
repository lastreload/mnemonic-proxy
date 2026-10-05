"""Richiamo automatico sul banco: pezzi inseriti (di serie) contro indizio (auto_recall_hint).

    python3 bench/auto_bench.py [--split all|dev|test]

Per ogni domanda positiva: parte nascosta = pezzi con idx < ultimo taglio di segmento prima della domanda (quello
che il modello non vede); query = query_terms(domanda) come fa il proxy per un messaggio dell'utente.
Misura: quante domande hanno ALMENO un pezzo di evidenza fra i pezzi inseriti / fra gli id citati dall'indizio,
quante TUTTE, token aggiunti; per i negativi, quante volte si inserisce qualcosa (falso allarme).
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ctxproxy.core as core  # noqa: E402
from ctxproxy.paging import query_terms  # noqa: E402
from recall_bench import build_store, TOK  # noqa: E402


def inject_tokens(mgr, cands):
    """Token dei pezzi che il richiamo di serie inserirebbe (stessa regola: k, tetto per pezzo, 1 argomento)."""
    cfg, used, chosen, n_args = mgr.cfg, 0, [], 0
    for rid, idx, role, name, content, tokens, score, matched in cands[:cfg.auto_recall_k * 3]:
        if len(chosen) >= cfg.auto_recall_k:
            break
        if role == "assistant-tool-args":
            if n_args >= cfg.auto_recall_max_args:
                continue
            n_args += 1
        low = content.lower()
        p = min([low.find(t) for t in matched if low.find(t) >= 0], default=0)
        s = content[max(0, p - 300):p + 2400]
        t = min(mgr.tc.count(s), cfg.auto_recall_piece_tokens) + 25
        if used + t > cfg.auto_recall_max_tokens:
            continue
        used += t
        chosen.append(rid)
    return chosen, used + (60 if chosen else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="all")
    ap.add_argument("--label", default="auto")
    args = ap.parse_args()
    qs = json.load(open(os.path.join(HERE, "questions.json")))
    if args.split != "all":
        qs = [q for q in qs if q["split"] == args.split]
    cfg = core.Config()
    tc = core.TokenCounter(cfg.chars_per_token, TOK if os.path.exists(TOK) else None)
    stores = {}
    res = {"inj": dict(any=0, all=0, tok=0, fired=0), "hint": dict(any=0, all=0, tok=0, fired=0)}
    neg = {"inj": 0, "hint": 0}
    npos = nneg = 0
    rows = []
    for q in qs:
        if q["db"] not in stores:
            st = build_store(core, os.path.join(HERE, "data", q["db"] + ".sqlite"))
            stores[q["db"]] = (st, core.Manager(cfg, st, tc, core.Journal(None)))
        st, mgr = stores[q["db"]]
        cuts = [c for c, in st.db.execute("SELECT cut_idx FROM segments WHERE conv=? ORDER BY seg", (q["conv"],))
                if c <= q["ask"]]
        lim = cuts[-1] if cuts else 0
        hidden = {r for r, in st.db.execute("SELECT rid FROM archive WHERE conv=? AND idx<?", (q["conv"], lim))}
        terms = query_terms(q["q"], [], [], "")
        cands = mgr.auto_candidates(q["conv"], terms, hidden, set(), q["ask"])
        inj, itok = inject_tokens(mgr, cands)
        text, chosen = mgr.auto_hint(cands, terms)
        hint = [c["rid"] for c in chosen]
        htok = tc.count(text) if chosen else 0
        groups = [set(g["rids"]) & hidden for g in q["evidence"]]
        groups_h = [g for g in groups if g]
        d = dict(id=q["id"], neg=q["neg"], hidden_ev=len(groups_h), groups=len(groups), inj=len(inj), hint=len(hint),
                 itok=itok, htok=htok)
        if q["neg"]:
            nneg += 1
            neg["inj"] += bool(inj)
            neg["hint"] += bool(hint)
        elif groups_h:                          # conta solo le domande con evidenza nella parte nascosta
            npos += 1
            for k, sel in (("inj", set(inj)), ("hint", set(hint))):
                hit = [bool(g & sel) for g in groups_h]
                res[k]["any"] += any(hit)
                res[k]["all"] += all(hit)
                res[k]["fired"] += bool(sel)
            res["inj"]["tok"] += itok
            res["hint"]["tok"] += htok
            d.update(inj_hit=[bool(g & set(inj)) for g in groups_h], hint_hit=[bool(g & set(hint)) for g in groups_h])
        rows.append(d)
    print("richiamo automatico, split %s: %d domande con evidenza nascosta, %d negativi" % (args.split, npos, nneg))
    for k, name in (("inj", "pezzi (serie)"), ("hint", "indizio")):
        r = res[k]
        print("  %-14s scatta %2d/%d  almeno un'evidenza %2d/%d  tutte %2d/%d  token medi %5.0f  negativi con "
              "inserimento %d/%d" % (name, r["fired"], npos, r["any"], npos, r["all"], npos, r["tok"] / max(1, npos),
                                     neg[k], nneg))
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    json.dump(dict(res=res, neg=neg, npos=npos, nneg=nneg, rows=rows),
              open(os.path.join(HERE, "out", "%s-%s.json" % (args.label, args.split)), "w"), indent=1)


if __name__ == "__main__":
    main()
