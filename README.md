# ScreenView — Lightweight Digital Signage (Xibo alternative)

ScreenView is a resilient, offline-first digital signage system with a
"Store & Forward" architecture. Players never stream from the web; they
keep playing their last validated cache indefinitely, and only swap in a
new playlist once every file has been downloaded and MD5-verified in the
background.

```
┌───────────────┐   WebSocket (signals)   ┌──────────────────┐
│  CMS (Vue)    │◀──────────────────────▶│  FastAPI server  │
│  /cms-frontend│                         │     /server      │
└───────▲───────┘                         └────────▲─────────┘
        │REST (admin)                              │REST (manifest + downloads)
        │                                          │WebSocket (sync triggers)
        │                              ┌───────────┴───────────┐
        │                              │                       │
        │                    ┌─────────┴─────────┐   ┌─────────┴──────────┐
        │                    │  Player (Linux)   │   │ Player (Windows)   │
        │                    │   PyQt6 + mpv     │   │   PyQt6 + mpv      │
        │                    │  /player-linux    │   │  /player-windows   │
        │                    └───────────────────┘   └────────────────────┘
```

## Repository layout

```
/digital-signage-project
├── server/            FastAPI backend (REST + WebSocket + uploads)
├── cms-frontend/      Vue 3 + Vite single-page admin UI
├── player-linux/      PyQt6 kiosk player for Linux (systemd watchdog)
└── player-windows/    PyQt6 kiosk player for Windows (Task Scheduler)
```

## Quick start

### 1. Backend

```bash
cd server
pip install -r requirements.txt
python -m server                        # listens on http://localhost:8000
```

The first run creates `server/screenview.db` (SQLite) and ensures
`server/uploads/` exists. Default admin credentials:

| User    | Password |
|---------|----------|
| `admin` | `admin`  |

Override them with environment variables before first boot:

```bash
SCREENVIEW_ADMIN_USERNAME=operator \
SCREENVIEW_ADMIN_PASSWORD='s3cret!' \
SCREENVIEW_SECRET_KEY=$(openssl rand -hex 32) \
python -m server
```

API docs are served from `http://localhost:8000/docs` (auto-generated
Swagger).

### 2. CMS frontend

```bash
cd cms-frontend
npm install
npm run dev      # http://localhost:5173 (proxies /api to :8000)
npm run build    # produces cms-frontend/dist — auto-served by the backend
```

When `cms-frontend/dist/` exists, the FastAPI server mounts it at `/` so a
production deployment only needs a single service.

### 3. Player (Linux)

```bash
cd player-linux
pip install -r requirements.txt

# Edit config.json: set "server_url" to the CMS host.
python main.py
```

On first launch the player auto-registers with the server (POST
`/api/register`) and waits in "pending" state until an administrator
approves it in the CMS. Until approved the screen shows a branded
placeholder frame — never a black screen or an error.

For production kiosks install `screenview-player.service` under
`/etc/systemd/system/` for a `Restart=always` watchdog:

```bash
sudo cp player-linux/screenview-player.service /etc/systemd/system/
sudo systemctl enable --now screenview-player.service
```

### 4. Player (Windows)

```powershell
cd player-windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Edit config.json: set "server_url" to the CMS host, then:
python main.py
```

The Windows player has identical behaviour to the Linux one, plus
Windows-specific integration:

- Stable machine identifier via the Windows registry (`MachineGuid`)
  with `wmic csproduct` and MAC fallbacks.
- Persistent state under `%LOCALAPPDATA%\ScreenView\` (config, cache,
  rotating logs).
- Per-monitor v2 DPI awareness so fullscreen matches the physical screen.
- `SetThreadExecutionState` keeps the display and system awake.
- Named-mutex single-instance guard, hidden subprocess windows.
- Disabled `Alt+F4` / `Ctrl+W` kiosk escape shortcuts.

To build a standalone `.exe` and register the auto-restart Task Scheduler
entry (Windows equivalent of `Restart=always`):

```powershell
# Build ScreenViewPlayer.exe (drop libmpv-2.dll next to the .spec first).
.\player-windows\scripts\build.ps1

# Then on each kiosk, from an elevated PowerShell:
.\player-windows\scripts\install.ps1 `
    -InstallDir "C:\Program Files\ScreenView" `
    -ServerUrl  "https://signage.example.com"
```

See `player-windows/README.md` for the full kiosk lockdown checklist.

## Architectural principles

1. **Store & Forward.** The player never renders a file streamed from the
   network. Media is downloaded, MD5-verified, then played from disk.
2. **Separation of channels.** WebSocket carries light signals (`ping`,
   `sync_required`). REST carries heavy payloads (manifest JSON + file
   downloads).
3. **Offline-first.** If the server is unreachable the player loops its
   last cached playlist forever; reconnection is fully idempotent.
4. **Strict thread separation on the player.** The `QThread` worker owns
   all I/O; the UI thread only renders. They communicate through PyQt
   signals — see `player-linux/worker_network.py` and
   `player-windows/worker_network.py`.
5. **Atomic playlist swaps.** A newly downloaded playlist only takes
   effect at the end of the currently playing media, so the viewer never
   sees partial or broken content.

## Synchronisation workflow

1. Admin edits a schedule in the CMS and clicks **Publish**.
2. Server emits `{"action": "sync_required"}` on the per-device WebSocket.
3. Player's worker receives the signal and GETs
   `/api/schedule/{device_id}` — a JSON manifest with URLs + MD5 hashes.
4. Worker diffs the manifest against its local cache, downloads the
   missing items, and verifies each MD5.
5. On success, the worker emits `playlist_ready` to the UI thread.
6. The UI thread queues the new list and switches at the end of the
   current media (gapless).

## Data model

Defined in `server/models.py` (SQLModel):

- **Device** — registered player, status, assigned schedule, last ping.
- **Media** — uploaded file (video/image/widget) with MD5 + default duration.
- **Schedule** — named playlist referenced by one or more devices.
- **ScheduleItem** — ordered join row allowing per-item duration overrides.

## Tests

```bash
# Backend
pip install pytest httpx
python -m pytest server/tests

# Players (pure helpers; PyQt tests auto-skip without a display)
python -m pytest player-linux/tests
python -m pytest player-windows/tests
```

## Roadmap

- [ ] Signed media download URLs (per-device HMAC).
- [ ] Per-device API tokens (no more UUID-as-shared-secret).
- [ ] Live preview of schedules in the CMS.
- [ ] PostgreSQL migration path once SQLite becomes a bottleneck.
- [ ] HLS / RTSP live streams as an opt-in media type.

## License

TBD.
