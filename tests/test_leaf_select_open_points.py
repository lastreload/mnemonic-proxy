# Author: Maurizio Verde — LastReload
"""recall_leaf_select: open points checked before the PR (each test fails on the previous code and passes now, or
documents a behaviour that was already right).

1. recall2 forwarded offset=None/query=None when the model gave neither (an absent offset is not offset=0).
2. The recall cap ignored the proxy's own frame: header + continuation line of recall(id), and the warning line
   that fit_tokens appends (it reserved 30 tokens, the warning alone counts ~40). With the selector on, the result
   (frame included) now stays within the cap / the room.
3. recall(id) with no query and no strong match with the question: the selector steps aside, the model gets the
   start of the text exactly as with the option off.
4. Recall2.recall (recall_struct/flex/multi, MCP server) did not use the selector when called directly.

    python3 -m unittest -v tests.test_leaf_select_open_points
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import core  # noqa: E402
from tests.test_leaf_select import BIG_YAML  # noqa: E402

RID = "r0123456789ab"
PLAIN = "".join("line %04d value abc-%04d lorem ipsum dolor sit amet\n" % (i, i) for i in range(3000))
QUESTION = "In servizi.yaml, qual è la porta della replica del database del servizio pagamenti?"
STRUCT = {"recall_struct": True, "recall_flex": True, "recall_multi": True}


def _same_start(on: str, off: str) -> bool:
    """Same header up to the end offset, and the body of `on` is a prefix of the body of `off`."""
    h_on, b_on = on.split("\n", 1)
    h_off, b_off = off.split("\n", 1)
    cut = lambda h: h.split("\u2013")[0]       # "...characters 0" / "...caratteri 100"
    b_on = b_on.split("\n[recall trunc")[0].split("\n[recall tronc")[0].rsplit("\n[", 1)[0]   # no notes
    return cut(h_on) == cut(h_off) and b_off.startswith(b_on) and len(b_on) > 0


def mgr(text=BIG_YAML, **kw):
    cfg = core.Config(**kw)
    st = core.Store(":memory:")
    st.archive_many([("u000000000001", "c", 0, "user", "", "mostra servizi.yaml", 3, 0),
                     (RID, "c", 1, "tool", "bash", text, max(1, len(text) // 3), 0)])
    return core.Manager(cfg, st, core.TokenCounter(cfg.chars_per_token, None), core.Journal(None))


class TestRecall2Offset(unittest.TestCase):
    # 1 (recall2.py:457 on 7ff7e7f)
    def test_absent_fields_are_not_forwarded(self):
        m = mgr(recall_max_tokens=400, **STRUCT)
        seen = []
        orig = m.recall_legacy
        m.recall_legacy = lambda conv, args, *a, **k: (seen.append(dict(args)), orig(conv, args, *a, **k))[1]
        m.recall("c", {"id": RID}, max_idx=5)
        self.assertEqual(seen[-1], {"id": RID})
        m.recall("c", {"id": RID, "offset": 0}, max_idx=5)
        self.assertEqual(seen[-1], {"id": RID, "offset": 0})          # offset=0 stays a sequential read
        m.recall("c", {"id": RID, "query": "replica"}, max_idx=5)
        self.assertEqual(seen[-1], {"id": RID, "query": "replica"})


class TestFrameInBudget(unittest.TestCase):
    # 2
    def test_fit_tokens_exact_counts_the_warning(self):
        for lang in ("it", "en"):
            m = mgr()
            m.lang = lang
            for cap in (60, 100, 300, 1000):
                self.assertLessEqual(m.tc.count(m.fit_tokens(PLAIN, cap, exact=True)), cap, (lang, cap))

    def test_fit_tokens_default_unchanged(self):
        # default (option off): same text as 0.3.2, which can exceed the cap by the size of the warning
        m = mgr()
        out = m.fit_tokens(PLAIN, 300)
        self.assertEqual(out[:-200], PLAIN[:len(out) - 200])
        self.assertGreater(m.tc.count(out), 300)

    def test_recall_id_fallback_within_cap(self):
        # text the selector cannot parse -> recall_legacy by id: header + chunk + continuation <= recall_max_tokens
        for over in ({}, STRUCT):
            for cap in (300, 1000):
                m = mgr(PLAIN, recall_leaf_select=True, recall_max_tokens=cap, **over)
                m.question = "what is abc-0100?"
                out = m.recall("c", {"id": RID}, max_idx=5)
                self.assertRegex(out.split("\n")[0], r"characters 0\u2013")
                self.assertIn("offset=", out.split("\n")[-1])
                self.assertLessEqual(m.tc.count(out), cap, (over, cap))
                off = mgr(PLAIN, recall_max_tokens=cap, **over).recall("c", {"id": RID}, max_idx=5)
                self.assertGreater(m.tc.count(off), cap)            # the option-off result does exceed it

    def test_selector_excerpt_within_room(self):
        m = mgr(recall_leaf_select=True, recall_max_tokens=6000)
        m.question = QUESTION
        for room in (120, 200, 300):
            out = m.recall("c", {"id": RID}, max_idx=5, max_tokens=room)
            self.assertLessEqual(m.tc.count(out), room)
            self.assertIn("porta: 47213", out)


class TestIdWithoutQuery(unittest.TestCase):
    # 3: no query, no strong match -> start of the text, exactly as with the option off
    def test_weak_or_no_question_is_start_of_file(self):
        # same reading as with the option off (start of the text, same continuation offset logic); the only
        # difference is point 2: the chunk is shorter so that header + chunk + continuation fit the cap
        for over in ({}, STRUCT):
            for cap in (400, 6000):
                off = mgr(recall_max_tokens=cap, **over).recall("c", {"id": RID}, max_idx=5)
                for q in ("", "ok", "continua", "correggi il bug", "fai i test", "riassumi", "replica",
                          "ok, vai avanti col refactoring"):
                    m = mgr(recall_leaf_select=True, recall_max_tokens=cap, **over)
                    m.question = q
                    on = m.recall("c", {"id": RID}, max_idx=5)
                    self.assertNotRegex(on.split("\n")[0], "ESTRATTO|EXCERPT", (over, q))
                    self.assertTrue(_same_start(on, off), (over, cap, q))
                    self.assertLessEqual(m.tc.count(on), cap, (over, cap, q))
                    if cap == 6000:                       # whole text fits: byte-identical to the option off
                        self.assertEqual(on, off, (over, q))

    def test_strong_question_selects(self):
        m = mgr(recall_leaf_select=True, recall_max_tokens=400)
        m.question = QUESTION
        out = m.recall("c", {"id": RID}, max_idx=5)
        self.assertIn("porta: 47213", out)
        self.assertNotIn("46097", out)


class TestRecall2UsesSelector(unittest.TestCase):
    # 4 (recall2.py:446 on 7ff7e7f): the selector is used on the recall2 path too
    def test_direct_recall2_call(self):
        from ctxproxy.recall2 import Recall2
        m = mgr(recall_leaf_select=True, recall_max_tokens=400, **STRUCT)
        m.question = QUESTION
        out = Recall2(m).recall("c", {"id": RID}, 5, [])
        self.assertRegex(out.split("\n")[0], "ESTRATTO|EXCERPT")
        self.assertIn("porta: 47213", out)
        self.assertNotIn("46097", out)

    def test_manager_with_struct_same_as_without(self):
        a = mgr(recall_leaf_select=True, recall_max_tokens=400, **STRUCT)
        b = mgr(recall_leaf_select=True, recall_max_tokens=400)
        a.question = b.question = QUESTION
        for args in ({"id": RID}, {"id": RID, "query": "replica"}, {"query": "47213"}):
            self.assertEqual(a.recall("c", args, max_idx=5), b.recall("c", args, max_idx=5), args)

    def test_struct_modes_unchanged(self):
        # mode: recall2 as before, with the option on or off; explicit offset: same reading from that offset,
        # chunk within the cap (point 2)
        for args in ({"mode": "tree"}, {"id": RID, "mode": "neighbors"}, {"id": RID, "offset": 100}):
            a = mgr(recall_leaf_select=True, recall_max_tokens=400, **STRUCT)
            b = mgr(recall_max_tokens=400, **STRUCT)
            a.question = QUESTION
            on, off = a.recall("c", args, max_idx=5), b.recall("c", args, max_idx=5)
            if "offset" in args:
                self.assertTrue(_same_start(on, off), args)
                self.assertLessEqual(a.tc.count(on), 400)
            else:
                self.assertEqual(on, off, args)


if __name__ == "__main__":
    unittest.main()
