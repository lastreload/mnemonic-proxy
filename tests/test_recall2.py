"""recall strutturato (recall2.py) e indizio del richiamo automatico: opzioni spente = comportamento di serie."""
import json
import unittest

from ctxproxy import core


def store_with(rows, fileops=()):
    st = core.Store(":memory:")
    st.archive_many(rows)
    if fileops:
        st.add_fileops(list(fileops))
    return st


ROWS = [
    # (rid, conv, idx, role, name, content, tokens, created)
    ("u000000000001", "c", 0, "user", "", "fai il gioco", 3, 0),
    ("t000000000002", "c", 1, "assistant-reasoning", "", "Economy: startGold 240 gold, lives 20.", 10, 0),
    ("a000000000003", "c", 1, "assistant-tool-args", "",
     'write {"path":"/p/src/config.js","content":"economy: {\\n  startGold: 265,\\n  startLives: 20\\n}"}', 30, 0),
    ("r000000000004", "c", 2, "tool", "write", "Successfully wrote 60 bytes to /p/src/config.js", 12, 0),
    ("a000000000005", "c", 3, "assistant-tool-args", "",
     'edit {"path":"/p/src/config.js","edits":[{"oldText":"startGold: 265","newText":"startGold: 250"}]}', 30, 0),
    ("r000000000006", "c", 4, "tool", "edit", "Successfully replaced 1 block in /p/src/config.js", 12, 0),
    ("r000000000007", "c", 5, "tool", "bash", "VITTORIA — vite superstiti 10/20", 9, 0),
]
FOPS = [("c", 2, "k1", "write", "/p/src/config.js", "riuscito", "a000000000003", "r000000000004", "write"),
        ("c", 4, "k2", "edit", "/p/src/config.js", "riuscito", "a000000000005", "r000000000006", "edit")]


class TestRecall2(unittest.TestCase):
    def mgr(self, **kw):
        cfg = core.Config(**kw)
        st = store_with(ROWS, FOPS)
        return core.Manager(cfg, st, core.TokenCounter(cfg.chars_per_token, None), core.Journal(None))

    def test_off_is_legacy(self):
        m = self.mgr()
        out = m.recall("c", {"query": "startGold"}, max_idx=10)
        self.assertTrue(out.startswith("[recall: "))
        self.assertEqual(out, m.recall_legacy("c", {"query": "startGold"}, max_idx=10))
        tools = [t["function"]["name"] for t in m.prepare_tools([]) ] if hasattr(m, "prepare_tools") else []
        self.assertTrue(tools == [] or core.RECALL_NAME in tools)

    def test_query_passage_and_links(self):
        m = self.mgr(recall_struct=True)
        out = m.recall("c", {"query": "startGold"}, max_idx=10)
        self.assertIn("startGold: 265", out)
        self.assertIn("id=a000000000003", out)

    def test_first_write_shows_args_and_reasoning(self):
        m = self.mgr(recall_struct=True)
        out = m.recall("c", {"path": "src/config.js", "mode": "first", "query": "startGold"}, max_idx=10)
        self.assertIn("FIRST write", out)   # 0.3.1: English
        self.assertIn("startGold: 265", out)
        self.assertIn("240 gold", out)

    def test_timeline_shows_edit(self):
        m = self.mgr(recall_struct=True)
        out = m.recall("c", {"path": "src/config.js", "mode": "timeline"}, max_idx=10)
        self.assertIn("startGold: 250", out)

    def test_max_idx_hides_future(self):
        m = self.mgr(recall_struct=True)
        out = m.recall("c", {"query": "VITTORIA"}, max_idx=5)
        self.assertNotIn("10/20", out)

    def test_multi_and_flex(self):
        m = self.mgr(recall_struct=True, recall_multi=True, recall_flex=True)
        out = m.recall("c", {"queries": ["start gold", "vite superstiti"]}, max_idx=10)
        self.assertIn("startGold", out)
        self.assertIn("10/20", out)

    def test_empty_gives_suggestions(self):
        m = self.mgr(recall_struct=True)
        out = m.recall("c", {"query": "zxqwv"}, max_idx=10)
        self.assertIn("no results", out)   # 0.3.1: English
        self.assertIn("config.js", out)

    def test_auto_hint_short_and_with_ids(self):
        m = self.mgr(auto_recall_min_score=0.0, auto_recall_min_terms=1)
        hidden = {r[0] for r in ROWS}
        cands = m.auto_candidates("c", ["startgold", "265"], hidden, set(), 10)
        self.assertTrue(cands)
        text, chosen = m.auto_hint(cands, ["startgold"])
        self.assertTrue(text.startswith(core.AUTO_HEAD_EN))   # 0.3.1: English
        self.assertIn("id=%s" % chosen[0]["rid"], text)
        self.assertNotIn("startLives", text)        # nessun testo dell'archivio


if __name__ == "__main__":
    unittest.main()
