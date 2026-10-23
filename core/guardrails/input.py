"""Conservative input checks that run before planning or logging."""

import re
from dataclasses import dataclass

_AIR_SCOPE = re.compile(
    r"\b(flight|airline|airport|carrier|cancel(?:led|lation)?|delay(?:ed)?|"
    r"refund|rebook|itinerary|connection|divert(?:ed|sion)?|baggage)\b",
    re.IGNORECASE,
)
_INJECTION = re.compile(
    r"\b(ignore (?:all|any|the) (?:previous|prior|system)|system prompt|"
    r"developer message|reveal (?:your|the) instructions|jailbreak)\b",
    re.IGNORECASE,
)
_SENSITIVE = (
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"),
    re.compile(r"\b(?:pnr|confirmation|record locator)\s*[:#-]?\s*[A-Z0-9]{5,8}\b", re.I),
)


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str | None = None
    contains_sensitive_data: bool = False


class InputGuard:
    """Reject out-of-scope or adversarial input before it reaches an LLM."""

    def __init__(self, us_airports: set[str] | None = None) -> None:
        self._us_airports = (
            {value.upper() for value in us_airports} if us_airports is not None else None
        )

    def assess(
        self,
        query: str,
        *,
        origin: str | None = None,
        destination: str | None = None,
    ) -> GuardDecision:
        normalized = query.strip()
        if not normalized:
            return GuardDecision(False, "empty_query")
        if _INJECTION.search(normalized):
            return GuardDecision(False, "prompt_injection")
        sensitive = any(pattern.search(normalized) for pattern in _SENSITIVE)
        if sensitive:
            return GuardDecision(False, "sensitive_booking_or_contact_data", True)
        if not _AIR_SCOPE.search(normalized):
            return GuardDecision(False, "outside_us_air_disruption_scope")
        if self._us_airports is not None and any(
            airport and airport.upper() not in self._us_airports
            for airport in (origin, destination)
        ):
            return GuardDecision(False, "outside_us_airport_scope")
        return GuardDecision(True)
