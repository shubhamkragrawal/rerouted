"""Load local artifacts and assemble the four US air-travel tools."""

from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, TextIO

import yaml  # type: ignore[import-untyped]

from core.contracts.tools import (
    DelayRiskRequest,
    DelayRiskResponse,
    FindAlternativesRequest,
    FindAlternativesResponse,
    SearchRulesRequest,
    SearchRulesResponse,
    WeatherContextRequest,
    WeatherContextResponse,
)
from core.retrieval.models import IndexedDocument
from domains.air_us.ingest.bts import BTSFlight, parse_bts_rows
from domains.air_us.ingest.nws import NWSClient
from domains.air_us.model import DelayRiskModel
from domains.air_us.rules.posture import PostureRegistry, load_default_posture
from domains.air_us.tools.delay_risk import delay_risk
from domains.air_us.tools.find_alternatives import find_alternatives
from domains.air_us.tools.search_rules import SearchRulesService
from domains.air_us.tools.weather_context import weather_context


class ArtifactError(ValueError):
    """Raised when an available local artifact is malformed or inconsistent."""


@dataclass(frozen=True)
class ComponentReadiness:
    ready: bool
    detail: str


def _relative_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _load_airport_settings(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    airports = raw.get("airports", {})
    if not isinstance(airports, dict):
        raise ArtifactError("configuration field 'airports' must be a mapping")
    return {
        str(code).upper(): dict(value)
        for code, value in airports.items()
        if isinstance(value, dict)
    }


def _load_supported_bts_rows(
    stream: TextIO,
    *,
    flight_date: date,
    airport_timezones: Mapping[str, str],
) -> tuple[BTSFlight, ...]:
    """Parse only routes within the runtime's configured airport scope.

    BTS monthly snapshots include US airports outside the supported reference
    set. Their unrelated rows must not make a supported route query fail because
    those airports have no configured timezone.
    """

    rows = csv.DictReader(stream)
    supported_rows = (
        row
        for row in rows
        if str(row.get("Origin", row.get("ORIGIN", ""))).strip().upper()
        in airport_timezones
        and str(row.get("Dest", row.get("DEST", ""))).strip().upper() in airport_timezones
    )
    return parse_bts_rows(
        supported_rows,
        flight_date=flight_date,
        airport_timezones=airport_timezones,
    )


def _read_index(path: Path) -> tuple[IndexedDocument, ...]:
    documents: list[IndexedDocument] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ArtifactError(f"rules index line {line_no} must be a JSON object")
                documents.append(
                    IndexedDocument(
                        document_id=str(item["document_id"]),
                        text=str(item["text"]),
                        authority=str(item["authority"]),
                        effective_from=date.fromisoformat(str(item["effective_from"])),
                        effective_to=(
                            date.fromisoformat(str(item["effective_to"]))
                            if item.get("effective_to")
                            else None
                        ),
                        source_url=str(item["source_url"]),
                        carrier=str(item["carrier"]) if item.get("carrier") else None,
                        doc_type=str(item.get("doc_type", item["authority"])),
                        metadata=dict(item.get("metadata", {})),
                    )
                )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, ArtifactError):
            raise
        raise ArtifactError(f"could not load rules index {path}: {exc}") from exc
    ids = [document.document_id for document in documents]
    if len(ids) != len(set(ids)):
        raise ArtifactError("rules index contains duplicate document_id values")
    return tuple(documents)


