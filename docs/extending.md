# Extension points

The parts of the pipeline that are actively being changed, and what each change
actually touches.

Read the "no stacking" rule at the top of `change.md` first — it's the standing
working agreement for this codebase, and it's stricter than most: fix at the source,
delete what the fix obsoletes, don't wrap a working function to correct its output.

---

## Add a new `blueprint_type`

The live example is `"illustration"` from `change3_short_animation.md` — specced,
not implemented.

`blueprint_type` currently selects blueprint **shape rules**, not a different
director. Both existing values (`opening`, `summary`) go through the same
`DIRECTOR_RULES` and differ only in the opening section. A genuinely different
blueprint shape needs more than a new enum value.

**Minimum for a shape variant** (like `summary`):

1. `schemas.py` — add the value to `BlueprintType`.
2. `director.py` — add it to `BLUEPRINT_TYPES` (the tuple `read_blueprint_type`
   validates against; anything unrecognised falls back to `"opening"`).
3. Decide what `has_opening_section` should return for it. That one function is what
   gates both `enforce_opening_length` (L2) and `apply_opening_title` (L8).

**For a variant with its own director** (like `illustration`), additionally:

4. A second rules constant and a second `build_*_prompt`. Don't add branches inside
   `DIRECTOR_RULES` — it's already at the limit of what one prompt can hold, and the
   validator is keyed to its exact structure.
5. A second validator. `validate_blueprint` assumes the start_frame/speaking/B-Roll
   topology throughout; a blueprint with no characters and no start frames would trip
   dozens of rules that don't apply. This is the real work.
6. A second blueprint model, or optional fields on `VideoBlueprint`. Note that
   `character_count`, `music_style`, `backgrounds`, `scenes` and `start_frame` are all
   currently required.
7. Dispatch in `main.run_director` on `config.blueprint_type`.
8. Check every downstream layer's assumptions. `/pipeline/select` requires at least
   one selection (`min_length=1` on `SelectIn.selections`) and `/pipeline/assets` and
   `/pipeline/scenes` both require `selections.json` to exist — a
   character-free blueprint cannot reach L4 today without changing those gates.

`bench/director_smoke.py` is the fast feedback loop: one live call, prints the
blueprint and runs the validator, no run directory needed.

---

## Add a new `style_type`

Cheap, because `style_type` only affects the title treatment.

1. `schemas.py` — add to the `StyleType` literal.
2. `director.derive_style_type` — add the keyword branch. **Order matters**: more
   specific before less specific, which is why `semi-realism` is checked before
   `realism`. Put your branch where it can't be swallowed by an existing substring.
3. `renderer.py` — define a `TitleStyle` for it and add it to `title_style_for`.

If your style needs a new **animation** rather than new colours, `TitleStyle.anim`
is handled in `apply_opening_title`: `wave` takes the per-letter path
(`_wave_letter_drawtexts`), everything else takes the single-block path with
`rise`/`drop`/`fade` picking the `y` expression. A new per-letter behaviour means a
new function alongside `_wave_letter_drawtexts`.

Two things to know before you do:

