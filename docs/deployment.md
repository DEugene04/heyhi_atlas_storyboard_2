# Deployment and persistence

**What happens to a run after it finishes, and what has to change before real
users can rely on it.**

Written 2026-08-01 against the code as it stands, updated 2026-08-02. Nothing
here is implemented — this is the gap analysis, not a description of a system that
exists.

**On hold pending the IT conversation** (open-questions #14). No deployment code
changes until that meeting has happened;

---

## 1. Where things live today

Everything is the **local filesystem of whatever machine runs the API**:

```python
# main.py
STORAGE_ROOT   = os.environ.get("ATLAS_STORAGE_ROOT", "_storage/atlas_storyboard")
CHAR_LIB_ROOT  = os.environ.get("ATLAS_CHAR_LIB",  <STORAGE_ROOT>/../character_library)
AUDIO_LIB_ROOT = os.environ.get("ATLAS_AUDIO_LIB", <STORAGE_ROOT>/../audio_library)
```

All three are env-overridable, and the default is **relative to the working
directory**. There is no database of any kind.

Confirmed absent from the codebase as of this date:

|                            | Status                                                                                                                   |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Database                   | none                                                                                                                     |
| Auth / accounts / sessions | none — no endpoint checks who is calling                                                                                 |
| S3 / object storage        | none. `.env` carries `HEYHI_S3_BUCKET`, `HEYHI_AWS_ACCESS_KEY_ID` etc. and **no code reads them** — planned, never built |
| Background jobs / queue    | none — every endpoint is synchronous                                                                                     |
| File locking               | none                                                                                                                     |

## 2. Does yesterday's video still exist?

Entirely a property of the disk. Nothing in the pipeline expires, cleans up or
backs anything up.

| Deployment                                           | Survives?                                    |
| ---------------------------------------------------- | -------------------------------------------- |
| VM or container with a **mounted persistent volume** | **Yes**, indefinitely — and it grows forever |
| Plain container, serverless, Heroku-style dyno       | **No.** Lost on restart or redeploy          |
| Local laptop (today)                                 | Yes, until manually deleted                  |

Sizing, measured from the runs in `_storage/`: **roughly 8MB per scene**, so a
16-scene video is ~130MB. Nine mostly-partial runs came to 355MB before the
2026-07-31 cleanup. At 100 videos/month that is ~13GB/month accumulating with no
retention policy.

## 3. Why "the file persists" is not enough

Even on a persistent disk, a user cannot meaningfully _own_ a video:

1. **No ownership.** Nothing maps a person to a run. There is no data from which
   "show me my videos" could be answered.
2. **No listing.** No endpoint enumerates runs. You must already hold the
   `run_id`.
3. **`run_id` is an unguessable bearer token with no expiry.** It is
   `uuid4().hex[:8]` — 4.3 billion values, so not enumerable in practice, but
   anyone who obtains one has _full_ control of that run. Including `force=true`,
   which regenerates and **spends API credits**.

`_run_dir()` validates the _shape_ of a `run_id` and prevents path traversal. It
performs no authorization, because there is no identity to authorize against.

## 4. The blocker that has nothing to do with storage

`/pipeline/scenes` runs the whole Veo + L6 loop **synchronously inside one HTTP
request**. For a 16-scene video: 16 Veo generations, each polling up to
`DEFAULT_TIMEOUT_S = 600`, plus per-scene STS and diarization. Tens of minutes.

Common gateway timeouts: ALB 60s, Cloudflare 100s, nginx 60s.

**This must become a background job before the pipeline can be deployed at all**,
regardless of where files are stored. `/pipeline/assets`, `/pipeline/direct` and
`/pipeline/render` are shorter but not instant either.

Related: with no file locking, two concurrent requests against the same `run_id`
race on the same files. Today that is prevented only by nobody doing it.

**Partial mitigation, added 2026-08-02.** `resume=true` on `/pipeline/assets` and
`/pipeline/scenes` keeps every artifact already on disk and generates only what is
missing (open-questions #24). A request that dies at scene 7 of 9 — timed out by a
gateway, killed by a redeploy — no longer costs seven clips to recover from.

This matters for the deployment conversation in two ways:

- It **lowers the cost of the timeout problem** without solving it. A 30-minute
  request still cannot survive a 60-second gateway, but the money is now
  recoverable. That makes a pilot on imperfect infrastructure viable.
- It makes the **persistent volume load-bearing rather than merely nice**. Resume
  reads the run directory; on ephemeral storage there is nothing to resume from
  and the spend goes with the container. This is the concrete argument for the
  volume in Stage 1.

When the job queue lands, per-scene resume is what its retry should call — the
decision logic already exists, so the queue only has to decide _when_ to re-invoke
it, not _what_ to skip.

## 5. Recommended staging

Three concerns are usually conflated. Only one of them wants a database.

| Concern                                | Belongs in                  | Size             |
| -------------------------------------- | --------------------------- | ---------------- |
| mp4 / png / wav artifacts              | **Object storage** (S3/GCS) | ~130MB per video |
| Owner, title, status, created_at, cost | **Postgres**                | ~1KB per run     |
| Which stage a run is at                | A job record                | tiny             |

Do **not** put video in the database.

### Stage 1 — persistent volume + job queue

Single VM or container with a mounted disk; point `ATLAS_STORAGE_ROOT` at it.
**Zero code changes for storage** — the env override already exists.

The work here is the job queue, not the storage. Enough for a pilot with trusted
users.

The volume is not optional in the way it might look. Per-scene `resume` (§4) reads
the run directory to decide what still needs generating, so on ephemeral storage
there is nothing to resume from and a failed run's spend is simply gone. Persistent
disk is what turns a crash from an expense into an inconvenience.

### Stage 2 — Postgres for metadata only

Files stay on disk. One table is enough to start:

```
runs(run_id PK, owner_id, status, blueprint_type, title,
     created_at, updated_at, total_cost_usd)
```

This is what buys ownership, listing and auth. Self-contained, and it does not
touch the pipeline — the run directory stays the source of truth for artifacts,
with the DB as an index over it.

Note the pipeline's existing design helps here: `cost_log.jsonl` and
`costs.summarize()` already produce the cost figure, and stage transitions are
already observable as file writes.

### Stage 3 — blobs to object storage

The invasive one, and the reason it is last: **ffmpeg needs real local files.**

- `ffmpeg_utils` operates entirely on filesystem paths.
- `renderer` chains five passes over local intermediates
  (`merged.mp4` → `merged_title.mp4` → `merged_trans.mp4` → …).
- `scene_gen` reads the _previous_ scene's mp4 to extract its last frame as the
  next scene's seed.

So this is not a matter of swapping a path for a URL. Either mount object storage
as a filesystem (FUSE — slow and fragile for this workload) or add a
download-work-upload wrapper at each layer boundary, demoting `_storage/` to
scratch space. The second is more work and far more predictable.

A useful intermediate: keep generation on local disk and upload only the **final
deliverables** (`final.mp4`, `subs.srt`) to object storage, serving users from
there. That gets durable, CDN-servable output without touching the pipeline's
internals.

## 6. Talking to the supervisor and IT

### 6.1 Measure these three numbers first

Do this **before** the meeting. Without them the conversation stays abstract, and
these are the numbers IT will actually ask for.

| Number                             | How                                                                               | Why they need it                                                     |
| ---------------------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| **Wall-clock time for a full run** | Time each endpoint on one real video. `/pipeline/scenes` is the one that matters. | Decides whether a job queue is needed and what timeout budget to set |
| **Cost per finished video**        | `GET /pipeline/cost?run_id=...` after a complete run                              | The budget conversation. Also the per-video cap, if there is one     |
| **Disk per finished video**        | `du -sh _storage/atlas_storyboard/<run_id>`                                       | Storage sizing and retention                                         |

From existing runs, disk is roughly **8MB per scene** (~130MB for a 16-scene
video).

**Time is now partly measured** (2026-08-03, recorded during the multi-language
runs):

| Stage               | Per unit  | 9-scene run              | 16-scene run        |
| ------------------- | --------- | ------------------------ | ------------------- |
| L4 image generation | ~12s each | ~6 images → **~1.2 min** | ~10 images → ~2 min |
| L5+L6 per scene     | ~60s each | **~9 min**               | **~16 min**         |
| L8 render           | —         | **~6s**                  | ~10s                |
| **Full run**        |           | **≈11 min**              | **≈18 min**         |

This is the number IT asks for first, and it settles the argument in §4: an
18-minute synchronous request against a 60-second gateway timeout is not a
tuning problem, it is a design one. Bring the table rather than the adjective.

The render being ~6s is worth mentioning too — it means re-rendering is
effectively free, which is why subtitle and transition changes can be iterated on
finished runs without regenerating anything billable.

Cost is now measured on every layer and broken down by model, so this is a
one-command answer rather than an estimate. Expect video to be ~95% of it. Have
§6.5 ready in case the follow-up is "can that be lower".

### 6.2 Bring this

**What it is.** One paragraph plus the pipeline diagram from
[architecture.md](architecture.md). Emphasise that it is a _generation_ system,
not a CRUD app: requests are long, expensive, and non-idempotent.

**Runtime profile.**

|                            |                                                                                                                                                                                                                                                                                                                                                                              |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Language                   | Python 3.11+ (3.13 in practice)                                                                                                                                                                                                                                                                                                                                              |
| Framework                  | FastAPI + uvicorn                                                                                                                                                                                                                                                                                                                                                            |
| **Non-pip dependency**     | **`ffmpeg` and `ffprobe` must be on PATH.** Not installable via pip. This surprises people — raise it early                                                                                                                                                                                                                                                                  |
| Fonts                      | A TTF for subtitles; non-Latin languages need script-capable fonts (`renderer._SCRIPT_FONTS`)                                                                                                                                                                                                                                                                                |
| GPU                        | Not needed                                                                                                                                                                                                                                                                                                                                                                   |
| CPU                        | Mostly idle during generation (waiting on APIs); CPU-bound during the render passes                                                                                                                                                                                                                                                                                          |
| Memory                     | Modest, but ffmpeg filtergraphs on long videos can spike — see open-questions #23                                                                                                                                                                                                                                                                                            |
| Disk                       | ~8MB per scene, plus intermediates during render                                                                                                                                                                                                                                                                                                                             |
| **Conditional: local ASR** | If the diarizer default moves to `whisper` ([#16](open-questions.md#16-fixed--whisperdiarizer-was-not-selectable)), L6 gains a real CPU workload and a **model cache**. `faster-whisper` downloads ~150MB on first use — bake it into the image or mount a cache volume, or every cold start re-downloads it. Raise it as a _conditional_ requirement; it is not decided yet |

**Outbound network** — IT will want this for firewall rules:

| Host              | Purpose                                   | Required?                     |
| ----------------- | ----------------------------------------- | ----------------------------- |
| Google Gemini API | director, images, Veo, Lyria, diarization | **Yes**                       |
| ElevenLabs API    | speech-to-speech, voice list              | **Yes** (default audio path)  |
| Azure Speech      | TTS                                       | Only if `tts_provider=azure`  |
| kie.ai            | PDF-review character images only          | Only for PDF review           |

**Environment variables:** `HEYHI_GEMINI_API_KEY`, `HEYHI_ELEVENLABS_API_KEY`,
plus `HEYHI_AZURE_SPEECH_KEY`/`_REGION` if Azure stays. Path overrides:
`ATLAS_STORAGE_ROOT`, `ATLAS_CHAR_LIB`, `ATLAS_AUDIO_LIB`,
`ATLAS_TRANSITIONS_DIR`. See
[open-questions #26](open-questions.md#26-on-hold--env-carries-keys-nothing-reads)
for keys in `.env` that are read by nothing and should not be provisioned — the
S3 group is kept deliberately, pending the AWS decision this meeting will make.

**Dependency pinning.** `requirements.txt` to install from,
`requirements.lock.txt` for a known-good set. Worth mentioning because the lock
was taken from a conda environment and `pip freeze` produces an uninstallable
file there — if IT builds the image, they need to know that.

**Current state, stated plainly.** Lead with this rather than letting it emerge:
no auth, no database, no object storage, synchronous long requests, no CI. All
known, all logged, none of it accidental. Being upfront about the gaps reads as
competence; having IT discover them reads as the opposite.

**The staged plan** from §5 above.

### 6.3 Questions to ask

**Scope — ask first, everything else depends on it**

- When does this need to be live, and for whom? Internal pilot or customer-facing?
- How many videos per month at launch? In a year?
- Standalone service, or does it slot into an existing HeyHi product?
- Who are the users — HeyHi staff, client admins, or end learners?

**What already exists — reuse beats building**

- What do we run on? AWS / GCP / Azure? Kubernetes, ECS, or plain VMs?
- Is there an existing Postgres I can add a table to?
- Existing object storage plus CDN?
- **Existing auth/identity system?** Do not build auth — find out what to
  integrate with.
- Existing job queue or worker tier? (Celery, SQS, Cloud Tasks, anything)
- What CI do we use? (relevant: the repo has tests but no CI)

**The long-running job problem — raise this explicitly**

- Video generation takes tens of minutes in a single request today. What is our
  standard pattern for long-running work?
- What is the gateway/load-balancer timeout?
- Is there a worker tier, or does this need one?
- How do we surface progress to a user waiting on a 30-minute job?

**Storage and retention**

- Where do generated videos live, and whose budget pays for that storage?
- How long do we keep them? Is there a company retention or deletion policy?
- Do users need to download them, or are they served from the platform?

**Data and compliance — easy to forget, expensive to get wrong**

- Source documents are corporate HR and training material. **Any restriction on
  sending customer documents to third-party APIs** (Google, ElevenLabs)?
- Do we need a DPA with those providers? Does one already exist?
- Data residency requirements? HeyHi is Singapore-based; Gemini and ElevenLabs
  process outside SG.
- Is kie.ai acceptable for PDF-review character images? It receives the project's
  presenter/background images; video no longer leaves the company network (H3 runs
  on the company GPU).
- Does generated video containing a likeness of a library character raise any
  rights question?

**Cost and quota**

- Who owns the API budget, and what is the monthly ceiling?
- Should the pipeline enforce a per-video or per-user spend cap in code?
- Today all users share one API key and one quota — do we need per-tenant limits?
  One heavy user can exhaust everyone's capacity.
- Who watches for a runaway loop burning credits?

**Secrets and access**

- How are secrets managed — Vault, AWS Secrets Manager, CI variables?
- Who gets production access, and how are keys rotated?

**Operations**

- What is the monitoring and alerting stack?
- Who is on call when a run fails overnight?
- What does the deploy process look like — and what would ours need to look like?

### 6.4 Answers to have ready

IT will very likely ask these. Have the answer before they do:

| Question                                   | Answer                                                                                                                                                                                                                                     |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| "Can it scale horizontally?"               | Not today — runs are local directories on one machine's disk. It needs shared storage (Stage 1) before a second instance is safe. There is also no file locking, so two workers on one run would race                                      |
| "Is it stateless?"                         | No. State is the filesystem                                                                                                                                                                                                                |
| "What happens if it crashes mid-run?"      | Nothing already paid for is lost. Pristine clips survive in `veo_raw/`, and `resume=true` regenerates only what is missing (open-questions #24). It is a manual re-POST today, not automatic — which is exactly the gap a job queue closes |
| "Can we containerise it?"                  | Yes — needs an ffmpeg-bearing base image and a mounted volume                                                                                                                                                                              |
| "What's the blast radius of a bad deploy?" | In-flight generations stop. Their **spend is not lost** — `resume=true` picks up from the artifacts on disk, provided the volume is persistent. Without a persistent volume the spend goes with the container                              |
| "How do we back it up?"                    | Nothing exists today. `_storage/` is gitignored                                                                                                                                                                                            |
| "Does it need to be highly available?"     | Product question — a generation service can usually tolerate short downtime, since jobs are already long                                                                                                                                   |

### 6.5 If anyone asks whether video generation can be made cheaper

Expect this from management rather than from IT, and it is worth having the
answer rather than improvising one. **There is a measured ~30% lever, and it is
deliberately not pulled.**

| Lever                                                                                                                                         | Saving                                                        | Status                                                                                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Composed reference plate instead of the 2×2 storyboard** — every scene becomes image-to-video on Veo 3.1 Lite, and the Fast tier disappears | **~30% of the video bill**, measured on three real blueprints | Held in reserve. [open-questions #27](open-questions.md#27-on-hold--would-a-single-composed-reference-image-replace-the-storyboard) |
| Diarization moves from Gemini to local Whisper                                                                                                | cents per run                                                 | Built and selectable, default unchanged. [#16](open-questions.md#16-fixed--whisperdiarizer-was-not-selectable)                      |
| B-Roll shorter than 4s is generated at Veo's floor and trimmed                                                                                | a few cents per B-Roll                                        | Unavoidable — Veo has no shorter step. [#9](open-questions.md#9-fixed--cost-reporting-was-video-only)                               |

Video dominates everything else by an order of magnitude, so the first row is the
only one that moves the number meaningfully.

**Why it is not already done, stated in one sentence:** the current output quality
is accepted, and swapping the reference format risks more static-looking shots —
so it is a costed proposal held ready rather than a change made speculatively.

**What to offer:** the de-risking experiment costs **$0.40** and an afternoon —
regenerate one scene from a composed plate and watch it beside the original.
`veo_raw/` means the current version is never at risk. If it holds up, the change
is contained to the director and L4.

The framing that lands: _we know where the 30% is, we know what it might cost in
quality, and we can find out for under a dollar._

### 6.6 The one recommendation to walk in with

Stage 1 is small, and framing it that way keeps the conversation productive:

> A persistent volume plus `ATLAS_STORAGE_ROOT` needs **zero code changes**. The
> real work is moving `/pipeline/scenes` to a background job. That single change
> unblocks deployment; everything else — database, object storage, auth — can
> follow once we know the scale.

---

## 7. Also unresolved

- **Retention.** Nothing expires. Decide before storage becomes the bill.
- **The libraries are global and mutable.** `character_library/` and
  `audio_library/` are shared by every run. Replacing a character PNG changes what
  all _future_ runs generate and silently invalidates nothing. With multiple
  users this needs either immutability or per-tenant libraries.
- **Cost attribution.** `cost_log.jsonl` is per-run and there is no user to
  attribute it to. Anyone with a `run_id` can spend against it via `force=true`.
- **Secrets.** All provider keys are process-level env vars, so every run shares
  one set of credentials and one quota. One user's activity can exhaust another's
  capacity.
- **`_storage/` is gitignored**, so nothing in it is version-controlled or
  backed up. See [examples/](examples/) for why the committed text artifacts live
  in `docs/` instead.
- **Language coverage is untested in production.** `config.language` is free text
  and the pipeline is built for multi-language output, but every run so far has
  been English. The untested link is L6: it depends on transcribing Veo's native
  audio, and nobody has checked how Veo speaks anything else. Relevant to
  deployment because a customer-facing launch in a second language would be
  discovering this in front of users. See
  [open-questions #18](open-questions.md#18-fixed--what-_norm-does-to-non-latin-scripts-and-what-it-actually-breaks).
