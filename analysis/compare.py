"""Run comparison between two parsed+triaged jobs (A=reference, B=candidate).

Reads runs/<job>/tasks.jsonl only; compares the task intersection so a subset
job can be diffed against a full one. Exception records count as failures
labelled "(exception)".
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from analysis.cli import tool_of

EXCEPTION_LABEL = "(exception)"
CAUSED_SIGNATURE = "caused-signature"

Verdicts = dict[str, tuple[bool, frozenset[str]]]


@dataclass(frozen=True)
class Regression:
    task_id: str
    labels: tuple[str, ...]
    classification: str


@dataclass(frozen=True)
class StillFailing:
    task_id: str
    status: Literal["SAME", "IMPROVED", "CHURNED"]
    a_labels: tuple[str, ...]
    b_labels: tuple[str, ...]


@dataclass(frozen=True)
class CompareResult:
    name_a: str
    name_b: str
    a_passed: int
    b_passed: int
    total: int
    fixed: tuple[str, ...]
    regressed: tuple[Regression, ...]
    still_failing: tuple[StillFailing, ...]

    @property
    def caused_signature(self) -> bool:
        return any(r.classification == CAUSED_SIGNATURE for r in self.regressed)


def load_verdicts(job_dir: Path) -> Verdicts:
    verdicts: Verdicts = {}
    for line in (job_dir / "tasks.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("exception") is not None:
            verdicts[record["task_id"]] = (False, frozenset({EXCEPTION_LABEL}))
            continue
        labels = frozenset(entry["label"] for entry in record.get("labels", ()))
        verdicts[record["task_id"]] = (bool(record["verifier"]["passed"]), labels)
    return verdicts


def compare(
    a: Verdicts,
    b: Verdicts,
    history: Sequence[tuple[str, Verdicts]],
    touched: frozenset[str] | None,
    name_a: str = "A",
    name_b: str = "B",
) -> CompareResult:
    shared = sorted(set(a) & set(b), key=lambda t: int(t.rsplit("-", 1)[-1]))
    fixed: list[str] = []
    regressed: list[Regression] = []
    still_failing: list[StillFailing] = []
    for task in shared:
        a_passed, a_labels = a[task]
        b_passed, b_labels = b[task]
        if not a_passed and b_passed:
            fixed.append(task)
        elif a_passed and not b_passed:
            first_seen: dict[str, str] = {}
            for job_name, verdicts in history:
                for label in verdicts.get(task, (True, frozenset()))[1]:
                    first_seen.setdefault(label, job_name)
            has_new = any(label not in first_seen for label in b_labels)
            touched_hit = touched is not None and any(
                "." in label and tool_of(label) in touched for label in b_labels
            )
            if has_new and touched_hit:
                classification = CAUSED_SIGNATURE
            elif not has_new:
                shown_in = next(
                    (
                        job_name
                        for job_name, verdicts in history
                        if b_labels <= verdicts.get(task, (True, frozenset()))[1]
                    ),
                    ", ".join(
                        dict.fromkeys(first_seen[label] for label in sorted(b_labels))
                    ),
                )
                suffix = f" in {shown_in}" if shown_in else ""
                classification = f"flake-suspect (shape pre-exists{suffix})"
            elif touched is None:
                classification = "new-shape (no touched-tools given)"
            else:
                classification = "new-shape (labels outside touched-tools)"
            regressed.append(Regression(task, tuple(sorted(b_labels)), classification))
        elif not a_passed and not b_passed:
            if b_labels == a_labels:
                status: Literal["SAME", "IMPROVED", "CHURNED"] = "SAME"
            elif b_labels < a_labels:
                status = "IMPROVED"
            else:
                status = "CHURNED"
            still_failing.append(
                StillFailing(
                    task, status, tuple(sorted(a_labels)), tuple(sorted(b_labels))
                )
            )
    return CompareResult(
        name_a=name_a,
        name_b=name_b,
        a_passed=sum(1 for task in shared if a[task][0]),
        b_passed=sum(1 for task in shared if b[task][0]),
        total=len(shared),
        fixed=tuple(fixed),
        regressed=tuple(regressed),
        still_failing=tuple(still_failing),
    )


def render(result: CompareResult) -> str:
    lines = [
        f"{result.name_a} ({result.a_passed}/{result.total}) -> "
        f"{result.name_b} ({result.b_passed}/{result.total})",
        "",
        f"FIXED ({len(result.fixed)}): {', '.join(result.fixed)}".rstrip(),
        "",
        f"REGRESSED ({len(result.regressed)}):",
    ]
    for regression in result.regressed:
        lines.append(f"  {regression.task_id}  [{', '.join(regression.labels)}]")
        lines.append(f"    {regression.classification}")
    lines.extend(["", f"STILL FAILING ({len(result.still_failing)}):"])
    for entry in result.still_failing:
        if entry.status == "SAME":
            lines.append(f"  {entry.task_id}  SAME")
        elif entry.status == "IMPROVED":
            gone = [label for label in entry.a_labels if label not in entry.b_labels]
            lines.append(f"  {entry.task_id}  IMPROVED (gone: {', '.join(gone)})")
        else:
            lines.append(
                f"  {entry.task_id}  CHURNED (was: {', '.join(entry.a_labels)}; "
                f"now: {', '.join(entry.b_labels)})"
            )
    return "\n".join(lines) + "\n"
