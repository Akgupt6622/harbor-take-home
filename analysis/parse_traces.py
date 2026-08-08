"""Parse a Harbor job directory into TrialRecord JSONL plus human transcripts.

Usage: uv run python -m analysis.parse_traces results/tau-retail/<job> [--check]

Primary sources per SCHEMA_NOTES.md §5.8: agent/tau3_runtime_state.json for the
conversation, trial result.json for verdict/cost/tokens/phases. The span file is
touched for exactly one thing: the tau3.evaluation.result span's status string,
which exists nowhere else. Reads only; never modifies results/.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from analysis.models import ToolCall, TrialRecord, Turn, VerifierResult

HARNESS_PACKAGE_ROOT = Path("benchmarks/tau3-bench")
RUNTIME_STATE_PATH = Path("agent/tau3_runtime_state.json")
SPAN_FILE_PATH = Path("verifier/tau3_openinference_spans.otlp.jsonl")
EVALUATOR_SPAN_NAME = "tau3.evaluation.result"
AGENT_CONTENT_WRAPPER_KEY = "message"
ERROR_RESULT_PREFIX = "Error: "
TRANSCRIPT_RESULT_LIMIT = 400
COST_ABS_TOLERANCE = 1e-9


class ParseError(Exception):
    """Raised on any artifact shape that contradicts SCHEMA_NOTES.md."""


def load_tool_types(harness_package_root: Path) -> dict[str, str]:
    """Map retail tool name -> ToolType name from @is_tool decorators in the source.

    AST-based rather than import-based: the harness transitively imports `addict`,
    which only exists in the sandbox image. Still derived from source on every run,
    never a hardcoded list; unrecognized decorator shapes fail loudly.
    """
    tools_path = harness_package_root / "harness" / "retail" / "tools.py"
    tree = ast.parse(tools_path.read_text(), filename=str(tools_path))
    tool_types: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "is_tool"
            ):
                continue
            if not (
                len(decorator.args) == 1
                and isinstance(decorator.args[0], ast.Attribute)
                and isinstance(decorator.args[0].value, ast.Name)
                and decorator.args[0].value.id == "ToolType"
            ):
                raise ParseError(
                    f"{tools_path}:{node.lineno}: unrecognized @is_tool argument shape"
                )
            tool_types[node.name] = decorator.args[0].attr
    if not tool_types:
        raise ParseError(f"no @is_tool-decorated methods found in {tools_path}")
    return tool_types


def extract_evaluation(span_path: Path) -> tuple[str, bool]:
    """Return (evaluation.status, evaluation.success) from the single evaluator span."""
    found: list[tuple[str, bool]] = []
    with span_path.open() as fh:
        for line in fh:
            if EVALUATOR_SPAN_NAME not in line:
                continue
            for resource_spans in json.loads(line).get("resource_spans", []):
                for scope_spans in resource_spans.get("scope_spans", []):
                    for span in scope_spans.get("spans", []):
                        if span.get("name") == EVALUATOR_SPAN_NAME:
                            attrs = {
                                a["key"]: a["value"] for a in span.get("attributes", [])
                            }
                            found.append(
                                (
                                    attrs["evaluation.status"]["string_value"],
                                    attrs["evaluation.success"]["bool_value"],
                                )
                            )
    if len(found) != 1:
        raise ParseError(
            f"{span_path}: expected exactly 1 evaluator span, found {len(found)}"
        )
    return found[0]


def decode_assistant_content(raw: str) -> str:
    """Unwrap the harness's {"message": ...} JSON envelope; pass plain text through.

    The envelope shape is coupled to the current harness response format — if a
    harness iteration changes it, this is the single place to update, and any
    unrecognized JSON object fails loudly rather than leaking wrappers downstream.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(parsed, dict):
        if set(parsed) == {AGENT_CONTENT_WRAPPER_KEY} and isinstance(
            parsed[AGENT_CONTENT_WRAPPER_KEY], str
        ):
            return parsed[AGENT_CONTENT_WRAPPER_KEY]
        raise ParseError(f"unrecognized assistant content envelope: {raw[:200]!r}")
    return raw


