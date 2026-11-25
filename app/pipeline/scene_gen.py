"""
app/pipeline/scene_gen.py — Layer 5 (Veo scene generation).

Generates scene videos from the blueprint by resolving the reference image,
calling Veo, and saving both the raw clip (`veo_raw/`) and working copy
(`scenes/`).

Video-with-audio scenes continue through L6 for audio replacement.
B-Roll scenes are finished here.
"""
from __future__ import annotations

import math, shutil
from pathlib import Path

from app import costs
from app.utils.ffmpeg_utils import (
    FfmpegError, extract_last_frame, get_duration, strip_audio, trim_video,
)
from app.clients.video_veo_client import (
    VeoClient, VeoError, VeoJobSpec, generate_with_retry,
)
from app.config import DirectorConfig
from app.pipeline import external_tts
from app.pipeline.director import (
    ROUTE, ROUTE_BY_PREFIX, SPEECH_MIN_SECONDS, parse_reference, speaking_seconds,
)

# B-Roll is generated at >=4s. Speaking scenes are sized to their dialogue and
# never exceed VEO_VIDEO_SECONDS (the director caps text to fit it).
VEO_BROLL_SECONDS = 4.0
VEO_VIDEO_SECONDS = 8.0
# Appended after the spoken line so the model does not keep "speaking" into spare time.
SILENT_AFTER_LINE = " Once the line is finished the character stays silent with their mouth closed."


class VeoRouteError(VeoError):
    pass


def _expected_route(reference_from: str) -> tuple[str, str] | None:
    """Return the expected (task, model) for a reference type."""
    prefix, _, _ = parse_reference(reference_from)
    return ROUTE_BY_PREFIX.get(prefix or "")

# Based on the reference_from, which veo task do we use
# Every scene is image-to-video; reference_from only picks the seed image
# (start frame, background, or the previous scene's last frame).
def route(scene: dict) -> tuple[str, str]:
    """Validate and return the scene's (task, model) route."""
    pair = (scene.get("task"), scene.get("model"))
    if pair != ROUTE:
        raise VeoRouteError(
            f"scene '{scene.get('id')}': task/model {pair} is not {ROUTE} — "
            "every scene is image-to-video")
    if _expected_route(scene.get("reference_from", "")) is None:
        raise VeoRouteError(
            f"scene '{scene.get('id')}': ambiguous reference_from "
            f"'{scene.get('reference_from')}' — use 'start_frame:<sf_id>', "
            f"'background:<bg_id>' or 'scene:<scene_id>'"
        )
    return pair

# Breakdown reference_from into the type and corresponding id
def _ref_id(reference_from: str) -> tuple[str, str]:
    """Return (reference_type, reference_id) from `reference_from`."""
    prefix, rid, _ = parse_reference(reference_from)
    return (prefix or "scene"), rid

# Create the Veo prompt so we can evaluate it, for the time being
def compose_veo_prompt(scene: dict) -> str:
    """Build the final prompt sent to Veo / H3.

    Speaking scenes append the dialogue so the model generates matching mouth movement.
    """
    prompt = scene["prompt"]
    if scene.get("type") == "video-with-audio":
        text = (scene.get("text") or "").strip()
        if text:
            prompt = f'{prompt} Audio sync: The character says "{text}"{SILENT_AFTER_LINE}'
    return prompt

def veo_generation_seconds(scene: dict) -> float:
    """Length asked of Veo (NOT the blueprint length). Pure — no assets needed.
    B-Roll: 4s floor (ceil(length) if longer), trimmed to blueprint length after.
    Video-with-audio: the scene's own `length` (words / pace, rounded down, 4s floor;
    see director.fill_speaking_lengths), 8s ceiling, so the model has no spare
    seconds to babble in; L6 then trims to the last TTS end."""
    if scene.get("type") == "B-Roll":
        length = scene.get("length")
        return (VEO_BROLL_SECONDS if not length or length <= VEO_BROLL_SECONDS
                else float(math.ceil(length)))
    length = scene.get("length")
    if not length:
        length = speaking_seconds(scene.get("text") or "", DirectorConfig.words_per_second)
    return float(min(VEO_VIDEO_SECONDS, max(SPEECH_MIN_SECONDS, length)))

