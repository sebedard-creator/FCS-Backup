"""Moteur FCS Backup v2, indépendant de Tkinter. Compatible Python 3.8."""

import hashlib
import os
import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy

import requests


PROTOCOL = 2
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class SourceScanError(Exception):
    pass


class BackupStopped(Exception):
    pass


def _ensure_sources_available(folders):
    if not folders:
        raise SourceScanError('Aucun dossier source configuré')
    for folder in folders:
        try:
            with os.scandir(folder) as entries:
                next(entries, None)
        except OSError as error:
            raise SourceScanError('Dossier source inaccessible : {} ({})'.format(folder, error)) from error


def scan_folders(folders, progress_callback=None, stopped=None):
    """Inventaire complet ou erreur ; une erreur os.walk n'est jamais ignorée."""
    folders = [os.path.normpath(folder) for folder in folders]
    _ensure_sources_available(folders)

    def walk_error(error):
        raise error

    def walk_source(folder):
        try:
            for item in os.walk(folder, onerror=walk_error):
                if stopped and stopped():
                    raise BackupStopped()
                yield item
        except OSError as error:
            raise SourceScanError('Analyse incomplète : {} ({})'.format(folder, error)) from error

    total = sum(len(names) for folder in folders for _, _, names in walk_source(folder))
    files = {}
    count = 0
    for folder in folders:
        parent = os.path.dirname(folder)
        for root, _, names in walk_source(folder):
            for name in names:
                if stopped and stopped():
                    raise BackupStopped()
                absolute = os.path.join(root, name)
                relative = os.path.relpath(absolute, parent).replace('\\', '/')
                try:
                    info = os.stat(absolute)
                except OSError as error:
                    raise SourceScanError('Fichier source inaccessible : {} ({})'.format(absolute, error)) from error
                files[relative] = (absolute, info.st_size, info.st_mtime_ns)
                count += 1
                if progress_callback and (count % 500 == 0 or count == total):
                    progress_callback(count, total)
    _ensure_sources_available(folders)
    return files


class HashCache:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('''CREATE TABLE IF NOT EXISTS hashes (
            path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT)''')
        self.entries = {row[0]: row[1:] for row in self.db.execute('SELECT * FROM hashes')}
        self.pending = 0

    @staticmethod
    def key(path):
        return os.path.normcase(os.path.abspath(path))

    def get(self, path, size, mtime_ns):
        row = self.entries.get(self.key(path))
        return row[2] if row and row[:2] == (size, mtime_ns) else None

    def save(self, path, size, mtime_ns, digest):
        key = self.key(path)
        self.db.execute('INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)',
                        (key, size, mtime_ns, digest))
        self.entries[key] = (size, mtime_ns, digest)
        self.pending += 1
        if self.pending >= 100:
            self.db.commit()
            self.pending = 0

    def close(self):
        try:
            self.db.commit()
        finally:
            self.db.close()


