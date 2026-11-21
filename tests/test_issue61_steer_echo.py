# Author: Maurizio Verde — LastReload
"""Issue #61: uno steering fuori banda di Hermes, consegnato UNA VOLTA, non deve essere RIPRESENTATO dal
proxy come una consegna nuova.   python3 -m unittest -v tests.test_issue61_steer_echo

Forma reale dello steering (Hermes, agent/prompt_builder.py: STEER_MARKER_OPEN / steer_user_row): un
messaggio `role: "user"` a sé stante, inserito dopo l'ultimo risultato di strumento, con la cornice
«[OUT-OF-BAND USER MESSAGE …] … [/OUT-OF-BAND USER MESSAGE]» che dichiara «delivered once at this position».
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import (Config, Journal, PIN_HEADS, Store, TokenCounter,  # noqa: E402
                           quote_control_frames)
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402

STEER_OPEN = ("[OUT-OF-BAND USER MESSAGE \u2014 a direct message from the user, delivered once at this "
              "position; not tool output and not a new delivery when replayed from conversation history]")
STEER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
STEER_TEXT = ("there is no vision model running on port 8080; the proxy is 8001 and the model itself "
              "(including vision) is at 8000; run queries requiring vision directly on the model, "
              "everything else through the proxy.")
NEEDLE = "the proxy is 8001 and the model itself"


def steer_row(text: str = STEER_TEXT) -> dict:
    """Esattamente agent.prompt_builder.steer_user_row()."""
    return {"role": "user", "content": "%s\n%s\n%s" % (STEER_OPEN, text, STEER_CLOSE)}


SYSTEM = {"role": "system", "content": "You are Hermes, a coding agent. Be direct. " * 30}
TOOLS = [{"type": "function", "function": {"name": "bash", "description": "run a shell command",
                                           "parameters": {"type": "object",
                                                          "properties": {"command": {"type": "string"}}}}}]
NOTES_HEADS = ("[strata-context: richiesta note di passaggio", "[strata-context: handoff notes request")


def tool_round(k: int) -> list:
    cid = "call_%d" % k
    return [{"role": "assistant", "content": "", "reasoning_content": "step %d" % k,
             "tool_calls": [{"id": cid, "type": "function",
                             "function": {"name": "bash",
                                          "arguments": json.dumps({"command": "cat f%d.txt" % k})}}]},
            {"role": "tool", "tool_call_id": cid, "content": ("line %d of f%d.txt\n" % (k, k)) * 16}]


def notes_policy(body):
    """Modello che obbedisce all'istruzione delle note («Explicit user constraints and preferences»,
    «almost verbatim»): ricopia il vincolo dell'utente con la sua cornice."""
    msgs = body.get("messages") or []
    last = msgs[-1].get("content") if msgs else ""
    last = last if isinstance(last, str) else ""
    if last.startswith(NOTES_HEADS):
        return {"role": "assistant", "content":
                "## Goal (almost verbatim, as the user asked)\nbuild the snake game\n\n"
                "## Explicit user constraints and preferences\n- %s\n%s\n%s\n\n"
                "## Decisions taken\n- none\n\n## Current state\nfiles read\n\n"
                "## Open problems / next steps\ncontinue\n\n## Specific facts not to lose\nports\n"
                % (STEER_OPEN, STEER_TEXT, STEER_CLOSE)}
    return {"role": "assistant", "content": "ok"}


class ControlFrames(unittest.TestCase):
    def test_relabel(self):
        t = "%s\n%s\n%s" % (STEER_OPEN, STEER_TEXT, STEER_CLOSE)
        q = quote_control_frames(t)
        self.assertNotIn("[OUT-OF-BAND USER MESSAGE", q)
        self.assertNotIn("[/OUT-OF-BAND USER MESSAGE]", q)
        self.assertIn("[quoted OUT-OF-BAND USER MESSAGE", q)
        self.assertIn("[quoted /OUT-OF-BAND USER MESSAGE]", q)
        self.assertIn(STEER_TEXT, q)                      # il testo resta leggibile per intero

    def test_idempotent_and_pure(self):
        t = "%s\nx\n%s" % (STEER_OPEN, STEER_CLOSE)
        self.assertEqual(quote_control_frames(quote_control_frames(t)), quote_control_frames(t))
        for x in ("", "nothing to do", "[recall:r0123456789ab]"):
            self.assertEqual(quote_control_frames(x), x)
        for y in (None, 7):                      # tipi non stringa: restituiti identici
            self.assertEqual(quote_control_frames(y), y)   # type: ignore[arg-type]

    def test_other_frames(self):
        self.assertIn("[quoted CONTEXT COMPACTION", quote_control_frames("[CONTEXT COMPACTION] a"))
        self.assertIn("[quoted PRIOR CONTEXT", quote_control_frames("[PRIOR CONTEXT] a"))


