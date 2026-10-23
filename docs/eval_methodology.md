# Evaluation methodology

## Evaluation status

The agent-drafted candidate set contains 80 stipulated scenarios: eight rule-behavior families
with ten cases each. The facts describe synthetic examples, not observed BTS flights or
passenger records. `source_of_truth` records the primary authority used for the expected
rule result and explicitly marks scenario facts as stipulated. The golden-set loader
checks each row, unique case IDs, the exact eight family names, and ten cases per family.

The current end-to-end adapter runs the production `AnswerService` path over the
synthetic cases without an LLM or cloud service. The current measured report is
[`data/eval/results/local-eval-v2.json`](../data/eval/results/local-eval-v2.json).
All 80 cases are scorable against the official local corpus. Judge repeatability
remains pending because no external judge was selected or run twice.

Any `1.0` values asserted by tests are resolver/golden self-consistency checks only; they
must not be presented as agent accuracy or end-to-end quality results.

## Families and cases

| Family | Cases | Main boundary |
|---|---:|---|
| `domestic_cancel_controllable` | 10 | Carrier-caused domestic cancellations and disposition |
| `domestic_cancel_weather` | 10 | Refund entitlement despite weather cause |
| `domestic_delay_over_3h` | 10 | Domestic 180-minute arrival threshold |
| `domestic_delay_under_3h` | 10 | Domestic negative cases, including 179 minutes |
| `international_delay_6h` | 10 | International 360-minute boundary, including 359 minutes |
| `enforcement_posture` | 10 | Renumbering scope at notice, gap, extension, and refresh boundaries |
| `downgrade_added_connection` | 10 | Non-clock significant-change limbs |
| `insufficient_facts` | 10 | Missing facts that leave eligibility unresolved and require refusal |

The scenario objects use the same fact names consumed by the resolver where possible.
Expected entitlement labels use the locked `EntitlementStatus` values. Expected
posture includes the checked bit, regime ID, applicable notice IDs, and an explicit
`obligation_preserved` assertion. This makes the July 1–6, 2026 gap distinguishable
from both the pause and an unchecked lookup. The expected governing clauses are kept
separate from Federal Register posture notice IDs.

