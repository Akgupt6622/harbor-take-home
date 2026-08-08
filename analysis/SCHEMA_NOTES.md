# Run-artifact schema notes (from smoke job, 2026-08-08)

Source: `results/tau-retail/smoke/` — 5 trials, 4 passed / 1 failed (`tau3-retail-91`),
job total cost $0.1443. All observations below are from reading raw files; nothing under
`results/` was modified.

## 1. Directory layout

```
results/tau-retail/smoke/               # <jobs_dir>/<job-name>/
├── config.json          # full job config (tau-retail.yaml resolved + CLI args)
├── job.log              # Harbor job-level orchestration log
├── lock.json            # provenance: harbor version + git commit, full CLI invocation,
│                        #   dataset/task checksums (schema_version 1)
├── result.json          # job aggregate: trial counts, per-eval reward histogram
│                        #   (reward value -> [trial names]), token totals, stats.cost_usd
└── tau3-retail-54__SB33V7b/            # one dir per trial: <task-name>__<shortuuid>
    ├── config.json      # per-trial config (verified byte-identical to result.json's
    │                    #   embedded "config" field)
    ├── result.json      # THE per-trial record (see field map below)
    ├── trial.log        # Harbor env orchestration: Modal DinD setup, compose, and the
    │                    #   exact runner command line (incl. --seed, --max-steps 200,
    │                    #   --max-errors 10)
    ├── artifacts/manifest.json          # artifact collection record (source/dest/status)
    ├── agent/
    │   ├── harness/                     # snapshot of benchmarks/tau3-bench/harness (gitignored)
    │   ├── _tau3_harness_upload/        # upload staging copy of same (gitignored)
    │   ├── setup/                       # empty in smoke run
    │   ├── tau3-orchestrator-harness.txt  # runner stdout/stderr (tau2 loguru logs)
    │   └── tau3_runtime_state.json      # ★ full conversation transcript + metadata
    └── verifier/
        ├── reward.txt   # literal "1.0" or "0.0"
        └── tau3_openinference_spans.otlp.jsonl   # ★ OTLP trace (§2)
```

Trial dir naming: `<task-name>__<7-char shortuuid>` — **no attempt number in the name**.
(Untested here: with `--n-attempts 3` the same task should produce multiple dirs
differing only in suffix; verify on the first multi-attempt job.)

### Where the required fields live

| Field | File | Path |
|---|---|---|
| task name | trial `result.json` | `task_name` (`sierra-research/tau3-bench__tau3-retail-54`), short id in `task_id.path`; also `tau3.task_id` attr ("54") on AGENT/TOOL/EVALUATOR spans |
| pass/fail | trial `result.json` | `verifier_result.rewards.reward` (1.0/0.0); duplicated in `verifier/reward.txt` and EVALUATOR span `evaluation.success` |
| failure reason | EVALUATOR span only | `evaluation.status`: `"passed"` / `"mismatch"` — **coarse; no detail anywhere** (see §2d) |
| trial cost | trial `result.json` | `agent_result.cost_usd`; per-role split in `agent_result.metadata.tau3_cost_usd_by_role` (`assistant` vs `user`); job total in job `result.json` `stats.cost_usd` |
| agent model | trial `result.json` | `config.agent.model_name` and `agent_info.model_info.name` (`gpt-5.6-luna`) |
| user model | trial `result.json` | `config.agent.env.TAU2_USER_MODEL` (`gpt-5.6-sol`) |
| timing | trial `result.json` | `started_at`/`finished_at` plus per-phase blocks: `environment_setup`, `agent_setup`, `agent_execution`, `verifier` (ISO-8601 **UTC with Z**) |
| tokens | trial `result.json` | `agent_result.n_input_tokens` / `n_cache_tokens` / `n_output_tokens` (input INCLUDES cached; see gotcha in §5) |
| exceptions | trial `result.json` | `exception_info` (null on success) |

`agent/tau3_runtime_state.json` additionally has: `termination_reason` (`user_stop` in
all 5 trials), `step_count`, `num_errors`, `seed`, and the full `messages` list.

