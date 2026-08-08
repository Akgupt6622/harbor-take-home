"""Rule-based triage: label parsed trials and group failures for the CLI.

Usage: uv run python -m analysis.triage runs/<job> [--check]

Recomputes source=="rule" labels idempotently (llm/human labels survive),
rewrites runs/<job>/tasks.jsonl in parse_traces.write_outputs format, and dumps
the TriageIndex to runs/<job>/triage/groups.json through the frozen contract.

Gold expected_actions include READ/GENERIC calls, but no retail task grades on
action matching directly: reward_basis is DB end state (+ NL assertions), so
only WRITE actions can explain a DB divergence — the matcher diffs gold WRITE
actions against successful write calls and ignores the rest.

The NL side: no retail task has COMMUNICATE in its reward_basis; the benchmark
compiled expected_communicate_info into gpt-5.2-judged nl_assertions instead.
The missing_communication labels mirror tau2's CommunicateEvaluator matching
(case-insensitive substring, commas stripped from the message) as a
deterministic PROXY for that sanitized NL-judge verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from analysis import contracts
from analysis.error_keys import match_error_key
from analysis.models import FailureLabel, ToolCall, TrialRecord
from analysis.parse_traces import HARNESS_PACKAGE_ROOT, load_tool_types

DEFAULT_DATASETS_ROOT = Path("datasets/tau3-bench")
DEFAULT_TAU2_ROOT = Path(os.getenv("TAU2_BENCH_ROOT", "../tau2-bench"))
ANCHOR_FIELD = "order_id"
UNRECOGNIZED_KEY = "unrecognized"
ANCHORED_CONFIDENCE = 1.0
UNANCHORED_CONFIDENCE = 0.8
DIVERGENCE_PREFIXES = (
    "wrong_args.",
    "missing_action.",
    "extra_write.",
    "attempted_but_rejected.",
    "missing_communication.",
)


@dataclass(frozen=True, slots=True)
class GoldAction:
    action_id: str
    name: str
    arguments: dict[str, Any]
    compare_fields: tuple[str, ...] | None  # None = compare every argument key


@dataclass(frozen=True, slots=True)
class Finding:
    label: str
    evidence_turns: tuple[int, ...]
    explanation: str
    confidence: float


def load_gold_actions(
    datasets_root: Path, task_id: str, tool_types: Mapping[str, str]
) -> list[GoldAction]:
    config_path = datasets_root / task_id / "tests" / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"{task_id}: no gold config at {config_path}")
    actions: list[GoldAction] = []
    for raw in json.loads(config_path.read_text())["expected_actions"]:
        if raw["requestor"] != "assistant":
            raise ValueError(
                f"{task_id}: expected action {raw['action_id']} has requestor "
                f"{raw['requestor']!r}; the matcher assumes assistant-only gold"
            )
        compare = raw["compare_args"]
        if compare is not None and not (
            isinstance(compare, list)
            and all(isinstance(field, str) for field in compare)
        ):
            raise ValueError(
                f"{task_id}: expected action {raw['action_id']} has unsupported "
                f"compare_args shape {compare!r}; only null or a list of arg "
                "names is defined"
            )
        if raw["name"] not in tool_types:
            raise LookupError(
                f"{task_id}: expected action tool {raw['name']!r} not in the "
                "harness toolkit"
            )
        if tool_types[raw["name"]] != "WRITE":
            continue
        actions.append(
            GoldAction(
                action_id=raw["action_id"],
                name=raw["name"],
                arguments=dict(raw["arguments"]),
                compare_fields=tuple(compare) if compare is not None else None,
            )
        )
    return actions


def load_communicate_info(datasets_root: Path, task_id: str) -> list[str]:
    config_path = datasets_root / task_id / "tests" / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"{task_id}: no gold config at {config_path}")
    return list(json.loads(config_path.read_text())["expected_communicate_info"] or [])


def values_equal(gold_value: object, agent_value: object) -> bool:
    if isinstance(gold_value, list) and isinstance(agent_value, list):
        # Gold list order is not part of the contract: compare as multisets.
        return sorted(json.dumps(v, sort_keys=True) for v in gold_value) == sorted(
            json.dumps(v, sort_keys=True) for v in agent_value
        )
    return gold_value == agent_value


def resolve_error_key(message: str, unrecognized: list[str]) -> str:
    try:
        return match_error_key(message)
    except LookupError:
        unrecognized.append(message)
        return UNRECOGNIZED_KEY


def load_item_products(tau2_root: Path) -> dict[str, str] | None:
    """item_id -> product_id from the tau2 retail catalog; None when unavailable."""
    db_path = tau2_root / "data" / "tau2" / "domains" / "retail" / "db.json"
    if not db_path.is_file():
        return None
    return {
        item_id: product["product_id"]
        for product in json.loads(db_path.read_text())["products"].values()
        for item_id in product["variants"]
    }


def classify_list_diff(
    gold_value: list[Any],
    agent_value: list[Any],
    item_products: Mapping[str, str] | None,
) -> str:
    def multiset(values: list[Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = json.dumps(value, sort_keys=True)
            counts[key] = counts.get(key, 0) + 1
        return counts

    gold_counts, agent_counts = multiset(gold_value), multiset(agent_value)
    gold_only = [
        json.loads(key)
        for key, count in gold_counts.items()
        for _ in range(count - agent_counts.get(key, 0))
        if count > agent_counts.get(key, 0)
    ]
    agent_only = [
        json.loads(key)
        for key, count in agent_counts.items()
        for _ in range(count - gold_counts.get(key, 0))
        if count > gold_counts.get(key, 0)
    ]
    if gold_only and not agent_only:
        return "missing_items"
    if agent_only and not gold_only:
        return "extra_items"
    if len(gold_value) == len(agent_value):
        swapped = [*gold_only, *agent_only]
        if item_products is not None and all(
            isinstance(item, str) and item in item_products for item in swapped
        ):
            products = {item_products[item] for item in swapped}
            return "wrong_variant" if len(products) == 1 else "unrelated_item"
        return "substituted"
    return "mixed"


def match_gold(
    gold_actions: Sequence[GoldAction],
    calls: Sequence[ToolCall],
    unrecognized: list[str],
    item_products: Mapping[str, str] | None = None,
) -> list[Finding]:
    writes = [call for call in calls if call.is_write and not call.is_error]
    findings: list[Finding] = []
    for name in sorted({g.name for g in gold_actions} | {c.name for c in writes}):
        pending_gold = [g for g in gold_actions if g.name == name]
        pending_writes = [c for c in writes if c.name == name]
        pending_errors = [c for c in calls if c.is_error and c.name == name]

        pairs: list[tuple[GoldAction, ToolCall, float]] = []
        for action in list(pending_gold):
            anchor = action.arguments.get(ANCHOR_FIELD)
            if anchor is None:
                continue
            anchored_gold = [
                g for g in pending_gold if g.arguments.get(ANCHOR_FIELD) == anchor
            ]
            anchored_writes = [
                c for c in pending_writes if c.args.get(ANCHOR_FIELD) == anchor
            ]
            # An anchor pairs only when unambiguous on both sides.
            if len(anchored_gold) == 1 and len(anchored_writes) == 1:
                pairs.append((action, anchored_writes[0], ANCHORED_CONFIDENCE))
                pending_gold.remove(action)
                pending_writes.remove(anchored_writes[0])
        while pending_gold and pending_writes:
            _, g_pos, c_pos = min(
                (
                    -sum(
                        1
                        for field in (
                            action.compare_fields
                            if action.compare_fields is not None
                            else tuple(action.arguments)
                        )
                        if values_equal(
                            action.arguments.get(field), call.args.get(field)
                        )
                    ),
                    g_pos,
                    c_pos,
                )
                for g_pos, action in enumerate(pending_gold)
                for c_pos, call in enumerate(pending_writes)
            )
            pairs.append(
                (
                    pending_gold.pop(g_pos),
                    pending_writes.pop(c_pos),
                    UNANCHORED_CONFIDENCE,
                )
            )

        for action, call, confidence in pairs:
            fields = (
                action.compare_fields
                if action.compare_fields is not None
                else tuple(action.arguments)
            )
            for field in fields:
                gold_value = action.arguments.get(field)
                agent_value = call.args.get(field)
                if values_equal(gold_value, agent_value):
                    continue
                label = f"wrong_args.{name}.{field}"
                if isinstance(gold_value, list) and isinstance(agent_value, list):
                    label += (
                        f".{classify_list_diff(gold_value, agent_value, item_products)}"
                    )
                findings.append(
                    Finding(
                        label=label,
                        evidence_turns=(call.turn_idx,),
                        explanation=(
                            f"{name}: agent used {field}={agent_value!r}, "
                            f"gold expects {gold_value!r}."
                        ),
                        confidence=confidence,
                    )
                )
        for action in pending_gold:
            anchor = action.arguments.get(ANCHOR_FIELD)
            rescue: ToolCall | None = None
            rescue_confidence = UNANCHORED_CONFIDENCE
            for call in pending_errors:
                call_anchor = call.args.get(ANCHOR_FIELD)
                if anchor is not None and call_anchor is not None:
                    if call_anchor == anchor:
                        rescue, rescue_confidence = call, ANCHORED_CONFIDENCE
                        break
                elif rescue is None:
                    rescue = call
            if rescue is not None:
                pending_errors.remove(rescue)
                key = resolve_error_key(rescue.error_msg or "", unrecognized)
                findings.append(
                    Finding(
                        label=f"attempted_but_rejected.{name}.{key}",
                        evidence_turns=(rescue.turn_idx,),
                        explanation=(
                            f"Gold expects {name}({json.dumps(action.arguments, sort_keys=True)}); "
                            f"the attempt at turn {rescue.turn_idx} was rejected "
                            f"with {rescue.error_msg!r}."
                        ),
                        confidence=rescue_confidence,
                    )
                )
            else:
                findings.append(
                    Finding(
                        label=f"missing_action.{name}",
                        evidence_turns=(),
                        explanation=(
                            f"Gold expects {name}({json.dumps(action.arguments, sort_keys=True)}) "
                            "but no successful write matches."
                        ),
                        confidence=1.0,
                    )
                )
        for call in pending_writes:
            findings.append(
                Finding(
                    label=f"extra_write.{name}",
                    evidence_turns=(call.turn_idx,),
                    explanation=(
                        f"Agent wrote {name}({json.dumps(call.args, sort_keys=True)}) "
                        "which matches no gold action."
                    ),
                    confidence=1.0,
                )
            )
    return findings


def communicate_key(info: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", info.lower()).strip("_")[:32] or "empty"


def missed_communications(
    record: TrialRecord, communicate_info: Sequence[str]
) -> list[FailureLabel]:
    # Mirrors tau2's CommunicateEvaluator: case-insensitive substring, commas
    # stripped from the message. A deterministic proxy for the NL-judge verdict.
    assistant_turns = [
        (turn.idx, turn.content.lower().replace(",", ""))
        for turn in record.turns
        if turn.role == "assistant" and turn.content
    ]
    evidence = [assistant_turns[-1][0]] if assistant_turns else []
    return [
        FailureLabel(
            label=f"missing_communication.{communicate_key(info)}",
            source="rule",
            evidence_turns=evidence,
            explanation=(
                f"Expected info {info!r} does not appear in any assistant message "
                "(proxy for the NL-assertion judge)."
            ),
            confidence=1.0,
        )
        for info in communicate_info
        if not any(info.lower() in text for _, text in assistant_turns)
    ]


def triage_record(
    record: TrialRecord,
    gold_actions: Sequence[GoldAction],
    unrecognized: list[str],
    communicate_info: Sequence[str] = (),
    item_products: Mapping[str, str] | None = None,
) -> None:
    record.labels = [label for label in record.labels if label.source != "rule"]
    record.fragile_pass = False
    if record.exception is not None:
        return
    calls = [call for turn in record.turns for call in turn.tool_calls]
    passed = record.verifier is not None and record.verifier.passed
    rule_labels: list[FailureLabel] = []
    for call in calls:
        if not call.is_error:
            continue
        key = resolve_error_key(call.error_msg or "", unrecognized)
        rule_labels.append(
            FailureLabel(
                label=f"tool_error.{call.name}.{key}",
                source="rule",
                evidence_turns=[call.turn_idx],
                explanation=f"{call.name} at turn {call.turn_idx} failed with {call.error_msg!r}.",
                confidence=1.0,
            )
        )
    findings = match_gold(gold_actions, calls, unrecognized, item_products)
    # Divergence findings on passed trials feed only fragile_pass: the verifier
    # compares DB end states, so a passed trial may legitimately diverge.
    if not passed:
        rule_labels.extend(
            FailureLabel(
                label=finding.label,
                source="rule",
                evidence_turns=list(finding.evidence_turns),
                explanation=finding.explanation,
                confidence=finding.confidence,
            )
            for finding in findings
        )
        rule_labels.extend(missed_communications(record, communicate_info))
    if passed and (findings or any(call.is_error for call in calls)):
        record.fragile_pass = True
    record.labels.extend(rule_labels)


def build_index(job_dir: Path, records: Sequence[TrialRecord]) -> contracts.TriageIndex:
    """Groups come from failed trials only; passed trials carry at most fragile_pass."""
    membership: dict[str, dict[str, dict[str, set[int]]]] = {}
    for record in records:
        if record.exception is not None or record.verifier is None:
            continue
        if record.verifier.passed:
            continue
        for label in record.labels:
            if label.source != "rule":
                continue
            attempts = membership.setdefault(label.label, {}).setdefault(
                record.task_id, {}
            )
            attempts.setdefault(record.attempt_key, set()).update(label.evidence_turns)
    groups: list[contracts.Group] = []
    for group_id, tasks in membership.items():
        if group_id.startswith("tool_error."):
            kind: contracts.GroupKind = "tool_error"
        elif group_id.startswith(DIVERGENCE_PREFIXES):
            kind = "divergence"
        else:
            raise ValueError(f"rule label {group_id!r} has no known group kind")
        groups.append(
            contracts.Group(
                group_id=group_id,
                kind=kind,
                tasks=tuple(
                    contracts.TaskRef(
                        task_id=task_id,
                        attempts=tuple(
                            contracts.AttemptEvidence(
                                attempt_key=attempt_key,
                                evidence_turns=tuple(sorted(evidence)),
                                transcript=(
                                    job_dir
                                    / "transcripts"
                                    / f"{task_id}__{attempt_key}.md"
                                ).as_posix(),
                            )
                            for attempt_key, evidence in sorted(tasks[task_id].items())
                        ),
                    )
                    for task_id in sorted(tasks)
                ),
            )
        )
    groups.sort(key=lambda group: (-len(group.tasks), group.group_id))
    return contracts.TriageIndex(
        job_name=job_dir.name,
        source=(job_dir / "tasks.jsonl").as_posix(),
        totals=contracts.Totals(
            trials=len(records),
            passed=sum(
                1
                for r in records
                if r.exception is None and r.verifier is not None and r.verifier.passed
            ),
            failed=sum(
                1
                for r in records
                if r.exception is None
                and r.verifier is not None
                and not r.verifier.passed
            ),
            errored=sum(1 for r in records if r.exception is not None),
        ),
        groups=tuple(groups),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path, help="runs/<job> dir with tasks.jsonl")
    parser.add_argument("--datasets-root", type=Path, default=DEFAULT_DATASETS_ROOT)
    parser.add_argument("--harness-root", type=Path, default=HARNESS_PACKAGE_ROOT)
    parser.add_argument("--tau2-root", type=Path, default=DEFAULT_TAU2_ROOT)
    parser.add_argument(
        "--check", action="store_true", help="validate only, write nothing"
    )
    args = parser.parse_args(argv)

    tasks_path = args.job_dir / "tasks.jsonl"
    records = [
        TrialRecord.model_validate_json(line)
        for line in tasks_path.read_text().splitlines()
    ]
    tool_types = load_tool_types(args.harness_root)
    item_products = load_item_products(args.tau2_root)
    unrecognized: list[str] = []
    for record in records:
        if record.exception is not None:
            triage_record(record, [], unrecognized)
            continue
        triage_record(
            record,
            load_gold_actions(args.datasets_root, record.task_id, tool_types),
            unrecognized,
            communicate_info=load_communicate_info(args.datasets_root, record.task_id),
            item_products=item_products,
        )
    index = build_index(args.job_dir, records)

    groups_path = contracts.triage_dir(args.job_dir) / contracts.GROUPS_FILENAME
    if not args.check:
        tasks_path.write_text(
            "".join(f"{record.model_dump_json()}\n" for record in records)
        )
        contracts.dump_index(index, groups_path)

    membership: dict[str, set[str]] = {}
    for group in index.groups:
        for task_id in group.task_ids:
            membership.setdefault(task_id, set()).add(group.group_id)
    for group in index.groups:
        # solo: this group is the task's only diagnosis, so fixing it alone
        # should flip the task — the primary fix-priority signal.
        n_solo = sum(
            1 for task_id in group.task_ids if membership[task_id] == {group.group_id}
        )
        print(
            f"{group.group_id}  kind={group.kind}  n_tasks={len(group.tasks)}  "
            f"n_solo={n_solo}  [{', '.join(group.task_ids)}]"
        )
    totals = index.totals
    grouped_tasks = {task_id for group in index.groups for task_id in group.task_ids}
    unexplained = sorted(
        record.task_id
        for record in records
        if record.exception is None
        and record.verifier is not None
        and not record.verifier.passed
        and record.task_id not in grouped_tasks
    )
    if unexplained:
        print(f"unexplained failures (no groups): [{', '.join(unexplained)}]")
    suffix = " (check only, nothing written)" if args.check else f" -> {groups_path}"
    print(
        f"{index.job_name}: {totals.trials} trials — {totals.passed} passed, "
        f"{totals.failed} failed, {totals.errored} errored; "
        f"{len(index.groups)} groups{suffix}"
    )
    if unrecognized:
        print()
        print("=" * 72)
        print(f"UNRECOGNIZED ERROR MESSAGES ({len(unrecognized)} occurrence(s))")
        print("=" * 72)
        for message in dict.fromkeys(unrecognized):
            print(f"  {message!r}")
        print("regenerate the key table: uv run python -m analysis.gen_error_keys")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
