"""notebudget - the handoff-notes request when A is near the window edge.

Story (real session, 11/11 01:56 on the journal): the notes request went out with prompt 129690, the window guard
lowered max_tokens from 16384 to 1318 and the reasoning budget (notes_max_tokens // 2, decided on the value *asked*)
was then capped to 806: the notes came out with 493 tokens and finish=length (the other ten handoffs of that
installation produced 957-4739), and the next segment inherited a poor handoff.

`notes_budgets` takes the room that really exists: 0 < max_tokens <= room, min(NOTES_ROOM, max_tokens) for the notes,
and (0, 0) when not even a useful note fits - the caller then sends nothing.

    python3 -m unittest -v tests.test_notes_budget
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import NOTES_MARK_EN, NOTES_MIN, NOTES_ROOM, notes_budgets  # noqa: E402
from tests.test_proxy import Base, tool_round  # noqa: E402


def budgets_0326(want: int, room: int) -> tuple[int, int]:
    """The 0.3.26 arithmetic, kept on purpose: the test that uses it fails if that behaviour comes back."""
    mt = min(int(want), int(room))
    return mt, max(1, min(int(want) // 2, mt - 512))


class TestNotesBudget(unittest.TestCase):
    def test_room_to_spare_is_unchanged(self):
        # with room to spare the values are the 0.3.26 ones: nobody's behaviour changes
        for want, room in ((12288, 13782), (12288, 30000), (16384, 40000)):
            mt, budget = notes_budgets(want, room, want // 2)
            self.assertEqual(mt, want)
            self.assertEqual(budget, want // 2)

    def test_nothing_is_asked_when_a_useful_note_does_not_fit(self):
        # (0, 0) = do not send: a request that fits nothing would only be refused by the guard and cost a turn
        for room in (-1000, -1, 0, 1, 2, 33, NOTES_MIN - 1):
            self.assertEqual(notes_budgets(12288, room, 6144), (0, 0), room)

    def test_max_tokens_never_exceeds_the_room(self):
        for room in (NOTES_MIN, 300, 900, NOTES_ROOM, 4096, 12288, 10 ** 6):
            mt, budget = notes_budgets(12288, room, 6144)
            self.assertGreaterEqual(mt, 1)
            self.assertLessEqual(mt, room)           # INV-1 holds by construction
            self.assertGreaterEqual(budget, 1)       # Strata reads 0 as "no cap"
            self.assertGreaterEqual(mt - budget, 1)  # at least one token of notes, always

    def test_squeezed_request_protects_the_notes(self):
        # the real case: 1318 tokens of room -> the reasoning must not eat the notes
        mt, budget = notes_budgets(12288, 1318, 12288 // 2)
        self.assertEqual(mt, 1318)
        self.assertLessEqual(budget, 32)
        self.assertGreaterEqual(mt - budget, mt - 1)

    def test_notes_get_notes_room_when_it_fits(self):
        mt, budget = notes_budgets(12288, 4096, 12288 // 2)
        self.assertEqual(mt, 4096)
        self.assertGreaterEqual(mt - budget, NOTES_ROOM)

    def test_without_thinking_the_reservation_is_not_applied(self):
        # notes_thinking = "off": the caller ignores the budget, but max_tokens stays the real one
        for room in (1318, 4096, 12288):
            mt, _ = notes_budgets(12288, room, 0)
            self.assertEqual(mt, min(12288, room))

    def test_the_old_arithmetic_is_gone(self):
        # the three numbers the journal measured: room 1318 -> max_tokens 1318, budget 806, notes 493, finish=length
        want, room = 12288, 1318
        old_mt, old_budget = budgets_0326(want, room)
        self.assertEqual((old_mt, old_budget), (1318, 806))
        new_mt, new_budget = notes_budgets(want, room, want // 2)
        self.assertEqual(new_mt, old_mt)
        self.assertLessEqual(new_budget, 32)
        self.assertGreaterEqual(new_mt - new_budget, 1024)      # 1317 notes instead of 512
        self.assertGreater(new_mt - new_budget, old_mt - old_budget)


class TestNotesSkipped(Base):
    """The `if mt:` branch of _switch: with no room for a useful note, nothing is asked and the segment still opens."""

    cfg_over = {"window": 24000, "mask_trigger": 10 ** 9, "keep_recent_tokens": 2000,
                "notes_max_tokens": 4096, "reserve": 512, "response_floor": 0, "default_response": 4096}

    def history(self, rounds=120, size=1000):
        msgs = [{"role": "system", "content": "s" * 3000},
                {"role": "user", "content": "build the service"}]
        for k in range(rounds):
            msgs += tool_round(k, size)
        return msgs

    def test_no_room_no_request_and_the_segment_opens_with_the_fallback_note(self):
        msgs = self.history()
        with patch("ctxproxy.core.notes_budgets", return_value=(0, 0)):
            try:
                self.proxy.mgr.prepare({"messages": msgs, "max_tokens": 4096}, upstream=self.proxy.up)
            except Exception:
                pass      # a window this small can still refuse the *next* request: the switch already ran
            # 1) nothing was asked, and no error was invented
            self.assertFalse([b for b in self.eng.requests
                              if NOTES_MARK_EN in json.dumps(b.get("messages") or [{}], ensure_ascii=False)],
                             "a notes request reached the engine")
            # 0.3.27 logs the handoff attempt even when nothing was asked: the status must say "fallback"
            self.assertEqual([e.get("handoff_status") for e in self.events("notes")], ["fallback"])
            self.assertEqual(self.events("notes_error"), [])
            skipped = self.events("notes_skipped")
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["reason"], "no_room")
            # 2) the segment is saved anyway, with the fallback note inside it
            switch = self.events("switch")
            self.assertEqual(len(switch), 1)
            self.assertGreater(switch[0]["notes_msg_tokens"], 0)
            conv = skipped[0]["conv"]
            segs = self.proxy.store.segments(conv)
            self.assertEqual(len(segs), 1)
            self.assertIn("notes not available", segs[0][3])
            # 3) and that message is what the next segment is built from - nothing waits for an answer
            from ctxproxy.core import chain
            hs = chain(msgs, None)
            seg, start, notes_msg, kind, found = self.proxy.mgr._segment_for(conv, hs)
            self.assertTrue(found)
            self.assertGreater(start, 0)
            self.assertIn("notes not available", notes_msg)


class TestNotesBudgetReview(unittest.TestCase):
    """Os casos que a revisão da 0.3.31 apontou: orçamento configurado pequeno, com janela folgada.

    `notes_budgets(want, room, thinking_budget)` recebia 1536/3072 de notas com 50 000 de room e devolvia 1 token
    de raciocínio, porque o reservado das notas (`min(NOTES_ROOM, max_tokens)`) comia a request inteira.
    """

    def test_a_small_configured_budget_keeps_its_reasoning(self):
        for want, thinking in ((1536, 768), (3072, 1536)):
            mt, budget = notes_budgets(want, 50000, thinking)
            self.assertEqual(mt, want, "a request configurada cabe inteira: max_tokens e o 'want'")
            self.assertEqual(budget, thinking, "com room de sobra o raciocinio mantem a cota configurada")
            self.assertGreaterEqual(mt - budget, NOTES_MIN, "e as notas ainda guardam o minimo delas")

    def test_room_to_spare_is_still_the_previous_result(self):
        # o caso que ja existia nao pode mudar: 12288 com room de sobra -> (12288, 6144)
        self.assertEqual(notes_budgets(12288, 13000, 6144), (12288, 6144))
        self.assertEqual(notes_budgets(16384, 40000, 8192), (16384, 8192))

    def test_the_squeezed_request_still_protects_the_notes(self):
        # o aperto da janela continua igual: room 1318 -> raciocinio 1, ~1300 tokens para as notas
        mt, budget = notes_budgets(12288, 1318, 6144)
        self.assertEqual(mt, 1318)
        self.assertEqual(budget, 1)
        self.assertGreaterEqual(mt - budget, 1200)

    def test_a_note_below_the_minimum_is_not_asked(self):
        # revisao 0.3.31: (1, 50000, 1) era (1, 1) - uma nota de 1 token e uma lista de titulos vazios
        self.assertEqual(notes_budgets(1, 50000, 1), (0, 0))
        self.assertEqual(notes_budgets(NOTES_MIN - 1, 50000, 0), (0, 0))
        # a fronteira em si continua sendo enviada
        mt, budget = notes_budgets(NOTES_MIN, 50000, 0)
        self.assertEqual(mt, NOTES_MIN)
        self.assertGreaterEqual(budget, 1)

    def test_the_notes_never_lose_their_token(self):
        # invariante em toda a faixa: 0 < max_tokens <= room, raciocinio >= 1 e ao menos 1 token de nota
        for want in (1, 256, 512, 1318, 1536, 3072, 12288):
            for room in (0, 255, 256, 300, 1318, 5000, 50000):
                for thinking in (0, 1, 512, 6144, 10 ** 6):
                    mt, budget = notes_budgets(want, room, thinking)
                    if mt == 0:
                        self.assertEqual(budget, 0)
                        continue
                    self.assertLessEqual(mt, room, (want, room, thinking))
                    self.assertGreaterEqual(budget, 1, (want, room, thinking))
                    self.assertGreaterEqual(mt - budget, 1, (want, room, thinking))


if __name__ == "__main__":
    unittest.main()
