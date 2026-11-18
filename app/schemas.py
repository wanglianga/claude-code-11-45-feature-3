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


# ---------------- 缺测潮位空间参考估计 ----------------

class SeaConditionIn(BaseModel):
    code: str
    name: str
    description: str | None = None


class SeaWindowIn(BaseModel):
    condition: str
    window_start: datetime
    window_end: datetime
    note: str = ""


class SeaWindowBatch(BaseModel):
    items: list[SeaWindowIn]


class EligibilityIn(BaseModel):
    station: str
    valid_from: datetime
    valid_to: datetime | None = None
    allowed_quality: list[Literal["GOOD", "SUSPECT", "PENDING_JUMP", "BAD"]] = ["GOOD"]
    note: str = ""


class StationReferenceIn(BaseModel):
    target_station: str
    ref_station: str
    pool: Literal["SINGLE", "MULTI"]
    bay_group: str = ""
    note: str = ""


class AnalysisIn(BaseModel):
    analysis_key: str = Field(description="幂等键；重复提交返回同一分析")
    target_station: str
    target_datum: str
    ref_stations: list[str] = Field(description="允许来源（须在预先划定的参考关系内）")
    fit_start: datetime
    fit_end: datetime
    gap_start: datetime
    gap_end: datetime
    data_cutoff: datetime
    grid_seconds: int = 600
    lag_min_sec: int = Field(description="允许滞后范围下限（秒，可负）")
    lag_max_sec: int = Field(description="允许滞后范围上限（秒，可正；正值需目标时刻后的参考读数）")
    lag_step_sec: int = 600
    path_mode: Literal["SINGLE", "MULTI"] = "SINGLE"
    allowed_conditions: list[str]
    validation_start: datetime | None = Field(
        None, description="预先划定的验证期起点；缺省时取拟合期最后一个连续片段")
    n_val_segments: int = 1
    min_segment_samples: int = 6
    error_method: str = "RMSE*1.96"


class EstimateBatchIn(BaseModel):
    analysis_key: str
    batch_key: str = Field(description="幂等键；离线回补使用新 batch_key 生成新批次")
    data_cutoff: datetime | None = None


class EstimateSubscriptionIn(BaseModel):
    name: str
    region: str
    target_station: str | None = None
    accept_estimates: bool = Field(True, description="订阅者显式声明能否接收估计产品")
    endpoint_url: str
    active: bool = True


class RecoveryReviewIn(BaseModel):
    analysis_key: str
    relation_id: int
    threshold_m: float = 0.35
