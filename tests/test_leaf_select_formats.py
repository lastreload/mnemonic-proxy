# Author: Maurizio Verde — LastReload
"""Selettore a sotto-blocco foglia: budget, candidati, contraddizioni, omissioni, recall senza query, formati.

1. budget prima della selezione: il server rifà la scelta nello spazio effettivo; quello che arriva al modello è
   l'estratto composto dal selettore (con la sua riga finale), mai tagliato a metà;
2. più candidati per archivio: con sola query si prova più di un'uscita che la contiene;
3. contraddizioni: domanda "replica" -> ramo "primario" penalizzato; due interpretazioni quasi pari -> entrambe;
4. omissioni con intervalli e offset; controllo automatico estratto == originale[a:b] (Unicode, CRLF);
5. recall(id) senza query: si seleziona solo con corrispondenza forte; domande non pertinenti -> comportamento di
   serie;
+ formati: json (identità del record antenato), diff (hunk + intestazione file), log (eventi), pytest, py (def),
  git log (commit).

    python3 -m unittest -v tests.test_leaf_select_formats
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import core, leafsel  # noqa: E402
from tests.test_leaf_select import BIG_YAML, ROWS, YAML, body_lines, count, pick  # noqa: E402

ACCOUNTS = json.dumps({"object": "list", "data": [
    {"id": "acct-%s" % k, "status": "active", "owner": {"name": "N%s" % k, "country": "IT"},
     "billing": {"plan": "basic", "settlement": {"currency": "USD", "window_code": "WX-%s" % k, "cutoff_utc": "16:00",
                                                 "batch": {"size": 3, "retry": 4}}}}
    for k in ("24ur", "77qz", "91ab")]}, indent=2)

DIFF = """diff --git a/internal/sched/batch_1.go b/internal/sched/batch_1.go
index eff73c2..2bef7f3 100644
--- a/internal/sched/batch_1.go
+++ b/internal/sched/batch_1.go
@@ -13,7 +13,7 @@ const (
 \tspillMax = 181
 \tbackoffLimit = 428
 \tprobeMin = 548
-\tdrainFloor = 274
+\tdrainFloor = 1993
 \twindowSize = 352
 \tdrainMin = 926
 )
diff --git a/internal/relay/budget_3.go b/internal/relay/budget_3.go
index e19c1c2..4e72778 100644
--- a/internal/relay/budget_3.go
+++ b/internal/relay/budget_3.go
@@ -19,7 +19,7 @@ const (
 \tbudgetMin = 4
 \tbackoffSize = 445
 \twindowSize = 987
-\trateCeilingKbps = 512
+\trateCeilingKbps = 1984
 \tshardMax = 975
 \tbackoffMax = 426
 )
"""

LOG = "\n".join([
    "2026-10-08T09:00:00.184Z INFO  fx-rates txn tx-2EA9-75 queued for settlement",
    "2026-10-08T09:00:13.603Z INFO  fraud-check txn tx-D8B5-38 passed (score 0.30)",
    "2026-10-08T09:00:26.732Z INFO  gateway txn tx-D7C1-24 passed (score 0.23)",
    "2026-10-08T09:00:52.016Z WARN  fraud-check txn tx-58A1-90 refused: velocity rule R7 (score 0.93)",
    "2026-10-08T09:00:18.438Z INFO  fraud-check heartbeat ok (lag 81 ms)",
    "2026-10-08T09:01:31.451Z INFO  gateway txn tx-7AC9-65 accepted in 129 ms",
    "2026-10-08T09:01:44.002Z WARN  gateway txn tx-11AA-02 refused: card expired",
]) + "\n"

PYTEST = "\n".join(["============================= test session starts =============================="] + [
    "tests/test_ledger.py::test_case_%02d PASSED                      [ %2d%%]" % (i, i * 4) for i in range(12)] + [
    "tests/test_ledger.py::test_rounding_half_even_negative FAILED                  [ 84%]",
    "tests/test_refunds.py::test_caches_mixed_locale PASSED                       [ 90%]",
    "=========================== 1 failed, 13 passed in 0.42s ===========================", ""])

PY = '''from decimal import Decimal


def aggrega_fornitore(codice: str, limite: int = 22) -> bool:
    """Aggrega fornitore."""
    return False


def riconcilia_lotti(magazzino_id: int, soglia: Decimal, *, dry_run: bool = True) -> list:
    """Allinea i lotti contabili con quelli fisici sopra la soglia."""
    differenze = []
    return differenze


def riconcilia_fornitori(fornitore_id: int) -> list:
    return []
'''

GITLOG = """commit 1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d
Author: Ada <ada@example.org>
Date:   Mon Sep 28 09:00:00 2026 +0200

    store: speed up migrate

 src/billing/tax.rs |  89 ++++++++++++++++++++++++++++++--------------------
 1 file changed, 53 insertions(+), 36 deletions(-)

