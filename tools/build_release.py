"""Archive de mise à jour, sans configuration personnelle, journal ni média."""
import hashlib
from pathlib import Path
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    names = ['.gitignore', 'README.md', 'INSTALL.md', 'LICENSE', 'docs/images/fcs-backup.png',
             'DECISIONS.md', 'TEST_REPORT.md',
             'Client/client.py', 'Client/backup_engine.py', 'Client/FCS Backup.vbs',
             'Client/requirements-client.txt', 'Client/client_config.example.json',
             'Server/server.py', 'Server/backup_store.py',
             'Server/requirements.txt', 'Server/config.example.json',
             'tools/benchmark_cache.py', 'tools/build_release.py']
    names.extend(path.relative_to(root).as_posix() for path in sorted((root / 'tests').glob('test_*.py')))
    output = root / 'dist' / 'FCS-Backup-v2.zip'
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            source = root / name
            if not source.is_file():
                raise FileNotFoundError(source)
            archive.write(source, 'FCS-Backup-v2/' + name)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert all(not name.endswith(('client_config.json', '/config.json', '.log', '.sqlite3'))
                   for name in archive.namelist())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  FCS-Backup-v2.zip\n', encoding='ascii')
    print(str(output))
    print('{} fichiers ; {} octets'.format(len(names), output.stat().st_size))
    print('SHA-256 : ' + digest)


if __name__ == '__main__':
    main()
