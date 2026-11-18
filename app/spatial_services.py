"""缺测潮位空间参考估计：编排服务（数据库交互 + 算法落地 + 独立发布）。

流水线（一次分析固定边界，全部结果实际计算并落库）：
  目录资料（海况/安装期资格/参考关系）
    -> 统一高程序列（仅 MEASURED、资格内、基准可换算的真实观测支持）
    -> 候选滞后对齐配对（共同观测支持，禁止同时刻直接相减）
    -> 连续潮汐片段切分与拟合/验证预先划定
    -> 单源 OLS / 多源联合 OLS（先查可识别性）
    -> 留出片段分海况验证 -> 经验证的传递关系（含适用范围与误差）
    -> 截止点感知的估计生成（未来数据未到即等待，离线回补另出新批次）
    -> 恢复对照与关系停用（不重新拟合原估计）

估计是独立产品类型 SPATIAL_ESTIMATE，走独立订阅/交付通道，
原站缺测事实（StationGapFact）始终原样保留，估计永不覆盖实测口径。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, spatial
from .datum import find_conversion
from .grid import Sample, resample_window
from .models import (
    Datum,
    EstimateBatch,
    EstimateDelivery,
    EstimatePoint,
    EstimateSubscription,
    LagCandidate,
    PairedSample,
    RecoveryPoint,
    ReferenceRelation,
    RelationReview,
    SeaCondition,
    SeaConditionWindow,
    SpatialAnalysis,
    Station,
    StationEligibility,
    StationGapFact,
    StationReference,
    TideSegment,
    ValidationRecord,
    estimate_publish_seq,
)

from .services import (
    UNIT_TO_M,
    ServiceError,
    _aware,
    current_datum_version,
    edges_at_version,
    station_datum_at_version,
)
from .models import RawObservation

# 状态枚举
ESTIMATED = "ESTIMATED"
WAITING = "WAITING_FUTURE_DATA"
UNAVAILABLE = "UNAVAILABLE"
PRODUCT_KIND = "SPATIAL_ESTIMATE"


# ====================================================================
# 目录资料：海况、资格、参考关系
# ====================================================================

def create_sea_condition(s: Session, code: str, name: str, description: str | None = None) -> SeaCondition:
    if s.execute(select(SeaCondition).where(SeaCondition.code == code)).scalar_one_or_none():
        raise ServiceError(409, f"sea condition {code} exists")
    row = SeaCondition(code=code, name=name, description=description)
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def add_sea_windows(s: Session, items: list[dict]) -> dict:
    """批量登记海况时间窗。海况只能来自明确资料，系统不自行推断类别。"""
    conds = {c.code: c.id for c in s.execute(select(SeaCondition)).scalars()}
    added = 0
    for it in items:
        cid = conds.get(it["condition"])
        if cid is None:
            raise ServiceError(404, f"unknown sea condition {it['condition']}")
        s.add(SeaConditionWindow(
            condition_id=cid,
            window_start=_aware(it["window_start"]),
            window_end=_aware(it["window_end"]),
            note=it.get("note", ""),
        ))
        added += 1
    s.commit()
    return {"added": added}


def set_eligibility(s: Session, station_id: int, valid_from: datetime,
                    valid_to: datetime | None, allowed_quality: list[str],
                    note: str = "") -> StationEligibility:
    row = StationEligibility(
        station_id=station_id, valid_from=_aware(valid_from),
        valid_to=_aware(valid_to) if valid_to else None,
        allowed_quality=allowed_quality, active=True, note=note,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def deactivate_eligibility(s: Session, eligibility_id: int) -> StationEligibility:
    """停用一条安装期/质量资格（如参考站在某时段退出）；历史分析保留其当时依据。"""
    row = s.get(StationEligibility, eligibility_id)
    if row is None:
        raise ServiceError(404, "eligibility not found")
    row.active = False
    s.commit()
    s.refresh(row)
    return row


def add_station_reference(s: Session, target_id: int, ref_id: int, pool: str,
                          bay_group: str, note: str = "") -> StationReference:
    if target_id == ref_id:
        raise ServiceError(422, "station cannot reference itself")
    if pool not in ("SINGLE", "MULTI"):
        raise ServiceError(422, "pool must be SINGLE or MULTI")
    # 环检测：单源传递关系图不允许甲->乙、乙->甲式互相支持
    if pool == "SINGLE" and _would_create_cycle(s, target_id, ref_id):
        raise ServiceError(409, "reference cycle refused: recursive chaining (A->B, B->A) not allowed")
    row = StationReference(
        target_station_id=target_id, ref_station_id=ref_id, pool=pool,
        bay_group=bay_group, active=True, note=note,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def _would_create_cycle(s: Session, target_id: int, ref_id: int) -> bool:
    """加入边 target->ref 后是否成环（沿现有 SINGLE 关系 DFS）。"""
    edges = s.execute(
        select(StationReference).where(StationReference.pool == "SINGLE",
                                       StationReference.active.is_(True))
    ).scalars().all()
    adj: dict[int, list[int]] = {}
    for e in edges:
        adj.setdefault(e.target_station_id, []).append(e.ref_station_id)
    adj.setdefault(target_id, []).append(ref_id)
    # DFS 从 ref 出发若能回到 target，则成环
    stack, seen = [ref_id], {ref_id}
    while stack:
        node = stack.pop()
        if node == target_id:
            return True
        for nxt in adj.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


# ====================================================================
# 分析创建与统一高程序列
# ====================================================================

def create_analysis(s: Session, spec) -> SpatialAnalysis:
    existing = s.execute(
        select(SpatialAnalysis).where(SpatialAnalysis.analysis_key == spec.analysis_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    target = s.execute(select(Station).where(Station.code == spec.target_station)).scalar_one_or_none()
    if target is None:
        raise ServiceError(404, f"unknown target station {spec.target_station}")
    target_datum = s.execute(
        select(Datum).where(Datum.code == spec.target_datum)
    ).scalar_one_or_none()
    if target_datum is None:
        raise ServiceError(404, f"unknown target datum {spec.target_datum}")

    ref_ids: list[int] = []
    for code in spec.ref_stations:
        st = s.execute(select(Station).where(Station.code == code)).scalar_one_or_none()
        if st is None:
            raise ServiceError(404, f"unknown reference station {code}")
        ref_ids.append(st.id)
    if not ref_ids:
        raise ServiceError(422, "at least one reference station required")
    if spec.path_mode not in ("SINGLE", "MULTI"):
        raise ServiceError(422, "path_mode must be SINGLE or MULTI")
    if spec.lag_min_sec > spec.lag_max_sec:
        raise ServiceError(422, "lag_min_sec must be <= lag_max_sec")
    if spec.lag_step_sec <= 0:
        raise ServiceError(422, "lag_step_sec must be positive")

    # 参考关系目录核对：只允许预先划定的关系
    catalog = s.execute(
        select(StationReference).where(
            StationReference.target_station_id == target.id,
            StationReference.pool == spec.path_mode,
            StationReference.active.is_(True),
        )
    ).scalars().all()
    allowed = {r.ref_station_id for r in catalog}
    bad = [rid for rid in ref_ids if rid not in allowed]
    if bad:
        raise ServiceError(422, f"reference stations not in pre-registered {spec.path_mode} relations: {bad}")

    version = current_datum_version(s)
    analysis = SpatialAnalysis(
        analysis_key=spec.analysis_key,
        target_station_id=target.id,
        target_datum_id=target_datum.id,
        datum_version_id=version.id,
        fit_start=_aware(spec.fit_start),
        fit_end=_aware(spec.fit_end),
        gap_start=_aware(spec.gap_start),
        gap_end=_aware(spec.gap_end),
        data_cutoff=_aware(spec.data_cutoff),
        grid_seconds=spec.grid_seconds,
        lag_min_sec=spec.lag_min_sec,
        lag_max_sec=spec.lag_max_sec,
        lag_step_sec=spec.lag_step_sec,
        path_mode=spec.path_mode,
        allowed_conditions=list(spec.allowed_conditions),
        ref_station_ids=ref_ids,
        validation_start=_aware(spec.validation_start) if getattr(spec, "validation_start", None) else None,
        n_val_segments=spec.n_val_segments,
        min_segment_samples=spec.min_segment_samples,
        error_method=spec.error_method or config.SPATIAL_ERROR_METHOD,
        status="CREATED",
    )
    s.add(analysis)
    # 缺测事实与分析同时登记，估计永远是旁路产品
    s.flush()
    fact = s.execute(
        select(StationGapFact).where(
            StationGapFact.station_id == target.id,
            StationGapFact.gap_start == analysis.gap_start,
        )
    ).scalar_one_or_none()
    if fact is None:
        s.add(StationGapFact(
            station_id=target.id, gap_start=analysis.gap_start, gap_end=analysis.gap_end,
            reason="registered with spatial analysis", evidence={"analysis_key": spec.analysis_key},
            analysis_id=analysis.id,
        ))
    s.commit()
    s.refresh(analysis)
    return analysis


def _eligibility_map(s: Session) -> dict[int, list[StationEligibility]]:
    rows = s.execute(select(StationEligibility).where(StationEligibility.active.is_(True))).scalars().all()
    out: dict[int, list[StationEligibility]] = {}
    for r in rows:
        out.setdefault(r.station_id, []).append(r)
    return out


def _eligible(elig: list[StationEligibility], t: datetime, quality: str) -> bool:
    for e in elig:
        if t < e.valid_from:
            continue
        if e.valid_to is not None and t >= e.valid_to:
            continue
        if quality in (e.allowed_quality or []):
            return True
    return False


def _condition_at(s: Session, t: datetime) -> str | None:
    win = s.execute(
        select(SeaConditionWindow).where(
            SeaConditionWindow.window_start <= t,
            SeaConditionWindow.window_end > t,
        )
    ).scalar_one_or_none()
    if win is None:
        return None
    return s.get(SeaCondition, win.condition_id).code


def measured_series(
    s: Session,
    st: Station,
    ws: datetime,
    we: datetime,
    analysis: SpatialAnalysis,
    elig_map: dict[int, list[StationEligibility]],
) -> dict[datetime, object]:
    """构造站点在 [ws,we) 的统一高程（目标基准）MEASURED 格元序列。

    筛选链：安装期+质量资格 -> 单位/正方向归一 -> 基准换算（冲突/无路径剔除）
    -> 共同时间格重采样 -> 只保留 MEASURED（INTERPOLATED/MISSING/PENDING 不构成观测支持）。
    """
    ws, we = _aware(ws), _aware(we)
    grid = analysis.grid_seconds
    pad = timedelta(seconds=grid * 2)
    rows = s.execute(
        select(RawObservation).where(
            RawObservation.station_id == st.id,
            RawObservation.is_current.is_(True),
            RawObservation.observed_at >= ws - pad,
            RawObservation.observed_at < we + pad,
        ).order_by(RawObservation.observed_at)
    ).scalars().all()

    elig = elig_map.get(st.id, [])
    edges = edges_at_version(s, analysis.datum_version_id)
    station_datum = station_datum_at_version(s, st.id, analysis.datum_version_id)
    samples: list[Sample] = []
    for obs in rows:
        t = _aware(obs.observed_at)
        if not _eligible(elig, t, obs.quality_flag):
            continue
        if station_datum is None:
            continue  # 基准资格不明（初报不明/无指派）不进入配对
        base = obs.value * UNIT_TO_M[obs.unit]
        if st.positive == "down":
            base = -base
        res = find_conversion(edges, station_datum, analysis.target_datum_id, t)
        if res.status != "OK":
            continue  # 基准冲突/无路径：不能被插值成训练配对
        samples.append(Sample(t=t, value_m=base + res.total_offset,
                              quality_flag=obs.quality_flag, obs_id=obs.id))

    cells = resample_window(samples, ws, we, grid,
                            max_rep_sec=grid * 2, min_coverage=0.5, max_interp_gap_sec=grid)
    return {c.grid_start: c for c in cells if c.grade == "MEASURED"}


# ====================================================================
# 分析运行：配对 -> 片段 -> 拟合 -> 验证 -> 关系
# ====================================================================

def _lag_values(analysis: SpatialAnalysis) -> list[int]:
    grid = analysis.grid_seconds
    lags = list(range(analysis.lag_min_sec, analysis.lag_max_sec + 1, analysis.lag_step_sec))
    return [lg for lg in lags if lg % grid == 0]


def _build_pairs_single(
    s: Session, analysis: SpatialAnalysis, ref_st: Station, lag: int,
    target_series: dict, ref_series: dict,
) -> list[spatial.Pair]:
    pairs: list[spatial.Pair] = []
    for t, tcell in sorted(target_series.items()):
        if not (analysis.fit_start <= t < analysis.fit_end):
            continue
        rt = t + timedelta(seconds=lag)
        rcell = ref_series.get(rt)
        if rcell is None:
            continue  # 两站窗口不同：只用共同有观测支持的时刻
        code = _condition_at(s, t)
        if code is None or code not in analysis.allowed_conditions:
            continue  # 海况类别缺失/不允许：不造配对，估计阶段同样停止
        pairs.append(spatial.Pair(
            t=t, target_value=tcell.value_m, ref_value=rcell.value_m,
            condition_code=code,
            target_obs_ids=tuple(tcell.sample_ids), ref_obs_ids=tuple(rcell.sample_ids),
        ))
    return pairs


def _persist_segments(s: Session, analysis: SpatialAnalysis, segments: list[spatial.Segment],
                      ref_id: int | None, combo: list[int] | None, lag: int) -> dict[datetime, int]:
    id_by_time: dict[datetime, int] = {}
    for seg in segments:
        row = TideSegment(
            analysis_id=analysis.id, ref_station_id=ref_id, ref_combo=combo, lag_sec=lag,
            seg_index=seg.index, seg_start=seg.start, seg_end=seg.end, split=seg.split,
            condition_codes=seg.condition_codes, pair_count=len(seg.pairs),
        )
        s.add(row)
        s.flush()
        seg.db_id = row.id
        for p in seg.pairs:
            id_by_time[p.t] = row.id
    return id_by_time


def _persist_pairs(s: Session, analysis: SpatialAnalysis, ref_id: int | None,
                   lag: int, segments: list[spatial.Segment], seg_id_by_time: dict) -> None:
    for seg in segments:
        for p in seg.pairs:
            s.add(PairedSample(
                analysis_id=analysis.id, ref_station_id=ref_id, lag_sec=lag,
                grid_time=p.t, ref_time=p.t + timedelta(seconds=lag),
                target_value_m=p.target_value, ref_value_m=p.ref_value,
                condition_code=p.condition_code, segment_id=seg_id_by_time[p.t],
                split=seg.split, target_obs_ids=list(p.target_obs_ids),
                ref_obs_ids=list(p.ref_obs_ids),
            ))


def _qualify(metrics: spatial.ConditionMetrics, analysis: SpatialAnalysis) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    ok = True
    if metrics.n < config.SPATIAL_MIN_VAL_N:
        ok = False
        reasons.append(f"validation sample n={metrics.n} < {config.SPATIAL_MIN_VAL_N}")
    if metrics.rmse > config.SPATIAL_MAX_VAL_RMSE_M:
        ok = False
        reasons.append(f"validation RMSE {metrics.rmse:.4f}m > {config.SPATIAL_MAX_VAL_RMSE_M}m")
    if metrics.condition_code not in analysis.allowed_conditions:
        ok = False
        reasons.append("condition not in analysis allowed list")
    return ok, reasons


def run_analysis(s: Session, analysis_id: int) -> dict:
    analysis = s.get(SpatialAnalysis, analysis_id)
    if analysis is None:
        raise ServiceError(404, "analysis not found")
    if analysis.status in ("FITTED", "RELATIONS_READY"):
        return {"analysis_id": analysis.id, "status": analysis.status, "idempotent": True}

    target = s.get(Station, analysis.target_station_id)
    refs = [s.get(Station, rid) for rid in analysis.ref_station_ids]
    elig_map = _eligibility_map(s)
    ws = analysis.fit_start
    we = max(analysis.fit_end, analysis.gap_end)
    target_series = measured_series(s, target, ws, we, analysis, elig_map)
    ref_series = {st.id: measured_series(s, st, ws, we, analysis, elig_map) for st in refs}

    # 清理重跑残留（幂等）
    for model in (ValidationRecord, LagCandidate, PairedSample, TideSegment, ReferenceRelation):
        for old in s.execute(select(model).where(model.analysis_id == analysis.id)).scalars():
            s.delete(old)
    s.flush()

    analysis.status = "FITTED"
    summary: dict = {"analysis_id": analysis.id, "mode": analysis.path_mode, "candidates": []}

    if analysis.path_mode == "SINGLE":
        relations = _run_single(s, analysis, refs, target_series, ref_series)
    else:
        relations = _run_multi(s, analysis, refs, target_series, ref_series)

    analysis.status = "RELATIONS_READY" if relations else "FITTED"
    s.commit()
    summary["relations"] = [
        {"id": r.id, "mode": r.mode, "ref_station_id": r.ref_station_id,
         "ref_combo": r.ref_combo, "lag_sec": r.lag_sec,
         "conditions": sorted(r.applicability.keys()), "status": r.status}
        for r in relations
    ]
    summary["relation_count"] = len(relations)
    return summary


def _fit_candidate(
    s: Session, analysis: SpatialAnalysis, segments: list[spatial.Segment],
    ref_id: int | None, combo: list[int] | None, lag: int,
    row_builder, promote: bool,
) -> tuple[LagCandidate, dict | None, ReferenceRelation | None]:
    """通用候选流程：落片段/配对 -> OLS -> 留出验证 -> 资格。

    参数（偏移/尺度/滞后）选择只使用 TRAIN 片段；VAL 片段从不参与拟合或选择，
    只独立报告误差并决定是否签发适用资格。promote=True 时才落地为关系。
    """
    train_segs = [g for g in segments if g.split == "TRAIN"]
    val_segs = [g for g in segments if g.split == "VAL"]
    train_pairs = [p for g in train_segs for p in g.pairs]
    val_pairs = [p for g in val_segs for p in g.pairs]
    n_features = len(combo) if combo else 1
    if len(segments) < 2 or not train_pairs or not val_pairs:
        cand = LagCandidate(
            analysis_id=analysis.id, ref_station_id=ref_id, ref_combo=combo, lag_sec=lag,
            train_n=len(train_pairs), val_n=len(val_pairs), identifiable=False,
            status="REJECTED_NO_SUPPORT",
            note="no held-out continuous validation segment (refusing train-only selection)",
        )
        s.add(cand)
        s.flush()
        return cand, None, None
    if len(train_pairs) < n_features + 2:
        cand = LagCandidate(
            analysis_id=analysis.id, ref_station_id=ref_id, ref_combo=combo, lag_sec=lag,
            train_n=len(train_pairs), val_n=len(val_pairs), identifiable=False,
            status="REJECTED_NO_SUPPORT", note="insufficient train samples",
        )
        s.add(cand)
        s.flush()
        return cand, None, None

    train_rows = [row_builder(p) for p in train_pairs]
    val_rows = [row_builder(p) for p in val_pairs]
    train_y = [p.target_value for p in train_pairs]
    val_y = [p.target_value for p in val_pairs]
    fit = spatial.fit_relation(train_rows, train_y, val_rows, val_y,
                               cond_limit=config.SPATIAL_COND_LIMIT)

    cand = LagCandidate(
        analysis_id=analysis.id, ref_station_id=ref_id, ref_combo=combo, lag_sec=lag,
        train_n=len(train_pairs), val_n=len(val_pairs),
        train_rmse=fit.train_rmse, val_rmse=fit.val_rmse,
        identifiable=fit.identifiable, condition_number=fit.condition_number,
        status="FIT" if fit.identifiable else "REJECTED_COLLINEAR", note=fit.note,
    )
    if fit.identifiable:
        scales = {str(stid): sc for stid, sc in zip(combo or [ref_id], fit.scales)}
        cand.fit_offset = fit.offset
        cand.fit_scales = scales
    s.add(cand)
    s.flush()
    if not fit.identifiable:
        return cand, None, None

    # 验证指标只在未参与选择的验证片段上计算，分海况列出
    val_pred = [spatial.predict(fit.offset, fit.scales, row) for row in val_rows]
    val_seg_ids = [g.db_id for g in val_segs for _ in g.pairs]
    metrics_list = spatial.metrics_by_condition(val_pairs, val_pred, val_seg_ids,
                                                 analysis.grid_seconds)
    applicability: dict = {}
    for m in metrics_list:
        qualified, reasons = _qualify(m, analysis)
        s.add(ValidationRecord(
            analysis_id=analysis.id, candidate_id=cand.id,
            ref_station_id=ref_id, ref_combo=combo, condition_code=m.condition_code,
            n_samples=m.n, coverage_sec=m.coverage_sec,
            amplitude_min=m.amplitude_min, amplitude_max=m.amplitude_max,
            mean_bias=m.mean_bias, mae=m.mae, rmse=m.rmse,
            extreme_error=m.extreme_error, qualified=qualified, reasons=reasons,
            segment_ids=m.segment_ids,
        ))
        if qualified:
            applicability[m.condition_code] = {
                "n": m.n, "coverage_sec": m.coverage_sec,
                "amplitude_min": m.amplitude_min, "amplitude_max": m.amplitude_max,
                "mean_bias": m.mean_bias, "mae": m.mae, "rmse": m.rmse,
                "extreme_error": m.extreme_error, "segment_ids": m.segment_ids,
                "error_method": analysis.error_method,
                "error_halfwidth": spatial.error_halfwidth(analysis.error_method, m.rmse, m.n),
            }
    if not applicability:
        # 样本很多但只覆盖平稳期：不签发风暴等未验证海况的资格
        return cand, None, None
    if not promote:
        return cand, applicability, None

    relation = _make_relation(s, analysis, ref_id, combo, lag, cand, applicability)
    return cand, applicability, relation


def _make_relation(s: Session, analysis: SpatialAnalysis, ref_id: int | None,
                   combo: list[int] | None, lag: int, cand: LagCandidate,
                   applicability: dict) -> ReferenceRelation:
    relation = ReferenceRelation(
        analysis_id=analysis.id, target_station_id=analysis.target_station_id,
        mode=analysis.path_mode, ref_station_id=ref_id, ref_combo=combo, lag_sec=lag,
        params={"offset": cand.fit_offset,
                "scales": {k: v for k, v in (cand.fit_scales or {}).items()},
                "lag_sec": lag},
        applicability=applicability, validation_candidate_id=cand.id, status="ACTIVE",
    )
    s.add(relation)
    s.flush()
    return relation


def _assign_splits(analysis: SpatialAnalysis, segments: list[spatial.Segment]) -> list[spatial.Segment]:
    """划分拟合/验证：优先用预先划定的验证期时间边界；否则取末尾 n 个连续片段。"""
    if analysis.validation_start is not None:
        return spatial.split_train_val(segments, _aware(analysis.validation_start))
    return spatial.assign_splits(segments, analysis.n_val_segments)


def _run_single(s: Session, analysis: SpatialAnalysis, refs: list[Station],
                target_series: dict, ref_series: dict) -> list[ReferenceRelation]:
    relations: list[ReferenceRelation] = []
    for ref in refs:
        # 每个参考站独立试算全部滞后；滞后选择只看 TRAIN RMSE，
        # 被选滞后还须通过独立 VAL 验证才签发关系（不能只选训练最优而不验证）。
        tried: list[tuple[LagCandidate, dict | None]] = []
        for lag in _lag_values(analysis):
            pairs = _build_pairs_single(s, analysis, ref, lag, target_series, ref_series[ref.id])
            segments = spatial.split_segments(pairs, analysis.grid_seconds, analysis.grid_seconds * 2)
            segments = _assign_splits(analysis, segments)
            seg_ids = _persist_segments(s, analysis, segments, ref.id, None, lag)
            _persist_pairs(s, analysis, ref.id, lag, segments, seg_ids)
            cand, applicability, _ = _fit_candidate(
                s, analysis, segments, ref.id, None, lag,
                row_builder=lambda p: [p.ref_value], promote=False,
            )
            tried.append((cand, applicability))
        identifiable = [(c, a) for c, a in tried if c.identifiable and a]
        if not identifiable:
            continue
        best_cand, best_app = min(identifiable, key=lambda x: (x[0].train_rmse, abs(x[0].lag_sec)))
        relations.append(_make_relation(s, analysis, ref.id, None, best_cand.lag_sec,
                                        best_cand, best_app))
    return relations


def _multi_combos(ref_ids: list[int]) -> list[list[int]]:
    """全部非空来源组合：任一组合都必须独立验证，一站退出不能直接把权重分给剩余站。"""
    out: list[list[int]] = []
    for mask in range(1, 1 << len(ref_ids)):
        out.append([rid for i, rid in enumerate(ref_ids) if mask & (1 << i)])
    return out


def _run_multi(s: Session, analysis: SpatialAnalysis, refs: list[Station],
               target_series: dict, ref_series: dict) -> list[ReferenceRelation]:
    relations: list[ReferenceRelation] = []
    for combo in _multi_combos(analysis.ref_station_ids):
        # 每个来源组合独立试算、独立验证；选择只看 TRAIN，资格由独立 VAL 决定。
        tried: list[tuple[LagCandidate, dict | None]] = []
        for lag in _lag_values(analysis):
            # 共同支持：目标时刻 + 组合内全部参考站在对齐时刻都有 MEASURED
            pairs: list[spatial.Pair] = []
            for t, tcell in sorted(target_series.items()):
                if not (analysis.fit_start <= t < analysis.fit_end):
                    continue
                rt = t + timedelta(seconds=lag)
                cells = [ref_series[rid].get(rt) for rid in combo]
                if any(c is None for c in cells):
                    continue
                code = _condition_at(s, t)
                if code is None or code not in analysis.allowed_conditions:
                    continue
                pairs.append(spatial.Pair(
                    t=t, target_value=tcell.value_m,
                    ref_value=cells[0].value_m,  # 多源值走 row_builder，Pair 仅承载时刻/海况
                    condition_code=code,
                    target_obs_ids=tuple(tcell.sample_ids),
                    ref_obs_ids=tuple(oid for c in cells for oid in c.sample_ids),
                ))
            if not pairs:
                continue
            # 多源配对需要各参考站值，挂在旁路映射上
            value_map = {
                p.t: [ref_series[rid][p.t + timedelta(seconds=lag)].value_m for rid in combo]
                for p in pairs
            }
            segments = spatial.split_segments(pairs, analysis.grid_seconds, analysis.grid_seconds * 2)
            segments = _assign_splits(analysis, segments)
            seg_ids = _persist_segments(s, analysis, segments, None, combo, lag)
            _persist_pairs(s, analysis, None, lag, segments, seg_ids)
            cand, applicability, _ = _fit_candidate(
                s, analysis, segments, None, combo, lag,
                row_builder=lambda p: value_map[p.t], promote=False,
            )
            tried.append((cand, applicability))
        identifiable = [(c, a) for c, a in tried if c.identifiable and a]
        if not identifiable:
            continue  # 共线/无独立验证支持的组合保持待核，不产出关系
        best_cand, best_app = min(identifiable, key=lambda x: (x[0].train_rmse, abs(x[0].lag_sec)))
        relations.append(_make_relation(s, analysis, None, combo, best_cand.lag_sec,
                                        best_cand, best_app))
    return relations


# ====================================================================
# 适用性查询
# ====================================================================

def applicability_for(s: Session, station_code: str, condition_code: str | None = None) -> list[dict]:
    st = s.execute(select(Station).where(Station.code == station_code)).scalar_one_or_none()
    if st is None:
        raise ServiceError(404, "unknown station")
    rows = s.execute(
        select(ReferenceRelation).where(
            ReferenceRelation.target_station_id == st.id,
            ReferenceRelation.status == "ACTIVE",
        ).order_by(ReferenceRelation.id)
    ).scalars().all()
    out = []
    for r in rows:
        for code, app in sorted(r.applicability.items()):
            if condition_code and code != condition_code:
                continue
            out.append({
                "relation_id": r.id, "mode": r.mode,
                "ref_station_id": r.ref_station_id, "ref_combo": r.ref_combo,
                "lag_sec": r.lag_sec, "condition_code": code, **app,
            })
    return out


# ====================================================================
# 估计生成（截止点感知，独立产品）
# ====================================================================

def generate_estimates(s: Session, analysis_id: int, batch_key: str,
                       data_cutoff: datetime | None = None) -> EstimateBatch:
    analysis = s.get(SpatialAnalysis, analysis_id)
    if analysis is None:
        raise ServiceError(404, "analysis not found")
    if analysis.status not in ("FITTED", "RELATIONS_READY"):
        raise ServiceError(409, "analysis has not been run")
    existing = s.execute(select(EstimateBatch).where(EstimateBatch.batch_key == batch_key)).scalar_one_or_none()
    if existing is not None:
        return existing

    cutoff = _aware(data_cutoff) if data_cutoff else analysis.data_cutoff
    target = s.get(Station, analysis.target_station_id)
    elig_map = _eligibility_map(s)
    # 估计只取真实观测支持；参考站自身缺测即无来源，不递归接力。
    # 窗口上界需覆盖正向滞后：最后一个目标格的对齐时刻可达 gap_end + lag_max。
    ws = analysis.fit_start
    we = analysis.gap_end + timedelta(
        seconds=max(0, analysis.lag_max_sec) + analysis.grid_seconds)
    series_map = {st.id: measured_series(s, st, ws, we, analysis, elig_map)
                  for st in [target] + [s.get(Station, rid) for rid in analysis.ref_station_ids]}

    relations = s.execute(
        select(ReferenceRelation).where(
            ReferenceRelation.analysis_id == analysis.id,
            ReferenceRelation.status == "ACTIVE",
        )
    ).scalars().all()

    batch = EstimateBatch(
        batch_key=batch_key, product_kind=PRODUCT_KIND, analysis_id=analysis.id,
        target_station_id=analysis.target_station_id, data_cutoff=cutoff,
        gap_start=analysis.gap_start, gap_end=analysis.gap_end,
        mode=analysis.path_mode, status="ISSUED",
        publish_seq=s.execute(select(estimate_publish_seq.next_value())).scalar(),
    )
    s.add(batch)
    s.flush()

    grid = analysis.grid_seconds
    t = analysis.gap_start
    n_est = n_wait = n_unavail = 0
    while t < analysis.gap_end:
        point, status = _estimate_cell(s, analysis, relations, series_map, t, cutoff)
        s.add(point)
        batch_id = batch.id
        point.batch_id = batch_id
        n_est += status == ESTIMATED
        n_wait += status == WAITING
        n_unavail += status == UNAVAILABLE
        t += timedelta(seconds=grid)

    if n_est == 0:
        batch.status = "ALL_UNAVAILABLE" if n_wait == 0 else "PARTIAL"
    elif n_wait or n_unavail:
        batch.status = "PARTIAL"
    s.flush()
    _queue_estimate_deliveries(s, batch)
    s.commit()
    s.refresh(batch)
    return batch


def _relation_sources(relation: ReferenceRelation) -> list[int]:
    if relation.mode == "SINGLE":
        return [relation.ref_station_id]
    return list(relation.ref_combo or [])


def _estimate_cell(s: Session, analysis: SpatialAnalysis, relations: list[ReferenceRelation],
                   series_map: dict, t: datetime, cutoff: datetime) -> tuple[EstimatePoint, str]:
    grid = analysis.grid_seconds
    base = EstimatePoint(target_station_id=analysis.target_station_id, grid_time=t,
                         reasons=[], source_refs=[], source_obs_ids=[], applicability={})
    code = _condition_at(s, t)

    # 1) 海况类别缺失：停止估计，绝不按候选结果反选海况
    if code is None:
        base.status = UNAVAILABLE
        base.reasons = ["sea condition unknown at target time; estimation stopped"]
        return base, UNAVAILABLE

    # 2) 该海况下有验证资格的关系，按验证 RMSE 确定选择顺序（切换依据可解释）
    qualified = [rel for rel in relations if rel.applicability.get(code)]
    if not qualified:
        base.status = UNAVAILABLE
        base.condition_code = code
        base.reasons = [f"no relation validated for condition {code}; "
                        f"calm-only validation cannot issue storm applicability"]
        return base, UNAVAILABLE
    qualified.sort(key=lambda rel: (rel.applicability[code]["rmse"],
                                    len(_relation_sources(rel))))

    # 3) 依次尝试：正向滞后需要目标时刻之后的参考读数
    for rel in qualified:
        app = rel.applicability[code]
        lag = rel.lag_sec
        rt = t + timedelta(seconds=lag)
        source_ids = _relation_sources(rel)
        ready_at = rt + timedelta(seconds=grid)

        # 3a) 所需读数时刻在资料截止点之后：实时不可知，返回等待（不用后来读数伪装）
        if rt > cutoff:
            base.status = WAITING
            base.condition_code = code
            base.relation_id = rel.id
            base.ready_at = ready_at
            base.applicability = app
            base.reasons = [
                f"aligned reference time {rt.isoformat()} later than cutoff "
                f"{cutoff.isoformat()}; future data not yet available"]
            return base, WAITING

        # 3b) 读数已到期：必须有真实观测支持；参考站自身缺测不递归接力，转下一个备用关系
        cells = [series_map.get(sid, {}).get(rt) for sid in source_ids]
        if any(c is None for c in cells):
            base.reasons.append(
                f"relation {rel.id} source missing at {rt.isoformat()}; fallback to next relation")
            continue

        # 4) 来源展开到真实观测；重复物理记录不增加独立支持
        source_refs = []
        obs_ids: list[int] = []
        ref_codes = {sid: s.get(Station, sid).code for sid in source_ids}
        for sid, cell in zip(source_ids, cells):
            oids = sorted(set(cell.sample_ids))
            source_refs.append({
                "station_id": sid, "station_code": ref_codes[sid],
                "aligned_time": rt.isoformat(), "obs_ids": oids,
                "value_m": cell.value_m, "quality_grade": cell.grade,
            })
            for oid in oids:
                if oid not in obs_ids:
                    obs_ids.append(oid)

        params = rel.params
        x_row = [cell.value_m for cell in cells]
        scales = tuple(params["scales"][str(sid)] for sid in source_ids)
        value = spatial.predict(params["offset"], scales, x_row)

        base.status = ESTIMATED
        base.condition_code = code
        base.relation_id = rel.id
        base.source_refs = source_refs
        base.source_obs_ids = obs_ids
        base.value_m = round(value, 6)
        base.error_method = app["error_method"]
        base.error_halfwidth = app["error_halfwidth"]
        base.error_n = app["n"]
        base.applicability = app
        base.ready_at = ready_at
        # 超出验证幅度范围：标明外推受限（仍给出值，但不假装误差带必然覆盖）
        if not (app["amplitude_min"] <= value <= app["amplitude_max"]):
            base.extrapolation_limited = True
            base.reasons.append(
                f"value {value:.3f}m outside validated amplitude "
                f"[{app['amplitude_min']:.3f},{app['amplitude_max']:.3f}]; extrapolation limited")
        return base, ESTIMATED

    # 5) 所有合格关系的来源在到期后仍缺测：该时刻不可用（不引用估计产品接力）
    base.status = UNAVAILABLE
    base.condition_code = code
    base.reasons.append("all validated relations lack real observed support after cutoff")
    return base, UNAVAILABLE


def _queue_estimate_deliveries(s: Session, batch: EstimateBatch) -> None:
    """估计批次与待交付任务同事务写入；只投给显式声明接收估计的订阅。"""
    subs = s.execute(
        select(EstimateSubscription).where(EstimateSubscription.active.is_(True))
    ).scalars().all()
    target_region = s.get(Station, batch.target_station_id).region
    for sub in subs:
        if not sub.accept_estimates:
            continue  # 只消费实测确认值的通道：显式拒绝，不混入
        if sub.region != target_region:
            continue
        if sub.target_station_id is not None and sub.target_station_id != batch.target_station_id:
            continue
        key = f"est-sub:{sub.id}-batch:{batch.id}"
        if s.execute(select(EstimateDelivery).where(EstimateDelivery.delivery_key == key)).scalar_one_or_none():
            continue
        s.add(EstimateDelivery(
            delivery_key=key, subscription_id=sub.id, batch_id=batch.id,
            publish_seq=batch.publish_seq,
        ))


# ====================================================================
# 缺测事实、恢复对照与停用
# ====================================================================

def list_gap_facts(s: Session, station_code: str) -> list[dict]:
    st = s.execute(select(Station).where(Station.code == station_code)).scalar_one_or_none()
    if st is None:
        raise ServiceError(404, "unknown station")
    rows = s.execute(
        select(StationGapFact).where(StationGapFact.station_id == st.id)
        .order_by(StationGapFact.gap_start)
    ).scalars().all()
    return [
        {"id": r.id, "station": st.code, "gap_start": r.gap_start.isoformat(),
         "gap_end": r.gap_end.isoformat(), "reason": r.reason, "evidence": r.evidence,
         "resolved": r.resolved, "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None}
        for r in rows
    ]


def review_recovery(s: Session, analysis_id: int, relation_id: int,
                    threshold_m: float) -> RelationReview:
    """目标站恢复后对照迟到实测。只读取原估计，不允许重新拟合。"""
    analysis = s.get(SpatialAnalysis, analysis_id)
    relation = s.get(ReferenceRelation, relation_id)
    if analysis is None or relation is None:
        raise ServiceError(404, "analysis/relation not found")
    target = s.get(Station, analysis.target_station_id)
    elig_map = _eligibility_map(s)
    measured = measured_series(s, target, analysis.gap_start, analysis.gap_end, analysis, elig_map)

    batches = s.execute(
        select(EstimateBatch).where(EstimateBatch.analysis_id == analysis.id)
        .order_by(EstimateBatch.id)
    ).scalars().all()
    review = RelationReview(
        relation_id=relation.id, analysis_id=analysis.id,
        target_station_id=target.id, threshold_m=threshold_m,
        max_abs_bias=0.0, decision="CONTINUE",
    )
    s.add(review)
    s.flush()

    segment_results: list[dict] = []
    affected_batches: list[int] = []
    for batch in batches:
        points = s.execute(
            select(EstimatePoint).where(
                EstimatePoint.batch_id == batch.id,
                EstimatePoint.relation_id == relation.id,
                EstimatePoint.status == ESTIMATED,
            ).order_by(EstimatePoint.grid_time)
        ).scalars().all()
        biases = []
        for p in points:
            mcell = measured.get(p.grid_time)
            if mcell is None:
                continue  # 仍缺测的时刻不参与对照
            bias = p.value_m - mcell.value_m
            biases.append(bias)
            s.add(RecoveryPoint(
                review_id=review.id, batch_id=batch.id, grid_time=p.grid_time,
                estimated_value_m=p.value_m, observed_value_m=mcell.value_m,
                bias_m=bias, condition_code=p.condition_code,
            ))
        if not biases:
            continue
        affected_batches.append(batch.id)
        seg_bias = sum(biases) / len(biases)
        segment_results.append({
            "batch_id": batch.id, "n": len(biases),
            "mean_bias": seg_bias, "max_abs_bias": max(abs(b) for b in biases),
            "threshold_exceeded": abs(seg_bias) > threshold_m,
        })

    max_abs = max((abs(x["mean_bias"]) for x in segment_results), default=0.0)
    review.max_abs_bias = max_abs
    review.segment_results = segment_results
    review.affected_batch_ids = affected_batches

    receivers: list[dict] = []
    if max_abs > threshold_m:
        relation.status = "DISABLED"
        relation.disable_reason = (
            f"post-recovery review: max segment mean bias {max_abs:.4f}m > threshold {threshold_m}m")
        relation.disabled_at = datetime.now(timezone.utc)
        review.decision = "DISABLE"
        # 找出引用该关系的估计批次及其接收方（实测产品不在此列，不受牵连）
        delivery_rows = s.execute(
            select(EstimateDelivery).where(EstimateDelivery.batch_id.in_(affected_batches))
        ).scalars().all()
        for d in delivery_rows:
            sub = s.get(EstimateSubscription, d.subscription_id)
            receivers.append({"delivery_id": d.id, "delivery_key": d.delivery_key,
                              "subscription": sub.name, "status": d.status})
    review.affected_receivers = receivers
    s.commit()
    s.refresh(review)
    return review


def resolve_gap_fact(s: Session, fact_id: int) -> StationGapFact:
    fact = s.get(StationGapFact, fact_id)
    if fact is None:
        raise ServiceError(404, "gap fact not found")
    fact.resolved = True
    fact.resolved_at = datetime.now(timezone.utc)
    s.commit()
    s.refresh(fact)
    return fact
