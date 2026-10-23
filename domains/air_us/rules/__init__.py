"""United States air-travel entitlement rules."""

from .entitlement import (
    AirTravelDecision,
    ConsumerDisposition,
    DisruptionFacts,
    ItineraryKind,
    resolve_air_travel,
    resolve_refund_entitlement,
)
from .posture import PostureRegistry, load_default_posture

__all__ = [
    "AirTravelDecision",
    "ConsumerDisposition",
    "DisruptionFacts",
    "ItineraryKind",
    "PostureRegistry",
    "load_default_posture",
    "resolve_air_travel",
    "resolve_refund_entitlement",
]
