import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

from Client import backup_engine
from test_server import server


class FlaskSession:
    def __init__(self, app, recorder=None):
        self.client = app.test_client()
        self.recorder = recorder
        self.closed = False

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        if self.recorder:
            self.recorder(method, urlsplit(url).path, kwargs)
        response = self.client.open(urlsplit(url).path, method=method, headers=headers,
                                    json=kwargs.get('json'), data=kwargs.get('data'))
        return SimpleNamespace(status_code=response.status_code, json=response.get_json)

    def close(self):
        self.closed = True


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='fcs-backup-integration-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / 'Photos'
        self.source.mkdir()
        self.destination = self.base / 'backup'
        self.app = server.create_app({'backup_folder': str(self.destination),
                                      'secret_token': 'test', 'min_free_bytes': 0})
        self.store = self.app.extensions['backup_store']
        self.reindex()
        self.config = {'folders': [str(self.source)], 'server_ip': 'unused.invalid',
                       'server_port': 5001, 'secret_token': 'test', 'parallel_transfers': 2,
                       'chunk_size': 3, 'hash_cache_file': str(self.base / 'hashes.sqlite3')}
        self.logs = []
        self.sessions = []

    def reindex(self):
        self.store.start_reindex()
        self.store.index_thread.join(10)
        self.assertIsNone(self.store.reindex_status()['error'])

    def engine(self, recorder=None):
        def factory():
            session = FlaskSession(self.app, recorder)
            self.sessions.append(session)
            return session
        return backup_engine.BackupEngine(self.config, self.logs.append, session_factory=factory)

    def write(self, name, data):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.stat().st_mtime_ns if path.exists() else 1000000000
        path.write_bytes(data)
        os.utime(path, ns=(before + 1000000000, before + 1000000000))
        return path

    def test_second_backup_does_not_hash_or_transfer_unchanged_files(self):
        self.write('one.bin', b'abcdef')
        self.write('two.bin', b'uvwxyz')
        self.assertTrue(self.engine().run_backup(), self.logs)
        engine = self.engine()
        engine._hash_file = Mock(side_effect=AssertionError('Unchanged file reread'))
        engine.upload_file = Mock(side_effect=AssertionError('Unchanged file resent'))
        self.assertTrue(engine.run_backup(), self.logs)
        engine._hash_file.assert_not_called()
        engine.upload_file.assert_not_called()
        self.assertTrue(all(session.closed for session in self.sessions))

    def test_changed_same_size_file_is_updated_and_timestamp_only_is_not_resent(self):
        self.write('one.bin', b'AAAA')
        self.assertTrue(self.engine().run_backup(), self.logs)
        self.write('one.bin', b'BBBB')
        self.assertTrue(self.engine().run_backup(), self.logs)
        self.assertEqual((self.destination / 'Photos/one.bin').read_bytes(), b'BBBB')
        self.write('one.bin', b'BBBB')
        engine = self.engine()
        engine.upload_file = Mock(side_effect=AssertionError('Timestamp-only change resent'))
        self.assertTrue(engine.run_backup(), self.logs)
        engine.upload_file.assert_not_called()

    def test_usb_copy_is_adopted_after_reindex_without_network_upload(self):
        self.write('one.bin', b'copied by USB')
        remote = self.destination / 'Photos/one.bin'
        remote.parent.mkdir()
        remote.write_bytes(b'copied by USB')
        self.reindex()
        engine = self.engine()
        engine.upload_file = Mock(side_effect=AssertionError('USB copy retransferred'))
        self.assertTrue(engine.run_backup(), self.logs)
        engine.upload_file.assert_not_called()

    def test_real_deletion_moves_to_trash_and_can_be_restored(self):
        path = self.write('one.bin', b'original')
        self.assertTrue(self.engine().run_backup(), self.logs)
        path.unlink()
        self.assertTrue(self.engine().run_backup(), self.logs)
        engine = self.engine()
        try:
            entries = engine.get_trash()
            self.assertEqual(len(entries), 1)
            engine.restore(entries[0]['id'])
        finally:
            engine.close()
        self.assertEqual((self.destination / 'Photos/one.bin').read_bytes(), b'original')

    def test_stop_preserves_partial_and_next_run_only_sends_remaining_bytes(self):
        self.write('one.bin', b'abcdefghijkl')
        engine = None

        def stop_after_first_chunk(method, path, kwargs):
            if method == 'PUT':
                engine.stop_requested = True

        engine = self.engine(stop_after_first_chunk)
        self.assertFalse(engine.run_backup())
        self.assertFalse((self.destination / 'Photos/one.bin').exists())
        sent = []

        def recorder(method, path, kwargs):
            if method == 'PUT':
                sent.append(len(kwargs['data']))

        self.assertTrue(self.engine(recorder).run_backup(), self.logs)
        self.assertEqual(sum(sent), 9)
        self.assertEqual((self.destination / 'Photos/one.bin').read_bytes(), b'abcdefghijkl')

    def test_two_transfer_workers_really_overlap(self):
        self.write('one.bin', b'abc')
        self.write('two.bin', b'xyz')
        barrier = threading.Barrier(2)
        workers = set()
        guard = threading.Lock()

        def recorder(method, path, kwargs):
            if method == 'PUT':
                with guard:
                    workers.add(threading.get_ident())
                barrier.wait(5)

        self.assertTrue(self.engine(recorder).run_backup(), self.logs)
        self.assertEqual(len(workers), 2)

    def test_single_worker_setting_is_respected(self):
        self.config['parallel_transfers'] = 1
        self.write('one.bin', b'abc')
        self.write('two.bin', b'xyz')
        workers = set()

        def recorder(method, path, kwargs):
            if method == 'PUT':
                workers.add(threading.get_ident())

        self.assertTrue(self.engine(recorder).run_backup(), self.logs)
        self.assertEqual(len(workers), 1)

    def test_old_server_is_refused_before_any_transfer_or_deletion(self):
        class OldServerSession:
            def request(self, *args, **kwargs):
                return SimpleNamespace(status_code=200, json=lambda: {'status': 'OK'})

            def close(self):
                pass

        self.write('one.bin', b'abc')
        engine = backup_engine.BackupEngine(self.config, self.logs.append, OldServerSession)
        engine.upload_file = Mock()
        engine.delete_file = Mock()
        self.assertFalse(engine.run_backup())
        engine.upload_file.assert_not_called()
        engine.delete_file.assert_not_called()

    def test_manual_reindex_operation_updates_inventory_without_transfers(self):
        target = self.destination / 'Photos/USB.bin'
        target.parent.mkdir()
        target.write_bytes(b'USB')
        engine = self.engine()
        states = []
        engine.state_callback = lambda state, detail='': states.append(state)
        self.assertTrue(engine.run_reindex(), self.logs)
        self.assertIn('Photos/USB.bin', self.store.manifest())
        self.assertIn('Analyse serveur', states)
        self.assertEqual(states[-1], 'Terminé')


if __name__ == '__main__':
    unittest.main()
