"""Mesure reproductible du cache, uniquement dans un dossier temporaire."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Client.backup_engine import BackupEngine, scan_folders


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--files', type=int, default=1000)
    parser.add_argument('--bytes', type=int, default=4096)
    args = parser.parse_args()
    if args.files < 1 or args.bytes < 1:
        parser.error('Les valeurs doivent être positives')
    with tempfile.TemporaryDirectory(prefix='fcs-backup-benchmark-') as temporary:
        base = Path(temporary)
        source = base / 'Photos'
        source.mkdir()
        data = b'a' * args.bytes
        manifest = {}
        for index in range(args.files):
            name = '{}.bin'.format(index)
            (source / name).write_bytes(data)
            manifest['Photos/' + name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        config = {'folders': [str(source)], 'hash_cache_file': str(base / 'hashes.sqlite3')}

        def measure(label):
            reads = 0
            engine = BackupEngine(config, lambda _message: None)
            original_hash = engine._hash_file

            def counted_hash(*values):
                nonlocal reads
                reads += 1
                return original_hash(*values)

            engine._hash_file = counted_hash
            start = time.perf_counter()
            pending = engine._plan(scan_folders(config['folders']), manifest)
            elapsed = time.perf_counter() - start
            engine.close()
            return {'phase': label, 'seconds': round(elapsed, 4),
                    'contents_read': reads, 'files_to_upload': len(pending)}

        results = [measure('initial'), measure('unchanged')]
        modified = source / '0.bin'
        info = modified.stat()
        modified.write_bytes(b'b' * args.bytes)
        os.utime(modified, ns=(info.st_mtime_ns + 1000000000, info.st_mtime_ns + 1000000000))
        results.append(measure('one_same_size_change'))
        assert results[0]['contents_read'] == args.files
        assert results[1]['contents_read'] == 0 and results[1]['files_to_upload'] == 0
        assert results[2]['contents_read'] == 1 and results[2]['files_to_upload'] == 1
        print(json.dumps({'files': args.files, 'bytes_each': args.bytes, 'results': results}, indent=2))


if __name__ == '__main__':
    main()
