"""
app/utils/ffmpeg_utils.py

Reusable low-level ffmpeg/ffprobe helpers used throughout the pipeline.
Provides a single wrapper for invoking ffmpeg plus common media operations
(extract, trim, probe, mux, audio processing, etc.).
"""
from __future__ import annotations

import os, re, tempfile, shutil, subprocess
from pathlib import Path


class FfmpegError(RuntimeError):
    pass


def _require(bin_name: str) -> str:
    path = shutil.which(bin_name)
    if not path:
        raise FfmpegError(f"'{bin_name}' not found on PATH")
    return path


# Default flags for ffmpeg commands that generate output.
# Inspecting commands override these when they need verbose stderr.
_STD_FLAGS = ("-y", "-hide_banner", "-loglevel", "error")


# Longest one ffmpeg command may run. Every pass in the pipeline finishes in well
# under a minute on a healthy machine; a command that exceeds this is stuck (ffmpeg
# can spin at 100% CPU after writing its output without ever exiting), and without a
# limit the request waiting on it never returns.
DEFAULT_TIMEOUT_S = 600.0


def _default_timeout() -> float:
    raw = os.environ.get("ATLAS_FFMPEG_TIMEOUT_S", "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def run_ffmpeg(args: list, *, expect: str | Path | None = None,
               cwd: str | Path | None = None, input: bytes | None = None,
               flags: tuple[str, ...] = _STD_FLAGS, check: bool = True,
               label: str = "ffmpeg",
               timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run one ffmpeg command.

    Raises FfmpegError if ffmpeg fails, the expected output file is missing, or the
    command runs longer than `timeout` seconds (default: ATLAS_FFMPEG_TIMEOUT_S, else
    600); on a timeout the process is killed first. Set check=False to return the
    CompletedProcess without validating the exit code or output; a timeout still raises.
    """
    limit = timeout if timeout is not None else _default_timeout()
    cmd = [_require("ffmpeg"), *flags, *[str(a) for a in args]]
    try:
        proc = subprocess.run(cmd, capture_output=True, input=input,
                              text=input is None,
                              cwd=str(cwd) if cwd is not None else None,
                              timeout=limit)
    except subprocess.TimeoutExpired:
        raise FfmpegError(f"{label} timed out after {limit:g}s and was killed")
    if not check:
        return proc
    if proc.returncode != 0 or (expect is not None and not Path(expect).is_file()):
        err = proc.stderr if isinstance(proc.stderr, str) else \
            (proc.stderr or b"").decode("utf-8", "ignore")
        raise FfmpegError(f"{label} failed: {err.strip()}")
    return proc


def run_ffprobe(args: list, *, check: bool = True,
                label: str = "ffprobe") -> str:
    """Run one ffprobe command and return stdout.

    Set check=False to allow failed probes (used when missing streams are valid).
    """
    proc = subprocess.run([_require("ffprobe"), "-v", "error", *[str(a) for a in args]],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise FfmpegError(f"{label} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


_NOKEY = "default=noprint_wrappers=1:nokey=1"


def probe(path: str | Path) -> dict:
    """Return basic video, audio, and format metadata.

    Missing audio is reported as has_audio=False instead of raising.
    """
    dur = run_ffprobe(["-show_entries", "format=duration", "-of", _NOKEY, str(path)],
                      check=False)
    v = run_ffprobe(["-select_streams", "v:0", "-show_entries",
                     "stream=width,height,r_frame_rate,pix_fmt", "-of", _NOKEY,
                     str(path)], check=False).splitlines()
    a = run_ffprobe(["-select_streams", "a:0", "-show_entries",
                     "stream=codec_name,sample_rate,channels", "-of", _NOKEY,
                     str(path)], check=False).splitlines()
    return {
        "duration": float(dur) if dur else 0.0,
        "w": v[0] if len(v) > 0 else "", "h": v[1] if len(v) > 1 else "",
        "fps": v[2] if len(v) > 2 else "", "pix": v[3] if len(v) > 3 else "",
        "has_audio": bool(a), "a_codec": a[0] if len(a) > 0 else "",
        "a_rate": a[1] if len(a) > 1 else "", "a_ch": a[2] if len(a) > 2 else "",
    }


def has_filter(name: str) -> bool:
    """Return True if this ffmpeg build includes the given filter."""
    r = run_ffmpeg(["-filters"], flags=("-hide_banner",), check=False)
    return any(line.split()[1:2] == [name]
               for line in r.stdout.splitlines() if len(line.split()) > 1)

# Extract the last frame for continuous scene
def extract_last_frame(mp4_path: str | Path, out_png: str | Path) -> Path:
    """Save the last visible frame of an MP4 as a PNG.

    Uses ffmpeg's -update mode so the final written frame is preserved.
    """
    mp4_path, out_png = Path(mp4_path), Path(out_png)
    if not mp4_path.is_file():
        raise FfmpegError(f"input MP4 not found: {mp4_path}")
    out_png.parent.mkdir(parents=True, exist_ok=True)

    run_ffmpeg(["-sseof", "-1", "-i", mp4_path, "-update", "1", "-q:v", "1", out_png],
               expect=out_png, label="last-frame extraction")
    return out_png

# Get the duration of the video generated for cost tracking
def get_duration(mp4_path: str | Path) -> float:
    """Return media duration in seconds via ffprobe (for cost tracking)."""
    mp4_path = Path(mp4_path)
    if not mp4_path.is_file():
        raise FfmpegError(f"input MP4 not found: {mp4_path}")
    out = run_ffprobe(["-show_entries", "format=duration", "-of", _NOKEY, mp4_path])
    try:
        return float(out)
    except ValueError:
        raise FfmpegError(f"could not parse duration: {out!r}")
    
def build_tts_track(items: list[tuple[str | Path, int]], out_wav: str | Path) -> Path:
    """Mix TTS phrase WAVs onto one timeline, each delayed to its start_ms
    (adelay per input -> amix). Output length = latest phrase end."""
    if not items:
        raise FfmpegError("build_tts_track: no items")
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    ins: list[str] = []
    filt: list[str] = []
    labels: list[str] = []
    for i, (wav, start_ms) in enumerate(items):
        ins += ["-i", str(wav)]
        filt.append(f"[{i}:a]adelay={int(start_ms)}|{int(start_ms)}[a{i}]")
        labels.append(f"[a{i}]")
    if len(items) == 1:
        graph = filt[0].replace("[a0]", "[out]")
    else:
        graph = ";".join(filt) + ";" + "".join(labels) + \
            f"amix=inputs={len(items)}:normalize=0:dropout_transition=0[out]"

    run_ffmpeg([*ins, "-filter_complex", graph, "-map", "[out]", out_wav],
               expect=out_wav, label="build_tts_track")
    return out_wav


def trim_and_mux(video_mp4: str | Path, tts_wav: str | Path,
                 out_mp4: str | Path, duration_s: float) -> Path:
    """Replace a video's audio with a TTS track and trim the result."""
    video_mp4, tts_wav, out_mp4 = Path(video_mp4), Path(tts_wav), Path(out_mp4)
    for p in (video_mp4, tts_wav):
        if not p.is_file():
            raise FfmpegError(f"trim_and_mux: input not found {p}")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-i", video_mp4, "-i", tts_wav,
        "-map", "0:v:0", "-map", "1:a:0",        # video only (mutes native) + TTS
        "-t", f"{max(duration_s, 0.0):.3f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        out_mp4,
    ], expect=out_mp4, label="trim_and_mux")
    return out_mp4


