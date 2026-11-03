# Setup and running the pipeline

---

## 1. What you need

### Python

**3.11 or newer.** 3.13 is what this has actually been run on (the `__pycache__`
tags in the repo are `cpython-313` and `cpython-314`).

Not optional. The pydantic models use `X | None` in annotations that pydantic
resolves at class-creation time, which is a `TypeError` on 3.9/3.10 despite the
`from __future__ import annotations` at the top of each file. macOS system Python
(3.9.6) **will not run this.**

### ffmpeg and ffprobe

Both must be on `PATH`. `ffmpeg_utils._require` raises a clear error if not.

```bash
brew install ffmpeg          # macOS
apt install ffmpeg           # Debian/Ubuntu
```

Check for **libass**, which the `libass` subtitle method needs:

```bash
ffmpeg -hide_banner -filters | grep subtitles
```

Not required — the default `boxed` method uses Pillow overlays instead and doesn't
need it.

### Fonts

Latin-script subtitles fall back through `_FONT_CANDIDATES` (Arial/Helvetica on
macOS, DejaVu on Linux). Non-Latin languages need a script-capable font installed —
CJK, Thai, Arabic and Devanagari have candidate lists in `renderer._SCRIPT_FONTS`.
If none is present you get a non-fatal `font_warning` on the render response and
blank boxes in the video.

`asset/fonts/Fredoka-SemiBold.ttf` ships in the repo and is used for cartoon titles.

### What to write in `config.language`

Free text — Gemini reads it raw. **One place the spelling matters:** font
selection does a case-insensitive _substring_ match against these keywords.

| Script     | Keywords that match                                      |
| ---------- | -------------------------------------------------------- |
| CJK        | `japanese`, `chinese`, `mandarin`, `cantonese`, `korean` |
| Thai       | `thai`                                                   |
| Arabic     | `arabic`, `urdu`, `persian`, `farsi`                     |
| Devanagari | `hindi`, `devanagari`, `nepali`, `marathi`               |

So write **`"Chinese"`** (or `"Simplified Chinese"`), **not** `"中文"` or
`"zh-CN"` — neither contains a keyword, so a Latin font gets picked and the
subtitles render as blank boxes with no warning. Latin-script languages like
Indonesian need no keyword; write whatever reads well.

