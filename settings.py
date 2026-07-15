"""
Persisted output-folder settings, configurable from the browser Settings page.
Re-read at the start of every batch, so changes apply without a service restart.
"""
import json
from pathlib import Path

SETTINGS_FILE = Path(__file__).parent / "settings.json"

DEFAULTS = {
    "video_dir": r"E:\Preparing for Increase VIDEOS",
    "transcript_dir": r"C:\Users\nielm\OneDrive\ENM New\Preparing for Increase Manuscripts\Transcripts",
    "manuscript_dir": r"C:\Users\nielm\OneDrive\ENM New\Preparing for Increase Manuscripts",
}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return {**DEFAULTS, **data}
        except Exception:
            pass
    return dict(DEFAULTS)


def save_settings(data: dict) -> None:
    settings = {
        "video_dir": data["video_dir"],
        "transcript_dir": data["transcript_dir"],
        "manuscript_dir": data["manuscript_dir"],
    }
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
