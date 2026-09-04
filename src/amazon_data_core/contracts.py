from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class DatasetRunIn(BaseModel):
    external_run_id: str | None = Field(default=None, min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=100)
    store_id: str = Field(min_length=1, max_length=120)
    marketplace: str = Field(min_length=1, max_length=40)
    dataset: str = Field(min_length=1, max_length=80)
    business_date: date
    fetched_at: datetime
    source_updated_at: datetime | None = None
    ingestion_status: Literal["complete", "partial"]
    source_count: int = Field(ge=0)
    normalized_count: int = Field(ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    checksum: str | None = Field(default=None, max_length=128)
    timezone: str = Field(default="UTC", min_length=1, max_length=80)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    raw_reference: str | None = Field(default=None, max_length=500)
    schema_version: str = Field(default="1", min_length=1, max_length=80)
    formula_version: str | None = Field(default=None, max_length=80)
    is_provisional: bool = False
    correction_of_run_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_counts(self) -> "DatasetRunIn":
        if self.normalized_count + self.duplicate_count + self.error_count > self.source_count:
            raise ValueError(
                "normalized_count + duplicate_count + error_count cannot exceed source_count"
            )
        return self


class ScopeIn(BaseModel):
    source: str
    store_id: str
    marketplace: str
    dataset: str


class RuleIn(BaseModel):
    rule_code: str = Field(pattern=r"^[A-Z0-9_-]+$", max_length=60)
    check_type: Literal["freshness", "completeness", "reconciliation", "ordering"]
    dataset: str
    source: str | None = None
    store_id: str | None = None
    marketplace: str | None = None
    severity: Literal["info", "warning", "critical"] = "warning"
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