def scene_ready(run_dir: str | Path, scene: dict) -> bool:
    """A scene is finished when its final clip exists. A speaking scene's working copy
    lands in scenes/ before the voice is laid on, so only L6's marker counts there."""
    run_dir = Path(run_dir)
    if scene.get("type") == "video-with-audio":
        return external_tts.scene_is_done(run_dir, scene["id"])
    return (run_dir / "scenes" / f"{scene['id']}.mp4").is_file()


def continues_from(scene: dict) -> str | None:
    """Id of the earlier scene whose last frame seeds this one, or None."""
    kind, rid = _ref_id(scene.get("reference_from", ""))
    return rid if kind == "scene" else None


def continuations_of(blueprint: dict, scene_id: str) -> list[str]:
    """Ids of every scene that continues, directly or through a chain, from `scene_id`
    - the scenes that go stale when `scene_id` is regenerated."""
    stale: set[str] = {scene_id}
    out: list[str] = []
    for sc in blueprint.get("scenes", []):
        if continues_from(sc) in stale:
            stale.add(sc["id"])
            out.append(sc["id"])
    return out


# Convert scene from blueprint into mp4
class SceneGenerator:
    def __init__(self, run_dir: str | Path, client: VeoClient,
                 max_retries: int = 3, backoff_s: float = 2.0,
                 seed: int | None = None, turbo: bool | None = None):
        self.run = Path(run_dir)
        self.seed = seed
        self.turbo = turbo
        self.client = client
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.scenes_dir = self.run / "scenes"
        self.raw_dir = self.run / "veo_raw"     # pristine Veo clips (enables TTS re-render)
        self.reused: list[Path] = []            # scenes run_all(resume=True) skipped

    # What image should Veo use as input for this scene
    def _resolve_seed_image(self, scene: dict) -> Path:
        """Resolve the image the clip starts from."""
        kind, rid = _ref_id(scene.get("reference_from", ""))
        if kind in ("start_frame", "background"):
            folder = "start_frame" if kind == "start_frame" else "backgrounds"
            p = self.run / folder / f"{rid}.png"
            if not p.is_file():
                raise VeoError(f"scene '{scene['id']}': {kind} '{rid}.png' missing")
            return p
        # Seed from the last frame of the previous processed scene.
        prev = self.scenes_dir / f"{rid}.mp4"
        if not prev.is_file():
            raise VeoError(f"scene '{scene['id']}': previous scene '{rid}.mp4' missing")
        seed = self.scenes_dir / f"_lastframe_{rid}.png"
        extract_last_frame(prev, seed)
        return seed

    def build_spec(self, scene: dict) -> VeoJobSpec:
        """Build the Veo request"""
        task, model = route(scene)
        # GENERATION length sent to Veo (not the blueprint length)
        gen_seconds = veo_generation_seconds(scene)
        return VeoJobSpec(
            scene_id=scene["id"], task=task, model=model,
            prompt=compose_veo_prompt(scene),           # appends spoken text for video-with-audio
            aspect_ratio=scene.get("aspect_ratio", "16:9"),
            duration_s=gen_seconds, image=self._resolve_seed_image(scene).read_bytes(),
            safety=scene.get("safety", "Allow (Adult only)"),
            seed=self.seed, turbo=self.turbo,
        )

    def generate_scene(self, scene: dict) -> Path:
        """Generate a single scene.

        Writes the original Veo clip to `veo_raw/` and a working copy to `scenes/`.
        B-Roll scenes are trimmed here; speaking scenes continue through L6.

        Records generation cost before returning the output path.
        """
        spec = self.build_spec(scene)
        mp4, attempts = generate_with_retry(
            self.client, spec, max_retries=self.max_retries, backoff_s=self.backoff_s
        )
        self.scenes_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = self.raw_dir / f"{scene['id']}.mp4"
        raw_path.write_bytes(mp4)
        if scene.get("type") == "B-Roll":
            # B-Roll is silent so the model's ambience never clashes with the
            # background music; the raw copy is silent too, so every later copy
            # (resume, re-render) is.
            strip_audio(raw_path)

        dest = self.scenes_dir / f"{scene['id']}.mp4"
        shutil.copyfile(raw_path, dest)

        if scene.get("type") == "B-Roll":
            length = scene.get("length")
            if length and 0 < float(length) < float(spec.duration_s or 0):
                trim_video(dest, float(length))

        # Billed per second of GENERATED video, measured off the pristine clip rather
        # than spec.duration_s, so the number is what the provider actually returned
        # (Veo snaps to 4/6/8s, and a B-Roll asking for 1s is generated, and billed,
        # at the 4s floor). Measuring the raw file also survives the trim above.
        raw = self.raw_dir / f"{scene['id']}.mp4"
        try:
            gen_dur, measured = get_duration(raw), True
        except FfmpegError:
            gen_dur, measured = float(spec.duration_s or 0.0), False
        extra: dict = {"scene_id": scene["id"], "task": spec.task, "attempts": attempts}
        if spec.turbo is not None:
            extra["turbo"] = spec.turbo
        # A client may name its own billing identity; the default is Gemini, whose
        # rate is keyed by the Veo model. The company GPU has no per-call charge, so
        # its seconds are logged for information only (priced=False) and do not make
        # the run total look like an undercount.
        if hasattr(self.client, "cost_provider"):
            extra["priced"] = False
        costs.record(self.run, layer="L5",
                     provider=getattr(self.client, "cost_provider", "gemini"),
                     model=getattr(self.client, "cost_model", spec.model),
                     unit="second", quantity=gen_dur, measured=measured, **extra)
        return dest

    def _finish_from_raw(self, scene: dict, voice_for, audio) -> Path:
        """Rebuild a scene from an existing raw Veo clip.

        Speaking scenes run through L6. B-Roll is copied and trimmed.
        """
        sid = scene["id"]
        raw = self.raw_dir / f"{sid}.mp4"
        if scene.get("type") == "video-with-audio":
            return audio.process(raw, scene, voice_for(scene), self.run)
        dest = self.scenes_dir / f"{sid}.mp4"
        shutil.copyfile(raw, dest)
        length = scene.get("length")
        if length and 0 < float(length) < veo_generation_seconds(scene):
            trim_video(dest, float(length))
        return dest

    def run_all(self, blueprint: dict, voice_for, audio, resume: bool = False) -> list[Path]:
        """Generate every scene in order.

        Speaking scenes continue through L6 before the next scene starts so
        `scene:<prev>` references the final trimmed output.

        With resume=True:
        - missing raw clip -> regenerate
        - completed scene -> skip
        - raw clip only -> rerun L6 only

        Returns the generated scene paths."""
        out: list[Path] = []
        self.reused = []
        for scene in blueprint.get("scenes", []):
            sid = scene["id"]
            raw = self.raw_dir / f"{sid}.mp4"
            if resume and raw.is_file():
                self.reused.append(raw)                       # Veo call skipped
                # Re-running the audio half is cheap and idempotent, so the only
                # thing the marker buys us is skipping it — never correctness.
                if not external_tts.scene_is_done(self.run, sid):
                    self._finish_from_raw(scene, voice_for, audio)
                out.append(self.scenes_dir / f"{sid}.mp4")
                continue
            out.append(self.run_one(scene, voice_for, audio))
        return out

    def run_one(self, scene: dict, voice_for, audio) -> Path:
        """Generate one scene end to end: L5, then L6 for a speaking scene.

        Always calls the video model - this is the single place a scene is paid for,
        used by run_all and by regenerating one scene on its own."""
        self.generate_scene(scene)                        # L5 (+ pristine veo_raw)
        if scene.get("type") == "video-with-audio":
            audio.process(self.raw_dir / f"{scene['id']}.mp4", scene,
                          voice_for(scene), self.run)     # L6 -> scenes/<id>.mp4
        return self.scenes_dir / f"{scene['id']}.mp4"

    def rerender_tts(self, blueprint: dict, voice_for, audio) -> list[Path]:
        """Re-run ONLY L6 from the preserved veo_raw/ clips. No Veo call, no
        paid video generation. Requires a prior run_all that saved veo_raw/."""
        self.scenes_dir.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for scene in blueprint.get("scenes", []):
            sid = scene["id"]
            if not (self.raw_dir / f"{sid}.mp4").is_file():
                raise VeoError(f"no raw Veo clip for '{sid}' — run /pipeline/scenes (with Veo) first")
            # unconditional: this endpoint's whole purpose is to redo the audio,
            # so an existing tts/<id>.done marker is ignored here
            self._finish_from_raw(scene, voice_for, audio)
            out.append(self.scenes_dir / f"{sid}.mp4")
        return out