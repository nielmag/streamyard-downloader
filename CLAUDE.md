# StreamYard Downloader — Claude Code Guide

## Purpose

Standalone Flask app (port 5001) for downloading StreamYard recordings to a local folder.
Produces three files per broadcast: `.mp4` video, `.vtt` transcript, `.docx` manuscript.

Separate from the main `video-pipeline` webapp (port 5000).

## Output

Files saved to `E:\Trumpters Call Mar-May 2026` (configured via `OUTPUT_DIR` in `.env`):
```
{Title} {M-D-YY}.mp4
{Title} {M-D-YY} Transcript.vtt
{Title} {M-D-YY} Manuscript.docx
```
Date format: local time, no zero-padding (e.g. `3-1-26`, `5-19-26`).

## Run

```
cd streamyard_app
python app.py        # http://localhost:5001
```

`use_reloader=False` is set intentionally — Flask's auto-reloader kills background download threads.

## Architecture

```
streamyard_app/
├── app.py                  # Flask routes (auth, broadcast list, download, progress)
├── streamyard_client.py    # StreamYard API client
├── transcriber.py          # Transcript: StreamYard VTT first, AssemblyAI fallback
├── manuscript.py           # VTT → Claude → Word doc (.docx)
├── requirements.txt
├── .env                    # API keys + OUTPUT_DIR (not committed)
├── session.pkl             # StreamYard session cookies (auto-generated, not committed)
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

### Processing pipeline (background thread)
`_process_batch()` runs sequentially per broadcast:
1. **Download video** → skipped if `.mp4` already exists
2. **Transcript** → tries StreamYard's VTT first; falls back to AssemblyAI if unavailable
3. **Manuscript** → VTT text → Claude (`claude-sonnet-4-6`) → `.docx` (Calibri 12pt, 18pt bold title)

All three outputs are cached — re-running skips completed steps.

### Manuscript prompt
Claude is prompted as a sermon/teaching manuscript editor:
- Remove filler words, false starts, timestamps
- Restructure oral sentences into written prose
- Preserve all theological content and scripture references
- Add `##` section headings at natural breaks
- Output full manuscript (not a summary)

## Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `ASSEMBLYAI_API_KEY` | AssemblyAI transcription (fallback only) |
| `ANTHROPIC_API_KEY` | Claude API for manuscript generation |
| `CLAUDE_MODEL` | Default: `claude-sonnet-4-6` |
| `OUTPUT_DIR` | Output folder (e.g. `E:\Trumpters Call Mar-May 2026`) |
| `SECRET_KEY` | Flask session secret |
