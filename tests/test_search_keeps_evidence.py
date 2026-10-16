# Author: Maurizio Verde — LastReload
"""0.3.5 (correction 2): a search round never consumes the evidence already acquired.

Regressions from the two reviews of correction 2 (Sol §5, Argon §5.1): verified copies only on certified coverage
(disjoint excerpts of one record, a cut after `meta`, records with identical text, a witness turned receipt, empty
`meta`), the search/final passage (several calls in one round, a generation far above S, a notice that does not fit,
exact boundaries, an underestimated search), answerfix K after the closure, tool definitions, mixed calls,
max_recall_rounds = 0 and equality, streaming, the same identifier in two scopes, a long proof against a short useless
text, the explicit F check, exact counting without silent fallback.

    CTX_TOKDIR=<pack tokenizer> python3 -m unittest -v tests.test_search_keeps_evidence
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MNEMONIC_PROXY_ALLOW_SMALL_WINDOW", "1")

from ctxproxy import window  # noqa: E402
from ctxproxy.core import LEGACY_RECALL_NAME, RECALL_NAME, Config, Journal, Manager, Store, TokenCounter  # noqa: E402
from ctxproxy.render import render_pieces, template_kwargs  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402
from tests.test_proxy import TOOLS  # noqa: E402

TOKDIR = os.environ.get("CTX_TOKDIR", "")
EXACT = bool(TOKDIR) and os.path.exists(os.path.join(TOKDIR, "vocab.json"))
RECEIPT = "result removed from the context for space"


def record(tag: str, n: int) -> str:
    return "".join("%s line %d: host db-%s-%03d port %d status ok\n" % (tag, i, tag, i, 30000 + i) for i in range(n))


# ----------------------------------------------------------------------------------------------- verified copies
class TestVerifiedCopy(unittest.TestCase):
    """What the model received decides a copy: never a guess from `rid` alone."""

    def mgr(self, **over):
        cfg = Config(**over)
        st = Store(":memory:")
        self.addCleanup(st.db.close)
        text = record("x", 400)
        st.archive_many([("u000000000001", "c", 0, "user", "", "show", 3, 0),
                         ("r0123456789ab", "c", 1, "tool", "bash", text, len(text) // 3, 0),
                         ("r0123456789cd", "c", 2, "tool", "bash", text, len(text) // 3, 0)])
        m = Manager(cfg, st, TokenCounter(3.5), Journal(None))
        m.question = "what is on line 7?"
        return m, text

    def result(self, m, args, cut=False):
        meta = []
        out = m.recall("c", args, max_idx=9, meta=meta)
        return {"role": "tool", "tool_call_id": "t%d" % id(out), "content": out,
                "_src": window.sources_from_meta(meta, cut=cut)}, meta

    def test_disjoint_lines_of_one_record_are_not_copies(self):
        """Sol: recall(id, query=alpha) and recall(id, query=omega) return different lines of the same record; with
        a rid-only rule the second was a 'copy'."""
        m, _ = self.mgr()
        a, ma = self.result(m, {"id": "r0123456789ab", "query": "line 7:"})
        b, mb = self.result(m, {"id": "r0123456789ab", "query": "line 390:"})
        self.assertTrue(ma[0]["line_set"] and mb[0]["line_set"])
        self.assertEqual(window.copy_of([a, b]), {})
        # the same lines again: certified copy (line set inside the older one)
        c, _ = self.result(m, {"id": "r0123456789ab", "query": "line 7:"})
        self.assertEqual(window.copy_of([a, b, c]), {2: 0})

    def test_disjoint_leaf_excerpts_are_not_copies(self):
        m, _ = self.mgr(recall_leaf_select=True, recall_max_tokens=300)
        m.question = "which port has db-x-007?"
        a, ma = self.result(m, {"id": "r0123456789ab", "query": "db-x-007"})
        m.question = "which port has db-x-390?"
        b, mb = self.result(m, {"id": "r0123456789ab", "query": "db-x-390"})
        srcs = [s[0] for s in a["_src"] + b["_src"]]
        self.assertTrue(all(k in ("leaf", "lines", "span", "cut") for k in srcs), srcs)
        self.assertEqual(window.copy_of([a, b]), {})

    def test_query_excerpts_have_spans_and_pages_cover_them(self):
        m, text = self.mgr()
        q, mq = self.result(m, {"query": "db-x-007"})
        self.assertTrue(all(isinstance(e.get("offset"), int) for e in mq))
        page, mp = self.result(m, {"id": "r0123456789ab"})                  # whole record (24000 chars max)
        self.assertEqual(mp[0]["offset"], 0)
        # the excerpt of r..ab inside the page: a copy only if ALL its sources are covered (the query also hit r..cd)
        rids = {e["rid"] for e in mq}
        self.assertEqual(window.copy_of([page, q]) != {}, rids == {"r0123456789ab"})
        # a page that was cut afterwards covers nothing
        page_cut = dict(page, _src=window.sources_from_meta(mp, cut=True))
        self.assertEqual(window.copy_of([page_cut, q]), {})

    def test_cut_after_meta_is_never_a_copy(self):
        """Argon 2.2: recall(id) of 3500 chars cut to 1000 by the guard: the next page [1000, ...) is not 'inside'."""
        m, _ = self.mgr()
        a, ma = self.result(m, {"id": "r0123456789ab", "offset": 0}, cut=True)
        b, _ = self.result(m, {"id": "r0123456789ab", "offset": 1000})
        self.assertEqual(a["_src"], [["cut"]])
        self.assertEqual(window.copy_of([a, b]), {})
        self.assertEqual(window.copy_of([b, a]), {})                          # a cut piece is never a copy either

    def test_records_with_identical_text_stay_distinct(self):
        """Two records with the same text are two provenances (ALB: 'appears in round 13 AND round 22')."""
        m, _ = self.mgr()
        a, _ = self.result(m, {"id": "r0123456789ab"})
        b, _ = self.result(m, {"id": "r0123456789cd"})
        self.assertNotEqual(a["content"], b["content"])                     # the header carries the id
        self.assertEqual(window.copy_of([a, b]), {})

    def test_grouped_provenance_is_opaque(self):
        """Recall2.render groups identical excerpts ('same text also in messages ...'): opaque, never a copy."""
        m, _ = self.mgr(recall_struct=True, recall_flex=True, recall_multi=True)
        self.assertEqual(window.sources_from_meta([{"rid": "r1", "offset": 0, "chars": 9, "grouped": True}]),
                         [["opaque"]])
        a, ma = self.result(m, {"query": "db-x-007"})
        # r..ab and r..cd have the same text: Recall2 shows one excerpt with "same text also in messages ..."
        self.assertTrue(any(e.get("grouped") for e in ma), ma)
        self.assertIn(["opaque"], a["_src"])
        b, _ = self.result(m, {"query": "db-x-007 port"})
        if a["content"] != b["content"]:
            self.assertEqual(window.copy_of([a, b]).get(1) is not None,
                             window._covered(b["_src"], [a["_src"]]))
            self.assertFalse(any(s == ["opaque"] for s in b["_src"]) and window.copy_of([a, b]))

    def test_empty_meta_and_no_results_are_not_copies_by_coverage(self):
        self.assertFalse(window._covered([], [[["span", "r", 0, 10]]]))
        a = {"role": "tool", "content": "recall: no results for 'zz'", "_src": []}
        b = {"role": "tool", "content": "recall: no results for 'yy'", "_src": []}
        self.assertEqual(window.copy_of([a, b]), {})
        # identical delivered text: a copy by identity (nothing is lost)
        self.assertEqual(window.copy_of([a, dict(a)]), {1: 0})

    def test_witness_turned_receipt_is_no_witness(self):
        m, _ = self.mgr()
        a, _ = self.result(m, {"id": "r0123456789ab"})
        b, _ = self.result(m, {"id": "r0123456789ab"})
        self.assertEqual(window.copy_of([a, b]), {1: 0})
        loop = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {
            "name": RECALL_NAME, "arguments": "{}"}}]}, dict(a, tool_call_id="a"),
            {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "type": "function", "function": {
                "name": RECALL_NAME, "arguments": "{}"}}]}, dict(b, tool_call_id="b")]
        order = window.removal_order(loop, TokenCounter(3.5).count, "", "value")
        self.assertEqual((order[0]["i"], order[0]["band"]), (3, 0))
        window.remove_piece(loop, [p for p in window.pieces(loop) if p["i"] == 1][0], RECALL_NAME, "en",
                            TokenCounter(3.5).count)
        self.assertEqual(window.copy_of(loop), {}, "a receipt certifies nothing")
        self.assertNotIn("_src", loop[1])

    def test_same_identifier_in_two_scopes_both_stay(self):
        """db-x-007 appears in two different records: two results, neither a copy of the other, both protected."""
        m, _ = self.mgr()
        a, _ = self.result(m, {"id": "r0123456789ab", "query": "db-x-007"})
        b, _ = self.result(m, {"id": "r0123456789cd", "query": "db-x-007"})
        loop = []
        for k, r in enumerate((a, b)):
            loop += [{"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % k, "type": "function",
                                                                          "function": {"name": RECALL_NAME,
                                                                                       "arguments": "{}"}}]},
                     dict(r, tool_call_id="c%d" % k)]
        order = window.removal_order(loop, TokenCounter(3.5).count, "which port has db-x-007?", "value")
        self.assertEqual([(p["band"], p["protected"]) for p in order], [(3, True), (3, True)])

    def test_reasoning_is_never_protected_and_no_discount(self):
        lp = [{"role": "assistant", "content": "", "reasoning_content": "I must find db-x-007 now",
               "tool_calls": [{"id": "a", "type": "function", "function": {"name": RECALL_NAME, "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "a", "content": "nothing about it here, plain words"}]
        order = window.removal_order(lp, TokenCounter(3.5).count, "which port has db-x-007?", "value")
        self.assertEqual([(p["kind"], p["band"], p["protected"]) for p in order],
                         [("reasoning", 1, False), ("result", 2, False)])
        pc = {"kind": "result", "round": 0, "text": "db-x-007 port 1"}
        self.assertEqual(window.piece_value(pc, "db-x-007?", [], 1, {"db-x-007"}),
                         window.piece_value(pc, "db-x-007?", [], 1, None))

    def test_src_is_private(self):
        self.assertIn("_src", window.PRIVATE)
        m = {"role": "tool", "content": "x", "_src": [["span", "r", 0, 1]], "_final_note": "n"}
        self.assertEqual(window.clean([m]), [{"role": "tool", "content": "x"}])


# --------------------------------------------------------------------------------------- the loop, end to end
class Session:
    """A long agent session (big tool outputs, masked and archived), then a question; the fake model calls recall
    with a scripted list of calls per round, with a long reasoning, then answers."""

    def __init__(self, window_: int = 16384, script=None, reasoning_chars: int = 9000, extra: dict | None = None,
                 steps: int = 12, size: int = 260, max_tokens: int = 4096, stream: bool = False):
        self.W, self.script, self.rchars, self.stream, self.mt = window_, script or [], reasoning_chars, stream, max_tokens
        self.k = None
        self.final_tools = []
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.eng = FakeEngine(self.policy, n_ctx=window_)
        self.eng.strict_window = True
        tc = TokenCounter(3.5, TOKDIR if EXACT else None)
        if EXACT:
            ref = TokenCounter(3.5, TOKDIR)

            def engine_count(body):
                msgs = [{k: v for k, v in m.items() if k != "extra"} for m in body.get("messages") or []]
                return sum(ref.raw(t) for _, t in render_pieces(msgs, body.get("tools"), template_kwargs(body)))
            self.eng.counter = engine_count
        url = self.eng.start()
        w = window_
        cfg = Config.from_dict({"window": w, "mask_trigger": w // 2, "mask_target": w // 4, "keep_recent_tokens": w // 8,
                                "min_mask_tokens": 200, "reserve": w // 16, "default_response": 4096,
                                "tail_max": w // 4, "notes_max_tokens": 1024, "recall_max_tokens": 1200,
                                "max_recall_rounds": 4, "response_floor": 4096, **(extra or {})})
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        self.proxy = Proxy(cfg, Upstream(url), Store(os.path.join(self.tmp.name, "a.sqlite")), Journal(self.jpath), tc)
        if not EXACT:
            self.eng.counter = lambda body: self.proxy.wguard.count_body(body)
        self.steps, self.size = steps, size

    def rids(self, body):
        return re.findall(r"\br[0-9a-f]{12}\b", json.dumps(body["messages"]))

    def policy(self, body):
        msgs = body["messages"]
        last = msgs[-1]
        names = {((t or {}).get("function") or {}).get("name") for t in body.get("tools") or []}
        if last.get("role") == "user" and "QUESTION" in str(last.get("content")):
            self.k = 0
        if self.k is not None and RECALL_NAME in names and self.k < len(self.script):
            calls = self.script[self.k](body) if callable(self.script[self.k]) else self.script[self.k]
            self.k += 1
            return {"role": "assistant", "content": "", "reasoning_content": ("checking svc rounds. " * 600)[
                :self.rchars] + "\nso: next call", "tool_calls": [
                {"id": "rc%d_%d" % (self.k, j), "type": "function", "function": {"name": c.get("name", RECALL_NAME),
                                                                                  "arguments": json.dumps(c["args"])}}
                for j, c in enumerate(calls)]}
        if self.k is not None:
            self.final_tools.append(sorted(names))
        return {"role": "assistant", "content": "answer: yes"}

    def history(self):
        h = [{"role": "system", "content": "you are a coding agent"}, {"role": "user", "content": "inspect"}]
        for k in range(1, self.steps + 1):
            cid = "call_%d" % k
            h += [{"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": "cat svc-%d.log" % k})}}]},
                  {"role": "tool", "tool_call_id": cid, "content": record("s%d" % k, self.size)},
                  {"role": "assistant", "content": "read svc-%d" % k}, {"role": "user", "content": "next"}]
        return h

    def run(self, question="QUESTION: does db-s3-007 appear in the outputs of both round 3 and round 9?"):
        h = self.history()
        out = []
        for k in range(4, len(h) + 1, 4):
            out.append(self.send(h[:k]))
        self.n0 = len(self.eng.requests)
        h.append({"role": "user", "content": question})
        out.append(self.send(h))
        return out

    def send(self, h):
        req = {"model": "m", "messages": h, "tools": TOOLS, "max_tokens": self.mt}
        if self.stream:
            st, r, _ = self.proxy.chat(req, emit=lambda c: None)
        else:
            st, r, _ = self.proxy.chat(req)
        return st, r

    def events(self, name=None, action=None):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath) as f:
            ev = [json.loads(l) for l in f]
        return [e for e in ev if (name is None or e["event"] == name) and (action is None or e.get("action") == action)]

    def turn_requests(self):
        return self.eng.requests[self.n0:]

    def close(self):
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()


def seen_then_lost(requests) -> list:
    """Engine-side check, independent of the proxy's bookkeeping: a tool result the model has SEEN (present in a
    request) and that a later request of the same turn no longer carries verbatim - replaced by a receipt or gone -
    while no other result still present has the same text. Returns the lost tool_call ids."""
    lost, seen = [], {}
    for body in requests:
        msgs = body["messages"]
        cur = {m.get("tool_call_id"): str(m.get("content") or "") for m in msgs if m.get("role") == "tool"}
        for tid, txt in seen.items():
            now = cur.get(tid)
            if now is not None and now.startswith(txt):
                continue
            if any(v.startswith(txt) for k, v in cur.items() if k != tid):
                continue                                   # an identical result still present: nothing lost
            if tid not in lost:
                lost.append(tid)
        for tid, txt in cur.items():
            if RECEIPT not in txt and tid not in seen and tid and tid.startswith("rc"):
                seen[tid] = txt
    return lost


def assert_pairs(tc, msgs):
    calls = [c["id"] for m in msgs if m.get("role") == "assistant" for c in m.get("tool_calls") or []]
    res = [m.get("tool_call_id") for m in msgs if m.get("role") == "tool"]
    tc.assertEqual(sorted(calls), sorted(res), "tool protocol: every call has its result and vice versa")


class TestLoop(unittest.TestCase):
    def check_window(self, s):
        self.assertEqual(s.eng.violations, [], "requests sent over the window")
        self.assertEqual(s.proxy.wguard.violations, 0)
        self.assertEqual(getattr(s.proxy.wguard, "count_errors", 0), 0)
        for b in s.turn_requests():
            self.assertLessEqual(s.eng.counter(b) + b["max_tokens"] + s.proxy.wguard.margin, s.W)
            assert_pairs(self, b["messages"])

    def two_proofs(self, **kw):
        def by_round(n):
            return lambda body: [{"args": {"query": "db-s%d-007" % n}}]
        return Session(script=[by_round(3), by_round(9), by_round(5), by_round(7)], **kw)

    def test_two_proofs_both_reach_the_final_call(self):
        """The 0031/0892 shape: two searches for two different rounds, ~2.5K tokens of reasoning each, F = 4096 at
        16K. Both results must be in the final request (0.3.4 removed the first to make room for the second)."""
        s = self.two_proofs()
        try:
            res = s.run()
            self.assertEqual(res[-1][0], 200)
            self.check_window(s)
            reqs = s.turn_requests()
            final = reqs[-1]
            tool_txt = [str(m.get("content")) for m in final["messages"] if m.get("role") == "tool"
                        and str(m.get("tool_call_id", "")).startswith("rc")]
            self.assertTrue(any("db-s3-007" in t and RECEIPT not in t for t in tool_txt), tool_txt)
            self.assertTrue(any("db-s9-007" in t and RECEIPT not in t for t in tool_txt), tool_txt)
            self.assertEqual(seen_then_lost(reqs), [])
            wg = s.events("window_guard")
            self.assertEqual(sum(e.get("noncopy_search") or 0 for e in wg), 0)
            self.assertEqual(max([e.get("evidence_lost") or 0 for e in wg] or [0]), 0)
            self.assertGreaterEqual(final["max_tokens"], 4096, "the final answer gets F")
        finally:
            s.close()

    def test_two_proofs_streaming(self):
        s = self.two_proofs(stream=True)
        try:
            res = s.run()
            self.assertEqual(res[-1][0], 200)
            self.check_window(s)
            self.assertEqual(seen_then_lost(s.turn_requests()), [])
        finally:
            s.close()

    def test_generation_far_above_s_costs_only_the_new_group(self):
        """The model generates far more than S (content + huge arguments, not removable): the newest group is
        reduced or cancelled, the evidence already seen stays."""
        def huge(body):
            return [{"args": {"query": "db-s9-007", "note": "z" * 30000}}]
        s = Session(script=[[{"args": {"query": "db-s3-007"}}], huge, huge], reasoning_chars=3000)
        try:
            s.run()
            self.check_window(s)
            reqs = s.turn_requests()
            self.assertEqual(seen_then_lost(reqs), [])
            final = reqs[-1]
            self.assertTrue(any("db-s3-007" in str(m.get("content")) and RECEIPT not in str(m.get("content"))
                                for m in final["messages"] if m.get("role") == "tool"))
            self.assertTrue(s.proxy is not None)
        finally:
            s.close()

    def test_several_calls_in_one_round_stop_searching_when_room_ends(self):
        """Three recalls in one round with little room: once a result finds no room the others do not search and
        leave the assistant message (protocol coherent); nothing seen before is lost."""

        def calls(body):
            ids = s.rids(body)[:3]
            return [{"args": {"id": r}} for r in ids] or [{"args": {"query": "db-s3"}}]
        s = Session(script=[calls, calls, calls], reasoning_chars=1500, extra={"recall_max_tokens": 4000})
        try:
            s.run()
            self.check_window(s)
            self.assertEqual(seen_then_lost(s.turn_requests()), [])
            self.assertTrue(s.events("recall_no_room"), "the scenario must run out of room inside one round")
            self.assertTrue(s.events("recall_skipped"), "the calls after the closure do not search")
            self.assertEqual(len(s.events("recall")), 1)
            fin = s.events("window_guard", "final")
            self.assertEqual(fin[-1]["reason"], "no_room_for_result")
        finally:
            s.close()

    def test_max_recall_rounds_zero_and_equality(self):
        for n in (0, 1, 2):
            with self.subTest(max_recall_rounds=n):
                s = Session(script=[[{"args": {"query": "db-s3-007"}}]] * 5, extra={"max_recall_rounds": n},
                            reasoning_chars=600, window_=32768)
                try:
                    s.run()
                    self.check_window(s)
                    self.assertEqual(len(s.events("recall")), n, "exactly max_recall_rounds searches")
                    self.assertEqual(len(s.turn_requests()), n + 1, "then the final call directly")
                    self.assertNotIn(RECALL_NAME, s.final_tools[-1])
                    self.assertEqual(s.events("recall_dropped"), [])
                finally:
                    s.close()

    def test_answerfix_k_after_the_closure_does_not_reopen_the_search(self):
        """K retry after a final round without the proxy's tools: the retry must not offer recall again."""
        s = Session(script=[[{"args": {"query": "db-s3-007"}}]] * 6, extra={
            "max_recall_rounds": 1, "truncation_guard": True, "truncation_guard_max_tokens": 64}, reasoning_chars=600,
            window_=32768)
        orig = s.policy

        def pol(body):
            m = orig(body)
            names = {((t or {}).get("function") or {}).get("name") for t in body.get("tools") or []}
            if s.k is not None and RECALL_NAME not in names and m.get("content") == "answer: yes":
                return {"role": "assistant", "content": "port 3000"}       # prefix of 30007 in the last recall
            return m
        s.eng.policy = pol
        try:
            s.run(question="QUESTION: which port has db-s3-007? use recall")
            self.check_window(s)
            reqs = s.turn_requests()
            for b in reqs[1:]:
                self.assertNotIn(RECALL_NAME, {((t or {}).get("function") or {}).get("name")
                                               for t in b.get("tools") or []})
        finally:
            s.close()

    def test_mixed_calls_streaming_return_only_client_calls(self):
        s = Session(script=[[{"args": {"query": "db-s3-007"}}, {"name": "bash", "args": {"command": "ls"}}]],
                    stream=True, window_=32768)
        try:
            res = s.run()
            self.assertEqual(res[-1][0], 200)
            self.check_window(s)
            self.assertTrue(s.events("recall_dropped"))
            self.assertEqual(s.events("recall"), [])
        finally:
            s.close()


