"""Banco di prova offline di strata_recall (solo CPU, nessun modello).

    python3 bench/recall_bench.py --label baseline                       # configurazione di serie
    python3 bench/recall_bench.py --label passo2 --set recall_struct=true
    python3 bench/recall_bench.py --label X --split dev --show pp-q4,pp-q5   # stampa i risultati di alcune domande

Per ogni domanda (bench/questions.json, da build_questions.py) ricostruisce l'archivio della sessione in un Store in
memoria (così gli indici nuovi si costruiscono con il codice in prova), poi:
  * evidenza@5 / MRR: sulla PRIMA ricerca testuale (queries[0]) — tutte le evidenze fra i primi 5 pezzi restituiti;
    MRR = 1/rango del primo pezzo di evidenza;
  * evidenza@budget: sequenza simulata di al più 5 chiamate (prima le chiamate strutturate `calls`, se la
    configurazione le supporta, poi le `queries`; con recall_multi una sola chiamata porta più varianti); copertura =
    ogni gruppo di evidenza ha un pezzo restituito il cui testo MOSTRATO contiene la regola del gruppo;
  * token: token restituiti fino alla copertura (o in tutte le chiamate se non coperta);
  * negativi: corretti se nessun risultato è marcato come corrispondenza forte (la versione di base non marca:
    qualunque risultato conta come risposta).
Scrive una riga di riepilogo in bench/results.jsonl e il dettaglio in bench/out/<label>.json.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOK = os.path.join(ROOT, "..", "cases", "nibble2", "tok", "hf-tokenizer.json")


def load_ctx(code_dir):
    sys.path.insert(0, code_dir)
    import ctxproxy.core as core  # noqa: E402
    return core


def build_store(core, path):
    """Store in memoria con archivio, registro file e segmenti copiati dalla copia d'archivio."""
    src = sqlite3.connect(path)
    st = core.Store(":memory:")
    rows = src.execute("SELECT rid, conv, idx, role, name, content, tokens, created FROM archive ORDER BY rowid").fetchall()
    st.archive_many(rows)
    try:
        st.add_fileops(src.execute("SELECT * FROM fileops").fetchall())
    except sqlite3.OperationalError:
        pass
    with st.lock:
        for r in src.execute("SELECT conv, seg, cut_idx, cut_h, notes_msg, kind, created FROM segments"):
            st.db.execute("INSERT OR IGNORE INTO segments(conv, seg, cut_idx, cut_h, notes_msg, kind, created) "
                          "VALUES(?,?,?,?,?,?,?)", r)
    if hasattr(st, "reindex"):
        st.reindex()
    return st


def shown_rids(meta):
    return [m["rid"] for m in meta if m.get("rid")]


def covered(groups, out_text, meta_rids, done):
    """Gruppi coperti: un pezzo del gruppo è fra quelli restituiti E la regola compare nel testo mostrato."""
    flat = re.sub(r"\s+", " ", out_text)
    for k, g in enumerate(groups):
        if k in done:
            continue
        if not set(g["rids"]) & set(meta_rids):
            continue
        rx = re.compile(g["rule"]["re"], re.S)
        if rx.search(out_text) or rx.search(flat) or rx.search(out_text.replace('\\"', '"').replace("\\n", "\n")):
            done.add(k)
    return done


def strong_any(out_text):
    """Marcatura delle corrispondenze forti (versioni nuove): '· forte' nelle intestazioni dei risultati."""
    return "\u00b7 forte" in out_text or "[forte]" in out_text