def extract_audio(mp4_path: str | Path, out_wav: str | Path,
                  sample_rate: int = 16000) -> Path:
    """Extract the audio track as mono PCM WAV (good for ASR/diarization)."""
    mp4_path, out_wav = Path(mp4_path), Path(out_wav)
    if not mp4_path.is_file():
        raise FfmpegError(f"extract_audio: input not found {mp4_path}")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", mp4_path, "-vn", "-ac", "1", "-ar", sample_rate,
                "-f", "wav", out_wav], expect=out_wav, label="extract_audio")
    return out_wav

def strip_audio(mp4_path: str | Path) -> Path:
    """Remove the audio track in place (video stream copied, not re-encoded)."""
    mp4_path = Path(mp4_path)
    if not mp4_path.is_file():
        raise FfmpegError(f"strip_audio: input not found {mp4_path}")
    tmp = Path(tempfile.mkstemp(dir=str(mp4_path.parent), suffix=".mp4")[1])
    try:
        run_ffmpeg(["-i", mp4_path, "-c:v", "copy", "-an", tmp],
                   expect=tmp, label="strip_audio")
    except FfmpegError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, mp4_path)
    return mp4_path

def trim_video(mp4_path: str | Path, seconds: float, out_mp4: str | Path | None = None) -> Path:
    """Trim a video to the first `seconds`.

    If out_mp4 is None, trims the file in place.
    """
    mp4_path = Path(mp4_path)
    if not mp4_path.is_file():
        raise FfmpegError(f"trim_video: input not found {mp4_path}")
    inplace = out_mp4 is None
    dest = Path(out_mp4) if out_mp4 else Path(
        tempfile.mkstemp(dir=str(mp4_path.parent), suffix=".mp4")[1])
    try:
        run_ffmpeg(["-i", mp4_path, "-t", f"{max(seconds, 0.0):.3f}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", dest],
                   expect=dest, label="trim_video")
    except FfmpegError:
        if inplace and dest.exists():
            dest.unlink()
        raise
    if inplace:
        os.replace(dest, mp4_path)
        return mp4_path
    return dest

def trim_silence(in_wav: str | Path, out_wav: str | Path | None = None,
                 threshold_db: float = -30.0) -> Path:
    """Remove leading and trailing silence.

    If out_wav is None, edits the file in place.
    """

    in_wav = Path(in_wav)
    if not in_wav.is_file():
        raise FfmpegError(f"trim_silence: input not found {in_wav}")
    dest = Path(out_wav) if out_wav else Path(
        tempfile.mkstemp(dir=str(in_wav.parent), suffix=".wav")[1])
    af = f"silenceremove=start_periods=1:start_silence=0:start_threshold={threshold_db}dB"
    try:
        run_ffmpeg(["-i", in_wav, "-af", af, dest], expect=dest, label="trim_silence")
    except FfmpegError:
        if out_wav is None and dest.exists():
            dest.unlink()
        raise
    if out_wav is None:
        os.replace(dest, in_wav)
        return in_wav
    return dest

def detect_speech_end(path: str | Path, threshold_db: float = -30.0,
                      min_silence_s: float = 0.30) -> int:
    """Return the end of speech in milliseconds.

    If no trailing silence is found, returns the full audio duration.
    """
    # inspecting call: needs silencedetect's info-level lines on stderr, and a
    # non-zero exit is not fatal — hence its own flags and check=False.
    proc = run_ffmpeg(
        ["-i", path, "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_s}",
         "-f", "null", "-"], flags=("-hide_banner",), check=False)
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", proc.stderr)]
    ends   = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)",   proc.stderr)]
    total_ms = int(get_duration(path) * 1000)
    # an unclosed final silence block == trailing silence
    if starts and len(starts) > len(ends):
        return min(int(starts[-1] * 1000), total_ms)
    return total_ms