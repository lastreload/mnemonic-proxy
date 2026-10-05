"""Test: conteggio esatto, segnaposti con provenienza, note con esito, FTS5/path,
📌 punti fermi, 🗑 usa e getta, coda protetta inclusiva."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import (DROP_SUFFIX, OUT_PREFIX, RECEIPT_OPEN, PIN_HEAD, Config, Journal, Manager, Store,  # noqa: E402
                           TokenCounter, call_note_text, chain, is_disposable, outcome, pin_texts)
from ctxproxy.render import render_pieces  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOKDIR = os.environ.get("CTX_TOKDIR", os.path.join(HERE, "..", "tok"))
SYSTEM = {"role": "system", "content": "sei un agente di programmazione"}
TOOLS = [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}
         for n in ("read", "write", "edit", "bash")]


def call(cid, name, **args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def mk(tc=None, **over):
    cfg = Config.from_dict({"window": 131072, "mask_trigger": 12000, "mask_target": 6000, "keep_recent_tokens": 2500,
                            "min_mask_tokens": 150, "min_batch_tokens": 200, "min_age_turns": 2,
                            "mask_anchor": False, **over})
    tmp = tempfile.TemporaryDirectory()
    st = Store(os.path.join(tmp.name, "a.sqlite"))
    j = Journal(os.path.join(tmp.name, "j.jsonl"))
    return Manager(cfg, st, tc or TokenCounter(3.5), j), tmp


def session(n=12, size=2600):
    """Storia sintetica di un agente: read/write/edit/bash su file, con un edit fallito."""
    msgs = [SYSTEM, {"role": "user", "content": "crea il gioco snake\n📌 usa solo canvas, niente librerie"}]
    for k in range(n):
        f = "src/f%d.js" % (k % 4)
        if k % 3 == 0:
            msgs.append({"role": "assistant", "content": "", "reasoning_content": "scrivo %s " % f * 60,
                         "tool_calls": [call("w%d" % k, "write", path=f, content=("// riga %d\n" % k) * 220)]})
            msgs.append({"role": "tool", "tool_call_id": "w%d" % k, "content": "Successfully wrote %s" % f})
        elif k % 3 == 1:
            msgs.append({"role": "assistant", "content": "", "reasoning_content": "leggo %s " % f * 60,
                         "tool_calls": [call("r%d" % k, "read", path=f)]})
            msgs.append({"role": "tool", "tool_call_id": "r%d" % k,
                         "content": "function update%d() {}\n" % k + ("riga del file %s\n" % f) * (size // 20)})
        else:
            msgs.append({"role": "assistant", "content": "", "reasoning_content": "provo " * 80,
                         "tool_calls": [call("b%d" % k, "bash", command="node test.js --caso %d" % k)]})
            msgs.append({"role": "tool", "tool_call_id": "b%d" % k,
                         "content": "Error: test %d fallito ZETA%d\n" % (k, k) + "traccia\n" * (size // 8)})
        msgs.append({"role": "assistant", "content": "passo %d fatto" % k})
        msgs.append({"role": "user", "content": "continua %d" % k})
    return msgs


class TestHelpers(unittest.TestCase):
    def test_marks(self):
        t = "ciao\n📌 la griglia è 40x30\n!! velocità fissa\n[[importante]]niente audio[[/importante]]"
        self.assertEqual(pin_texts(t), ["niente audio", "la griglia è 40x30", "velocità fissa"])
        self.assertTrue(is_disposable("🗑 avvia il server"))
        self.assertTrue(is_disposable("~ prova veloce"))
        self.assertFalse(is_disposable("~~barrato~~"))
        self.assertFalse(is_disposable("testo ~ normale"))

    def test_outcome_and_note(self):
        self.assertEqual(outcome("edit", "Could not find the exact text ... RETRYABLE"), "fallito")
        self.assertEqual(outcome("write", "Successfully wrote 300 bytes"), "riuscito")
        n = call_note_text("edit", "src/game.js", "a1", "Error: oldText not found in the current file")
        self.assertIn("FALLITO", n)
        self.assertIn("src/game.js", n)
        self.assertIn("nessuna modifica esterna", n)
        self.assertIn("recall id=a1", n)


class TestManager(unittest.TestCase):
    def prep(self, mgr, msgs):
        return mgr.prepare({"messages": msgs, "tools": TOOLS, "max_tokens": 256})

    def test_provenance_placeholders_and_no_placeholders_where_model_writes(self):
        mgr, tmp = mk()
        msgs = session()
        p = self.prep(mgr, msgs)
        self.assertGreater(p.masked, 0)
        ph = [m for m in p.messages if m.get("role") == "tool" and (m["content"].startswith(OUT_PREFIX) or m["content"].startswith(RECEIPT_OPEN))]
        self.assertTrue(ph)
        for m in ph:
            self.assertRegex(m["content"], r"uscita di (read src/f\d\.js|bash `node test\.js)")
            self.assertRegex(m["content"], r"Inizio: «|Ricevuta: ")
        for m in p.messages:
            if m.get("role") == "assistant":
                # mai segnaposti dove scrive il modello
                self.assertNotIn("omess", m.get("reasoning_content") or "")
                self.assertNotIn("gestore del contesto", m.get("content") or "")
                for c in m.get("tool_calls") or []:
                    self.assertNotIn("gestore del contesto", c["function"]["arguments"])
        # il punto fermo del primo messaggio è registrato e il messaggio non è mai nascosto
        self.assertEqual([r[2] for r in mgr.store.pins(p.conv)], ["usa solo canvas, niente librerie"])
        tmp.cleanup()

    def test_tail_protection_inclusive(self):
        mgr, tmp = mk()
        msgs = session()
        p = self.prep(mgr, msgs)
        ev = [e for e in p.events if e["event"] == "mask"][0]
        _, per = mgr.estimate(p.messages, p.tools)
        # il messaggio a cavallo del confine dei keep_recent_tokens non è stato toccato
        acc, j = 0, len(per) - 1
        while acc + per[j] <= mgr.cfg.keep_recent_tokens:
            acc += per[j]
            j -= 1
        self.assertNotIn(j, [x["indice"] for x in ev["pieces"]])
        tmp.cleanup()

    def test_fts_and_path_recall(self):
        mgr, tmp = mk()
        msgs = session()
        p = self.prep(mgr, msgs)
        out = mgr.recall(p.conv, {"query": "ZETA5 fallito"})
        self.assertIn("ZETA5", out)
        out = mgr.recall(p.conv, {"query": "zeta5"})       # FTS: maiuscole/minuscole
        self.assertIn("ZETA5", out)
        out = mgr.recall(p.conv, {"path": "src/f0.js"})
        self.assertIn("write src/f0.js", out)
        self.assertIn("riuscito", out)
        self.assertIn("id=a", out)
        # filtro di catena: niente risultati dalla domanda corrente in poi
        self.assertIn("nessun risultato", mgr.recall(p.conv, {"query": "ZETA11"}, max_idx=3))
        # id di un'altra conversazione: non trovato prima di cercare nella propria
        self.assertIn("non trovato", mgr.recall(p.conv, {"id": "r0000000000"}))
        tmp.cleanup()

    def test_reread_and_failed_edit_events(self):
        mgr, tmp = mk(mask_enabled=False)
        msgs = session()
        msgs += [{"role": "assistant", "content": "", "tool_calls": [call("e1", "edit", path="src/f1.js",
                                                                          oldText="x", newText="y")]},
                 {"role": "tool", "tool_call_id": "e1", "content": "Error: oldText not found in the current file"}]
        p = self.prep(mgr, msgs)
        names = [e["event"] for e in p.events]
        self.assertIn("reread", names)
        self.assertIn("edit_failed", names)
        p2 = self.prep(mgr, msgs)        # idempotente: nessun evento ripetuto
        self.assertNotIn("edit_failed", [e["event"] for e in p2.events])
        tmp.cleanup()

    def test_drop_exchange_becomes_receipt(self):
        mgr, tmp = mk()
        msgs = [SYSTEM, {"role": "user", "content": "~ avvia il server di prova"}]
        msgs += [{"role": "assistant", "content": "", "reasoning_content": "avvio " * 300,
                  "tool_calls": [call("s1", "bash", command="node tools/serve.mjs 8123")]},
                 {"role": "tool", "tool_call_id": "s1", "content": "in ascolto su 8123\n" + "log\n" * 900},
                 {"role": "assistant", "content": "server avviato"}]
        msgs += session(12)[1:]
        p = self.prep(mgr, msgs)
        self.assertIn(1, mgr.store.drops(p.conv))
        ev = [e for e in p.events if e["event"] == "mask"]
        self.assertTrue(any(x["tipo"] == "scambio" for e in ev for x in e["pieces"]))
        u = [m for m in p.messages if m.get("role") == "user" and "avvia il server" in m["content"]][0]
        self.assertIn("usa e getta", u["content"])
        self.assertIn("bash `node tools/serve.mjs 8123`: ", u["content"])
        self.assertFalse(any(m.get("tool_call_id") == "s1" for m in p.messages))
        self.assertTrue(mgr.store.masks_for([chain(msgs, TOOLS)[1] + DROP_SUFFIX]))
        tmp.cleanup()

    def test_pins_block(self):
        mgr, tmp = mk()
        msgs = session()
        self.prep(mgr, msgs)
        conv = mgr.store.find_conv(chain(msgs, TOOLS))[0]
        txt, ev = mgr.pins_block(conv, cut=10)
        self.assertTrue(txt.startswith(PIN_HEAD))
        self.assertIn("usa solo canvas, niente librerie", txt)
        mgr.cfg.pins_max_tokens = 5
        txt, ev = mgr.pins_block(conv, cut=10)
        self.assertTrue(ev["over_limit"])
        self.assertIn("avviso", txt)
        tmp.cleanup()


class TestMarksApi(unittest.TestCase):
    def test_pin_drop_via_proxy(self):
        from ctxproxy.server import Proxy
        mgr, tmp = mk()
        px = Proxy(mgr.cfg, None, mgr.store, mgr.journal, mgr.tc)
        msgs = session(4)
        p = px.mgr.prepare({"messages": msgs, "tools": TOOLS})
        st, out = px.mark_action({"conv": p.conv, "action": "pin", "index": 5, "text": "griglia 40x30"})
        self.assertEqual(st, 200)
        self.assertIn("griglia 40x30", [x["text"] for x in out["pins"]])
        st, out = px.mark_action({"conv": p.conv, "action": "drop", "index": 7})   # dentro lo scambio di #5
        self.assertEqual(out["index"], 5)
        self.assertEqual([d["index"] for d in out["drops"]], [5])
        pid = out["pins"][-1]["id"]
        st, out = px.mark_action({"conv": p.conv, "action": "unpin", "id": pid})
        self.assertFalse([x for x in out["pins"] if x["id"] == pid][0]["active"])
        st, out = px.mark_action({"conv": p.conv, "action": "undrop", "index": 5})
        self.assertEqual(out["drops"], [])
        tmp.cleanup()


@unittest.skipUnless(os.path.exists(os.path.join(TOKDIR, "vocab.json")), "tokenizer del pack non disponibile")
class TestExact(unittest.TestCase):
    tc = None

    @classmethod
    def setUpClass(cls):
        cls.tc = TokenCounter(3.5, TOKDIR)
        assert cls.tc.exact, cls.tc.kind

    def test_estimate_equals_full_render(self):
        mgr, tmp = mk(self.tc, mask_enabled=False)
        msgs = session(6)
        from ctxproxy.render import template_kwargs
        mgr.kw = template_kwargs({"reasoning_effort": "high"})
        tot, per = mgr.estimate(msgs, TOOLS)
        full = "".join(t for _, t in render_pieces(msgs, TOOLS, mgr.kw))
        self.assertEqual(tot, len(self.tc.tk.encode(full, add_special_tokens=False).ids))
        tmp.cleanup()

    def test_mask_delta_measured_equals_planned(self):
        mgr, tmp = mk(self.tc)
        p = mgr.prepare({"messages": session(), "tools": TOOLS, "max_tokens": 256, "reasoning_effort": "high"})
        ev = [e for e in p.events if e["event"] == "mask"][0]
        # Δ pianificato (somma dei risparmi per pezzo) vs Δ misurato sulla stessa richiesta: entro il 2%
        self.assertLessEqual(abs(ev["tokens_saved"] - ev["tokens_saved_measured"]),
                             0.02 * ev["tokens_saved_measured"] + 5, ev)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
