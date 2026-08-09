"""Inspection/composition CLI over triage output.

Consumes runs/<job>/triage/groups.json strictly through analysis.contracts and
freezes TaskSets under runs/<job>/triage/tasksets/. `run` never executes
run_subset.sh (which spends API credits) without an explicit --yes.

Exit codes: 0 success, 2 user error (bad group id, missing files, clobber
refusal). Errors go to stderr.
"""

from __future__ import annotations

import argparse
import json
import random
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from analysis import contracts
from analysis.causes import DEFAULT_CAUSES_PATH, Cause, get_cause, load_causes

PREVIEW_TASK_IDS = 3


def tool_of(group_id: str) -> str:
    """Group ids follow <type>.<tool>[.<qualifier>]; the tool is segment 1.

    missing_communication.<key> labels have no tool; they roll up into a
    synthetic 'communicate' family.
    """
    if group_id.startswith("missing_communication."):
        return "communicate"
    return group_id.split(".")[1]


def _print_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [
        max(len(row[i]) for row in (header, *rows)) for i in range(len(header) - 1)
    ]
    for row in (header, *rows):
        cells = [cell.ljust(width) for cell, width in zip(row[:-1], widths)]
        print(" | ".join((*cells, row[-1])))


def _preview(task_ids: tuple[str, ...]) -> str:
    preview = ", ".join(task_ids[:PREVIEW_TASK_IDS])
    if len(task_ids) > PREVIEW_TASK_IDS:
        preview += f" (+{len(task_ids) - PREVIEW_TASK_IDS} more)"
    return preview


def cmd_groups(index: contracts.TriageIndex, by_tool: bool) -> int:
    totals = index.totals
    print(
        f"{index.job_name}: {totals.passed}/{totals.trials} passed, "
        f"{totals.failed} failed, {totals.errored} errored"
    )
    print()
    if by_tool:
        families: dict[str, list[contracts.Group]] = {}
        for group in index.groups:
            families.setdefault(tool_of(group.group_id), []).append(group)
        family_tasks = {
            tool: tuple(sorted({t for g in groups for t in g.task_ids}))
            for tool, groups in families.items()
        }
        membership: dict[str, set[str]] = {}
        for tool, task_ids in family_tasks.items():
            for task_id in task_ids:
                membership.setdefault(task_id, set()).add(tool)
        rows = [
            (
                tool,
                str(len(families[tool])),
                str(len(family_tasks[tool])),
                str(
                    sum(
                        1
                        for task_id in family_tasks[tool]
                        if membership[task_id] == {tool}
                    )
                ),
                _preview(family_tasks[tool]),
            )
            for tool in sorted(families, key=lambda t: (-len(family_tasks[t]), t))
        ]
        _print_table(("tool", "n_groups", "n_tasks", "n_solo", "tasks"), rows)
        return 0
    membership = {}
    for group in index.groups:
        for task_id in group.task_ids:
            membership.setdefault(task_id, set()).add(group.group_id)
    rows = []
    # Groups arrive pre-sorted by (-len(tasks), group_id); never re-sort.
    for group in index.groups:
        # solo: this group is the task's only diagnosis, so fixing it alone
        # should flip the task — the fix-priority signal.
        n_solo = sum(
            1 for task_id in group.task_ids if membership[task_id] == {group.group_id}
        )
        rows.append(
            (
                group.group_id,
                group.kind,
                str(len(group.tasks)),
                str(n_solo),
                _preview(group.task_ids),
            )
        )
    _print_table(("group_id", "kind", "n_tasks", "n_solo", "tasks"), rows)
    return 0


def cmd_show(
    index: contracts.TriageIndex,
    group_id: str,
    show_transcript: bool,
    repo_root: Path,
) -> int:
    try:
        group = index.group(group_id)
    except KeyError:
        valid = ", ".join(g.group_id for g in index.groups)
        print(
            f"error: unknown group {group_id!r}; valid ids: {valid}",
            file=sys.stderr,
        )
        return 2
    print(f"{group.group_id} ({group.kind}, {len(group.tasks)} tasks)")
    for ref in group.tasks:
        print()
        print(ref.task_id)
        for attempt in ref.attempts:
            turns = ",".join(str(turn) for turn in attempt.evidence_turns)
            print(f"  {attempt.attempt_key}  turns [{turns}]  {attempt.transcript}")
        if show_transcript and ref.attempts:
            first = ref.attempts[0]
            path = repo_root / first.transcript
            if not path.is_file():
                print(f"error: transcript not found: {path}", file=sys.stderr)
                return 2
            print(f"--- {first.transcript} ---")
            print(path.read_text(), end="")
    return 0


