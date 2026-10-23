"""Stable feature construction shared by model training and serving."""

from __future__ import annotations

from datetime import date, time
from typing import TypeAlias

from core.contracts import DelayRiskRequest

FEATURE_PIPELINE_VERSION = "fp-1"
FEATURE_NAMES = (
    "carrier",
    "origin",
    "dest",
    "month",
    "day_of_week",
    "departure_hour_block",
    "route_historical_ontime_rate",
    "carrier_origin_historical_rate",
    "aircraft_turn_delay_proxy",
    "adverse_weather",
)


FeatureValue: TypeAlias = str | int | float
FlightFeatures: TypeAlias = dict[str, FeatureValue]


def build_features(
    *,
    carrier: str,
    origin: str,
    dest: str,
    flight_date: date,
    crs_dep_time: time,
    route_historical_ontime_rate: float = 0.8,
    carrier_origin_historical_rate: float = 0.8,
    aircraft_turn_delay_proxy: float = 0.0,
    adverse_weather: bool = False,
) -> FlightFeatures:
    """Build the locked feature set; history inputs must be leakage-safe upstream."""
    for value, field in (
        (route_historical_ontime_rate, "route_historical_ontime_rate"),
        (carrier_origin_historical_rate, "carrier_origin_historical_rate"),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{field} must be between zero and one")
    return {
        "carrier": carrier,
        "origin": origin,
        "dest": dest,
        "month": flight_date.month,
        "day_of_week": flight_date.weekday(),
        "departure_hour_block": crs_dep_time.hour // 3,
        "route_historical_ontime_rate": route_historical_ontime_rate,
        "carrier_origin_historical_rate": carrier_origin_historical_rate,
        "aircraft_turn_delay_proxy": aircraft_turn_delay_proxy,
        "adverse_weather": int(adverse_weather),
    }


def request_features(request: DelayRiskRequest) -> FlightFeatures:
    """Fallback feature values for the contract-only endpoint when histories are absent."""
    return build_features(
        carrier=request.carrier,
        origin=request.origin,
        dest=request.dest,
        flight_date=request.flight_date,
        crs_dep_time=request.crs_dep_time,
    )
