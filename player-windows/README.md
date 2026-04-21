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
| `libmpv-2.dll` provisioning    | Auto-downloaded from `zhongfly/mpv-winbuild`; extracted using an auto-bootstrapped `7zr.exe` from 7-zip.org (handles BCJ2); cached under `%LOCALAPPDATA%\ScreenView\libmpv\` |
| Broken media handling          | Render failures are counted per item; after 2 failures the item is skipped for this playlist, and if every item fails the placeholder is shown until a new manifest arrives |

Everything else (registration, manifest diff, MD5 verify, atomic
playlist swap, offline loop, placeholder frame) is shared line-for-line
with the Linux player.

## Requirements

- Windows 10 1903 or later (Windows 11 recommended for best WebView2).
- Python 3.10–3.12 (only needed for source runs or PyInstaller builds).
- `libmpv-2.dll` (x64) for video playback.
  **The player fetches it automatically on first launch** when it is
  missing, dropping the DLL into `%LOCALAPPDATA%\ScreenView\libmpv\`.
  Extraction bootstraps the official **`7zr.exe`** (~600 KB, signed) from
  <https://www.7-zip.org/a/7zr.exe> on first use and caches it next to
  the DLL. `7zr.exe` handles the **BCJ2** filter used by the mpv release
  archives, which pure-Python 7z libraries do not support.
  For airgapped kiosks, set `"libmpv_auto_download": false` in
  `config.json` and provide the DLL manually via one of:
  - Drop `libmpv-2.dll` (or `mpv-2.dll`) next to `ScreenViewPlayer.exe`
    or `main.py`.
  - Run `.\scripts\fetch-libmpv.ps1` once during provisioning.
  - Set `"libmpv_dir"` in `config.json` to a directory containing the DLL.

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
administrator approves it and assigns a schedule in the CMS. If
`libmpv-2.dll` is missing, the first boot also downloads it
automatically (a one-off ~30 MB fetch from the official
[zhongfly/mpv-winbuild](https://github.com/zhongfly/mpv-winbuild)
release). The DLL is cached under
`%LOCALAPPDATA%\ScreenView\libmpv\` and reused on subsequent launches.

### Fetch libmpv manually

If auto-download is disabled or fails (corporate proxy, air-gapped
network, GitHub rate-limit), grab the DLL yourself:

```powershell
.\scripts\fetch-libmpv.ps1
# Or target a specific directory:
.\scripts\fetch-libmpv.ps1 -TargetDir "C:\Program Files\ScreenView"
```

The script extracts `libmpv-2.dll` from the latest `mpv-dev-x86_64-*.7z`
release asset and copies it next to the player. If no local `7z.exe` /
`7zr.exe` is available, it downloads the official standalone `7zr.exe`
from <https://www.7-zip.org/a/7zr.exe> (signed, ~600 KB) and caches it
under `%LOCALAPPDATA%\ScreenView\libmpv\` for reuse.

## Build a standalone `.exe`

```powershell
cd player-windows

.\scripts\build.ps1
# -> dist\ScreenViewPlayer.exe
```

The build script automatically invokes `fetch-libmpv.ps1` when
`libmpv-2.dll` is not already in the project root, so the resulting
executable is self-contained. Copy it to `C:\Program Files\ScreenView\`
(or anywhere else) on the target kiosk.

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

## Self-healing when the server forgets the device

If the CMS database is reset, the device is deleted from the admin UI,
or the kiosk's cached `device_id` otherwise becomes unknown to the
server, the player detects it via two signals:

- `GET /api/schedule/{device_id}` responds **404**.
- The WebSocket connection gets closed with code **4404**
  (`unknown device`). Older server builds that pre-date this fix
  closed the handshake with HTTP 403; the player treats both forms as
  "re-register" triggers.

When either happens, the worker:

1. Logs a single `Re-registering device: …` line (no tight loop).
2. Clears `device_id` / `device_name` from `config.json`.
3. POSTs `/api/register` again with the kiosk's MAC + `MachineGuid`.
4. Resumes normal sync / WebSocket loop.

The admin then sees a new **pending** device in the CMS, approves it,
and re-assigns a schedule. No console access required on the kiosk.

## WebSocket reconnect

On transient network loss, the WebSocket loop uses exponential
backoff between attempts, starting at 5 s and capped at 5 min. Each
successful connection resets the delay. Identical consecutive error
messages are logged only once instead of spamming the file every
few seconds.

## Troubleshooting

- **"Content unavailable" placeholder on video items:** `libmpv-2.dll`
  is missing or mismatched. The player no longer burns a CPU core
  retrying a broken item — it skips past it, and if every item in the
  playlist fails it holds on the placeholder until a new manifest
  arrives. To resolve:
  - Run `.\scripts\fetch-libmpv.ps1` manually, or
  - Confirm auto-download worked by checking
    `%LOCALAPPDATA%\ScreenView\libmpv\libmpv-2.dll`, or
  - Ensure `"libmpv_auto_download": true` in `config.json` and that
    the kiosk can reach `api.github.com` / `objects.githubusercontent.com`.
- **Stuck in "Waiting for schedule…":** the device is still `pending`
  approval in the CMS. Approve it and assign a schedule.
- **Duplicate windows after logon:** a previous process is still alive.
  The mutex guard exits the second instance cleanly; check Task
  Manager for ghost `ScreenViewPlayer.exe` entries.
- **HTTPS certificate errors from a self-signed CMS:** install your
  private CA root into the Windows certificate store (or use a proper
  public certificate via Let's Encrypt).
- **Auto-download of libmpv fails on first boot:** likely a firewall,
  proxy, or rate-limit issue. Hosts the player needs to reach:
  - `api.github.com` (release metadata)
  - `objects.githubusercontent.com` (release asset download)
  - `www.7-zip.org` (bootstrap of `7zr.exe`, only once per machine)

  The player still boots even when all three are blocked — images and
  widgets keep working, and failures are logged at WARNING level
  without crashing the UI thread. To fix: fetch the DLL from a machine
  with Internet access and copy it into the install dir, or set
  `"libmpv_dir"` in `config.json` to a UNC path.
- **`BCJ2 filter is not supported by py7zr` in logs:** informational
  only. The current mpv release archives use BCJ2 and `py7zr` cannot
  decode them. The player falls back to the bootstrapped `7zr.exe`
  automatically; no action needed.
- **Repeated WebSocket 403 / 4404 in the log:** the server doesn't
  know this device. Normally self-healed by automatic re-registration;
  if it persists, delete `%LOCALAPPDATA%\ScreenView\config.json` and
  restart the player, or confirm the CMS hasn't filtered out the
  device MAC.

## Tests

```powershell
pip install pytest
python -m pytest tests
```

The helper tests cover config seeding/round-trip, cache/log paths,
hardware-ID derivation, and WS URL scheme conversion. Full UI tests
require PyQt6 and are skipped automatically if the module cannot be
imported (e.g. in headless CI without Qt platform plugins).