def cmd_compose(
    index: contracts.TriageIndex,
    job_dir: Path,
    name: str,
    group_ids: tuple[str, ...],
    causes: tuple[Cause, ...],
    n_controls: int | None,
    seed: int | None,
    control_jobs: tuple[Path, ...],
    force: bool,
    repo_root: Path,
) -> int:
    valid = ", ".join(g.group_id for g in index.groups)
    unknown = [
        gid for gid in group_ids if gid not in {g.group_id for g in index.groups}
    ]
    if unknown:
        print(
            f"error: unknown group(s) {', '.join(unknown)}; valid ids: {valid}",
            file=sys.stderr,
        )
        return 2
    union: set[str] = set()
    for gid in group_ids:
        union.update(index.group(gid).task_ids)
    provenance = group_ids
    for cause in causes:
        union.update(cause.tasks)
        provenance = (*provenance, f"cause:{cause.cause_id}")
    controls: contracts.Controls | None = None
    if n_controls is not None:
        if seed is None:
            print("error: --seed is required when --controls is given", file=sys.stderr)
            return 2
        universe, error = _control_universe(index, union, control_jobs, repo_root)
        if error is not None:
            print(f"error: {error}", file=sys.stderr)
            return 2
        if n_controls > len(universe):
            print(
                f"error: requested {n_controls} controls but only {len(universe)} "
                f"tasks exist outside the selected groups",
                file=sys.stderr,
            )
            return 2
        sampled = random.Random(seed).sample(universe, n_controls)
        controls = contracts.Controls(
            n_requested=n_controls, seed=seed, sampled=tuple(sampled)
        )
    taskset = contracts.TaskSet(
        name=name,
        job_name=index.job_name,
        from_groups=provenance,
        controls=controls,
        tasks=tuple(sorted(union | set(controls.sampled if controls else ()))),
    )
    tasksets_dir = contracts.triage_dir(job_dir) / contracts.TASKSETS_DIRNAME
    json_path = tasksets_dir / f"{name}.json"
    txt_path = tasksets_dir / f"{name}.txt"
    existing = [path for path in (json_path, txt_path) if path.exists()]
    if existing and not force:
        listing = ", ".join(str(path) for path in existing)
        print(
            f"error: refusing to overwrite frozen taskset file(s): {listing} "
            f"(pass --force to overwrite)",
            file=sys.stderr,
        )
        return 2
    contracts.dump_taskset(taskset, json_path)
    contracts.write_tasklist(taskset, txt_path)
    n_controls_written = len(controls.sampled) if controls else 0
    print(
        f"wrote {json_path} and {txt_path} "
        f"({len(taskset.tasks)} tasks: {len(union)} from groups, {n_controls_written} controls)"
    )
    return 0


def _control_universe(
    index: contracts.TriageIndex,
    members: set[str],
    control_jobs: tuple[Path, ...],
    repo_root: Path,
) -> tuple[list[str], str | None]:
    """Sampling universe for regression controls, in deterministic file order.

    With --controls-from, only tasks that PASSED in every listed job qualify:
    controls exist to catch regressions, and a task that was already failing or
    flaky carries no regression signal (baseline noise is ±7 tasks).
    """
    sources = [repo_root / job / "tasks.jsonl" for job in control_jobs] or [
        repo_root / index.source
    ]
    passing_everywhere: dict[str, bool] | None = None
    order: list[str] = []
    for source in sources:
        if not source.is_file():
            return [], f"controls source not found: {source}"
        verdicts: dict[str, bool] = {}
        for line in source.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            verdict = record.get("verifier")
            passed = bool(verdict and verdict.get("passed"))
            verdicts[record["task_id"]] = passed if control_jobs else True
            if passing_everywhere is None:
                order.append(record["task_id"])
        if passing_everywhere is None:
            passing_everywhere = verdicts
        else:
            passing_everywhere = {
                task: ok and verdicts.get(task, False)
                for task, ok in passing_everywhere.items()
            }
    assert passing_everywhere is not None
    return [
        task
        for task in order
        if task not in members and passing_everywhere.get(task, False)
    ], None


