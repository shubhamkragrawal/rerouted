# Rerouted

Rerouted explains realistic alternatives after a US flight disruption, what published
rules and airline contracts say the airline owes, and whether the relevant DOT
obligation was being enforced on the date of travel. Every material conclusion is
designed to carry a clause-level citation.

> This explains what published DOT rules and airline contracts of carriage say, with
> citations. It is not legal advice, it does not contact the airline, and what you
> actually receive may differ.

## Current status

The local app exposes all four locked tools, a guarded deterministic `/answer` route,
Streamlit UI, and MCP Python SDK v2 transports. The reviewed rule corpus is checked
in; official BTS/NOAA bulk data and generated analytical files remain local and
git-ignored. Setup and tests do not download data or start services. Readiness reports
each configured component, and routes fail closed when a needed artifact is absent.
See `docs/STATUS.md` for measured status and provenance.

## Product boundaries

- US air-travel disruptions only.
- No booking, airline contact, or prediction of what an airline *will* do.
- Entitlement and DOT enforcement posture are separate outputs. An obligation can
  remain owed while DOT has paused enforcement.
- Alternative flights come from a historical BTS schedule and are replayable; they
  are not live inventory.
- Historical weather comes from NCEI LCD; live weather comes from NWS.

## Local setup

Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/) are recommended.

```bash
cp .env.example .env
make setup
make start
```

The API is at `http://localhost:8000`, its health response is at `/health`, its
startup/readiness gate is at `/ready`, and its build version is at `/version`. The
Streamlit interface is at `http://localhost:8501`.

Local API requests are limited to 1 MiB and 120 requests per client address per
minute. Responses include opaque `X-Trace-ID` and `X-Session-ID` headers; valid UUIDs
provided by a caller are preserved. Structured JSON telemetry is written to the local
application log. OpenTelemetry and Langfuse support are unimplemented scaffolding;
there are no spans or exporters. Set `REROUTED_TELEMETRY_EXPORT=none` for the explicit offline mode;
it is the only supported setting in this build. Events omit query text, request bodies,
contact data, exception messages, and tool arguments. Langfuse/OpenTelemetry export is
not configured locally.

Configure local files through `config/default.yaml` (or `REROUTED_CONFIG_PATH`). The
default paths point to `data/rules/documents.jsonl`, `data/model/delayrisk.json`,
monthly `data/raw/bts/YYYY-MM.zip`, station-year files under `data/raw/lcd/`, and the
checked-in airport reference CSVs. Missing artifacts are not fetched automatically.
The answer route is limited to the 53 configured US airport codes.
Live weather calls NWS only when explicitly requested and a valid contact-bearing
User-Agent is configured; tests use injected clients and make no network calls.

The local BTS + NOAA LCD acquisition, Parquet/DuckDB materialization, and real-data
model workflow is documented in [`docs/real_data_pipeline.md`](docs/real_data_pipeline.md).
It requires `uv sync --extra data`; setup and tests do not fetch or retrain it.

## Local evaluation

Run the deterministic 80-case answer-service evaluation with
`uv run python scripts/run_local_eval.py`. It makes no LLM or cloud calls and writes
the versioned report under `data/eval/results/`. The current result is
`data/eval/results/local-eval-v2.json`.

| Local evaluation v2 | Result |
|---|---:|
| Answer cases | 80 |
| Entitlement accuracy | 100% (80/80) |
| Posture correctness | 100% (80/80) |
| Citation precision / recall | 100% / 100% (200/200) |
| Refusal accuracy | 100% (80/80) |
| Hybrid + rerank recall@10 | 0.7333 |
| p95 latency | about 13.9 ms |
| Mean measured cost | $0.00/query (no model calls) |
| Tool error rate | 0% |
| CI | Pending approved baselines and external judge repeatability |

These are deterministic synthetic-case measurements, not human legal validation or
real-world passenger-outcome accuracy. The full method and limitations are in
[`docs/eval_methodology.md`](docs/eval_methodology.md).

The four tools are also available through MCP. Use `make mcp` to run its local stdio
transport for an MCP host. The API hosts the SDK's Streamable HTTP MCP app alongside
the REST routes. Alternatives are explicitly historical BTS schedule replays, not
live inventory.

To run checks:

```bash
make check
```

Docker is optional. `docker compose up --build` starts only the API and UI and mounts
the local `data/` directory read-only. No vector database, cloud account, or remote
service is required for startup.

No GCP resources or deployment configuration are included.

## Repository map

```text
core/            Domain-neutral agent, retrieval, resolver, evaluation, and contracts
domains/air_us/  US-air rules, source ingestion, delay model, and golden cases
local_app/       Typed local controller and provider-neutral planner boundary
api/             FastAPI application
ui/              Streamlit application
config/          Local-first runtime defaults
docs/            Decisions, methodology, and architecture notes
tests/           Unit and integration tests
```

## Contract stability

The four tool request/response models in `core/contracts/tools.py` are the integration
boundary for the agent, API, and tool implementations. They reject unknown fields and
should be changed only through a reviewed schema migration.

## License

MIT