def build_turns(
    messages: list[dict[str, Any]], tool_types: Mapping[str, str]
) -> list[Turn]:
    turns: list[Turn] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    staged: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    for position, message in enumerate(messages):
        role, idx = message["role"], message["turn_idx"]
        if idx != position:
            raise ParseError(
                f"turn_idx {idx} at position {position}: turns must be contiguous"
            )
        if role in ("assistant", "user"):
            if role == "user" and message.get("tool_calls"):
                raise ParseError(f"turn {idx}: user message with tool_calls")
            content = message["content"] or ""
            calls: list[dict[str, Any]] = []
            for call in message.get("tool_calls") or []:
                if call["requestor"] != "assistant":
                    raise ParseError(
                        f"turn {idx}: unexpected tool requestor {call['requestor']!r}"
                    )
                entry: dict[str, Any] = {
                    "turn_idx": idx,
                    "call_id": call["id"],
                    "name": call["name"],
                    "args": call["arguments"],
                    "is_write": _is_write(call["name"], tool_types, idx),
                }
                if call["id"] in pending_calls:
                    raise ParseError(f"turn {idx}: duplicate call_id {call['id']}")
                pending_calls[call["id"]] = entry
                calls.append(entry)
            staged.append(
                (
                    {
                        "idx": idx,
                        "role": role,
                        "content": decode_assistant_content(content)
                        if role == "assistant"
                        else content,
                        "ts": message["timestamp"],
                    },
                    calls,
                )
            )
        elif role == "tool":
            entry = pending_calls.pop(message["id"], None)
            if entry is None:
                raise ParseError(
                    f"turn {idx}: tool result for unknown call_id {message['id']}"
                )
            is_error = message["error"]
            if not isinstance(is_error, bool):
                raise ParseError(
                    f"turn {idx}: tool error flag is {type(is_error).__name__}, not bool"
                )
            content = message["content"]
            if is_error and not content.startswith(ERROR_RESULT_PREFIX):
                raise ParseError(
                    f"turn {idx}: error result without {ERROR_RESULT_PREFIX!r}: {content[:100]!r}"
                )
            entry["result"] = content
            entry["is_error"] = is_error
            entry["error_msg"] = (
                content.removeprefix(ERROR_RESULT_PREFIX) if is_error else None
            )
            staged.append(
                (
                    {
                        "idx": idx,
                        "role": role,
                        "content": content,
                        "ts": message["timestamp"],
                    },
                    [],
                )
            )
        else:
            raise ParseError(f"turn {idx}: unknown role {role!r}")

    if pending_calls:
        raise ParseError(f"tool calls without results: {sorted(pending_calls)}")
    for fields, calls in staged:
        turns.append(Turn(**fields, tool_calls=[ToolCall(**c) for c in calls]))
    return turns


def _is_write(name: str, tool_types: Mapping[str, str], idx: int) -> bool:
    if name not in tool_types:
        raise ParseError(f"turn {idx}: tool {name!r} not found in harness toolkit")
    return tool_types[name] == "WRITE"


def parse_trial(
    trial_dir: Path, job_name: str, tool_types: Mapping[str, str]
) -> TrialRecord:
    result = json.loads((trial_dir / "result.json").read_text())
    task_id = Path(result["task_id"]["path"]).name
    trial_name, attempt_key = result["trial_name"].rsplit("__", 1)
    if trial_name != task_id:
        raise ParseError(
            f"{trial_dir}: trial_name {result['trial_name']!r} != task {task_id!r}"
        )

    base: dict[str, Any] = {
        "task_id": task_id,
        "job_name": job_name,
        "attempt_key": attempt_key,
        "trial_dir": Path(os.path.relpath(trial_dir)).as_posix(),
        "agent_model": result["config"]["agent"]["model_name"],
        "user_model": result["config"]["agent"]["env"]["TAU2_USER_MODEL"],
    }
    if result["exception_info"] is not None:
        return TrialRecord(
            **base,
            exception=json.dumps(result["exception_info"], sort_keys=True),
            **_best_effort_fields(trial_dir, result, tool_types),
        )

    state = json.loads((trial_dir / RUNTIME_STATE_PATH).read_text())
    turns = build_turns(state["messages"], tool_types)
    status, success = extract_evaluation(trial_dir / SPAN_FILE_PATH)
    rewards = result["verifier_result"]["rewards"]
    if set(rewards) != {"reward"} or rewards["reward"] not in (0.0, 1.0):
        raise ParseError(f"{trial_dir}: unexpected rewards shape {rewards!r}")
    passed = rewards["reward"] == 1.0
    if success != passed:
        raise ParseError(
            f"{trial_dir}: evaluator success={success} but reward says passed={passed}"
        )

    calls = [call for turn in turns for call in turn.tool_calls]
    n_tool_errors = sum(call.is_error for call in calls)
    if n_tool_errors != state["num_errors"]:
        raise ParseError(
            f"{trial_dir}: {n_tool_errors} tool errors != num_errors {state['num_errors']}"
        )
    # A parallel tool batch is one orchestrator step but yields one message per
    # call (observed: smoke trial 96 turn 8), hence the extra-results term.
    n_call_batches = sum(1 for t in turns if t.tool_calls)
    extra_results = sum(1 for t in turns if t.role == "tool") - n_call_batches
    if len(state["messages"]) != state["step_count"] + 1 + extra_results:
        raise ParseError(
            f"{trial_dir}: {len(state['messages'])} messages != step_count "
            f"{state['step_count']} + 1 + {extra_results} extra parallel results"
        )

    agent_result = result["agent_result"]
    return TrialRecord(
        **base,
        seed=state["seed"],
        termination_reason=state["termination_reason"],
        step_count=state["step_count"],
        verifier=VerifierResult(passed=passed, status=status),
        turns=turns,
        n_agent_messages=sum(1 for t in turns if t.role == "assistant" and t.content),
        n_tool_calls=len(calls),
        n_tool_errors=n_tool_errors,
        n_write_calls=sum(call.is_write for call in calls),
        cost_usd=agent_result["cost_usd"],
        cost_by_role=agent_result["metadata"]["tau3_cost_usd_by_role"],
        tokens_in=agent_result["n_input_tokens"],
        tokens_out=agent_result["n_output_tokens"],
        n_cache_tokens=agent_result["n_cache_tokens"],
        wall_time_s=_phase_seconds(result["agent_execution"]),
    )


