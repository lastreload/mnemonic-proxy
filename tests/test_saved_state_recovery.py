"""CPU-only tests for recoverable saved-state suspension (#46, follow-up of #47)."""
from __future__ import annotations

import json
import threading
import time
import unittest

from ctxproxy.savedstate import COOLDOWN_S, SavedStateUnavailable, classify
from ctxproxy.upstream import UpstreamError
from tests.test_saved_state_breaker import events, make_runtime, send
from tests.test_proxy import history

POLY_500 = ("the engine answered a session save with 'ERR expected: GEN <max_new> <id,id,...>'; "
            "the server ended the engine; the next request starts the engine again")


def body(message: str) -> bytes:
    return json.dumps({"error": {"message": message}}).encode()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class RouterStub:
    """Just enough of the llama.cpp router for Upstream._router_slot (owners.record, restore_refusal)."""

    def __init__(self):
        self.owners = type("Owners", (), {})()
        self.owners.saved = []
        self.owners.record = lambda filename, model: self.owners.saved.append(filename)

    def restore_refusal(self, filename):
        return None


def runtime(case, **kw):
    rt = make_runtime(case, **kw)
    rt.clock = Clock()
    rt.up.saved_state.clock = rt.clock
    rt.up.saved_state.select("model-A")   # direct rt.up.slot() calls use the same key as send()
    return rt


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        cases = [
            (501, "slot save/restore is disabled (start the server with --slot-save-path DIR)", "config"),
            (501, 'slot save/restore is not available with parallel requests ("parallel" / --batch)', "config"),
            (501, "slot save/restore is not supported by this model's engine (strata-poly)", "config"),
            (501, "Not Implemented", "unknown"),
            (500, POLY_500, "fatal"),
        ]
        for status, message, kind in cases:
            with self.subTest(message=message):
                self.assertEqual(classify(status, body(message))[0], kind)
        for status, message in ((500, "boom"), (404, "file not found"), (503, "loading"), (409, "owner")):
            with self.subTest(status=status):
                self.assertIsNone(classify(status, body(message)))
