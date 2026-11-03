# Atlas — source-to-video pipeline

Turns corporate training documents into AI-generated videos. Part of HeyHi.

Feed it a `.txt` and a config; it writes a script, generates characters'
backgrounds and reference frames, produces video clips, replaces the voices with
consistent ones, adds music and subtitles, and renders an MP4.

**Stack:** FastAPI · Gemini 2.5 Flash (script) · Gemini 2.5 Flash Image (assets) ·
Veo 3.1 Fast/Lite (video) · ElevenLabs speech-to-speech (voice, default) or Azure
TTS · Google Lyria 3 Clip (music) · ffmpeg (render)

---

## Docs

| | |
|---|---|
| **[Architecture](docs/architecture.md)** | How the eight layers fit together, module map, where every external call happens. **Start here.** |
| **[blueprint.json schema](docs/blueprint-schema.md)** | Schema v3 field reference, the `reference_from` routing grammar, and how `blueprint_type` / `style_type` change generation. |
| **[Setup and running](docs/setup-and-run.md)** | Dependencies, environment, asset libraries, an end-to-end walkthrough, troubleshooting. |
| **[Extension points](docs/extending.md)** | Adding a blueprint type, a style type, a provider; changing the character pipeline. |
| **[Open questions](docs/open-questions.md)** | 28 numbered items — every bug, limitation and deliberate trade-off, each with an outcome and the reason for it. Nothing is currently undecided. **Read before changing anything**, and add to it rather than fixing something quietly. |
| **[Deployment](docs/deployment.md)** | What has to change before real users can rely on this: persistence, long-running jobs, auth. Plus the prep for the IT conversation. |
| **[Cost model](cost.md)** | Per-provider rates, transcribed into `PRICING` in `app/costs.py`. Rates go stale and nothing detects it — re-check the date at the top before trusting a total. |

Change specs — the working history, each one a scoped batch of changes, all in
[`docs/change-md/`](docs/change-md/): `change.md` · `change2.md` ·
`change3_short_animation.md` · `change_kieai.md` · `change_get_elevenlabs_voice.md`

The `CLAUDE.md` one directory up is the **original design document**, not a
description of this code. It's diverged in several places (it still documents
`keyframe`, a `topic-transition` B-Roll role, and Azure as the TTS layer). Trust the
code and these docs; see [open-questions.md §5](docs/open-questions.md#5-partly-fixed--schema-drift).

---

## Quick start

Needs **Python 3.11+** and **ffmpeg** on `PATH`.

```bash
pip install -r requirements.txt
cp .env .env.local   # then fill in HEYHI_GEMINI_API_KEY and HEYHI_ELEVENLABS_API_KEY
uvicorn main:app --reload --port 8000    # from the repo root — relative asset paths
```

`requirements.txt` is the one to install from — lower bounds derived from the
imports, no upper bounds. `requirements.lock.txt` is a snapshot of an environment
the pipeline
has run end to end in; reach for it only to bisect against a known-good set after
an upgrade breaks something. It has a regeneration footgun (`pip freeze` is wrong
in a conda environment) documented in
[setup-and-run.md §2](docs/setup-and-run.md#2-dependencies).

`http://localhost:8000/docs` for the interactive API.

### PDF review prototype

Open `http://localhost:8000/pdf-review/` for the review-first PDF workflow. It
accepts a public or presigned S3/CloudFront URL (and a local PDF during
development), renders and extracts the slides, then guides a reviewer through:

1. source-slide selection;
2. a free local draft or one explicit OpenAI script call;
3. scene timing and presenter direction;
4. one OpenAI call to shortlist the best visual slides, followed by human approval;
5. manual or OpenAI-suggested crop coordinates with locally rendered previews;
6. a locked Seedance 2.0 / 480p preflight manifest.

The prototype cannot submit a video-generation task. Its final button is
intentionally disabled, so using the UI cannot spend Kie video credits.

Set `OPENAI_API_KEY` in `.env` for script synthesis, visual-slide selection,
and crop analysis. The default model is `gpt-5.4-mini`; override it with
`OPENAI_PDF_MODEL` if needed.

Full walkthrough with curl: [setup-and-run.md](docs/setup-and-run.md#5-running-it).

---

## The pipeline at a glance

```
L1  /pipeline/config, /pipeline/upload    config.json, source.txt
L2  /pipeline/direct       💸             blueprint.json          ← the human gate
L3  /pipeline/select                      blueprint.json resolved
L4  /pipeline/assets       💸             backgrounds/, storyboard/
L5  /pipeline/scenes       💸💸           veo_raw/, scenes/       ← L5 and L6 interleave
L6  (same endpoint)        💸             tts/, scenes/ rewritten
L7  /pipeline/music        💸             music_bg/track.mp3
L8  /pipeline/render                      final.mp4
```

💸 = spends credits. **Read the blueprint after L2 and look at the PNGs after L4** —
everything downstream executes whatever they say, and L5 is where the money goes.

---

## Two things to know before reading the code

**The run directory is the state.** No database, no queue. A `run_id` names a folder
under `_storage/atlas_storyboard/`, and every endpoint's precondition is "does the
file the previous stage should have written exist". That's why each handler starts
with a cluster of `409` checks. The pipeline is resumable by POSTing the next stage,
and nothing ever regenerates automatically — billable stages need an explicit
`force=true` to redo everything, or `resume=true` to fill in only what is missing.

**`blueprint.json` is the contract.** The director (L2) is the only layer that makes
creative decisions; everything after it mechanically executes the blueprint. To
change what the videos look like, you usually change the director's prompt or its
validator, not the layers downstream.

And one thing about the director specifically: it runs a **retry loop against its own
validator**. Gemini won't satisfy a spec this detailed first time, so
`validate_blueprint` produces a precise list of violations and `direct()` feeds them
back into the next attempt. Which means:

> A rule stated in `DIRECTOR_RULES` but not checked in `validate_blueprint` is a
> suggestion the model can ignore, because nothing will ever tell it that it did.

---

## Repo layout

```
main.py              application composition + the original staged endpoints
app/
  pdf_review.py      PDF import, scripting, crop review, locked preflight API
  config.py          director knobs + the only Gemini key read
  schemas.py         blueprint v3 models + HTTP models
  libraries.py       character/audio index (has a CLI)
  pipeline/          one module per layer, L2 through L8
  clients/           every external API boundary
  utils/             ffmpeg, retry, text, atomic writes
asset/               fonts + transition overlays
bench/               scratch tooling: director smoke test, Veo payload
                     inspector, diarization accuracy harness
tests/               the unit suite (see below) + real-output fixtures
_storage/            runs + the character and audio libraries (gitignored)
docs/                the documents linked above, plus change-md/ and examples/
```

```bash
pytest      # 276 tests, ~0.3s, no network / keys / ffmpeg / credits
```

Fixtures are real director output from `docs/examples/`. Six endpoints are driven
through `TestClient` — enough to pin the `force` / `resume` / `tts_only`
preconditions and the cost recording, not enough to call the API covered. What it
still does *not* cover — CI, the remaining endpoints, the ffmpeg passes, the
clients — and the reasoning about which layers are worth testing at all, is in
[open-questions.md §7](docs/open-questions.md#7-partly-fixed--no-ci-integration-coverage-is-thin).
