# Decision log

This log records decisions that materially affect correctness, reproducibility, or
operability. Each entry names the rejected alternative and the condition that would
justify revisiting it.

## 2026-10-08 — Separate obligations from enforcement posture

**Decision.** Store regulation and contract clauses separately from enforcement-policy
notices. The resolver returns both an entitlement and an enforcement qualifier.

**Why.** An enforcement-discretion notice changes what the Department pursues; it does
not erase the underlying obligation. Treating both as a single boolean can incorrectly
tell a passenger that nothing is owed.

**Rejected alternative.** Mutating or replacing regulation chunks during a pause.
That loses the source distinction and makes citations misleading.

**Revisit when.** A rulemaking actually amends the CFR text. The two-layer model still
stands, but the regulation layer will then gain a new effective interval.

## 2026-10-08 — Keep the July 2026 documentary gap

**Decision.** Model 2026-07-01 through 2026-07-06 inclusive as having no applicable
enforcement-discretion notice. The successor notice begins on 2026-07-07 and contains
no retroactive or backdating language.

**Why.** The first notice runs only through 2026-06-30. The second notice's operative
date is 2026-07-07. Interpolating between notices would invent a policy interval.

**Failure mode guarded against.** A retrieval or resolver layer that silently treats
adjacent notices as continuous would give the same answer on 2026-07-03 and
2026-07-07.

**Revisit when.** DOT publishes a correction or authoritative interpretation that
expressly makes the second notice continuous or retroactive.

## 2026-10-08 — Refund III is not an active regime

**Decision.** Do not add a Refund III clause version or fifth posture regime.

**Why.** RIN 2105-AF36 remains at the proposed-rule stage and has no final-rule Federal
Register citation. Current Part 260 still traces the relevant text to 2024.

**Revisit when.** A proposed or final rule is published. Ingestion must detect it and
surface a review-required change instead of silently extending current intervals.

## 2026-10-08 — Local-first execution; cloud deployment deferred

**Decision.** All ingestion, retrieval, model serving, API, UI, and evaluation work is
implemented for local execution. No GCP resources, credentials, manifests, or billing
assumptions are required for the current build.

**Why.** The project will use a different cloud account later. Keeping storage and
interfaces portable avoids coupling correctness work to that account.

**Rejected alternative.** Provisioning temporary Vertex, GKE, or Cloud Run resources.

**Revisit when.** The target account and budget are available. Deployment should be a
separate adapter around the same contracts.

## 2026-10-08 — Local bge embeddings with an offline fallback

**Decision.** Use `BAAI/bge-small-en-v1.5` as the default dense embedding model and a
deterministic lexical hashing fallback for tests and disconnected development. Use a
local cross-encoder reranker when installed.

**Why.** This keeps the rules corpus local, avoids per-query embedding charges, and
does not add a second cloud dependency. The fallback keeps setup and CI reliable.

**Rejected alternative.** Vertex embeddings, because cloud access is intentionally
deferred. Pure dense retrieval is also rejected because regulatory identifiers and
threshold language benefit from BM25.

**Revisit when.** Measured retrieval ablations show another model materially improves
recall or when a deployment environment cannot fit the local model.

## 2026-10-08 — Schedule by dependency, not calendar date

**Decision.** The obsolete eleven-day calendar is not used. Work is ordered by data
and correctness dependencies and proceeds continuously.

**Order.** Source verification; contracts and schemas; posture/resolver; ingestion and
retrieval; tools and agent loop; evaluation; API/UI/MCP; packaging and local runbook.

## 2026-10-08 — Make checked-empty posture explicit

**Decision.** `search_rules` returns `posture_checked` alongside the separate posture
list. `posture=[]` with `posture_checked=true` means the lookup succeeded and no notice
was in force. `posture_checked=false` means the reviewed source horizon ended or the
posture layer was not checked, and the resolver must not infer the default regime.

**Why.** An empty list alone cannot distinguish evidence of the July 2026 gap from a
missing or stale lookup. The original locked response shape omitted this bit, so this
is an intentional correctness amendment to the contract.

## 2026-10-08 — Separate eCFR version dates from lookup dates

**Decision.** Every eCFR snapshot carries both `version_effective_on` and `queried_on`.
Clause intervals are derived only from version-effective dates. Repeated lookups of the
same version confirm stability but do not create new intervals.

**Why.** A document retrieved in November 2025 can represent a version effective since
August 2024. Using the retrieval date would incorrectly hide the clause from earlier
queries and fabricate a change that never occurred.

**Interval mapping.** Ingestion and posture storage use half-open intervals. The public
tool contract retains its original inclusive `effective_to` field, so day-granular
records convert an exclusive end date to the preceding inclusive date and retain the
original exclusive boundary in metadata.

## 2026-10-08 — Group unnumbered definition paragraphs by section

**Decision.** When an eCFR section consists of several unnumbered paragraphs, ingest
them as one section-level record under the official citation instead of inventing
ordinal suffixes.

**Why.** Definitions in §260.2 are load-bearing, but synthetic identifiers such as
`#p2` would not be valid legal citations. Section-level grouping is stable and keeps
the official citation intact.
