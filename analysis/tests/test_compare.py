"""Compare coverage: real baseline-vs-final smoke plus synthetic jobs.

Every synthetic test drives main(argv) or the pure functions against jobs
written under a tmp repo root; no run data is ever mutated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from analysis.cli import main
from analysis.compare import compare, load_verdicts, render

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_ARGV = [
    "compare",
    "runs/baseline",
    "runs/final",
    "--touched-tools",
    "exchange_delivered_order_items,modify_pending_order_items",
    "--history",
    "runs/baseline,runs/baseline-2,runs/baseline-3",
]


def make_job(
    root: Path, name: str, tasks: dict[str, tuple[bool | None, list[str]]]
) -> str:
    job_dir = root / "runs" / name
    job_dir.mkdir(parents=True)
    lines = []
    for task_id, (passed, labels) in tasks.items():
        record = {
            "task_id": task_id,
            "exception": "Boom" if passed is None else None,
            "verifier": (
                None
                if passed is None
                else {"passed": passed, "status": "passed" if passed else "mismatch"}
            ),
            "labels": [
                {
                    "label": label,
                    "source": "rule",
                    "evidence_turns": [],
                    "explanation": "",
                    "confidence": 1.0,
                }
                for label in labels
            ],
        }
        lines.append(json.dumps(record))
    (job_dir / "tasks.jsonl").write_text("\n".join(lines) + "\n")
    return f"runs/{name}"


def section(out: str, header: str) -> str:
    body = out.split(f"{header} (", 1)[1]
    return body.split("\n\n", 1)[0]


def task_line(block: str, task_id: str) -> str:
    return next(
        line for line in block.splitlines() if line.strip().startswith(f"{task_id} ")
    )


def test_real_load_verdicts_baseline() -> None:
    verdicts = load_verdicts(REPO_ROOT / "runs" / "baseline")
    assert len(verdicts) == 114
    passed, labels = verdicts["tau3-retail-98"]
    assert not passed
    assert labels == frozenset(
        {
            "wrong_args.exchange_delivered_order_items.new_item_ids.wrong_variant",
            "wrong_args.exchange_delivered_order_items.payment_method_id",
        }
    )
    assert verdicts["tau3-retail-0"] == (True, frozenset())


def test_real_baseline_vs_final(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(REAL_ARGV, repo_root=REPO_ROOT)
    out = capsys.readouterr().out
    assert exit_code == 1
    assert out.startswith("runs/baseline (83/114) -> runs/final (97/114)")
    regressed = section(out, "REGRESSED")
    assert (
        "caused-signature"
        in regressed.split("tau3-retail-49", 1)[1].split("tau3-retail-", 1)[0]
    )
    assert (
        "caused-signature"
        in regressed.split("tau3-retail-60", 1)[1].split("tau3-retail-", 1)[0]
    )
    still = section(out, "STILL FAILING")
    line = task_line(still, "tau3-retail-98")
    assert "IMPROVED" in line
    assert (
        "wrong_args.exchange_delivered_order_items.new_item_ids.wrong_variant" in line
    )


def test_real_flake_suspect_names_history_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(REAL_ARGV, repo_root=REPO_ROOT) == 1
    regressed = section(capsys.readouterr().out, "REGRESSED")
    classification = regressed.split("tau3-retail-64", 1)[1].split("tau3-retail-", 1)[0]
    assert "flake-suspect (shape pre-exists in runs/baseline-3)" in classification


def test_synthetic_flake_suspect_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_a = make_job(tmp_path, "a", {"t-1": (True, [])})
    job_b = make_job(tmp_path, "b", {"t-1": (False, ["wrong_args.tool1.x"])})
    job_h = make_job(tmp_path, "hist", {"t-1": (False, ["wrong_args.tool1.x"])})
    argv = ["compare", job_a, job_b, "--touched-tools", "tool1", "--history", job_h]
    assert main(argv, repo_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "flake-suspect (shape pre-exists in runs/hist)" in out
    assert "caused-signature" not in out


def test_synthetic_caused_signature_requires_new_label_and_touched_tool(
    tmp_path: Path,
) -> None:
    a = {"t-1": (True, frozenset()), "t-2": (True, frozenset())}
    b: dict[str, tuple[bool, frozenset[str]]] = {
        "t-1": (False, frozenset({"wrong_args.tool1.x"})),
        "t-2": (False, frozenset({"wrong_args.tool2.x"})),
    }
    result = compare(a, b, history=(), touched=frozenset({"tool1"}))
    assert result.caused_signature
    by_task = {r.task_id: r.classification for r in result.regressed}
    assert by_task == {
        "t-1": "caused-signature",
        "t-2": "new-shape (labels outside touched-tools)",
    }
    without_touched = compare(a, b, history=(), touched=None)
    assert not without_touched.caused_signature
    assert {r.classification for r in without_touched.regressed} == {
        "new-shape (no touched-tools given)"
    }


def test_synthetic_still_failing_buckets(tmp_path: Path) -> None:
    a = {
        "t-1": (False, frozenset({"wrong_args.tool1.x"})),
        "t-2": (False, frozenset({"wrong_args.tool1.x", "wrong_args.tool2.x"})),
        "t-3": (False, frozenset({"wrong_args.tool1.x"})),
    }
    b = {
        "t-1": (False, frozenset({"wrong_args.tool1.x"})),
        "t-2": (False, frozenset({"wrong_args.tool2.x"})),
        "t-3": (False, frozenset({"wrong_args.tool3.x"})),
    }
    result = compare(a, b, history=(), touched=None)
    assert result.total == 3
    assert not result.fixed and not result.regressed
    statuses = {s.task_id: s.status for s in result.still_failing}
    assert statuses == {"t-1": "SAME", "t-2": "IMPROVED", "t-3": "CHURNED"}
    out = render(result)
    assert "t-2  IMPROVED (gone: wrong_args.tool1.x)" in out
    assert "t-3  CHURNED (was: wrong_args.tool1.x; now: wrong_args.tool3.x)" in out


def test_synthetic_subset_vs_full_compares_intersection_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_a = make_job(
        tmp_path,
        "full",
        {
            "t-2": (True, []),
            "t-9": (False, ["wrong_args.tool1.x"]),
            "t-10": (False, ["wrong_args.tool1.x"]),
            "t-99": (False, ["wrong_args.tool9.x"]),
        },
    )
    job_b = make_job(
        tmp_path,
        "subset",
        {"t-10": (True, []), "t-9": (True, []), "t-2": (True, [])},
    )
    assert main(["compare", job_a, job_b], repo_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert out.startswith("runs/full (1/3) -> runs/subset (3/3)")
    assert "FIXED (2): t-9, t-10" in out
    assert "t-99" not in out


def test_synthetic_exception_record_is_failure_with_exception_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_a = make_job(tmp_path, "a", {"t-1": (True, [])})
    job_b = make_job(tmp_path, "b", {"t-1": (None, [])})
    verdicts = load_verdicts(tmp_path / "runs" / "b")
    assert verdicts == {"t-1": (False, frozenset({"(exception)"}))}
    argv = ["compare", job_a, job_b, "--touched-tools", "tool1"]
    assert main(argv, repo_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "t-1  [(exception)]" in out
    assert "new-shape (labels outside touched-tools)" in out


def test_compare_orders_tasks_numerically() -> None:
    a = {f"t-{n}": (False, frozenset()) for n in (11, 2, 100, 9)}
    b = {f"t-{n}": (True, frozenset()) for n in (11, 2, 100, 9)}
    result = compare(a, b, history=(), touched=None)
    assert result.fixed == ("t-2", "t-9", "t-11", "t-100")


def test_missing_tasks_jsonl_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_a = make_job(tmp_path, "a", {"t-1": (True, [])})
    assert main(["compare", job_a, "runs/ghost"], repo_root=tmp_path) == 2
    assert "tasks.jsonl not found" in capsys.readouterr().err
