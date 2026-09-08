"""
StreamYard Downloader — Flask web app.
Browse your StreamYard video library, select by date range, and download
video + transcript + manuscript in one click.
"""
import logging
import hmac
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for

from manuscript import vtt_to_manuscript
from settings import load_settings, save_settings
from streamyard_client import StreamYardClient
from transcriber import transcribe_to_vtt

load_dotenv()
logging.basicConfig(
    filename=Path(__file__).parent / 'service.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CACHE_DIR = Path(__file__).parent / "cache"
STATE_FILE = Path(__file__).parent / "job-state.json"

NSSM_PATH = r"C:\Users\nielm\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
SERVICE_NAME = "StreamYardDownloader"
SITE_API_TOKEN = os.environ.get("SITE_API_TOKEN", "")

# Single StreamYard client instance (personal app — single user)
sy_client = StreamYardClient()

# In-memory batch state
batches: dict[str, dict] = {}
batches_lock = threading.Lock()


def _save_batches() -> None:
    """Atomically retain job state so a service restart does not erase the queue."""
    with batches_lock:
        state = json.dumps(batches)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(state, encoding="utf-8")
    temporary.replace(STATE_FILE)


def _load_batches() -> None:
    if not STATE_FILE.exists():
        return
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            with batches_lock:
                batches.update(saved)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Could not restore saved job state: %s", exc)


def _site_authorized() -> bool:
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return bool(SITE_API_TOKEN) and hmac.compare_digest(supplied, SITE_API_TOKEN)


def _start_batch(selected: list[dict]) -> str:
    dirs = load_settings()
    for key in ("video_dir", "transcript_dir", "manuscript_dir"):
        Path(dirs[key]).mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    batch_id = str(uuid.uuid4())
    items = [{"broadcast_id": b["id"], "name": b["name"], "title": b["title"], "display_date": b["display_date"], "fallback_video_url": b.get("fallback_video_url"), "status": "pending", "message": "Waiting..."} for b in selected]
    with batches_lock:
        batches[batch_id] = {"items": items, "dirs": dirs}
    _save_batches()
    threading.Thread(target=_process_batch, args=(batch_id,), daemon=True).start()
    return batch_id


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _safe_filename(s: str) -> str:
    """Remove characters illegal in Windows filenames."""
    return re.sub(r'[\\/:*?"<>|]', "", s).strip()


def _format_date(started_at: str) -> str:
    """
    Parse an ISO 8601 UTC timestamp and return M-D-YY in local time.
    e.g. "2026-03-01T15:30:00Z" → "3-1-26"
    """
    dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    local_dt = dt.astimezone()
    return f"{local_dt.month}-{local_dt.day}-{str(local_dt.year)[-2:]}"


def _build_name(title: str, started_at: str) -> str:
    """Build the filename stem: '{title} {M-D-YY}'."""
    date_str = _format_date(started_at)
    return _safe_filename(f"{title} {date_str}")


def _youtube_fallback_url(broadcast: dict) -> str | None:
    """Return the broadcast's published YouTube output, if StreamYard supplied one."""
    for output in broadcast.get("outputs") or []:
        url = output.get("platformLink")
        if output.get("platform") == "youtube" and isinstance(url, str):
            return url
    return None


def _update_item(batch_id: str, broadcast_id: str, status: str, message: str) -> None:
    with batches_lock:
        batch = batches.get(batch_id)
        if not batch:
            return
        for item in batch["items"]:
            if item["broadcast_id"] == broadcast_id:
                item["status"] = status
                item["message"] = message
                break
    _save_batches()


# ------------------------------------------------------------------
# Background processing
# ------------------------------------------------------------------

def _process_batch(batch_id: str) -> None:
    """Process every item in a batch sequentially in a background thread."""
    with batches_lock:
        items = list(batches[batch_id]["items"])
        dirs = batches[batch_id]["dirs"]

    video_dir = Path(dirs["video_dir"])
    transcript_dir = Path(dirs["transcript_dir"])
    manuscript_dir = Path(dirs["manuscript_dir"])

    for item in items:
        if item["status"] == "done":
            continue
        bid = item["broadcast_id"]
        name = item["name"]

        video_path = video_dir / f"{name}.mp4"
        vtt_path = transcript_dir / f"{name} Transcript.vtt"
        docx_path = manuscript_dir / f"{name} Manuscript.docx"
        cache_dir = CACHE_DIR / bid

        def cb(msg: str, _bid=bid, _batch_id=batch_id) -> None:
            _update_item(_batch_id, _bid, "in_progress", msg)

        try:
            # 1. Download video
            _update_item(batch_id, bid, "downloading", "Requesting download link from StreamYard...")
            sy_client.download_video(bid, video_path, status_callback=cb, fallback_url=item.get("fallback_video_url"))

            # 2. Transcribe → VTT (tries StreamYard transcript first, falls back to AssemblyAI)
            _update_item(batch_id, bid, "transcribing", "Checking for StreamYard transcript...")
            transcribe_to_vtt(
                video_path, vtt_path, cache_dir, ASSEMBLYAI_API_KEY,
                status_callback=cb, sy_client=sy_client, broadcast_id=bid,
            )

            # 3. Manuscript → Word doc
            _update_item(batch_id, bid, "manuscript", "Generating manuscript with Claude...")
            vtt_to_manuscript(vtt_path, name, docx_path, ANTHROPIC_API_KEY, CLAUDE_MODEL, status_callback=cb)

            _update_item(batch_id, bid, "done", f"Saved to {video_dir}")

        except Exception as exc:
            _update_item(batch_id, bid, "error", str(exc))


# ------------------------------------------------------------------
# Routes — auth
# ------------------------------------------------------------------

def _is_logged_in() -> bool:
    """Check login via Flask session flag (set on successful OTP verify)."""
    return bool(session.get("sy_authenticated"))


@app.route("/")
def index():
    if _is_logged_in():
        return redirect(url_for("broadcasts"))
    return render_template("index.html", step="email")


@app.route("/auth/request", methods=["POST"])
def auth_request():
    email = request.form.get("email", "").strip()
    if not email:
        return render_template("index.html", step="email", error="Please enter your email.")
    try:
        sy_client.request_otp(email)
        session["pending_email"] = email
        return render_template("index.html", step="otp", email=email)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[app] auth_request error: {exc}", flush=True)
        logging.error(f"[app] auth_request error: {exc}")
        with open(Path(__file__).parent / 'service.log', 'a', encoding='utf-8') as f:
            f.write(f"[app] auth_request error: {exc}\n")
        return render_template("index.html", step="email", error=str(exc))


@app.route("/auth/verify", methods=["POST"])
def auth_verify():
    otp = request.form.get("otp", "").strip()
    email = session.get("pending_email", "")
    if not otp:
        return render_template("index.html", step="otp", email=email,
                               error="Please enter the code from your email.")
    try:
        ok, err = sy_client.verify_otp(otp)
    except Exception as exc:
        return render_template("index.html", step="otp", email=email,
                               error=f"Unexpected error: {exc}")
    if ok:
        session["sy_authenticated"] = True
        return redirect(url_for("broadcasts"))
    return render_template("index.html", step="otp", email=email,
                           error=err or "Invalid code — please try again.")


@app.route("/auth/logout")
def auth_logout():
    sy_client.clear_session()
    session.clear()  # clears sy_authenticated flag too
    return redirect(url_for("index"))


# ------------------------------------------------------------------
# Routes — broadcast listing
# ------------------------------------------------------------------

@app.route("/broadcasts")
def broadcasts():
    if not _is_logged_in():
        return redirect(url_for("index"))
    return render_template("broadcasts.html")


@app.route("/api/broadcasts")
def api_broadcasts():
    """Return broadcasts filtered by optional from_date / to_date query params."""
    if not _is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    from_date_str = request.args.get("from_date", "")
    to_date_str = request.args.get("to_date", "")

    try:
        raw = sy_client.list_broadcasts()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    results = []
    for b in raw:
        # Only show broadcasts that have ended (have a recording)
        status = b.get("status", "")
        if status not in ("ended", "complete", "completed", ""):
            continue

        started_at = b.get("startedAt", "")
        if not started_at:
            continue

        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone()
        dt_date = dt.date()

        if from_date_str:
            try:
                if dt_date < datetime.strptime(from_date_str, "%Y-%m-%d").date():
                    continue
            except ValueError:
                pass

        if to_date_str:
            try:
                if dt_date > datetime.strptime(to_date_str, "%Y-%m-%d").date():
                    continue
            except ValueError:
                pass

        results.append({
            "id": b.get("id"),
            "title": b.get("title", "Untitled"),
            "started_at": started_at,
            "display_date": dt.strftime("%-m/%-d/%y") if os.name != "nt" else dt.strftime("%#m/%#d/%y"),
            "name": _build_name(b.get("title", "Untitled"), started_at),
            "fallback_video_url": _youtube_fallback_url(b),
        })

    return jsonify({"broadcasts": results})


@app.route("/site/broadcasts")
def site_broadcasts():
    """List recordings for the private Site; StreamYard session remains on this worker."""
    if not _site_authorized():
        return jsonify({"error": "Unauthorized"}), 401

    from_date_str = request.args.get("from_date", "")
    to_date_str = request.args.get("to_date", "")
    try:
        raw = sy_client.list_broadcasts()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    results = []
    for b in raw:
        if b.get("status", "") not in ("ended", "complete", "completed", ""):
            continue
        started_at = b.get("startedAt", "")
        if not started_at:
            continue
        try:
            dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        if from_date_str:
            try:
                if dt.date() < datetime.strptime(from_date_str, "%Y-%m-%d").date():
                    continue
            except ValueError:
                pass
        if to_date_str:
            try:
                if dt.date() > datetime.strptime(to_date_str, "%Y-%m-%d").date():
                    continue
            except ValueError:
                pass
        results.append({
            "id": b.get("id"),
            "title": b.get("title", "Untitled"),
            "started_at": started_at,
            "display_date": dt.strftime("%-m/%-d/%y") if os.name != "nt" else dt.strftime("%#m/%#d/%y"),
            "name": _build_name(b.get("title", "Untitled"), started_at),
            "fallback_video_url": _youtube_fallback_url(b),
        })
    return jsonify({"broadcasts": results})


@app.route("/site/auth/status")
def site_auth_status():
    if not _site_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        # A cookie alone is not enough: confirm this session can read a workspace.
        sy_client.list_broadcasts()
    except Exception:
        return jsonify({"connected": False})
    return jsonify({"connected": True})


@app.route("/site/auth/request", methods=["POST"])
def site_auth_request():
    if not _site_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    email = (request.get_json(silent=True) or {}).get("email", "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "Enter a valid StreamYard email address."}), 400
    try:
        sy_client.request_otp(email)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True})


