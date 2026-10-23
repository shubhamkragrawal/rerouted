"""Shared, dependency-free authority vocabulary."""

from enum import StrEnum


class Authority(StrEnum):
    """Kinds of source authority kept distinct throughout the system."""

    REGULATION = "regulation"
    COMMITMENT = "commitment"
    CONTRACT = "contract"
    ENFORCEMENT_POLICY = "enforcement_policy"
