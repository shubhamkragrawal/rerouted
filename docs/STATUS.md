# Build status — 2026-10-23

Rerouted is implemented as a local-first US air-disruption decision-support project.
No GCP resources, configuration, or deployment artifacts are included.

## Completed locally

- Four typed tools: delay risk, dated rule search, weather context, and historical
  alternative-flight search.
- FastAPI, Streamlit, and official MCP Python SDK entry points.
- Bounded deterministic tool planning, budgets, input/output guards, citation audit,
  refusal handling, and an entitlement resolver that keeps legal obligation separate
  from DOT enforcement posture.
- A typed local 2.0 controller with contradiction-aware clarification, versioned facts,
  checkpoints, bounded/cycle-safe transitions, explicit terminal states, and
  simulator-only transaction evidence. LangGraph workflow wiring and credentialed
  Gemini planning are unimplemented scaffolding; the running controller and planner
  are local deterministic code.
- Versioned retrieval-provider interfaces, deterministic offline fallback, corpus/cache
  hashes, truthful provider diagnostics, bounded reranking fallback, and temporal,
  authority, carrier, and jurisdiction filters. Learned-model checkpoints are optional
  and are not represented as measured when unavailable.
- Privacy-allowlisted local telemetry, trace/session identifiers, readiness and version
  endpoints, request-size limits, and a bounded local rate limiter.
- Official eCFR Versioner snapshots for Parts 259, 260, and §399.80, with versioned
  effective intervals, stable public citations, source hashes, and local source files.
- Separately validated Federal Register posture notices, including the reviewed
  July 1–6, 2026 gap and a refresh-required horizon from July 8, 2027.
- Reviewed AA, Delta, and United contract excerpts plus DOT dashboard commitments.
- A local BTS/NOAA acquisition, Parquet/DuckDB feature, and calibrated delay/cancel
  model pipeline. Its offline refresh path is checksum-driven and staged, records
  freshness/lineage/quality checks, and fails on conflicting duplicate rows. Directory
  promotion uses two renames with rollback on a failed second rename, not an atomic
  swap; readers may briefly see the output path absent.
  Raw and processed bulk data remain git-ignored.
- An 80-case agent-drafted synthetic candidate golden set, 20-case independent-agent
  cold review, four-mode retrieval ablation, operational measurements, and CI gates.
- Local Docker/Make workflows and automated tests, lint, and strict type checking.

## Latest measured evaluation

`data/eval/results/local-eval-v2.json` records the production answer path over all 80
synthetic scenarios: 80/80 entitlement decisions, 100% posture exact match, 100%
citation precision and recall (200/200), zero obligation/enforcement conflations,
80/80 refusal decisions, zero tool errors, and $0 measured model cost because this
path is deterministic and local. Hybrid-plus-rerank retrieval recall@10 is 0.7333;
all 80 cases are scorable. The CI status is `pending_baseline_or_measurements`, not a
failure: approved regression baselines and a repeated external judge run do not exist.

The cold review produced 0 disagreements in 20 cases. That is a self-consistency
result from an independent agent, not human legal validation or real-world accuracy.

## Latest local real-data model — 2026-10-23

The cached BTS/NOAA sources were rebuilt locally with feature pipeline
`local-real-data-4`, then used to regenerate `data/model/delayrisk.json`; no source
acquisition or network access ran. The dataset contains 1,531,942 flights from local
service dates 2025-01-02 through 2025-08-24 for the ten selected origins. Training
used deterministic chronological samples capped at 50,000 training, 20,000
validation, and 50,000 test rows. Splits use whole UTC prediction days, with strict
non-overlapping timestamp bounds; the held-out UTC range ends on 2025-08-25 because
late August 24 departures cross midnight UTC.

The validation gate did not select isotonic calibration for either target. On held-out
test rows, delay Brier score was 0.2132 and expected calibration error (ECE) was
0.0961 across 48,966 observed-label rows. Cancellation Brier was 0.0163 and ECE was
0.0028 on 50,000 rows, with a 1.66% cancellation rate. These are offline
metrics for this time window and airport profile, not evidence of production or
future-period performance. Delay reliability remains uneven at probability extremes.
The samples, target definitions, full reliability curves, and provenance are in the
model and sibling manifest. The compact model and manifest are release artifacts;
bulk raw and processed data remain local and git-ignored.

## External handoffs, not local build blockers

- Human legal/domain review of the candidate golden set and source interpretations.
- Two repeated scores from a chosen external judge if judge variance will become a
  release gate.
- Installed local learned-retrieval checkpoints plus independently reviewed relevance
  judgments for the BGE/cross-encoder benchmark.
- LangGraph, Google GenAI, and Gemini integration are unimplemented scaffolding. The
  repository has a custom local controller, deterministic planner, and disabled optional
  adapter; it has no LangGraph runtime wiring or credentialed Gemini runs.
- OpenTelemetry and Langfuse integration are unimplemented scaffolding. Telemetry is
  privacy-allowlisted local JSON logging only, with no spans, exporters, or trace export.
- Actual participant recruitment and execution for the preregistered user study.
- A valid contact-bearing NWS User-Agent before live forecast calls.
- Any public hosting, GCP work, or production credentials; intentionally deferred.

## Verification

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy core api domains ui scripts local_app
.venv/bin/python scripts/run_local_eval.py --version local-eval-v2
```

See `README.md`, `docs/source_corpus.md`, `docs/real_data_pipeline.md`, and
`docs/eval_methodology.md` for operation and provenance details.
