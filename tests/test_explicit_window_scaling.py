# Author: Maurizio Verde — LastReload
"""A `window` written in the config below the engine's n_ctx scales the thresholds too.

Found on Strata 0.2.13 (n_ctx 131072) with {"window": 32768}: the thresholds stayed at their 131K defaults
(mask_trigger 80000, tail_max 24000, reserve 8192), so masking never ran and every segment started already too
full: ten segment switches in a row, one per request. The 0.3.24 scaling only covered engines that report no n_ctx.

    python3 -m unittest -v tests.test_explicit_window_scaling
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import engines  # noqa: E402
from ctxproxy.core import Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402
from tests.test_proxy import SYSTEM, TOOLS, tool_round  # noqa: E402


class TestApply(unittest.TestCase):
    def test_window_below_engine_n_ctx_scales_the_defaults(self):
        cfg = Config.from_dict({"window": 32768})
        defaults = Config()
        engines.apply(cfg, engines.Engine("strata", n_ctx=131072), explicit={"window"}, log=None)
        self.assertEqual(cfg.window, 32768)
        for f in ("mask_trigger", "mask_target", "tail_max", "reserve", "keep_recent_tokens"):
            self.assertEqual(getattr(cfg, f), int(getattr(defaults, f) * 0.25), f)

    def test_fields_written_by_the_user_are_kept(self):
        cfg = Config.from_dict({"window": 32768, "tail_max": 9000})
        engines.apply(cfg, engines.Engine("strata", n_ctx=131072), explicit={"window", "tail_max"}, log=None)
        self.assertEqual(cfg.tail_max, 9000)
        self.assertEqual(cfg.mask_trigger, int(Config().mask_trigger * 0.25))

    def test_window_equal_to_engine_is_unchanged(self):
        cfg = Config.from_dict({"window": 131072})
        engines.apply(cfg, engines.Engine("strata", n_ctx=131072), explicit={"window"}, log=None)
        self.assertEqual((cfg.mask_trigger, cfg.tail_max), (Config().mask_trigger, Config().tail_max))


class TestNoSwitchCascade(unittest.TestCase):
    """The session that showed it: one tool output of ~3,800 tokens per request, 32K window, engine with 131K."""

    def test_reads_mask_instead_of_switching_every_request(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        eng = FakeEngine(flavor="strata", n_ctx=131072)
        url = eng.start()
        self.addCleanup(eng.stop)
        raw = {"window": 32768}
        cfg = Config.from_dict(raw)
        up = Upstream(url)
        jpath = os.path.join(tmp.name, "journal.jsonl")
        j = Journal(jpath)
        engine = engines.setup(cfg, up, j, explicit=set(raw), log=lambda *_: None)
        p = Proxy(cfg, up, Store(os.path.join(tmp.name, "a.sqlite")), j, TokenCounter(3.5))
        p.engine = engine
        self.addCleanup(p.store.db.close)
        msgs = [SYSTEM, {"role": "user", "content": "read the twelve files and add up the readings"}]
        for k in range(24):
            msgs += tool_round(k, 13300)            # ~3,800 tokens per output at 3.5 characters per token
            st, r, _ = p.chat({"model": eng.model, "messages": msgs, "tools": TOOLS, "max_tokens": 4096})
            self.assertEqual(st, 200, r)
        with open(jpath) as f:
            evs = [json.loads(x) for x in f]
        ev = [e["event"] for e in evs]
        switches = ev.count("switch")
        batch = [e for e in evs if e["event"] == "mask" and not e.get("emergency")]
        emergency = [e for e in evs if e["event"] == "mask" and e.get("emergency")]
        # Unscaled (mask_trigger 80000 > window, reserve 8192) the ordinary masking never runs: every request either
        # hides the output the model has just asked for (emergency mask of the current tool) or switches segment.
        self.assertGreater(len(batch), 0, "ordinary masking must run inside a 32K window")
        self.assertLessEqual(len(emergency), 2, "current outputs hidden on %d of 24 requests" % len(emergency))
        # 24 outputs of ~3,800 tokens are ~91K tokens: a few switches are fine, one per request (the cascade) is not.
        self.assertLessEqual(switches, 4, "segment switches: %d" % switches)


if __name__ == "__main__":
    unittest.main()