Full detail, including what else to expect on a first CJK run, is in
[open-questions #18](open-questions.md#18-fixed--what-_norm-does-to-non-latin-scripts-and-what-it-actually-breaks).

---

## 2. Dependencies

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` is **derived from the imports** — the lower bounds are the
minimum the code needs, and there are no upper bounds, because a bound nobody has
tested is a guess that ages badly. Install from this file.

`requirements.lock.txt` (added 2026-08-02) is the other half: a full snapshot of
the environment the pipeline has actually been run end to end in, Python 3.14.6
on macOS arm64. Reach for it when something breaks after an upgrade and you need
a known-good set to bisect against — not for day-to-day installs, since it pins
the whole shared environment including packages Atlas never imports.

**Regenerating it, if you are in a conda environment — and this repo is:**

```bash
python -m pip list --format=freeze > requirements.lock.txt   # correct
pip freeze > requirements.lock.txt                            # WRONG here
```

`pip freeze` writes conda-installed packages as
`fastapi @ file:///Users/ec2-user/croot/fastapi-split_.../work` — a build path
from the machine that made the conda package, which exists nowhere on yours.
Nine packages come out that way, `fastapi` and `pydantic` among them, so the
resulting file will not install. `pip list --format=freeze` emits
`name==version` for all of them.

Regenerate only after a deliberate upgrade you have then verified with a full
live run.

What's in it, and why:

| Package                          | Needed for                                                                                                                                                                                                                                                                           |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `fastapi`, `uvicorn[standard]`   | the API and the server that runs it                                                                                                                                                                                                                                                  |
| `python-multipart`               | **not imported directly** — FastAPI requires it for `Form()`/`UploadFile` in `/pipeline/upload`. Missing it fails at request time, not import time.                                                                                                                                  |
| `pydantic>=2.7`                  | v2 only (`model_validate`, `model_dump`, `ConfigDict`)                                                                                                                                                                                                                               |
| `python-dotenv`                  | `app/config.py` loads `.env` at import                                                                                                                                                                                                                                               |
| `google-genai`                   | director, Gemini image, Veo, Lyria **and** diarization                                                                                                                                                                                                                               |
| `openai`                         | PDF review script synthesis, visual-slide selection, and vision-based crop-coordinate suggestions                                                                                                                                                                                     |
| `elevenlabs`                     | speech-to-speech + the paginated voice list                                                                                                                                                                                                                                          |
| `azure-cognitiveservices-speech` | only for `tts_provider="azure"`                                                                                                                                                                                                                                                      |
| `requests`                       | Kie video requests and downloading S3/CloudFront PDFs for review                                                                                                                                                                                                                      |
| `Pillow`                         | `boxed` subtitles and cartoon-title glyph metrics                                                                                                                                                                                                                                    |
| `PyMuPDF`                        | PDF text extraction, slide rendering, and local source-image cropping                                                                                                                                                                                                                 |
| `faster-whisper`                 | **optional.** Only for `diarizer="whisper"` and `bench/diarize_local.py`. Commented out in `requirements.txt`: the import is inside `diarize()`, so the pipeline runs fine without it, and installing it pulls in ctranslate2 + av + onnxruntime and downloads a model on first use. |

### Running the tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

276 tests, about a quarter of a second, **no network, no API keys, no ffmpeg, no
credits**. Safe to run on every save — that's the point. It covers the pipeline's
decision-making logic: blueprint validation, placeholder resolution, Veo routing,
audio alignment, subtitle timing, cost recording, resume semantics, and whether
request flags actually reach the clients they configure.

It needs the runtime dependencies installed even though it never calls anything,
because it imports the real modules and `app/config.py` imports `google.genai` at
module load.

If you're mid-refactor, this replaces running the pipeline end to end after every
step. What it does **not** cover: the endpoints, the ffmpeg passes in the
renderer, and every client — see
[open-questions #7](open-questions.md#7-partly-fixed--no-ci-integration-coverage-is-thin).

**Not** dependencies, despite appearances:

- **`moviepy`** — was imported only by `app/eleven_labs.py`, which was dead code
  and has been deleted ([#10](open-questions.md#10-fixed--appeleven_labspy-was-dead-code)).
  The pipeline drives ffmpeg directly through `app/utils/ffmpeg_utils.py`.
- **`boto3`** and the DeepSeek SDK — legacy `.env` entries exist, but the
  application does not read them. Storage is always local
  ([#26](open-questions.md#26-on-hold--env-carries-keys-nothing-reads)).

`ffmpeg` and `ffprobe` are not pip packages — see §1.

### Version-sensitive notes

- **`google-genai`** is the sharp one. Three surfaces here are version-dependent and
  the code says so in comments: Veo model ids (`veo-3.1-fast-generate-preview`,
  `veo-3.1-lite-generate-preview`), the `generate_videos` / `operations.get`
  shape, and how an image comes back from `generate_content`
  (`part.inline_data.data`). If a live run fails with an attribute error inside a
  client, this is why. Pin whatever version you verify against.
- **`elevenlabs`** — the client is accessed via `voices.search(...)` and
  `speech_to_speech.convert(...)`. `_get()` in the STS client deliberately handles
  both object and dict responses because the SDK has changed shape across versions.
- **Pillow** — `ImageDraw.rounded_rectangle`, `textbbox`, `font.getlength`. All
  present in Pillow 9+.
- **`faster-whisper`** — only reached through `diarizer="whisper"`. The import is
  inside `WhisperDiarizer.diarize()`, so a missing package fails that request
  rather than the whole app.

---

## 3. Environment

Copy `.env` and fill it in. **Never commit it** — it's already in `.gitignore`.

```bash
# Required for everything (director, images, Veo, Lyria, diarization)
HEYHI_GEMINI_API_KEY=

# Required for the default audio path
HEYHI_ELEVENLABS_API_KEY=

# Required only for tts_provider=azure
HEYHI_AZURE_SPEECH_KEY=
HEYHI_AZURE_SPEECH_REGION=

# Required only for video_provider=kieai (testing)
KIE_API_KEY=

# Required only for PDF review script, visual selection, and AI crop suggestions
OPENAI_API_KEY=
```

`load_dotenv()` runs when `app.config` is imported, so the API picks these up
automatically. Keys are read at exactly one point each and are never logged or
persisted. A missing key surfaces as HTTP **503** from the endpoint that needs it,
not as a crash at boot — clients are built lazily behind seams for that reason.

### Optional overrides

| Variable                | Default                               | Effect                      |
| ----------------------- | ------------------------------------- | --------------------------- |
| `ATLAS_STORAGE_ROOT`    | `_storage/atlas_storyboard`           | Where run directories live  |
| `ATLAS_CHAR_LIB`        | `<storage_root>/../character_library` | Character library           |
| `ATLAS_AUDIO_LIB`       | `<storage_root>/../audio_library`     | Audio library               |
| `ATLAS_TRANSITIONS_DIR` | `asset/transitions`                   | Transition overlays         |
| `ATLAS_IMAGE_BACKOFF`   | 2.0                                   | Image-gen retry backoff (s) |
| `ATLAS_VEO_BACKOFF`     | 2.0                                   | Veo retry backoff (s)       |
| `ATLAS_VEO_POLL_S`      | 10.0                                  | Veo poll interval (s)       |
| `ATLAS_LYRIA_BACKOFF`   | 2.0                                   | Lyria retry backoff (s)     |
| `OPENAI_PDF_MODEL`      | `gpt-5.4-mini`                        | PDF review AI model         |

The legacy `HEYHI_OPENAI_API_KEY` name is still unused. The PDF review workflow
uses the standard `OPENAI_API_KEY` name instead. `HEYHI_DEEPSEEK_API_KEY`, the
credential-based S3 group and `HEYHI_DIRECTOR_PROVIDER` /
`HEYHI_DIRECTOR_MODEL` also remain unused. PDF review can download a public or
presigned S3 URL, but all run artifacts are still stored locally.

### Working directory matters

`ATLAS_TRANSITIONS_DIR` and the cartoon title font path both default to
**relative** paths. Start the server from anywhere but the repo root and:

- transitions silently stop appearing (index not found → empty pool → pass skipped);
- cartoon-style renders **fail** with `RenderError: title font not found`.

Either run from the repo root or set absolute paths.

---

## 4. Asset libraries

Both live **outside** the run tree and are shared by all runs. Default layout:

```
_storage/
  character_library/
    char_female_casual_realism.png
    ...
    index.json
  audio_library/
    en_us_female_warm.wav
    ...
    index.json
  atlas_storyboard/          <- run directories
```

`libraries.py` has a CLI for both:

```bash
# Scan PNGs, scaffold/merge index.json, validate.
# Idempotent — never clobbers human-written description/tags.
python -m app.libraries characters --root _storage/character_library

# Validate the audio index (--strict-files also requires every wav to exist)
python -m app.libraries audios --root _storage/audio_library
```

Characters are **scan-and-merge**: the PNGs on disk are the source of truth, and
`description`/`tags` are preserved from the existing index. Audio is a **union
merge keyed by filename**, because `voice_name`/`language` can't be derived from a
wav — authored entries survive even when their sample hasn't been generated yet
(warned), and an orphan wav gets a stub to fill in.

`index.json` entries:

```json
// character_library
{ "char_id": "char_female_casual_realism_1",
  "description": "a young woman with long dark hair, dressed casually...",
  "tags": ["female", "casual", "realism"],
  "image_path": "char_female_casual_realism.png" }

// audio_library
{ "audio_id": "en_us_female_warm",
  "voice_name": "en-US-JennyNeural",          // Azure short name
  "voice_id_elevenlabs": "cgSgspJ2msm6clMCkdW9",   // optional; null = Azure-only
  "language": "en-US",                         // BCP-47, validated
  "sample_path": "en_us_female_warm.wav" }
```

Both ids must be `snake_case` (`^[a-z][a-z0-9_]*$`). Paths are **relative to the
library root** so the tree can move without rewrites.

Note the two ID namespaces. `voice_name` is Azure's, `voice_id_elevenlabs` is
ElevenLabs'. `audio_providers` picks the right column via `provider.voice_field`. An
entry with no `voice_id_elevenlabs` is rejected at `/pipeline/scenes` with
`tts_provider=elevenlabs` — before any spend.

`asset/transitions/index.json` is authored by hand, not scanned. A listed file that
doesn't exist raises `RenderError`; a missing index is fine (transitions skipped).

---

## 5. Running it

```bash
uvicorn main:app --reload --port 8000        # from the repo root
```

Interactive docs at `http://localhost:8000/docs`. Liveness at `/health`.

### End to end

Stages marked 💸 spend money.

```bash
BASE=http://localhost:8000

# ── L1 ────────────────────────────────────────────────────────────
RUN=$(curl -s -X POST $BASE/pipeline/config \
  -H 'Content-Type: application/json' -d '{
    "blueprint_type": "opening",
    "language": "English",
    "audience": "new employees",
    "depth": "standard",
    "style": "professional realism art style",
    "objective": "explain the annual leave policy",
    "aspect_ratio": "16:9"
  }' | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "run_id = $RUN"

curl -s -X POST $BASE/pipeline/upload \
  -F "run_id=$RUN" -F "file=@bench/sources/hr_policies.txt"

# ── L2  💸 1 Gemini call per attempt ──────────────────────────────
curl -s -X POST $BASE/pipeline/direct \
  -H 'Content-Type: application/json' \
  -d "{\"run_id\":\"$RUN\",\"max_retries\":2}"
```

**Stop here and read the blueprint.** This is the human gate — everything after it
spends real money on whatever the blueprint says.

```bash
python3 -m json.tool _storage/atlas_storyboard/$RUN/blueprint.json | less

# Free dry run: exactly what each scene would send to Veo
python -m bench.inspect_veo_payloads --run _storage/atlas_storyboard/$RUN
```

Continue:

```bash
# ── L3 ────────────────────────────────────────────────────────────
curl -s $BASE/pipeline/characters
curl -s "$BASE/pipeline/audios?voice_source=index"
# or the live ElevenLabs list:
curl -s "$BASE/pipeline/audios?voice_source=elevenlabs"

# One selection per placeholder in the blueprint. Read them off with:
#   jq -r '[.scenes[].characters[]] | unique[]' blueprint.json
curl -s -X POST $BASE/pipeline/select \
  -H 'Content-Type: application/json' -d "{
    \"run_id\": \"$RUN\",
    \"voice_source\": \"index\",
    \"selections\": [
      {\"placeholder_id\": \"char_narrator\",
       \"char_id\": \"char_female_casual_realism_1\",
       \"audio_id\": \"en_us_female_warm\"}
    ]
  }"

# ── L4  💸 1 image per background + per storyboard ────────────────
curl -s -X POST $BASE/pipeline/assets \
  -H 'Content-Type: application/json' -d "{\"run_id\":\"$RUN\"}"
```

**Look at the PNGs before continuing** — `backgrounds/` and `storyboard/`. This is
where character likeness drift shows up, and it's much cheaper to catch here than
after nine Veo calls.

```bash
# ── L5 + L6  💸 the expensive one: 1 Veo call per scene ───────────
curl -s -X POST $BASE/pipeline/scenes \
  -H 'Content-Type: application/json' -d "{
    \"run_id\": \"$RUN\",
    \"tts_provider\": \"elevenlabs\",
    \"video_provider\": \"gemini\"
  }"

# ── L7  💸 1 Lyria call ───────────────────────────────────────────
curl -s -X POST $BASE/pipeline/music \
  -H 'Content-Type: application/json' -d "{\"run_id\":\"$RUN\"}"

# ── L8  free, re-runnable ─────────────────────────────────────────
curl -s -X POST $BASE/pipeline/render \
  -H 'Content-Type: application/json' -d "{\"run_id\":\"$RUN\"}"

open _storage/atlas_storyboard/$RUN/final.mp4
```

### Iterating cheaply

The flags that matter when you're debugging:

```bash
# Re-run ONLY the audio layer from the preserved pristine clips.
# No Veo call, no video spend. Requires a prior full run (veo_raw/ must exist).
curl -X POST $BASE/pipeline/scenes -H 'Content-Type: application/json' \
  -d "{\"run_id\":\"$RUN\",\"tts_only\":true,\"tts_provider\":\"elevenlabs\"}"

# Re-render. Free, always safe.
curl -X POST $BASE/pipeline/render -H 'Content-Type: application/json' \
  -d "{\"run_id\":\"$RUN\",\"force\":true}"

# Regenerate a billable stage. Always explicit — nothing auto-regenerates.
curl -X POST $BASE/pipeline/assets -H 'Content-Type: application/json' \
  -d "{\"run_id\":\"$RUN\",\"force\":true}"

# PICK UP A STAGE THAT DIED PART WAY. Keeps everything already on disk and
# generates only what is missing — the clips in veo_raw/ are already paid for.
# This is the one to reach for after a 502 mid-run.
curl -X POST $BASE/pipeline/scenes -H 'Content-Type: application/json' \
  -d "{\"run_id\":\"$RUN\",\"resume\":true}"

# Same for assets.
curl -X POST $BASE/pipeline/assets -H 'Content-Type: application/json' \
  -d "{\"run_id\":\"$RUN\",\"resume\":true}"

# Word timing locally instead of one Gemini call per speaking scene.
# Needs `pip install faster-whisper`; downloads a model on first use.
curl -X POST $BASE/pipeline/scenes -H 'Content-Type: application/json' \
  -d "{\"run_id\":\"$RUN\",\"diarizer\":\"whisper\"}"

# Subtitle method. Render is free, so A/B them on the same run.
# "boxed" = PNG overlays (default), "ass" = ASS vector shapes, flat cost.
curl -X POST $BASE/pipeline/render -H 'Content-Type: application/json' \
  -d "{\"run_id\":\"$RUN\",\"force\":true,\"subtitle_method\":\"ass\"}"
```

**`force` vs `resume`.** They are opposites, and passing both is a `422` rather
than a guess:

|                 | What it does                           | When                      |
| --------------- | -------------------------------------- | ------------------------- |
| neither         | `409` if the stage already has output  | the safe default          |
| `resume=true`   | keep what exists, generate the rest    | a stage crashed part way  |
| `force=true`    | throw it all away and regenerate       | you edited the blueprint  |
| `tts_only=true` | replay L6 over `veo_raw/`, no Veo call | tuning voice or subtitles |

Resume looks only at whether files exist. It cannot tell that a prompt changed,
so after editing a blueprint the flag you want is `force`.

The response reports `reused` — how many artifacts were skipped. If you resume
and `reused` is 0, nothing was on disk to keep and you just paid for the lot.

A rough sense of where the money goes on a 9-scene run: ~$4.20 of Veo against ~6
image calls, 9 STS calls, 9 diarization calls and 1 Lyria call — cents, all told.
**Video dominates by an order of magnitude**, which is exactly why `veo_raw/`,
`tts_only` and `resume` exist.

Rates live in one table, `app/costs.PRICING`, transcribed from
[cost.md](../cost.md) with a `PRICING_LAST_CHECKED` stamp. `GET /pipeline/cost`
returns the real figure for a run, broken down by layer and model — check
`unpriced` before trusting `total_usd`.

### Layers standalone

```bash
# L2 only — one live call, prints the blueprint + validation
python -m bench.director_smoke bench/sources/hr_policies.txt temp_storage/run001

# L4 only
python -m app.pipeline.image_gen --run _storage/atlas_storyboard/$RUN \
       --char-lib _storage/character_library

# L3 patch only
python -m app.pipeline.patch_blueprint --run _storage/atlas_storyboard/$RUN

# Veo payload inspection — no network, no credits
python -m bench.inspect_veo_payloads --run _storage/atlas_storyboard/$RUN

# Diarization accuracy, free and local
pip install faster-whisper
python -m bench.diarize_local --video path/to/clip.mp4 --expected "the exact words"
```

---

## 6. Troubleshooting

| Symptom                                                   | Cause                                                                                                                        |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `503 director not configured`                             | `HEYHI_GEMINI_API_KEY` unset                                                                                                 |
| `503 character library not initialized`                   | No `index.json` at `CHAR_LIB_ROOT`. Run the `app.libraries` CLI.                                                             |
| `502 director failed after N attempts`                    | Gemini couldn't produce a valid blueprint. The last error names the rules it broke — raise `max_retries` or loosen the rule. |
| `409 blueprint.json exists`                               | Pass `force=true`. Note this wipes the selection.                                                                            |
| `409 scenes exist` / `409 assets exist`                   | `resume=true` to fill in what is missing, `force=true` to pay for it all again.                                              |
| `422 force and resume are mutually exclusive`             | Pick one. `resume` after a crash, `force` after a blueprint edit.                                                            |
| `409 run /pipeline/select first`                          | L4/L5 need the resolved blueprint, not just the director's.                                                                  |
| `422 {"errors": [...]}`                                   | Selection or voice resolution problem. The list is specific.                                                                 |
| `VeoRouteError: incoherent task/model`                    | Blueprint edited by hand; `task`/`model` no longer match `reference_from`.                                                   |
| `Veo returned no video (filtered/blocked)`                | Safety filter. Usually a prompt Veo read as depicting a real person or restricted content.                                   |
| Subtitles are blank boxes                                 | Language needs a script font that isn't installed. Check `font_warning` on the render response.                              |
| `RenderError: title font not found`                       | Cartoon style, and the server wasn't started from the repo root.                                                             |
| Transitions missing, no error                             | Same cause, quiet failure. Set `ATLAS_TRANSITIONS_DIR` absolute.                                                             |
| `'ffmpeg' not found on PATH`                              | Install ffmpeg.                                                                                                              |
| `TypeError: unsupported operand type(s) for \|` at import | Python < 3.11.                                                                                                               |
| Voice changes between scenes                              | Two `audio_id`s for one `char_id` — should have been caught; check `selections.json`.                                        |
| A visible jump at a scene cut                             | The `scene:<prev>` continuity chain. Check `scenes/_lastframe_<prev>.png` — that's the exact frame Veo was seeded with.      |
