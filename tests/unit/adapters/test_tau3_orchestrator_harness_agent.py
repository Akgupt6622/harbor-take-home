from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harbor.models.agent.context import AgentContext


TAU3_BENCH_ROOT = Path(__file__).resolve().parents[3] / "adapters" / "tau3-bench"
sys.path.insert(0, str(TAU3_BENCH_ROOT))

from tau3_orchestrator_harness_agent import (  # noqa: E402
    Tau3OrchestratorHarnessAgent,
)


def _agent(tmp_path: Path) -> Tau3OrchestratorHarnessAgent:
    harness_path = Path(__file__).resolve().parents[3] / "benchmarks/tau3-bench/harness"
    return Tau3OrchestratorHarnessAgent(
        logs_dir=tmp_path,
        harness_path=harness_path,
        model_name="gpt-5.5",
    )


def test_populate_context_aggregates_tokens_and_cost_by_role(tmp_path: Path) -> None:
    state = {
        "messages": [
            {
                "role": "assistant",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "cost": 0.03,
                "raw_data": {
                    "usage": {
                        "prompt_tokens_details": {"cached_tokens": 64},
                    }
                },
            },
            {
                "role": "user",
                "usage": {"prompt_tokens": 40, "completion_tokens": 10},
                "cost": 0.01,
                "raw_data": {
                    "usage": {
                        "prompt_tokens_details": {"cached_tokens": 0},
                    }
                },
            },
            {"role": "tool", "usage": None, "cost": 0.0, "raw_data": None},
        ]
    }
    (tmp_path / "tau3_runtime_state.json").write_text(json.dumps(state))
    context = AgentContext(metadata={"existing": True})

    _agent(tmp_path).populate_context_post_run(context)

    assert context.n_input_tokens == 140
    assert context.n_output_tokens == 30
    assert context.n_cache_tokens == 64
    assert context.cost_usd == pytest.approx(0.04)
    assert context.metadata == {
        "existing": True,
        "tau3_cost_usd_by_role": {
            "assistant": pytest.approx(0.03),
            "tool": 0.0,
            "user": pytest.approx(0.01),
        },
    }


@pytest.mark.parametrize(
    "contents",
    ["not json", json.dumps({}), json.dumps({"messages": "invalid"})],
)
def test_populate_context_ignores_unusable_runtime_state(
    tmp_path: Path,
    contents: str,
) -> None:
    (tmp_path / "tau3_runtime_state.json").write_text(contents)
    context = AgentContext()

    _agent(tmp_path).populate_context_post_run(context)

    assert context.is_empty()
