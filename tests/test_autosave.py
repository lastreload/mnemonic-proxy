"""Test autosalvataggio / ripristino automatico.   python3 -m unittest -v tests.test_autosave"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.test_proxy import TOOLS, Base, history, tool_round  # noqa: E402


class AutoBase(Base):
    cfg_over = {"autosave": True, "autosave_idle_s": 180, "autosave_min_tokens": 2000, "autorestore_min_gain": 1000,
                "slot_save": True}

    def setUp(self):
        super().setUp()
        self.slot_dir = os.path.join(self.tmp.name, "sessions")
        os.makedirs(self.slot_dir)
        self.proxy.cfg.slot_dir = self.slot_dir
        self.proxy.cfg.autosave_min_free_gb = 0
        self.saver = self.proxy.enable_autosave(start=False)

    def later(self, s=200):
        return time.time() + s

    def tick(self, s=200):
        ev = self.saver.tick(now=self.later(s))
        if ev:   # il motore finto non scrive file: li simula nella cartella degli slot per la pulizia
            with open(os.path.join(self.slot_dir, ev["file"]), "wb") as f:
                f.write(b"x" * 16)
        return ev

    def conv(self, n=6, first="crea il gioco snake"):
        return history(n, first=first) + [{"role": "assistant", "content": "fatto"},
                                          {"role": "user", "content": "e adesso?"}]

    def restart_proxy(self):
        """Nuovo processo proxy: stesso archivio SQLite e giornale, stesso motore."""
        cfg = self.proxy.cfg
        self.proxy.store.db.close()
        self.proxy = Proxy(Config.from_dict(cfg.__dict__), Upstream(self.url),
                           Store(os.path.join(self.tmp.name, "a.sqlite")), Journal(self.jpath), TokenCounter(3.5))
        self.saver = self.proxy.enable_autosave(start=False)


class TestAutosave(AutoBase):
    def test_save_after_idle_once(self):
        h = self.conv()
        self.send(h)
        self.assertIsNone(self.saver.tick(now=time.time() + 10), "conversazione non ancora ferma")
        ev = self.tick()
        self.assertIsNotNone(ev)
        self.assertEqual(self.eng.slots[-1][0], "save")
        self.assertEqual(self.eng.slots[-1][1], ev["file"])
        self.assertIn("-seg0-auto-", ev["file"])
        rows = self.proxy.store.autosaves()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file"], ev["file"])
        self.assertGreater(rows[0]["tokens"], 2000)
        self.assertTrue(rows[0]["phash"])
        self.assertIsNone(self.tick(), "stesso stato: nessun secondo salvataggio")
        self.assertEqual(len(self.events("autosave")), 1)

    def test_never_during_request(self):
        self.send(self.conv())
        self.proxy.lock.acquire()
        try:
            self.assertIsNone(self.tick(), "richiesta in corso nel proxy")
        finally:
            self.proxy.lock.release()
        self.eng.busy = True
        self.assertIsNone(self.tick(), "Strata occupato (in_flight)")
        self.eng.busy = False
        self.assertIsNotNone(self.tick())

    def test_small_conversation_not_saved(self):
        self.send(history(0) + [{"role": "user", "content": "ciao"}])
        self.assertIsNone(self.tick())

    def test_foreign_request_invalidates(self):
        self.send(self.conv())
        Upstream(self.url).chat({"messages": [{"role": "user", "content": "altro client"}]})
        self.assertIsNone(self.tick(), "Strata contiene la richiesta di un altro client: non salvare")
        self.assertEqual(self.events("strata_state_lost")[-1]["reason"], "foreign_request")


class TestAutorestore(AutoBase):
    def test_restore_after_strata_restart(self):
        h = self.conv()
        self.send(h)
        ev = self.tick()
        self.eng.restart()
        h2 = h + [{"role": "assistant", "content": "risposta"}, {"role": "user", "content": "continua"}]
        n_slots = len(self.eng.slots)
        r, _ = self.send(h2)
        self.assertEqual(self.eng.slots[n_slots][:2], ("restore", ev["file"]))
        ar = self.events("autorestore")
        self.assertEqual(len(ar), 1)
        self.assertEqual(ar[0]["reason"], "strata_restart")
        req = self.events("request")[-1]
        self.assertGreater(req["reused"], 0.9 * ev["tokens"], "dopo il restore Strata riusa il prefisso salvato")
        self.assertLess(req["prompt_read"], 500)

    def test_restore_after_other_conversation(self):
        a = self.conv(first="progetto A")
        self.send(a)
        ev = self.tick()
        b = self.conv(first="progetto B")
        self.send(b)
        self.assertFalse(self.events("autorestore"), "B è nuova: niente da ripristinare")
        a2 = a + [{"role": "assistant", "content": "ok A"}, {"role": "user", "content": "torno su A"}]
        self.send(a2)
        ar = self.events("autorestore")
        self.assertEqual(len(ar), 1)
        self.assertEqual(ar[0]["file"], ev["file"])
        self.assertEqual(ar[0]["reason"], "other_state")
        self.assertGreater(self.events("request")[-1]["reused"], 0.9 * ev["tokens"])

    def test_no_restore_when_strata_already_has_it(self):
        h = self.conv()
        self.send(h)
        self.tick()
        self.send(h + [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}])
        self.assertFalse(self.events("autorestore"))

    def test_no_restore_for_edited_history(self):
        """Il salvataggio vale solo se è prefisso ESATTO: storia modificata prima della fine -> niente restore."""
        h = self.conv()
        self.send(h)
        self.tick()
        self.eng.restart()
        h2 = [dict(m) for m in h]
        h2[-1]["content"] = "domanda diversa"
        h2[3]["content"] = "uscita cambiata"
        self.send(h2)
        self.assertFalse(self.events("autorestore"))

    def test_missing_file_is_forgotten(self):
        h = self.conv()
        self.send(h)
        ev = self.tick()
        del self.eng.files[ev["file"]]
        self.eng.restart()
        self.send(h + [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}])
        self.assertTrue(self.events("autorestore_error"))
        self.assertEqual(self.proxy.store.autosaves(), [])


class TestPrune(AutoBase):
    def test_keep_k_and_supersede(self):
        self.proxy.cfg.autosave_keep = 2
        files = []
        for name in ("A", "B", "C"):
            self.send(self.conv(first="progetto " + name))
            files.append(self.tick()["file"])
        active = [r["file"] for r in self.proxy.store.autosaves()]
        self.assertEqual(sorted(active), sorted(files[1:]))
        self.assertFalse(os.path.exists(os.path.join(self.slot_dir, files[0])))
        self.assertTrue(os.path.exists(os.path.join(self.slot_dir, files[2])))
        # stessa conversazione più avanti: il file nuovo sostituisce il vecchio
        c = self.conv(first="progetto C") + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "+"}]
        self.send(c)
        f4 = self.tick()["file"]
        active = [r["file"] for r in self.proxy.store.autosaves()]
        self.assertIn(f4, active)
        self.assertNotIn(files[2], active)
        pr = self.events("autosave_prune")
        self.assertIn("superseded", [p["reason"] for p in pr])
        self.assertIn("limit", [p["reason"] for p in pr])
        # testo e archivio restano
        self.assertGreater(self.proxy.store.stats(self.events("autosave")[0]["conv"])["archived"], 0)

    def test_disk_cap(self):
        self.proxy.cfg.autosave_max_gb = 0    # tetto 0: tiene solo l'ultimo appena salvato
        for name in ("A", "B"):
            self.send(self.conv(first="progetto " + name))
            self.tick()
        self.assertEqual(len(self.proxy.store.autosaves()), 1)


class TestProxyRestart(AutoBase):
    cfg_over = {**AutoBase.cfg_over, "mask_anchor": True, "anchor_min_tokens": 500, "min_batch_tokens": 3000,
                "segments_enabled": False}

    def test_same_physical_prompt_after_proxy_and_strata_restart(self):
        h = history(1)
        for k in range(1, 30):
            h += tool_round(k, 3500)
            self.send(h)
        self.assertTrue(self.events("mask"), "servono mask per provare la stabilità delle decisioni")
        self.send(h)
        before = self.eng.prompts[-1]
        ev = self.tick()
        self.assertIsNotNone(ev)
        # 1) riavvio del solo proxy: Strata intatto -> nessun restore, prompt fisico identico, tutto in cache
        self.restart_proxy()
        self.send(h)
        self.assertEqual(self.eng.prompts[-1], before)
        self.assertFalse(self.events("autorestore"))
        self.assertLess(self.events("request")[-1]["prompt_read"], 50)
        # 2) riavvio di proxy e Strata -> restore del salvataggio e stesso prompt fisico
        self.restart_proxy()
        self.eng.restart()
        self.send(h)
        self.assertEqual(self.eng.prompts[-1], before)
        self.assertEqual(len(self.events("autorestore")), 1)
        self.assertLess(self.events("request")[-1]["prompt_read"], 50)


if __name__ == "__main__":
    unittest.main()
