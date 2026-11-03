# Open questions, known limitations and bugs

A running log, started 2026-07-30 from a read of the code, so nobody has to
rediscover the same things.

Every item carries a status and a stable number. **Numbers never change** — an
item that gets fixed keeps its id and moves to the closed section, so older
references stay valid.

| Status | Meaning |
|---|---|
| **OPEN** | Not yet discussed, or discussed and still needs a decision |
| **ON HOLD** | Discussed and deliberately parked. Says what would un-park it |
| **ON TRIAL** | Built, but not the default until it passes a quality check |
| **FIXED** | Changed in the code. Says what and when |
| **DECIDED** | Deliberately left as-is. Says why, and what would make us revisit |

Each item also says whether it is **confirmed from the code**, **observed in run
output**, or an **assumption**.

---

## Status at a glance

**Reviewed end to end 2026-08-02. Nothing is undecided.** Every item has been
walked through and given an outcome: built, parked with a named trigger, or
deliberately left alone with the reason recorded.

**Nothing is OPEN.** New findings go here as they turn up; the section will come
back when there is something in it.

**Built, awaiting your quality check (1):**
[23](#23-on-trial--_burn_boxed-filtergraph-grows-per-subtitle-cue) — ASS subtitles, `subtitle_method="ass"`

**Parked, with the trigger written down (5):**
[14](#14-on-hold--nothing-about-the-current-storage-model-survives-deployment) — deployment, in progress with IT ·
[20](#20-on-hold--subtitles-and-audio-can-disagree-on-the-elevenlabs-path) ·
[21](#21-on-hold--music-is-mixed-at-a-fixed-level-with-no-ducking) ·
[26](#26-on-hold--env-carries-keys-nothing-reads) ·
[27](#27-on-hold--would-a-single-composed-reference-image-replace-the-storyboard) — **the costed answer to "can video be cheaper"**

**Partly closed, remainder named (4):**
[5](#5-partly-fixed--schema-drift) ·
[7](#7-partly-fixed--no-ci-integration-coverage-is-thin) ·
[12](#12-decided--align_starts-is-non-monotonic-by-design) ·
[19](#19-partly-fixed--is-per-phrase-subtitle-timing-worth-a-gemini-call-per-scene)

**Closed — fixed (12):** [2](#2-fixed--background-was-a-string-not-a-tuple) ·
[3](#3-fixed--relative-asset-paths-broke-when-cwd-wasnt-the-repo-root) ·
[8](#8-fixed--no-pinned-dependency-set) ·
[9](#9-fixed--cost-reporting-was-video-only) ·
[10](#10-fixed--appeleven_labspy-was-dead-code) ·
[11](#11-fixed--benchinspect_veo_payloadspy-had-a-stale-fixture) ·
[16](#16-fixed--whisperdiarizer-was-not-selectable) ·
[18](#18-fixed--what-_norm-does-to-non-latin-scripts-and-what-it-actually-breaks) ·
[24](#24-fixed--no-resume-within-a-stage) ·
[25](#25-fixed--geminidirectoruse_schema-defaulted-off-and-nobody-recorded-why) ·
[28](#28-fixed--the-director-wrote-english-narration-when-the-config-asked-for-indonesian) — **found on the first non-English run** ·
[29](#29-fixed--subtitles-strobed-and-one-caption-was-silently-dropped) — **found reviewing the video that had already been signed off**

**Closed — decided (8):** [1](#1-decided--placeholder-char_ids-leak-into-veo-prompts) ·
[4](#4-decided--illustration-blueprint-type-is-specced-not-implemented) ·
[6](#6-decided--two-character_count-invariants-that-can-conflict) ·
[12](#12-decided--align_starts-is-non-monotonic-by-design) ·
[13](#13-decided--169-locked-at-the-config-layer) ·
[15](#15-decided--character-consistency-is-good-enough-and-the-storyboard-is-really-a-reference-panel) ·
[17](#17-decided--the-50-match-threshold-is-a-judgement-call-and-stays-one) ·
[22](#22-decided--min_scene_seconds-is-advisory-and-enforcing-it-would-be-worse)

### Waiting on real-world results

Two things are settled in the code but unconfirmed in the field. Update them when
the evidence arrives rather than leaving the doc claiming more certainty than we
have:

- [18](#18-fixed--what-_norm-does-to-non-latin-scripts-and-what-it-actually-breaks)
  and [28](#28-fixed--the-director-wrote-english-narration-when-the-config-asked-for-indonesian)
  — the first non-English run (`run_77d71eba`) found a real bug, now fixed and
  guarded by a validator. The rest of the chain is still unproven; the untested
  link is whether Veo's native audio is transcribable in that language, which is
  what L6 depends on.
- [23](#23-on-trial--_burn_boxed-filtergraph-grows-per-subtitle-cue) — ASS
  subtitles render correctly; whether they look right on a full video is your
  call.

---

# ON TRIAL — built, awaiting a quality check

## 23. ON TRIAL — `_burn_boxed` filtergraph grows per subtitle cue

**Built 2026-08-02, not yet the default.** `subtitle_method="ass"` on
`POST /pipeline/render` draws the same rounded box as ASS vector shapes in a
single subtitle file. `"boxed"` stays the default until it passes your visual
check on a real video; if it does, `boxed` retires.

**The problem.** `_burn_boxed` renders each cue to a PNG and overlays it: one
ffmpeg **input** and two filtergraph nodes per cue, chained. A long video with
per-phrase captions reaches a hundred-plus inputs in one command. It works today
and it will hit an argument or complexity limit eventually, failing with an
obscure filtergraph error that names nothing.

**Why the escape hatch was thought to be square corners.** The comment in
`_burn_boxed` says *"Neither libass nor drawtext can draw rounded corners"*. That
is true of libass's built-in box — `BorderStyle=4` is a rectangle — but **not of
libass**, which renders ASS vector drawing commands. So a rounded,
semi-transparent box is expressible after all:

```
Dialogue: 0,...,Box,,0,0,0,,{\pos(451,622)\1c&H000000&\1a&H72&\bord0\shad0\p1}m 12 0 l 365 0 b 377 0 377 0 377 12 ...{\p0}
Dialogue: 1,...,Text,,0,0,0,,{\pos(640,657)}Email the payroll team with your\Npayslip attached.
```

Two lines per cue in one file — a drawing on layer 0, the text on layer 1 above
it. One ffmpeg input, one `subtitles` filter, **any number of cues**.

| | `boxed` | `ass` |
|---|---|---|
| ffmpeg inputs | 1 + one per cue | **1** |
| filtergraph nodes | 2 per cue | **0** |
| Temp files | one PNG per cue | one `.ass` |
| Rounded corners | yes (Pillow) | yes (bezier) |
| Adjustable opacity | yes | yes (ASS alpha) |
| Needs libass | no | **yes** |

Verified on a real clip: both render, and the output is near-identical. `ass`
took 1.36s against `boxed`'s 0.54s on a **2-cue** clip — that is libass's fixed
startup cost — but `boxed` grows with cue count and `ass` does not, so they cross
over quickly.

**Three things worth knowing about the implementation**, because each was a trap:

- **ASS alpha is inverted.** `00` is opaque, `FF` invisible. `box_opacity=0.55`
  becomes `\1a&H72&`.
- **ASS colours are BGR**, not RGB. A red box written naively comes out blue.
- **libass resolves fonts by family name via fontconfig, not by path.** The box
  is measured with Pillow using a font *file*; if libass then substitutes a
  different font, the box is the wrong width for the text in it. `fontsdir` on
  the filter prevents that, and the family name is read off the file that was
  measured.

Pillow is still used, but only to measure — only a font engine can say how wide
the text is. libass does the drawing.

The text style deliberately sets `Outline=0` rather than `style.outline`: the box
provides the contrast, and `_burn_boxed` draws plain unoutlined text for the same
reason. `style.outline` belongs to the `libass` method, which has no box.

**How to run the comparison.** Rendering is free and re-runnable, so do both on
the same run:

```bash
curl -X POST $BASE/pipeline/render -H "Content-Type: application/json" \
  -d "{\"run_id\":\"$RUN\",\"force\":true,\"subtitle_method\":\"boxed\"}"
cp _storage/atlas_storyboard/$RUN/final.mp4 /tmp/final_boxed.mp4

curl -X POST $BASE/pipeline/render -H "Content-Type: application/json" \
  -d "{\"run_id\":\"$RUN\",\"force\":true,\"subtitle_method\":\"ass\"}"
```

Look at a long caption, a two-line caption, and a non-Latin one if you have one.
**To retire `boxed`** once it passes: change the default on `RenderIn`, then
delete `_burn_boxed` and its PNG plumbing.

Pinned by `tests/test_renderer_ass.py` (20 tests) — including one asserting that
200 cues produce the same ffmpeg command as 2, which is the whole point.

---
---

# ON HOLD — parked, with the trigger to un-park written down

## 14. ON HOLD — nothing about the current storage model survives deployment

**On hold 2026-08-02, pending the IT conversation.** Everything known is written
up in [deployment.md](deployment.md); nothing changes in the code until that
meeting has happened. This item stays open as the pointer.

**Confirmed.** No database, no auth, no user concept, no object storage (the S3
keys in `.env` are read by nothing — see
[26](#26-on-hold--env-carries-keys-nothing-reads)), no background jobs, no file
locking. A run is a directory, and it persists exactly as long as the disk does.

Consequences: on ephemeral infrastructure yesterday's video is gone after a
redeploy; nothing can answer "show me my videos" because no user→run mapping
exists; and anyone holding a `run_id` can spend credits against it via
`force=true`.

Separately — and this blocks deployment regardless of storage —
`/pipeline/scenes` runs the whole Veo + L6 loop **synchronously inside one HTTP
request**. Tens of minutes, against gateway timeouts of 60–100s.

Full gap analysis, the staged plan, the questions to put to IT and the answers to
have ready are in [deployment.md](deployment.md) — §6 is the meeting prep.

**Before the meeting**, measure the three numbers in §6.1. Wall-clock for a full
run is the one nobody has yet and the one IT will ask for first.

## 20. ON HOLD — subtitles and audio can disagree on the ElevenLabs path

**On hold 2026-08-02: not observed in practice.** Revisit if a caption is ever
seen contradicting what is heard.

**Confirmed by design.** The words *heard* come from Veo's delivery, converted by
STS. The subtitle text comes from `scene.text`. If Veo improvised, dropped or
reordered a word, the caption and the audio disagree.

Subtitles are authoritative for the script; audio is authoritative for the
performance. Not detected, not reported.

Worth knowing *why* it has not shown up: Veo is prompted with the exact text
(`compose_veo_prompt` appends `Audio sync: The character says "..."`), so it
usually delivers the script verbatim. The divergence is possible, not routine.

**What would surface it cheaply if it ever matters:** the diarizer already
transcribes the converted track for subtitle timing, and `phrase_spans` already
computes a match ratio against `scene.text`. A low ratio on a scene that
otherwise looks fine *is* the signal — it just is not reported anywhere today.
Logging that ratio would turn this from invisible into a warning line, without
any new calls. Cheap when the time comes.

## 21. ON HOLD — music is mixed at a fixed level with no ducking

**On hold 2026-08-02: no problem observed, and ducking is a product decision
nobody is ready to make.** Revisit when a video comes back with the bed fighting
the narration.

**Confirmed in code.** −20dB under narration, hard-coded as
`render_merge_and_subs`'s `music_gain_db` default and **not exposed on
`RenderIn`** — so it cannot be tuned per run without editing code.

No ducking: music sits at a constant level rather than dipping under speech. Fine
for a quiet bed; audible if Lyria returns something busy.

Separately, Lyria returns ~30s and the bed is looped with chained `acrossfade`
joins. A long video means many loops of the same 30 seconds. Nobody has said how
long is too long.

**If it is ever picked up, do the cheap half first.** Exposing `music_gain_db` on
`RenderIn` is a two-line change and makes the level tunable per run without a
redeploy — render is free and re-runnable, so it costs nothing to experiment
with. Actual sidechain ducking (`sidechaincompress` in the ffmpeg mix) is the
larger job and only worth it if a fixed level turns out to be the real problem.

## 26. ON HOLD — `.env` carries keys nothing reads

**On hold 2026-08-02, flagged in place.** Keeping them: the plan is to connect to
AWS, at which point the S3 group becomes real rather than decorative. Revisit
when [14](#14-on-hold--nothing-about-the-current-storage-model-survives-deployment)
resolves — that conversation decides what storage actually looks like, and these
keys either become live or get deleted.

**Confirmed.** `HEYHI_OPENAI_API_KEY`, `HEYHI_DEEPSEEK_API_KEY`, the whole S3
group (`HEYHI_S3_BUCKET`, `HEYHI_S3_REGION`, `HEYHI_AWS_ACCESS_KEY_ID`,
`HEYHI_AWS_SECRET_ACCESS_KEY`), `HEYHI_USE_LOCAL_STORAGE`, and
`HEYHI_DIRECTOR_PROVIDER` / `HEYHI_DIRECTOR_MODEL` are read by **nothing** in
`new_code_v2`.

Storage is always local; there is no S3 path and no provider-override path for
the director.

**One thing worth separating out.** `HEYHI_OPENAI_API_KEY` and
`HEYHI_DEEPSEEK_API_KEY` are not part of the AWS plan — nothing in this repo has
ever used them and nothing is expected to. Those two are just live credentials
sitting in a file for no reason, and they are the ones to remove. The S3 group
has a future; those do not.

Low risk but not zero: unused credentials in a deployed environment are still
credentials that can leak.

**Addendum 2026-10-29.** The PDF review prototype now uses the OpenAI SDK for
script synthesis, visual-slide selection, and visual crop analysis. It deliberately uses the standard
`OPENAI_API_KEY` variable; the legacy `HEYHI_OPENAI_API_KEY` statement above
remains true. The original eight-layer pipeline is unchanged.

## 27. ON HOLD — would a single composed reference image replace the storyboard?

**Decision 2026-08-02: not changing the winning team.** Output quality is
accepted today and there is no cost pressure, so this stays a costed proposal
rather than a change.

> **Un-park it the moment anyone above asks whether video generation can be made
> cheaper.** This is the answer to that question, it is already measured, and the
> experiment that de-risks it costs $0.40. Do not re-derive any of it — the
> numbers below are ready to present.

**Your idea, logged 2026-08-02 so it does not get lost.** Raised while closing
[15](#15-decided--character-consistency-is-good-enough-and-the-storyboard-is-really-a-reference-panel),
and it deserves its own number because it is a concrete, measurable experiment
rather than an observation.

**The proposal.** Replace the 2×2 storyboard grid with one image: the character
composited into the background. Every scene then becomes `image-to-video`
seeded from that plate, which means **every scene routes to Veo 3.1 Lite** and
the Fast tier disappears from the pipeline.

**What it would save.** Lite is $0.05/s against Fast's $0.10/s, and only the
topic-opening scenes use Fast today. Measured on the three committed example
blueprints:

| Run | Scenes | On Fast | Veo cost now | All-Lite | Saving |
|---|---|---|---|---|---|
| `run_0616c9e5` | 9 | 3 | $4.20 | $3.00 | $1.20 (29%) |
| `run_d852054a` | 2 | 1 | $1.00 | $0.60 | $0.40 (40%) |
| `run_ff808532` | 3 | 1 | $1.40 | $1.00 | $0.40 (29%) |

So **≈30% off the video bill**, which is ≈30% off the run, since video dominates
everything else by an order of magnitude. No saving on images — it is one
generated plate either way.

**What it would cost.** Honestly: unknown, and that is the whole problem. The
grid is what tells Veo how the shot should develop over eight seconds — four
panels with timecodes. A single frame says where things start and nothing about
where they go. The plausible failure is not ugly video, it is *static* video:
a talking head that holds one composition for eight seconds because nothing
asked it to move.

**"I'm afraid it will change the winning team" is the right instinct, and the
answer is not to avoid the experiment — it is to make it non-destructive.**
Nothing about this has to be decided blind:

1. Take a run you already have. `veo_raw/` still holds the originals.
2. Generate one composed plate for one topic-opening scene.
3. Regenerate that one scene as `background:`-style image-to-video.
4. Watch the two clips side by side.

Cost: one Lite call, **$0.40**. Against a ~30% standing discount on every video
from here on. That is a cheap question to answer, and it stays cheap because
`veo_raw/` means the current version is never at risk.

If it does hold up, the changes are contained: the director stops emitting
`storyboard` entries and emits composed-plate prompts instead, `ROUTE_BY_PREFIX`
loses the `storyboard:` branch, and L4 generates plates rather than grids.
`Veo 3.1 Fast` stays in the schema — no reason to remove an option you are not
using.

### If it is ever asked for, this is the shape of the answer

1. **The lever exists and is measured.** ~30% off the video bill, which is ~30%
   off the run.
2. **The risk is known and named.** Possibly more static shots, because the plate
   becomes frame 0 rather than a reference Veo composes from.
3. **De-risking costs $0.40 and one afternoon.** One scene, regenerated from a
   composed plate, watched next to the original. `veo_raw/` means the current
   version is never at risk.
4. **If it works, the change is contained** — the director stops emitting
   `storyboard` entries, `ROUTE_BY_PREFIX` loses its `storyboard:` branch, and L4
   generates plates instead of grids.

Two smaller levers to mention in the same breath, both already logged: B-Roll is
generated at Veo's 4s floor and often trimmed to 1s
([9](#9-fixed--cost-reporting-was-video-only)), and diarization can move off
Gemini for free ([16](#16-fixed--whisperdiarizer-was-not-selectable)). Neither is
close to 30%, but together they show the cost question has been thought about
rather than answered on the spot.

Also relevant to [14](#14-on-hold--nothing-about-the-current-storage-model-survives-deployment):
"cost per finished video" is one of the three numbers to bring to IT, and this is
the lever that moves it.

---
---

### "But Lite does not support reference-to-video"

**Correct — and that is what makes the saving, not what blocks it.** The proposal
removes the need for reference-to-video entirely. Nothing would ask Lite to do it.

The two Veo tasks feed the model differently:

| | How the image is used | Model |
|---|---|---|
| `reference-to-video` | `cfg.reference_images`, `reference_type="asset"` — Veo composes a NEW shot conditioned on the asset | Fast |
| `image-to-video` | `image=` — the plate IS frame 0; the video starts exactly there | Lite |

**The strongest evidence is already in your own runs.** Counted across every
blueprint on disk:

```
scene:      Veo 3.1 Lite   image-to-video        90 speaking scenes
storyboard: Veo 3.1 Fast   reference-to-video    37 speaking scenes
```

**71% of speaking scenes already run image-to-video on Lite** — every `scene:`
continuation — and those are talking-head scenes whose quality is accepted today.
So "can Lite generate an acceptable speaking scene from a single seed frame" is
not a hypothetical: it is what most of your video already is.

What changes is only *where the seed frame comes from*: the last frame of the
previous clip today, a composed plate under this proposal. Both are a first frame
with the character in it.

**The one real difference to watch**, and it is why this stays an experiment: with
`image-to-video` the plate is literally frame 0, so the shot *starts* in that
exact composition. Reference-to-video lets Veo pick its own opening framing. That
is the source of the static-shot risk — not identity loss, which if anything gets
stronger, since the character is the actual first frame rather than a reference
Veo redraws.

# ON HOLD — parked on purpose

---
---

# PARTLY FIXED — remainder named

## 5. PARTLY FIXED — schema drift

**Addressed 2026-07-31:** text artifacts from three representative runs are now
committed under [docs/examples/](examples/), and ~340MB of generated media in
`_storage/` was deleted (keeping `run_652b50f7` whole as a local fixture). The
evidence now survives a clone, which `_storage/` never could — it is gitignored.

**Doc drift resolved.** For the record, the root `claude.md` is the *original
design document*, not a description of this code. Where they disagree, the code
is right:

- `broll_role: "topic-transition"` — documented there, does not exist.
  `BRollRole` is `("opening", "closing")`; topic ends are marked by a `closing`
  B-Roll and the renderer composites the transition across that cut.
- Azure TTS as *the* TTS layer — it is now the backup; ElevenLabs is the default.
- A `keyframe/` output directory — not produced.
- Six style options, three TBD — `StyleType` has three values, all real.
- A `char_manager` module — never existed in this repo. Character logic lives in
  `libraries.py`, `main.py`, `patch_blueprint.py` and `image_gen.py`. Nothing
  suppresses artifacts, nothing does background removal, there is no `rembg`.

**Old blueprints are not migrated — and 2026-08-02 the decision is not to
migrate them.** You are deleting the runs that predate the current schema, which
is the right call: they are development artifacts, nobody needs to re-render
them, and a migration script for data that is about to be deleted is work for
nothing.

The drift, for the record, so it is recognisable if an old run does turn up:

| Run | `keyframe` | `style_type` |
|---|---|---|
| `run_a89810af` | present | absent |
| `run_aab9b754_azure` | present | absent |
| `run_0616c9e5` | present | `realism` |
| `run_d852054a` | absent | `realism` |
| `run_ff808532` | absent | `cartoon` |

At least three blueprint generations coexisted. `keyframe` is not in
`VideoBlueprint`, and pydantic drops unknown keys, so it is silently discarded on
parse. A run without `style_type` silently takes the realism title treatment via
`bp.get("style_type", "realism")` — degraded, not broken.

**Keep the three under `docs/examples/`** whatever else goes. They are the test
suite's golden fixtures (`tests/conftest.py` loads `run_0616c9e5`), so deleting
them breaks 200-odd tests. They are text only, ~140KB total.

**Still open, and now the only remainder:** `Storyboard.model` is carried on every
entry, defaults to `"gemini-2.5-flash-image"`, and is never consulted —
`GeminiImageClient` hard-codes its own model. Harmless, but it is a field that
looks like a setting and is not.

## 7. PARTLY FIXED — no CI, integration coverage is thin

**Unit suite added 2026-08-01, extended 2026-08-02: 276 tests, ~0.2s, no network
/ keys / ffmpeg.** `pytest` from the repo root. Covers `validate_blueprint` (the
~25 semantic rules), `align_starts`/`phrase_spans`, `apply_selection` +
`_check_resolved` + `patch_run`, `parse_reference`/`route`, `build_srt`, the cost
module, the `/pipeline/direct` cost recording, the resume semantics
([24](#24-fixed--no-resume-within-a-stage)), the provider/diarizer wiring
([16](#16-fixed--whisperdiarizer-was-not-selectable),
[25](#25-fixed--geminidirectoruse_schema-defaulted-off-and-nobody-recorded-why)),
plus the duration/style/prompt/retry helpers.

Fixtures are **real director output** from [docs/examples/](examples/), which all
validate clean — so a test that mutates one field and expects one problem is
measuring that rule and nothing else.

It has earned its keep three times now: writing it surfaced the mischaracterised
[12](#12-decided--align_starts-is-non-monotonic-by-design), the first live run
surfaced the cost-recording bug now pinned by
`tests/test_director_cost_recording.py`, and writing the multi-language cases
disproved the original diagnosis in
[18](#18-fixed--what-_norm-does-to-non-latin-scripts-and-what-it-actually-breaks).

### "Should we test all layers, not just the director?"

**Agreed on the principle — but the layers are not equally worth testing, and
treating them as if they were is how a test suite becomes something people skip.**
Ranked by value per hour:

**1. Endpoint tests with fake clients — do these first.** Every seam already
exists: `_director_llm`, `_image_client`, `_scene_clients`, `_music_client` are
overridable, and `ATLAS_STORAGE_ROOT` redirects the run tree. `TestClient` plus a
fake gets you full coverage of all eight endpoints — the 409 ordering rules, the
422 validation, the force/resume/tts_only matrix — in a few hundred lines, with
no network and no ffmpeg. **This is where the bugs actually are**: the two real
ones found so far (cost recording on the 502 path, force vs resume) were both
endpoint-level. `tests/test_resume.py` already does this for three endpoints, so
the pattern is there to copy.

**2. Renderer ffmpeg passes — valuable, but they need ffmpeg in CI.** merge,
transitions, title, subtitle burn, music mix. A 1-second synthetic clip generated
by ffmpeg itself is enough input. Slower (seconds, not milliseconds), so worth
keeping in a separate marked group that runs in CI but not on every save. Ask IT
whether the runner can have ffmpeg — it changes what is worth writing.

**3. Client tests — lowest value, and worth saying why.** `GeminiVeoClient`,
`ElevenLabsSTSClient` and friends are thin translation layers. A unit test with a
mocked SDK only proves the mock matches what you *think* the SDK does — which is
precisely the assumption that breaks. The genuine risk here is an SDK response
shape changing under you (it has happened once already: `_get()` in
`sts_elevenlabs_client` tolerates both objects and dicts *because* of it), and
no offline test can catch that. **The honest answer for this layer is a scheduled
smoke run against the real APIs** — one cheap call per provider, weekly, alerting
on failure. That belongs on the IT list next to CI, because it needs credentials
in the runner and it costs a few cents a week.

**Still open:**

- **No CI.** Nothing runs these automatically. On the list for the IT
  conversation ([14](#14-on-hold--nothing-about-the-current-storage-model-survives-deployment)).
- **Untested modules:** `renderer`'s ffmpeg passes, `libraries` index
  build/validate, `resolve_voices`, every client.
- Three tests deliberately assert **known** behaviour that is wrong or
  surprising, so it stays visible: the placeholder-prose leak
  ([1](#1-decided--placeholder-char_ids-leak-into-veo-prompts)), the final-topic
  closing-B-Roll gap, and the non-monotonic alignment
  ([12](#12-decided--align_starts-is-non-monotonic-by-design)). Each says so in
  its docstring and will fail if the underlying decision changes — that is the
  signal to update it, not a broken test.

## 19. PARTLY FIXED — is per-phrase subtitle timing worth a Gemini call per scene?

**Answered 2026-08-02 by making the choice free instead of arguing about it.**
The question was whether per-phrase captions justify one Gemini call per speaking
scene. With [16](#16-fixed--whisperdiarizer-was-not-selectable) fixed, the
diarizer can run locally for nothing —
so the feature stays and the cost is the part that goes away.

**Confirmed.** Both audio paths call the diarizer once per speaking scene. On the
ElevenLabs path the result is used **only** to time subtitles, never to place
audio — STS synthesises no individual phrases, so there is no per-phrase duration
to measure any other way. `build_srt` already falls back to one whole-scene
caption when the sidecar is missing, so the feature was always optional.

**Remainder: the default is still `gemini`.** Switching it means one comparison
run — `bench/diarize_local.py` prints WER against a known transcript, and
`faster-whisper` is already installed in the working environment. Two or three
real clips is enough to decide. Until that happens the saving is available but
not taken.

To put a number on it: diarization is a handful of cents per run against a video
bill of several dollars. **This was never where the money was** — which is worth
saying plainly, because "one LLM call per scene" sounds more alarming than it
costs. The reason to switch is accuracy first (see
[16](#16-fixed--whisperdiarizer-was-not-selectable)), cost second.

---
---

# CLOSED — fixed

## 2. FIXED — `("background")` was a string, not a tuple

**Fixed 2026-07-31.** In the opening-B-Roll branch of `validate_blueprint`:

```python
if original_prefix not in ("background"):     # string — substring test
if original_prefix not in ("background",):    # tuple  — membership test
```

The missing comma made it a substring test. It gave the right answer for the
three real prefixes, which is why nothing surfaced — but `original_prefix` is
`None` for a bare keyword, and `None not in "background"` raises `TypeError`.
`direct()` catches `ValidationError`/`JSONDecodeError`/`ValueError`, not
`TypeError`, so it escaped `/pipeline/direct` as an unhandled **500** instead of
becoming a problem the retry loop could repair.

Pinned by
`tests/test_director_validation.py::test_ambiguous_reference_from_is_reported_not_raised`.

## 3. FIXED — relative asset paths broke when cwd wasn't the repo root

**Fixed 2026-07-31.** `renderer.transitions_dir()` defaulted to
`"asset/transitions"` and `CARTOON_TITLE_STYLE.font_file` to
`"asset/fonts/Fredoka-SemiBold.ttf"`, both resolved against the process working
directory. Started from anywhere else, transitions vanished **silently** and
cartoon titles **502'd**.

`app/paths.py` now resolves code-adjacent assets from `__file__` (`REPO_ROOT`,
`ASSET_DIR`, `FONT_DIR`, `TRANSITIONS_DIR`). Run data (`STORAGE_ROOT`,
`CHAR_LIB_ROOT`, `AUDIO_LIB_ROOT`) deliberately still resolves against cwd with
an env override — that is data, and it belongs outside the repo.

Also: an index missing from an **explicitly configured** `ATLAS_TRANSITIONS_DIR`
now raises instead of silently skipping. Absence stays quiet only at the default
location, where it plausibly means "no transitions installed".

Verified by importing from `/tmp`.

## 8. FIXED — no pinned dependency set

**`requirements.txt` added 2026-07-31**, derived from the imports, plus
`requirements-dev.txt` for `pytest` and `httpx`. Verified complete: every
third-party import is covered, nothing listed is unused.

Two entries are there despite never being imported — `uvicorn` runs the app, and
`python-multipart` is required by FastAPI for `Form()`/`UploadFile` in
`/pipeline/upload`. Missing the latter fails at *request* time, not import time.

**`requirements.lock.txt` added 2026-08-02**, from the conda environment
`heyhi_atlas_ai_video` — the one the pipeline has actually been run end to end
in, with the cost log verified against live calls on every layer. Python 3.14.6,
macOS arm64.

That closes the remainder. It matters more here than usual because three surfaces
are explicitly version-sensitive and the code says so: `google-genai`'s Veo model
ids and `generate_videos` shape, the image-extraction path
(`part.inline_data.data`), and the ElevenLabs response shape (handled by
`_get()`, which tolerates both objects and dicts *because* it has already changed
once).

**One thing worth knowing before you regenerate it.** In a conda environment
`pip freeze` is wrong:

```
fastapi @ file:///Users/ec2-user/croot/fastapi-split_1782280679236/work
```

That is a build path on the machine that built the conda package. Nine packages
come out that way — `fastapi` and `pydantic` among them — so the file will not
install anywhere. Use `pip list --format=freeze`, which emits `name==version` for
all of them. The lock file's own header says this too, so nobody has to
rediscover it.

The lock pins the *whole* environment, including jupyter, moviepy, rembg and
boto3, which Atlas never imports — the env is shared with other work. Pinning
them is harmless; reading their presence as a requirement is not.

**Keep installing from `requirements.txt`.** The lock is for bisecting a broken
upgrade, not for daily use.

## 9. FIXED — cost reporting was video-only

**Fixed 2026-08-01, verified against live calls on every layer.**

`cost_log.jsonl` used to record Veo calls only, priced from a hard-coded
`COST_PER_SECOND` in `video_veo_client`, and `/pipeline/scenes` summed it and
called the result `total_cost_usd`. Images, TTS/STS, diarization, Lyria and every
director attempt were invisible. `music_lyria_client.COST_PER_CALL` existed and
was never read.

[`app/costs.py`](../app/costs.py) is now the only place spend is priced. Records
carry the **unit and quantity** rather than a bare dollar figure, so per-token /
per-second / per-image / per-call / per-credit billing coexist and a run can be
re-priced if a rate turns out wrong. Rates live in one `PRICING` table
transcribed from [cost.md](../cost.md), with a `PRICING_LAST_CHECKED` stamp.

Quantities are **measured** where possible:

| Layer | Unit | Measured from |
|---|---|---|
| L2 director | tokens in/out | SDK `usage_metadata`, one record per attempt |
| L4 images | per image | per successful image (tokens recorded, not priced) |
| L5 Veo | seconds | the `veo_raw/` mp4 — so the 4s floor bills honestly |
| L5 kie.ai | credits | the provider's own before/after balance delta |
| L6 Azure | seconds | **before** silence trimming |
| L6 STS | seconds | the **input** audio, before the temp file is deleted |
| L6 diarize | tokens | `usage_metadata`, both audio paths |
| L7 Lyria | per call | nothing to measure — quantity is always 1 |

`COST_PER_SECOND`, `estimate_cost`, `log_cost` and `COST_PER_CALL` were deleted
rather than left alongside the replacement.

`GET /pipeline/cost?run_id=...` returns the rollup; `/pipeline/scenes` reports
the whole-run total.

**Follow-up bug, found on the first live run and fixed the same day:** the L2
recording sat *after* `direct()` returned, so when the director exhausted its
retries the handler raised a 502 and the recording never ran. Three failed runs —
nine billed Gemini calls — reported `$0.00`. Backwards, because a failing
director is the **expensive** case: every attempt is a real, answered, billed
call, and only our own validation rejected the content. Now recorded in a
`finally` and tagged `director_outcome: ok|failed`.

**Residual notes, not blocking:**

- The same pattern exists in L4, L5 and L7 — a failed generation records nothing,
  because recording sits after the retry helper returns. Left alone because the
  risk differs: there the API *call* errored, so it probably was not billed. The
  case worth revisiting is a safety-filtered Veo response, which consumed compute
  and may well be charged.
- Image output is priced **per image** ($0.039), not per token. Token counts are
  recorded with `priced: false`. Per-image and per-token agree once the *image*
  output rate ($30/1M) is used instead of the text rate ($2.50/1M) —
  1290 × $30/1M = $0.0387. The earlier $0.003 figure was a units mix-up.
- `snap_duration` rounds B-Roll up: a 1s B-Roll is generated and billed at Veo's
  4s floor, then trimmed. Paid-for footage discarded — unavoidable, and now
  correctly reflected in the cost log.

## 10. FIXED — `app/eleven_labs.py` was dead code

**Deleted 2026-07-31.** Nothing imported it. Blank `API_KEY`, hardcoded filenames
from one manual test. Its working parts had already become
`clients/sts_elevenlabs_client.py` and `pipeline/sts_elevenlabs.py`.

It was also the **only** thing importing `moviepy`, so `requirements.txt` omits
that dependency. Verified no stale imports remain.

## 11. FIXED — `bench/inspect_veo_payloads.py` had a stale fixture

**Fixed 2026-07-31.** `SAMPLE_SCENES` entry `b1` used
`reference_from: "keyframe:kf1"`. `keyframe` was removed from the grammar, so
`route()` raised every time the script ran without `--run`.

`b1` now reads `"background:bg1"`, and the no-arg mode creates a dummy
`backgrounds/bg1.png` alongside the existing dummy storyboard, so the payload
print shows what actually gets attached. All four fixtures route cleanly and
cover the whole grammar.

The no-arg mode was kept rather than making `--run` mandatory — it is the only
way someone with no runs yet can see what a Veo payload looks like.

## 16. FIXED — `WhisperDiarizer` was not selectable

**Fixed 2026-08-02.** `POST /pipeline/scenes` now takes
`diarizer: "gemini" | "whisper"`, defaulting to `gemini`. `DIARIZERS` in
`audio_providers.py` is the registry; both audio builders take an optional
diarizer and fall back to Gemini, so existing callers are unchanged.

Pinned by `tests/test_wiring.py`.

### "Can Whisper do both transcription and diarization, or is Gemini necessary?"

**Whisper does both, and Gemini was never necessary for this.** The short answer
is that what this pipeline calls "diarization" is not diarization.

Real speaker diarization answers *who spoke when* — separating two voices in one
recording. This pipeline never needs that: one character speaks per scene, and
which character it is comes from `selections.json`, not from the audio. What the
`Diarizer` Protocol actually asks for is **word-level timestamps for a single
speaker**, which is forced alignment. The name is just wrong, and it has made the
task sound harder than it is.

`faster-whisper` with `word_timestamps=True` returns transcript *and* per-word
start/end in one pass — exactly the contract. No second model, no `pyannote`, no
extra step.

**It is likely to be more accurate, not less.** Whisper derives word timings from
cross-attention alignment against the audio — a measurement. `GeminiDiarizer`
asks a language model to *write down* millisecond integers describing sound it
was shown; the docstring in `external_tts.py` already calls word-timing "Gemini's
weak spot", and `bench/diarize_local.py` exists to compare them. So this is not a
cheap-but-worse substitution. Free and probably better is the likely outcome, and
one comparison run would settle it.

### What you give up by switching

Listed honestly, because "free" is not the same as "no cost":

| | Gemini | Whisper |
|---|---|---|
| Money | ~a few cents per run | nothing |
| Where it runs | Google's servers | your CPU |
| Wall-clock | one API round trip | `base` is roughly real-time on CPU; larger models slower |
| First use | instant | downloads ~150MB (`base`) or ~1.5GB (`large-v3`) |
| Install weight | already there | pulls ctranslate2 + av + onnxruntime |
| Determinism | not guaranteed even at temperature 0 | same model, same input, same output |
| Silence | handles it fine | **can hallucinate repeated phrases on non-speech** — a known Whisper failure |
| Low-resource languages | broad coverage | accuracy drops off noticeably |
| Cost log | one `L6 diarize` line per scene | nothing to record, so that row goes empty |

Two of those matter for real:

- **Deployment.** Whisper needs CPU and RAM in whatever container runs L6, and
  the model has to be baked into the image or cached on a volume — otherwise
  every cold start re-downloads it. Add this to the IT list
  ([14](#14-on-hold--nothing-about-the-current-storage-model-survives-deployment)):
  it changes the container spec.
- **Language.** `config.language` is free text and the pipeline is built for
  multi-language output. Whisper is excellent in English and good in the major
  European and East Asian languages; below that it degrades. If a video is ever
  commissioned in something unusual, `diarizer` is a per-request field — you can
  keep Gemini for that run and Whisper for everything else, without a redeploy.

**Recommendation: compare, then switch the default.** `faster-whisper` is already
installed in the working environment, so it costs nothing to try:

```bash
python -m bench.diarize_local --video path/to/clip.mp4 --expected "the exact words"
```

Two or three real clips. If the WER and the out-of-order count are comparable,
change the default in `ScenesIn` and the L6 diarization line disappears from the
cost log for good.

## 18. FIXED — what `_norm` does to non-Latin scripts, and what it actually breaks

**Original diagnosis 2026-07-31 was WRONG, corrected 2026-08-02 by measuring it.**
Recorded rather than quietly deleted, because the wrong version was specific
enough to sound authoritative.

**The claim was:** for scripts where `isalnum()` is false, words collapse to empty
strings, and two empty strings compare equal, so the matcher can align on noise.

**What is actually true.** Python's `str.isalnum()` is true for any Unicode letter
or digit, not just ASCII. Measured:

| Script | Sample | After `_norm` |
|---|---|---|
| Chinese | `你好世界` | `你好世界` — intact |
| Japanese | `こんにちは` / `日本語` | intact |
| Korean | `안녕하세요` | intact |
| Arabic | `مرحبا` | intact |
| Hebrew | `שלום` | intact |
| Cyrillic | `Привет` | intact |
| Greek | `Γειά` | intact |
| Thai | `สวัสดี` | `สวสด` — tone marks stripped |
| Devanagari | `किताब` | `कतब` — matras stripped |
| Emoji | `👋` | **empty** |

So the collapse-to-empty case is real but tiny: emoji and punctuation-only
tokens, not entire writing systems.

**Combining marks are stripped** (Thai tone marks, Devanagari matras, Arabic
diacritics, Vietnamese tone marks — Unicode category Mn/Mc). That turns out to be
*helpful*, not harmful: both streams are normalised identically, so a transcript
that omits diacritics still matches text that has them. It only loses the ability
to distinguish two words differing by nothing else.

### What was fixed

Tokens that normalise to `""` are now **dropped** from both streams rather than
compared, in `align_starts` and `phrase_spans` alike. Before, a stray `...` in
the transcript could match a stray `...` in the script and pin a phrase to
whatever moment that token happened to sit at. `_norm_phrases` and `_norm_timings`
do this; the second keeps words, starts and ends index-aligned, which is the only
reason it is a function and not three comprehensions.

Pinned by `tests/test_audio_alignment.py` —
`test_tokens_that_normalise_to_nothing_cannot_match_each_other`, plus Cyrillic,
Arabic and Vietnamese cases.

### "Do we need separate logic per language?"

**No — and the real problem is somewhere else entirely.** It is not
normalisation, it is **word segmentation**, and it is upstream of `_norm`.

Chinese, Japanese and Thai do not put spaces between words. `split_phrases` then
`p.split()` yields **one token for the entire sentence**. That single token
matches none of the diarizer's per-word output, the match ratio lands near zero,
the 50% threshold trips
([17](#17-decided--the-50-match-threshold-is-a-judgement-call-and-stays-one)), and
the layer falls back — phrases play back-to-back (Azure) or the subtitle sidecar
is skipped and L8 writes one caption per scene (ElevenLabs).

**Degraded, not broken.** And on the ElevenLabs default the audio is completely
unaffected: STS preserves Veo's own timing, so only per-phrase subtitle
granularity is lost. A CJK video today gets correct audio and coarser captions.

Pinned by `test_a_script_without_word_spaces_degrades_to_the_fallback`, which
documents the limit rather than endorsing it.

**When it is worth fixing** — that is, when a CJK video is actually commissioned —
the fix is one function, not a per-language branch: segment into characters when a
phrase yields far fewer space-separated tokens than characters. CJK is
near-morphemic per character, so character-level matching against a per-word
transcript works well enough. Roughly ten lines, applied to both streams, and it
is a no-op for every space-separated language. **Not worth building on
speculation**, which is why it has not been.

### What string does `config.language` accept?

**Free text — with exactly one place where the spelling matters.**

The language string goes two places. In the **director prompt** it is
interpolated raw (`LANGUAGE: {config.language}`), so anything Gemini understands
works: `"Indonesian"`, `"Bahasa Indonesia"`, `"Simplified Chinese"` are all fine.
Nothing validates it.

The place it matters is **font selection for subtitles**.
`renderer._SCRIPT_FONTS` does a case-insensitive **substring** match to decide
whether a non-Latin font is needed, against these keywords:

| Script | Keywords that match |
|---|---|
| CJK | `japanese`, `chinese`, `mandarin`, `cantonese`, `korean` |
| Thai | `thai` |
| Arabic | `arabic`, `urdu`, `persian`, `farsi` |
| Devanagari | `hindi`, `devanagari`, `nepali`, `marathi` |

Anything not matching falls through to the Latin candidates, which is **correct
for Indonesian** and for every European language.

So, concretely:

- **Indonesian** — write whatever you like. `"Indonesian"`, `"Bahasa
  Indonesia"`, `"id-ID"` all work, because Latin script needs no special font.
- **Chinese** — write **`"Chinese"`** (or `"Simplified Chinese"`,
  `"Mandarin Chinese"` — any string *containing* `chinese` or `mandarin`).
  **Do not write `"中文"` or `"zh-CN"`**: neither contains a keyword, so the
  matcher falls through to a Latin font and the subtitles render as blank boxes.
  There is no error — `font_warning` only fires when the script IS recognised and
  the font is missing, so an unrecognised label fails silently.

On this machine `/System/Library/Fonts/Hiragino Sans GB.ttc` is present, so
Chinese subtitles will render once the language string matches.

**Two other things to expect on a first Chinese run**, neither fatal:

- Per-phrase subtitle timing degrades to one caption per scene — Chinese has no
  spaces between words, which is the segmentation limit described above. The
  audio is unaffected.
- `split_phrases` splits on ASCII `.` `,` `?` `!`. Chinese text uses the
  full-width forms `。` `，` `？` `！`, which are **not** in that set, so the
  whole scene becomes one phrase. Harmless on the ElevenLabs path (one phrase
  means one caption, which is what you get anyway); it would flatten the Azure
  path's phrase placement. Adding the full-width marks to `split_phrases` is a
  one-line change if it becomes worth doing.

**Recommendation: try Indonesian first.** It exercises the multi-language path
end to end with zero font or segmentation risk, so anything that breaks is a real
problem rather than a known limitation.

**Field test done, and it found a bug (2026-08-02).** `run_77d71eba` asked for
Indonesian and the director returned English narration — nothing in the prompt
ever told it to write `scene.text` in the configured language. Fixed and now
validated: see [28](#28-fixed--the-director-wrote-english-narration-when-the-config-asked-for-indonesian).
Everything below is still read off the code rather than observed on video. Everything above is read off the code and measured on
strings, not observed on generated video. Update this section when the run
happens — particularly whether Gemini honours the language faithfully in
`scene.text`, and whether Veo's native audio (which the diarizer transcribes) is
usable in that language at all. That second one is the untested link: L6 depends
on transcribing what Veo said, and nobody has checked how Veo speaks Indonesian.

## 24. FIXED — no resume within a stage

**Fixed 2026-08-02.** `POST /pipeline/assets` and `POST /pipeline/scenes` take
`resume=true`: keep everything already on disk, generate only what is missing.
`POST /pipeline/music` takes it too, where it can only mean "leave the track
alone" — included so a caller replaying the whole pipeline does not have to
special-case one stage.

**The problem it solves.** `/pipeline/scenes` failing at scene 7 of 9 left seven
paid clips in `veo_raw/` and no way to use them: `force=true` regenerated all
nine and paid again, `tts_only=true` only helped if the video half had fully
succeeded. On a 9-scene run that is several dollars thrown away per flaky attempt.

**How it decides, per scene:**

| On disk | Action | Spend |
|---|---|---|
| no `veo_raw/<id>.mp4` | generate it, then L6 | Veo + audio |
| raw + `tts/<id>.done` | skip entirely | nothing |
| raw, no marker | redo the **audio half only** | audio only |

The third row is the one that matters. A crash lands almost anywhere, and most
often it lands with the expensive half done and the cheap half not.

**Why a marker file.** `scenes/<id>.mp4` cannot answer "did L6 run", because L5
writes the raw clip there as a placeholder before handing over — so a finished
scene and a half-dead one look identical. The timings sidecar cannot answer it
either: the STS path skips the sidecar when the phrase match is too sparse to
trust. `mark_scene_done` writes `tts/<id>.done` as the **last** step of both L6
paths, after `scenes/<id>.mp4` is in place, so it exists only when the finished
clip does. `scene_is_done` checks for both halves, so clearing `scenes/` to
re-render is not defeated by a stale marker.

B-Roll has no marker — there is no L6 to write one. Resume restores it from the
pristine clip and re-trims, which costs nothing and is deterministic, so there is
nothing to reason about.

**What resume deliberately does not do:** notice that the blueprint changed. It
reads file existence and nothing else, so a scene whose prompt was edited after
its clip was generated is skipped. **Editing a blueprint means `force`.** Passing
both flags is a `422` rather than a guess about which one you meant — guessing is
how you spend someone's credits by accident.

`AssetsOut`/`ScenesOut` report `reused`, so it is visible whether a resume
actually saved anything. Resuming and seeing `reused: 0` means nothing was there
to keep.

Pinned by `tests/test_resume.py` (13 tests), which counts calls rather than
inspecting output — every one of them is really asking "did we avoid paying for
something we already had".

Related: [14](#14-on-hold--nothing-about-the-current-storage-model-survives-deployment).
Once generation is a background job, partial-failure recovery becomes a job-level
concern too — but the per-scene decision above is what that job would call, so
the work is not wasted.

## 25. FIXED — `GeminiDirector.use_schema` defaulted off and nobody recorded why

**Fixed 2026-08-02: on by default.** `DirectIn.use_schema` exposes it per
request, so it can be turned back off without a code change.

`VideoBlueprint` is handed to Gemini as `response_schema`. The SDK constrains
decoding, so the reply **cannot** be malformed JSON, cannot be missing a required
field, and cannot carry a value outside a closed vocabulary — `task`, `model`,
`type`, `broll_role` and `style_type` become impossible to get wrong.

**Be clear about what it does not do, because this is where the expectation
overshoots.** `validate_blueprint`'s ~25 cross-field rules — `character_count`
matching the scenes, `reference_from` pointing at something that exists, the six
required prompt labels, the word budget, B-Roll carrying no characters — are
*semantic*, and no JSON schema can express them. **Those are the retries you
actually see.** The three failed director runs that prompted the cost
investigation failed validation, not parsing.

So the honest expectation: one class of failure removed, not most of them. Worth
having anyway, because it is free, and a parse failure is the most annoying kind —
a whole billed call thrown away over a stray markdown fence.

**On shrinking the prompt** — tempting, and worth being careful about.
`DIRECTOR_RULES` and the schema now overlap: field descriptions travel with the
schema, so the model sees some guidance twice. But most of `DIRECTOR_RULES` is
*not* shape, it is the AI RULES (no negations, wordless surfaces, style words
banned from individual prompts) and the structural rules `validate_blueprint`
enforces. **Cutting those would trade a smaller prompt for more retries** — the
opposite of the goal. If the prompt is trimmed, trim only what is now genuinely
duplicated by the schema: field-by-field type and enum descriptions. Do it as a
separate change, and watch the validation-failure rate.

**The old reason, for the record, because it was a guess presented as fact.** The
docstring claimed the schema "constrains generation in a way that has produced
blander, more literal prompts". Nobody measured that. It may still be true — if
prompt quality drops noticeably from here, that guess was right and
`use_schema: false` on a single request is the way to check, no code change
needed.

Pinned by `tests/test_wiring.py`, including a test that
`google-genai` can still render `VideoBlueprint` as a response schema — a nested
model with enums, optionals and lists of models is not guaranteed to survive an
SDK upgrade, and that failing loudly in the test suite beats it failing mid-run.

## 28. FIXED — the director wrote English narration when the config asked for Indonesian

**Found on `run_77d71eba` (2026-08-02), fixed the same day.** The first
non-English run, and it came back with English `text` in every speaking scene.

### The tell: `title` was correct

```json
"title":  "Panduan Intranet: Fitur Utama & HR",          <- Indonesian
"scenes": [{"text": "PowerOne tiles include Salesforce, Epicor Mobile,
                     AI Desktop, Pico Concierge, Partners, and Helpdesk
                     for quick access."}]                 <- English
```

One field in the requested language, the rest not. That narrows the cause to
almost nothing, because **`title` was the only field anywhere that was told to be
in the language.**

`%(language)s` appeared in exactly three places, and only one of them was an
instruction about output:

| Where | What it said | Effect |
|---|---|---|
| `DIRECTOR_RULES`, TITLE section | "3-8 words, written in %(language)s" | **worked** |
| `DIRECTOR_RULES`, scene prompt template | `Language: %(language)s` inside the Veo prompt | tells **Veo** what language to speak — nothing to do with `text` |
| system suffix | `LANGUAGE: indonesian` | a bare label with no verb; reads as metadata about the audience, not an instruction |

**Nothing told the director what language `scene.text` should be in.** The model
was not disobeying; it was never asked.

The `Language:` label did reach all ten Veo prompts, so the *characters* would
have spoken Indonesian while the *script* was English — a mismatch that would
also have broken L6, since diarization aligns Veo's audio against `scene.text`.

### The fix, in three places

1. **A dedicated OUTPUT LANGUAGE section in `DIRECTOR_RULES`** — states that
   `text` must be in `%(language)s`, that proper nouns (Salesforce, Helpdesk)
   stay as they are, and — importantly — that `prompt`, `style` and
   `music_style` stay **English**, because they instruct Veo and the image
   model rather than the viewer. Without that last clause the obvious risk is
   over-correcting and translating the Veo prompts too.
2. **The bare `LANGUAGE:` label became a sentence**: *"write every scene's
   `text` and the `title` in indonesian. Image and video prompts stay in
   English."*
3. **`Scene.text`'s pydantic Field description** now says "written in
   config.language". This is not decoration: with
   [25](#25-fixed--geminidirectoruse_schema-defaulted-off-and-nobody-recorded-why)
   turned on, these descriptions travel to Gemini as JSON-schema annotations —
   and `title`, the field that worked, is described as "short on-screen video
   title, in config.language". Same mechanism, applied to the field that needed
   it.

### And a validator, because a rule nobody checks is a suggestion

This is the repo's own principle, and the run is what it looks like when it
bites. The blueprint was **otherwise completely valid** — running
`validate_blueprint` on it today reports one problem and one only, the language.
Without the check it passed clean, and the error would have surfaced when someone
watched the finished video, i.e. **after the entire Veo bill had been paid.** The
most expensive possible place to find it.

`check_output_language` counts English function words. It answers one narrow
question — *is this English?* — which is all that is needed, because English is
the only language the model defaults to here.

Measured before choosing the threshold:

| Text | Marker ratio |
|---|---|
| The English narration from `run_77d71eba` | **0.229** |
| Indonesian, French, Spanish, German, Malay, Vietnamese | **0.000** |

Threshold **0.10** — far from both sides rather than tuned to either.

Three decisions inside it worth knowing:

- **The marker list excludes words shared with Romance languages** — `a`, `on`,
  `as`, `or`, `in`, `at`. Leave them in and French scores as English. A test
  pins this for five languages.
- **It aggregates over the whole blueprint, not per scene.** One scene of mostly
  product names ("PowerOne tiles include Salesforce, Epicor Mobile") scores low
  on its own and would slip through; across a blueprint the signal is stable, and
  one oddly-worded scene cannot fail a run.
- **It stays silent under 20 words, and on English runs.** A false positive costs
  a director retry; staying quiet is the cheaper error.

**Why not a real language detector.** `langdetect` and fasttext would be more
precise and would add a dependency to catch a failure this already separates by
0.23 against 0.00. If the check ever misfires, that is the upgrade path.

Pinned by `tests/test_output_language.py` (20 tests), including one asserting the
golden English blueprint still validates clean.

### Still unverified

The fix is not confirmed against a live run yet — it is confirmed against the
*failed* one, which the new check now catches. What remains untested is the rest
of the multi-language chain, and the untested link is L6: **it transcribes Veo's
native audio to time the subtitles, and nobody has checked how Veo speaks
Indonesian.** If the delivery is poor the diarizer's match rate drops and the
layer degrades to its fallback — intelligible, worse lip-sync. See
[18](#18-fixed--what-_norm-does-to-non-latin-scripts-and-what-it-actually-breaks)
for what else to watch on a non-English run.

## 29. FIXED — subtitles strobed, and one caption was silently dropped

**Found in `run_77d71eba` on 2026-08-03 while reviewing the first Indonesian
video, fixed the same day.** The run had already been signed off — the defect is
invisible unless you look at `subs.srt`.

### What shipped

```
0.50s  PowerOne tiles
0.50s  termasuk Salesforce
0.50s  Epicor Mobile
0.52s  AI Desktop
0.78s  Pico Concierge
0.50s  Partners
0.00s  dan Helpdesk            <-- in the SRT, never on screen
0.50s  memberikan akses cepat.
7.18s  Mengklik tile seperti Helpdesk akan membawa Anda...
```

Eight captions inside one 8-second scene, median 0.35s before flooring, against a
readable minimum of about 1s. And the same video's next scene held **one** caption
for 7.18s. Three separate causes, all now fixed.

### Cause 1 — comma splitting outlived its reason

`split_phrases` split on commas by default. That existed for the **Azure** path,
where each phrase is re-synthesised separately and placed at its own diarized
timestamp, so finer phrases meant tighter lip-sync. **ElevenLabs speech-to-speech
places no audio at all** — it converts the whole clip and inherits Veo's timing,
so phrases only ever time subtitles. On the default path, comma splitting bought
nothing and cost readability. A comma-heavy list became a caption per list item.

`split_on_comma` now defaults to **False**. `external_tts` (Azure) passes `True`
explicitly, because there the trade-off is still real.

### Cause 2 — the clamp could destroy a cue

`build_srt` floored every cue at `MIN_CUE_MS` (500ms), then clamped each cue's end
to the next cue's start with a floor of `a + 1` — one millisecond.

When words at the end of a phrase fail to match the transcript, `phrase_spans`
falls back to the nearest word it *did* match — so several trailing phrases can
collapse onto the **same** start. The clamp then shortened the earlier one to
1ms. It was written to the SRT and never reached the screen.

**Corrected 2026-08-03.** This was first diagnosed as the transcript stopping
early — "5.84s of a 7.99s track". Re-measuring disproved it: the diarizer
transcribed the whole clip (last word ends 7.72s). What actually happened is that
it **mis-transcribed the tail**. The narration ends "memberikan akses cepat" and
the transcript reads "Numerikan Access Chapat", so those words matched nothing,
and 5.84s was simply the last word that *did* match. Same symptom, different
cause, and the distinction matters: a truncated transcript would be a model
limit, whereas garbled words are a normalisation-and-matching problem that our
own phrase boundaries make better or worse (see Cause 1).

The floor and the clamp contradicted each other and the clamp won. Cues closer
together than `MIN_CUE_MS` are now **merged** before clamping — two phrases at the
same instant *are* one caption, and merging keeps the words instead of losing
them. Cues that merely overlap are still clamped, which is correct.

Worth being clear about what was and was not the model's fault: the truncated
transcript is diarizer behaviour and will recur. **The lost text was ours.** A
short transcript should degrade to coarser captions, never to missing ones.

### Cause 3 — nothing wrapped long lines, and removing commas exposed it

With comma splitting off, captions became whole sentences — 90 to **127 display
columns** in this content, against a broadcast norm of ~42 per line. Nothing in
the renderer wraps: `_burn_boxed` clamps the *box* to frame width but never the
text, and the `ass` method sets `WrapStyle 2` (explicit breaks only). An unwrapped
caption is drawn straight off the edge of the picture.

Comma splitting had been hiding this by never producing a long caption. Removing
it exposed the gap rather than creating it.

`_wrap_caption` now breaks captions in `build_srt`, so the SRT deliverable and
every burn method get it:

- measured in **display columns**, not characters — a CJK glyph is two columns
  wide, so 26 Chinese characters are as wide as 52 Latin ones and `len()` would
  let them overflow;
- **fewest lines first, then balanced** within that count. Wrapping at the ideal
  average alone costs an extra line, because words do not divide evenly; greedy
  wrapping alone strands the remainder ("...memberikan akses" / "cepat."), which
  reads as a mistake;
- **CJK is chunked by width**, since there are no spaces to break on.

### Result on the real run

| | Before | After |
|---|---|---|
| Captions in scene 1 | 8, median 0.35s | 1, wrapped to 4 balanced lines |
| Lost text | "dan Helpdesk" | none |
| Longest line | 127 columns, unwrapped | 42 columns |

**Every already-generated run benefits** — rendering is free and re-runnable, and
this changes only `build_srt` and the phrase split, both downstream of anything
billable.

Pinned by 9 tests in `tests/test_renderer_srt.py` and 4 in
`tests/test_audio_alignment.py`.

### Measured: why comma splitting was the trigger

Re-running the same audio through the diarizer and feeding the result to
`phrase_spans` both ways isolates it exactly. Same clip, same transcript, only the
phrase boundaries differ:

| Split | Phrases | Degenerate spans | Last phrase ends |
|---|---|---|---|
| on commas (old default) | 8 | **2** | 5.90s |
| sentences only (new default) | 1 | **0** | 5.90s |

The reason is arithmetic. `phrase_spans` needs the **first and last word of every
phrase** to match the transcript. Eight phrases means sixteen boundary words that
must survive transcription; one phrase means two. The transcript garbles the tail
either way — but with one phrase the only casualty is an end time that is 2s
early, while with eight, two whole phrases collapse onto one instant and the
clamp destroys one of them.

So comma splitting was not merely a readability choice. **It multiplied the
pipeline's exposure to imperfect transcription**, which is the more serious of the
two reasons to leave it off.

### Still open

**FIXED 2026-08-03 — the last caption is now held to the end of speech.**
`phrase_spans` takes `speech_end_ms` and extends the final phrase to it. That is
the same value the caller trims the video to, so a caption can never run past its
clip.

Measured on both Indonesian scenes with the `base` model:

| Scene | Before | After |
|---|---|---|
| s1 (garbled tail) | ends 5.90s of 7.99s — **2.09s short** | **0.00s short** |
| s2 (clean transcript) | ends 7.18s of 7.99s — 0.81s short | **0.00s short** |

Two things worth noting. It repairs the *symptom* without needing the transcript
to be good, which is what makes staying on the `base` model reasonable. And it
fixes s2 as well — that scene's captions were correct but the video ran 0.81s
past the last word, so the caption now holds through the wordless tail instead of
leaving a gap before the cut. Holding a caption a beat past the audio is normal
subtitle practice anyway.

Only the LAST phrase is extended; stretching an interior one would overlap its
neighbour, and its end is a real measurement. The value is a floor, not a clamp —
a last word that genuinely runs past the detected speech end keeps its own
timing.

Side effect, deliberate: `detect_speech_end` now runs BEFORE diarization, so a
silent clip fails without paying for a diarization call first.

Pinned by 5 tests in `tests/test_audio_alignment.py`.

**Transcription quality in non-English is the underlying limit — decision
2026-08-03: stay on `base`.** Measured on the two Indonesian scenes:

| Scene | Model | Words matched | Caption ends short by |
|---|---|---|---|
| s1 (garbled tail) | `base` | 9/16 — 56% | **2.09s** |
| s1 | `small` | 13/16 — 81% | **0.25s** |
| s2 (clean) | `base` | 14/15 — 93% | 0.81s |
| s2 | `small` | 14/15 — 93% | 0.77s |

`small` nearly eliminates the shortfall on a scene whose tail transcribes badly,
for ~3x the CPU (1.8s vs 0.6s per scene, against a ~60s Veo call) and a 460MB
model instead of 150MB. Passing `language="id"` changed nothing on either — model
size is the lever, not the hint.

Staying on `base` is reasonable **provided the last-phrase fix above is done**:
extending the final phrase to the detected speech end repairs the same symptom in
the code, for free, on every model. Without that fix, `base` leaves ~2s of
uncaptioned speech whenever a sentence ends in words it mangles.

Note this affects **timing only** — caption text comes from `scene.text`, so
viewers never see the garbled words. That is why a mediocre transcript is
survivable at all.

**Caption width: settled at 64 columns, not 42** (2026-08-03). The first
implementation used the broadcast convention of ~42 characters per line, and the
result was rejected on sight — a four-line block stacking up the frame. Those
guides are written for cinema and TV, where the title-safe area is far tighter
than a 1280px video played in a browser.

| Width | Longest sentence | Widest box |
|---|---|---|
| 42 | **4 lines** | 438px (34% of frame) |
| 64 | **2 lines** | 750px (59% of frame) |

At 64 both Indonesian sentences fit two lines, and a typical Chinese caption fits
**one**. 59% of frame width reads as a normal subtitle. Raise it only if captions
start touching the edges.

`_wrap_caption` resolves the width at call time rather than as a default
argument, because a default is bound at import and silently ignores any override
— which caught the author while producing the comparison above.

---
---

# CLOSED — decided, deliberately unchanged

## 1. DECIDED — placeholder `char_id`s leak into Veo prompts

**Decision 2026-07-31: not fixing.** Revisit if prompt quality becomes a suspect
in a character-consistency investigation
([15](#15-decided--character-consistency-is-good-enough-and-the-storyboard-is-really-a-reference-panel)).

**Confirmed in code and observed in every committed run.**
`patch_blueprint.apply_selection` rewrites placeholder ids in
`scene.characters`, `scene.audios[].char_id` and `storyboard[].reference`. It
does **not** touch `scene.prompt` or `storyboard.prompt`, so when the director
writes a placeholder into prose the raw token reaches Veo:

```json
"characters": ["char_realism_informal_male"],
"prompt": "Cinematography: A medium shot of char_narrator, framed from the chest up..."
```

Frequency (re-measured 2026-07-31 after an earlier case-sensitive regex missed
mixed-case ids like `char_HR_rep`):

| Run | Placeholder | Scenes with the id in `prompt` |
|---|---|---|
| `run_0616c9e5` | `char_narrator` | 8 of 9 |
| `run_d852054a` | `char_HR_rep` | 1 of 2 |
| `run_ff808532` | `char_narrator` | 2 of 3 |

Every run leaks, in most speaking scenes.

**Why not fixing:** Veo takes the character's appearance from the attached
reference image, not the prompt text, so impact is judged small — though that is
an **assumption**; nobody has A/B'd a leaked prompt against a clean one. The
cheapest clean fix spends retries on every run.

Three options if it is ever revisited, none obviously right:

- Rewrite prompt prose in `apply_selection`. Needs longest-key-first ordering or
  a `\b`-anchored regex to avoid substring collisions (`char_hr` inside
  `char_hr_rep`). Note it leaves a *different* database key in the prose —
  `char_realism_informal_male` reads no better than `char_narrator`.
- Have `validate_blueprint` reject placeholder ids in prompt fields, forcing real
  prose. Cleanest, fits the feedback-loop design, costs retries.
- Substitute the library `description` instead of the id, turning the leak into
  useful signal. Better output, no retry cost — but a character named three times
  gets the full description three times.

Pinned by `tests/test_patch_blueprint.py::test_prompt_prose_is_not_rewritten`.

## 4. DECIDED — `"illustration"` blueprint type is specced, not implemented

**Decision 2026-07-31: on hold**, pending a product decision on whether short
motion-graphics videos are in scope. Not an oversight.

`change3_short_animation.md` describes a third `blueprint_type` with its own
director layer for 10–15s motion-graphics videos with no characters, no
storyboards and no TTS. None of it exists — `schemas.BlueprintType` is
`("opening", "summary")` and `director.BLUEPRINT_TYPES` agrees.

Adding the enum value alone would be **worse than not adding it**: it would pass
config validation and then fail somewhere downstream. `read_blueprint_type` falls
back to `"opening"` for anything unrecognised, which is why a half-added type
degrades quietly rather than erroring.

See [extending.md](extending.md#add-a-new-blueprint_type) for what it actually
needs. Flagged in `schemas.py`.

## 6. DECIDED — two `character_count` invariants that can conflict

**Decision 2026-07-31: current behaviour is correct** — different roles require
different characters.

The count is checked twice against different meanings:

- `validate_blueprint` — did the director count its own placeholders correctly.
- `patch_blueprint._check_resolved` — did the user's selection preserve the
  number of distinct characters.

The second fails if a user maps two placeholders onto the **same** library
character, because the distinct count drops. Rejected with a message that says so
("selection collapses/expands distinct roles").

**Front-end implication:** the character picker must prevent selecting the same
character for two roles. Server-side rejection is right, but it should not be how
the user discovers the rule — they would hit a 422 that reads like a bug.

Pinned by
`tests/test_patch_blueprint.py::test_collapsing_two_roles_onto_one_character_is_rejected`.

## 12. DECIDED — `align_starts` is non-monotonic by design

**Originally logged 2026-08-01 as a bug. That was wrong — corrected the same day
after the author explained the intent.** Recorded here because the rationale had
never been written down, which is exactly how it came to be mistaken for a
defect.

**The design.** Azure re-synthesises the script as separate phrase clips, and
Azure's delivery speed does not match the speed Veo animated the mouth at. The
lip movement for phrase N+1 therefore often begins *before* the Azure audio for
phrase N has finished. Each phrase is pinned to the lips it belongs to, even when
that means starting before its predecessor ends. Overlapping audio is preferred
over audio that visibly lags the mouth.

Clamping monotonic would trade correct lip-sync for tidier numbers. **Do not add
the clamp.** The `prev` variable is the fallback for a phrase that matches
nothing, not a vestigial clamp.

**What was actually wrong** was the docstring, which claimed "starts are clamped
monotonic". Fixed — it now carries the rationale.

**Scope.** `align_starts` is called *only* from `external_tts.process_scene`, the
Azure path. The ElevenLabs path calls `phrase_spans` and never places audio by
timing, because STS preserves Veo's own timing. Subtitles are unaffected either
way: `renderer.build_srt` sorts cues and clamps overlaps independently.

**Remainder still open — gross vs small disorder.** The design assumes disorder
is *small*: the natural overlap of a phrase starting slightly early. It cannot
distinguish that from *gross* disorder caused by a bad transcript, where a phrase
lands seconds before its predecessor and the narration plays scrambled rather
than overlapped:

```python
align_starts(["Hello there.", "How are you?"], <badly out-of-order timings>, ...)
# -> [5000, 100]
```

Gemini's word timings are not guaranteed monotonic; `bench/diarize_local.py`
counts "out-of-order word starts" as a sanity metric. Nothing detects the
difference.

If it ever matters, the fix is *not* a clamp — it is to treat gross disorder as
evidence the transcript is untrustworthy and trip the existing sequential
fallback, the same way a sub-50% match already does. Low priority while
ElevenLabs is the default.

Pinned by
`tests/test_audio_alignment.py::test_out_of_order_diarization_is_preserved_not_clamped`.

## 13. DECIDED — 16:9 locked at the config layer

**Decision 2026-08-01: lock output to 16:9** rather than fix the portrait path
now.

`ConfigIn.aspect_ratio` is `Literal["16:9"]`, so a `9:16` request is a 422 rather
than a run that silently produces something broken.

**Why portrait was broken.** `main._image_client()` builds the image client with
no aspect ratio, and `main.py` never reads `config.aspect_ratio` at all:

```python
def _image_client():
    from app.pipeline.image_gen import GeminiImageClient
    return GeminiImageClient(make_client())      # aspect_ratio defaults to "16:9"
```

So a `9:16` run generated **landscape** backgrounds and storyboards, then handed
them to Veo asking for portrait video. `image_gen.main()` (the CLI) does accept
`--aspect-ratio`; only the API path was affected.

Two related gaps the lock also makes moot:

- `TransitionEntry.aspect_ratio` is parsed by `load_transition_index` and **never
  read**. `apply_transitions` does `scale={W}:{H}` unconditionally, so a 16:9
  overlay on a 9:16 video would stretch, not letterbox. All five bundled overlays
  are 1920×1080.
- Mixed `aspect_ratio` across scenes is schema-legal; `merge_scenes` normalises
  everything to the *first* scene's geometry with scale+pad, silently
  letterboxing the rest. Nothing validates uniformity.

**The lock is deliberately only at the config layer.** `Scene.aspect_ratio`, the
`AspectRatio` literal and the Veo plumbing all still carry both values — so
unlocking is this one line plus passing config through in `_image_client`, not a
re-implementation.

**Gap found and closed 2026-08-04.** The config lock stopped the *user* asking
for portrait. It did not stop the *director* emitting it:

- `DIRECTOR_RULES` showed the scene schema as a hardcoded
  `"aspect_ratio": "16:9" | "9:16"` — not interpolated, so the model was
  explicitly told both were available. (`%(aspect)s` was used, but only in the
  background and storyboard image prompts.)
- `validate_blueprint` never mentioned `aspect_ratio` at all.

So a `9:16` scene would pass pydantic, pass validation, and reach Veo — asking
for portrait video against landscape reference images. The repo's own principle,
one more time: a rule stated nowhere and checked nowhere is not a rule.

It had never actually happened — **203 of 203 scenes** on disk are `16:9`,
because `Scene.aspect_ratio` defaults to `16:9` and that default travels to
Gemini as a schema annotation ([25](#25-fixed--geminidirectoruse_schema-defaulted-off-and-nobody-recorded-why)).
A coin that had landed the same way 203 times, not a coin that could not land the
other way.

Two lines fixed it: the prompt now states `%(aspect)s` instead of offering a
choice, and `validate_blueprint` rejects any scene whose ratio differs from
`config.aspect_ratio`. Checked against **config**, not a hardcoded `16:9`, so it
stays correct if portrait is unlocked — and it doubles as the **uniformity**
check the renderer needs, since `merge_scenes` silently letterboxes any scene
that differs from the first.

Pinned by 4 tests in `tests/test_director_validation.py`.

## 15. DECIDED — character consistency is good enough, and the storyboard is really a reference panel

**Decision 2026-08-02: not investigating. Output quality is accepted by the
manager.** Recorded because the mechanism is worth understanding when something
does eventually drift.

**How it works.** Nothing generates a character. The user's library PNG is
attached as a reference image to each storyboard the character appears in, and
that storyboard is attached to Veo as the reference for the speaking scene:

```
character_library/<char_id>.png  →  storyboard/<sb_id>.png  →  scenes/<id>.mp4
```

**Observed in practice:** Gemini takes the attached character well and the
storyboard does not drift much. The one failure seen, and it is rare, is the
**background** drifting — the storyboard's environment coming out unrelated to
the background plate the blueprint generated. Character likeness has not been the
problem; environment continuity occasionally is.

**The storyboard is not doing what its name says, and that is now understood
rather than suspected.** The 2×2 grid with per-panel timecodes was meant to
direct how eight seconds develop. Veo does not respect the panels. In practice it
reads the grid as **a reference panel** — a source of character appearance,
wardrobe and general composition — and ignores the temporal structure. So the
storyboard earns its keep as a likeness carrier, not as a storyboard.

That reframing is what makes
[27](#27-on-hold--would-a-single-composed-reference-image-replace-the-storyboard)
worth asking: if the grid is only ever used as a reference panel, a single
composed image might do the same job for less money. That is tracked separately.

**Not measured, and deliberately so:** there is no similarity check, no failure
signal, no metric on either hop, and drift along a `scene:<prev>` continuation
chain accumulates — scene 3 of a topic is two generations from the storyboard.
None of that has caused a problem worth the cost of building a measurement for.

Related: [1](#1-decided--placeholder-char_ids-leak-into-veo-prompts) was left
unfixed on the assumption that prompt text does not drive appearance. Nothing
here contradicts that.

## 17. DECIDED — the 50% match threshold is a judgement call, and stays one

**Decision 2026-08-02: leave it. Nothing has gone wrong that it would have
caught.** Rewritten because the original entry did not explain the mechanism.

### What the threshold is

When L6 places audio, it has two lists of words:

1. **What we meant to say** — `scene.text`, split into phrases.
2. **What was actually heard** — the diarizer's transcript of the Veo clip, each
   word with a millisecond start and end.

Alignment matches list 1 against list 2 by content (`difflib`), so each phrase can
be placed at the timestamp of the word it starts with. That is what keeps the
voice on the lips.

But the transcript can be wrong — Veo's audio is muddy, or the model mis-hears.
So `align_starts` counts how many words matched, and:

- **more than 50% matched** → trust the alignment completely, place every phrase
  at its matched timestamp;
- **50% or fewer** → throw the whole alignment away and play the phrases
  back-to-back from the detected speech onset.

The fallback is worse lip-sync but stays intelligible and stays inside the clip.

### What the open question was

**Nothing handles the middle.** At 60% matched, the alignment is trusted
completely — including the 40% of phrases positioned by guessing from their
nearest matched neighbour. There is no "matched, but poorly" state, and no signal
that a scene came out mediocre rather than fine or obviously broken. The 50%
number itself was chosen by judgement; nobody measured what a good transcript
scores versus a bad one.

### Why it stays

Three reasons, in order:

1. **It only affects the Azure path.** ElevenLabs is the default and does not
   call `align_starts` at all — STS preserves Veo's own timing, so there is
   nothing to align. This threshold governs the backup.
2. **Both outcomes are acceptable.** Trusted-but-mediocre gives slightly loose
   lip-sync. The fallback gives sequential audio. Neither produces a broken
   video, and neither wastes money.
3. **Tuning it needs data nobody has.** The right move is not to pick a different
   number — it is to log the ratio across real runs and look at the distribution.
   That is a measurement exercise, and there is no symptom motivating it.

Related: [18](#18-fixed--what-_norm-does-to-non-latin-scripts-and-what-it-actually-breaks)
made the ratio slightly more honest — empty tokens no longer count toward a match.
And a script with no word spaces trips this threshold every time, which is
documented there as intended graceful degradation.

## 22. DECIDED — `min_scene_seconds` is advisory, and enforcing it would be worse

**Decision 2026-08-02: leave it advisory.** Your read was right — scene text is
not too short in practice — and measuring it turned up something more useful than
the answer to the original question.

**Confirmed in code.** `min_scene_seconds` is interpolated into the director
prompt as `min_words` but never checked by `validate_blueprint`. A 3-word
speaking scene would pass.

### What the blueprints actually say

Measured across **94 unique speaking scenes** in every blueprint on disk:

```
  5w  ###                      3          median 13 words
  6w  ##                       2          mean   12.5
  7w  ##                       2          min     5
  8w  ####                     4          max    18
  9w  ###                      3
 10w  ########                 8          implied floor (6.0s x 2.5 wps) = 15
 11w  ####                     4          implied ceiling               = 20
 12w  ###################     19
 13w  #############           13
 14w  ##############          14
 15w  #########                9
 16w  #####                    5
 17w  ###                      3
 18w  #####                    5
```

**77% of speaking scenes fall below the implied 15-word floor.** Enforcing it
would reject essentially every blueprint the director has ever written, and since
it writes ~12.5 words consistently, the retry loop would not converge — it would
burn its attempts and 502. That is the opposite of a saving.

### The real finding: `words_per_second` is miscalibrated

The director never breaches the 20-word ceiling and never reaches the 15-word
floor. It is not ignoring the rules; **the floor is derived from a pace that does
not match reality.**

Measured on rendered clips — the L6-trimmed output next to the pristine Veo clip:

| Scene | Words | Veo raw | After L6 trim |
|---|---|---|---|
| `run_26b13053/scene_intro_1` | 18 | 8.00s | **7.99s** |
| `run_652b50f7/scene_payroll_contact` | 14 | 8.00s | **7.99s** |
| `run_652b50f7/scene_gather_proof` | 13 | 8.00s | **7.99s** |

13-18 words **fill the whole 8 seconds**. Real delivery is therefore ~1.6-2.25
words/second, not the 2.5 in `DirectorConfig`. The director's ~12.5 words is
correct for an 8s clip; the config arithmetic is what is wrong.

**This also kills an optimisation that looked attractive.** If speech only ran
~5s, speaking scenes could be generated at Veo's 6s step instead of 8s and save
25% on every one. The measurement says no — dropping to 6s would truncate the
narration. Recorded so nobody proposes it again from the config numbers alone.

### What is left, and why it is not urgent

There is a genuine short tail: **7 of 94 scenes (7%) are under 8 words**, e.g.
`"This applies unless otherwise specified."` (5 words). Those trim to ~3s of an
8s clip, so roughly $0.50 of paid Veo is discarded per occurrence — about $1 on a
22-scene run. Real, small, and not worth a validator rule that risks the retry
loop.

**If it is ever picked up**, the floor to enforce is **8-10 words** (which only
15% of scenes breach), not 15 — and `words_per_second` should be corrected to
~2.0 first, so the prompt and the validator agree about what a scene is.
