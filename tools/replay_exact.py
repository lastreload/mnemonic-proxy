"""Rigioco a secco ESATTO di una sessione pi attraverso il Manager.

Differenze rispetto a replay_dry.py: system prompt e strumenti VERI di pi (g3/pi-first-request.json, + strata_recall
iniettato dal proxy), reasoning_effort della richiesta vera, conteggio con template + tokenizer del pack (esatto).
Per ogni richiesta di pi: storia completa -> Manager.prepare -> token del prompt fisico, riletture (prima differenza
di prefisso), pacchetti di masking con Δ pianificato vs Δ misurato, switch.

    python3 tools/replay_exact.py SESSION.jsonl --pi-request ../g3/pi-first-request.json \
        --tokenizer ./tok --config cfg.json --label nuovo --out replay-out
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctxproxy.core import RECALL_TOOL, Config, Journal, Manager, Store, TokenCounter, canon  # noqa: E402
from tools.replay_dry import StubUpstream, load, to_openai  # noqa: E402


def run(session, pi_req, cfg: Config, tc: TokenCounter, label: str, out_dir: str | None, notes_tokens=3000,
        limit: int | None = None):
    rows = load(session)
    body = pi_req.get("body", pi_req)
    system = body["messages"][0]
    assert system["role"] in ("system", "developer")
    tools = body["tools"]
    extra = {k: body[k] for k in ("reasoning_effort", "reasoning", "chat_template_kwargs") if k in body}
    jpath = os.path.join(out_dir, "journal-%s.jsonl" % label) if out_dir else None
    if jpath and os.path.exists(jpath):
        os.remove(jpath)
    mgr = Manager(cfg, Store(":memory:"), tc, Journal(jpath))
    up = StubUpstream(notes_tokens, cfg.chars_per_token)
    msgs = [system]
    series, prev = [], None
    t0 = time.time()
    for r in rows:
        m = r.get("message")
        if not m:
            continue
        if m["role"] == "assistant":
            u = m.get("usage") or {}
            n_ev = len(mgr.journal.mem)
            p = mgr.prepare({"messages": list(msgs), "tools": tools, "max_tokens": cfg.default_response, **extra},
                            upstream=up)
            evs = mgr.journal.mem[n_ev:]
            pc = [canon(x) for x in p.messages]
            _, per = mgr.estimate(p.messages, p.tools)
            k = 0
            if prev is not None:
                for a, b in zip(prev, pc):
                    if a != b:
                        break
                    k += 1
            reread = p.est_tokens if prev is None else sum(per[k:])
            mk = [e for e in evs if e["event"] == "mask"]
            ar = [e for e in evs if e["event"] == "auto_recall"]
            inv = p.invalidated or {}
            series.append({"i": len(series), "ts": r["timestamp"], "virtual": p.virtual_tokens,
                           "physical": p.est_tokens, "reread": reread, "seg": p.seg, "masked": p.masked,
                           "masked_saved": p.masked_saved,
                           "invalidated": inv.get("tokens", 0), "invalidated_cause": inv.get("cause"),
                           "fixed": mgr.estimate(p.messages[:1], p.tools)[0],
                           "auto_recall": [{k2: e.get(k2) for k2 in ("trigger", "injected", "tokens", "pieces",
                                                                      "candidates", "terms")} for e in ar],
                           "s_over_delta": [e.get("s_over_delta") for e in mk],
                           "events": sorted({e["event"] for e in evs} - {"new_conversation", "request"}),
                           "mask": [{k2: e.get(k2) for k2 in ("count", "tokens_saved", "tokens_saved_measured",
                                                             "est_before", "est_after_measured", "by_kind")}
                                    for e in mk],
                           "pi_prompt": (u.get("input") or 0) + (u.get("cacheRead") or 0)})
            prev = pc
            if limit and len(series) >= limit:
                break
        msgs += to_openai(m, True)
    W = cfg.window
    s = {"label": label, "requests": len(series), "tokenizer": tc.kind, "seconds": round(time.time() - t0, 1),
         "virtual_final": series[-1]["virtual"], "physical_max": max(x["physical"] for x in series),
         "physical_final": series[-1]["physical"], "reread_total": sum(x["reread"] for x in series),
         "physical_total": sum(x["physical"] for x in series),
         "mask_batches": sum(len(x["mask"]) for x in series),
         "switches": sum(1 for x in series if "switch" in x["events"]),
         "over_window": sum(1 for x in series if x["physical"] + cfg.reserve + cfg.default_response > W),
         "events": {}}
    for x in series:
        for e in x["events"]:
            s["events"][e] = s["events"].get(e, 0) + 1
    by_cause: dict = {}
    for x in series:
        if x["invalidated"]:
            c = x["invalidated_cause"] or "?"
            by_cause[c] = by_cause.get(c, 0) + x["invalidated"]
    ars = [a for x in series for a in x["auto_recall"]]
    sd = [v for x in series for v in x["s_over_delta"] if v is not None]
    s.update({"fixed_tokens": series[0]["fixed"], "invalidated_total": sum(x["invalidated"] for x in series),
              "invalidated_by_cause": by_cause,
              "switch_at": [x["i"] for x in series if "switch" in x["events"]],
              "auto_recall": {"triggers": len(ars), "injections": sum(1 for a in ars if a["injected"]),
                              "pieces": sum(a["injected"] or 0 for a in ars),
                              "tokens": sum(a["tokens"] or 0 for a in ars)},
              "s_over_delta": {"n": len(sd), "min": min(sd, default=None), "max": max(sd, default=None),
                               "mean": round(sum(sd) / len(sd), 3) if sd else None}})
    md = [(b["tokens_saved"], b["tokens_saved_measured"]) for x in series for b in x["mask"]
          if b.get("tokens_saved_measured") is not None]
    if md:
        s["mask_delta_planned_vs_measured"] = {"n": len(md), "planned": sum(a for a, _ in md),
                                               "measured": sum(b for _, b in md),
                                               "max_abs_err": max(abs(a - b) for a, b in md)}
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "series-%s.json" % label), "w") as f:
            json.dump({"summary": s, "series": series}, f, ensure_ascii=False, indent=1)
    return s, series


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--pi-request", required=True)
    ap.add_argument("--tokenizer", required=True)
    ap.add_argument("--config", help="JSON con i campi di Config")
    ap.add_argument("--set", action="append", default=[], help="chiave=valore JSON (sovrascrive --config)")
    ap.add_argument("--label", default="run")
    ap.add_argument("--out")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    d = json.load(open(a.config)) if a.config else {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        d[k] = json.loads(v)
    d["slot_save"] = False          # niente motore: niente SAVE/ancore
    d["mask_anchor"] = False
    cfg = Config.from_dict(d)
    tc = TokenCounter(cfg.chars_per_token, a.tokenizer)
    if not tc.exact:
        sys.exit("tokenizer non caricato: " + tc.kind)
    s, _ = run(a.session, json.load(open(a.pi_request)), cfg, tc, a.label, a.out, limit=a.limit)
    print(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
