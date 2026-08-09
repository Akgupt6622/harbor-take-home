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

## Step 6 — Fix #1: cause auth_username_deadend (first harness change)

**Change:** one paragraph added to the authentication section of
`benchmarks/tau3-bench/harness/retail/policy.md` — when the user offers a
username-shaped handle (firstname_lastname_NNNN) plus ZIP, derive the name from
the handle and authenticate via name + zip; never end the conversation
unauthenticated while a username and ZIP are available. No other harness edits;
auth still goes through the sanctioned name+zip path.

**Verification set (pre-registered before any run):**
`uv run python -m analysis.cli compose runs/baseline authfix-verify
--cause auth_username_deadend --controls 5 --seed 7
--controls-from runs/baseline,runs/baseline-2,runs/baseline-3`
→ 12 tasks: members 5, 6, 7, 8, 9, 67, 68 + stable-pass controls 12, 24, 60,
76, 111 (passed in all three baselines; seed 7). Frozen copy:
`analysis/tasksets/authfix-verify.json`.

**Success criterion (registered in causes.json before running):** ≥5 of 7
members pass, including ≥3 of the deterministic four (5, 8, 9, 67), and 5/5
controls pass, in one subset run. Post-auth conversations are unexplored
territory — members may newly hit downstream causes (revealed, not caused;
judged via label transitions).

**Run command (pending approval):**
`./run_subset.sh authfix-v1 analysis/tasksets/authfix-verify.txt`

## Step 7 — Fixes #2–#5 applied; cause F refuted as a benchmark defect

**Cause F resolved by evidence, not a fix:** task 105's gold action is
argument-identical to the call the backend rejected. DB ground truth: paid
2×$94.80, replacements $210.70 (+$21.10 difference), gift card balance $17.00 —
the runtime forbids the exchange the grader assumes succeeded. The agent's
handling was correct (explained the shortfall; no alternative method exists on
the account). Suspected unsolvable task; documented for the writeup's
unresolved-issues section; registry status → refuted. Task 71 removed from F.

**Harness changes (policy.md, one section each):**
- B `exchange_payment_method_assumed`: ask which payment method to use for
  price differences/refunds; never assume the original.
- E `multi_request_scope_errors`: keep a checklist across multi-order
  requests; only user-requested items in item lists; swaps are exchanges or
  item modifications, never returns.
- C `exchange_wrong_variant_resolution`: resolve replacements via
  get_product_details to the variant matching the requested options; never
  echo the current item id or guess.
- D `address_country_not_canonical`: copy unchanged address fields verbatim
  from the stored record.

**Combined verification set (pre-registered):** `fixpack-verify` — 23 members
across causes A–E + 5 stable-pass controls (2, 30, 83, 85, 92; seed 11),
frozen at `analysis/tasksets/fixpack-verify.json`. Per-cause success criteria
registered in causes.json before running. A single subset run evaluates all
five fixes: per-task outcomes attribute to causes because member sets are
disjoint.

**Run command (pending approval):**
`./run_subset.sh fixpack-v1 analysis/tasksets/fixpack-verify.txt`
(28 tasks ≈ $1.40–1.80, ~6–8 min; auto-parses, auto-records under the new
harness digest.)

## Step 8 — fixpack-v1 results (harness digest 48910ac73d57)

**Run:** `./run_subset.sh fixpack-v1 analysis/tasksets/fixpack-verify.txt` —
28 tasks, 20 passed, 0 exceptions, 4m52s, ~$0.83 agent+user. Controls 5/5.

**Per-cause verdicts against pre-registered criteria:**
- **A auth_username_deadend: FIXED — 7/7 members pass** (criterion ≥5/7). The
  username-derivation paragraph fully unlocked the cluster. Status → fixed.
- **D address_country_not_canonical: FIXED per criterion** — 59's
  `wrong_args…country` label is gone; the task still fails, but now only on
  `missing_action.cancel_pending_order` (an E residual). Status → fixed.
- **E multi_request_scope_errors: criterion MET (3 of 5)** — 31, 72, 104 pass;
  27 and 32 still fail. Partial.
- **C exchange_wrong_variant_resolution: criterion NOT met** — 18, 45, 107
  pass, but 91 and 99 fail. 91 is a *transition*, not a persistence: its
  exchange is now correct (wrong_variant label gone) and it fails on a return
  missing one item (scope). 99's wrong_variant persists.
- **B exchange_payment_method_assumed: NOT met** — 52 still
  `missing_action.exchange` (98 passed, informational). Needs a fresh read.

**Remaining failures and their new diagnoses:** 27 (still exchanges a
non-delivered order + returns instead), 32 (misses TWO cancels + a return),
52 (still refuses the exchange), 59 (cancel only), 91 (return missing an
item), 93 (cross-order confusion), 99 (variant), 112 (address modify missed).

