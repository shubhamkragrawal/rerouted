# Local-first operating mode

The current build deliberately has no GCP dependency.

Durable artifacts live under version control or in ignored local data directories:

- raw source snapshots in `data/raw/`;
- normalized Parquet and corpus artifacts in `data/processed/`;
- trained model artifacts in `models/`;
- retrieval indexes generated from the normalized corpus.

The application interfaces remain deployment-neutral. A future cloud deployment may
replace storage and serving implementations, but it must preserve the four public tool
contracts and resolver behavior.

The local FastAPI service exposes `/health`, `/ready`, and `/version`. `/ready` returns
503 until the configured airport scope, rules, posture, delay model, historical weather,
and alternatives artifacts are ready; live NWS configuration remains optional. Request
bodies are capped at 1 MiB, and a bounded in-process limiter allows 120 requests per
client address per minute by default. Local structured JSON events carry opaque trace and
session IDs and safe operational metadata only. `REROUTED_TELEMETRY_EXPORT=none` is the
supported no-export setting; there is no local Langfuse account or remote exporter.

The local controls are process-local. They do not provide a shared rate limit across
workers, persistent trace storage, cloud secret injection, hosted logging, deployment
rollback, or remote LLM tracing.

Cloud work is deferred until a target account is provided. No cloud resource should be
created as part of local setup or tests.
