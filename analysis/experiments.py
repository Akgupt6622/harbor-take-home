"""Append-only experiment ledger: harness state -> runs -> scores.

Usage:
  uv run python -m analysis.experiments record results/tau-retail/<job> [--note ...]
  uv run python -m analysis.experiments list

Each run is one JSONL entry in experiments.jsonl binding the harness content
digest and launch commit (from the job's lock.json) to the run's score and
cost. `list` groups runs by harness digest and reports each run's pass rate
plus the per-harness average — the hill-climbing view. Re-doing an analysis:
`git checkout <harness_commit>`, then parse_traces + triage on results_dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

DEFAULT_LEDGER = Path("experiments.jsonl")
DEFAULT_HARNESS_DIR = Path("benchmarks/tau3-bench/harness")


@dataclass(frozen=True, slots=True)
class Experiment:
    job_name: str
    results_dir: str
    harness_commit: str  # repo commit recorded by harbor at launch (lock.json)
    harness_digest: str  # sha256 over the harness dir contents at record time
    taskset: str | None  # task-list file for subset runs; None = full split
    agent_model: str
    user_model: str
    seed: int
    trials: int
    passed: int
    failed: int
    errored: int
    mean: float
    cost_usd: float
    started_at: str
    finished_at: str
    note: str


_ADAPTER: TypeAdapter[Experiment] = TypeAdapter(Experiment)


def harness_digest(harness_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in harness_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(harness_dir).as_posix().encode())
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return f"sha256:{digest.hexdigest()}"


def load_ledger(ledger_path: Path) -> list[Experiment]:
    if not ledger_path.is_file():
        return []
    return [
        _ADAPTER.validate_json(line)
        for line in ledger_path.read_text().splitlines()
        if line.strip()
    ]


def build_entry(
    results_dir: Path,
    harness_dir: Path,
    taskset: str | None,
    note: str,
) -> Experiment:
    result = json.loads((results_dir / "result.json").read_text())
    config = json.loads((results_dir / "config.json").read_text())
    lock = json.loads((results_dir / "lock.json").read_text())
    stats = result["stats"]
    evals = stats["evals"]
    if len(evals) != 1:
        raise ValueError(f"{results_dir}: expected exactly 1 eval, got {sorted(evals)}")
    eval_stats = next(iter(evals.values()))
    histogram: dict[str, list[str]] = eval_stats["reward_stats"]["reward"]
    (agent,) = config["agents"]
    return Experiment(
        job_name=config["job_name"],
        results_dir=results_dir.as_posix(),
        harness_commit=lock["harbor"]["git_commit_hash"],
        harness_digest=harness_digest(harness_dir),
        taskset=taskset,
        agent_model=agent["model_name"],
        user_model=agent["env"]["TAU2_USER_MODEL"],
        seed=agent["kwargs"]["seed"],
        trials=stats["n_completed_trials"],
        passed=len(histogram.get("1.0", [])),
        failed=len(histogram.get("0.0", [])),
        errored=stats["n_errored_trials"],
        mean=eval_stats["metrics"][0]["mean"],
        cost_usd=stats["cost_usd"],
        started_at=result["started_at"],
        finished_at=result["finished_at"],
        note=note,
    )


def record(
    results_dir: Path,
    ledger_path: Path,
    harness_dir: Path,
    taskset: str | None,
    note: str,
) -> Experiment:
    entry = build_entry(results_dir, harness_dir, taskset, note)
    for existing in load_ledger(ledger_path):
        if existing.results_dir == entry.results_dir:
            raise ValueError(
                f"{entry.results_dir} is already recorded (job {existing.job_name!r}); "
                "the ledger is append-only and runs are immutable"
            )
    with ledger_path.open("a") as fh:
        fh.write(_ADAPTER.dump_json(entry).decode() + "\n")
    return entry


def render_list(entries: list[Experiment]) -> None:
    if not entries:
        print("no experiments recorded")
        return
    by_digest: dict[str, list[Experiment]] = {}
    for entry in entries:
        by_digest.setdefault(entry.harness_digest, []).append(entry)
    # Ledger order is chronological; first appearance orders the harness states.
    for digest, runs in by_digest.items():
        full = [run for run in runs if run.taskset is None]
        label = f"harness {digest[len('sha256:') :][:12]} (commit {runs[0].harness_commit[:7]})"
        if full:
            avg = sum(run.mean for run in full) / len(full)
            rates = ", ".join(f"{run.passed}/{run.trials}" for run in full)
            label += f" — full runs: {rates}; avg mean {avg:.3f}"
        print(label)
        for run in runs:
            scope = run.taskset or "full split"
            print(
                f"  {run.job_name:<16} {run.passed:>3}/{run.trials:<3} "
                f"mean {run.mean:.3f}  ${run.cost_usd:.2f}  {scope}"
                + (f"  — {run.note}" if run.note else "")
            )
        print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record_parser = sub.add_parser("record", help="append one run to the ledger")
    record_parser.add_argument("results_dir", type=Path)
    record_parser.add_argument("--note", default="")
    record_parser.add_argument("--taskset", default=None)
    record_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    record_parser.add_argument("--harness-dir", type=Path, default=DEFAULT_HARNESS_DIR)
    list_parser = sub.add_parser("list", help="runs grouped by harness state")
    list_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    args = parser.parse_args(argv)

    if args.command == "record":
        try:
            entry = record(
                args.results_dir, args.ledger, args.harness_dir, args.taskset, args.note
            )
        except (ValueError, FileNotFoundError) as error:
            print(f"error: {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(
            f"recorded {entry.job_name}: {entry.passed}/{entry.trials} "
            f"(mean {entry.mean:.3f}) on harness {entry.harness_digest[:19]}"
        )
    else:
        render_list(load_ledger(args.ledger))


if __name__ == "__main__":
    main()
