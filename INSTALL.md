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
| Server | Windows, Python 3.8+, NSSM; pinned dependencies provided |
| Network | Both computers connected through Tailscale; matching secret tokens |

Run each set of commands from the project folder on the relevant computer. Update the client and server together, and preserve existing configuration files when upgrading.

## 1. Configure the server

For a new installation, copy `Server/config.example.json` to `Server/config.json`. Set `backup_folder` to the destination drive and choose a private `secret_token`.

```powershell
python -m pip install -r Server/requirements.txt
```

Server tests cover Python 3.8 and 3.11. See the [validation report](TEST_REPORT.md) for the tested environments. Keep the provided dependency versions pinned.

To check the configuration manually, run `python Server/server.py`. Stop it with **Ctrl+C** before setting up the service.

## 2. Run the server as a Windows service

NSSM starts the server with Windows, without signing in, and can restart it after a crash. Waitress handles HTTP requests inside that process. This keeps the offsite server autonomous after reboots and power outages.

**Existing installation:** keep your existing NSSM service and use its actual name instead of `FCSBackupServer` in the commands below. Skip service creation. Stop the service before updating files, preserve `Server/config.json`, and use NSSM's service editor to update the Application paths if the installation moved.

Keep `nssm.exe` in a permanent local folder on the server. Open **PowerShell as Administrator** in that folder. For a new installation, create a service named `FCSBackupServer`:

```powershell
.\nssm.exe install FCSBackupServer
```

In the NSSM window, fill in the **Application** tab:

| NSSM field | Value |
| --- | --- |
| Path | Absolute path to the `python.exe` used to install the server dependencies |
| Startup directory | Absolute path to the deployed `Server` folder |
| Arguments | `server.py` |

Click **Install service**, then enable automatic startup and process recovery:

```powershell
.\nssm.exe set FCSBackupServer Start SERVICE_AUTO_START
.\nssm.exe set FCSBackupServer AppExit Default Restart
.\nssm.exe start FCSBackupServer
```

The service account needs read/write access to the backup drive. Keep `nssm.exe` at its registered location and run a single server instance per port. NSSM restarts the server process; failed client backup runs are not automatically retried. See the [official NSSM guide](https://nssm.cc/usage) for service management details.

## 3. Configure the client

For a new installation, copy `Client/client_config.example.json` to `Client/client_config.json`. Set the server's Tailscale address, port, matching token, source folders, and daily `backup_time`. Source folders must have distinct names.

```powershell
python -m pip install -r Client/requirements-client.txt
python Client/client.py
```

`Client/FCS Backup.vbs` starts the client without a console. To launch it at sign-in, place a shortcut to this file in the Windows Startup folder. Update the shortcut if you move or rename the project folder.

## 4. Index, then back up

Once any initial file copy is complete, select **Réanalyser la sauvegarde** and wait for **Terminé**, then select **Sauvegarder**. Reindex after any manual change to the backup drive. Identical files already on the server are adopted without retransmission.

Reindexing saves verified fingerprints progressively. After an error, cancellation, or service restart, rerun it: unchanged files with saved fingerprints need only metadata checks. A file interrupted during hashing must be read again. The inventory becomes available only after a complete successful pass.

## Backup behavior

> **This is a mirror with temporary recovery.** Removing files from an accessible source folder moves their backup copies to trash. After 48 hours, the running server can permanently purge them.

- Missing or unreadable sources, failed uploads, and stop requests block the deletion phase. Removing a folder from the client configuration preserves its server copy.
- Restoration returns files to the **server**. Copy them back to their source before the next backup if you want to keep them.
- Keep the client running for scheduled backups. Missed runs are not caught up, and failed runs are not automatically retried.
- Keep the HTTP service on the private Tailscale network. Preserve the server's `.fcs2` directory: it holds the inventory, recovery records, and resumable uploads.
- The server handles long Windows paths internally for indexing, transfers, and trash operations, preserving the original filenames and folder structure.

## Validation

With the client and server dependencies installed:

```powershell
python -B -m unittest discover -s tests -v
python -B tools/benchmark_cache.py
```

The latest validation passed **63 tests on Python 3.11** and **58 on Python 3.8**; five interface tests were skipped on the Python 3.8 runtime without Tkinter. Tests use temporary folders and local test services.

See the [validation report](TEST_REPORT.md) and [design decisions](DECISIONS.md) for details.

