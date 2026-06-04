# StreamYard Downloader — Claude Code Guide

## Purpose

Standalone Flask app (port 5001) for downloading StreamYard recordings to a local folder.
Produces three files per broadcast: `.mp4` video, `.vtt` transcript, `.docx` manuscript.

Separate from the main `video-pipeline` webapp (port 5000).

## Output

- **Video + transcript** → `OUTPUT_DIR` in `.env` (e.g. `E:\Trumpters Call Mar-May 2026`)
- **Manuscripts** → `MANUSCRIPT_DIR` in `.env` (e.g. `C:\Users\nielm\OneDrive\ENM New\Dominion Manuscripts`)
  - Falls back to `OUTPUT_DIR` if `MANUSCRIPT_DIR` is not set

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
It starts automatically on Windows boot. Access at `http://localhost:5001`.

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
venv\Scripts\python app.py    # http://localhost:5001
```

`use_reloader=False` is set intentionally — Flask's auto-reloader kills background download threads.

## After code changes

Always restart the service for changes to take effect:
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
├── app.py                  # Flask routes (auth, broadcast list, download, progress)
├── streamyard_client.py    # StreamYard API client
├── transcriber.py          # Transcript: StreamYard VTT first, AssemblyAI fallback
├── manuscript.py           # VTT → Claude → Word doc (.docx); falls back to raw transcript on content filter
├── requirements.txt
├── install-service.ps1     # One-time Windows Service installer (run as admin)
├── .env                    # API keys + OUTPUT_DIR + MANUSCRIPT_DIR (not committed)
├── session.pkl             # StreamYard session cookies (auto-generated, not committed)
├── service.log             # Service stdout/stderr log (not committed)
├── venv/                   # Python virtual environment (not committed)
│                           # Used by the service so packages are always available
├── cache/                  # Per-broadcast AssemblyAI cache (gitignored)
└── templates/
    ├── index.html          # Email + OTP login
    ├── broadcasts.html     # Date-filtered broadcast list with checkboxes
    └── progress.html       # Live per-broadcast progress polling
```

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

### Windows Service
The service runs `venv\Scripts\python.exe` (not the system Python) so all packages in `venv/` are always available regardless of which user account runs the service. If packages change, re-run `venv\Scripts\pip install -r requirements.txt` and restart the service.

### Manuscript prompt
Claude is prompted as a sermon/teaching manuscript editor:
- Remove filler words, false starts, timestamps
- Restructure oral sentences into written prose
- Preserve all theological content and scripture references
- Add `##` section headings at natural breaks
- Output full manuscript (not a summary)

## Recent debugging notes / known startup issues
- The VM was refusing browser connections because the Flask app never successfully started.
- The first crash was `ModuleNotFoundError: No module named 'docx'` because `python-docx` was missing from the runtime environment.
- After installing dependencies, the next crash was `ModuleNotFoundError: No module named 'assemblyai_transcribe'`.
- `transcriber.py` had a legacy import path that assumed a separate `webapp/assemblyai_transcribe.py` file existed.
- That file was not present in this standalone `streamyard_app` workspace, so the import failed before the app could bind the port.
- The browser error `161.35.50.28 refused to connect` was therefore a symptom of the app crashing immediately, not a firewall or VM network issue.
- Fix applied: added a local `assemblyai_transcribe.py` helper and updated `transcriber.py` to import it directly.
- Fix applied: updated the fallback path so AssemblyAI returns real VTT text and `transcribe_to_vtt()` saves it correctly.
- Always verify local startup first with `curl -v http://127.0.0.1:5001` on the VM before debugging remote access.
- For remote use, the app must bind to `0.0.0.0` and use the correct `PORT=5001`; otherwise the browser will refuse the connection.

## Quick Ubuntu VM run checklist
1. `cd /root/streamyard_app`
2. `python3 -m venv venv`
3. `source venv/bin/activate`
4. `pip install -r requirements.txt`
5. `python -u app.py`
6. In the VM shell, verify locally with `curl -v http://127.0.0.1:5001`
7. Open browser at `http://<vm-ip>:5001`

If the app crashes, read the first traceback and fix that before testing remote access.

## Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `ASSEMBLYAI_API_KEY` | AssemblyAI transcription (fallback only) |
| `ANTHROPIC_API_KEY` | Claude API for manuscript generation |
| `CLAUDE_MODEL` | Default: `claude-sonnet-4-6` |
| `OUTPUT_DIR` | Output folder for video + transcript (e.g. `E:\Trumpters Call Mar-May 2026`) |
| `MANUSCRIPT_DIR` | Output folder for manuscripts (e.g. `C:\Users\nielm\OneDrive\ENM New\Dominion Manuscripts`); falls back to `OUTPUT_DIR` if unset |
| `SECRET_KEY` | Flask session secret |
