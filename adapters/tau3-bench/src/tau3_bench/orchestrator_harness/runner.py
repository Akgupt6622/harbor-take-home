from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .harness_overlay import (
    build_harness_environment,
    import_attr,
    load_harness_policy,
)
from .tracing import (
    configured_tracer,
    instrument_tool_calls,
    trace_orchestrator_run,
)

DEFAULT_USER_MODEL = "gpt-5.5"


def _llm_args(
    *,
    seed: int | None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if temperature is not None:
        args["temperature"] = temperature
    if reasoning_effort:
        args["reasoning_effort"] = reasoning_effort
    if seed is not None:
        args["seed"] = seed
    if os.getenv("OPENAI_API_KEY"):
        args["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.getenv("OPENAI_BASE_URL"):
        args["api_base"] = os.environ["OPENAI_BASE_URL"]
    return args


def _user_llm_args(seed: int | None) -> dict[str, Any]:
    temperature = os.getenv("TAU2_USER_TEMPERATURE")
    return _llm_args(
        seed=seed,
        temperature=float(temperature) if temperature else None,
        reasoning_effort=os.getenv("TAU2_USER_REASONING_EFFORT", "low"),
    )


def _termination_value(simulation: Any) -> str:
    termination_reason = simulation.termination_reason
    return getattr(termination_reason, "value", termination_reason)


def _write_state(
    *,
    path: Path,
    domain: str,
    task: Any,
    orchestrator: Any,
    simulation: Any,
    model: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "domain": domain,
                "task_id": task.id,
                "termination_reason": _termination_value(simulation),
                "step_count": orchestrator.step_count,
                "num_errors": orchestrator.num_errors,
                "max_steps": orchestrator.max_steps,
                "max_errors": orchestrator.max_errors,
                "seed": orchestrator.seed,
                "messages": [
                    message.model_dump(mode="json") for message in simulation.messages
                ],
                "agent": "tau3-orchestrator-harness",
                "agent_model": model,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def run(args: argparse.Namespace) -> None:
    config = json.loads(args.task_config.read_text(encoding="utf-8"))
    with configured_tracer(
        args.trace_path,
        service_name="tau3-bench-orchestrator-harness",
        instrument_litellm=True,
    ) as tracer:
        domain, task, environment, _env_kwargs = build_harness_environment(
            config, args.harness_dir
        )
        domain_policy = load_harness_policy(domain, args.harness_dir)

        LLMAgent = import_attr("harness.llm_agent", "LLMAgent")
        UserSimulator = import_attr("tau2.user.user_simulator", "UserSimulator")
        Orchestrator = import_attr("tau2.orchestrator.orchestrator", "Orchestrator")

        user_tools = (
            environment.get_user_tools(include=task.user_tools)
            if environment.user_tools is not None
            else None
        )
        agent = LLMAgent(
            tools=environment.get_tools(),
            domain_policy=domain_policy,
            llm=args.model,
            llm_args=_llm_args(
                seed=args.seed,
                temperature=args.temperature,
                reasoning_effort=args.reasoning_effort,
            ),
        )
        user = UserSimulator(
            llm=os.getenv("TAU2_USER_MODEL", DEFAULT_USER_MODEL),
            instructions=str(task.user_scenario),
            tools=user_tools,
            llm_args=_user_llm_args(args.seed),
        )
        orchestrator = Orchestrator(
            domain=domain,
            agent=agent,
            user=user,
            environment=environment,
            task=task,
            max_steps=args.max_steps,
            max_errors=args.max_errors,
            seed=args.seed,
            simulation_id=f"harbor-{domain}-{task.id}",
            solo_mode=False,
        )
        with trace_orchestrator_run(
            tracer=tracer,
            domain=domain,
            task=task,
            model=args.model,
            seed=args.seed,
            max_steps=args.max_steps,
            max_errors=args.max_errors,
        ) as span:
            with instrument_tool_calls(
                tracer=tracer,
                environment=environment,
                domain=domain,
                task=task,
            ):
                simulation = orchestrator.run()
            span.set_attribute(
                "tau3.termination_reason", _termination_value(simulation)
            )
            span.set_attribute("tau3.step_count", orchestrator.step_count)
            span.set_attribute("tau3.num_errors", orchestrator.num_errors)
            span.set_attribute(
                "tau3.trajectory_message_count", len(simulation.messages)
            )
    _write_state(
        path=args.state_path,
        domain=domain,
        task=task,
        orchestrator=orchestrator,
        simulation=simulation,
        model=args.model,
    )


def _seed(value: str) -> int | None:
    return None if value == "none" else int(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--harness-dir", type=Path, required=True)
    parser.add_argument("--task-config", type=Path, required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--trace-path", type=Path, required=True)
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--max-errors", type=int, default=10)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=_seed, default="300")
    parser.add_argument("--reasoning-effort")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
