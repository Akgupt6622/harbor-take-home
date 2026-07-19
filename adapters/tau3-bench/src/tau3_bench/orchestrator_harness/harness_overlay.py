from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

TAU2_RUNTIME_ROOT = Path("/opt/tau2-bench")

SHARED_MODULE_ALIASES = {
    "harness.config": "tau2.config",
    "harness.agent.base.llm_config": "tau2.agent.base.llm_config",
    "harness.agent.base.participant": "tau2.agent.base.participant",
    "harness.agent.base_agent": "tau2.agent.base_agent",
    "harness.data_model.message": "tau2.data_model.message",
    "harness.environment.db": "tau2.environment.db",
    "harness.environment.tool": "tau2.environment.tool",
    "harness.environment.toolkit": "tau2.environment.toolkit",
    "harness.utils": "tau2.utils",
    "harness.utils.io_utils": "tau2.utils.io_utils",
    "harness.utils.llm_utils": "tau2.utils.llm_utils",
    "harness.utils.pydantic_utils": "tau2.utils.pydantic_utils",
    "harness.utils.utils": "tau2.utils.utils",
}

DOMAIN_MODEL_ALIASES = {
    "harness.airline.data_model": "tau2.domains.airline.data_model",
    "harness.retail.data_model": "tau2.domains.retail.data_model",
    "harness.telecom.data_model": "tau2.domains.telecom.data_model",
    "harness.banking_knowledge.data_model": (
        "tau2.domains.banking_knowledge.data_model"
    ),
}

DOMAIN_TOOLKITS = {
    "airline": ("harness.airline.tools", "AirlineTools"),
    "retail": ("harness.retail.tools", "RetailTools"),
    "telecom": ("harness.telecom.tools", "TelecomTools"),
    "banking_knowledge": ("harness.banking_knowledge.tools", "KnowledgeTools"),
}


def import_attr(module_name: str, attr_name: str) -> Any:
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def prepare_tau2(config: dict[str, Any]) -> None:
    root = Path(
        str(
            config.get("tau2_root") or os.getenv("TAU2_BENCH_ROOT") or TAU2_RUNTIME_ROOT
        )
    )
    src = root / "src"
    if not (src / "tau2").exists():
        raise ImportError(f"Could not locate tau2 sources under {src}")

    sys.path.insert(0, str(src))
    os.environ.setdefault("TAU2_BENCH_ROOT", str(root))
    os.environ.setdefault("TAU2_DATA_DIR", str(root / "data"))


def prepare_harness(harness_dir: Path) -> None:
    if not harness_dir.is_dir():
        raise FileNotFoundError(f"Harness directory not found: {harness_dir}")

    sys.path.insert(0, str(harness_dir.parent))
    for harness_module, tau2_module in (
        SHARED_MODULE_ALIASES | DOMAIN_MODEL_ALIASES
    ).items():
        sys.modules[harness_module] = importlib.import_module(tau2_module)


def load_task(config: dict[str, Any]) -> Any:
    Task = import_attr("tau2.data_model.tasks", "Task")
    return Task.model_validate(config["task"])


def resolve_env_constructor(
    domain: str,
    config: dict[str, Any],
    task: Any,
) -> tuple[Any, dict[str, Any]]:
    if domain == "airline":
        return import_attr("tau2.domains.airline.environment", "get_environment"), {}
    if domain == "retail":
        return import_attr("tau2.domains.retail.environment", "get_environment"), {}
    if domain == "telecom":
        return (
            import_attr(
                "tau2.domains.telecom.environment", "get_environment_manual_policy"
            ),
            {},
        )
    if domain == "banking_knowledge":
        return import_attr(
            "tau2.domains.banking_knowledge.environment", "get_environment"
        ), {
            "retrieval_variant": config["retrieval_variant"],
            "task": task,
        }
    raise ValueError(f"Unsupported tau2 domain: {domain}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _format_full_kb_documents(documents_dir: Path) -> str:
    documents: list[str] = []
    for path in sorted(documents_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        document_id = str(payload["id"])
        documents.append(
            "\n".join(
                [
                    f'<document id="{document_id}">',
                    f"<title>{payload['title']}</title>",
                    "<content>",
                    str(payload["content"]),
                    "</content>",
                    "</document>",
                ]
            )
        )
    return "\n\n".join(documents)


def load_harness_policy(domain: str, harness_dir: Path) -> str:
    if domain == "airline":
        return _read(harness_dir / "airline" / "policy.md")
    if domain == "retail":
        return _read(harness_dir / "retail" / "policy.md")
    if domain == "telecom":
        domain_dir = harness_dir / "telecom"
        return (
            _read(domain_dir / "main_policy.md")
            + "\n\n"
            + _read(domain_dir / "tech_support_manual.md")
        )
    if domain == "banking_knowledge":
        domain_dir = harness_dir / "banking_knowledge"
        prompt = _read(domain_dir / "prompts" / "full_kb.md")
        for component_path in sorted(
            (domain_dir / "prompts" / "components").glob("*.md")
        ):
            prompt = prompt.replace(
                "{{component:" + component_path.stem + "}}", _read(component_path)
            )
        return prompt.replace(
            "{{full_knowledge_base}}",
            _format_full_kb_documents(domain_dir / "documents"),
        ).strip()
    raise ValueError(f"Unsupported tau2 domain: {domain}")


def _banking_user_discoverable_specs(user_tools: Any) -> dict[str, dict[str, Any]]:
    parse_docstring = import_attr(
        "harness.banking_knowledge.tools", "parse_discoverable_tool_docstring"
    )
    return {
        name: parse_docstring(method)
        for name, method in user_tools.get_discoverable_tools().items()
    }


def instantiate_harness_toolkit(domain: str, environment: Any) -> Any:
    module_name, class_name = DOMAIN_TOOLKITS[domain]
    toolkit_class = import_attr(module_name, class_name)
    db = environment.tools.db
    if domain == "banking_knowledge":
        specs = _banking_user_discoverable_specs(environment.user_tools)
        return toolkit_class(
            db,
            discoverable_user_tool_names=set(specs),
            discoverable_user_tool_specs=specs,
        )
    return toolkit_class(db)


def apply_harness_overlay(environment: Any, domain: str) -> Any:
    environment.tools = instantiate_harness_toolkit(domain, environment)
    environment.sync_tools()
    return environment


def make_harness_environment_constructor(
    domain: str,
    constructor: Any,
    harness_dir: Path,
) -> Any:
    prepare_harness(harness_dir)

    def wrapped_constructor(*args: Any, **kwargs: Any) -> Any:
        environment = constructor(*args, **kwargs)
        return apply_harness_overlay(environment, domain)

    return wrapped_constructor


def environment_spec(config: dict[str, Any]) -> tuple[str, Any, Any, dict[str, Any]]:
    prepare_tau2(config)
    task = load_task(config)
    domain = str(config["domain"])
    constructor, env_kwargs = resolve_env_constructor(domain, config, task)
    return domain, task, constructor, env_kwargs


def build_harness_environment(
    config: dict[str, Any],
    harness_dir: Path,
) -> tuple[str, Any, Any, dict[str, Any]]:
    domain, task, constructor, env_kwargs = environment_spec(config)
    prepare_harness(harness_dir)
    environment = constructor(solo_mode=False, **env_kwargs)
    apply_harness_overlay(environment, domain)
    return domain, task, environment, env_kwargs