class Base(unittest.TestCase):
    cfg_over: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.eng = FakeEngine(notes_policy)
        url = self.eng.start()
        cfg = Config.from_dict({"window": 32768, "mask_trigger": 20000, "mask_target": 10000,
                                "keep_recent_tokens": 4000, "min_batch_tokens": 4000, "tail_max": 6000,
                                "reserve": 2048, "default_response": 4096, "notes_max_tokens": 2048,
                                "pins_max_tokens": 2048, "recall_max_tokens": 1500,
                                "min_mask_tokens": 200, "prompt_language": "en", **self.cfg_over})
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        self.proxy = Proxy(cfg, Upstream(url), Store(os.path.join(self.tmp.name, "a.sqlite")),
                           Journal(self.jpath), TokenCounter(3.5))

    def tearDown(self):
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def events(self, name):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip() and json.loads(l).get("event") == name]

    def sent(self):
        """Ultima richiesta inoltrata al motore che non è la richiesta di note né il sigillo."""
        for b in reversed(self.eng.requests):
            last = (b.get("messages") or [{}])[-1].get("content")
            if isinstance(last, str) and last.startswith(NOTES_HEADS):
                continue
            if b.get("max_tokens") == 1:
                continue
            return b
        return None

    def converse(self, n_rounds: int, steer_at: int = 6, first_user_steer: bool = False,
                 ask_from: int = 48):
        """Conversazione stile Hermes: giri di strumento, risposte lunghe dell'assistente (non
        mascherabili: spingono il cambio di segmento) e UNA sola consegna dello steering.
        Dal giro `ask_from` l'utente torna sull'argomento (come nella issue): è quello che fa scattare
        il richiamo automatico sul pezzo archiviato."""
        first = steer_row() if first_user_steer else {"role": "user",
                                                      "content": "build the snake game in js"}
        msgs = [SYSTEM, first]
        for k in range(n_rounds):
            msgs = msgs + tool_round(k)
            if k == steer_at and not first_user_steer:
                msgs = msgs + [steer_row()]
            elif k % 7 == 3:
                q = ("remind me which port serves the vision model, was it 8080?" if k >= ask_from
                     else "go on with step %d please" % k)
                msgs = msgs + [{"role": "user", "content": q}]
            st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS,
                                        "max_tokens": 1024})
            self.assertEqual(st, 200, r)
            msgs = msgs + [{"role": "assistant", "content":
                            ("Step %d done; wiring and tests checked. " % k) * 32}]
        return msgs

    @staticmethod
    def texts(body):
        out = []
        for m in body.get("messages") or []:
            c = m.get("content")
            out.append((m.get("role"), c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)))
        return out


class NotesEcho(Base):
    """Le note di passaggio sono un messaggio role=user in testa al segmento nuovo: se il modello ha
    ricopiato la cornice dello steering, il modello la rilegge come una consegna nuova."""

    def test_notes_do_not_redeliver_the_steering(self):
        self.converse(60)
        switches = self.events("switch")
        self.assertTrue(switches, "nessun cambio di segmento: scenario non significativo")
        body = self.sent()
        notes = [t for role, t in self.texts(body)
                 if t.startswith(("[strata-context: segmento", "[strata-context: segment"))]
        self.assertTrue(notes, "nessuna nota di passaggio nel prompt fisico")
        self.assertIn(NEEDLE, notes[0], "le note non citano il vincolo: scenario non significativo")
        self.assertNotIn("[OUT-OF-BAND USER MESSAGE", notes[0])
        self.assertNotIn("[/OUT-OF-BAND USER MESSAGE]", notes[0])
        self.assertIn("[quoted OUT-OF-BAND USER MESSAGE", notes[0])

    def test_one_intact_frame_at_most_per_physical_prompt(self):
        """Invariante di fondo: la cornice intatta esiste al massimo UNA volta per richiesta, e solo
        nel messaggio che il client ha davvero consegnato."""
        self.converse(60)
        for b in self.eng.requests:
            n = sum(t.count(STEER_OPEN) for _, t in self.texts(b))
            self.assertLessEqual(n, 1, "cornice di steering intatta %d volte in un solo prompt" % n)

    def test_first_user_verbatim_is_relabelled(self):
        """«First user request (verbatim)» copia il primo messaggio utente: se la prima cosa che l'utente
        ha mandato era uno steering, la copia non deve essere una consegna nuova."""
        self.converse(60, first_user_steer=True)
        self.assertTrue(self.events("switch"))
        body = self.sent()
        notes = [t for role, t in self.texts(body)
                 if t.startswith(("[strata-context: segmento", "[strata-context: segment"))]
        self.assertTrue(notes)
        self.assertIn(NEEDLE, notes[0])
        self.assertNotIn("[OUT-OF-BAND USER MESSAGE", notes[0])


