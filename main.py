"""
main.py

Endpoints
  GET  /health              liveness probe
  POST /pipeline/config     L1  create a run, persist config, return run_id
  POST /pipeline/upload     L1  attach a UTF-8 .txt source to a run
  POST /pipeline/direct     L2  run the director (Gemini) -> blueprint.json      [network+credits]
  GET  /pipeline/characters L3  list the character library
  GET  /pipeline/audios     L3  list the audio/voice library
  POST /pipeline/select     L3  persist selection + resolve blueprint placeholders
  POST /pipeline/assets     L4  generate backgrounds/storyboards                  [network+credits]
  POST /pipeline/scenes     L5+L6  generate Veo scenes + TTS interleave           [network+credits]
  POST /pipeline/music      L7  generate the background bed (Lyria)          [network+credits]
  POST /pipeline/render     L8  merge + title + transitions + subs + music   [local ffmpeg]

  GET  /pdf-review/         review-first PDF prototype; intentionally stops
                            at a locked Seedance preflight manifest

THE RUN IS THE STATE
There is no database. A run_id names a directory under STORAGE_ROOT, and every
endpoint's precondition is "does the file the previous stage should have written
exist". That is why each handler opens with a cluster of 409 checks — those are
the pipeline's ordering constraints, expressed as file existence:

    config.json                 /pipeline/config       required by direct
    source.txt                  /pipeline/upload       required by direct
    blueprint.json              /pipeline/direct       required by select, music
    blueprint.placeholder.json  /pipeline/select       pristine pre-patch snapshot
    selections.json             /pipeline/select       required by assets, scenes
    backgrounds/, storyboard/   /pipeline/assets       required by scenes
    veo_raw/<id>.mp4            /pipeline/scenes       pristine clips; enable tts_only
    scenes/<id>.mp4             /pipeline/scenes       voiced + trimmed; required by render
    tts/                        L6                     voice wavs + timings sidecars
    tts/<id>.done               L6                     written last; marks a scene
                                                       finished, so resume can skip it
    cost_log.jsonl              L2 onward              one line per billed call
    music_bg/track.mp3          /pipeline/music        optional; render mixes it if present
    merged*.mp4, subs.srt       /pipeline/render       per-pass intermediates
    final.mp4                   /pipeline/render       the deliverable

Consequences to keep in mind: unfinished runs simply sit on disk, and a stage that
spends credits is guarded by an explicit flag rather than by a timestamp
comparison — so you can never regenerate something billable by accident, but you
also never regenerate it automatically when an input changes.

RESUMING. Two different things get called "resume" and it is worth keeping them
apart. Between stages, resuming has always just meant POSTing the next endpoint.
Within a stage, /pipeline/assets and /pipeline/scenes now take `resume=true`,
which keeps every artifact already on disk and generates only what is missing —
the recovery path for a stage that died at scene 7 of 9 with seven paid clips in
veo_raw/. `force=true` is the opposite and still means regenerate everything;
passing both is a 422. Resume looks at file existence only, so it cannot tell that
a blueprint changed underneath it: edit a prompt and you want force.

Handlers stay thin on purpose. Each one validates preconditions, builds its
clients through a `_*_client()` seam (lazily imported, so the API boots with no
keys and tests can substitute fakes), calls one pipeline function, and maps that
layer's error type onto a status code. The convention throughout:
    409 wrong order / already exists      422 bad input or invalid blueprint
    502 upstream provider failed          503 not configured (missing key/library)
"""
from __future__ import annotations

import codecs
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from app.config import make_client
from app.clients.video_veo_client import VeoError, retry_backoff_from_env
from app.clients.tts_azure_client import TTSError
from app.clients.sts_elevenlabs_client import STSError, ElevenLabsSTSClient
from app.pipeline.external_tts import L6Error
from app.pipeline.audio_providers import DIARIZERS, PROVIDERS, resolve_voices
from app.pipeline.video_providers import VIDEO_PROVIDERS
from app.pipeline.scene_gen import SceneGenerator
from app.utils.ffmpeg_utils import FfmpegError
from app.utils.io_utils import atomic_write_text

from app.schemas import (
    AssetsIn, AssetsOut, ConfigIn, ConfigOut, DirectIn, DirectOut,
    ScenesIn, ScenesOut, SelectIn, SelectOut, UploadOut, MusicIn, MusicOut,
    RenderIn, RenderOut, VoiceEntry
)
from app.libraries import CharacterEntry, load_audio_index, load_character_index

