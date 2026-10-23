# Rerouted — Complete Project Brief

Historical project proposal. Statements below describe original goals and planned
milestones, not implemented status. LangGraph, Gemini, OpenTelemetry, and Langfuse
integrations remain unimplemented scaffolding; see `docs/STATUS.md` for current state.

> An agent for the moment a flight falls apart: what your realistic options are, how
> likely each one is to hold, and what the airline actually owes you — with the
> governing clause cited, and whether it was being enforced on the day it happened.

**Single source of truth.** This file consolidates the build spec, the architecture
decision on repo topology, the eleven-day schedule, the GCP switchover plan, the cost
model, and the September 22 source probe that changed the project's premise.

| | |
|---|---|
| **Status** | Spec locked and amended. Repo not yet scaffolded. No code written. |
| **Scope** | ~3 weeks of sessions, ~2GB of data, all sources free and keyless |
| **Spec amended** | 22 September 2026 — see §0 |
| **This file compiled** | 7 October 2026 |
| **Repo** | `rerouted`, to live beside RLAIF / Regintel / pharmaNER / trialoutcome |

---

## §0 · Read this first — the amendment that changed the premise

The original spec assumed the interesting problem was **versioned regulation text**:
fetch 14 CFR Part 260 at two dates, diff it, carry per-clause effective windows.
On 22 September 2026 that assumption was probed against the live sources, eight days
before build was due to start. **It failed.**

### What the probe found

