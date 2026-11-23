"""
patch_blueprint — resolving placeholder char_ids to library ids (L3).

This is the hinge between "the director decided a video needs two characters"
and "the user picked these two". It runs inside /pipeline/select, transactionally
— if it raises, the endpoint deletes selections.json and returns 422, so a run
can never hold a selection whose blueprint was not resolved.

The known gap (open-questions #1) is pinned here too: prompt PROSE is not
rewritten. That test documents current behaviour, not desired behaviour.
"""
from __future__ import annotations

import json

import pytest

from app.pipeline.patch_blueprint import PatchError, apply_selection, _check_resolved, patch_run


def test_selection_rewrites_characters_and_audios(bp_dict, selections):
    patched, problems = apply_selection(bp_dict, selections)
    assert problems == []

    ph_to_char = {s["placeholder_id"]: s["char_id"] for s in selections}
    ph_to_audio = {s["placeholder_id"]: s["audio_id"] for s in selections}

    for sc in patched["scenes"]:
        for c in sc["characters"]:
            assert c in ph_to_char.values(), "a placeholder survived in characters"
        for a in sc["audios"]:
            assert a["char_id"] in ph_to_char.values()
            assert a["audio_id"] in ph_to_audio.values()


def test_selection_rewrites_start_frame_char_refs_but_keeps_bg_refs(bp_dict, selections):
    """start_frame.reference mixes char_ids and bg_ids. The rewrite is
    `.get(r, r)`, so anything that is not a known placeholder passes through —
    which is how background ids survive without needing a second lookup."""
    bg_ids = {b["id"] for b in bp_dict["backgrounds"]}
    patched, _ = apply_selection(bp_dict, selections)
    resolved = {s["char_id"] for s in selections}

    for sf in patched["start_frame"]:
        for ref in sf["reference"]:
            assert ref in resolved or ref in bg_ids


def test_unselected_placeholder_is_reported(bp_dict, selections):
    for s in selections:
        s["placeholder_id"] = "char_someone_else"
    _, problems = apply_selection(bp_dict, selections)
    assert any("no selection for placeholder" in p for p in problems)


def test_patching_is_idempotent_from_the_pristine_snapshot(bp_dict, selections):
    """Patching twice from the SAME source gives the same result. This is why
    patch_run always reads blueprint.placeholder.json rather than the file it
    last wrote — re-selecting a character has to be safe."""
    once, _ = apply_selection(bp_dict, selections)
    twice, _ = apply_selection(bp_dict, selections)
    assert once == twice


def test_patching_an_already_patched_blueprint_loses_the_placeholders(bp_dict, selections):
    """The failure mode the snapshot prevents: the second pass finds library ids
    where it expects placeholders and reports every scene."""
    once, _ = apply_selection(bp_dict, selections)
    _, problems = apply_selection(once, selections)
    assert problems, "expected the second pass over resolved ids to complain"


# ---------------------------------------------------------------- invariants
def _resolved(bp_dict, selections):
    patched, problems = apply_selection(bp_dict, selections)
    assert problems == []
    return patched


def test_resolved_blueprint_passes_its_invariants(bp_dict, selections):
    assert _check_resolved(_resolved(bp_dict, selections)) == []


def test_character_without_audio_rejected(bp_dict, selections):
    patched = _resolved(bp_dict, selections)
    for sc in patched["scenes"]:
        if sc["characters"]:
            sc["audios"] = []
            break
    assert any("has no audio mapping" in p for p in _check_resolved(patched))


def test_orphan_audio_rejected(bp_dict, selections):
    patched = _resolved(bp_dict, selections)
    for sc in patched["scenes"]:
        if sc["audios"]:
            sc["audios"][0]["char_id"] = "char_not_here"
            break
    assert any("not in characters" in p for p in _check_resolved(patched))


def test_unstable_voice_rejected(bp_dict, selections):
    patched = _resolved(bp_dict, selections)
    seen = 0
    for sc in patched["scenes"]:
        if sc["audios"]:
            seen += 1
            if seen == 2:
                sc["audios"][0]["audio_id"] = "a_different_voice"
                break
    assert any("inconsistent audio" in p for p in _check_resolved(patched))


def test_collapsing_two_roles_onto_one_character_is_rejected(bp_dict, selections):
    """open-questions #6, pinned. Mapping two placeholders to the SAME library
    character drops the distinct count below character_count, and that is
    rejected. Arguably legitimate (one actor, two roles) but currently not
    allowed — a front-end offering the same character twice hits a 422 that
    looks like a bug.

    If that decision is ever revisited, this test should start failing.
    """
    patched = _resolved(bp_dict, selections)
    patched["character_count"] = len(
        {c for sc in patched["scenes"] for c in sc["characters"]}
    ) + 1
    assert any("character_count" in p for p in _check_resolved(patched))


# ---------------------------------------------------------------- known gap
def test_prompt_prose_is_not_rewritten(bp_dict, selections):
    """DOCUMENTS THE BUG (open-questions #1), does not assert desired behaviour.

    apply_selection rewrites ids in characters / audios / start_frame.reference
    but never in prompt text. When the director wrote a placeholder into prose,
    the raw token reaches Veo. Measured across all three committed examples,
    this happens in most speaking scenes of every run.

    Decision 2026-07-31: not fixing — Veo takes appearance from the attached
    reference image, not the prompt text. If that is ever fixed, this test
    should start failing.
    """
    placeholders = {c for sc in bp_dict["scenes"] for c in sc.get("characters", [])}
    patched, _ = apply_selection(bp_dict, selections)
    leaked = [sc["id"] for sc in patched["scenes"]
              if any(p in sc["prompt"] for p in placeholders)]
    assert leaked, "expected placeholder ids to survive in prompt prose"


# ---------------------------------------------------------------- patch_run (file I/O)
def test_patch_run_snapshots_then_resolves(tmp_path, bp_dict, selections):
    run = tmp_path / "run_deadbeef"
    run.mkdir()
    (run / "blueprint.json").write_text(json.dumps(bp_dict), encoding="utf-8")
    (run / "selections.json").write_text(
        json.dumps({"selections": selections}), encoding="utf-8")

    patched = patch_run(run)

    snapshot = json.loads((run / "blueprint.placeholder.json").read_text())
    assert snapshot == bp_dict, "the snapshot must be the pristine director output"

    on_disk = json.loads((run / "blueprint.json").read_text())
    assert on_disk == patched
    assert on_disk != snapshot


def test_patch_run_can_be_rerun_with_a_different_selection(tmp_path, bp_dict, selections):
    """Re-selecting must work, because the snapshot — not the patched file — is
    always the input."""
    run = tmp_path / "run_deadbeef"
    run.mkdir()
    (run / "blueprint.json").write_text(json.dumps(bp_dict), encoding="utf-8")
    sel_path = run / "selections.json"
    sel_path.write_text(json.dumps({"selections": selections}), encoding="utf-8")
    patch_run(run)

    other = [dict(s, char_id="char_female_casual_realism_1") for s in selections]
    sel_path.write_text(json.dumps({"selections": other}), encoding="utf-8")
    again = patch_run(run)

    chars = {c for sc in again["scenes"] for c in sc["characters"]}
    assert chars == {"char_female_casual_realism_1"}


def test_patch_run_without_selections_raises(tmp_path, bp_dict):
    run = tmp_path / "run_deadbeef"
    run.mkdir()
    (run / "blueprint.json").write_text(json.dumps(bp_dict), encoding="utf-8")
    with pytest.raises(PatchError):
        patch_run(run)
