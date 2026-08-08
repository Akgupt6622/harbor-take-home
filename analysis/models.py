"""Pydantic schema for parsed trial records (offline analysis only).

Grounded in analysis/SCHEMA_NOTES.md, verified against the smoke job.
Attempts are an unordered set: `attempt_key` is a uniqueness tag (the trial
dir's shortuuid suffix), never an ordering.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCall(StrictModel):
    turn_idx: int  # the assistant turn that issued the call
    call_id: str  # provider id, "call_..." form (joins to the tool turn)
    name: str
    args: dict
    result: str
    is_error: bool
    # ValueError text with the "Error: " prefix stripped; None when not an error.
    error_msg: str | None
    # Derived from @is_tool(ToolType.WRITE) in the harness source, never hardcoded.
    is_write: bool
    # Per-call wall time; only available via span enrichment (later module).
    duration_ms: float | None = None


class Turn(StrictModel):
    idx: int
    role: Literal["assistant", "user", "tool"]
    content: str
    tool_calls: list[ToolCall] = []
    ts: str  # ISO string, verbatim from runtime_state (naive UTC, sandbox clock)


class VerifierResult(StrictModel):
    passed: bool
    # Verbatim evaluation.status from the tau3.evaluation.result span.
    # Open vocabulary: "passed" and "mismatch" observed; others may exist.
    status: str


class FailureLabel(StrictModel):
    """Empty at parse time; the triage module owns this field."""

    label: str  # open vocabulary
    source: Literal["rule", "llm", "human"]
    evidence_turns: list[int]
    explanation: str
    confidence: float


class TrialRecord(StrictModel):
    task_id: str  # full form, e.g. "tau3-retail-91"
    job_name: str
    attempt_key: str  # dir shortuuid suffix — uniqueness tag ONLY, unordered
    trial_dir: str  # repo-relative path to the raw trial dir
    agent_model: str
    user_model: str
    # Fields below are None only on exception records (see validator).
    seed: int | None = None
    termination_reason: str | None = None
    step_count: int | None = None
    verifier: VerifierResult | None = None
    # Serialized exception_info for crashed trials. A third bucket: excluded
    # from pass/fail cross-checks, counted against the job's n_errored_trials.
    exception: str | None = None
    turns: list[Turn] = []
    n_agent_messages: int = 0  # assistant turns with non-empty content
    n_tool_calls: int = 0
    n_tool_errors: int = 0
    n_write_calls: int = 0
    cost_usd: float | None = None
    cost_by_role: dict[str, float] = {}
    tokens_in: int | None = None  # result.json convention: INCLUDES cached
    tokens_out: int | None = None
    n_cache_tokens: int | None = None
    wall_time_s: float | None = None  # agent_execution phase duration
    labels: list[FailureLabel] = []
    fragile_pass: bool = False

    @model_validator(mode="after")
    def _complete_unless_exception(self) -> "TrialRecord":
        if self.exception is None:
            required = (
                "seed",
                "termination_reason",
                "step_count",
                "verifier",
                "cost_usd",
                "tokens_in",
                "tokens_out",
                "n_cache_tokens",
                "wall_time_s",
            )
            missing = [f for f in required if getattr(self, f) is None]
            if missing:
                raise ValueError(
                    f"non-exception record is missing required fields: {missing}"
                )
            if not self.turns:
                raise ValueError("non-exception record has no turns")
        return self
