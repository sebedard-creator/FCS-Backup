"""HTTP reel sur 127.0.0.1 seulement ; aucun acces au serveur configure."""
import tempfile
import threading
import unittest
from pathlib import Path

import requests
from Client import backup_engine
from test_server import server

try:
    from waitress import create_server
except ImportError:
    create_server = None


@unittest.skipIf(create_server is None, 'Installer le profil serveur pour tester Waitress')
class PersistentHTTPTests(unittest.TestCase):
    def test_real_transfer_reuses_tcp_connections_and_validates_bytes(self):
        with tempfile.TemporaryDirectory(prefix='fcs-backup-http-test-') as temporary:
            base = Path(temporary)
            source = base / 'Photos'
            source.mkdir()
            (source / 'one.bin').write_bytes(b'abcdef' * 1000)
            (source / 'two.bin').write_bytes(b'uvwxyz' * 1000)
            app = server.create_app({'backup_folder': str(base / 'backup'),
                                      'secret_token': 'test', 'min_free_bytes': 0})
            store = app.extensions['backup_store']
            store.start_reindex()
            store.index_thread.join(10)
            socket_map = {}
            http_server = create_server(app, map=socket_map, host='127.0.0.1', port=0,
                                         threads=4, asyncore_loop_timeout=0.1)
            thread = threading.Thread(target=http_server.run, daemon=True)
            thread.start()
            ports = {}
            guard = threading.Lock()

            def record(response, *args, **kwargs):
                connection = response.raw._connection
                self.assertIsNotNone(connection)
                self.assertIsNotNone(connection.sock)
                with guard:
                    ports.setdefault(threading.get_ident(), []).append(connection.sock.getsockname()[1])

            def factory():
                session = requests.Session()
                session.trust_env = False
                session.hooks['response'].append(record)
                return session

            logs = []
            config = {'folders': [str(source)], 'server_ip': '127.0.0.1',
                       'server_port': int(http_server.effective_port), 'secret_token': 'test',
                       'chunk_size': 1024, 'parallel_transfers': 2,
                       'hash_cache_file': str(base / 'hashes.sqlite3')}
            engine = backup_engine.BackupEngine(config, logs.append, factory)
            try:
                self.assertTrue(engine.run_backup(), logs)
                for name in ('one.bin', 'two.bin'):
                    self.assertEqual((source / name).read_bytes(),
                                     (base / 'backup' / 'Photos' / name).read_bytes())
                self.assertGreaterEqual(len(ports), 2)
                for observed in ports.values():
                    self.assertEqual(len(set(observed)), 1, 'TCP connection was not reused')
                self.assertTrue(any(len(observed) >= 6 for observed in ports.values()))
            finally:
                engine.close()
                http_server.close()
                http_server.task_dispatcher.shutdown()
                for channel in list(socket_map.values()):
                    channel.close()
                thread.join(5)
            self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
