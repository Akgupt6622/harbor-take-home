# Harness failure investigation — decision trace

Chronological log of every analysis step: the command run, what it showed, and
the decision it produced. All analysis uses the pipeline in `analysis/`
(parse_traces → triage → cli → experiments); benchmark runs are recorded in
`experiments.jsonl`.

## Step 1 — Establish the baseline and the noise floor

**Command:** `uv run python -m analysis.experiments list`

**Observed:** stock harness (digest `64d7d8e0372b`, commit c9ed372), three
matched full runs: **83/114, 82/114, 89/114 — avg mean 0.743**. $3.2–3.3 and
~10 min per run; zero exceptions/retries in all three.

**Decision:** run-to-run spread is ±7 tasks, so single-run comparisons are
meaningless. Every fix gets judged on matched runs / pass fractions against
avg 0.743, tracked in the ledger.

## Step 2 — Separate deterministic failures from flake

**Command:** compared the failed-task sets of the three runs' triage outputs
(`runs/baseline*/triage/groups.json`). (Ad-hoc set-diff; candidate for a
future `report` module.)

**Observed:** 45 distinct tasks failed at least once, but only

- failed 3/3 (deterministic core, 15): 5, 8, 9, 27, 31, 32, 38, 52, 59, 67, 72, 91, 104, 105, 107
- failed 2/3 (13): 6, 7, 10, 18, 20, 45, 68, 71, 93, 98, 99, 110, 112
- failed 1/3 (flake, 17): 19, 28, 33, 36, 39, 41, 43, 46, 47, 48, 56, 64, 66, 74, 97, 100, 109

**Decision:** fixes target the deterministic core first, then the 2/3 tier.
The 1/3 tier is temperature noise — no fix effort, it only moves the average.

## Step 3 — Locate failure concentration

**Command:** `uv run python -m analysis.cli groups runs/<job> --by-tool` and
`... groups runs/<job>` for each baseline run.

**Observed:** `exchange_delivered_order_items` is the #1 family in all three
runs (11–13 tasks, n_solo 10–11). The same two groups top every run:
`missing_action.exchange_delivered_order_items` (4, 5, 7 tasks across runs)
and `wrong_args.exchange….new_item_ids.wrong_variant` (4, 5, 4 tasks).

**Decision:** dig the exchange family's evidence first.

## Step 4 — Read exemplars of missing_action.exchange

**Command:** `uv run python -m analysis.cli show runs/baseline
missing_action.exchange_delivered_order_items`, then read the referenced
transcripts (8, 9, 52).

**Observed:**
- Tasks 8, 9: **zero tool calls.** User can't recall the account email, offers
  username `mei_kovacs_8020` + ZIP 28236; agent refuses ("username and ZIP
  aren't sufficient") and never notices the username encodes the name
  Mei Kovacs. Sim ends with `###OUT-OF-SCOPE###`.
- Task 52: agent authenticated fine, found the correct max-zoom variant
  (9228757377 — exactly what gold expects), but proposed it with the
  **original Visa** for the price difference; the user wanted PayPal (gold's
  `payment_method_id` is `paypal_8194385`) and declined; no exchange happened.

## Step 5 — Pull gold expectations for the whole deterministic core

**Command:** dumped `labels[].explanation` for the 15 core tasks from
`runs/baseline/tasks.jsonl` (explanations carry agent-vs-gold values).

**Observed (selected):**
- 5, 8, 9 share order `#W6390527` — same user (mei_kovacs) as the auth
  dead-end; task 5's return also never happened.
- 67: zero calls; `missing_communication.829_43` — transcript shows the same
  auth dead-end (user gives "Noah" + ZIP + username `noah_ito_3850`; agent
  demands the last name that the username encodes; sim bails).
- 59: `wrong_args.modify_pending_order_address.country` — agent wrote
  `'United States'`, gold expects `'USA'` (the DB's canonical value).
- 72: `.extra_items` on both item lists (user asked to modify one item, agent
  modified two) + wrong payment method.
- 27: returned two items but the task wanted an exchange; also attempted an
  exchange on a non-delivered order (rejected by the backend).
- 105: attempted the right exchange, rejected —
  `insufficient_gift_card_balance_to_pay_for_the_price_difference`.
- 91, 107: `.wrong_variant` — right product, wrong item id (91: echoed the
  user's existing item id instead of resolving the equivalent variant).

## Cause table (as of three baseline runs)

| # | Cause | Core tasks | 2/3-tier tasks | Implied harness fix |
|---|---|---|---|---|
| A | **Auth dead-end**: user offers username (+ partial name) + ZIP; agent refuses instead of deriving first/last name from the username and authenticating via name+zip | 5, 8, 9, 67 | 6, 7, 68 | Prompt: when a customer offers a username/user-id-shaped handle, parse name parts from it and attempt `find_user_id_by_name_zip`; usernames are `first_last_NNNN` |
| B | Payment-method assumption when proposing exchanges (offers original card; user wanted another method) | 52 | 98 (paym.) | Prompt: ask which payment method to use for price differences instead of assuming the original |
| C | **Wrong variant on exchange**: agent echoes the old item id / picks wrong sibling variant instead of resolving requested options via `get_product_details` | 91, 107 | 18, 45, 99 | Prompt: exchanges must look up the product's variants and select the item id matching the requested options; never reuse the old id unless options are identical |
| D | Value normalization: address fields rewritten in non-canonical form (`United States` vs `USA`) | 59 (partial) | — | Prompt: when modifying addresses, copy unchanged fields verbatim from the existing record |
| E | Scope errors on multi-part requests: extra items modified, wrong action type, one of several orders missed | 27, 72, 104, 31, 32 | 93, 110, 112 | Needs per-task reads; likely running-checklist guidance |
| F | Gift-card balance rejection unhandled (right action, no fallback offered) | 105 | 71? | Read 105; likely policy-handling guidance for insufficient balance |

**Decision after Step 5:** Fix #1 = **Cause A** — largest deterministic unlock
(4 core + ~3 tier-2 tasks), crisp one-sentence prompt change, trivially
verifiable (the failures share one shape: zero calls, auth refusal).
Fix #2 candidate = Cause C (same-product variant resolution).

## Tooling gap noted

`cli compose` builds task sets from *groups*, but Cause A spans four symptom
groups (its tasks died before producing tool-level symptoms). Composing a
cause-level set requires either a hand-written task list for `run_subset.sh`
or a `compose --tasks` extension. → decide before fix #1 verification.