def cmd_run(
    job_dir: Path,
    name: str,
    harbor_job: str,
    yes: bool,
    extra: list[str],
    repo_root: Path,
    runner: Callable[[list[str], Path], int],
) -> int:
    tasksets_dir = contracts.triage_dir(job_dir) / contracts.TASKSETS_DIRNAME
    json_path = tasksets_dir / f"{name}.json"
    txt_path = tasksets_dir / f"{name}.txt"
    missing = [path for path in (json_path, txt_path) if not path.is_file()]
    if missing:
        listing = ", ".join(str(path) for path in missing)
        print(f"error: taskset {name!r} is missing file(s): {listing}", file=sys.stderr)
        return 2
    taskset = contracts.load_taskset(json_path)
    try:
        txt_display = str(txt_path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        txt_display = str(txt_path)
    command = ["./run_subset.sh", harbor_job, txt_display, *extra]
    print(
        f"taskset {taskset.name}: {len(taskset.tasks)} tasks (from job {taskset.job_name})"
    )
    print(shlex.join(command))
    if not yes:
        print("(costs API credits; re-run with --yes to execute)")
        return 0
    return runner(command, repo_root)


def subprocess_runner(command: list[str], cwd: Path) -> int:
    # Inherits stdout/stderr so run_subset.sh output streams live.
    return subprocess.run(command, cwd=cwd, check=False).returncode


def main(
    argv: Sequence[str],
    repo_root: Path | None = None,
    runner: Callable[[list[str], Path], int] = subprocess_runner,
) -> int:
    root = repo_root if repo_root is not None else Path.cwd()
    parser = argparse.ArgumentParser(prog="analysis.cli", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    groups_parser = sub.add_parser(
        "groups", help="print totals and the ranked group table"
    )
    groups_parser.add_argument("job_dir", type=Path)
    groups_parser.add_argument(
        "--by-tool",
        action="store_true",
        help="roll groups up into per-tool families",
    )

    show_parser = sub.add_parser(
        "show", help="print a group's tasks and attempt evidence"
    )
    show_parser.add_argument("job_dir", type=Path)
    show_parser.add_argument("group_id")
    show_parser.add_argument(
        "--transcript",
        action="store_true",
        help="also print each task's first-attempt transcript",
    )

    compose_parser = sub.add_parser(
        "compose", help="freeze a named taskset from groups"
    )
    compose_parser.add_argument("job_dir", type=Path)
    compose_parser.add_argument("name")
    compose_parser.add_argument(
        "--groups", default="", help="comma-separated group ids"
    )
    compose_parser.add_argument(
        "--tools",
        default="",
        help="comma-separated tool names; selects every group of those tools",
    )
    compose_parser.add_argument(
        "--cause", default=None, help="cause_id from the registry"
    )
    compose_parser.add_argument("--cause-file", type=Path, default=DEFAULT_CAUSES_PATH)
    compose_parser.add_argument(
        "--controls-from",
        default="",
        help="comma-separated runs/<job> dirs; controls sample only from tasks passing in ALL of them",
    )
    compose_parser.add_argument("--controls", type=int, default=None)
    compose_parser.add_argument("--seed", type=int, default=None)
    compose_parser.add_argument("--force", action="store_true")

    run_parser = sub.add_parser("run", help="wrap run_subset.sh for a frozen taskset")
    run_parser.add_argument("job_dir", type=Path)
    run_parser.add_argument("name")
    run_parser.add_argument("--job-name", required=True, dest="harbor_job")
    run_parser.add_argument("--yes", action="store_true")

    compare_parser = sub.add_parser(
        "compare", help="diff two parsed+triaged jobs (A=reference, B=candidate)"
    )
    compare_parser.add_argument("job_dir_a", type=Path)
    compare_parser.add_argument("job_dir_b", type=Path)
    compare_parser.add_argument(
        "--touched-tools",
        default=None,
        help="comma-separated tool families changed by the candidate",
    )
    compare_parser.add_argument(
        "--history",
        default="",
        help="comma-separated runs/<job> dirs supplying prior failure shapes",
    )

    args, extra = parser.parse_known_args(list(argv))
    if extra and args.command != "run":
        print(f"error: unrecognized arguments: {' '.join(extra)}", file=sys.stderr)
        return 2
    if args.command == "compare":
        from analysis.compare import compare, load_verdicts, render

        named_dirs = [
            (str(path), path if path.is_absolute() else root / path)
            for path in (
                args.job_dir_a,
                args.job_dir_b,
                *(Path(name) for name in args.history.split(",") if name),
            )
        ]
        missing_sources = [
            path / "tasks.jsonl"
            for _, path in named_dirs
            if not (path / "tasks.jsonl").is_file()
        ]
        if missing_sources:
            listing = ", ".join(str(path) for path in missing_sources)
            print(f"error: tasks.jsonl not found: {listing}", file=sys.stderr)
            return 2
        verdicts = [(name, load_verdicts(path)) for name, path in named_dirs]
        touched = (
            frozenset(tool for tool in args.touched_tools.split(",") if tool)
            if args.touched_tools is not None
            else None
        )
        result = compare(
            verdicts[0][1],
            verdicts[1][1],
            verdicts[2:],
            touched,
            name_a=verdicts[0][0],
            name_b=verdicts[1][0],
        )
        print(render(result), end="")
        return 1 if result.caused_signature else 0
    job_dir: Path = args.job_dir if args.job_dir.is_absolute() else root / args.job_dir
    if args.command == "run":
        return cmd_run(
            job_dir, args.name, args.harbor_job, args.yes, extra, root, runner
        )
    groups_path = contracts.triage_dir(job_dir) / contracts.GROUPS_FILENAME
    if not groups_path.is_file():
        print(f"error: triage index not found: {groups_path}", file=sys.stderr)
        return 2
    index = contracts.load_index(groups_path)
    if args.command == "groups":
        return cmd_groups(index, args.by_tool)
    if args.command == "show":
        return cmd_show(index, args.group_id, args.transcript, root)
    group_ids = tuple(gid for gid in args.groups.split(",") if gid)
    tools = tuple(tool for tool in args.tools.split(",") if tool)
    if tools:
        valid_tools = {tool_of(group.group_id) for group in index.groups}
        unknown_tools = [tool for tool in tools if tool not in valid_tools]
        if unknown_tools:
            print(
                f"error: unknown tool(s) {', '.join(unknown_tools)}; "
                f"valid tools: {', '.join(sorted(valid_tools))}",
                file=sys.stderr,
            )
            return 2
        group_ids += tuple(
            group.group_id
            for group in index.groups
            if tool_of(group.group_id) in tools and group.group_id not in group_ids
        )
    selected_causes: tuple[Cause, ...] = ()
    if args.cause is not None:
        causes_path = (
            args.cause_file if args.cause_file.is_absolute() else root / args.cause_file
        )
        if not causes_path.is_file():
            print(f"error: cause file not found: {causes_path}", file=sys.stderr)
            return 2
        registry = load_causes(causes_path)
        try:
            selected_causes = tuple(
                get_cause(registry, cid) for cid in args.cause.split(",") if cid
            )
        except KeyError as missing:
            valid_causes = ", ".join(c.cause_id for c in registry)
            print(
                f"error: unknown cause {missing}; valid: {valid_causes}",
                file=sys.stderr,
            )
            return 2
    if not group_ids and not selected_causes:
        print("error: provide --groups, --tools, and/or --cause", file=sys.stderr)
        return 2
    control_jobs = tuple(Path(p) for p in args.controls_from.split(",") if p)
    return cmd_compose(
        index,
        job_dir,
        args.name,
        group_ids,
        selected_causes,
        args.controls,
        args.seed,
        control_jobs,
        args.force,
        root,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
