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
6. **Per-device credentials + HMAC-signed media URLs.** Each player is
   issued an opaque `api_token` at registration. Every REST and
   WebSocket call authenticates with it, and the manifest returns
   pre-signed download URLs whose HMAC binds `device_id`, `media_id`
   and an `exp` timestamp to the device's own token. A leaked URL is
   unusable after expiry, from a different device, or after the
   operator rotates the token from the CMS.

## Security model

| Transport        | Credential                                                   | Behaviour on failure                     |
|------------------|--------------------------------------------------------------|------------------------------------------|
| Admin REST       | JWT from `/api/auth/login`                                   | 401                                      |
| Player REST      | `Authorization: Bearer <api_token>`                          | 401 → player clears config, re-registers |
| Player WebSocket | `?token=<api_token>` query parameter                         | Close code 4401 → same recovery flow     |
| Player download  | Pre-signed `?device_id&exp&sig` URL                          | 403 on bad sig, 403 on expiry            |
| Admin preview    | Pre-signed `?admin_exp&admin_sig` URL (server's secret_key)  | 403 on bad sig, 403 on expiry            |
| Unknown device   | any                                                          | 404 REST / close 4404 WS                 |

Token rotation is available from the **Devices** view in the CMS.
Rotating a token invalidates every outstanding manifest for that device;
the player transparently recovers on its next call.

The **Live preview** in the CMS uses short-lived admin-signed download
URLs (default TTL: 15 min) so browsers can embed media directly in
`<img>` / `<video>` tags without attaching the admin JWT to each
request. Rotating `SCREENVIEW_SECRET_KEY` invalidates every outstanding
preview link.

## Synchronisation workflow

1. Admin edits a schedule in the CMS and clicks **Publish**.
2. Server emits `{"action": "sync_required"}` on the per-device WebSocket.
3. Player's worker receives the signal and GETs
   `/api/schedule/{device_id}` with `Authorization: Bearer <api_token>` —
   a JSON manifest containing, for every item, a pre-signed download URL
   (`?device_id&exp&sig`) and the MD5 hash.
4. Worker diffs the manifest against its local cache, downloads the
   missing items (no extra auth header needed — the URL itself is a
   credential), and verifies each MD5.
5. On success, the worker emits `playlist_ready` to the UI thread.
6. The UI thread queues the new list and switches at the end of the
   current media (gapless).

## Data model

Defined in `server/models.py` (SQLModel):

- **Device** — registered player, status, assigned schedule, last ping,
  per-device `api_token` (rotated at registration and from the CMS).
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

- [x] Per-device API tokens (replaces UUID-as-shared-secret).
- [x] Signed media download URLs (per-device HMAC with expiry).
- [x] Live preview of schedules in the CMS (admin-signed HMAC URLs).
- [ ] PostgreSQL migration path once SQLite becomes a bottleneck.
- [ ] HLS / RTSP live streams as an opt-in media type.
- [ ] Per-device HTTP/S TLS certificates (mTLS) for zero-trust fleets.
- [ ] Scheduled content (day-parting) instead of a single active playlist per device.

## License

TBD.
