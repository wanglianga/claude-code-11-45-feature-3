"""数据模型：站点目录、基准与版本化转换、原始观测、产品、订阅与交付。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------- 基准目录 ----------------

class Datum(Base):
    """垂直基准（如 1985 国家高程、某港区当地零点）。"""

    __tablename__ = "datums"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)


class DatumVersion(Base):
    """基准配置版本。任何转换边/站点基准变更都生成新版本，作业按版本快照取数。"""

    __tablename__ = "datum_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DatumConversion(Base):
    """基准转换有向边：height(to) = height(from) + offset_m，带生效区间与版本。

    读数单位固定为米；正方向统一为“水位向上为正”。边只增不改：
    变更 = 新版本下新增边并将旧边 retired_in_version 置为该版本。
    """

    __tablename__ = "datum_conversions"

    id: Mapped[int] = mapped_column(primary_key=True)
    from_datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"), index=True)
    to_datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"), index=True)
    offset_m: Mapped[float] = mapped_column(Float)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_in_version: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))
    retired_in_version: Mapped[int | None] = mapped_column(nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")


# ---------------- 站点目录 ----------------

class Station(Base):
    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(100), index=True)  # 港区/海湾
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(8), default="m")     # m / cm / mm
    positive: Mapped[str] = mapped_column(String(8), default="up")  # up=水位向上为正 down=距水面距离向下为正
    nominal_interval_sec: Mapped[int] = mapped_column(Integer, default=600)
    device_status: Mapped[str] = mapped_column(String(32), default="ONLINE")
    datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"))  # 当前站点零点基准
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StationDatumHistory(Base):
    """站点零点基准指派历史（版本化，供作业按快照取）。"""

    __tablename__ = "station_datum_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"))
    created_in_version: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))
    retired_in_version: Mapped[int | None] = mapped_column(nullable=True)


# ---------------- 原始观测 ----------------

class RawObservation(Base):
    """原始观测版本链。同一 (station, sample_seq) 仅一条 is_current；
    内容相同的重复报文直接去重，内容不同视为更正并生成新版本。"""

    __tablename__ = "raw_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    sample_seq: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(8), default="m")
    quality_flag: Mapped[str] = mapped_column(String(16), default="GOOD")  # GOOD/SUSPECT/PENDING_JUMP/BAD
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by: Mapped[int | None] = mapped_column(nullable=True)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)  # 到达时对应窗口已有产品
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index(
            "uq_current_obs",
            "station_id",
            "sample_seq",
            unique=True,
            postgresql_where=(is_current.is_(True)),
        ),
    )


# ---------------- 产品 ----------------

class ProductJob(Base):
    """产品生成作业。创建时固定 datum_version_id，运行全程使用该快照。"""

    __tablename__ = "product_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_key: Mapped[str] = mapped_column(String(200), unique=True)  # 幂等键
    region: Mapped[str] = mapped_column(String(100))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grid_seconds: Mapped[int] = mapped_column(Integer)
    target_datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"))
    stage: Mapped[str] = mapped_column(String(16))  # PRELIMINARY / CONFIRMED
    datum_version_id: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))
    source_product_id: Mapped[int | None] = mapped_column(nullable=True)  # 确认产品来源初报
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING/RUNNING/COMMITTED/FAILED
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# 全局产品发布序号（订阅游标固定在该序号上）
publish_seq = Sequence("publish_seq", start=1, metadata=Base.metadata)


class Product(Base):
    """潮位产品。系列 = 区域+窗口+粒度+目标基准；每次生成为系列内一个新版本。"""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("product_jobs.id"))
    series_key: Mapped[str] = mapped_column(String(300), index=True)
    region: Mapped[str] = mapped_column(String(100), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grid_seconds: Mapped[int] = mapped_column(Integer)
    target_datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"))
    stage: Mapped[str] = mapped_column(String(16))  # PRELIMINARY / CONFIRMED，服务端赋值，不可伪造
    version_no: Mapped[int] = mapped_column(Integer)
    publish_seq: Mapped[int] = mapped_column(BigInteger, unique=True)  # 发布序号（全局递增）
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    datum_version_id: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    units: Mapped[list["ProductUnit"]] = relationship(back_populates="product")

    __table_args__ = (UniqueConstraint("series_key", "version_no", name="uq_product_version"),)


class ProductUnit(Base):
    """产品单元：一站 × 一个共同时间格。记录样本来源、覆盖、换算路径与质量原因。"""

    __tablename__ = "product_units"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    grid_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grid_seconds: Mapped[int] = mapped_column(Integer)
    value_m: Mapped[float | None] = mapped_column(Float, nullable=True)  # 目标基准水位（米）
    quality_grade: Mapped[str] = mapped_column(String(16))  # MEASURED/INTERPOLATED/PENDING/MISSING/CONFLICT/NO_PATH
    coverage_sec: Mapped[float] = mapped_column(Float, default=0.0)
    coverage_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    source_sample_ids: Mapped[list] = mapped_column(JSONB, default=list)  # 参与的原始样本 id
    conversion_path: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)  # 边序列与偏移
    conversion_status: Mapped[str] = mapped_column(String(16), default="OK")  # OK/NO_PATH/CONFLICT
    quality_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    datum_version_id: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))

    product: Mapped[Product] = relationship(back_populates="units")

    __table_args__ = (
        UniqueConstraint("product_id", "station_id", "grid_start", name="uq_unit_cell"),
    )


# ---------------- 订阅与交付 ----------------

class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    region: Mapped[str] = mapped_column(String(100), index=True)
    stages: Mapped[list] = mapped_column(JSONB, default=list)  # ["PRELIMINARY","CONFIRMED"]
    endpoint_url: Mapped[str] = mapped_column(String(500))
    cursor_seq: Mapped[int] = mapped_column(BigInteger, default=0)  # 已确认的最大发布序号
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Delivery(Base):
    """待交付任务。与产品同事务写入；delivery_key 全局唯一，重送保留同一标识。"""

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_key: Mapped[str] = mapped_column(String(120), unique=True)  # sub:{id}-prod:{id}
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    publish_seq: Mapped[int] = mapped_column(BigInteger)
    stage: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING/SENT/ACKED/FAILED
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- 模拟消费端状态（独立进程使用） ----------------

class ConsumerInbox(Base):
    __tablename__ = "consumer_inbox"

    delivery_key: Mapped[str] = mapped_column(String(120), primary_key=True)  # 按标识去重
    subscription_name: Mapped[str] = mapped_column(String(100), index=True)
    product_id: Mapped[int] = mapped_column(Integer)
    publish_seq: Mapped[int] = mapped_column(BigInteger)
    stage: Mapped[str] = mapped_column(String(16))
    payload_hash: Mapped[str] = mapped_column(String(64))
    receive_count: Mapped[int] = mapped_column(Integer, default=0)  # 重复投递次数（去重证据）
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsumerState(Base):
    """消费端当前水位视图：每 (系列, 站点, 时间格) 只保留 publish_seq 最大的记录。"""

    __tablename__ = "consumer_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    series_key: Mapped[str] = mapped_column(String(300))
    station_id: Mapped[int] = mapped_column(Integer)
    grid_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_grade: Mapped[str] = mapped_column(String(16))
    stage: Mapped[str] = mapped_column(String(16))
    product_version: Mapped[int] = mapped_column(Integer)
    publish_seq: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("series_key", "station_id", "grid_start", name="uq_consumer_cell"),
    )


class ConsumerFlag(Base):
    __tablename__ = "consumer_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(200), default="")


# ====================================================================
# 缺测潮位空间参考估计（独立产品类型，不与实测产品混通道）
# ====================================================================

# ---------------- 海况资料（明确提供，不从观测推断） ----------------

class SeaCondition(Base):
    """海况类别目录（如 CALM 平稳 / STORM 风暴），类别只能来自明确资料。"""

    __tablename__ = "sea_conditions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)


class SeaConditionWindow(Base):
    """海况时间窗：[window_start, window_end) -> 海况类别。

    目标时段查不到类别时必须停止估计，不允许按候选结果反选海况。
    """

    __tablename__ = "sea_condition_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    condition_id: Mapped[int] = mapped_column(ForeignKey("sea_conditions.id"), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(String(500), default="")


# ---------------- 站点安装期与质量资格 ----------------

class StationEligibility(Base):
    """站点有效安装期与质量资格。安装期外、资格外的样本不进入配对。"""

    __tablename__ = "station_eligibility"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    allowed_quality: Mapped[list] = mapped_column(JSONB, default=list)  # 如 ["GOOD"]
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(500), default="")


class StationReference(Base):
    """站点可参考关系目录：目标站可使用的参考站及路径池。

    pool=SINGLE：单参考站传递关系候选（逐对独立验证、可多备用）；
    pool=MULTI：多源联合拟合候选成员（来源组合需独立验证）。
    关系按同步覆盖与海湾关联人工预先划定，系统不凭空造邻站。
    """

    __tablename__ = "station_references"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    ref_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    pool: Mapped[str] = mapped_column(String(16))  # SINGLE / MULTI
    bay_group: Mapped[str] = mapped_column(String(100), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(500), default="")

    __table_args__ = (
        UniqueConstraint("target_station_id", "ref_station_id", "pool", name="uq_station_ref"),
    )


# ---------------- 分析上下文（一次分析固定全部边界） ----------------

class SpatialAnalysis(Base):
    """一次空间参考分析：固定目标站、缺测时段、资料截止点、允许海况与来源。

    后续配对、候选参数、验证与估计全部挂在该分析下，边界不可中途改写。
    """

    __tablename__ = "spatial_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_key: Mapped[str] = mapped_column(String(200), unique=True)
    target_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    target_datum_id: Mapped[int] = mapped_column(ForeignKey("datums.id"))
    datum_version_id: Mapped[int] = mapped_column(ForeignKey("datum_versions.id"))
    fit_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fit_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gap_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gap_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grid_seconds: Mapped[int] = mapped_column(Integer, default=600)
    lag_min_sec: Mapped[int] = mapped_column(Integer)
    lag_max_sec: Mapped[int] = mapped_column(Integer)
    lag_step_sec: Mapped[int] = mapped_column(Integer)
    path_mode: Mapped[str] = mapped_column(String(16))  # SINGLE / MULTI
    allowed_conditions: Mapped[list] = mapped_column(JSONB, default=list)
    ref_station_ids: Mapped[list] = mapped_column(JSONB, default=list)
    validation_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    n_val_segments: Mapped[int] = mapped_column(Integer, default=1)
    min_segment_samples: Mapped[int] = mapped_column(Integer, default=6)
    error_method: Mapped[str] = mapped_column(String(64), default="RMSE*1.96")
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    # CREATED/PREPARED/FITTED/RELATIONS_READY/FAILED
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TideSegment(Base):
    """连续潮汐片段：配对支持中断即切分；拟合/验证归属在拟合前预先划定。

    单源按 (参考站, 滞后) 划定；多源按 (来源组合, 滞后) 的共同支持时刻划定。
    """

    __tablename__ = "tide_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("spatial_analyses.id"), index=True)
    ref_station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    ref_combo: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    lag_sec: Mapped[int] = mapped_column(Integer)
    seg_index: Mapped[int] = mapped_column(Integer)
    seg_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seg_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    split: Mapped[str] = mapped_column(String(8))  # TRAIN / VAL
    condition_codes: Mapped[list] = mapped_column(JSONB, default=list)
    pair_count: Mapped[int] = mapped_column(Integer, default=0)


class PairedSample(Base):
    """候选滞后下对齐后的配对样本（目标时刻 t 对参考时刻 t+lag_sec）。

    仅在两站共同有观测支持（MEASURED）、安装期/质量资格有效、基准可换算、
    海况类别明确时生成；长缺口、初报不明、基准冲突不会被插值成配对。
    """

    __tablename__ = "paired_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("spatial_analyses.id"), index=True)
    ref_station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    lag_sec: Mapped[int] = mapped_column(Integer, index=True)
    grid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 目标站时刻
    ref_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # 对齐到的参考站时刻
    target_value_m: Mapped[float] = mapped_column(Float)
    ref_value_m: Mapped[float] = mapped_column(Float)
    condition_code: Mapped[str] = mapped_column(String(64))
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("tide_segments.id"), nullable=True)
    split: Mapped[str] = mapped_column(String(8))  # TRAIN / VAL
    target_obs_ids: Mapped[list] = mapped_column(JSONB, default=list)
    ref_obs_ids: Mapped[list] = mapped_column(JSONB, default=list)


class LagCandidate(Base):
    """候选滞后与实际拟合的偏移/尺度（含多源组合的设计矩阵诊断）。"""

    __tablename__ = "lag_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("spatial_analyses.id"), index=True)
    ref_station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    ref_combo: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # MULTI 时的参考站 id 组合
    lag_sec: Mapped[int] = mapped_column(Integer)
    fit_offset: Mapped[float | None] = mapped_column(Float, nullable=True)
    fit_scales: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {ref_station_id: scale}
    train_n: Mapped[int] = mapped_column(Integer, default=0)
    val_n: Mapped[int] = mapped_column(Integer, default=0)
    train_rmse: Mapped[float | None] = mapped_column(Float, nullable=True)
    val_rmse: Mapped[float | None] = mapped_column(Float, nullable=True)
    identifiable: Mapped[bool] = mapped_column(Boolean, default=True)
    condition_number: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # FIT / REJECTED_COLLINEAR / REJECTED_NO_SUPPORT
    note: Mapped[str] = mapped_column(String(500), default="")


class ValidationRecord(Base):
    """分海况验证记录：覆盖时长、幅度范围、平均偏差、极端水位误差与资格结论。"""

    __tablename__ = "validation_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("spatial_analyses.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("lag_candidates.id"))
    ref_station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    ref_combo: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    condition_code: Mapped[str] = mapped_column(String(64), index=True)
    n_samples: Mapped[int] = mapped_column(Integer)
    coverage_sec: Mapped[float] = mapped_column(Float)
    amplitude_min: Mapped[float] = mapped_column(Float)
    amplitude_max: Mapped[float] = mapped_column(Float)
    mean_bias: Mapped[float] = mapped_column(Float)
    mae: Mapped[float] = mapped_column(Float)
    rmse: Mapped[float] = mapped_column(Float)
    extreme_error: Mapped[float] = mapped_column(Float)  # 最高水位处的误差
    qualified: Mapped[bool] = mapped_column(Boolean, default=False)
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    segment_ids: Mapped[list] = mapped_column(JSONB, default=list)


class ReferenceRelation(Base):
    """经过验证、可在缺测时使用的传递关系（单源一条 / 多源一个来源组合一条）。

    停用只改状态与原因，不删除；历史估计永久保留其使用时的关系快照。
    """

    __tablename__ = "reference_relations"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("spatial_analyses.id"), index=True)
    target_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16))  # SINGLE / MULTI
    ref_station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    ref_combo: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    lag_sec: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict] = mapped_column(JSONB)  # offset + 各参考站 scale
    # 适用范围：{condition: {n, coverage_sec, amplitude_min, amplitude_max, rmse, ...}}
    applicability: Mapped[dict] = mapped_column(JSONB, default=dict)
    validation_candidate_id: Mapped[int] = mapped_column(ForeignKey("lag_candidates.id"))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE / DISABLED
    disable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------- 原站缺测事实（估计永不覆盖该事实） ----------------

class StationGapFact(Base):
    """目标站缺测事实登记。估计是独立产品，缺测事实始终原样可查。"""

    __tablename__ = "station_gap_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    gap_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    gap_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(200), default="")
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("spatial_analyses.id"), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("station_id", "gap_start", name="uq_gap_fact"),
    )


# ---------------- 估计批次与估计点（独立产品类型 SPATIAL_ESTIMATE） ----------------

estimate_publish_seq = Sequence("estimate_publish_seq", start=1, metadata=Base.metadata)


class EstimateBatch(Base):
    """一次估计生成：固定资料截止点；离线回补生成新批次，不改写旧批次。"""

    __tablename__ = "estimate_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_key: Mapped[str] = mapped_column(String(200), unique=True)
    product_kind: Mapped[str] = mapped_column(String(32), default="SPATIAL_ESTIMATE")
    analysis_id: Mapped[int] = mapped_column(ForeignKey("spatial_analyses.id"), index=True)
    target_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gap_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gap_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="ISSUED")  # ISSUED/PARTIAL/ALL_UNAVAILABLE
    publish_seq: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    note: Mapped[str] = mapped_column(String(500), default="")


class EstimatePoint(Base):
    """估计序列单点：值、状态、适用范围、经验误差、来源链（展开到真实观测）。"""

    __tablename__ = "estimate_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("estimate_batches.id"), index=True)
    target_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"), index=True)
    grid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ESTIMATED / WAITING_FUTURE_DATA / UNAVAILABLE
    status: Mapped[str] = mapped_column(String(32))
    condition_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relation_id: Mapped[int | None] = mapped_column(ForeignKey("reference_relations.id"), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, default=list)   # 来源站与对齐时刻
    source_obs_ids: Mapped[list] = mapped_column(JSONB, default=list)  # 展开后的真实观测 id
    error_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_halfwidth: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_n: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applicability: Mapped[dict] = mapped_column(JSONB, default=dict)
    extrapolation_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reasons: Mapped[list] = mapped_column(JSONB, default=list)

    __table_args__ = (
        UniqueConstraint("batch_id", "grid_time", name="uq_estimate_cell"),
    )


# ---------------- 估计订阅与独立交付通道 ----------------

class EstimateSubscription(Base):
    """估计产品订阅：订阅者显式声明能否接收估计，与实测订阅完全分开。"""

    __tablename__ = "estimate_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    region: Mapped[str] = mapped_column(String(100), index=True)
    target_station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    accept_estimates: Mapped[bool] = mapped_column(Boolean, default=True)
    endpoint_url: Mapped[str] = mapped_column(String(500))
    cursor_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EstimateDelivery(Base):
    """估计产品待交付任务，独立于实测 deliveries；delivery_key 全局唯一。"""

    __tablename__ = "estimate_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_key: Mapped[str] = mapped_column(String(160), unique=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("estimate_subscriptions.id"), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("estimate_batches.id"), index=True)
    publish_seq: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------- 恢复对照与关系停用 ----------------

class RelationReview(Base):
    """目标站恢复后的对照复核：逐片段偏差、停用决策、受影响估计与接收方。"""

    __tablename__ = "relation_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    relation_id: Mapped[int] = mapped_column(ForeignKey("reference_relations.id"), index=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("spatial_analyses.id"))
    target_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    threshold_m: Mapped[float] = mapped_column(Float)
    max_abs_bias: Mapped[float] = mapped_column(Float)
    segment_results: Mapped[list] = mapped_column(JSONB, default=list)
    decision: Mapped[str] = mapped_column(String(16))  # CONTINUE / DISABLE
    affected_batch_ids: Mapped[list] = mapped_column(JSONB, default=list)
    affected_receivers: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecoveryPoint(Base):
    """恢复对照单点：迟到实测 vs 原估计（原估计不重新拟合）。"""

    __tablename__ = "recovery_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("relation_reviews.id"), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("estimate_batches.id"))
    grid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    estimated_value_m: Mapped[float] = mapped_column(Float)
    observed_value_m: Mapped[float] = mapped_column(Float)
    bias_m: Mapped[float] = mapped_column(Float)
    condition_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ---------------- 模拟消费端：估计产品信箱（独立于实测信箱） ----------------

class ConsumerEstimateInbox(Base):
    __tablename__ = "consumer_estimate_inbox"

    delivery_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    subscription_name: Mapped[str] = mapped_column(String(100), index=True)
    batch_id: Mapped[int] = mapped_column(Integer)
    product_kind: Mapped[str] = mapped_column(String(32))
    publish_seq: Mapped[int] = mapped_column(BigInteger)
    source_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    receive_count: Mapped[int] = mapped_column(Integer, default=1)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsumerEstimateState(Base):
    """消费端估计视图：按目标站+时刻保留最新批次，带独立产品标识与来源说明。"""

    __tablename__ = "consumer_estimate_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_station_id: Mapped[int] = mapped_column(Integer)
    grid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    batch_id: Mapped[int] = mapped_column(Integer)
    product_kind: Mapped[str] = mapped_column(String(32))
    value_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    condition_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_halfwidth: Mapped[float | None] = mapped_column(Float, nullable=True)
    relation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, default=list)
    publish_seq: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("target_station_id", "grid_time", name="uq_consumer_est_cell"),
    )
