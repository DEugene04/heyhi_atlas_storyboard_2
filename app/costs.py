"""
app/costs.py — central pricing and cost logging.

Every provider records spend here. Older versions only tracked Veo video costs,
so images, TTS, STS, diarization, director retries, and music were invisible.

Providers bill in different units (tokens, seconds, images, calls, credits),
so each record stores the measured quantity and unit, with cost derived from the
pricing table:

    {"layer": "L5", "provider": "gemini", "model": "Veo 3.1 Fast",
     "unit": "second", "quantity": 8.0,
     "cost_usd": 0.8, "measured": true}

`measured` marks quantities taken from the real artifact or provider (mp4
duration, deducted credits, etc.) rather than inferred from the request.

Pricing is data, not logic: update PRICING (from cost.md), not the code.

Costs are stored as append-only JSONL so each event survives crashes without
requiring read-modify-write.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

COST_LOG = "cost_log.jsonl"

# Rates last checked 2026-08-01 (cost.md).
# Each entry maps billable units to (unit_name, usd_per_unit).
PRICING: dict[str, dict[str, tuple[str, float]]] = {
    # --- L2 director (gpt-4o) + L6 diarization (gemini): token-billed -------------------------
    "gemini-2.5-flash": {
        "token_in":  ("token_in",  0.30 / 1_000_000),
        "token_out": ("token_out", 2.50 / 1_000_000),
    },
    "gpt-4o": {
        "token_in":  ("token_in",  2.50 / 1_000_000),
        "token_out": ("token_out", 10.00 / 1_000_000),
    },
    # --- L4 images ---------------------------------------------------------
    # gpt-image-2 is token-billed with three rates: text prompt in, reference
    # images in, image out. Standard tier, from the OpenAI pricing page.
    "gpt-image-2": {
        "token_in":       ("token_in",       5.00 / 1_000_000),
        "image_token_in": ("image_token_in", 8.00 / 1_000_000),
        "token_out":      ("token_out",     30.00 / 1_000_000),
    },
    # RETIRED as the L4 model; kept so runs already on disk still price.
    "gemini-2.5-flash-image": {
        "image": ("image", 0.039),
    },
    # --- L5 video (Gemini/AI Studio) ---------------------------------------
    "Veo 3.1 Fast": {"second": ("second", 0.10)},
    "Veo 3.1 Lite": {"second": ("second", 0.05)},
    # --- L5 video (kie.ai) -------------------------------------------------
    # kie bills in credits. Scene generation measures credits consumed directly.
    "kie.ai": {"credit": ("credit", 0.005)},
    # --- L6 audio ----------------------------------------------------------
    # Azure: per second of SYNTHESISED audio — measured before silence trimming,
    # because Azure billed for what it produced, not for what we kept.
    "azure-tts": {"second": ("second", 0.000412)},
    # ElevenLabs STS: per second of INPUT audio. The converted output is not
    # separately charged.
    "eleven_multilingual_sts_v2": {"second": ("second", 0.0033)},
    # --- L7 music ----------------------------------------------------------
    # Lyria emits a fixed ~30s clip, so per-call and per-clip are the same thing.
    "lyria-3-clip-preview": {"call": ("call", 0.04)},
}

PRICING_LAST_CHECKED = "2026-08-01"


class UnknownRate(KeyError):
    """No rate for (model, unit).

    Raised only by price(). Recorders catch it and still log the usage with
    cost_usd=None so the run can be repriced later.
    """


def price(model: str, unit: str, quantity: float) -> float:
    """Return the USD cost for a quantity of a model/unit pair."""    
    table = PRICING.get(model)
    if table is None or unit not in table:
        raise UnknownRate(f"no rate for model={model!r} unit={unit!r}")
    _, per_unit = table[unit]
    return round(per_unit * max(float(quantity), 0.0), 6)


def record(run_dir: str | Path, *, layer: str, provider: str, model: str,
           unit: str, quantity: float, measured: bool = False,
           **extra) -> dict:
    """Append one priced usage event to cost_log.jsonl.

    Returns the written record. Unknown pricing never aborts generation—the event is
    still recorded with cost_usd=None for later repricing.

    `extra` is written verbatim (scene_id, attempt, phrase index, etc.).
    """
    try:
        usd = price(model, unit, quantity)
    except UnknownRate:
        usd = None
    rec = {"layer": layer, "provider": provider, "model": model,
           "unit": unit, "quantity": round(float(quantity), 6),
           "cost_usd": usd, "measured": bool(measured), **extra}
    p = Path(run_dir) / COST_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def record_tokens(run_dir: str | Path, *, layer: str, model: str,
                  usage: dict | None, provider: str = "gemini", **extra) -> None:
    """Record input and output token usage for one LLM call.

    Missing token counts are ignored rather than recorded as zero.
    """
    if not usage:
        return
    for unit, key in (("token_in", "input_tokens"), ("token_out", "output_tokens")):
        n = usage.get(key)
        if n:
            record(run_dir, layer=layer, provider=provider, model=model,
                   unit=unit, quantity=n, measured=True, **extra)


def record_image_usage(run_dir: str | Path, *, layer: str, provider: str,
                       model: str, usage: dict, **extra) -> None:
    """Record one generated image as its three token-billed parts."""
    for unit, key in (("token_in", "input_text_tokens"),
                      ("image_token_in", "input_image_tokens"),
                      ("token_out", "output_tokens")):
        n = usage.get(key)
        if n:
            record(run_dir, layer=layer, provider=provider, model=model,
                   unit=unit, quantity=n, measured=True, **extra)


def usage_from_response(resp) -> dict:
    """Extract token counts from a google-genai or OpenAI response.

    SDK response shapes vary across versions. Unknown layouts return {}, allowing
    generation to continue without token accounting.
    """
    meta = getattr(resp, "usage_metadata", None)      # google-genai
    if meta is None:
        meta = getattr(resp, "usage", None)           # openai
    if meta is None:
        return {}
    out = {}
    for key, attrs in (
        ("input_tokens",  ("prompt_token_count", "input_token_count", "prompt_tokens")),
        ("output_tokens", ("candidates_token_count", "output_token_count", "completion_tokens")),
    ):
        for a in attrs:
            v = getattr(meta, a, None)
            if v:
                out[key] = int(v)
                break
    return out


# ---------------------------------------------------------------- reporting
def load(run_dir: str | Path) -> list[dict]:
    """Load all cost records for a run.

    Returns [] if the log is missing. Invalid final lines are ignored.
    """    
    p = Path(run_dir) / COST_LOG
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # a torn final line must not lose the rest
    return out


def summarize(run_dir: str | Path) -> dict:
    """"Summarize spend by total, layer, and model.

    `unpriced` lists usage that could not be costed. If non-empty, `total_usd` is an
    undercount rather than the true total.
    """
    records = load(run_dir)
    total = 0.0
    by_layer: dict[str, float] = defaultdict(float)
    by_model: dict[str, float] = defaultdict(float)
    unpriced: list[dict] = []

    for r in records:
        usd = r.get("cost_usd")
        if usd is None:
            # priced=False marks a record kept for INFORMATION only — image
            # token counts, which are recorded but deliberately not billed. Those
            # are not evidence of an undercount, so they stay out of `unpriced`.
            if r.get("priced") is not False:
                unpriced.append({"model": r.get("model"), "unit": r.get("unit"),
                                 "quantity": r.get("quantity")})
            continue
        total += usd
        by_layer[r.get("layer", "?")] += usd
        by_model[r.get("model", "?")] += usd

    return {
        "total_usd": round(total, 6),
        "by_layer": {k: round(v, 6) for k, v in sorted(by_layer.items())},
        "by_model": {k: round(v, 6) for k, v in sorted(by_model.items())},
        "events": len(records),
        "unpriced": unpriced,
        "pricing_last_checked": PRICING_LAST_CHECKED,
    }
