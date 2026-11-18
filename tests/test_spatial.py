"""缺测潮位空间参考估计：纯算法 + 服务层集成测试。

合成序列具有可预知的滞后与尺度：
  ref(t) = A·sin(2πt/12h)，target(t) = 0.3 + 1.2·ref(t+1800s)
即真实滞后 +1800s、尺度 1.2、偏移 0.3，用于核对拟合参数与防泄漏/资格/截止点语义。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import spatial, spatial_services as svc
from app.consumer import app as consumer_app
from app.models import (
    Datum,
    DatumVersion,
    Delivery,
    EstimateDelivery,
    EstimatePoint,
    EstimateSubscription,
    LagCandidate,
    PairedSample,
    ReferenceRelation,
    SeaCondition,
    SeaConditionWindow,
    Station,
    StationDatumHistory,
    StationGapFact,
    StationReference,
    TideSegment,
    ValidationRecord,
)
from app.services import ingest_batch, new_datum_version

T0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
GRID = 600
PERIOD = 12 * 3600
FIT_START = T0
FIT_END = T0 + timedelta(hours=48)
GAP_START = FIT_END
GAP_END = FIT_END + timedelta(hours=6)
STORM_START = T0 + timedelta(hours=42)      # 验证片段落在风暴段
VAL_START = STORM_START - timedelta(hours=3)  # 预先划定的验证期起点
BREAK1 = T0 + timedelta(hours=12)           # 训练片段内断口
BREAK2 = T0 + timedelta(hours=41)           # 训练/验证之间的断口
TRUE_LAG = 1800
TRUE_SCALE = 1.2
TRUE_OFFSET = 0.3


class Spec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def tide(sec: float, amplitude: float) -> float:
    return amplitude * math.sin(2 * math.pi * sec / PERIOD)


def ref_value(t: datetime) -> float:
    amp = 3.0 if t >= STORM_START else 2.0
    return tide((t - T0).total_seconds(), amp)


def target_value(t: datetime) -> float:
    return TRUE_OFFSET + TRUE_SCALE * ref_value(t + timedelta(seconds=TRUE_LAG))


def _add_station(s, code, datum_id, v1, region="two-bays"):
    st = Station(code=code, name=code, region=region, unit="m", positive="up",
                 nominal_interval_sec=GRID, datum_id=datum_id)
    s.add(st)
    s.flush()
    s.add(StationDatumHistory(station_id=st.id, datum_id=datum_id, created_in_version=v1))
    return st


def _feed(s, station_code, value_fn, skip=(), seq_start=1, end=None):
    end = end or GAP_END
    items, seq = [], seq_start
    t = FIT_START
    while t < end:
        if not any(a <= t < b for a, b in skip):
            items.append({"station": station_code, "sample_seq": seq,
                          "observed_at": t, "value": value_fn(t), "unit": "m",
                          "quality_flag": "GOOD"})
        seq += 1
        t += timedelta(seconds=GRID)
    ingest_batch(s, items)


def build_world(s, target_skip=((GAP_START, GAP_END),), multi=False, storm_bias=0.0):
    nd = Datum(code="ND85", name="统一高程")
    s.add(nd)
    s.flush()
    v1 = new_datum_version(s, "bootstrap").id
    tgt = _add_station(s, "ST-NAN", nd.id, v1)
    r1 = _add_station(s, "ST-BEI", nd.id, v1)
    r1b = _add_station(s, "ST-BEI-B", nd.id, v1)
    r2 = _add_station(s, "ST-BEI-2", nd.id, v1)
    r3 = _add_station(s, "ST-BEI-3", nd.id, v1)
    s.flush()

    # 海况目录与时间窗（类别只来自明确资料）
    calm = SeaCondition(code="CALM", name="平稳"); storm = SeaCondition(code="STORM", name="风暴")
    s.add_all([calm, storm])
    s.flush()
    s.add_all([
        SeaConditionWindow(condition_id=calm.id, window_start=FIT_START, window_end=STORM_START),
        SeaConditionWindow(condition_id=storm.id, window_start=STORM_START,
                           window_end=GAP_END + timedelta(hours=1)),
    ])

    # 安装期与质量资格
    for st in (tgt, r1, r1b, r2, r3):
        svc.set_eligibility(s, st.id, datetime(2025, 1, 1, tzinfo=timezone.utc),
                            None, ["GOOD"])

    # 参考关系目录（预先划定）
    svc.add_station_reference(s, tgt.id, r1.id, "SINGLE", "two-bays")
    svc.add_station_reference(s, tgt.id, r1b.id, "SINGLE", "two-bays")
    svc.add_station_reference(s, tgt.id, r2.id, "MULTI", "two-bays")
    svc.add_station_reference(s, tgt.id, r3.id, "MULTI", "two-bays")

    # 观测：目标站在缺口与两个片段断口处无样本；参考站连续
    target_skip = tuple(target_skip) + (
        (BREAK1, BREAK1 + timedelta(minutes=30)),
        (BREAK2, BREAK2 + timedelta(minutes=30)),
    )

    def tval(t):
        v = target_value(t)
        if storm_bias and t >= STORM_START:
            v += storm_bias
        return v

    _feed(s, "ST-NAN", tval, skip=target_skip)
    ref_end = GAP_END + timedelta(hours=2)
    _feed(s, "ST-BEI", ref_value, end=ref_end)
    _feed(s, "ST-BEI-B", ref_value, end=ref_end)
    _feed(s, "ST-BEI-2", ref_value, end=ref_end)
    _feed(s, "ST-BEI-3", ref_value, end=ref_end)
    s.commit()
    return dict(tgt=tgt, r1=r1, r1b=r1b, r2=r2, r3=r3, nd=nd)


def analysis_spec(key="A1", refs=("ST-BEI",), mode="SINGLE",
                  fit_start=FIT_START, fit_end=FIT_END,
                  gap_start=GAP_START, gap_end=GAP_END,
                  cutoff=GAP_END + timedelta(hours=1),
                  conditions=("CALM", "STORM"), n_val=2, validation_start=VAL_START):
    return Spec(analysis_key=key, target_station="ST-NAN", target_datum="ND85",
                ref_stations=list(refs), fit_start=fit_start, fit_end=fit_end,
                gap_start=gap_start, gap_end=gap_end, data_cutoff=cutoff,
                grid_seconds=GRID, lag_min_sec=-1800, lag_max_sec=3600,
                lag_step_sec=GRID, path_mode=mode, allowed_conditions=list(conditions),
                validation_start=validation_start,
                n_val_segments=n_val, min_segment_samples=6, error_method="RMSE*1.96")


def run_one(s, spec):
    a = svc.create_analysis(s, spec)
    svc.run_analysis(s, a.id)
    return s.get(type(a), a.id)


# ---------------- 纯算法 ----------------

def test_ols_recovers_known_offset_and_scale():
    xs = [[0.5 * i] for i in range(20)]
    ys = [TRUE_OFFSET + TRUE_SCALE * row[0] for row in xs]
    beta, cond = spatial.ols(xs, ys)
    assert cond is not None and cond < 5
    assert abs(beta[0] - TRUE_OFFSET) < 1e-9
    assert abs(beta[1] - TRUE_SCALE) < 1e-9


def test_collinear_design_is_rejected_not_masked_by_huge_coefficients():
    rows = [[1.0 * i, 1.0 * i] for i in range(20)]  # 两列完全相同
    y = [0.3 + 1.2 * r[0] for r in rows]
    fit = spatial.fit_relation(rows, y, rows[:5], y[:5])
    assert fit.identifiable is False
    assert fit.condition_number in (None, float("inf")) or fit.condition_number > 30


def test_segment_split_keeps_neighbors_together():
    pairs = [spatial.Pair(t=T0 + timedelta(seconds=GRID * i), target_value=1.0,
                          ref_value=2.0, condition_code="CALM") for i in range(6)]
    pairs += [spatial.Pair(t=T0 + timedelta(hours=5), target_value=1.0,
                           ref_value=2.0, condition_code="CALM")]  # 长断口后
    segs = spatial.split_segments(pairs, GRID, GRID * 2)
    assert len(segs) == 2
    spatial.assign_splits(segs, 1)
    train_t = {p.t for g in segs if g.split == "TRAIN" for p in g.pairs}
    val_t = {p.t for g in segs if g.split == "VAL" for p in g.pairs}
    assert train_t and val_t and not (train_t & val_t)  # 邻近采样不跨两边


def test_metrics_by_condition_lists_required_fields():
    pairs = [spatial.Pair(t=T0, target_value=2.0, ref_value=1.0, condition_code="STORM")]
    m = spatial.metrics_by_condition(pairs, [2.1], [7], GRID)[0]
    assert m.condition_code == "STORM" and m.n == 1 and m.coverage_sec == GRID
    assert abs(m.mean_bias - 0.1) < 1e-12 and abs(m.extreme_error - 0.1) < 1e-12


# ---------------- 服务层：端到端分析 ----------------

def test_single_source_recovers_lag_scale_and_validates_storm(session):
    w = build_world(session)
    a = run_one(session, analysis_spec())
    rels = session.execute(select(ReferenceRelation).where(
        ReferenceRelation.analysis_id == a.id)).scalars().all()
    assert len(rels) == 1
    rel = rels[0]
    assert rel.mode == "SINGLE" and rel.ref_station_id == w["r1"].id
    assert rel.lag_sec == TRUE_LAG                      # 滞后被试算精确恢复
    # 尺度/偏移在格元平滑后仍贴近已知真值（重采样对正弦振幅有轻微衰减）
    assert abs(rel.params["offset"] - TRUE_OFFSET) < 0.05
    assert abs(rel.params["scales"][str(w["r1"].id)] - TRUE_SCALE) < 0.05
    assert set(rel.applicability) == {"CALM", "STORM"}  # 风暴资格由独立验证签发
    storm = rel.applicability["STORM"]
    assert storm["rmse"] < 0.05 and storm["n"] >= 3
    # 误差范围按声明方法计算并保留样本量与适用条件
    assert storm["error_method"] == "RMSE*1.96"
    assert abs(storm["error_halfwidth"] - 1.96 * storm["rmse"]) < 1e-12

    # lag=0（同时刻直接相减）只是显式候选之一，且训练误差不是最优
    cands = session.execute(select(LagCandidate).where(
        LagCandidate.analysis_id == a.id, LagCandidate.ref_station_id == w["r1"].id)).scalars().all()
    lags = {c.lag_sec: c for c in cands}
    assert 0 in lags and TRUE_LAG in lags
    assert lags[TRUE_LAG].train_rmse < lags[0].train_rmse

    # 配对只落在共同支持时刻，且每条配对带真实观测 id 与海况
    ps = session.execute(select(PairedSample).where(
        PairedSample.analysis_id == a.id, PairedSample.lag_sec == TRUE_LAG)).scalars().all()
    assert ps and all(p.target_obs_ids and p.ref_obs_ids and p.condition_code for p in ps)
    # 片段划分：训练/验证不相交，验证片段含风暴
    val = session.execute(select(TideSegment).where(
        TideSegment.analysis_id == a.id, TideSegment.split == "VAL")).scalars().all()
    assert val and any("STORM" in g.condition_codes for g in val)


def test_pairs_exist_only_on_common_observed_support(session):
    w = build_world(session)
    # 删掉参考站一段真实观测：对应时刻不得出现配对（长缺口不被插值）
    from app.models import RawObservation
    gap_a, gap_b = T0 + timedelta(hours=20), T0 + timedelta(hours=21)
    deleted = session.execute(select(RawObservation).where(
        RawObservation.station_id == w["r1"].id,
        RawObservation.observed_at >= gap_a, RawObservation.observed_at < gap_b)).scalars().all()
    for r in deleted:
        session.delete(r)
    session.commit()
    a = run_one(session, analysis_spec(key="A2"))
    ps = session.execute(select(PairedSample).where(
        PairedSample.analysis_id == a.id, PairedSample.lag_sec == TRUE_LAG)).scalars().all()
    # 删去参考站 [gap_a,gap_b) 的真实观测：断口核心区（边缘格元的代表时段之外）
    # 不允许出现配对；长缺口绝不被插值成训练样本
    inner_a, inner_b = gap_a + timedelta(minutes=20), gap_b - timedelta(minutes=20)
    assert all(not (inner_a <= p.ref_time < inner_b) for p in ps)
    assert ps  # 断口之外的共同支持配对仍然存在


def test_calm_only_validation_does_not_issue_storm_qualification(session):
    build_world(session)
    # 拟合资料全部位于平稳期；缺口处于风暴期
    spec = analysis_spec(key="A3", fit_end=STORM_START - timedelta(hours=2),
                         conditions=("CALM",), n_val=1)
    a = run_one(session, spec)
    rels = session.execute(select(ReferenceRelation).where(
        ReferenceRelation.analysis_id == a.id)).scalars().all()
    assert rels and set(rels[0].applicability) == {"CALM"}
    batch = svc.generate_estimates(session, a.id, "B3")
    points = session.execute(select(EstimatePoint).where(
        EstimatePoint.batch_id == batch.id)).scalars().all()
    assert all(p.status == "UNAVAILABLE" for p in points)
    assert any("STORM" in " ".join(p.reasons) for p in points)


def test_positive_lag_waits_for_future_data_then_backfill_is_new_batch(session):
    build_world(session)
    a = run_one(session, analysis_spec())
    # 截止点早于第一个对齐参考时刻：实时只可等待
    early_cutoff = GAP_START + timedelta(seconds=GRID)
    b1 = svc.generate_estimates(session, a.id, "B-wait", data_cutoff=early_cutoff)
    p1 = session.execute(select(EstimatePoint).where(
        EstimatePoint.batch_id == b1.id).order_by(EstimatePoint.grid_time)).scalars().all()
    assert p1[0].status == "WAITING_FUTURE_DATA"
    assert p1[0].ready_at > early_cutoff
    assert all(p.status == "WAITING_FUTURE_DATA" for p in p1)
    # 离线回补：资料到齐后生成新批次，原批次依据不变
    b2 = svc.generate_estimates(session, a.id, "B-backfill",
                                data_cutoff=GAP_END + timedelta(hours=2))
    p2 = session.execute(select(EstimatePoint).where(
        EstimatePoint.batch_id == b2.id)).scalars().all()
    assert all(p.status == "ESTIMATED" for p in p2)
    assert b1.id != b2.id and b2.publish_seq > b1.publish_seq
    old = session.execute(select(EstimatePoint).where(
        EstimatePoint.batch_id == b1.id)).scalars().all()
    assert all(p.status == "WAITING_FUTURE_DATA" for p in old)


def test_unknown_sea_condition_stops_estimation(session):
    build_world(session)
    a = run_one(session, analysis_spec())
    # 删光风暴海况窗：缺口时段类别缺失
    session.execute(SeaConditionWindow.__table__.delete().where(
        SeaConditionWindow.window_start >= STORM_START))
    session.commit()
    batch = svc.generate_estimates(session, a.id, "B-nocond")
    points = session.execute(select(EstimatePoint).where(
        EstimatePoint.batch_id == batch.id)).scalars().all()
    assert all(p.status == "UNAVAILABLE" and p.condition_code is None for p in points)


def test_reference_missing_falls_back_without_recursive_chaining(session):
    w = build_world(session)
    # 主参考站 r1 在整个缺口对齐时刻缺测；备用 r1b 连续
    from app.models import RawObservation
    rows = session.execute(select(RawObservation).where(
        RawObservation.station_id == w["r1"].id,
        RawObservation.observed_at >= GAP_START,
        RawObservation.observed_at < GAP_END + timedelta(hours=2))).scalars().all()
    for r in rows:
        session.delete(r)
    session.commit()
    a = run_one(session, analysis_spec(key="A4", refs=("ST-BEI", "ST-BEI-B")))
    rels = session.execute(select(ReferenceRelation).where(
        ReferenceRelation.analysis_id == a.id)).scalars().all()
    assert {r.ref_station_id for r in rels} == {w["r1"].id, w["r1b"].id}
    batch = svc.generate_estimates(session, a.id, "B4",
                                   data_cutoff=GAP_END + timedelta(hours=2))
    pts = session.execute(select(EstimatePoint).where(
        EstimatePoint.batch_id == batch.id,
        EstimatePoint.status == "ESTIMATED")).scalars().all()
    assert pts and all(p.source_refs[0]["station_id"] == w["r1b"].id for p in pts)
    # 来源全部展开为真实观测 id，绝无估计产品接力
    assert all(p.source_obs_ids and all(isinstance(i, int) for i in p.source_obs_ids) for p in pts)


def test_multi_source_collinear_combo_kept_pending(session):
    w = build_world(session)
    a = run_one(session, analysis_spec(key="A5", refs=("ST-BEI-2", "ST-BEI-3"), mode="MULTI"))
    cands = session.execute(select(LagCandidate).where(
        LagCandidate.analysis_id == a.id)).scalars().all()
    combo_cand = [c for c in cands if c.ref_combo and len(c.ref_combo) == 2]
    assert combo_cand and all(c.status == "REJECTED_COLLINEAR" for c in combo_cand)
    # 单站组合仍可识别并独立验证
    singles = [c for c in cands if c.ref_combo and len(c.ref_combo) == 1 and c.identifiable]
    assert singles
    rels = session.execute(select(ReferenceRelation).where(
        ReferenceRelation.analysis_id == a.id)).scalars().all()
    assert all(len(r.ref_combo) == 1 for r in rels)


def test_gap_fact_always_available_even_without_estimates(session):
    build_world(session)
    a = run_one(session, analysis_spec(key="A6"))
    facts = svc.list_gap_facts(session, "ST-NAN")
    assert len(facts) == 1 and facts[0]["gap_start"] == GAP_START.isoformat()
    # 估计产品与缺测事实分表存放，互不覆盖
    assert session.get(StationGapFact, facts[0]["id"]).resolved is False


def test_recovery_review_disables_relation_and_keeps_original_estimate(session):
    w = build_world(session)
    a = run_one(session, analysis_spec(key="A7"))
    batch = svc.generate_estimates(session, a.id, "B7",
                                   data_cutoff=GAP_END + timedelta(hours=2))
    # 一个显式接收估计的订阅：生成同事务交付任务
    sub = EstimateSubscription(name="est-watch", region="two-bays",
                               accept_estimates=True,
                               endpoint_url="http://consumer:8001/receive-estimates/est-watch")
    session.add(sub)
    session.commit()
    svc._queue_estimate_deliveries(session, batch)
    session.commit()
    original = [(p.grid_time, p.value_m) for p in session.execute(
        select(EstimatePoint).where(EstimatePoint.batch_id == batch.id)).scalars()]

    # 目标站恢复：迟到实测带 +0.5m 系统性偏差（不重新拟合原估计）
    late = []
    t = GAP_START
    seq = 10000
    while t < GAP_END:
        late.append({"station": "ST-NAN", "sample_seq": seq, "observed_at": t,
                     "value": target_value(t) + 0.5, "unit": "m", "quality_flag": "GOOD"})
        seq += 1
        t += timedelta(seconds=GRID)
    ingest_batch(session, late)

    rel = session.execute(select(ReferenceRelation).where(
        ReferenceRelation.analysis_id == a.id)).scalar_one()
    review = svc.review_recovery(session, a.id, rel.id, threshold_m=0.35)
    assert review.decision == "DISABLE" and review.max_abs_bias > 0.35
    assert session.get(ReferenceRelation, rel.id).status == "DISABLED"
    assert batch.id in review.affected_batch_ids
    assert any(r["subscription"] == "est-watch" for r in review.affected_receivers)
    # 原估计数值未被偷偷重算；实测产品通道无任何记录
    after = [(p.grid_time, p.value_m) for p in session.execute(
        select(EstimatePoint).where(EstimatePoint.batch_id == batch.id)).scalars()]
    assert after == original
    assert session.execute(select(Delivery)).scalars().all() == []
    # 停用后新批次不再使用该关系
    b2 = svc.generate_estimates(session, a.id, "B7-after",
                                data_cutoff=GAP_END + timedelta(hours=2))
    assert all(p.status == "UNAVAILABLE" for p in session.execute(
        select(EstimatePoint).where(EstimatePoint.batch_id == b2.id)).scalars())


def test_estimate_delivery_channel_isolated_from_measured(session):
    build_world(session)
    a = run_one(session, analysis_spec(key="A8"))
    session.add_all([
        EstimateSubscription(name="accept", region="two-bays", accept_estimates=True,
                             endpoint_url="http://consumer:8001/receive-estimates/accept"),
        EstimateSubscription(name="refuse", region="two-bays", accept_estimates=False,
                             endpoint_url="http://consumer:8001/receive-estimates/refuse"),
    ])
    session.commit()
    batch = svc.generate_estimates(session, a.id, "B8",
                                   data_cutoff=GAP_END + timedelta(hours=2))
    subs = {x.name: x.id for x in session.execute(select(EstimateSubscription)).scalars()}
    dels = session.execute(select(EstimateDelivery)).scalars().all()
    assert {d.subscription_id for d in dels} == {subs["accept"]}
    assert all(d.publish_seq == batch.publish_seq for d in dels)

    client = TestClient(consumer_app)
    headers = {"X-API-Key": "consumer-key"}
    # 估计混入实测通道 -> 显式拒绝
    r = client.post("/receive/duty", headers=headers, json={
        "delivery_key": "x", "product_kind": "SPATIAL_ESTIMATE", "units": []})
    assert r.status_code == 422
    # 估计独立通道接收，保留独立标识与来源说明
    r = client.post("/receive-estimates/accept", headers=headers, json={
        "delivery_key": "est-1", "product_kind": "SPATIAL_ESTIMATE",
        "source_summary": {"is_measured_product": False,
                           "source_stations": [{"station_code": "ST-BEI", "distinct_obs_count": 3}]},
        "batch": {"id": batch.id, "target_station_id": a.target_station_id,
                  "publish_seq": batch.publish_seq},
        "points": [{"grid_time": GAP_START.isoformat(), "value_m": 1.23,
                    "status": "ESTIMATED", "condition_code": "STORM",
                    "relation_id": None, "source_refs": [{"station_code": "ST-BEI"}],
                    "error_halfwidth": 0.1}]})
    assert r.status_code == 200 and r.json()["ack"] is True
    state = client.get("/estimate-state", headers=headers).json()
    assert len(state) == 1 and state[0]["product_kind"] == "SPATIAL_ESTIMATE"
    inbox = client.get("/estimate-inbox", headers=headers).json()
    assert inbox[0]["source_summary"]["source_stations"][0]["station_code"] == "ST-BEI"


def test_reference_cycle_is_refused(session):
    w = build_world(session)
    # 目录已存在 ST-NAN -> ST-BEI；反向 SINGLE 关系构成互相支持，必须拒绝
    with pytest.raises(svc.ServiceError) as ei:
        svc.add_station_reference(session, w["r1"].id, w["tgt"].id, "SINGLE", "two-bays")
    assert ei.value.status == 409


def test_validation_records_carry_required_metrics(session):
    build_world(session)
    a = run_one(session, analysis_spec(key="A9"))
    recs = session.execute(select(ValidationRecord).where(
        ValidationRecord.analysis_id == a.id)).scalars().all()
    by_cond = {r.condition_code: r for r in recs}
    assert {"CALM", "STORM"} <= set(by_cond)
    # 错误滞后的候选按 RMSE 阈值不合格，且保留明确原因
    rejected = [r for r in recs if not r.qualified]
    assert rejected and all(r.reasons for r in rejected)
    # 被签发关系所依据的验证记录两海况均合格且字段齐全
    qualified = [r for r in recs if r.qualified]
    assert {r.condition_code for r in qualified} == {"CALM", "STORM"}
    for r in qualified:
        assert r.n_samples >= 1 and r.coverage_sec > 0
        assert r.amplitude_min <= r.amplitude_max
        assert r.segment_ids
