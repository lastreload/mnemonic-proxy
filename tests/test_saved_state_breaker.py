"""CPU-only HTTP regressions for unsupported saved-state sessions (#28)."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from ctxproxy import check, engines
from ctxproxy.core import Config, Journal, Store, TokenCounter
from ctxproxy.fake_engine import FakeEngine
from ctxproxy.kvarchive import ArchivingUpstream
from ctxproxy.savedstate import SavedStateUnavailable, state_available, unsupported_reason
from ctxproxy.server import Proxy
from ctxproxy.upstream import Upstream, UpstreamError
from tests.test_proxy import TOOLS, history, tool_round


def make_runtime(case, flavor="strata-poly", slot_save=True, **over):
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    engine = FakeEngine(flavor=flavor, slot_save=slot_save)
    url = engine.start()
    raw = {"window": 131072, "mask_trigger": 30000, "mask_target": 15000,
           "keep_recent_tokens": 6000, "min_mask_tokens": 200, "slot_save": slot_save, **over}
    cfg = Config.from_dict(raw)
    journal = Journal(None)
    warnings = []
    up = Upstream(url)
    up.saved_state.bind(journal, warnings.append)
    proxy = Proxy(cfg, up, Store(os.path.join(tmp.name, "archive.sqlite")), journal, TokenCounter(3.5))

    def close():
        if proxy.autosaver is not None:
            proxy.autosaver.stop()
            if proxy.autosaver.th is not None:
                proxy.autosaver.th.join(timeout=2)
        if getattr(proxy, "archiver", None) is not None:
            proxy.archiver.stop()
            if proxy.archiver.th is not None:
                proxy.archiver.th.join(timeout=2)
        proxy.store.db.close()
        engine.stop()
        tmp.cleanup()

    case.addCleanup(close)
    return SimpleNamespace(tmp=tmp, eng=engine, up=up, cfg=cfg, journal=journal,
                           warnings=warnings, proxy=proxy)


def send(rt, messages, model="model-A", max_tokens=1024):
    status, response, _ = rt.proxy.chat({"model": model, "messages": messages, "tools": TOOLS,
                                        "max_tokens": max_tokens})
    if status != 200:
        raise AssertionError(response)
    return response


def events(rt, name):
    return [event for event in rt.journal.mem if event["event"] == name]


def drive_to_anchor_refusal(rt):
    rt.cfg.mask_anchor = True
    rt.cfg.slot_save = True
    rt.cfg.segments_enabled = False
    rt.cfg.anchor_min_tokens = 500
    rt.cfg.min_batch_tokens = 3000
    h = history(1)
    for k in range(1, 40):
        h += tool_round(k, 3500)
        send(rt, h)
        if not state_available(rt.up):
            return h
    raise AssertionError("no masking anchor reached the engine")


class SavedStateHTTPTests(unittest.TestCase):
    def test_poly_and_501_stop_slots_and_later_anchor_generation(self):
        for flavor, slot_save, expected_status in (("strata-poly", True, 500), ("strata", False, 501)):
            with self.subTest(flavor=flavor, slot_save=slot_save):
                rt = make_runtime(self, flavor=flavor, slot_save=slot_save)
                h = drive_to_anchor_refusal(rt)
                self.assertEqual(events(rt, "saved_state_suspended")[0]["status"], expected_status)
                slots = len(rt.eng.slots)
                anchors = sum(body.get("max_tokens") == 1 for body in rt.eng.requests)
                self.assertEqual(slots, 1)
                self.assertEqual(anchors, 1)  # the first SAVE/501 cannot prevent its own anchor prefill
                for k in range(40, 50):
                    h += tool_round(k, 3500)
                    send(rt, h)
                self.assertEqual(len(rt.eng.slots), slots)
                self.assertEqual(sum(body.get("max_tokens") == 1 for body in rt.eng.requests), anchors)
                self.assertEqual(len(events(rt, "saved_state_suspended")), 1)
                self.assertEqual(len(rt.warnings), 1)
                self.assertIn("until the proxy restarts", rt.warnings[0])
                self.assertIn("mask_anchor", rt.warnings[0])

    def test_fatal_save_is_local_after_first_http_reply(self):
        rt = make_runtime(self)
        rt.up.chat({"model": "model-A", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 4})
        with self.assertRaises(UpstreamError) as caught:
            rt.up.slot("save", "a.bin")
        self.assertEqual(caught.exception.status, 500)
        self.assertIn("the engine answered a session save", str(caught.exception))
        before = (len(rt.eng.slots), len(rt.eng.requests))
        for action in ("save", "restore", "save"):
            with self.assertRaises(SavedStateUnavailable):
                rt.up.slot(action, "later.bin")
        self.assertEqual((len(rt.eng.slots), len(rt.eng.requests)), before)
        self.assertEqual(len(events(rt, "saved_state_suspended")), 1)
        self.assertEqual(events(rt, "saved_state_suspended")[0]["model"], "model-A")

    def test_restore_404_does_not_suspend(self):
        rt = make_runtime(self)
        with self.assertRaises(UpstreamError) as caught:
            rt.up.slot("restore", "missing.bin")
        self.assertEqual(caught.exception.status, 404)
        self.assertTrue(state_available(rt.up))
        self.assertFalse(events(rt, "saved_state_suspended"))
        rt.up.chat({"model": "model-A", "messages": [{"role": "user", "content": "hello"}]})
        rt.eng.session_unsupported_models = set()  # same fake service, now with a supported concrete model
        self.assertIn("n_saved", rt.up.slot("save", "works.bin"))

    def test_temporary_and_generic_errors_do_not_suspend(self):
        rt = make_runtime(self, flavor="strata")
        rt.eng.slot_errors = [(503, "not enough memory"), (507, "insufficient storage"),
                              (500, "unrelated internal error"), (400, "invalid filename")]
        for expected in (503, 507, 500, 400):
            with self.assertRaises(UpstreamError) as caught:
                rt.up.slot("save", "temporary.bin")
            self.assertEqual(caught.exception.status, expected)
            self.assertTrue(state_available(rt.up))
        self.assertIn("n_saved", rt.up.slot("save", "temporary.bin"))
        self.assertFalse(events(rt, "saved_state_suspended"))
        self.assertIsNone(unsupported_reason(500, b'{"error":{"message":"disk controller failed"}}'))
        self.assertIsNone(unsupported_reason(0, b"timeout"))
        self.assertIsNone(unsupported_reason(400, b"the server ended the engine"))

    def test_supported_prompt_is_unchanged_and_slots_still_run(self):
        rt = make_runtime(self, flavor="strata")
        body = {"model": "model-A", "messages": [{"role": "system", "content": "exact"},
                                                 {"role": "user", "content": "same prompt"}],
                "tools": TOOLS, "max_tokens": 17, "temperature": 0}
        rt.up.chat(body)
        self.assertEqual(rt.eng.requests[-1], body)
        self.assertIn("n_saved", rt.up.slot("save", "state.bin"))
        self.assertIn("n_restored", rt.up.slot("restore", "state.bin"))
        self.assertEqual([entry[0] for entry in rt.eng.slots], ["save", "restore"])
        self.assertTrue(state_available(rt.up))

    def test_fatal_anchor_restore_prevents_anchor_generation_and_save(self):
        rt = make_runtime(self)
        phys = [{"role": "system", "content": "s"}, {"role": "user", "content": "old"},
                {"role": "assistant", "content": "a"}, {"role": "user", "content": "new"}]
        origin = [0, 1, 2, 3]
        rt.eng.files["old.bin"] = "saved"
        rt.proxy.store.set_anchor("conv", 0, 1, rt.proxy.mgr.prefix_hash(phys[:1], TOOLS, {"model": "model-A"}),
                                  "old.bin", 10)
        rt.up.saved_state.select("model-A")
        result = rt.proxy.mgr._anchor("conv", 0, {"model": "model-A"}, phys, origin, [1000] * 4, TOOLS,
                                      {"frontier_index": 2}, rt.up)
        self.assertTrue(any(event["event"] == "anchor_error" and event["step"] == "restore" for event in result))
        self.assertEqual([entry[0] for entry in rt.eng.slots], ["restore"])
        self.assertEqual(rt.eng.requests, [])
        self.assertFalse(state_available(rt.up))
        rt.proxy.mgr._anchor("conv", 0, {"model": "model-A"}, phys, origin, [1000] * 4, TOOLS,
                             {"frontier_index": 2}, rt.up)
        self.assertEqual(len(rt.eng.slots), 1)
        self.assertEqual(rt.eng.requests, [])

    def test_already_running_autosave_worker_stops_after_refusal(self):
        rt = make_runtime(self, autosave=True, autorestore=True, slot_save=True,
                          autosave_idle_s=0, autosave_poll_s=0.01, autosave_min_tokens=0,
                          autosave_min_free_gb=0)
        saver = rt.proxy.enable_autosave(start=True)
        send(rt, history(3))
        deadline = time.time() + 3
        while state_available(rt.up) and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(state_available(rt.up), "the running worker did not reach its first SAVE")
        count = len(rt.eng.slots)
        time.sleep(0.08)
        self.assertEqual(count, 1)
        self.assertEqual(len(rt.eng.slots), count)
        self.assertTrue(saver.th.is_alive())
        self.assertEqual(len(events(rt, "saved_state_suspended")), 1)

    def test_segment_seal_and_save_are_skipped_after_refusal(self):
        rt = make_runtime(self, window=40000, mask_enabled=False, tail_max=12000,
                          reserve=2000, default_response=2000, slot_save=True, seal_experimental=True)
        h = history(1) + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "continue"}]
        for k in range(1, 14):
            h += tool_round(k, 12000)
            send(rt, h, max_tokens=1000)
            if not state_available(rt.up):
                break
        self.assertFalse(state_available(rt.up))
        self.assertEqual(len(events(rt, "seal")), 1)
        self.assertEqual(len(rt.eng.slots), 1)
        anchors_or_seals = sum(body.get("max_tokens") == 1 for body in rt.eng.requests)
        for k in range(14, 30):
            h += tool_round(k, 12000)
            send(rt, h, max_tokens=1000)
            if len(events(rt, "switch")) >= 2:
                break
        self.assertGreaterEqual(len(events(rt, "switch")), 2)
        self.assertEqual(len(rt.eng.slots), 1)
        self.assertEqual(sum(body.get("max_tokens") == 1 for body in rt.eng.requests), anchors_or_seals)

    def test_autorestore_refusal_does_not_retry_on_next_request(self):
        rt = make_runtime(self, flavor="strata", autosave=True, autorestore=True, slot_save=True,
                          autosave_idle_s=180, autosave_min_tokens=0, autorestore_min_gain=0,
                          autosave_min_free_gb=0)
        saver = rt.proxy.enable_autosave(start=False)
        h = history(3) + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "next"}]
        send(rt, h)
        saved = saver.tick(now=time.time() + 200)
        self.assertIsNotNone(saved)
        rt.eng.flavor = "strata-poly"
        rt.eng.restart()
        requests = len(rt.eng.requests)
        h2 = h + [{"role": "assistant", "content": "answer"}, {"role": "user", "content": "continue"}]
        send(rt, h2)
        self.assertEqual([entry[0] for entry in rt.eng.slots], ["save", "restore"])
        self.assertEqual(len(rt.eng.requests), requests + 1)  # ordinary generation remains available
        self.assertFalse(state_available(rt.up))
        rt.eng.restart()
        requests = len(rt.eng.requests)
        send(rt, h2 + [{"role": "assistant", "content": "again"}, {"role": "user", "content": "more"}])
        self.assertEqual(len(rt.eng.slots), 2)
        self.assertEqual(len(rt.eng.requests), requests + 1)

    def test_suspended_restore_does_not_materialize_kv_archive(self):
        rt = make_runtime(self)
        rt.up.chat({"model": "model-A", "messages": [{"role": "user", "content": "hello"}]})
        with self.assertRaises(UpstreamError):
            rt.up.slot("save", "first.bin")
        slots = len(rt.eng.slots)

        class Archive:
            restored = 0

            def has(self, filename):
                return True

            def restore(self, filename, dest):
                self.restored += 1
                return {"bytes": 1, "ms": 1}

        arc = Archive()
        rt.cfg.slot_dir = rt.tmp.name
        wrapped = ArchivingUpstream(rt.up, rt.cfg, rt.journal, arc)
        with self.assertRaises(SavedStateUnavailable):
            wrapped.slot("restore", "archived.bin")
        self.assertEqual(arc.restored, 0)
        self.assertEqual(len(rt.eng.slots), slots)

    def test_two_concrete_models_on_one_upstream_are_isolated_without_profiles(self):
        rt = make_runtime(self)
        rt.eng.session_unsupported_models = {"model-A"}
        for model in ("model-A", "model-B"):
            rt.up.chat({"model": model, "messages": [{"role": "user", "content": model}]})
            if model == "model-A":
                with self.assertRaises(UpstreamError):
                    rt.up.slot("save", "a.bin")
            else:
                self.assertIn("n_saved", rt.up.slot("save", "b.bin"))
        self.assertFalse(state_available(rt.up, "model-A"))
        self.assertTrue(state_available(rt.up, "model-B"))
        slots = len(rt.eng.slots)
        rt.up.chat({"model": "model-A", "messages": [{"role": "user", "content": "A again"}]})
        with self.assertRaises(SavedStateUnavailable):
            rt.up.slot("save", "a-later.bin")
        self.assertEqual(len(rt.eng.slots), slots)
        self.assertEqual([event["model"] for event in events(rt, "saved_state_suspended")], ["model-A"])

    def test_profile_style_explicit_keys_are_compatible_with_separate_runtimes(self):
        rt = make_runtime(self)
        rt.eng.session_unsupported_models = {"concrete-A"}
        first = rt.up
        second = Upstream(rt.up.base)
        second.saved_state.bind(rt.journal, rt.warnings.append)
        first.profile_model = "concrete-A"
        second.profile_model = "concrete-B"
        first.chat({"model": "concrete-A", "messages": [{"role": "user", "content": "A"}]})
        with self.assertRaises(UpstreamError):
            first.slot("save", "a.bin")
        second.chat({"model": "concrete-B", "messages": [{"role": "user", "content": "B"}]})
        self.assertIn("n_saved", second.slot("save", "b.bin"))
        self.assertFalse(state_available(first))
        self.assertTrue(state_available(second))


class LiveCheckTests(unittest.TestCase):
    def make(self, flavor="strata"):
        engine = FakeEngine(flavor=flavor)
        up = Upstream(engine.start())
        self.addCleanup(engine.stop)
        detected = engines.detect(up)
        return engine, up, detected

    def test_all_six_options_false_never_send_save_or_restore(self):
        engine, up, detected = self.make()
        cfg = Config(**{name: False for name in engines.NEEDS_SLOTS})
        report = check.Report()
        check._live(report, up, detected, cfg)
        self.assertEqual(engine.slots, [])
        self.assertEqual(len(engine.requests), 2)  # generation and tool check only
        evidence = [item["evidence"] for item in report.items if item["check"] == "live: save/restore"]
        self.assertTrue(any("all six" in text and "no SAVE or RESTORE" in text for text in evidence), report.items)

    def test_default_autorestore_alone_does_not_enable_save(self):
        engine, up, detected = self.make()
        report = check.Report()
        check._live(report, up, detected, Config())
        self.assertEqual(engine.slots, [])
        evidence = next(item["evidence"] for item in report.items if item["check"] == "live: save/restore")
        self.assertIn("autorestore alone is inert", evidence)
        self.assertNotIn("all six", evidence)

    def test_strata_detection_has_no_save_or_invalid_action_probe(self):
        engine, up, detected = self.make("strata-poly")
        self.assertTrue(detected.slot_save)
        self.assertEqual(engine.slots, [])
        self.assertEqual(engine.requests, [])

    def test_poly_backup_refusal_is_clear_and_has_no_restore_retry(self):
        engine, up, detected = self.make("strata-poly")
        engine.live = "existing conversation"
        report = check.Report()
        check._live(report, up, detected, Config(slot_save=True))
        self.assertEqual([entry[0] for entry in engine.slots], ["save"])
        self.assertEqual(engine.requests, [])
        backup = next(item for item in report.items if item["check"] == "live: backup")
        self.assertEqual(backup["status"], "fail")
        self.assertIn("the engine answered a session save", backup["evidence"])

    def test_fatal_after_successful_backup_skips_final_restore(self):
        engine, up, detected = self.make("strata-poly")
        engine.session_unsupported_models = {"local-model"}
        engine.live = "existing conversation"
        report = check.Report()
        check._live(report, up, detected, Config(slot_save=True))
        self.assertEqual([entry[0] for entry in engine.slots], ["save", "save"])
        self.assertFalse(any(item["check"] == "live" and "put back" in item["evidence"] for item in report.items))
        self.assertTrue(any("no further RESTORE" in item["evidence"] for item in report.items), report.items)


if __name__ == "__main__":
    unittest.main()