from app.pipeline.patch_blueprint import patch_run, PatchError
from app.pdf_review import router as pdf_review_router

# storage root is env-overridable so tests/prod never share a tree
STORAGE_ROOT = Path(
    os.environ.get("ATLAS_STORAGE_ROOT", "_storage/atlas_storyboard")
).resolve()

# libraries are GLOBAL (not per-run); siblings of the run tree by default
CHAR_LIB_ROOT = Path(
    os.environ.get("ATLAS_CHAR_LIB", str(STORAGE_ROOT.parent / "character_library"))
).resolve()
AUDIO_LIB_ROOT = Path(
    os.environ.get("ATLAS_AUDIO_LIB", str(STORAGE_ROOT.parent / "audio_library"))
).resolve()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB hard cap (DoS guard)
CHUNK = 1 << 20                       # 1 MB streaming reads
_RUN_ID_RE = re.compile(r"^run_[a-z0-9]{8}$")

app = FastAPI(title="Atlas Pipeline - Layer 1", version="1.0")

# Review-only PDF workflow. Its final generation control is intentionally
# locked; the existing text-to-video endpoints remain unchanged.
app.include_router(pdf_review_router)

def _run_dir(run_id: str, must_exist: bool = True) -> Path:
    """Resolve a run_id to its directory. Shape check + containment check (never
    let a run_id escape STORAGE_ROOT) + existence, which every endpoint except
    /pipeline/config requires — that one is creating the run, so it passes
    must_exist=False."""
    if not _RUN_ID_RE.match(run_id):
        raise HTTPException(422, f"invalid run_id '{run_id}'")
    d = (STORAGE_ROOT / run_id).resolve()
    if d.parent != STORAGE_ROOT:
        raise HTTPException(400, "run_id path escapes storage root")
    if must_exist and not d.is_dir():
        raise HTTPException(404, "unknown run_id - POST /pipeline/config first")
    return d


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/pipeline/cost")
def run_cost(run_id: str) -> dict:
    """Everything spent on a run so far, broken down by layer and by model.

    Read-only, free, and safe to call at any point — cost_log.jsonl accumulates
    from L2 onward, so this works on a half-finished run.

    Check `unpriced` before trusting `total_usd`: a non-empty list means some
    usage was recorded with no rate in app/costs.PRICING, and the total is an
    undercount rather than a total.
    """
    from app.costs import summarize
    return {"run_id": run_id, **summarize(_run_dir(run_id))}

# ----------------------------------------------------------------------
# Layer 1 - Source + Config
# ----------------------------------------------------------------------
# Save the config from user's input
@app.post("/pipeline/config", response_model=ConfigOut)
def create_config(cfg: ConfigIn) -> ConfigOut:
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    d = _run_dir(run_id, must_exist=False)
    d.mkdir(parents=True, exist_ok=False)
    cfg_path = d / "config.json"
    atomic_write_text(cfg_path, cfg.model_dump_json(indent=2))
    return ConfigOut(run_id=run_id, config_path=str(cfg_path))

# Save the upload txt into local file
@app.post("/pipeline/upload", response_model=UploadOut)
async def upload_source(
    run_id: str = Form(...),
    file: UploadFile = File(...),
) -> UploadOut:
    d = _run_dir(run_id)
    if not (file.filename or "").lower().endswith(".txt"):
        raise HTTPException(415, "only .txt sources are accepted")

    dest = d / "source.txt"                       # fixed name; client name ignored
    dec = codecs.getincrementaldecoder("utf-8")()  # validate text while streaming
    total = 0
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(CHUNK):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"file exceeds {MAX_UPLOAD_BYTES} byte cap")
                try:
                    piece = dec.decode(chunk)      # UTF-8 gate (rejects binary)
                except UnicodeDecodeError:
                    raise HTTPException(415, "source is not valid UTF-8 text")
                if "\x00" in piece:
                    raise HTTPException(415, "source contains NUL bytes")
                out.write(chunk)
            try:
                dec.decode(b"", final=True)         # catch truncated multibyte tail
            except UnicodeDecodeError:
                raise HTTPException(415, "source truncated / invalid UTF-8")
        if total == 0:
            raise HTTPException(422, "empty source file")
        os.replace(tmp, dest)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    finally:
        await file.close()

    return UploadOut(run_id=run_id, source_path=str(dest), bytes=total)

# ----------------------------------------------------------------------
# Layer 2 — Director later
# ----------------------------------------------------------------------

