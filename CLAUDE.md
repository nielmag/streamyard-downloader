# StreamYard Downloader — Claude Code Guide

## Purpose

Standalone Flask app (port 5003, the Site tunnel target) for downloading StreamYard recordings to a local folder.
Produces three files per broadcast: `.mp4` video, `.vtt` transcript, `.docx` manuscript.

Separate from the main `video-pipeline` webapp (port 5000).

## Output

Videos, transcripts, and manuscripts each save to their own configurable folder, set from the **Settings** page in the browser (`/settings`) — not `.env`. Changes apply immediately to the next batch; no service restart needed.

- Settings are persisted to `settings.json` (gitignored, alongside `session.pkl`), managed by `settings.py` (`load_settings()` / `save_settings()`).
- Defaults (used the first time, before `settings.json` exists) live in `settings.py:DEFAULTS`.
- The Settings page's folder picker is a server-side directory browser (`GET /api/browse`) — browsers don't expose real filesystem paths from a native OS picker, so this is a custom "click through folders" UI instead.
- Each batch snapshots the three paths at `/download` time into `batches[batch_id]["dirs"]`, so a settings change mid-processing never mixes folders within one batch, and `/files/...` downloads always match what `_process_batch` actually used.

Files named:
```
{Title} {M-D-YY}.mp4
{Title} {M-D-YY} Transcript.vtt
{Title} {M-D-YY} Manuscript.docx
```
Date format: local time, no zero-padding (e.g. `3-1-26`, `5-19-26`).

## Run

### As Windows Service (normal operation)
The app runs as a persistent Windows Service named `StreamYardDownloader` (installed via NSSM).
It starts automatically on Windows boot and serves the Site tunnel at `http://127.0.0.1:5003`.

Manage from an **admin PowerShell**:
```powershell
$nssm = "C:\Users\nielm\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
& $nssm restart StreamYardDownloader   # restart after code changes
& $nssm stop StreamYardDownloader
& $nssm start StreamYardDownloader
& $nssm status StreamYardDownloader
```

Or via Windows Services UI: `services.msc` → StreamYardDownloader.

Logs: `streamyard_app\service.log` (rotates at 5 MB).

### As a manual terminal process (for debugging)
```
cd streamyard_app
venv\Scripts\python app.py    # http://127.0.0.1:5003
```

`use_reloader=False` is set intentionally — Flask's auto-reloader kills background download threads.

## After code changes

Restart the service for changes to take effect. The service runs as `LocalSystem`, which has full privilege to control its own Windows Service without any UAC prompt — so the app can restart itself:

