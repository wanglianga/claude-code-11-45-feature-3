"""交付工作进程：把待交付任务推送到订阅者端点。

崩溃恢复语义：
- 产品、清单与待交付任务已在同一事务提交，工作进程只读已提交数据；
- 发送前崩溃：任务保持 PENDING，下次运行重新发送；
- 发送后、落库前崩溃：任务仍为 PENDING，重送时保留同一 delivery_key，
  消费端按标识去重后再次确认，不会重复应用；
- 每个订阅者独立确认、独立重试，已确认的消费者无需重收。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from . import config
from .db import SessionLocal
from .models import (
    Delivery,
    EstimateBatch,
    EstimateDelivery,
    EstimatePoint,
    EstimateSubscription,
    Product,
    ProductUnit,
    Station,
    Subscription,
)


def build_payload(session, delivery: Delivery) -> dict:
    product = session.get(Product, delivery.product_id)
    sub = session.get(Subscription, delivery.subscription_id)
    units = session.execute(
        select(ProductUnit).where(ProductUnit.product_id == product.id).order_by(
            ProductUnit.station_id, ProductUnit.grid_start)
    ).scalars().all()
    station_codes = {st.id: st.code for st in session.execute(select(Station)).scalars()}
    return {
        "delivery_key": delivery.delivery_key,
        "subscription": sub.name,
        "product": {
            "id": product.id,
            "series_key": product.series_key,
            "region": product.region,
            "stage": product.stage,
            "version_no": product.version_no,
            "publish_seq": product.publish_seq,
            "window_start": product.window_start.isoformat(),
            "window_end": product.window_end.isoformat(),
            "grid_seconds": product.grid_seconds,
            "target_datum_id": product.target_datum_id,
            "datum_version_id": product.datum_version_id,
            "payload_hash": product.payload_hash,
            "supersedes_id": product.supersedes_id,
        },
        "units": [
            {
                "station_id": u.station_id,
                "station_code": station_codes.get(u.station_id),
                "grid_start": u.grid_start.isoformat(),
                "grid_seconds": u.grid_seconds,
                "value_m": u.value_m,
                "quality_grade": u.quality_grade,
                "coverage_sec": u.coverage_sec,
                "coverage_ratio": u.coverage_ratio,
                "source_sample_ids": u.source_sample_ids,
                "conversion_path": u.conversion_path,
                "conversion_status": u.conversion_status,
                "quality_reasons": u.quality_reasons,
            }
            for u in units
        ],
    }


def deliver_one(session, delivery: Delivery, client: httpx.Client | None = None) -> str:
    """发送单条交付。返回 ACKED / FAILED。崩溃安全：先发送，后落库。"""
    sub = session.get(Subscription, delivery.subscription_id)
    payload = build_payload(session, delivery)
    delivery.attempts += 1
    delivery.last_attempt_at = datetime.now(timezone.utc)
    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True
    try:
        resp = client.post(
            sub.endpoint_url,
            json=payload,
            headers={"X-API-Key": config.CONSUMER_KEY},
        )
        if resp.status_code == 200 and resp.json().get("ack") is True:
            delivery.status = "ACKED"
            delivery.acked_at = datetime.now(timezone.utc)
            delivery.last_error = None
            if delivery.publish_seq > sub.cursor_seq:
                sub.cursor_seq = delivery.publish_seq
        else:
            delivery.status = "FAILED"
            delivery.last_error = f"status={resp.status_code} body={resp.text[:300]}"
    except Exception as exc:  # noqa: BLE001  网络错误一律记为失败待重试
        delivery.status = "FAILED"
        delivery.last_error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if close_client:
            client.close()
    return delivery.status


def run_once(limit: int = 100) -> dict:
    """处理一轮待交付任务（PENDING 与可重试的 FAILED）。"""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(Delivery)
            .where(
                (Delivery.status == "PENDING")
                | ((Delivery.status == "FAILED") & (Delivery.attempts < config.WORKER_MAX_ATTEMPTS))
            )
            .order_by(Delivery.publish_seq, Delivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars().all()
        stats = {"ACKED": 0, "FAILED": 0, "processed": 0}
        client = httpx.Client(timeout=10.0)
        try:
            for d in rows:
                stats[deliver_one(session, d, client)] += 1
                stats["processed"] += 1
        finally:
            client.close()
        session.commit()
        return stats
    finally:
        session.close()


def main() -> None:
    interval = float(config.WORKER_RETRY_BACKOFF_SEC)
    while True:
        try:
            stats = run_once()
            if stats["processed"]:
                print(f"[worker] {json.dumps(stats)}", flush=True)
            estats = run_estimate_once()
            if estats["processed"]:
                print(f"[worker:estimate] {json.dumps(estats)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] error: {exc}", flush=True)
        time.sleep(interval)


# ---------------- 估计产品独立交付通道 ----------------

def build_estimate_payload(session, delivery: EstimateDelivery) -> dict:
    """构造估计交付载荷：独立产品标识 + 逐点来源说明（展开到真实观测）。"""
    batch = session.get(EstimateBatch, delivery.batch_id)
    sub = session.get(EstimateSubscription, delivery.subscription_id)
    points = session.execute(
        select(EstimatePoint).where(EstimatePoint.batch_id == batch.id)
        .order_by(EstimatePoint.grid_time)
    ).scalars().all()
    station_codes = {st.id: st.code for st in session.execute(select(Station)).scalars()}
    # 来源汇总：展开到真实观测站，重复物理记录去重后计数
    source_stations: dict[int, set[int]] = {}
    for p in points:
        for ref in p.source_refs:
            source_stations.setdefault(ref["station_id"], set()).update(ref.get("obs_ids", []))
    source_summary = {
        "product_kind": batch.product_kind,
        "is_measured_product": False,
        "source_stations": [
            {"station_id": sid, "station_code": station_codes.get(sid),
             "distinct_obs_count": len(oids)}
            for sid, oids in sorted(source_stations.items())
        ],
    }
    return {
        "delivery_key": delivery.delivery_key,
        "subscription": sub.name,
        "product_kind": batch.product_kind,
        "batch": {
            "id": batch.id,
            "batch_key": batch.batch_key,
            "analysis_id": batch.analysis_id,
            "target_station_id": batch.target_station_id,
            "target_station_code": station_codes.get(batch.target_station_id),
            "data_cutoff": batch.data_cutoff.isoformat(),
            "gap_start": batch.gap_start.isoformat(),
            "gap_end": batch.gap_end.isoformat(),
            "status": batch.status,
            "publish_seq": batch.publish_seq,
            "mode": batch.mode,
        },
        "source_summary": source_summary,
        "points": [
            {
                "grid_time": p.grid_time.isoformat(),
                "value_m": p.value_m,
                "status": p.status,
                "condition_code": p.condition_code,
                "relation_id": p.relation_id,
                "source_refs": p.source_refs,
                "source_obs_ids": p.source_obs_ids,
                "error_method": p.error_method,
                "error_halfwidth": p.error_halfwidth,
                "error_n": p.error_n,
                "applicability": p.applicability,
                "extrapolation_limited": p.extrapolation_limited,
                "ready_at": p.ready_at.isoformat() if p.ready_at else None,
                "reasons": p.reasons,
            }
            for p in points
        ],
    }


def deliver_estimate_one(session, delivery: EstimateDelivery,
                         client: httpx.Client | None = None) -> str:
    """发送单条估计交付。与实测交付完全独立，崩溃语义一致（先发送后落库）。"""
    sub = session.get(EstimateSubscription, delivery.subscription_id)
    payload = build_estimate_payload(session, delivery)
    delivery.attempts += 1
    delivery.last_attempt_at = datetime.now(timezone.utc)
    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True
    try:
        resp = client.post(
            sub.endpoint_url,
            json=payload,
            headers={"X-API-Key": config.CONSUMER_KEY},
        )
        if resp.status_code == 200 and resp.json().get("ack") is True:
            delivery.status = "ACKED"
            delivery.acked_at = datetime.now(timezone.utc)
            delivery.last_error = None
            if delivery.publish_seq > sub.cursor_seq:
                sub.cursor_seq = delivery.publish_seq
        else:
            delivery.status = "FAILED"
            delivery.last_error = f"status={resp.status_code} body={resp.text[:300]}"
    except Exception as exc:  # noqa: BLE001
        delivery.status = "FAILED"
        delivery.last_error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if close_client:
            client.close()
    return delivery.status


def run_estimate_once(limit: int = 100) -> dict:
    """处理一轮估计产品待交付任务（PENDING 与可重试 FAILED）。"""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(EstimateDelivery)
            .where(
                (EstimateDelivery.status == "PENDING")
                | ((EstimateDelivery.status == "FAILED")
                   & (EstimateDelivery.attempts < config.WORKER_MAX_ATTEMPTS))
            )
            .order_by(EstimateDelivery.publish_seq, EstimateDelivery.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars().all()
        stats = {"ACKED": 0, "FAILED": 0, "processed": 0}
        client = httpx.Client(timeout=10.0)
        try:
            for d in rows:
                stats[deliver_estimate_one(session, d, client)] += 1
                stats["processed"] += 1
        finally:
            client.close()
        session.commit()
        return stats
    finally:
        session.close()


if __name__ == "__main__":
    main()
