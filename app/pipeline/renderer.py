"""
app/pipeline/renderer.py — Layer 8 renderer.

(merge):  concat all trimmed scene MP4s IN BLUEPRINT ORDER via the ffmpeg
          concat demuxer, preserving audio. -> run_0X/merged.mp4
(subs):   build an SRT from scene.text + persisted L6 TTS phrase timings
          (whole-scene caption fallback if a scene has no timing sidecar),
          burn it into the merged video. -> run_0X/subs.srt, merged_subs.mp4

Robust concat: scenes are heterogeneous (video-with-audio carry an AAC TTS
track; B-Roll are silent; aspect can differ). The concat demuxer with -c copy
only works when segments share codec/params, so we probe first and stream-copy
when uniform, else normalise each segment to a common spec (video re-encode +
silent AAC for B-Roll) and then concat-copy. Either way the real TTS audio is
preserved (never re-synthesised).

THE PASS ORDER, and why it is this order (render_merge_and_subs):

    merge -> title -> transitions -> subtitles -> music -> copy to final.mp4

  * Title before transitions, so a transition overlay can sweep across the
    finished title card rather than being painted over by it.
  * Transitions before subtitles, so captions sit on top of the transition and
    stay readable through it.
  * Music last, because it needs the FINAL duration to loop against, and because
    mixing it in earlier would mean re-encoding the bed at every later pass.

Each pass is a separate ffmpeg invocation writing its own intermediate file
(merged.mp4, merged_title.mp4, merged_trans.mp4, merged_subs.mp4). That costs
several video re-encodes per render and generation loss with them, but it makes
each stage independently inspectable — when a render looks wrong, the
intermediates tell you which pass did it. Every pass except merge is a no-op that
returns its input unchanged when it has nothing to do, so the chain stays valid
for a run with no title, no transition assets and no music.

This layer is LOCAL ONLY: no API calls, no credits, just ffmpeg and Pillow. It is
the one layer you can re-run freely.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random as _random
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from app.paths import FONT_DIR, TRANSITIONS_DIR
from app.pipeline.director import has_opening_section
from app.utils.ffmpeg_utils import FfmpegError, has_filter, probe, run_ffmpeg


class RenderError(RuntimeError):
    pass


def _ffmpeg(args: list, *, expect: Path | None = None,
            cwd: Path | None = None, label: str = "ffmpeg"):
    """run_ffmpeg with L8's error contract: every failure in this layer must
    surface as RenderError, which is what the /pipeline/render endpoint
    catches (a bare FfmpegError would escape as a 500 instead of a 502)."""
    try:
        return run_ffmpeg(args, expect=expect, cwd=cwd, label=label)
    except FfmpegError as e:
        raise RenderError(str(e)) from e


@dataclass
class SubtitleStyle:
    """Corporate-deliverable caption style. Applied via libass force_style;
    the drawtext fallback honours the size/colour/position it can."""
    font_name: str = "Helvetica"
    font_size: int = 24
    primary_hex: str = "FFFFFF"     # RRGGBB text colour
    outline_hex: str = "000000"     # RRGGBB outline/box colour
    outline: int = 2                # outline thickness (px)
    margin_v: int = 28              # distance from bottom edge (px)
    alignment: int = 2             # ASS numpad: 2 = bottom-centre

    # --- 'boxed' renderer only (rounded corners are not expressible in ASS) ---
    box_hex: str = "000000"         # box fill colour
    box_opacity: float = 0.55       # slightly transparent
    box_radius: int = 12            # corner radius (px)
    box_pad_x: int = 18             # horizontal padding — keeps the box compact
    box_pad_y: int = 10             # vertical padding

    def _ass_colour(self, rgb: str) -> str:
        # ASS wants &HAABBGGRR (alpha 00 = opaque)
        r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
        return f"&H00{b}{g}{r}".upper()

    def force_style(self) -> str:
        return (
            f"FontName={self.font_name},FontSize={self.font_size},"
            f"PrimaryColour={self._ass_colour(self.primary_hex)},"
            f"OutlineColour={self._ass_colour(self.outline_hex)},"
            f"BorderStyle=1,Outline={self.outline},Shadow=0,"
            f"Alignment={self.alignment},MarginV={self.margin_v}"
        )


DEFAULT_STYLE = SubtitleStyle()


# ------------------------------------------------------------------ scene list
def _ordered_scene_paths(run_dir: Path, blueprint: dict) -> list[tuple[str, Path]]:
    """(scene_id, mp4 path) in BLUEPRINT order, not directory order.

    The blueprint is the timeline, sorting scenes/*.mp4 by filename would give a
    plausible-looking but wrong sequence. Raises RenderError naming the first
    missing clip, which is the common symptom of a scene generation run that died
    part-way.
    """
    scenes_dir = run_dir / "scenes"
    out: list[tuple[str, Path]] = []
    for sc in blueprint.get("scenes", []):
        sid = sc.get("id")
        p = scenes_dir / f"{sid}.mp4"
        if not p.is_file():
            raise RenderError(f"scene '{sid}': {p} missing — run /pipeline/scenes first")
        out.append((sid, p))
    if not out:
        raise RenderError("no scenes to merge")
    return out


def scene_probes(run_dir: str | Path, blueprint: dict) -> list[tuple[str, Path, dict]]:
    """(scene_id, path, probe) for every scene, in blueprint order — ONE ffprobe
    pass over the scene files.

    The merge, subtitle, transition and title passes all need these same
    numbers. Each used to probe every scene itself, so a render re-measured the
    identical files once per pass. Callers may pass the result back in as
    `scenes=` to reuse it; each public entry point still computes its own when
    called standalone.
    """
    return [(sid, p, probe(p)) for sid, p in _ordered_scene_paths(Path(run_dir), blueprint)]


def _durations(scenes: list[tuple[str, Path, dict]]) -> list[float]:
    return [float(pr["duration"]) for _, _, pr in scenes]


def _uniform(probes: list[dict]) -> bool:
    """
    The concat demuxer stitches streams without decoding, so every segment has to
    agree on frame size, pixel format and audio layout — a mismatch produces a
    file that either fails to mux or plays back corrupted after the first cut.
    These seven keys are the ones that have actually bitten; frame rate is
    deliberately not among them because Veo output is uniform on that axis and
    checking it would force a re-encode on nothing.

    In a normal run this returns False: speaking scenes carry AAC and B-Roll
    doesn't, so has_audio differs and the normalise path runs. It returns True
    only for edge cases like an all-B-Roll video.
    """
    first = probes[0]
    keys = ("w", "h", "pix", "has_audio", "a_codec", "a_rate", "a_ch")
    return all(all(p[k] == first[k] for k in keys) for p in probes)


def _normalise(src: Path, dst: Path, w: str, h: str, pr: dict | None = None) -> None:
    """Re-encode video to a common spec; add silent stereo AAC if no audio.
    Video-with-audio keeps its TTS audio (re-encoded to the same AAC spec).
    `pr` is src's already-computed probe; omitted means probe it here."""
    pr = pr if pr is not None else probe(src)
    args = ["-i", str(src)]
    if not pr["has_audio"]:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = "1:a"
    else:
        amap = "0:a"
    args += [
        "-map", "0:v", "-map", amap,
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
        str(dst),
    ]
    _ffmpeg(args, expect=dst, label=f"normalise failed for {src.name}")


def _concat_copy(segments: list[Path], out_mp4: Path) -> None:
    """Concatenate with the concat demuxer and -c copy.

    The demuxer needs its input list as a FILE, hence the temp listfile (written
    next to the output so it shares a filesystem, and always removed). -safe 0
    permits the absolute paths written into it. +faststart moves the moov atom to
    the front so the result streams in a browser without a full download.

    Requires uniform segments — call _uniform first or normalise. Passing
    mismatched segments here does not necessarily fail loudly; it can produce a
    file that plays correctly up to the first cut and then breaks.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     dir=str(out_mp4.parent)) as f:
        for seg in segments:
            f.write(f"file '{seg.resolve().as_posix()}'\n")
        listfile = f.name
    try:
        _ffmpeg(["-f", "concat", "-safe", "0", "-i", listfile,
                 "-c", "copy", "-movflags", "+faststart", str(out_mp4)],
                expect=out_mp4, label="concat demuxer")
    finally:
        Path(listfile).unlink(missing_ok=True)


def merge_scenes(run_dir: str | Path, blueprint: dict,
                 scenes: list[tuple[str, Path, dict]] | None = None) -> Path:
    """D6T5: merged.mp4 = all scenes concatenated in order, audio preserved."""
    run_dir = Path(run_dir)
    scenes = scenes if scenes is not None else scene_probes(run_dir, blueprint)
    paths = [p for _, p, _ in scenes]
    probes = [pr for _, _, pr in scenes]
    out = run_dir / "merged.mp4"

    if _uniform(probes):
        _concat_copy(paths, out)
        return out

    # heterogeneous -> normalise to the first scene's frame size, then concat-copy.
    # The FIRST scene sets the target geometry, and the first scene is always the
    # opening B-Roll. Everything else is letterboxed into it rather than cropped
    # (scale + pad, force_original_aspect_ratio=decrease), so a scene authored at
    # a different aspect_ratio gets bars, never a cropped subject. A blueprint
    # with mixed aspect ratios is not rejected anywhere — it just looks like this.
    w = probes[0]["w"] or "1280"
    h = probes[0]["h"] or "720"
    norm_dir = run_dir / "_render_norm"
    norm_dir.mkdir(parents=True, exist_ok=True)
    norm: list[Path] = []
    for sid, src, pr in scenes:
        dst = norm_dir / f"{sid}.mp4"
        _normalise(src, dst, w, h, pr)
        norm.append(dst)
    _concat_copy(norm, out)
    return out


def _srt_ts(ms: int) -> str:
    """ms -> SRT timestamp "HH:MM:SS,mmm" (comma, not the period WebVTT uses)."""
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _load_timings(run_dir: Path, scene_id: str) -> dict | None:
    """Scenes sidecar written by process_scene: {phrases, starts_ms, durs_ms}."""
    p = run_dir / "tts" / f"{scene_id}_timings.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# Maximum caption line, in DISPLAY columns — not characters. A CJK glyph occupies
# two columns, so 26 Chinese characters are as wide as 52 Latin ones and a naive
# len() would let them run off frame.
#
# 64, NOT the 42 that broadcast style guides recommend. Those guides are written
# for cinema and TV, where the title-safe area is far tighter than a 1280px video
# played in a browser. At 42 this content needed FOUR lines for one sentence,
# which stacks up the frame and looks worse than a long line ever did. Measured
# at font_size 24 on 1280px:
#
#     width   longest sentence   widest box
#      42        4 lines           438px (34% of frame)
#      64        2 lines           750px (59% of frame)
#
# 59% is a normal-looking subtitle. Two lines is the number people expect to see;
# raise this only if captions start touching the edges of the frame.
_CAPTION_WIDTH = 64


def _display_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _wrap_caption(text: str, width: int | None = None) -> str:
    """Break a caption into lines with '\\n', which every burn method honours.

    ADDED 2026-08-03, and it is not optional. Dropping comma-splitting
    (open-questions #29) made captions whole sentences, and sentences in this
    content run to 90-127 characters. Nothing else in the renderer wraps:
    `_burn_boxed` clamps the BOX to the frame width but never the text, and the
    ASS method sets WrapStyle 2 (explicit breaks only). So an unwrapped long
    caption is drawn straight off the edge of the picture.

    Comma-splitting used to hide this by never producing a long caption. Removing
    it exposed the gap rather than creating it.

    Chinese and Japanese have no spaces, so word wrapping cannot work there — a
    caption with no break opportunities is chunked by display width instead.

    `width` resolves from the module constant at CALL time, not as a default
    argument. A default would be bound once at import, so overriding
    _CAPTION_WIDTH — in a test, or to try a different look — would silently do
    nothing. That is exactly the trap it looks like.
    """
    width = _CAPTION_WIDTH if width is None else width
    text = " ".join((text or "").split())
    if not text or _display_width(text) <= width:
        return text

    words = text.split(" ")
    if len(words) == 1:                       # CJK / unbroken run
        lines, cur = [], ""
        for ch in text:
            if _display_width(cur) + _display_width(ch) > width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return "\n".join(lines)
    
    def greedy(limit: int) -> list[str]:
        """Fill lines up to `limit` display columns. Never overflows, except on a
        single word longer than the limit, nothing but hyphenation would help
        there, and these are captions, not typesetting."""
        out, cur = [], ""
        for w in words:
            cand = f"{cur} {w}".strip()
            if cur and _display_width(cand) > limit:
                out.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            out.append(cur)
        return out

    # Fewest lines first, then balance within that count. Wrapping at the ideal
    # average alone tends to cost an extra line, because words do not divide
    # evenly, so widen the target until the line count comes back down to the
    # minimum. Balanced AND minimal, rather than one at the expense of the other.
    total = _display_width(text)
    n_lines = max(1, -(-total // width))                 # ceil: the best possible
    for limit in range(-(-total // n_lines), width + 1):
        lines = greedy(limit)
        if len(lines) <= n_lines:
            return "\n".join(lines)
    return "\n".join(greedy(width))


def build_srt(run_dir: str | Path, blueprint: dict,
              scenes: list[tuple[str, Path, dict]] | None = None) -> Path:
    """D6T6: SRT across the merged timeline. Per-phrase cues when L6 timings
    exist; otherwise one whole-scene caption. B-Roll (no text) is skipped.
    Text is scene.text, already authored in config.language.

    The scene layer sidecar timings are relative to their own scene; `offset_ms`
    accumulates scene durations to convert them onto the merged timeline. It
    accumulates the PROBED duration of each clip, not the blueprint length, which
    is what keeps captions in sync after scene layer trimmed each scene to its own voice
    track.

    Three guards against unreadable output, in this order:
      * MIN_CUE_MS floors every cue at 500ms, so a one-word phrase does not flash;
      * a cue is then truncated at its own scene's end, and if that leaves it
        under the floor, its START is pulled back instead of shortening it;
      * cues closer together than MIN_CUE_MS are MERGED into one caption, so a
        run of fragments becomes a single readable line rather than a strobe.
    Only then are overlaps resolved by clamping the earlier cue's end to the
    later one's start — two captions must never be on screen at once.

    The merge has to come before the clamp. Clamping first can shorten a cue to
    1ms (two phrases sharing a start time), which drops its text from the video
    without any error — see the comment on the merge loop below.

    Writes run_0X/subs.srt and returns the path. The SRT is a real deliverable in
    its own right, not only an input to the burn step.
    """
    run_dir = Path(run_dir)
    scenes = scenes if scenes is not None else scene_probes(run_dir, blueprint)
    by_id = {sc.get("id"): sc for sc in blueprint.get("scenes", [])}

    cues: list[tuple[int, int, str]] = []
    offset_ms = 0
    MIN_CUE_MS = 500
    for sid, path, pr in scenes:
        dur_ms = int(round(pr["duration"] * 1000))
        text = (by_id.get(sid, {}).get("text") or "").strip()
        if text:
            tim = _load_timings(run_dir, sid)
            if tim and tim.get("phrases") and tim.get("starts_ms") and tim.get("durs_ms"):
                scene_end = offset_ms + dur_ms
                for phrase, st, du in zip(tim["phrases"], tim["starts_ms"], tim["durs_ms"]):
                    txt = phrase.strip()
                    if not txt:
                        continue
                    a = offset_ms + int(st)
                    b = offset_ms + int(st) + max(int(du), MIN_CUE_MS)   # floor duration
                    b = min(b, scene_end)
                    if b - a < MIN_CUE_MS:            # ran into scene end -> pull start back
                        a = max(offset_ms, scene_end - MIN_CUE_MS)
                    cues.append((a, b, txt))
            else:
                cues.append((offset_ms, offset_ms + dur_ms, text))  # fallback
        offset_ms += dur_ms

    # MERGE cues that are too close to be read separately, THEN clamp what is
    # left. Order matters and the old order lost text.
    #
    # Merging is the right answer rather than clamping harder: two phrases at the
    # same instant ARE one caption. It keeps the words, and it gives the result a
    # readable duration instead of a sliver.
    cues.sort()
    merged: list[tuple[int, int, str]] = []
    for a, b, t in cues:
        if merged:
            pa, pb, pt = merged[-1]
            # Merge ONLY when the next cue starts so soon that clamping the
            # previous one would leave it under the floor. Cues that merely
            # overlap are left alone — the clamp handles those correctly and
            # still gives each its own line, which is what you want.
            if a - pa < MIN_CUE_MS:
                merged[-1] = (pa, max(pb, b), f"{pt} {t}".strip())
                continue
        merged.append((a, b, t))
    cues = merged

    for i in range(len(cues) - 1):
        a, b, t = cues[i]
        na = cues[i + 1][0]
        if b > na:                       # clamp end to next cue's start
            cues[i] = (a, max(a + 1, na), t)

    out = run_dir / "subs.srt"
    lines: list[str] = []
    for i, (a, b, txt) in enumerate(cues, 1):
        # Wrapped last, after merging — merging concatenates text, so wrapping
        # any earlier would have to be redone anyway.
        lines += [str(i), f"{_srt_ts(a)} --> {_srt_ts(b)}", _wrap_caption(txt), ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",       # macOS
    "/System/Library/Fonts/Helvetica.ttc",                # macOS
    "/Library/Fonts/Arial.ttf",                           # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",    # Debian/Ubuntu
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",             # Fedora
)


# Scripts whose glyphs the default Latin fonts do NOT contain. Subtitles in
# these languages render as blank boxes unless a script-capable font is used.
_SCRIPT_FONTS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("japanese", "chinese", "korean", "mandarin", "cantonese"): (
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ),
    ("thai",): (
        "/System/Library/Fonts/Supplemental/Ayuthaya.ttf",
        "/usr/share/fonts/truetype/tlwg/Loma.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    ),
    ("arabic", "urdu", "persian", "farsi"): (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    ),
    ("hindi", "devanagari", "nepali", "marathi"): (
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    ),
}


def read_language(run_dir: str | Path) -> str:
    """config.language — the language scene.text (and therefore the subtitles)
    is already authored in. Subtitles are NOT translated here; L8 only has to
    RENDER the language correctly."""
    cfg = Path(run_dir) / "config.json"
    if cfg.is_file():
        try:
            return str(json.loads(cfg.read_text(encoding="utf-8")).get("language") or "English")
        except Exception:
            pass
    return "English"

def _needs_script_font(language: str | None) -> bool:
    """True when the language needs non-Latin glyphs (CJK/Thai/Arabic/Devanagari)."""
    if not language:
        return False
    lang = language.strip().lower()
    return any(any(k in lang for k in keys) for keys in _SCRIPT_FONTS)


def _title_font(style: TitleStyle, language: str | None) -> str:
    if style.font_file and not _needs_script_font(language):
        p = Path(style.font_file).resolve()
        if not p.is_file():
            raise RenderError(
                f"title font not found: {style.font_file} (resolved {p}) — "
                f"place the TTF there or unset font_file")
        return str(p)
    font = _find_font(language)
    if not font:
        raise RenderError("opening title needs a TTF font; none found on this system")
    return font


def _find_font(language: str | None = None) -> str | None:
    """Pick a font that can actually draw `language`. Falls back to the Latin
    candidates, which are correct for English/Indonesian/most European
    languages."""
    if language:
        lang = language.strip().lower()
        for keys, fonts in _SCRIPT_FONTS.items():
            if any(k in lang for k in keys):
                for f in fonts:
                    if Path(f).is_file():
                        return f
                break            # script known but no font installed -> warn below
    for p in _FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    return None

def font_warning(run_dir: str | Path) -> str | None:
    """Non-fatal warning when the run's language needs a script font that is
    not installed — subtitles would render as empty boxes."""
    lang = read_language(run_dir).strip().lower()
    for keys, fonts in _SCRIPT_FONTS.items():
        if any(k in lang for k in keys):
            if not any(Path(f).is_file() for f in fonts):
                return (f"language '{lang}' needs a script-capable font; none of "
                        f"{', '.join(fonts)} is installed - subtitles may render as blank boxes")
            return None
    return None


def _parse_srt(path: Path) -> list[tuple[float, float, str]]:
    """Parse our own SRT back into (start_s, end_s, text) cues.

    Deliberately minimal — it only has to read what build_srt wrote, which is why
    it assumes well-formed blocks and no styling tags. The round trip through a
    file exists because the SRT is a deliverable AND because the two non-libass
    burn methods need per-cue times as numbers.
    """
    def to_s(ts: str) -> float:
        hh, mm, rest = ts.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0

    cues: list[tuple[float, float, str]] = []
    block: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines() + [""]:
        if line.strip() == "":
            if len(block) >= 2 and "-->" in block[1]:
                a, b = [x.strip() for x in block[1].split("-->")]
                cues.append((to_s(a), to_s(b), "\n".join(block[2:]).strip()))
            block = []
        else:
            block.append(line)
    return cues


def _drawtext_esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


def _burn_libass(run_dir: Path, merged: Path, srt: Path, out: Path,
                 style: SubtitleStyle) -> None:
    """Burn captions with ffmpeg's subtitles filter (needs a libass-enabled build).

    Runs with cwd=run_dir and passes only srt.name, not the full path. That is not
    tidiness: the subtitles filter's filename argument goes through ffmpeg's own
    filtergraph parser, where the colon in an absolute path (or a Windows drive
    letter) is an argument separator. Making it a bare relative filename sidesteps
    the escaping problem entirely.
    """
    esc = _drawtext_esc(srt.name)
    vf = f"subtitles=filename='{esc}':force_style='{style.force_style()}'"
    _ffmpeg(["-i", str(merged.resolve()), "-vf", vf,
             "-c:a", "copy", "-movflags", "+faststart", str(out.resolve())],
            expect=out, cwd=run_dir, label="libass burn")


def _burn_drawtext(run_dir: Path, merged: Path, srt: Path, out: Path,
                   style: SubtitleStyle) -> None:
    """libass-free burn: one drawtext per cue, timed with enable=between().
    Each cue's text goes to a bare textfile in run_dir to avoid escaping.
    Honours font size / colour / vertical margin from the style (drawtext has
    no rich ASS styling, so this is a best-effort match)."""
    font = _find_font(read_language(run_dir))
    if not font:
        raise RenderError(
            "no subtitles filter (ffmpeg built without libass) and no usable "
            "font found for the drawtext fallback — install an ffmpeg build "
            "with libass, or place a .ttf at one of: " + ", ".join(_FONT_CANDIDATES))

    cues = _parse_srt(srt)
    if not cues:
        raise RenderError("no subtitle cues to burn")

    font_esc = _drawtext_esc(font)
    steps: list[str] = []
    cue_files: list[Path] = []
    for i, (a, b, txt) in enumerate(cues):
        cf = run_dir / f"_cue_{i}.txt"
        cf.write_text(txt, encoding="utf-8")
        cue_files.append(cf)
        steps.append(
            f"drawtext=fontfile='{font_esc}':textfile='{cf.name}':reload=0:"
            f"enable='between(t,{a:.3f},{b:.3f})':"
            f"x=(w-text_w)/2:y=h-text_h-{style.margin_v}:"
            f"fontsize={style.font_size}:fontcolor=0x{style.primary_hex}:"
            f"box=1:boxcolor=0x{style.outline_hex}@0.5:boxborderw=10:line_spacing=6"
        )
    vf = ",".join(steps)
    try:
        _ffmpeg(["-i", str(merged.resolve()), "-vf", vf,
                 "-c:a", "copy", "-movflags", "+faststart", str(out.resolve())],
                expect=out, cwd=run_dir, label="drawtext burn")
    finally:
        for cf in cue_files:
            cf.unlink(missing_ok=True)

def _burn_boxed(run_dir: Path, merged: Path, srt: Path, out: Path,
                style: SubtitleStyle) -> None:
    """Rounded-box captions. Neither libass nor drawtext can draw rounded
    corners, so each cue is rendered to a transparent PNG (Pillow) and overlaid
    on its time window. Gives full control of radius, opacity and padding.

    COST: this builds ONE ffmpeg input and TWO filtergraph nodes per cue, chained.
    A long video with per-phrase captions can reach a hundred-plus inputs in a
    single command; it works, but it is the slowest pass in the renderer and it
    will hit ffmpeg's argument limits eventually. If you see the burn step fail on
    a long video with an obscure filtergraph error, this is the first suspect.

    The sentence above about rounded corners is true of libass's BUILT-IN box
    (BorderStyle=4 is square) but not of libass itself, which renders ASS vector
    drawings. `_burn_ass_boxed` (method='ass') produces the same look at flat cost
    — one input, one filter, any number of cues. It is on trial rather than the
    default; when it passes a visual check, this function can go. See
    docs/open-questions.md #23.

    Pillow is used only for measurement and drawing; the PNGs are deleted in the
    finally block. Requires a TTF the run's language can be drawn with.
    """
    from PIL import Image, ImageDraw, ImageFont

    font_path = _find_font(read_language(run_dir))
    if not font_path:
        raise RenderError("boxed subtitles need a TTF font; none found on this system")

    cues = _parse_srt(srt)
    if not cues:
        raise RenderError("no subtitle cues to burn")

    vp = probe(merged)
    W, H = int(vp["w"] or 1280), int(vp["h"] or 720)
    font = ImageFont.truetype(font_path, style.font_size)
    fill = tuple(int(style.box_hex[i:i + 2], 16) for i in (0, 2, 4)) + \
        (int(max(0.0, min(1.0, style.box_opacity)) * 255),)
    text_rgb = tuple(int(style.primary_hex[i:i + 2], 16) for i in (0, 2, 4)) + (255,)

    png_files: list[Path] = []
    inputs: list[str] = ["-i", str(merged.resolve())]
    steps: list[str] = []
    base = "[0:v]"

    for i, (a, b, txt) in enumerate(cues):
        lines = txt.split("\n")
        probe_img = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        widths, heights = [], []
        for ln in lines:
            x0, y0, x1, y1 = probe_img.textbbox((0, 0), ln, font=font)
            widths.append(x1 - x0)
            heights.append(y1 - y0)
        line_gap = max(4, style.font_size // 5)
        tw = max(widths) if widths else 0
        th = sum(heights) + line_gap * (len(lines) - 1)

        bw = min(W, tw + 2 * style.box_pad_x)
        bh = th + 2 * style.box_pad_y
        img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, bw - 1, bh - 1], radius=style.box_radius, fill=fill)

        y = style.box_pad_y
        for ln, hgt in zip(lines, heights):
            x0, y0, x1, y1 = d.textbbox((0, 0), ln, font=font)
            d.text(((bw - (x1 - x0)) / 2 - x0, y - y0), ln, font=font, fill=text_rgb)
            y += hgt + line_gap

        png = run_dir / f"_sub_{i}.png"
        img.save(png)
        png_files.append(png)

        j = i + 1
        inputs += ["-i", str(png.resolve())]
        x_pos = int((W - bw) / 2)
        y_pos = int(H - bh - style.margin_v)
        out_lbl = f"[s{j}]"
        steps.append(
            f"{base}[{j}:v]overlay={x_pos}:{y_pos}:"
            f"enable='between(t,{a:.3f},{b:.3f})'{out_lbl}")
        base = out_lbl

    args = [*inputs, "-filter_complex", ";".join(steps), "-map", base]
    if vp["has_audio"]:
        args += ["-map", "0:a", "-c:a", "copy"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out.resolve())]
    try:
        _ffmpeg(args, expect=out, label="boxed subtitle burn")
    finally:
        for p in png_files:
            p.unlink(missing_ok=True)


def _ass_time(t: float) -> str:
    """ASS timestamps are H:MM:SS.cc — centiseconds, and the hour is not padded."""
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_text_esc(s: str) -> str:
    """Escape a caption for an ASS Dialogue line.

    Braces open an override block in ASS, so a literal one in the script would
    silently eat the rest of the caption. Newlines become \\N (hard break)."""
    return (s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
             .replace("\n", "\\N"))


def _ass_rounded_rect(w: int, h: int, r: int) -> str:
    """A rounded rectangle as ASS vector drawing commands.

    This is the whole trick behind the `ass` method. libass renders `\\p1`
    drawing mode, so a rounded box is a SHAPE in the subtitle file rather than a
    PNG that has to be overlaid as a separate ffmpeg input. `b` is a cubic
    bezier; putting both control points on the corner itself gives the usual
    circular-ish fillet, which is what Pillow's rounded_rectangle draws too.

    Coordinates are relative to the drawing's own origin, which \\pos places.
    """
    r = max(0, min(r, w // 2, h // 2))
    if r == 0:
        return f"m 0 0 l {w} 0 l {w} {h} l 0 {h}"
    return (
        f"m {r} 0 "
        f"l {w - r} 0 b {w} 0 {w} 0 {w} {r} "          # top-right
        f"l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "  # bottom-right
        f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} "            # bottom-left
        f"l 0 {r} b 0 0 0 0 {r} 0"                       # top-left
    )


def _burn_ass_boxed(run_dir: Path, merged: Path, srt: Path, out: Path,
                    style: SubtitleStyle) -> None:
    """Rounded-box captions as a single ASS file — the flat-cost alternative to
    _burn_boxed (docs/open-questions.md #23).

    Same look, different mechanism. `_burn_boxed` renders each cue to a PNG and
    overlays it, which costs one ffmpeg INPUT and two filtergraph nodes per cue;
    a hundred cues is a hundred inputs and it will eventually hit a limit. Here
    every cue is two lines in one .ass file: a drawing on layer 0 for the box and
    the text on layer 1 above it. One input, one `subtitles` filter, any number
    of cues.

    The claim in _burn_boxed that "neither libass nor drawtext can draw rounded
    corners" is true of libass's BUILT-IN box (BorderStyle=4 is square) but not of
    libass itself, which renders vector drawings.

    Pillow is still used, but only to MEASURE — the box has to be wide enough for
    the text, and only a font engine can say how wide that is. libass then does
    the drawing. Measurement is approximate (Pillow and libass are different
    rasterisers), which is what box_pad_x absorbs.

    Requires an ffmpeg built with libass. Same output contract as every other
    burn method: writes `out`, copies audio.
    """
    from PIL import ImageFont

    font_path = _find_font(read_language(run_dir))
    if not font_path:
        raise RenderError("ass subtitles need a TTF font; none found on this system")

    cues = _parse_srt(srt)
    if not cues:
        raise RenderError("no subtitle cues to burn")

    vp = probe(merged)
    W, H = int(vp["w"] or 1280), int(vp["h"] or 720)
    font = ImageFont.truetype(font_path, style.font_size)
    # libass resolves fonts by FAMILY NAME through fontconfig, not by path — so
    # the name has to come off the file we measured with, or the box would be
    # sized for one font and drawn with another. fontsdir (set on the filter
    # below) is what makes a font outside the system directories findable.
    family = font.getname()[0] or style.font_name

    def bgr(rgb: str) -> str:
        return f"{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()

    # ASS alpha is INVERTED: 00 is opaque, FF is invisible.
    box_alpha = f"{int(max(0.0, min(1.0, 1.0 - style.box_opacity)) * 255):02X}"

    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "WrapStyle: 2",              # no automatic wrapping — build_srt already broke the lines
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        # Box: alignment 7 (top-left) so \pos is the box's top-left corner.
        f"Style: Box,{family},{style.font_size},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        # Text: alignment 5 (centre-centre) so \pos is the text block's centre —
        # that is what keeps it centred in the box whatever libass measures.
        #
        # Outline is 0, NOT style.outline. That looks like a bug and is not: the
        # box behind the text is what provides contrast, and _burn_boxed draws
        # plain unoutlined text for the same reason. style.outline belongs to the
        # 'libass' method, which has no box. Setting it here would make 'ass'
        # visibly heavier than 'boxed' and spoil the A/B this method exists for.
        f"Style: Text,{family},{style.font_size},&H00{bgr(style.primary_hex)},"
        f"&H000000FF,&H00{bgr(style.outline_hex)},&H00000000,0,0,0,0,100,100,"
        f"0,0,1,0,0,5,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]

    line_gap = max(4, style.font_size // 5)
    events: list[str] = []
    for a, b, txt in cues:
        lines = txt.split("\n")
        widths, heights = [], []
        for ln in lines:
            x0, y0, x1, y1 = font.getbbox(ln)
            widths.append(x1 - x0)
            heights.append(y1 - y0)
        tw = max(widths) if widths else 0
        th = sum(heights) + line_gap * (len(lines) - 1)

        bw = min(W, tw + 2 * style.box_pad_x)
        bh = th + 2 * style.box_pad_y
        x0 = int((W - bw) / 2)
        y0 = int(H - bh - style.margin_v)

        start, end = _ass_time(a), _ass_time(b)
        shape = _ass_rounded_rect(bw, bh, style.box_radius)
        events.append(
            f"Dialogue: 0,{start},{end},Box,,0,0,0,,"
            f"{{\\pos({x0},{y0})\\1c&H{bgr(style.box_hex)}&\\1a&H{box_alpha}&"
            f"\\bord0\\shad0\\p1}}{shape}{{\\p0}}"
        )
        events.append(
            f"Dialogue: 1,{start},{end},Text,,0,0,0,,"
            f"{{\\pos({W // 2},{y0 + bh // 2})}}{_ass_text_esc(txt)}"
        )

    ass = run_dir / "subs.ass"
    ass.write_text("\n".join(head + events) + "\n", encoding="utf-8")

    # cwd=run_dir + bare filename for the same reason as _burn_libass: the
    # filter's filename argument goes through ffmpeg's filtergraph parser, where
    # a colon in an absolute path is an argument separator.
    vf = (f"subtitles=filename='{_drawtext_esc(ass.name)}'"
          f":fontsdir='{_drawtext_esc(str(Path(font_path).parent))}'")
    _ffmpeg(["-i", str(merged.resolve()), "-vf", vf,
             "-c:a", "copy", "-movflags", "+faststart", str(out.resolve())],
            expect=out, cwd=run_dir, label="ass subtitle burn")


def burn_subtitles(run_dir: str | Path, merged_mp4: str | Path, srt: str | Path,
                   method: str = "boxed", style: SubtitleStyle | None = None) -> Path:
    """Burn subs into the merged video -> merged_subs.mp4. Audio copied.

    Methods:
      'boxed'    (default) rounded semi-transparent box behind each cue, drawn
                 as PNG overlays. Correct output, but the filtergraph grows per
                 cue — see _burn_boxed and open-questions #23.
      'ass'      the same rounded box drawn as ASS vector shapes in ONE subtitle
                 file. Flat cost regardless of cue count. Needs libass.
                 ON TRIAL — not the default until it passes a visual check
                 against 'boxed'. If it does, 'boxed' can retire.
      'libass'   plain ASS styling via force_style (square box / outline).
      'drawtext' libass-free fallback (square box).
      'auto'     libass when available, else drawtext.
    """
    run_dir = Path(run_dir)
    merged = Path(merged_mp4)
    srt = Path(srt)
    out = run_dir / "merged_subs.mp4"
    style = style or DEFAULT_STYLE

    use = method
    if use == "auto":
        use = "libass" if has_filter("subtitles") else "drawtext"

    if use == "boxed":
        _burn_boxed(run_dir, merged, srt, out, style)
    elif use == "ass":
        if not has_filter("subtitles"):
            raise RenderError(
                "method='ass' needs an ffmpeg built with libass; use 'boxed'")
        _burn_ass_boxed(run_dir, merged, srt, out, style)
    elif use == "libass":
        _burn_libass(run_dir, merged, srt, out, style)
    elif use == "drawtext":
        _burn_drawtext(run_dir, merged, srt, out, style)
    else:
        raise RenderError(f"unknown subtitle method '{method}'")
    return out


def render_merge_and_subs(run_dir: str | Path, blueprint: dict,
                          style: SubtitleStyle | None = None,
                          music_gain_db: float = -20.0,
                          subtitle_method: str = "boxed") -> dict:
    """D6T5 + D6T6 (+ D7T2 music if present) chained.

    merge -> burn subtitles -> mix music_bg/track.mp3 under the narration
    (looped+crossfaded to the video length) when the track exists. Returns
    {merged, srt, merged_subs, music}.

    `subtitle_method` is passed straight to burn_subtitles. It exists so 'ass'
    can be compared against 'boxed' on the same run without editing code —
    rendering is free and re-runnable, so an A/B costs nothing but disk."""
    run_dir = Path(run_dir)
    # ONE ffprobe pass over the scene files, shared by every pass below.
    scenes = scene_probes(run_dir, blueprint)
    merged = merge_scenes(run_dir, blueprint, scenes)
    titled = apply_opening_title(run_dir, blueprint, merged, style=None,
                                 scenes=scenes)                    # D7T1b (no-op if no title)
    transitioned = apply_transitions(run_dir, blueprint, titled,
                                     scenes=scenes)                # D7T1 (no-op if no assets)
    srt = build_srt(run_dir, blueprint, scenes)
    subbed = burn_subtitles(run_dir, transitioned, srt, method=subtitle_method,
                            style=style)

    music_used = False
    track = run_dir / "music_bg" / "track.mp3"
    if track.is_file():
        tmp = run_dir / "_subs_music.mp4"
        mix_music_under_video(subbed, track, tmp, music_gain_db=music_gain_db)
        tmp.replace(subbed)                      # music now inside merged_subs.mp4
        music_used = True

    final = run_dir / "final.mp4"
    shutil.copy2(subbed, final)

    return {"merged": str(merged), "srt": str(srt),
            "merged_subs": str(subbed), "music": music_used,
            "final": str(final), "language": read_language(run_dir),
            "font_warning": font_warning(run_dir)}


# ================================================================== D7T2 music
def loop_music_to_duration(music_path: str | Path, target_s: float,
                           out_path: str | Path, overlap_s: float = 3.0,
                           tail_fade_s: float = 1.0) -> Path:
    """Produce a seamless bed of exactly `target_s` from a shorter track by
    chaining acrossfade joins (each ~overlap_s of musical overlap = no click at
    the seam), then trimming to length with a short tail fade-out. If the track
    already covers target_s, just trim + fade.

    N crossfaded copies span:  N*L - (N-1)*overlap  seconds.

    Lyria returns a fixed ~30s clip, so any video longer than that needs looping,
    and a plain loop clicks audibly at the seam. acrossfade blends each copy into
    the next over `overlap_s` of real music, which is why the arithmetic above
    subtracts the overlap per join when working out how many copies are needed.

    `ov` is capped at half the track length so the crossfade cannot consume more
    material than exists. The tail fade is cosmetic — it stops the bed ending
    mid-phrase when the video does.
    """
    music_path = Path(music_path)
    out_path = Path(out_path)
    L = probe(music_path)["duration"]
    if L <= 0:
        raise RenderError(f"music has no duration: {music_path}")

    ov = max(0.1, min(overlap_s, L * 0.5))            # overlap can't exceed half the track

    if L >= target_s:
        _ffmpeg(["-i", str(music_path),
                 "-af", f"atrim=0:{target_s:.3f},afade=t=out:st={max(0.0, target_s - tail_fade_s):.3f}:d={tail_fade_s}",
                 str(out_path)], expect=out_path, label="music trim")
        return out_path

    # how many copies to exceed target, given the overlap consumed per join
    n = max(2, math.ceil((target_s - ov) / (L - ov)))

    inputs: list[str] = []
    for _ in range(n):
        inputs += ["-i", str(music_path)]
    # chain: [0][1]acrossfade -> [x1]; [x1][2]acrossfade -> [x2]; ...
    steps = []
    prev = "[0:a]"
    for i in range(1, n):
        lbl = f"[x{i}]"
        steps.append(f"{prev}[{i}:a]acrossfade=d={ov:.3f}:c1=tri:c2=tri{lbl}")
        prev = lbl
    fade_start = max(0.0, target_s - tail_fade_s)
    steps.append(f"{prev}atrim=0:{target_s:.3f},afade=t=out:st={fade_start:.3f}:d={tail_fade_s}[out]")
    fc = ";".join(steps)

    _ffmpeg([*inputs, "-filter_complex", fc, "-map", "[out]", str(out_path)],
            expect=out_path, label="music loop")
    return out_path


def mix_music_under_video(video_path: str | Path, music_path: str | Path,
                          out_path: str | Path, music_gain_db: float = -20.0,
                          overlap_s: float = 3.0) -> Path:
    """Loop music to the video's duration and mix it UNDER the existing audio
    (narration) at music_gain_db. Narration stays at full level (amix
    normalize=0). If the video has no audio track (all B-Roll), the looped
    music becomes the track.

    normalize=0 is the important flag. amix normalises by default, which would
    attenuate the narration by the number of inputs — the voice would audibly duck
    the moment music appeared. With it off, the music is scaled by an explicit
    volume filter (-20dB by default) and the narration passes through untouched.

    duration=first ties the output to the video, so a bed that came out slightly
    long cannot extend it. Video is stream-copied; only audio is re-encoded.
    """
    video_path = Path(video_path)
    music_path = Path(music_path)
    out_path = Path(out_path)

    vp = probe(video_path)                   # one probe serves duration + audio test
    vdur = vp["duration"]
    if vdur <= 0:
        raise RenderError(f"video has no duration: {video_path}")

    gain = 10 ** (music_gain_db / 20.0)
    tmp_bed = out_path.parent / "_music_bed.m4a"
    loop_music_to_duration(music_path, vdur, tmp_bed, overlap_s=overlap_s)

    try:
        args = ["-i", str(video_path), "-i", str(tmp_bed)]
        if vp["has_audio"]:
            fc = (f"[1:a]volume={gain:.4f}[m];"
                  f"[0:a][m]amix=inputs=2:duration=first:normalize=0[a]")
            args += ["-filter_complex", fc, "-map", "0:v", "-map", "[a]"]
        else:
            args += ["-map", "0:v", "-map", "1:a"]
        args += ["-c:v", "copy", "-c:a", "aac", "-shortest",
                 "-movflags", "+faststart", str(out_path)]
        _ffmpeg(args, expect=out_path, label="music mix")
    finally:
        tmp_bed.unlink(missing_ok=True)
    return out_path


# ================================================================== D7T1 transitions
@dataclass
class TransitionEntry:
    transition_id: str
    file: str                       # relative to the transitions dir
    roles: list[str]                # broll_roles this overlay suits
    aspect_ratio: str = "16:9"

    # How to composite:
    #   "alpha"    -> file has a real alpha channel (.mov/.webm); honour it.
    #   "colorkey" -> no alpha (e.g. black-background clip); key out key_color.
    blend: str = "alpha"
    key_color: str = "0x000000"     # colour to make transparent when blend="colorkey"
    key_similarity: float = 0.15    # 0..1 — how close to key_color counts as bg
    key_blend: float = 0.10         # 0..1 — edge softness


def transitions_dir() -> Path:
    """Global transition asset library, like the character/audio libraries.

    Defaults to the repo's own asset/transitions, resolved from __file__ (see
    app/paths.py) so it works from any working directory. ATLAS_TRANSITIONS_DIR
    overrides it — use that to point at a library outside the repo.
    """
    override = os.environ.get("ATLAS_TRANSITIONS_DIR")
    return Path(override) if override else TRANSITIONS_DIR


def load_transition_index(assets_dir: Path | None = None) -> list[TransitionEntry]:
    """Read <transitions dir>/index.json -> the overlay pool.

    Returns [] when no index exists at the DEFAULT location: transitions are
    optional, and a repo with none installed should still render.

    But an index missing from an EXPLICITLY configured location raises. Absence
    is only allowed to be silent when it is plausibly intentional; a deployment
    that set ATLAS_TRANSITIONS_DIR meant to have transitions, and silently
    dropping them there produces the worst symptom in the renderer — a video
    that succeeds and is quietly wrong.

    Also raises if the index lists a file that isn't on disk, which is why a
    broken library fails at load rather than mid-filtergraph.
    """
    d = Path(assets_dir) if assets_dir else transitions_dir()
    idx = d / "index.json"
    if not idx.is_file():
        if os.environ.get("ATLAS_TRANSITIONS_DIR"):
            raise RenderError(
                f"ATLAS_TRANSITIONS_DIR is set to '{d}' but no index.json is there. "
                f"Unset it to fall back to the bundled library, or add an index."
            )
        return []
    raw = json.loads(idx.read_text(encoding="utf-8"))
    out: list[TransitionEntry] = []
    for e in raw:
        entry = TransitionEntry(
            transition_id=e["transition_id"], file=e["file"],
            roles=list(e.get("roles", [])), aspect_ratio=e.get("aspect_ratio", "16:9"),
            blend=e.get("blend", "alpha"), key_color=e.get("key_color", "0x000000"),
            key_similarity=float(e.get("key_similarity", 0.15)),
            key_blend=float(e.get("key_blend", 0.10)))
        if not (d / entry.file).is_file():
            raise RenderError(f"transition '{entry.transition_id}': file '{entry.file}' missing in {d}")
        out.append(entry)
    return out


def _transition_seed(run_dir: Path, blueprint: dict) -> int:
    """Stable per-SOURCE seed: hash of source.txt so the same input file yields
    the same transition picks on any future run; a different file differs.
    Falls back to the blueprint's scene structure if source.txt is absent."""
    src = run_dir / "source.txt"
    if src.is_file():
        h = hashlib.sha256(src.read_bytes())
    else:
        skel = "|".join(f"{s.get('id')}:{s.get('broll_role')}" for s in blueprint.get("scenes", []))
        h = hashlib.sha256(skel.encode("utf-8"))
    return int.from_bytes(h.digest()[:8], "big")


def _transition_boundaries(blueprint: dict) -> list[tuple[int, str]]:
    """(boundary_index, role) placements from broll_role rules (§10):
      opening  -> after the scene
      closing  -> after the scene (== every topic boundary, incl. video end)
    Topic boundaries are marked by the closing B-Roll that ends each topic;
    there is no separate transition scene. Deduped, first role wins.
    boundary_index b means 'the cut after scene b'.

    Reads broll_role off the blueprint rather than looking at the video, which is
    why the director is required to emit a closing B-Roll at the end of every
    topic — that field is the only signal this function has about where topics
    end.

    b == n-1 is allowed on purpose: the last topic also ends with a closing
    B-Roll, and an overlay there straddles the end of the video, so only its first
    half is visible. That is intended (it plays as an outro sting), not an
    off-by-one.
    """
    scenes = blueprint.get("scenes", [])
    n = len(scenes)
    seen: dict[int, str] = {}

    def add(b: int, role: str):
        if 0 <= b <= n - 1 and b not in seen:      # b == n-1 -> end (tail-only) overlay
            seen[b] = role

    for i, sc in enumerate(scenes):
        if sc.get("broll_role") in ("opening", "closing"):
            add(i, sc["broll_role"])
    return sorted(seen.items())


def _select_transitions(placements: list[tuple[int, str]],
                        pool: list[TransitionEntry], seed: int
                        ) -> list[tuple[int, TransitionEntry]]:
    """Seeded-random pick per placement (reproducible for a given seed). Prefer
    overlays whose roles include the placement role; fall back to the full pool."""
    rng = _random.Random(seed)
    chosen: list[tuple[int, TransitionEntry]] = []
    for b, role in placements:
        matches = [e for e in pool if role in e.roles] or pool
        if matches:
            chosen.append((b, rng.choice(matches)))
    return chosen


def apply_transitions(run_dir: str | Path, blueprint: dict, merged_video: str | Path,
                      assets_dir: Path | None = None,
                      scenes: list[tuple[str, Path, dict]] | None = None) -> Path:
    """Overlay transparent transition clips ABOVE the merged video at role-driven
    cut points (§10). Each clip straddles its cut 50/50 (tail of outgoing + head
    of incoming). Returns the transitioned mp4, or the input unchanged when there
    are no assets/placements.

    ABOVE is the whole design. The overlay is composited on top of an already-
    continuous video, so the sequence is video --transition--> video, never
    video -> transition -> video. Nothing is inserted into the timeline, so the
    total duration is unchanged and every cue time computed elsewhere stays valid.
    Straddling the cut 50/50 (s = cut - d/2) is what makes the overlay read as
    covering the change rather than decorating one side of it.

    Returns the input path untouched when there are no assets or no placements,
    which is what lets render_merge_and_subs chain it unconditionally.

    Same per-clip filtergraph growth as _burn_boxed, though transition counts are
    small in practice.
    """
    run_dir = Path(run_dir)
    merged_video = Path(merged_video)
    pool = load_transition_index(assets_dir)
    placements = _transition_boundaries(blueprint)
    if not pool or not placements:
        return merged_video

    selected = _select_transitions(placements, pool, _transition_seed(run_dir, blueprint))
    if not selected:
        return merged_video

    # cumulative scene end times = cut times
    scenes = scenes if scenes is not None else scene_probes(run_dir, blueprint)
    prefix = [0.0]
    for d in _durations(scenes):
        prefix.append(prefix[-1] + d)
    total = prefix[-1]

    mp = probe(merged_video)
    W, H = mp["w"] or "1280", mp["h"] or "720"
    adir = Path(assets_dir) if assets_dir else transitions_dir()

    inputs: list[str] = ["-i", str(merged_video.resolve())]
    steps: list[str] = []
    base = "[0:v]"
    for j, (b, entry) in enumerate(selected, start=1):
        clip = (adir / entry.file).resolve()
        d = float(probe(clip)["duration"]) or 1.0
        cut = prefix[b + 1]                          # cut after scene b
        s = max(0.0, cut - d / 2.0)
        e = min(total, s + d)
        inputs += ["-i", str(clip)]
        # preserve/derive alpha: colorkey turns key_color transparent for
        # black-background (alpha-less) clips; alpha keeps the file's own channel.
        if entry.blend == "colorkey":
            pre = (f"scale={W}:{H},format=rgba,"
                   f"colorkey={entry.key_color}:{entry.key_similarity}:{entry.key_blend}")
        else:  # "alpha"
            pre = f"scale={W}:{H},format=rgba"
        steps.append(f"[{j}:v]{pre},setpts=PTS-STARTPTS+{s:.3f}/TB[ov{j}]")
        out = f"[b{j}]"
        steps.append(f"{base}[ov{j}]overlay=0:0:enable='between(t,{s:.3f},{e:.3f})':eof_action=pass{out}")
        base = out

    fc = ";".join(steps)
    out_mp4 = run_dir / "merged_trans.mp4"
    args = [*inputs, "-filter_complex", fc, "-map", base]
    if mp["has_audio"]:
        args += ["-map", "0:a", "-c:a", "copy"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_mp4.resolve())]
    _ffmpeg(args, expect=out_mp4, label="transition overlay")
    return out_mp4



# ================================================================== D7T1b opening title card
# NOTE: the opening-plate length lives in director.OPENING_LENGTH_S — it is a
# VEO-SIDE rule (enforce_opening_length runs before generation), so the renderer
# does not own it and no longer keeps a second copy.


@dataclass
class TitleStyle:
    """Opening title card: dim the opening B-Roll with a black scrim and put
    the video title in the middle of the frame."""
    scrim_opacity: float = 0.30      # 30% black overlay
    font_file: str | None = None
    font_size: int = 96              # large, poster-style
    font_hex: str = "FFFFFF"         # white fill
    outline_hex: str = "9CD3FF"      # light blue outline
    outline_w: int = 4               # outline thickness (px)
    fade_s: float = 0.5              # fade out at the end
    anim: str = "rise"               # "rise" | "drop" | "fade" | "wave"
    anim_s: float = 0.6              # entrance duration
    anim_travel: int = 70            # px the title travels during the entrance
    max_chars_per_line: int = 22     # wrap long titles (fewer chars — bigger font)
    wave_amp: int = 18
    wave_beat_s: float = 0.05
    wave_bounce_s: float = 0.35
    idle_amp: int = 5
    idle_period_s: float = 1.5
    line_gap: int = 16               # px between wrapped lines

# All pixel values in TitleStyle are authored for a frame whose short side is this.
TITLE_REF_SHORT_SIDE = 720



DEFAULT_TITLE_STYLE = TitleStyle()

REALISM_TITLE_STYLE = DEFAULT_TITLE_STYLE

# cartoon: NO dim, non-white fill + white outline, thicker border, bouncy entrance.
# font_file is resolved from __file__ via app/paths.py, so it holds regardless of
# the process working directory. It used to be the relative string
# "asset/fonts/..." and 502'd the whole render when the server was started from
# anywhere but the repo root.
CARTOON_TITLE_STYLE = TitleStyle(
    scrim_opacity=0.0,      # <- the acceptance criterion: no dim layer
    font_file=str(FONT_DIR / "Fredoka-SemiBold.ttf"),
    font_hex="FFD23F",      # non-white fill (bright yellow) — TUNABLE
    outline_hex="FFFFFF",   # white outline
    outline_w=6,            # thicker for pop
    font_size=104,
    anim="wave",
)

def scale_title_style(style: TitleStyle, width: int, height: int) -> TitleStyle:
    """Rescale every pixel measure of `style` to the real frame.

    The style is authored for a 720px short side; the H3 model renders at 480P, where
    an unscaled 96px title is a third larger relative to the frame than intended.
    Scaling by the short side keeps the proportions in portrait too."""
    k = min(width, height) / TITLE_REF_SHORT_SIDE
    px = lambda v: max(1, round(v * k))
    return replace(style, font_size=px(style.font_size), outline_w=px(style.outline_w),
                   anim_travel=px(style.anim_travel), wave_amp=px(style.wave_amp),
                   idle_amp=px(style.idle_amp), line_gap=px(style.line_gap))


def title_style_for(style_type: str) -> TitleStyle:
    """cartoon -> energetic no-dim treatment; realism/semi-realism -> current."""
    return CARTOON_TITLE_STYLE if style_type == "cartoon" else REALISM_TITLE_STYLE


def _wrap_title(title: str, width: int) -> str:
    """Greedy word wrap at `width` CHARACTERS (not pixels).

    Character counting is an approximation that holds because the title is capped
    at 80 chars by the schema and the font is known. A single word longer than
    `width` is never broken — it overflows its line rather than being hyphenated.
    Splits on whitespace, so it does not wrap scripts that don't use spaces.
    """
    words, lines, cur = title.split(), [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if len(cand) > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _opening_span(scenes: list[tuple[str, Path, dict]],
                  blueprint: dict) -> tuple[float, float] | None:
    """(start_s, end_s) of the opening B-Roll on the merged timeline, or None."""
    t = 0.0
    for (sid, path, pr), sc in zip(scenes, blueprint.get("scenes", [])):
        d = float(pr["duration"])
        if sc.get("broll_role") == "opening":
            return (t, t + d)
        t += d
    return None


def _drawtext_text_esc(s: str) -> str:
    """Escape a LITERAL string for drawtext text= (not a path)."""
    out = s.replace("\\", "\\\\")
    for ch in (":", "'", "%", ","):
        out = out.replace(ch, "\\" + ch)
    return out


def _wave_letter_drawtexts(title: str, style: TitleStyle, font_path: str,
                           W: int, H: int, s: float, e: float, alpha: str) -> str:
    """Change 2: cartoon title in-animation. One drawtext per visible letter,
    laid out with Pillow-measured advances (same idiom as _burn_boxed), each
    given a single delayed bounce so a wave travels left-to-right. Letters settle
    exactly on their resting line (sin(PI*1)=0) — the rest position Change 3's
    idle loop starts from. Beat index runs in reading order across wrapped lines;
    spaces consume a beat but draw nothing.
    NOTE: assumes a left-to-right, non-shaping script (Latin, etc.). Cartoon +
    a shaping script (Arabic/Indic) would mis-render per-letter — see review flag."""
    from PIL import ImageFont
    font = ImageFont.truetype(font_path, style.font_size)
    asc, desc = font.getmetrics()
    line_h = asc + desc
    line_gap = style.line_gap
    lines = _wrap_title(title, style.max_chars_per_line).split("\n")
    block_h = len(lines) * line_h + (len(lines) - 1) * line_gap
    top = (H - block_h) / 2.0
    dur = max(0.05, style.wave_bounce_s)
    fe = _drawtext_esc(font_path)

    parts: list[str] = []
    k = 0
    for li, line in enumerate(lines):
        start_x = (W - font.getlength(line)) / 2.0
        y_top = int(round(top + li * (line_h + line_gap)))
        for j, ch in enumerate(line):
            if ch != " ":
                x = int(round(start_x + font.getlength(line[:j])))
                bt = font.getbbox(ch)[1]        # ink-top offset -> shared baseline
                y0 = y_top + bt                  # rest position for THIS glyph
                dk = k * style.wave_beat_s
                t_settle = s + dk + dur
                y = (f"{y0}"
                     f"-{style.wave_amp}*sin(PI*clip((t-{s + dk:.3f})/{dur:.3f}\\,0\\,1))"
                     f"-{style.idle_amp}*sin(2*PI*max(t-{t_settle:.3f}\\,0)/{style.idle_period_s:.3f})")
                parts.append(
                    f"drawtext=fontfile='{fe}':text='{_drawtext_text_esc(ch)}':"
                    f"x={x}:y='{y}':fontsize={style.font_size}:"
                    f"fontcolor=0x{style.font_hex}:"
                    f"borderw={style.outline_w}:bordercolor=0x{style.outline_hex}:"
                    f"alpha='{alpha}':enable='between(t,{s:.3f},{e:.3f})'"
                )
            k += 1
    return ",".join(parts)

def apply_opening_title(run_dir: str | Path, blueprint: dict, video: str | Path,
                        style: TitleStyle | None = None,
                        scenes: list[tuple[str, Path, dict]] | None = None) -> Path:
    """Over the opening B-Roll: 30% black scrim + centred video title.

    Returns the input path untouched — no re-encode — in any of these cases:
      * the run's blueprint_type is not 'opening' (see has_opening_section);
      * the blueprint has no title;
      * the blueprint has no scene with broll_role == 'opening'.

    CORRECTION to the older wording here, which named 'middle' and 'closing' as
    the other cases: those blueprint types do not exist. schemas.BlueprintType is
    ('opening', 'summary'), and director.BLUEPRINT_TYPES agrees — anything
    unrecognised falls back to 'opening'. So in practice the only non-title run
    type today is 'summary'.

    The style is chosen from bp['style_type'] when the caller passes none, which is
    the one place style_type affects output: cartoon drops the scrim entirely and
    animates per letter, realism/semi-realism dim the plate and move the title as
    one block. Also mutes audio across the title span, so a B-Roll ambience bed
    doesn't play under the card.
    """
    run_dir, video = Path(run_dir), Path(video)
    if style is None:
        style = title_style_for(blueprint.get("style_type", "realism"))

    if not has_opening_section(run_dir, blueprint):
        return video

    title = (blueprint.get("title") or "").strip()
    scenes = scenes if scenes is not None else scene_probes(run_dir, blueprint)
    span = _opening_span(scenes, blueprint)
    if not title or span is None:
        return video

    s, e = span
    vp = probe(video)                    # one probe: frame size + audio test
    style = scale_title_style(style, int(vp["w"] or 1280), int(vp["h"] or 720))
    font = _title_font(style, read_language(run_dir))

    fin, fout = s, max(s, e - style.fade_s)
    a_in = max(0.05, style.anim_s)
    alpha = (f"if(lt(t\\,{fin + a_in:.3f})\\,(t-{fin:.3f})/{a_in:.3f}\\,"
             f"if(lt(t\\,{fout:.3f})\\,1\\,({e:.3f}-t)/{style.fade_s:.3f}))")

    scrim = (f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{style.scrim_opacity:.2f}:"
             f"t=fill:enable='between(t,{s:.3f},{e:.3f})'")

    if style.anim == "wave":
        # cartoon: per-letter travelling wave (Change 2). No _title.txt needed.
        W, H = int(vp["w"] or 1280), int(vp["h"] or 720)
        tf = None
        vf = f"{scrim},{_wave_letter_drawtexts(title, style, font, W, H, s, e, alpha)}"
    else:
        # realism / semi-realism: unchanged single-block title (rise/drop/fade).
        tf = run_dir / "_title.txt"
        tf.write_text(_wrap_title(title, style.max_chars_per_line), encoding="utf-8")
        centre_y = "(h-text_h)/2"
        prog = f"min(1\\,max(0\\,(t-{s:.3f})/{a_in:.3f}))"
        ease = f"(1-(1-{prog})*(1-{prog}))"
        if style.anim == "rise":
            y_expr = f"{centre_y}+{style.anim_travel}*(1-{ease})"
        elif style.anim == "drop":
            y_expr = f"{centre_y}-{style.anim_travel}*(1-{ease})"
        else:                                                  # "fade" / "none"
            y_expr = centre_y
        vf = (
            f"{scrim},"
            f"drawtext=fontfile='{_drawtext_esc(font)}':textfile='{tf.name}':reload=0:"
            f"x=(w-text_w)/2:text_align=center:y='{y_expr}':fontsize={style.font_size}:"
            f"fontcolor=0x{style.font_hex}:"
            f"borderw={style.outline_w}:bordercolor=0x{style.outline_hex}:"
            f"line_spacing={style.line_gap}:alpha='{alpha}':enable='between(t,{s:.3f},{e:.3f})'"
        )

    out = run_dir / "merged_title.mp4"
    args = ["-i", str(video.resolve()), "-vf", vf]
    if vp["has_audio"]:
        args += ["-af", f"volume=enable='between(t,{s:.3f},{e:.3f})':volume=0",
                 "-c:a", "aac", "-b:a", "192k"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out.resolve())]
    try:
        _ffmpeg(args, expect=out, cwd=run_dir, label="opening title")
    finally:
        if tf is not None:
            tf.unlink(missing_ok=True)
    return out