The rule authority in `source_of_truth` points to the [current eCFR Part 260](https://www.ecfr.gov/current/title-14/chapter-II/subchapter-A/part-260)
and, for posture cases, to the primary Federal Register notices documented in
[`source_verification.md`](source_verification.md). Synthetic scenario details are
stipulated inputs, not independently verified flight records. The set does not test
amenities or airline-specific contract promises; those expected values would need
reviewed contracts and policy evidence before being added.

## Answer metrics

`core.eval.evaluate_answers` requires exactly one prediction per case and reports:

- Entitlement accuracy overall and separately for each family.
- Posture exact-match accuracy for status, regime ID, checked state, and notice IDs;
  obligation-preserved assertion; the headline posture-family score is also reported
  separately.
- Obligation/enforcement conflation events. In expected-eligible, expected-discretion
  cases, a not-eligible answer or an explicit claim that the pause extinguishes the
  refund obligation is a zero-tolerance error.
- Micro citation precision and recall against each case's expected governing clauses.
- Refusal accuracy, including the expected refusal reason when one is specified, and
  over-refusal rate among families 1–7.

An answer adapter must set `claims_refund_not_owed_due_to_posture` when the generated
answer makes that claim; the evaluator also flags a not-eligible label for the
otherwise-eligible narrow-scope posture scenarios. Entitlement scoring and citation
scoring remain separate so that a right conclusion with unsupported citations cannot
pass as grounded.

## Retrieval and calibration

`retrieval_metrics` accepts ranked clause IDs and computes mean per-case recall@10 and
MRR against `governing_clauses`. `retrieval_ablation` requires caller-supplied rankings
for all four modes—BM25, vector, hybrid, and hybrid plus reranking—and returns the same
metrics per mode. These functions do not run a retriever or invent ablation values.

`calibration_metrics` accepts held-out binary labels and predicted probabilities and
returns Brier score, expected calibration error, and reliability bins. Run it separately
for calibrated and uncalibrated probabilities and label the source split. Do not report
the synthetic delay fixture as BTS model quality. The model training module itself
marks validation-fold calibration metrics as in-sample fit diagnostics and identifies
its chronological test split separately.

## Judge repeatability and utility

The judge rubric is a normalized 0–1 utility score with fixed weights: factual accuracy
0.40, citation support 0.30, completeness 0.20, and clarity 0.10. Before citation
precision becomes a CI gate, use the same judge, prompt, and rubric to score the same 20
held-out answers twice. `judge_variance` requires the same 20 case IDs and reports mean,
95th-percentile, and maximum absolute score differences plus differences greater than
the declared material threshold. Do not silently convert judge disagreement into a
binary answer label.

Judge repeatability result: **pending human review; no judge has scored these cases
twice.** No variance or judge-derived quality result is claimed.

## Cold re-derivation

Twenty cases must be independently re-derived without showing the reviewer the expected
answers. Record the derived entitlement, posture, clause IDs, and refusal decision;
then compare those values with the agent-drafted candidate answer. `cold_rederivation_result`
requires exactly 20 matching IDs and reports the disagreement count and rate. A zero
disagreement rate should prompt scrutiny rather than an automatic pass.

Cold re-derivation status: **complete** for the 20 scenarios in
`data/eval/blind_review_packet.jsonl`. An independent reviewer adjudicated the packet
before consulting the expected answers. A safe comparison keyed each packet scenario
to the golden candidate by its stipulated flight identifier, then compared entitlement,
enforcement status and regime, applicable notice IDs, governing clause sets, and refusal.
The result was **0 disagreements out of 20 (0%)**. Exact disagreements: **none**. The
independent rationales, reviewer identifier, and input provenance are recorded in
`data/eval/blind_review_results.jsonl`. This measures agreement with the agent-drafted
expected answers for this sample; it is not an agent accuracy or legal-outcome measure.

| Packet ID | Golden case | Independent entitlement | Enforcement posture / regime | Governing clauses | Refusal | Disagreement |
|---|---|---|---|---|---|---|
| blind-001 | gs-002 | eligible | active / notice_FR-2025-22140_scope_not_met | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-002 | gs-004 | eligible | active / notice_FR-2025-22140_scope_not_met | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-003 | gs-007 | eligible | active / notice_FR-2025-22140_scope_not_met | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-004 | gs-012 | eligible | no published carveout / active_no_discretion_notice | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-005 | gs-014 | eligible | no published carveout / active_no_discretion_notice | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-006 | gs-017 | eligible | no published carveout / active_no_discretion_notice | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-007 | gs-022 | eligible | no published carveout / active_no_discretion_notice | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-008 | gs-024 | eligible | no published carveout / active_no_discretion_notice | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-009 | gs-027 | eligible | no published carveout / active_no_discretion_notice | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-010 | gs-032 | not eligible | no published carveout / active_no_discretion_notice | 14CFR260.2 | no | none |
| blind-011 | gs-034 | not eligible | no published carveout / active_no_discretion_notice | 14CFR260.2 | no | none |
| blind-012 | gs-037 | not eligible | no published carveout / active_no_discretion_notice | 14CFR260.2 | no | none |
| blind-013 | gs-062 | eligible | no published carveout / active_no_discretion_notice | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-014 | gs-067 | eligible | no published carveout / active_no_discretion_notice | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-015 | gs-052 | eligible | discretion / paused_FR-2025-22140 | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-016 | gs-057 | eligible | discretion / paused_FR-2026-13675 | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |
| blind-017 | gs-072 | unresolved | no published carveout / active_no_discretion_notice | 14CFR260.3; 14CFR260.6(a)(1) | yes | none |
| blind-018 | gs-077 | unresolved | no published carveout / active_no_discretion_notice | 14CFR260.2 | yes | none |
| blind-019 | gs-042 | not eligible | no published carveout / active_no_discretion_notice | 14CFR260.2 | no | none |
| blind-020 | gs-047 | eligible | no published carveout / active_no_discretion_notice | 14CFR260.2; 14CFR260.6(a)(1); 14CFR260.6(a)(2) | no | none |

## Operational metrics

`operations_metrics` consumes measured per-query latency, cost, tool-error, and optional
cache-hit records. It reports p95 latency, mean cost/query, tool-error rate, and cache-hit
rate with its observed denominator. Missing cache observations are reported as `null`;
empty telemetry raises an error rather than yielding zeros.

The current `local-eval-v2` run records 80 answer-service calls, p95 latency about
13.9 ms, mean cost $0.00/query, tool-error rate 0%, and cache-hit rate unavailable
(no cache observations). The zero cost reflects the absence of model calls in this
deterministic path; it is not a production LLM cost estimate. User-facing results are
80/80 entitlement decisions, 80/80 posture decisions, 200/200 expected citations,
80/80 refusal decisions, and zero obligation/enforcement conflations. Retrieval is
measured for every case; hybrid-plus-rerank recall@10 is 0.7333 and MRR is 0.3777.

## CI thresholds

`evaluate_ci` applies the current gates to a measured metric payload:

| Gate | Threshold |
|---|---|
| Citation precision | At least 0.90 |
| Entitlement accuracy by family | Fail if it drops by more than 0.02 from an approved baseline |
| Posture correctness | Any regression from an approved baseline fails |
| Obligation/enforcement conflation | Zero occurrences |
| Refusal correctness | Any regression from an approved baseline fails |
| Mean cost/query | Fail above 25% increase from an approved measured baseline |
| Judge variance | Pending human review; not a CI gate until measured and reviewed |

Without a baseline, the evaluator returns `pending_baseline_or_measurements` for
baseline-dependent comparisons. It does not claim a pass based on missing data. The
hard citation and conflation checks can still fail on measured current results. The
over-refusal rate is reported separately and any refusal in the first seven families
is currently surfaced as a failure for review.

## Result ledger

| Result | Status |
|---|---|
| Golden schema and family counts | Checked in tests |
| Resolver-aligned expected answers | Checked in tests; independently agent-reviewed on 20 cases; human review pending |
| Answer-service evaluation | `local-eval-v2.json`: 80/80 entitlement, posture, and refusal decisions; 200/200 citations |
| Four-mode retrieval ablation | Measured on all 80 cases; hybrid-plus-rerank recall@10 0.7333, MRR 0.3777 |
| Delay-model real-data calibration | Produced by the local BTS/NCEI pipeline; see model manifest |
| Judge repeatability on 20 held-out answers | Pending; no judge runs; no judge-derived quality claimed |
| Cold re-derivation disagreement on 20 cases | Completed: 0/20 disagreements; see blind review records above |
| p95 latency, cost/query, tool errors, cache hits | Measured in `local-eval-v2`; cache not observed |
| CI metric baseline | Pending approved end-to-end baseline |
