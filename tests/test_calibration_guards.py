# Author: Maurizio Verde — LastReload
"""Guards on the calibration of the token estimate (`TokenCounter.calibrate`).

The estimate drives masking: an estimate *below* the real prompt makes the proxy mask too late, and the
engine can cut the prompt in silence. Guards, all on the estimate side only (exact counting with a
tokenizer is never calibrated):

* a minimum number of accepted samples before the factor may be *lowered* (during warm-up it may only grow,
  which is the prudent direction);
* rejection of samples whose real/estimated ratio is outside a plausible range, journalled.

The clamp stays 0.5-2.0: with chars/3.5 as the raw estimate a factor below 1.0 is a legitimate correction
of a deliberately pessimistic baseline, not a defect (see CHANGELOG and RESULT.md for the measurement).
"""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import core  # noqa: E402
from tests.lang_scenario import run_session  # noqa: E402


def counter() -> core.TokenCounter:
    return core.TokenCounter(chars_per_token=3.5)


def feed(tc: core.TokenCounter, ratio: float, times: int, estimated: int = 4000) -> None:
    for _ in range(times):
        tc.calibrate(estimated, int(estimated * ratio))


class TestCalibrationGuards(unittest.TestCase):
    def test_warmup_never_lowers_the_estimate(self):
        """Before the minimum is reached the factor may grow but never shrink: masking stays prudent."""
        tc = counter()
        feed(tc, 1.8, 1)                               # one sample pushes the factor up
        after_one = tc.scale
        self.assertGreater(after_one, 1.0)
        tc.calibrate(4000, 2000)                       # ratio 0.5 would pull it back down
        self.assertEqual(tc.scale, after_one)
        self.assertLess(tc._pending, after_one)        # the average does move, it is just not applied yet
        self.assertEqual(tc.samples, 2)

    def test_the_factor_may_be_lowered_once_the_minimum_is_reached(self):
        tc = counter()
        feed(tc, 1.8, 1)
        after_one = tc.scale
        feed(tc, 0.5, core.CALIB_MIN_SAMPLES - 1)
        self.assertLess(tc.scale, after_one)
        self.assertEqual(tc.scale, tc._pending)

    def test_lower_clamp_is_unchanged(self):
        tc = counter()
        feed(tc, 0.25, core.CALIB_MIN_SAMPLES * 40)    # the engine keeps reporting a quarter of the estimate
        self.assertEqual(tc.scale, core.CALIB_FLOOR)
        self.assertEqual(core.CALIB_FLOOR, 0.5)

    def test_count_uses_the_applied_factor_not_the_pending_one(self):
        tc = counter()
        text = "x" * 3500
        feed(tc, 1.8, 1)
        feed(tc, 0.5, 2)
        self.assertEqual(tc.count(text), int(tc.raw(text) * tc.scale + 0.5))
        self.assertGreater(tc.count(text), tc.raw(text))

    def test_implausible_samples_are_rejected_and_journalled(self):
        tc = counter()
        journal = core.Journal(None)
        tc.journal = journal
        feed(tc, 1.2, core.CALIB_MIN_SAMPLES)          # converged on a plausible ratio
        converged = tc.scale
        tc.calibrate(4000, 400)                        # real/estimated 0.1: a cut prompt or a broken usage
        tc.calibrate(4000, 40000)                      # real/estimated 10.0
        self.assertEqual(tc.scale, converged)
        self.assertEqual(tc.samples, core.CALIB_MIN_SAMPLES)
        rejected = [r for r in journal.mem if r["event"] == "calibration_rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertEqual([r["ratio"] for r in rejected], [0.1, 10.0])
        self.assertEqual({r["estimated"] for r in rejected}, {4000})

    def test_rejection_without_a_journal_does_not_raise(self):
        tc = counter()
        feed(tc, 1.2, core.CALIB_MIN_SAMPLES)
        converged = tc.scale
        tc.calibrate(4000, 400)
        self.assertEqual(tc.scale, converged)

    def test_boundary_ratios_are_accepted(self):
        for ratio in (core.CALIB_MIN_RATIO, core.CALIB_MAX_RATIO):
            tc = counter()
            feed(tc, ratio, core.CALIB_MIN_SAMPLES)
            # accepted = counted as a sample; the resulting factor may still be pinned by a clamp
            self.assertEqual(tc.samples, core.CALIB_MIN_SAMPLES, "boundary ratio %s rejected" % ratio)

    def test_ratios_just_outside_the_range_are_rejected(self):
        for ratio in (core.CALIB_MIN_RATIO / 2, core.CALIB_MAX_RATIO * 2):
            tc = counter()
            feed(tc, ratio, core.CALIB_MIN_SAMPLES)
            self.assertEqual(tc.samples, 0, "out-of-range ratio %s accepted" % ratio)
            self.assertEqual(tc.scale, 1.0)

    def test_small_samples_are_still_ignored_and_not_counted(self):
        tc = counter()
        for _ in range(core.CALIB_MIN_SAMPLES * 2):
            tc.calibrate(500, 750)                     # under 1000 estimated tokens: too noisy
        self.assertEqual(tc.scale, 1.0)
        self.assertEqual(tc.samples, 0)

    def test_lower_clamp_still_bounds_a_persistent_overestimate(self):
        tc = counter()
        feed(tc, 0.5, core.CALIB_MIN_SAMPLES * 40)     # the engine keeps reporting half of the estimate
        self.assertGreaterEqual(tc.scale, core.CALIB_FLOOR)
        text = "y" * 7000
        self.assertLess(tc.count(text), tc.raw(text))

    def test_upper_clamp_unchanged(self):
        tc = counter()
        feed(tc, 4.0, core.CALIB_MIN_SAMPLES * 20)
        self.assertEqual(tc.scale, 2.0)

    def test_exact_counting_is_never_calibrated(self):
        tc = counter()
        tc.tk = object()                               # exact tokenizer loaded
        feed(tc, 1.5, core.CALIB_MIN_SAMPLES * 4)
        self.assertEqual(tc.scale, 1.0)
        self.assertEqual(tc.samples, 0)

    def test_zero_or_negative_real_is_not_a_sample(self):
        tc = counter()
        for _ in range(core.CALIB_MIN_SAMPLES * 2):
            tc.calibrate(4000, 0)
        self.assertEqual(tc.scale, 1.0)
        self.assertEqual(tc.samples, 0)

    def test_server_wires_the_journal_into_the_counter(self):
        """A rejected sample must be visible in the journal of a real run, not only in a unit test."""
        import inspect

        from ctxproxy import server
        src = inspect.getsource(server.serve)
        self.assertIn("counter.journal = journal", src)


class TestEffectOnThePhysicalPrompt(unittest.TestCase):
    """What the guards change in the prompt the engine receives, on the 26-request language scenario.

    The scenario replayed with `calib_min_samples=1` is byte-identical to 0.3.0 (tests/test_lang.py keeps
    that lock). Under the new default the masking decisions are the same — same requests, same messages,
    same texts — and the only difference is in the token counts the proxy prints (placeholders of blocks
    masked during the warm-up and the archived-pieces list of a segment), because the factor applied at
    that moment differs. Receipts and placeholders already written stay as they are: a stored mask keeps
    its count.
    """

    TOKENS = re.compile(r"\b(\d+) tokens?\b")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        for name in ("legacy", "guarded"):
            os.makedirs(os.path.join(cls.tmp.name, name))
        cls.legacy = run_session(os.path.join(cls.tmp.name, "legacy"), {"window_guard": False}, legacy_until=1,
                                 calib_min_samples=1)
        cls.guarded = run_session(os.path.join(cls.tmp.name, "guarded"), {"window_guard": False}, legacy_until=1,
                                  calib_min_samples=core.CALIB_MIN_SAMPLES)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def dumps(self, reqs):
        return [json.dumps({"c": r["conv"], "m": r["messages"], "t": r["tools"]}, ensure_ascii=False, sort_keys=True)
                for r in reqs]

    def test_same_shape_and_same_requests(self):
        self.assertEqual([(r["conv"], len(r["messages"])) for r in self.guarded],
                         [(r["conv"], len(r["messages"])) for r in self.legacy])

    def test_only_the_printed_token_counts_differ(self):
        a, b = self.dumps(self.legacy), self.dumps(self.guarded)
        self.assertEqual([self.TOKENS.sub("N tokens", x) for x in a],
                         [self.TOKENS.sub("N tokens", x) for x in b])

    def test_the_printed_counts_move_by_a_few_percent_at_most(self):
        worst = 0.0
        for x, y in zip(self.dumps(self.legacy), self.dumps(self.guarded)):
            na, nb = self.TOKENS.findall(x), self.TOKENS.findall(y)
            self.assertEqual(len(na), len(nb))
            for p, q in zip(na, nb):
                p, q = int(p), int(q)
                worst = max(worst, abs(p - q) / max(p, 1))
        self.assertGreater(worst, 0.0, "the scenario printed no count affected by the factor")
        self.assertLess(worst, 0.05, "a printed token count moved by %.1f%%" % (100 * worst))


if __name__ == "__main__":
    unittest.main()