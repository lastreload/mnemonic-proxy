"""0.3.0 — argomenti mascherati copiati in una scrittura (LIVE020, problema 3: 3 Write/Edit su 140 con Claude Code
finivano con la forma accorciata "prime righe + …", e una ha prodotto un NameError vero).

Il motore finto rilegge dal prompt fisico l'argomento accorciato di una write vecchia e lo rimanda identico in una
write nuova: il proxy non deve farla arrivare al client, deve rispondere al modello con l'errore che indica recall.

    python3 -m unittest -v tests.test_args_copy
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import masked_calls  # noqa: E402
from ctxproxy.paging import receipt_contaminated  # noqa: E402
from tests.test_proxy import Base  # noqa: E402

SYSTEM = {"role": "system", "content": "sei un agente di programmazione"}
SRC = "".join("def f%d(x):\n    return x + %d\n\n" % (k, k) for k in range(60))


def wcall(cid, name, **args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def masked_value(name, key, value):
    m = {"role": "assistant", "content": "", "tool_calls": [wcall("x", name, path="a.py", **{key: value})]}
    return json.loads(masked_calls(m, "a0123456789ab", 1)[0]["function"]["arguments"])[key]


class TestGuardUnit(unittest.TestCase):
    def test_masked_value_is_recognised(self):
        # pi (write/edit), Claude Code (Write/Edit/MultiEdit), Codex (apply_patch, exec_command)
        for name, key in (("write", "content"), ("Write", "content"), ("edit", "newText"), ("Edit", "new_string"),
                          ("MultiEdit", "edits"), ("apply_patch", "input"), ("exec_command", "cmd"),
                          ("Bash", "command")):
            v = masked_value(name, key, SRC)
            self.assertNotEqual(v, SRC)
            self.assertTrue(receipt_contaminated(name, json.dumps({"path": "a.py", key: v})), name)
            # anche se il modello ricopia solo la testa più la riga finale, con escape \uXXXX
            self.assertTrue(receipt_contaminated(name, json.dumps({key: v}, ensure_ascii=True)), name)

    def test_old_0_2_form_is_recognised(self):
        # la forma della 0.2.0 (testa + riga fatta solo di "…"), che il modello può aver visto in sessioni vecchie
        self.assertTrue(receipt_contaminated("Write", json.dumps({"content": SRC[:160] + "\n\u2026"})))

    def test_real_code_passes(self):
        self.assertFalse(receipt_contaminated("Write", json.dumps({"path": "a.py", "content": SRC})))
        self.assertFalse(receipt_contaminated("Write", json.dumps({"content": "print('attendere\u2026')\n"})))
        self.assertFalse(receipt_contaminated("Read", json.dumps({"path": masked_value("Write", "content", SRC)})))


class TestCopiedArgsProxy(Base):
    """Il motore finto copia nella write nuova l'argomento accorciato che vede nel prompt: il proxy la blocca."""
    cfg_over = {"mask_reasoning": True, "mask_tool_args": True, "min_batch_tokens": 2000, "mask_trigger": 6000,
                "mask_target": 3000, "keep_recent_tokens": 1500}

    def policy(self, body):
        last = body["messages"][-1]
        if last.get("role") == "user" and "riscrivi" in str(last.get("content")):
            old = next(m for m in body["messages"] if m.get("role") == "assistant" and m.get("tool_calls")
                       and m["tool_calls"][0]["id"] == "w1")
            seen = json.loads(old["tool_calls"][0]["function"]["arguments"])["content"]
            self.copied = seen
            return {"role": "assistant", "content": "", "tool_calls": [wcall("w99", "Write", file_path="f1.py",
                                                                           content=seen)]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "w99":
            self.guard_msg = last["content"]
            return {"role": "assistant", "content": "", "tool_calls": [wcall("r1", "Read", file_path="f1.py")]}
        return {"role": "assistant", "content": "ok"}

    def test_copied_masked_write_is_blocked(self):
        h = [SYSTEM, {"role": "user", "content": "scrivi i file"}]
        for k in range(1, 18):
            cid = "w%d" % k
            h += [{"role": "assistant", "content": "", "tool_calls": [wcall(cid, "Write", file_path="f%d.py" % k,
                                                                            content=SRC)]},
                  {"role": "tool", "tool_call_id": cid, "content": "File created successfully"}]
            self.send(h)
        self.assertTrue(self.events("mask"))
        r, _ = self.send(h + [{"role": "user", "content": "riscrivi f1.py"}])
        self.assertNotEqual(self.copied, SRC, "nel prompt fisico l'argomento di w1 deve essere accorciato")
        m = r["choices"][0]["message"]
        self.assertEqual([c["function"]["name"] for c in m["tool_calls"]], ["Read"])
        self.assertIn("recall", self.guard_msg)
        self.assertEqual(len(self.events("receipt_guard")), 1)

    def test_stream_never_forwards_the_copied_write(self):
        h = [SYSTEM, {"role": "user", "content": "scrivi i file"}]
        for k in range(1, 18):
            cid = "w%d" % k
            h += [{"role": "assistant", "content": "", "tool_calls": [wcall(cid, "Write", file_path="f%d.py" % k,
                                                                            content=SRC)]},
                  {"role": "tool", "tool_call_id": cid, "content": "File created successfully"}]
            self.send(h)
        chunks = []
        st, _, _ = self.proxy.chat({"model": "m", "messages": h + [{"role": "user", "content": "riscrivi f1.py"}],
                                    "tools": [], "max_tokens": 1024, "stream": True}, None, emit=chunks.append)
        self.assertEqual(st, 200)
        names = [tc["function"]["name"] for ch in chunks if ch for c in ch.get("choices") or []
                 for tc in (c.get("delta") or {}).get("tool_calls") or [] if (tc.get("function") or {}).get("name")]
        self.assertEqual(names, ["Read"])


if __name__ == "__main__":
    unittest.main()
