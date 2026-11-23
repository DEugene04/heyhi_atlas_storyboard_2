# blueprint.json — schema v3 reference

The one artifact every layer agrees on. Written by L2, resolved by L3, consumed by
L4–L8.

Defined in [`app/schemas.py`](../app/schemas.py) (`VideoBlueprint`). Semantic rules
live in `director.validate_blueprint` — pydantic only checks shape, so a blueprint
can satisfy the models here and still be rejected there.

---

## Top level

```json
{
  "title": "Understanding Annual Leave Policies",
  "style": "A professional realism art style, ensuring character consistency across all scenes.",
  "style_type": "realism",
  "character_count": 1,
  "music_style": "Warm acoustic guitar and light piano, calm and reassuring — corporate training explainer mood.",
  "backgrounds": [ { "id": "...", "prompt": "..." } ],
  "scenes":      [ { ... } ],
  "start_frame": [ { "id": "...", "prompt": "...", "reference": ["<char_id>", "<bg_id>"] } ]
}
```

| Field             | Type      | Req | What it controls                                                                                                                                      |
| ----------------- | --------- | --- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `title`           | str, 1–80 | ✅  | Burned over the opening B-Roll. Only used when `blueprint_type == "opening"`. Written in `config.language`.                                           |
| `style`           | str       | ✅  | The art direction. Prepended to **every** image prompt at L4 generation time. The single highest-leverage field in the file.                          |
| `style_type`      | enum      | —   | `realism` \| `cartoon` \| `semi-realism`. Default `realism`. **Derived, not authored** — see [below](#style_type).                                    |
| `character_count` | int ≥ 0   | ✅  | Must equal the number of distinct `char_id`s across all `scenes[].characters`. Hard-checked twice. Also caps how many characters the user may select. |
| `music_style`     | str       | ✅  | Passed verbatim to Lyria (wrapped by `build_bed_prompt`). Genre + mood + instrumentation, one line.                                                   |
| `backgrounds`     | list      | ✅  | One per distinct location. Each becomes `backgrounds/<id>.png`.                                                                                       |
| `scenes`          | list      | ✅  | **The timeline.** List order is playback order.                                                                                                       |
| `start_frame`     | list      | ✅  | One per group: the group's character placed into a background. Each becomes `start_frame/<id>.png`.                                                    |

---

## `backgrounds[]`

| Field    | Type | Notes                                                                                                                |
| -------- | ---- | -------------------------------------------------------------------------------------------------------------------- |
| `id`     | str  | Becomes the PNG filename. Referenced by `scene.reference_from` as `background:<id>` and by `start_frame[].reference`. |
| `prompt` | str  | Empty environment plate, **no characters in frame**, wide establishing shot. Style-neutral — L4 prepends `bp.style`. |

Duplicate ids are rejected.

## `start_frame[]`

One per **group** — a group is every scene between two B-Rolls (a B-Roll ends one
group and starts the next). Each group has exactly one character.

| Field       | Type      | Notes                                                                                                                                  |
| ----------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `id`        | str       | Becomes the PNG filename in `start_frame/`. Referenced as `start_frame:<id>`.                                                            |
| `prompt`    | str       | A single frame: the group's character centered, facing the camera, set into the background. Not a grid.                                |
| `reference` | list[str] | **Exactly two ids**: the group's `char_id`, then one `bg_id`. Resolved to `character_library/<id>.png` and `backgrounds/<id>.png`.      |

Duplicate ids are rejected, and so is a start frame that no scene uses. The scene
that references it must have the same character the frame shows. It is the first
frame of the group's **first** speaking scene; later scenes of the group chain from
the previous clip's last frame.

---

## `scenes[]`

The timeline. Concatenated in list order with no cuts.

```json
{
  "id": "scene_basics_1",
  "task": "image-to-video",
  "model": "Veo 3.1 Lite",
  "text": "Employees receive 30 calendar days of annual leave per year.",
  "prompt": "Cinematography: ... Subject: ... Action: ... Context: ... Style and Ambience: ... Ambient noise: ... Language: english",
  "safety": "Allow (Adult only)",
  "reference_from": "start_frame:sf_basics_1",
  "type": "video-with-audio",
  "broll_role": null,
  "length": null,
  "aspect_ratio": "16:9",
  "topic": "Annual Leave Basics",
  "characters": ["char_narrator"],
  "audios": [{ "char_id": "char_narrator", "audio_id": "audio_narrator_voice" }]
}
```

| Field            | Type      | Notes                                                                                                                    |
| ---------------- | --------- | ------------------------------------------------------------------------------------------------------------------------ |
| `id`             | str       | Used as a **filename** in `veo_raw/`, `scenes/` and `tts/`. Must be filesystem-safe. Duplicates rejected.                |
| `task`           | enum      | Always `image-to-video`.                                                                                                 |
| `model`          | enum      | Always `Veo 3.1 Lite`.                                                                                                   |
| `text`           | str       | Spoken words. `""` for B-Roll (enforced). Non-empty for speaking scenes (enforced). Word-capped (see [pacing](#pacing)). |
| `prompt`         | str       | Structured Veo prompt. Must contain six labels (see below).                                                              |
| `safety`         | str       | Maps to Veo `person_generation` via `GeminiVeoClient._PERSON_GENERATION`. Ignored entirely on the kie.ai path.           |
| `reference_from` | str       | The routing key. See [below](#reference_from).                                                                           |
| `type`           | enum      | `video-with-audio` \| `B-Roll`.                                                                                          |
| `broll_role`     | enum?     | `opening` \| `closing`. Required for B-Roll, must be `null` for speaking scenes.                                         |
| `length`         | float?    | Seconds. Required > 0 for B-Roll, capped at `max_scene_seconds`. Speaking scenes: the director leaves it `null` and `director.fill_speaking_lengths` sets it to `max(4, floor(words / words_per_second))` from that scene's own text.                     |
| `aspect_ratio`   | enum      | `16:9` in practice. The type still admits `9:16`, but the config layer pins it — see below.                              |
| `topic`          | str?      | Required for speaking scenes. Drives B-Roll boundary validation.                                                         |
| `characters`     | list[str] | Placeholder `char_id`s (L2) → library ids (after L3). Must be `[]` for B-Roll. Non-empty for speaking scenes.            |
| `audios`         | list      | One entry per `char_id` in `characters`. Must be `[]` for B-Roll.                                                        |

#### `aspect_ratio` is locked to 16:9, one layer above this one

`Scene.aspect_ratio` and the `AspectRatio` literal both still carry `9:16`, but
**`ConfigIn.aspect_ratio` is `Literal["16:9"]`** — a portrait request is a `422`
at `/pipeline/config`, and the director is handed the config's value, so no
user-reachable path produces a `9:16` scene. Reading this table alone would
suggest otherwise.

The lock lives at the config layer *deliberately*, not out of neglect. Portrait
does not work end to end: `main._image_client()` never reads `config.aspect_ratio`,
so a `9:16` run would generate **landscape** backgrounds and start frames and then
ask Veo for portrait video; and all five bundled transition overlays are
1920×1080, which `apply_transitions` would stretch rather than letterbox. Keeping
the schema bilingual means unlocking is one line plus passing config through in
`_image_client`, instead of a re-implementation. Full reasoning in
[open-questions #13](open-questions.md#13-decided--169-locked-at-the-config-layer).

**Mixed `aspect_ratio` across scenes** is therefore unreachable today, but the
schema would allow it and nothing validates uniformity. If the lock is ever
lifted, note that the renderer normalises every scene to the **first** scene's
geometry with scale+pad, so an off-ratio scene gets silently letterboxed.

### `reference_from`

The single most important field. It determines Veo task, Veo model, and which asset
gets attached. Grammar defined once in `director.py` (`REFERENCE_PREFIXES`,
`ROUTE_BY_PREFIX`, `parse_reference`) and re-checked by `scene_gen.route()` at
generation time — the last gate before a billable call.

| Value                                                 | Task               | Model            | Input attached                                     |
| ----------------------------------------------------- | ------------------ | ---------------- | -------------------------------------------------- |
| `start_frame:<sf_id>`                                 | image-to-video     | Veo 3.1 **Lite** | `start_frame/<id>.png` as the seed frame           |
| `background:<bg_id>`                                  | image-to-video     | Veo 3.1 **Lite** | `backgrounds/<id>.png` as the seed frame           |
| `scene:<prev_id>`                                     | image-to-video     | Veo 3.1 **Lite** | **last frame** of `scenes/<prev_id>.mp4`           |
| `<prev_id>` (bare)                                    | image-to-video     | Veo 3.1 **Lite** | same as `scene:` — tolerated, reported differently |
| bare `start_frame` / `background` / `scene` with no id | —                  | —                | **rejected as ambiguous**                          |

The referenced id must resolve, and a `scene:` reference must point at an
**earlier** scene — validation is a single forward pass, so a forward reference
simply isn't in the seen set yet. This also means **scene order is authoritative**;
reordering scenes changes validity.

### Prompt structure

`validate_blueprint` requires all six labels verbatim:

```
Cinematography: <camera + composition + lens>
Subject: <who or what is in frame>
Action: <what happens>
Context: <environment>
Style and Ambience: <mood + lighting>, wordless illustration; all surfaces smooth
    and blank; no signage, writing, labels, or numerical characters visible on any
    object, wall, or background
Ambient noise: <environmental sound only>
Language: <config.language>
```

`Language:` is emitted but **not** enforced. Additional content checks:

- **Wordless anchor** — the prompt must contain one of `wordless`, `no writing`,
  `no text`, `no signage`. Veo hallucinates signage otherwise.
- **No pure negations** — `"no speaking"`, `"closed mouth"`, `"static face"` etc.
  are rejected outright. Veo tends to _act out_ a negation: tell it "not speaking"
  and you get a character making a deliberate not-speaking face. Stillness must be
  described positively.
- **Speaking scenes** must mention `mouth` or `lip`, and should contain the verbatim
  lip-sync clause.
- **Simple performance** (AI RULE #9, prompt-enforced) — the character is centered,
  speaks to the camera, and uses only small body language; no props or movement.
- **Closing B-Roll** must contain `consistent with previous frame`.

### Timeline rules

1. Exactly one `opening` B-Roll, first, referencing `background:<bg_id>`.
2. Speaking scenes: the **first** of a topic (group) references a `start_frame`; **continuations**
   (same `topic` as the previous scene) reference `scene:<prev_id>`.
3. Every topic ends with a `closing` B-Roll, **including the last**. It references
   the immediately-previous scene, which must be a speaking scene.
4. A topic change with no closing B-Roll between is invalid. A closing B-Roll
   _between two scenes of the same topic_ is also invalid.
5. **One character per group.** Every speaking scene lists exactly one `char_id`,
   and it is the same one for every scene between two B-Rolls. It may change after
   a B-Roll. There is no dialogue between characters.
6. B-Roll carries no characters at all, ever — including closing B-Rolls, where
   anyone already in the inherited frame stays visible but motionless.

There is no `topic-transition` role. The renderer composites a transition overlay
_across the cut_ after each closing B-Roll; transitions are never authored as
scenes.

### Continuation scenes are state diffs

For `reference_from: "scene:<prev>"`, the previous frame is the starting visual
state and everything in it is inherited. The prompt must describe **only the
delta** — new movement, expression, gesture, object. Restating inherited properties
makes the renderer apply them again (blur on blur, grade on grade) and degrades the
image. `Style and Ambience` on a continuation should read `"Consistent with previous
frame"`.

This is `DIRECTOR_RULES` AI RULE #6 and is only partly validator-enforced (the
`consistent with previous frame` check applies to closing B-Rolls only).

---

## Config flags that change generation

### `blueprint_type`

Set in `config.json` at L1. `schemas.BlueprintType` = `("opening", "summary")`.
Read by `director.read_blueprint_type`, which falls back to `"opening"` for
anything unrecognised.

|                       | `opening`                                      | `summary`                        |
| --------------------- | ---------------------------------------------- | -------------------------------- |
| Opening B-Roll length | **forced to 4.0s** by `enforce_opening_length` | director's own short plate (~1s) |
| Title card            | burned over the opening B-Roll                 | **none**                         |
| Scrim                 | per `style_type`                               | none                             |

Nothing else differs. The director prompt is identical for both — `blueprint_type`
is passed into `DirectorConfig` but `build_director_prompt` never interpolates it.

> `change3_short_animation.md` specs a third value, `"illustration"`, with its own
> director layer. **Not implemented.** See [open-questions.md](open-questions.md#4-decided--illustration-blueprint-type-is-specced-not-implemented).

### `style_type`

Derived, never authored. `director.derive_style_type` keyword-matches the free-text
`style` string, checking `semi-realism` **before** `realism` (otherwise the naive
substring match mislabels).

| Matches                                          | Result              |
| ------------------------------------------------ | ------------------- |
| `semi-realism`, `semi realism`, `semi-realistic` | `semi-realism`      |
| `cartoon`, `flat 2d`, `cel-shad`, `cel shad`     | `cartoon`           |
| `realism`, `realistic`, `photoreal`              | `realism`           |
| anything else                                    | `realism` (default) |

Its only consumer is `renderer.title_style_for`:

|                | `cartoon`                                         | `realism` / `semi-realism`       |
| -------------- | ------------------------------------------------- | -------------------------------- |
| Scrim          | **none** (opacity 0.0)                            | 30% black                        |
| Font           | `asset/fonts/Fredoka-SemiBold.ttf`                | first available system font      |
| Fill / outline | yellow `FFD23F` / white, 6px                      | white / light blue `9CD3FF`, 4px |
| Size           | 104                                               | 96                               |
| Animation      | `wave` — per-letter travelling bounce + idle loop | `rise` — one block, eased        |

An unrecognised style silently becomes `realism`. That's deliberate: a wrong title
style is cosmetic, and failing hard would waste a director call.

### `style`

Not a flag but worth restating: this free-text sentence is prepended to every image
prompt at L4 and is what `style_type` is derived from. It's the one place art
direction is authored.

### `aspect_ratio`

Interpolated into `DIRECTOR_RULES` so the director writes it into background and
start-frame prompt templates, and copied onto each scene. Passed to Veo per scene and
to `GeminiImageClient` per image.

### `language`

Interpolated into the director prompt, so `scene.text` and `title` are authored in
it. **Subtitles are never translated** — L8 only has to _render_ the language, which
means picking a font with the right glyphs (`_SCRIPT_FONTS` covers CJK, Thai,
Arabic, Devanagari). A missing script font produces a non-fatal `font_warning` on
the render response and blank boxes in the video.

### Pacing

Not in `config.json` — code-only, in `DirectorConfig`:

| Field                  | Default | Effect                                                            |
| ---------------------- | ------- | ----------------------------------------------------------------- |
| `words_per_second`     | 2.5     | With `max_scene_seconds`, sets the word ceiling                   |
| `max_scene_seconds`    | 8.0     | **max_words = 2.5 × 8 = 20 words per speaking scene**             |
| `min_scene_seconds`    | 6.0     | Interpolated as `min_words` but **not enforced** — advisory       |
| `broll_length_seconds` | 1       | Requested B-Roll length (overridden to 4s for an `opening` plate) |

`max_words` is derived identically in `build_director_prompt` and
`validate_blueprint`. If they diverged, the retry loop couldn't converge — the model
would be told one ceiling and judged against another.

Raising `max_scene_seconds` past 8 is not enough on its own: Veo only emits 4/6/8s
clips (`VEO_ALLOWED_SECONDS`) and `scene_gen.VEO_VIDEO_SECONDS` hard-codes 8s for
speaking scenes.

---

## Lifecycle on disk

```
blueprint.json               director output, placeholder char_ids
   │  POST /pipeline/select
   ├──▶ blueprint.placeholder.json    snapshot, written once, always patched FROM
   └──▶ blueprint.json                rewritten with resolved library ids
```

`patch_run` always patches from the snapshot, so re-selecting characters is safe.
`POST /pipeline/direct` with `force=true` deletes both the snapshot and
`selections.json`, because a selection made against the old blueprint may reference
placeholders the new one doesn't have.

**What gets rewritten:** `scenes[].characters`, `scenes[].audios[].char_id`,
`start_frame[].reference`.
**What does not:** `scenes[].prompt`, `start_frame[].prompt`. See
[open-questions.md](open-questions.md#1-decided--placeholder-char_ids-leak-into-veo-prompts).

---

## Validation, in order of when it runs

| Where                             | Checks                                                                           | On failure                      |
| --------------------------------- | -------------------------------------------------------------------------------- | ------------------------------- |
| `parse_blueprint`                 | JSON parses (fence-tolerant), pydantic shape                                     | fed back to the model, retry    |
| `validate_blueprint`              | ~25 semantic rules                                                               | fed back to the model, retry    |
| exhausted retries                 | —                                                                                | `RuntimeError` → HTTP 502       |
| `/pipeline/select`                | char/audio ids exist, placeholders covered exactly once, one voice per character | HTTP 422                        |
| `patch_blueprint._check_resolved` | post-patch: `character_count`, voice stability, no orphans                       | 422 + selection rolled back     |
| `scene_gen.route`                 | task/model coherent **and** matching the prefix                                  | `VeoRouteError` before spending |
| `resolve_voices`                  | every speaking scene has a resolvable voice                                      | 422 before spending             |

The two `character_count` checks catch different things. In `validate_blueprint` it's
"did the director count its own placeholders correctly". In `_check_resolved` it's
"did the user's selection collapse two roles onto one character", which changes the
distinct count — the error message says so explicitly.
