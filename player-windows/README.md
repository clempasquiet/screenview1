# ScreenView — Windows player

Windows build of the ScreenView digital-signage player. Functionally
identical to `player-linux/`: same store-and-forward contract, same
strict UI/worker `QThread` separation, same WebSocket protocol against
the ScreenView server. Only the platform integration differs.

## What's Windows-specific

| Concern                        | Implementation                                                                 |
|--------------------------------|---------------------------------------------------------------------------------|
| Stable machine identifier      | `HKLM\Software\Microsoft\Cryptography\MachineGuid` → `wmic csproduct` → MAC     |
| Persistent state location      | `%LOCALAPPDATA%\ScreenView\{config.json, cache, logs}`                          |
| DPI handling                   | `SetProcessDpiAwarenessContext(PER_MONITOR_V2)` at startup                      |
| Keep the screen on             | `SetThreadExecutionState(ES_CONTINUOUS \| ES_DISPLAY_REQUIRED \| ES_SYSTEM_REQUIRED)` |
| Auto-restart watchdog          | Task Scheduler entry, `-Restart` replacement for `systemd Restart=always`       |
| Single instance guard          | Named global mutex `Global\ScreenViewPlayer`                                    |
| Silent subprocesses (WMIC)     | `CREATE_NO_WINDOW` flag                                                         |
| Packaging                      | PyInstaller single-file GUI `.exe` (`console=False`)                            |
| Kiosk lockdown                 | Frameless + always-on-top + `BlankCursor`; `Alt+F4`/`Ctrl+W` shortcuts disabled |

Everything else (registration, manifest diff, MD5 verify, atomic
playlist swap, offline loop, placeholder frame) is shared line-for-line
with the Linux player.

## Requirements

- Windows 10 1903 or later (Windows 11 recommended for best WebView2).
- Python 3.10–3.12 (only needed for source runs or PyInstaller builds).
- `libmpv-2.dll` (x64) for video playback.
  - Download a recent release from <https://sourceforge.net/projects/mpv-player-windows/files/libmpv/>
  - Drop `libmpv-2.dll` (or `mpv-2.dll`) next to `ScreenViewPlayer.exe`
    (or next to `main.py` for source runs). The player can also resolve
    it from a user-configured directory via `config.libmpv_dir`.

## Run from source (development)

```powershell
cd player-windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Edit config.json: set "server_url" to your CMS host
python main.py
```

On first launch the player auto-registers with the server
(`POST /api/register`) and shows a branded placeholder until an
administrator approves it and assigns a schedule in the CMS.

## Build a standalone `.exe`

```powershell
cd player-windows

# Put libmpv-2.dll here first so PyInstaller bundles it.
Copy-Item C:\path\to\libmpv-2.dll .

.\scripts\build.ps1
# -> dist\ScreenViewPlayer.exe
```

The resulting executable is self-contained; copy it to
`C:\Program Files\ScreenView\` (or anywhere else) on the target kiosk.

## Install as a kiosk (Task Scheduler)

Run from an **elevated** PowerShell session on the kiosk machine:

```powershell
.\install.ps1 `
    -InstallDir "C:\Program Files\ScreenView" `
    -ServerUrl  "https://signage.example.com"
```

What the script does:

1. Optionally seeds `%LOCALAPPDATA%\ScreenView\config.json` with the
   given `ServerUrl`.
2. Registers a scheduled task named **ScreenView Player** that:
   - Starts at user logon **and** at machine boot (30 s delay).
   - Runs with the highest available privileges of the kiosk user.
   - Restarts every minute if the process exits (up to 9999 times —
     effectively a Windows equivalent of `Restart=always`).
   - Ignores battery state so the player survives short UPS events.
   - Has no execution time limit.

An equivalent declarative XML definition is shipped at
`scripts\ScreenViewPlayer.xml` if you'd rather import it with
`schtasks /XML`.

Uninstall with `.\uninstall.ps1 [-PurgeData]`.

## Files and directories at runtime

```
%LOCALAPPDATA%\ScreenView\
├── config.json          persistent config (device_id, server_url, …)
├── cache\               md5-named media cache (managed by the worker)
└── logs\
    └── player.log       rotated; 2 MiB × 3 backups
```

The cache files are named after the manifest's MD5 hashes, so they are
content-addressable: two schedules referencing the same media share a
single file, and corrupted downloads are always detected before use.

## Kiosk-mode checklist

For a truly infallible kiosk we recommend, in addition to the player:

- Dedicate a low-privilege local user (e.g. `signage`) and enable
  automatic logon for that account
  (`netplwiz` → uncheck "Users must enter a user name and password").
- Configure Windows to never sleep / hibernate on AC power:
  `powercfg /change standby-timeout-ac 0` and `/monitor-timeout-ac 0`.
  (The player additionally calls `SetThreadExecutionState` as a
  belt-and-braces guard.)
- Disable lock screen timeout via `gpedit.msc` or the registry.
- Disable Windows Update automatic restarts during business hours.
- Hide the taskbar + desktop icons under the `signage` user profile.
- Optionally enable [Assigned Access](https://learn.microsoft.com/windows/configuration/assigned-access)
  or Shell Launcher to make `ScreenViewPlayer.exe` the explicit shell.

## Troubleshooting

- **Black / placeholder screen on videos:** `libmpv-2.dll` missing or
  mismatched architecture. Download the x64 build and place it next to
  the exe, or set `"libmpv_dir"` in `config.json`.
- **Stuck in "Waiting for schedule…":** the device is still `pending`
  approval in the CMS. Approve it and assign a schedule.
- **Duplicate windows after logon:** a previous process is still alive.
  The mutex guard exits the second instance cleanly; check Task
  Manager for ghost `ScreenViewPlayer.exe` entries.
- **HTTPS certificate errors from a self-signed CMS:** install your
  private CA root into the Windows certificate store (or use a proper
  public certificate via Let's Encrypt).

## Tests

```powershell
pip install pytest
python -m pytest tests
```

The helper tests cover config seeding/round-trip, cache/log paths,
hardware-ID derivation, and WS URL scheme conversion. Full UI tests
require PyQt6 and are skipped automatically if the module cannot be
imported (e.g. in headless CI without Qt platform plugins).
