"""Permanent disclosure and unsafe-output checks."""

import re

DISCLOSURE = (
    "This explains what published DOT rules and airline contracts of carriage say, "
    "with citations. It is not legal advice, it does not contact the airline, and "
    "what you actually receive may differ."
)
_WILL_CLAIM = re.compile(
    r"\b(?:the\s+)?(?:airline|carrier)\s+will\s+(?:pay|refund|provide|give|rebook)",
    re.IGNORECASE,
)


class OutputViolation(ValueError):
    pass


class OutputGuard:
    def enforce(self, answer: str) -> str:
        cleaned = answer.strip()
        if _WILL_CLAIM.search(cleaned):
            raise OutputViolation("prediction_of_airline_action")
        if DISCLOSURE not in cleaned:
            cleaned = f"{cleaned}\n\n{DISCLOSURE}" if cleaned else DISCLOSURE
        return cleaned
