"""Locked request and response shapes for Rerouted's four tools."""

from datetime import date, datetime, time
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from core.authority import Authority
from core.contracts.base import ContractModel

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
AirportCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
CarrierCode = Annotated[str, Field(min_length=2, max_length=3, pattern=r"^[A-Z0-9]+$")]


class DelayRiskRequest(ContractModel):
    carrier: CarrierCode
    origin: AirportCode
    dest: AirportCode
    flight_date: date
    crs_dep_time: time
    mode: Annotated[str, Field(min_length=1)]


class FeatureContribution(ContractModel):
    feature: Annotated[str, Field(min_length=1)]
    contribution: float


class DelayRiskResponse(ContractModel):
    p_delay_15: Probability
    p_cancel: Probability
    conformal_interval: tuple[Probability, Probability]
    calibration_bin: Annotated[
        str,
        Field(pattern=r"^0(?:\.\d+)?-1(?:\.0+)?$|^0(?:\.\d+)?-0(?:\.\d+)?$"),
    ]
    top_features: tuple[FeatureContribution, ...]
    plain_english: Annotated[str, Field(min_length=1)]
    model_version: Annotated[str, Field(min_length=1)]
    feature_pipeline_version: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def interval_is_ordered(self) -> "DelayRiskResponse":
        if self.conformal_interval[0] > self.conformal_interval[1]:
            raise ValueError("conformal_interval lower bound must not exceed upper bound")
        return self


class SearchRulesRequest(ContractModel):
    query: Annotated[str, Field(min_length=1)]
    as_of_date: date
    carrier: CarrierCode | None = None
    doc_types: tuple[Authority, ...]
    k: Annotated[int, Field(ge=1, le=100)]


class RuleClause(ContractModel):
    clause_id: Annotated[str, Field(min_length=1)]
    doc: Annotated[str, Field(min_length=1)]
    text: Annotated[str, Field(min_length=1)]
    effective_from: date
    effective_to: date | None
    authority: Authority
    score: float
    rerank_score: float
    source_url: Annotated[str, Field(pattern=r"^https://")]

    @model_validator(mode="after")
    def effective_window_is_ordered(self) -> "RuleClause":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be on or after effective_from")
        return self


class EnforcementPosture(ContractModel):
    fr_doc: Annotated[str, Field(min_length=1)]
    effect: Annotated[str, Field(min_length=1)]
    sections: tuple[Annotated[str, Field(min_length=1)], ...]
    scope: Annotated[str, Field(min_length=1)]
    effective_from: date
    effective_to: date | None
    source_url: Annotated[str, Field(pattern=r"^https://")]

    @model_validator(mode="after")
    def effective_window_is_ordered(self) -> "EnforcementPosture":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be on or after effective_from")
        return self


class SearchRulesResponse(ContractModel):
    clauses: tuple[RuleClause, ...]
    posture: tuple[EnforcementPosture, ...]
    # An empty posture list is meaningful only when this flag is true. False means
    # the reviewed posture horizon ended or the posture source was not checked.
    posture_checked: bool
    as_of_date: date
    excluded_by_date: NonNegativeInt
    corpus_version: Annotated[str, Field(min_length=1)]
    # Additive retrieval diagnostics. Defaults preserve compatibility with older
    # tool producers and recorded payloads.
    retrieval_provider: Annotated[str, Field(min_length=1)] = "unknown"
    retrieval_model: Annotated[str, Field(min_length=1)] = "unknown"
    retrieval_fallback_reason: str | None = None


class WeatherMode(StrEnum):
    LIVE = "live"
    HISTORICAL = "historical"


class WeatherContextRequest(ContractModel):
    airport: AirportCode
    window_start: datetime
    window_end: datetime
    mode: WeatherMode

    @model_validator(mode="after")
    def window_is_ordered(self) -> "WeatherContextRequest":
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


class WeatherObservation(ContractModel):
    ts: datetime
    ceiling_ft: NonNegativeInt | None
    visibility_mi: Annotated[float, Field(ge=0)] | None
    wind_kt: Annotated[float, Field(ge=0)] | None
    wx_codes: tuple[str, ...]


class WeatherContextResponse(ContractModel):
    source: Literal["NWS", "NCEI-LCD"]
    observations: tuple[WeatherObservation, ...]
    adverse_flags: tuple[str, ...]
    confidence: Literal["observed", "forecast"]
    as_of: datetime


class FindAlternativesRequest(ContractModel):
    origin: AirportCode
    dest: AirportCode
    not_before: datetime
    original_scheduled_arrival: datetime
    max_stops: Annotated[int, Field(ge=0, le=3)]
    same_carrier_only: bool
    flight_date: date

    @model_validator(mode="after")
    def schedule_timestamps_are_aware(self) -> "FindAlternativesRequest":
        for name, value in (
            ("not_before", self.not_before),
            ("original_scheduled_arrival", self.original_scheduled_arrival),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must include a timezone")
        return self


class FlightLeg(ContractModel):
    carrier: CarrierCode
    flight_no: Annotated[str, Field(min_length=1, max_length=8)]
    origin: AirportCode
    dest: AirportCode
    crs_dep: datetime
    crs_arr: datetime

    @model_validator(mode="after")
    def arrival_is_after_departure(self) -> "FlightLeg":
        if self.crs_arr <= self.crs_dep:
            raise ValueError("crs_arr must be after crs_dep")
        return self


class AlternativeFlightOption(ContractModel):
    legs: tuple[FlightLeg, ...] = Field(min_length=1)
    total_duration_min: Annotated[int, Field(gt=0)]
    connection_buffer_min: NonNegativeInt
    hist_ontime_rate: Probability
    arrival_delta_vs_original_min: int


class FindAlternativesResponse(ContractModel):
    options: tuple[AlternativeFlightOption, ...]
    searched_date: date
    schedule_source: Literal["historical_bts"] = "historical_bts"
