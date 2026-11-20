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
    FfmpegError, extract_last_frame, get_duration, trim_video,
)
from app.clients.video_veo_client import (
    VeoClient, VeoError, VeoJobSpec, generate_with_retry,
)
from app.config import DirectorConfig
from app.pipeline import external_tts
from app.pipeline.director import ROUTE_BY_PREFIX, parse_reference


# Expected Veo routes for each reference type.
_FAST = ROUTE_BY_PREFIX["storyboard"]
_LITE = ROUTE_BY_PREFIX["scene"]

# B-Roll is generated at >=4s. Speaking scenes are sized to their dialogue and
# never exceed VEO_VIDEO_SECONDS (the director caps text to fit it).
VEO_BROLL_SECONDS = 4.0
VEO_VIDEO_SECONDS = 8.0
# Headroom over the pure reading time: speech onset plus a short tail. Every spare
# second is a second the model can fill with babble, so keep it small. It must stay
# large enough that the clip outlasts the TTS, since L6 can only trim, not extend.
SPEECH_HEADROOM_S = 1.0
# Appended after the spoken line so the model does not keep "speaking" into spare time.
SILENT_AFTER_LINE = " Once the line is finished the character stays silent with their mouth closed."


class VeoRouteError(VeoError):
    pass


def _expected_route(reference_from: str) -> tuple[str, str] | None:
    """Return the expected (task, model) for a reference type."""
    prefix, _, _ = parse_reference(reference_from)
    return ROUTE_BY_PREFIX.get(prefix or "")

# Based on the reference_from, which veo task do we use
# Storyboard -> reference-to-image
# Background & previous scene -> image-to-video
def route(scene: dict) -> tuple[str, str]:
    """Validate and return the scene's (task, model) route."""
    task, model = scene.get("task"), scene.get("model")
    # Validate the task and model
    pair = (task, model)
    if pair not in (_FAST, _LITE):
        raise VeoRouteError(f"scene '{scene.get('id')}': incoherent task/model {pair}")
    exp = _expected_route(scene.get("reference_from", ""))
    if exp is None:
        raise VeoRouteError(
            f"scene '{scene.get('id')}': ambiguous reference_from "
            f"'{scene.get('reference_from')}' — use 'storyboard:<sb_id>', "
            f"'background:<bg_id>' or 'scene:<scene_id>'"
        )
    if pair != exp:
        raise VeoRouteError(
            f"scene '{scene.get('id')}': task/model {pair} does not match "
            f"reference_from '{scene.get('reference_from')}' (expected {exp})"
        )
    return pair

# Breakdown reference_from into the type and corresponding id
def _ref_id(reference_from: str) -> tuple[str, str]:
    """Return (reference_type, reference_id) from `reference_from`."""
    prefix, rid, _ = parse_reference(reference_from)
    return (prefix or "scene"), rid

# Create the Veo prompt so we can evaluate it, for the time being
def compose_veo_prompt(scene: dict, background_picture: int | None = None) -> str:
    """Build the final prompt sent to Veo / H3.

    `background_picture` is the 1-based position of the shared-library background
    among the request's reference images; H3 addresses references as `<Picture N>`.
    Speaking scenes append the dialogue so the model generates matching mouth movement.
    """
    prompt = scene["prompt"]
    if background_picture:
        prompt = (f"{prompt} The environment matches <Picture {background_picture}>, "
                  "in the same colors, lighting and layout.")
    if scene.get("type") == "video-with-audio":
        text = (scene.get("text") or "").strip()
        if text:
            prompt = f'{prompt} Audio sync: The character says "{text}"{SILENT_AFTER_LINE}'
    return prompt

