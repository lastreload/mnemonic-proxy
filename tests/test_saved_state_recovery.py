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


class CooldownTest(unittest.TestCase):
    def test_unknown_501_pauses_then_one_probe_resumes(self):
        rt = runtime(self, flavor="strata")
        rt.eng.slot_errors = [(501, "Not Implemented")]
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "a.bin")
        with self.assertRaises(SavedStateUnavailable):
            rt.up.slot("save", "b.bin")
        self.assertEqual(len(rt.eng.slots), 1)
        self.assertFalse(rt.up.saved_state.available())
        rt.clock.t += COOLDOWN_S + 1
        self.assertTrue(rt.up.saved_state.available())
        rt.up.slot("save", "c.bin")                    # the probe: the fake engine saves normally now
        self.assertEqual(len(rt.eng.slots), 2)
        self.assertEqual(len(events(rt, "saved_state_resumed")), 1)
        rt.up.slot("save", "d.bin")
        self.assertEqual(len(rt.eng.slots), 3)

    def test_failed_probe_rearms(self):
        rt = runtime(self, flavor="strata")
        rt.eng.slot_errors = [(501, "Not Implemented"), (501, "Not Implemented"), (404, "file not found")]
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "a.bin")
        for _ in range(2):                              # 501 again, then a non-refusal error: both re-arm
            rt.clock.t += COOLDOWN_S + 1
            with self.assertRaises(UpstreamError):
                rt.up.slot("save", "probe.bin")
            with self.assertRaises(SavedStateUnavailable):
                rt.up.slot("save", "blocked.bin")
        self.assertEqual(len(rt.eng.slots), 3)
        self.assertEqual(len(events(rt, "saved_state_probe_failed")), 2)
        self.assertEqual(len(events(rt, "saved_state_suspended")), 1)

    def test_fatal_reply_to_the_probe_makes_it_permanent(self):
        rt = runtime(self, flavor="strata")
        rt.eng.slot_errors = [(501, "Not Implemented"), (500, POLY_500)]
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "a.bin")
        rt.clock.t += COOLDOWN_S + 1
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "probe.bin")
        rt.clock.t += COOLDOWN_S * 10
        self.assertFalse(rt.up.saved_state.available())
        self.assertEqual(rt.up.saved_state.view()[0]["kind"], "fatal")

    def test_only_one_concurrent_probe(self):
        rt = runtime(self, flavor="strata")
        rt.eng.slot_errors = [(501, "Not Implemented")]
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "a.bin")
        rt.clock.t += COOLDOWN_S + 1
        gate, inside = threading.Event(), threading.Event()
        real_raw = rt.up.raw

        def slow_raw(method, path, *a, **k):
            if path.startswith("/slots/"):
                inside.set()
                gate.wait(10)
            return real_raw(method, path, *a, **k)

        rt.up.raw = slow_raw
        outcome = {}

        def run_probe():
            try:
                outcome["result"] = rt.up.slot("save", "probe.bin")
            except BaseException as e:            # reported to the main thread below
                outcome["error"] = e

        probe = threading.Thread(target=run_probe, daemon=True)
        probe.start()
        try:
            self.assertTrue(inside.wait(10), "the probe never reached the engine")
            with self.assertRaises(SavedStateUnavailable) as cm:   # the probe is in flight: everybody else waits
                rt.up.slot("save", "second.bin")
            self.assertIn("a retry is in progress", str(cm.exception))
        finally:
            gate.set()
            probe.join(10)
        self.assertFalse(probe.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(len(rt.eng.slots), 2)
        self.assertEqual(len(events(rt, "saved_state_resumed")), 1)

    def test_probe_ended_by_a_transport_error_rearms(self):
        rt = runtime(self, flavor="strata")
        rt.eng.slot_errors = [(501, "Not Implemented")]
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "a.bin")
        rt.clock.t += COOLDOWN_S + 1
        real_raw = rt.up.raw

        def timeout_raw(method, path, *a, **k):
            if path.startswith("/slots/"):
                raise TimeoutError("timed out")
            return real_raw(method, path, *a, **k)

        rt.up.raw = timeout_raw
        with self.assertRaises(TimeoutError):
            rt.up.slot("save", "probe.bin")
        rec = rt.up.saved_state.view()[0]
        self.assertFalse(rec["probing"])
        self.assertEqual((rec["kind"], rec["retry_after_s"]), ("unknown", int(COOLDOWN_S)))
        self.assertEqual(events(rt, "saved_state_probe_failed")[-1]["retry_after_s"], int(COOLDOWN_S))

    def test_permanent_reply_to_the_probe_is_reported_as_permanent(self):
        for status, message, kind in ((500, POLY_500, "fatal"),
                                      (501, "slot save/restore is disabled (start the server with --slot-save-path DIR)",
                                       "config")):
            with self.subTest(kind=kind):
                rt = runtime(self, flavor="strata")
                rt.eng.slot_errors = [(501, "Not Implemented"), (status, message)]
                with self.assertRaises(UpstreamError):
                    rt.up.slot("save", "a.bin")
                rt.clock.t += COOLDOWN_S + 1
                with self.assertRaises(UpstreamError):
                    rt.up.slot("save", "probe.bin")
                ev = events(rt, "saved_state_probe_failed")[-1]
                self.assertEqual((ev["kind"], ev["retry_after_s"]), (kind, None))
                rec = rt.up.saved_state.view()[0]
                self.assertEqual((rec["kind"], rec["retry_after_s"], rec["probing"]), (kind, None, False))

    def test_stale_probe_does_not_close_a_newer_record(self):
        rt = runtime(self, flavor="strata")
        st = rt.up.saved_state
        rt.eng.slot_errors = [(501, "Not Implemented")]
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "a.bin")
        rt.clock.t += COOLDOWN_S + 1
        old = st.require("save")                  # a probe opened by a direct caller
        self.assertIsNotNone(old)
        st.reset()
        rt.eng.slot_errors = [(501, "Not Implemented")]
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "b.bin")           # a new episode for the same model
        rt.clock.t += COOLDOWN_S + 1
        new = st.require("save")                  # the new episode's own probe is now in flight
        self.assertIsNotNone(new)
        self.assertNotEqual(old, new)
        st.end_probe(old, True)                   # the old probe finishes late: must be ignored
        rec = st.view()
        self.assertEqual(len(rec), 1, "the newer record must survive")
        self.assertTrue(rec[0]["probing"], "the newer probe must still be open")
        self.assertEqual(len(events(rt, "saved_state_resumed")), 0)
        st.end_probe(new, True)                   # the real probe closes it
        self.assertEqual(st.view(), [])
        self.assertEqual(len(events(rt, "saved_state_resumed")), 1)

    def test_router_probe(self):
        rt = runtime(self, flavor="strata")
        rt.up.router, rt.up.router_model = RouterStub(), "big"
        real_raw = rt.up.raw
        replies = [(501, {}, body("Not Implemented"))]

        seen = []

        def router_raw(method, path, *a, **k):
            if path.startswith("/slots/"):
                seen.append(path)
                if replies:
                    return replies.pop(0)
                # the fake Strata engine reads the action as the text after "action=": drop the router-only
                # parameter so that it really runs the SAVE (the router itself is not under test here)
                path = path.replace("&autoload=false", "")
            return real_raw(method, path, *a, **k)

        rt.up.raw = router_raw
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "a.bin")
        self.assertEqual(rt.up.saved_state.view()[0]["model"], "big")
        rt.clock.t += COOLDOWN_S + 1
        rt.up.slot("save", "b.bin")               # router branch, probe succeeds
        self.assertEqual(rt.up.saved_state.view(), [])
        self.assertEqual(rt.up.router.owners.saved, ["b.bin"])
        self.assertIn("&autoload=false", seen[-1], "the router branch must have been used")
        self.assertEqual(rt.eng.slots[-1][:2], ("save", "b.bin"), "the fake engine must have run a real SAVE")
        self.assertIn("b.bin", rt.eng.files, "the saved file must exist in the fake engine")

    def test_fatal_and_config_never_expire(self):
        for flavor, slot_save in (("strata-poly", True), ("strata", False)):
            with self.subTest(flavor=flavor, slot_save=slot_save):
                rt = runtime(self, flavor=flavor, slot_save=slot_save)
                with self.assertRaises(UpstreamError):
                    rt.up.slot("save", "a.bin")
                rt.clock.t += COOLDOWN_S * 100
                with self.assertRaises(SavedStateUnavailable) as cm:
                    rt.up.slot("save", "b.bin")
                self.assertIn("until proxy restart", str(cm.exception))
                self.assertEqual(len(rt.eng.slots), 1)
