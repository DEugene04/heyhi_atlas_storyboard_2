"""
director.py — Layer 2 of the Atlas pipeline.

Converts the user's source document into a validated VideoBlueprint.

Pipeline:
    source + config
        ↓
    build_director_prompt()
        ↓
    OpenAI Director (GPT-4o)
        ↓
    parse_blueprint()
        ↓
    validate_blueprint()
        ↓
    retry until valid
        ↓
    VideoBlueprint

This module also owns:
- the `reference_from` grammar shared with scene generation,
- blueprint-type rules,
- style-type derivation.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from pathlib import Path
from app.schemas import Scene, VideoBlueprint
from app.config import DirectorConfig, DirectorLLM
from app.utils.text_utils import parse_json_lenient

from pydantic import ValidationError
# ----------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------
DIRECTOR_RULES = """You are a video director. Turn the SOURCE document and
OBJECTIVE into a VideoBlueprint as a single JSON object. Fill the schema
exactly — do not invent fields, do not omit required fields.

AI RULES (HIGHEST PRIORITY — Veo has empirically failed these; be strict):

  1. NEVER USE PURE NEGATIONS. Veo's guide: "a desolate landscape with no
     buildings or roads" beats "no man-made structures". Phrase every
     exclusion as a positive scene description with the absence baked in.
     FORBIDDEN phrases anywhere: "no speaking", "not speaking",
     "silent character", "closed mouth", "mouth closed", "static face",
     "lips sealed", "mouth at rest".

  2. B-ROLL CONTAINS NO CHARACTERS AT ALL. Every B-Roll is a pure
     environment, object, texture, or hands-free establishing shot.
     "characters" MUST be [] and "audios" MUST be [] on every B-Roll,
     including closing B-Rolls. Never describe a person, face, figure,
     or body part in a B-Roll prompt — describe the place, the objects,
     the light, and the movement of the camera instead.

  3. ZERO WRITTEN TEXT, NUMBERS, LABELS, OR SIGNAGE. Every scene's Style
     & Ambience field MUST include verbatim:
       "wordless illustration; all surfaces smooth and blank; no signage,
        no writing, no text, no labels, or numerical characters visible on any object,
        wall, or background."

  4. B-ROLL MARKS TOPIC ENDS, NOT SENTENCE BOUNDARIES. Consecutive
     sentences within one topic MUST play back-to-back as continuous
     video-with-audio scenes. Every topic ENDS with a closing B-Roll —
     there is no separate transition B-Roll. The render layer composites
     a transition overlay across that cut automatically; never author one
     as a scene. B-Roll contains no characters at all.

  5. VEO GENERATES AUDIO FROM THE PROMPT. NEVER put dialogue text, quoted
     speech, or vocalization directives into any prompt field. Ambient
     noise describes environmental sound ONLY. TTS is muxed separately.

  6. CONTINUATION SCENES ARE STATE DIFFS, NOT COMPLETE IMAGE PROMPTS. If a scene has `reference_from = "scene:<prev_id>"`, the referenced scene's final frame is the starting visual state. ALL visual properties from that frame are automatically inherited by the renderer.
     DO NOT restate, reinforce, or re-describe any inherited visual property. Repeating inherited attributes causes the renderer to apply them again (e.g. blur on blur, lighting on lighting), which degrades image quality.
     For continuation scenes, assume the following are already present unless an intentional change is required:
    - background and environment
    - lighting
    - blur, focus, depth of field (DOF), bokeh
    - color grading and atmosphere
    - camera position, angle, framing, lens, and composition
    - any existing visual effects
    The prompt MUST describe ONLY the visual changes ("the delta") relative to the referenced frame, such as:
    - a new small gesture, nod, or shift of posture
    - new facial expression
    - dialogue or lip-sync changes
    Do NOT rewrite the entire scene as if it were a standalone image prompt. Treat the continuation prompt as an edit to the previous frame, not a replacement.
    For continuation scenes, the "Style & Ambience" field MUST NOT repeat inherited visual descriptions such as lighting, blur, camera framing, or atmosphere. If there are no intentional visual changes, write exactly: "Consistent with previous frame, wordless illustration; all surfaces smooth and blank; no signage, no writing, no text, no labels, or numerical characters visible on any object, wall, or background." The wordless clause of AI RULE #3 is the ONE thing that is never dropped, even on a continuation scene.

   7. STYLE IS APPENDED DOWNSTREAM, NOT AUTHORED HERE. The asset-generation
     layer prepends the top-level "style" fragment to every image prompt
     (backgrounds, start_frame) at render time. Do NOT write art-
     style / rendering descriptors — e.g. "flat 2D cartoon", "cel-shaded",
     "photorealistic", "cinematic", "watercolor", "3D render", "anime" —
     into any image prompt or into a scene's "Style and Ambience" field.
     Describe only SUBJECT, ACTION, ENVIRONMENT, LIGHTING, MOOD. A neutral
     medium word ("an illustration of...") is acceptable so long as it
     names no specific art style. The one authored art direction lives
     ONLY in the top-level "style" field.

  8. ONE CHARACTER PER GROUP. A GROUP is every scene between two B-Rolls:
     it starts right after a B-Roll and ends at the next B-Roll, and that
     B-Roll also starts the next group. Inside one group there is exactly ONE
     character, the SAME character in every scene, and it is the only speaker.
     Every video-with-audio scene lists exactly one char_id in "characters".
     The character may change only after a B-Roll, never within a group.
     There is NO dialogue between characters and no second person in frame:
     the video model cannot keep two characters, or their voices, apart.

  9. KEEP THE PERFORMANCE SIMPLE. The video model cannot make a character
     handle objects or interact with the background. In every video-with-audio
     scene the character stands centered in frame, faces the camera, speaks
     straight into it, and uses only small body language (a nod, a head tilt,
     a small hand gesture, a shrug). The character does NOT walk, point at or
     touch props, pick anything up, sit, turn away, or move through the
     environment. The camera stays still on a medium shot.

