"""
Transcription pipeline: video file → AssemblyAI → WebVTT file.
"""
from pathlib import Path

from assemblyai_transcribe import transcribe_with_assemblyai


def _seconds_to_vtt_time(s: float) -> str:
    hours = int(s // 3600)
    minutes = int((s % 3600) // 60)
    secs = s % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def whisper_to_vtt(whisper_result: dict) -> str:
    """Convert a Whisper-compatible transcript dict to a WebVTT string."""
    lines = ["WEBVTT", ""]
    for i, seg in enumerate(whisper_result.get("segments", []), start=1):
        start = _seconds_to_vtt_time(float(seg["start"]))
        end = _seconds_to_vtt_time(float(seg["end"]))
        text = seg["text"].strip()
        if not text:
            continue
        lines += [str(i), f"{start} --> {end}", text, ""]
    return "\n".join(lines)


def download_vtt(url: str, vtt_dest: Path, session=None) -> Path:
    """Download a VTT file from a URL and save to vtt_dest."""
    import requests
    s = session or requests.Session()
    resp = s.get(url, timeout=60)
    resp.raise_for_status()
    vtt_dest.parent.mkdir(parents=True, exist_ok=True)
    vtt_dest.write_bytes(resp.content)
    return vtt_dest


def transcribe_to_vtt(
    video_path: Path,
    vtt_dest: Path,
    cache_dir: Path,
    api_key: str,
    status_callback=None,
    sy_client=None,
    broadcast_id: str = None,
) -> Path:
    """
    Get VTT transcript for a broadcast.
    Tries StreamYard's own transcript first (free, instant).
    Falls back to AssemblyAI transcription if unavailable.
    """
    if vtt_dest.exists():
        if status_callback:
            status_callback("Using cached transcript")
        return vtt_dest

    # Try StreamYard's built-in transcript first
    if sy_client and broadcast_id:
        if status_callback:
            status_callback("Checking for StreamYard transcript...")
        try:
            vtt_url = sy_client.get_transcript_url(broadcast_id)
            if vtt_url:
                if status_callback:
                    status_callback("Downloading transcript from StreamYard...")
                download_vtt(vtt_url, vtt_dest, session=sy_client.session)
                if status_callback:
                    status_callback("Transcript downloaded from StreamYard")
                return vtt_dest
        except Exception as e:
            if status_callback:
                status_callback(f"StreamYard transcript unavailable, using AssemblyAI... ({e})")

    # Fall back to AssemblyAI
    if status_callback:
        status_callback("Transcribing with AssemblyAI (1-5 minutes)...")

    cache_dir.mkdir(parents=True, exist_ok=True)
    vtt_content = transcribe_with_assemblyai(video_path, cache_dir, api_key, status_callback)
    vtt_dest.parent.mkdir(parents=True, exist_ok=True)
    vtt_dest.write_text(vtt_content, encoding="utf-8")

    if status_callback:
        status_callback(f"Transcript saved: {vtt_dest.name}")

    return vtt_dest
