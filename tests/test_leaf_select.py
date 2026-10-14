# Author: Maurizio Verde — LastReload
"""Selettore a sotto-blocco foglia (leafsel.py, opzione recall_leaf_select).

Casi giocattolo per formato: la foglia giusta, le sole righe-chiave degli antenati, nessun fratello con lo stesso
attributo, righe verbatim, budget rispettato, fallback al comportamento di serie con parse incerto/opzione spenta,
recall2 che non passa più offset quando manca.

    python3 -m unittest -v tests.test_leaf_select
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import core, leafsel  # noqa: E402

YAML = """# servizi.yaml
versione: 7
servizi:
  anagrafica:
    immagine: reg/anagrafica:1
    database:
      motore: mariadb
      primario:
        host: db-anagrafica-01
        porta: 43314
      replica:
        host: db-anagrafica-02
        porta: 41383
        sola_lettura: true
  pagamenti:
    immagine: reg/pagamenti:4
    http:
      porta: 27535
    database:
      motore: postgres
      primario:
        host: db-pagamenti-01
        porta: 46097
      replica:
        host: db-pagamenti-02
        porta: 47213
        sola_lettura: true
    cache:
      ttl_s: 281
  sondaggi:
    database:
      replica:
        host: db-sondaggi-02
        porta: 21102
"""

TOML = """# impianti.toml
versione = 4

[impianti.adige]
comune = "Bassano"

[impianti.adige.pompa_riserva]
modello = "KSB-1"
soglia_bar = 11.30

[impianti.brenta]
comune = "Cittadella"

[impianti.brenta.pompa_principale]
modello = "KSB-164"
soglia_bar = 6.36

[impianti.brenta.pompa_riserva]
modello = "GRF-921"
portata_lps = 50
soglia_bar = 8.65
"""

XML = """<?xml version="1.0" encoding="UTF-8"?>
<!-- sensori.xml -->
<catalogo versione="7">
  <centrale nome="arsiero" zona="nord">
    <sensore id="T-0447" tipo="ntc">
      <soglia_min>18.0</soglia_min>
      <soglia_max>64.3</soglia_max>
    </sensore>
  </centrale>
  <centrale nome="valbruna" zona="ovest">
    <sensore id="T-0474" tipo="pt100">
      <soglia_min>16.1</soglia_min>
      <soglia_max>59.8</soglia_max>
    </sensore>
    <sensore id="T-0447" tipo="ntc">
      <soglia_min>20.0</soglia_min>
      <soglia_max>71.3</soglia_max>
    </sensore>
  </centrale>
</catalogo>
"""

JSON = json.dumps({"services": [
    {"name": "orders-api", "port": 40793, "db": {"host": "o-1", "port": 5432}},
    {"name": "orders-db", "port": 48219, "db": {"host": "o-2", "port": 5433}},
    {"name": "orders-db-replica", "port": 29409, "db": {"host": "o-3", "port": 5434}},
]}, indent=2)

DOCKER = """CONTAINER ID   IMAGE          COMMAND       CREATED      STATUS      PORTS                     NAMES
a1d9136cc36a   postgres:16    "/bin/sh"     3 days ago   Up 3 days   0.0.0.0:29409->5432/tcp   orders-db-replica
5fe834a85700   orders:2.8     "/app/o"      3 days ago   Up 3 days   0.0.0.0:40793->8080/tcp   orders-api
30b5221c6249   postgres:16    "/bin/sh"     3 days ago   Up 3 days   0.0.0.0:48219->5432/tcp   orders-db
c2b60c565847   redis:7.4      "/entry"      3 days ago   Up 10 hrs   0.0.0.0:40717->9090/tcp   cache-svc-2
"""

MDTAB = """# release_matrix.md

