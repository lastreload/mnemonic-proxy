"""Rimedi allo stop prematuro dopo un recall (ctxproxy/answerfix.py), con motore finto.
    python3 -m unittest -v tests.test_answerfix
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctxproxy import answerfix  # noqa: E402
from ctxproxy.core import RECALL_NAME  # noqa: E402
from tests.test_proxy import TOOLS, Base, history  # noqa: E402

DATA = ("account acct-77qz\nwindow_code: WX-5130-TEAL\nportata: 71.3 m3/s\n"
        "tests/test_money.py::test_rounding_half_even_negative FAILED\n") * 3


def hist(question="qual è il window_code di acct-77qz?"):
    h = [{"role": "system", "content": "sei un agente"}, {"role": "user", "content": "leggi conti.txt"},
         {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
             "name": "bash", "arguments": json.dumps({"command": "cat conti.txt"})}}]},
         {"role": "tool", "tool_call_id": "c1", "content": DATA},
         {"role": "assistant", "content": "Letto."}]
    return h + [{"role": "user", "content": question}]


def thinking(body):
    return ((body.get("chat_template_kwargs") or {}).get("enable_thinking")) is True


class _Policy:
    """Il modello: alla domanda chiama recall; dopo il recall risponde troncato senza thinking, intero con."""
    short = "The window_code for account acct-77qz is WX"
    full = "The window_code for account acct-77qz is WX-5130-TEAL."

    def policy(self, body):
        msgs = body["messages"]
        last = msgs[-1]
        if last.get("role") == "user" and "window_code" in str(last.get("content")):
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "rq1", "type": "function", "function": {
                "name": RECALL_NAME, "arguments": json.dumps({"query": "window_code"})}}]}
        if last.get("role") == "tool" and "WX-5130-TEAL" in str(last.get("content")):
            if thinking(body):
                return {"role": "assistant", "content": self.full, "reasoning_content": "il valore è WX-5130-TEAL"}
            return {"role": "assistant", "content": self.reply_off}
        return {"role": "assistant", "content": "ok"}

    reply_off = short


class TestDefaultOff(_Policy, Base):
    def test_nothing_changes(self):
        n0 = len(self.eng.requests)
        r, _ = self.send(hist())
        self.assertEqual(r["choices"][0]["message"]["content"], self.short)
        self.assertEqual(len(self.eng.requests) - n0, 2)
        for b in self.eng.requests[n0:]:
            self.assertFalse(thinking(b))
            self.assertNotIn("reasoning_budget_tokens", b)
            self.assertEqual(b["max_tokens"], 1024)
        self.assertFalse(self.events("truncation_guard") or self.events("answer_thinking"))


class TestThinkingAfterRecall(_Policy, Base):
    cfg_over = {"answer_thinking_after_recall": True, "answer_thinking_budget": 256}

    def test_thinking_only_on_the_answer_after_recall(self):
        n0 = len(self.eng.requests)
        r, _ = self.send(hist())
        b1, b2 = self.eng.requests[n0:]
        self.assertFalse(thinking(b1))                        # la richiesta del client: invariata
        self.assertNotIn("reasoning_budget_tokens", b1)
        self.assertTrue(thinking(b2))                         # dopo il recall: thinking con il tetto
        self.assertEqual(b2["reasoning_budget_tokens"], 256)
        self.assertEqual(b2["max_tokens"], 1024 + 256 + 64)
        msg = r["choices"][0]["message"]
        self.assertEqual(msg["content"], self.full)
        self.assertNotIn("reasoning_content", msg)            # il ragionamento non torna al client
        self.assertTrue(self.events("answer_thinking_stripped"))
        self.assertTrue(self.events("answer_thinking")[0]["used"])

    def test_history_identical_to_off(self):
        """Il turno dopo: i messaggi al motore sono quelli che si avrebbero senza T (a parte il testo risposta)."""
        h = hist()
        r, _ = self.send(h)
        h2 = h + [r["choices"][0]["message"], {"role": "user", "content": "grazie"}]
        self.send(h2)
        msgs = self.eng.requests[-1]["messages"]
        self.assertFalse(any(m.get("reasoning_content") for m in msgs))
        self.assertFalse(thinking(self.eng.requests[-1]))     # nessun recall in coda: niente thinking
        ids = [c["id"] for m in msgs for c in (m.get("tool_calls") or [])]
        self.assertIn("rq1", ids)                             # il ciclo recall interno è reinserito


class TestThinkingNoBudget(_Policy, Base):
    cfg_over = {"answer_thinking_after_recall": True, "answer_thinking_budget": 0}

    def test_no_cap(self):
        n0 = len(self.eng.requests)
        self.send(hist())
        b2 = self.eng.requests[n0 + 1]
        self.assertTrue(thinking(b2))
        self.assertNotIn("reasoning_budget_tokens", b2)
        self.assertEqual(b2["max_tokens"], 1024)


class TestGuard(_Policy, Base):
    cfg_over = {"truncation_guard": True, "truncation_guard_budget": 512}
    reply_off = "It is WX"         # corta come nei casi veri (il motore finto conta i token sul json del messaggio)

    def test_replays_once_with_thinking(self):
        n0 = len(self.eng.requests)
        r, _ = self.send(hist())
        reqs = self.eng.requests[n0:]
        self.assertEqual(len(reqs), 3)
        self.assertFalse(thinking(reqs[1]))
        self.assertTrue(thinking(reqs[2]))
        self.assertEqual(reqs[2]["reasoning_budget_tokens"], 512)
        self.assertEqual(reqs[2]["messages"], reqs[1]["messages"])     # stessa richiesta, rigiocata
        msg = r["choices"][0]["message"]
        self.assertEqual(msg["content"], self.full)
        self.assertNotIn("reasoning_content", msg)
        ev = self.events("truncation_guard")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["word"], ev[0]["token"], ev[0]["used"]), ("WX", "WX-5130-TEAL", True))
        self.assertEqual(ev[0]["before"], "It is WX")


class TestGuardNoTrigger(_Policy, Base):
    cfg_over = {"truncation_guard": True}
    reply_off = "WX-5130-TEAL"

    def test_complete_answer_not_replayed(self):
        n0 = len(self.eng.requests)
        r, _ = self.send(hist())
        self.assertEqual(len(self.eng.requests) - n0, 2)
        self.assertEqual(r["choices"][0]["message"]["content"], "WX-5130-TEAL")
        self.assertFalse(self.events("truncation_guard"))

    def test_no_recall_no_guard(self):
        n0 = len(self.eng.requests)
        r, _ = self.send(hist("dimmi ciao"))
        self.assertEqual(len(self.eng.requests) - n0, 1)
        self.assertFalse(self.events("truncation_guard"))


class TestGuardLongAnswer(_Policy, Base):
    cfg_over = {"truncation_guard": True, "truncation_guard_max_tokens": 16}
    reply_off = "Ho controllato il file conti.txt riga per riga e il codice che cerchi comincia con " * 3 + "WX"

    def test_long_not_replayed(self):
        n0 = len(self.eng.requests)
        self.send(hist())
        self.assertEqual(len(self.eng.requests) - n0, 2)
        self.assertFalse(self.events("truncation_guard"))


class TestGuardAndThinking(_Policy, Base):
    cfg_over = {"truncation_guard": True, "answer_thinking_after_recall": True}

    def test_no_double_replay(self):
        n0 = len(self.eng.requests)
        r, _ = self.send(hist())
        self.assertEqual(len(self.eng.requests) - n0, 2)     # T ha già il thinking: K non rigioca
        self.assertEqual(r["choices"][0]["message"]["content"], self.full)


class TestStreamThinking(_Policy, Base):
    cfg_over = {"answer_thinking_after_recall": True, "answer_thinking_budget": 256, "truncation_guard": True}

    def test_stream_round_gets_thinking(self):
        chunks = []
        n0 = len(self.eng.requests)
        st, r, _ = self.proxy.chat({"model": "m", "messages": hist(), "tools": TOOLS, "max_tokens": 1024,
                                    "stream": True}, emit=lambda c: chunks.append(c))
        self.assertEqual(st, 200)
        reqs = self.eng.requests[n0:]
        self.assertEqual(len(reqs), 2)                        # in streaming K non rigioca
        self.assertTrue(thinking(reqs[1]))
        self.assertEqual(reqs[1]["reasoning_budget_tokens"], 256)
        text = "".join((c["choices"][0]["delta"].get("content") or "") for c in chunks if c and c.get("choices"))
        self.assertEqual(text, self.full)


class TestPrefixHit(unittest.TestCase):
    R = ("window_code: WX-5130-TEAL\nportata 71.3 m3/s\nnode ip-10-4-77-212 Ready\n"
         "tests/test_money.py::test_rounding_half_even_negative FAILED\nsoglia_bar = 8.65\n")

    def test_cases_from_p5(self):
        for reply, tok in (("...is WX", "WX-5130-TEAL"), ("71.", "71.3"), ("7", "71.3"),
                           ("The output shows: ip", "ip-10-4-77-212"), ("tests", "tests/test_money.py::test_rounding_half_even_negative"),
                           ("soglia_bar = 8", "8.65"), ("**WX**", "WX-5130-TEAL")):
            hit = answerfix.prefix_hit(reply, self.R)
            self.assertIsNotNone(hit, reply)
            self.assertEqual(hit[1], tok, reply)

    def test_complete_or_absent(self):
        for reply in ("WX-5130-TEAL", "71.3", "ip-10-4-77-212 Ready", "La porta è 47213", "", "ok.", "8.65 bar"):
            self.assertIsNone(answerfix.prefix_hit(reply, self.R), reply)

    def test_ends_with_recall(self):
        al = frozenset((RECALL_NAME,))
        m = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": RECALL_NAME}}]},
             {"role": "tool", "tool_call_id": "a", "content": "x"}]
        self.assertTrue(answerfix.ends_with_recall(m, al))
        m2 = [{"role": "assistant", "content": "", "tool_calls": [{"id": "b", "function": {"name": "bash"}}]},
              {"role": "tool", "tool_call_id": "b", "content": "x"}]
        self.assertFalse(answerfix.ends_with_recall(m2, al))
        self.assertFalse(answerfix.ends_with_recall(m + [{"role": "user", "content": "?"}], al))


class TestFitWithBudgetGuard(unittest.TestCase):
    """Con il controllo del budget del proxy (finestre piccole) il tetto si riduce allo spazio rimasto."""

    class P:
        conv, tools, recall_aliases = "c", None, frozenset((RECALL_NAME,))

    def proxy(self, room):
        class Cfg:
            default_response = 512

        class Up:
            def guard(self, body, up):
                return {**body, "max_tokens": min(body["max_tokens"], room)}

        class Px:
            cfg, up = Cfg(), Up()
        return Px()

    def test_room(self):
        body = {"max_tokens": 512}
        s = answerfix.fit_thinking(self.proxy(10000), self.P, body, [], 512)
        self.assertEqual((s["reasoning_budget_tokens"], s["max_tokens"]), (512, 512 + 512 + 64))
        s = answerfix.fit_thinking(self.proxy(600), self.P, body, [], 512)
        self.assertEqual((s["reasoning_budget_tokens"], s["max_tokens"]), (600 - 64 - 64, 600))
        self.assertIsNone(answerfix.fit_thinking(self.proxy(150), self.P, body, [], 512))
        self.assertIsNone(answerfix.fit_thinking(self.proxy(400), self.P, body, [], 0))
        s = answerfix.fit_thinking(self.proxy(600), self.P, body, [], 0)       # senza tetto: entra la risposta intera
        self.assertEqual((s.get("reasoning_budget_tokens"), s["max_tokens"]), (None, 512))


if __name__ == "__main__":
    unittest.main()