- Bundle the font under `asset/fonts/` and reference it via `TitleStyle.font_file`.
  Note the existing relative-path fragility documented in
  [open-questions.md](open-questions.md#3-fixed--relative-asset-paths-broke-when-cwd-wasnt-the-repo-root)
  — prefer an absolute path or resolve against `__file__`.
- `_wave_letter_drawtexts` assumes a **left-to-right, non-shaping** script. Per-letter
  layout mis-renders Arabic and Indic text. The function says so in a comment.

Remember `derive_style_type` fails silently — an unmatched style becomes `realism`.
Add the keyword or your style never activates, with no error to tell you.

---

## Change the character pipeline

Worth being precise about what this means, because **nothing generates a
character**. The user picks a PNG from a library and it's attached as a reference
image. The chain is:

```
character_library/<char_id>.png
        │  attached as a reference image at L4
        ▼
run_0X/start_frame/<sf_id>.png      (one frame, the image model re-draws the character)
        │  used as the first frame of the group's first scene
        ▼
run_0X/scenes/<scene_id>.mp4
```

Consistency depends on each hop preserving likeness, and **neither is verified**.
The start-frame hop is where drift enters — the image model re-draws rather than composites.

There is **no `char_manager` module** in this repo, despite it being referenced in
older notes. The character logic is spread across four places:

| File                              | Role                                                               |
| --------------------------------- | ------------------------------------------------------------------ |
| `app/libraries.py`                | Index build/load/validate for both libraries. Has a CLI.           |
| `main.py`                         | `GET /pipeline/characters`, and `POST /pipeline/select` validation |
| `app/pipeline/patch_blueprint.py` | Placeholder → library id resolution                                |
| `app/pipeline/image_gen.py`       | `_resolve_ref` — attaches the PNG as a reference                   |

### Common changes

**Add metadata to characters** — extend `CharacterEntry`. `build_character_index`
merges over the existing index and never clobbers human-authored fields, so add
your field to the "preserved from prior" set alongside `description` and `tags`, or
it'll be wiped on the next scan.

**Use character metadata in prompts** — nothing reads `description` or `tags` today.
They're returned by the API for the front-end to display, and that's it. The
interesting version of this is feeding `description` into the start-frame prompt at
L4 so the text reinforces the reference image; that's a change in
`image_gen.generate_start_frames`, and `_compose_prompt` is the natural place to hang
it.

**Improve consistency** — the leverage is at L4, not L2. Options, roughly in order
of effort: strengthen the "character design and wardrobe identical across panels"
language in the start-frame template; the character PNG already reaches the clip
through the start frame (the video request carries a single seed image); or generate a per-run character sheet
from the library PNG and reference that instead.

**Change how references resolve** — `image_gen._resolve_ref` decides that anything
not a known `bg_id` is a library character id. Adding a third reference kind means
changing that function and probably namespacing the ids.

> Before touching any of this, note that placeholder ids leak into prompt prose —
> `patch_blueprint` doesn't rewrite `scene.prompt`. See
> [open-questions.md](open-questions.md#1-decided--placeholder-char_ids-leak-into-veo-prompts).

---

## Add a video provider

The pattern is already established by the MiniMax H3 client — copy it.

1. New module in `app/clients/`, implementing the `VeoClient` Protocol:
   `generate(spec: VeoJobSpec) -> bytes`.
2. One entry in `video_providers.VIDEO_PROVIDERS`.
3. Add the name to `ScenesIn.video_provider`'s Literal.

Nothing else changes. Routing, retry, cost logging and `scene_gen` are all
provider-agnostic — only the provider call differs.

Contract details worth getting right:

- **Raise `TransientVeoError`** for 5xx/rate-limit/timeout and `VeoError` for
  everything else. `generate_with_retry` retries only the former.
  `utils/retry.looks_transient` does the substring classification.
- **Return bytes, synchronously.** If the provider is async internally, poll inside
  `generate` like both existing clients do.
- **Optional `credits()` method.** `scene_gen.generate_scene` calls it before and
  after if present and records the measured credit delta; without it, it measures
  the generated duration off the `veo_raw/` mp4 and prices it per second from
  `app.costs.PRICING`. Either way the recorded quantity is measured rather than
  assumed — add a `PRICING` entry for your provider's unit and it is accounted
  for like the rest.
- **Read your key at construction**, from the environment, in that client and nowhere
  else. Never log it.
- **Surface parity gaps, don't silently map them.** `video_minimax_h3_client.py`'s module
  docstring is the model to follow: it documents the task-to-endpoint mapping, the
  async submit/poll flow, and the WebM-to-mp4 conversion. State where data goes: the
  H3 client talks only to the company GPU server on the private network.

## Add an audio provider

Same shape:

1. New module in `app/pipeline/`, exposing `process_scene(...) -> Path`.
2. An adapter class in `audio_providers.py` with a `voice_field` attribute and a
   `process(raw_mp4, scene, voice, run_dir)` method.
3. A builder function and one `PROVIDERS` entry.
4. Add the name to `ScenesIn.tts_provider`'s Literal.

Contract:

- `process` receives the **pristine** clip from `veo_raw/` and must write
  `scenes/<id>.mp4`. Write to a temp file and `os.replace` — the next scene's
  last-frame read depends on that file being complete.
- Trim to where speech ends. The whole L5⇄L6 interleave assumes it.
- Write `tts/<id>_timings.json` via `write_timings_sidecar` if you can produce
  per-phrase timings. Without it, L8 falls back to one caption per scene.
- `voice_field` names the `AudioEntry` attribute holding your voice id. Azure and
  ElevenLabs occupy different namespaces, which is exactly why this indirection
  exists. A new provider needs a new column on `AudioEntry` unless it can reuse one.
- If your provider's ids can be used directly without the library (as
  `voice_source=elevenlabs` does), that's a branch in `resolve_voices`, and a
  corresponding guard in `run_scenes` to reject incompatible
  `voice_source`/`tts_provider` pairs.

---

## Add or change transitions

The lightest extension point — data only, no code.

Drop a `.mov`/`.webm` into `asset/transitions/` and add an entry to `index.json`:

```json
{
  "transition_id": "swipe_yellow",
  "file": "arrow_wipe_yellow.mov",
  "roles": ["opening"],
  "aspect_ratio": "16:9",
  "blend": "alpha",
  "key_color": "0x000000",
  "key_similarity": 0.15,
  "key_blend": 0.1
}
```

- `roles` — which `broll_role` boundaries this suits (`opening`, `closing`). Selection
  prefers role matches and falls back to the whole pool.
- `blend` — `alpha` honours the file's own alpha channel; `colorkey` keys out
  `key_color` for clips that have a solid background instead of transparency.
- A listed file that doesn't exist raises `RenderError` at render time. A missing
  index is fine — the pass is skipped.

Selection is seeded from a SHA-256 of `source.txt`, so the same source document
always gets the same picks. If you want variety across renders of the same source,
that's `_transition_seed`.

Placement rules are `_transition_boundaries`, keyed on `broll_role`. Adding a new
boundary kind means teaching that function, `BRollRole`, and `DIRECTOR_RULES`
together — the renderer can only place a transition where the director marked one.

---

## Change the subtitle look

`SubtitleStyle` covers font, size, colours, outline, margin, and the boxed-only
fields (radius, opacity, padding). `burn_subtitles(method=...)` picks the renderer:

| Method            | Notes                                                                                                                        |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `boxed` (default) | Pillow PNG overlays. The only one that can do rounded corners. Slowest — one ffmpeg input and two filtergraph nodes per cue. |
| `libass`          | ASS `force_style`. Fast, square box, needs a libass-enabled ffmpeg.                                                          |
| `drawtext`        | libass-free fallback, square box.                                                                                            |
| `auto`            | `libass` if available, else `drawtext`.                                                                                      |

`render_merge_and_subs` doesn't expose `method` — it takes a `style` and lets
`burn_subtitles` default to `boxed`. Threading the method through is a two-line
change if you need it.

Cue **timing** is `build_srt`, not the style. If captions are mistimed the problem is
upstream in the L6 timings sidecar, not here.

---

## Change the director's rules

The highest-leverage and highest-risk change in the codebase.

`DIRECTOR_RULES` is a `%`-interpolated string, so **every literal `%` must be
doubled**. The pacing knobs come from `DirectorConfig` via `build_director_prompt`.

The thing to internalise:

> A rule stated in `DIRECTOR_RULES` but not checked in `validate_blueprint` is a
> suggestion the model can ignore, because nothing will ever tell it that it did.

So adding a rule is two edits, not one, and the validator half is the one that
matters. Conversely, adding a validator check without the prompt half means the
model has to discover the rule by failing — costing at least one extra retry every
run.

Rules of thumb:

- Validator messages are **prompt fragments aimed at an LLM**. Name the scene id,
  state what's wrong, state what the correct value would be. Vague messages cost
  retries.
- Return every problem you can find. `direct()` feeds the whole list back at once.
- Keep checks cheap and deterministic. They run on every attempt.
- Test with `bench/director_smoke.py` before wiring anything up. One call, prints
  blueprint + validation.
- Watch for oscillation: too many interacting rules and the model fixes one thing
  while breaking another, exhausting retries with a different error each time.

If you change the **prompt label structure**, `_REQUIRED_PROMPT_LABELS` must match,
and check whether any downstream code parses those labels.

---

## Where the seams are, if you need to test

Every networked client is built through a `_*_client()` function in `main.py`, all
lazily imported. Override the seam and the endpoint runs with no network and no
spend. The Protocols — `DirectorLLM`, `ImageLLM`, `VeoClient`, `TTSClient`,
`Diarizer`, `MusicClient`, `SceneAudioProvider` — exist for exactly this.

`ATLAS_STORAGE_ROOT` is env-overridable specifically so tests never share a tree
with real runs.

There is **no test suite in this repo**. The seams are all there for one; nobody has
written it. See [open-questions.md](open-questions.md#7-partly-fixed--no-ci-integration-coverage-is-thin).
