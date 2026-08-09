# Tau-2 take-home writeup

## Overview

(to be written)

## Baseline and failure analysis

I ran the stock harness three times with identical config (gpt-5.6-luna agent,
gpt-5.6-sol user, seed 300, concurrency 60; `results/tau-retail/baseline`,
`baseline-2`, `baseline-3`): 83/114, 82/114 and 89/114 passed (72.8%, 71.9%,
78.1%; average 74.3%), $9.76 agent+user cost total, zero exceptions and zero
retries. Two facts from these runs set the methodology. First, matched runs
disagree on up to 23 individual tasks (pairwise flip counts 23/20/17 — the
user sim runs at temperature 1), so I never trusted a single-run comparison;
every later fix is judged on matched runs or frozen subset re-runs against the
74.3% average. Second, the failures are two very different populations: 45
distinct tasks failed at least once, but only 15 fail in all three runs (the
deterministic core) while 30 fail only sometimes and 69 always pass. Fix
effort targets the core; the sometimes-tier only moves averages.

Triage put the same two groups at the top of all three runs: the agent never
performs an expected exchange (`missing_action.exchange_delivered_order_items`,
4–7 tasks per run) and exchanges resolved to the wrong variant of the right
product (4–5 tasks per run); by tool, exchanges are involved in 11–13 of each
run's failures. Reading the flagged transcripts turned the groups into causes
(full evidence in `analysis/INVESTIGATION.md` and `analysis/causes.json`): an
authentication dead-end where the customer offers only a username that encodes
their name and the agent refuses to derive name+ZIP from it, ending with zero
tool calls (7 tasks: tau3-retail-5, -8, -67); wrong-variant resolution, where
the agent echoes the old item id instead of matching the requested options
(5 tasks: tau3-retail-91, -107, -18); scope errors on multi-part requests —
dropped, extra, or mis-typed actions (10 tasks: tau3-retail-27, -72, -104);
assuming the original payment method for price differences (tau3-retail-52,
-98); rewriting untouched address fields non-canonically ("United States" for
"USA", tau3-retail-59); mapping items to the wrong order in multi-order
conversations (tau3-retail-27, -93); and a transfer-to-human on one
out-of-scope request that abandoned the remaining in-scope work
(tau3-retail-32, -59).

These causes cover 14 of the 15 deterministic-core tasks. The exceptions are
honest gaps: tau3-retail-38 fails every run (a guessed email that hits
user-not-found, plus a never-communicated refund amount) but I could not pin a
single root cause; and tau3-retail-105 I investigated and refuted as an
agent-side failure — the gold action is argument-identical to the call the
backend rejected for insufficient gift-card balance.

## Iteration walkthrough

(to be written)

## Tooling

(to be written — must quote the same noise floor: up to 23 task flips between
matched stock runs)

## Final validation

(to be written)

## AI assistance

(to be written)
