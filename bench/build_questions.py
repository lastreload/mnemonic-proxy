"""Risolve le regole di evidenza di questions_spec.py in id d'archivio e scrive questions.json.

    python3 bench/build_questions.py        # dalla cartella proxy/

Ogni gruppo di evidenza deve avere almeno un pezzo; altrimenti errore (domanda mal definita).
"""
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from questions_spec import Q  # noqa: E402


def segment_of(db, conv, idx):
    cuts = [c for c, in db.execute("SELECT cut_idx FROM segments WHERE conv=? ORDER BY seg", (conv,))]
    return sum(1 for c in cuts if c <= idx)


def main():
    dbs, out, bad = {}, [], 0
    for q in Q:
        db = dbs.get(q["db"])
        if db is None:
            db = dbs[q["db"]] = sqlite3.connect(os.path.join(HERE, "data", q["db"] + ".sqlite"))
        conv = db.execute("SELECT conv FROM archive GROUP BY conv ORDER BY count(*) DESC LIMIT 1").fetchone()[0]
        rows = db.execute("SELECT rid, idx, role, content FROM archive WHERE conv=? AND idx<? ORDER BY idx",
                          (conv, q["ask"])).fetchall()
        groups = []
        for g in q["ev"]:
            rx = re.compile(g["re"], re.S)
            roles = set(g["role"].split("|")) if g.get("role") else None
            lo, hi = g.get("lo", 0), g.get("hi", 10 ** 9)
            hit = [(rid, idx) for rid, idx, role, c in rows
                   if (roles is None or role in roles) and lo <= idx <= hi and c and rx.search(c)]
            if not hit:
                print("ERRORE %s: gruppo senza evidenza: %r" % (q["id"], g), file=sys.stderr)
                bad += 1
            groups.append({"rule": g, "rids": [r for r, _ in hit],
                           "msgs": sorted({i + 1 for _, i in hit}),
                           "segs": sorted({segment_of(db, conv, i) for _, i in hit})})
        d = {k: v for k, v in q.items() if k != "ev"}
        d.update(conv=conv, evidence=groups)
        d.setdefault("para", False)
        d.setdefault("neg", False)
        d.setdefault("calls", [])
        out.append(d)
    with open(os.path.join(HERE, "questions.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    n = len(out)
    print("domande %d (dev %d, test %d, negative %d, parafrasi %d) -> bench/questions.json; errori %d" % (
        n, sum(q["split"] == "dev" for q in out), sum(q["split"] == "test" for q in out),
        sum(q["neg"] for q in out), sum(q["para"] for q in out), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