def veo_generation_seconds(scene: dict) -> float:
    """Length asked of Veo (NOT the blueprint length). Pure — no assets needed.
    B-Roll: 4s floor (ceil(length) if longer), trimmed to blueprint length after.
    Video-with-audio: just long enough for the dialogue (words / pace + headroom,
    4s floor, 8s ceiling), so the model has no spare seconds to babble in; L6 then
    trims to the last TTS end."""
    if scene.get("type") == "B-Roll":
        length = scene.get("length")
        return (VEO_BROLL_SECONDS if not length or length <= VEO_BROLL_SECONDS
                else float(math.ceil(length)))
    words = len((scene.get("text") or "").split())
    need = words / DirectorConfig.words_per_second + SPEECH_HEADROOM_S
    return float(min(VEO_VIDEO_SECONDS, max(VEO_BROLL_SECONDS, math.ceil(need))))

# Convert scene from blueprint into mp4
class SceneGenerator:
    def __init__(self, run_dir: str | Path, client: VeoClient,
                 max_retries: int = 3, backoff_s: float = 2.0,
                 background: Path | None = None, reference_video: Path | None = None,
                 seed: int | None = None, turbo: bool | None = None):
        self.run = Path(run_dir)
        # Shared-library extras (H3). Applied to reference-to-video scenes only:
        # image-to-video scenes already carry a first frame, which H3 cannot combine
        # with reference images or video.
        self.background = background
        self.reference_video = reference_video
        self.seed = seed
        self.turbo = turbo
        self.client = client
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.scenes_dir = self.run / "scenes"
        self.raw_dir = self.run / "veo_raw"     # pristine Veo clips (enables TTS re-render)
        self.reused: list[Path] = []            # scenes run_all(resume=True) skipped

    # What image should Veo use as input for this scene
    def _resolve_input_paths(self, scene: dict) -> tuple[Path | None, list[Path]]:
        """Resolve the image seed and reference images for a scene."""
        kind, rid = _ref_id(scene.get("reference_from", ""))
        if kind == "storyboard":
            p = self.run / "storyboard" / f"{rid}.png"
            if not p.is_file():
                raise VeoError(f"scene '{scene['id']}': storyboard '{rid}.png' missing")
            return None, [p]                              # reference-to-video
        if kind == "background":
            p = self.run / "backgrounds" / f"{rid}.png"
            if not p.is_file():
                raise VeoError(f"scene '{scene['id']}': background '{rid}.png' missing")
            return p, []                                  # image-to-video seed
        # Seed from the last frame of the previous processed scene.
        prev = self.scenes_dir / f"{rid}.mp4"
        if not prev.is_file():
            raise VeoError(f"scene '{scene['id']}': previous scene '{rid}.mp4' missing")
        seed = self.scenes_dir / f"_lastframe_{rid}.png"
        extract_last_frame(prev, seed)
        return seed, []

    def build_spec(self, scene: dict) -> VeoJobSpec:
        """Build the Veo request"""
        task, model = route(scene)
        image_path, ref_paths = self._resolve_input_paths(scene)
        image = image_path.read_bytes() if image_path else None
        references = [p.read_bytes() for p in ref_paths]
        is_ref = task == "reference-to-video"
        background_picture = None
        if is_ref and self.background:
            references.append(self.background.read_bytes())
            background_picture = len(references)
        # GENERATION length sent to Veo (not the blueprint length)
        gen_seconds = veo_generation_seconds(scene)
        return VeoJobSpec(
            scene_id=scene["id"], task=task, model=model,
            prompt=compose_veo_prompt(scene, background_picture),           # appends spoken text for video-with-audio
            aspect_ratio=scene.get("aspect_ratio", "16:9"),
            duration_s=gen_seconds, image=image, references=references,
            safety=scene.get("safety", "Allow (Adult only)"),
            reference_video=self.reference_video if is_ref else None,
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
        (self.raw_dir / f"{scene['id']}.mp4").write_bytes(mp4)

        dest = self.scenes_dir / f"{scene['id']}.mp4"
        dest.write_bytes(mp4)

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
            self.generate_scene(scene)                        # L5 (+ pristine veo_raw)
            if scene.get("type") == "video-with-audio":
                audio.process(raw, scene, voice_for(scene), self.run)  # L6 -> scenes/<id>.mp4
            out.append(self.scenes_dir / f"{sid}.mp4")
        return out

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