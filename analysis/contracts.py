"""Frozen contract between triage (producer) and the CLI (consumer).

Triage emits runs/<job>/triage/groups.json (a TriageIndex); the CLI composes
frozen TaskSets under runs/<job>/triage/tasksets/. Both sides load and dump
exclusively through the TypeAdapter helpers here, so any contract drift fails
loudly at the file boundary rather than surfacing as silent shape bugs.

Semantics both sides rely on:
- Group membership is non-exclusive: a task joins every group any attempt
  evidenced. Labels never affect pass/fail; the verifier verdict is the only
  ground truth.
- Determinism: groups sorted by (-len(tasks), group_id); tasks by task_id;
  attempts by attempt_key; TaskSet.tasks sorted; control sampling is seeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

TRIAGE_DIRNAME = "triage"
GROUPS_FILENAME = "groups.json"
TASKSETS_DIRNAME = "tasksets"

GroupKind = Literal["divergence", "tool_error", "behavior", "llm"]


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    attempt_key: str
    evidence_turns: tuple[int, ...]
    transcript: str  # repo-relative path to the rendered markdown


@dataclass(frozen=True, slots=True)
class TaskRef:
    task_id: str
    attempts: tuple[AttemptEvidence, ...]


@dataclass(frozen=True, slots=True)
class Group:
    group_id: str  # the label string itself; filesystem-safe, stable across runs
    kind: GroupKind
    tasks: tuple[TaskRef, ...]

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(ref.task_id for ref in self.tasks)


@dataclass(frozen=True, slots=True)
class Totals:
    trials: int
    passed: int
    failed: int
    errored: int


@dataclass(frozen=True, slots=True)
class TriageIndex:
    job_name: str
    source: str  # repo-relative path to the labeled tasks.jsonl
    totals: Totals
    groups: tuple[Group, ...]

    def group(self, group_id: str) -> Group:
        for candidate in self.groups:
            if candidate.group_id == group_id:
                return candidate
        raise KeyError(group_id)


@dataclass(frozen=True, slots=True)
class Controls:
    n_requested: int
    seed: int
    sampled: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskSet:
    name: str
    job_name: str  # job whose triage this set was composed from
    from_groups: tuple[str, ...]
    controls: Controls | None
    tasks: tuple[str, ...]  # frozen union: group tasks + sampled controls


_INDEX_ADAPTER: TypeAdapter[TriageIndex] = TypeAdapter(TriageIndex)
_TASKSET_ADAPTER: TypeAdapter[TaskSet] = TypeAdapter(TaskSet)


def triage_dir(runs_job_dir: Path) -> Path:
    return runs_job_dir / TRIAGE_DIRNAME


def load_index(path: Path) -> TriageIndex:
    return _INDEX_ADAPTER.validate_json(path.read_bytes())


def dump_index(index: TriageIndex, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_INDEX_ADAPTER.dump_json(index, indent=2))


def load_taskset(path: Path) -> TaskSet:
    return _TASKSET_ADAPTER.validate_json(path.read_bytes())


def dump_taskset(taskset: TaskSet, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_TASKSET_ADAPTER.dump_json(taskset, indent=2))


def write_tasklist(taskset: TaskSet, path: Path) -> None:
    """Flat one-task-per-line export, directly consumable by run_subset.sh."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{task_id}\n" for task_id in taskset.tasks))