class AirUSServices:
    """Runtime services backed only by configured local files and NWS live API."""

    def __init__(self, *, config_path: Path | None = None) -> None:
        selected_config = config_path or Path(
            os.environ.get("REROUTED_CONFIG_PATH", "config/default.yaml")
        )
        selected_config = selected_config.expanduser().resolve()
        project_root = selected_config.parent.parent
        try:
            raw_config = yaml.safe_load(selected_config.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ArtifactError(
                f"could not load local configuration {selected_config}: {exc}"
            ) from exc
        if not isinstance(raw_config, dict):
            raise ArtifactError("configuration root must be a mapping")

        paths = raw_config.get("paths", {})
        if not isinstance(paths, dict):
            raise ArtifactError("configuration field 'paths' must be a mapping")
        self.config_path = selected_config
        self.project_root = project_root
        self._path_values = {str(key): str(value) for key, value in paths.items()}
        self._airports = _load_airport_settings(raw_config)
        timezone_reference = self._configured_path("airport_timezones")
        if timezone_reference and timezone_reference.is_file():
            with timezone_reference.open(newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    code = str(row.get("airport", "")).strip().upper()
                    zone = str(row.get("time_zone", "")).strip()
                    if code and zone:
                        self._airports.setdefault(code, {})["timezone"] = zone
        station_reference = self._configured_path("airport_weather_stations")
        if station_reference and station_reference.is_file():
            with station_reference.open(newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    code = str(row.get("airport", "")).strip().upper()
                    station = str(row.get("station_id", "")).strip()
                    if code and station:
                        self._airports.setdefault(code, {})["ncei_station"] = station
        airport_metadata = self._configured_path("airport_metadata")
        if airport_metadata and airport_metadata.is_file():
            try:
                metadata_payload = (
                    yaml.safe_load(airport_metadata.read_text(encoding="utf-8")) or {}
                )
                metadata_airports = _load_airport_settings({"airports": metadata_payload})
            except (OSError, yaml.YAMLError, ArtifactError) as exc:
                raise ArtifactError(
                    f"could not load airport metadata {airport_metadata}: {exc}"
                ) from exc
            self._airports.update(metadata_airports)
        self._nws_config = raw_config.get("nws", {})
        self._data_version = raw_config.get("data_version", {})

        self._documents: tuple[IndexedDocument, ...] = ()
        self._rules_error: str | None = None
        self._rules_path = self._configured_path("rules_documents")
        if self._rules_path and self._rules_path.is_file():
            try:
                self._documents = _read_index(self._rules_path)
            except ArtifactError as exc:
                self._rules_error = str(exc)
        self.search_service = SearchRulesService(
            self._documents,
            corpus_version=str(self._data_version.get("rules", "rules-local")),
        )
        self.posture_registry: PostureRegistry = load_default_posture()

        self.model: DelayRiskModel | None = None
        self._model_error: str | None = None
        self._model_path = self._configured_path("delay_model")
        if self._model_path and self._model_path.is_file():
            try:
                self.model = DelayRiskModel.load(self._model_path)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._model_error = f"could not load delay model: {exc}"

        self._airport_timezones = {
            code: str(info["timezone"])
            for code, info in self._airports.items()
            if info.get("timezone")
        }
        self.us_airport_codes = frozenset(self._airport_timezones)
        self._airport_stations = {
            code: str(info["ncei_station"])
            for code, info in self._airports.items()
            if info.get("ncei_station")
        }
        nws_locations = {
            code: (float(info["latitude"]), float(info["longitude"]))
            for code, info in self._airports.items()
            if info.get("latitude") is not None and info.get("longitude") is not None
        }
        user_agent = (
            str(self._nws_config.get("user_agent", ""))
            if isinstance(self._nws_config, dict)
            else ""
        )
        self._nws_client: NWSClient | None = None
        self._nws_error: str | None = None
        if nws_locations and user_agent and "example.invalid" not in user_agent:
            try:
                self._nws_client = NWSClient(nws_locations, user_agent=user_agent)
            except ValueError as exc:
                self._nws_error = str(exc)
        elif not user_agent or "example.invalid" in user_agent:
            self._nws_error = "configure a valid NWS User-Agent with contact information"

        self._bts_template = self._path_values.get("bts_schedule_template")
        self._ncei_template = self._path_values.get("ncei_station_template")

        self.readiness = self._build_readiness()

    def _configured_path(self, key: str) -> Path | None:
        value = self._path_values.get(key)
        return _relative_path(self.project_root, value) if value else None

    def _build_readiness(self) -> dict[str, ComponentReadiness]:
        bts_directory = (
            self._configured_path_for_template(self._bts_template).parent
            if self._bts_template
            else None
        )
        ncei_directory = (
            self._configured_path_for_template(self._ncei_template, station="station").parent
            if self._ncei_template
            else None
        )
        bts_ready = bool(
            self._airport_timezones
            and bts_directory
            and (
                any(bts_directory.rglob("*.csv"))
                or any(bts_directory.rglob("*.zip"))
            )
        )
        ncei_ready = bool(
            self._airport_stations
            and self._airport_timezones
            and ncei_directory
            and any(ncei_directory.rglob("*.csv"))
        )
        return {
            "airport_scope": ComponentReadiness(
                bool(self.us_airport_codes),
                f"{len(self.us_airport_codes)} configured US airport codes"
                if self.us_airport_codes
                else "configure at least one airport with its IANA time zone",
            ),
            "search_rules": ComponentReadiness(
                bool(self._documents) and self._rules_error is None,
                self._rules_error
                or (
                    f"loaded {len(self._documents)} local rule documents"
                    if self._documents
                    else f"rules index missing: {self._rules_path}"
                ),
            ),
            "posture": ComponentReadiness(True, "reviewed posture registry loaded"),
            "delay_risk": ComponentReadiness(
                self.model is not None,
                self._model_error
                or (
                    f"loaded model {self.model.metadata.get('model_version')}"
                    if self.model
                    else f"model artifact missing: {self._model_path}"
                ),
            ),
            "weather_live": ComponentReadiness(
                self._nws_client is not None,
                self._nws_error
                or (
                    "NWS configured for known airports"
                    if self._nws_client
                    else "airport NWS coordinates are not configured"
                ),
            ),
            "weather_historical": ComponentReadiness(
                ncei_ready,
                "NCEI station files and airport time zones configured"
                if ncei_ready
                else "NCEI CSV data, station IDs, and airport time zones must be configured",
            ),
            "alternatives": ComponentReadiness(
                bts_ready,
                "local historical BTS schedule and airport time zones configured"
                if bts_ready
                else "local BTS CSV schedule data and airport time zones must be configured",
            ),
        }

    def _configured_path_for_template(self, template: str, **values: Any) -> Path:
        format_values: dict[str, Any] = {"year": 2025, "month": 1, "station": "station"}
        format_values.update(values)
        rendered = template.format(**format_values)
        return _relative_path(self.project_root, rendered)

    @property
    def indexed_documents(self) -> tuple[IndexedDocument, ...]:
        return self._documents

    def search_rules(self, request: SearchRulesRequest) -> SearchRulesResponse:
        return self.search_service.search(request)

    def predict(self, request: DelayRiskRequest) -> DelayRiskResponse:
        if self.model is None:
            raise RuntimeError(self.readiness["delay_risk"].detail)
        return delay_risk(request, self.model)

    def weather(self, request: WeatherContextRequest) -> WeatherContextResponse:
        station_files: dict[str, Path] = {}

        def provider(station: str, start: Any, end: Any) -> tuple[dict[str, str], ...]:
            template = self._ncei_template
            if not template:
                raise RuntimeError(self.readiness["weather_historical"].detail)
            rows: list[dict[str, str]] = []
            for year in range(start.year, end.year + 1):
                path = self._configured_path_for_template(
                    template, station=station, year=year
                )
                if not path.is_file():
                    raise RuntimeError(f"NCEI station file missing: {path}")
                station_files[f"{station}:{year}"] = path
                with path.open(newline="", encoding="utf-8-sig") as stream:
                    rows.extend(csv.DictReader(stream))
            return tuple(rows)

        return weather_context(
            request,
            nws_client=self._nws_client,
            historical_provider=provider,
            airport_stations=self._airport_stations,
            airport_timezones=self._airport_timezones,
        )

    def alternatives(self, request: FindAlternativesRequest) -> FindAlternativesResponse:
        if not self._bts_template:
            raise RuntimeError(self.readiness["alternatives"].detail)
        path = self._configured_path_for_template(
            self._bts_template,
            year=request.flight_date.year,
            month=request.flight_date.month,
        )
        if not path.is_file():
            raise RuntimeError(f"historical BTS schedule file missing: {path}")
        if path.suffix.casefold() == ".zip":
            with zipfile.ZipFile(path) as archive:
                names = [name for name in archive.namelist() if name.casefold().endswith(".csv")]
                if len(names) != 1:
                    raise RuntimeError(f"BTS archive must contain exactly one CSV: {path}")
                with (
                    archive.open(names[0]) as raw_stream,
                    io.TextIOWrapper(
                        raw_stream, encoding="utf-8-sig", newline=""
                    ) as stream,
                ):
                    flights = _load_supported_bts_rows(
                        stream,
                        flight_date=request.flight_date,
                        airport_timezones=self._airport_timezones,
                    )
        else:
            with path.open(newline="", encoding="utf-8-sig") as stream:
                flights = _load_supported_bts_rows(
                    stream,
                    flight_date=request.flight_date,
                    airport_timezones=self._airport_timezones,
                )
        return find_alternatives(request, flights=flights)

    def tool_handlers(self) -> dict[str, Any]:
        """Return the four public tool handlers for the API and MCP server."""

        return {
            "delay_risk": self.predict,
            "search_rules": self.search_rules,
            "weather_context": self.weather,
            "find_alternatives": self.alternatives,
        }
