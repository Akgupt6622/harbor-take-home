"""Failure-path coverage using mutated copies of smoke artifacts.

The smoke job contains no crashed trials and no malformed shapes, so these
tests synthesize them in tmp copies; results/ is never touched.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from analysis.parse_traces import (
    HARNESS_PACKAGE_ROOT,
    ParseError,
    decode_assistant_content,
    load_tool_types,
    parse_job,
    parse_trial,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_JOB_DIR = REPO_ROOT / "results" / "tau-retail" / "smoke"
TRIAL_NAME = "tau3-retail-54__SB33V7b"


@pytest.fixture()
def tool_types() -> dict[str, str]:
    return load_tool_types(REPO_ROOT / HARNESS_PACKAGE_ROOT)


@pytest.fixture()
def job_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "smoke"
    shutil.copytree(SMOKE_JOB_DIR, dest, ignore=shutil.ignore_patterns("harness", "_tau3_harness_upload"))
    return dest


def _edit_json(path: Path, mutate) -> None:
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def test_exception_trial_becomes_third_bucket(job_copy: Path, tool_types: dict[str, str]) -> None:
    trial_dir = job_copy / TRIAL_NAME
    _edit_json(
        trial_dir / "result.json",
        lambda d: d.update(exception_info={"type": "AgentTimeout", "message": "boom"}),
    )
    _edit_json(
        job_copy / "result.json",
        lambda d: (
            d["stats"].update(n_errored_trials=1),
            next(iter(d["stats"]["evals"].values()))["reward_stats"]["reward"]["1.0"].remove(TRIAL_NAME),
        ),
    )
    records = parse_job(job_copy, tool_types)
    record = next(r for r in records if r.attempt_key == "SB33V7b")
    assert record.exception == json.dumps({"message": "boom", "type": "AgentTimeout"}, sort_keys=True)
    assert record.verifier is None
    assert record.turns, "runtime state exists, so turns should be salvaged"
    assert record.seed == 626729


def test_exception_count_mismatch_fails_loudly(job_copy: Path, tool_types: dict[str, str]) -> None:
    _edit_json(
        (job_copy / TRIAL_NAME) / "result.json",
        lambda d: d.update(exception_info="boom"),
    )
    _edit_json(
        job_copy / "result.json",
        lambda d: next(iter(d["stats"]["evals"].values()))["reward_stats"]["reward"]["1.0"].remove(TRIAL_NAME),
    )
    with pytest.raises(ParseError, match="n_errored_trials"):
        parse_job(job_copy, tool_types)


def test_unknown_tool_name_fails_loudly(job_copy: Path, tool_types: dict[str, str]) -> None:
    state_path = job_copy / TRIAL_NAME / "agent" / "tau3_runtime_state.json"

    def rename_first_call(state: dict) -> None:
        message = next(m for m in state["messages"] if m.get("tool_calls"))
        message["tool_calls"][0]["name"] = "no_such_tool"

    _edit_json(state_path, rename_first_call)
    with pytest.raises(ParseError, match="no_such_tool"):
        parse_trial(job_copy / TRIAL_NAME, "smoke", tool_types)


def test_orphan_tool_result_fails_loudly(job_copy: Path, tool_types: dict[str, str]) -> None:
    state_path = job_copy / TRIAL_NAME / "agent" / "tau3_runtime_state.json"

    def break_call_id(state: dict) -> None:
        message = next(m for m in state["messages"] if m["role"] == "tool")
        message["id"] = "call_severed"

    _edit_json(state_path, break_call_id)
    with pytest.raises(ParseError, match="call_severed|without results"):
        parse_trial(job_copy / TRIAL_NAME, "smoke", tool_types)


def test_assistant_envelope_decoding() -> None:
    assert decode_assistant_content('{"message": "hi"}') == "hi"
    assert decode_assistant_content("plain greeting") == "plain greeting"
    padded = '{"order_id": "#W1", "status": "processed", "message": "hi"}'
    assert decode_assistant_content(padded) == "hi"
    non_envelope = '{"order_id": "#W1", "status": "processed"}'
    assert decode_assistant_content(non_envelope) == non_envelope
    assert decode_assistant_content('{"message": 5}') == '{"message": 5}'


def test_cost_drift_fails_loudly(job_copy: Path, tool_types: dict[str, str]) -> None:
    _edit_json(
        job_copy / "result.json",
        lambda d: d["stats"].update(cost_usd=d["stats"]["cost_usd"] + 0.01),
    )
    with pytest.raises(ParseError, match="cost sum"):
        parse_job(job_copy, tool_types)
