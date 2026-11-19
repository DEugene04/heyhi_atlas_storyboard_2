# Cost model

**Rates last checked: 2026-08-01.** Re-check before trusting any total; provider
pricing moves and nothing in the pipeline detects a stale rate.

These rates are transcribed into `PRICING` in [`app/costs.py`](app/costs.py),
which is the only place spend is priced. **Change the table there when you change
this file** — the code does not read this document.

Everything is recorded to `run_0X/cost_log.jsonl`, one JSON object per usage
event. `GET /pipeline/cost?run_id=...` returns the rollup.

---

## Layer 2 — Director

**Gemini 2.5 Flash** — $0.30 per 1M input tokens, $2.50 per 1M output tokens.

Billed per director call, and `direct()` retries internally, so a run costs
`attempts × tokens`. Every attempt is recorded separately with its `attempt`
number.

Token counts come from the SDK's `usage_metadata`. If the SDK stops reporting
it, **nothing is recorded rather than a zero** — an unknown call must not look
like a free one.

## Layer 4 — Images

**Gemini 2.5 Flash Image** — **$0.039 per output image** (AI Studio: $0.034,
worth validating).

Priced per image, not per token. Token counts *are* recorded when the SDK reports
them, but marked `priced: false` and excluded from the total.

**Why the earlier $0.003 figure was wrong.** It came from applying Layer 2's
*text* output rate to an image. Image output is billed at its own, much higher
rate — reported as **$30 per 1M output tokens**, and an image is ~1290 tokens:

```
1290 tokens × $30 / 1,000,000 = $0.0387  ≈  $0.039 per image
```

So per-image and per-token pricing agree exactly, once the image rate is used
instead of the text rate. That reconciles the two numbers and confirms $0.039.
$0.003 was an artefact of mixing up the two output rates, not a real price.

If you'd rather bill per token, add a separate image-output rate to `PRICING`
rather than reusing `gemini-2.5-flash`'s — they are different products.
**Verify $30/1M against the invoice** before relying on it.

Recorded per **successful** image. Failed attempts are noted in `attempts` but
not billed separately, on the assumption that an errored call produced no image.

## Layer 5 — Video

### Gemini / AI Studio

| Model | Rate |
|---|---|
| Veo 3.1 Fast | $0.10 per second |
| Veo 3.1 Lite | $0.05 per second |

Duration is **measured from `veo_raw/<scene>.mp4`**, not from what was requested
and not from the finished clip. That is what makes the 4/6/8s floor bill
correctly.

Measured on a real run (`run_652b50f7`):

| Scene | Type | Blueprint `length` | `veo_raw` mp4 | Final clip | Billed |
|---|---|---|---|---|---|
| `scene_opening_broll` | B-Roll | 1.0s | **4.000s** | 1.000s | 4s → $0.20 |
| `scene_payroll_contact` | video-with-audio | `null` | **8.000s** | 7.987s | 8s → $0.80 |
| `scene_gather_proof` | video-with-audio | `null` | **8.000s** | 7.987s | 8s → $0.40 |

The B-Roll asked for 1s, Veo generated 4s because that is its floor, and the clip
was then trimmed to 1s. Billing from the blueprint length would report $0.05 —
**a 4× undercount**. Billing from the finished clip would be worse still.

Note the raw files come back at exactly 4.000 / 8.000, so there is no fractional
drift to worry about, and the measured number matches whole-second billing.

### Company GPU (MiniMax H3)

**No per-call charge.** H3 runs on the company's own GPU server, so `scene_gen`
logs each clip's generated seconds as `provider="local-gpu"`, `priced=False`:
visible in the cost log, but not counted in the run total and not flagged as an
undercount. Set a rate in `PRICING["minimax-h3"]` if GPU time should be costed.

### kie.ai

**$0.005 per credit**, used only for PDF-review character images (Nano Banana).
The image client reports `credits_consumed` per call.

## Layer 6 — Audio

**Diarization: Gemini 2.5 Flash**, same token rates as Layer 2. One call per
speaking scene **on both audio paths** — Azure needs the timings to place audio,
ElevenLabs needs them only for subtitles. This is an easy one to forget.

### Azure TTS

**$0.000412 per second**, billed against the **synthesised** audio — measured
before silence trimming, because Azure charged for the padding we then discard.
One call per *phrase*, not per scene.

The measurement happens in place: the duration is read, then the file is trimmed
over itself. No second copy is kept.

### ElevenLabs STS

**$0.0033 per second of INPUT audio.** The converted output is not separately
charged.

Measured on the extracted Veo audio before conversion — the full ~8s clip,
including trailing silence the trim later discards. Captured before the temp file
is deleted.

## Layer 7 — Music

**Google Lyria 3 Clip** — $0.04 per call. Lyria emits a fixed ~30s clip
regardless of the requested duration, so per-call and per-clip are the same
thing. One per run.

## Free

No charge, listed so nobody wonders:

- **Layer 8 (render)** — local ffmpeg only.
- **`WhisperDiarizer`** — local `faster-whisper`, compute only. Not currently
  selectable; both providers hard-code `GeminiDiarizer`.
- **`GET /pipeline/audios?voice_source=elevenlabs`** — ElevenLabs' voice list is
  free, though rate-limited.

---

## Reading a cost log

```json
{"layer": "L5", "provider": "gemini", "model": "Veo 3.1 Fast",
 "unit": "second", "quantity": 8.0, "cost_usd": 0.8, "measured": true,
 "scene_id": "scene_basics_1", "attempts": 1}
```

- **`unit` + `quantity`** rather than a bare dollar figure, so per-token,
  per-second, per-image, per-call and per-credit billing coexist and the run can
  be re-priced later if a rate turns out wrong.
- **`measured: true`** means the quantity was read from the real artifact — the
  length of the mp4 on disk, the credits the provider actually deducted.
  `false` means it was inferred from what we asked for. Only measured numbers are
  worth reconciling against an invoice.
- **`unpriced` in the summary** lists usage recorded with no rate in the table. A
  non-empty list means `total_usd` is an **undercount, not a total**. Check it
  before quoting the number.

Adding a provider means one entry in `PRICING` and one `costs.record(...)` call
at the point of use. If the rate is missing the event is still recorded, with
`cost_usd: null` — a pricing gap never loses the evidence of something already
paid for.