def _best_effort_fields(
    trial_dir: Path, result: dict[str, Any], tool_types: Mapping[str, str]
) -> dict[str, Any]:
    """Salvage whatever a crashed trial left behind; absent pieces stay None/empty."""
    fields: dict[str, Any] = {}
    state_path = trial_dir / RUNTIME_STATE_PATH
    if state_path.exists():
        state = json.loads(state_path.read_text())
        fields["turns"] = build_turns(state["messages"], tool_types)
        fields["seed"] = state["seed"]
        fields["termination_reason"] = state["termination_reason"]
        fields["step_count"] = state["step_count"]
    agent_result = result.get("agent_result")
    if agent_result is not None:
        fields["cost_usd"] = agent_result["cost_usd"]
        fields["cost_by_role"] = agent_result["metadata"]["tau3_cost_usd_by_role"]
        fields["tokens_in"] = agent_result["n_input_tokens"]
        fields["tokens_out"] = agent_result["n_output_tokens"]
        fields["n_cache_tokens"] = agent_result["n_cache_tokens"]
    if result.get("agent_execution"):
        fields["wall_time_s"] = _phase_seconds(result["agent_execution"])
    return fields


def _phase_seconds(phase: dict[str, str]) -> float:
    started = datetime.fromisoformat(phase["started_at"])
    finished = datetime.fromisoformat(phase["finished_at"])
    return (finished - started).total_seconds()


def parse_job(job_dir: Path, tool_types: Mapping[str, str]) -> list[TrialRecord]:
    trial_dirs = sorted(d for d in job_dir.iterdir() if d.is_dir())
    for trial_dir in trial_dirs:
        if not (trial_dir / "result.json").exists():
            raise ParseError(f"{trial_dir}: trial dir without result.json")
    records = [parse_trial(d, job_dir.name, tool_types) for d in trial_dirs]
    cross_check(records, json.loads((job_dir / "result.json").read_text()))
    return records


