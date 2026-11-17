"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DatumIn(BaseModel):
    code: str
    name: str
    description: str | None = None


class ConversionIn(BaseModel):
    from_datum: str = Field(description="源基准 code")
    to_datum: str = Field(description="目标基准 code")
    offset_m: float = Field(description="height(to) = height(from) + offset_m，单位米，水位向上为正")
    valid_from: datetime
    valid_to: datetime | None = None
    note: str = ""


class StationIn(BaseModel):
    code: str
    name: str
    region: str
    lat: float | None = None
    lon: float | None = None
    unit: Literal["m", "cm", "mm"] = "m"
    positive: Literal["up", "down"] = "up"
    nominal_interval_sec: int = 600
    device_status: str = "ONLINE"
    datum: str = Field(description="站点零点基准 code")


class StationDatumUpdate(BaseModel):
    datum: str
    note: str = ""


class ObservationItem(BaseModel):
    station: str
    sample_seq: int
    observed_at: datetime
    value: float
    unit: Literal["m", "cm", "mm"] = "m"
    quality_flag: Literal["GOOD", "SUSPECT", "PENDING_JUMP", "BAD"] = "GOOD"


class IngestBatch(BaseModel):
    items: list[ObservationItem]


class JobIn(BaseModel):
    job_key: str = Field(description="幂等键；重复提交返回同一作业")
    region: str
    window_start: datetime
    window_end: datetime
    grid_seconds: int = 600
    target_datum: str
    stage: Literal["PRELIMINARY"] = "PRELIMINARY"
    max_rep_sec: int | None = None
    min_coverage: float | None = None
    max_interp_gap_sec: int | None = None


class ConfirmIn(BaseModel):
    job_key: str


class SubscriptionIn(BaseModel):
    name: str
    region: str
    stages: list[Literal["PRELIMINARY", "CONFIRMED"]]
    endpoint_url: str
    active: bool = True
