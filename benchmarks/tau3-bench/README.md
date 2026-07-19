## Tau2 Overlay Harness For Tau3-Bench

The tau3-bench adapter should run tasks with tau2's normal half-duplex
conversation loop:

- `agent.generate_next_message(...)`
- `user.generate_next_message(...)`
- `environment.get_response(...)`

The editable `harness/` directory is not the full executable runtime. It is an
overlay containing the pieces an improvement step may change, plus a small
amount of read-only interface context. Harbor's tau3-bench adapter owns task
execution and should use the installed tau2 runtime for orchestration, user
simulation, environment construction, evaluator logic, and task/config loading.

The overlay boundary is:

- editable: `LLMAgent`, assistant-side domain toolkits, assistant policies, and
  assistant prompt/knowledge files
- stock runtime: default simulated users, user toolkits, environment lifecycle,
  task loading, verifier/evaluator code, task definitions, and runtime DB
  fixtures

This keeps the improvement step focused on agent behavior and assistant tools
without exposing the user action surface or evaluation/task internals.

## Runtime Model

The shared coordination point between agent and user is the tau2 `Environment`.
It owns:

- `tools`: assistant-side toolkit
- `user_tools`: optional user-side toolkit
- `sync_tools()`: optional domain-specific synchronization hook

Domain state is managed by the installed runtime:

- airline/retail: assistant tools only; no user toolkit DB
- banking knowledge: stock user tools and custom assistant tools operate on the
  same `TransactionalDB` object
- telecom: stock user tools and custom assistant tools use separate DBs, and
  stock `sync_tools()` copies/derives user-visible facts from assistant state
  into user state

User tools should come from installed tau2, not from editable `harness/`. The
custom assistant toolkit must remain compatible with the stock shared DB/data
model so stock user tools and `sync_tools()` continue to work.

## Runner And Verifier Symmetry

The runner and verifier must apply the same assistant-toolkit overlay to the
candidate trajectory. If the runner executes tool calls with a modified
`TelecomTools` but the verifier replays the candidate trajectory with stock
`TelecomTools`, final DB reconstruction can drift or fail.

The adapter should therefore expose a single domain-constructor hook used by
both orchestration and candidate verification:

1. construct the stock tau2 domain environment from task config
2. replace or bind the assistant toolkit with the matching class from
   `harness/<domain>/tools.py`
3. keep user tools stock
4. keep stock `sync_tools()` behavior

For banking knowledge, the current overlay mode is `full_kb`: assistant policy
uses the copied full-KB prompt/documents, while user tools and transactional DB
loading remain stock runtime responsibilities.

Current adapter implementation:

- installed agent:
  `adapters/tau3-bench/tau3_orchestrator_harness_agent.py`
- in-container runner package:
  `adapters/tau3-bench/src/tau3_bench/orchestrator_harness/runner.py`
- runner overlay helper:
  `adapters/tau3-bench/src/tau3_bench/orchestrator_harness/harness_overlay.py`
- runner/verifier tracing helper:
  `adapters/tau3-bench/src/tau3_bench/orchestrator_harness/tracing.py`
- generated verifier overlay helper:
  `<generated-task>/tests/harness_overlay.py`, copied from the runner overlay
  helper when the adapter generates the task
- generated verifier tracing helper:
  `<generated-task>/tests/tracing.py`, copied from the runner tracing helper
  when the adapter generates the task
- Harbor import path:
  `tau3_orchestrator_harness_agent:Tau3OrchestratorHarnessAgent` with
  `PYTHONPATH=adapters/tau3-bench`

At install time, the agent uploads local `benchmarks/tau3-bench/harness` to
`/installed-agent/harness` and uploads the packaged runner to
`/installed-agent/tau3_bench/orchestrator_harness`. At run time, it copies the
exact uploaded harness tree to `/logs/agent/harness` before orchestration and
runs:

```bash
cd /installed-agent
python3 -m tau3_bench.orchestrator_harness.runner
```

