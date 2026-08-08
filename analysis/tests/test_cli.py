"""CLI coverage against the hand-written fixtures in fixtures/cli/.

Every test drives main(argv) directly with an injected repo root (a tmp copy
of the fixture job) and, for `run`, an injected runner — the real
run_subset.sh is never executed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from analysis import contracts
from analysis.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cli"
JOB_ARG = "runs/fixturejob"
ALL_GROUPS = "wrong_args.exchange_delivered_order_items.new_item_ids,missing_action.return_delivered_order_items,tool_error.find_user_id_by_email.user_not_found"
GROUP_TASKS = {
    "tau3-retail-2",
    "tau3-retail-3",
    "tau3-retail-5",
    "tau3-retail-7",
    "tau3-retail-8",
}


class RecordingRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, command: list[str], cwd: Path) -> int:
        self.calls.append((command, cwd))
        return self.returncode


def make_repo(root: Path) -> Path:
    job_dir = root / "runs" / "fixturejob"
    (job_dir / "triage").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "groups.json", job_dir / "triage" / "groups.json")
    shutil.copyfile(FIXTURES / "tasks.jsonl", job_dir / "tasks.jsonl")
    return job_dir


def test_groups_renders_totals_and_ranked_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    assert main(["groups", JOB_ARG], repo_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "fixturejob: 3/6 passed, 3 failed, 0 errored" in out
    positions = [
        out.index("wrong_args.exchange_delivered_order_items.new_item_ids"),
        out.index("missing_action.return_delivered_order_items"),
        out.index("tool_error.find_user_id_by_email.user_not_found"),
    ]
    assert positions == sorted(positions), "groups must render in index order"
    assert "divergence" in out and "tool_error" in out
    assert "tau3-retail-2, tau3-retail-3, tau3-retail-5 (+1 more)" in out


def test_show_lists_tasks_attempts_and_transcript_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    assert (
        main(
            ["show", JOB_ARG, "wrong_args.exchange_delivered_order_items.new_item_ids"],
            repo_root=tmp_path,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert (
        "wrong_args.exchange_delivered_order_items.new_item_ids (divergence, 4 tasks)"
        in out
    )
    assert "tau3-retail-2" in out
    assert "aK3mPq" in out and "zR8xWn" in out
    assert "turns [4,7]" in out
    assert "runs/fixturejob/transcripts/tau3-retail-2__aK3mPq.md" in out


def test_show_unknown_group_exits_2_listing_valid_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    assert main(["show", JOB_ARG, "nope"], repo_root=tmp_path) == 2
    err = capsys.readouterr().err
    assert "unknown group 'nope'" in err
    for group_id in ALL_GROUPS.split(","):
        assert group_id in err


def test_show_transcript_prints_first_attempt_content_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    transcripts = tmp_path / "runs" / "fixturejob" / "transcripts"
    transcripts.mkdir()
    (transcripts / "tau3-retail-2__aK3mPq.md").write_text("FIRST ATTEMPT BODY\n")
    (transcripts / "tau3-retail-2__zR8xWn.md").write_text("SECOND ATTEMPT BODY\n")
    exit_code = main(
        [
            "show",
            JOB_ARG,
            "tool_error.find_user_id_by_email.user_not_found",
            "--transcript",
        ],
        repo_root=tmp_path,
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "FIRST ATTEMPT BODY" in out
    assert "SECOND ATTEMPT BODY" not in out


def test_compose_is_deterministic_across_directories(tmp_path: Path) -> None:
    argv = [
        "compose",
        JOB_ARG,
        "probe",
        "--groups",
        "wrong_args.exchange_delivered_order_items.new_item_ids,tool_error.find_user_id_by_email.user_not_found",
        "--controls",
        "2",
        "--seed",
        "7",
    ]
    payloads: list[tuple[bytes, bytes, tuple[str, ...]]] = []
    for sub in ("a", "b"):
        root = tmp_path / sub
        job_dir = make_repo(root)
        assert main(argv, repo_root=root) == 0
        json_path = job_dir / "triage" / "tasksets" / "probe.json"
        txt_path = job_dir / "triage" / "tasksets" / "probe.txt"
        taskset = contracts.load_taskset(json_path)
        assert taskset.controls is not None
        payloads.append(
            (json_path.read_bytes(), txt_path.read_bytes(), taskset.controls.sampled)
        )
    assert payloads[0] == payloads[1]
    assert len(payloads[0][2]) == 2


def test_compose_controls_exclude_group_tasks(tmp_path: Path) -> None:
    job_dir = make_repo(tmp_path)
    argv = [
        "compose",
        JOB_ARG,
        "full",
        "--groups",
        ALL_GROUPS,
        "--controls",
        "3",
        "--seed",
        "1",
    ]
    assert main(argv, repo_root=tmp_path) == 0
    taskset = contracts.load_taskset(job_dir / "triage" / "tasksets" / "full.json")
    assert taskset.controls is not None
    assert set(taskset.controls.sampled) == {
        "tau3-retail-1",
        "tau3-retail-4",
        "tau3-retail-6",
    }
    assert not set(taskset.controls.sampled) & GROUP_TASKS
    assert taskset.tasks == tuple(sorted(GROUP_TASKS | set(taskset.controls.sampled)))


def test_compose_controls_require_seed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    argv = ["compose", JOB_ARG, "noseed", "--groups", ALL_GROUPS, "--controls", "2"]
    assert main(argv, repo_root=tmp_path) == 2
    assert "--seed is required" in capsys.readouterr().err


def test_compose_unknown_group_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    assert (
        main(["compose", JOB_ARG, "bad", "--groups", "ghost"], repo_root=tmp_path) == 2
    )
    err = capsys.readouterr().err
    assert "unknown group(s) ghost" in err
    assert "wrong_args.exchange_delivered_order_items.new_item_ids" in err


def test_compose_refuses_clobber_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_dir = make_repo(tmp_path)
    argv = [
        "compose",
        JOB_ARG,
        "frozen",
        "--groups",
        "missing_action.return_delivered_order_items",
    ]
    assert main(argv, repo_root=tmp_path) == 0
    json_path = job_dir / "triage" / "tasksets" / "frozen.json"
    before = json_path.read_bytes()
    assert main(argv, repo_root=tmp_path) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert json_path.read_bytes() == before
    assert main([*argv, "--force"], repo_root=tmp_path) == 0


def test_composed_taskset_round_trips_through_contracts(tmp_path: Path) -> None:
    job_dir = make_repo(tmp_path)
    argv = [
        "compose",
        JOB_ARG,
        "rt",
        "--groups",
        ALL_GROUPS,
        "--controls",
        "1",
        "--seed",
        "42",
    ]
    assert main(argv, repo_root=tmp_path) == 0
    taskset = contracts.load_taskset(job_dir / "triage" / "tasksets" / "rt.json")
    assert taskset.name == "rt"
    assert taskset.job_name == "fixturejob"
    assert taskset.from_groups == tuple(ALL_GROUPS.split(","))
    assert taskset.controls is not None
    assert (taskset.controls.n_requested, taskset.controls.seed) == (1, 42)
    assert taskset.tasks == tuple(sorted(taskset.tasks))
    txt = (job_dir / "triage" / "tasksets" / "rt.txt").read_text()
    assert txt == "".join(f"{task_id}\n" for task_id in taskset.tasks)


def test_compose_without_controls_freezes_union_only(tmp_path: Path) -> None:
    job_dir = make_repo(tmp_path)
    argv = [
        "compose",
        JOB_ARG,
        "plain",
        "--groups",
        "missing_action.return_delivered_order_items",
    ]
    assert main(argv, repo_root=tmp_path) == 0
    taskset = contracts.load_taskset(job_dir / "triage" / "tasksets" / "plain.json")
    assert taskset.controls is None
    assert taskset.tasks == ("tau3-retail-7",)


def test_run_without_yes_prints_command_and_never_executes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    assert (
        main(["compose", JOB_ARG, "sub", "--groups", ALL_GROUPS], repo_root=tmp_path)
        == 0
    )
    runner = RecordingRunner()
    exit_code = main(
        ["run", JOB_ARG, "sub", "--job-name", "retry1"],
        repo_root=tmp_path,
        runner=runner,
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "./run_subset.sh retry1 runs/fixturejob/triage/tasksets/sub.txt" in out
    assert "(costs API credits; re-run with --yes to execute)" in out
    assert runner.calls == []


def test_run_with_yes_invokes_runner_and_propagates_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    assert (
        main(["compose", JOB_ARG, "sub", "--groups", ALL_GROUPS], repo_root=tmp_path)
        == 0
    )
    runner = RecordingRunner(returncode=7)
    argv = ["run", JOB_ARG, "sub", "--job-name", "retry1", "--yes", "--n-attempts", "3"]
    assert main(argv, repo_root=tmp_path, runner=runner) == 7
    assert runner.calls == [
        (
            [
                "./run_subset.sh",
                "retry1",
                "runs/fixturejob/triage/tasksets/sub.txt",
                "--n-attempts",
                "3",
            ],
            tmp_path,
        )
    ]
    assert "./run_subset.sh retry1" in capsys.readouterr().out


def test_run_missing_taskset_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    runner = RecordingRunner()
    exit_code = main(
        ["run", JOB_ARG, "ghost", "--job-name", "retry1", "--yes"],
        repo_root=tmp_path,
        runner=runner,
    )
    assert exit_code == 2
    assert "missing file(s)" in capsys.readouterr().err
    assert runner.calls == []


def test_missing_index_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["groups", JOB_ARG], repo_root=tmp_path) == 2
    assert "triage index not found" in capsys.readouterr().err


def test_groups_by_tool_rolls_up_families(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    assert main(["groups", JOB_ARG, "--by-tool"], repo_root=tmp_path) == 0
    out = capsys.readouterr().out
    exchange_row = next(
        line for line in out.splitlines() if line.startswith("exchange_delivered")
    )
    assert "| 1" in exchange_row  # one group in the family
    # tau3-retail-2 sits in two families (exchange + find_user), so it is not
    # solo for either; the family's other tasks are.
    assert (
        out.index("exchange_delivered_order_items")
        < out.index("find_user_id_by_email")
        < out.index("return_delivered_order_items")
    )


def test_compose_by_tool_selects_all_family_groups(tmp_path: Path) -> None:
    job_dir = make_repo(tmp_path)
    argv = [
        "compose",
        JOB_ARG,
        "exchange-family",
        "--tools",
        "exchange_delivered_order_items,find_user_id_by_email",
    ]
    assert main(argv, repo_root=tmp_path) == 0
    taskset = contracts.load_taskset(
        job_dir / "triage" / "tasksets" / "exchange-family.json"
    )
    assert taskset.from_groups == (
        "wrong_args.exchange_delivered_order_items.new_item_ids",
        "tool_error.find_user_id_by_email.user_not_found",
    )
    assert "tau3-retail-2" in taskset.tasks


def test_compose_unknown_tool_lists_valid_tools(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    argv = ["compose", JOB_ARG, "bad", "--tools", "warp_drive"]
    assert main(argv, repo_root=tmp_path) == 2
    err = capsys.readouterr().err
    assert "warp_drive" in err
    assert "exchange_delivered_order_items" in err


def test_compose_requires_groups_or_tools(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path)
    assert main(["compose", JOB_ARG, "empty"], repo_root=tmp_path) == 2
    assert "--groups and/or --tools" in capsys.readouterr().err