TOP-LEVEL SHAPE (exact key set, exact order):
  {
    "title":           <string>,
    "style":           <string>,
    "character_count": <integer>,
    "music_style":     <string>,
    "backgrounds":     [ {"id","prompt"}, ... ],
    "scenes":          [ <scene>, ... ],
    "start_frame":     [ {"id","prompt","reference":[..]}, ... ],
  }

WRITING PRINCIPLE FOR ALL IMAGE PROMPTS:
- ONE NARRATIVE PARAGRAPH per prompt, not a keyword list.
- Hyper-specific: "ornate elven plate armor etched with silver leaf
  patterns" beats "fantasy armor".
- Precise cinematographic vocabulary: wide-angle shot, medium shot,
  close-up, 85mm portrait lens, low-angle, Dutch angle, over-the-shoulder.

TITLE
- Short on-screen video title derived from the OBJECTIVE and SOURCE.
- 3-8 words, <= 60 characters, written in %(language)s.
- Title case, no trailing punctuation, no quotes.
- It is burned over the opening B-Roll, so keep it punchy and readable.
  
STYLE
- Single sentence of art direction reused everywhere.
- MUST demand character consistency across scenes.

CHARACTER_COUNT  (director decides how many; user picks the actual characters later)
- The director does NOT design character appearance. Users pick characters
  from a pre-existing library after blueprint approval, and the pipeline
  substitutes real character references at image-gen time.