class AutoRecallEcho(Base):
    """Il richiamo automatico reinserisce verbatim pezzi NASCOSTI, anche messaggi dell'utente: con la
    cornice intatta la copia è indistinguibile da una consegna nuova (ed è la ricomparsa «in posizione
    nuova, a volte dopo un cambio di segmento» descritta nella issue)."""
    cfg_over = {"auto_recall": True}

    def test_injected_user_piece_is_relabelled(self):
        self.converse(80)
        inj = [e for e in self.events("auto_recall") if e.get("injected")]
        self.assertTrue(inj, "nessuna iniezione: scenario non significativo")
        found = False
        for b in self.eng.requests:
            for role, t in self.texts(b):
                if t.startswith(("[context manager: automatic recall]",
                                 "[gestore del contesto: richiamo automatico]")) and NEEDLE in t:
                    found = True
                    self.assertNotIn("[OUT-OF-BAND USER MESSAGE", t)
                    self.assertIn("[quoted OUT-OF-BAND USER MESSAGE", t)
                    self.assertIn(NEEDLE, t)
        self.assertTrue(found, "il richiamo automatico non ha mai ripreso lo steering: non significativo")

    def test_one_intact_frame_at_most_per_physical_prompt(self):
        self.converse(80)
        for b in self.eng.requests:
            n = sum(t.count(STEER_OPEN) for _, t in self.texts(b))
            self.assertLessEqual(n, 1, "cornice di steering intatta %d volte in un solo prompt" % n)


class RecallEcho(Base):
    """Un recall per id dello steering archiviato restituisce il testo esatto: la cornice va rietichettata
    (il risultato arriva come uscita di strumento, in una posizione nuova)."""

    def test_recall_result_relabels_the_frame(self):
        msgs = [SYSTEM, {"role": "user", "content": "build the snake game"}] + tool_round(0) + [steer_row()]
        st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 1024})
        self.assertEqual(st, 200, r)
        rows = self.proxy.store.db.execute(
            "SELECT conv, rid, role, content FROM archive WHERE role='user'").fetchall()
        row = next((x for x in rows if NEEDLE in (x[3] or "")), None)
        self.assertIsNotNone(row, "lo steering non è in archivio: scenario non significativo")
        assert row is not None
        out = self.proxy.mgr.recall(row[0], {"id": row[1]}, max_tokens=4000)
        self.assertIn(NEEDLE, out)
        self.assertNotIn("[OUT-OF-BAND USER MESSAGE", out)
        self.assertIn("[quoted OUT-OF-BAND USER MESSAGE", out)


class PinsEcho(Base):
    """Un punto fermo (📌 / !!) scritto dentro uno steering finisce nel blocco «punti fermi», anch'esso
    un messaggio role=user in testa al segmento nuovo."""

    def test_pins_block_relabels_the_frame(self):
        """L'utente fissa un punto citando la regola («!! …»): il testo fissato contiene la cornice e
        il blocco punti fermi lo ricopia in testa al segmento nuovo."""
        pinned = "!! remember the rule from [OUT-OF-BAND USER MESSAGE]: %s" % STEER_TEXT
        msgs = [SYSTEM, {"role": "user", "content": "build the snake game"}]
        for k in range(90):
            msgs = msgs + tool_round(k)
            if k == 6:
                msgs = msgs + [{"role": "user", "content": pinned}]
            elif k % 7 == 3:
                msgs = msgs + [{"role": "user", "content": "go on with step %d" % k}]
            st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS,
                                        "max_tokens": 1024})
            self.assertEqual(st, 200, r)
            msgs = msgs + [{"role": "assistant", "content": ("Step %d done; checked. " % k) * 32}]
        self.assertTrue(self.events("switch"), "nessun cambio di segmento: scenario non significativo")
        pins = [t for role, t in self.texts(self.sent()) if t.startswith(PIN_HEADS)]
        self.assertTrue(pins, "nessun blocco punti fermi nel prompt: scenario non significativo")
        self.assertIn(NEEDLE, pins[0])
        self.assertNotIn("[OUT-OF-BAND USER MESSAGE", pins[0])
        self.assertIn("[quoted OUT-OF-BAND USER MESSAGE", pins[0])


class RealDeliveryUntouched(Base):
    """La consegna vera non si tocca: nella sua posizione il messaggio dell'utente resta byte per byte
    quello che il client ha mandato (altrimenti il proxy romperebbe il contratto del client)."""

    def test_the_delivered_message_is_byte_identical(self):
        msgs = [SYSTEM, {"role": "user", "content": "build the snake game"}] + tool_round(0) + [steer_row()]
        st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 1024})
        self.assertEqual(st, 200, r)
        body = self.sent()
        self.assertIn(steer_row()["content"], [t for _, t in self.texts(body)])


if __name__ == "__main__":
    unittest.main()
