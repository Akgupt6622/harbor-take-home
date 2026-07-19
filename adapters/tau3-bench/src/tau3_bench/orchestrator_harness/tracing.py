from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from google.protobuf.json_format import MessageToJson
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

AGENT_NAME = "agent.name"
INPUT_VALUE = "input.value"
OPENINFERENCE_SPAN_KIND = "openinference.span.kind"
OUTPUT_VALUE = "output.value"
TOOL_ID = "tool.id"
TOOL_NAME = "tool.name"


def _safe_json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=True)


def _span_attrs(attributes: dict[str, Any]) -> dict[str, str | bool | int | float]:
    return {
        key: value if isinstance(value, (str, bool, int, float)) else _safe_json(value)
        for key, value in attributes.items()
        if value is not None
    }


class _OtlpJsonFileSpanExporter(SpanExporter):
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")

    def export(self, spans: Any) -> SpanExportResult:
        request = encode_spans(spans)
        self._file.write(
            MessageToJson(
                request,
                preserving_proto_field_name=True,
                indent=None,
                ensure_ascii=True,
            )
            + "\n"
        )
        self._file.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._file.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self._file.flush()
        return True


@contextmanager
def configured_tracer(
    trace_path: Path, *, service_name: str, instrument_litellm: bool = False
) -> Iterator[Any]:
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "tau3-bench",
            }
        )
    )
    exporter = _OtlpJsonFileSpanExporter(trace_path)
    try:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        if instrument_litellm:
            from openinference.instrumentation.litellm import LiteLLMInstrumentor

            LiteLLMInstrumentor().instrument(tracer_provider=provider)
        yield provider.get_tracer("tau3_bench.orchestrator_harness")
    finally:
        provider.shutdown()


@contextmanager
def instrument_tool_calls(*, tracer: Any, environment: Any, domain: str, task: Any):
    original_get_response = environment.get_response

    def traced_get_response(tool_call: Any) -> Any:
        with tracer.start_as_current_span(
            f"tau3.tool.{tool_call.name}",
            attributes=_span_attrs(
                {
                    OPENINFERENCE_SPAN_KIND: "TOOL",
                    TOOL_NAME: tool_call.name,
                    TOOL_ID: tool_call.id,
                    INPUT_VALUE: _safe_json(tool_call.arguments),
                    "tau3.domain": domain,
                    "tau3.task_id": str(task.id),
                    "tau3.tool.requestor": tool_call.requestor,
                }
            ),
        ) as span:
            try:
                tool_result = original_get_response(tool_call)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(type(exc).__name__)))
                raise

            span.set_attribute(OUTPUT_VALUE, str(tool_result.content))
            span.set_attribute("tau3.tool.error", bool(tool_result.error))
            if tool_result.error:
                span.set_status(Status(StatusCode.ERROR, "tool_error"))
            else:
                span.set_status(Status(StatusCode.OK))
            return tool_result

    environment.get_response = traced_get_response
    try:
        yield
    finally:
        environment.get_response = original_get_response


@contextmanager
def trace_orchestrator_run(
    *,
    tracer: Any,
    domain: str,
    task: Any,
    model: str,
    seed: int | None,
    max_steps: int,
    max_errors: int,
) -> Iterator[Any]:
    with tracer.start_as_current_span(
        "tau3.orchestrator.run",
        attributes=_span_attrs(
            {
                OPENINFERENCE_SPAN_KIND: "AGENT",
                AGENT_NAME: "tau3-orchestrator-harness",
                "tau3.domain": domain,
                "tau3.task_id": str(task.id),
                "tau3.model": model,
                "tau3.seed": seed,
                "tau3.max_steps": max_steps,
                "tau3.max_errors": max_errors,
            }
        ),
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(type(exc).__name__)))
            raise
        else:
            span.set_status(Status(StatusCode.OK))


def write_evaluation_trace(
    *,
    trace_path: Path,
    result: dict[str, Any],
    config: dict[str, Any],
    runtime_state: dict[str, Any] | None,
) -> None:
    with configured_tracer(
        trace_path,
        service_name="tau3-bench-verifier",
    ) as tracer:
        trace_attributes = evaluation_trace_attributes(
            result=result,
            config=config,
            runtime_state=runtime_state,
        )

        with tracer.start_as_current_span(
            "tau3.evaluation.result",
            attributes=_span_attrs(
                {
                    OPENINFERENCE_SPAN_KIND: "EVALUATOR",
                    **trace_attributes,
                }
            ),
        ) as span:
            span.add_event(
                "harbor.evaluation.result",
                {
                    "evaluation.success": trace_attributes["evaluation.success"],
                    "evaluation.status": trace_attributes["evaluation.status"],
                    "benchmark": "tau3-bench",
                },
            )
            span.set_status(Status(StatusCode.OK))


def evaluation_trace_attributes(
    *,
    result: dict[str, Any],
    config: dict[str, Any],
    runtime_state: dict[str, Any] | None,
) -> dict[str, Any]:
    task = config.get("task") or {}
    reward = float(result.get("reward", 0.0))
    status = str(result.get("status") or "unknown")
    attrs: dict[str, Any] = {
        "benchmark": "tau3-bench",
        "tau3.domain": config.get("domain"),
        "tau3.task_id": task.get("id") or config.get("source_task_id"),
        "evaluation.success": reward == 1.0 and status == "passed",
        "evaluation.status": status,
        "evaluation.used_tau2_evaluator": result.get("used_tau2_evaluator"),
    }
    if isinstance(runtime_state, dict):
        attrs.update(
            {
                "tau3.runtime_termination_reason": runtime_state.get(
                    "termination_reason"
                ),
                "tau3.step_count": runtime_state.get("step_count"),
                "tau3.num_errors": runtime_state.get("num_errors"),
                "tau3.trajectory_message_count": len(
                    runtime_state.get("messages") or []
                ),
            }
        )
    return attrs
