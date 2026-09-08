from pathlib import Path

import assemblyai as aai


def transcribe_with_assemblyai(video_path: Path, cache_dir: Path, api_key: str, status_callback=None) -> str:
    """Upload a video to AssemblyAI, transcribe it, and return VTT text."""
    if not api_key:
        raise RuntimeError("ASSEMBLYAI_API_KEY is required for transcription.")

    cache_dir.mkdir(parents=True, exist_ok=True)

    aai.settings.api_key = api_key

    if status_callback:
        status_callback("Uploading to AssemblyAI and transcribing (1-5 minutes)...")

    config = aai.TranscriptionConfig(format_text=True, punctuate=True)
    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.transcribe(str(video_path))

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")

    if status_callback:
        status_callback("Exporting VTT from AssemblyAI...")

    # Some spoken words exceed 30 characters. Use a readable caption size
    # that accommodates them instead of failing the entire recording.
    vtt_text = transcript.export_subtitles_vtt(chars_per_caption=120)
    return vtt_text