## 2. Span file deep-dive (`verifier/tau3_openinference_spans.otlp.jsonl`)

Examined: `tau3-retail-54` (PASSED, 60 spans, contains one tool error) and
`tau3-retail-91` (FAILED, 39 spans).

**Format:** protobuf-JSON OTLP export batches — one `ExportTraceServiceRequest` per line
(`resource_spans[].scope_spans[].spans[]`), NOT one span per line
(trial 54: 60 lines happened to equal 60 spans; trial 91: 39/39 — but parse the nested
structure, don't assume 1:1). Attributes are OTLP `{key, value: {string_value|int_value|
bool_value|double_value}}` lists that need unwrapping. `int_value` arrives as a string.

**Span kinds** (`openinference.span.kind` attr; OTLP `kind` is always `SPAN_KIND_INTERNAL`):

| kind | count (54 / 91) | span names | scope |
|---|---|---|---|
| LLM | 44 / 29 | `responses`, `completion` | `openinference.instrumentation.litellm` |
| TOOL | 14 / 8 | `tau3.tool.<tool_name>` | `tau3_bench.orchestrator_harness` |
| AGENT | 1 / 1 | `tau3.orchestrator.run` | same |
| EVALUATOR | 1 / 1 | `tau3.evaluation.result` | same |

### (a) LLM messages

Chat-completions-shaped spans (`completion`): `llm.input_messages.<i>.message.role` +
`.message.content` (flat string). Responses-API-shaped spans (`responses`): role at
`llm.input_messages.<i>.message.role`, text at
`.message.contents.<j>.message_content.text` (+ `.type: "text"`). Output messages use
the same pattern under `llm.output_messages.<i>.…`. Roles seen: `system`, `user`,
`assistant`, `tool`. Raw request/response JSON also present in `input.value` /
`output.value` (mime `application/json`).

Agent (luna) assistant text content is a JSON-wrapped string: `{"message": "..."}` —
parser must unwrap. User-sim (sol) content is plain text. Luna reasoning arrives as
`contents.<j>.message_content.type: "reasoning"` with **`encrypted_content`** — not
readable; only `llm.token_count.completion_details.reasoning` is usable.

The user-sim runs as separate `completion` spans with `llm.model_name: gpt-5.6-sol`;
its system prompt is the "User Simulation Guidelines" and roles are **transposed**
(the sim's `assistant` output is the customer utterance). Only 2 messages in its input
(system + user) — it gets the conversation re-serialized, not the agent's view.

### (b) Tool calls — BOTH representations

1. On the LLM span output: `llm.output_messages.<i>.message.tool_calls.<j>.tool_call
   .function.name` / `.function.arguments` (JSON string) / `.id`.
2. As separate TOOL spans (children of the AGENT span, NOT of LLM spans):
   - `tool.name` (bare name, span name is `tau3.tool.<name>`)
   - `tool.id` = provider call id (`call_…`)
   - `input.value` = arguments as JSON string
   - `output.value` = result (plain string; JSON string for dict results)
   - `tau3.tool.requestor` = `"assistant"` (user-requested tools possible in tau2)

### (c) Tool errors — real example from trial 54

Three signals fire simultaneously on the TOOL span; there are NO exception events:

```
status: {"code": "STATUS_CODE_ERROR", "message": "tool_error"}
tau3.tool.error: true
input.value:  {"email": "silva7872@example.com"}
output.value: "Error: User not found"      <- ValueError text with "Error: " prefix
```

(Agent had guessed a truncated email; retried with the full one 6s later and succeeded.)
The error also increments `num_errors` on the AGENT span / runtime state. In
`tau3_runtime_state.json` the same event is a `role: "tool"` message with `error: true`.

### (d) Verifier / evaluation span — quoted in full (failed trial 91)

```json
{ "name": "tau3.evaluation.result",
  "status": {"code": "STATUS_CODE_OK"},
  "events": [{ "name": "harbor.evaluation.result",
               "attributes": { "evaluation.success": false,
                               "evaluation.status": "mismatch",
                               "benchmark": "tau3-bench" }}],
  "attributes": { "openinference.span.kind": "EVALUATOR",
                  "benchmark": "tau3-bench", "tau3.domain": "retail",
                  "tau3.task_id": "91",
                  "evaluation.success": false, "evaluation.status": "mismatch",
                  "evaluation.used_tau2_evaluator": true,
                  "tau3.runtime_termination_reason": "user_stop",
                  "tau3.step_count": 25, "tau3.num_errors": 0,
                  "tau3.trajectory_message_count": 26 }}
