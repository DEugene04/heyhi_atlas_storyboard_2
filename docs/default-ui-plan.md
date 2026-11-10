# Plan: front-end for the default (`/pipeline/*`) APIs + mode picker

## 1. What exists today

| Piece | State |
| --- | --- |
| `/pipeline/*` (10 endpoints + `/pipeline/cost`) | Working, JSON only, **no UI**. "The run is the state": a `run_id` is a directory, each stage's precondition is a file on disk. |
| `/pdf-review/*` | Working UI (`web/pdf_review/`, vanilla JS, no build) served by `app/pdf_review.py` via `GET /`, `GET /ui/{file}`, `GET /files/{project}/{path}`. |
| `GET /` | **Does not exist.** Nothing chooses between the two apps. |

## 2. Backend gaps the UI cannot work around

The default API is write/trigger-only. A UI needs to *see* results, so these must be added first (all small, read-only, free):

| # | Gap | Why the UI needs it | Addition |
| --- | --- | --- | --- |
| G1 | No way to view artifacts | Show backgrounds, storyboards, scene clips, `final.mp4`, character thumbnails, voice samples | `GET /pipeline/files/{run_id}/{path}` (containment-checked like `_run_dir`, reuse the `_safe_child` idea from pdf_review) and `GET /pipeline/library/characters/{char_id}/image` (+ audio sample for `voice_source=index`) |
| G2 | No way to read the blueprint | Step 3 needs the placeholder ids; review step shows scenes/script | `GET /pipeline/blueprint?run_id=` (returns `blueprint.json`, and `blueprint.placeholder.json` for placeholders after select) |
| G3 | No way to know where a run is | Resume after refresh, enable/disable steps, decide force vs resume | `GET /pipeline/status?run_id=` derived **only from file existence** (same rule as the 409 checks): `{config, source, blueprint, selected, backgrounds:n, storyboards:n, veo_raw:n, scenes:n, tts_done:n, scenes_total, music, final}`. No DB, no new state. |
| G4 | No run list | "Resume a previous run" | `GET /pipeline/runs` listing run dirs with title/stage/mtime |
| G5 | `/pipeline/scenes` is one blocking call of many minutes | Progress bar, page refresh | **Keep it synchronous** (matches pdf_review and the "thin handlers" rule). Progress comes from polling G3 (`veo_raw/`, `tts/*.done` counts) while the POST is open. Handlers are plain `def`, so they run in the threadpool and status polls are served concurrently. |

Decision to confirm (see §6): no job queue in v1. Consequence: if the tab is closed mid-run the server finishes anyway, and G3 shows the progress, but the UI cannot tell "still running" from "died". Acceptable for a prototype; revisit if needed.

## 3. Architecture

- **No change to `web/pdf_review/` or `app/pdf_review.py`.** It works end to end; keep it untouched (the working tree already has uncommitted edits there).
- New `web/landing/index.html`: two cards, **Default** → `/studio/`, **Presentation** → `/pdf-review/`. Served at `GET /`.
- New `web/studio/{index.html,app.js,styles.css}`, vanilla JS, no build step, same conventions as pdf_review (`api()` helper, busy overlay, notice bar, step rail). Reuse its CSS variables so both apps look related.
- New `app/studio_ui.py`: an `APIRouter(prefix="/studio")` serving the three static files (allow-list of filenames, like `review_ui_asset`). Mounted with one `include_router` line in `main.py`. The G1–G4 endpoints live in `main.py` next to the others, under `/pipeline/`.
- Fetch base is same-origin, so **no CORS** needed.
- All state in the browser is only `run_id` (+ `localStorage` for "last run"). The server is the source of truth, so refresh = `GET /pipeline/status` and jump to the right step.

## 4. UI flow (one step per endpoint group)

