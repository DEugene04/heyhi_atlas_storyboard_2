"""
app/schemas.py — Layer 1 request and blueprint schemas.

This file defines both pipeline contracts:

  • Blueprint models (VideoBlueprint, Scene, Storyboard, ...) describe
    blueprint.json, the artifact shared by every layer after the director.
    Changing them changes both what the director may emit and what existing
    blueprints validate against.

  • Request/response models (ConfigIn, DirectIn, ...) define the HTTP API
    exposed by main.py and are independent of stored blueprints.

Pydantic validates structure only. Cross-field rules (character counts,
references, B-Roll constraints, word budgets, etc.) are enforced separately by
director.validate_blueprint and patch_blueprint._check_resolved.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

class BackgroundRef(BaseModel):
    id: str = Field(..., description="short unique background id, e.g. 'bg_office_day'")
    prompt: str = Field(
        ...,
        description=(
            "narrative image-gen prompt for the empty environment plate "
            "(no characters). Wide establishing shot."
        ),
    )


class Storyboard(BaseModel):
    # REMOVED 2026-08-03: `model` (defaulted to "gemini-2.5-flash-image"). It was
    # never read — GeminiImageClient hard-codes its own model — so it looked like
    # a per-storyboard setting that did nothing. Blueprints on disk still carry
    # it; pydantic drops unknown keys, so old runs parse unchanged.
    id: str = Field(..., description="short unique storyboard id, e.g. 'sb_1'")
    prompt: str = Field(
        ...,
        description=(
            "2x2 multipanel storyboard prompt with per-panel timecodes "
            "[00:00-00:02] .. [00:06-00:08]"
        ),
    )
    reference: list[str] = Field(
        default_factory=list,
        description=(
            "placeholder char_ids (from scene.characters) + bg_ids used to "
            "compose this keyframe. char_ids are opaque tokens the user "
            "resolves to real library characters."
        ),
    )

class AudioMapping(BaseModel):
    """Voice assignment for one character in one scene.

    A character must keep the same audio_id throughout the video. This invariant is
    validated before and after placeholder resolution.
    """
    char_id: str = Field(..., description="placeholder char_id from scene.characters")
    audio_id: str = Field(
        ...,
        description="placeholder audio id the user resolves to a library voice",
    )

# For the front-end voice contract
class VoiceEntry(BaseModel):
    voice_id: str
    name: str
    preview_url: str | None = None
    languages: list[str] = Field(default_factory=list)  # verified_languages[].language, else labels.language, else ["unknown"]
    gender: str = "unknown"                              # labels.gender, else "unknown"


# ---------------------------------------------------------------- vocabularies
# Pipeline vocabularies. Adding a value usually requires matching downstream
# logic—not just updating the Literal.

SceneType = Literal["B-Roll", "video-with-audio"]

# Only two roles exist in code.
BRollRole = Literal["opening", "closing"]

Depth = Literal["standard", "deep dive"]

# Derived from the free-text style string. Used by the renderer for
# style-specific presentation.
StyleType = Literal["realism", "cartoon", "semi-realism"]

# Add objective literal (popular use cases)
AspectRatio = Literal["16:9", "9:16"]

# reference_from determines both task and model. scene_gen re-validates the
# pairing before generation.
VeoTask = Literal["image-to-video", "reference-to-video"]
VeoModel = Literal["Veo 3.1 Fast", "Veo 3.1 Lite"]

# Selects blueprint behavior. Currently only "opening" and "summary" are
# implemented.
# TODO: change3_short_animation.md specs a third value, "illustration", with its
# own director. It is NOT implemented — adding it here would let it through
# validation and then fail further down.
BlueprintType = Literal["opening", "summary"]

class Scene(BaseModel):
    """One Veo clip.

    The scenes list defines the final video timeline. Scene IDs must be filesystem-
    safe because they are reused as output filenames.
    """
    id: str = Field(..., description="short unique scene id, e.g. 's1'")
    task: VeoTask = Field(
        ...,
        description=(
            "'reference-to-video' when reference_from is 'storyboard:...', "
            "'image-to-video' when reference_from is a "
            "prior scene id. Enforced by validator."
        ),
    )
    model: VeoModel = Field(
        ...,
        description=(
            "'Veo 3.1 Fast' pairs with reference-to-video. 'Veo 3.1 Lite' "
            "pairs with image-to-video."
        ),
    )
    # Mentioning config.language here is intentional: response_schema passes Field
    # descriptions to Gemini, and explicitly naming the language improves adherence.See
    # docs/open-questions.md #28.
    text: str = Field(
        "",
        description=(
            "spoken narration for this scene, written in config.language "
            "(empty for B-Roll). This is what the viewer HEARS and reads as "
            "subtitles — unlike `prompt`, which stays in English because it "
            "instructs Veo."
        ),
    )
    # KNOWN GAP: patch_blueprint rewrites placeholder char_ids in `characters`,
    # `audios` and storyboard references — but NOT inside this prompt string. If
    # the director writes a placeholder id into the prompt text (it sometimes
    # does, e.g. "a medium shot of char_narrator"), that raw token is what Veo
    # receives. See docs/open-questions.md.
    prompt: str = Field(
        ...,
        description=(
            "Structured Veo prompt. MUST contain the labels: Cinematography, "
            "Subject, Action, Context, Style and Ambience, Ambient noise. "
            "Language emitted but not validator-enforced. Negative prompt "
            "intentionally omitted per AI RULE #1."
        ),
    )
    safety: str = Field("Allow (Adult only)")
    reference_from: str = Field(
        ...,
        description=(
            "one of: 'storyboard:<sb_id>', "
            "'scene:<earlier_scene_id>', or bare '<earlier_scene_id>'"
        ),
    )
    type: SceneType
    broll_role: BRollRole | None = None
    length: float | None = Field(
        None,
        description="seconds; REQUIRED for B-Roll, null for video-with-audio",
    )
    aspect_ratio: AspectRatio = "16:9"
    topic: str | None = Field(
        None,
        description="short topic label — REQUIRED for video-with-audio",
    )
    characters: list[str] = Field(
        default_factory=list,
        description=(
            "placeholder char_ids present in this scene (e.g. "
            "['char_teacher', 'char_student']). Semantic role-based ids."
        ),
    )
    audios: list[AudioMapping] = Field(
        default_factory=list,
        description=(
            "voice mapping for each char_id in this scene. Every char_id in "
            "'characters' MUST have a corresponding entry."
        ),
    )


class VideoBlueprint(BaseModel):
    """blueprint.json — schema v3. The one artifact the whole pipeline agrees on.

    Written twice per run, both times atomically:
      1. /pipeline/direct writes the director's output (placeholder char_ids).
      2. /pipeline/select snapshots that to blueprint.placeholder.json and
         rewrites blueprint.json with the user's real library ids.
    Everything downstream reads the resolved version.

    """
    title: str = Field(..., min_length=1, max_length=80,
                       description="short on-screen video title, in config.language")
    style: str
    style_type: StyleType = "realism"
    character_count: int = Field(
        ...,
        ge=0,
        description=(
            "distinct character count. Must equal the number of unique "
            "char_ids appearing across all scenes."
        ),
    )
    music_style: str = Field(
        ...,
        description=(
            "one-line description of the video's music track (genre + mood + "
            "instrumentation). Consumed by the music generation layer."
        ),
    )
    backgrounds: list[BackgroundRef]
    scenes: list[Scene]
    storyboard: list[Storyboard]

class ConfigIn(BaseModel):
    # extra="forbid": unknown/typo'd fields -> 422 instead of silent drop
    model_config = ConfigDict(extra="forbid")
    blueprint_type: BlueprintType = "opening"
    language: str = Field(..., min_length=1)
    audience: str = Field(..., min_length=1)
    depth: Depth = "standard"
    style: str = Field(..., min_length=1)
    objective: str = Field(..., min_length=1)

    # Locked to 16:9. Portrait is not supported end-to-end yet, so accepting 9:16
    # would produce inconsistent assets. See
    # docs/open-questions.md #13.
    aspect_ratio: Literal["16:9"] = "16:9"


class ConfigOut(BaseModel):
    run_id: str
    config_path: str


class UploadOut(BaseModel):
    run_id: str
    source_path: str
    bytes: int

class DirectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    force: bool = False                               # overwrite an existing blueprint, for regeneration purposes
    max_retries: int = Field(2, ge=0, le=5)
    # Pass VideoBlueprint as Gemini's response_schema to guarantee valid JSON and
    # enum values. Semantic validation still happens afterward.
    # See docs/open-questions.md #25.
    use_schema: bool = True
 
class DirectOut(BaseModel):
    run_id: str
    blueprint_path: str
    character_count: int
    scenes: int
    regenerated: bool

class SelectionItem(BaseModel):
    # extra="forbid": typo'd keys -> 422
    model_config = ConfigDict(extra="forbid")
    placeholder_id: str = Field(..., min_length=1)   # blueprint scene.characters id, e.g. 'char_1'
    char_id: str = Field(..., min_length=1)          # id in character_library/index.json
    audio_id: str = Field(..., min_length=1)         # id in audio_library/index.json
 
 
class SelectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    voice_source: Literal["index", "elevenlabs"] = "elevenlabs"
    selections: list[SelectionItem] = Field(..., min_length=1)
 

class SelectOut(BaseModel):
    run_id: str
    selections_path: str
    blueprint_path: str
    count: int

# Billable stages support two mutually exclusive modes:
#
#   force  -> regenerate everything
#   resume -> generate only missing artifacts
#
# Neither flag returns 409 if output already exists, preventing accidental
# reruns.
class AssetsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    force: bool = False                               # regenerate even if assets exist
    resume: bool = False                              # generate only missing PNGs

class AssetsOut(BaseModel):
    run_id: str
    assets_dir: str
    backgrounds: int
    storyboard: int
    reused: int = 0                                   # existing PNGs left untouched

class ScenesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    force: bool = False
    resume: bool = False          # keep clips already in veo_raw/; generate the rest
    tts_only: bool = False        # reuse veo_raw/ clips; re-run L6 only (no Veo)
    tts_provider: Literal["azure", "elevenlabs"] = "elevenlabs"
    video_provider: Literal["minimax_h3", "gemini"] = "minimax_h3"   # gemini = legacy Veo
    # H3 only. File names inside the shared _storage/image_library and video_library.
    background: str = ""
    reference_video: str = ""
    seed: int | None = Field(None, ge=0, lt=2**31)
    turbo: bool | None = None     # H3 only: True = turbo LoRA (4 steps); None = ATLAS_H3_TURBO default
    # Provides word timings for subtitles. Gemini is billed per scene; Whisper runs
    # locally but requires the optional dependency. See docs/open-questions.md #16.
    diarizer: Literal["gemini", "whisper"] = "gemini"

class ScenesOut(BaseModel):
    run_id: str
    scenes_dir: str
    scenes: int
    total_cost_usd: float
    reused: int = 0               # scenes whose Veo call was skipped (resume)

class MusicIn(BaseModel):
    run_id: str
    duration_s: float | None = None   # hint only; Lyria 3 Clip fixes ~30s
    instrumental: bool = True
    force: bool = False               # overwrite an existing track.mp3
    resume: bool = False              # keep an existing track.mp3, generate if absent
 
 
class MusicOut(BaseModel):
    run_id: str
    music_path: str
    music_style: str
    regenerated: bool
    reused: bool = False              # resume found an existing track and kept it

class RenderIn(BaseModel):
    run_id: str
    force: bool = False   # overwrite an existing final.mp4
   # Subtitle renderer. "boxed" is the default PNG-overlay implementation. "ass"
    # requires libass and renders from a single subtitle file.
    # See docs/open-questions.md #23.
    subtitle_method: Literal["boxed", "ass", "libass", "drawtext", "auto"] = "boxed"
 
 
class RenderOut(BaseModel):
    run_id: str
    scenes: int
    merged_path: str
    srt_path: str
    merged_subs_path: str
    final_path: str                    
    language: str                      # config.language the subtitles render in
    music: bool                        # was music_bg/track.mp3 mixed in
    font_warning: str | None = None    # set when the language needs a missing script font
