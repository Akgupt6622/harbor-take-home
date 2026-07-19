from __future__ import annotations

import json
import os
import random
import shlex
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

RUNNER_MODULE = "tau3_bench.orchestrator_harness.runner"
REMOTE_TASK_CONFIG_PATH = "/installed-agent/task_config.json"
DEFAULT_MODEL = "gpt-5.4-mini"
RUNTIME_STATE_FILENAME = "tau3_runtime_state.json"


class Tau3OrchestratorHarnessAgent(BaseInstalledAgent):
    _REMOTE_AGENT_DIR = PurePosixPath("/installed-agent")
    _REMOTE_HARNESS_DIR = _REMOTE_AGENT_DIR / "harness"
    _REMOTE_PACKAGE_ROOT = _REMOTE_AGENT_DIR / "tau3_bench"
    _REMOTE_PACKAGE_DIR = _REMOTE_AGENT_DIR / "tau3_bench" / "orchestrator_harness"
    _LOG_HARNESS_DIR = EnvironmentPaths.agent_dir / "harness"

    def __init__(
        self,
        harness_path: str | Path = "benchmarks/tau3-bench/harness",
        *args: Any,
        temperature: float = 0.0,
        max_steps: int = 200,
        max_errors: int = 10,
        seed: int | None = 300,
        tau2_trial_index: int = 0,
        reasoning_effort: str | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("model_name", DEFAULT_MODEL)
        super().__init__(*args, **kwargs)
        self.harness_path = Path(harness_path).expanduser().resolve()
        self.temperature = temperature
        self.max_steps = max_steps
        self.max_errors = max_errors
        self.seed = seed
        self.tau2_trial_index = tau2_trial_index
        self.reasoning_effort = reasoning_effort

        if not (self.harness_path / "llm_agent.py").is_file():
            raise FileNotFoundError(f"Invalid harness path: {self.harness_path}")

    @staticmethod
    def name() -> str:
        return "tau3-orchestrator-harness"

    def version(self) -> str | None:
        return self._version or "1.0"

    async def install(self, environment: BaseEnvironment) -> None:
        remote_harness = self._REMOTE_HARNESS_DIR.as_posix()
        remote_package = self._REMOTE_PACKAGE_DIR.as_posix()
        await self.exec_as_root(
            environment,
            command=(
                f"rm -rf {shlex.quote(remote_harness)} "
                f"{shlex.quote(remote_package)} && "
                f"mkdir -p {shlex.quote(remote_harness)} "
                f"{shlex.quote(remote_package)}"
            ),
        )
        await environment.upload_dir(self._staged_harness_dir(), remote_harness)
        await environment.upload_file(
            self._runner_package_root() / "__init__.py",
            (self._REMOTE_PACKAGE_ROOT / "__init__.py").as_posix(),
        )
        await environment.upload_dir(self._runner_package_dir(), remote_package)
        user = str(environment.default_user or "root")
        await self.exec_as_root(
            environment,
            command=f"chown -R {shlex.quote(user)}:{shlex.quote(user)} /installed-agent",
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        await environment.upload_file(self._task_config_path(), REMOTE_TASK_CONFIG_PATH)
        await self.exec_as_agent(
            environment,
            command=self._run_command(),
            env=self._runner_env(),
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        state_path = self.logs_dir / RUNTIME_STATE_FILENAME
        if not state_path.is_file():
            return

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        messages = state.get("messages") if isinstance(state, dict) else None
        if not isinstance(messages, list):
            return

        input_tokens = 0
        output_tokens = 0
        cache_tokens = 0
        total_cost_usd = 0.0
        cost_usd_by_role: dict[str, float] = {}
        saw_input_tokens = False
        saw_output_tokens = False
        saw_cache_tokens = False
        saw_cost = False

        for message in messages:
            if not isinstance(message, dict):
                continue

            usage = message.get("usage")
            if isinstance(usage, dict):
                prompt_tokens = self._nonnegative_int(usage.get("prompt_tokens"))
                if prompt_tokens is not None:
                    input_tokens += prompt_tokens
                    saw_input_tokens = True

                completion_tokens = self._nonnegative_int(
                    usage.get("completion_tokens")
                )
                if completion_tokens is not None:
                    output_tokens += completion_tokens
                    saw_output_tokens = True

            raw_data = message.get("raw_data")
            raw_usage = raw_data.get("usage") if isinstance(raw_data, dict) else None
            prompt_details = (
                raw_usage.get("prompt_tokens_details")
                if isinstance(raw_usage, dict)
                else None
            )
            if isinstance(prompt_details, dict):
                cached_tokens = self._nonnegative_int(
                    prompt_details.get("cached_tokens")
                )
                if cached_tokens is not None:
                    cache_tokens += cached_tokens
                    saw_cache_tokens = True

            cost_usd = self._nonnegative_float(message.get("cost"))
            if cost_usd is not None:
                total_cost_usd += cost_usd
                saw_cost = True
                role = message.get("role")
                if isinstance(role, str):
                    cost_usd_by_role[role] = cost_usd_by_role.get(role, 0.0) + cost_usd

        if saw_input_tokens:
            context.n_input_tokens = input_tokens
        if saw_output_tokens:
            context.n_output_tokens = output_tokens
        if saw_cache_tokens:
            context.n_cache_tokens = cache_tokens
        if saw_cost:
            context.cost_usd = total_cost_usd
            metadata = dict(context.metadata or {})
            metadata["tau3_cost_usd_by_role"] = dict(sorted(cost_usd_by_role.items()))
            context.metadata = metadata

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _nonnegative_float(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None
        return float(value)

    def _staged_harness_dir(self) -> Path:
        target = self.logs_dir / "_tau3_harness_upload"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            self.harness_path,
            target,
            ignore=shutil.ignore_patterns(
                ".env",
                ".git",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "__pycache__",
                "*.pyc",
            ),
        )
        return target

    def _runner_package_dir(self) -> Path:
        return self._runner_package_root() / "orchestrator_harness"

    def _runner_package_root(self) -> Path:
        return Path(__file__).parent / "src" / "tau3_bench"

    def _task_config_path(self) -> Path:
        trial_config = json.loads(
            (self.logs_dir.parent / "config.json").read_text(encoding="utf-8")
        )
        return Path(trial_config["task"]["path"]) / "tests" / "config.json"

    def _run_command(self) -> str:
        agent_dir = EnvironmentPaths.agent_dir.as_posix()
        effective_seed = self._effective_seed()
        seed = "none" if effective_seed is None else str(effective_seed)
        parts = [
            "python3",
            "-m",
            RUNNER_MODULE,
            "--model",
            shlex.quote(self.model_name or DEFAULT_MODEL),
            "--harness-dir",
            self._REMOTE_HARNESS_DIR.as_posix(),
            "--task-config",
            REMOTE_TASK_CONFIG_PATH,
            "--state-path",
            f"{agent_dir}/tau3_runtime_state.json",
            "--trace-path",
            f"{EnvironmentPaths.verifier_dir.as_posix()}/tau3_openinference_spans.otlp.jsonl",
            "--max-steps",
            str(self.max_steps),
            "--max-errors",
            str(self.max_errors),
            "--temperature",
            str(self.temperature),
            "--seed",
            seed,
        ]
        if self.reasoning_effort:
            parts.extend(["--reasoning-effort", shlex.quote(self.reasoning_effort)])
        run = " ".join(parts)
        return (
            f"rm -rf {self._LOG_HARNESS_DIR.as_posix()} && "
            f"cp -a {self._REMOTE_HARNESS_DIR.as_posix()} "
            f"{self._LOG_HARNESS_DIR.as_posix()} && "
            f"cd {self._REMOTE_AGENT_DIR.as_posix()} && "
            f"{run} 2>&1 | tee {agent_dir}/tau3-orchestrator-harness.txt"
        )

    def _runner_env(self) -> dict[str, str]:
        names = {
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "TAU2_USER_MODEL",
            "TAU2_USER_TEMPERATURE",
            "TAU2_USER_REASONING_EFFORT",
        }
        env = {name: value for name in names if (value := self._get_env(name))}
        env.update(
            {
                name: value
                for source in (os.environ, self._extra_env)
                for name, value in source.items()
                if name.startswith("LITELLM_") and value
            }
        )
        return env

    def _effective_seed(self) -> int | None:
        if self.seed is None:
            return None
        rng = random.Random(self.seed)
        seed = self.seed
        for _ in range(max(0, self.tau2_trial_index) + 1):
            seed = rng.randint(0, 1_000_000)
        return seed