| Step | Screen | Calls | Rules the UI must enforce (from the code) |
| --- | --- | --- | --- |
| 0 | Run picker: new / resume | `GET /pipeline/runs` | |
| 1 | **Config + source** | `POST /config`, `POST /upload` | `blueprint_type` opening/summary; `depth` standard/deep dive; `aspect_ratio` fixed 16:9 (don't offer 9:16); `style` is free text; **`.txt` only, UTF-8, ≤50 MB**. Config creates the run, so upload is disabled until `run_id` exists. |
| 2 | **Director** | `POST /direct`, `GET /blueprint` | Paid. 409 if blueprint exists, so show "Regenerate" (`force=true`) behind a confirm: it **deletes `selections.json` and the placeholder snapshot**. Show scene list/title/character_count/music_style read-only. 502 → show retries message. |
| 3 | **Characters + voices** | `GET /characters`, `GET /audios`, `POST /select` | One row per placeholder id from the blueprint (count = `character_count`). Pick a library character and a voice per row. Block duplicates client-side: a placeholder is assigned once, and a `char_id` can map to only one audio. `voice_source` (`elevenlabs` default / `index`) is chosen **once here** and must stay consistent with `tts_provider` in step 5 (elevenlabs voices need `tts_provider=elevenlabs`, else 422). Render 422 `{"errors":[...]}` as a list. |
| 4 | **Assets** | `POST /assets` | Paid. States: none → generate; some exist → **Resume** (default) / **Regenerate** (force, confirm). `force`+`resume` never sent together. Gallery of backgrounds + storyboards via G1. |
| 5 | **Scenes + TTS** | `POST /scenes`, poll `/status` | Paid and slowest. Options: `tts_provider`, `video_provider` (hide `kieai`, testing-only), `diarizer`. Actions: Generate / Resume / Regenerate / **Re-run TTS only** (needs `veo_raw/`). Per-scene progress list from status, clip preview per finished scene. |
| 6 | **Music** (optional) | `POST /music` | Paid. `instrumental`, resume/force. Audio player on `music_bg/track.mp3`. Skippable: render mixes it only if present. |
| 7 | **Render** | `POST /render` | Local ffmpeg, free. `subtitle_method` selector (default `boxed`, see §6). 409 if `final.mp4` exists, so "Re-render" sends `force=true`. Show `font_warning`. Final `<video>` + download. |
| side | **Cost panel** (always visible) | `GET /pipeline/cost` | Refresh after every paid step. If `unpriced` is non-empty, show "undercount" warning instead of the total. |

Cross-cutting: every paid button states what it will spend and is never auto-triggered; status codes map to messages exactly as the docstring convention says (409 order / exists, 422 input, 502 upstream, 503 not configured).

## 5. Step-by-step development (each step ends in a gate; do not start the next until it passes)

**Setup**
- S0. Create branch `feature/default-ui` off the current branch. Leave the existing uncommitted `pdf_review` / minimax edits out of our commits. *Gate: `git status` shows only our files.*
- S0b. Baseline: run the existing test suite with the conda interpreter (see `atlas-python-env` memory). *Gate: green before any change.*

**Backend (free, test-first, with fakes: no credits spent)**
- B1. `GET /pipeline/status` + tests built on a temp `ATLAS_STORAGE_ROOT` with hand-made files for each stage. *Gate: one test per stage boundary, plus unknown run_id → 404, bad id → 422.*
- B2. `GET /pipeline/blueprint`. *Gate: 409 before direct; placeholder vs resolved after select.*
- B3. `GET /pipeline/files/{run_id}/{path}` + library image/sample routes. *Gate: `../` traversal, absolute paths, symlinks and other runs' dirs all rejected; correct media types for png/mp4/mp3.*
- B4. `GET /pipeline/runs`. *Gate: ignores non-`run_xxxxxxxx` dirs, sorted by mtime.*
- B5. `GET /` landing route + `studio_ui` router. *Gate: both `/pdf-review/` and `/studio/` still serve; unknown UI asset → 404.*
- B6. Full suite green. Commit.

**Front-end (manual check in a browser at each step against a real or example run; use `docs/examples/run_*` for read-only steps)**
- F1. Landing page + mode picker. *Gate: both links work; pdf_review unchanged.*
- F2. Studio shell: step rail, `api()` / busy / notice helpers, run picker, cost panel, status-driven step enabling. *Gate: refresh on an existing run lands on the right step.*
- F3. Step 1 config + upload. *Gate: creates a run; non-.txt and empty file show server errors.*
- F4. Step 2 director (**first paid call**, run once on a tiny source from `bench/sources`). *Gate: blueprint renders; second click shows the 409 → force confirm path.*
- F5. Step 3 characters + voices. Can be built and tested on an existing run's blueprint with no spend. *Gate: duplicate assignments blocked client-side; a forced 422 renders readably; selection survives refresh.*
- F6. Step 4 assets (paid; use `resume` on a run that already has assets for the no-spend test). *Gate: gallery from G1; resume path leaves existing PNGs.*
- F7. Step 5 scenes + progress polling. *Gate: tested first with `tts_only` on a run that already has `veo_raw/` (cheap), then one real short run. Polling stops on completion/error; refresh mid-run still shows progress.*
- F8. Step 6 music and Step 7 render + final player. Render is free, so test on an existing run. *Gate: `final.mp4` plays, re-render uses force, font warning shown.*
- F9. Polish and failure pass: every 409/422/502/503 path once, mobile width sanity, disable double-submit on paid buttons.

**Finish**
- E1. One end-to-end run through the new UI on a minimal source, compare cost panel with `/pipeline/cost`.
- E2. Update `docs/setup-and-run.md` and the endpoint list in `main.py`'s docstring (add the new GET routes and `/studio/`).

## 6. Decisions I need from you

1. **Long jobs:** synchronous POST + status polling (recommended, v1), or a background job runner now?
2. **Landing at `/`:** OK to add a root route, or should the picker live inside one of the existing apps?
3. **Voices:** default to ElevenLabs (the API default) and hide Azure/`index` in v1, or expose the `voice_source` toggle?
4. **Subtitles:** your parked item (ASS vs boxed default). The UI will default to `boxed` and expose the selector until you decide.
5. **Style:** the API takes free-text `style`, while CLAUDE.md talks about 6 fixed styles. Free text input with suggestion chips, or a fixed dropdown?
