"""Stockage FCS Backup : inventaire transactionnel et opérations sur disque.

Compatible Python 3.8. Aucune initialisation du disque à l'import.
"""

import hashlib
import ntpath
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


RETENTION_SECONDS = 48 * 3600
MAX_CHUNK_SIZE = 16 * 1024 * 1024
RESERVED = {'.fcs2', '.partial_temp', '.manifest_cache.json'}


class StoreError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def filesystem_path(path):
    """Utilise l'API Windows des chemins longs sans modifier les noms stockés."""
    absolute = os.path.abspath(path)
    if os.name != 'nt' or absolute.startswith('\\\\?\\'):
        return absolute
    if absolute.startswith('\\\\'):
        return '\\\\?\\UNC\\' + absolute[2:]
    return '\\\\?\\' + absolute


def file_hash(path, cancel=None):
    before = os.stat(path)
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            if cancel is not None and cancel.is_set():
                raise StoreError('Analyse arrêtée', 409)
            block = stream.read(4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    after = os.stat(path)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise StoreError('Fichier modifié pendant la vérification : ' + path, 409)
    return digest.hexdigest(), after


def is_link(path):
    info = os.lstat(path)
    return os.path.islink(path) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


class BackupStore:
    def __init__(self, folder, min_free_bytes=64 * 1024 * 1024):
        self.configured_root = filesystem_path(folder)
        self.root = os.path.realpath(self.configured_root)
        os.makedirs(self.root, exist_ok=True)
        self.internal = os.path.join(self.root, '.fcs2')
        if os.path.lexists(self.internal) and is_link(self.internal):
            raise StoreError('Le dossier interne ne peut pas être un lien')
        self.partial_dir = os.path.join(self.internal, 'partials')
        self.trash_dir = os.path.join(self.internal, 'trash')
        for directory in (self.internal, self.partial_dir, self.trash_dir):
            if os.path.lexists(directory) and is_link(directory):
                raise StoreError('Dossier interne invalide : ' + directory)
            os.makedirs(directory, exist_ok=True)
        self.db_file = os.path.join(self.internal, 'inventory.sqlite3')
        if os.path.lexists(self.db_file) and is_link(self.db_file):
            raise StoreError('Inventaire invalide')
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.condition = threading.Condition()
        self.active = 0
        self.indexing = False
        self.index_cancel = threading.Event()
        self.index_thread = None
        self.index_status = {'running': False, 'count': 0, 'hashed': 0,
                             'path': '', 'error': None, 'cancelled': False}
        self.lock_guard = threading.Lock()
        self.path_locks = {}
        self.space_lock = threading.RLock()
        self.reservations = {}
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    source_mtime_ns INTEGER);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS reindex_cache (
                    path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    source_mtime_ns INTEGER, scan_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transfers (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, source_mtime_ns INTEGER NOT NULL,
                    chunk_size INTEGER NOT NULL, received INTEGER NOT NULL,
                    updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS transfers_path ON transfers(path);
                CREATE TABLE IF NOT EXISTS trash (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, source_mtime_ns INTEGER,
                    deleted_at REAL NOT NULL, expires_at REAL NOT NULL,
                    state TEXT NOT NULL);
            ''')
        self.recover_moves()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_file, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def assert_root(self):
        if (os.path.normcase(os.path.realpath(self.configured_root)) !=
                os.path.normcase(self.root) or not os.path.isdir(self.root)):
            raise StoreError('Disque de sauvegarde inaccessible ou déplacé', 503)
        for path in (self.internal, self.partial_dir, self.trash_dir):
            if not os.path.isdir(path) or is_link(path):
                raise StoreError('Stockage interne inaccessible', 503)

    @contextmanager
    def operation(self):
        with self.condition:
            if self.indexing:
                raise StoreError('Réanalyse en cours ; modifications bloquées', 409)
            self.active += 1
        try:
            self.assert_root()
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def path_lock(self, path):
        key = path.casefold()
        with self.lock_guard:
            return self.path_locks.setdefault(key, threading.RLock())

    def safe_path(self, path):
        if not isinstance(path, str) or not path or '\\' in path:
            raise StoreError('Chemin relatif invalide')
        parts = path.split('/')
        if ntpath.splitdrive(path)[0] or path.startswith('/'):
            raise StoreError('Chemin absolu refusé')
        for part in parts:
            stem = part.split('.')[0].upper()
            if (not part or part in ('.', '..') or part[-1:] in (' ', '.') or
                    any(ord(char) < 32 or char in '<>:"|?*' for char in part) or
                    stem in {'CON', 'PRN', 'AUX', 'NUL'} or
                    re.fullmatch(r'(COM|LPT)[1-9]', stem)):
                raise StoreError('Composant de chemin invalide')
        if parts[0].casefold() in RESERVED or parts[0].casefold().startswith('.manifest_cache.'):
            raise StoreError('Chemin réservé au serveur')
        dest = os.path.realpath(os.path.join(self.root, *parts))
        try:
            within = os.path.commonpath([self.root, dest])
        except ValueError:
            raise StoreError('Chemin hors sauvegarde')
        if os.path.normcase(within) != os.path.normcase(self.root) or dest == self.root:
            raise StoreError('Chemin hors sauvegarde')
        # Protéger aussi les chemins résolus, notamment les alias courts NTFS.
        internal = os.path.normcase(os.path.realpath(self.internal))
        resolved = os.path.normcase(dest)
        if resolved == internal or resolved.startswith(internal + os.sep):
            raise StoreError('Chemin réservé au serveur')
        probe = self.root
        for part in parts:
            probe = os.path.join(probe, part)
            if os.path.lexists(probe) and is_link(probe):
                raise StoreError('Liens et jonctions refusés dans les sauvegardes')
        return dest

    def ready(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='ready'").fetchone()
        return bool(row and row[0] == '1')

    def manifest(self):
        self.assert_root()
        with self.condition:
            if self.indexing:
                raise StoreError('Réanalyse en cours', 202)
            if not self.ready():
                raise StoreError('Inventaire à reconstruire avec « Réanalyser la sauvegarde »', 503)
            with self.connection() as db:
                return {row['path']: {key: row[key] for key in
                        ('size', 'mtime_ns', 'sha256', 'source_mtime_ns')}
                        for row in db.execute('SELECT * FROM files')}

    def disk_info(self):
        self.assert_root()
        usage = shutil.disk_usage(self.root)
        with self.space_lock:
            self._expire_reservations()
            reserved = sum(item[0] for item in self.reservations.values())
        with self.connection() as db:
            trash_size = db.execute("SELECT COALESCE(SUM(size),0) FROM trash").fetchone()[0]
        return {'free': usage.free, 'total': usage.total, 'trash_bytes': trash_size,
                'reserved_bytes': reserved, 'min_free_bytes': self.min_free_bytes}

    def _expire_reservations(self):
        cutoff = time.monotonic() - 600
        self.reservations = {key: item for key, item in self.reservations.items()
                             if item[1] >= cutoff}

    def reserve(self, transfer_id, remaining):
        with self.space_lock:
            self._expire_reservations()
            others = sum(item[0] for key, item in self.reservations.items() if key != transfer_id)
            free = shutil.disk_usage(self.root).free
            if remaining and free - others < remaining + self.min_free_bytes:
                raise StoreError('Espace disque insuffisant ; ancienne copie conservée', 507)
            self.reservations[transfer_id] = (remaining, time.monotonic())

    def start_reindex(self):
        with self.condition:
            if self.indexing:
                return self.reindex_status()
            if self.active:
                raise StoreError('Un transfert ou une modification est en cours', 409)
            with self.connection() as db:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('ready','0')")
            self.indexing = True
            self.index_cancel.clear()
            self.index_status = {'running': True, 'count': 0, 'hashed': 0,
                                 'path': '', 'error': None, 'cancelled': False}
            self.index_thread = threading.Thread(target=self._reindex_worker, daemon=True)
            self.index_thread.start()
            return dict(self.index_status)

    def reindex_status(self):
        with self.condition:
            return dict(self.index_status)

    def cancel_reindex(self):
        self.index_cancel.set()

    def _reindex_worker(self):
        try:
            self.assert_root()
            with self.connection() as db:
                previous = {row['path']: dict(row) for row in db.execute('SELECT * FROM files')}
                scan_id = uuid.uuid4().hex
                pending = 0
                last_commit = time.monotonic()
                # Nettoyer la table de travail des versions antérieures.
                db.execute('DROP TABLE IF EXISTS next_files')

                def walk_error(error):
                    raise error

                try:
                    for root, dirs, filenames in os.walk(self.root, onerror=walk_error):
                        if self.index_cancel.is_set():
                            raise StoreError('Analyse arrêtée', 409)
                        if root == self.root:
                            dirs[:] = [name for name in dirs if name.casefold() not in RESERVED]
                        for name in dirs:
                            relative_dir = os.path.relpath(os.path.join(root, name), self.root).replace('\\', '/')
                            self.safe_path(relative_dir)
                        for name in filenames:
                            relative = os.path.relpath(os.path.join(root, name), self.root).replace('\\', '/')
                            if root == self.root and (name.casefold() in RESERVED or
                                                      name.casefold().startswith('.manifest_cache.')):
                                continue
                            if self.index_cancel.is_set():
                                raise StoreError('Analyse arrêtée', 409)
                            path = self.safe_path(relative)
                            info = os.stat(path)
                            old = previous.get(relative)
                            metadata = (info.st_size, info.st_mtime_ns)
                            if not old or (old['size'], old['mtime_ns']) != metadata:
                                old = db.execute('SELECT * FROM reindex_cache WHERE path=?',
                                                 (relative,)).fetchone()
                            if old and (old['size'], old['mtime_ns']) == metadata:
                                digest = old['sha256']
                                source_mtime = old['source_mtime_ns']
                            else:
                                digest, info = file_hash(path, self.index_cancel)
                                source_mtime = None
                                with self.condition:
                                    self.index_status['hashed'] += 1
                            db.execute('INSERT OR REPLACE INTO reindex_cache VALUES (?,?,?,?,?,?)',
                                       (relative, info.st_size, info.st_mtime_ns, digest, source_mtime, scan_id))
                            pending += 1
                            if pending >= 100 or time.monotonic() - last_commit >= 1:
                                db.commit()
                                pending = 0
                                last_commit = time.monotonic()
                            with self.condition:
                                self.index_status['count'] += 1
                                self.index_status['path'] = relative
                finally:
                    # Conserver les empreintes terminées même après erreur ou annulation.
                    # L'inventaire publié reste bloqué jusqu'à la réussite complète.
                    db.commit()
                self.assert_root()
                if self.index_cancel.is_set():
                    raise StoreError('Analyse arrêtée', 409)
                db.execute('DELETE FROM files')
                db.execute('''INSERT INTO files
                    SELECT path,size,mtime_ns,sha256,source_mtime_ns
                    FROM reindex_cache WHERE scan_id=?''', (scan_id,))
                db.execute('DELETE FROM reindex_cache WHERE scan_id<>?', (scan_id,))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('ready','1')")
        except Exception as error:
            with self.condition:
                self.index_status['error'] = str(error)
                self.index_status['cancelled'] = self.index_cancel.is_set()
        finally:
            with self.condition:
                self.index_status['running'] = False
                self.indexing = False
                self.condition.notify_all()

    def start_transfer(self, path, size, digest, mtime_ns, chunk_size):
        self.safe_path(path)
        if (type(size) is not int or size < 0 or size > 2**63 - 1 or
                type(mtime_ns) is not int or not 0 <= mtime_ns <= 2**63 - 1 or
                type(chunk_size) is not int or not 1 <= chunk_size <= MAX_CHUNK_SIZE or
                not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise StoreError('Métadonnées de transfert invalides')
        identity = '{}\0{}\0{}\0{}\0{}'.format(path.casefold(), size, digest, mtime_ns, chunk_size)
        transfer_id = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        with self.path_lock(path), self.operation():
            partial = self.partial_path(transfer_id)
            with self.connection() as db:
                # Un changement de contenu ne doit jamais reprendre l'ancien partiel.
                for old in db.execute('SELECT id FROM transfers WHERE path=? AND id<>?',
                                      (path, transfer_id)).fetchall():
                    old_path = self.partial_path(old['id'])
                    if os.path.exists(old_path):
                        os.remove(old_path)
                    db.execute('DELETE FROM transfers WHERE id=?', (old['id'],))
                    with self.space_lock:
                        self.reservations.pop(old['id'], None)
                row = db.execute('SELECT * FROM transfers WHERE id=?', (transfer_id,)).fetchone()
                received = row['received'] if row else 0
                actual = os.path.getsize(partial) if os.path.exists(partial) else 0
                if actual < received or received > size:
                    received = 0
                with open(partial, 'r+b' if os.path.exists(partial) else 'w+b') as stream:
                    stream.truncate(received)
                self.reserve(transfer_id, size - received)
                db.execute('INSERT OR REPLACE INTO transfers VALUES (?,?,?,?,?,?,?,?)',
                           (transfer_id, path, size, digest, mtime_ns, chunk_size, received, time.time()))
            return {'transfer_id': transfer_id, 'offset': received, 'size': size}

    def partial_path(self, transfer_id):
        if not isinstance(transfer_id, str) or not re.fullmatch('[0-9a-f]{64}', transfer_id):
            raise StoreError('Identifiant de transfert invalide')
        path = os.path.join(self.partial_dir, transfer_id + '.part')
        if os.path.lexists(path) and is_link(path):
            raise StoreError('Fichier temporaire invalide')
        return path

    def transfer(self, transfer_id):
        self.partial_path(transfer_id)
        with self.connection() as db:
            row = db.execute('SELECT * FROM transfers WHERE id=?', (transfer_id,)).fetchone()
        if not row:
            raise StoreError('Transfert inconnu ; redémarrer ce fichier', 404)
        return dict(row)

    def append_chunk(self, transfer_id, offset, data, digest):
        initial = self.transfer(transfer_id)
        with self.path_lock(initial['path']), self.operation():
            row = self.transfer(transfer_id)
            if (type(offset) is not int or offset < 0 or not data or
                    len(data) > row['chunk_size'] or offset + len(data) > row['size']):
                raise StoreError('Taille ou position du morceau invalide')
            if hashlib.sha256(data).hexdigest() != digest:
                raise StoreError('Empreinte du morceau invalide', 409)
            partial = self.partial_path(transfer_id)
            if offset < row['received'] and offset + len(data) <= row['received']:
                with open(partial, 'rb') as stream:
                    stream.seek(offset)
                    if stream.read(len(data)) != data:
                        raise StoreError('Morceau déjà reçu avec un contenu différent', 409)
                return {'offset': row['received']}
            if offset != row['received']:
                raise StoreError('Morceau hors ordre ; position attendue {}'.format(row['received']), 409)
            with self.space_lock:
                self.reserve(transfer_id, row['size'] - offset)
                with open(partial, 'r+b') as stream:
                    stream.truncate(offset)
                    stream.seek(offset)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                with self.connection() as db:
                    db.execute('UPDATE transfers SET received=?, updated=? WHERE id=?',
                               (offset + len(data), time.time(), transfer_id))
                self.reservations[transfer_id] = (row['size'] - offset - len(data), time.monotonic())
            return {'offset': offset + len(data)}

    def finish_transfer(self, transfer_id):
        initial = self.transfer(transfer_id)
        with self.path_lock(initial['path']), self.operation():
            row = self.transfer(transfer_id)
            if row['received'] != row['size']:
                raise StoreError('Fichier incomplet', 409)
            partial = self.partial_path(transfer_id)
            digest, info = file_hash(partial)
            if info.st_size != row['size'] or digest != row['sha256']:
                # La prochaine tentative doit repartir à zéro, pas reprendre un partiel corrompu.
                with self.connection() as db:
                    db.execute('UPDATE transfers SET received=0 WHERE id=?', (transfer_id,))
                with self.space_lock:
                    self.reservations.pop(transfer_id, None)
                raise StoreError('Empreinte du fichier complet invalide ; ancienne copie conservée', 409)
            dest = self.safe_path(row['path'])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            self.safe_path(row['path'])
            # Les nanosecondes sont conservées si le système de fichiers le permet.
            os.utime(partial, ns=(row['source_mtime_ns'], row['source_mtime_ns']))
            os.replace(partial, dest)
            info = os.stat(dest)
            with self.connection() as db:
                db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)',
                           (row['path'], info.st_size, info.st_mtime_ns, digest, row['source_mtime_ns']))
                db.execute('DELETE FROM transfers WHERE id=?', (transfer_id,))
            with self.space_lock:
                self.reservations.pop(transfer_id, None)
            return {'complete': True, 'path': row['path'], 'sha256': digest, 'size': info.st_size}

    def move_to_trash(self, path, expected_hash):
        dest = self.safe_path(path)
        with self.path_lock(path), self.operation():
            with self.connection() as db:
                row = db.execute('SELECT * FROM files WHERE path=?', (path,)).fetchone()
                if not row or row['sha256'] != expected_hash:
                    raise StoreError('Inventaire modifié ; réanalyser avant suppression', 409)
                if not os.path.exists(dest):
                    raise StoreError('Fichier absent du disque ; réanalyser la sauvegarde', 409)
                info = os.stat(dest)
                if (info.st_size, info.st_mtime_ns) != (row['size'], row['mtime_ns']):
                    raise StoreError('Fichier modifié sur le serveur ; suppression refusée', 409)
                trash_id = uuid.uuid4().hex
                now = time.time()
                db.execute('INSERT INTO trash VALUES (?,?,?,?,?,?,?,?)',
                           (trash_id, path, row['size'], row['sha256'], row['source_mtime_ns'],
                            now, now + RETENTION_SECONDS, 'pending'))
            # L'intention est durable avant le déplacement, récupérable après coupure.
            os.replace(dest, os.path.join(self.trash_dir, trash_id + '.data'))
            with self.connection() as db:
                db.execute("UPDATE trash SET state='ready' WHERE id=?", (trash_id,))
                db.execute('DELETE FROM files WHERE path=?', (path,))
            return {'trash_id': trash_id, 'expires_at': now + RETENTION_SECONDS}

    def list_trash(self):
        self.assert_root()
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM trash WHERE state='ready' ORDER BY deleted_at DESC")]

    def restore(self, trash_id):
        if not isinstance(trash_id, str) or not re.fullmatch('[0-9a-f]{32}', trash_id):
            raise StoreError('Identifiant de corbeille invalide')
        with self.connection() as db:
            initial = db.execute('SELECT * FROM trash WHERE id=?', (trash_id,)).fetchone()
        if not initial:
            raise StoreError('Fichier absent de la corbeille', 404)
        with self.path_lock(initial['path']), self.operation():
            with self.connection() as db:
                row = db.execute("SELECT * FROM trash WHERE id=? AND state='ready'", (trash_id,)).fetchone()
                if not row:
                    raise StoreError('Fichier indisponible', 409)
                dest = self.safe_path(row['path'])
                if os.path.lexists(dest):
                    raise StoreError('Un fichier existe déjà à cet emplacement ; restauration refusée', 409)
                source = os.path.join(self.trash_dir, trash_id + '.data')
                if not os.path.isfile(source) or is_link(source):
                    raise StoreError('Fichier de corbeille inaccessible', 409)
                digest, info = file_hash(source)
                if digest != row['sha256'] or info.st_size != row['size']:
                    raise StoreError('Fichier de corbeille corrompu ; restauration refusée', 409)
                db.execute("UPDATE trash SET state='restoring' WHERE id=?", (trash_id,))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            self.safe_path(row['path'])
            os.replace(source, dest)
            info = os.stat(dest)
            with self.connection() as db:
                db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)',
                           (row['path'], info.st_size, info.st_mtime_ns, digest, row['source_mtime_ns']))
                db.execute('DELETE FROM trash WHERE id=?', (trash_id,))
            return {'path': row['path']}

    def recover_moves(self):
        with self.connection() as db:
            # Coupure entre le remplacement atomique et l'enregistrement SQLite.
            for row in db.execute('SELECT * FROM transfers').fetchall():
                if os.path.exists(self.partial_path(row['id'])):
                    continue
                dest = self.safe_path(row['path'])
                if row['received'] == row['size'] and os.path.isfile(dest):
                    digest, info = file_hash(dest)
                    if info.st_size == row['size'] and digest == row['sha256']:
                        db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)',
                                   (row['path'], info.st_size, info.st_mtime_ns, digest,
                                    row['source_mtime_ns']))
                        db.execute('DELETE FROM transfers WHERE id=?', (row['id'],))
                        continue
                db.execute('UPDATE transfers SET received=0 WHERE id=?', (row['id'],))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('ready','0')")
            for row in db.execute("SELECT * FROM trash WHERE state IN ('pending','restoring')").fetchall():
                stored = os.path.join(self.trash_dir, row['id'] + '.data')
                dest = self.safe_path(row['path'])
                if row['state'] == 'pending' and os.path.isfile(stored):
                    db.execute("UPDATE trash SET state='ready' WHERE id=?", (row['id'],))
                    if not os.path.exists(dest):
                        db.execute('DELETE FROM files WHERE path=?', (row['path'],))
                elif row['state'] == 'pending' and os.path.isfile(dest):
                    db.execute('DELETE FROM trash WHERE id=?', (row['id'],))
                elif row['state'] == 'restoring' and os.path.isfile(stored):
                    db.execute("UPDATE trash SET state='ready' WHERE id=?", (row['id'],))
                elif row['state'] == 'restoring' and os.path.isfile(dest):
                    digest, info = file_hash(dest)
                    if digest == row['sha256']:
                        db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)',
                                   (row['path'], info.st_size, info.st_mtime_ns, digest,
                                    row['source_mtime_ns']))
                        db.execute('DELETE FROM trash WHERE id=?', (row['id'],))
                    else:
                        db.execute("INSERT OR REPLACE INTO meta VALUES ('ready','0')")
                else:
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('ready','0')")

    def cleanup(self):
        removed = 0
        with self.operation():
            with self.connection() as db:
                candidates = db.execute("SELECT * FROM trash WHERE state='ready' AND expires_at<=?",
                                        (time.time(),)).fetchall()
            for row in candidates:
                with self.path_lock(row['path']), self.connection() as db:
                    current = db.execute("SELECT * FROM trash WHERE id=? AND state='ready' AND expires_at<=?",
                                         (row['id'], time.time())).fetchone()
                    if current:
                        stored = os.path.join(self.trash_dir, row['id'] + '.data')
                        if os.path.lexists(stored):
                            if is_link(stored):
                                raise StoreError('Fichier de corbeille invalide')
                            os.remove(stored)
                        db.execute('DELETE FROM trash WHERE id=?', (row['id'],))
                        removed += 1
            with self.connection() as db:
                stale = db.execute('SELECT * FROM transfers WHERE updated<?',
                                   (time.time() - 7 * 86400,)).fetchall()
            for row in stale:
                with self.path_lock(row['path']), self.connection() as db:
                    current = db.execute('SELECT * FROM transfers WHERE id=? AND updated<?',
                                         (row['id'], time.time() - 7 * 86400)).fetchone()
                    if current:
                        partial = self.partial_path(row['id'])
                        if os.path.exists(partial):
                            os.remove(partial)
                        db.execute('DELETE FROM transfers WHERE id=?', (row['id'],))
                        with self.space_lock:
                            self.reservations.pop(row['id'], None)
        return removed
