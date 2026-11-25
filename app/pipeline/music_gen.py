"""
app/pipeline/music_gen.py

Layer 7: Background music generation.

Reads `music_style` from the blueprint, generates one music track via the
injected MusicClient, and saves it as:

    run_0X/music_bg/track.mp3

The client is injected for easy testing. Non-MP3 output is transcoded with
ffmpeg.
"""
from __future__ import annotations

import json
from pathlib import Path

from app import costs
from app.clients.music_lyria_client import (
    MusicClient,
    MusicJobSpec,
    MusicError,
    generate_with_retry,
)
from app.utils.ffmpeg_utils import FfmpegError, run_ffmpeg

TRACK_NAME = "track.mp3"


def _read_music_style(run_dir: Path) -> str:
    bp_path = run_dir / "blueprint.json"
    if not bp_path.is_file():
        raise MusicError(f"blueprint.json not found in {run_dir}")
    bp = json.loads(bp_path.read_text(encoding="utf-8"))
    style = (bp.get("music_style") or "").strip()
    if not style:
        raise MusicError("blueprint.music_style is empty — nothing to generate")
    return style


def _is_mp3(data: bytes) -> bool:
    # ID3 tag or MPEG audio frame sync (0xFFEx/0xFFFx)
    return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)


def _write_track(audio: bytes, out_mp3: Path) -> Path:
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    if _is_mp3(audio):
        out_mp3.write_bytes(audio)                     # pass-through, no re-encode
        return out_mp3
    # unknown container -> let ffmpeg detect from stdin and transcode to mp3
    try:
        run_ffmpeg(["-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", "192k", str(out_mp3)],
                   expect=out_mp3, input=audio, label="mp3 encode")
    except FfmpegError as e:                 # L7's error contract is MusicError
        raise MusicError(str(e)) from e
    return out_mp3


def generate_run_music(
    run_dir: str | Path,
    client: MusicClient,
    *,
    duration_s: float | None = None,   # hint only; Lyria fixes ~30s
    instrumental: bool = True,
    style: str | None = None,          # given directly by callers with no blueprint.music_style
) -> Path:
    """Generate via Lyria from `style` (default: bp.music_style), save
    run_0X/music_bg/track.mp3. Returns the output path. One track per run
    (overwrites on re-run)."""
    run_dir = Path(run_dir)
    style = style or _read_music_style(run_dir)

    spec = MusicJobSpec(mood=style, instrumental=instrumental, duration_s=duration_s)
    audio, attempts = generate_with_retry(client, spec)

    # Billed per call. Lyria emits a fixed ~30s clip regardless of duration_s, so
    # per-call and per-clip are the same thing and there is nothing to measure.
    costs.record(run_dir, layer="L7", provider="gemini",
                 model=getattr(client, "model", "lyria-3-clip-preview"),
                 unit="call", quantity=1, measured=True, attempts=attempts)

    return _write_track(audio, run_dir / "music_bg" / TRACK_NAME)