# Author: Maurizio Verde — LastReload
"""Handoff notes at a segment switch: tool_choice "none" where it keeps the prompt prefix, and notes recovered from
the reasoning / the attempted call instead of the empty fallback.

Seen with Qwen3.6 on Unsloth (llama-server): asked for the notes, the model answered twice in a row with a tool call
and no text, so the new segment started with "(notes not available ...)". Replaying the same request with
tool_choice "none" gave notes 2/2 with the prefix still cached. Strata, on the other hand, offers no tools at all when
tool_choice is "none": the system prompt changes and the whole segment would be read again, so there "auto" sends
nothing and relies on the recovery.

    python3 -m unittest -v tests.test_notes_handoff
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import engines  # noqa: E402
from ctxproxy.core import Config  # noqa: E402
from tests.test_proxy import NOTES_MARK, Base, history, tool_round  # noqa: E402

SEG = {"window": 40000, "mask_enabled": False, "tail_max": 12000, "reserve": 2000, "default_response": 2000,
       "slot_save": True}


class _Switch(Base):
    notes_reply: dict = {"role": "assistant", "content": "x"}

    def policy(self, body):
        last = body["messages"][-1]
        if last.get("role") == "user" and str(last.get("content", "")).startswith(NOTES_MARK):
            return dict(self.notes_reply)
        return {"role": "assistant", "content": "ok"}

    def switch_once(self):
        h = history(1) + [{"role": "assistant", "content": "first step done"}, {"role": "user", "content": "go on"}]
        for k in range(1, 14):
            h += tool_round(k, 12000)
            n0 = len(self.eng.requests)
            self.proxy.chat({"model": "m", "messages": h, "tools": self.tools(), "max_tokens": 1000})
            if self.events("switch"):
                reqs = self.eng.requests[n0:]
                notes_req = next(r for r in reqs if str(r["messages"][-1].get("content", "")).startswith(NOTES_MARK))
                b = self.eng.requests[-1]["messages"]
                return notes_req, self.events("notes")[0], b[1]["content"]
        self.fail("no segment switch")

    def tools(self):
        from tests.test_proxy import TOOLS
        return TOOLS


class TestClean(_Switch):
    cfg_over = {**SEG, "notes_tool_choice": "none"}
    notes_reply = {"role": "assistant", "content": "## Goal\nbuild snake\n## Current state\nf0..f9 read"}

    def test_clean_notes_and_tool_choice_none(self):
        req, ev, b1 = self.switch_once()
        self.assertEqual(req.get("tool_choice"), "none")
        self.assertTrue(req.get("tools"), "the tools stay in the request: same prompt prefix")
        self.assertEqual(ev["handoff_status"], "clean")
        self.assertIn("build snake", b1)


class TestRecovered(_Switch):
    cfg_over = {**SEG, "notes_tool_choice": "none"}
    notes_reply = {"role": "assistant", "content": "",
                   "reasoning_content": "I must finish the test in test_core.py, the parser is done",
                   "tool_calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "bash", "arguments": "{\"command\": \"pytest tests/test_core.py\"}"}}]}

    def test_reasoning_and_attempted_call_become_the_notes(self):
        req, ev, b1 = self.switch_once()
        self.assertEqual(ev["handoff_status"], "recovered")
        self.assertIn("I must finish the test in test_core.py", b1)
        self.assertIn("bash", b1)
        self.assertIn("pytest tests/test_core.py", b1)
        self.assertNotIn("notes not available", b1)


class TestThinkInContent(_Switch):
    cfg_over = SEG
    notes_reply = {"role": "assistant", "content": "<think>\nplanning the notes\n</think>\n## Goal\nbuild snake"}

    def test_think_block_is_not_part_of_the_notes(self):
        req, ev, b1 = self.switch_once()
        self.assertEqual(ev["handoff_status"], "clean")
        self.assertIn("build snake", b1)
        self.assertNotIn("planning the notes", b1)


class TestFallback(_Switch):
    cfg_over = SEG
    notes_reply = {"role": "assistant", "content": ""}

    def test_nothing_usable_keeps_the_fallback(self):
        req, ev, b1 = self.switch_once()
        self.assertNotIn("tool_choice", req, "auto without a resolved engine sends nothing")
        self.assertEqual(ev["handoff_status"], "fallback")
        self.assertIn("notes not available", b1)


class TestAutoPerEngine(unittest.TestCase):
    def resolve(self, kind, value="auto"):
        cfg = Config.from_dict({"notes_tool_choice": value})
        engines.apply(cfg, engines.Engine(kind, n_ctx=131072, status_kind=None if kind != "strata" else "strata"),
                      log=None)
        return cfg.notes_tool_choice

    def test_llama_server_gets_none(self):
        self.assertEqual(self.resolve("llama.cpp"), "none")

    def test_strata_and_ds4_send_nothing(self):
        # Strata drops the tools from the template when tool_choice is "none": the prefix would change.
        self.assertEqual(self.resolve("strata"), "keep")
        self.assertEqual(self.resolve("ds4"), "keep")

    def test_written_value_wins(self):
        self.assertEqual(self.resolve("strata", "none"), "none")
        self.assertEqual(self.resolve("llama.cpp", "keep"), "keep")


if __name__ == "__main__":
    unittest.main()