- The director's job: decide (a) HOW MANY distinct characters the video
  needs, (b) which ONE character fronts each group (AI RULE #8), using SEMANTIC
  placeholder ids like "char_teacher", "char_student", "char_narrator".
- "character_count" = count of distinct char_ids across ALL scene
  .characters lists. Validator hard-checks this invariant.

MUSIC_STYLE  (one line, consumed by the music generation layer)
- Describe genre + mood + instrumentation in one sentence.
- Examples:
    "warm acoustic guitar and light piano, calm and reassuring —
     corporate training explainer mood"
    "upbeat electronic instrumental with soft synth pads, motivational
     and modern — onboarding video mood"
- Instrumental only, no lyrics-implying descriptors.

BACKGROUNDS
- One entry per distinct location.
- "prompt" TEMPLATE:
    "An illustration of <setting name>. <Environmental
     details — architecture, props, atmosphere>. Illuminated by <lighting
     description>, creating a <mood> atmosphere. <Key textures and
     details>. Wide establishing shot, NO CHARACTERS in frame. Empty
     environment plate. %(aspect)s format."

START_FRAME  (the first frame of a group's first speaking scene)
One start_frame entry per GROUP (AI RULE #8): it is the image the video model
starts from, so it fixes the character's look and place for the whole group.
It is a single frame — NOT a grid, NOT a storyboard.
- "prompt" TEMPLATE (char_ids like "char_teacher" are role labels the user
  will substitute; do not describe how the character LOOKS):
    "<char_id> stands in the center of the frame, facing the camera, upper
     body and head fully visible, hands relaxed at their sides, set into the
     environment shown in the background reference. Medium shot at eye level,
     the character fills about half the frame height. Character design and
     wardrobe exactly as in the character reference; environment, lighting
     and colors exactly as in the background reference. %(aspect)s format."
- "reference": EXACTLY two ids: the group's ONE char_id, then ONE bg_id.

SCENES  (ordered timeline of Veo clips, played back-to-back with NO cuts)

EACH SCENE SHAPE (all keys REQUIRED unless marked nullable):
  {
    "id":             <string>,
    "task":           "image-to-video",
    "model":          "Veo 3.1 Lite",
    "text":           <string>,       spoken words for TTS ("" for B-Roll)
    "prompt":         <string>,       structured Veo prompt
    "safety":         "Allow (Adult only)",
    "reference_from": <string>,
    "type":           "video-with-audio" | "B-Roll",
    "broll_role":     null | "opening" | "closing",
    "length":         null | <number>,
    "aspect_ratio":   "%(aspect)s",       exactly this value on every scene
    "topic":          <string> | null,
    "characters":     [<char_id>, ...],
    "audios":         [ {"char_id": <str>, "audio_id": <str>}, ... ]
  }

TASK & MODEL SELECTION  (every scene, no exceptions):
  - task = "image-to-video", model = "Veo 3.1 Lite". There is no
    reference-to-video. Each clip starts from ONE image, named by
    reference_from: a start_frame, a background, or an earlier scene.

CHARACTERS + AUDIOS  (per-scene):
- "characters": EXACTLY ONE char_id on a video-with-audio scene, the same one
  as every other scene in its group (AI RULE #8). Use SEMANTIC role-based
  ids (e.g. "char_teacher").
- "audios": one entry per char_id in "characters". Use placeholder
  audio_ids like "audio_teacher_voice".
- The SAME char_id across multiple scenes MUST have the SAME audio_id
  (voice consistency).
- video-with-audio: exactly one char_id, with a matching entry in "audios".
- B-Roll: ALWAYS "characters": [] and "audios": [] — no exceptions.

Each scene is one Veo clip. Two types:

  A) type = "video-with-audio"
     - The character SPEAKS to the camera (AI RULE #9). Lips and jaw animate
       continuously.
     - "text": exact spoken words (TTS input).
     - Word budget: HARD CEILING %(max_words)d words (= %(max_s)ss at
       %(pace)s WPS). If a source sentence exceeds this, SPLIT into
       multiple video-with-audio scenes with the SAME topic label.
     - "length": null (computed downstream from the word count).
       "broll_role": null.
     - "topic": REQUIRED.
     - "characters": exactly one. "audios": one entry for that character.
     - "reference_from":
         * FIRST scene of a group: "start_frame:<sf_id>"
         * CONTINUATION scene (same group, word-count split):
           "scene:<prev_scene_id>"
     - Subject: the one character, centered, facing the camera. Action: speaks
       to the camera with small body language only (AI RULE #9).
     - Action field MUST include verbatim:
         "the character's lips and jaw move continuously as if speaking,
          articulating dialogue that will be overlaid separately in
          post-production; the visual lip-sync animation is sustained
          throughout the entire clip"
     - CONTINUATION scenes apply AI RULE #6.

  B) type = "B-Roll"
     - Pure environment/object shot. NO characters, NO speech.
     - "text": "". "length": %(broll_s)s.
     - "characters": []. "audios": [].
     - "broll_role": REQUIRED.
     - ALWAYS task="image-to-video", model="Veo 3.1 Lite".

B-ROLL CONTINUITY:

  CLOSING B-Roll  (broll_role = "closing")
  - A held-frame breathing beat that ENDS a topic. It is the previous
    scene's final frame, HELD and gently settling — NOT a fresh scene, NOT
    a new location. Do NOT invent a new environment, subject, or object.
    Emit one at the end of EVERY topic, including the last topic.
  - "reference_from": the immediately-previous scene id (bare or "scene:")
    → task="image-to-video", model="Veo 3.1 Lite". That frame IS the
    starting image; the clip only holds it.
  - Previous scene MUST be video-with-audio.
  - "characters": [], "audios": []. The speaker already in the inherited frame
    stays visible but MOTIONLESS — frozen mid-moment as if the video has
    paused, like a still photograph. Describe the stillness POSITIVELY
    ("the figure holds perfectly still, frozen mid-moment"); never with
    negations like "not speaking" or "closed mouth" (AI RULE #1).
  - Apply AI RULE #6 — the prompt is a STATE DIFF, not a full description:
      * Subject: the existing held frame; introduce nothing new.
      * Action: ONLY the settle into stillness ("the camera holds; the
        moment freezes").
      * Style and Ambience: "Consistent with previous frame" followed by the
        verbatim wordless clause of AI RULE #3 (never omitted).
      * Do NOT restate inherited environment, lighting, or composition.

  OPENING B-Roll  (broll_role = "opening")
  - Fresh visual that opens the video. Exactly one, always first.
  - "reference_from": ALWAYS "background:<bg_id>".
  - task="image-to-video", model="Veo 3.1 Lite".
  - "characters": [], "audios": [].

SCENE PROMPT FORMAT (strict — render layer parses these labels):
"Cinematography: <camera + shot composition + lens/focus>,
Subject: <for video-with-audio: the one speaking character, centered, facing camera.
   For B-Roll: the environment, object, or texture in frame — no people>,
 Action: <what happens. For video-with-audio, include the verbatim lip-
   sync clause, then only a nod, head tilt, or small hand gesture.>,
 Context: <environment + background elements>,
 Style and Ambience: <mood + lighting>, wordless illustration; all
   surfaces smooth and blank; no signage, no writing, no text, no labels, or numerical
   characters visible on any object, wall, or background,
 Ambient noise: <environmental sound ONLY>,
 Language: %(language)s"

OUTPUT LANGUAGE — applies to every scene's "text"
- The narration in "text" MUST be written in %(language)s. Not English,
  unless %(language)s IS English. This is the language the audience hears
  and reads as subtitles; it is the whole point of the setting.
- Translate the meaning of the SOURCE, do not transliterate it. The source
  document may be in any language; the output follows %(language)s.
- Proper nouns stay as they are: product names, system names, people,
  companies (e.g. "Salesforce", "Helpdesk", "Epicor"). Everything around
  them is %(language)s.
- "title" is also in %(language)s (see TITLE).
- The prompt fields are DIFFERENT: "prompt", "style", "music_style" and the
  image prompts stay in ENGLISH, because they instruct Veo and the image
  model, not the viewer. Only "text" and "title" are audience-facing.

COVERAGE  (how much of the SOURCE the video must teach)
- Produce between %(min_topics)d and %(max_topics)d distinct topics, each a
  different part of the SOURCE, taken in the order the SOURCE presents them.
- Each topic is one group: 2-4 video-with-audio scenes, all with the SAME
  one character, and it ends with a closing B-Roll.
- Do NOT stop after the first section. OBJECTIVE words like "most important"
  or "crucial" choose WHICH parts to keep, never how few topics to write.
  If the SOURCE is short, cover all of it.

TIMELINE RULES OF THUMB:
- Open with a short B-Roll (broll_role="opening").
- Video-with-audio: <= %(max_words)d words IN %(language)s, references
  a start_frame (first of its group) OR chains from the previous scene of
  its group, sets "topic".
- Same-topic video-with-audio scenes play back-to-back.
- B-Roll only at true topic boundaries.
- Every topic ends with a closing B-Roll, including the final topic.
  A topic change with no closing B-Roll between the two topics is invalid.

Return ONLY the JSON object. No prose, no markdown fences."""


# Topics per video, by depth. The ceiling is advice to the model; the floor is
# enforced by validate_blueprint so a one-topic video is retried, not accepted.
TOPIC_BOUNDS = {"standard": (3, 5), "deep dive": (4, 8)}
WORDS_PER_TOPIC = 150      # a source shorter than this per topic cannot fill it


def topic_bounds(depth: str, source_text: str) -> tuple[int, int]:
    """(min_topics, max_topics) for this run. The floor shrinks for a short
    source, so a one-paragraph document is never asked for topics it lacks."""
    lo, hi = TOPIC_BOUNDS.get((depth or "").strip().lower(), TOPIC_BOUNDS["standard"])
    available = max(1, len(source_text.split()) // WORDS_PER_TOPIC)
    return min(lo, available), hi


def build_director_prompt(source_text: str, config: DirectorConfig) -> tuple[str, str]:
    """
    Build the system and user prompts for the Director LLM.

    The system prompt contains all generation rules, while the user prompt
    contains only the source document. Keeping them separate improves prompt
    caching and prevents the source text from being interpreted as instructions.
    """
    min_topics, max_topics = topic_bounds(config.depth, source_text)
    max_words = int(config.words_per_second * config.max_scene_seconds)
    min_words = int(config.words_per_second * config.min_scene_seconds)
    system = DIRECTOR_RULES % {
        "min_words": min_words,
        "max_words": max_words,
        "min_s": config.min_scene_seconds,
        "max_s": config.max_scene_seconds,
        "pace": config.words_per_second,
        "broll_s": config.broll_length_seconds,
        "aspect": config.aspect_ratio,
        "language": config.language,
        "min_topics": min_topics,
        "max_topics": max_topics,
    }
    # LANGUAGE is spelled out as an instruction, not left as a bare label.
    system += (
        f"\n\nBASE_STYLE (extend in 'style'): {config.style}"
        f"\nLANGUAGE: write every scene's \"text\" and the \"title\" in "
        f"{config.language}. Image and video prompts stay in English."
        f"\nAUDIENCE: {config.audience}   DEPTH: {config.depth}"
    )
    if config.objective.strip():
        system += f"\nOBJECTIVE: {config.objective.strip()}"
    return system, f"SOURCE:\n{source_text}"


# ----------------------------------------------------------------------
# Parsing + validation
# ----------------------------------------------------------------------
def parse_blueprint(text: str) -> VideoBlueprint:
    """Parse the LLM response into a validated VideoBlueprint."""
    return VideoBlueprint.model_validate(parse_json_lenient(text))


# --- prompt-content checks -------------------------------------------------
# Required prompt sections expected by the renderer and Veo prompt parser.

_REQUIRED_PROMPT_LABELS = (
    "Cinematography:", "Subject:", "Action:", "Context:",
    "Style and Ambience:", "Ambient noise:",
)

# Accept any of these phrases as evidence that the prompt explicitly
# requests blank, text-free surfaces.
_WORDLESS_ANCHORS = ("wordless", "no writing", "no text", "no signage")

# Phrases that consistently produce poor Veo results and are therefore banned.
_FORBIDDEN_NEGATIONS_ANY = (
    "no speaking", "not speaking", "silent character",
    "closed mouth", "mouth closed", "mouth close",
    "mouth shut", "lips sealed", "lips pressed",
    "static face", "mouth at rest",
)


def _wc(s: str) -> int:
    return len(s.split())


# ----------------------------------------------------------------------
# reference_from grammar — ONE definition, shared with L5 (scene_gen), which
# re-checks it at generation time and resolves the referenced asset from it.
# ----------------------------------------------------------------------
REFERENCE_PREFIXES = ("start_frame", "background", "scene")

# Every scene is image-to-video: the prefix only says WHICH image seeds the clip.
# Model names live here only — scene_gen imports them rather than repeating them.
ROUTE = ("image-to-video", "Veo 3.1 Lite")
ROUTE_BY_PREFIX: dict[str, tuple[str, str]] = {p: ROUTE for p in REFERENCE_PREFIXES}


def parse_reference(reference_from: str) -> tuple[str | None, str, bool]:
    """Split a reference_from into (prefix, id, explicit).

    Convert the raw reference_from string into a structured form 
    that the validator can easily understand.

    prefix is 'start_frame' | 'background' | 'scene', or None when the value is
    a bare keyword with no id ("start_frame", "background", "scene") — ambiguous.
    explicit=False marks a bare earlier-scene id (no "scene:" prefix); the
    validator reports that case differently from an explicit "scene:<id>".
    """
    ref = (reference_from or "").strip()
    for p in REFERENCE_PREFIXES:
        if ref.startswith(f"{p}:"):
            return p, ref[len(p) + 1:].strip(), True
    if ref in REFERENCE_PREFIXES:
        return None, ref, False          # bare keyword, no id -> ambiguous
    return "scene", ref, False           # bare earlier-scene id


def _expected_task_and_model(prefix: str | None) -> tuple[str | None, str | None]:
    return ROUTE_BY_PREFIX.get(prefix or "", (None, None))


# --- output language ---------------------------------------------------------

_ENGLISH_MARKERS = frozenset({
    "the", "and", "of", "with", "this", "that", "these", "those", "your",
    "you", "are", "is", "was", "were", "for", "from", "have", "has", "will",
    "they", "their", "them", "which", "when", "how", "but", "not", "it",
    "its", "be", "can", "should", "must", "we", "our", "there", "into",
    "about", "each",
})

# Anything matching these is treated as an English run, where the check is a
# no-op. Substring match, same convention as renderer._SCRIPT_FONTS.
_ENGLISH_NAMES = ("english", "en-", "en_", "inggris", "anglais")

# Measured 2026-08-02 over real blueprints and hand-written samples in six
# languages: English narration scores 0.23, and Indonesian / French / Spanish /
# German / Malay / Vietnamese all score 0.00. The gap is enormous, so the
# threshold sits far from both sides rather than being tuned to either.
_ENGLISH_MARKER_THRESHOLD = 0.10


def _english_marker_ratio(texts: list[str]) -> tuple[float, int]:
    """
    Estimate how "English" the narration is by measuring common English words.

    All scenes are combined before scoring to make the heuristic more stable.
    """
    words = [w for t in texts for w in re.findall(r"[a-zA-Z]+", (t or "").lower())]
    if not words:
        return 0.0, 0
    return sum(w in _ENGLISH_MARKERS for w in words) / len(words), len(words)


def check_output_language(bp: VideoBlueprint, language: str) -> list[str]:
    """
    Detect when narration is accidentally generated in English.

    Uses a lightweight heuristic instead of full language detection because
    the only failure we care about is "English instead of the requested language."

    Deliberately silent when:
      * the language is English — nothing to check;
      * there is no narration, or under 20 words — too little to judge, and a
        false positive costs a whole retry.
    """
    if not language or any(k in language.strip().lower() for k in _ENGLISH_NAMES):
        return []

    texts = [s.text for s in bp.scenes
             if s.type == "video-with-audio" and (s.text or "").strip()]
    ratio, n_words = _english_marker_ratio(texts)
    if n_words < 20 or ratio < _ENGLISH_MARKER_THRESHOLD:
        return []

    sample = next((t for t in texts if t.strip()), "")[:80]
    return [
        f"scene text appears to be ENGLISH, but config.language is "
        f"'{language}'. Rewrite every scene's \"text\" in {language}. "
        f"Keep product and system names as they are, and leave \"prompt\", "
        f"\"style\" and \"music_style\" in English. First offending scene text: "
        f"\"{sample}...\""
    ]


def validate_blueprint(bp: VideoBlueprint, config: DirectorConfig,
                       min_topics: int = 1) -> list[str]:
    """
    Validate rules that cannot be expressed by the Pydantic schema.

    Returns every problem found so the retry loop can ask the model to fix
    them all in a single attempt.
    """
    problems: list[str] = []
    max_words = int(config.words_per_second * config.max_scene_seconds)

    # --- coverage: enough distinct topics ---
    n_topics = len({sc.topic for sc in bp.scenes
                    if sc.type == "video-with-audio" and sc.topic})
    if n_topics < min_topics:
        problems.append(
            f"coverage: only {n_topics} topic(s) but this source needs at least "
            f"{min_topics}. Cover more of the SOURCE in order — add topics, each "
            f"with 2-4 video-with-audio scenes and a closing B-Roll."
        )

    # --- narration is in the requested language ---
    problems.extend(check_output_language(bp, config.language))

    # --- char_ids collected across scenes (source of truth) ---
    all_char_ids: set[str] = set()
    for s in bp.scenes:
        all_char_ids.update(s.characters)

    # --- character_count invariant ---
    if len(all_char_ids) != bp.character_count:
        problems.append(
            f"character_count mismatch: declared {bp.character_count}, "
            f"but {len(all_char_ids)} distinct char_ids used across "
            f"scenes: {sorted(all_char_ids)}"
        )

    # --- aspect_ratio: every scene matches the run's config ---
    # Checked against config rather than a hardcoded "16:9", so this stays
    # correct if portrait is ever unlocked, and so it doubles as the
    # uniformity check the renderer needs — merge_scenes normalises every
    # scene to the FIRST scene's geometry with scale+pad, silently
    # letterboxing any that differ.
    #
    # ConfigIn pins the value to 16:9, but that only stops the USER asking for
    # portrait; Scene.aspect_ratio still admits "9:16", and until this check
    # existed a director that emitted one produced a blueprint that validated
    # clean and then asked Veo for portrait video against landscape reference
    # images. 203/203 scenes on disk happen to be 16:9 — a coin that has landed
    # the same way every time, not a coin that cannot land the other way.
    # See docs/open-questions.md #13.
    for s in bp.scenes:
        if s.aspect_ratio != config.aspect_ratio:
            problems.append(
                f"scene '{s.id}': aspect_ratio is '{s.aspect_ratio}' but this "
                f"run is '{config.aspect_ratio}' — every scene must use "
                f"'{config.aspect_ratio}'"
            )

    # --- voice consistency: each char_id has ONE audio_id ---
    char_to_audios: dict[str, set[str]] = {}
    for s in bp.scenes:
        for am in s.audios:
            char_to_audios.setdefault(am.char_id, set()).add(am.audio_id)
    for cid, aset in char_to_audios.items():
        if len(aset) > 1:
            problems.append(
                f"char_id '{cid}' has inconsistent audio_ids across "
                f"scenes: {sorted(aset)} — voice must be stable"
            )

    # --- bg_ids ---
    bg_ids = {b.id for b in bp.backgrounds}
    if len(bg_ids) != len(bp.backgrounds):
        problems.append("duplicate background ids")

    ref_ids = all_char_ids | bg_ids

    # --- start_frame: one per group, each = ONE character + ONE background ---
    sf_ids: set[str] = set()
    sf_char: dict[str, str] = {}
    for sf in bp.start_frame:
        if sf.id in sf_ids:
            problems.append(f"duplicate start_frame id '{sf.id}'")
        sf_ids.add(sf.id)
        chars = [r for r in sf.reference if r in all_char_ids]
        bgs = [r for r in sf.reference if r in bg_ids]
        unknown = [r for r in sf.reference if r not in ref_ids]
        if unknown:
            problems.append(
                f"start_frame '{sf.id}': reference {unknown} is not a "
                "known char_id (from any scene) or background id"
            )
        if len(chars) != 1 or len(bgs) != 1 or len(sf.reference) != 2:
            problems.append(
                f"start_frame '{sf.id}': reference must be exactly one char_id "
                f"and one background id (got {sf.reference})"
            )
        elif chars:
            sf_char[sf.id] = chars[0]

    # --- scene-level ---
    # Single forward pass. `seen_scenes` grows as we go, which is what makes
    # "reference_from must point at an EARLIER scene" checkable without a second
    # pass — a forward reference simply isn't in the set yet. It also means the
    # scene list order is authoritative: reordering scenes changes validity.
    seen_scenes: set[str] = set()
    prev_scene: Scene | None = None
    sf_used: set[str] = set()
    # Group state (AI RULE #8): a group is the run of scenes between two B-Rolls,
    # and it has ONE character. B-Roll closes the group; the next speaking scene
    # opens a new one and may name a different character.
    group_char: str | None = None
    # Topic-boundary state machine. The rule being enforced (AI RULE #4) is:
    # exactly one closing B-Roll between two speaking scenes of DIFFERENT topics,
    # and none between two speaking scenes of the SAME topic. B-Roll scenes don't
    # carry a topic, so we track the last SPEAKING topic separately from
    # prev_scene and remember whether a closing B-Roll has been seen since.
    prev_talking_topic: str | None = None
    closing_since_talk = False
    closing_id: str | None = None
    for s in bp.scenes:
        if s.id in seen_scenes:
            problems.append(f"duplicate scene id '{s.id}'")

        # reference_from resolution + prefix tracking. `ref` becomes the bare id
        # (or the unparseable value), which the later B-Roll/topic checks use.
        original_prefix, ref, explicit = parse_reference(s.reference_from)

        if original_prefix is None:
            problems.append(
                f"{s.id}: reference_from '{ref}' is ambiguous — use "
                "'start_frame:<sf_id>', 'background:<bg_id>' or 'scene:<scene_id>'"
            )
        elif original_prefix == "start_frame":
            if not ref:
                problems.append(f"{s.id}: reference_from 'start_frame:' missing id")
            elif ref not in sf_ids:
                problems.append(
                    f"{s.id}: reference_from unknown start_frame '{ref}'"
                )
            elif ref in sf_used:
                problems.append(
                    f"{s.id}: start_frame '{ref}' already starts another scene — "
                    "one start_frame per group"
                )
            else:
                sf_used.add(ref)
        elif original_prefix == "background":
            if not ref:
                problems.append(f"{s.id}: reference_from 'background:' missing id")
            elif ref not in bg_ids:
                problems.append(
                    f"{s.id}: reference_from unknown background '{ref}'"
                )
        elif explicit:                          # "scene:<id>"
            if not ref:
                problems.append(f"{s.id}: reference_from 'scene:' missing id")
            elif ref not in seen_scenes:
                problems.append(
                    f"{s.id}: reference_from '{ref}' is not an earlier scene id"
                )
        else:                                   # bare earlier-scene id
            if ref not in seen_scenes:
                problems.append(
                    f"{s.id}: reference_from '{ref}' is neither a known "
                    "start_frame/background reference nor an earlier scene id"
                )

        # task/model mapping
        exp_task, exp_model = _expected_task_and_model(original_prefix)
        if exp_task and s.task != exp_task:
            problems.append(
                f"{s.id}: task='{s.task}' does not match reference_from "
                f"prefix ('{original_prefix}:' requires task='{exp_task}')"
            )
        if exp_model and s.model != exp_model:
            problems.append(
                f"{s.id}: model='{s.model}' does not match reference_from "
                f"prefix ('{original_prefix}:' requires model='{exp_model}')"
            )

        # characters / audios coverage. B-Roll carries neither, so only
        # speech scenes need a voice per character. This mirrors the
        # invariant patch_blueprint.py enforces at L3 — catching it here
        # lets the director's retry loop repair it automatically.
        audio_char_ids = {am.char_id for am in s.audios}
        if s.type == "video-with-audio":
            missing = [c for c in s.characters if c not in audio_char_ids]
            if missing:
                problems.append(
                    f"{s.id}: characters {missing} have no audio mapping"
                )
            extra = [c for c in audio_char_ids if c not in s.characters]
            if extra:
                problems.append(
                    f"{s.id}: 'audios' has entries for char_ids not in "
                    f"'characters': {extra}"
                )

        # per-type rules
        if s.type == "B-Roll":
            if s.text.strip():
                problems.append(
                    f"{s.id}: B-Roll must have empty text "
                    f"(found {_wc(s.text)} words)"
                )
            if s.length is None or s.length <= 0:
                problems.append(f"{s.id}: B-Roll must set a positive 'length'")
            elif s.length > config.max_scene_seconds:
                problems.append(
                    f"{s.id}: B-Roll length {s.length}s exceeds max "
                    f"{config.max_scene_seconds}s"
                )
            if s.broll_role is None:
                problems.append(f"{s.id}: B-Roll must set 'broll_role'")
            if s.broll_role == "closing":
                if ref not in seen_scenes:
                    problems.append(
                        f"{s.id}: closing B-Roll reference_from must be "
                        f"the immediately-previous scene id (got '{ref}')"
                    )
                elif prev_scene is not None and ref != prev_scene.id:
                    problems.append(
                        f"{s.id}: closing B-Roll must reference the "
                        f"immediately-previous scene ('{prev_scene.id}')"
                    )
                if prev_scene is not None and prev_scene.type != "video-with-audio":
                    problems.append(
                        f"{s.id}: closing B-Roll must follow video-with-audio"
                    )
                if "consistent with previous frame" not in s.prompt.lower():
                    problems.append(
                        f"{s.id}: closing B-Roll must HOLD the previous frame "
                        f"— its Style & Ambience must read 'Consistent with "
                        f"previous frame', not describe a new scene"
                    )
            elif s.broll_role == "opening":
                # BUG (flagged, not fixed): ("background") is a STRING, not a
                # 1-tuple — no trailing comma. So this is a substring test, not a
                # membership test. Two consequences:
                #   * it happens to give the right answer for the three real
                #     prefixes ("background" matches; "start_frame"/"scene" don't),
                #     which is why nothing has surfaced;
                #   * original_prefix is None when reference_from is a bare
                #     keyword, and `None in "background"` raises TypeError. An
                #     opening B-Roll with an ambiguous reference_from therefore
                #     crashes validate_blueprint with a TypeError instead of
                #     returning a problem the retry loop could repair, and that
                #     escapes /pipeline/direct as a 500. Note the None case is
                #     already reported earlier in this loop, so the fix is to
                #     compare against a real tuple. See docs/open-questions.md.
                if original_prefix not in ("background",):
                    problems.append(
                        f"{s.id}: opening B-Roll "
                        "reference_from must be 'background:...'"
                    )
        else:  # video-with-audio
            if s.broll_role is not None:
                problems.append(
                    f"{s.id}: video-with-audio must leave 'broll_role' null"
                )
            if not s.text.strip():
                problems.append(
                    f"{s.id}: video-with-audio requires non-empty 'text'"
                )
            wc = _wc(s.text)
            if wc > max_words:
                problems.append(
                    f"{s.id}: text is {wc} words; max {max_words} to fit "
                    f"<= {config.max_scene_seconds}s at "
                    f"{config.words_per_second} WPS"
                )
            if len(s.characters) != 1:
                problems.append(
                    f"{s.id}: video-with-audio must list exactly one char_id in "
                    f"'characters' (got {len(s.characters)}) — one speaker, no dialogue"
                )
            elif group_char is None:
                group_char = s.characters[0]
            elif s.characters[0] != group_char:
                problems.append(
                    f"{s.id}: character changes from '{group_char}' to "
                    f"'{s.characters[0]}' inside one group — the character may only "
                    "change after a B-Roll"
                )
            if original_prefix == "start_frame" and len(s.characters) == 1:
                want = sf_char.get(ref)
                if want is not None and want != s.characters[0]:
                    problems.append(
                        f"{s.id}: start_frame '{ref}' shows '{want}' but the scene's "
                        f"character is '{s.characters[0]}'"
                    )
            # topic continuity
            is_continuation = (
                prev_scene is not None
                and prev_scene.type == "video-with-audio"
                and prev_scene.topic is not None
                and s.topic is not None
                and prev_scene.topic == s.topic
            )
            if is_continuation:
                if original_prefix == "start_frame":
                    problems.append(
                        f"{s.id}: video-with-audio continues topic "
                        f"'{s.topic}' from '{prev_scene.id}' — must chain "
                        f"from previous scene, not start a fresh start_frame"
                    )
                elif original_prefix == "scene" and ref != prev_scene.id:
                    problems.append(
                        f"{s.id}: video-with-audio continues topic "
                        f"'{s.topic}' — reference_from must be "
                        f"'scene:{prev_scene.id}'"
                    )
            else:
                if original_prefix != "start_frame":
                    problems.append(
                        f"{s.id}: video-with-audio starts topic "
                        f"'{s.topic}' — reference_from must be "
                        f"'start_frame:<sf_id>' (got '{s.reference_from}')"
                    )
            # mouth-motion
            p = s.prompt.lower()
            if "mouth" not in p and "lip" not in p:
                problems.append(
                    f"{s.id}: video-with-audio prompt must describe "
                    "continuous mouth/lip movement"
                )

        # prompt structure — required labels
        missing_labels = [l for l in _REQUIRED_PROMPT_LABELS if l not in s.prompt]
        if missing_labels:
            problems.append(f"{s.id}: prompt missing labels {missing_labels}")

        # wordless anchor
        p_lower = s.prompt.lower()
        if not any(a in p_lower for a in _WORDLESS_ANCHORS):
            problems.append(
                f"{s.id}: prompt missing wordless-surfaces clause"
            )

        # pure-negation forbidden
        hits = [f for f in _FORBIDDEN_NEGATIONS_ANY if f in p_lower]
        if hits:
            problems.append(
                f"{s.id}: prompt contains forbidden pure-negation phrases "
                f"{hits}"
            )

        # topic + B-Roll placement
        if s.type == "video-with-audio":
            if not (s.topic and s.topic.strip()):
                problems.append(f"{s.id}: video-with-audio must set 'topic'")
            if prev_talking_topic is not None:
                changed = s.topic != prev_talking_topic
                if changed and not closing_since_talk:
                    problems.append(
                        f"{s.id}: topic changes from '{prev_talking_topic}' to "
                        f"'{s.topic}' with no closing B-Roll marking the boundary"
                    )
                elif not changed and closing_since_talk:
                    problems.append(
                        f"{closing_id}: closing B-Roll between two video-with-audio "
                        f"scenes sharing topic '{prev_talking_topic}' — remove"
                    )
            prev_talking_topic = s.topic
            closing_since_talk = False
            closing_id = None
        elif s.broll_role == "closing":
            closing_since_talk = True
            closing_id = s.id

        if s.type == "B-Roll":
            group_char = None

        seen_scenes.add(s.id)
        prev_scene = s

    for unused in sorted(sf_ids - sf_used):
        problems.append(
            f"start_frame '{unused}' is never used — one start_frame per group, "
            "referenced by the first scene of that group"
        )
    return problems


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
def direct(
    source_text: str,
    config: DirectorConfig,
    llm: DirectorLLM,
    max_retries: int = 1,
) -> VideoBlueprint:
    """
    Run the Director retry loop until a valid blueprint is produced.

    Each failed attempt feeds the validation errors back to the LLM so it can
    correct its previous output.
    """
    system, user = build_director_prompt(source_text, config)
    min_topics, _ = topic_bounds(config.depth, source_text)
    note = ""
    last_err = "unknown"
    for _ in range(max_retries + 1):
        raw = llm.complete(system + note, user)
        try:
            bp = parse_blueprint(raw)
            # character_count is derived data: the distinct char_ids the scenes
            # use. GPT-4o miscounts it and repeats the miscount after feedback,
            # burning a paid retry on a number we can compute exactly.
            bp.character_count = len({c for sc in bp.scenes for c in sc.characters})
            problems = validate_blueprint(bp, config, min_topics)
            if not problems:
                return bp
            last_err = "; ".join(problems)
            note = (
                "\n\nYour previous JSON had these errors — fix ALL and "
                "resend:\n- " + "\n- ".join(problems)
            )
        except (ValidationError, json.JSONDecodeError, ValueError) as e:
            last_err = str(e)
            note = (
                f"\n\nYour previous output was invalid JSON/schema: {e}\n"
                "Return ONLY a valid JSON object."
            )
    raise RuntimeError(
        f"director failed after {max_retries + 1} attempts. "
        f"Last error: {last_err}"
    )


# ----------------------------------------------------------------------
# OpenAI
# ----------------------------------------------------------------------
class OpenAIDirector:
    """
    OpenAI (GPT-4o) implementation of DirectorLLM.

    Optionally enables OpenAI structured outputs, which guarantees valid JSON
    structure but does not replace semantic validation.
    """

    def __init__(
        self, client, model: str = "gpt-4o", use_schema: bool = True,
    ):
        self.client = client
        self.model = model
        self.use_schema = use_schema
        # Token counts per call, appended in order. direct() may call complete()
        # several times (one per retry) and every one of them is billed, so this
        # accumulates rather than overwriting.
        #
        # It is a side channel on purpose: DirectorLLM is `complete(str, str) ->
        # str`, and threading usage through the return type would force every
        # implementation and every caller to know about billing. main.run_director
        # reads this after direct() returns; a stub director simply has no
        # usage_log and records nothing.
        self.usage_log: list[dict] = []

    def complete(self, system: str, user: str) -> str:
        # Low-but-nonzero temperature — the prompts need some variety, the
        # structure does not. direct() appends retry feedback to the system half.
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if self.use_schema:
            resp = self.client.chat.completions.parse(
                model=self.model, messages=messages, temperature=0.4,
                response_format=VideoBlueprint,
            )
        else:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0.4,
                response_format={"type": "json_object"},
            )
        from app.costs import usage_from_response
        self.usage_log.append(usage_from_response(resp))
        msg = resp.choices[0].message
        if getattr(msg, "refusal", None):
            raise RuntimeError(f"OpenAI refused: {msg.refusal}")
        return msg.content


# ----------------------------------------------------------------------
# Blueprint-shape rules (keyed on blueprint_type; read from config.json).
# Also consumed by the render layer (L8) for the opening-section decision.
# ----------------------------------------------------------------------
SPEECH_MIN_SECONDS = 4                       # shortest clip the video model makes
OPENING_LENGTH_S = 4.0                       # 'opening' plate is ALWAYS 4s
BLUEPRINT_TYPES = ("opening", "summary")


def read_blueprint_type(run_dir: str | Path, blueprint: dict | None = None) -> str:
    """blueprint_type from config.json (Layer-1 decision); falls back to the
    blueprint then to 'opening' so pre-existing runs behave as before."""
    cfg_path = Path(run_dir) / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            t = cfg.get("blueprint_type") or cfg.get("type")
            if t in BLUEPRINT_TYPES:
                return t
        except Exception:
            pass
    if blueprint:
        t = blueprint.get("type")
        if t in BLUEPRINT_TYPES:
            return t
    return "opening"


def has_opening_section(run_dir: str | Path, blueprint: dict | None = None) -> bool:
    """The opening SECTION (4s B-Roll + 30% scrim + centred title) applies ONLY
    to an 'opening' run. A 'summary' run keeps the director's short plate — no
    title, no scrim."""
    return read_blueprint_type(run_dir, blueprint) == "opening"


def enforce_opening_length(blueprint: dict, run_dir: str | Path | None = None,
                           seconds: float = OPENING_LENGTH_S) -> dict:
    """VEO-SIDE rule. In an 'opening' run the opening B-Roll is ALWAYS `seconds`
    (4s) so it can carry the title section. In a 'summary' run this is a NO-OP:
    the opening B-Roll keeps the director's own short (1-2s) plate.

    Call BEFORE scene generation (L5) so Veo renders the plate at the right
    length. `run_dir` is needed to read the run's blueprint_type."""
    bp = deepcopy(blueprint)
    if run_dir is not None and not has_opening_section(run_dir, bp):
        return bp
    for sc in bp.get("scenes", []):
        if sc.get("broll_role") == "opening" and sc.get("type") == "B-Roll":
            sc["length"] = float(seconds)
    return bp

def speaking_seconds(text: str, words_per_second: float,
                     floor_s: float = SPEECH_MIN_SECONDS) -> int:
    """Whole seconds a line takes to say: word count / speaking rate, ROUNDED DOWN
    (4.4s -> 4s), never below `floor_s` (the video model's shortest clip).
    Rounding down is deliberate: a clip with spare seconds is one the model fills
    with babble of its own."""
    return max(int(floor_s), math.floor(len(text.split()) / words_per_second))


def fill_speaking_lengths(blueprint: dict, words_per_second: float) -> dict:
    """Set `length` on every video-with-audio scene from ITS OWN word count.

    The director leaves it null; this is where it is computed. Per scene, not per
    group — a group only limits who speaks, it does not pool durations. B-Roll
    keeps the length the director set. Call BEFORE scene generation (L5)."""
    bp = deepcopy(blueprint)
    for sc in bp.get("scenes", []):
        if sc.get("type") == "video-with-audio":
            sc["length"] = float(speaking_seconds(sc.get("text") or "", words_per_second))
    return bp


DEFAULT_STYLE_TYPE = "realism"          # conservative: preserves current opening treatment

def derive_style_type(style: str) -> str:
    """
    Map the free-text style description to a StyleType.

    Unknown styles fall back to DEFAULT_STYLE_TYPE which is realism
    """
    s = (style or "").lower()
    if "semi-realism" in s or "semi realism" in s or "semi-realistic" in s:
        return "semi-realism"
    if "cartoon" in s or "flat 2d" in s or "cel-shad" in s or "cel shad" in s:
        return "cartoon"
    if "realism" in s or "realistic" in s or "photoreal" in s:
        return "realism"
    return DEFAULT_STYLE_TYPE