# Local BTS + NOAA LCD data pipeline

This pipeline acquires the official BTS TranStats monthly on-time ZIPs and NOAA
NCEI LCDv1 station-year CSVs, then builds a local partitioned Parquet dataset and
DuckDB database. It does not use GCP or any other cloud storage.

Install the pipeline dependencies and inspect its options:

```sh
uv sync --extra dev --extra data
uv run python -m domains.air_us.data_pipeline --help
uv run python -m domains.air_us.data_pipeline all --help
```

The current model uses a chronological 60/20/20 split by distinct UTC prediction day
for training, calibration/conformal validation, and held-out testing. UTC prediction
timestamps govern chronology because local service dates can move backward when
flights from multiple time zones are combined. The following local
build stops at the final full day before the official LCDv1 feed ended in August 2025:

```sh
uv run python -m domains.air_us.data_pipeline all \
  --start 2025-01-02 --end 2025-08-24 \
  --output-dir data/processed/flight_features_2025 \
  --top-airports 10 \
  --model data/model/delayrisk.json
```

The acquisition automatically retrieves an additional 365 days of BTS and LCD
history before `--start` to seed trailing features. The output dataset itself
contains only the requested dates. Airport selection ranks origin airports by
flight count within that requested date range. Use `--airports JFK,SFO,...` to
choose airports explicitly. Every selected origin needs both a time-zone row and
an explicit LCD station row. Destinations need time-zone rows only for aircraft
turn calculations; when unavailable, the turn feature has no observed prior
arrival evidence for that destination.

BTS ZIP members are streamed: the first pass counts airports in the requested
range, and the second pass normalizes only selected origins. The normalized
selected rows are held for global chronological ordering; raw BTS rows are not
retained. Each run manifest records process peak resident memory and the number of
normalized rows held so an actual acquisition provides an environment-specific
measurement. The fixture test is intentionally too small to estimate full-year
memory use.

The output directory contains:

- `flights/`: Parquet files partitioned by year and month;
- `analytics.duckdb`: a materialized `flights` table;
- `training.csv`: the existing model's feature and target contract;
- `manifest.json`: source URLs and SHA-256 values, filters, station/time-zone map
  hashes, feature schema, Parquet hashes, benchmark SQL and benchmark result;
- the trained model JSON and a sibling `.manifest.json` with the model data hash,
  chronological evaluation metrics and input dataset manifest hash.

To build without training, use the `build` subcommand. To train an already built
dataset, use `train --dataset-dir ... --model ...`. When a publisher supplies a
checksum, `fetch --checksums checksums.json` accepts a JSON object mapping source
URLs to SHA-256 hex digests; downloaded and cached bytes are checked against it.
Regardless of pinned digests, BTS ZIP CRC, CSV headers, and nonempty BTS data are
validated. Computed SHA-256 values are always recorded in provenance manifests.

Use `refresh` to materialize or refresh an existing local dataset without training
or fetching. An unchanged source snapshot and intact output hashes produce an
idempotent no-op, recorded as `current_at_last_refresh_check`. Exact duplicate BTS
flight rows are counted and removed; records with the same flight identity but
different values fail closed. Dedupe state stores fixed-size cryptographic digests
and is retained only while monthly source date ranges overlap, so warmup processing
memory does not grow across the full source history. A changed BTS ZIP or LCD station-year checksum marks
the source snapshot as corrected and rebuilds the complete requested date window,
because rolling history and weather joins can propagate a correction beyond its
source month. Refresh builds beside the current dataset and validates SQL quality
checks before promotion. Promotion renames the current directory to a backup and
then renames the staged directory into place, restoring the backup if the second
rename fails. These two filesystem renames are not one atomic operation: readers can
briefly see the output path absent, and a process or machine interruption between
renames can leave the previous dataset at the backup path. The manifest records source snapshot lineage, changed
source digests, freshness at the local check time, duplicate counts, and SQL check
results. This freshness status only describes the files present locally; it does
not verify whether BTS or NOAA has published newer data.

```sh
uv run python -m domains.air_us.data_pipeline refresh \
  --start 2025-01-02 --end 2025-08-24 \
  --raw-dir data/raw \
  --output-dir data/processed/flight_features_2025 \
  --top-airports 10
```

## Reference data coverage

`config/airport_timezones.csv` and `config/airport_weather_stations.csv` are
editable explicit mappings. The checked-in reference profile covers 53 large
U.S. airports, with explicit IANA time zones and NOAA LCD station identifiers.
The checked-in model profile uses the ten busiest origins in its stated source
window; the wider reference table supports deliberate future rebuilds.
The station reference includes NCEI HOMR/CDO provenance links; review each
airport/station pairing against NCEI metadata before using the profile for a
production claim. The pipeline validates time-zone identifiers and mapping
uniqueness and fails with missing airport codes instead of guessing a station.
Reference coverage is not evidence that a 12-month acquisition, materialization,
or model run has succeeded.

HOU uses the nearby IAH LCD station because the Hobby LCDv1 file is absent from the
official bulk-year inventory; FLL uses nearby Fort Lauderdale Executive for the same
reason. These are explicit fallbacks, not hidden proximity guesses.

