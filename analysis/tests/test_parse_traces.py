"""Validation suite for the parse pipeline, pinned to the frozen smoke job.

Run: uv run pytest analysis/tests -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analysis.error_keys import load_error_keys
from analysis.models import TrialRecord
from analysis.parse_traces import (
    HARNESS_PACKAGE_ROOT,
    load_tool_types,
    parse_job,
    write_outputs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_JOB_DIR = REPO_ROOT / "results" / "tau-retail" / "smoke"
GOLDEN_DIR = Path(__file__).parent / "golden" / "smoke"
TOOLS_PATH = REPO_ROOT / HARNESS_PACKAGE_ROOT / "harness" / "retail" / "tools.py"


@pytest.fixture(scope="module")
def smoke_records() -> list[TrialRecord]:
    return parse_job(SMOKE_JOB_DIR, load_tool_types(REPO_ROOT / HARNESS_PACKAGE_ROOT))


def test_smoke_verdicts(smoke_records: list[TrialRecord]) -> None:
    by_task = {r.task_id: r for r in smoke_records}
    assert len(smoke_records) == 5
    assert all(r.exception is None for r in smoke_records)
    failed = [r.task_id for r in smoke_records if r.verifier and not r.verifier.passed]
    assert failed == ["tau3-retail-91"]
    assert by_task["tau3-retail-91"].verifier is not None
    assert by_task["tau3-retail-91"].verifier.status == "mismatch"
    assert by_task["tau3-retail-54"].n_tool_errors == 1
    assert by_task["tau3-retail-96"].step_count == 21


def test_every_error_msg_has_a_key(smoke_records: list[TrialRecord]) -> None:
    error_msgs = [
        call.error_msg
        for record in smoke_records
        for turn in record.turns
        for call in turn.tool_calls
        if call.is_error
    ]
    assert error_msgs, "smoke set is expected to contain at least one tool error"
    error_keys = load_error_keys(TOOLS_PATH)
    for msg in error_msgs:
        assert msg is not None
        error_keys.match(msg)


def test_write_flags_derived_from_source(smoke_records: list[TrialRecord]) -> None:
    tool_types = load_tool_types(REPO_ROOT / HARNESS_PACKAGE_ROOT)
    assert sorted(v for v in set(tool_types.values())) == ["GENERIC", "READ", "WRITE"]
    for record in smoke_records:
        for turn in record.turns:
            for call in turn.tool_calls:
                assert call.is_write == (tool_types[call.name] == "WRITE")


def test_golden_fixtures_byte_identical(
    smoke_records: list[TrialRecord], tmp_path: Path
) -> None:
    write_outputs(smoke_records, tmp_path / "smoke")
    golden_files = sorted(p for p in GOLDEN_DIR.rglob("*") if p.is_file())
    fresh_files = sorted(p for p in (tmp_path / "smoke").rglob("*") if p.is_file())
    assert [p.relative_to(GOLDEN_DIR) for p in golden_files] == [
        p.relative_to(tmp_path / "smoke") for p in fresh_files
    ]
    for golden, fresh in zip(golden_files, fresh_files):
        assert fresh.read_bytes() == golden.read_bytes(), f"{golden.name} drifted"


def test_records_roundtrip_through_json(smoke_records: list[TrialRecord]) -> None:
    for record in smoke_records:
        assert TrialRecord(**json.loads(record.model_dump_json())) == record
