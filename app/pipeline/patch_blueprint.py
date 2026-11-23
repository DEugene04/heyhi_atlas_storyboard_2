"""
app/pipeline/patch_blueprint.py — Layer 3 blueprint patcher (D2T6).

Replaces the director's placeholder character IDs with the user's selected
library characters and voices.

Updates:
- scene.characters
- scene.audios[].char_id / audio_id
- start_frame[].reference

The original blueprint is preserved as `blueprint.placeholder.json` so the
patch can be safely reapplied after changing selections.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from app.pipeline.director import VideoBlueprint
from app.utils.io_utils import atomic_write_json


class PatchError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def apply_selection(blueprint: dict, selections: list[dict]) -> tuple[dict, list[str]]:
    """Replace placeholder IDs with the selected character and audio IDs.

    Returns:
        (patched_blueprint, problems)
    """
    ph_to_char = {s["placeholder_id"]: s["char_id"] for s in selections}
    ph_to_audio = {s["placeholder_id"]: s["audio_id"] for s in selections}
    problems: list[str] = []
    bp = deepcopy(blueprint)

    for sc in bp.get("scenes", []):
        sid = sc.get("id", "?")
        new_chars: list[str] = []
        for ph in sc.get("characters", []):
            if ph not in ph_to_char:
                problems.append(f"scene '{sid}': no selection for placeholder '{ph}'")
                continue
            new_chars.append(ph_to_char[ph])
        sc["characters"] = new_chars

        new_audios: list[dict] = []
        for am in sc.get("audios", []):
            ph = am.get("char_id")
            if ph not in ph_to_char:
                problems.append(f"scene '{sid}': audios entry for unknown placeholder '{ph}'")
                continue
            new_audios.append({"char_id": ph_to_char[ph], "audio_id": ph_to_audio[ph]})
        sc["audios"] = new_audios

    # Replace character placeholders while leaving background IDs unchanged.
    for sf in bp.get("start_frame", []):
        sf["reference"] = [ph_to_char.get(r, r) for r in sf.get("reference", [])]

    return bp, problems


def _check_resolved(bp: dict) -> list[str]:
    """Validate the patched blueprint's character and audio mappings."""
    problems: list[str] = []
    all_chars: set[str] = set()
    char_audio: dict[str, set[str]] = {}

    for sc in bp.get("scenes", []):
        sid = sc.get("id", "?")
        chars = sc.get("characters", [])
        all_chars.update(chars)
        aud_chars = {a["char_id"] for a in sc.get("audios", [])}
        for c in chars:
            if c not in aud_chars:
                problems.append(f"scene '{sid}': char '{c}' has no audio mapping")
        for a in sc.get("audios", []):
            if a["char_id"] not in chars:
                problems.append(f"scene '{sid}': audio for '{a['char_id']}' not in characters")
            char_audio.setdefault(a["char_id"], set()).add(a["audio_id"])

    for c, aset in char_audio.items():
        if len(aset) > 1:
            problems.append(f"char '{c}' has inconsistent audio {sorted(aset)}")

    cc = bp.get("character_count")
    if cc is not None and len(all_chars) != cc:
        problems.append(
            f"character_count {cc} != {len(all_chars)} distinct resolved chars "
            f"{sorted(all_chars)} — selection collapses/expands distinct roles"
        )
    return problems


def patch_run(run_dir: str | Path) -> dict:
    """Patch a run's blueprint using selections.json.

    Loads the original placeholder blueprint, applies the user's selections,
    validates the result, and atomically overwrites blueprint.json.

    Raises PatchError if patching or validation fails.
    """
    run_dir = Path(run_dir)
    sel_path = run_dir / "selections.json"
    bp_path = run_dir / "blueprint.json"
    pristine = run_dir / "blueprint.placeholder.json"

    if not sel_path.is_file():
        raise PatchError(["selections.json not found — run /pipeline/select first"])

    # Save the original blueprint once and always patch from it.
    if not pristine.is_file():
        if not bp_path.is_file():
            raise PatchError(["blueprint.json not found"])
        pristine.write_text(bp_path.read_text(encoding="utf-8"), encoding="utf-8")

    blueprint = json.loads(pristine.read_text(encoding="utf-8"))
    selections = json.loads(sel_path.read_text(encoding="utf-8"))["selections"]

    patched, problems = apply_selection(blueprint, selections)
    if problems:
        raise PatchError(problems)

    try:
        VideoBlueprint.model_validate(patched)          # structural v3
    except Exception as e:
        raise PatchError([f"patched blueprint fails v3 schema: {e}"])

    problems = _check_resolved(patched)                  # patch invariants
    if problems:
        raise PatchError(problems)

    atomic_write_json(bp_path, patched)      # resolved blueprint, never half-written
    return patched


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Resolve blueprint placeholders from selections.json")
    ap.add_argument("--run", type=Path, required=True, help="run directory")
    a = ap.parse_args(argv)
    try:
        bp = patch_run(a.run)
    except PatchError as e:
        print("PATCH FAILED:\n  - " + "\n  - ".join(e.problems))
        return 1
    chars = sorted({c for sc in bp["scenes"] for c in sc.get("characters", [])})
    print(f"patched {a.run/'blueprint.json'} — resolved chars: {chars}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())