The timezone names use the IANA database. Station identifiers are the legacy
LCDv1 identifiers used by the NOAA bulk station-year endpoint. Check each
station's airport association against NCEI station metadata before relying on or
extending the table. `config/carrier_reference.csv` provides a BTS carrier-code
label reference; unknown codes are retained in the data and listed in the
dataset manifest.
Sources: [BTS TranStats PREZIP](https://transtats.bts.gov/PREZIP/), [NCEI LCDv1
bulk-access documentation](https://www.ncei.noaa.gov/oa/local-climatological-data/v2/doc/lcdv2_DOCUMENTATION.pdf)
(the documentation notes that LCDv1 files remain available and describes LCDv2
separately), and [NCEI HOMR](https://www.ncei.noaa.gov/access/homr/).

The weather feature joins the latest mapped-station LCD observation at or before
scheduled departure UTC, after interpreting the station's offset-free LCDv1 time
using the mapped IANA timezone. If no observation falls within the configured
age window, the dataset build fails; missing weather is never silently labeled
clear. Route and carrier-origin on-time rates use only flights strictly earlier
than the current scheduled departure and within the preceding 365 days. Equal
scheduled departure instants are excluded from one another's history. The
aircraft-turn proxy uses prior observed, non-cancelled arrival delay for the same
tail among the fetched selected-origin rows, only when the prior scheduled arrival
time can be timezone-normalized and actual arrival preceded the current scheduled
departure. Rows whose destinations are outside the configured timezone reference
cannot seed this proxy. Route and carrier-origin outcomes become eligible only at
their outcome-availability timestamp: actual arrival for observed, non-cancelled
flights, and scheduled departure for cancellations. The rolling-window age remains
anchored to the prior flight's scheduled departure. Thus a flight still in the air
cannot contribute its eventual on-time result to a later departure's feature.

Real-data metrics are written only by a successful local `all` or `train` run and
remain tied to the source and model manifests. Bulk raw/processed data are ignored by
Git; the compact model and its manifest are local reproducible artifacts.
To bound memory and CPU, model fitting uses deterministic evenly spaced samples of at
most 50,000 training rows, 20,000 validation rows, and 50,000 held-out rows from the
chronological folds. The manifest records both full dataset size and sampling policy;
the feature dataset itself retains every filtered flight row.

## Current local model run

On 2026-10-23, the dataset was rebuilt from the existing local raw sources after a
temporal-feature correction, then the model was retrained. No sources were fetched:

```sh
.venv/bin/python -m domains.air_us.data_pipeline refresh \
  --start 2025-01-02 --end 2025-08-24 \
  --raw-dir data/raw \
  --output-dir data/processed/flight_features_2025 \
  --top-airports 10
.venv/bin/python -m domains.air_us.data_pipeline train \
  --dataset-dir data/processed/flight_features_2025 \
  --model data/model/delayrisk.json
```

This used 1,531,942 feature rows covering service dates 2025-01-02 through 2025-08-24
and the ten selected origins ATL, CLT, DEN, DFW, LAS, LAX, MCO, ORD, PHX, and SEA.
The sampled UTC prediction-day folds cover 2025-01-02–2025-05-22 for training
(50,000 rows), 2025-05-23–2025-07-08 for validation (20,000 sampled rows), and
2025-07-09–2025-08-25 UTC for held-out testing (50,000 sampled rows). The last UTC
day contains flights whose local service date is Aug 24. Their prediction timestamp
bounds are 2025-01-02T07:28Z–2025-05-22T23:59Z, 2025-05-23T00:00Z–2025-07-08T23:59Z,
and 2025-07-09T00:00Z–2025-08-25T06:59Z. Delay evaluation excludes test rows without
observed arrival-delay labels, leaving 48,966 eligible test rows.

Isotonic calibration is selected only when its Brier score on the disjoint validation
conformal subset is no worse than the raw estimator's. It was not selected for either
target. The chronological held-out metrics are:

| Target | Calibration selected | Test rows | Brier, raw → served | ECE, raw → served |
| --- | --- | ---: | ---: | ---: |
| Arrival delay ≥15 minutes (`ArrDel15`, observed labels only) | No | 48,966 | 0.2132 → 0.2132 | 0.0961 → 0.0961 |
| Cancellation (`Cancelled`) | No | 50,000 | 0.0163 → 0.0163 | 0.0028 → 0.0028 |

The held-out cancellation rate is 1.66%, so its Brier and ECE should be read in light
of the rare positive class; they are not standalone evidence of useful ranking or
decision thresholds. Delay calibration remains imperfect, with held-out ECE 0.0961.
Metrics describe a single sampled future window from this airport/date profile. They
do not establish performance at other airports, under distribution shift, or in live
operations.

The regenerated artifacts link to the inputs by SHA-256: `training.csv`
`f8cd001b1554f082ee912b857f4cb61d7e36f78d074e64f2df693eca87dba972`, dataset
`manifest.json` `339637345083316a6cb5464faa5eef367ed4832c676b947aecf6fad7fe763c7d`, and
model `delayrisk.json` `8f690b8f7a5e921b40fb9f10145b15b0c62e6982ab4201c167a126ecb444364b`.
The source snapshot SHA-256 is
`309408dcd4ed2e1daa54b0a58ce01326232094701a4b4afc37f007ea06f4b421`; the training
manifest records and matches the dataset, training CSV, and model hashes. It retains
the 128 BTS/NOAA source artifact hashes; no source files were downloaded in this run.
The feature rebuild recorded 3,887,418 normalized rows for temporal ordering, no raw
BTS rows retained, and a peak RSS of 5,646,352,384 bytes.

## Verification

The tests use tiny local ZIP/CSV fixtures only; they make no network calls. The
end-to-end fixture verifies Parquet and DuckDB output, benchmark query results,
and both delay and cancellation targets when the optional data dependencies are
installed.