| component | version | owner | status |
|---|---|---|---|
| vortex-queue | 1.2.3 | Ada Moreau | beta |
| vortex-cache | 4.12.3 | Solveig Brekke | stable |
| ember-store | 9.19.7 | Dmitri Orlov | stable |
"""

CSV = """name,port,owner
orders-api,40793,ada
orders-db,48219,solveig
cache-svc,40717,dmitri
"""


def count(t):
    return len(t) // 3


def pick(text, question="", query="", budget=4000):
    out, info = leafsel.leaf_recall(text, question, query, "r0123456789ab", "messaggio 3 (tool bash)", count, budget)
    return out, info


def body_lines(out):
    return out.split("\n")[1:-1]


class TestFormats(unittest.TestCase):
    def check(self, text, question, must, forbid=(), query="", fmt=None):
        out, info = pick(text, question, query)
        self.assertIsNotNone(out, info)
        if fmt:
            self.assertEqual(info["fmt"], fmt)
        body = body_lines(out)
        orig = text.split("\n")
        for l in body:
            if l != leafsel.GAP:
                self.assertIn(l, orig, "riga non verbatim: %r" % l)
        joined = "\n".join(body)
        for m in must:
            self.assertIn(m, joined)
        for f in forbid:
            self.assertNotIn(f, joined)
        self.assertIn("offset=0", out.split("\n")[-1])
        self.assertIn("r0123456789ab", out.split("\n")[-1])
        return out, info

    def test_yaml_leaf_and_ancestors_only(self):
        out, info = self.check(YAML, "qual è la porta della replica del database del servizio pagamenti?",
                               ["servizi:", "  pagamenti:", "    database:", "      replica:", "porta: 47213"],
                               ["46097", "41383", "21102", "27535", "postgres"], fmt="yaml")
        self.assertEqual(info["blocks"], 1)
        self.assertEqual(body_lines(out), ["servizi:", leafsel.GAP, "  pagamenti:", leafsel.GAP, "    database:",
                                           leafsel.GAP, "      replica:", "        host: db-pagamenti-02",
                                           "        porta: 47213", "        sola_lettura: true"])

    @unittest.skipIf(sys.version_info < (3, 11), "TOML selection needs tomllib (Python >= 3.11)")
    def test_toml_section(self):
        out, _ = self.check(TOML, "qual è la soglia_bar della pompa di riserva dell'impianto brenta?",
                            ["[impianti.brenta]", "[impianti.brenta.pompa_riserva]", "soglia_bar = 8.65"],
                            ["6.36", "11.30", "Cittadella"], fmt="toml")

    @unittest.skipIf(sys.version_info >= (3, 11), "fallback only without tomllib")
    def test_toml_falls_back_without_tomllib(self):
        # Python 3.10: no tomllib, so TOML is never parsed and recall stays the normal one
        self.assertIsNone(leafsel.parse_toml(TOML.split("\n"), TOML))

    def test_xml_element_same_id_other_parent(self):
        # lo stesso id T-0447 sta in due centrali: la domanda (valbruna) distingue
        self.check(XML, "qual è la soglia_max del sensore T-0447 della centrale valbruna?",
                   ['<centrale nome="valbruna" zona="ovest">', '<sensore id="T-0447" tipo="ntc">', "71.3"],
                   ["64.3", "59.8"], query="T-0447", fmt="xml")

    def test_json_object(self):
        self.check(JSON, "which port has the service orders-db?", ['"name": "orders-db"', "48219"],
                   ["40793", "29409"], fmt="json")

    def test_docker_table_row_with_header(self):
        out, _ = self.check(DOCKER, "which host port is mapped to the container orders-db?",
                            ["CONTAINER ID", "48219"], ["29409", "40793"], fmt="table")
        self.assertEqual(len(body_lines(out)), 3)        # intestazione, ⋮, riga

    def test_markdown_table(self):
        self.check(MDTAB, "Who is the owner of the component vortex-cache?", ["| component |", "Solveig Brekke"],
                   ["Ada Moreau", "Dmitri Orlov"], fmt="mdtab")

    def test_csv(self):
        self.check(CSV, "owner of orders-db?", ["name,port,owner", "orders-db,48219,solveig"], ["40793"],
                   fmt="csv")

    def test_plural_returns_several_blocks(self):
        out, info = pick(YAML, "quali sono le porte delle repliche dei database?")
        self.assertIsNotNone(out)
        self.assertGreaterEqual(info["blocks"], 3)
        for v in ("47213", "41383", "21102"):
            self.assertIn(v, out)
        self.assertLessEqual(info["blocks"], leafsel.MAX_BLOCKS)


class TestFallback(unittest.TestCase):
    def test_log_and_python_are_parsed_as_events_and_defs(self):
        # log con marca temporale = eventi; Python valido = def/class (prima: comportamento di serie)
        log = "\n".join("2026-10-06 08:%02d:00 INFO auth handled request rq-%d in 20 ms" % (i, i) for i in range(40))
        self.assertEqual(leafsel.parse(log)[0], "log")
        py = "def a(x):\n    return x\n\n\nclass B:\n    porta = 1\n\n    def c(self):\n        return 2\n"
        self.assertEqual(leafsel.parse(py)[0], "py")
        self.assertIsNone(leafsel.parse("def a(x:\n    return x\n\ndef b():\n    pass\n"))   # sintassi rotta
        self.assertIsNone(leafsel.parse("testo libero\nsenza struttura\nné marche\ntemporali\n"))

    def test_broken_formats(self):
        self.assertIsNone(leafsel.parse(JSON[:-3]))             # json troncato
        self.assertIsNone(leafsel.parse(XML.replace("</catalogo>", "")))
        self.assertIsNone(leafsel.parse(TOML + "\n[rotto\n"))
        self.assertIsNone(leafsel.parse("a: |\n  testo\n  altro\nb: 1\nc:\n  d: 2\n"))   # blocco letterale yaml

    def test_no_discriminating_word(self):
        out, why = pick(YAML, "dimmi tutto")
        self.assertIsNone(out)

    def test_budget(self):
        out, info = pick(YAML, "quali sono le porte delle repliche dei database?", budget=10_000)
        n_all = info["blocks"]
        # appena sotto l'estratto pieno si accorcia prima la cornice (tutti i blocchi restano)
        small = count(out) - 1
        out2, info2 = pick(YAML, "quali sono le porte delle repliche dei database?", budget=small)
        self.assertIsNotNone(out2)
        self.assertLessEqual(count(out2), small)
        self.assertEqual(info2["blocks"], n_all)
        # più stretto ancora: si tolgono i blocchi meno pertinenti, con l'avviso
        b = small
        while info2["blocks"] == n_all:
            b -= 1
            out2, info2 = pick(YAML, "quali sono le porte delle repliche dei database?", budget=b)
            self.assertIsNotNone(out2)
        self.assertLessEqual(count(out2), b)
        self.assertLess(info2["blocks"], n_all)
        self.assertIn("non mostrati", out2.split("\n")[-1])
        out3, why = pick(YAML, "porta della replica del database pagamenti", budget=20)
        self.assertIsNone(out3)
        self.assertEqual(why, "oltre il budget")


BIG_YAML = YAML + "".join(
    "  servizio%02d:\n    database:\n      primario:\n        host: db-s%02d-01\n        porta: %d\n"
    "      replica:\n        host: db-s%02d-02\n        porta: %d\n" % (i, i, 30000 + i, i, 31000 + i)
    for i in range(30))

ROWS = [
    ("u000000000001", "c", 0, "user", "", "mostra servizi.yaml", 3, 0),
    ("r0123456789ab", "c", 1, "tool", "bash", BIG_YAML, 900, 0),
    ("r0123456789cd", "c", 2, "tool", "bash", "2026 INFO ok\n" * 30, 80, 0),
]


class TestManager(unittest.TestCase):
    def mgr(self, **kw):
        cfg = core.Config(**kw)
        st = core.Store(":memory:")
        st.archive_many(ROWS)
        m = core.Manager(cfg, st, core.TokenCounter(cfg.chars_per_token, None), core.Journal(None))
        m.question = "In servizi.yaml, qual è la porta della replica del database del servizio pagamenti?"
        return m

    def test_off_by_default(self):
        self.assertFalse(core.Config().recall_leaf_select)
        m = self.mgr(recall_max_tokens=100)
        self.assertEqual(m.recall("c", {"id": "r0123456789ab"}, max_idx=5),
                         m.recall_legacy("c", {"id": "r0123456789ab"}, max_idx=5))

    def test_id_only_uses_question(self):
        m = self.mgr(recall_leaf_select=True, recall_max_tokens=400)
        out = m.recall("c", {"id": "r0123456789ab"}, max_idx=5)
        self.assertIn("porta: 47213", out)
        self.assertNotIn("46097", out)
        self.assertIn("last user question", out.split("\n")[0])
        self.assertIn("recall id=r0123456789ab offset=0", out)

    def test_text_that_fits_is_given_whole(self):
        m = self.mgr(recall_leaf_select=True, recall_max_tokens=6000)
        self.assertEqual(m.recall("c", {"id": "r0123456789ab"}, max_idx=5),
                         m.recall_legacy("c", {"id": "r0123456789ab"}, max_idx=5))

    def test_offset_and_unparsable_fall_back(self):
        m = self.mgr(recall_leaf_select=True, recall_max_tokens=400)
        for a in ({"id": "r0123456789ab", "offset": 0}, {"id": "r0123456789ab", "offset": 200},
                  {"id": "r0123456789cd"}, {"id": "r0123456789cd", "query": "INFO"}):
            self.assertEqual(m.recall("c", a, max_idx=5), m.recall_legacy("c", a, max_idx=5), a)

    def test_query_only_and_id_query(self):
        m = self.mgr(recall_leaf_select=True, recall_max_tokens=6000)
        for a in ({"query": "porta"}, {"id": "r0123456789ab", "query": "replica"}):
            out = m.recall("c", a, max_idx=5)
            self.assertIn("porta: 47213", out, a)
            self.assertNotIn("46097", out, a)
        m.question = ""
        out = m.recall("c", {"query": "db-pagamenti-02"}, max_idx=5)
        self.assertIn("porta: 47213", out)

    def test_max_tokens_is_respected(self):
        m = self.mgr(recall_leaf_select=True, recall_max_tokens=6000)
        out = m.recall("c", {"id": "r0123456789ab"}, max_idx=5, max_tokens=250)
        self.assertLessEqual(m.tc.count(out), 250)
        self.assertIn("porta: 47213", out)

    def test_bad_id_uses_latest_output_chosen_by_question(self):
        m = self.mgr(recall_leaf_select=True, recall_max_tokens=400)
        out = m.recall("c", {"id": "recall id=servizi.yaml"}, max_idx=5)
        self.assertIn("porta: 47213", out)
        self.assertIn("used id=r0123456789ab", out.split("\n")[0])
        m.question = "dimmi tutto"
        self.assertEqual(m.recall("c", {"id": "recall id=servizi.yaml"}, max_idx=5),
                         m.recall_legacy("c", {"id": "recall id=servizi.yaml"}, max_idx=5))

    def test_query_absent_everywhere_uses_question(self):
        m = self.mgr(recall_leaf_select=True, recall_max_tokens=400)
        out = m.recall("c", {"query": "5848"}, max_idx=5)
        self.assertIn("porta: 47213", out)
        self.assertNotIn("46097", out)
        self.assertIn("appears in no output", out.split("\n")[0])

    def test_recall2_does_not_invent_offset(self):
        m = self.mgr(recall_struct=True, recall_max_tokens=400)
        seen = []
        orig = m.recall_legacy

        def spy(conv, args, *a, **k):
            seen.append(dict(args))
            return orig(conv, args, *a, **k)
        m.recall_legacy = spy
        m.recall("c", {"id": "r0123456789ab"}, max_idx=5)
        self.assertEqual(seen[-1], {"id": "r0123456789ab"})
        m.recall("c", {"id": "r0123456789ab", "offset": 0}, max_idx=5)
        self.assertEqual(seen[-1], {"id": "r0123456789ab", "offset": 0})


if __name__ == "__main__":
    unittest.main()
