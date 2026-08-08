"""Triage validation pinned to tmp copies of the frozen smoke/pipecheck runs.

Run: uv run pytest analysis/tests -q
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from analysis import contracts
from analysis.models import FailureLabel, ToolCall, TrialRecord, Turn, VerifierResult
from analysis.parse_traces import HARNESS_PACKAGE_ROOT, load_tool_types
from analysis.triage import (
    GoldAction,
    load_gold_actions,
    main,
    match_gold,
    triage_record,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASETS_ROOT = REPO_ROOT / "datasets" / "tau3-bench"
WRONG_EXCHANGE = "wrong_args.exchange_delivered_order_items.new_item_ids"
WRONG_RETURN = "wrong_args.return_delivered_order_items.item_ids"


def run_triage(tmp_path: Path, job: str) -> Path:
    job_dir = tmp_path / job
    if not job_dir.exists():
        shutil.copytree(REPO_ROOT / "runs" / job, job_dir)
    main(
        [
            str(job_dir),
            "--datasets-root",
            str(DATASETS_ROOT),
            "--harness-root",
            str(REPO_ROOT / HARNESS_PACKAGE_ROOT),
        ]
    )
    return job_dir


def load_records(job_dir: Path) -> dict[str, TrialRecord]:
    lines = (job_dir / "tasks.jsonl").read_text().splitlines()
    return {r.task_id: r for r in map(TrialRecord.model_validate_json, lines)}


def make_call(
    turn_idx: int,
    name: str,
    args: dict[str, Any],
    *,
    is_error: bool = False,
    error_msg: str | None = None,
) -> ToolCall:
    return ToolCall(
        turn_idx=turn_idx,
        call_id=f"call_{turn_idx}",
        name=name,
        args=args,
        result=f"Error: {error_msg}" if is_error else "{}",
        is_error=is_error,
        error_msg=error_msg,
        is_write=True,
    )


def make_record(calls: list[ToolCall], *, passed: bool) -> TrialRecord:
    return TrialRecord(
        task_id="tau3-retail-0",
        job_name="synthetic",
        attempt_key="synth01",
        trial_dir="unused",
        agent_model="agent-model",
        user_model="user-model",
        seed=0,
        termination_reason="user_stop",
        step_count=1,
        verifier=VerifierResult(
            passed=passed, status="passed" if passed else "mismatch"
        ),
        turns=[
            Turn(
                idx=0,
                role="assistant",
                content="ok",
                tool_calls=calls,
                ts="2026-01-01T00:00:00",
            )
        ],
        cost_usd=0.0,
        tokens_in=0,
        tokens_out=0,
        n_cache_tokens=0,
        wall_time_s=0.0,
    )


def gold(name: str, arguments: dict[str, Any], action_id: str = "syn_0") -> GoldAction:
    return GoldAction(
        action_id=action_id, name=name, arguments=arguments, compare_fields=None
    )


def test_smoke_retail_91_exact_divergence(tmp_path: Path) -> None:
    job_dir = run_triage(tmp_path, "smoke")
    record = load_records(job_dir)["tau3-retail-91"]
    rule_labels = [label for label in record.labels if label.source == "rule"]
    assert [label.label for label in rule_labels] == [WRONG_EXCHANGE]
    (label,) = rule_labels
    assert label.evidence_turns == [22]
    assert label.confidence == 1.0
    assert "7609274509" in label.explanation
    assert "9494281769" in label.explanation
    assert record.fragile_pass is False


def test_smoke_retail_54_fragile_pass(tmp_path: Path) -> None:
    job_dir = run_triage(tmp_path, "smoke")
    record = load_records(job_dir)["tau3-retail-54"]
    assert record.fragile_pass is True
    assert {label.label for label in record.labels} == {"tool_error.find_user_id_by_email.user_not_found"}
    index = contracts.load_index(job_dir / "triage" / "groups.json")
    # Passed trials never seed groups: the record keeps its tool_error label and
    # the fragile_pass flag, but no group references the task.
    for group in index.groups:
        assert "tau3-retail-54" not in group.task_ids


def test_smoke_index_and_contract_roundtrip(tmp_path: Path) -> None:
    job_dir = run_triage(tmp_path, "smoke")
    index = contracts.load_index(job_dir / "triage" / "groups.json")
    assert [group.group_id for group in index.groups] == [WRONG_EXCHANGE]
    assert index.totals == contracts.Totals(trials=5, passed=4, failed=1, errored=0)
    assert index.group(WRONG_EXCHANGE).kind == "divergence"
    (ref,) = index.group(WRONG_EXCHANGE).tasks
    (attempt,) = ref.attempts
    assert attempt.attempt_key == "v29PA5V"
    assert attempt.evidence_turns == (22,)
    assert Path(attempt.transcript).exists()
    records = load_records(job_dir)
    assert records["tau3-retail-65"].labels == []
    assert records["tau3-retail-65"].fragile_pass is False


def test_pipecheck_retail_91_labels_and_groups(tmp_path: Path) -> None:
    job_dir = run_triage(tmp_path, "pipecheck")
    record = load_records(job_dir)["tau3-retail-91"]
    assert {label.label for label in record.labels if label.source == "rule"} == {
        WRONG_RETURN,
        WRONG_EXCHANGE,
    }
    index = contracts.load_index(job_dir / "triage" / "groups.json")
    assert [group.group_id for group in index.groups] == [
        WRONG_EXCHANGE,
        WRONG_RETURN,
    ]
    assert index.totals == contracts.Totals(trials=2, passed=1, failed=1, errored=0)


def test_triage_is_idempotent(tmp_path: Path) -> None:
    for job in ("smoke", "pipecheck"):
        job_dir = run_triage(tmp_path, job)
        first_tasks = (job_dir / "tasks.jsonl").read_bytes()
        first_groups = (job_dir / "triage" / "groups.json").read_bytes()
        run_triage(tmp_path, job)
        assert (job_dir / "tasks.jsonl").read_bytes() == first_tasks
        assert (job_dir / "triage" / "groups.json").read_bytes() == first_groups


def test_non_rule_labels_survive_reruns(tmp_path: Path) -> None:
    job_dir = run_triage(tmp_path, "smoke")
    records = [
        TrialRecord.model_validate_json(line)
        for line in (job_dir / "tasks.jsonl").read_text().splitlines()
    ]
    for record in records:
        if record.task_id == "tau3-retail-91":
            record.labels.append(
                FailureLabel(
                    label="llm_hunch",
                    source="llm",
                    evidence_turns=[],
                    explanation="synthetic",
                    confidence=0.5,
                )
            )
    (job_dir / "tasks.jsonl").write_text(
        "".join(f"{record.model_dump_json()}\n" for record in records)
    )
    run_triage(tmp_path, "smoke")
    record = load_records(job_dir)["tau3-retail-91"]
    assert [label.label for label in record.labels] == ["llm_hunch", WRONG_EXCHANGE]


def test_matcher_anchors_on_order_id_across_two_same_tool_calls() -> None:
    gold_actions = [
        gold(
            "cancel_pending_order",
            {"order_id": "#A", "reason": "no longer needed"},
            "s_0",
        ),
        gold(
            "cancel_pending_order",
            {"order_id": "#B", "reason": "ordered by mistake"},
            "s_1",
        ),
    ]
    calls = [
        make_call(
            3,
            "cancel_pending_order",
            {"order_id": "#A", "reason": "ordered by mistake"},
        ),
        make_call(
            5,
            "cancel_pending_order",
            {"order_id": "#B", "reason": "ordered by mistake"},
        ),
    ]
    findings = match_gold(gold_actions, calls, [])
    assert [(f.label, f.evidence_turns, f.confidence) for f in findings] == [
        ("wrong_args.cancel_pending_order.reason", (3,), 1.0)
    ]


def test_matcher_unanchored_pairing_gets_lower_confidence() -> None:
    gold_actions = [
        gold(
            "modify_pending_order_items",
            {"item_ids": ["1"], "new_item_ids": ["2"], "payment_method_id": "pm"},
        )
    ]
    calls = [
        make_call(
            4,
            "modify_pending_order_items",
            {"item_ids": ["1"], "new_item_ids": ["3"], "payment_method_id": "pm"},
        )
    ]
    findings = match_gold(gold_actions, calls, [])
    assert [(f.label, f.confidence) for f in findings] == [
        ("wrong_args.modify_pending_order_items.new_item_ids", 0.8)
    ]


def test_matcher_upgrades_missing_to_attempted_but_rejected() -> None:
    arguments = {
        "order_id": "#A",
        "item_ids": ["1"],
        "new_item_ids": ["2"],
        "payment_method_id": "pm",
    }
    calls = [
        make_call(
            7,
            "exchange_delivered_order_items",
            arguments,
            is_error=True,
            error_msg="Non-delivered order cannot be exchanged",
        )
    ]
    findings = match_gold(
        [gold("exchange_delivered_order_items", arguments)], calls, []
    )
    assert [(f.label, f.evidence_turns) for f in findings] == [
        (
            "attempted_but_rejected.exchange_delivered_order_items."
            "non_delivered_order_cannot_be_exchanged",
            (7,),
        )
    ]


def test_matcher_compares_lists_as_multisets() -> None:
    gold_actions = [
        gold(
            "return_delivered_order_items",
            {"order_id": "#A", "item_ids": ["1", "2", "3"], "payment_method_id": "pm"},
        )
    ]
    calls = [
        make_call(
            9,
            "return_delivered_order_items",
            {"order_id": "#A", "item_ids": ["3", "1", "2"], "payment_method_id": "pm"},
        )
    ]
    assert match_gold(gold_actions, calls, []) == []


def test_matcher_flags_extra_write() -> None:
    calls = [
        make_call(
            6, "cancel_pending_order", {"order_id": "#A", "reason": "no longer needed"}
        )
    ]
    findings = match_gold([], calls, [])
    assert [(f.label, f.evidence_turns) for f in findings] == [
        ("extra_write.cancel_pending_order", (6,))
    ]


def test_matcher_honors_compare_args_restriction() -> None:
    action = GoldAction(
        action_id="s_0",
        name="cancel_pending_order",
        arguments={"order_id": "#A", "reason": "no longer needed"},
        compare_fields=("order_id",),
    )
    calls = [
        make_call(2, "cancel_pending_order", {"order_id": "#A", "reason": "different"})
    ]
    assert match_gold([action], calls, []) == []


def test_unrecognized_error_message_collected_not_crashed() -> None:
    call = make_call(
        0,
        "cancel_pending_order",
        {"order_id": "#A", "reason": "no longer needed"},
        is_error=True,
        error_msg="Flux capacitor misaligned",
    )
    record = make_record([call], passed=False)
    unrecognized: list[str] = []
    triage_record(record, [], unrecognized)
    assert unrecognized == ["Flux capacitor misaligned"]
    by_label = {label.label: label for label in record.labels}
    assert set(by_label) == {"tool_error.cancel_pending_order.unrecognized"}
    assert (
        "Flux capacitor misaligned" in by_label["tool_error.cancel_pending_order.unrecognized"].explanation
    )


def test_matching_writes_on_failed_trial_yield_no_labels() -> None:
    arguments = {"order_id": "#A", "reason": "no longer needed"}
    record = make_record(
        [make_call(0, "cancel_pending_order", arguments)], passed=False
    )
    triage_record(record, [gold("cancel_pending_order", arguments)], [])
    # Unexplained failures carry no labels; triage surfaces them in its summary
    # line rather than as a group.
    assert record.labels == []


def test_unrecognized_errors_exit_nonzero_after_writing(tmp_path: Path) -> None:
    job_dir = tmp_path / "smoke"
    shutil.copytree(REPO_ROOT / "runs" / "smoke", job_dir)
    tasks_path = job_dir / "tasks.jsonl"
    tasks_path.write_text(
        tasks_path.read_text().replace("User not found", "Gremlins ate the database")
    )
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                str(job_dir),
                "--datasets-root",
                str(DATASETS_ROOT),
                "--harness-root",
                str(REPO_ROOT / HARNESS_PACKAGE_ROOT),
            ]
        )
    assert excinfo.value.code == 1
    assert (job_dir / "triage" / "groups.json").exists()
    record = load_records(job_dir)["tau3-retail-54"]
    unrecognized = [
        label for label in record.labels if label.label == "tool_error.find_user_id_by_email.unrecognized"
    ]
    assert len(unrecognized) == 1
    assert "Gremlins ate the database" in unrecognized[0].explanation


def test_gold_loader_is_loud_on_bad_inputs(tmp_path: Path) -> None:
    tool_types = load_tool_types(REPO_ROOT / HARNESS_PACKAGE_ROOT)
    with pytest.raises(FileNotFoundError, match="tau3-retail-999"):
        load_gold_actions(tmp_path, "tau3-retail-999", tool_types)
    config_dir = tmp_path / "tau3-retail-999" / "tests"
    config_dir.mkdir(parents=True)
    action = {
        "action_id": "999_0",
        "name": "cancel_pending_order",
        "arguments": {"order_id": "#W1", "reason": "no longer needed"},
        "requestor": "assistant",
        "compare_args": {"bad": "shape"},
    }
    (config_dir / "config.json").write_text(json.dumps({"expected_actions": [action]}))
    with pytest.raises(ValueError, match="tau3-retail-999"):
        load_gold_actions(tmp_path, "tau3-retail-999", tool_types)
    action["compare_args"] = None
    action["requestor"] = "user"
    (config_dir / "config.json").write_text(json.dumps({"expected_actions": [action]}))
    with pytest.raises(ValueError, match="requestor"):
        load_gold_actions(tmp_path, "tau3-retail-999", tool_types)
    action["requestor"] = "assistant"
    (config_dir / "config.json").write_text(json.dumps({"expected_actions": [action]}))
    (loaded,) = load_gold_actions(tmp_path, "tau3-retail-999", tool_types)
    assert loaded.name == "cancel_pending_order"
    assert loaded.compare_fields is None


def test_gold_loader_keeps_only_write_actions() -> None:
    tool_types = load_tool_types(REPO_ROOT / HARNESS_PACKAGE_ROOT)
    actions = load_gold_actions(DATASETS_ROOT, "tau3-retail-54", tool_types)
    assert [action.name for action in actions] == [
        "cancel_pending_order",
        "cancel_pending_order",
        "return_delivered_order_items",
    ]
