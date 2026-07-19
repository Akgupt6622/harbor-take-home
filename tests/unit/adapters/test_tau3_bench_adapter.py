from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from google.protobuf.json_format import Parse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


TAU3_BENCH_SRC = Path(__file__).resolve().parents[3] / "adapters" / "tau3-bench" / "src"
TAU3_BENCH_TEMPLATE_TESTS = TAU3_BENCH_SRC / "tau3_bench" / "task-template" / "tests"
sys.path.insert(0, str(TAU3_BENCH_SRC))

from tau3_bench.adapter import Tau3BenchAdapter, TauTask  # noqa: E402
from tau3_bench.orchestrator_harness import (  # noqa: E402
    harness_overlay,
    tracing,
)
from tau3_bench.orchestrator_harness.harness_overlay import (  # noqa: E402
    DOMAIN_MODEL_ALIASES,
)
from tau3_bench.orchestrator_harness.tracing import (  # noqa: E402
    _OtlpJsonFileSpanExporter,
    evaluation_trace_attributes,
    instrument_tool_calls,
)


def _read_spans(trace_path: Path):
    spans = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        request = Parse(line, ExportTraceServiceRequest())
        spans.extend(request.resource_spans[0].scope_spans[0].spans)
    return spans


def _span_attributes(span) -> dict[str, object]:
    attributes = {}
    for attr in span.attributes:
        value = attr.value
        value_type = value.WhichOneof("value")
        attributes[attr.key] = getattr(value, value_type)
    return attributes


def _load_template_evaluator() -> ModuleType:
    package_name = "tau3_bench_template_tests"
    package_spec = importlib.util.spec_from_file_location(
        package_name,
        TAU3_BENCH_TEMPLATE_TESTS / "__init__.py",
        submodule_search_locations=[str(TAU3_BENCH_TEMPLATE_TESTS)],
    )

    assert package_spec is not None
    assert package_spec.loader is not None

    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)
    sys.modules[f"{package_name}.harness_overlay"] = harness_overlay
    sys.modules[f"{package_name}.tracing"] = tracing

    module_name = f"{package_name}.evaluate"
    spec = importlib.util.spec_from_file_location(
        module_name,
        TAU3_BENCH_TEMPLATE_TESTS / "evaluate.py",
    )

    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    return module


def test_harness_domain_models_use_stock_runtime_classes() -> None:
    assert DOMAIN_MODEL_ALIASES["harness.retail.data_model"] == (
        "tau2.domains.retail.data_model"
    )


def test_banking_discoverable_user_tool_actions_are_replayable() -> None:
    task = TauTask(
        domain="banking_knowledge",
        source_id="task_discoverable",
        local_task_id="tau3-banking_knowledge-task-discoverable",
        policy="",
        task={
            "id": "task_discoverable",
            "user_scenario": {"instructions": "Use a discoverable user tool."},
            "evaluation_criteria": {
                "actions": [
                    {
                        "action_id": "0",
                        "requestor": "assistant",
                        "name": "give_discoverable_user_tool",
                        "arguments": {
                            "discoverable_tool_name": "submit_cash_back_dispute_0589",
                        },
                    },
                    {
                        "action_id": "1",
                        "requestor": "user",
                        "name": "call_discoverable_user_tool",
                        "arguments": {
                            "discoverable_tool_name": "submit_cash_back_dispute_0589",
                            "arguments": (
                                '{"user_id": "user_123", "transaction_id": "txn_456"}'
                            ),
                        },
                    },
                ],
                "reward_basis": ["DB"],
            },
        },
    )

    config = Tau3BenchAdapter._build_test_config(  # noqa: SLF001
        object.__new__(Tau3BenchAdapter),
        task,
    )

    expected_give = config["expected_actions"][0]
    embedded_give = config["task"]["evaluation_criteria"]["actions"][0]
    expected_user_call = config["expected_actions"][1]
    embedded_user_call = config["task"]["evaluation_criteria"]["actions"][1]

    assert expected_give == embedded_give
    assert expected_user_call == embedded_user_call
    assert expected_give["compare_args"] == ["discoverable_tool_name"]
    assert expected_give["arguments"] == {
        "discoverable_tool_name": "submit_cash_back_dispute_0589",
        "arguments": '{"user_id": "user_123", "transaction_id": "txn_456"}',
    }
    assert expected_user_call["arguments"] == {
        "discoverable_tool_name": "submit_cash_back_dispute_0589",
        "arguments": '{"user_id": "user_123", "transaction_id": "txn_456"}',
    }


