# Tau-2 Retail Take-Home — Writeup

## Overview

**Results: baseline 83/82/89 of 114 (mean 74.3%) over three matched runs → final harness 98/99/101 of 114 (mean 87.1%) over three matched runs. Total spend: ~$80 of the $100 budget ($40.84 agent+user across all 18 runs per the ledger, ~$29 Modal at the reference rate, remainder the gpt-5.2 verifier via provider usage).**

The brute force way to do this would be to run the full eval → read failures one by one → fix each by hand → run the full eval again. This is not scalable and would not meet our allocated budget and time constraints. So proper tooling had to be developed to run this cycle in a more efficient and automated way. I created a 5 step pipeline:

**Parse:** Harbor's per-trial output is verbose JSON spread across several files. The parser distills each trial into one record (verdict, turns, tool calls, errors, cost) plus a transcript I can actually read.

**Triage:** A failed trial tells you almost nothing by itself, the verifier returns reward 0.0 and that's it. But the trial artifacts contain two things you can diagnose mechanically. First, rejected tool calls: every error the backend can raise lives in the tool source, so each rejection maps to a stable key like "user_not_found". Second, each task ships a gold definition of the write actions a correct agent would make. Since the verifier grades final DB state, and only writes change the DB, comparing the agent's writes against the gold writes explains the failure. The triage step pairs them up (matching on order id) and classifies each difference (wrong value, the required tool was never attempted, the tool was attempted but rejected, unnecessary tool attempted). This is implemented as one deterministic pass (`analysis/triage.py`) that derives error keys from the tool source at runtime, reads gold writes from each task's config, and groups the resulting labels into a ranked table (`triage/groups.json`).

**Causes:** An LLM analyst runs over these groups first, drafting a cause entry for each into `analysis/causes.json`: member tasks, transcript citations, a proposed fix, and the tasks that fix should flip. I review those drafts against the flagged transcripts confirming, editing, or rejecting each, and make the confirmed fixes in the harness (prompt, policy text, or tool layer).

**Compose + Verify:** Each confirmed cause becomes a verification dataset, built automatically from its registry entry: the cause's member tasks, plus a random sample of unrelated tasks that passed all three baseline runs (seeded, so the set is reproducible). The random tasks guard against overfitting. Re-running the set costs cents and answers both questions at once: did the predicted tasks flip, and did the controls stay green. Only fixes that survive this get a full run.

Every stage of this pipeline is exposed as a CLI so the loop can be driven by a coding agent today, and it's one step away from an orchestrator agent autonomously running the full cycle — triage, draft causes, compose, verify — end to end.

**Final Harness:** The stock agent rarely made malformed calls, it failed by giving up early, guessing, or quietly dropping one of several requests. The final harness rewrites the policy text into concrete recovery rules for those exact cases: derive the name from a username handle, look up the catalog and ask when more than one variant fits, one confirmation and one call per action per order. A small validator in the runtime also checks every write before it goes out and bounces bad calls back to the model with a hint instead of letting them hit the database.

## Baseline and Failure Analysis

Command:

```
PYTHONPATH=adapters/tau3-bench uv run harbor run -y -c tau-retail.yaml \
      --job-name baseline --env-file .env
```

- Job dirs: `results/tau-retail/baseline`, `baseline-2`, `baseline-3`
- Pass rates: baseline 83/114 (72.8%), baseline-2 82/114 (71.9%), baseline-3 89/114 (78.1%); mean 74.3%
- Cost: $3.22, $3.26, $3.29 agent+user per run ($9.77 total). Breakout across the three runs: agent $1.19, simulated user $8.58 (the sim dominates trace cost); the gpt-5.2 evaluator is not visible in trace costs and is counted via provider usage in the total; analysis tooling cost $0 against this budget (deterministic code, and the LLM analyst ran on Claude — see AI assistance)
- Seed: 300
- Task count: 114 per run
- Models: gpt-5.6-luna (agent), gpt-5.6-sol (simulated user), gpt-5.2 (NL-assertion verifier). Every run in this writeup — baselines, subsets, and finals — used exactly these models (recorded per run in `experiments.jsonl`); no other models were tried

Issues:

- **Authentication dead-end** (7 tasks; e.g. tau3-retail-8): The customer can't recall their email and offers username mei_kovacs_8020 plus a ZIP, the agent twice insists that a username is not a valid credential and the customer gives up. The conversation ends after 7 turns with zero tool calls, though the username plainly encodes the name needed for name+ZIP auth.
- **Wrong variant on exchange** (5 tasks; e.g. tau3-retail-91): Asked to exchange an e-reader "for the same model", the agent passes the customer's existing item id as the replacement instead of looking up the equivalent in-stock variant. The DB ends one item id away from gold and the trial fails.
- **Scope errors on multi-part requests** (10 tasks; e.g. tau3-retail-72): Asked to swap one item in a pending order, the agent's modify call swaps two and charges a gift card the customer never chose. Every call is well-formed and accepted, the executed set of changes just isn't the requested set.
- **Payment method assumed** (2 tasks; e.g. tau3-retail-52): The agent finds the correct replacement camera but proposes charging the price difference to the original Visa, the customer wants PayPal and declines, so no exchange happens at all.
- **Transfer instead of decline-and-continue** (2 tasks; e.g. tau3-retail-32): One out-of-scope request (a refund for a lost tablet) triggers transfer_to_human_agents, which ends the conversation and abandons the in-scope return the customer also asked for.