commit 7f7adf6de64ccfb4f0e6ed02b3eb61b0c41e426a
Author: Bram Visser <bram@example.org>
Date:   Mon Sep 28 09:42:56 2026 +0200

    billing: prorate partial months by calendar days

 src/billing/proration.rs | 170 ++++++++++++++++++++++++++++++--------------------
 src/billing/invoice.rs   |  16 ++++++++++++----
 2 files changed, 155 insertions(+), 31 deletions(-)

commit f5d61ed87c75ce74ed9cd1e60ef769241c75414a
Author: Jonas Berg <jonas@example.org>
Date:   Tue Sep 29 10:00:00 2026 +0200

    api: auth cleanup

 src/api/auth.rs    |  73 ++++++++++++++++++++++++++++++--------------------
 1 file changed, 60 insertions(+), 13 deletions(-)
"""


def verbatim(test, out, text):
    orig = text.split("\n")
    for l in body_lines(out):
        if l != leafsel.GAP:
            test.assertIn(l, orig, "riga non verbatim: %r" % l)


class TestNewFormats(unittest.TestCase):
    def test_json_record_identity_is_kept(self):
        out, info = pick(ACCOUNTS, "What is the window_code in the settlement block of account acct-77qz?")
        self.assertIsNotNone(out, info)
        verbatim(self, out, ACCOUNTS)
        self.assertIn('"id": "acct-77qz"', out)
        self.assertIn('"settlement": {', out)
        self.assertIn('"window_code": "WX-77qz"', out)
        self.assertNotIn("WX-24ur", out)
        self.assertNotIn("WX-91ab", out)

    def test_diff_hunk_with_file_header(self):
        out, info = pick(DIFF, "In changes.diff, what new value was rateCeilingKbps changed to?")
        self.assertIsNotNone(out, info)
        self.assertEqual(info["fmt"], "diff")
        verbatim(self, out, DIFF)
        self.assertIn("+\trateCeilingKbps = 1984", out)
        self.assertIn("+++ b/internal/relay/budget_3.go", out)
        self.assertNotIn("drainFloor", out)

    def test_diff_bad_counts_fall_back(self):
        self.assertIsNone(leafsel.parse(DIFF.replace("@@ -19,7 +19,7 @@", "@@ -19,9 +19,7 @@")))

    def test_log_events(self):
        out, info = pick(LOG, "Which transaction id was refused by fraud-check in gateway.log?")
        self.assertIsNotNone(out, info)
        self.assertEqual(info["fmt"], "log")
        self.assertIn("tx-58A1-90 refused", out)
        self.assertNotIn("tx-D8B5-38", out)

    def test_pytest_failed_line(self):
        out, info = pick(PYTEST, "Quale test è fallito in pytest_report.txt? Dammi il nome completo del test.")
        self.assertIsNotNone(out, info)
        self.assertEqual(info["fmt"], "pytest")
        self.assertIn("test_rounding_half_even_negative FAILED", out)

    def test_query_and_question_disagree_both_shown(self):
        text = PYTEST.replace("=========================== 1 failed",
                              "E       AssertionError: Decimal('-2.5') != Decimal('-2')\n"
                              "=========================== 1 failed")
        out, info = pick(text, "Quale test è fallito in pytest_report.txt? Dammi il nome completo del test.",
                         query="AssertionError")
        self.assertIsNotNone(out, info)
        self.assertIn("AssertionError: Decimal", out)
        self.assertIn("test_rounding_half_even_negative FAILED", out)

    def test_python_def(self):
        out, info = pick(PY, "Qual è la firma della funzione riconcilia_lotti in gestione_lotti.py?")
        self.assertIsNotNone(out, info)
        self.assertEqual(info["fmt"], "py")
        self.assertIn("def riconcilia_lotti(magazzino_id: int, soglia: Decimal, *, dry_run: bool = True) -> list:",
                      out)
        self.assertNotIn("def riconcilia_fornitori", out)

    def test_gitlog_commit(self):
        out, info = pick(GITLOG, "In git_log.txt, how many insertions in total does the commit that changed "
                                 "src/billing/proration.rs have?")
        self.assertIsNotNone(out, info)
        self.assertEqual(info["fmt"], "gitlog")
        self.assertIn("2 files changed, 155 insertions(+), 31 deletions(-)", out)
        self.assertNotIn("53 insertions", out)


class TestSolPoints(unittest.TestCase):
    # 3: contraddizioni
    def test_primary_branch_is_penalised(self):
        yaml = ("servizi:\n  pagamenti:\n    database:\n      primario:\n        porta: 46097\n"
                "      replica:\n        porta: 47213\n  ordini:\n    database:\n      primario:\n        porta: 1\n"
                "      replica:\n        porta: 2\n")
        out, info = pick(yaml, "porta della replica del database pagamenti")
        self.assertIsNotNone(out, info)
        self.assertIn("47213", out)
        self.assertNotIn("46097", out)
        r, _, _ = leafsel._rank(leafsel.parse(yaml)[2], yaml.split("\n"),
                                *leafsel.question_terms("porta della replica del database pagamenti"))
        top = [x[2] for x in r[:2]]
        self.assertNotIn(0, top[:1])           # la foglia 'primario' di pagamenti non è prima

    def test_two_plausible_readings_are_both_shown(self):
        # 'porta' sia in http sia in database: la domanda non distingue -> entrambe, con i loro antenati
        yaml = ("servizi:\n  pagamenti:\n    http:\n      porta: 27535\n    database:\n      porta: 46097\n"
                "  ordini:\n    http:\n      porta: 1\n    database:\n      porta: 2\n")
        out, info = pick(yaml, "porta del servizio pagamenti")
        self.assertIsNotNone(out, info)
        self.assertIn("27535", out)
        self.assertIn("46097", out)
        self.assertIn("    http:", out)
        self.assertIn("    database:", out)

    def test_lexicon_it_en(self):
        out, info = pick(YAML, "what is the port of the payments database replica? (pagamenti)")
        self.assertIsNotNone(out, info)
        self.assertIn("47213", out)
        self.assertNotIn("46097", out)

    # 4: omissioni e fedeltà
    def test_omitted_ranges_with_offsets(self):
        out, info = pick(YAML, "qual è la porta della replica del database del servizio pagamenti?")
        tail = out.split("\n")[-1]
        self.assertIn("righe omesse:", tail)
        starts, pos = [], 0
        for l in YAML.split("\n"):
            starts.append(pos)
            pos += len(l) + 1
        import re
        for a, b, off in re.findall(r"(\d+)\u2013(\d+) offset=(\d+)", tail.split("righe omesse:")[1]):
            self.assertEqual(int(off), starts[int(a) - 1])
            self.assertTrue(set(range(int(a), int(b) + 1)).isdisjoint(set(info["lines"])))

    def test_faithful_unicode_crlf(self):
        text = "città:\r\n  nome: Forlì\r\n  porta: 1\r\naltra:\r\n  nome: Ñandú\r\n  porta: 2\r\n"
        out, info = pick(text, "porta di Forlì")
        if out is not None:
            lines = text.split("\n")
            for k in info["lines"]:
                self.assertTrue(lines[k - 1].endswith("\r"))
            self.assertTrue(leafsel.faithful(text, [k - 1 for k in info["lines"]]))
        self.assertFalse(leafsel.faithful("a\nb", [5]))

    # 5: domanda non pertinente
    def test_unrelated_question_falls_back(self):
        for q in ("correggi il bug", "ok continua", "fai i test", "riassumi", "dimmi tutto"):
            self.assertIsNone(leafsel.select(BIG_YAML, q), q)
        # una sola parola, non identificatore: corrispondenza debole
        self.assertIsNone(leafsel.select(BIG_YAML, "replica"))
        self.assertIsNotNone(leafsel.select(BIG_YAML, "replica pagamenti"))

    def test_manager_unrelated_question_is_legacy(self):
        cfg = core.Config(recall_leaf_select=True, recall_max_tokens=400)
        st = core.Store(":memory:")
        st.archive_many(ROWS)
        m = core.Manager(cfg, st, core.TokenCounter(cfg.chars_per_token, None), core.Journal(None))
        for q in ("correggi il bug", "ok, vai avanti col refactoring"):
            m.question = q
            self.assertEqual(m.recall("c", {"id": "r0123456789ab"}, max_idx=5),
                             m.recall_legacy("c", {"id": "r0123456789ab"}, max_idx=5), q)


ROWS2 = ROWS + [
    ("r0123456789ef", "c", 3, "tool", "bash", "porta 47213 citata in un log libero\nnessuna struttura qui\n" * 3, 40, 0),
]


class TestManagerCandidates(unittest.TestCase):
    # 2: più candidati: la query compare in un testo libero (più recente) e nello yaml; si sceglie lo yaml
    def test_query_tries_more_outputs(self):
        cfg = core.Config(recall_leaf_select=True, recall_max_tokens=400)
        st = core.Store(":memory:")
        st.archive_many(ROWS2)
        m = core.Manager(cfg, st, core.TokenCounter(cfg.chars_per_token, None), core.Journal(None))
        m.question = "In servizi.yaml, qual è la porta della replica del database del servizio pagamenti?"
        out = m.recall("c", {"query": "47213"}, max_idx=9)
        self.assertIn("r0123456789ab", out.split("\n")[0])
        self.assertRegex(out.split("\n")[0], "ESTRATTO|EXCERPT")
        self.assertIn("porta: 47213", out)


class TestServerBudget(unittest.TestCase):
    """1: il server passa lo spazio effettivo; quello che arriva al modello è l'estratto del selettore, intero."""

    def test_delivered_is_selector_output(self):
        from ctxproxy import engines
        from ctxproxy.core import RECALL_NAME, Config, Journal, Store, TokenCounter
        from ctxproxy.server import Proxy
        from ctxproxy.upstream import Upstream
        from tests.fake_engine import FakeEngine
        from tests.test_proxy import TOOLS
        got, state = [], {"k": None}

        def policy(body):
            msgs = body["messages"]
            last = msgs[-1]
            if last.get("role") == "user" and "replica" in str(last.get("content")):
                state["k"] = 0
            if last.get("role") == "tool" and state["k"] is not None and state["k"] > 0:
                got.append(str(last.get("content")))
            if state["k"] == 0:
                import re
                rid = re.findall(r"r[0-9a-f]{12}", json.dumps(msgs))[0]
                state["k"] = 1
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"type": "function", "function": {"name": RECALL_NAME, "arguments": json.dumps({"id": rid})}}]}
            return {"role": "assistant", "content": "ok"}

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        eng = FakeEngine(policy, flavor="ds4", n_ctx=8192)
        url = eng.start()
        try:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(root, "examples", "config.ds4.json")) as f:
                raw = json.load(f)
            raw["recall_leaf_select"] = True
            cfg = Config.from_dict(raw)
            jpath = os.path.join(tmp.name, "journal.jsonl")
            up, j = Upstream(url), Journal(jpath)
            e = engines.setup(cfg, up, j, explicit=set(raw), log=None)
            proxy = Proxy(cfg, up, Store(os.path.join(tmp.name, "a.sqlite")), j, TokenCounter(3.5))
            proxy.engine = e
            h = [{"role": "system", "content": "sei un agente di programmazione"},
                 {"role": "user", "content": "Esegui: cat servizi.yaml"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": "call_cat", "type": "function",
                                                                      "function": {"name": "bash", "arguments":
                                                                                   json.dumps({"command": "cat x"})}}]},
                 {"role": "tool", "tool_call_id": "call_cat", "content": BIG_YAML * 3},
                 {"role": "assistant", "content": "Letto."}]
            for k in range(14):
                h += [{"role": "user", "content": "passo %d" % k},
                      {"role": "assistant", "content": "", "tool_calls": [{"id": "call_s%d" % k, "type": "function",
                                                                           "function": {"name": "bash", "arguments":
                                                                                        json.dumps({"command": "ls"})}}]},
                      {"role": "tool", "tool_call_id": "call_s%d" % k, "content": "auth 1\nbilling 1\n" * 30},
                      {"role": "assistant", "content": "Fatto %d." % k}]
                st, r, _ = proxy.chat({"model": "m", "messages": h + [{"role": "user", "content": "ok"}],
                                       "tools": TOOLS, "max_tokens": 2048})
                self.assertEqual(st, 200, r)
            h.append({"role": "user", "content": "Senza rieseguire: qual è la porta della replica del database del "
                                                 "servizio pagamenti in servizi.yaml?"})
            st, r, _ = proxy.chat({"model": "m", "messages": h, "tools": TOOLS, "max_tokens": 2048})
            self.assertEqual(st, 200, r)
            proxy.store.db.close()
        finally:
            eng.stop()
        self.assertTrue(got, "il recall deve arrivare al motore")
        d = got[0]
        self.assertIn("porta: 47213", d)
        self.assertRegex(d.split("\n")[0], "ESTRATTO|EXCERPT")
        self.assertTrue(d.rstrip().endswith("]") and re.search("righe omesse:|omitted lines:", d.split("\n")[-1]),
                        d[-200:])
        self.assertNotRegex(d, "troncat|truncated")
        with open(jpath) as f:
            ev = [json.loads(l) for l in f]
        self.assertFalse([x for x in ev if x["event"] == "recall_trimmed"], "nessun taglio dopo la selezione")
        tmp.cleanup()