def test_assistant_action_only_reward_basis_is_removed() -> None:
    adapter = object.__new__(Tau3BenchAdapter)
    task = TauTask(
        domain="telecom",
        source_id="task_env_action",
        local_task_id="tau3-telecom-task-env-action",
        policy="",
        task={
            "id": "task_env_action",
            "user_scenario": {"instructions": "Fix my internet."},
            "evaluation_criteria": {
                "actions": [
                    {
                        "action_id": "0",
                        "requestor": "assistant",
                        "name": "update_ticket",
                        "arguments": {"ticket_id": "ticket_1"},
                    }
                ],
                "env_assertions": [
                    {
                        "env_type": "assistant",
                        "func_name": "assert_ticket_status",
                        "arguments": {"ticket_id": "ticket_1"},
                        "assert_value": True,
                    }
                ],
                "reward_basis": ["ENV_ASSERTION", "ACTION"],
            },
        },
    )

    config = Tau3BenchAdapter._build_test_config(adapter, task)  # noqa: SLF001

    assert config["reward_basis"] == ["ENV_ASSERTION"]
    assert config["task"]["evaluation_criteria"]["reward_basis"] == ["ENV_ASSERTION"]
    assert Tau3BenchAdapter._has_grading_signal(adapter, task)  # noqa: SLF001


def test_assistant_action_only_tasks_without_other_basis_are_excluded() -> None:
    adapter = object.__new__(Tau3BenchAdapter)
    task = TauTask(
        domain="banking_knowledge",
        source_id="task_action_only",
        local_task_id="tau3-banking_knowledge-task-action-only",
        policy="",
        task={
            "id": "task_action_only",
            "user_scenario": {"instructions": "Read my account details."},
            "evaluation_criteria": {
                "actions": [
                    {
                        "action_id": "0",
                        "requestor": "assistant",
                        "name": "read_customer",
                        "arguments": {"user_id": "user_123"},
                    }
                ],
                "reward_basis": ["ACTION"],
            },
        },
    )

    config = Tau3BenchAdapter._build_test_config(adapter, task)  # noqa: SLF001

    assert config["reward_basis"] == []
    assert not Tau3BenchAdapter._has_grading_signal(adapter, task)  # noqa: SLF001


def test_user_action_reward_basis_is_kept() -> None:
    adapter = object.__new__(Tau3BenchAdapter)
    task = TauTask(
        domain="banking_knowledge",
        source_id="task_user_action",
        local_task_id="tau3-banking_knowledge-task-user-action",
        policy="",
        task={
            "id": "task_user_action",
            "user_scenario": {"instructions": "Call a user tool."},
            "evaluation_criteria": {
                "actions": [
                    {
                        "action_id": "0",
                        "requestor": "assistant",
                        "name": "give_discoverable_user_tool",
                        "arguments": {
                            "discoverable_tool_name": "get_referral_link",
                        },
                    },
                    {
                        "action_id": "1",
                        "requestor": "user",
                        "name": "call_discoverable_user_tool",
                        "arguments": {
                            "discoverable_tool_name": "get_referral_link",
                            "arguments": (
                                '{"user_id": "user_123", "card_name": "Gold Rewards"}'
                            ),
                        },
                    },
                ],
                "reward_basis": ["ACTION"],
            },
        },
    )

    config = Tau3BenchAdapter._build_test_config(adapter, task)  # noqa: SLF001

    assert config["reward_basis"] == ["ACTION"]
    assert config["task"]["evaluation_criteria"]["reward_basis"] == ["ACTION"]
    assert Tau3BenchAdapter._has_grading_signal(adapter, task)  # noqa: SLF001


def test_empty_reward_basis_is_not_replaced_with_default() -> None:
    adapter = object.__new__(Tau3BenchAdapter)
    task = TauTask(
        domain="airline",
        source_id="task_empty_basis",
        local_task_id="tau3-airline-task-empty-basis",
        policy="",
        task={
            "id": "task_empty_basis",
            "user_scenario": {"instructions": "No reward basis."},
            "evaluation_criteria": {
                "actions": [],
                "reward_basis": [],
            },
        },
    )

    config = Tau3BenchAdapter._build_test_config(adapter, task)  # noqa: SLF001

    assert config["reward_basis"] == []
    assert not Tau3BenchAdapter._has_grading_signal(adapter, task)  # noqa: SLF001


def test_template_verifier_uses_upstream_nl_aware_evaluation_mode() -> None:
    evaluator = _load_template_evaluator()
    expected = object()

    class EvaluationType:
        ALL_WITH_NL_ASSERTIONS = expected

    assert evaluator._select_evaluation_type(EvaluationType) is expected  # noqa: SLF001


def test_template_verifier_propagates_evaluation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = _load_template_evaluator()

    def fail_evaluation(**_kwargs: Any) -> None:
        raise RuntimeError("broken evaluator")

    monkeypatch.setattr(evaluator, "_evaluate_from_state", fail_evaluation)

    with pytest.raises(RuntimeError, match="broken evaluator"):
        evaluator._run_evaluation(  # noqa: SLF001
            config={},
            runtime_state={},
            skip_nl_assertion=False,
        )


