from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, Field, field_validator

ALLOWED_REGION_CODES = {"EUTTP", "USTTP", "ROW"}


class TimeRange(BaseModel):
    type: str = "last_n_days"
    n: int = 7


class Metric(BaseModel):
    metric_id: str = "pv"


class Event(BaseModel):
    event_name: str = "app_launch"


class GroupBy(BaseModel):
    dimension_id: str = "country"


class Filter(BaseModel):
    property_name: str
    op: str
    value: Any


class SemanticDSL(BaseModel):
    dsl_version: str = "sem_v1"
    registry_version: str = "2026-02-18"
    timezone: str = "UTC"

    project_id: int
    region_filter: List[str] = Field(default_factory=list)

    metric: Metric
    event: Event
    time_range: TimeRange
    group_by: List[GroupBy] = Field(default_factory=list)
    filters: List[Filter] = Field(default_factory=list)

    @field_validator("region_filter")
    @classmethod
    def _check_region(cls, v):
        bad = [x for x in v if x not in ALLOWED_REGION_CODES]
        if bad:
            raise ValueError(f"Invalid region_filter: {bad}, allowed={sorted(ALLOWED_REGION_CODES)}")
        return v
