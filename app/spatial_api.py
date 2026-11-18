"""缺测潮位空间参考估计 API（独立产品类型，独立订阅/交付通道）。

路由统一挂在 /spatial 前缀下；实测产品（/products、/deliveries）与估计产品
（/spatial/batches、/spatial/estimate-deliveries）在 URL、表、发布序号上完全分开，
只消费实测确认值的订阅者不会收到任何估计。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from .db import SessionLocal
from .models import (
    EstimateBatch,
    EstimateDelivery,
    EstimatePoint,
    EstimateSubscription,
    LagCandidate,
    PairedSample,
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
)
from .schemas import (
    AnalysisIn,
    EligibilityIn,
    EstimateBatchIn,
    EstimateSubscriptionIn,
    RecoveryReviewIn,
    SeaConditionIn,
    SeaWindowBatch,
    StationReferenceIn,
)
from .security import require_role
from . import spatial_services as svc
from .services import ServiceError, _aware

router = APIRouter(prefix="/spatial", tags=["spatial-estimate"])

ADMIN = "admin"
OPERATOR = "operator"
CONSUMER = "consumer"
ALL_ROLES = ("admin", "operator", "consumer")


def _handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


def _station_id(s, code: str) -> int:
    st = s.execute(select(Station).where(Station.code == code)).scalar_one_or_none()
    if st is None:
        raise HTTPException(404, f"unknown station {code}")
    return st.id


# ---------------- 海况资料 ----------------

@router.post("/sea-conditions", status_code=201)
def create_sea_condition(body: SeaConditionIn, _role: str = Depends(require_role(ADMIN))):
    s = SessionLocal()
    try:
        row = _handle(svc.create_sea_condition, s, body.code, body.name, body.description)
        return {"id": row.id, "code": row.code}
    finally:
        s.close()


@router.get("/sea-conditions")
def list_sea_conditions(_role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        return [{"id": c.id, "code": c.code, "name": c.name, "description": c.description}
                for c in s.execute(select(SeaCondition).order_by(SeaCondition.id)).scalars()]
    finally:
        s.close()


@router.post("/sea-windows", status_code=201)
def add_sea_windows(body: SeaWindowBatch, _role: str = Depends(require_role(ADMIN, OPERATOR))):
    s = SessionLocal()
    try:
        return _handle(svc.add_sea_windows, s, [it.model_dump() for it in body.items])
    finally:
        s.close()


@router.get("/sea-windows")
def list_sea_windows(_role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        conds = {c.id: c.code for c in s.execute(select(SeaCondition)).scalars()}
        rows = s.execute(select(SeaConditionWindow).order_by(SeaConditionWindow.window_start)).scalars()
        return [{"id": w.id, "condition": conds[w.condition_id],
                 "window_start": w.window_start.isoformat(), "window_end": w.window_end.isoformat(),
                 "note": w.note} for w in rows]
    finally:
        s.close()


# ---------------- 安装期资格与参考关系 ----------------

@router.post("/eligibility", status_code=201)
def set_eligibility(body: EligibilityIn, _role: str = Depends(require_role(ADMIN))):
    s = SessionLocal()
    try:
        st_id = _station_id(s, body.station)
        row = _handle(svc.set_eligibility, s, st_id, body.valid_from, body.valid_to,
                      body.allowed_quality, body.note)
        return {"id": row.id, "station_id": row.station_id,
                "valid_from": row.valid_from.isoformat(),
                "valid_to": row.valid_to.isoformat() if row.valid_to else None,
                "allowed_quality": row.allowed_quality}
    finally:
        s.close()


@router.get("/eligibility")
def list_eligibility(station: str | None = None,
                     _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        q = select(StationEligibility).order_by(StationEligibility.id)
        if station:
            q = q.where(StationEligibility.station_id == _station_id(s, station))
        codes = {st.id: st.code for st in s.execute(select(Station)).scalars()}
        return [{"id": e.id, "station": codes.get(e.station_id),
                 "valid_from": e.valid_from.isoformat(),
                 "valid_to": e.valid_to.isoformat() if e.valid_to else None,
                 "allowed_quality": e.allowed_quality, "active": e.active, "note": e.note}
                for e in s.execute(q).scalars()]
    finally:
        s.close()


@router.post("/eligibility/{eligibility_id}/deactivate")
def deactivate_eligibility(eligibility_id: int,
                           _role: str = Depends(require_role(ADMIN, OPERATOR))):
    """停用一条安装期/质量资格（参考站异常退出等场景）。"""
    s = SessionLocal()
    try:
        row = _handle(svc.deactivate_eligibility, s, eligibility_id)
        return {"id": row.id, "active": row.active}
    finally:
        s.close()


@router.post("/station-references", status_code=201)
def add_reference(body: StationReferenceIn, _role: str = Depends(require_role(ADMIN))):
    s = SessionLocal()
    try:
        tid = _station_id(s, body.target_station)
        rid = _station_id(s, body.ref_station)
        row = _handle(svc.add_station_reference, s, tid, rid, body.pool,
                      body.bay_group, body.note)
        return {"id": row.id, "target_station_id": row.target_station_id,
                "ref_station_id": row.ref_station_id, "pool": row.pool,
                "bay_group": row.bay_group}
    finally:
        s.close()


@router.get("/station-references")
def list_references(_role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        codes = {st.id: st.code for st in s.execute(select(Station)).scalars()}
        rows = s.execute(select(StationReference).order_by(StationReference.id)).scalars()
        return [{"id": r.id, "target_station": codes.get(r.target_station_id),
                 "ref_station": codes.get(r.ref_station_id), "pool": r.pool,
                 "bay_group": r.bay_group, "active": r.active, "note": r.note}
                for r in rows]
    finally:
        s.close()


# ---------------- 分析：配对资料导入、滞后试算、连续片段验证 ----------------

@router.post("/analyses", status_code=201)
def create_analysis(body: AnalysisIn, _role: str = Depends(require_role(OPERATOR, ADMIN))):
    s = SessionLocal()
    try:
        row = _handle(svc.create_analysis, s, body)
        return {"id": row.id, "analysis_key": row.analysis_key, "status": row.status,
                "path_mode": row.path_mode, "data_cutoff": row.data_cutoff.isoformat()}
    finally:
        s.close()


@router.get("/analyses")
def list_analyses(_role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        return [_analysis_out(a) for a in
                s.execute(select(SpatialAnalysis).order_by(SpatialAnalysis.id)).scalars()]
    finally:
        s.close()


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: int, _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        a = s.get(SpatialAnalysis, analysis_id)
        if a is None:
            raise HTTPException(404, "analysis not found")
        return _analysis_out(a)
    finally:
        s.close()


def _analysis_out(a: SpatialAnalysis) -> dict:
    return {
        "id": a.id, "analysis_key": a.analysis_key,
        "target_station_id": a.target_station_id, "target_datum_id": a.target_datum_id,
        "fit_start": a.fit_start.isoformat(), "fit_end": a.fit_end.isoformat(),
        "gap_start": a.gap_start.isoformat(), "gap_end": a.gap_end.isoformat(),
        "data_cutoff": a.data_cutoff.isoformat(), "grid_seconds": a.grid_seconds,
        "lag_min_sec": a.lag_min_sec, "lag_max_sec": a.lag_max_sec,
        "lag_step_sec": a.lag_step_sec, "path_mode": a.path_mode,
        "allowed_conditions": a.allowed_conditions, "ref_station_ids": a.ref_station_ids,
        "validation_start": a.validation_start.isoformat() if a.validation_start else None,
        "n_val_segments": a.n_val_segments, "error_method": a.error_method, "status": a.status,
    }


@router.post("/analyses/{analysis_id}/run", status_code=201)
def run_analysis(analysis_id: int, _role: str = Depends(require_role(OPERATOR, ADMIN))):
    """执行配对、滞后试算、连续片段验证与关系签发（全部实际计算）。"""
    s = SessionLocal()
    try:
        return _handle(svc.run_analysis, s, analysis_id)
    finally:
        s.close()


@router.get("/analyses/{analysis_id}/segments")
def get_segments(analysis_id: int, _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        rows = s.execute(
            select(TideSegment).where(TideSegment.analysis_id == analysis_id)
            .order_by(TideSegment.ref_station_id, TideSegment.ref_combo,
                      TideSegment.lag_sec, TideSegment.seg_index)
        ).scalars()
        return [{"id": g.id, "ref_station_id": g.ref_station_id, "ref_combo": g.ref_combo,
                 "lag_sec": g.lag_sec, "seg_index": g.seg_index,
                 "seg_start": g.seg_start.isoformat(), "seg_end": g.seg_end.isoformat(),
                 "split": g.split, "condition_codes": g.condition_codes,
                 "pair_count": g.pair_count} for g in rows]
    finally:
        s.close()


@router.get("/analyses/{analysis_id}/pairs")
def get_pairs(analysis_id: int, lag_sec: int | None = None,
              split: str | None = None, _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        q = select(PairedSample).where(PairedSample.analysis_id == analysis_id)
        if lag_sec is not None:
            q = q.where(PairedSample.lag_sec == lag_sec)
        if split:
            q = q.where(PairedSample.split == split)
        q = q.order_by(PairedSample.lag_sec, PairedSample.grid_time).limit(2000)
        return [{"id": p.id, "ref_station_id": p.ref_station_id, "lag_sec": p.lag_sec,
                 "grid_time": p.grid_time.isoformat(), "ref_time": p.ref_time.isoformat(),
                 "target_value_m": p.target_value_m, "ref_value_m": p.ref_value_m,
                 "condition_code": p.condition_code, "split": p.split,
                 "target_obs_ids": p.target_obs_ids, "ref_obs_ids": p.ref_obs_ids}
                for p in s.execute(q).scalars()]
    finally:
        s.close()


@router.get("/analyses/{analysis_id}/lag-candidates")
def get_lag_candidates(analysis_id: int, _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        rows = s.execute(
            select(LagCandidate).where(LagCandidate.analysis_id == analysis_id)
            .order_by(LagCandidate.ref_station_id, LagCandidate.ref_combo, LagCandidate.lag_sec)
        ).scalars()
        return [{"id": c.id, "ref_station_id": c.ref_station_id, "ref_combo": c.ref_combo,
                 "lag_sec": c.lag_sec, "fit_offset": c.fit_offset, "fit_scales": c.fit_scales,
                 "train_n": c.train_n, "val_n": c.val_n,
                 "train_rmse": c.train_rmse, "val_rmse": c.val_rmse,
                 "identifiable": c.identifiable, "condition_number": c.condition_number,
                 "status": c.status, "note": c.note} for c in rows]
    finally:
        s.close()


@router.get("/analyses/{analysis_id}/validations")
def get_validations(analysis_id: int, _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        rows = s.execute(
            select(ValidationRecord).where(ValidationRecord.analysis_id == analysis_id)
            .order_by(ValidationRecord.condition_code, ValidationRecord.id)
        ).scalars()
        return [{"id": v.id, "candidate_id": v.candidate_id,
                 "ref_station_id": v.ref_station_id, "ref_combo": v.ref_combo,
                 "condition_code": v.condition_code, "n_samples": v.n_samples,
                 "coverage_sec": v.coverage_sec,
                 "amplitude_min": v.amplitude_min, "amplitude_max": v.amplitude_max,
                 "mean_bias": v.mean_bias, "mae": v.mae, "rmse": v.rmse,
                 "extreme_error": v.extreme_error, "qualified": v.qualified,
                 "reasons": v.reasons, "segment_ids": v.segment_ids} for v in rows]
    finally:
        s.close()


# ---------------- 关系与适用性 ----------------

@router.get("/relations")
def list_relations(station: str | None = None,
                   _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        q = select(ReferenceRelation).order_by(ReferenceRelation.id)
        if station:
            q = q.where(ReferenceRelation.target_station_id == _station_id(s, station))
        return [_relation_out(r) for r in s.execute(q).scalars()]
    finally:
        s.close()


def _relation_out(r: ReferenceRelation) -> dict:
    return {"id": r.id, "analysis_id": r.analysis_id,
            "target_station_id": r.target_station_id, "mode": r.mode,
            "ref_station_id": r.ref_station_id, "ref_combo": r.ref_combo,
            "lag_sec": r.lag_sec, "params": r.params,
            "applicability": r.applicability, "status": r.status,
            "disable_reason": r.disable_reason,
            "created_at": r.created_at.isoformat()}


@router.get("/applicability")
def get_applicability(station: str, condition: str | None = None,
                      _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        return _handle(svc.applicability_for, s, station, condition)
    finally:
        s.close()


# ---------------- 缺测事实（估计永不覆盖） ----------------

@router.get("/gap-facts")
def get_gap_facts(station: str, _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        return _handle(svc.list_gap_facts, s, station)
    finally:
        s.close()


@router.post("/gap-facts/{fact_id}/resolve")
def resolve_gap(fact_id: int, _role: str = Depends(require_role(OPERATOR, ADMIN))):
    s = SessionLocal()
    try:
        row = _handle(svc.resolve_gap_fact, s, fact_id)
        return {"id": row.id, "resolved": row.resolved,
                "resolved_at": row.resolved_at.isoformat()}
    finally:
        s.close()


# ---------------- 估计生成与估计序列 ----------------

@router.post("/estimates:generate", status_code=201)
def generate_estimates(body: EstimateBatchIn,
                       _role: str = Depends(require_role(OPERATOR, ADMIN))):
    s = SessionLocal()
    try:
        a = s.execute(select(SpatialAnalysis).where(
            SpatialAnalysis.analysis_key == body.analysis_key)).scalar_one_or_none()
        if a is None:
            raise HTTPException(404, "unknown analysis_key")
        batch = _handle(svc.generate_estimates, s, a.id, body.batch_key,
                        _aware(body.data_cutoff) if body.data_cutoff else None)
        return _batch_out(s, batch)
    finally:
        s.close()


@router.get("/batches")
def list_batches(analysis_key: str | None = None,
                 _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        q = select(EstimateBatch).order_by(EstimateBatch.id)
        if analysis_key:
            a = s.execute(select(SpatialAnalysis).where(
                SpatialAnalysis.analysis_key == analysis_key)).scalar_one_or_none()
            if a is None:
                raise HTTPException(404, "unknown analysis_key")
            q = q.where(EstimateBatch.analysis_id == a.id)
        return [_batch_out(s, b) for b in s.execute(q).scalars()]
    finally:
        s.close()


def _batch_out(s, b: EstimateBatch) -> dict:
    points = s.execute(select(EstimatePoint).where(EstimatePoint.batch_id == b.id)).scalars().all()
    counts: dict[str, int] = {}
    for p in points:
        counts[p.status] = counts.get(p.status, 0) + 1
    return {"id": b.id, "batch_key": b.batch_key, "product_kind": b.product_kind,
            "analysis_id": b.analysis_id, "target_station_id": b.target_station_id,
            "data_cutoff": b.data_cutoff.isoformat(),
            "gap_start": b.gap_start.isoformat(), "gap_end": b.gap_end.isoformat(),
            "mode": b.mode, "status": b.status, "publish_seq": b.publish_seq,
            "point_counts": counts, "created_at": b.created_at.isoformat()}


@router.get("/batches/{batch_id}/points")
def get_batch_points(batch_id: int, _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        if s.get(EstimateBatch, batch_id) is None:
            raise HTTPException(404, "batch not found")
        rows = s.execute(
            select(EstimatePoint).where(EstimatePoint.batch_id == batch_id)
            .order_by(EstimatePoint.grid_time)
        ).scalars()
        return [{"id": p.id, "grid_time": p.grid_time.isoformat(), "value_m": p.value_m,
                 "status": p.status, "condition_code": p.condition_code,
                 "relation_id": p.relation_id, "source_refs": p.source_refs,
                 "source_obs_ids": p.source_obs_ids, "error_method": p.error_method,
                 "error_halfwidth": p.error_halfwidth, "error_n": p.error_n,
                 "applicability": p.applicability,
                 "extrapolation_limited": p.extrapolation_limited,
                 "ready_at": p.ready_at.isoformat() if p.ready_at else None,
                 "reasons": p.reasons} for p in rows]
    finally:
        s.close()


# ---------------- 估计订阅与独立交付 ----------------

@router.post("/estimate-subscriptions", status_code=201)
def create_estimate_subscription(body: EstimateSubscriptionIn,
                                 _role: str = Depends(require_role(ADMIN))):
    s = SessionLocal()
    try:
        if s.execute(select(EstimateSubscription).where(
                EstimateSubscription.name == body.name)).scalar_one_or_none():
            raise HTTPException(409, "subscription name exists")
        target_id = None
        if body.target_station:
            target_id = _station_id(s, body.target_station)
        sub = EstimateSubscription(
            name=body.name, region=body.region, target_station_id=target_id,
            accept_estimates=body.accept_estimates, endpoint_url=body.endpoint_url,
            active=body.active,
        )
        s.add(sub)
        s.commit()
        return {"id": sub.id, "name": sub.name, "accept_estimates": sub.accept_estimates}
    finally:
        s.close()


@router.get("/estimate-subscriptions")
def list_estimate_subscriptions(_role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        return [{"id": sub.id, "name": sub.name, "region": sub.region,
                 "target_station_id": sub.target_station_id,
                 "accept_estimates": sub.accept_estimates,
                 "endpoint_url": sub.endpoint_url, "cursor_seq": sub.cursor_seq,
                 "active": sub.active}
                for sub in s.execute(select(EstimateSubscription).order_by(
                    EstimateSubscription.id)).scalars()]
    finally:
        s.close()


@router.get("/estimate-deliveries")
def list_estimate_deliveries(status: str | None = None,
                             _role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        q = select(EstimateDelivery).order_by(EstimateDelivery.publish_seq, EstimateDelivery.id)
        if status:
            q = q.where(EstimateDelivery.status == status)
        return [{"id": d.id, "delivery_key": d.delivery_key,
                 "subscription_id": d.subscription_id, "batch_id": d.batch_id,
                 "publish_seq": d.publish_seq, "status": d.status, "attempts": d.attempts,
                 "acked_at": d.acked_at.isoformat() if d.acked_at else None,
                 "last_error": d.last_error} for d in s.execute(q).scalars()]
    finally:
        s.close()


# ---------------- 恢复对照与停用 ----------------

@router.post("/recovery-review", status_code=201)
def recovery_review(body: RecoveryReviewIn,
                    _role: str = Depends(require_role(OPERATOR, ADMIN))):
    s = SessionLocal()
    try:
        a = s.execute(select(SpatialAnalysis).where(
            SpatialAnalysis.analysis_key == body.analysis_key)).scalar_one_or_none()
        if a is None:
            raise HTTPException(404, "unknown analysis_key")
        review = _handle(svc.review_recovery, s, a.id, body.relation_id, body.threshold_m)
        return _review_out(review)
    finally:
        s.close()


@router.get("/reviews")
def list_reviews(_role: str = Depends(require_role(*ALL_ROLES))):
    s = SessionLocal()
    try:
        return [_review_out(r) for r in
                s.execute(select(RelationReview).order_by(RelationReview.id)).scalars()]
    finally:
        s.close()


def _review_out(r: RelationReview) -> dict:
    return {"id": r.id, "relation_id": r.relation_id, "analysis_id": r.analysis_id,
            "target_station_id": r.target_station_id, "threshold_m": r.threshold_m,
            "max_abs_bias": r.max_abs_bias, "segment_results": r.segment_results,
            "decision": r.decision, "affected_batch_ids": r.affected_batch_ids,
            "affected_receivers": r.affected_receivers,
            "created_at": r.created_at.isoformat()}