class BackupEngine:
    CHUNK_SIZE = 5 * 1024 * 1024

    def __init__(self, config, log_callback, session_factory=None):
        self.config = deepcopy(config)
        self.log = log_callback
        self._stop = threading.Event()
        self._thread_local = threading.local()
        self._sessions = []
        self._sessions_lock = threading.Lock()
        self._session_factory = session_factory or requests.Session
        self._expected = {}
        self._server_manifest = {}
        self.last_result = {'uploaded': 0, 'deleted': 0, 'errors': 0, 'state': 'Analyse'}

    @property
    def stop_requested(self):
        return self._stop.is_set()

    @stop_requested.setter
    def stop_requested(self, value):
        if value:
            self._stop.set()
        else:
            self._stop.clear()

    def _check_stop(self):
        if self.stop_requested:
            raise BackupStopped()

    def _state(self, state, detail=''):
        self.last_result['state'] = state
        callback = getattr(self, 'state_callback', None)
        if callback:
            callback(state, detail)

    def _base_url(self):
        return 'http://{}:{}'.format(self.config['server_ip'], self.config['server_port'])

    def _headers(self):
        return {'X-Secret-Token': self.config['secret_token'], 'X-FCS2-Protocol': str(PROTOCOL)}

    def _session(self):
        if not hasattr(self._thread_local, 'session'):
            session = self._session_factory()
            self._thread_local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return self._thread_local.session

    def close(self):
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()
        self._thread_local = threading.local()

    def _request(self, method, path, timeout=30, **kwargs):
        headers = self._headers()
        headers.update(kwargs.pop('headers', {}))
        response = self._session().request(method, self._base_url() + path,
                                            headers=headers, timeout=(10, timeout), **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get('error', 'Erreur HTTP')
            except (ValueError, AttributeError):
                detail = 'Erreur HTTP'
            raise RuntimeError('{} (HTTP {})'.format(detail, response.status_code))
        return response

    def health_check(self):
        try:
            data = self._request('GET', '/health', timeout=5).json()
            if data.get('protocol') != PROTOCOL:
                raise RuntimeError('Serveur ancien : installer FCS Backup v2 avant de synchroniser')
            self.health_data = data
            callback = getattr(self, 'disk_callback', None)
            if callback:
                callback(data.get('disk', {}))
            return True
        except Exception as error:
            self.log('❌ Connexion au serveur : ' + str(error))
            return False

    def get_server_manifest(self):
        while True:
            self._check_stop()
            response = self._request('GET', '/get_manifest', timeout=300)
            if response.status_code == 200:
                data = response.json()
                if data.get('protocol') != PROTOCOL or not isinstance(data.get('manifest'), dict):
                    raise RuntimeError('Inventaire serveur invalide ou ancien')
                return data['manifest']
            if response.status_code != 202:
                raise RuntimeError('Réponse inattendue pour l’inventaire')
            self._state('Analyse', 'Le serveur réanalyse sa sauvegarde')
            self._stop.wait(1)

    def _hash_file(self, path, size, mtime_ns):
        digest = hashlib.sha256()
        self._verify_source(path, size, mtime_ns)
        with open(path, 'rb') as stream:
            while True:
                self._check_stop()
                block = stream.read(4 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
        self._verify_source(path, size, mtime_ns)
        return digest.hexdigest()

    @staticmethod
    def _verify_source(path, size, mtime_ns):
        info = os.stat(path)
        if (info.st_size, info.st_mtime_ns) != (size, mtime_ns):
            raise SourceScanError('Fichier modifié pendant le cycle : ' + path)

    def upload_file(self, filepath, relative_path):
        try:
            expected = self._expected[relative_path]
            size, mtime_ns, digest = expected['size'], expected['mtime_ns'], expected['sha256']
            self._check_stop()
            self._verify_source(filepath, size, mtime_ns)
            chunk_size = int(self.config.get('chunk_size', self.CHUNK_SIZE))
            if not 1 <= chunk_size <= 16 * 1024 * 1024:
                raise ValueError('Taille de morceau invalide')
            data = self._request('POST', '/upload/start', json={
                'path': relative_path, 'size': size, 'sha256': digest,
                'mtime_ns': mtime_ns, 'chunk_size': chunk_size}).json()
            transfer_id, offset = data['transfer_id'], data['offset']
            if type(offset) is not int or not 0 <= offset <= size:
                raise RuntimeError('Position de reprise invalide')
            if offset:
                self.log('↪ Reprise {} à {} / {} octets'.format(relative_path, offset, size))
            chunk_callback = getattr(self, 'chunk_progress_callback', None)
            with open(filepath, 'rb') as stream:
                stream.seek(offset)
                while offset < size:
                    self._check_stop()
                    block = stream.read(min(chunk_size, size - offset))
                    if not block:
                        raise SourceScanError('Fichier source tronqué : ' + filepath)
                    self._state('Transfert', relative_path)
                    result = self._request('PUT', '/upload/' + transfer_id, timeout=120, data=block,
                                           headers={'X-Offset': str(offset),
                                                    'X-Chunk-Hash': hashlib.sha256(block).hexdigest(),
                                                    'Content-Type': 'application/octet-stream'}).json()
                    offset += len(block)
                    if result.get('offset') != offset:
                        raise RuntimeError('Accusé de réception invalide')
                    if chunk_callback:
                        chunk_callback(relative_path, offset, size)
            self._check_stop()
            self._verify_source(filepath, size, mtime_ns)
            self._state('Vérification', relative_path)
            result = self._request('POST', '/upload/' + transfer_id + '/finalize', timeout=7200).json()
            if not result.get('complete') or result.get('sha256') != digest or result.get('size') != size:
                raise RuntimeError('Validation finale du serveur invalide')
            return True
        except BackupStopped:
            return False
        except Exception as error:
            self.log('❌ Envoi {} : {}'.format(relative_path, error))
            return False

    def delete_file(self, relative_path):
        try:
            entry = self._server_manifest[relative_path]
            self._request('POST', '/delete', json={'path': relative_path, 'sha256': entry['sha256']})
            return True
        except Exception as error:
            self.log('❌ Corbeille {} : {}'.format(relative_path, error))
            return False

    def _plan(self, current, manifest):
        pending = []
        cache = HashCache(self.config.get('hash_cache_file', os.path.join(BASE_DIR, '.fcs2_client.sqlite3')))
        try:
            for relative, (path, size, mtime_ns) in current.items():
                self._check_stop()
                digest = cache.get(path, size, mtime_ns)
                if digest is None:
                    self._state('Analyse', 'Empreinte : ' + relative)
                    digest = self._hash_file(path, size, mtime_ns)
                    cache.save(path, size, mtime_ns, digest)
                self._expected[relative] = {'size': size, 'mtime_ns': mtime_ns, 'sha256': digest}
                remote = manifest.get(relative)
                if not remote or remote.get('size') != size or remote.get('sha256') != digest:
                    pending.append((relative, path))
        finally:
            cache.close()
        return pending

    def _upload_pending(self, pending):
        workers = int(self.config.get('parallel_transfers', 2))
        if not 1 <= workers <= 4:
            raise ValueError('Le nombre de transferts doit être compris entre 1 et 4')
        iterator = iter(pending)
        active = {}
        finished = 0
        failed = False
        with ThreadPoolExecutor(max_workers=workers) as executor:
            while True:
                while len(active) < workers and not self.stop_requested and not failed:
                    try:
                        relative, path = next(iterator)
                    except StopIteration:
                        break
                    self.log('↑ ' + relative)
                    active[executor.submit(self.upload_file, path, relative)] = relative
                if not active:
                    break
                completed, _ = wait(active, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in completed:
                    relative = active.pop(future)
                    try:
                        uploaded = future.result()
                    except Exception as error:
                        self.log('❌ Envoi {} : {}'.format(relative, error))
                        uploaded = False
                    if uploaded:
                        self.last_result['uploaded'] += 1
                    elif not self.stop_requested:
                        self.last_result['errors'] += 1
                        failed = True
                    finished += 1
                    callback = getattr(self, 'file_progress_callback', None)
                    if callback:
                        callback(finished, len(pending))

    def _run_backup(self):
        folders = [os.path.normpath(folder) for folder in self.config['folders']]
        _ensure_sources_available(folders)
        self._check_stop()
        if not self.health_check():
            raise RuntimeError('Serveur inaccessible ou incompatible')
        manifest = self.get_server_manifest()
        self._server_manifest = manifest
        self.log('📋 Serveur : {} fichiers'.format(len(manifest)))
        self._state('Analyse', 'Dossiers sources')
        current = scan_folders(folders, getattr(self, 'progress_callback', None), self._stop.is_set)
        self.log('📁 {} fichiers sources scannés'.format(len(current)))
        pending = self._plan(current, manifest)
        self.log('📦 {} fichier(s) à transférer'.format(len(pending)))
        self._upload_pending(pending)
        self._check_stop()
        if self.last_result['errors']:
            return False
        _ensure_sources_available(folders)
        prefixes = tuple(os.path.basename(folder) + '/' for folder in folders if os.path.basename(folder))
        for relative in manifest:
            self._check_stop()
            if relative.startswith(prefixes) and relative not in current:
                self._state('Corbeille', relative)
                self.log('♻ ' + relative)
                if not self.delete_file(relative):
                    self.last_result['errors'] += 1
                    return False
                self.last_result['deleted'] += 1
        return True

    def run_backup(self):
        self.log('─── Début du backup ───')
        self._state('Analyse')
        success = False
        try:
            success = self._run_backup()
        except BackupStopped:
            pass
        except Exception as error:
            self.last_result['errors'] += 1
            self.log('❌ Backup interrompu : ' + str(error))
        finally:
            self.close()
        state = 'Arrêté' if self.stop_requested else ('Terminé' if success else 'Échec')
        self._state(state)
        self.log('{} — {} envoyés, {} mis en corbeille, {} erreur(s)'.format(
            state, self.last_result['uploaded'], self.last_result['deleted'], self.last_result['errors']))
        if not success and not self.last_result['deleted']:
            self.log('Aucune suppression effectuée.')
        return success

    def run_reindex(self):
        success = False
        try:
            if not self.health_check():
                raise RuntimeError('Serveur inaccessible ou incompatible')
            self._request('POST', '/reindex')
            cancel_sent = False
            while True:
                if self.stop_requested and not cancel_sent:
                    self._request('POST', '/reindex/cancel')
                    cancel_sent = True
                data = self._request('GET', '/reindex_status').json()
                self._state('Analyse serveur', '{} fichiers, {} empreintes calculées — {}'.format(
                    data.get('count', 0), data.get('hashed', 0), data.get('path', '')))
                if not data.get('running'):
                    if data.get('error'):
                        raise RuntimeError(data['error'])
                    success = not data.get('cancelled') and not self.stop_requested
                    break
                # Ne pas utiliser l'événement déjà déclenché : éviter une boucle occupée.
                threading.Event().wait(0.5)
        except Exception as error:
            self.log('❌ Réanalyse : ' + str(error))
        finally:
            self.close()
        self._state('Arrêté' if self.stop_requested else ('Terminé' if success else 'Échec'))
        self.log('Réanalyse ' + ('terminée' if success else 'arrêtée ou échouée'))
        return success

    def get_trash(self):
        return self._request('GET', '/trash').json()['files']

    def restore(self, trash_id):
        return self._request('POST', '/trash/restore', timeout=7200, json={'id': trash_id}).json()
