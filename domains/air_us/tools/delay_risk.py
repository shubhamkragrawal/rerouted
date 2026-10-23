"""Public delay-risk tool adapter around the locked contracts."""

from __future__ import annotations

from core.contracts import DelayRiskRequest, DelayRiskResponse
from domains.air_us.model import DelayRiskModel
from domains.air_us.model.features import FlightFeatures
from domains.air_us.model.pipeline import predict_request


def delay_risk(
    request: DelayRiskRequest,
    model: DelayRiskModel,
    features: FlightFeatures | None = None,
) -> DelayRiskResponse:
    """Return calibrated delay/cancellation estimates for one scheduled flight."""
    return predict_request(model, request, features)