```

Granularity: **a single boolean + one status string** (`passed` seen on all 4 passes,
`mismatch` on the failure). No db-state vs nl-assertion breakdown, no per-assertion
results, no diff of expected vs actual state — the span is sanitized (matches the PDF's
wording). **Failure-mode analysis will have to be inferred from the transcript itself,
not from the verifier output.** Also: the gpt-5.2 NL-assertion verifier makes **no LLM
spans** in this trace (its calls run in Harbor's verifier phase, untraced — consistent
with the PDF's cost warning). This is the only span with `events`.

### (e) call_id linkage — yes, with a subtlety

TOOL `tool.id` (`call_…`) joins to the LLM side, but the key differs by span shape:
- `completion`-shaped spans: `…tool_calls.<j>.tool_call.id` = `call_…` ✔ direct join.
- `responses`-shaped spans: `…tool_calls.<j>.tool_call.id` = **`fc_…`** (Responses-API
  item id); the joinable `call_…` id is at `…message.tool_call_id` on the same output
  message.

Verified in trial 54: all 14 TOOL `tool.id`s appear among LLM-span tool-call ids.
In `tau3_runtime_state.json`, `tool_calls[].id` and tool-message `id` are `call_…` ✔.

### (f) Timestamps

`start_time_unix_nano` / `end_time_unix_nano` — **strings of unix nanoseconds** (cast to
int). Spans are **NOT in timestamp order in file order** (litellm vs harness scopes flush
in separate batches) → always sort by `start_time_unix_nano`. Trace/span/parent ids are
base64. Every span carries `trace_id`; all spans of a trial share one trace.

### (g) Double-count — confirmed and structural

Every luna call is exported **twice**: a `completion` span AND a `responses` child span
(the `responses` span's `parent_span_id` IS its duplicate `completion` span), starting
within ~2–4 ms with **identical `llm.token_count.*` and `llm.cost.total`**. Trial 54:
19 luna calls → 19 `responses` + 19 duplicate `completion` (4 of which have
`llm.model_name: null`), plus 6 genuine sol `completion` spans = 44 LLM spans for 25
real calls. Sol calls are exported once (`completion` only, child of the AGENT span).

**Dedup rule (pick one, both verified equivalent):**
- Structural: drop any `completion` span whose span_id is the `parent_span_id` of a
  `responses` span. Keep all `responses` + remaining `completion`.
- Practical: keep `responses` spans (all luna) + `completion` spans with
  `llm.model_name == "gpt-5.6-sol"`. (Don't key on model alone — 4 luna dupes have
  null model_name.)

Summing `llm.cost.total` naively over all LLM spans double-counts luna exactly 2×.

### Reconstructed transcript (trial 91, FAILED) — from spans only

Built from deduped LLM spans + TOOL spans sorted by start time:

```
[  0.0s] USER(sol) : Hi. I'm pretty upset with the quality of two skateboards I just
                     bought, and I'd like to return both of them for a refund to my
                     credit card.
[  3.0s] AGENT(luna): I'm sorry the skateboards didn't meet your expectations. …please
                     provide the email address on your account, or full name + ZIP.
[  6.7s] USER      : I don't remember which email I used — Mei Ahmed, ZIP 78705.
[  7.9s] AGENT     : → find_user_id_by_name_zip({first_name: Mei, last_name: Ahmed,
                     zip: 78705})