@app.route("/site/auth/verify", methods=["POST"])
def site_auth_verify():
    if not _site_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    otp = (request.get_json(silent=True) or {}).get("otp", "").strip()
    if not otp:
        return jsonify({"error": "Enter the sign-in code."}), 400
    ok, error = sy_client.verify_otp(otp)
    if not ok:
        return jsonify({"error": error or "Could not verify the StreamYard code."}), 400
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Routes — download
# ------------------------------------------------------------------

@app.route("/download", methods=["POST"])
def download():
    if not _is_logged_in():
        return redirect(url_for("index"))

    data = request.get_json(silent=True) or {}
    selected: list[dict] = data.get("broadcasts", [])

    if not selected:
        return jsonify({"error": "No broadcasts selected"}), 400

    dirs = load_settings()
    for key in ("video_dir", "transcript_dir", "manuscript_dir"):
        Path(dirs[key]).mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    batch_id = str(uuid.uuid4())
    items = [
        {
            "broadcast_id": b["id"],
            "name": b["name"],
            "title": b["title"],
            "display_date": b["display_date"],
            "fallback_video_url": b.get("fallback_video_url"),
            "status": "pending",
            "message": "Waiting...",
        }
        for b in selected
    ]

    with batches_lock:
        batches[batch_id] = {"items": items, "dirs": dirs}
    _save_batches()

    thread = threading.Thread(target=_process_batch, args=(batch_id,), daemon=True)
    thread.start()

    return jsonify({"batch_id": batch_id})