def test_evaluation_trace_attributes_do_not_leak_grading_details() -> None:
    attrs = evaluation_trace_attributes(
        result={
            "status": "mismatch",
            "reward": 0.0,
            "used_tau2_evaluator": True,
            "reward_info": {
                "reward_basis": ["DB"],
                "reward_breakdown": {"DB": 0.0},
                "db_check": {"db_match": False},
                "action_checks": [{"name": "secret_expected_action"}],
            },
        },
        config={
            "domain": "retail",
            "source_task_id": "0",
            "expected_actions": [{"name": "secret_expected_action"}],
            "task": {
                "id": "0",
                "evaluation_criteria": {"reward_basis": ["DB"]},
            },
        },
        runtime_state={
            "termination_reason": "user_stop",
            "step_count": 4,
            "num_errors": 0,
            "messages": [{"role": "user", "content": "private user text"}],
        },
    )

    assert attrs == {
        "benchmark": "tau3-bench",
        "tau3.domain": "retail",
        "tau3.task_id": "0",
        "evaluation.success": False,
        "evaluation.status": "mismatch",
        "evaluation.used_tau2_evaluator": True,
        "tau3.runtime_termination_reason": "user_stop",
        "tau3.step_count": 4,
        "tau3.num_errors": 0,
        "tau3.trajectory_message_count": 1,
    }


def test_trace_exporter_writes_otlp_json_lines(tmp_path: Path) -> None:
    trace_path = tmp_path / "spans.otlp.jsonl"
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(_OtlpJsonFileSpanExporter(trace_path))
    )
    tracer = provider.get_tracer("tau3-test")

    span = tracer.start_span("tau3.test.span")
    span.set_attribute("tau3.test", True)
    span.end()
    provider.shutdown()

    lines = trace_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    request = Parse(lines[0], ExportTraceServiceRequest())
    spans = request.resource_spans[0].scope_spans[0].spans
    assert spans[0].name == "tau3.test.span"


def test_tool_call_instrumentation_records_execution_span(tmp_path: Path) -> None:
    trace_path = tmp_path / "spans.otlp.jsonl"
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(_OtlpJsonFileSpanExporter(trace_path))
    )
    tracer = provider.get_tracer("tau3-test")

    class Task:
        id = "task-1"

    class ToolCall:
        id = "call-1"
        name = "lookup_order"
        requestor = "assistant"
        arguments = {"order_id": "order-1"}

    class ToolResult:
        content = "found"
        error = False

    class Environment:
        def __init__(self) -> None:
            self.calls = 0

        def get_response(self, tool_call: ToolCall) -> ToolResult:
            assert tool_call.name == "lookup_order"
            self.calls += 1
            return ToolResult()

    environment = Environment()
    original_get_response = environment.get_response

    with instrument_tool_calls(
        tracer=tracer,
        environment=environment,
        domain="retail",
        task=Task(),
    ):
        tool_result = environment.get_response(ToolCall())

    provider.shutdown()

    assert tool_result.content == "found"
    assert environment.calls == 1
    assert environment.get_response == original_get_response

    spans = _read_spans(trace_path)
    assert len(spans) == 1
    assert spans[0].name == "tau3.tool.lookup_order"
    attrs = _span_attributes(spans[0])
    assert attrs["openinference.span.kind"] == "TOOL"
    assert attrs["tool.name"] == "lookup_order"
    assert attrs["output.value"] == "found"
    assert attrs["tau3.tool.error"] is False
    assert spans[0].end_time_unix_nano > spans[0].start_time_unix_nano


def test_generated_tests_include_tracing_helper(tmp_path: Path) -> None:
    adapter = object.__new__(Tau3BenchAdapter)
    adapter.output_dir = tmp_path
    adapter.overwrite = True
    adapter._TEMPLATE_DIR = Tau3BenchAdapter._TEMPLATE_DIR  # noqa: SLF001
    adapter._HARNESS_OVERLAY_HELPER = (  # noqa: SLF001
        Tau3BenchAdapter._HARNESS_OVERLAY_HELPER
    )
    adapter._TRACING_HELPER = Tau3BenchAdapter._TRACING_HELPER  # noqa: SLF001

    task = TauTask(
        domain="airline",
        source_id="0",
        local_task_id="tau3-airline-0",
        policy="policy",
        task={
            "id": "0",
            "user_scenario": {"instructions": "Book a flight."},
            "evaluation_criteria": {"actions": [], "reward_basis": ["DB"]},
        },
    )

    test_config = Tau3BenchAdapter._build_test_config(adapter, task)  # noqa: SLF001
    task_dir = tmp_path / "tau3-airline-0"
    task_dir.mkdir()
    Tau3BenchAdapter._copy_template(adapter, task_dir)  # noqa: SLF001
    Tau3BenchAdapter._write_test_assets(adapter, task_dir, test_config)  # noqa: SLF001

    assert (task_dir / "tests" / "tracing.py").is_file()