**Decision:** revealed-vs-caused analysis says nothing was caused (controls
clean, every remaining failure is a pre-existing cause or a downstream reveal).
Next: read the four fresh transcripts (52, 27, 93, 99) before any further
spend; then fix round 2 and a full matched run.

## Step 9 — Round-2 analysis (human) and fixes #6–#9

Fresh reads of all 8 fixpack-v1 survivors (evidence-turn guided) produced four
new confirmed causes — registered in causes.json with fixpack-v1 transcripts:

- `proposal_framing_leads_with_downside` (52): fix B worked mechanically (gold
  item offered, payment choice given) but the proposal led with the drawback
  and the rigid sim declined. Fix: frame the best option on the user's primary
  goal.
- `cross_order_item_mapping` (27, 93): 27's return was placed on the order
  that needed the exchange — the status flip then blocked the gold exchange;
  93 exchanged on the wrong order. Fix: verify item-to-order mapping; treat
  same-order return+exchange as a mapping error.
- `constraint_priority_variant_selection` (99): ranked preferences resolved to
  the wrong variant. Fix: hard constraints first, then preferences in order.
- `transfer_instead_of_decline_and_continue` (32): one out-of-scope request
  (lost-item refund) triggered a transfer that abandoned two cancels and a
  return the sim never got to voice. Fix: decline and continue; transfer only
  with nothing serviceable left.

Plus checklist strengthening for the E residuals (91's dropped return item,
112's missed address change, 59's missing cancel).

**Verification set:** `fixpack2-verify` — 12 members (incl. round-1 winners
31/72/104/110 as natural canaries) + 5 stable-pass controls (2, 4, 23, 40, 54;
seed 13). Per-cause criteria pre-registered. In parallel, the LLM analyst is
running blind in a clean room over the same 8 survivors; its proposals will be
compared against this table as its known-answer debut before merging.

**Run command (pending approval):**
`./run_subset.sh fixpack2-v1 analysis/tasksets/fixpack2-verify.txt`

## Step 10 — fixpack2-v1: the controls catch a caused regression; analyst merge

**Run:** 8/17 (harness a0a2c0c69023). Criteria: transfer fix ✓ (32 passes,
status → fixed); mapping half ✓ (27 passes; 93 persists — needed the
enumerate-all-orders mechanism); 91 ✓. But canaries 72/104/110 (round-1 wins)
and stable control 2 FAILED, all missing_action.modify_* — the round-2
"blocks any further state-changing action" sentence read as "avoid multiple
writes per order" and suppressed legitimate modifications. This is a CAUSED
regression, detected exactly as designed (canaries + controls + label
transitions).

**Adjudications from the run + analyst comparison:**
- 52: framing hypothesis REFUTED (0/5 lifetime); adopting the analyst's
  suspected task-defect. Joins 105 on the unsolvable-suspect list.
- 99: preference hypothesis REFUTED; analyst's tool_output_misread verified
  against raw records (available:true vs "unavailable").
- Analyst debut scorecard: agreed 4/8, beat the human table on 4/8 (59's
  transfer, 91/99 misreads, 112 lock-ordering); its two boldest claims
  verified in raw data. Output preserved verbatim in causes_proposed_llm.json;
  reviewed entries merged (tool_output_misread, modify_ordering_and_lock).

**Round 3 (commit 78dd86a):** rewrote the harmful paragraph with correct
scoping (delivered = one return-or-exchange; pending = address+payment+items
with address/payment BEFORE the locking item-modify), added enumerate-all-
orders (93) and the misread guard (91/99).

## Step 11 — fixpack3-v1: matched A/B confirms round 3 (8/17 → 14/17)

Identical task set, harness 3f510fcd8816. Regression fully repaired for
72/110/control-2 (all re-flipped to pass); first-ever passes for 99 (misread
guard), 93 (enumerate-all-orders), 112 and 59 (lock-ordering). Round-1/2 wins
27/31/32 held; all 5 controls pass. Remaining: 104 (address modify still
dropped — partial residual, parked), 91 (unstable across harness versions —
new messy failure shape after passing fixpack2), 52 (task-defect suspect,
0/6 lifetime; note it now attempts the exchange but with wrong variant).

**Decision:** subset iteration has converged (3 fixed causes this round;
remaining failures are one residual, one unstable, one suspected defect).
Next: matched full runs of the final harness for the writeup's required
baseline-vs-final comparison.

## Step 12 — Final harness, full run 1: 97/114 (mean 0.851)