What the baseline taught us: although there is variance in the results between the trials (a ±7-task single-run spread; only 15 tasks failed in all three runs), harness failures form clusters that can be identified and targeted.

## Iteration Walkthrough

**Round 1:** Baseline triage gave five causes and each got one policy section. Derive name and ZIP from username shaped handles, ask which payment method to use, keep a checklist across multi part requests, resolve exchange variants via product lookup instead of echoing the old item id, copy unchanged address fields verbatim. The gift card cause was refuted instead of fixed: gold is argument-identical to the call the backend rejects — a suspected task defect. Evaluated on `fixpack-verify`, 28 tasks, job `fixpack-v1`, 20/28, $0.96. Auth was fixed outright at 7/7, country fixed, scope and variant partial, payment not met, controls 5/5.

**Round 2:** Reading the eight survivors gave four new causes: cross-order item mapping, transfer instead of continuing, proposal framing, and constraint priority. Evaluated on `fixpack2-verify`, 17 tasks with four round 1 wins as canaries, job `fixpack2-v1`, 8/17, $0.59. The gate caught a caused regression: a new sentence suppressed legitimate modifications and flipped the canaries. Two hypotheses were also refuted here, and two diagnoses from the LLM analyst verified against raw records and were merged.

**Round 3:** The round 2 failures all pointed at that one paragraph, so I rewrote it with proper scoping: one return or exchange for delivered orders, address and payment changes before the item change that locks a pending order. The analyst's diagnoses added a rule to enumerate all orders and a guard against contradicting tool output already fetched. Same 17-task set, 14/17 (`fixpack3-v1`, $0.62). The broken tasks came back and four passed for the first time. That looked converged so I ran the full split, 97/114 (job `final`, $3.44) against the 74.3% baseline average. Comparing to the baselines, 18 tasks flipped to passing and 5 the other way. Three of those look like normal run-to-run noise. Two were my fault: the variant guidance never said unmentioned options should stay the same, so the agent started changing them. That was a one-sentence fix.

**Round 4:** Looking across all the remaining failures, every one was a well-formed call with wrong content — wrong order, wrong variant, duplicate write — the same mistake classes recurring no matter how much policy text I added. So the one code change: a validator in the harness that checks each write before it goes out (order status, item-to-order mapping, variant exists, payment method on file, duplicates) and bounces bad calls back with a hint, three bounces max. The first test run scored 15/27 (`round4-v1`, $0.92) and the compare gate flagged tasks it should not have touched. The bug was mine: the validator counted its own bounced calls as real writes and rejected the retry as a duplicate. One line fix and a regression test later, the same set went 20/27 (`round4-v2`, $0.95). The full run came in at 96/114 ($3.71), under the bar of 104 I had set.

**Rounds 4.2 to 4.5:** The round 4 full run's gate output showed what the validator itself was costing. Three tasks got stuck retrying an identical rejected call until the model concluded its exchange budget was spent, one task lost an address change to country formatting, and later runs surfaced a false bounce loop on a piece count the customer never said verbatim, a stall on a compound two-order confirmation, and silent guessing when several variants matched. Each got a targeted repair: bounces now name the order that actually holds the item and say nothing was consumed, country names are canonicalized, a repeated identical call counts as confirmation, one confirmation and one call per action per order, and ambiguous variants get listed with a question instead of a guess. The check set went 20/24 and the full acceptance run hit 101/114 (`round42-full`, $3.77), the best so far. The last two repair passes came from transcript reads alone and were verified inside the final matched runs, `final-r44-1` at 94/114 ($3.85) and `final-r45-1` at 98/114 ($3.82).

A few things existed but never drove a decision. The per-call duration field was never populated, the confidence numbers on labels were never read, and nothing from the LLM analyst went into the harness without checking it against the raw records first.

## Tooling

**Parser:** Reads a Harbor job directory. Produces `runs/<job>/tasks.jsonl`, one record per trial with turns, tool calls, errors and cost, plus a markdown transcript per trial. Run with `uv run python -m analysis.parse_traces results/tau-retail/<job>`. Refuses to write output unless its totals match Harbor's own accounting.

