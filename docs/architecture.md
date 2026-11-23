# Architecture

How a training document becomes a video, and which file does what.

> Written from the code in `new_code_v2` as of 2026-07-30. Where something is an
> inference rather than something the code states, it says **Assumption**.

---

## 1. The shape of the thing

Atlas is a **FastAPI app with no database**. Eight layers, each exposed as one
endpoint, each reading what the previous one left on disk.

```
                       ┌─────────────────────────────────┐
   source.txt  ────────▶  L1  config + upload            │
   config.json ────────▶      main.py                    │
                       └───────────────┬─────────────────┘
                                       │
                       ┌───────────────▼─────────────────┐
                       │  L2  DIRECTOR                   │  Gemini 2.5 Flash
                       │      pipeline/director.py       │  ← the only LLM call
                       └───────────────┬─────────────────┘    that reasons
                                       │
                              blueprint.json  ◀── everything downstream reads this
                                       │
                       ┌───────────────▼─────────────────┐
                       │  L3  character + voice choice   │  no network
                       │      libraries.py               │  (except live voice list)
                       │      patch_blueprint.py         │
                       └───────────────┬─────────────────┘
                                       │
                              blueprint.json (placeholders resolved)
                                       │
                       ┌───────────────▼─────────────────┐
                       │  L4  ASSETS                     │  OpenAI gpt-image-2    
                       │      pipeline/image_gen.py      │
                       └───────────────┬─────────────────┘
                                       │
                            backgrounds/*.png, start_frame/*.png
                                       │
              ┌────────────────────────▼────────────────────────┐
              │  L5 ⇄ L6  INTERLEAVED, ONE SCENE AT A TIME      │
              │                                                 │
              │   L5 pipeline/scene_gen.py        Veo 3.1       │
              │   L6 pipeline/sts_elevenlabs.py   ElevenLabs    │
              │      pipeline/external_tts.py     Azure (alt)   │
              └────────────────────────┬────────────────────────┘
                                       │
                              veo_raw/*.mp4  (pristine)
                              scenes/*.mp4   (voiced + trimmed)
                              tts/*_timings.json
                                       │
                       ┌───────────────▼─────────────────┐
                       │  L7  MUSIC                      │  Google Lyria 3 Clip
                       │      pipeline/music_gen.py      │
                       └───────────────┬─────────────────┘
                                       │
                              music_bg/track.mp3
                                       │
                       ┌───────────────▼─────────────────┐
                       │  L8  RENDER                     │  local ffmpeg only
                       │      pipeline/renderer.py       │
                       └───────────────┬─────────────────┘
                                       │
                                  final.mp4
```

Two structural facts explain most of the code:

**The run directory is the state.** No DB, no job queue, no in-memory session.
`run_id` names a folder; each endpoint's precondition is "does the file the
previous stage should have written exist". Every `409` in `main.py` is an ordering
constraint expressed as a file check. This is why the pipeline is resumable by
just POSTing the next stage, and why nothing ever regenerates automatically — a
stage that costs money is gated behind an explicit flag.

That same property is what makes resuming _within_ a stage possible: since the
files are the state, "what still needs generating" is answerable by looking at
the directory. `/pipeline/assets` and `/pipeline/scenes` take `resume=true`,
which keeps everything already on disk and generates only the rest — the
recovery path for a stage that died at scene 7 of 9. `force=true` is the
opposite and regenerates everything; passing both is a 422. Resume reads file
existence and nothing else, so it cannot notice that a blueprint changed
underneath it — edit a prompt and you want force.

**`blueprint.json` is the contract.** L2 is the only layer that makes creative
decisions. Everything after it is mechanical execution of the blueprint. If you
want to change what videos look like, you almost always change L2's prompt or
validator, not the layers downstream.

---

## 2. Module map

