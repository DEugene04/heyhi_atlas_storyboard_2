"""
L4 image client (OpenAI gpt-image-2) and its cost recording. No network: a fake
OpenAI client captures which endpoint and arguments the client used.
"""
from __future__ import annotations

import base64

import pytest

from app.costs import load, price, summarize
from app.pipeline.image_gen import AssetGenerator, OpenAIImageClient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _Resp:
    def __init__(self):
        class D:
            b64_json = base64.b64encode(PNG).decode()
        self.data = [D()]

        class Det:
            text_tokens, image_tokens = 40, 2000

        class U:
            input_tokens_details, output_tokens = Det(), 1000
        self.usage = U()


class FakeOpenAI:
    def __init__(self):
        self.calls = []
        outer = self

        class Images:
            def generate(self, **kw):
                outer.calls.append(("generate", kw)); return _Resp()

            def edit(self, **kw):
                outer.calls.append(("edit", kw)); return _Resp()
        self.images = Images()


def test_no_references_uses_generate_with_exact_16x9_size():
    fake = FakeOpenAI()
    out = OpenAIImageClient(fake, quality="low").generate("a plate", [])
    assert out == PNG
    kind, kw = fake.calls[0]
    assert kind == "generate" and kw["model"] == "gpt-image-2"
    assert kw["size"] == "1536x864" and kw["quality"] == "low"


def test_references_use_edit_and_are_all_attached():
    fake = FakeOpenAI()
    OpenAIImageClient(fake).generate("a grid", [PNG, PNG, PNG])
    kind, kw = fake.calls[0]
    assert kind == "edit" and len(kw["image"]) == 3


def test_usage_is_recorded_per_image():
    fake = FakeOpenAI(); c = OpenAIImageClient(fake)
    c.generate("x", [PNG]); c.generate("y", [])
    assert c.usage_log == [{"input_text_tokens": 40, "input_image_tokens": 2000,
                            "output_tokens": 1000}] * 2


def test_unsupported_aspect_ratio_is_refused():
    with pytest.raises(ValueError):
        OpenAIImageClient(FakeOpenAI(), aspect_ratio="4:3")


def test_gpt_image_2_rates():
    assert price("gpt-image-2", "token_in", 1_000_000) == pytest.approx(5.0)
    assert price("gpt-image-2", "image_token_in", 1_000_000) == pytest.approx(8.0)
    assert price("gpt-image-2", "token_out", 1_000_000) == pytest.approx(30.0)


def test_asset_generation_bills_every_image_and_nothing_is_unpriced(tmp_path):
    import json
    bp = {"style": "flat", "backgrounds": [{"id": "bg1", "prompt": "plate"}],
          "storyboard": [{"id": "sb1", "prompt": "grid", "reference": ["bg1"]}]}
    (tmp_path / "blueprint.json").write_text(json.dumps(bp))
    AssetGenerator(tmp_path, tmp_path, OpenAIImageClient(FakeOpenAI()),
                   retry_backoff_s=0).generate_all()
    assert (tmp_path / "backgrounds/bg1.png").is_file()
    assert (tmp_path / "storyboard/sb1.png").is_file()
    recs = [r for r in load(tmp_path) if r["layer"] == "L4"]
    assert {r["provider"] for r in recs} == {"openai"}
    s = summarize(tmp_path)
    assert s["unpriced"] == []
    # 2 images x (40 text in + 2000 image in + 1000 out)
    expected = 2 * (40 * 5 + 2000 * 8 + 1000 * 30) / 1e6
    assert s["total_usd"] == pytest.approx(expected)


def test_every_generated_image_gets_the_no_text_rule(tmp_path):
    """GPT Image 2 draws captions, timecodes and signage unless told not to."""
    import json
    from app.pipeline.image_gen import NO_TEXT_CLAUSE
    bp = {"style": "flat", "backgrounds": [{"id": "bg1", "prompt": "plate"}],
          "storyboard": [{"id": "sb1", "prompt": "grid", "reference": ["bg1"]}]}
    (tmp_path / "blueprint.json").write_text(json.dumps(bp))
    fake = FakeOpenAI()
    AssetGenerator(tmp_path, tmp_path, OpenAIImageClient(fake),
                   retry_backoff_s=0).generate_all()
    prompts = [kw["prompt"] for _, kw in fake.calls]
    assert len(prompts) == 2
    assert all(p.endswith(NO_TEXT_CLAUSE) for p in prompts)
    assert prompts[0].startswith("flat, plate")      # style still prepended