def _director_llm(*, use_schema: bool = True):
    """Seam: build the networked Gemini director. Imported lazily so the API
    starts without the key; tests override this to avoid any live call.
    Keyword-only, so a stub can be `lambda **kw: fake` and ignore the flag."""
    from app.pipeline.director import GeminiDirector
    return GeminiDirector(make_client(), use_schema=use_schema)
 
 
@app.post("/pipeline/direct", response_model=DirectOut)
def run_director(body: DirectIn) -> DirectOut:
    d = _run_dir(body.run_id)
    
    # Ensures the config and source are ready
    cfg_path, src_path, bp_path = d / "config.json", d / "source.txt", d / "blueprint.json"
    if not cfg_path.is_file():
        raise HTTPException(409, "config.json missing - POST /pipeline/config first")
    if not src_path.is_file():
        raise HTTPException(409, "source.txt missing - POST /pipeline/upload first")
    existed = bp_path.is_file()
    if existed and not body.force:
        raise HTTPException(409, "blueprint.json exists - pass force=true to regenerate")
 
    # config.json (ConfigIn subset) maps 1:1 onto DirectorConfig
    from app.pipeline.director import DirectorConfig, direct
    config = DirectorConfig(**json.loads(cfg_path.read_text(encoding="utf-8"))) # Change the config.json into passable params
    source_text = src_path.read_text(encoding="utf-8") # Load the source text
 
    try:
        llm = _director_llm(use_schema=body.use_schema)
    except RuntimeError as e:                 # key missing / not configured
        raise HTTPException(503, f"director not configured: {e}")
 
    ok = False
    try:
        bp = direct(source_text, config, llm, max_retries=body.max_retries)
        ok = True
    except RuntimeError as e:                  # exhausted retries / LLM failure
        raise HTTPException(502, f"director failed: {e}")
    finally:
        # RECORDED IN `finally` ON PURPOSE. A director that exhausts its retries
        # still made (max_retries + 1) billed calls — the API answered every one
        # of them, we just rejected the content. The failure path is in fact the
        # EXPENSIVE one, so recording only on success made exactly the runs you
        # most want to account for invisible.
        # `attempt` numbers the calls; `director_outcome` says whether the run
        # they belong to ended up usable, so wasted spend can be totalled.
        from app.costs import record_tokens
        for i, usage in enumerate(getattr(llm, "usage_log", []) or []):
            record_tokens(d, layer="L2",
                          model=getattr(llm, "model", "gemini-2.5-flash"),
                          usage=usage, attempt=i + 1,
                          director_outcome="ok" if ok else "failed")
 
    # a fresh blueprint invalidates any prior placeholder snapshot + selection of
    # characters and audio. Both MUST go: the snapshot is what patch_run patches
    # from, and selections.json refers to placeholder ids the new blueprint may not
    # even contain. Leaving either behind would let /pipeline/select apply a stale
    # mapping to a new blueprint.
    if existed:
        for stale in ("blueprint.placeholder.json", "selections.json"):
            (d / stale).unlink(missing_ok=True)

    # Two post-processing steps between validation and disk, so what lands in
    # blueprint.json is NOT byte-identical to what Gemini returned:
    #   enforce_opening_length — forces the opening B-Roll to 4s on an 'opening'
    #     run so it can carry the title card (no-op on 'summary'). Has to happen
    #     before L5 or Veo renders the plate too short.
    #   derive_style_type — keyword-maps the free-text style into the StyleType the
    #     renderer's title treatment keys on. Stored once so nothing re-derives it.
    from app.pipeline.director import enforce_opening_length, derive_style_type
    bp_dict = enforce_opening_length(bp.model_dump(mode="json"), d)
    bp_dict["style_type"] = derive_style_type(bp_dict.get("style", ""))
    atomic_write_text(bp_path, json.dumps(bp_dict, indent=2, ensure_ascii=False))
    return DirectOut(
        run_id=body.run_id, blueprint_path=str(bp_path),
        character_count=bp_dict["character_count"], scenes=len(bp_dict["scenes"]),
        regenerated=existed,
    )
 

# ----------------------------------------------------------------------
# Layer 3 — libraries (read-only) + selection
# ----------------------------------------------------------------------
# Return all characters in the library --> Use to display list of characters in the front end
@app.get("/pipeline/characters", response_model=list[CharacterEntry])
def list_characters() -> list[CharacterEntry]:
    try:
        return load_character_index(CHAR_LIB_ROOT)
    except FileNotFoundError:
        raise HTTPException(503, "character library not initialized")