```
main.py                          the 12 endpoints; precondition checks; error mapping
app/
  config.py                      DirectorConfig knobs + the ONLY Gemini key read
  schemas.py                     blueprint v3 models AND the HTTP request models
  libraries.py                   character/audio index load + build + validate (has a CLI)
  costs.py                       the ONLY place spend is priced and recorded
  paths.py                       code-adjacent asset paths, resolved from __file__

  pipeline/
    director.py        L2        DIRECTOR_RULES, validate_blueprint, the retry loop,
                                 the reference_from grammar, blueprint_type rules
    patch_blueprint.py L3        placeholder char_ids -> real library ids (has a CLI)
    image_gen.py       L4        backgrounds then start frames (order matters)
    scene_gen.py       L5        Veo routing, the L5⇄L6 loop, cost logging
    external_tts.py    L6        Azure path: TTS per phrase + diarize + align
    sts_elevenlabs.py  L6        ElevenLabs path: voice conversion (DEFAULT)
    audio_providers.py L6        provider registry + voice resolution
    video_providers.py L5        provider registry (minimax_h3 | gemini)
    music_gen.py       L7        read music_style, call Lyria, write track.mp3
    renderer.py        L8        merge, title, transitions, subtitles, music mix

  clients/                       every external API boundary lives here
    video_veo_client.py          Veo via google-genai; VeoJobSpec; cost estimate
    video_minimax_h3_client.py   MiniMax H3 on the company GPU (sd-server, over Tailscale)
    tts_azure_client.py          Azure Speech -> 24kHz mono WAV
    sts_elevenlabs_client.py     ElevenLabs STS + paginated voice list
    music_lyria_client.py        Lyria via google-genai

  utils/
    ffmpeg_utils.py              the ONLY place ffmpeg/ffprobe are invoked
    retry.py                     the one retry-with-backoff loop
    text_utils.py               phrase splitting + lenient JSON parsing
    io_utils.py                  atomic writes (never a half-written blueprint)

bench/                           scratch tooling, not part of the pipeline
  director_smoke.py              one live director call, print + validate
  inspect_veo_payloads.py        show exactly what would go to Veo, no network
  diarize_local.py               measure diarization accuracy locally (free)

asset/
  fonts/Fredoka-SemiBold.ttf     cartoon title font
  transitions/                   .mov overlays + index.json
```

### Where external calls happen

Every billable call is in exactly one of these places. Nothing else touches the
network.

| Provider                    | Called from                                           | Cost per run                                    |
| --------------------------- | ----------------------------------------------------- | ----------------------------------------------- |
| Gemini 2.5 Flash (director) | `director.GeminiDirector.complete`                    | 1 call per attempt, up to `max_retries + 1`     |
| OpenAI gpt-image-2          | `image_gen.OpenAIImageClient.generate`                | 1 per background + 1 per start frame (+ retries) |
| Veo 3.1 Fast / Lite         | `video_veo_client.GeminiVeoClient.generate`           | 1 per scene                                     |
| ElevenLabs STS              | `sts_elevenlabs_client.ElevenLabsSTSClient.convert`   | 1 per speaking scene                            |
| Azure TTS                   | `tts_azure_client.AzureTTSClient.tts`                 | 1 **per phrase** per speaking scene             |
| Gemini 2.5 Flash (diarize)  | `external_tts.GeminiDiarizer.diarize`                 | 1 per speaking scene, **on both audio paths**   |
| Google Lyria 3 Clip         | `music_lyria_client.GeminiLyriaClient.generate_music` | 1 per run                                       |

Veo spend is recorded in `cost_log.jsonl`. Nothing else is. `/pipeline/scenes`
returns the sum of that file, so the reported `total_cost_usd` is **video only** —
it excludes image, TTS, diarization and music.

---

## 3. The data flow, stage by stage

### L1 — config + source

`POST /pipeline/config` mints `run_id = run_<8 hex>`, creates the directory, and
writes `config.json`. `POST /pipeline/upload` streams a `.txt` to `source.txt`.

The upload path is stricter than it looks: 50MB cap, incremental UTF-8 decode so
binary is rejected mid-stream, NUL-byte rejection, and a final `decode(b"",
final=True)` to catch a truncated multibyte tail. Written to a temp file and
`os.replace`d, so a rejected upload leaves nothing behind. Only `.txt` is
accepted — PDF/DOCX extraction (`extract.py`) was retired.

`_run_dir()` guards path traversal: `run_id` must match `^run_[a-z0-9]{8}$` **and**
the resolved directory's parent must be exactly `STORAGE_ROOT`.

### L2 — the director

This is the interesting layer. `POST /pipeline/direct`:

