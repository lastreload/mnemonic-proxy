"""End-to-end check of the prompt language (0.3.1) with the fake engine — no GPU, no real model.

Runs the long session of tests/lang_scenario.py (pinned point, disposable exchange, masking with receipts, automatic
recall, a recall call, the receipt guard, tools paging; then a second conversation through a segment switch with
handoff notes and the pinned-points block) twice: as a NEW conversation (English) and as a conversation created by
0.3.0 (stored language removed after the first request: Italian). Prints the final physical prompts and checks that
the English one contains no Italian text and the legacy one matches tests/data/lang_golden_030.sha256.

    python3 tools/lang_e2e.py [--full]
Author: Maurizio Verde — LastReload
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.lang_scenario import prompt_text, run_session  # noqa: E402
from tests.test_lang import GOLDEN_030, IT_WORDS, digest  # noqa: E402


def show(reqs, conv, full):
    last = [r for r in reqs if r["conv"] == conv][-1]
    print("=== conversation %s, last engine request: %d messages, tools %s" % (
        conv, len(last["messages"]), [t["function"]["name"] for t in last["tools"] or []]))
    for i, m in enumerate(last["messages"]):
        parts = []
        for k in ("content", "reasoning_content"):
            if m.get(k):
                parts.append("%s=%s" % (k, m[k]))
        if m.get("tool_calls"):
            parts.append("tool_calls=" + json.dumps([c["function"] for c in m["tool_calls"]], ensure_ascii=False))
        s = " ".join(parts).replace("\n", " | ")
        if not full and len(s) > 260:
            s = s[:260] + " …"
        print("[%d] %s: %s" % (i, m["role"], s))


def main():
    full = "--full" in sys.argv
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "en"))
        os.makedirs(os.path.join(d, "legacy"))
        en = run_session(os.path.join(d, "en"))
        legacy = run_session(os.path.join(d, "legacy"), legacy_until=1)
    for conv in ("a", "b"):
        show(en, conv, full)
        print()
    t = prompt_text(en).lower()
    bad = [w for w in IT_WORDS if w.lower() in t]
    with open(GOLDEN_030) as f:
        golden = f.read().split()
    same = digest(legacy) == golden
    print("engine requests: %d (new) / %d (legacy)" % (len(en), len(legacy)))
    print("Italian words in the prompts of the new conversations: %s" % (bad or "none"))
    print("legacy conversations byte-identical to 0.3.0: %s" % same)
    return 0 if not bad and same else 1


if __name__ == "__main__":
    sys.exit(main())