The verifier treats
`/logs/agent/harness` as the replay overlay if that directory exists.

For grading, the verifier intentionally uses asymmetric replay:

- candidate DB/state: replay `/logs/agent/tau3_runtime_state.json` with the
  harness assistant toolkit and stock tau2 user toolkit
- golden DB/state: replay the task's expected actions with stock tau2 assistant
  and user toolkits
- `DB`: compare candidate state to the stock golden state
- `ENV_ASSERTION`, `COMMUNICATE`, and `NL_ASSERTION`: keep the stock tau2
  component semantics
- `ACTION`: ignore expected assistant actions and keep only expected user
  actions, since user tools remain stock

Generated task configs keep the full expected action list for DB/golden replay
but write an effective `reward_basis` with assistant-only `ACTION` checks
removed. Tasks whose remaining reward basis is empty are excluded from the
generated harness dataset.

Harbor uploads `/tests` only for the verifier phase, so the agent runner cannot
read `/tests/config.json`. The installed agent resolves the local task path
from the trial config, uploads `<task>/tests/config.json` to
`/installed-agent/task_config.json`, and runs the in-process orchestrator
against that uploaded config.

Both runner and verifier alias copied shared interface modules such as
`harness.data_model.message` and `harness.environment.toolkit` to the installed
`tau2` runtime modules before importing editable harness code. This avoids
creating divergent message/tool class identities while still letting the
editable assistant agent and assistant toolkits import from `harness.*`.

The aliasing, stock environment construction, assistant-toolkit replacement,
banking `full_kb` policy rendering, and verifier registry wrapper live in the
packaged `harness_overlay.py` helper. Generated tasks receive a copy under
`tests/harness_overlay.py` because Harbor uploads `/tests` for verification but
not the installed-agent package. Treat the packaged helper as the source of
truth and regenerate tasks after changing it.

Banking task configs generated by the adapter should use
`"retrieval_variant": "full_kb"`. Existing old generated tasks may still say
`"bm25"` and should be regenerated before full harness runs.

Banking user-discoverable tools remain stock tau2 user tools. The harness
assistant toolkit receives their metadata from the runtime and must give them to
the user with complete bound JSON arguments:

```text
give_discoverable_user_tool(discoverable_tool_name, arguments)
```

Do not default `arguments` to `{}`. The stock banking user-discoverable tools
all require arguments, and giving an unbound tool lets the user simulator see a
tool it cannot execute for the task. Generated task configs also normalize
`give_discoverable_user_tool` expected actions so runner/verifier replay can use
bound arguments while action matching compares only `discoverable_tool_name`.

## Runtime State Serialization

The in-process runner should write the same state shape as the current tau3 MCP
runtime:

- path: `/logs/agent/tau3_runtime_state.json`
- top-level fields include domain/task/termination metadata
- top-level `messages` is a list of tau2 message model dumps:
  `message.model_dump(mode="json")`

Verifier-side reader behavior to match:

- `_load_runtime_state()`
- `_parse_trajectory()`
- `_evaluate_from_state()`

Those live in:

```text
adapters/tau3-bench/src/tau3_bench/task-template/tests/evaluate.py
```

The easiest implementation path is to serialize the orchestrator trajectory
using the same tau2 message models and copy the current runtime state's shape
rather than inventing a new schema.

## OpenInference Tracing

The editable harness runner and verifier append OpenInference/OpenTelemetry
traces to one file:

- path: `/logs/verifier/tau3_openinference_spans.otlp.jsonl`
- export format: OTLP JSON lines, one `ExportTraceServiceRequest` per line

The verifier appends a sanitized evaluation span to the same file. It includes
only success/failure, status, domain/task id, and runtime counters. It excludes
reward basis, reward breakdowns, expected actions, assertion details, DB diffs,
message contents, and other grader internals.

## Harness Contents

Current intended layout:

```text
harness/
    config.py
    llm_agent.py

    agent/
        base_agent.py
        base/
            llm_config.py
            participant.py

    data_model/
        message.py

    environment/
        db.py
        tool.py
        toolkit.py

    utils/
        __init__.py
        io_utils.py
        llm_utils.py
        pydantic_utils.py
        utils.py

    airline/
        __init__.py
        data_model.py
        policy.md
        tools.py

    retail/
        __init__.py
        data_model.py
        policy.md
        tools.py

    telecom/
        __init__.py
        data_model.py
        main_policy.md
        tech_support_manual.md
        tech_support_workflow.md
        tools.py
        utils.py

    banking_knowledge/
        __init__.py
        DATABASE_GUIDE.md
        data_model.py
        db_query.py
        documents/
        prompts/
            full_kb.md
            components/
                additional_instructions.md
                policy_header.md
        tools.py
        utils.py
```

The copied shared interface files are read-only context for editing the overlay
and for local importability. At execution time, avoid creating divergent class
identities between runner and verifier. The adapter should either use installed
tau2 shared interfaces directly or load/alias these copied interfaces
consistently in both runner and verifier.

## What To Copy From Tau2-Bench

Copy only files needed for the editable overlay and small interface context.

Shared agent/interface context:

```text
src/tau2/config.py                         -> harness/config.py
src/tau2/agent/base_agent.py               -> harness/agent/base_agent.py
src/tau2/agent/base/llm_config.py          -> harness/agent/base/llm_config.py
src/tau2/agent/base/participant.py         -> harness/agent/base/participant.py
src/tau2/data_model/message.py             -> harness/data_model/message.py
src/tau2/environment/db.py                 -> harness/environment/db.py
src/tau2/environment/tool.py               -> harness/environment/tool.py
src/tau2/environment/toolkit.py            -> harness/environment/toolkit.py
src/tau2/llm_agent.py                      -> harness/llm_agent.py
src/tau2/utils/{io_utils,llm_utils,pydantic_utils,utils}.py
```

Airline:

```text
src/tau2/domains/airline/data_model.py     -> harness/airline/data_model.py
src/tau2/domains/airline/tools.py          -> harness/airline/tools.py
data/tau2/domains/airline/policy.md        -> harness/airline/policy.md
```

Retail:

```text
src/tau2/domains/retail/data_model.py      -> harness/retail/data_model.py
src/tau2/domains/retail/tools.py           -> harness/retail/tools.py
data/tau2/domains/retail/policy.md         -> harness/retail/policy.md
```

Telecom:

```text
src/tau2/domains/telecom/data_model.py     -> harness/telecom/data_model.py
src/tau2/domains/telecom/tools.py          -> harness/telecom/tools.py
src/tau2/domains/telecom/utils.py          -> harness/telecom/utils.py
data/tau2/domains/telecom/main_policy.md
data/tau2/domains/telecom/tech_support_manual.md
data/tau2/domains/telecom/tech_support_workflow.md
```

Banking knowledge, using `full_kb`:

```text
src/tau2/domains/banking_knowledge/data_model.py
src/tau2/domains/banking_knowledge/db_query.py
src/tau2/domains/banking_knowledge/tools.py
src/tau2/domains/banking_knowledge/utils.py
src/tau2/domains/banking_knowledge/DATABASE_GUIDE.md
data/tau2/domains/banking_knowledge/documents/
data/tau2/domains/banking_knowledge/prompts/full_kb.md
data/tau2/domains/banking_knowledge/prompts/components/policy_header.md
data/tau2/domains/banking_knowledge/prompts/components/additional_instructions.md
```

## Required Cleanup After Copying

Shared cleanup:

- keep only the default text `LLMAgent` path
- remove solo agents, ground-truth/task helpers, orchestrator references, and
  voice/full-duplex support
- keep only text message models required by the half-duplex agent/tool loop
- keep `Tool`, `ToolKitBase`, DB, Pydantic, file IO, hashing, and LLM call
  helpers as interface/context support
- remove copied `Environment` implementation; the adapter/runtime owns
  environment construction and tool execution