def run(args):
    core = load_ctx(args.code)
    cfg = core.Config()
    for kv in args.set or []:
        k, v = kv.split("=", 1)
        cur = getattr(cfg, k)
        setattr(cfg, k, (v.lower() in ("1", "true", "yes")) if isinstance(cur, bool) else type(cur)(v))
    tc = core.TokenCounter(cfg.chars_per_token, TOK if os.path.exists(TOK) else None)
    qs = json.load(open(os.path.join(HERE, "questions.json")))
    if args.split != "all":
        qs = [q for q in qs if q["split"] == args.split]
    show = set((args.show or "").split(",")) - {""}
    stores, det = {}, []
    multi = getattr(cfg, "recall_multi", False)
    struct = getattr(cfg, "recall_struct", False)
    t0 = time.time()
    for q in qs:
        if q["db"] not in stores:
            st = build_store(core, os.path.join(HERE, "data", q["db"] + ".sqlite"))
            stores[q["db"]] = (st, core.Manager(cfg, st, tc, core.Journal(None)))
        st, mgr = stores[q["db"]]
        groups = q["evidence"]
        # --- prima ricerca: evidenza@5, MRR
        meta = []
        out = mgr.recall(q["conv"], {"query": q["queries"][0]}, max_idx=q["ask"], meta=meta)
        rids = shown_rids(meta)
        top5 = rids[:5]
        allev = {r for g in groups for r in g["rids"]}
        rank = next((i + 1 for i, r in enumerate(rids) if r in allev), None)
        e5 = bool(groups) and len(covered(groups, out, top5, set())) == len(groups)
        # --- sequenza simulata
        calls = []
        if struct:
            calls += [dict(c) for c in q.get("calls") or []]
        else:
            calls += [{"path": c["path"]} for c in q.get("calls") or [] if c.get("path")]
        if multi:
            calls.append({"queries": list(q["queries"])})
        calls += [{"query": x} for x in q["queries"]]
        calls = calls[:5]
        done, tokens, n_calls, strong, outs = set(), 0, 0, False, []
        newv = any(getattr(cfg, k, False) for k in ("recall_struct", "recall_multi", "recall_flex"))
        for c in calls:
            meta = []
            o = mgr.recall(q["conv"], c, max_idx=q["ask"], meta=meta)
            n_calls += 1
            tokens += tc.count(o)
            strong = strong or strong_any(o)
            outs.append((c, o))
            covered(groups, o, shown_rids(meta), done)
            if groups and len(done) == len(groups):
                break
        if not newv:
            # versione di base: nessuna marcatura; un negativo è "corretto" solo se tutte le ricerche sono vuote
            strong = any(not o.startswith("recall: nessun") for _, o in outs)
        eb = bool(groups) and len(done) == len(groups)
        d = dict(id=q["id"], split=q["split"], kind=q["kind"], neg=q["neg"], para=q["para"], e5=e5,
                 rr=(1.0 / rank) if rank else 0.0, rank=rank, eb=eb, groups=len(groups), covered=len(done),
                 calls=n_calls, tokens=tokens, strong=strong)
        det.append(d)
        if q["id"] in show:
            print("=" * 100)
            print(q["id"], q["q"])
            print("evidenza:", [(g["msgs"][:5]) for g in groups], "->", d)
            for c, o in outs:
                print("-" * 40, json.dumps(c, ensure_ascii=False))
                print(o[:args.chars])
    pos = [d for d in det if not d["neg"]]
    neg = [d for d in det if d["neg"]]

    def agg(ds):
        n = len(ds) or 1
        return dict(n=len(ds), e5=round(sum(d["e5"] for d in ds) / n, 3), eb=round(sum(d["eb"] for d in ds) / n, 3),
                    mrr=round(sum(d["rr"] for d in ds) / n, 3),
                    tok=round(sum(d["tokens"] for d in ds) / n), calls=round(sum(d["calls"] for d in ds) / n, 2))
    summ = dict(label=args.label, split=args.split, set=args.set or [], ts=time.strftime("%Y-%m-%d %H:%M"),
                pos=agg(pos), neg_ok=sum(not d["strong"] for d in neg), neg_n=len(neg),
                by_kind={k: agg([d for d in pos if d["kind"] == k]) for k in sorted({d["kind"] for d in pos})},
                para=agg([d for d in pos if d["para"]]),
                by_split={s: agg([d for d in pos if d["split"] == s]) for s in ("dev", "test")},
                secs=round(time.time() - t0, 1))
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    json.dump(dict(summary=summ, detail=det), open(os.path.join(HERE, "out", args.label + "-" + args.split + ".json"),
                                                  "w"), ensure_ascii=False, indent=1)
    if not args.no_log:
        with open(os.path.join(HERE, "results.jsonl"), "a") as f:
            f.write(json.dumps(summ, ensure_ascii=False) + "\n")
    p = summ["pos"]
    print("%-14s %-4s  e@5 %.3f  e@budget %.3f  MRR %.3f  tok/dom %5d  chiamate %.2f  negativi %d/%d  (%ss)" % (
        args.label, args.split, p["e5"], p["eb"], p["mrr"], p["tok"], p["calls"], summ["neg_ok"], summ["neg_n"],
        summ["secs"]))
    for k, a in summ["by_kind"].items():
        print("   %-15s n=%2d  e@5 %.2f  e@budget %.2f  MRR %.2f  tok %5d" % (k, a["n"], a["e5"], a["eb"], a["mrr"],
                                                                         a["tok"]))
    a = summ["para"]
    print("   %-15s n=%2d  e@5 %.2f  e@budget %.2f  MRR %.2f  tok %5d" % ("(parafrasi)", a["n"], a["e5"], a["eb"],
                                                                     a["mrr"], a["tok"]))
    if args.list_miss:
        for d in det:
            if not d["neg"] and not d["eb"]:
                print("   MANCATA", d["id"], "coperti %d/%d" % (d["covered"], d["groups"]), "rango", d["rank"])
            if d["neg"] and d["strong"]:
                print("   NEGATIVO con risultato forte", d["id"])
    return summ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--code", default=ROOT, help="cartella che contiene il pacchetto ctxproxy")
    ap.add_argument("--set", action="append", help="opzione di Config: nome=valore (ripetibile)")
    ap.add_argument("--split", default="all", choices=["all", "dev", "test"])
    ap.add_argument("--show", default="")
    ap.add_argument("--chars", type=int, default=2500)
    ap.add_argument("--list-miss", action="store_true")
    ap.add_argument("--no-log", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