# ------------------------------------------------------------------
# Routes — progress
# ------------------------------------------------------------------

@app.route("/site/jobs", methods=["POST"])
def site_start_job():
    if not _site_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    selected = (request.get_json(silent=True) or {}).get("broadcasts", [])
    if not selected:
        return jsonify({"error": "No broadcasts selected"}), 400
    return jsonify({"batch_id": _start_batch(selected)})


@app.route("/site/jobs/<batch_id>")
def site_job_status(batch_id: str):
    if not _site_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    with batches_lock:
        batch = batches.get(batch_id)
    if not batch:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"items": batch["items"]})


@app.route("/site/files/<batch_id>/<broadcast_id>/<filetype>")
def site_file(batch_id: str, broadcast_id: str, filetype: str):
    if not _site_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    return download_file(batch_id, broadcast_id, filetype)

@app.route("/progress/<batch_id>")
def progress(batch_id: str):
    if batch_id not in batches:
        return "Batch not found", 404
    return render_template("progress.html", batch_id=batch_id)


@app.route("/progress/<batch_id>/status")
def progress_status(batch_id: str):
    with batches_lock:
        batch = batches.get(batch_id)
    if not batch:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"items": batch["items"], "dirs": batch["dirs"]})


@app.route("/files/<batch_id>/<broadcast_id>/<filetype>")
def download_file(batch_id: str, broadcast_id: str, filetype: str):
    with batches_lock:
        batch = batches.get(batch_id)
    if not batch:
        return "Batch not found", 404
    item = next((i for i in batch["items"] if i["broadcast_id"] == broadcast_id), None)
    if not item:
        return "Item not found", 404

    name = item["name"]
    dirs = batch["dirs"]
    if filetype == "video":
        path = Path(dirs["video_dir"]) / f"{name}.mp4"
    elif filetype == "transcript":
        path = Path(dirs["transcript_dir"]) / f"{name} Transcript.vtt"
    elif filetype == "manuscript":
        path = Path(dirs["manuscript_dir"]) / f"{name} Manuscript.docx"
    else:
        return "Unknown file type", 400

    if not path.exists():
        return "File not found on server", 404

    return send_file(path, as_attachment=True)


