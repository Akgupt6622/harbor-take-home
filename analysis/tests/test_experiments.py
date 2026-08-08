"""Ledger coverage against the real committed run artifacts (read-only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from analysis.experiments import (
    DEFAULT_HARNESS_DIR,
    build_entry,
    harness_digest,
    load_ledger,
    record,
    render_list,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = REPO_ROOT / "results" / "tau-retail" / "baseline"
SMOKE_DIR = REPO_ROOT / "results" / "tau-retail" / "smoke"
HARNESS_DIR = REPO_ROOT / DEFAULT_HARNESS_DIR


def test_build_entry_from_baseline() -> None:
    entry = build_entry(BASELINE_DIR, HARNESS_DIR, taskset=None, note="stock")
    assert entry.job_name == "baseline"
    assert (entry.trials, entry.passed, entry.failed, entry.errored) == (114, 83, 31, 0)
    assert entry.mean == pytest.approx(0.728, abs=1e-3)
    assert entry.agent_model == "gpt-5.6-luna"
    assert entry.user_model == "gpt-5.6-sol"
    assert entry.seed == 300
    assert entry.harness_commit
    assert entry.harness_digest.startswith("sha256:")


def test_digest_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "sub" / "b.md").write_text("policy\n")
    first = harness_digest(tmp_path)
    assert first == harness_digest(tmp_path)
    (tmp_path / "sub" / "b.md").write_text("policy v2\n")
    assert harness_digest(tmp_path) != first


def test_record_appends_and_refuses_duplicates(tmp_path: Path) -> None:
    ledger = tmp_path / "experiments.jsonl"
    record(SMOKE_DIR, ledger, HARNESS_DIR, taskset=None, note="smoke")
    record(BASELINE_DIR, ledger, HARNESS_DIR, taskset=None, note="stock")
    entries = load_ledger(ledger)
    assert [entry.job_name for entry in entries] == ["smoke", "baseline"]
    with pytest.raises(ValueError, match="already recorded"):
        record(SMOKE_DIR, ledger, HARNESS_DIR, taskset=None, note="dup")
    assert len(load_ledger(ledger)) == 2


def test_render_list_groups_by_digest_and_averages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "experiments.jsonl"
    record(SMOKE_DIR, ledger, HARNESS_DIR, taskset=None, note="")
    record(BASELINE_DIR, ledger, HARNESS_DIR, taskset="lists/subset.txt", note="sub")
    render_list(load_ledger(ledger))
    out = capsys.readouterr().out
    assert out.count("harness ") == 1  # same digest -> one harness section
    assert "full runs: 4/5; avg mean 0.800" in out
    assert "lists/subset.txt" in out