@app.get("/pipeline/audios", response_model=None)
def list_audios(voice_source: Literal["index", "elevenlabs"] = "elevenlabs"):
    # B1 gate: default reproduces today's index.json response byte-for-byte.
    if voice_source == "index":
        try:
            return load_audio_index(AUDIO_LIB_ROOT)
        except FileNotFoundError:
            raise HTTPException(503, "audio library not initialized")

    # elevenlabs: live v2/voices, fully paginated, via the existing STS client.
    try:
        voices = ElevenLabsSTSClient().list_voices()
    except STSError as e:
        raise HTTPException(502, f"ElevenLabs voice list failed: {e}")
    return [VoiceEntry(**v) for v in voices]

# Select character and corresponding audio
@app.post("/pipeline/select", response_model=SelectOut)
def select(sel: SelectIn) -> SelectOut:
    d = _run_dir(sel.run_id)
 
    bp_path = d / "blueprint.json"
    if not bp_path.is_file():
        raise HTTPException(409, "blueprint.json not found - run director first")
 
    try:
        char_ids = {e.char_id for e in load_character_index(CHAR_LIB_ROOT)}
    except FileNotFoundError:
        raise HTTPException(503, "character library not initialized")
    
    audio_ids: set[str] | None = None
    if sel.voice_source == "index":
        try:
            audio_ids = {e.audio_id for e in load_audio_index(AUDIO_LIB_ROOT)}
        except FileNotFoundError:
            raise HTTPException(503, "audio library not initialized")
 
    # placeholders the director actually emitted = union of scene.characters
    bp = json.loads(bp_path.read_text(encoding="utf-8"))
    expected: set[str] = set()
    for sc in bp.get("scenes", []):
        expected.update(sc.get("characters", []))
 
    problems: list[str] = []
    seen_ph: set[str] = set()
    char_to_audio: dict[str, str] = {}
    for s in sel.selections:
        if s.char_id not in char_ids:
            problems.append(f"char_id '{s.char_id}' not in character library")
        if audio_ids is not None and s.audio_id not in audio_ids:
            problems.append(f"audio_id '{s.audio_id}' not in audio library")
        if s.placeholder_id in seen_ph:
            problems.append(f"placeholder '{s.placeholder_id}' assigned more than once")
        seen_ph.add(s.placeholder_id)
        prev = char_to_audio.setdefault(s.char_id, s.audio_id)
        if prev != s.audio_id:
            problems.append(
                f"char_id '{s.char_id}' mapped to two audios "
                f"('{prev}','{s.audio_id}') - voice must be stable"
            )
 
    missing = expected - seen_ph
    extra = seen_ph - expected
    if missing:
        problems.append(f"selection missing blueprint placeholders: {sorted(missing)}")
    if extra:
        problems.append(f"selection has placeholders not in blueprint: {sorted(extra)}")
 
    if problems:
        raise HTTPException(422, {"errors": problems})
 
    payload = {"run_id": sel.run_id, "voice_source": sel.voice_source,
               "selections": [s.model_dump() for s in sel.selections]}
    out = d / "selections.json"
    atomic_write_text(out, json.dumps(payload, indent=2, ensure_ascii=False))
 
    # resolve placeholders -> library ids in blueprint.json (D2T6), in the same
    # call. Transactional: if resolution fails, roll back the selection so we
    # never leave a selection without a matching resolved blueprint.
    try:
        patch_run(d)
    except PatchError as e:
        out.unlink(missing_ok=True)
        raise HTTPException(422, {"errors": e.problems})
 
    return SelectOut(
        run_id=sel.run_id, selections_path=str(out),
        blueprint_path=str(d / "blueprint.json"), count=len(sel.selections),
    )

# ----------------------------------------------------------------------
# Layer 4 — asset generation (Gemini 2.5 Flash Image). NETWORK + CREDITS.
# ----------------------------------------------------------------------
def _image_client():
    """Seam: build the networked Gemini image client. Lazily imported so the
    API starts without a key; tests override this to avoid any live call."""
    from app.pipeline.image_gen import GeminiImageClient
    return GeminiImageClient(make_client())


def _check_force_resume(body) -> None:
    """force and resume are opposites — refuse to guess which one was meant.

    Shared by every billable stage so the message and the status code are the
    same wherever you hit it."""
    if body.force and body.resume:
        raise HTTPException(
            422, "force and resume are mutually exclusive - force regenerates "
                 "everything, resume keeps what exists and generates the rest")


