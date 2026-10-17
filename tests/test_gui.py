import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from Client import client
except ImportError:
    client = None


@unittest.skipIf(client is None, 'Tkinter non disponible dans ce runtime')
class BackupStatusTests(unittest.TestCase):
    def test_hidden_window_progress_space_and_trash_selection_without_network(self):
        try:
            root = client.tk.Tk()
        except client.tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        try:
            config = client.deepcopy(client.DEFAULT_CONFIG)
            config['folders'] = ['Y:/Ancienne Source', 'Y:/Photos']
            config['folder_labels'] = {'Y:/Ancienne Source': 'FCS Backup'}
            with tempfile.TemporaryDirectory(prefix='fcs-backup-ui-test-') as temporary, \
                    patch.object(client, 'load_config', return_value=config), \
                    patch.object(client.FCSBackupApp, '_schedule_health_check'), \
                    patch.object(client.FCSBackupApp, '_start_scheduler'), \
                    patch.object(client.FCSBackupApp, '_refresh_trash'), \
                    patch.object(client, 'LOG_FILE', str(Path(temporary) / 'test.log')):
                app = client.FCSBackupApp(root)
                self.assertTrue(root.title().startswith('FCS Backup'))
                self.assertEqual(app.folder_lb.get(0), 'FCS Backup')
                self.assertEqual(app.config['folders'][0], 'Y:/Ancienne Source')
                app._log('Test sans réseau')
                app._log('Y:/Ancienne Source/test.jpg')
                app._update_progress(10, 20)
                app._update_chunk_progress('Photos/test.bin', 5, 10)
                app._post(app._set_disk_info, {'free': 1000, 'total': 2000, 'trash_bytes': 100})
                app._drain_ui()
                self.assertIn('Test sans réseau', app.log_text.get('1.0', 'end'))
                self.assertIn('FCS Backup/test.jpg', app.log_text.get('1.0', 'end'))
                self.assertNotIn('Ancienne Source', app.log_text.get('1.0', 'end'))
                app.folder_lb.selection_set(0)
                with patch.object(client, 'save_config'):
                    app._remove_folder()
                self.assertEqual(app.config['folders'], ['Y:/Photos'])
                self.assertEqual(app.progress_bar['value'], 50)
                self.assertEqual(app.chunk_bar['value'], 50)
                self.assertIn('libres', app.disk_label['text'])
                self.assertEqual(app.parallel_var.get(), '2')
                app._set_busy(True)
                self.assertEqual(str(app.backup_btn['state']), 'disabled')
                app._set_busy(False)
                self.assertEqual(str(app.backup_btn['state']), 'normal')
                app._show_trash()
                app.trash_window.withdraw()
                entries = [{'id': str(index) * 32, 'path': 'Photos/{}.jpg'.format(index),
                            'size': 100, 'deleted_at': 1700000000, 'expires_at': 1700172800}
                           for index in (1, 2)]
                app._populate_trash(app.trash_window, entries)
                app.trash_tree.selection_set([entry['id'] for entry in entries])
                app._start_operation = Mock()
                app._restore_selected()
                app._start_operation.assert_called_once_with('restore', tuple(entry['id'] for entry in entries))
                root.update_idletasks()
                app.running = False
        finally:
            for callback in root.tk.call('after', 'info'):
                root.after_cancel(callback)
            root.destroy()

    def app(self):
        app = client.FCSBackupApp.__new__(client.FCSBackupApp)
        app.config = {'last_backup': '2026-01-01 02:00'}
        app.root = SimpleNamespace(after=Mock())
        app.ui_queue = queue.Queue()
        app.running = True
        app.quit_pending = False
        app._log = Mock()
        app._set_tray_color = Mock()
        app._hide_progress = Mock()
        app._set_busy = Mock()
        app._set_operation_state = Mock()
        app._launch_health_check = Mock()
        app.last_label = Mock()
        return app

    def test_failed_backup_does_not_update_last_successful_backup(self):
        app = self.app()
        with patch.object(client, 'save_config') as save_config:
            app._finish_operation('backup', False, 'Échec', '')
        self.assertEqual(app.config['last_backup'], '2026-01-01 02:00')
        save_config.assert_not_called()
        self.assertIsNone(app.current_engine)
        app._set_busy.assert_called_once_with(False)

    def test_successful_backup_still_updates_last_backup(self):
        app = self.app()
        with patch.object(client, 'save_config') as save_config:
            app._finish_operation('backup', True, 'Terminé', '')
        self.assertNotEqual(app.config['last_backup'], '2026-01-01 02:00')
        save_config.assert_called_once_with(app.config)

    def test_unexpected_worker_error_always_schedules_ui_cleanup(self):
        app = self.app()
        engine = Mock(stop_requested=False)
        engine.run_backup.side_effect = RuntimeError('Unexpected failure')
        app.current_engine = engine
        app._run_operation('backup')
        engine.close.assert_called_once()
        callback, args = app.ui_queue.get_nowait()
        self.assertEqual(callback, app._finish_operation)
        self.assertEqual(args, ('backup', False, 'Échec', ''))
        app.root.after.assert_not_called()

    def test_worker_logging_queues_ui_work_and_includes_full_date(self):
        app = self.app()
        app._append_log_ui = Mock()
        client.FCSBackupApp._log(app, 'message')
        callback, args = app.ui_queue.get_nowait()
        self.assertEqual(callback, app._append_log_ui)
        self.assertRegex(args[0], r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] message$')
        app._append_log_ui.assert_not_called()
        app.root.after.assert_not_called()


if __name__ == '__main__':
    unittest.main()