_load_batches()
for _saved_batch_id, _saved_batch in list(batches.items()):
    if any(item.get("status") not in ("done", "error") for item in _saved_batch.get("items", [])):
        threading.Thread(target=_process_batch, args=(_saved_batch_id,), daemon=True).start()


# ------------------------------------------------------------------
# Routes — admin
# ------------------------------------------------------------------

@app.route("/admin/restart", methods=["POST"])
def admin_restart():
    if not _is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    subprocess.Popen(
        [NSSM_PATH, "restart", SERVICE_NAME],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return jsonify({"ok": True})


@app.route("/settings")
def settings_page():
    if not _is_logged_in():
        return redirect(url_for("index"))
    return render_template("settings.html", dirs=load_settings())


@app.route("/settings", methods=["POST"])
def settings_save():
    if not _is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json(silent=True) or {}
    dirs = {
        "video_dir": data.get("video_dir", "").strip(),
        "transcript_dir": data.get("transcript_dir", "").strip(),
        "manuscript_dir": data.get("manuscript_dir", "").strip(),
    }
    for key, value in dirs.items():
        if not value or not os.path.isabs(value):
            return jsonify({"error": f"{key} must be an absolute path"}), 400
    try:
        for value in dirs.values():
            Path(value).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": f"Could not create folder: {exc}"}), 400
    save_settings(dirs)
    return jsonify({"ok": True})


@app.route("/api/browse")
def api_browse():
    if not _is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    path = request.args.get("path", "")

    if not path:
        # Root view: list available drive letters
        drives = [f"{c}:\\" for c in "CDEFGH" if os.path.exists(f"{c}:\\")]
        return jsonify({"path": "", "parent": None, "dirs": drives})

    try:
        p = Path(path)
        if not p.is_dir():
            return jsonify({"error": "Not a directory"}), 400
        entries = []
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            try:
                if child.is_dir():
                    entries.append(str(child))
            except OSError:
                continue
        parent = str(p.parent) if p.parent != p else None
        return jsonify({"path": str(p), "parent": parent, "dirs": entries})
    except (OSError, PermissionError) as exc:
        return jsonify({"error": str(exc)}), 400


# ------------------------------------------------------------------

if __name__ == "__main__":
    # use_reloader=False prevents Flask from killing background download threads on file changes.
    # The reverse tunnel reaches loopback; do not expose the worker directly.
    host = os.environ.get("HOST", "127.0.0.1")
    # The auto-start service uses the Site tunnel's local target by default.
    # PORT remains configurable for local development.
    port = int(os.environ.get("PORT", "5003"))
    app.run(debug=True, host=host, port=port, use_reloader=False)