1. `build_director_prompt` interpolates the pacing knobs into `DIRECTOR_RULES`
   (system half) and puts the source document in the user half.
2. `GeminiDirector.complete` calls `gemini-2.5-flash`, temperature 0.4, JSON
   response mime type.
3. `parse_blueprint` — `parse_json_lenient` strips a stray ``` fence, then pydantic
   validates the shape.
4. `validate_blueprint` runs the ~25 semantic rules pydantic can't express.
5. **If there are problems, the problem list is appended to the system prompt and
   the model is asked again.** Up to `max_retries` more times.

Step 5 is the design. Gemini will not satisfy a spec this detailed first time, so
rather than prompting harder, the validator produces a precise, actionable error
list and feeds it back. The practical implication:

> A rule stated in `DIRECTOR_RULES` but not checked in `validate_blueprint` is a
> suggestion the model can ignore, because nothing will ever tell it that it did.

Then two post-processing steps run in `main.run_director` before the file is
written, so **`blueprint.json` is not byte-identical to what Gemini returned**:

- `enforce_opening_length` — on an `opening` run, forces the opening B-Roll to
  4.0s so it can carry the title card. No-op on `summary`.
- `derive_style_type` — keyword-maps the free-text `style` sentence into
  `realism | cartoon | semi-realism`, stored on the blueprint.

Regenerating a blueprint deletes `blueprint.placeholder.json` and
`selections.json`, because a selection made against the old blueprint may
reference placeholder ids the new one doesn't have.

### L3 — character and voice selection

The director never designs a character. It decides **how many** are needed and
**what role each plays**, emitting semantic placeholders (`char_narrator`,
`char_hr_rep`). The user then picks real characters from a library.

- `GET /pipeline/characters` reads `character_library/index.json`.
- `GET /pipeline/audios` reads `audio_library/index.json`, or with
  `voice_source=elevenlabs` calls the live paginated ElevenLabs voice list.
- `POST /pipeline/select` validates the mapping and writes `selections.json`, then
  immediately calls `patch_blueprint.patch_run` **in the same request**.

That combination is transactional on purpose: if patching fails, `selections.json`
is deleted and a 422 is returned, so you never end up with a selection whose
blueprint wasn't resolved.

`patch_run` snapshots the pristine director output to
`blueprint.placeholder.json` on first call and **always patches from the
snapshot**. That's what makes re-selecting safe.

Validated here: every char_id exists in the library, every blueprint placeholder is
assigned exactly once, no extras, and one character never maps to two voices.

> `apply_selection` rewrites ids in `scene.characters`, `scene.audios[].char_id`
> and `start_frame[].reference`. It does **not** rewrite prompt prose. See
> [open-questions.md](open-questions.md#1-decided--placeholder-char_ids-leak-into-veo-prompts).

### L4 — asset generation

`POST /pipeline/assets` generates **backgrounds first, then start frames** — a hard
dependency, since each start-frame prompt attaches a background PNG (and the
group's character PNG) as reference images. There is one start frame per group
(the scenes between two B-Rolls).

The style is applied here and nowhere else. The director is forbidden from writing
art-style words into individual prompts (AI RULE #7), so the art direction lives
once on `bp['style']` and `_compose_prompt` prepends it at generation time:

```python
final_prompt = f"{bp['style']}, {entry['prompt']}"
```

Change `bp['style']` and every image in the run changes together.

**Characters are not generated.** The user's library PNG is attached as a reference
to the start frame of each group the character fronts. So character consistency
rests on a two-hop chain — library PNG → start frame → video clip — and neither hop
is verified.

### L5 ⇄ L6 — scenes and voice, interleaved

`POST /pipeline/scenes` runs both layers in one loop, alternating per scene.

**Routing** is derived entirely from the `reference_from` prefix
(`director.ROUTE_BY_PREFIX`), and `scene_gen.route()` re-checks it at generation
time — the last gate before a billable call:

| `reference_from`     | Veo task           | Model            | Input attached                       |
| -------------------- | ------------------ | ---------------- | ------------------------------------ |
| `start_frame:<sf_id>`| image-to-video     | Veo 3.1 **Lite** | `start_frame/<id>.png` as the seed   |
| `background:<bg_id>` | image-to-video     | Veo 3.1 **Lite** | `backgrounds/<id>.png` as the seed   |
| `scene:<prev_id>`    | image-to-video     | Veo 3.1 **Lite** | last frame of `scenes/<prev>.mp4`    |

**Generation lengths are fixed and differ from blueprint lengths.** Veo only emits
4/6/8s clips. Speaking scenes are always generated at 8s and trimmed by L6 to
where the voice ends. B-Roll is generated at 4s and trimmed down to
`scene.length`.

**The prompt sent to Veo is not the blueprint prompt.** For speaking scenes,
`compose_veo_prompt` appends:

```
Audio sync: The character says "<scene.text>" Once the line is finished the character stays silent with their mouth closed.
```

Which looks contradictory, since the scenes layer then mutes Veo's audio. It isn't: Veo needs
the words in order to animate the mouth to them, and — on the Azure path — its
native audio is the timing reference the overlay aligns against.

**Why the loop can't be parallelised.** A `scene:<prev>` scene seeds Veo with the
last frame of the previous clip, and it must be the last frame of the **trimmed**
clip. Veo pads speaking scenes with trailing silence, and the character drifts
during it; seeding from the untrimmed frame would start the next scene from a
moment the viewer never sees, and the cut would jump. So the order is strictly
generate → scene layer → generate, and scenes layer must finish writing `scenes/<id>.mp4` before the
next iteration starts.

**`veo_raw/` exists to make L6 re-runnable.** Every clip is saved pristine before
anything touches it, so `tts_only=true` replays the whole audio layer with zero
video spend.

#### The two audio paths

**ElevenLabs (default, `tts_provider=elevenlabs`)** — `sts_elevenlabs.py`. Extract
Veo's native audio, send it to speech-to-speech conversion, mux the result back.
Because STS preserves the source timing, lip-sync is inherited for free. There is
no alignment step and nothing to get wrong.

**Azure (`tts_provider=azure`)** — `external_tts.py`. Split `scene.text` on
punctuation, synthesise each phrase, trim its silence, diarize Veo's native audio
for per-word timings, then place each phrase at the diarized start of its first
word — matched by **content** via `difflib`, not by position. Then mute Veo, mux
the assembled track, trim to the last phrase end.

The content matching matters: an earlier positional implementation shifted every
later phrase when diarization dropped a single word, and the drift compounded.
When fewer than 50% of words match, the whole alignment is discarded and phrases
are placed back-to-back from the speech onset — intelligible, but lip-sync is
gone.

> Both paths call the diarizer. On the ElevenLabs path it's used **only** to time
> subtitles, never to place audio, because STS synthesises no individual phrases
> so there's no per-phrase duration to measure. The saving over Azure is in
> reliability, not in calls.

Either way, `tts/<id>_timings.json` is the handoff to L8's subtitle builder. Without
it, L8 falls back to one caption for the whole scene.

Voices are resolved and **fully pre-validated before any credits are spent**
(`audio_providers.resolve_voices`). With `voice_source=index`, `audio_id` looks up
the library entry and `provider.voice_field` picks the right column (`voice_name`
for Azure, `voice_id_elevenlabs` for ElevenLabs) — the two occupy different ID
namespaces. With `voice_source=elevenlabs`, the `audio_id` **is** the voice_id and
there's no lookup. Mixing `voice_source=elevenlabs` with `tts_provider=azure` is
rejected at the endpoint, since an ElevenLabs voice_id is meaningless to Azure.

### L7 — music

`POST /pipeline/music` reads `blueprint.music_style` verbatim, wraps it in
`build_bed_prompt` (adds "instrumental only" and "loopable bed under narration"),
calls Lyria, and writes `music_bg/track.mp3`. If Lyria returns something other
than mp3, ffmpeg transcodes it via stdin. One track per run. Lyria returns a fixed
~30s clip — `duration_s` is a hint only.

### L8 — render

`POST /pipeline/render`. All local ffmpeg, no credits, freely re-runnable. Five
passes in a fixed order, each writing its own inspectable intermediate:

```
scenes/*.mp4 ──merge──▶ merged.mp4 ──title──▶ merged_title.mp4
    ──transitions──▶ merged_trans.mp4 ──subtitles──▶ merged_subs.mp4
        ──music (in place)──▶ copy ──▶ final.mp4
```

The order is load-bearing:

- **Title before transitions** so an overlay can sweep across the finished card.
- **Transitions before subtitles** so captions stay readable on top.
- **Music last** because it needs the final duration to loop against.

Every pass except merge returns its input unchanged when it has nothing to do, so
the chain stays valid for a run with no title, no transition assets and no music.

**Merge** probes all scenes once (`scene_probes`, shared by every later pass) and
stream-copies if they're uniform. In a normal run they aren't — speaking scenes
carry AAC and B-Roll doesn't — so each is normalised to the first scene's geometry
(scale + pad, never crop) with silent AAC added where missing, then concat-copied.

**Title** applies only when `blueprint_type == "opening"` and there's both a title
and an opening B-Roll. This is the one place `style_type` changes the output:
`cartoon` drops the scrim and animates per letter via one `drawtext` per glyph;
`realism`/`semi-realism` dim the plate 30% and move the title as one block.

**Transitions** overlay `.mov` clips from `asset/transitions/` **above** the merged
video at role-driven cut points, each straddling its cut 50/50. Nothing is inserted
into the timeline, so total duration is unchanged and every previously-computed cue
time stays valid. Placements come from `broll_role` — `opening` and `closing` each
mark a boundary. Which overlay is picked is seeded from a SHA-256 of `source.txt`,
so the same input file always produces the same picks.

**Subtitles** — `build_srt` converts per-scene L6 timings onto the merged timeline
using **probed** durations (not blueprint lengths), floors every cue at 500ms, and
clamps overlaps so two captions are never on screen at once. Four burn methods;
default `boxed` renders each cue to a transparent PNG with Pillow because neither
libass nor drawtext can draw rounded corners.

**Music** loops `track.mp3` to the video duration with chained `acrossfade` joins
(a plain loop clicks at the seam) and mixes it under the narration at −20dB with
`amix normalize=0` — without that flag, amix would attenuate the voice by the
number of inputs and the narration would duck whenever music appeared.

---

## 4. Cross-cutting conventions

**Client seams.** Every networked client is built through a `_*_client()` function
in `main.py`, lazily imported. The API boots with no keys configured; a missing key
surfaces as `503`, not a crash at import. Tests override the seam.

**Provider registries.** `PROVIDERS` (audio) and `VIDEO_PROVIDERS` (video) map a
name to a builder, so endpoints look up a string and never branch on it. Adding a
provider is one module plus one dict entry.

**One retry loop.** `utils/retry.py`. Note the two calling conventions are
different and preserved deliberately: Veo/Lyria pass `max_retries` as the _total_
attempt count and retry only their `Transient*` subclass; image gen passes
`max_retries + 1` and retries _any_ exception.

**Transient classification** is substring matching on the error message
(`"500"`, `"502"`, `"rate"`, `"timeout"`, `"unavailable"`). Crude, but it's the only
thing that works uniformly across three SDKs.

**One ffmpeg call site.** `ffmpeg_utils.run_ffmpeg` is the only place ffmpeg is
invoked. It raises `FfmpegError` on non-zero exit _or_ a missing expected output.
The two inspecting calls (`detect_speech_end`, `has_filter`) pass their own flags
because they need info-level stderr.

**Atomic writes.** `io_utils` for anything a later stage reads, so a reader can
never observe a half-written `blueprint.json`.

**Error type per layer**, mapped to a status code at the endpoint: `AssetError`,
`VeoError`, `L6Error`, `TTSError`, `STSError`, `MusicError`, `RenderError`,
`PatchError`, `FfmpegError`.

---

## 5. Reading order for a new contributor

1. `main.py` module docstring — the run directory layout and the gates.
2. `app/schemas.py` — the vocabulary.
3. `app/pipeline/director.py` — `DIRECTOR_RULES`, then `validate_blueprint`, then
   `direct`. Budget real time; this is the layer.
4. Run `python -m bench.inspect_veo_payloads` against an existing run in
   `_storage/` to see exactly what Veo would receive. Free, no network.
5. `app/pipeline/scene_gen.py` — `run_all`, for the L5⇄L6 interleave.
6. `app/pipeline/renderer.py` — `render_merge_and_subs`, then follow the passes.

Then read [open-questions.md](open-questions.md) before changing anything.
