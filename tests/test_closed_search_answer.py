# Author: Maurizio Verde — LastReload
"""Closed-search failures are explicit; private reasoning is never promoted."""
import unittest

from tests import test_final_recall_retry as retry
from tests.test_final_recall_retry import Stubborn, names_of
from ctxproxy.core import RECALL_NAME


class ClosedSearch(Stubborn):
    def __init__(self, mode="obey", **kw):
        self.mode = mode
        super().__init__(stubborn=50, script=[[{"args": {"query": "db-s3-007"}}]] * 5,
                         extra={"max_recall_rounds": 1}, reasoning_chars=600, window_=32768, **kw)

    def policy(self, body):
        if self.k is not None and RECALL_NAME not in names_of(body) and body.get("tool_choice") == "none":
            self.final_bodies.append(body)
            if self.mode == "obey":
                return {"role": "assistant", "content": "answer: yes"}
            if self.mode == "silent":
                return {"role": "assistant", "content": "  ", "reasoning_content": "Secret answer: 0"}
            self.final_bodies.pop()
        return super().policy(body)


class TestClosedSearchAnswer(unittest.TestCase):
    def test_last_attempt_disables_tools_or_reports_failure(self):
        for mode in ("obey", "silent", "ignore"):
            for stream in (False, True):
                with self.subTest(mode=mode, stream=stream):
                    s = ClosedSearch(mode=mode, stream=stream)
                    try:
                        st, resp = s.run()[-1]
                        self.assertEqual(st, 200)
                        retry.TestFinalRecallRetry().check_window(s)
                        msg = resp["choices"][0]["message"]
                        text = msg.get("content") or ""
                        self.assertTrue(text.strip())
                        self.assertNotIn("tool_calls", msg)
                        self.assertEqual(len(s.final_bodies), 3)
                        self.assertEqual(s.final_bodies[-1].get("tool_choice"), "none")
                        self.assertEqual(s.final_bodies[-1].get("tools"), s.final_bodies[-2].get("tools"))
                        self.assertEqual(resp["strata_context"]["final_answer_missing"], mode != "obey")
                        self.assertNotIn("tool_choice", s.final_bodies[0])
                        self.assertNotIn("tool_choice", s.final_bodies[1])
                        self.assertGreaterEqual(s.final_bodies[-1]["max_tokens"], 4096)
                        if mode == "obey":
                            self.assertEqual(text, "answer: yes")
                            self.assertEqual(s.events("final_answer_missing"), [])
                        else:
                            self.assertIn("[context manager:", text)
                            self.assertIn("did not produce a final text answer", text)
                            self.assertNotIn("Secret answer", text)
                            self.assertEqual(len(s.events("final_answer_missing")), 1)
                        if stream:
                            deltas = [c["choices"][0]["delta"] for c in s.chunks if c.get("choices")]
                            self.assertEqual("".join(d.get("content") or "" for d in deltas).strip(), text.strip())
                            self.assertFalse(any(d.get("tool_calls") for d in deltas))
                            self.assertEqual(s.chunks[-1]["choices"][0]["finish_reason"], "stop")
                    finally:
                        s.close()


if __name__ == "__main__":
    unittest.main()
