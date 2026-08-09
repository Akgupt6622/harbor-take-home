# Tau-2 take-home writeup

## Overview

(to be written)

## Baseline and failure analysis

**Setup.** I evaluated the unmodified harness on all 114 retail tasks, three
times, under identical configuration: agent gpt-5.6-luna, simulated user
gpt-5.6-sol, seed 300, concurrency 60 on Modal. The job directories are
`results/tau-retail/baseline`, `baseline-2`, and `baseline-3`.

**Results.** The runs passed 83, 82, and 89 of 114 tasks (mean pass rate
74.3%). All 342 trials completed with no exceptions and no retries. Total
agent-and-user cost for the three runs was $9.76.

**Variance.** Aggregate scores differ by at most 7 tasks, but per-task
outcomes vary much more: each pair of runs disagrees on 17–23 individual
tasks, because the simulated user samples at temperature 1. Across the three
runs, 45 distinct tasks failed at least once. Fifteen failed in all three
runs — I call these the *deterministic core*. Thirty failed in one or two
runs, and 69 passed in all three. This gives two methodological rules used
throughout: fixes target the deterministic core, and no change is judged on a
single run — only on matched runs or frozen subset re-runs.

**Failure causes.** Automated triage produced the same ranking in all three
runs: exchange handling dominates, involved in 11–13 of each run's failures.
Reading the flagged transcripts reduced the failures to seven causes (evidence
per task in `analysis/INVESTIGATION.md` and `analysis/causes.json`):

1. Authentication dead-end — the customer offers only a username that encodes
   their name (e.g. `mei_kovacs_8020`); the agent refuses to derive name+ZIP
   from it and the conversation ends with zero tool calls. 7 tasks
   (tau3-retail-5, -8, -67).
2. Wrong variant on exchange — the agent echoes the old item id instead of
   resolving the requested options to the correct sibling variant. 5 tasks
   (tau3-retail-91, -107, -18).
3. Scope errors on multi-part requests — dropped, extra, or wrong-typed
   actions. 10 tasks (tau3-retail-27, -72, -104).
4. Payment method assumed for price differences instead of asked.
   2 tasks (tau3-retail-52, -98).
5. Address fields rewritten non-canonically ("United States" for "USA").
   1 task (tau3-retail-59).
6. Items mapped to the wrong order in multi-order conversations.
   2 tasks (tau3-retail-27, -93).
7. Transfer-to-human on one out-of-scope request, abandoning remaining
   in-scope work. 2 tasks (tau3-retail-32, -59).

**Coverage.** These causes account for 14 of the 15 deterministic-core tasks.
Two caveats: tau3-retail-38 fails every run but has no single identified root
cause, and tau3-retail-105 was investigated and excluded — its gold action is
argument-identical to the call the backend rejected, so the failure is not an
agent error.

## Iteration walkthrough

(to be written)

## Tooling

(to be written — must quote the same noise floor: up to 23 task flips between
matched stock runs)

## Final validation

(to be written)

## AI assistance

(to be written)