**Triage:** Reads `tasks.jsonl`, each task's gold config, and the retail tool source. Produces labels per failed trial and a ranked group table in `runs/<job>/triage/groups.json`. Run with `uv run python -m analysis.triage runs/<job>`. Grouping is deterministic. Rejected calls map to keys derived from the tool source, successful writes are diffed against gold writes argument by argument, matched by order id, and unexplained failures are checked against expected communicated strings. Identical labels across tasks form a group. Example: task 91 gets `wrong_args.exchange_delivered_order_items.new_item_ids.wrong_variant` — agent sent 9494281769, gold expects 7609274509, same product. This ranking picked round 1's targets.

**CLI:** `uv run python -m analysis.cli groups runs/<job>` prints the group table, `--by-tool` rolls singleton groups into per-tool families. `show` lists a group's transcripts and the turns to read. `compose` freezes a task set from groups or causes plus seeded control tasks for later identical re-runs. `run` prints the harbor command and only executes with `--yes`. `compare` diffs two runs (fixed/regressed/still-failing with label transitions) and exits non-zero when a regression's labels are new for that task and fall in a tool family the harness diff touched — this gate caught every caused regression in the campaign.

**Ledger:** `uv run python -m analysis.experiments record results/tau-retail/<job>` appends score, cost, models, seed and a content hash of the harness directory to `experiments.jsonl`. `list` groups runs by harness hash and averages full runs. Every score in this writeup comes from this file.

**LLM analyst:** Drafts cause entries from groups and transcripts into `causes_proposed_llm.json`. Nothing was applied without checking the cited transcripts; two of its diagnoses verified against raw records and were merged.

Unused: the per-call duration field and the label confidence numbers were recorded but never drove a decision.

## Final Validation

Final harness (`round 4.5`; content digest `bbf3a21…`, the version with every mechanism verified): three matched full runs, same models, task set, seed and concurrency as the baselines. (Note: the instructions suggest one job with `--n-attempts 3`; I ran three separately-named jobs instead — identical config per the ledger's harness hash and settings — because my per-job analysis pipeline treats attempts as unordered runs. Same evidence, different packaging.)

| Run | Score | Mean | Cost (agent+user) |
|---|---|---|---|
| baseline / -2 / -3 (stock) | 83 / 82 / 89 | **0.743 avg** | $9.77 |
| final-r45-1 / -2 / -3 | 98 / 99 / 101 | **0.871 avg** | $11.28 |

Task-level: 10–12 of the 15 tasks that failed in all three baseline runs pass in each matched final (only 38 and 105 fail in all three finals); the persistent residue is two suspected task defects (52: the sim refuses the gold exchange under every framing tried, 0/7 lifetime; 105: gold's exchange is argument-identical to the call the backend rejects for insufficient gift-card balance — paid 2×$94.80, replacements $210.70, balance $17.00, no other payment method), two sim/gold wobbles (38: sim explicitly says "ordered by mistake" where gold wants "no longer needed"; 41: sim picks the credit card, gold wants PayPal, the scenario states no preference), and the variant-ambiguity trio (49/60/79) where enumerate-and-ask did not reliably convert.

Run-to-run noise: single runs swing ±7 tasks (measured on the stock harness: 83/82/89), so every fix in this work was judged on matched runs or frozen subsets with never-failed control tasks, and each run passed an automated compare gate before acceptance. Results not reproducible on demand: the rotating flake tier (~10–17 tasks per run, documented per-run in the ledger), and round-4.2's single 101/114 — a lucky draw of a variant with known deterministic failure loops, deliberately not certified (the ledger keeps it on the record).

Unresolved and next steps: the 49/60/79 ambiguity family (next attempt: have the validator enumerate the qualifying variants inside its bounce message); the communicate-proxy watch-items (tasks 29, 2); reporting tasks 52 and 105 upstream as suspected task defects.

Total spend: $40.84 agent+user across all 18 runs (from the ledger), ~$29 Modal (10 full runs + subsets at the reference rate), plus provider-side gpt-5.2 verifier usage not visible in trace costs — approximately $80 all-in against the $100 budget.

## AI Assistance

AI tools used: Claude (Claude Code CLI) as the coding and analysis assistant, including parallel subagents for isolated module builds and a clean-room subagent as the LLM analyst. The pipeline was designed by hand (with some brainstorming using AI) from the data classes to the end-to-end data flow. All the harness changes were also proposed by hand with some assistance in analysis with AI (as mentioned in the Causes step). All code was written with the assistance of AI through well-thought-out prompts.

AI output was never fully trusted without verification:

- Code: every module ships with tests. I reviewed all the diffs before committing.
- Analyst causes: drafted entries carry a status lifecycle, nothing runs until I confirm it against the cited transcripts.
- Spend: I put a statement in my Claude memories file to never run an e2e test that would use the model budget without explicit approval from me.
