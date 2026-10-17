# Installation guide

[← Back to FCS Backup](README.md)

For offsite protection, keep the backup server at a different location from the client. Both computers must be running and connected when a backup takes place.

## Project layout

```text
FCS Backup/
├── Client/     Desktop application, launcher, and client configuration
├── Server/     Backup API, storage engine, and server configuration
├── docs/       Screenshots
├── tests/      Automated checks
└── tools/      Release packaging and cache benchmark
```

Configurations, logs, and the client hash cache stay beside their respective application. Application paths are resolved from the files themselves, so the top-level folder can be renamed.

## Requirements

| Component | Requirements |
| --- | --- |
| Client | Windows, Python 3.10+, Tkinter |
| Server | Python 3.8; pinned dependencies provided for the Windows 7 target |
| Network | Both computers connected through Tailscale; matching secret tokens |

Run each set of commands from the project folder on the relevant computer. Update the client and server together, and preserve existing configuration files when upgrading.

## 1. Configure the server

For a new installation, copy `Server/config.example.json` to `Server/config.json`. Set `backup_folder` to the destination drive and choose a private `secret_token`.

```powershell
python -m pip install -r Server/requirements-win7.txt
python Server/server.py
```

Use Python 3.8 on the Windows 7 server. Compatibility tests run on a development PC; deployment on the actual Windows 7 machine still requires validation.

## 2. Configure the client

For a new installation, copy `Client/client_config.example.json` to `Client/client_config.json`. Set the server's Tailscale address, port, matching token, source folders, and daily `backup_time`. Source folders must have distinct names.

```powershell
python -m pip install -r Client/requirements-client.txt
python Client/client.py
```

`Client/FCS Backup.vbs` starts the client without a console. To launch it at sign-in, place a shortcut to this file in the Windows Startup folder. Update the shortcut if you move or rename the project folder.

## 3. Index, then back up

Once any initial file copy is complete, select **Réanalyser la sauvegarde** and wait for **Terminé**, then select **Sauvegarder**. Reindex after any manual change to the backup drive. Identical files already on the server are adopted without retransmission.

## Backup behavior

> **This is a mirror with temporary recovery.** Removing files from an accessible source folder moves their backup copies to trash. After 48 hours, the running server can permanently purge them.

- Missing or unreadable sources, failed uploads, and stop requests block the deletion phase. Removing a folder from the client configuration preserves its server copy.
- Restoration returns files to the **server**. Copy them back to their source before the next backup if you want to keep them.
- Keep the client running for scheduled backups. Missed runs are not caught up, and failed runs are not automatically retried.
- Keep the HTTP service on the private Tailscale network. Preserve the server's `.fcs2` directory: it holds the inventory, recovery records, and resumable uploads.

## Validation

With the client and server dependencies installed:

```powershell
python -B -m unittest discover -s tests -v
python -B tools/benchmark_cache.py
```

The latest validation passed **55 tests on Python 3.11** and **50 on Python 3.8**; five interface tests were skipped on the Python 3.8 runtime without Tkinter. Tests use temporary folders and local test services.

See the [validation report](TEST_REPORT.md) and [design decisions](DECISIONS.md) for details.

