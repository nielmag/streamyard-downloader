"""
StreamYard Downloader — Flask web app.
Browse your StreamYard video library, select by date range, and download
video + transcript + manuscript in one click.
"""
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from manuscript import vtt_to_manuscript
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

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "outputs"))
MANUSCRIPT_DIR = Path(os.environ.get("MANUSCRIPT_DIR", "")) if os.environ.get("MANUSCRIPT_DIR") else OUTPUT_DIR
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CACHE_DIR = Path(__file__).parent / "cache"

# Single StreamYard client instance (personal app — single user)
sy_client = StreamYardClient()

# In-memory batch state
batches: dict[str, dict] = {}
batches_lock = threading.Lock()


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


# ------------------------------------------------------------------
# Background processing
# ------------------------------------------------------------------

def _process_batch(batch_id: str) -> None:
    """Process every item in a batch sequentially in a background thread."""
    with batches_lock:
        items = list(batches[batch_id]["items"])

    for item in items:
        bid = item["broadcast_id"]
        name = item["name"]

        video_path = OUTPUT_DIR / f"{name}.mp4"
        vtt_path = OUTPUT_DIR / f"{name} Transcript.vtt"
        docx_path = MANUSCRIPT_DIR / f"{name} Manuscript.docx"
        cache_dir = CACHE_DIR / bid

        def cb(msg: str, _bid=bid, _batch_id=batch_id) -> None:
            _update_item(_batch_id, _bid, "in_progress", msg)

        try:
            # 1. Download video
            _update_item(batch_id, bid, "downloading", "Requesting download link from StreamYard...")
            sy_client.download_video(bid, video_path, status_callback=cb)

            # 2. Transcribe → VTT (tries StreamYard transcript first, falls back to AssemblyAI)
            _update_item(batch_id, bid, "transcribing", "Checking for StreamYard transcript...")
            transcribe_to_vtt(
                video_path, vtt_path, cache_dir, ASSEMBLYAI_API_KEY,
                status_callback=cb, sy_client=sy_client, broadcast_id=bid,
            )

            # 3. Manuscript → Word doc
            _update_item(batch_id, bid, "manuscript", "Generating manuscript with Claude...")
            vtt_to_manuscript(vtt_path, name, docx_path, ANTHROPIC_API_KEY, CLAUDE_MODEL, status_callback=cb)

            _update_item(batch_id, bid, "done", f"Saved to {OUTPUT_DIR}")

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
    print(f"[app] /auth/request received email='{email}'", flush=True)
    logging.info(f"[app] /auth/request received email='{email}'")
    if not email:
        return render_template("index.html", step="email", error="Please enter your email.")
    try:
        sy_client.request_otp(email)
        print(f"[app] /auth/request request_otp called for {email}", flush=True)
        logging.info(f"[app] /auth/request request_otp called for {email}")
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
    print(f"[app] /auth/verify received otp='{otp}' for email='{email}'", flush=True)
    logging.info(f"[app] /auth/verify received otp='{otp}' for email='{email}'")
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
        })

    return jsonify({"broadcasts": results})


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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MANUSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    batch_id = str(uuid.uuid4())
    items = [
        {
            "broadcast_id": b["id"],
            "name": b["name"],
            "title": b["title"],
            "display_date": b["display_date"],
            "status": "pending",
            "message": "Waiting...",
        }
        for b in selected
    ]

    with batches_lock:
        batches[batch_id] = {"items": items}

    thread = threading.Thread(target=_process_batch, args=(batch_id,), daemon=True)
    thread.start()

    return jsonify({"batch_id": batch_id})


# ------------------------------------------------------------------
# Routes — progress
# ------------------------------------------------------------------

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
    return jsonify({"items": batch["items"], "output_dir": str(OUTPUT_DIR)})


# ------------------------------------------------------------------

if __name__ == "__main__":
    # use_reloader=False prevents Flask from killing background download threads on file changes
    # bind to 0.0.0.0 so remote browsers can reach the server
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5001"))
    app.run(debug=True, host=host, port=port, use_reloader=False)
