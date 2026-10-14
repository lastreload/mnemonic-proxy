# Author: Maurizio Verde — LastReload
"""0.3.3 window guard (window.py): no request the proxy sends to the engine may exceed the window.

The fake engine runs in strict mode: it rejects (HTTP 400) every chat request with prompt + max_tokens > n_ctx and
every request whose thinking budget leaves no answer (INV-2). The engine's prompt count is independent of the guard:
the pack tokenizer + render_pieces when CTX_TOKDIR is available (exact, as Strata), otherwise the proxy's chars/3.5
counter on the rendered prompt (same counter the guard uses: tests the plumbing, not the count).

    python3 -m unittest -v tests.test_window_guard
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import window  # noqa: E402
from ctxproxy.core import RECALL_NAME, Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.render import render_pieces, template_kwargs  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402
from tests.test_proxy import TOOLS  # noqa: E402

TOKDIR = os.environ.get("CTX_TOKDIR", "")
EXACT = bool(TOKDIR) and os.path.exists(os.path.join(TOKDIR, "vocab.json"))


def file_text(k: int, n_lines: int) -> str:
    return "".join("svc-%d line %d: host db-%d-%03d port %d status ok latency %d ms\n"
                   % (k, i, k, i, 30000 + i * 7 + k, (i * 13 + k) % 400) for i in range(n_lines))


class Scenario:
    """A long agent session with big tool outputs (masked/archived), then a question that makes the model call
    recall many times, each round with a long reasoning: the loop grows past the window unless the guard acts."""

    def __init__(self, window: int, rounds: int = 6, reasoning_chars: int = 6000, stream: bool = False,
                 policy: str = "value", extra: dict | None = None):
        self.window, self.rounds, self.rchars, self.stream = window, rounds, reasoning_chars, stream
        self.k = None
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.eng = FakeEngine(self.policy, n_ctx=window)
        self.eng.strict_window = True
        tc = TokenCounter(3.5, TOKDIR if EXACT else None)
        if EXACT:
            ref = TokenCounter(3.5, TOKDIR)

            def engine_count(body):
                msgs = [{k: v for k, v in m.items() if k != "extra"} for m in body.get("messages") or []]
                return sum(ref.raw(t) for _, t in render_pieces(msgs, body.get("tools"), template_kwargs(body)))
            self.eng.counter = engine_count
        url = self.eng.start()
        w = window
        cfg = Config.from_dict({"window": w, "mask_trigger": w // 2, "mask_target": w // 4,
                                "keep_recent_tokens": w // 8, "min_mask_tokens": 200, "reserve": w // 16,
                                "default_response": 2048, "tail_max": w // 4, "notes_max_tokens": 1024,
                                "recall_max_tokens": 4096, "max_recall_rounds": 4, "recall_removal": policy,
                                **(extra or {})})
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        self.proxy = Proxy(cfg, Upstream(url), Store(os.path.join(self.tmp.name, "a.sqlite")), Journal(self.jpath), tc)
        if not EXACT:
            self.eng.counter = lambda body: self.proxy.wguard.count_body(body)

    def policy(self, body):
        msgs = body["messages"]
        last = msgs[-1]
        names = {((t or {}).get("function") or {}).get("name") for t in body.get("tools") or []}
        if last.get("role") == "user" and "QUESTION" in str(last.get("content")):
            self.k = 0
        if self.k is not None and self.k < self.rounds and RECALL_NAME in names:
            self.k += 1
            return {"role": "assistant", "content": "", "reasoning_content": ("thinking about svc-%d. " % self.k)
                    * (self.rchars // 24) + "\nConclusion: look at svc-%d next." % self.k,
                    "tool_calls": [{"id": "rc%d" % self.k, "type": "function", "function": {
                        "name": RECALL_NAME, "arguments": json.dumps({"query": "svc-%d" % self.k})}}]}
        return {"role": "assistant", "content": "answer: port 30007"}

    def run(self, steps: int = 10, size: int = 400):
        h = [{"role": "system", "content": "you are a coding agent"}, {"role": "user", "content": "inspect the services"}]
        out = []
        for k in range(1, steps + 1):
            cid = "call_%d" % k
            h += [{"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": "cat svc-%d.log" % k})}}]},
                  {"role": "tool", "tool_call_id": cid, "content": file_text(k, size)},
                  {"role": "assistant", "content": "read svc-%d" % k}, {"role": "user", "content": "next"}]
            out.append(self.send(h))
        h.append({"role": "user", "content": "QUESTION: which port did svc-3 line 7 use? use recall if needed"})
        out.append(self.send(h))
        return out

    def send(self, h):
        req = {"model": "m", "messages": h, "tools": TOOLS, "max_tokens": 2048}
        if self.stream:
            chunks = []
            st, r, _ = self.proxy.chat(req, emit=lambda c: chunks.append(c) if c is not None else None)
        else:
            st, r, _ = self.proxy.chat(req)
        return st, r

    def events(self, name=None, action=None):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath) as f:
            ev = [json.loads(l) for l in f]
        return [e for e in ev if (name is None or e["event"] == name) and (action is None or e.get("action") == action)]

    def close(self):
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()


class TestStrictEngine(unittest.TestCase):
    def check(self, sc: Scenario, results):
        self.assertEqual(sc.eng.violations, [], "requests sent over the window")
        self.assertGreater(sc.eng.checked, 10)
        self.assertEqual(sc.proxy.wguard.violations, 0)
        for st, r in results:
            if st != 200:
                self.assertEqual((st, r["error"]["code"]), (400, "window_guard"), r)
        st, r = results[-1]
        self.assertEqual(st, 200, r)

    def test_sweep_windows_and_policies(self):
        for w in (16384, 24576, 32768):
            for pol in window.POLICIES:
                for stream in (False, True):
                    with self.subTest(window=w, policy=pol, stream=stream):
                        sc = Scenario(w, policy=pol, stream=stream)
                        try:
                            res = sc.run()
                            self.check(sc, res)
                            self.assertTrue(sc.events("recall"), "the loop must run")
                        finally:
                            sc.close()

    def test_long_loop_results_never_empty(self):
        """Long reasoning per round: every recall result still reaches the model (pieces are removed to make room,
        INV-3), and the loop ends with an answer."""
        sc = Scenario(16384, rounds=20, reasoning_chars=12000, extra={"max_recall_rounds": 8})
        try:
            res = sc.run()
            self.check(sc, res)
            rec = sc.events("recall")
            self.assertGreaterEqual(len(rec), 4)
            self.assertTrue(all(e["tokens"] > 0 for e in rec), [e["tokens"] for e in rec])
            self.assertTrue(sc.events("window_guard", "remove"))
        finally:
            sc.close()

    def test_plan_round_final_for_space(self):
        """P0 leaves room for the answer floor but not for one more search: the round is final, the proxy's tools
        are not offered and the model is told why (note on the last result)."""
        from types import SimpleNamespace
        from ctxproxy.core import LEGACY_RECALL_NAME
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        cfg = Config.from_dict({"window": 16384})
        px = Proxy(cfg, Upstream("http://127.0.0.1:9"), Store(os.path.join(tmp.name, "a.sqlite")), Journal(None),
                   TokenCounter(3.5))
        self.addCleanup(px.store.db.close)
        rtool = {"type": "function", "function": {"name": RECALL_NAME, "parameters": {"type": "object"}}}
        p = SimpleNamespace(conv="c", tools=TOOLS + [rtool], recall_name=RECALL_NAME, lang="en", catalog=None,
                            recall_aliases=frozenset((RECALL_NAME, LEGACY_RECALL_NAME)))
        body = {"max_tokens": 2048}
        floor = px._answer_floor(body)
        # P0 sized so that P0 + F + margin fits but P0 + CALL + RESULT_MIN + F + margin does not
        target = 16384 - 64 - floor - 300
        phys0 = [{"role": "system", "content": "s"}, {"role": "user", "content": "x" * int(target * 3.5)}]
        while px._wcount(phys0, p.tools, body) > target:
            phys0[1]["content"] = phys0[1]["content"][:-35]
        loop = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {
            "name": RECALL_NAME, "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": "a", "content": "short"}]
        final, tools = px._plan_round(p, phys0, loop, body, 1, floor, "q")
        self.assertTrue(final)
        self.assertNotIn(RECALL_NAME, {t["function"]["name"] for t in tools or []})
        self.assertIn("no room left for more searches", window.clean(loop)[-1]["content"])
        P = px._wcount(phys0 + window.clean(loop), tools, body)
        self.assertLessEqual(P + floor + px.wguard.margin, 16384)
        # round limit reached (room plenty): final too, and still no internal tools - a recall there would be
        # dropped and the answer left empty (smoke G3, 2_i6_j13_a_0892); no space note in this case
        loop2 = [{"role": "assistant", "content": "", "tool_calls": [{"id": "b", "type": "function", "function": {
            "name": RECALL_NAME, "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": "b", "content": "short"}]
        small = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        final, tools = px._plan_round(p, small, loop2, body, cfg.max_recall_rounds + 1, floor, "q")
        self.assertTrue(final)
        self.assertNotIn(RECALL_NAME, {t["function"]["name"] for t in tools or []})
        self.assertNotIn("no room left for more searches", window.clean(loop2)[-1]["content"])

    def test_guard_off_goes_over_the_window(self):
        """Control: the same scenario without the guard does send over-window requests (the bug of ALB w16k)."""
        sc = Scenario(16384, rounds=20, reasoning_chars=12000, extra={"window_guard": False})
        try:
            sc.run()
            self.assertTrue(sc.eng.violations, "the scenario must exercise the over-window path")
        finally:
            sc.close()

    def test_thinking_budget_inv2(self):
        sc = Scenario(16384, extra={"answer_thinking_after_recall": True, "answer_thinking_budget": 4096})
        try:
            res = sc.run()
            self.check(sc, res)
            for b in sc.eng.requests:
                rb = b.get("reasoning_budget_tokens")
                if isinstance(rb, int) and rb > 0:
                    self.assertLess(rb, b["max_tokens"])
        finally:
            sc.close()


class TestQuestionIdents(unittest.TestCase):
    """0.3.4: the pieces carrying the identifiers of the current question are removed last (ALB w16k, case 0031).

    The defect: with value pz the duplication rule discounted a result whose identifiers had already appeared in an
    older result (x0.25), so the receipts the question is about looked like receipts with nothing useful in them and
    were the first ones thrown away - with the guard on the model answered "no" to a question whose answer was yes.
    """
    W = 16384
    QID = "db-77-001"
    QUESTION = "which port did db-77-001 use? use recall if needed"

    def setUp(self):
        import tempfile as _t
        from types import SimpleNamespace
        self.tmp = _t.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        from ctxproxy.core import LEGACY_RECALL_NAME
        cfg = Config.from_dict({"window": self.W})
        self.jpath = os.path.join(self.tmp.name, "j.jsonl")
        self.px = Proxy(cfg, Upstream("http://127.0.0.1:9"), Store(os.path.join(self.tmp.name, "a.sqlite")),
                        Journal(self.jpath), TokenCounter(3.5))
        self.addCleanup(self.px.store.db.close)
        rtool = {"type": "function", "function": {"name": RECALL_NAME, "parameters": {"type": "object"}}}
        self.p = SimpleNamespace(conv="c", tools=TOOLS + [rtool], recall_name=RECALL_NAME, lang="en", catalog=None,
                                 recall_aliases=frozenset((RECALL_NAME, LEGACY_RECALL_NAME)))
        self.body = {"max_tokens": 2048}
        self.floor = self.px._answer_floor(self.body)

    def asked(self, tokens: int) -> str:
        """A recall result the question is about (contains db-77-001)."""
        line = "host %s port 31010 status ok latency 12 ms\n" % self.QID
        return (line * (int(tokens * 3.5) // len(line) + 1))[:int(tokens * 3.5)]

    def plain(self, tokens: int) -> str:
        """A recall result with no identifier of the question."""
        line = "plain filler words only here and there\n"
        return (line * (int(tokens * 3.5) // len(line) + 1))[:int(tokens * 3.5)]

    def loop(self, *texts):
        out = []
        for k, t in enumerate(texts):
            out += [{"role": "assistant", "content": "", "reasoning_content": "looking at svc %d\nso: next" % k,
                     "tool_calls": [{"id": "c%d" % k, "type": "function",
                                     "function": {"name": RECALL_NAME, "arguments": '{"query": "svc-%d"}' % k}}]},
                    {"role": "tool", "tool_call_id": "c%d" % k, "content": t}]
        return out

    def size_p0(self, loop, target: int):
        """phys0 whose window count (with the loop) is exactly `target` tokens: bisection on the filler length."""
        base = [{"role": "system", "content": "s"}, {"role": "user", "content": ""}]

        def at(n):
            base[1]["content"] = "x" * n
            return self.px._wcount(base + window.clean(loop), self.p.tools, self.body)

        lo, hi = 0, 8
        while at(hi) < target:
            hi *= 2
        while lo < hi:
            mid = (lo + hi) // 2
            if at(mid) < target:
                lo = mid + 1
            else:
                hi = mid
        self.assertGreater(lo, 0, "P0 already over the target: the scenario is not usable")
        base[1]["content"] = "x" * lo
        return base

    def freed(self, text, kind="result"):
        return window.freed({"kind": kind, "text": text}, self.px.tc.count, RECALL_NAME, "en")

    def events(self, action=None):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath) as f:
            ev = [json.loads(l) for l in f]
        return [e for e in ev if e["event"] == "window_guard" and (action is None or e.get("action") == action)]

    def test_receipts_the_question_is_about_are_removed_last(self):
        """Room is short by one removal: the piece removed is the one WITHOUT the question's identifiers, even
        though the result with them looks duplicated (its identifiers also appear in an older result)."""
        a0, a, b = self.asked(500), self.asked(3000), self.plain(800)
        loop = self.loop(a0, a, b)
        S = window.CALL_ALLOWANCE + window.RESULT_MIN + self.floor
        M = self.px.wguard.margin
        free_b = self.freed(b)
        phys0 = self.size_p0(loop, self.W - S - M + free_b // 2)
        # sensitivity: by value per freed token (the old key) the duplicated piece i=3 is the first to go, i.e. the
        # old rule would have removed exactly the receipt the question is about
        order = window.removal_order(loop, self.px.tc.count, self.QUESTION, "value")
        dup = [p for p in order if p["i"] == 3][0]
        plain_pc = [p for p in order if p["i"] == 5][0]
        self.assertLess(dup["value"] / dup["tokens"], plain_pc["value"] / plain_pc["tokens"])
        self.assertEqual(order[0]["i"], 5, "the piece without the question's identifiers is removed first")
        final, tools = self.px._plan_round(self.p, phys0, loop, self.body, 1, self.floor, self.QUESTION)
        self.assertFalse(final)
        self.assertIn("result removed from the context for space", loop[5]["content"])   # b -> receipt
        self.assertEqual(loop[1]["content"], a0)                                         # untouched
        self.assertEqual(loop[3]["content"], a)                                          # untouched: kept
        P = self.px._wcount(phys0 + window.clean(loop), tools, self.body)
        self.assertLessEqual(P + S + M, self.W, "INV-3 after removing a piece without the question's identifiers")
        ev = self.events("remove")
        self.assertTrue(ev)
        self.assertEqual(ev[-1]["protected"], [], "nothing carrying the question's identifiers was removed")
        self.assertEqual(ev[-1]["removed"], [["result", 2, self.px.tc.count(b)]])

    def test_a_piece_with_the_question_idents_is_removed_when_it_is_the_only_one(self):
        """Window invariant first: if the only piece left carries the question's identifiers it is removed too,
        and the journal says so (protected)."""
        a = self.asked(3000)
        loop = self.loop(a)
        S = window.CALL_ALLOWANCE + window.RESULT_MIN + self.floor
        M = self.px.wguard.margin
        free_a = self.freed(a)
        phys0 = self.size_p0(loop, self.W - S - M + free_a // 2)
        final, tools = self.px._plan_round(self.p, phys0, loop, self.body, 1, self.floor, self.QUESTION)
        self.assertFalse(final)
        self.assertIn("result removed from the context for space", loop[1]["content"])
        P = self.px._wcount(phys0 + window.clean(loop), tools, self.body)
        self.assertLessEqual(P + S + M, self.W, "the request must never go over the window")
        ev = self.events("remove")
        self.assertTrue(ev)
        self.assertEqual(ev[-1]["protected"], [["result", 0, self.px.tc.count(a)]], "the journal must record it")
        self.assertIn(["result", 0, self.px.tc.count(a)], ev[-1]["removed"])


class FakeMgr:
    def __init__(self):
        self.tc = TokenCounter(3.5)

    def piece_tokens(self, t):
        return self.tc.raw(t)

    def estimate(self, msgs, tools):
        n = sum(self.tc.count(json.dumps(m)) for m in msgs) + (self.tc.count(json.dumps(tools)) if tools else 0)
        return n, []


class TestEnforce(unittest.TestCase):
    def guard(self, w=1000, **kw):
        cfg = Config.from_dict({"window": w, "window_margin": 10, **kw})
        return window.WindowGuard(cfg, FakeMgr(), Journal(None))

    def test_clamp_refuse_and_explicit_max_tokens(self):
        g = self.guard()
        msgs = [{"role": "user", "content": "x" * 700}]          # ~200 tokens
        p = g.count(msgs, None)
        out = g.enforce({"messages": msgs, "max_tokens": 5000})
        self.assertEqual(out["max_tokens"], 1000 - 10 - p)
        out = g.enforce({"messages": msgs})                      # no max_tokens: made explicit
        self.assertEqual(out["max_tokens"], 1000 - 10 - p)
        out = g.enforce({"messages": msgs, "max_tokens": 7})
        self.assertEqual(out["max_tokens"], 7)
        with self.assertRaises(window.WindowExceeded) as cm:
            g.enforce({"messages": [{"role": "user", "content": "x" * 4000}], "max_tokens": 10})
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(g.refused, 1)

    def test_inv2_budget_cap(self):
        g = self.guard(w=100000, answer_min_tokens=512)
        out = g.enforce({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2000,
                         "reasoning_budget_tokens": 4000})
        self.assertEqual(out["reasoning_budget_tokens"], 2000 - 512)
        out = g.enforce({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 300,
                         "reasoning_budget_tokens": 4000})
        self.assertEqual(out["reasoning_budget_tokens"], 150)
        out = g.enforce({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2000,
                         "reasoning_budget_tokens": 100})
        self.assertEqual(out["reasoning_budget_tokens"], 100)

    def test_guard_off_is_identity(self):
        g = self.guard(window_guard=False)
        b = {"messages": [{"role": "user", "content": "x" * 9000}], "max_tokens": 99999}
        self.assertIs(g.enforce(b), b)

    def test_fit_text_is_strict(self):
        g = self.guard()
        t = "abc def ghi " * 500
        for cap in (0, 5, 20, 100, 400):
            out = g.fit_text(t, cap, "\n[...cut for space]")
            self.assertLessEqual(g.mgr.tc.count(out), cap)


class TestRemoval(unittest.TestCase):
    def loop(self):
        def rnd(k, reason, result):
            return [{"role": "assistant", "content": "", "reasoning_content": reason,
                     "tool_calls": [{"id": "c%d" % k, "type": "function",
                                     "function": {"name": "recall", "arguments": "{\"query\": \"q%d\"}" % k}}]},
                    {"role": "tool", "tool_call_id": "c%d" % k, "content": result}]
        return (rnd(0, "think " * 300 + "\nso: check db-7", "port 31010 host db-7-001\n" * 40)
                + rnd(1, "more " * 100, "unrelated text without ids " * 60)
                + rnd(2, "", "port 31010 host db-7-001\n" * 40))

    def test_deterministic_and_pure(self):
        tc = TokenCounter(3.5)
        lp = self.loop()
        snap = json.dumps(lp)
        a = window.removal_order(lp, tc.count, "which port for db-7-001?", "value")
        b = window.removal_order(self.loop(), tc.count, "which port for db-7-001?", "value")
        self.assertEqual([(p["i"], p["kind"]) for p in a], [(p["i"], p["kind"]) for p in b])
        self.assertEqual(json.dumps(lp), snap)

    def test_value_keeps_relevant_result(self):
        tc = TokenCounter(3.5)
        order = window.removal_order(self.loop(), tc.count, "which port for db-7-001?", "value")
        results = [p for p in order if p["kind"] == "result"]
        # 0.3.4: the results carrying the question's identifiers are removed last (the question is about them);
        # the result without them (round 1) goes first - the duplication rule no longer discounts them
        self.assertEqual(results[0]["round"], 1)
        self.assertFalse(results[0]["protected"])
        self.assertEqual([p["round"] for p in results[-2:]], [0, 2])
        self.assertTrue(all(p["protected"] for p in results[-2:]))

    def test_question_idents_are_removed_last_whatever_the_policy(self):
        """Only one piece frees the room and it carries the identifiers of the question: it is removed last."""
        tc = TokenCounter(3.5)
        q = "which port for db-7-001?"
        for pol in window.POLICIES:
            order = window.removal_order(self.loop(), tc.count, q, pol)
            self.assertTrue(order[-1]["protected"], (pol, [(p["kind"], p["round"], p["protected"]) for p in order]))
            asked = [p for p in order if p["protected"]]
            self.assertEqual(len(asked), 2)                      # rounds 0 and 2 carry db-7-001
            self.assertEqual([p["round"] for p in asked], [0, 2])
            self.assertTrue(all(not p["protected"] for p in order[:len(order) - len(asked)]))

    def test_policies(self):
        tc = TokenCounter(3.5)
        age = window.removal_order(self.loop(), tc.count, "", "age")
        self.assertEqual([p["round"] for p in age], sorted(p["round"] for p in age))
        rs = window.removal_order(self.loop(), tc.count, "", "reasoning")
        kinds = [p["kind"] for p in rs]
        self.assertEqual(kinds, sorted(kinds, key=lambda k: 0 if k == "reasoning" else 1))

    def test_remove_piece_receipt_and_conclusion(self):
        tc = TokenCounter(3.5)
        lp = self.loop()
        for pc in window.removal_order(lp, tc.count, "", "age"):
            window.remove_piece(lp, pc, "recall", "en", tc.count)
        self.assertEqual(window.pieces(lp), [])
        self.assertEqual(lp[0]["reasoning_content"], "so: check db-7")
        self.assertIn("result removed from the context for space", lp[1]["content"])
        self.assertIn("q0", lp[1]["content"])
        self.assertTrue(all(not any(k in m for k in window.PRIVATE) for m in window.clean(lp)))


class TestNotesSource(unittest.TestCase):
    def test_shortened_from_the_end_without_orphans(self):
        cfg = Config.from_dict({"window": 3000, "window_margin": 10})
        g = window.WindowGuard(cfg, FakeMgr(), Journal(None))
        phys = [{"role": "system", "content": "s"}]
        for k in range(8):
            phys += [{"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % k, "type": "function",
                                                                          "function": {"name": "b", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": "c%d" % k, "content": "y" * 1400}]
        instr = {"role": "user", "content": "write notes"}
        src = window.notes_source(g, phys, None, instr, {}, 500)
        self.assertLess(len(src), len(phys))
        self.assertEqual(src, phys[:len(src)])
        self.assertGreaterEqual(g.room(g.count(src + [instr], None)), 500)
        self.assertNotIn("tool_calls", src[-1])          # no unanswered call at the end
        self.assertNotEqual(src[-1].get("role"), "assistant") if src[-1].get("tool_calls") else None


if __name__ == "__main__":
    unittest.main()