@app.post("/pipeline/assets", response_model=AssetsOut)
def run_assets(body: AssetsIn) -> AssetsOut:
    d = _run_dir(body.run_id)
    _check_force_resume(body)
    if not (d / "selections.json").is_file():
        raise HTTPException(409, "run /pipeline/select first (resolved blueprint required)")

    bg_dir = d / "backgrounds"
    # resume is an explicit "I know assets exist, keep them", so it clears the
    # same guard force does — the difference is what happens next, not here.
    if bg_dir.is_dir() and any(bg_dir.glob("*.png")) and not (body.force or body.resume):
        raise HTTPException(409, "assets exist - pass force=true to regenerate, or resume=true to fill in what is missing")

    from app.pipeline.image_gen import AssetError, AssetGenerator, backoff_from_env
    try:
        client = _image_client()
    except RuntimeError as e:
        raise HTTPException(503, f"image generator not configured: {e}")

    gen = AssetGenerator(d, CHAR_LIB_ROOT, client,
                         retry_backoff_s=backoff_from_env(), resume=body.resume)
    try:
        res = gen.generate_all()
    except AssetError as e:
        raise HTTPException(502, f"asset generation failed: {e}")

    return AssetsOut(
        run_id=body.run_id, assets_dir=str(d),
        backgrounds=len(res["backgrounds"]),
        storyboard=len(res["storyboard"]),
        reused=len(gen.reused),
    )

# ----------------------------------------------------------------------
# Layer 5 & 6 — scene generation (Veo) + TTS interleave. NETWORK + CREDITS.
# ----------------------------------------------------------------------
def _scene_clients(tts_provider: str, video_provider: str = "gemini",
                   diarizer: str = "gemini"):
    """Seam: (veo, audio_provider). Lazily imported; tests override.
    One Gemini client serves the gemini Veo path and Gemini diarization; the
    whisper diarizer ignores it and runs locally."""
    gemini = make_client()
    dia = DIARIZERS[diarizer](gemini)
    return VIDEO_PROVIDERS[video_provider](gemini), PROVIDERS[tts_provider](gemini, diarizer=dia)


@app.post("/pipeline/scenes", response_model=ScenesOut)
def run_scenes(body: ScenesIn) -> ScenesOut:
    d = _run_dir(body.run_id)
    _check_force_resume(body)
    if not (d / "selections.json").is_file():
        raise HTTPException(409, "run /pipeline/select first (resolved blueprint required)")
    bg_dir = d / "backgrounds"
    if not (bg_dir.is_dir() and any(bg_dir.glob("*.png"))):
        raise HTTPException(409, "run /pipeline/assets first (backgrounds/storyboards required)")
    scenes_dir = d / "scenes"

    if not body.tts_only:
        if (scenes_dir.is_dir() and any(scenes_dir.glob("*.mp4"))
                and not (body.force or body.resume)):
            raise HTTPException(409, "scenes exist - pass resume=true to generate only what is missing, force=true to regenerate everything, or tts_only=true to re-render TTS")
    else:
        raw_dir = d / "veo_raw"
        if not (raw_dir.is_dir() and any(raw_dir.glob("*.mp4"))):
            raise HTTPException(409, "no raw Veo clips - run /pipeline/scenes first (tts_only needs veo_raw/)")

    bp = json.loads((d / "blueprint.json").read_text(encoding="utf-8"))

    sel = json.loads((d / "selections.json").read_text(encoding="utf-8"))
    voice_source = sel.get("voice_source", "index")
    # a raw ElevenLabs voice_id is meaningless to Azure — fail fast, don't
    # feed it in as a voice_name.
    if voice_source == "elevenlabs" and body.tts_provider == "azure":
        raise HTTPException(422, "voice_source='elevenlabs' requires tts_provider='elevenlabs'")

    try:
        veo, audio = _scene_clients(body.tts_provider, body.video_provider, body.diarizer)
    except KeyError:
        raise HTTPException(422, f"unknown provider (tts='{body.tts_provider}', video='{body.video_provider}', diarizer='{body.diarizer}')")
    except (RuntimeError,) as e:
        raise HTTPException(503, f"scene generation not configured: {e}")

    # resolve + pre-validate voices for every speaking scene (fail fast).
    entries = None
    if voice_source != "elevenlabs":
        try:
            entries = load_audio_index(AUDIO_LIB_ROOT)
        except FileNotFoundError:
            raise HTTPException(503, "audio library not initialized")
    voice_for, problems = resolve_voices(
        bp, voice_source, audio, entries, body.tts_provider)
    if problems:
        raise HTTPException(422, {"errors": problems})

    gen = SceneGenerator(d, veo, backoff_s=retry_backoff_from_env())
    try:
        if body.tts_only:
            paths = gen.rerender_tts(bp, voice_for=voice_for, audio=audio)
        else:
            paths = gen.run_all(bp, voice_for=voice_for, audio=audio, resume=body.resume)
    except (VeoError, L6Error, TTSError, STSError, FfmpegError) as e:
        raise HTTPException(502, f"scene generation failed: {e}")

    # Whole-run total, not just this endpoint's: cost_log.jsonl accumulates from
    # L2 onward, so a run that has already been directed and had assets generated
    # reports those here too. Previously this summed Veo lines only and called the
    # result a total.
    from app.costs import summarize
    return ScenesOut(run_id=body.run_id, scenes_dir=str(scenes_dir),
                     scenes=len(paths), reused=len(gen.reused),
                     total_cost_usd=round(summarize(d)["total_usd"], 4))

