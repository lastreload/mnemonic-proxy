"""Regressions for the 0.3.34 reconciliation, using only CPU fixtures."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from ctxproxy import autoconfig, forget
from ctxproxy.core import Store, rid_for
from tests.test_model_profiles import ProfileBase
from tests.test_proxy import history


class ProfileCurrentMainTests(ProfileBase):
    def directory(self, model):
        return Path(self.tmp.name) / 'models' / hashlib.sha256(model.encode()).hexdigest()[:20]

    def test_explicit_small_window_is_not_scaled_twice(self):
        self.build({'auto_config': False, 'profiles': {'alpha': {
            'window': 16384, 'window_override': True}}})
        self.send('alpha')
        child = self.proxy.contexts['alpha']
        self.assertEqual(child.cfg.mask_trigger, 10000)
        self.assertEqual(child.cfg.reserve, 1024)

    def test_automatic_choices_survive_refresh_and_restart(self):
        slots = str(Path(self.tmp.name) / 'slots-a')
        raw = {'profiles': {'alpha': {'slot_dir': slots}}}
        machine = autoconfig.Machine(ram_total_mb=32768, ram_free_mb=16384)
        with patch.object(autoconfig.Machine, 'read', return_value=machine):
            self.build(raw)
            first = self.send('alpha')['strata_context']['conversation_id']
            a = self.proxy.contexts['alpha']
            self.assertEqual(a.cfg.response_floor, 4096)
            self.assertTrue(a.cfg.slot_save)
            self.assertTrue(a.cfg.autosave)
            self.assertIsNotNone(a.autosaver)
            self.assertIsNotNone(self.proxy.autosaver)
            self.assertTrue((self.directory('alpha') / autoconfig.MARK_FILE).is_file())
            self.assertEqual(a.cfg.data_dir, str(self.directory('alpha')))
            self.send('alpha')
            self.assertEqual(a.cfg.response_floor, 4096)
            self.assertTrue(a.cfg.autosave)
            self.proxy.close()
            self.proxy = None
            self.build(raw)
            resumed = self.send('alpha')['strata_context']['conversation_id']
            self.assertEqual(first, resumed)
            self.assertEqual(self.proxy.contexts['alpha'].cfg.response_floor, 4096)
            self.assertTrue(self.proxy.contexts['alpha'].cfg.autosave)

    def test_earlier_profile_data_keeps_automatic_choices_off(self):
        directory = self.directory('alpha')
        directory.mkdir(parents=True)
        (directory/'old-install').write_text('existing')
        self.build({'profiles': {'alpha': {'slot_dir': str(directory/'slots')}}})
        self.send('alpha')
        a = self.proxy.contexts['alpha']
        self.assertFalse(a.cfg.slot_save)
        self.assertFalse(a.cfg.autosave)
        self.assertEqual(a.cfg.response_floor, 0)
        self.assertFalse((directory/autoconfig.MARK_FILE).exists())

    def test_explicit_profile_disables_win_over_auto_configuration(self):
        self.build({'profiles': {'alpha': {'slot_dir': str(self.directory('alpha')/'slots'),
                    'slot_save': False, 'mask_anchor': False, 'autosave': False,
                    'response_floor': 0, 'notes_max_tokens': 2048}}})
        self.send('alpha')
        a = self.proxy.contexts['alpha']
        self.assertEqual((a.cfg.slot_save, a.cfg.mask_anchor, a.cfg.autosave), (False, False, False))
        self.assertEqual((a.cfg.response_floor, a.cfg.notes_max_tokens), (0, 2048))

    def test_off_does_not_start_profile_saved_state_workers(self):
        self.build({'auto_config': False, 'autosave': True,
                    'profiles': {'alpha': {}, 'beta': {}}}, mode='off')
        self.send('alpha')
        self.send('beta')
        self.assertEqual(self.proxy.workers(), [])
        for child in self.proxy.contexts.values():
            self.assertEqual(child.store.db.execute('SELECT count(*) FROM archive').fetchone()[0], 0)

    def test_recall_cannot_read_other_profile_exclusive_record(self):
        self.build({'profiles': {'alpha': {}, 'beta': {}}})
        a_history = history(2)
        a_history[1] = {'role': 'user', 'content': 'ONLY_ALPHA_SECRET_Z91'}
        a_conv = self.send('alpha', a_history)['strata_context']['conversation_id']
        b_conv = self.send('beta', history(2))['strata_context']['conversation_id']
        rid = rid_for(1, a_history[1])
        a, b = self.proxy.contexts['alpha'], self.proxy.contexts['beta']
        self.assertIsNotNone(a.store.get(rid, a_conv))
        self.assertIsNone(b.store.get(rid, b_conv))
        result = b.mgr.recall(b_conv, {'id': rid})
        self.assertNotIn('ONLY_ALPHA_SECRET_Z91', json.dumps(result))

    def test_auto_disabled_after_refresh_never_ticks_saved_state(self):
        self.build({'auto_config': False, 'autosave': True, 'profiles': {'alpha': {}}})
        self.send('alpha')
        child = self.proxy.contexts['alpha']
        child.cfg.autosave = False
        child.cfg.autosave_poll_s = 0
        worker = self.proxy.autosaver
        worker.stop()
        worker.th.join()
        with patch.object(child.autosaver, 'tick') as tick, \
             patch.object(worker.stop_ev, 'wait', side_effect=[False, True]):
            worker._loop()
        tick.assert_not_called()

    def test_engine_selection_failure_keeps_previous_runtime_intact(self):
        self.build({'profiles': {'alpha': {}, 'unavailable-model': {}}})
        conv = self.send('alpha')['strata_context']['conversation_id']
        previous = self.proxy.contexts['alpha']
        status, body, _ = self.proxy.chat({'model': 'unavailable-model',
                                         'messages': [{'role': 'user', 'content': 'hello'}]})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.proxy.active_model, 'alpha')
        self.assertNotIn('unavailable-model', self.proxy.contexts)
        self.assertIs(self.proxy.contexts['alpha'], previous)
        self.assertEqual(conv, self.send('alpha')['strata_context']['conversation_id'])

    def test_forget_profiles_refuses_without_claiming_success_or_mutation(self):
        self.build({'profiles': {'alpha': {}, 'beta': {}}})
        conv = self.send('alpha')['strata_context']['conversation_id']
        self.proxy.close()
        self.proxy = None
        root = Path(self.tmp.name)
        Store(str(root/'archive.sqlite')).db.close()  # normal facade archive is empty
        before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        for suffix in (['--conv', conv], ['--conv', conv, '--dry-run'], ['--list']):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = forget.main(['--data', self.tmp.name, *suffix])
            self.assertEqual(status, 1, out.getvalue())
            self.assertIn('model-profile archives', err.getvalue())
            self.assertNotIn('Forgot ', out.getvalue())
        after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
