import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER_DIR = Path(__file__).resolve().parents[1] / 'Server'
sys.path.insert(0, str(SERVER_DIR))
import backup_store
from backup_store import BackupStore, RETENTION_SECONDS, StoreError

spec = importlib.util.spec_from_file_location('fcs_backup_server', SERVER_DIR / 'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='fcs-backup-server-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'BACKUP'
        self.store = BackupStore(str(self.root), min_free_bytes=0)

    def index(self, store=None):
        store = store or self.store
        store.start_reindex()
        store.index_thread.join(10)
        self.assertFalse(store.index_thread.is_alive())
        self.assertIsNone(store.reindex_status()['error'])

    def add_file(self, path='Photos/media.bin', data=b'original'):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.index()
        return target

    def start(self, data, path='Photos/media.bin', chunk_size=3, mtime=1000000000):
        return self.store.start_transfer(path, len(data), sha(data), mtime, chunk_size)

    def send(self, data, path='Photos/media.bin', chunk_size=3):
        result = self.start(data, path, chunk_size)
        transfer_id = result['transfer_id']
        for offset in range(result['offset'], len(data), chunk_size):
            block = data[offset:offset + chunk_size]
            self.store.append_chunk(transfer_id, offset, block, sha(block))
        return self.store.finish_transfer(transfer_id)

    def test_first_index_uses_disk_and_keeps_existing_tree(self):
        target = self.add_file()
        entry = self.store.manifest()['Photos/media.bin']
        self.assertEqual(entry['sha256'], sha(target.read_bytes()))
        self.assertEqual(set(self.store.manifest()), {'Photos/media.bin'})

    def test_unchanged_reindex_does_not_read_file_contents(self):
        self.add_file()
        with patch.object(backup_store, 'file_hash', side_effect=AssertionError('Reread unchanged file')):
            self.index()
        self.assertEqual(self.store.reindex_status()['hashed'], 0)

    def test_reindex_detects_same_size_modification_and_external_removal(self):
        target = self.add_file(data=b'AAAA')
        before = target.stat().st_mtime_ns
        target.write_bytes(b'BBBB')
        os.utime(target, ns=(before + 1000000000, before + 1000000000))
        self.index()
        self.assertEqual(self.store.manifest()['Photos/media.bin']['sha256'], sha(b'BBBB'))
        target.unlink()
        self.index()
        self.assertEqual(self.store.manifest(), {})

    def test_failed_reindex_keeps_old_rows_but_blocks_using_stale_inventory(self):
        self.add_file()
        with patch.object(backup_store.os, 'walk', side_effect=PermissionError('unreadable')):
            self.store.start_reindex()
            self.store.index_thread.join(10)
        self.assertIsNotNone(self.store.reindex_status()['error'])
        with self.assertRaises(StoreError):
            self.store.manifest()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)

    def test_reindex_excludes_mutations_and_can_be_cancelled(self):
        self.add_file()
        entered, release = threading.Event(), threading.Event()
        real_hash = backup_store.file_hash
        target = self.root / 'Photos/new.bin'
        target.write_bytes(b'new')

        def blocked_hash(path, cancel=None):
            entered.set()
            release.wait(5)
            return real_hash(path, cancel)

        with patch.object(backup_store, 'file_hash', side_effect=blocked_hash):
            self.store.start_reindex()
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(StoreError):
                    self.start(b'changed')
                self.store.cancel_reindex()
            finally:
                release.set()
                self.store.index_thread.join(10)
        self.assertTrue(self.store.reindex_status()['cancelled'])

    def test_paths_cannot_escape_or_target_internal_files(self):
        for path in ('../BACKUP_other/media.bin', '/absolute', 'C:/BACKUP/media.bin',
                     'Photos/../file', 'Photos//file', '.fcs2/trash/x',
                     '.FCS2/inventory.sqlite3', '.partial_temp/x', '.manifest_cache.json',
                     'Photos/CON.txt', 'Photos/file:stream', 'Photos/file. ', 'Photos\\file'):
            with self.subTest(path=path), self.assertRaises(StoreError):
                self.store.safe_path(path)

    def test_partial_names_do_not_collide(self):
        first = self.start(b'AAA', 'Photos/Vacances_2025/plage.jpg')
        second = self.start(b'BBB', 'Photos/Vacances/2025_plage.jpg')
        self.assertNotEqual(first['transfer_id'], second['transfer_id'])
        self.store.append_chunk(first['transfer_id'], 0, b'AAA', sha(b'AAA'))
        self.store.append_chunk(second['transfer_id'], 0, b'BBB', sha(b'BBB'))
        self.store.finish_transfer(first['transfer_id'])
        self.store.finish_transfer(second['transfer_id'])
        self.assertEqual((self.root / 'Photos/Vacances_2025/plage.jpg').read_bytes(), b'AAA')
        self.assertEqual((self.root / 'Photos/Vacances/2025_plage.jpg').read_bytes(), b'BBB')

    def test_restarts_resume_same_content_and_changed_content_starts_at_zero(self):
        first = self.start(b'abcdef')
        self.store.append_chunk(first['transfer_id'], 0, b'abc', sha(b'abc'))
        self.store = BackupStore(str(self.root), min_free_bytes=0)
        resumed = self.start(b'abcdef')
        self.assertEqual(resumed['offset'], 3)
        new = self.start(b'UVWXYZ')
        self.assertNotEqual(new['transfer_id'], first['transfer_id'])
        self.assertEqual(new['offset'], 0)
        self.send(b'UVWXYZ')
        self.assertEqual((self.root / 'Photos/media.bin').read_bytes(), b'UVWXYZ')

    def test_duplicate_chunk_acknowledgement_does_not_append_twice(self):
        transfer = self.start(b'abcdef')['transfer_id']
        self.store.append_chunk(transfer, 0, b'abc', sha(b'abc'))
        self.assertEqual(self.store.append_chunk(transfer, 0, b'abc', sha(b'abc'))['offset'], 3)
        self.store.append_chunk(transfer, 3, b'def', sha(b'def'))
        self.store.finish_transfer(transfer)
        self.assertEqual((self.root / 'Photos/media.bin').read_bytes(), b'abcdef')

    def test_uncommitted_partial_tail_is_truncated_before_resume(self):
        result = self.start(b'abcdef')
        transfer = result['transfer_id']
        self.store.append_chunk(transfer, 0, b'abc', sha(b'abc'))
        with open(self.store.partial_path(transfer), 'ab') as stream:
            stream.write(b'bad')
        self.assertEqual(self.start(b'abcdef')['offset'], 3)
        self.assertEqual(Path(self.store.partial_path(transfer)).read_bytes(), b'abc')

    def test_bad_chunk_and_invalid_final_hash_preserve_old_file(self):
        target = self.add_file(data=b'old data')
        transfer = self.start(b'newdata', chunk_size=7)['transfer_id']
        with self.assertRaises(StoreError):
            self.store.append_chunk(transfer, 0, b'newdata', sha(b'bad'))
        self.store.append_chunk(transfer, 0, b'newdata', sha(b'newdata'))
        Path(self.store.partial_path(transfer)).write_bytes(b'corrupt')
        with self.assertRaises(StoreError):
            self.store.finish_transfer(transfer)
        self.assertEqual(target.read_bytes(), b'old data')
        self.assertEqual(self.start(b'newdata', chunk_size=7)['offset'], 0)

    def test_atomic_replace_failure_preserves_old_file_and_partial(self):
        target = self.add_file(data=b'old')
        transfer = self.start(b'new')['transfer_id']
        self.store.append_chunk(transfer, 0, b'new', sha(b'new'))
        with patch.object(backup_store.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                self.store.finish_transfer(transfer)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertTrue(Path(self.store.partial_path(transfer)).exists())

    def test_zero_byte_file_is_validated(self):
        self.send(b'')
        self.assertEqual((self.root / 'Photos/media.bin').read_bytes(), b'')

    def test_low_space_at_start_or_mid_transfer_preserves_old_copy(self):
        target = self.add_file(data=b'old')
        usage = shutil.disk_usage(self.root)
        low = usage._replace(free=0)
        with patch.object(backup_store.shutil, 'disk_usage', return_value=low):
            with self.assertRaises(StoreError) as error:
                self.start(b'abcdef')
            self.assertEqual(error.exception.status, 507)
        transfer = self.start(b'abcdef')['transfer_id']
        with patch.object(backup_store.shutil, 'disk_usage', return_value=low):
            with self.assertRaises(StoreError):
                self.store.append_chunk(transfer, 0, b'abc', sha(b'abc'))
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(self.store.transfer(transfer)['received'], 0)

    def test_parallel_transfers_reserve_space_without_overcommitting(self):
        usage = shutil.disk_usage(self.root)._replace(free=10)
        with patch.object(backup_store.shutil, 'disk_usage', return_value=usage):
            self.start(b'abcdef', 'Photos/one.bin')
            with self.assertRaises(StoreError):
                self.start(b'uvwxyz', 'Photos/two.bin')

    def test_trash_retention_is_exactly_48_hours_not_purged_early(self):
        target = self.add_file()
        entry = self.store.manifest()['Photos/media.bin']
        with patch.object(backup_store.time, 'time', return_value=1000000):
            moved = self.store.move_to_trash('Photos/media.bin', entry['sha256'])
        self.assertFalse(target.exists())
        self.assertEqual(moved['expires_at'], 1000000 + RETENTION_SECONDS)
        with patch.object(backup_store.time, 'time', return_value=1000000 + RETENTION_SECONDS - 1):
            self.assertEqual(self.store.cleanup(), 0)
        self.assertEqual(len(self.store.list_trash()), 1)
        with patch.object(backup_store.time, 'time', return_value=1000000 + RETENTION_SECONDS):
            self.assertEqual(self.store.cleanup(), 1)
        self.assertEqual(self.store.list_trash(), [])

    def test_restore_refuses_overwrite_then_restores_and_updates_inventory(self):
        target = self.add_file()
        moved = self.store.move_to_trash('Photos/media.bin', sha(b'original'))
        target.write_bytes(b'newer')
        with self.assertRaises(StoreError):
            self.store.restore(moved['trash_id'])
        self.assertEqual(target.read_bytes(), b'newer')
        target.unlink()
        self.store.restore(moved['trash_id'])
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(self.store.manifest()['Photos/media.bin']['sha256'], sha(b'original'))
        self.assertEqual(self.store.list_trash(), [])

    def test_modified_server_file_is_not_moved_to_trash_from_stale_inventory(self):
        target = self.add_file()
        target.write_bytes(b'changed externally')
        with self.assertRaises(StoreError):
            self.store.move_to_trash('Photos/media.bin', sha(b'original'))
        self.assertTrue(target.exists())

    def test_crash_after_atomic_replacement_repairs_inventory_on_restart(self):
        target = self.add_file(data=b'old')
        transfer = self.start(b'new')['transfer_id']
        self.store.append_chunk(transfer, 0, b'new', sha(b'new'))
        real_replace = os.replace

        def interrupted_replace(source, destination):
            real_replace(source, destination)
            raise OSError('Simulated interruption after filesystem commit')

        with patch.object(backup_store.os, 'replace', side_effect=interrupted_replace):
            with self.assertRaises(OSError):
                self.store.finish_transfer(transfer)
        self.store = BackupStore(str(self.root), min_free_bytes=0)
        self.assertEqual(target.read_bytes(), b'new')
        self.assertEqual(self.store.manifest()['Photos/media.bin']['sha256'], sha(b'new'))
        with self.assertRaises(StoreError):
            self.store.transfer(transfer)

    def test_crash_after_trash_move_is_recoverable_on_restart(self):
        self.add_file(data=b'original')
        real_replace = os.replace

        def interrupted_replace(source, destination):
            real_replace(source, destination)
            raise OSError('Simulated interruption after trash move')

        with patch.object(backup_store.os, 'replace', side_effect=interrupted_replace):
            with self.assertRaises(OSError):
                self.store.move_to_trash('Photos/media.bin', sha(b'original'))
        self.store = BackupStore(str(self.root), min_free_bytes=0)
        entries = self.store.list_trash()
        self.assertEqual(len(entries), 1)
        self.assertNotIn('Photos/media.bin', self.store.manifest())
        self.store.restore(entries[0]['id'])
        self.assertEqual((self.root / 'Photos/media.bin').read_bytes(), b'original')

    def test_crash_after_restore_repairs_inventory_on_restart(self):
        self.add_file(data=b'original')
        trash_id = self.store.move_to_trash('Photos/media.bin', sha(b'original'))['trash_id']
        real_replace = os.replace

        def interrupted_replace(source, destination):
            real_replace(source, destination)
            raise OSError('Simulated interruption after restore')

        with patch.object(backup_store.os, 'replace', side_effect=interrupted_replace):
            with self.assertRaises(OSError):
                self.store.restore(trash_id)
        self.store = BackupStore(str(self.root), min_free_bytes=0)
        self.assertEqual(self.store.list_trash(), [])
        self.assertEqual(self.store.manifest()['Photos/media.bin']['sha256'], sha(b'original'))

    def test_reparse_point_in_server_tree_is_rejected(self):
        directory = self.root / 'Photos'
        directory.mkdir()
        real_is_link = backup_store.is_link

        def link(path):
            return os.fspath(path) == str(directory) or real_is_link(path)

        with patch.object(backup_store, 'is_link', side_effect=link):
            with self.assertRaises(StoreError):
                self.store.safe_path('Photos/media.bin')
            self.store.start_reindex()
            self.store.index_thread.join(10)
        self.assertIsNotNone(self.store.reindex_status()['error'])


class APITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='fcs-backup-api-test-')
        self.addCleanup(temporary.cleanup)
        self.app = server.create_app({'backup_folder': temporary.name, 'secret_token': 'test',
                                      'min_free_bytes': 0})
        self.client = self.app.test_client()
        self.headers = {'X-Secret-Token': 'test', 'X-FCS2-Protocol': '2'}

    def test_authentication_and_protocol_are_required_even_for_health(self):
        self.assertEqual(self.client.get('/health').status_code, 401)
        self.assertEqual(self.client.get('/health', headers={'X-Secret-Token': 'test'}).status_code, 426)
        response = self.client.get('/health', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['protocol'], 2)
        self.assertIn('free', response.json['disk'])

    def test_malformed_metadata_and_legacy_upload_cannot_modify_backup(self):
        for data in ([], {}, {'path': '../escape', 'size': 1},
                     {'path': 'Photos/x', 'size': -1, 'sha256': sha(b'x'), 'mtime_ns': 1, 'chunk_size': 3}):
            response = self.client.post('/upload/start', headers=self.headers, json=data)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post('/upload', headers=self.headers).status_code, 404)

    def test_complete_upload_through_api(self):
        response = self.client.post('/upload/start', headers=self.headers, json={
            'path': 'Photos/media.bin', 'size': 3, 'sha256': sha(b'abc'), 'mtime_ns': 1000000000,
            'chunk_size': 3})
        transfer = response.json['transfer_id']
        headers = dict(self.headers, **{'X-Offset': '0', 'X-Chunk-Hash': sha(b'abc')})
        self.assertEqual(self.client.put('/upload/' + transfer, headers=headers, data=b'abc').status_code, 200)
        result = self.client.post('/upload/' + transfer + '/finalize', headers=self.headers)
        self.assertTrue(result.json['complete'])
        self.assertEqual(result.json['sha256'], sha(b'abc'))


if __name__ == '__main__':
    unittest.main()
