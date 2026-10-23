# Source corpus provenance

Reviewed and retrieved on **2026-10-08**. These are retrieval-ready clause excerpts
captured from primary official pages. The `source_sha256` in `data/rules/documents.jsonl`
hashes the exact local snapshot bytes listed in each record; it is not represented as a
hash of the full upstream webpage. Clause hashes cover normalized clause text.

## Reviewed official sources

| Carrier/source | Official URL | Published version or update date | Retrieval date |
|---|---|---:|---:|
| American Conditions of Carriage | <https://www.aa.com/web/i18n/customer-service/support/conditions-of-carriage.html> | Updated 2026-08-13 | 2026-10-08 |
| Delta U.S. Contract of Carriage | <https://www.delta.com/us/en/legal/contract-of-carriage-dgr> | Last modified 2026-08-18 | 2026-10-08 |
| United Contract of Carriage | <https://www.united.com/en/us/fly/contract-of-carriage.html> | Revised 2026-09-30 | 2026-10-08 |
| DOT Airline Cancellation and Delay Dashboard | <https://www.transportation.gov/airconsumer/airline-cancellation-delay-dashboard> | Page last updated 2025-09-11 | 2026-10-08 |

The source snapshots in `data/rules/sources/` contain only the exact excerpts used for
the clause records, not complete contract documents. Their clause IDs identify the
carrier, section, and topic. DOT records use `authority=commitment`; a commitment is
not a regulation or a carrier contract. DOT dashboard records are bounded to the
observed window from the page's stated update date through retrieval, and their
metadata explicitly says that this is a snapshot observation window, not a legal
effective-period finding. DOT says the dashboard reflects controllable delays and
cancellations; the records must not imply that an unchecked/red-X service is never
provided.

United's page is JavaScript-rendered. A browser-readable official render was captured
after the page loaded, and the dated revision and Rules 24/27 anchors were checked
before clauses were indexed. For future updates, provide a rendered HTML/PDF/text
snapshot to the CLI or use `--fetch`; if the supplied text no longer contains every
reviewed anchor and clause, ingestion exits without writing an index.

## Scope and date limits

Contract source version dates are carried as the first date in the local indexed
window because the official pages publish those dates. They do not reconstruct prior
contract versions; do not use these excerpts to answer historical ticket-date questions
before the recorded version date. Recheck the source and review the affected clauses
before extending or replacing any interval. The U.S. DOT dashboard page currently
states a 2025-09-11 update date; this capture therefore records the displayed state
observed through 2026-10-08 and does not assert when an individual carrier commitment
first became operative.

The live local corpus contains **273 official eCFR effective-interval records** plus
13 reviewed contract/commitment records. It preserves parent paragraphs whose text
is independently operative, groups definition sections under their official section
citation, and exposes stable clause IDs while retaining versioned storage IDs.

The eCFR and Federal Register parser samples under `tests/fixtures/` are explicitly
synthetic parser fixtures, not official source snapshots. They are never copied into
the live retrieval corpus. The local regulatory fetcher below downloads official
eCFR Versioner XML for Parts 260, 259, and §399.80 at the effective dates returned by
the Versioner history endpoint, parses those snapshots through `ecfr.py`, and merges
the resulting dated regulation clauses into the existing contract/commitment JSONL.
Each regulation record carries its query date, version effective date, official XML
URL, exact source SHA-256, and saved source path. Re-running is idempotent; a
Each refresh replaces the regulation-owned slice atomically so parser or upstream
changes cannot leave stale regulation rows; contract and commitment rows are preserved.

Federal Register notices 2025-22140 and 2026-13675 are downloaded as metadata and
text snapshots under `data/rules/sources/`. The fetcher verifies their document
identity, title, publication date, official URL, reviewed text anchors, and interval
against the reviewed `domains/air_us/posture.yaml` manifest. These notices remain a
separate enforcement-posture layer and are not inserted into `documents.jsonl` as
ranked clauses. The parser tests use local fixtures only and never access the network.

## Ingest

```bash
.venv/bin/python -m domains.air_us.ingest.source_corpus \
  --manifest data/rules/source_manifest.json \
  --output data/rules/documents.jsonl
```

The manifest is the review gate. To test a changed upstream source, pass the saved
file with `--input SOURCE_ID=PATH`; a changed review anchor or excerpt fails closed.

Fetch and merge the regulatory corpus (requires network access):

```bash
.venv/bin/python -m domains.air_us.ingest.regulatory_corpus \
  --start-date 1996-01-01 \
  --query-date 2026-10-08 \
  --retrieved-on 2026-10-08
```

The `--start-date` controls the historical version window; `--query-date` is the
eCFR version-history cutoff; `--retrieved-on` records when the source bytes were
queried. Use the actual current query and retrieval dates when refreshing. The command
validates all source inputs before writing snapshots or the combined corpus. If eCFR
response schema, dates, or reviewed Federal Register anchors change, it exits without
merging records and requires source review.
