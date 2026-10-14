# Author: Maurizio Verde — LastReload
"""recall by id, page after page, must return every character of the archived text exactly once.

Found by the AgentLongBench harness (card t_72e4c026): when a page is cut by recall_max_tokens, the truncation
notice of fit_tokens stayed inside the page (the cleanup only knew the old "... use offset]" wording), the header
counted it as data and the next offset skipped as many real characters as the notice is long (146 per page).

    python3 -m unittest -v tests.test_recall_paging
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_proxy import Base, history  # noqa: E402
from ctxproxy.core import rid_for  # noqa: E402

PAGE = re.compile(r"^\[recall:(\S+) \u2014 [^\]]*characters (\d+)\u2013(\d+) of (\d+)\]\n", re.S)
NEXT = re.compile(r"\n\[continues: \S+ id=(\S+) offset=(\d+)\]$")


def ids_text(n: int = 900) -> str:
    # dense, non-repeating data, like the id lists of AgentLongBench: any lost character is a lost fact
    return "\n".join("item %04d -> code %06d" % (i, (i * 7919) % 1000003) for i in range(n)) + "\n"


class TestRecallPaging(Base):
    cfg_over = {"mask_enabled": False}

    def _pages(self, options: dict):
        for k, v in options.items():
            setattr(self.proxy.cfg, k, v)
        mgr = self.proxy.mgr
        h = history(1)
        text = ids_text()
        h[-1] = dict(h[-1], content=text)
        p = mgr.prepare({"messages": h, "tools": []})
        rid = rid_for(len(h) - 1, h[-1])
        got, off, pages = [], 0, 0
        while True:
            out = mgr.recall(p.conv, {"id": rid, "offset": off})
            m = PAGE.match(out)
            self.assertIsNotNone(m, out[:200])
            a, b, total = int(m.group(2)), int(m.group(3)), int(m.group(4))
            body = out[m.end():]
            nxt = NEXT.search(body)
            if nxt:
                body = body[:nxt.start()]
            self.assertEqual(a, off)
            self.assertEqual(total, len(text))
            self.assertNotIn("[recall truncated", body)
            self.assertEqual(body, text[a:b], "page %d is not the declared slice" % pages)
            got.append(body)
            pages += 1
            if not nxt:
                break
            self.assertEqual(int(nxt.group(2)), b, "next offset must start where this page ended")
            off = b
            self.assertLess(pages, 200)
        return text, "".join(got), pages

    def test_pages_cover_text_default(self):
        text, got, pages = self._pages({"recall_max_tokens": 300})
        self.assertGreater(pages, 3)          # the token cap, not recall_max_chars, cuts the pages
        self.assertEqual(got, text)

    def test_pages_cover_text_leaf_select(self):
        text, got, pages = self._pages({"recall_max_tokens": 300, "recall_leaf_select": True})
        self.assertGreater(pages, 3)
        self.assertEqual(got, text)


if __name__ == "__main__":
    unittest.main()