vs. stock baseline 83/82/89 (avg 0.743): +14 tasks over the average, +8 over
the best baseline run. Zero exceptions/retries; all parser cross-checks hold.
8 of the 15 deterministic-core tasks now pass (5, 8, 9, 27, 32, 67, 72, 107).
17 failures: 12 from the known ever-failed pool (incl. defect-suspects 52,
105) and 5 first-time failures (1, 49, 60, 81, 111) — all with pre-existing
failure shapes, 111 a known fragile pass: consistent with flake-tier rotation,
no caused-regression signature. Watch-item: modify-family failures (104, 110,
111) across the remaining matched finals. Single-run caveat: the ±7 noise
floor means the required 3-run matched average is what the writeup reports.

## Step 13 — Final-run transition analysis; one caused regression found and fixed

**Fixed (failed ≥2/3 baselines, now pass) — 18 tasks:** the full auth cluster
(5/6/7/8/9/67/68), mapping (27, 93), variants (18, 45, 99, 107), transfer (32),
lock-ordering (71, 72), plus 10 and 20.

**Regressed (passed 3/3 baselines, now fail) — 5 tasks:** 1, 81, 111 carry
pre-existing shapes (flake rotation; 111 was a fragile pass). 49 and 60 are a
CAUSED regression with a verified mechanism: the variant guidance said "match
the requested options" without constraining unmentioned ones — both tasks
picked blue/8h/IPX4 (8555936349) where gold preserves the current item's
unmentioned options. Fixed with one sentence (unmentioned options stay
identical); registered as variant_unmentioned_options_drift. Verification
rides with the later triple-final.

**Still failing (12):** defect-suspects 52/105 (105 now doesn't attempt the
impossible exchange); 91 unstable (third distinct failure shape); 98 improved
(variant component gone, payment remains); 38/59 show auth wobbles
(user_not_found) plus known misses; 31/64/110/104/112/41 shuffle within known
shapes — candidates for the flake tier or one more targeted pass after the
triple-final.

## Step 14 — Harness round 4: write validator + checklist consolidation

Structural change (commits 11d204b, 3575fbf): agent-side write validator (5
mechanical checks, bounce-with-guidance, 3-bounce cap, import-guarded) wired
at the only genuinely-loaded harness point (the adapter aliases most harness
modules to tau2 — llm_agent is live); policy consolidated into one pre-write
checklist with explicit defaults, plus 10 evidence-cited residual fixes.
Registry corrected by round-4 evidence: 41 and 38 are sim/gold wobbles, 110
was a graded no-op write (perform-anyway rule), 64's gold contradicts strict
option preservation (V3 confirm-turn covers it).

**Pre-registered verification (round4-verify, 27 tasks, frozen before any
run):** members = 9 residuals + 6 regressed re-checks + 7 stratified canaries
(one per validator mechanism: 8 auth, 27/93 mapping, 99/107 variant, 72 lock,
32 transfer); controls = 5 stable passes (seed 17). Criteria: zero caused-
signature regressions in `cli compare runs/final runs/round4-v1` with full
history; all 7 canaries pass; >=4 of {91, 98, 104, 110, 112} flip; both {49,
60} re-flip; {1, 64, 81, 111} and {38, 41} informational (flake/wobble
calibration); controls 5/5. Then stage 2: full-run acceptance, then the
triple-final.

## Step 15 — round4-v1: validator phantom-write bug found by compare gate

round4-v1 (15/27): 6 real fixes (31, 91, 104, 110, 111, 112 — the modify/scope
residual family), all 7 canaries' mechanisms held where untouched, controls
5/5. But compare exited 1: 99 caused-signature + 49/60/98/8/72 churned or
regressed to missing_action shapes. Evidence (99 turns 31-38): the validator
counted its own BOUNCED calls as executed writes — bounce results are
error=False by design, so collect_facts entered them into successful_writes;
the retry then hit a phantom "return or exchange already submitted" (V4a) and
the agent dead-ended into transfer. Bounces did fire (8 trials show VALIDATOR:
text in LLM span inputs; runner stdout does not carry them). One-line fix:
tool results starting with "VALIDATOR:" never enter write history; regression
test added (109 tests). Re-verification on the identical frozen set follows.

## Step 16 — round4-v2: phantom fix verified, gate clean (15/27 → 20/27)

Matched A/B on the frozen set: FIXED 1, 8, 72, 81, 99; zero regressions;
compare exit 0. All round4-v1 wins held; canaries and controls clean.
Pre-registered criteria: flips 4/5 met (98 persists), canaries met, controls
met; {49, 60} option-drift pair did not re-flip — the one open agent-side
problem (V3 confirm-turn not converting). 38/41 informational as registered.
Projection: ~105/114 on the full split. Next: stage-2 full acceptance run.