Domain cleanup:

- keep assistant toolkit classes and domain DB/data model classes imported by
  those toolkits
- keep non-tool assistant toolkit methods called by the stock runtime after
  overlay replacement, including `sync_tools()`, task
  `initialization_actions`, and `ENV_ASSERTION` functions
- remove domain `environment.py` files unless a future adapter requires a small
  overlay shim
- remove domain DB fixture files such as `db.json`, `db.toml`, and
  `user_db.toml`; task/runtime DB state comes from Harbor/tau2 task config
- remove `get_db()`, `get_environment()`, task-loading helpers, task paths,
  task splits, generated-task code, scenario mutators, oracle helpers, and eval
  helpers
- remove copied user toolkits and user data models; user tools remain stock
  tau2 runtime code
- remove debug/demo `__main__` blocks and generated caches
- for banking, remove the copied `KnowledgeUserTools` class; assistant
  discoverable-tool validation should use metadata injected by the adapter from
  the stock tau2 user toolkit at runtime
- for banking, keep `give_discoverable_user_tool` compatible with stock user
  tools by requiring and validating bound JSON `arguments`; do not reintroduce
  a default empty argument object for required-argument user tools
- for banking, remove retrieval variants not used by `full_kb`: no
  `retrieval.py`, `retrieval_mixins.py`, `retrieval_toolkits.py`, `knowledge/`,
  embeddings, BM25, grep, terminal/sandbox, reranker, golden retrieval, or
  non-`full_kb` prompt variants

Do not include:

- task JSON/TOML definitions, split files, voice task files, audio difficulty
  files, generated telecom task builders
- evaluator, reward, metrics, registry, CLI, runner, orchestrator, API service,
  gym, scripts, experiments, tests, simulation outputs, leaderboard/submission
  tooling
- MCP sidecar bridge code or Modal-only `entrypoint.sh`/`setup.sh`
- voice/audio/full-duplex modules

## Verification Checklist

After cleanup:

```bash
uv run ruff check benchmarks/tau3-bench/harness
PYTHONDONTWRITEBYTECODE=1 python3.12 -m compileall -q benchmarks/tau3-bench/harness
```

Adapter-side checks:

```bash
uv run ruff check adapters/tau3-bench/tau3_orchestrator_harness_agent.py \
  adapters/tau3-bench/src/tau3_bench/adapter.py \
  adapters/tau3-bench/src/tau3_bench/orchestrator_harness \
  adapters/tau3-bench/src/tau3_bench/task-template/tests/evaluate.py \
  benchmarks/tau3-bench/harness
uv run python -m py_compile adapters/tau3-bench/tau3_orchestrator_harness_agent.py \
  adapters/tau3-bench/src/tau3_bench/adapter.py \
  adapters/tau3-bench/src/tau3_bench/orchestrator_harness/__init__.py \
  adapters/tau3-bench/src/tau3_bench/orchestrator_harness/runner.py \
  adapters/tau3-bench/src/tau3_bench/orchestrator_harness/harness_overlay.py \
  adapters/tau3-bench/src/tau3_bench/orchestrator_harness/tracing.py \
  adapters/tau3-bench/src/tau3_bench/task-template/tests/evaluate.py
PYTHONPATH=adapters/tau3-bench/src \
  uv run python -m tau3_bench.orchestrator_harness.runner --help
```

Also verify:

- no source `__pycache__/` directories remain outside local virtualenv/cache
  directories
- no copied user toolkit or domain environment constructor remains
- no task/evaluation files or task path constants remain
- banking has only `full_kb` prompt assets and no retrieval runtime variants
- runner and verifier both apply the same assistant toolkit overlay

## Known Limitation

Because tool execution goes through the tau2 environment, `harness/` is not a
fully self-contained executable agent package. That is intentional for the
current evaluation path. If agents later need to run regression tests against
their own toolkit changes without the Harbor adapter, add a separate local test
execution layer rather than expanding `harness/` back into a full runtime.