# ----------------------------------------------------------------------
# Layer 7 — music generation (Google Lyria). NETWORK + CREDITS.
# ----------------------------------------------------------------------
def _music_client():
    """Seam: build the networked Lyria client. Lazily imported so the API
    starts without a key; tests override this to avoid any live call."""
    from app.clients.music_lyria_client import GeminiLyriaClient
    return GeminiLyriaClient(make_client())
 
 
@app.post("/pipeline/music", response_model=MusicOut)
def run_music(body: MusicIn) -> MusicOut:
    d = _run_dir(body.run_id)
    _check_force_resume(body)

    bp_path = d / "blueprint.json"
    if not bp_path.is_file():
        raise HTTPException(409, "blueprint.json not found - run director first")
 
    bp = json.loads(bp_path.read_text(encoding="utf-8"))
    style = (bp.get("music_style") or "").strip()
    if not style:
        raise HTTPException(422, "blueprint.music_style is empty - nothing to generate")
 
    out_mp3 = d / "music_bg" / "track.mp3"
    existed = out_mp3.is_file()
    if existed and body.resume:
        # L7 is a single artifact, so "resume" can only mean "leave it alone".
        # It exists for symmetry: a caller replaying the whole pipeline with
        # resume=true should not have to special-case this one stage.
        return MusicOut(run_id=body.run_id, music_path=str(out_mp3),
                        music_style=style, regenerated=False, reused=True)
    if existed and not body.force:
        raise HTTPException(409, "track.mp3 exists - pass force=true to regenerate, or resume=true to keep it")
 
    from app.clients.music_lyria_client import MusicError
    from app.pipeline.music_gen import generate_run_music
 
    try:
        client = _music_client()
    except RuntimeError as e:                    # key missing / not configured
        raise HTTPException(503, f"music generator not configured: {e}")
 
    try:
        path = generate_run_music(
            d, client, duration_s=body.duration_s, instrumental=body.instrumental,
        )
    except MusicError as e:
        raise HTTPException(502, f"music generation failed: {e}")
 
    return MusicOut(
        run_id=body.run_id, music_path=str(path),
        music_style=style, regenerated=existed,
    )

# ----------------------------------------------------------------------
# Layer 8 — renderer (PARTIAL: D6T5 merge + D6T6 subtitles). LOCAL FFMPEG.
# ----------------------------------------------------------------------
@app.post("/pipeline/render", response_model=RenderOut)
def run_render(body: RenderIn) -> RenderOut:
    d = _run_dir(body.run_id)
 
    scenes_dir = d / "scenes"
    if not (scenes_dir.is_dir() and any(scenes_dir.glob("*.mp4"))):
        raise HTTPException(409, "no scenes - run /pipeline/scenes first")
 
    out_final = d / "final.mp4"
    if out_final.is_file() and not body.force:
        raise HTTPException(409, "final.mp4 exists - pass force=true to re-render")
 
    bp = json.loads((d / "blueprint.json").read_text(encoding="utf-8"))
 
    from app.pipeline.renderer import RenderError, render_merge_and_subs
    try:
        res = render_merge_and_subs(d, bp, subtitle_method=body.subtitle_method)
    except RenderError as e:
        raise HTTPException(502, f"render failed: {e}")

    return RenderOut(
        run_id=body.run_id, scenes=len(bp.get("scenes", [])),
        merged_path=res["merged"], srt_path=res["srt"],
        merged_subs_path=res["merged_subs"], final_path=res["final"],
        language=res["language"], music=res["music"],
        font_warning=res["font_warning"],
    )
