"""Injectable, User-Agent compliant client for the NWS hourly forecast API."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx


class NWSIngestError(ValueError):
    """Raised for invalid locations, unavailable forecasts, or malformed NWS data."""


class HTTPGetter(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class NWSClient:
    """Fetch a forecast using an injected HTTP client and a descriptive User-Agent.

    ``airport_locations`` is intentionally injected: this module does not bundle an
    airport database and never silently geocodes a code to a guessed location.
    """

    def __init__(
        self,
        airport_locations: Mapping[str, tuple[float, float]],
        *,
        user_agent: str,
        client: HTTPGetter | None = None,
        base_url: str = "https://api.weather.gov",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not user_agent.strip() or "contact" not in user_agent.lower():
            raise ValueError(
                "NWS User-Agent must identify the application and include contact info"
            )
        self._locations = dict(airport_locations)
        self._user_agent = user_agent.strip()
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._base_url = base_url.rstrip("/")

    def hourly_forecast(self, airport: str) -> tuple[dict[str, Any], ...]:
        coords = self._locations.get(airport)
        if coords is None:
            raise NWSIngestError(f"no configured NWS coordinates for airport {airport}")
        lat, lon = coords
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise NWSIngestError(f"invalid NWS coordinates for airport {airport}")
        headers = {"User-Agent": self._user_agent, "Accept": "application/geo+json"}
        point = self._request(f"{self._base_url}/points/{lat:.4f},{lon:.4f}", headers)
        try:
            forecast_url = point["properties"]["forecastHourly"]
        except (KeyError, TypeError) as exc:
            raise NWSIngestError("NWS point response did not contain forecastHourly") from exc
        forecast = self._request(str(forecast_url), headers)
        try:
            periods = forecast["properties"]["periods"]
        except (KeyError, TypeError) as exc:
            raise NWSIngestError("NWS forecast response did not contain periods") from exc
        if not isinstance(periods, list):
            raise NWSIngestError("NWS forecast periods must be a list")
        return tuple(p for p in periods if isinstance(p, dict))

    def _request(self, url: str, headers: Mapping[str, str]) -> dict[str, Any]:
        response = self._client.get(url, headers=dict(headers))
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise NWSIngestError("NWS response must be a JSON object")
        return payload


def utc_now() -> datetime:
    """Return an aware UTC timestamp (separate function for easy injection)."""

    return datetime.now(UTC)
