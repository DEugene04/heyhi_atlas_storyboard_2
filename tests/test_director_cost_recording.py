"""
/pipeline/direct must record token spend even when the director FAILS.

REGRESSION GUARD. The first version of this instrumentation recorded tokens
after direct() returned. When the director exhausted its retries it raised, the
handler returned 502, and the recording line was never reached — so a run that
burned (max_retries + 1) billed director calls recorded nothing at all.

That is backwards. A failing director is the EXPENSIVE case: every attempt is a
real, answered, billed API call; the only thing that failed was our own
validation of the content. Recording only on success hid exactly the spend worth
knowing about.

These tests use a fake DirectorLLM, so they exercise the endpoint's recording
logic with no network and no key.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import main
from app.costs import load, summarize


class FakeDirectorLLM:
    """Stands in for OpenAIDirector. Appends a usage entry per call, exactly as
    the real client does, and returns whatever text it was configured with."""

    model = "gpt-4o"

    def __init__(self, reply: str):
        self.reply = reply
        self.usage_log: list[dict] = []

    def complete(self, system: str, user: str) -> str:
        self.usage_log.append({"input_tokens": 1000, "output_tokens": 500})
        return self.reply


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient whose storage root is a temp dir, so tests never touch real
    runs. ATLAS_STORAGE_ROOT is read at import time, so the module constant is
    patched directly."""
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    return TestClient(main.app)


def _make_run(client, tmp_path) -> str:
    r = client.post("/pipeline/config", json={
        "language": "english", "audience": "new employees",
        "style": "professional realism art style",
        "objective": "explain the onboarding process",
    })
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    (tmp_path / run_id / "source.txt").write_text("Some policy text.", encoding="utf-8")
    return run_id


def test_failed_director_still_records_its_token_spend(client, tmp_path, monkeypatch):
    """The bug: three failed director runs cost real money and reported $0.00."""
    run_id = _make_run(client, tmp_path)
    llm = FakeDirectorLLM("this is not json")          # fails parsing every attempt
    monkeypatch.setattr(main, "_director_llm", lambda **kw: llm)

    r = client.post("/pipeline/direct", json={"run_id": run_id, "max_retries": 2})
    assert r.status_code == 502, "director should have failed"

    records = load(tmp_path / run_id)
    assert records, "a failed director run must still record what it spent"

    # 3 attempts (max_retries=2 -> 3 calls), each writing token_in + token_out
    assert len(llm.usage_log) == 3
    assert len(records) == 6
    assert {r["director_outcome"] for r in records} == {"failed"}
    assert sorted({r["attempt"] for r in records}) == [1, 2, 3]

    # 3 x (1000 in + 500 out) at $2.50/$10.00 per 1M (gpt-4o)
    expected = 3 * (1000 * 2.50 + 500 * 10.00) / 1_000_000
    assert summarize(tmp_path / run_id)["total_usd"] == pytest.approx(expected)


def test_successful_director_records_every_attempt_once(client, tmp_path, monkeypatch, bp_dict):
    """The other half: a success must record all its attempts and must NOT
    double-count them (an earlier fix left two recording blocks in place)."""
    run_id = _make_run(client, tmp_path)
    llm = FakeDirectorLLM(json.dumps(bp_dict))         # valid on the first try
    monkeypatch.setattr(main, "_director_llm", lambda **kw: llm)

    r = client.post("/pipeline/direct", json={"run_id": run_id})
    assert r.status_code == 200, r.text

    records = load(tmp_path / run_id)
    assert len(llm.usage_log) == 1
    assert len(records) == 2, "one token_in + one token_out, recorded exactly once"
    assert {r["director_outcome"] for r in records} == {"ok"}


def test_cost_endpoint_reports_failed_spend(client, tmp_path, monkeypatch):
    """/pipeline/cost is how you find out; it must work on a run that never
    produced a blueprint."""
    run_id = _make_run(client, tmp_path)
    monkeypatch.setattr(main, "_director_llm", lambda **kw: FakeDirectorLLM("nope"))
    client.post("/pipeline/direct", json={"run_id": run_id, "max_retries": 1})

    r = client.get("/pipeline/cost", params={"run_id": run_id})
    assert r.status_code == 200
    body = r.json()
    assert body["total_usd"] > 0
    assert body["by_layer"]["L2"] > 0
    assert body["unpriced"] == []


def test_a_run_that_never_called_the_director_costs_nothing(client, tmp_path):
    run_id = _make_run(client, tmp_path)
    assert client.get("/pipeline/cost", params={"run_id": run_id}).json()["total_usd"] == 0.0
