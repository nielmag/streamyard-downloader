from pathlib import Path
import time

from assemblyai import Client, api, types


def _upload_video(client: Client, video_path: Path, status_callback=None) -> str:
    if status_callback:
        status_callback("Uploading video to AssemblyAI...")
    with open(video_path, "rb") as audio_file:
        upload_url = api.upload_file(client.http_client, audio_file)
    if status_callback:
        status_callback("Upload complete.")
    return upload_url


def _wait_for_transcript(client: Client, transcript_id: str, status_callback=None) -> types.TranscriptResponse:
    while True:
        transcript = api.get_transcript(client.http_client, transcript_id)
        if transcript.status in (types.TranscriptStatus.completed, types.TranscriptStatus.error):
            return transcript
        if status_callback:
            status_callback("AssemblyAI still processing transcript...")
        time.sleep(client.settings.polling_interval)


def transcribe_with_assemblyai(video_path: Path, cache_dir: Path, api_key: str, status_callback=None) -> str:
    """Upload a video to AssemblyAI, transcribe it, and return VTT text."""
    if not api_key:
        raise RuntimeError("ASSEMBLYAI_API_KEY is required for transcription.")

    cache_dir.mkdir(parents=True, exist_ok=True)
    client = Client(settings=types.Settings(api_key=api_key))

    upload_url = _upload_video(client, video_path, status_callback)
    if status_callback:
        status_callback("Starting AssemblyAI transcription...")

    transcript = api.create_transcript(
        client.http_client,
        types.TranscriptRequest(audio_url=upload_url, format_text=True, punctuate=True),
    )

    transcript = _wait_for_transcript(client, transcript.id, status_callback)
    if transcript.status == types.TranscriptStatus.error:
        raise RuntimeError(
            f"AssemblyAI transcription failed: {getattr(transcript, 'error', 'unknown error')}"
        )

    if status_callback:
        status_callback("Exporting VTT from AssemblyAI...")

    vtt_text = api.export_subtitles_vtt(
        client.http_client,
        transcript_id=transcript.id,
        chars_per_caption=30,
    )
    return vtt_text