eCFR records exactly **two** version dates for Part 260 — `2024-06-25` and
`2024-08-12` — and none since. Across the entire window a golden set could cover,
the regulation text is byte-identical. The original M3 definition of done
("fetching Part 260 as of two dates that straddle Dec 2025 yields materially
different clause sets") would have returned an empty diff on day 1.

The reason matters: **an enforcement-discretion notice does not amend the CFR.** It is
a statement of what the Department will pursue, published in the Federal Register,
leaving the regulation untouched. Both governing notices say so explicitly.

### What is actually there — four regimes, one unchanged text

| Window | Enforcement posture | Source |
|---|---|---|
| 12 Aug 2024 → 4 Dec 2025 | Rule enforced | eCFR Part 260 |
| 5 Dec 2025 → 30 Jun 2026 | Renumbering carve-out | FR 2025-22140 |
| **1 Jul 2026 → 6 Jul 2026** | **Pause lapsed — fully enforceable** | gap between notices |
| 7 Jul 2026 → 7 Jul 2027 | Carve-out restored | FR 2026-13675 |

Two facts the original spec did not have:

1. **The discretion was extended.** A second notice published 7 July 2026 extends the
   pause to 7 July 2027. The spec's "ran through 30 June 2026" was written on
   21 September but missed this.
2. **There appears to be a six-day gap.** The first pause expired 30 June 2026; the
   extension published 7 July 2026. On the published dates, 1–6 July 2026 is a window
   where no pause was in force. If real, it is the single best golden-set case in the
   project — three dates weeks apart with three different correct answers, and the
   hardest one is six days wide. **See §16 for the verification still owed on this.**

### Why this makes the project better

The corpus becomes **two-layered**, which is how regulation actually behaves:

- **Layer 1 — regulation text.** eCFR Parts 260, 259, 399.80. Chunked on clause
  boundaries, each chunk carrying `effective_from` / `effective_to` from eCFR's own
  version dates. In this window those windows never close.
- **Layer 2 — enforcement posture.** Federal Register notices as a separate document
  class with their own effective windows, modifying what the unchanged text means in
  practice on a given date.

A system that models only Layer 1 — which is what "date-aware RAG" usually means —
returns the same answer for all four dates and is wrong for three of them.

### The one rule that makes this defensible

> **"Not enforced" is not "not owed."**

An enforcement-discretion notice does not extinguish an obligation. Inside a carve-out
window the carrier still owes the refund under §260.6; DOT has simply said it will not
bring an enforcement action. The passenger can still press the claim, and a chargeback
or small-claims route is unaffected.

Collapsing those two into a flat `NOT_ELIGIBLE` is the single most likely way for this
system to say something false to someone who is stranded. The resolver must carry the
distinction — an entitlement label **plus** an enforcement-posture qualifier — and the
answer must state both. Getting this right is worth more than any retrieval metric in
this document.

---

## §1 · Scope boundary — fixed from day one

This tool **explains what published rules and contracts say, and cites them.** It does
not give legal advice, does not contact the airline, does not book anything, and does
not tell anyone what they will receive.

That boundary goes in the README, permanently in the UI, and into the refusal
behaviour. It is a design strength, not a hedge.

**Permanent disclosure text, visible at all times — not triggered only on refusal:**

> This explains what published DOT rules and airline contracts of carriage say, with
> citations. It is not legal advice, it does not contact the airline, and what you
> actually receive may differ.

---

## §2 · Architecture

One bounded loop, four tools, and a deterministic resolver the model does not get to
overrule.

```
query
  │
  ├─> parse ──> disruption record ──> as_of_date ───────────────┐
  │                                                            │
  ├─> input guard (scope · injection · PII)                     │
  │                                                            │
  ├─> bounded agent loop  [planner ⇄ executor]                  │
  │      ├── delay_risk()         ML model                      │
  │      ├── search_rules()       date-filtered, BOTH layers <──┘
  │      ├── weather_context()    live NWS | historical NCEI
  │      └── find_alternatives()  historical BTS schedule
  │
  ├─> deterministic entitlement resolver    ← decides, not the LLM
  │
  ├─> auditor: every claim carries a clause that exists AND was
  │            in force on as_of_date (deterministic check)
  │
  └─> answer  |  refusal
```

**The resolver is deterministic on purpose.** The model finds and explains the
governing clause; a plain Python function decides the entitlement label from the facts
and that clause. That makes the decision testable, identical on every run, and
diffable in code review — and it is the honest answer to "how do you stop it
hallucinating an entitlement."

### Repo layout

```
rerouted/
├── core/                          # domain-agnostic — no airline strings anywhere
│   ├── agents/                    # planner · executor · auditor · refusal
│   ├── retrieval/                 # hybrid BM25+kNN+RRF · reranker · temporal filter
│   ├── resolver/                  # rule-engine primitives (predicates, precedence)
│   ├── eval/                      # harness · metrics · judge · variance check
│   ├── guardrails/
│   └── mcp/
├── domains/air_us/
│   ├── ingest/                    # BTS · eCFR · Federal Register · NCEI
│   ├── rules/                     # entitlement predicates + precedence table
│   ├── model/                     # delay-risk training + calibration
│   ├── chunking_config.yaml
│   ├── tools_registry.yaml
│   ├── posture.yaml               # ← reviewed-by-hand notice intervals (see §5)
│   └── golden_set.jsonl
├── api/                           # FastAPI + SSE
├── ui/                            # Streamlit
├── docs/  decisions.md  eval_methodology.md  mcp_architecture.md
└── Makefile                       # start · stop · test · eval · health
```

### Agent state

```python
class RerouteState(TypedDict):
    query: str
    disruption: Disruption          # carrier, flight, date, origin, dest, event
    as_of_date: date                # drives rule versioning — the key field
    plan: list[ToolCall]
    tool_results: list[ToolResult]
    options: list[Option]
    entitlement: Entitlement | None # set by the resolver, never by the LLM
    citations: list[Citation]
    unresolved_facts: list[str]     # → triggers refusal instead of a guess
    llm_call_count: int             # ceiling 8
    cost_so_far_usd: float          # ceiling $0.12
    started_at: float               # ceiling 20s
    answer: str
    refusal_reason: str | None
    trace_id: str
```

---

## §3 · Data

All free, all keyless, all verified live.

| Source | What it gives | Access | Size as scoped |
|---|---|---|---|
| **BTS On-Time Performance** | Every domestic flight: scheduled vs actual, delay minutes, cancellation + cause code, diversion, delay-cause breakdown | TranStats monthly CSV, no key. Current through Dec 2025. | 12 months × top 50 airports ≈ 400MB raw → ~60MB Parquet |
| **eCFR** | 14 CFR Part 260 (refunds), Part 259 (passenger protections), 399.80. Versioned by date. | ecfr.gov API/XML, no key | a few MB |
| **Federal Register** | **The posture layer — the only thing that actually moves.** FR 2025-22140 (5 Dec 2025) and FR 2026-13675 (7 Jul 2026), plus whatever Refund III publishes | federalregister.gov API, no key | < 1MB |
| **Contracts of carriage** | Carrier-specific obligations beyond the regulation | Public PDFs — scope to 3 carriers | ~3MB |
| **DOT customer-service commitments** | Meals/hotel/rebooking on controllable delays — commitments, not regulation. The distinction matters. | DOT dashboard, public | tiny |
| **NWS API** | Live forecast + recent observations | api.weather.gov, no key, User-Agent required | live |
| **NOAA NCEI LCD** | Historical hourly observations — needed because NWS won't serve 2025 weather | NCEI, free CSV | ~40MB |

### Two honest constraints, named up front

**There is no free live schedule feed.** So `find_alternatives` searches a historical
schedule table built from BTS. Turn that into a feature: the demo **replays a real
disruption day**, which makes every run reproducible and the eval exactly repeatable.
Say this in the README rather than letting a reviewer discover it.

**Weather has two modes.** Live queries hit NWS; golden-set cases hit NCEI historical.
The tool takes a `mode` parameter and reports which source answered, so a trace never
leaves you guessing which one ran.

---

## §4 · Tool contracts — locked

Write these down before any of them exists, and build the agent against these exact
shapes. A mismatch here is the integration bug that passes every local test and dies
in the demo.

### 1 · `delay_risk` — ML model

```
REQUEST  { carrier, origin, dest, flight_date, crs_dep_time, mode }

RESPONSE {
  "p_delay_15"        : 0.23,     # P(arrival delay ≥ 15 min)
  "p_cancel"          : 0.018,
  "conformal_interval": [0.17, 0.31],
  "calibration_bin"   : "0.20-0.30",
  "top_features"      : [{"feature": "carrier_route_ontime_90d",
                          "contribution": -0.08}, ...],
  "plain_english"     : "Roughly a 1-in-4 chance of a 15+ minute delay; ...",
  "model_version"     : "delayrisk-0.3.1",
  "feature_pipeline_version": "fp-2"
}
```

### 2 · `search_rules` — the differentiator

```
REQUEST  { query, as_of_date, carrier?, doc_types[], k }

RESPONSE {
  "clauses": [{
    "clause_id": "14CFR260.6(a)",
    "doc": "14 CFR Part 260",
    "text": "...",
    "effective_from": "2024-08-12",
    "effective_to": null,
    "authority": "regulation",   # regulation | commitment | contract | enforcement_policy
    "score": 0.87, "rerank_score": 0.94,
    "source_url": "https://www.ecfr.gov/..."
  }],
  "posture": [{                  # layer 2 — empty list is a real answer
    "fr_doc": "2026-13675",
    "effect": "enforcement_paused",
    "sections": ["260.6", "260.9", "399.80(l)"],
    "scope": "renumbered_flight_no_significant_change",
    "effective_from": "2026-07-07",
    "effective_to": "2027-07-07",
    "source_url": "https://www.federalregister.gov/..."
  }],
  "as_of_date": "2026-03-14",
  "excluded_by_date": 3,         # visible, not silent
  "corpus_version": "rules-2026-09"
}
```

The `authority` field carries real weight. A regulation, an airline's voluntary
DOT-dashboard commitment, a contract-of-carriage clause and an enforcement-policy
notice are **four different kinds of obligation**, and conflating them is the most
likely way for this project to say something wrong. Precedence is encoded in the
resolver, not left to the model.

`posture` is returned **alongside** clauses, never merged into them, because the two
answer different questions: what the rule says, and whether anyone is enforcing it.
An empty `posture` list is a meaningful result — it is what the six-day July 2026
window returns — so the resolver must distinguish "no notice in force" from
"not checked."

### 3 · `weather_context` — external API

```
REQUEST  { airport, window_start, window_end, mode: "live" | "historical" }

RESPONSE {
  "source": "NWS" | "NCEI-LCD",
  "observations": [{ ts, ceiling_ft, visibility_mi, wind_kt, wx_codes[] }],
  "adverse_flags": ["low_ceiling", "thunderstorm"],
  "confidence": "observed" | "forecast",
  "as_of": "2026-03-14T18:00:00Z"
}
```

### 4 · `find_alternatives` — structured

```
REQUEST  { origin, dest, not_before, original_scheduled_arrival,
           max_stops, same_carrier_only, flight_date }

RESPONSE {
  "options": [{
    "legs": [{ carrier, flight_no, origin, dest, crs_dep, crs_arr }],
    "total_duration_min": 260,
    "connection_buffer_min": 55,
    "hist_ontime_rate": 0.78,
    "arrival_delta_vs_original_min": 410   # feeds the resolver directly
  }],
  "searched_date": "2026-03-14",
  "schedule_source": "historical_bts"      # never claim it's live
}
```

`original_scheduled_arrival` is the disrupted flight's timezone-aware scheduled
arrival instant from the disruption record. Candidate arrival deltas are measured
against this value; a candidate BTS flight is never used to guess the original.
`hist_ontime_rate` uses only outcomes available by `not_before`: flown flights become
available at scheduled arrival plus BTS `ArrDelay`. Rows with missing arrival delay
are omitted because their completion time is unknown. BTS does not provide a
cancellation-event timestamp, so cancellation labels are omitted from reliability.
Candidate flights remain enumerated from their published schedule and are excluded
from the historical rate.

---

## §5 · The rules corpus

Where most of the quality lives, and where most of the work is.

**Chunk on clause boundaries, never on token count.** A regulation has structure —
part, section, paragraph, subparagraph — and that structure *is* the citation unit.
Splitting mid-clause destroys your ability to cite, which is the whole product. One
chunk per leaf paragraph, with the full hierarchy carried as metadata so
`14CFR260.6(a)(2)` is reconstructable.

**Every chunk gets an effective window — and in this corpus most of them never close.**
The ingestion job walks `/api/versioner/v1/versions/title-14.json?part=260`, fetches
the full text at each version date, and emits per-clause
`(effective_from, effective_to, text_hash)` intervals. For Part 260 that yields exactly
two dates and one open interval. Build it properly regardless — it is thirty lines more
than hard-coding the answer, and hard-coding it is how the project quietly breaks when
Refund III lands.

**The posture layer is the one that actually moves, and it is a different ingestion
job.** Federal Register notices are documents, not clause amendments: fetch by CFR
citation from `/api/v1/documents.json`, keep the notices that reference Parts
260/259/399.80, and extract for each one its effect, the sections it touches, the
conduct it scopes to, and its own effective interval. Those intervals are **not
contiguous** — the 1–6 July 2026 gap exists precisely because one notice lapsed before
its successor published — so the job must never interpolate between them. **A gap is
data.**

**Parse the dates, don't infer them.** Both notices state their windows in prose
("pausing until June 30, 2026"; "for 1-year from the date of this publication,
expiring on July 7, 2027"). Extract those into structured intervals once, by hand,
into a reviewed `posture.yaml` with the source URL beside each — and write a test that
re-fetches the notices and asserts the prose still matches. An LLM parsing an effective
date at query time is a hallucination with a date on it.

**Retrieval is hybrid and date-filtered.** BM25 alongside dense vectors — regulation
text is full of exact identifiers ("260.6", "significant change", "three hours") that
pure vector search handles badly — fused with RRF, then a bge reranker over the top 50.
The date filter is a pushed-down metadata predicate, not a post-filter, so your top-k
is genuinely top-k among clauses that were in force.

> This is the concrete reason to use **Qdrant or pgvector rather than Chroma**: you
> need real metadata filtering pushed down into the search, not a post-filter that
> quietly empties your top-k.

**Measure retrieval on its own.** Recall@10 and MRR against the golden set's
`governing_clauses`, reported before any generation quality. Run the
bm25-only / vector-only / hybrid / hybrid+rerank ablation once and put the table in the
README — it's four numbers and it demonstrates you know what each component bought you.

---

## §6 · The delay model

Small, honest, and calibrated — **calibration is the point, not accuracy.**

**Gradient-boosted trees on BTS.** Target: `ArrDel15` for delay, `Cancelled` for
cancellation — two models, same pipeline. Features: carrier, origin, dest, month,
day-of-week, departure hour block, route historical on-time rate over a trailing
window, carrier-at-origin historical rate, aircraft-turn proxy from
`LateAircraftDelay` history, and a weather flag joined from NCEI.

**Split by time, not at random.** Train on months 1–9, validate on 10, test on 11–12.
A random split leaks: the same aircraft, the same day, the same weather event appear on
both sides and your numbers come out beautiful and meaningless. Saying this out loud in
an interview is worth more than a high AUC.

**Report calibration, not just discrimination.** Brier score, a reliability curve, and
expected calibration error. The agent surfaces probabilities to a person deciding
whether to take a 55-minute connection — a model that ranks well but is badly
calibrated is actively harmful there. Add isotonic or Platt calibration on the
validation fold and show the before/after curve.

**Serve it behind FastAPI `/predict`** in the locked response shape above. Same pattern
specced for TrialOutcome, reused deliberately — reusing your own convention across
projects is itself a thing to point at.

---

## §7 · Guardrails and refusal

The section that makes the project defensible. Build it properly rather than bolting it
on in week three.

| Layer | Check | On failure |
|---|---|---|
| **Input** | In scope? (US air-travel disruption) · injection heuristics · PII — never log PNRs or names | templated refusal naming the boundary |
| **Executor** | Tool args validate against schema before execution | error returned as observation, model repairs |
| **Budget** | 8 LLM calls · $0.12 · 20s wall clock | graceful partial answer + logged incident |
| **Resolver** | Are all facts the predicate needs present? | fact added to `unresolved_facts` → refusal path |
| **Auditor** | Every entitlement claim carries a `clause_id` that exists in the corpus and was in force on `as_of_date` — a deterministic check, not a judgment | claim stripped; if load-bearing, refuse |
| **Output** | No claim about what the airline "will" do · no legal conclusion · disclosure present · **entitlement and enforceability stated separately** | regenerate once, then refuse |

### The refusal template — specified, not left to the model

```
I can't answer this one reliably, because the rule that applies turns on
something I don't know: {unresolved_facts}.

Here's what I can tell you: {grounded_partial}

The relevant rule is {clause_id} — {one_line_summary}.
If {fact} turns out to be {value}, that clause would apply.
```

A refusal that names the missing fact and points at the governing clause is more useful
than most systems' confident answers. It is also the behaviour the golden set's eighth
family exists to test — and **over-refusal is scored too**, so this can't be gamed by
refusing everything.

---

## §8 · The golden set and the eval

**80 cases, 8 families of 10.** The families are chosen so that a naive system fails
visibly.

```json
{
  "case_id": "gs-041",
  "scenario": { "carrier": "DL", "flight": "2422", "date": "2026-03-14",
                "origin": "MSP", "dest": "BUF", "event": "cancelled",
                "cancellation_code": "A",
                "rebooked_arrival_delta_min": 410 },
  "as_of_date": "2026-03-14",
  "expected": {
    "entitlement": "REFUND_ELIGIBLE",
    "enforcement_posture": "paused_renumbering_carveout_not_applicable",
    "governing_clauses": ["14CFR260.6(a)", "14CFR260.2"],
    "amenities": ["meal", "hotel"],
    "rationale_facts": ["controllable cancellation",
                        "arrival delta > 3h, domestic"]
  },
  "expected_refusal": null,
  "source_of_truth": "BTS row 2026-03-14/DL2422 + eCFR as of 2026-03-14 + posture.yaml",
  "family": "domestic_cancel_controllable",
  "difficulty": "medium"
}
```

### The eight families

1. **`domestic_cancel_controllable`** — the base case. Carrier-caused cancellation,
   refund plus amenities.
2. **`domestic_cancel_weather`** — refund still applies; amenities generally don't.
   Tests that the model doesn't over-generalise from family 1.
3. **`domestic_delay_over_3h`** — significant change by arrival delta. Tests the
   threshold arithmetic.
4. **`domestic_delay_under_3h`** — negative cases. Not eligible. Most portfolio golden
   sets contain no negatives at all, which is why they score so well.
5. **`international_delay_6h`** — different threshold, same logic. Tests that the rule
   isn't hard-coded to 3 hours.
6. **`enforcement_posture`** ← **THE HEADLINE.** One renumbering scenario at four
   `as_of_date`s: before 5 Dec 2025, inside the carve-out, **inside the six-day July
   2026 gap**, and after the extension. Four different correct answers over identical
   regulation text. The gap cases are the ones nothing else gets right.
7. **`downgrade / added_connection`** — significant change by a route not a clock.
   Tests the other limbs of the definition.
8. **`insufficient_facts`** — must refuse. The governing rule depends on something the
   scenario doesn't state. Over-refusal on families 1–7 is scored against you.

### Metrics — scored separately, because they fail independently

| Metric | What it catches | Gate |
|---|---|---|
| **Entitlement accuracy**, per family | The headline. Per-family reporting stops a strong family hiding a broken one. | drop > 2pts fails CI |
| **Posture correctness** (family 6) | Did it apply the enforcement posture in force on `as_of_date`, and keep entitlement separate from enforceability? | any regression fails |
| **Obligation/enforcement conflation** | Did any answer in a carve-out window tell the passenger they are *not owed* a refund, rather than that DOT will not pursue it? The one error that is actively harmful. | **any occurrence fails** |
| **Citation precision** | Does the cited clause actually support the claim — an answer right for the wrong reason. | < 0.90 fails |
| **Citation recall** | Did it find every governing clause, or just one that happened to fit? | report |
| **Refusal correctness** (family 8) + over-refusal rate (1–7) | Refusing well, without refusing everything. | any regression fails |
| **Retrieval recall@10, MRR** | Isolates retrieval from generation. | report + ablation table |
| **Brier, ECE, reliability curve** | Delay model calibration. | report |
| **p95 latency · $/query · tool error rate · cache hit** | That you operated it. | cost +25% fails |

### Before citation precision becomes a CI gate

Score 20 held-out answers **twice** with the same judge and rubric, and report the
variance. If judge noise alone could cross a 2-point threshold, either widen the gate
or average several judge calls — and write down which you chose and why. A gate that
fires on noise is worse than no gate, because it teaches you to ignore it.

Same discipline on the golden set itself: re-derive 20 cases cold, without looking at
the recorded answer, and report the disagreement rate. **If it comes out at zero, be
suspicious and say so.**

---

## §9 · Milestones

Eight, each with a definition of done checkable without judgement.

### M1 · Data in
BTS 12 months → Parquet → DuckDB. NCEI weather joined. Airport and carrier reference
tables.
**DoD ·** `SELECT` a known cancelled flight by date and get the right row back in under
200ms.

### M2 · Delay model, calibrated
Time-based split, two targets, isotonic calibration, served behind `/predict` in the
locked shape.
**DoD ·** reliability curve committed; Brier reported for calibrated and uncalibrated;
the split is chronological and a test asserts it.

### M3 · Two-layer corpus
eCFR ingestion producing per-clause effective intervals, **and** a separate Federal
Register posture ingestion producing per-notice intervals. Contracts of carriage for
three carriers.
**DoD ·** a test asserts Part 260's text is identical at `2025-11-01` and `2026-03-14` —
the *negative* result, pinned so a future change is visible. A second test resolves
posture at four dates (`2025-06-01`, `2026-03-14`, **`2026-07-03`**, `2026-08-02`) and
gets four distinct answers, with the July date returning an empty notice list.

### M4 · Temporal hybrid retrieval
BM25 + kNN + RRF + reranker, with `as_of_date` as a pushed-down filter across both
layers.
**DoD ·** the four-way ablation table exists with real numbers; a golden case returns
non-empty `posture` and another returns empty, both surfaced in the response rather
than silently dropped.

### M5 · Resolver + loop
Deterministic entitlement predicates with an authority-precedence table. LangGraph
loop, all four tools wired, budgets enforced, Langfuse tracing live.
**DoD ·** resolver unit-tested independently of the LLM; a forced pathological query
trips the budget and degrades gracefully; traces visible with per-node cost.

### M6 · Golden set to 80
All eight families, ten each, with sources of truth recorded per case.
**DoD ·** 20-case blind re-derivation done, disagreement rate written into
`eval_methodology.md`.

### M7 · Eval harness + CI gate + guardrails
All metrics, judge variance check, GitHub Action gating merges. Refusal path and
permanent disclosure live.
**DoD ·** a deliberately broken prompt fails the build in CI and you can show the
failing run.

### M8 · MCP, UI, README
MCP server exposing all four tools; Streamlit with a reasoning panel; README with
architecture diagram and real numbers; `decisions.md`.
**DoD ·** demo GIF of the tools running inside an MCP client; `make start` works from a
clean clone on a machine that isn't yours.

---

## §10 · Repo topology — one repo, or three?

**Verdict: one flagship, and at most two extractions — each triggered by a reason, not
a plan.**

`rerouted` is a monorepo and stays one. The agent, its four tools, the delay model, the
rules pipeline and the eval suite all serve a single product, and a reviewer should
open one link and see the whole thing working.

Two things eventually earn their own repo, and only two:
- **the eval harness**, because a benchmark that lives inside somebody's portfolio repo
  is not a benchmark
- **the warehouse**, because a dbt project has its own lifecycle, CI and docs site

Everything else — the fine-tuned classifier included — stays inside.

### How the tree grows

```
rerouted/                          # the flagship, from day one
├── core/
│   ├── agents/  retrieval/  resolver/  guardrails/
│   ├── redteam/                   + S1
│   ├── cache/                     + S1  (exact + semantic)
│   ├── mcp/                       deepened in S1 (client · sampling · roots · gate)
│   └── eval/                      → extracted to agenteval/ at S3
├── domains/air_us/
│   ├── ingest/  rules/
│   ├── model/
│   │   ├── delay_risk/            XGBoost + calibration + conformal
│   │   ├── registry/              + S2  (MLflow · Optuna · drift · retrain)
│   │   └── clause_clf/            + S4  (fine-tune vs prompt, LoRA ablation)
│   ├── golden_set.jsonl
│   ├── posture.yaml
│   └── tools_registry.yaml
├── api/  ui/  deploy/             deploy/ + S2 (Cloud Run · GKE manifests)
├── analysis/                      + S6  (A/B methodology · causal notebooks)
└── docs/  decisions.md  Makefile

agenteval/                         appears at S3
├── adapters/  metrics/  cli.py    predict(q)->dict, typer CLI, async variant
└── datasets/rerouted-v1/          the golden set, with a card and honest limits

warehouse/                         appears at S5, only if S5 happens
├── models/staging/  models/marts/ fct_flight · dim_carrier · dim_airport · bridges
├── macros/  tests/  airflow/
└── profiles.yml                   targets BigQuery
```

### The three contracts

| Boundary | Contract | Why it's a contract and not a call |
|---|---|---|
| **agent ↔ tools** | `tools_registry.yaml` + the four locked response shapes | The agent is built against the shapes, not the implementations. Swapping `find_alternatives` from DuckDB to BigQuery at S5 touches no agent code. |
| **rerouted ↔ agenteval** | `predict(question) -> {answer, retrieved_ids, abstained}` | Rerouted becomes just another scored system. That's what makes the package credible to someone who isn't you. |
| **rerouted ↔ warehouse** | Mart table schemas, read-only role, versioned | The warehouse publishes; the agent reads. Neither imports the other. This is the boundary that keeps a dbt project from leaking into an agent repo. |

**Three, not fifteen.** The old suite had contracts between six projects, which meant
every change rippled. Here the flagship is self-contained and the two satellites touch
it at exactly one seam each. If a seam ever needs a second version, that's the signal
the split was wrong — not a reason to add coordination.

### When to split, and when not to

| Candidate | Split? | Test |
|---|---|---|
| Eval harness | **yes, at S3** | Would a stranger `pip install` it? Yes — so it needs its own README, version and release. |
| Warehouse | **yes, at S5** | Does it have its own toolchain and CI? dbt does. Jamming dbt models into an agent repo is a category error. |
| Clause classifier | no | Does anything but Rerouted call it? No. It's a module, not a product. |
| Delay model | no | Same test. It's behind an internal endpoint with a locked shape — that's already the boundary. |
| Red team / cache / MCP | no | Layers of the agent, not artifacts anyone consumes independently. |
| A/B + causal notebooks | no | They analyse Rerouted's own data. Their value is the contrast with the delay model, which only holds if they sit beside it. |

> **The failure mode this is designed against.** The previous portfolio's flaw wasn't
> its architecture — the core/domains split and the locked contracts were good ideas.
> It was that six repos meant six READMEs, six quickstarts and six things to keep
> working, and the result after months was that none of them were public.
>
> **The rule that prevents a repeat: a new repo needs a consumer who isn't you.**

### The same system, two run modes

Architecturally this is a config change, not a second build.

| Workload | Where | Reason |
|---|---|---|
| Ingest, embed, index | **local** | Minutes on your chip, and you'll rerun it fifty times tuning chunking. |
| Agent loop LLM | Vertex · Flash | Reasoning quality decides tool selection. Never local for this. |
| Eval judge | Vertex · Pro | ~$1.40 a full 80-case run (see §13). |
| Optuna sweeps, fine-tuning | Vertex Custom Training | The genuinely heavy jobs. L4, artifacts to GCS. |
| Live demo | Cloud Run | Scales to zero, index baked into the image, no second service. |
| k8s milestone (S2) | GKE Autopilot | Reads better than k3d. Tear it down after. |
| Warehouse (S5) | BigQuery | Under 1GB — free tier. Airflow as a Cloud Run Job, not Composer. |
| Statistics (S6) | **local** | statsmodels and DoWhy on this data is a laptop job. |

Keeping litellm behind the model calls and an interface behind the retriever is what
lets the same artifact run on your laptop and on Cloud Run — and it's what makes
"this runs fully inside your perimeter" a demo rather than a promise.

---

## §11 · The eleven-day schedule

> ⚠️ **Rebase these dates.** This schedule was written for 30 Sep → 10 Oct 2026 and
> the calendar has moved past it without the build starting. The *sequence* and the
> dependencies are still correct — treat the "Day N" column as relative and pick a new
> Day 1. Nothing in the plan assumes a specific calendar date except the GCP credit
> boundary in §12, which needs rechecking against your actual account.

15 sessions across 11 days, ~1.4/day. Two things shape it: long-running jobs move to
overnight, so no session is spent watching a download bar; and the GCP switchover
happens on day 6, five days before credits lapse, so there's no cliff.

| Day | Sessions | What lands | Overnight |
|---|---|---|---|
| **S0** (2 days) | S0 | **Ship RLAIF.** README, diagram, PPO-vs-DPO curves, failure analysis. Resume header rewritten. Create `rerouted` public with the thesis in it. **Start the new GCP account activation** — it takes days. | BTS + NCEI downloads kick off |
| **Day 1** | S1 · S2 | ~~The probe, then the intervals.~~ **AMENDED: the probe is done (§0).** Day 1 is now: posture-layer ingestion from the Federal Register, `posture.yaml` built by hand and reviewed, and the four-date resolution test. | eCFR fetch across all version dates; FR crawl |
| **Day 2** | S3 | Clause-boundary chunking, hierarchy as metadata, three carriers' contracts. Load to Qdrant with date payloads. **Golden set starts today** — clause ids now exist. | Embed + build index |
| **Day 3** | S4 · S5 | Hybrid retrieval with `as_of_date` pushed down across both layers, then the reranker. | Four-way ablation run |
| **Day 4** | S6 | BTS → DuckDB, NCEI join, `find_alternatives()` with connection logic. Ablation table from last night goes in the README. | Feature pipeline batch build |
| **Day 5** | S7 | Feature pipeline, chronological split with the test that asserts it, baseline model. | **Optuna sweep on Vertex** |
| **Day 6** | S8 | Isotonic calibration with the before/after curve, conformal, SHAP, served in the locked shape. **🔄 Move to the new GCP account today.** | Golden-set tooling; nothing heavy |
| **Day 7** | S9 | The resolver — entitlement predicates, authority precedence, **and the posture qualifier**. Plain Python, unit tested. No automation anywhere near this one. | — |
| **Day 8** | S10 · S11 | The LangGraph loop with budgets and circuit breaker. Then Langfuse, the auditor's citation check, the refusal path. **Openable from tonight.** | Smoke run across 20 golden cases |
| **Day 9** | S12 | Eval harness, every metric scored separately, judge variance check before anything becomes a gate. Golden set should hit 80 today. | Full 80-case eval run |
| **Day 10** | S13 | Fix whatever last night's scorecard exposed. Then the CI gate and a light red-team pass. | Re-run eval; CI validation |
| **Day 11** | S14 · S15 | MCP server and the Streamlit panel. Then Cloud Run deploy with the index baked in, README with real numbers, `decisions.md`. **Live URL.** | — |

### What actually runs unattended

**Genuinely overnight** — long jobs that just need starting:
BTS monthly downloads (12 files, I/O-bound) · eCFR fetches across every version date ·
Federal Register crawl · embedding + index build over the full corpus · Optuna sweep on
Vertex · full 80-case eval runs · re-index after any chunking change.

**Safe to hand off, review at breakfast** — mechanical scaffolding:
Repo structure, Makefile, Dockerfile · pydantic models generated from the locked
contracts · CI workflow files · test harness skeletons · the four-way ablation runner ·
data-quality assertions over the loaded tables.

**Cannot be offloaded** — where your judgement is the product:
- **The golden set.** A system that writes its own eval set has no eval set.
- **The resolver predicates.** Correctness lives here.
- **`posture.yaml`.** Hand-parsed from notice prose, by you, with sources.
- Chunking strategy and authority precedence
- Refusal thresholds
- Interpreting a failing eval run

> **The daily discipline that decides whether this works: eight golden-set cases a day,
> every day from day 2.** Eighty minutes. It's the only thing in this plan that can't be
> caught up, can't be automated, and can't be rushed — and it's the artifact the whole
> project's credibility rests on. **If a day goes badly, drop a session before you drop
> the cases.**

---

## §12 · The GCP switchover

**The rule: GCP holds compute, never state.** Every durable thing lives in git or in a
GCS bucket you can rsync in minutes — code, the Parquet files, the rules corpus, the
Qdrant snapshot, trained model artifacts. Nothing irreplaceable should exist only inside
a GCP project.

Keep to that and the "migration" is: create the project, enable APIs,
`gsutil -m rsync` a few hundred megabytes, rebuild the container, repoint
`GOOGLE_CLOUD_PROJECT`. Under an hour, most of it waiting.

| Thing | Moves how |
|---|---|
| **Code** | It's in git. Nothing to move. |
| **Data (Parquet, rules, NCEI)** | `gsutil -m rsync` between buckets — a few hundred MB. |
| **Model artifacts** | Same. They're files. |
| **Container images** | Don't move them — rebuild from source on the new registry. Faster than transferring. |
| **Vertex training jobs** | Ephemeral. Nothing to move. |
| **Cloud Run service** | Redeploy from source. One command. |
| **Langfuse traces** | Not on GCP. Unaffected. |

### Two things to avoid

**Don't stand up anything stateful on the old account.** No Cloud SQL, no persistent
Vertex endpoint, no GKE cluster. Those are the only things that would turn a switchover
into a migration — and none are needed before S15 anyway.

**Start the new account early, not on switch day.** Billing verification can take days,
and the one scenario that actually hurts is discovering on switch day that the new
project isn't ready. Overlap the two accounts for a few days; that's what makes this a
non-event.

---

## §13 · Cost model

**Roughly $60 to build and $40–68/month to keep the URL alive. Budget $150 and
$110/month with margin.** A $300 free-trial credit covers the build and several months
of hosting.

### Per-query economics

One agent run is ~6 LLM calls × ~6k input tokens. At Gemini 2.5 Flash
($0.075 / $0.30 per million) that is **$0.0032 per query** — 37× under the spec's
$0.12 ceiling. Even running the whole loop on 2.5 Pro is $0.063, still inside the cap.
**The $0.12 budget is a circuit breaker, not a forecast.**

A full 80-case eval run with a Flash agent and a Pro judge: **$1.38**.

### The build

| Line | Expected | Basis |
|---|---|---|
| Agent LLM calls, dev iteration | $5 | ~1,500 ad-hoc runs at Flash |
| Eval runs | $41 | 30 full 80-case runs, Flash agent + Pro judge |
| Optuna sweeps | $10 | 3 × 8h on `n1-standard-8` @ $0.437/hr |
| Embeddings | $0–5 | free if you embed locally with bge |
| Storage + Artifact Registry | $2 | ~10GB each |
| Cloud Run during build | ~$1 | `min-instances=0` until ship day |
| **Total** | **~$60** | **budget $150** |

### Steady state, with a live URL

| Line | Expected | With margin |
|---|---|---|
| Cloud Run, `min-instances=1`, 2 vCPU / 4 GiB, request-based billing | $38.88 | $65 |
| Storage + registry | $1.15 | $2 |
| CI eval runs on merges (~20/mo) | $28 | $45 |
| **Total** | **~$68/mo** | **~$110/mo** |

`min-instances=0` is near-free but gives a 40–90s cold start while the baked-in Qdrant
index and bge reranker load. Wrong trade for the one artifact the project exists to
show. **Keep the warm instance while the link is live.**

### The four things that would actually blow this

1. **A Vertex prediction endpoint left deployed — $160/month doing nothing.** You are
   charged per deployed model whether or not it serves a prediction. The spec routes the
   delay model behind FastAPI on Cloud Run instead; the risk is reaching for a Vertex
   endpoint out of habit.
2. **Cloud Run on instance-based billing instead of request-based.** Same 2 vCPU / 4 GiB
   container costs **$114/month** instead of $39. Set this explicitly at deploy.
3. **8 GiB instead of 4.** Takes the warm instance from $39 to $65/month. Check whether
   `bge-reranker-base` fits in 4 GiB alongside Qdrant before provisioning up.
4. **A runaway loop before S10.** The 8-call/$0.12 ceiling doesn't land until the loop
   milestone. Put a crude call counter in on day 1 — ten lines — rather than waiting for
   the real budget node.

**GKE** (Google Kubernetes Engine — managed Kubernetes, control plane run by Google,
containers across a cluster of nodes) sits outside this. An Autopilot cluster is ~$75/mo
in management fee alone before workloads. For one agent serving portfolio traffic it is
pure overhead; Cloud Run does the same job with no cluster to pay for. Skip it unless
you specifically want the k8s line, and then treat it as a separate exercise.

---

## §14 · Running it locally after teardown

**Plan: keep the URL live ~10 days after ship, then take it down.** That works, and the
architecture already supports it — §10's two run modes are a config change.

| Component | After teardown |
|---|---|
| Qdrant, DuckDB, BM25, bge reranker | local containers/files, no network |
| FastAPI + Streamlit | localhost |
| Delay model (XGBoost pickle) | local, behind `/predict` |
| Weather (NWS / NCEI) | public keyless APIs |
| **LLM calls** | **the only cloud dependency — Vertex via litellm** |
| Langfuse | cloud free tier |

`make start` gives you the whole thing for ~$0.003/query and no fixed cost.

### Three things to get right

**"Take it down" must mean delete the Cloud Run service, not the GCP project.** If you
delete the project you lose Vertex AI access with it and would be re-creating
credentials to run your own demo. An idle project with Vertex enabled and nothing
deployed bills essentially nothing. Delete the Cloud Run service and the Artifact
Registry images; **keep the project.**

**Ten days at `min-instances=1` costs about $13.** Don't economize there — that's the
window where the link actually gets clicked.

**Kill the README link the same day you kill the service.** A dead demo URL on a
portfolio repo reads worse than never having had one. M8's DoD already requires a demo
GIF; make the swap on teardown day so the README goes from "live at $URL" to the GIF
plus `make start`. **Set a reminder — this is exactly the kind of thing that sits broken
for six months.**

Also: local p95 latency will be noticeably worse than Cloud Run's, mostly from the
reranker on laptop CPU over the top 50. **Label the README latency number as measured on
Cloud Run** so you're not quoting a figure your local demo contradicts.

---

## §15 · README skeleton and `decisions.md`

Write the README in week one with the numbers blank. It keeps you honest about what
you're actually going to measure.

```markdown
# Rerouted — what you're owed when a flight falls apart
> Agentic RAG over DOT regulations and contracts of carriage, with
> date-aware retrieval, a calibrated delay model, and an eval-gated CI pipeline.

[architecture diagram]

## The interesting problem
The regulation text never changed. Enforcement of it changed four times.
DOT paused enforcement for renumbered flights in Dec 2025, let the pause
lapse for six days in Jul 2026, then extended it to Jul 2027. So the right
answer depends on when the disruption happened — and the corpus is two
layers, text and posture, filtered independently by as_of_date.

[timeline figure]

## Numbers that matter
entitlement accuracy __/80 · posture family __/10 · citation precision 0.__
refusal correct __/10 · over-refusal __% · recall@10 0.__ · Brier 0.__
p95 __s · $0.__/query · cache hit __%

## Retrieval ablation
| mode | recall@10 | MRR |   # bm25 / vector / hybrid / hybrid+rerank

## How it decides
The model retrieves and explains. A deterministic resolver decides.
Every claim carries a clause that existed and was in force on the date asked —
and states separately whether anyone was enforcing it.

## What it doesn't do
Not legal advice. No booking. No airline contact. Schedules are historical
(BTS) — the demo replays a real disruption day, which is why it's reproducible.

## Run it
make start   # api, ui, qdrant, langfuse-cloud — ~2GB
```

**`decisions.md` — one entry per milestone.** What / why over the alternative / failure
mode / what changes at 10× and 100× / which interview question it answers. Write it as
you go; reconstructing it at the end produces something that reads reconstructed. This
file is the single highest-value hour of the project for interview prep, because it
turns every choice you made into a rehearsed answer.

**First entry is already written for you:** the September 22 probe. The hypothesis, the
negative result, what you changed, and why the replacement is better. A decision log
that opens with a disproved assumption is worth more than one that opens with a
success.

---

## §16 · Open items

| # | Item | Why it matters |
|---|---|---|
| 1 | **Verify the six-day gap by reading FR 2026-13675's own text.** The gap is inferred from the two notices' published dates and stated expiries. If DOT framed the extension as continuous with the first, the gap is narrative rather than legal, and the family-6 cases dated 1–6 July 2026 need their expected answers rewritten. | The four-regime architecture stands either way; only those cases move. Pin whichever answer you find with a test and a dated note in `decisions.md`. |
| 2 | **Confirm Refund III (RIN 2105-AF36) hasn't published.** Both notices reference a pending rulemaking that would formally change the definition of "cancelled flight." If it has landed since July 2026, there is a fifth regime — and possibly an actual CFR text change, which would make Layer 1 live after all. | Would materially improve the project. Check before M3. |
| 3 | **Rebase the schedule.** §11's dates have passed. Pick a new Day 1. | — |
| 4 | **Recheck the GCP credit boundary** against the actual account state. | §12 assumed a specific lapse date. |
| 5 | **Scaffold the repo.** Not started. Monorepo layout, thesis README, Makefile, pydantic models from the §4 contracts, `.gitignore`, `git init`. | The S0 item that unblocks Day 1. |
| 6 | **Decide embeddings: local bge vs Vertex.** Affects §13 and whether the local run mode has one cloud dependency or two. | Minor cost, real simplicity difference. |

### Deliberately out of scope

Dropped from the earlier six-project portfolio and not coming back: dbt marts,
Airflow/Composer, Tableau dashboards, executive memos, PySpark, A/B-testing
methodology, causal inference, transformer fine-tuning for NER. These aimed at the half
of the market that contracted; cutting them is the decision, not a casualty of it.

Bolt-ons that fit onto Rerouted later, 1–3 days each, none blocking: red-team suite
(30 prompts, 3–4 families against the guard) · MCP depth (client, sampling, roots,
approval gate) · semantic cache with a measured hit rate · MLflow on the delay model ·
drift → retrain trigger with human-gated promotion.

The one extraction worth prioritizing: **`agenteval` as a pip-installable package.**
`core/eval/` already *is* that harness. A `predict(question) -> dict` adapter, a
leaderboard and a short write-up is maybe two days, and it is what moves the portfolio
from "student with projects" to "shipped something other people can use."

---

## §17 · What you say about it

### The 30-second version

> "It answers what an airline owes you when a flight is cancelled, citing the rule. I
> went in assuming the interesting part would be versioned regulation text — and when I
> actually probed eCFR, the text hadn't changed at all. What changed was enforcement:
> DOT paused enforcement for renumbered flights, let the pause lapse for six days, then
> extended it. So the corpus is two layers — text and enforcement posture — filtered
> independently by the date of the disruption. And the model doesn't decide entitlement;
> a deterministic resolver does, so the decision is unit-testable and identical every
> run."

### The question this is really designed to earn

> **"Inside the carve-out window, is the passenger owed a refund?"**
>
> Yes. DOT just won't enforce it. Keeping obligation and enforceability as separate
> fields, and saying both in the answer, is the thing I'd point at. Every system that
> models this as one boolean tells a stranded passenger they have no claim, and that's
> false.

### When they push on evaluation

> "Eighty cases in eight families, including negatives and a family that must refuse —
> over-refusal is scored, so I can't game it by declining everything. I score citation
> precision separately from answer accuracy, because an answer that's right for the
> wrong reason is a latent bug. And I checked judge variance before gating CI on it: I
> scored twenty answers twice and compared the spread against my threshold, because a
> gate that fires on noise trains you to ignore it."

Neither of those is a claim about model sophistication. They're claims about judgement
under constraint, which is what the system-design round is actually scoring.

---

## Sources

Probed live 22 September 2026:

- **eCFR Part 260 version history** — https://www.ecfr.gov/api/versioner/v1/versions/title-14.json?part=260
  (two version dates, both 2024; none since)
- **FR 2025-22140**, 5 Dec 2025, "Airline Refunds and Other Consumer Protections" —
  https://www.federalregister.gov/documents/2025/12/05/2025-22140/airline-refunds-and-other-consumer-protections
  (notice of enforcement discretion; pause through 30 Jun 2026; §260.6, §260.9,
  §399.80(l); references Refund III, RIN 2105-AF36)
- **FR 2026-13675**, 7 Jul 2026, same title —
  https://regulations.justia.com/regulations/fedreg/2026/07/07/2026-13675.html
  (extends the pause one year, expiring 7 Jul 2027; explicitly not a rulemaking)
- **BTS TranStats** — https://transtats.bts.gov/Marketing_Annual.aspx
  (current through December 2025)

Pricing verified 22 September 2026:

- **Cloud Run pricing** — https://cloud.google.com/run/pricing
- **Gemini API pricing** — https://ai.google.dev/gemini-api/docs/pricing
- **Vertex AI pricing** — https://cloud.google.com/vertex-ai/pricing
- **GCP free trial FAQs** — https://cloud.google.com/signup-faqs

### Relationship to RLAIF

No technical relationship. Separate repos, no shared code or data. The link is domain
coherence for a reader: RLAIF is a flight-booking agent, Rerouted is a
flight-disruption agent, so someone scanning the GitHub profile sees a direction rather
than a scatter. What carried across is experience, not architecture — Vertex serving
already done once, the PPO/DPO instinct for comparing two approaches on stability
rather than final score, and flight-domain familiarity that shortens the rules reading.
Nothing in this plan is blocked by RLAIF, and nothing here breaks if it never ships.
