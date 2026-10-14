# Author: Maurizio Verde — LastReload
"""0.3.4 verification on real data: which pieces the removal rule throws away (ALB w16k, runs of 14/10 evening).

Reads one real ALB run (proxy/journal.jsonl + proxy/archive.sqlite) and, for every removal decision the run
recorded (window_guard action=remove), rebuilds the candidate pieces - the recall results the loop had added - from
the run itself: the journal says which archive records each result returned (results: rid, idx, tokens) and the
archive holds their text. On those pieces it recomputes the order in which the guard removes them:

  0.3.3 (the rule the run used): ascending value per freed token, where piece_value discounts x0.25 a piece whose
        identifiers had all appeared in an older piece;
  0.3.4 (this change): the same, but a piece carrying identifiers of the question is never discounted and is
        removed last.

piece_value needs the text of a piece; the run's recorder also puts a header in front of the records (with the query
echoed) and the journal does not keep it, so the rebuilt text is the records themselves: its token count is printed
next to the one the journal recorded (the header is a constant of a few tens of tokens).

    python tools/replay_removal_034.py --run <case dir> --tokdir <pack tokenizer> [--json out.json]

The archive is copied to a temporary directory: the run is never touched. Nothing here needs the GPU.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import window  # noqa: E402
from ctxproxy.core import Config, Store, TokenCounter  # noqa: E402


def old_piece_value(pc: dict, question: str, loop: list, n_rounds: int, seen_before: set | None = None) -> float:
    """`window.piece_value` as it was in 0.3.3: the duplication discount (x0.25) applied even when the piece carried
    identifiers of the question. Kept here (and only here) for the before column of the comparison."""
    q = window.idents(question)
    ids = window.idents(pc["text"])
    dup = bool(ids) and seen_before is not None and ids <= seen_before
    v = 3.0 * len(ids & q) + 1.0 + (pc["round"] + 1) / float(max(n_rounds, 1))
    if dup:
        v *= 0.25
    return v * (0.25 if pc["kind"] == "reasoning" else 1.0)


def rank(ps: list, question: str, n_rounds: int, ntok, old: bool) -> list:
    """Order of the pieces (first = removed first) under the 0.3.3 rule (old) or the 0.3.4 one."""
    q = window.idents(question)
    seen: set = set()
    for p in ps:
        p["tokens"] = window.freed(p, ntok, "recall", "en")
        ids = window.idents(p["text"])
        p["protected"] = bool(ids & q)
        p["asked"] = len(ids & q)
        fn = old_piece_value if old else window.piece_value
        p["value"] = fn(p, question, [], n_rounds, seen)     # piece_value does not use `loop`
        seen |= ids
    if old:
        return sorted(ps, key=lambda p: (p["value"] / p["tokens"], p["round"], p["i"]))
    return sorted(ps, key=lambda p: (1 if p["protected"] else 0, p["value"] / p["tokens"], p["round"], p["i"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="case dir, e.g. runs/.../G/kf-c/64k/38_i13_j22_a_0031")
    ap.add_argument("--tokdir", required=True)
    ap.add_argument("--window", type=int, default=16384)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    src = os.path.join(a.run, "proxy")
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    for f in ("archive.sqlite", "archive.sqlite-wal", "archive.sqlite-shm"):
        if os.path.exists(os.path.join(src, f)):
            shutil.copy2(os.path.join(src, f), os.path.join(tmp.name, f))
    journal = [json.loads(l) for l in open(os.path.join(src, "journal.jsonl"))]
    cfg = Config.from_dict({**json.load(open(os.path.join(src, "cfg.json"))), "window": a.window})
    tc = TokenCounter(3.5, a.tokdir)
    if not tc.exact:
        raise SystemExit("tokenizer not usable: %s" % tc.kind)
    store = Store(os.path.join(tmp.name, "archive.sqlite"))
    conv = next(e["conv"] for e in journal if "conv" in e)
    question = next(e["last_user"] for e in journal if e["event"] == "recall")
    q = window.idents(question)
    print("# %s" % a.run)
    print("question: %s" % question)
    print("identifiers of the question: %s" % sorted(q))
    print("max_recall_rounds %d  policy %s" % (cfg.max_recall_rounds, cfg.recall_removal))

    def text_of(res):
        out = []
        for r in res:
            row = store.get(r["rid"], conv) or store.get(r["rid"])
            out.append((row[5] if row else "") or "")
        return "\n".join(out)

    pieces, dev = [], []
    for e in journal:
        if e["event"] == "recall":
            text = text_of(e["results"])
            pieces.append({"i": len(pieces) + 1, "kind": "result", "round": e["round"], "text": text,
                           "idx": [r["idx"] for r in e["results"]], "journal_tokens": e["tokens"],
                           "tokens_text": tc.count(text), "value": 0.0, "protected": False, "asked": 0,
                           "query": (e.get("args") or "")[:60]})
            dev.append(tc.count(text) - e["tokens"])
    print("\n## rebuilt pieces against the journal's token count (the recorder's header is not in the rebuild)")
    print("  pieces: %d   deviation in tokens: min %d, median %d, max %d"
          % (len(dev), min(dev), sorted(dev)[len(dev) // 2], max(dev)))
    n_rounds = max(p["round"] for p in pieces) + 1
    vold = {p["i"]: p for p in rank([dict(p) for p in pieces], question, n_rounds, tc.count, True)}
    vnew = {p["i"]: p for p in rank([dict(p) for p in pieces], question, n_rounds, tc.count, False)}
    print("  %-6s %-7s %-9s %-6s %-9s %-9s %-6s %s"
          % ("round", "journal", "rebuilt", "ids&q", "val_0.3.3", "val_0.3.4", "prot", "query/args"))
    for p in pieces:
        po, pn = vold[p["i"]], vnew[p["i"]]
        print("  %-6d %-7d %-9d %-6s %-9.3f %-9.3f %-6s %s"
              % (p["round"], p["journal_tokens"], p["tokens_text"], po["asked"], po["value"] / po["tokens"],
                 pn["value"] / pn["tokens"], po["protected"], p["query"]))
        p["value"] = po["value"]
        p["protected"] = po["protected"]

    print("\n## every removal the run recorded, and what each rule takes on the same candidates")
    tot_old = tot_new = matched = unmatched = 0
    report = []
    alive: list = []
    nxt = 0
    for e in journal:
        if e["event"] == "recall":
            alive.append(pieces[nxt])
            nxt += 1
        elif e["event"] == "window_guard" and e.get("action") == "remove":
            want = [x for x in e["removed"] if x[0] == "result"]
            old = rank([dict(p) for p in alive], question, n_rounds, tc.count, True)
            new = rank([dict(p) for p in alive], question, n_rounds, tc.count, False)
            # 0.3.3 column: the journal's own removals, looked up in the order recomputed here (matches = the
            # reconstruction is faithful); 0.3.4 column: the same number of removals, taken by the new order
            old_first = old[:len(want)]
            new_first = new[:len(want)]
            got = sorted(p["journal_tokens"] for p in old_first)
            exp = sorted(x[2] for x in want)
            ok = got == exp
            matched += 1 if ok else 0
            unmatched += 0 if ok else 1
            tot_old += sum(1 for p in old_first if p["protected"])
            tot_new += sum(1 for p in new_first if p["protected"])
            print("  round %s (%s): the run removed %s%s" % (e.get("round"), e.get("where"), json.dumps(e["removed"]),
                                                             "" if ok else "   [rebuilt order differs: %s]" % json.dumps(got)))
            print("      0.3.3 takes %s -> with the question's ids: %d"
                  % ([[p["round"], p["journal_tokens"], p["asked"], p["protected"]] for p in old_first],
                     sum(1 for p in old_first if p["protected"])))
            print("      0.3.4 takes %s -> with the question's ids: %d"
                  % ([[p["round"], p["journal_tokens"], p["asked"], p["protected"]] for p in new_first],
                     sum(1 for p in new_first if p["protected"])))
            report.append({"round": e.get("round"), "where": e.get("where"), "removed_journal": e["removed"],
                           "rebuilt_order_matches": ok,
                           "old_first": [[p["round"], p["journal_tokens"], p["asked"], p["protected"]] for p in old_first],
                           "new_first": [[p["round"], p["journal_tokens"], p["asked"], p["protected"]] for p in new_first]})
            for x in want:                     # the pieces the run removed leave the loop
                for i, p in enumerate(alive):
                    if p["journal_tokens"] == x[2] and p["round"] == x[1]:
                        del alive[i]
                        break
    asked = [p for p in pieces if window.idents(p["text"]) & q]
    print("\n## totals")
    print("  recall results of the run: %d, of which carrying identifiers of the question: %d" % (len(pieces), len(asked)))
    print("  removal events: %d, of which the rebuilt 0.3.3 order reproduces the journal's pieces: %d"
          % (matched + unmatched, matched))
    print("  result pieces removed with the question's identifiers in the run: %d; the 0.3.4 order takes %d"
          % (tot_old, tot_new))
    if a.json:
        json.dump({"run": a.run, "question": question, "identifiers": sorted(q),
                   "deviation": {"min": min(dev), "median": sorted(dev)[len(dev) // 2], "max": max(dev)},
                   "events": report, "totals": {"asked": len(asked), "pieces": len(pieces),
                                                "removal_events": matched + unmatched, "rebuilt_matches": matched,
                                                "removed_with_ids_in_run": tot_old, "new_first_with_ids": tot_new}},
                  open(a.json, "w"), indent=1)
    store.db.close()
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
