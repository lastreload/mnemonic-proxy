"""Rigioco a secco di una sessione pi (JSONL) attraverso il Manager del proxy, SENZA motore.

Per ogni richiesta che pi ha fatto (= ogni messaggio assistant) ricostruisce la storia COMPLETA del client (come se
pi non avesse mai compattato), la passa a Manager.prepare e misura:
  - virtual: token della storia completa (stima);
  - fisico: token del prompt che il proxy manderebbe a Strata;
  - reread: token da rileggere rispetto al prompt fisico precedente (cache a prefisso: si rilegge dalla prima
    differenza a livello di messaggio);
  - quando scattano mask / freeze+switch.
e confronta con ciò che pi ha fatto davvero (usage.input+cacheRead di ogni richiesta; eventi compaction).

    python3 tools/replay_dry.py session/*.jsonl --tokenizer tokenizer.json \
        --out replay-out
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctxproxy.core import Config, Journal, Manager, Store, TokenCounter, canon  # noqa: E402

SENTINEL = "\u0000PI-SYSTEM-PROMPT+TOOLS\u0000"


class SentinelCounter(TokenCounter):
    """Il system prompt + tool di pi non sono nel file di sessione: si rappresentano con un messaggio sentinella il
    cui costo in token è ricavato dalla prima richiesta reale (usage.input - messaggio utente)."""

    def __init__(self, sys_tokens: int, *a, **k):
        super().__init__(*a, **k)
        self.sys_tokens = sys_tokens

    def raw(self, text: str) -> int:
        if text == SENTINEL:
            return self.sys_tokens
        return super().raw(text)


class StubUpstream:
    """Note di passaggio finte di lunghezza fissa (il contenuto non conta per i numeri di dimensione)."""

    def __init__(self, notes_tokens: int, cpt: float):
        self.text = ("nota " * int(notes_tokens * cpt / 5))
        self.calls = 0

    def chat(self, body):
        self.calls += 1
        return {"choices": [{"message": {"role": "assistant", "content": self.text}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

    def slot(self, action, fn):
        return {"n_saved": 0}


def load(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    return rows


def to_openai(m: dict, keep_reasoning: bool) -> list[dict]:
    role = m.get("role")
    parts = m.get("content") or []
    if role == "user":
        return [{"role": "user", "content": "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")}]
    if role == "assistant":
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        think = "".join(p.get("thinking", "") for p in parts if p.get("type") == "thinking")
        calls = [{"id": p["id"], "type": "function",
                  "function": {"name": p["name"], "arguments": json.dumps(p.get("arguments"), ensure_ascii=False)}}
                 for p in parts if p.get("type") == "toolCall"]
        out = {"role": "assistant", "content": text}
        if calls:
            out["tool_calls"] = calls
        if keep_reasoning and think:
            out["reasoning_content"] = think
        return [out]
    if role == "toolResult":
        content = []
        for p in parts:
            if p.get("type") == "text":
                content.append({"type": "text", "text": p.get("text", "")})
            elif p.get("type") == "image":
                content.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (
                    p.get("mimeType"), p.get("data", "")[:64])}})
        return [{"role": "tool", "tool_call_id": m.get("toolCallId"), "content": content}]
    return []


def run(path, cfg: Config, tokenizer, keep_reasoning: bool, label: str, notes_tokens: int, out_dir: str | None):
    rows = load(path)
    first_ass = next(r for r in rows if (r.get("message") or {}).get("role") == "assistant")
    first_user = next(r for r in rows if (r.get("message") or {}).get("role") == "user")
    tc0 = TokenCounter(cfg.chars_per_token, tokenizer)
    user_tokens = cfg.msg_overhead + tc0.count(first_user["message"]["content"][0]["text"])
    sys_tokens = first_ass["message"]["usage"]["input"] - user_tokens - 3
    tc = SentinelCounter(sys_tokens, cfg.chars_per_token, tokenizer)
    jpath = os.path.join(out_dir, "journal-%s.jsonl" % label) if out_dir else None
    if jpath and os.path.exists(jpath):
        os.remove(jpath)
    mgr = Manager(cfg, Store(":memory:"), tc, Journal(jpath))
    up = StubUpstream(notes_tokens, cfg.chars_per_token)
    msgs = [{"role": "system", "content": SENTINEL}]
    series = []
    prev_phys = None
    prev_canon = None
    pi_comp = []
    t0 = time.time()
    for r in rows:
        if r["type"] == "compaction":
            pi_comp.append({"ts": r["timestamp"], "tokensBefore": r["tokensBefore"], "request": len(series),
                            "input": (r.get("usage") or {}).get("input", 0),
                            "output": (r.get("usage") or {}).get("output", 0)})
            continue
        m = r.get("message")
        if not m:
            continue
        if m["role"] == "assistant":
            u = m.get("usage") or {}
            req = {"messages": list(msgs), "tools": None, "max_tokens": cfg.default_response}
            n_ev = len(mgr.journal.mem)
            p = mgr.prepare(req, upstream=up)
            evs = [e["event"] for e in mgr.journal.mem[n_ev:]]
            pc = [canon(x) for x in p.messages]
            _, per = mgr.estimate(p.messages, p.tools)
            if prev_canon is None:
                reread = p.est_tokens
            else:
                k = 0
                for a, b in zip(prev_canon, pc):
                    if a != b:
                        break
                    k += 1
                reread = sum(per[k:])
            series.append({"i": len(series), "ts": r["timestamp"], "virtual": p.virtual_tokens,
                           "physical": p.est_tokens, "reread": reread, "seg": p.seg, "masked": p.masked,
                           "masked_tokens": p.masked_tokens, "events": [e for e in evs if e != "new_conversation"],
                           "pi_prompt": (u.get("input") or 0) + (u.get("cacheRead") or 0),
                           "pi_reread": u.get("input") or 0})
            prev_canon = pc
        msgs += to_openai(m, keep_reasoning)
    return series, pi_comp, {"sys_tokens": sys_tokens, "tokenizer": tc.kind, "seconds": round(time.time() - t0, 1),
                             "notes_calls": up.calls}


def summarize(series, pi_comp, W):
    s = {}
    s["requests"] = len(series)
    s["virtual_final"] = series[-1]["virtual"]
    s["virtual_max"] = max(x["virtual"] for x in series)
    s["physical_max"] = max(x["physical"] for x in series)
    s["physical_final"] = series[-1]["physical"]
    s["reread_total"] = sum(x["reread"] for x in series)
    s["pi_reread_total"] = sum(x["pi_reread"] for x in series)
    s["pi_prompt_max"] = max(x["pi_prompt"] for x in series)
    s["mask_events"] = [(x["i"], x["ts"][11:19], x["virtual"], x["physical"]) for x in series if "mask" in x["events"]]
    s["switch_events"] = [(x["i"], x["ts"][11:19], x["virtual"], x["physical"]) for x in series
                          if "switch" in x["events"]]
    s["over_window"] = sum(1 for x in series if x["physical"] + 16384 + 8192 + 8 > W)
    s["pi_compactions"] = [(c["request"], c["ts"][11:19], c["tokensBefore"]) for c in pi_comp]
    s["pi_compaction_input"] = sum(c.get("input", 0) for c in pi_comp)
    s["pi_compaction_output"] = sum(c.get("output", 0) for c in pi_comp)
    s["over_128k_physical"] = sum(1 for x in series if x["physical"] > W)
    big = sorted(series, key=lambda x: -x["reread"])[:8]
    s["top_reread"] = [(x["i"], x["reread"], x["events"]) for x in big]
    return s


def calib(series, n):
    """Errore della stima sulla storia completa rispetto al prompt reale di pi, prima della prima compattazione."""
    pts = [(x["virtual"], x["pi_prompt"]) for x in series[:n] if x["pi_prompt"]]
    r = [v / p for v, p in pts]
    return {"n": len(pts), "ratio_mean": round(sum(r) / len(r), 3), "ratio_min": round(min(r), 3),
            "ratio_max": round(max(r), 3), "last": pts[-1]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--tokenizer")
    ap.add_argument("--out")
    ap.add_argument("--notes-tokens", type=int, default=3000)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    report = {}
    base = {"window": 131072}
    variants = {
        # calibrazione: nessuna gestione; con e senza il ragionamento dei turni vecchi
        "nessuna-gestione+reasoning": ({**base, "mask_enabled": False, "segments_enabled": False}, True),
        "nessuna-gestione-senza-reasoning": ({**base, "mask_enabled": False, "segments_enabled": False}, False),
    }
    for label, (c, kr) in variants.items():
        series, comp, meta = run(a.session, Config.from_dict(c), a.tokenizer, kr, label, a.notes_tokens, a.out)
        first_comp = comp[0]["request"] if comp else len(series)
        report[label] = {"meta": meta, "calib_before_first_pi_compaction": calib(series, first_comp),
                         "summary": summarize(series, comp, 131072)}
        json.dump(series, open(os.path.join(a.out, "series-%s.json" % label), "w"))
    # quale ipotesi sul ragionamento torna coi numeri reali di pi?
    e = {k: abs(report[k]["calib_before_first_pi_compaction"]["ratio_mean"] - 1) for k in variants}
    keep = e["nessuna-gestione+reasoning"] < e["nessuna-gestione-senza-reasoning"]
    report["reasoning_hypothesis"] = "pi rimanda reasoning_content" if keep else "pi NON rimanda reasoning_content"
    managed = {
        "solo-masking-uscite": {**base, "segments_enabled": False},
        "masking-totale": {**base, "segments_enabled": False, "mask_reasoning": True, "mask_tool_args": True},
        "masking-uscite+segmenti": {**base},
        "masking-totale+segmenti": {**base, "mask_reasoning": True, "mask_tool_args": True},
        "solo-segmenti": {**base, "mask_enabled": False},
    }
    for label, c in managed.items():
        series, comp, meta = run(a.session, Config.from_dict(c), a.tokenizer, keep, label, a.notes_tokens, a.out)
        report[label] = {"meta": meta, "config": c, "summary": summarize(series, comp, 131072)}
        json.dump(series, open(os.path.join(a.out, "series-%s.json" % label), "w"))
    json.dump(report, open(os.path.join(a.out, "report.json"), "w"), indent=1, ensure_ascii=False)
    print("tokenizer:", report["nessuna-gestione+reasoning"]["meta"]["tokenizer"],
          "| system+tool pi:", report["nessuna-gestione+reasoning"]["meta"]["sys_tokens"])
    for k in variants:
        print("calibrazione %-34s %s" % (k, report[k]["calib_before_first_pi_compaction"]))
    print(report["reasoning_hypothesis"])
    hdr = "%-26s %8s %8s %8s %9s %7s %5s %5s %5s %6s" % ("variante", "virt.fin", "fis.max", "fis.fin", "rilettura",
                                                         "s@2.5K", "mask", "swit", ">W*", ">128K")
    print(hdr)
    for k, v in report.items():
        if not isinstance(v, dict) or "summary" not in v:
            continue
        s = v["summary"]
        print("%-26s %8d %8d %8d %9d %7.0f %5d %5d %5d %6d" % (
            k, s["virtual_final"], s["physical_max"], s["physical_final"], s["reread_total"],
            s["reread_total"] / 2500, len(s["mask_events"]), len(s["switch_events"]), s["over_window"],
            s["over_128k_physical"]))
    s = report["solo-segmenti"]["summary"]
    pi_tot = s["pi_reread_total"] + s["pi_compaction_input"]
    print("%-26s %8s %8d %8s %9d %7.0f   (richieste %d + compattazioni %d letti; %d token di riassunti generati)" % (
        "pi reale (log)", "-", s["pi_prompt_max"], "-", pi_tot, pi_tot / 2500, s["pi_reread_total"],
        s["pi_compaction_input"], s["pi_compaction_output"]))
    print("  >W* = richieste con fisico + 16K risposta + 8K riserva + 8 > 131072")
    for k in managed:
        s = report[k]["summary"]
        print("\n==", k)
        print("  primo mask:", s["mask_events"][:1], " switch:", s["switch_events"])
        print("  top rilettura:", s["top_reread"][:5])
    print("\ncompattazioni pi (richiesta, ora, tokensBefore):", report["solo-segmenti"]["summary"]["pi_compactions"])


if __name__ == "__main__":
    main()
