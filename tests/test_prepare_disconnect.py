"""Offline regression: a disconnected prepare worker must remain inside Proxy.chat's lock."""
from __future__ import annotations

import copy
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from ctxproxy import server
from ctxproxy.core import Config, Journal, Store, TokenCounter
from ctxproxy.server import ClientGone, Proxy
from ctxproxy.upstream import Upstream


WAIT = 5  # Test watchdog only; no production deadline or sleep-based scheduling.


class ManagerStopped(RuntimeError):
    """Stop real chat immediately after the manager seam; no engine call is needed."""


class TestPrepareDisconnect(unittest.TestCase):
    def make_proxy(self):
        store = Store(':memory:')
        self.addCleanup(store.db.close)
        upstream = Upstream('http://127.0.0.1:1')
        # No operation may reach an engine in these tests.
        reject = mock.patch.object(upstream, 'raw', side_effect=AssertionError('unexpected HTTP'))
        reject.start()
        self.addCleanup(reject.stop)
        return Proxy(Config(), upstream, store, Journal(None), TokenCounter())

    def test_disconnect_keeps_second_chat_out_until_prepare_finishes(self):
        proxy = self.make_proxy()
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        reacted = threading.Event()
        attempted = threading.Event()
        second_entered = threading.Event()
        workers = []
        callers = []
        outcomes = {}
        observations = {}
        active = 0
        peak = 0
        state_lock = threading.Lock()
        disconnected = ClientGone('writer disconnected')
        stopped = ManagerStopped('manager complete')
        req_a = {'model': 'a', 'messages': [{'role': 'user', 'content': 'A'}], 'stream': True}
        req_b = {'model': 'b', 'messages': [{'role': 'user', 'content': 'B'}]}
        original = copy.deepcopy([req_a, req_b])

        class Manager:
            def prepare(self, req, upstream=None, hint=None):
                nonlocal active, peak
                with state_lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    if req is req_a:
                        entered.set()
                        if not release.wait(WAIT):
                            raise AssertionError('first manager not released by test')
                    else:
                        observations['second_entered_before_first_finished'] = not finished.is_set()
                        second_entered.set()
                    raise stopped
                finally:
                    with state_lock:
                        active -= 1
                    if req is req_a:
                        finished.set()

        class ObserveThread(threading.Thread):
            # Observe a real cleanup wait, without shortening the 2s keepalive join.
            def join(self, timeout=None):
                if timeout is None and self.is_alive():
                    reacted.set()
                return super().join(timeout)

        def worker_factory(*args, **kwargs):
            t = ObserveThread(*args, **kwargs)
            workers.append(t)
            return t

        class ObserveLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if threading.current_thread().name == 'prepare-test-B':
                    got = self.lock.acquire(blocking=False)
                    observations['second_acquired_while_first_alive'] = got
                    attempted.set()
                    if not got:
                        self.lock.acquire()
                else:
                    self.lock.acquire()
                return self

            def __exit__(self, *exc):
                self.lock.release()

        def emit(chunk):
            self.assertIsNone(chunk)
            observations['keepalives'] = observations.get('keepalives', 0) + 1
            raise disconnected

        def call(label, req, writer=None):
            try:
                outcomes[label] = proxy.chat(req, emit=writer)
            except BaseException as exc:
                outcomes[label] = exc
            finally:
                if label == 'A':
                    reacted.set()  # Original code returns while its worker is still blocked.

        with mock.patch.object(proxy, 'mgr', Manager()), \
                mock.patch.object(proxy, 'lock', ObserveLock()), \
                mock.patch.object(server, 'threading', SimpleNamespace(Thread=worker_factory)):
            try:
                a = threading.Thread(target=call, args=('A', req_a, emit), name='prepare-test-A')
                callers.append(a)
                a.start()
                self.assertTrue(entered.wait(WAIT), 'first prepare never entered')
                self.assertTrue(reacted.wait(WAIT), 'caller neither returned nor awaited its worker')
                self.assertEqual(len(workers), 1)
                observations['first_worker_alive_at_second_attempt'] = workers[0].is_alive()
                self.assertTrue(observations['first_worker_alive_at_second_attempt'])
                b = threading.Thread(target=call, args=('B', req_b), name='prepare-test-B')
                callers.append(b)
                b.start()
                self.assertTrue(attempted.wait(WAIT), 'second chat never attempted the lock')
                if observations['second_acquired_while_first_alive']:
                    # Preserve a positive overlap witness on RED, not a timed absence.
                    self.assertTrue(second_entered.wait(WAIT), 'second manager did not enter')
            finally:
                release.set()
                for t in callers + workers:
                    if t.ident is not None:
                        t.join(WAIT)
                for t in callers + workers:
                    self.assertFalse(t.is_alive(), 'test left a live thread: ' + t.name)

        self.assertIs(outcomes['A'], disconnected)
        self.assertIs(outcomes['B'], stopped)
        self.assertEqual(observations['keepalives'], 1)
        self.assertEqual([req_a, req_b], original)
        self.assertEqual(active, 0)
        self.assertFalse(observations['second_acquired_while_first_alive'], str(observations))
        self.assertFalse(observations['second_entered_before_first_finished'], str(observations))
        self.assertEqual(peak, 1, str(observations))


    def prepare_case(self, stream, manager_error=None, writer_error=None):
        """Exercise real _prepare lifecycle; only the manager and writer are stubs."""
        proxy = self.make_proxy()
        req = {'model': 'm', 'messages': [{'role': 'user', 'content': 'unchanged'}],
               'stream': stream, 'chat_template_kwargs': {'enable_thinking': False}}
        original = copy.deepcopy(req)
        release = threading.Event()
        finished = threading.Event()
        workers = []
        calls = []
        keepalives = []
        result = object()
        received = None
        error = None
        settled_at_return = False
        # Gate success/failed-writer cases until a real keepalive is emitted.
        gated = stream and manager_error is None

        def prepare(body, upstream=None, hint=None):
            calls.append((body is req, upstream is proxy.up, hint, threading.current_thread()))
            try:
                if gated and not release.wait(WAIT):
                    raise AssertionError('manager gate was not released')
                if manager_error is not None:
                    raise manager_error
                return result
            finally:
                finished.set()

        class PrepareThread(threading.Thread):
            def join(self, timeout=None):
                # Complete the blocked manager only once its owner really waits.
                # Timed keepalive joins retain their original 2s interval.
                if timeout is None:
                    release.set()
                return super().join(timeout)

        def worker_factory(*args, **kwargs):
            t = PrepareThread(*args, **kwargs)
            workers.append(t)
            return t

        def emit(chunk):
            self.assertIsNone(chunk)
            keepalives.append(chunk)
            if writer_error is not None:
                raise writer_error
            release.set()

        with mock.patch.object(proxy.mgr, 'prepare', side_effect=prepare), \
                mock.patch.object(server, 'threading', SimpleNamespace(Thread=worker_factory)):
            try:
                try:
                    received = proxy._prepare(req, 'stable-hint', emit if stream else None)
                except BaseException as exc:
                    error = exc
                settled_at_return = finished.is_set() and all(not t.is_alive() for t in workers)
            finally:
                release.set()
                for t in workers:
                    t.join(WAIT)
                    self.assertFalse(t.is_alive(), 'residual prepare thread')

        self.assertEqual(req, original)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], (True, True, 'stable-hint'))
        self.assertEqual(len(workers), int(stream))
        self.assertIs(calls[0][3], workers[0] if stream else threading.current_thread())
        self.assertTrue(settled_at_return, 'prepare returned before its manager worker settled')
        self.assertEqual(len(keepalives), int(gated))
        expected_error = writer_error if writer_error is not None else manager_error
        self.assertIs(error, expected_error)
        if expected_error is None:
            self.assertIs(received, result)
        self.assertTrue(proxy.lock.acquire(blocking=False))
        proxy.lock.release()

    def test_success_stream_and_nonstream_preserve_result_and_input(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.prepare_case(stream)

    def test_manager_exception_stream_and_nonstream_is_preserved(self):
        for stream in (False, True):
            for error in (RuntimeError('manager failed'), SystemExit('manager base exception')):
                with self.subTest(stream=stream, error=type(error).__name__):
                    self.prepare_case(stream, manager_error=error)

    def test_disconnected_writer_waits_for_successful_manager(self):
        self.prepare_case(True, writer_error=ClientGone('client left'))

    def test_writer_error_waits_for_successful_manager(self):
        self.prepare_case(True, writer_error=OSError('writer failed'))

    def test_writer_base_exception_waits_for_successful_manager(self):
        self.prepare_case(True, writer_error=KeyboardInterrupt('writer interrupted'))


if __name__ == '__main__':
    unittest.main()