- **Primary method:** click **⟳ Restart App** in the browser topbar (calls `POST /admin/restart`, which fires `nssm restart` as a detached subprocess). No PowerShell, no admin window, ever.
- **Fallback** (if the app is unreachable/hung and can't serve the button click), from an admin PowerShell:
  ```powershell
  & $nssm restart StreamYardDownloader
  ```

## Debugging and best practices

- When testing manually, run the app with unbuffered output so logs appear immediately:
  ```powershell
  python -u app.py
  ```
- Confirm the exact port the app is running on. If the terminal says `Running on http://127.0.0.1:5002`, open that URL in the browser, not `5001`.
- Do not navigate directly to `http://127.0.0.1:<port>/auth/request` in the browser. That endpoint only accepts POST requests and will return `405 Method Not Allowed` when opened by GET.
- Always open the root app URL first, then use the login form.
- If the browser still shows the old app behavior after a code change, an old Python/Flask process may still be running on the same port. Check active listeners and stop stale processes before retrying.
- Use the terminal output and `service.log` to diagnose problems. The browser network panel only shows requests to the local app, not the internal StreamYard API calls.
- For auth failures, a `404` response from StreamYard usually means the email was not recognized. Use the exact StreamYard account email.
- If you change code, make sure the process currently running is the updated one. Stale ports and duplicate Python servers are the most common source of repeated failures.

## Architecture

```
streamyard_app/
├── app.py                  # Flask routes (auth, broadcast list, download, progress, admin/settings)
├── settings.py             # load_settings()/save_settings() — video/transcript/manuscript folders
├── streamyard_client.py    # StreamYard API client
├── transcriber.py          # Transcript: StreamYard VTT first, AssemblyAI fallback
├── manuscript.py           # VTT → Claude → Word doc (.docx); falls back to raw transcript on content filter
├── requirements.txt
├── install-service.ps1     # One-time Windows Service installer (run as admin)
├── .env                    # API keys + SECRET_KEY (not committed)
├── settings.json           # Configured output folders (auto-generated, not committed)
├── session.pkl             # StreamYard session cookies (auto-generated, not committed)
├── service.log             # Service stdout/stderr log (not committed)
├── venv/                   # Python virtual environment (not committed)
│                           # Used by the service so packages are always available
├── cache/                  # Per-broadcast AssemblyAI cache (gitignored)
└── templates/
    ├── index.html          # Email + OTP login
    ├── broadcasts.html     # Date-filtered broadcast list with checkboxes
    ├── progress.html       # Live per-broadcast progress polling
    ├── settings.html       # Output folder configuration + in-app folder browser
    └── _admin_bar.html     # Shared partial: Restart App + Settings links (included in topbars)
```

## Admin routes

- `POST /admin/restart` — restarts the Windows Service from within the app itself (see "After code changes" above). Gated behind `_is_logged_in()`.
- `GET /settings` / `POST /settings` — view/save the three output folders (`settings.py`).
- `GET /api/browse?path=<p>` — lists subdirectories of `<p>` (or drive letters when `<p>` is empty) as JSON; powers the Settings page's folder browser.

## StreamYard API (reverse-engineered)

StreamYard has no public API. All endpoints are internal.

### Auth (email OTP)
1. `GET /login` → get `csrfToken` cookie
2. `POST /api/user/login` `{email, csrfToken}` → sends OTP email
3. `POST /api/user/otp_token` `{email, csrfToken, otpToken}` → sets `jwt` + `csrfToken` cookies
   - StreamYard sets **duplicate `jwt` cookies** — use `_cookie()` helper, never `.cookies.get()`
4. Session saved to `session.pkl`; Flask `session["sy_authenticated"]` flag used for route guards

### User info
- `GET /api/user` → returns `{primaryTeam, primaryWorkspace, ...}`
- Workspace ID (`primaryWorkspace`) is required for all broadcast/download API calls

### Broadcast listing
```
GET /api/workspaces/{workspaceId}/broadcasts/list
    ?limit=99&withEpisodeCount=true&withLastEpisodeId=true
    &wasCompleted=true&pinnedAsReusable=false
```
Returns `{broadcasts: [{id, title, startedAt (ISO 8601 UTC), status, ...}]}`

### Video download (3-step async)
1. `POST /api/workspaces/{workspaceId}/broadcasts/{id}/vod` `{csrfToken, type:"video"}`
2. `GET /api/workspaces/{workspaceId}/broadcasts/{id}/vod` — poll until `status != "creating"`
3. `GET /api/workspaces/{workspaceId}/broadcasts/{id}/vod_download_urls?type=video` → `{videoUrl}`

### Transcript download
Same 3-step flow with `type="captions"` or `type="transcript"`.
VTT stored at: `https://storage.googleapis.com/streamyard-vods/media/{broadcastId}/transcript.vtt?{signed}`

## Key Implementation Notes

### Cookie handling
StreamYard sets duplicate `jwt` cookies after OTP verify. Never call `session.cookies.get(name)` directly — use `self._cookie(name)` which iterates the jar safely. `_save_session()` deduplicates using `{c.name: c.value for c in self.session.cookies}`.

### Authentication flow
Routes use `session["sy_authenticated"]` (Flask session flag set on successful OTP verify), **not** `sy_client.is_authenticated()` on every request. `is_authenticated()` makes a live API call and was causing redirect loops.

### File download
`download_video()` uses `tmp_path.replace(dest_path)` (not `.rename()`) to atomically move the completed `.tmp` file to `.mp4`. `replace()` overwrites if the destination already exists; `rename()` raises WinError 183 on Windows when the destination is present.

### Processing pipeline (background thread)
`_process_batch()` runs sequentially per broadcast:
1. **Download video** → skipped if `.mp4` already exists
2. **Transcript** → tries StreamYard's VTT first; falls back to AssemblyAI if unavailable
3. **Manuscript** → VTT text → Claude (`claude-sonnet-4-6`) → `.docx` (Calibri 12pt, 18pt bold title)

All three outputs are cached — re-running skips completed steps.

### Content filter fallback
If Claude's API blocks manuscript generation (HTTP 400 `invalid_request_error` / "Output blocked by content filtering policy"), `manuscript.py` catches `anthropic.BadRequestError` and writes the raw transcript text into the Word doc instead. The broadcast completes as Done rather than Error.

### AssemblyAI SDK version
`assemblyai_transcribe.py` uses the modern SDK API (`assemblyai>=0.17`):
```python
aai.settings.api_key = api_key
transcriber = aai.Transcriber(config=aai.TranscriptionConfig(...))
transcript = transcriber.transcribe(str(video_path))
vtt_text = transcript.export_subtitles_vtt(chars_per_caption=30)
```
The old internal `Client`/`api`/`types` interface was removed in SDK v0.17+ — do not revert to it.

### File downloads (progress page)
When a broadcast completes, the progress page shows **Download Video**, **Download Transcript**, and **Download Manuscript** buttons. These call `GET /files/<batch_id>/<broadcast_id>/<filetype>` (filetype: `video`, `transcript`, `manuscript`) which uses `send_file()` to stream the file to the browser. This is the intended way for cloud VM users to retrieve their files — the output directory on the VM is not otherwise accessible.

### Windows Service
The service runs `venv\Scripts\python.exe` (not the system Python) so all packages in `venv/` are always available regardless of which user account runs the service. If packages change, re-run `venv\Scripts\pip install -r requirements.txt` and restart the service.

### Manuscript prompt
Claude is prompted as a sermon/teaching manuscript editor:
- Remove filler words, false starts, timestamps
- Restructure oral sentences into written prose
- Preserve all theological content and scripture references
- Add `##` section headings at natural breaks
- Output full manuscript (not a summary)

## Known issues / debugging notes
- Always verify local startup first with `curl -v http://127.0.0.1:5001` on the VM before debugging remote access.
- For remote use, the app must bind to `0.0.0.0` and use the correct `PORT=5001`; otherwise the browser will refuse the connection.
- If transcription fails with "Could not resolve authentication method", check that both `ASSEMBLYAI_API_KEY` and `ANTHROPIC_API_KEY` are set in `.env` on the VM. Both are required — AssemblyAI for transcription fallback, Anthropic for manuscript generation.
- The `assemblyai_transcribe.py` module uses the modern SDK API (v0.64+). The old `Client`/`api`/`types` interface no longer exists in that package.
- StreamYard broadcasts that were recorded but have no hosted transcript (e.g. some live streams) will always fall back to AssemblyAI — `ASSEMBLYAI_API_KEY` is required for those.

## Cloud VM Deployment (DigitalOcean)

The app is deployed on a DigitalOcean droplet (Ubuntu 24.04 LTS):
- **IP:** `161.35.50.28`
- **URL:** `http://161.35.50.28:5001`
- **Code location:** `/root/streamyard_new`
- **GitHub repo:** `https://github.com/nielmag/streamyard-downloader` (public)
- **Runs as:** systemd service named `streamyard`

### Manage the service (SSH in first)
```bash
ssh root@161.35.50.28
systemctl status streamyard
systemctl restart streamyard
systemctl stop streamyard
journalctl -u streamyard -f   # live logs
```

### Deploy code changes
```bash
ssh root@161.35.50.28
cd /root/streamyard_new
git pull
systemctl restart streamyard
```

### First-time setup on a fresh VM
```bash
git clone https://github.com/nielmag/streamyard-downloader.git ~/streamyard_new
cd ~/streamyard_new
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp /path/to/saved.env .env   # restore API keys

# Create systemd service
cat > /etc/systemd/system/streamyard.service << 'EOF'
[Unit]
Description=StreamYard Downloader
After=network.target

[Service]
WorkingDirectory=/root/streamyard_new
ExecStart=/root/streamyard_new/venv/bin/python -u app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload && systemctl enable streamyard && systemctl start streamyard
```

### Notes
- The `.env` file is not in git — back it up before destroying/rebuilding the droplet (`cp .env /tmp/` then restore after clone)
- GitHub repo is public so no token needed to clone
- The app binds to `0.0.0.0:5001` — make sure port 5001 is open in DigitalOcean's firewall if connections are refused
- `session.pkl` (StreamYard auth cookies) is also not in git — after a fresh deploy you must log in once via the browser

If the app crashes, check `journalctl -u streamyard -n 50` for the traceback before testing remote access.

## Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `ASSEMBLYAI_API_KEY` | AssemblyAI transcription (fallback only) |
| `ANTHROPIC_API_KEY` | Claude API for manuscript generation |
| `CLAUDE_MODEL` | Default: `claude-sonnet-4-6` |
| `SECRET_KEY` | Flask session secret |

Output folders (video/transcript/manuscript) are **not** set via `.env` — see the "Output" section above; they're configured from the `/settings` page and persisted to `settings.json`.
