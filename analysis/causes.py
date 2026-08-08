"""The cause registry: durable, reviewed diagnoses that outlive any single run.

analysis/causes.json is the committed source of truth for WHY tasks fail.
Symptom groups (runs/<job>/triage/groups.json) are recomputed on every triage
pass and cannot hold judgments; causes are made by reading evidence, span
symptom groups, and drive fix verification via `cli compose --cause`.

Lifecycle: proposed (by human or the LLM analyst) -> confirmed (human review)
-> fixed | refuted (verification run results). Only confirmed causes may drive
paid runs. Controls/seed for a verification set live inside the composed
TaskSet artifact, not here — single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

DEFAULT_CAUSES_PATH = Path("analysis/causes.json")

CauseStatus = Literal["proposed", "confirmed", "fixed", "refuted"]
ProposedBy = Literal["human", "llm"]


@dataclass(frozen=True, slots=True)
class CauseVerification:
    taskset: str  # repo-relative path to the frozen TaskSet json
    predicted_flips: tuple[str, ...]  # member tasks the fix should flip to pass
    success_criterion: str  # pre-registered, mechanically checkable (counts)


@dataclass(frozen=True, slots=True)
class Cause:
    cause_id: str
    status: CauseStatus
    description: str
    tasks: tuple[str, ...]
    evidence: dict[str, str]  # task_id -> transcript path (inspected, not assumed)
    proposed_fix: str
    discovered_in: tuple[str, ...]  # job names
    proposed_by: ProposedBy
    verification: CauseVerification | None = None


_ADAPTER: TypeAdapter[tuple[Cause, ...]] = TypeAdapter(tuple[Cause, ...])


def load_causes(path: Path) -> tuple[Cause, ...]:
    causes = _ADAPTER.validate_json(path.read_bytes())
    seen: set[str] = set()
    for cause in causes:
        if cause.cause_id in seen:
            raise ValueError(f"duplicate cause_id {cause.cause_id!r} in {path}")
        seen.add(cause.cause_id)
    return causes


def dump_causes(causes: tuple[Cause, ...], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_ADAPTER.dump_json(causes, indent=2))


def get_cause(causes: tuple[Cause, ...], cause_id: str) -> Cause:
    for cause in causes:
        if cause.cause_id == cause_id:
            return cause
    raise KeyError(cause_id)