[  9.6s] TOOL      : ← mei_ahmed_4909
[  9.6s] AGENT     : → get_user_details(mei_ahmed_4909)      ← {…profile…}
[ 11.6s] AGENT     : → get_order_details(#W7553978)          ← {…2 skateboards…}
[ 13.3s] AGENT     : Found order #W7553978: skateboard $186.06 + skateboard $202.13…
[ 15.7s] USER      : Also return the smart watch and the e-reader I just bought; for
                     the e-reader prefer an exchange if the same item is available.
[ 17.8s] AGENT     : → get_order_details(#W3239882)          ← {…e-reader order…}
[ 20.2s] AGENT     : → get_product_details(3801771308)       ← {…e-reader variants…}
[ 22.7s] AGENT     : → get_order_details(#W2598324)          ← {…third order…}
[ 26.5s] AGENT     : Found: Smart Watch (order #W7553978, $339.85), E-Reader (#W3239882,
                     $252.06); same e-reader variant is available…
[ 31.2s] USER      : Yes — return the two skateboards + smart watch to the Mastercard,
                     exchange the e-reader for the same model.
[ 32.6s] AGENT     : → return_delivered_order_items(#W7553978, [4545791457, 3098764622,
                     1631806422], credit_card_5902940)        ← {…return requested…}
[ 34.6s] AGENT     : → exchange_delivered_order_items(#W3239882, [9494281769],
                     [9494281769], credit_card_5902940)       ← {…exchange requested…}
[ 36.3s] AGENT     : Your requests are complete… refund $728.04 to Mastercard …9375…
[ 38.4s] USER      : ###STOP###
```

Conversation-end sentinel: the sim emits literal `###STOP###`;
runtime `termination_reason: "user_stop"`.

**Cross-check:** identical, message-for-message and in order, to
`agent/tau3_runtime_state.json` (26 messages, `trajectory_message_count: 26`).
**`harbor view` comparison:** the viewer (web server; headless API probed at
`/api/jobs/smoke/trials/…`) renders trial `result.json` metadata plus a raw file
browser; for this agent it has **no parsed transcript** (`/agent-logs` returns empty —
the agent doesn't emit ATIF trajectories). So no transcript mismatch is possible;
`tau3_runtime_state.json` is the authoritative comparison target and it matches. One
viewer discrepancy: the jobs list reports `total_input_tokens: 47170` = uncached only
(316,002 − 268,832), while `result.json` reports `n_input_tokens: 316002` (includes
cached).

## 3. Sanity totals

| trial | reward | agent_result.cost_usd | in_tok | out_tok | msgs | term | tool errors |
|---|---|---|---|---|---|---|---|
| tau3-retail-54__SB33V7b | 1.0 | $0.0396 | 121,154 | 2,175 | 40 | user_stop | 1 |
| tau3-retail-62__HDbtg28 | 1.0 | $0.0334 | 48,548 | 1,188 | 22 | user_stop | 0 |
| tau3-retail-65__kjt5QNv | 1.0 | $0.0139 | 29,200 | 584 | 16 | user_stop | 0 |
| tau3-retail-91__v29PA5V | **0.0** | $0.0304 | 63,403 | 1,808 | 26 | user_stop | 0 |
| tau3-retail-96__h2JZKXu | 1.0 | $0.0270 | 53,697 | 1,383 | 23 | user_stop | 0 |

- Σ trial costs = **$0.144347** = job `stats.cost_usd` exactly ✔
- trials with reward 1.0 = **4** = Harbor's reported mean 0.800 × 5 ✔ (job result.json's
  `reward_stats` lists the exact trial names per reward value)

## 4. Verdict vs target TrialRecord schema

| Target field | Source in raw data | Verdict |
|---|---|---|
| turns: role | `runtime_state.messages[].role` (assistant/user/tool); spans: message roles | CONFIRMED |
| turns: content | `runtime_state.messages[].content`; spans: `llm.output_messages…` | CONFIRMED — but luna assistant content is JSON-wrapped `{"message": …}`; unwrap |
| turns: tool_calls | `runtime_state.messages[].tool_calls[]` `{id, name, arguments, requestor}` — arguments already a parsed dict | CONFIRMED |
| call: name | TOOL span `tool.name`; runtime_state `tool_calls[].name` | CONFIRMED |
| call: args | TOOL span `input.value` (JSON string); runtime_state (dict) | CONFIRMED |
| call: result | TOOL span `output.value`; runtime_state tool msg `content` | CONFIRMED |
| call: is_error | TOOL span `tau3.tool.error` + `status.code`; runtime_state tool msg `error` | CONFIRMED |
| call: call_id | TOOL span `tool.id`; runtime_state ids — all `call_…` | CONFIRMED — but on `responses` LLM spans use `message.tool_call_id`, not `tool_call.id` (`fc_…`) |
| verifier: passed | `verifier_result.rewards.reward` / `reward.txt` / `evaluation.success` | CONFIRMED |
| verifier: check_type | `evaluation.status` string only (`passed`/`mismatch`) | DIFFERENT — one coarse enum, not db-state vs nl-assertion |
| verifier: detail | — | **MISSING** — evaluator output is sanitized; no per-assertion or state-diff info exists anywhere in artifacts |
| cost_usd | trial `agent_result.cost_usd`; per-role `metadata.tau3_cost_usd_by_role`; per-call `llm.cost.total` (after dedup) and `runtime_state.messages[].cost` | CONFIRMED |
| token counts | trial `agent_result.n_*_tokens`; per-span `llm.token_count.{prompt,completion,total}` + `prompt_details.cache_read`, `completion_details.reasoning` | CONFIRMED |
| timestamps | spans: unix-ns strings (sort required); runtime_state: ISO per message; trial phases in result.json | CONFIRMED |

## 5. Surprises / gotchas (do not silently adapt)

1. **Verifier detail is missing by design.** `mismatch` is all you get for a failure.
   Failure clustering must be built from transcripts + final tool states, or by re-running
   task checks offline — not from verifier output.
2. **Seed mismatch:** `tau-retail.yaml` sets agent kwargs `seed: 300`, but every trial's
   runner was invoked with `--seed 626729` (identical across all 5 trials; visible in
   `trial.log` and runtime state). The transform 300→626729 is somewhere in the adapter
   agent — worth reading `tau3_orchestrator_harness_agent.py` before trusting
   seed-based reproducibility claims.
3. **Luna reasoning is encrypted** in traces (`encrypted_content`); only reasoning token
   counts are observable.
4. **Two token-accounting conventions:** trial `n_input_tokens` includes cached tokens;
   the viewer's `total_input_tokens` subtracts them. Pick one for reports (suggest
   quoting result.json values).
5. `runtime_state.messages[].usage/cost` exists for user-sim (sol) messages too — sol cost
   ($0.0313 of trial 54's $0.0396) dominates agent cost in some trials; the per-role
   split in `agent_result.metadata.tau3_cost_usd_by_role` is the cheap way to see this.
6. The voice/audio fields in runtime_state (`is_audio`, `audio_*`, `speech_effects`, …)
   are all null/false in retail — text-only; ignore them in the parser.
7. `int_value` in OTLP attrs is a **string** in protobuf-JSON; cast explicitly.
7b. **Parallel tool calls exist** (smoke trial 96, turn 8: two `get_order_details`
   in one assistant message). Each call gets its own `role: "tool"` message, but
   the batch is ONE orchestrator step, so `len(messages) == step_count + 1` only
   holds for single-call trials. General form (verified on all 5 smoke trials):
   `len(messages) == step_count + 1 + (n_tool_result_msgs − n_assistant_msgs_with_calls)`.
8. Easiest parser path: **`agent/tau3_runtime_state.json` for conversation + costs**,
   spans only for per-call timing/token detail, `result.json` for verdict/cost/phases.
   The two agree exactly on the smoke data.
