"""Strict base types for public integration contracts."""

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    """An immutable integration contract that rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