class TestP5CompactFrame(unittest.TestCase):
    """Se l'estratto pieno supera il budget di poco, prima si accorcia la cornice (intestazione/coda), poi si
    tolgono blocchi; mai la pagina di serie tagliata a metà riga (caso dev '        "monthly": ', 1/1555)."""

    Q_JSON = "What is the window_code in the settlement block of account acct-77qz?"
    Q_GIT = "In git_log.txt, how many insertions in total does the commit that changed src/billing/proration.rs have?"

    def _tight(self, text, q, query=""):
        full, _ = pick(text, q, query)
        self.assertIsNotNone(full)
        lo = None
        for b in range(count(full) - 1, 0, -1):
            out, info = pick(text, q, query, budget=b)
            if out is None:
                break
            lo = (b, out, info)
        return full, lo

    def test_json_fits_with_compact_frame(self):
        full, lo = self._tight(ACCOUNTS, self.Q_JSON)
        self.assertIsNotNone(lo, "con budget appena sotto l'estratto pieno deve entrare la cornice corta")
        b, out, info = lo
        self.assertLess(b, count(full) - 20)
        self.assertLessEqual(count(out), b)
        self.assertIn('"window_code": "WX-77qz"', out)
        self.assertIn('"id": "acct-77qz"', out)
        verbatim(self, out, ACCOUNTS)
        self.assertIn("offset=0", out.split("\n")[-1])

    def test_gitlog_fits_with_compact_frame(self):
        full, lo = self._tight(GITLOG, self.Q_GIT, "Insertions")
        self.assertIsNotNone(lo)
        b, out, info = lo
        self.assertLess(b, count(full) - 20)
        self.assertIn("2 files changed, 155 insertions(+), 31 deletions(-)", out)
        verbatim(self, out, GITLOG)

    def test_every_body_line_is_whole_original_line(self):
        # per ogni budget in cui il selettore risponde: ogni riga del corpo è una riga intera dell'originale
        full, _ = pick(ACCOUNTS, self.Q_JSON)
        orig = ACCOUNTS.split("\n")
        for b in range(count(full), 0, -3):
            out, info = pick(ACCOUNTS, self.Q_JSON, budget=b)
            if out is None:
                self.assertEqual(info, "oltre il budget")
                break
            for ln in body_lines(out):
                self.assertTrue(ln == leafsel.GAP or ln in orig, (b, ln))

    def test_compact_frame_it(self):
        text = ACCOUNTS
        full, _ = leafsel.leaf_recall(text, self.Q_JSON, "", "r0123456789ab", "messaggio 3 (tool bash)", count, 4000)
        out, _ = leafsel.leaf_recall(text, self.Q_JSON, "", "r0123456789ab", "messaggio 3 (tool bash)", count,
                                     count(full) - 10)
        self.assertIsNotNone(out)
        self.assertIn("ESTRATTO", out.split("\n")[0])
        self.assertIn("testo intero", out.split("\n")[-1])


if __name__ == "__main__":
    unittest.main()