# ------------------------------------------------------------------------------------- plan / fit, unit level
class Unit(unittest.TestCase):
    W = 16384

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.jpath = os.path.join(self.tmp.name, "j.jsonl")
        self.px = Proxy(Config.from_dict({"window": self.W}), Upstream("http://127.0.0.1:9"),
                        Store(os.path.join(self.tmp.name, "a.sqlite")), Journal(self.jpath),
                        TokenCounter(3.5, TOKDIR if EXACT else None))
        self.addCleanup(self.px.store.db.close)
        rtool = {"type": "function", "function": {"name": RECALL_NAME, "parameters": {"type": "object"}}}
        self.p = SimpleNamespace(conv="c", tools=TOOLS + [rtool], recall_name=RECALL_NAME, lang="en", catalog=None,
                                 recall_aliases=frozenset((RECALL_NAME, LEGACY_RECALL_NAME)))
        self.body = {"max_tokens": 4096}
        self.floor = self.px._answer_floor(self.body)

    def events(self, action=None):
        with open(self.jpath) as f:
            ev = [json.loads(l) for l in f]
        return [e for e in ev if e["event"] == "window_guard" and (action is None or e.get("action") == action)]

    def filler(self, target, extra_msgs=()):
        base = [{"role": "system", "content": "s"}, {"role": "user", "content": ""}]

        def at(n):
            base[1]["content"] = "x " * n
            return self.px._wcount(base + window.clean(list(extra_msgs)), self.p.tools, self.body)
        lo, hi = 0, 8
        while at(hi) < target:
            hi *= 2
        while lo < hi:
            mid = (lo + hi) // 2
            if at(mid) < target:
                lo = mid + 1
            else:
                hi = mid
        base[1]["content"] = "x " * lo
        self.assertEqual(at(lo), target, "filler must hit the target exactly")
        return base

    def test_admission_boundary_is_exact(self):
        """P + max(S, CALL + R + F) + H + margin == W -> search; one token more -> final."""
        st = window.SearchState()
        H = self.px._frame_tokens(self.p, [{"role": "user", "content": "x"}], self.body)
        extra = max(st.s_est, window.CALL_ALLOWANCE + window.RESULT_R + self.floor) + H
        M = self.px.wguard.margin
        for delta, want_final in ((0, False), (1, True)):
            with self.subTest(delta=delta):
                try:
                    phys0 = self.filler(self.W - M - extra + delta)
                except AssertionError:
                    self.skipTest("token boundary not reachable with this filler")
                H2 = self.px._frame_tokens(self.p, phys0, self.body)
                if H2 != H:
                    self.skipTest("frame cost depends on the prompt")
                final, _ = self.px._plan_round(self.p, phys0, [], self.body, 0, self.floor, "q", window.SearchState())
                self.assertEqual(final, want_final)

    def test_s_never_decreases_and_censored(self):
        st = window.SearchState()
        st.saw_generation(600, "tool_calls")
        self.assertEqual(st.s_est, window.S_MIN)
        st.saw_generation(5000, "length")
        self.assertEqual(st.s_est, 5000 + window.S_MARGIN)
        st.saw_generation(100, "tool_calls")
        self.assertEqual(st.s_est, 5000 + window.S_MARGIN)

    def test_notice_that_does_not_fit_cancels_the_call(self):
        st = window.SearchState()
        new = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {
            "name": RECALL_NAME, "arguments": "{}"}}]}]
        M = self.px.wguard.margin
        note = self.px.tc.count(window.FINAL_NOTE_EN)
        phys0 = self.filler(self.W - M - self.floor - note - 10, new + [{"role": "tool", "tool_call_id": "x",
                                                                       "content": ""}])
        got = self.px._fit_result(self.p, phys0, [], new, self.body, 1, self.floor, "q", "{}", "y " * 3000, [], None,
                                  st)
        self.assertIsNone(got, "not even the notice fits: the call leaves the group")
        self.assertTrue(st.force_final)

    def test_short_result_that_fits_stays_whole(self):
        """Argon: cap < 256 but the result fits -> delivered whole, no notice, search not closed."""
        st = window.SearchState()
        new = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {
            "name": RECALL_NAME, "arguments": "{}"}}]}]
        M = self.px.wguard.margin
        note = self.px.tc.count(window.FINAL_NOTE_EN)
        phys0 = self.filler(self.W - M - self.floor - note - 120, new + [{"role": "tool", "tool_call_id": "x",
                                                                        "content": ""}])
        out = "port 31010 for db-1"
        got = self.px._fit_result(self.p, phys0, [], new, self.body, 1, self.floor, "q", "{}", out,
                                  [{"rid": "r1", "offset": 0, "chars": len(out)}], None, st)
        self.assertEqual(got[0], out)
        self.assertFalse(st.force_final)

    def test_notice_when_no_room(self):
        st = window.SearchState()
        new = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {
            "name": RECALL_NAME, "arguments": "{}"}}]}]
        M = self.px.wguard.margin
        note = self.px.tc.count(window.FINAL_NOTE_EN)
        phys0 = self.filler(self.W - M - self.floor - note - 150, new + [{"role": "tool", "tool_call_id": "x",
                                                                        "content": ""}])
        meta = [{"rid": "r1", "offset": 0, "chars": 9}]
        got = self.px._fit_result(self.p, phys0, [], new, self.body, 1, self.floor, "q", "{}", "y " * 3000, meta,
                                  None, st)
        self.assertIsNotNone(got)
        self.assertIn("no room left", got[0])
        self.assertEqual((got[1], meta), ([], []))
        self.assertTrue(st.force_final)
        self.assertLessEqual(self.px.tc.count(got[0]), 150)

    def test_floor_check_before_the_loop(self):
        st = window.SearchState()
        phys0 = self.filler(self.W - self.floor)            # P0 + F + margin > W
        self.px._check_floor(self.p, phys0, self.body, self.floor, st)
        self.assertTrue(st.floor_short and st.force_final)
        self.assertEqual(self.events("floor_short")[-1]["room"], self.W - self.px.wguard.margin -
                         self.px._wcount(phys0, self.px._final_tools(self.p, phys0), self.body))
        final, _ = self.px._plan_round(self.p, phys0, [], self.body, 0, self.floor, "q", st)
        self.assertTrue(final)

    def test_long_proof_against_short_useless_text(self):
        """Final emergency: a long result the question is about vs a short useless one -> the useless one goes
        first even though it frees less (band 2 before band 3)."""
        proof = ("host db-77-001 port 31010 status ok\n" * 300)
        junk = "plain words nothing here\n" * 10
        loop = []
        for k, t in enumerate((proof, junk, "tiny")):
            loop += [{"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % k, "type": "function",
                                                                          "function": {"name": RECALL_NAME,
                                                                                       "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": "c%d" % k, "content": t, "_src": []}]
        order = window.removal_order(loop, self.px.tc.count, "which port has db-77-001?", "value")
        self.assertEqual([p["i"] for p in order if p["kind"] == "result"][-1], 1)

    def test_tool_definitions_pass_the_same_check(self):
        st = window.SearchState()
        new = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {
            "name": "tools", "arguments": "{}"}}]}]
        M = self.px.wguard.margin
        note = self.px.tc.count(window.FINAL_NOTE_EN)
        phys0 = self.filler(self.W - M - self.floor - note - 100, new + [{"role": "tool", "tool_call_id": "x",
                                                                        "content": ""}])
        out = self.px._fit_other(self.p, phys0, [], new, self.body, 1, self.floor, "q", "def " * 2000, st)
        self.assertIsNone(out)
        self.assertTrue(st.force_final)

    def test_exact_count_error_is_not_silent(self):
        if not EXACT:
            self.skipTest("needs CTX_TOKDIR")
        g = self.px.wguard
        n0 = g.count_errors
        g.count([{"role": "weird", "content": "x"}], None)
        self.assertEqual(g.count_errors, n0 + 1)
        self.assertTrue(self.events("count_error"))


if __name__ == "__main__":
    unittest.main()