def cross_check(records: list[TrialRecord], job_result: dict[str, Any]) -> None:
    """Job-level invariants: parsed records must reproduce Harbor's own accounting."""
    stats = job_result["stats"]
    evals = stats["evals"]
    if len(evals) != 1:
        raise ParseError(f"expected exactly 1 eval in job stats, got {sorted(evals)}")
    histogram: dict[str, list[str]] = next(iter(evals.values()))["reward_stats"][
        "reward"
    ]

    scored = [r for r in records if r.exception is None]
    errored = [r for r in records if r.exception is not None]
    for reward_value, expected_passed in (("1.0", True), ("0.0", False)):
        expected = set(histogram.get(reward_value, []))
        actual = {
            f"{r.task_id}__{r.attempt_key}"
            for r in scored
            if r.verifier is not None and r.verifier.passed == expected_passed
        }
        if actual != expected:
            raise ParseError(
                f"reward={reward_value} trial-name mismatch: "
                f"parsed-only={sorted(actual - expected)}, harbor-only={sorted(expected - actual)}"
            )
    if len(errored) != stats["n_errored_trials"]:
        raise ParseError(
            f"{len(errored)} exception records != n_errored_trials {stats['n_errored_trials']}"
        )

    total_cost = sum(r.cost_usd for r in records if r.cost_usd is not None)
    if not math.isclose(total_cost, stats["cost_usd"], abs_tol=COST_ABS_TOLERANCE):
        raise ParseError(
            f"cost sum {total_cost!r} != job stats.cost_usd {stats['cost_usd']!r}"
        )
    for record_field, stats_field in (
        ("tokens_in", "n_input_tokens"),
        ("tokens_out", "n_output_tokens"),
        ("n_cache_tokens", "n_cache_tokens"),
    ):
        total = sum(getattr(r, record_field) or 0 for r in records)
        if total != stats[stats_field]:
            raise ParseError(
                f"{record_field} sum {total} != job stats.{stats_field} {stats[stats_field]}"
            )


def render_transcript(record: TrialRecord) -> str:
    if record.exception is not None:
        verdict = "EXCEPTION"
    elif record.verifier is not None and record.verifier.passed:
        verdict = f"PASSED ({record.verifier.status})"
    else:
        verdict = (
            f"FAILED ({record.verifier.status if record.verifier else 'no verdict'})"
        )
    lines = [
        f"# {record.task_id} — {verdict}",
        "",
        f"- job: {record.job_name} | attempt: {record.attempt_key} | seed: {record.seed}",
        f"- agent: {record.agent_model} | user-sim: {record.user_model}",
        f"- cost: ${record.cost_usd:.4f} | steps: {record.step_count} | "
        f"tool calls: {record.n_tool_calls} ({record.n_tool_errors} errors, "
        f"{record.n_write_calls} writes) | agent_execution: {record.wall_time_s:.1f}s"
        if record.cost_usd is not None
        else f"- exception: {record.exception}",
        "",
        "---",
        "",
    ]
    for turn in record.turns:
        if turn.role == "tool":
            continue
        speaker = "agent" if turn.role == "assistant" else "user"
        if turn.content:
            lines.append(f"**[{turn.idx}] {speaker}**: {turn.content}")
            lines.append("")
        for call in turn.tool_calls:
            marker = " ⚠️ ERROR" if call.is_error else ""
            write_tag = " [write]" if call.is_write else ""
            result = call.result
            if len(result) > TRANSCRIPT_RESULT_LIMIT:
                result = f"{result[:TRANSCRIPT_RESULT_LIMIT]}…[+{len(result) - TRANSCRIPT_RESULT_LIMIT} chars]"
            lines.append(
                f"**[{turn.idx}] agent → {call.name}{write_tag}**{marker}\n"
                f"- args: `{json.dumps(call.args)}`\n"
                f"- result: `{result}`"
            )
            lines.append("")
    return "\n".join(lines)


def write_outputs(records: list[TrialRecord], out_dir: Path) -> None:
    """Rebuild out_dir from scratch — it is fully derived, so wiping it is safe."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    transcripts_dir = out_dir / "transcripts"
    transcripts_dir.mkdir(parents=True)
    (out_dir / "tasks.jsonl").write_text(
        "".join(f"{r.model_dump_json()}\n" for r in records)
    )
    for record in records:
        path = transcripts_dir / f"{record.task_id}__{record.attempt_key}.md"
        path.write_text(render_transcript(record))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument(
        "--check", action="store_true", help="validate only, write nothing"
    )
    parser.add_argument("--runs-root", type=Path, default=Path("runs"))
    parser.add_argument("--harness-root", type=Path, default=HARNESS_PACKAGE_ROOT)
    args = parser.parse_args(argv)

    records = parse_job(args.job_dir, load_tool_types(args.harness_root))
    passed = sum(1 for r in records if r.verifier is not None and r.verifier.passed)
    errored = sum(1 for r in records if r.exception is not None)
    failed = len(records) - passed - errored
    summary = f"{args.job_dir.name}: {len(records)} trials — {passed} passed, {failed} failed, {errored} errored"
    if args.check:
        print(f"{summary}; all invariants and cross-checks hold (nothing written)")
        return
    out_dir = args.runs_root / args.job_dir.name
    write_outputs(records, out_dir)
    print(f"{summary} -> {out_dir / 'tasks.jsonl'} + {len(records)} transcripts")


if __name__ == "__main__":
    main()
