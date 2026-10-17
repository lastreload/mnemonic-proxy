# Author: Maurizio Verde — LastReload
"""0.3.5: no empty answer when the model calls `recall` anyway in the final round.

In the final round (search closed for space or for the round limit) the proxy does not offer its tools, but the model
has seen `recall` used in the conversation and may call it anyway. Before, the call was dropped and the client got an
EMPTY answer (finish_reason=stop; ALB pilot t_caa8225d, 13_i3_b and 49_i7_b). Now: one new final attempt, with the
calls answered by the closing notice (nothing searched), if it fits with F.

0.3.6: Flash Next called recall again in that attempt too (AgentLongBench confirmation run at 64K, 14_r11_a_0175_neg
and 18_r7_a_0639: reasoning up to the budget, then recall). If the new attempt returns only internal calls, a LAST
one with the reasoning off (chat_template_kwargs.enable_thinking=false, no thinking budget) and the explicit
instruction to answer now in the requested format; never more than two new attempts. If that too calls only
internal tools, the answer stays empty (nothing invented).

    CTX_TOKDIR=<pack tokenizer> python3 -m unittest -v tests.test_final_recall_retry
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MNEMONIC_PROXY_ALLOW_SMALL_WINDOW", "1")

from ctxproxy import window  # noqa: E402
from ctxproxy.core import RECALL_NAME  # noqa: E402
from ctxproxy.render import template_kwargs  # noqa: E402
from tests.test_proxy import TOOLS  # noqa: E402
from tests import test_search_keeps_evidence as tse  # noqa: E402
from tests.test_search_keeps_evidence import RECEIPT, Session, assert_pairs, seen_then_lost  # noqa: E402

LIMIT_EN = "search limit reached for this answer"
ANSWER_NOW_EN = "answer the question now, in the requested format"
SPACE_EN = "no room left for more searches"


def names_of(body) -> set:
    return {((t or {}).get("function") or {}).get("name") for t in body.get("tools") or []}


def thinking_off(body) -> bool:
    return template_kwargs(body).get("enable_thinking") is False


class Stubborn(Session):
    """The fake model calls `recall` in the final round even though it is not offered, `stubborn` times; `extra_call`
    adds a client call to that round (mixed), `content` gives it a visible text."""

    def __init__(self, stubborn: int = 1, extra_call: bool = False, content: str = "", answer_if_no_think=False,
                 **kw):
        self.stubborn, self.extra_call, self.content = stubborn, extra_call, content
        self.answer_if_no_think = answer_if_no_think        # 0.3.6: recalls while thinking, answers with it off
        self.final_calls = 0
        self.final_bodies = []
        self.chunks = []
        super().__init__(**kw)

    def policy(self, body):
        if self.k is not None and RECALL_NAME not in names_of(body):
            self.final_bodies.append(body)
            if self.final_calls < self.stubborn and not (self.answer_if_no_think and thinking_off(body)):
                self.final_calls += 1
                calls = [{"id": "fx%d" % self.final_calls, "type": "function", "function": {
                    "name": RECALL_NAME, "arguments": json.dumps({"query": "db-s3-007 port"})}}]
                if self.extra_call:
                    calls.append({"id": "cb%d" % self.final_calls, "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({"command": "ls"})}})
                return {"role": "assistant", "content": self.content,
                        "reasoning_content": ("I still want to check. " * 400) + "\nso: one more recall",
                        "tool_calls": calls}
        return super().policy(body)

    def send(self, h):
        if not self.stream:
            return super().send(h)
        self.chunks = []
        req = {"model": "m", "messages": h, "tools": TOOLS, "max_tokens": self.mt}
        st, r, _ = self.proxy.chat(req, emit=lambda c: self.chunks.append(c))
        return st, r


def by_round(n):
    return lambda body: [{"args": {"query": "db-s%d-007" % n}}]


def space_session(**kw):
    """16K, F = 4096, ~2.5K tokens of reasoning per round: the search closes for space."""
    return Stubborn(script=[by_round(3), by_round(9), by_round(5), by_round(7)], **kw)


def limit_session(**kw):
    """32K, max_recall_rounds = 1: the search closes at the round limit."""
    return Stubborn(script=[[{"args": {"query": "db-s3-007"}}]] * 5, extra={"max_recall_rounds": 1},
                    reasoning_chars=600, window_=32768, **kw)


class TestFinalRecallRetry(unittest.TestCase):
    def check_window(self, s):
        self.assertEqual(s.eng.violations, [], "requests sent over the window")
        self.assertEqual(s.proxy.wguard.violations, 0)
        self.assertEqual(s.proxy.wguard.count_errors, 0)
        for b in s.turn_requests():
            self.assertLessEqual(s.eng.counter(b) + b["max_tokens"] + s.proxy.wguard.margin, s.W)
            assert_pairs(self, b["messages"])

    def retried(self, s, reason, notice):
        res = s.run()
        st, r = res[-1]
        self.assertEqual(st, 200)
        self.check_window(s)
        msg = r["choices"][0]["message"]
        self.assertEqual(msg.get("content"), "answer: yes", "the second attempt answers")
        self.assertNotIn("tool_calls", msg)
        self.assertEqual(r["choices"][0]["finish_reason"], "stop")
        ev = s.events("final_recall_retry")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["reason"], ev[0]["used"]), (reason, True))
        self.assertGreater(ev[0]["prompt"], ev[0]["prompt_before"])
        self.assertEqual(s.events("recall_dropped"), [], "nothing dropped: the calls got the notice")
        self.assertEqual(len(s.final_bodies), 2, "exactly one new attempt")
        retry = s.final_bodies[-1]
        self.assertNotIn(RECALL_NAME, names_of(retry), "the new attempt is a final call: client tools only")
        self.assertGreaterEqual(retry["max_tokens"], 4096, "the new attempt gets F")
        msgs = retry["messages"]
        a = next(m for m in msgs if m.get("role") == "assistant" and any(c["id"] == "fx1" for c in m.get(
            "tool_calls") or []))
        self.assertEqual(a.get("reasoning_content"), "so: one more recall", "reasoning reduced to its conclusion")
        t = next(m for m in msgs if m.get("role") == "tool" and m.get("tool_call_id") == "fx1")
        self.assertIn(notice, t["content"])
        self.assertFalse(any(k.startswith("_") for m in msgs for k in m), "private markers never reach the engine")
        self.assertEqual(seen_then_lost(s.turn_requests()), [], "no evidence lost to pay for the attempt")
        self.assertEqual(len(s.events("recall")), len([m for m in msgs if m.get("role") == "tool" and str(
            m.get("tool_call_id", "")).startswith("rc")]), "nothing searched in the attempt")
        return r

    def test_a_space(self):
        s = space_session()
        try:
            self.retried(s, "space", SPACE_EN)
            fin = s.events("window_guard", "final")
            self.assertEqual(fin[0]["reason"], "space")
        finally:
            s.close()

    def test_b_round_limit(self):
        s = limit_session()
        try:
            self.retried(s, "round_limit", LIMIT_EN)
            self.assertEqual(len(s.events("recall")), 1)
        finally:
            s.close()

    def test_c_recall_always_gives_empty_after_two_attempts(self):
        """0.3.6: the model calls recall in every final call: two new attempts (the second with the reasoning off),
        then the answer stays empty - nothing invented, never a third attempt."""
        for mk in (space_session, limit_session):
            for stream in (False, True):
                with self.subTest(case=mk.__name__, stream=stream):
                    s = mk(stubborn=50, stream=stream)
                    try:
                        res = s.run()
                        st, r = res[-1]
                        self.assertEqual(st, 200)
                        self.check_window(s)
                        msg = r["choices"][0]["message"]
                        self.assertFalse((msg.get("content") or "").strip())
                        self.assertNotIn("tool_calls", msg)
                        self.assertEqual(r["choices"][0]["finish_reason"], "stop")
                        ev = s.events("final_recall_retry")
                        self.assertEqual([(e["attempt"], e["used"], e["thinking_off"]) for e in ev],
                                         [(1, True, False), (2, True, True)])
                        self.assertEqual(len(s.final_bodies), 3, "never more than two new attempts")
                        self.assertTrue(thinking_off(s.final_bodies[-1]))
                        dr = s.events("recall_dropped")
                        self.assertEqual(len(dr), 1)
                        self.assertTrue(dr[0]["final"] and dr[0]["retry"])
                        self.assertEqual((dr[0]["attempts"], dr[0]["thinking_off"]), (2, True))
                        self.assertEqual(seen_then_lost(s.turn_requests()), [])
                    finally:
                        s.close()

    def last_attempt(self, s, reason, reasoning=True):
        """The 0.3.6 attempt: the third final call, reasoning off, the calls of attempt 1 answered by ANSWER_NOW."""
        res = s.run()
        st, r = res[-1]
        self.assertEqual(st, 200)
        self.check_window(s)
        msg = r["choices"][0]["message"]
        self.assertEqual(msg.get("content"), "answer: yes", "the last attempt answers")
        self.assertNotIn("tool_calls", msg)
        self.assertEqual(r["choices"][0]["finish_reason"], "stop")
        ev = s.events("final_recall_retry")
        self.assertEqual([(e["attempt"], e["used"], e["thinking_off"], e["reason"]) for e in ev],
                         [(1, True, False, reason), (2, True, True, reason)])
        self.assertGreater(ev[1]["prompt"], ev[1]["prompt_before"])
        self.assertEqual(s.events("recall_dropped"), [])
        self.assertEqual(len(s.final_bodies), 3)
        first, second, last = s.final_bodies
        self.assertFalse(thinking_off(first) or thinking_off(second), "attempt 1 keeps the client's reasoning")
        self.assertTrue(thinking_off(last), "the last attempt goes with the reasoning off")
        self.assertIs(last["chat_template_kwargs"]["enable_thinking"], False)
        self.assertNotIn("reasoning_budget_tokens", last)
        self.assertNotIn(RECALL_NAME, names_of(last))
        self.assertGreaterEqual(last["max_tokens"], 4096, "the last attempt gets F")
        msgs = last["messages"]
        t1 = next(m for m in msgs if m.get("role") == "tool" and m.get("tool_call_id") == "fx1")
        t2 = next(m for m in msgs if m.get("role") == "tool" and m.get("tool_call_id") == "fx2")
        self.assertIn(LIMIT_EN if reason == "round_limit" else SPACE_EN, t1["content"])
        self.assertIn(ANSWER_NOW_EN, t2["content"], "explicit instruction: answer now, in the requested format")
        self.assertIn("Do not call any tool", t2["content"])
        a2 = next(m for m in msgs if m.get("role") == "assistant" and any(c["id"] == "fx2" for c in m.get(
            "tool_calls") or []))
        # with answer T on the reasoning of a call made after T's thinking is stripped (answer_thinking_strip)
        self.assertEqual(a2.get("reasoning_content"), "so: one more recall" if reasoning else None)
        self.assertFalse(any(k.startswith("_") for m in msgs for k in m), "private markers never reach the engine")
        self.assertEqual(seen_then_lost(s.turn_requests()), [], "no evidence lost to pay for the attempts")
        self.assertEqual(len(s.events("recall")), len([m for m in msgs if m.get("role") == "tool" and str(
            m.get("tool_call_id", "")).startswith("rc")]), "nothing searched in the attempts")
        return r

    def test_d_recalls_twice_answers_at_the_last_attempt(self):
        """0.3.6: recall in the final call and in attempt 1, an answer in attempt 2 (reasoning off)."""
        for mk, reason in ((space_session, "space"), (limit_session, "round_limit")):
            with self.subTest(case=mk.__name__):
                s = mk(stubborn=2)
                try:
                    self.last_attempt(s, reason)
                finally:
                    s.close()

    def test_d_answers_only_with_the_reasoning_off(self):
        """0.3.6: the model of the confirmation run - with the reasoning on it always calls recall; with it off it
        answers. Answer T (thinking after a recall) is on: it must not turn the reasoning back on."""
        for mk, reason in ((space_session, "space"), (limit_session, "round_limit")):
            with self.subTest(case=mk.__name__):
                s = mk(stubborn=50, answer_if_no_think=True)
                s.proxy.cfg.answer_thinking_after_recall = True
                s.proxy.cfg.answer_thinking_budget = 512
                try:
                    self.last_attempt(s, reason, reasoning=False)
                    self.assertFalse(thinking_off(s.final_bodies[0]) or thinking_off(s.final_bodies[1]))
                finally:
                    s.close()

    def test_d_streaming_last_attempt(self):
        for mk, reason in ((space_session, "space"), (limit_session, "round_limit")):
            with self.subTest(case=mk.__name__):
                s = mk(stubborn=2, stream=True)
                try:
                    self.last_attempt(s, reason)
                    deltas = [c["choices"][0]["delta"] for c in s.chunks if c and c.get("choices")]
                    self.assertFalse(any(d.get("tool_calls") for d in deltas), "internal calls never reach the client")
                    self.assertEqual("".join(d.get("content") or "" for d in deltas), "answer: yes")
                    reasoning = "".join(d.get("reasoning_content") or "" for d in deltas)
                    self.assertIn("new answer attempt", reasoning)
                    self.assertIn("last answer attempt, reasoning off", reasoning)
                    self.assertEqual(s.chunks[-1]["choices"][0]["finish_reason"], "stop")
                finally:
                    s.close()

    def test_e_streaming(self):
        for mk in (space_session, limit_session):
            with self.subTest(case=mk.__name__):
                s = mk(stream=True)
                try:
                    self.retried(s, "space" if mk is space_session else "round_limit",
                                 SPACE_EN if mk is space_session else LIMIT_EN)
                    deltas = [c["choices"][0]["delta"] for c in s.chunks if c and c.get("choices")]
                    self.assertFalse(any(d.get("tool_calls") for d in deltas), "internal calls never reach the client")
                    self.assertEqual("".join(d.get("content") or "" for d in deltas), "answer: yes")
                    reasoning = "".join(d.get("reasoning_content") or "" for d in deltas)
                    self.assertIn("new answer attempt", reasoning)
                    self.assertEqual(s.chunks[-1]["choices"][0]["finish_reason"], "stop")
                finally:
                    s.close()

    def test_e_streaming_recall_again(self):
        s = space_session(stream=True, stubborn=5)
        try:
            res = s.run()
            self.check_window(s)
            deltas = [c["choices"][0]["delta"] for c in s.chunks if c and c.get("choices")]
            self.assertFalse(any(d.get("tool_calls") for d in deltas))
            self.assertEqual("".join(d.get("content") or "" for d in deltas), "")
            self.assertEqual(len(s.final_bodies), 3)
            self.assertEqual(res[-1][1]["choices"][0]["finish_reason"], "stop")
        finally:
            s.close()

    def test_f_mixed_calls_unchanged(self):
        """A client call in the same final round: the client gets its call, the recall is dropped, no attempt."""
        for stream in (False, True):
            with self.subTest(stream=stream):
                s = space_session(extra_call=True, stream=stream)
                try:
                    res = s.run()
                    st, r = res[-1]
                    self.check_window(s)
                    calls = r["choices"][0]["message"].get("tool_calls") or []
                    self.assertEqual([c["function"]["name"] for c in calls], ["bash"])
                    self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")
                    self.assertEqual(s.events("final_recall_retry"), [])
                    self.assertEqual(len(s.final_bodies), 1)
                    dr = s.events("recall_dropped")
                    self.assertTrue(dr and dr[0]["mixed"])
                finally:
                    s.close()

    def test_f_visible_content_unchanged(self):
        """The final round wrote an answer and also called recall: the answer goes out as before, no attempt."""
        s = space_session(content="port 30007")
        try:
            res = s.run()
            self.check_window(s)
            self.assertEqual(res[-1][1]["choices"][0]["message"]["content"], "port 30007")
            self.assertEqual(s.events("final_recall_retry"), [])
            self.assertEqual(len(s.final_bodies), 1)
        finally:
            s.close()

    def test_normal_final_unchanged(self):
        """The final round answers normally: no event, same requests as before."""
        s = space_session(stubborn=0)
        try:
            res = s.run()
            self.check_window(s)
            self.assertEqual(res[-1][1]["choices"][0]["message"]["content"], "answer: yes")
            self.assertEqual(s.events("final_recall_retry"), [])
            self.assertEqual(len(s.final_bodies), 1)
        finally:
            s.close()


class TestRetryAtTheLimit(unittest.TestCase):
    """(d) Space at the limit: the attempt fits (paying only with verified copies / finished reasoning) or it is not
    made and nothing is removed; evidence is never removed to pay for it."""
    W = tse.Unit.W
    filler = tse.Unit.filler          # the helpers of the unit tests of correction 2 (not their tests)

    def setUp(self):
        tse.Unit.setUp(self)
        self.p.tools = TOOLS                                  # the final tools: the client's only
        self.proof = "host db-77-001 port 31010 status ok\n" * 40

    def loop(self, with_copy: bool):
        out = []
        texts = (self.proof, self.proof) if with_copy else (self.proof, "host db-88-002 port 31020 status ok\n" * 40)
        for k, t in enumerate(texts):
            out += [{"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % k, "type": "function",
                                                                          "function": {"name": RECALL_NAME,
                                                                                       "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c%d" % k, "content": t, "_src": []}]
        return out

    def attempt(self, slack: int, with_copy: bool, floor=None, n: int = 1):
        floor = self.floor if floor is None else floor
        body = self.px._no_think_body(self.body) if n == 2 else self.body
        M = self.px.wguard.margin
        loop = self.loop(with_copy)
        phys0 = self.filler(self.W - M - floor - slack, loop)
        snap = [dict(m) for m in loop]
        st = window.SearchState()
        st.final_reason = "space"
        msg = {"role": "assistant", "content": "", "reasoning_content": "thinking. " * 300 + "\nso: recall again"}
        mine = [{"id": "fx", "type": "function", "function": {"name": RECALL_NAME, "arguments": "{\"query\": \"x\"}"}}]
        ok = self.px._final_retry(self.p, phys0, loop, body, 3, floor, "which port has db-77-001?", st, msg,
                                  mine, TOOLS, attempt=n, ref_body=self.body)
        return ok, loop, snap, phys0, st

    def last_event(self):
        with open(self.jpath) as f:
            return [json.loads(l) for l in f if '"final_recall_retry"' in l][-1]

    def test_fits_with_room(self):
        ok, loop, snap, phys0, st = self.attempt(500, with_copy=False)
        self.assertTrue(ok)
        self.assertEqual(loop[:4], snap, "nothing removed when it fits")
        self.assertTrue(st.force_final and st.reason == "space")
        self.assertLessEqual(self.px._wcount(phys0 + window.clean(loop), TOOLS, self.body) + self.floor +
                             self.px.wguard.margin, self.W)

    def test_pays_with_a_copy_never_with_evidence(self):
        ok, loop, snap, phys0, st = self.attempt(5, with_copy=True)
        self.assertTrue(ok)
        self.assertEqual(loop[1]["content"], self.proof, "the evidence stays whole")
        self.assertTrue(loop[3].get("_receipt") and RECEIPT in loop[3]["content"], "the verified copy pays")
        self.assertEqual(st.evidence_lost, [])
        self.assertLessEqual(self.px._wcount(phys0 + window.clean(loop), TOOLS, self.body) + self.floor +
                             self.px.wguard.margin, self.W)
        ev = self.last_event()
        self.assertTrue(ev["used"])
        self.assertEqual([r[3] for r in ev["removed"]], [0])

    def test_gives_up_without_removing_evidence(self):
        ok, loop, snap, phys0, st = self.attempt(5, with_copy=False)
        self.assertFalse(ok)
        self.assertEqual(loop, snap, "loop restored: nothing removed for an attempt that is not made")
        self.assertFalse(st.force_final)
        ev = self.last_event()
        self.assertFalse(ev["used"])
        self.assertEqual(ev["prompt"], ev["prompt_before"])

    def test_floor_short_keeps_the_room_of_the_first_attempt(self):
        """P0 + F + margin > W (floor_short): the attempt must leave at least the room the first one had."""
        ok, loop, snap, phys0, st = self.attempt(-1000, with_copy=True)
        before = self.W - self.px.wguard.margin - self.floor + 1000
        after = self.px._wcount(phys0 + window.clean(loop), TOOLS, self.body)
        self.assertTrue(ok)
        self.assertLessEqual(after, before)
        self.assertEqual(loop[1]["content"], self.proof)
        ok2, loop2, snap2, _, _ = self.attempt(-1000, with_copy=False)
        self.assertFalse(ok2)
        self.assertEqual(loop2, snap2)


class TestLastAttemptAtTheLimit(TestRetryAtTheLimit):
    """0.3.6 (d): the last attempt (reasoning off) at the limit - the same check P + F + margin <= W, counted with
    the template the engine renders with the reasoning off; paid only with verified copies / finished reasoning, or
    not made (nothing removed)."""

    def no_think_count(self, phys0, loop):
        return self.px._wcount(phys0 + window.clean(loop), TOOLS, self.px._no_think_body(self.body))

    def test_fits_with_room(self):
        ok, loop, snap, phys0, st = self.attempt(500, with_copy=False, n=2)
        self.assertTrue(ok)
        self.assertEqual(loop[:4], snap)
        self.assertLessEqual(self.no_think_count(phys0, loop) + self.floor + self.px.wguard.margin, self.W)
        ev = self.last_event()
        self.assertEqual((ev["attempt"], ev["thinking_off"]), (2, True))
        self.assertIn(ANSWER_NOW_EN, loop[-1]["content"])

    def test_pays_with_a_copy_never_with_evidence(self):
        ok, loop, snap, phys0, st = self.attempt(5, with_copy=True, n=2)
        self.assertTrue(ok)
        self.assertEqual(loop[1]["content"], self.proof)
        self.assertEqual(st.evidence_lost, [])
        self.assertLessEqual(self.no_think_count(phys0, loop) + self.floor + self.px.wguard.margin, self.W)

    def delta(self):
        """Tokens the reasoning-off template adds to the same prompt (Qwen: the empty think block)."""
        loop = self.loop(False)
        phys0 = self.filler(4000, loop)
        return self.no_think_count(phys0, loop) - self.px._wcount(phys0 + window.clean(loop), TOOLS, self.body)

    def test_bare_when_the_calls_do_not_fit(self):
        """No room for the calls + ANSWER_NOW and nothing removable: the last attempt goes bare - the calls leave,
        the evidence stays whole, the answer gets F with the reasoning-off template."""
        d = self.delta()
        ok, loop, snap, phys0, st = self.attempt(max(5, d), with_copy=False, n=2)
        self.assertTrue(ok)
        self.assertEqual(loop, snap, "bare: nothing added, nothing removed (ANSWER_NOW does not fit either)")
        self.assertLessEqual(self.no_think_count(phys0, loop) + self.floor + self.px.wguard.margin, self.W)
        self.assertEqual(st.evidence_lost, [])
        self.assertTrue(st.force_final)
        ev = self.last_event()
        self.assertEqual((ev["attempt"], ev["used"], ev["thinking_off"], ev["bare"]), (2, True, True, True))
        self.assertEqual(ev["need"], self.floor)

    def test_bare_with_room_for_the_instruction(self):
        """The calls cost more than the room, ANSWER_NOW alone fits: appended to the last result (its text kept)."""
        # the slack between the cost of ANSWER_NOW appended to the last result and the cost of the calls + it,
        # measured with the proxy's own counter (exact or estimated) and the reasoning-off template
        from ctxproxy.server import ANSWER_NOW_EN as text
        text = text.replace("{rn}", RECALL_NAME)
        loop = self.loop(False)
        phys0 = self.filler(4000, loop)
        base = self.no_think_count(phys0, loop)
        bare = loop[:-1] + [{**loop[-1], "content": loop[-1]["content"] + "\n[" + text + "]"}]
        full = loop + [{"role": "assistant", "content": "", "reasoning_content": "so: recall again", "tool_calls": [
            {"id": "fx", "type": "function", "function": {"name": RECALL_NAME, "arguments": "{\"query\": \"x\"}"}}]},
            {"role": "tool", "tool_call_id": "fx", "content": text}]
        c_bare, c_full = self.no_think_count(phys0, bare) - base, self.no_think_count(phys0, full) - base
        self.assertLess(c_bare, c_full)
        n = (c_bare + c_full) // 2 + self.delta()        # room left = n - delta
        ok, loop, snap, phys0, st = self.attempt(n, with_copy=False, n=2)
        self.assertTrue(ok)
        ev = self.last_event()
        self.assertTrue(ev["bare"])
        if ev["bare"]:
            self.assertTrue(loop[-1]["content"].startswith(snap[-1]["content"]))
            self.assertIn(ANSWER_NOW_EN, loop[-1]["content"])
            self.assertEqual(len(loop), len(snap))
        else:                                   # the calls + ANSWER_NOW fitted after all
            self.assertIn(ANSWER_NOW_EN, loop[-1]["content"])
        self.assertEqual(loop[1]["content"], self.proof)
        self.assertLessEqual(self.no_think_count(phys0, loop) + self.floor + self.px.wguard.margin, self.W)

    def test_gives_up_without_removing_evidence(self):
        """Not even bare: the reasoning-off template alone exceeds the room and nothing removable is left."""
        d = self.delta()
        if d <= 0:
            self.skipTest("the reasoning-off template adds no token with this counter")
        ok, loop, snap, phys0, st = self.attempt(d - 1, with_copy=False, n=2)
        self.assertFalse(ok)
        self.assertEqual(loop, snap)
        self.assertFalse(st.force_final)
        ev = self.last_event()
        self.assertEqual((ev["attempt"], ev["used"], ev["thinking_off"], ev["bare"]), (2, False, True, False))

    def test_floor_short_keeps_the_room_of_the_first_attempt(self):
        ok, loop, snap, phys0, st = self.attempt(-1000, with_copy=True, n=2)
        self.assertTrue(ok)
        self.assertEqual(loop[1]["content"], self.proof)
        before = self.W - self.px.wguard.margin - self.floor + 1000
        self.assertLessEqual(self.no_think_count(phys0, loop), before)
        ok2, loop2, snap2, phys2, _ = self.attempt(-1000, with_copy=False, n=2)
        # bare: the room of the previous call, never less - the reasoning-off template must fit inside it
        self.assertEqual(ok2, self.delta() <= 0)
        self.assertEqual(loop2[1]["content"], self.proof)
        if ok2:
            self.assertLessEqual(self.no_think_count(phys2, loop2), before)
        else:
            self.assertEqual(loop2, snap2)

    def test_reasoning_off_body(self):
        b = self.px._no_think_body({"max_tokens": 4096, "reasoning_budget_tokens": 3072, "reasoning_effort": "high",
                                    "chat_template_kwargs": {"x": 1, "enable_thinking": True}})
        self.assertEqual(b["chat_template_kwargs"], {"x": 1, "enable_thinking": False})
        self.assertNotIn("reasoning_budget_tokens", b)
        self.assertEqual(template_kwargs(b), {"enable_thinking": False}, "wins over reasoning_effort")
        self.assertEqual(b["max_tokens"], 4096)


if __name__ == "__main__":
    unittest.main()
