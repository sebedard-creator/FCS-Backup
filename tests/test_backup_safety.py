"""Régressions anti-suppression, sans réseau ni interface graphique."""

import os
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from Client import backup_engine


class BackupSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='fcs-backup-safety-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / 'Photos'
        self.source.mkdir()
        self.config = {
            'server_ip': 'unused.invalid',
            'server_port': 5001,
            'secret_token': 'test-only',
            'folders': [str(self.source)],
            'hash_cache_file': str(self.base / 'hashes.sqlite3'),
        }
        # Aucune requête ne doit quitter le processus, même en cas de régression.
        requests_patch = patch.object(backup_engine, 'requests', Mock())
        requests_patch.start()
        self.addCleanup(requests_patch.stop)

    def engine(self, manifest=None):
        engine = backup_engine.BackupEngine(self.config, Mock())
        engine.health_check = Mock(return_value=True)
        manifest = {key: dict(value) for key, value in (manifest or {}).items()}
        for key, value in manifest.items():
            local = self.base / key
            value['sha256'] = hashlib.sha256(local.read_bytes()).hexdigest() if local.is_file() else '0' * 64
        engine.get_server_manifest = Mock(return_value=manifest)
        engine.upload_file = Mock(return_value=True)
        engine.delete_file = Mock(return_value=True)
        return engine

    def assert_cancelled_without_file_changes(self, engine):
        self.assertFalse(engine.run_backup())
        engine.upload_file.assert_not_called()
        engine.delete_file.assert_not_called()
        self.assertTrue(any('Aucune suppression' in call.args[0]
                            for call in engine.log.call_args_list))

    def test_missing_source_preserves_its_backup_and_healthy_sources(self):
        (self.source / 'new.jpg').write_bytes(b'new')
        self.config['folders'].append(str(self.base / 'Disconnected'))
        engine = self.engine({
            'Photos/removed.jpg': {'size': 3},
            'Disconnected/media.bin': {'size': 100},
        })
        self.assert_cancelled_without_file_changes(engine)
        engine.health_check.assert_not_called()

    def test_no_configured_sources_cannot_purge_server(self):
        self.config['folders'] = []
        self.assert_cancelled_without_file_changes(
            self.engine({'Photos/media.jpg': {'size': 3}}))

    def test_file_instead_of_source_directory_cancels_backup(self):
        source_file = self.base / 'not-a-directory'
        source_file.write_bytes(b'data')
        self.config['folders'] = [str(source_file)]
        self.assert_cancelled_without_file_changes(self.engine())

    def test_unreadable_subdirectory_cancels_entire_scan(self):
        inaccessible = self.source / 'Private'
        inaccessible.mkdir()
        (inaccessible / 'media.jpg').write_bytes(b'data')
        real_scandir = os.scandir

        def scandir(path):
            if os.fspath(path) == str(inaccessible):
                raise PermissionError(13, 'Access denied', str(inaccessible))
            return real_scandir(path)

        engine = self.engine({'Photos/Private/media.jpg': {'size': 4}})
        with patch.object(backup_engine.os, 'scandir', side_effect=scandir):
            self.assert_cancelled_without_file_changes(engine)

    def test_file_stat_failure_cannot_turn_into_remote_deletion(self):
        source_file = self.source / 'media.jpg'
        source_file.write_bytes(b'data')
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if os.fspath(path) == str(source_file):
                raise FileNotFoundError(2, 'Disconnected', str(source_file))
            return real_stat(path, *args, **kwargs)

        engine = self.engine({'Photos/media.jpg': {'size': 4}})
        with patch.object(backup_engine.os, 'stat', side_effect=stat):
            self.assert_cancelled_without_file_changes(engine)

    def test_disconnect_between_scan_passes_cancels_backup(self):
        real_walk = os.walk
        passes = 0

        def walk(path, **kwargs):
            nonlocal passes
            passes += 1
            if passes == 2:
                kwargs['onerror'](FileNotFoundError(2, 'Disconnected', str(path)))
            return real_walk(path, **kwargs)

        engine = self.engine({'Photos/media.jpg': {'size': 4}})
        with patch.object(backup_engine.os, 'walk', side_effect=walk):
            self.assert_cancelled_without_file_changes(engine)

    def test_disconnect_during_upload_blocks_subsequent_deletions(self):
        (self.source / 'new.jpg').write_bytes(b'new')
        engine = self.engine({'Photos/removed.jpg': {'size': 3}})
        offline = False
        real_scandir = os.scandir

        def upload(*args):
            nonlocal offline
            offline = True
            return True

        def scandir(path):
            if offline and os.fspath(path) == str(self.source):
                raise FileNotFoundError(2, 'Disconnected', str(path))
            return real_scandir(path)

        engine.upload_file.side_effect = upload
        with patch.object(backup_engine.os, 'scandir', side_effect=scandir):
            self.assertFalse(engine.run_backup())
        engine.upload_file.assert_called_once()
        engine.delete_file.assert_not_called()

    def test_accessible_empty_folder_still_mirrors_genuine_deletions(self):
        engine = self.engine({'Photos/removed.jpg': {'size': 3}})
        self.assertTrue(engine.run_backup())
        engine.delete_file.assert_called_once_with('Photos/removed.jpg')

    def test_failed_upload_blocks_all_deletions(self):
        (self.source / 'new.jpg').write_bytes(b'new')
        engine = self.engine({'Photos/removed.jpg': {'size': 3}})
        engine.upload_file.return_value = False
        self.assertFalse(engine.run_backup())
        engine.upload_file.assert_called_once()
        engine.delete_file.assert_not_called()

    def test_disk_root_without_folder_namespace_cannot_purge_server(self):
        self.config['folders'] = [os.path.abspath(os.sep)]
        engine = self.engine({'Photos/media.jpg': {'size': 4}})
        with patch.object(backup_engine, '_ensure_sources_available'), \
                patch.object(backup_engine, 'scan_folders', return_value={}):
            self.assertTrue(engine.run_backup())
        engine.delete_file.assert_not_called()

    def test_only_currently_configured_folder_can_be_purged(self):
        engine = self.engine({
            'Photos/removed.jpg': {'size': 3},
            'PhotosArchive/keep.jpg': {'size': 3},
            'RemovedFromConfig/keep.bin': {'size': 100},
        })
        self.assertTrue(engine.run_backup())
        engine.delete_file.assert_called_once_with('Photos/removed.jpg')

    def test_normal_backup_uploads_new_files_and_preserves_existing_files(self):
        existing = self.source / 'existing.jpg'
        existing.write_bytes(b'data')
        new = self.source / 'new.jpg'
        new.write_bytes(b'new')
        engine = self.engine({
            'Photos/existing.jpg': {'size': 4},
            'Photos/removed.jpg': {'size': 3},
        })
        self.assertTrue(engine.run_backup())
        engine.upload_file.assert_called_once_with(str(new), 'Photos/new.jpg')
        engine.delete_file.assert_called_once_with('Photos/removed.jpg')

    def test_configuration_changes_apply_to_next_backup(self):
        engine = self.engine({'Photos/removed.jpg': {'size': 3}})
        self.config['folders'].clear()
        self.config['server_ip'] = 'changed.invalid'
        self.assertTrue(engine.run_backup())
        self.assertEqual(engine.config['server_ip'], 'unused.invalid')
        engine.delete_file.assert_called_once_with('Photos/removed.jpg')

    def test_trailing_separator_keeps_the_correct_server_folder(self):
        self.config['folders'] = [str(self.source) + os.sep]
        media = self.source / 'media.jpg'
        media.write_bytes(b'data')
        engine = self.engine({'Photos/media.jpg': {'size': 4}})
        self.assertTrue(engine.run_backup())
        engine.upload_file.assert_not_called()
        engine.delete_file.assert_not_called()

    def test_stop_request_prevents_all_deletions(self):
        engine = self.engine({'Photos/removed.jpg': {'size': 3}})
        engine.stop_requested = True
        self.assertFalse(engine.run_backup())
        engine.delete_file.assert_not_called()


if __name__ == '__main__':
    unittest.main()
