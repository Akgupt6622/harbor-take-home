"""Registry + compose --cause + stable-pass control sampling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analysis import contracts
from analysis.causes import Cause, dump_causes, get_cause, load_causes
from analysis.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cli"
JOB_ARG = "runs/fixturejob"
REPO_ROOT = Path(__file__).resolve().parents[2]


def make_repo(root: Path) -> Path:
    job_dir = root / "runs" / "fixturejob"
    (job_dir / "triage").mkdir(parents=True)
    for src, dest in (("groups.json", job_dir / "triage" / "groups.json"),):
        dest.write_bytes((FIXTURES / src).read_bytes())
    (job_dir / "tasks.jsonl").write_bytes((FIXTURES / "tasks.jsonl").read_bytes())
    return job_dir


def write_cause(root: Path, tasks: tuple[str, ...]) -> Path:
    path = root / "causes.json"
    dump_causes(
        (
            Cause(
                cause_id="fixture_cause",
                status="confirmed",
                description="fixture",
                tasks=tasks,
                evidence={},
                proposed_fix="fixture fix",
                discovered_in=("fixturejob",),
                proposed_by="human",
            ),
        ),
        path,
    )
    return path


def write_verdict_job(root: Path, job: str, verdicts: dict[str, bool]) -> None:
    job_dir = root / "runs" / job
    job_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"task_id": task, "verifier": {"passed": passed, "status": "x"}})
        for task, passed in verdicts.items()
    ]
    (job_dir / "tasks.jsonl").write_text("".join(f"{line}\n" for line in lines))


def test_registry_roundtrip_and_duplicate_guard(tmp_path: Path) -> None:
    causes = load_causes(REPO_ROOT / "analysis" / "causes.json")
    assert get_cause(causes, "auth_username_deadend").cause_id  # exists; status is lifecycle-mutable
    for cause in causes:
        for task, transcript in cause.evidence.items():
            assert task in cause.tasks
            assert (REPO_ROOT / transcript).is_file(), transcript
    path = tmp_path / "dup.json"
    dup = (causes[0], causes[0])
    dump_causes(dup, path)
    with pytest.raises(ValueError, match="duplicate cause_id"):
        load_causes(path)


def test_compose_from_cause_records_provenance(tmp_path: Path) -> None:
    job_dir = make_repo(tmp_path)
    cause_file = write_cause(tmp_path, ("tau3-retail-2", "tau3-retail-999"))
    argv = [
        "compose",
        JOB_ARG,
        "authset",
        "--cause",
        "fixture_cause",
        "--cause-file",
        str(cause_file),
    ]
    assert main(argv, repo_root=tmp_path) == 0
    taskset = contracts.load_taskset(job_dir / "triage" / "tasksets" / "authset.json")
    assert taskset.from_groups == ("cause:fixture_cause",)
    assert set(taskset.tasks) == {"tau3-retail-2", "tau3-retail-999"}


def test_compose_unknown_cause_lists_valid_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    cause_file = write_cause(tmp_path, ("tau3-retail-2",))
    argv = [
        "compose",
        JOB_ARG,
        "bad",
        "--cause",
        "nope",
        "--cause-file",
        str(cause_file),
    ]
    assert main(argv, repo_root=tmp_path) == 2
    assert "fixture_cause" in capsys.readouterr().err


def test_controls_from_samples_only_stable_passes(tmp_path: Path) -> None:
    job_dir = make_repo(tmp_path)
    cause_file = write_cause(tmp_path, ("tau3-retail-2",))
    write_verdict_job(
        tmp_path,
        "jobA",
        {
            "tau3-retail-1": True,
            "tau3-retail-2": True,
            "tau3-retail-3": True,
            "tau3-retail-4": True,
            "tau3-retail-5": False,
        },
    )
    write_verdict_job(
        tmp_path,
        "jobB",
        {
            "tau3-retail-1": True,
            "tau3-retail-2": True,
            "tau3-retail-3": False,
            "tau3-retail-4": True,
            "tau3-retail-5": True,
        },
    )
    argv = [
        "compose",
        JOB_ARG,
        "stable",
        "--cause",
        "fixture_cause",
        "--cause-file",
        str(cause_file),
        "--controls",
        "2",
        "--seed",
        "11",
        "--controls-from",
        "runs/jobA,runs/jobB",
    ]
    assert main(argv, repo_root=tmp_path) == 0
    taskset = contracts.load_taskset(job_dir / "triage" / "tasksets" / "stable.json")
    assert taskset.controls is not None
    # Universe is exactly the tasks passing in BOTH jobs minus the member: 1 and 4.
    assert set(taskset.controls.sampled) == {"tau3-retail-1", "tau3-retail-4"}
    rerun = [
        "compose",
        JOB_ARG,
        "stable2",
        "--cause",
        "fixture_cause",
        "--cause-file",
        str(cause_file),
        "--controls",
        "2",
        "--seed",
        "11",
        "--controls-from",
        "runs/jobA,runs/jobB",
    ]
    assert main(rerun, repo_root=tmp_path) == 0
    again = contracts.load_taskset(job_dir / "triage" / "tasksets" / "stable2.json")
    assert again.controls is not None
    assert again.controls.sampled == taskset.controls.sampled
