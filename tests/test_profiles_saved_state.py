"""Integration (#47 + #45): saved-state suspension with model profiles on one shared engine."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from ctxproxy import engines
from ctxproxy.core import Config, Journal, Store, TokenCounter
from ctxproxy.fake_engine import FakeEngine
from ctxproxy.profiles import ProfileProxy, load_profiles
from ctxproxy.server import Proxy, make_handler
from ctxproxy.upstream import Upstream
from tests.test_model_profiles import TOOLS, managed_requests
from tests.test_proxy import history


def anchor_count(engine):
    """One-token calls that are anchors: managed_requests() already drops the #45 activation probes."""
    return sum(b.get("max_tokens") == 1 for b in managed_requests(engine))


class ProfilesSavedStateTest(unittest.TestCase):
    def test_tiel_suspended_flash_keeps_saving(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        eng = FakeEngine(flavor="strata-poly", models={"tiel": 32768, "flash": 32768})
        eng.session_unsupported_models = {"tiel"}
        url = eng.start()
        self.addCleanup(eng.stop)
        raw = {"mask_trigger": 1000, "mask_target": 500, "min_mask_tokens": 50, "keep_recent_tokens": 200,
               "min_age_turns": 1, "min_batch_tokens": 100, "mask_anchor": True, "anchor_min_tokens": 100,
               "slot_save": True, "segments_enabled": False, "seal_experimental": False,
               "profiles": {"tiel": {}, "flash": {}}}
        cfg = Config.from_dict(raw)
        up = Upstream(url)
        journal = Journal(os.path.join(tmp.name, "journal.jsonl"))
        counter = TokenCounter(3.5)
        legacy = Proxy(cfg, up, Store(os.path.join(tmp.name, "legacy.sqlite")), journal, counter, mode="tools")
        legacy.engine = engines.setup(cfg, up, journal, explicit=set(raw), counter=counter, log=None)
        specs, default = load_profiles(raw, cfg)
        proxy = ProfileProxy(legacy, SimpleNamespace(data=tmp.name), raw, specs, default)
        self.addCleanup(proxy.close)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(proxy))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = "http://127.0.0.1:%d" % srv.server_address[1]

        def http(method, path, payload=None):
            data = None if payload is None else json.dumps(payload).encode()
            req = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())

        def chat(model, turns):
            return http("POST", "/v1/chat/completions",
                        {"model": model, "messages": history(turns), "tools": TOOLS, "max_tokens": 128})

        def events(name):
            with open(os.path.join(tmp.name, "journal.jsonl"), encoding="utf-8") as f:
                return [e for e in map(json.loads, f) if e["event"] == name]

        # 1) tiel: masking with an anchor -> the first SAVE gets the strata-poly 500 -> suspended
        chat("tiel", 5)
        chat("tiel", 6)
        suspended = events("saved_state_suspended")
        self.assertEqual([e["model"] for e in suspended], ["tiel"])
        # 2) flash on the same engine keeps saving
        saves_before = len(eng.saved_by)
        chat("flash", 5)
        chat("flash", 6)
        self.assertGreater(len(eng.saved_by), saves_before, "flash must save successfully")
        self.assertTrue(all(owner == "flash" for owner in eng.saved_by.values()))
        # 3) tiel again: no slot call and no new anchor after the suspension
        for turns in (7, 8):
            slots, anchors = len(eng.slots), anchor_count(eng)
            chat("tiel", turns)
            self.assertEqual(len(eng.slots), slots, "no /slots call for a suspended model")
            self.assertEqual(anchor_count(eng), anchors, "no anchor for a suspended model")
        self.assertEqual(len(events("saved_state_suspended")), 1)
        # 4) view and reset go through the real ProfileProxy
        self.assertEqual([r["model"] for r in http("GET", "/v1/strata/saved-state")["suspended"]], ["tiel"])
        self.assertEqual(http("POST", "/v1/strata/saved-state/reset", {"model": "tiel"}), {"cleared": ["tiel"]})
        self.assertEqual(http("GET", "/v1/strata/saved-state"), {"suspended": []})
