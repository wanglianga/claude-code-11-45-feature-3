"""模拟消费端：独立进程运行的 FastAPI 应用（默认 8001 端口）。

行为契约：
- 按 delivery_key 去重：同一交付标识重复投递只应用一次，再次确认（ack）；
- 应用时按 publish_seq 排序保护：只接受比当前状态更新的产品，
  慢重试的旧版本不会把接收方恢复成旧值；
- 故障标志：offline -> 503；drop_ack -> 应用并落库后返回 500（模拟确认包丢失）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from . import config
from .db import SessionLocal, init_db
from .models import (
    ConsumerEstimateInbox,
    ConsumerEstimateState,
    ConsumerFlag,
    ConsumerInbox,
    ConsumerState,
)
from .security import require_role

app = FastAPI(title="Tide Product Simulated Consumer", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _flag(session, key: str) -> bool:
    row = session.get(ConsumerFlag, key)
    return row is not None and row.value.lower() == "true"


@app.post("/receive/{subscription_name}")
def receive(subscription_name: str, payload: dict,
            _role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        if _flag(session, config.FLAG_OFFLINE):
            raise HTTPException(status_code=503, detail="consumer offline (simulated)")

        key = payload.get("delivery_key")
        if not key:
            raise HTTPException(status_code=400, detail="missing delivery_key")

        # 独立产品隔离：估计产品不得进入只消费实测确认值的通道
        if payload.get("product_kind") == "SPATIAL_ESTIMATE":
            raise HTTPException(
                status_code=422,
                detail="SPATIAL_ESTIMATE rejected by measured-product channel; "
                       "use /receive-estimates/{name}",
            )

        inbox = session.get(ConsumerInbox, key)
        if inbox is not None:
            # 重复投递：不重复应用，直接再次确认
            inbox.receive_count += 1
            session.commit()
            if _flag(session, config.FLAG_DROP_ACK):
                raise HTTPException(status_code=500, detail="ack lost (simulated)")
            return {"ack": True, "delivery_key": key, "deduplicated": True}

        product = payload["product"]
        applied_cells = 0
        skipped_stale = 0
        for u in payload.get("units", []):
            grid_start = datetime.fromisoformat(u["grid_start"])
            if grid_start.tzinfo is None:
                grid_start = grid_start.replace(tzinfo=timezone.utc)
            row = session.execute(
                select(ConsumerState).where(
                    ConsumerState.series_key == product["series_key"],
                    ConsumerState.station_id == u["station_id"],
                    ConsumerState.grid_start == grid_start,
                )
            ).scalar_one_or_none()
            if row is not None and row.publish_seq >= product["publish_seq"]:
                skipped_stale += 1  # 慢重试/乱序到达的旧版本：不覆盖
                continue
            if row is None:
                row = ConsumerState(
                    series_key=product["series_key"],
                    station_id=u["station_id"],
                    grid_start=grid_start,
                )
                session.add(row)
            row.value_m = u["value_m"]
            row.quality_grade = u["quality_grade"]
            row.stage = product["stage"]
            row.product_version = product["version_no"]
            row.publish_seq = product["publish_seq"]
            row.updated_at = datetime.now(timezone.utc)
            applied_cells += 1

        session.add(ConsumerInbox(
            delivery_key=key,
            subscription_name=subscription_name,
            product_id=product["id"],
            publish_seq=product["publish_seq"],
            stage=product["stage"],
            payload_hash=product.get("payload_hash", ""),
            receive_count=1,
            applied=True,
        ))
        session.commit()

        if _flag(session, config.FLAG_DROP_ACK):
            # 确认包丢失：已应用并落库，但应答失败，等待重送（重送会被去重）
            raise HTTPException(status_code=500, detail="ack lost (simulated)")
        return {
            "ack": True,
            "delivery_key": key,
            "deduplicated": False,
            "applied_cells": applied_cells,
            "skipped_stale_cells": skipped_stale,
        }
    finally:
        session.close()


@app.get("/state")
def get_state(series_key: str | None = None,
              _role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        q = select(ConsumerState).order_by(
            ConsumerState.series_key, ConsumerState.station_id, ConsumerState.grid_start)
        if series_key:
            q = q.where(ConsumerState.series_key == series_key)
        rows = session.execute(q).scalars().all()
        return [
            {
                "series_key": r.series_key,
                "station_id": r.station_id,
                "grid_start": r.grid_start.isoformat(),
                "value_m": r.value_m,
                "quality_grade": r.quality_grade,
                "stage": r.stage,
                "product_version": r.product_version,
                "publish_seq": r.publish_seq,
            }
            for r in rows
        ]
    finally:
        session.close()


@app.get("/inbox")
def get_inbox(_role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        rows = session.execute(
            select(ConsumerInbox).order_by(ConsumerInbox.publish_seq)
        ).scalars().all()
        return [
            {
                "delivery_key": r.delivery_key,
                "subscription_name": r.subscription_name,
                "product_id": r.product_id,
                "publish_seq": r.publish_seq,
                "stage": r.stage,
                "receive_count": r.receive_count,
                "applied": r.applied,
                "received_at": r.received_at.isoformat(),
            }
            for r in rows
        ]
    finally:
        session.close()


class FlagIn(BaseModel):
    value: bool


@app.put("/flags/{key}")
def put_flag(key: str, body: FlagIn,
             _role: str = Depends(require_role("operator", "admin"))):
    session = SessionLocal()
    try:
        row = session.get(ConsumerFlag, key)
        if row is None:
            row = ConsumerFlag(key=key)
            session.add(row)
        row.value = "true" if body.value else "false"
        session.commit()
        return {"key": key, "value": row.value}
    finally:
        session.close()


@app.get("/flags")
def get_flags(_role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        return {r.key: r.value for r in session.execute(select(ConsumerFlag)).scalars()}
    finally:
        session.close()


@app.post("/reset")
def reset(_role: str = Depends(require_role("operator", "admin"))):
    session = SessionLocal()
    try:
        for model in (ConsumerEstimateState, ConsumerEstimateInbox,
                      ConsumerState, ConsumerInbox, ConsumerFlag):
            session.query(model).delete()
        session.commit()
        return {"reset": True}
    finally:
        session.close()


# ---------------- 估计产品独立接收端（与实测通道分开） ----------------

@app.post("/receive-estimates/{subscription_name}")
def receive_estimates(subscription_name: str, payload: dict,
                      _role: str = Depends(require_role("consumer", "operator", "admin"))):
    """接收 SPATIAL_ESTIMATE：去重、按发布序号应用、保留独立标识与来源说明。"""
    session = SessionLocal()
    try:
        if _flag(session, config.FLAG_OFFLINE):
            raise HTTPException(status_code=503, detail="consumer offline (simulated)")
        if payload.get("product_kind") != "SPATIAL_ESTIMATE":
            raise HTTPException(
                status_code=422,
                detail="estimate endpoint accepts only product_kind=SPATIAL_ESTIMATE")
        key = payload.get("delivery_key")
        if not key:
            raise HTTPException(status_code=400, detail="missing delivery_key")

        inbox = session.get(ConsumerEstimateInbox, key)
        if inbox is not None:
            inbox.receive_count += 1
            session.commit()
            if _flag(session, config.FLAG_DROP_ACK):
                raise HTTPException(status_code=500, detail="ack lost (simulated)")
            return {"ack": True, "delivery_key": key, "deduplicated": True}

        batch = payload["batch"]
        applied = skipped = 0
        for p in payload.get("points", []):
            grid_time = datetime.fromisoformat(p["grid_time"])
            if grid_time.tzinfo is None:
                grid_time = grid_time.replace(tzinfo=timezone.utc)
            row = session.execute(
                select(ConsumerEstimateState).where(
                    ConsumerEstimateState.target_station_id == batch["target_station_id"],
                    ConsumerEstimateState.grid_time == grid_time,
                )
            ).scalar_one_or_none()
            if row is not None and row.publish_seq >= batch["publish_seq"]:
                skipped += 1  # 旧批次不覆盖新批次
                continue
            if row is None:
                row = ConsumerEstimateState(
                    target_station_id=batch["target_station_id"], grid_time=grid_time)
                session.add(row)
            row.batch_id = batch["id"]
            row.product_kind = payload["product_kind"]
            row.value_m = p["value_m"]
            row.status = p["status"]
            row.condition_code = p["condition_code"]
            row.error_halfwidth = p["error_halfwidth"]
            row.relation_id = p["relation_id"]
            row.source_refs = p["source_refs"]
            row.publish_seq = batch["publish_seq"]
            row.updated_at = datetime.now(timezone.utc)
            applied += 1

        session.add(ConsumerEstimateInbox(
            delivery_key=key,
            subscription_name=subscription_name,
            batch_id=batch["id"],
            product_kind=payload["product_kind"],
            publish_seq=batch["publish_seq"],
            source_summary=payload.get("source_summary", {}),
            receive_count=1,
            applied=True,
        ))
        session.commit()

        if _flag(session, config.FLAG_DROP_ACK):
            raise HTTPException(status_code=500, detail="ack lost (simulated)")
        return {"ack": True, "delivery_key": key, "deduplicated": False,
                "applied_points": applied, "skipped_stale_points": skipped,
                "product_kind": payload["product_kind"]}
    finally:
        session.close()


@app.get("/estimate-inbox")
def get_estimate_inbox(_role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        rows = session.execute(
            select(ConsumerEstimateInbox).order_by(ConsumerEstimateInbox.publish_seq)
        ).scalars().all()
        return [
            {
                "delivery_key": r.delivery_key, "subscription_name": r.subscription_name,
                "batch_id": r.batch_id, "product_kind": r.product_kind,
                "publish_seq": r.publish_seq, "source_summary": r.source_summary,
                "receive_count": r.receive_count, "applied": r.applied,
                "received_at": r.received_at.isoformat(),
            }
            for r in rows
        ]
    finally:
        session.close()


@app.get("/estimate-state")
def get_estimate_state(target_station_id: int | None = None,
                       _role: str = Depends(require_role("consumer", "operator", "admin"))):
    session = SessionLocal()
    try:
        q = select(ConsumerEstimateState).order_by(
            ConsumerEstimateState.target_station_id, ConsumerEstimateState.grid_time)
        if target_station_id is not None:
            q = q.where(ConsumerEstimateState.target_station_id == target_station_id)
        return [
            {
                "target_station_id": r.target_station_id,
                "grid_time": r.grid_time.isoformat(), "batch_id": r.batch_id,
                "product_kind": r.product_kind, "value_m": r.value_m, "status": r.status,
                "condition_code": r.condition_code, "error_halfwidth": r.error_halfwidth,
                "relation_id": r.relation_id, "source_refs": r.source_refs,
                "publish_seq": r.publish_seq,
            }
            for r in session.execute(q).scalars()
        ]
    finally:
        session.close()
