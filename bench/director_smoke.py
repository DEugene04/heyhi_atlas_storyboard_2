"""
director_smoke.py — ONE live call to the v2.1 director, so you can eyeball
whether Gemini 2.5 Flash produces blueprints matching the new schema
(style + characters + backgrounds + scenes + start_frame).

Use to run the director layer only seperately.

Use case (check blueprint)

How to run
- python -m bench.director_smoke bench/sources/hr_policies.txt temp_storage/run001
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import make_client
from app.pipeline.director import DirectorConfig, GeminiDirector, direct, validate_blueprint


SAMPLE_DOC = (
    "Teach childrens ways to greet others politely and in a friendly manner. Make it in the form dialogues between 2 different character so that children's get to visualize it more easily"
    "Use a an animal cartoon style for the characters"
)

CONFIG = DirectorConfig(
    language = "English",
    audience = "New employees",
    depth = "standard",
    objective = "Teach new employees regarding the HR policies in this company",   # user-provided description of the video's goal
    style = (
        "Realism style video"
    ),
    aspect_ratio = "16:9"
)


def _summary(bp) -> str:
    rows = [
        f"style: {bp.style[:80]}{'...' if len(bp.style) > 80 else ''}",
        f"backgrounds: {[b.id for b in bp.backgrounds]}",
        f"start_frame: {[sf.id for sf in bp.start_frame]}",
        f"scenes     : {len(bp.scenes)}",
    ]
    for s in bp.scenes:
        length = f"{s.length}s" if s.length is not None else "tts"
        text_preview = (s.text[:40] + "...") if len(s.text) > 40 else s.text
        rows.append(
            f"  {s.id}: type={s.type:16s} ref={s.reference_from:22s} "
            f"len={length:5s} text={text_preview!r}"
        )
    return "\n".join(rows)


def main() -> int:
    argv = sys.argv[1:]
    source = Path(argv[0]).read_text() if argv else SAMPLE_DOC
    save_to = Path(argv[1]) if len(argv) > 1 else None

    print("Calling director (gemini-2.5-flash, one call)...\n")
    llm = GeminiDirector(make_client())              # env-only credential edge
    bp = direct(source, CONFIG, llm, max_retries=2)

    payload = bp.model_dump(mode="json")
    print("=== BLUEPRINT (JSON) ===")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    print("\n=== SUMMARY ===")
    print(_summary(bp))

    problems = validate_blueprint(bp, CONFIG)
    print("\n=== SEMANTIC VALIDATION ===")
    print("clean" if not problems else "PROBLEMS:\n  - " + "\n  - ".join(problems))

    if save_to:
        save_to.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"\nSaved blueprint JSON -> {save_to}")

    print(
        "\nEyeball check: opening and closing B-Roll present? each video-with-audio <= 20 words? "
        "every scene's reference_from resolves? every start_frame reference id exists?"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())