"""API FCS Backup v2, Python 3.8 / Windows 7. Aucun disque ouvert a l'import."""

import errno
import hmac
import json
import logging
import os
import platform
import threading
from logging.handlers import RotatingFileHandler

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from backup_store import BackupStore, MAX_CHUNK_SIZE, RETENTION_SECONDS, StoreError

PROTOCOL = 2


def create_app(config):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = MAX_CHUNK_SIZE
    store = BackupStore(config['backup_folder'], config.get('min_free_bytes', 64 * 1024 * 1024))
    app.extensions['backup_store'] = store
    secret = config['secret_token']
    if not isinstance(secret, str) or not secret:
        raise ValueError('Un jeton serveur non vide est obligatoire')

    @app.before_request
    def authenticate():
        provided = request.headers.get('X-Secret-Token', '')
        if not hmac.compare_digest(provided.encode('utf-8'), secret.encode('utf-8')):
            raise StoreError('Non autorisé', 401)
        if request.headers.get('X-FCS2-Protocol') != str(PROTOCOL):
            raise StoreError('Mettre à jour le client FCS Backup : protocole v2 requis', 426)

    @app.errorhandler(StoreError)
    def store_error(error):
        return jsonify(error=str(error), protocol=PROTOCOL), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        logging.exception('Erreur API %s', request.path)
        status = 507 if isinstance(error, OSError) and error.errno == errno.ENOSPC else 500
        message = ('Espace disque insuffisant ; ancienne copie conservée' if status == 507
                   else 'Erreur serveur ; consulter server.log')
        return jsonify(error=message), status

    def payload():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise StoreError('Objet JSON requis')
        return value

    @app.get('/health')
    def health():
        return jsonify(status='OK', protocol=PROTOCOL, python=platform.python_version(),
                       ready=store.ready(), indexing=store.reindex_status(), disk=store.disk_info())

    @app.get('/get_manifest')
    def get_manifest():
        manifest = store.manifest()
        return jsonify(status='OK', protocol=PROTOCOL, manifest=manifest, count=len(manifest))

    @app.post('/reindex')
    def reindex():
        return jsonify(store.start_reindex()), 202

    @app.get('/reindex_status')
    def reindex_status():
        return jsonify(store.reindex_status())

    @app.post('/reindex/cancel')
    def cancel_reindex():
        store.cancel_reindex()
        return jsonify(status='Arrêt demandé')

    @app.post('/upload/start')
    def start_upload():
        data = payload()
        return jsonify(store.start_transfer(data.get('path'), data.get('size'), data.get('sha256'),
                                             data.get('mtime_ns'), data.get('chunk_size')))

    @app.put('/upload/<transfer_id>')
    def upload_chunk(transfer_id):
        try:
            offset = int(request.headers.get('X-Offset', '-1'))
        except ValueError:
            raise StoreError('Position invalide')
        data = request.get_data(cache=False)
        return jsonify(store.append_chunk(transfer_id, offset, data, request.headers.get('X-Chunk-Hash')))

    @app.post('/upload/<transfer_id>/finalize')
    def finalize_upload(transfer_id):
        return jsonify(store.finish_transfer(transfer_id))

    @app.post('/delete')
    def delete():
        data = payload()
        result = store.move_to_trash(data.get('path'), data.get('sha256'))
        logging.info('Mis en corbeille pour 48 h : %s', data['path'])
        return jsonify(result)

    @app.get('/trash')
    def list_trash():
        return jsonify(files=store.list_trash(), retention_seconds=RETENTION_SECONDS)

    @app.post('/trash/restore')
    def restore():
        result = store.restore(payload().get('id'))
        logging.info('Restauré depuis la corbeille : %s', result['path'])
        return jsonify(result)

    @app.post('/cleanup')
    def cleanup():
        return jsonify(purged=store.cleanup())

    return app


def main():
    directory = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(directory, 'config.json'), encoding='utf-8-sig') as stream:
        config = json.load(stream)
    handler = RotatingFileHandler(os.path.join(directory, 'server.log'),
                                  maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(handlers=[handler], level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    app = create_app(config)
    store = app.extensions['backup_store']

    def maintenance():
        while True:
            try:
                store.cleanup()
            except StoreError as error:
                if error.status != 409:
                    logging.warning('Maintenance : %s', error)
            except Exception:
                logging.exception('Erreur de maintenance')
            threading.Event().wait(60)

    from waitress import serve
    threading.Thread(target=maintenance, daemon=True).start()
    logging.info('FCS Backup v2 démarré ; Python %s ; inventaire prêt=%s',
                 platform.python_version(), store.ready())
    serve(app, host=config.get('host', '0.0.0.0'), port=config.get('port', 5001),
          threads=6, channel_timeout=7200, max_request_body_size=MAX_CHUNK_SIZE)


if __name__ == '__main__':
    main()
