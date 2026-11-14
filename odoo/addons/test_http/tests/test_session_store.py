# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from werkzeug.wrappers import Response

from odoo.http import session as session_module
from odoo.http.session import (
    SESSION_DELETION_TIMER,
    STORED_SESSION_BYTES,
    SessionStore,
    get_default_session,
)
from odoo.tests import BaseCase

from odoo.addons.test_http.utils import MemorySessionStore


class SessionStoreContract:
    allow_inherited_tests_method = True

    def setUp(self):
        super().setUp()
        self.tmpdir = TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store = self.store_class(path=self.tmpdir.name)
        self.startPatcher(patch.object(session_module, 'update_session_token'))

    def new_session(self):
        session = self.store.new()
        session.update(get_default_session(), uid=42, create_time=time.time())
        self.assertTrue(self.store.save(session))
        return session

    def test_create_and_update(self):
        session = self.new_session()
        self.assertFalse(session.is_new)
        session['value'] = 'updated'
        self.assertTrue(self.store.save(session))
        self.assertEqual(self.store.get(session.sid)['value'], 'updated')

    def test_deleted_snapshot_cannot_be_saved(self):
        session = self.new_session()
        stale = self.store.get(session.sid)
        self.store.delete(session)
        for snapshot in (session, stale):
            self.assertFalse(self.store.save(snapshot))
        self.assertFalse(self.store.get(session.sid).uid)

    def test_unsaved_cookie_session_can_be_created(self):
        sid = self.store.generate_key()
        session = self.store.get(sid, keep_sid=True)
        self.assertTrue(session.is_new)
        self.assertFalse(session.uid)
        session['value'] = 'first update'
        self.assertTrue(self.store.save(session))
        self.assertFalse(session.is_new)
        self.store.delete(session)
        self.assertFalse(self.store.save(session))

    def test_unsaved_cookie_session_can_authenticate(self):
        sid = self.store.generate_key()
        session = self.store.get(sid, keep_sid=True)
        session.update(get_default_session(), uid=42)
        session.should_rotate = True
        self.assertTrue(self.store.rotate(session, object()))
        self.assertNotEqual(sid, session.sid)
        self.assertFalse(session.is_new)
        self.assertEqual(self.store.get(session.sid).uid, 42)

    def test_soft_rotations_reuse_successor(self):
        session = self.new_session()
        concurrent = self.store.get(session.sid)
        self.store.rotate(session, object(), soft=True)
        self.assertTrue(self.store.rotate(concurrent, object(), soft=True))
        self.assertEqual(concurrent.sid, session.sid)
        self.assertEqual(self.store.get(session.sid).uid, 42)

    def test_late_save_preserves_rotation_metadata(self):
        session = self.new_session()
        stale = self.store.get(session.sid)
        old_sid = session.sid
        self.store.rotate(session, object(), soft=True)
        stale['counter'] = 1
        self.assertTrue(self.store.save(stale))
        self.assertEqual(stale.sid, session.sid)
        self.assertEqual(self.store.get(old_sid)['next_sid'], session.sid)
        self.assertIn('deletion_time', self.store.get(old_sid))

    def test_predecessor_cleanup_cannot_delete_successor(self):
        session = self.new_session()
        self.store.rotate(session, object(), soft=True)
        session['create_time'] -= SESSION_DELETION_TIMER + 1
        self.store.save(session)
        stale = self.store.get(session.sid)
        self.store.rotate(session, object(), soft=True)
        self.store.delete_old_sessions(stale)
        self.assertEqual(self.store.get(session.sid).uid, 42)

    def test_failed_rotation_cannot_be_retried_as_new_session(self):
        session = self.new_session()
        native_save = self.store._save
        calls = 0

        def fail_successor(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                message = 'injected storage failure'
                raise OSError(message)
            native_save(value)

        with patch.object(self.store, '_save', side_effect=fail_successor), self.assertRaises(OSError):
            self.store.rotate(session, object(), soft=True)
        self.assertFalse(session.can_save)
        self.assertFalse(self.store.save(session))
        self.assertFalse(self.store.rotate(session, object(), soft=True))
        self.assertFalse(self.store._exists(session.sid))

    def test_pending_logout_cannot_adopt_authenticated_successor(self):
        session = self.new_session()
        stale = self.store.get(session.sid)
        self.store.rotate(session, object(), soft=True)
        stale.uid = None
        stale.should_rotate = True
        self.assertFalse(self.store.save(stale))
        self.assertFalse(stale.uid)

    def test_revoke_all_rotated_sessions(self):
        session = self.new_session()
        stale = self.store.get(session.sid)
        other = self.new_session()
        original_sid = session.sid
        self.assertTrue(self.store.rotate(session, object(), soft=True))
        self.assertNotEqual(original_sid, session.sid)
        self.assertFalse(session.is_new)
        self.store.delete(stale)
        self.assertFalse(self.store.get(original_sid).uid)
        self.assertFalse(self.store.get(session.sid).uid)
        self.assertFalse(self.store.save(stale))
        self.assertFalse(self.store.save(session))
        self.assertEqual(self.store.get(other.sid).uid, 42)

    def test_revoke_before_rotation(self):
        session = self.new_session()
        for soft in (True, False):
            with self.subTest(soft=soft):
                stale = self.store.get(session.sid)
                self.store.delete(session)
                self.assertFalse(self.store.rotate(stale, object(), soft=soft))
                self.assertFalse(self.store.get(stale.sid).uid)
                session = self.new_session()

    def test_cleanup_preserves_current_session(self):
        session = self.new_session()
        stale = self.store.get(session.sid)
        self.store.rotate(session, object(), soft=True)
        session['create_time'] -= SESSION_DELETION_TIMER + 1
        self.store.save(session)
        self.store.delete_old_sessions(session)
        self.assertFalse(self.store._exists(stale.sid))
        self.assertEqual(self.store.get(session.sid).uid, 42)
        self.assertNotIn('gc_previous_sessions', self.store.get(session.sid))
        self.assertFalse(self.store.save(stale))

    def test_cleanup_cannot_restore_revoked_session(self):
        session = self.new_session()
        self.store.rotate(session, object(), soft=True)
        session['create_time'] -= SESSION_DELETION_TIMER + 1
        self.store.delete_from_identifiers([session.sid[:STORED_SESSION_BYTES]])
        self.store.delete_old_sessions(session)
        self.assertFalse(self.store.get(session.sid).uid)

    def test_logout_using_pruned_predecessor_revokes_successor(self):
        session = self.new_session()
        stale = self.store.get(session.sid)
        self.store.rotate(session, object(), soft=True)
        session['create_time'] -= SESSION_DELETION_TIMER + 1
        self.store.save(session)
        self.store.delete_old_sessions(session)
        stale.clear()
        stale.update(get_default_session())
        self.assertTrue(self.store.rotate(stale, object()))
        self.assertFalse(self.store.get(session.sid).uid)

    def test_late_save_cannot_publish_old_cookie(self):
        session = self.new_session()
        old_sid = session.sid
        stale = self.store.get(old_sid)
        stale['counter'] = 1
        self.store.delete(session)
        response = Response()
        request = SimpleNamespace(
            session=stale, cookies={'session_id': old_sid},
            httprequest=SimpleNamespace(path='/my/counters', headers={}),
            future_response=response,
        )
        with patch.object(session_module, 'session_store', return_value=self.store), \
             patch.object(session_module, 'get_session_max_inactivity', return_value=3600):
            session_module.save_session(request, object())
        self.assertFalse(stale.uid)
        self.assertNotEqual(stale.sid, old_sid)
        self.assertNotIn(old_sid, response.headers['Set-Cookie'])

    def assert_revoke_waits_for_write(self, *, rotate):
        session = self.new_session()
        stale = self.store.get(session.sid)
        entered = threading.Event()
        release = threading.Event()
        revoked = threading.Event()
        native_save = self.store._save

        def held_save(value):
            entered.set()
            if not release.wait(5):
                message = 'test did not release session write'
                raise TimeoutError(message)
            native_save(value)

        def revoke():
            self.store.delete(stale)
            revoked.set()

        with ThreadPoolExecutor(2) as pool:
            with patch.object(self.store, '_save', side_effect=held_save):
                writer = pool.submit(self.store.rotate, session, object(), soft=True) if rotate else pool.submit(self.store.save, session)
                try:
                    self.assertTrue(entered.wait(5))
                    revoker = pool.submit(revoke)
                    self.assertFalse(revoked.wait(0.05))
                finally:
                    release.set()
                self.assertTrue(writer.result(timeout=5))
                revoker.result(timeout=5)
        self.assertFalse(self.store.get(stale.sid).uid)
        self.assertFalse(self.store.get(session.sid).uid)

    def test_revoke_serializes_with_save(self):
        self.assert_revoke_waits_for_write(rotate=False)

    def test_revoke_serializes_with_rotation(self):
        self.assert_revoke_waits_for_write(rotate=True)


class TestFileSessionStore(SessionStoreContract, BaseCase):
    store_class = SessionStore

    def test_separate_worker_cannot_restore_deleted_session(self):
        session = self.new_session()
        code = '''
import json, sys
from odoo.http.session import Session, SessionStore
store = SessionStore(path=sys.argv[1])
snapshot = Session(json.loads(sys.argv[3]), sys.argv[2])
print('ready', flush=True)
print(store.save(snapshot), flush=True)
'''
        with self.store._lock(session.sid[:STORED_SESSION_BYTES]):
            process = subprocess.Popen(
                [sys.executable, '-c', code, self.tmpdir.name, session.sid, json.dumps(dict(session))],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.addCleanup(lambda: process.poll() is None and process.kill())
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.store._delete_from_identifier(session.sid[:STORED_SESSION_BYTES])
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), 'False')
        self.assertFalse(self.store.get(session.sid).uid)

    def test_legacy_backend_fails_explicitly(self):
        class LegacyStore(SessionStore):
            def save(self, session):
                pass

        with self.assertRaisesRegex(TypeError, 'storage hooks'):
            LegacyStore(path=self.tmpdir.name)


class TestMemorySessionStore(SessionStoreContract, BaseCase):
    store_class = MemorySessionStore
