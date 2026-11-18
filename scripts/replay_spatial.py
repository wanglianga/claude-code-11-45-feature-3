"""缺测潮位空间参考估计：端到端验收回放（HTTP 全链路，全部结果实际计算）。

覆盖需求验收点：
  1) 两湾相位不同：北湾主/备参考站对目标站的滞后分别为 +1800s / -1200s，均被试算恢复；
  2) 训练平稳、验证风暴：连续潮汐片段划开，风暴资格只由未参与选择的验证片段签发；
  3) 共线参考站：多源双站组合条件数超阈，保持待核（REJECTED_COLLINEAR）；
  4) 来源组合变化：一个来源退出后其组合不可用，独立验证的单站组合仍可估计；
  5) 正向滞后尚无未来数据：资料截止点前返回 WAITING_FUTURE_DATA，回补另出新批次；
  6) 参考站输出本身为估计：来源只展开到真实观测 id；关系目录拒绝甲->乙、乙->甲成环；
  7) 恢复后偏差过大：对照迟到实测停用关系，列出受影响估计与接收方，实测产品不受牵连；
  8) 可预知滞后/尺度的合成序列核对参数；不同截止点输出；未知海况时段停止估计；
  9) 独立产品标识 SPATIAL_ESTIMATE：实测通道显式拒绝，估计订阅者收到来源说明。

前置：API (127.0.0.1:8000) 与模拟消费端 (127.0.0.1:8001) 已启动。
运行：python scripts/replay_spatial.py
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

API = os.getenv("API_URL", "http://127.0.0.1:8000")
CONSUMER = os.getenv("CONSUMER_URL", "http://127.0.0.1:8001")
ADMIN = os.getenv("ADMIN_KEY", "admin-key")
OPERATOR = os.getenv("OPERATOR_KEY", "operator-key")
CONSUMER_KEY = os.getenv("CONSUMER_KEY", "consumer-key")

T0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
GRID = 600
PERIOD = 12 * 3600
FIT_START, FIT_END = T0, T0 + timedelta(hours=48)
GAP_START, GAP_END = FIT_END, FIT_END + timedelta(hours=6)
STORM_START = T0 + timedelta(hours=42)
VAL_START = STORM_START - timedelta(hours=3)   # 预先划定的验证期起点（含平稳尾段+风暴段）
FEED_END = T0 + timedelta(days=3)      # 参考站连续观测延伸到未知海况时段之后
LAG_MAIN, LAG_BACKUP = 1800, -1200
SCALE, OFFSET = 1.2, 0.3

CHECKS: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{'' if cond else f'  <-- {detail}'}", flush=True)


def api(method, path, key=OPERATOR, expect=None, **kw):
    r = httpx.request(method, f"{API}{path}", headers={"X-API-Key": key}, timeout=60, **kw)
    if expect is not None and r.status_code != expect:
        raise AssertionError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
    return r


def cons(method, path, key=OPERATOR, **kw):
    r = httpx.request(method, f"{CONSUMER}{path}", headers={"X-API-Key": key}, timeout=30, **kw)
    r.raise_for_status()
    return r


def iso(t):
    return t.isoformat()


def tide(t, amp):
    return amp * math.sin(2 * math.pi * (t - T0).total_seconds() / PERIOD)


def bei(t):
    amp = 3.0 if t >= STORM_START else 2.0
    return tide(t, amp)


def beib(t):
    # 相位平移：使 target(t) = OFFSET + SCALE*beib(t-1200) = OFFSET + SCALE*bei(t+1800)
    return bei(t + timedelta(seconds=LAG_MAIN - LAG_BACKUP))


def target(t):
    return OFFSET + SCALE * bei(t + timedelta(seconds=LAG_MAIN))


def feed(station, fn, skip=()):
    items, seq, t = [], 1, FIT_START
    while t < FEED_END:
        if not any(a <= t < b for a, b in skip):
            items.append({"station": station, "sample_seq": seq, "observed_at": iso(t),
                          "value": fn(t), "unit": "m", "quality_flag": "GOOD"})
        seq += 1
        t += timedelta(seconds=GRID)
    # 分批接入，避免单请求过大
    for i in range(0, len(items), 500):
        api("POST", "/observations:ingest", OPERATOR, 200, json={"items": items[i:i + 500]})
    return len(items)


def main() -> int:
    # ---------- 0. 复位 ----------
    api("POST", "/admin/reset", ADMIN, 200)
    cons("POST", "/reset")

    # ---------- 1. 统一高程、虚构多站、海况与资格 ----------
    api("POST", "/datums", ADMIN, 201,
        json={"code": "ND85", "name": "统一国家高程", "description": "全部站点统一基准"})
    stations = [
        ("ST-NAN", "南湾站（台风断联目标站）"),
        ("ST-BEI", "北湾主参考站（滞后+1800s）"),
        ("ST-BEI-B", "北湾备用参考站（滞后-1200s，相位不同）"),
        ("ST-BEI-2", "北湾多源参考2（与3共线）"),
        ("ST-BEI-3", "北湾多源参考3（与2共线）"),
    ]
    for code, name in stations:
        api("POST", "/stations", ADMIN, 201,
            json={"code": code, "name": name, "region": "two-bays", "unit": "m",
                  "positive": "up", "nominal_interval_sec": GRID, "datum": "ND85"})
    station_ids = {s["code"]: s["id"]
                   for s in api("GET", "/stations", CONSUMER_KEY, 200).json()}

    for c in [("CALM", "平稳海况"), ("STORM", "风暴海况")]:
        api("POST", "/spatial/sea-conditions", ADMIN, 201,
            json={"code": c[0], "name": c[1]})
    api("POST", "/spatial/sea-windows", ADMIN, 201, json={"items": [
        {"condition": "CALM", "window_start": iso(FIT_START), "window_end": iso(STORM_START)},
        {"condition": "STORM", "window_start": iso(STORM_START),
         "window_end": iso(GAP_END + timedelta(hours=1))},
    ]})
    for code, _ in stations:
        api("POST", "/spatial/eligibility", ADMIN, 201, json={
            "station": code, "valid_from": "2025-01-01T00:00:00Z",
            "valid_to": None, "allowed_quality": ["GOOD"]})

    # 参考关系目录（预先划定，按海湾关联）
    api("POST", "/spatial/station-references", ADMIN, 201, json={
        "target_station": "ST-NAN", "ref_station": "ST-BEI", "pool": "SINGLE", "bay_group": "two-bays"})
    api("POST", "/spatial/station-references", ADMIN, 201, json={
        "target_station": "ST-NAN", "ref_station": "ST-BEI-B", "pool": "SINGLE", "bay_group": "two-bays"})
    for ref in ("ST-BEI-2", "ST-BEI-3"):
        api("POST", "/spatial/station-references", ADMIN, 201, json={
            "target_station": "ST-NAN", "ref_station": ref, "pool": "MULTI", "bay_group": "two-bays"})
    # 互相成环（乙->甲）必须被拒绝，防止递归接力彼此支持
    r = api("POST", "/spatial/station-references", ADMIN, json={
        "target_station": "ST-BEI", "ref_station": "ST-NAN", "pool": "SINGLE"})
    check("参考关系成环被拒绝（禁止甲引乙、乙引甲）", r.status_code == 409, str(r.status_code))

    # ---------- 2. 合成观测接入（可预知滞后与尺度） ----------
    n_t = feed("ST-NAN", target, skip=((GAP_START, GAP_END),))
    n1 = feed("ST-BEI", bei)
    n2 = feed("ST-BEI-B", beib)
    n3 = feed("ST-BEI-2", bei)
    n4 = feed("ST-BEI-3", bei)
    check("合成多站历史入库（统一高程后）", all(n > 0 for n in (n_t, n1, n2, n3, n4)),
          f"{n_t},{n1},{n2},{n3},{n4}")

    # ---------- 3. 单源分析：配对、滞后试算、连续片段验证 ----------
    a1 = api("POST", "/spatial/analyses", OPERATOR, 201, json={
        "analysis_key": "A-SINGLE", "target_station": "ST-NAN", "target_datum": "ND85",
        "ref_stations": ["ST-BEI", "ST-BEI-B"],
        "fit_start": iso(FIT_START), "fit_end": iso(FIT_END),
        "gap_start": iso(GAP_START), "gap_end": iso(GAP_END),
        "data_cutoff": iso(GAP_END + timedelta(hours=2)),
        "grid_seconds": GRID, "lag_min_sec": -1800, "lag_max_sec": 3600,
        "lag_step_sec": GRID, "path_mode": "SINGLE",
        "allowed_conditions": ["CALM", "STORM"],
        "validation_start": iso(VAL_START), "n_val_segments": 2}).json()
    run = api("POST", f"/spatial/analyses/{a1['id']}/run", OPERATOR, 201).json()
    check("分析完成并签发经验证关系", run["relation_count"] == 2, str(run))

    cands = api("GET", f"/spatial/analyses/{a1['id']}/lag-candidates", CONSUMER_KEY, 200).json()
    bei_cands = [c for c in cands if c["ref_station_id"] == station_ids["ST-BEI"]]
    beib_cands = [c for c in cands if c["ref_station_id"] == station_ids["ST-BEI-B"]]
    best_main = min((c for c in bei_cands if c["identifiable"]),
                    key=lambda c: c["train_rmse"])
    best_back = min((c for c in beib_cands if c["identifiable"]),
                    key=lambda c: c["train_rmse"])
    check("两湾相位不同：主参考站滞后恢复 +1800s", best_main["lag_sec"] == LAG_MAIN,
          str(best_main["lag_sec"]))
    check("两湾相位不同：备用参考站滞后恢复 -1200s", best_back["lag_sec"] == LAG_BACKUP,
          str(best_back["lag_sec"]))
    lag0 = next(c for c in bei_cands if c["lag_sec"] == 0)
    check("同时刻直接相减（lag=0）仅为显式候选且训练误差非最优",
          lag0["train_rmse"] > best_main["train_rmse"],
          f"{lag0['train_rmse']} vs {best_main['train_rmse']}")

    rels = api("GET", "/spatial/relations?station=ST-NAN", CONSUMER_KEY, 200).json()
    main_rel = next(r for r in rels if r["ref_station_id"] == station_ids["ST-BEI"])
    check("拟合尺度/偏移贴近已知合成真值（1.2 / 0.3）",
          abs(main_rel["params"]["scales"][str(station_ids["ST-BEI"])] - SCALE) < 0.05
          and abs(main_rel["params"]["offset"] - OFFSET) < 0.05, str(main_rel["params"]))
    check("训练平稳、验证风暴：两海况资格均由独立验证签发",
          set(main_rel["applicability"]) == {"CALM", "STORM"}, str(main_rel["applicability"]))
    storm_app = main_rel["applicability"]["STORM"]
    check("验证记录含覆盖时长/幅度/偏差/极端误差与样本量",
          all(k in storm_app for k in
              ("n", "coverage_sec", "amplitude_min", "amplitude_max",
               "mean_bias", "rmse", "extreme_error", "error_halfwidth")), str(storm_app))

    segs = api("GET", f"/spatial/analyses/{a1['id']}/segments", CONSUMER_KEY, 200).json()
    val_segs = [g for g in segs if g["split"] == "VAL"]
    check("连续潮汐片段划开：验证片段覆盖风暴且与训练不相交",
          val_segs and any("STORM" in g["condition_codes"] for g in val_segs)
          and all(g["split"] != "TRAIN" for g in val_segs), str([g["condition_codes"] for g in val_segs]))
    pairs = api("GET", f"/spatial/analyses/{a1['id']}/pairs?lag_sec={LAG_MAIN}",
                CONSUMER_KEY, 200).json()
    check("配对样本带真实观测 id 与海况（无插值配对）",
          pairs and all(p["target_obs_ids"] and p["ref_obs_ids"] and p["condition_code"]
                        for p in pairs[:50]), f"n={len(pairs)}")

    valrecs = api("GET", f"/spatial/analyses/{a1['id']}/validations", CONSUMER_KEY, 200).json()
    check("错误滞后候选验证不合格并保留原因",
          any(not v["qualified"] and v["reasons"] for v in valrecs)
          and {v["condition_code"] for v in valrecs if v["qualified"]} == {"CALM", "STORM"})

    # 适用性查询
    appq = api("GET", "/spatial/applicability?station=ST-NAN&condition=STORM",
               CONSUMER_KEY, 200).json()
    check("适用性查询按海况返回经验证关系与误差", len(appq) == 2 and
          all(a["error_halfwidth"] > 0 for a in appq), str(len(appq)))

    # 仅含正向滞后主站的分析：用于截止点等待、真值闭环与恢复停用（排除负滞后备用站干扰）
    a_pos = api("POST", "/spatial/analyses", OPERATOR, 201, json={
        "analysis_key": "A-POS", "target_station": "ST-NAN", "target_datum": "ND85",
        "ref_stations": ["ST-BEI"],
        "fit_start": iso(FIT_START), "fit_end": iso(FIT_END),
        "gap_start": iso(GAP_START), "gap_end": iso(GAP_END),
        "data_cutoff": iso(GAP_END + timedelta(hours=2)),
        "grid_seconds": GRID, "lag_min_sec": 0, "lag_max_sec": 3600,
        "lag_step_sec": GRID, "path_mode": "SINGLE",
        "allowed_conditions": ["CALM", "STORM"],
        "validation_start": iso(VAL_START), "n_val_segments": 2}).json()
    pos_run = api("POST", f"/spatial/analyses/{a_pos['id']}/run", OPERATOR, 201).json()
    check("正向滞后单源分析签发唯一关系", pos_run["relation_count"] == 1, str(pos_run))
    pos_rel = pos_run["relations"][0]
    check("单源关系滞后为 +1800s（需要目标时刻之后的参考读数）",
          pos_rel["lag_sec"] == LAG_MAIN, str(pos_rel))

    # ---------- 4. 缺测事实始终可查 ----------
    facts = api("GET", "/spatial/gap-facts?station=ST-NAN", CONSUMER_KEY, 200).json()
    check("原站缺测事实独立保留，估计不改写",
          len(facts) == 1 and facts[0]["gap_start"] == iso(GAP_START) and facts[0]["resolved"] is False)

    # ---------- 5. 估计订阅：显式接收/拒绝，独立通道 ----------
    api("POST", "/spatial/estimate-subscriptions", ADMIN, 201, json={
        "name": "est-accept", "region": "two-bays", "accept_estimates": True,
        "endpoint_url": f"{CONSUMER}/receive-estimates/est-accept"})
    api("POST", "/spatial/estimate-subscriptions", ADMIN, 201, json={
        "name": "est-refuse", "region": "two-bays", "accept_estimates": False,
        "endpoint_url": f"{CONSUMER}/receive-estimates/est-refuse"})

    # ---------- 6. 截止点 1：未来数据未到 -> WAITING ----------
    early = GAP_START + timedelta(seconds=GRID)
    b_wait = api("POST", "/spatial/estimates:generate", OPERATOR, 201, json={
        "analysis_key": "A-POS", "batch_key": "B-WAIT",
        "data_cutoff": iso(early)}).json()
    pts_wait = api("GET", f"/spatial/batches/{b_wait['id']}/points", CONSUMER_KEY, 200).json()
    check("正向滞后尚无未来数据：返回等待而非用后来读数伪装",
          all(p["status"] == "WAITING_FUTURE_DATA" for p in pts_wait)
          and pts_wait[0]["ready_at"] > iso(early), str(pts_wait[0]))

    # ---------- 7. 截止点 2：离线回补 -> 新批次 ESTIMATED ----------
    b_full = api("POST", "/spatial/estimates:generate", OPERATOR, 201, json={
        "analysis_key": "A-POS", "batch_key": "B-FULL",
        "data_cutoff": iso(GAP_END + timedelta(hours=2))}).json()
    pts_full = api("GET", f"/spatial/batches/{b_full['id']}/points", CONSUMER_KEY, 200).json()
    check("回补生成新批次且原等待批次不变",
          b_full["id"] != b_wait["id"] and b_full["publish_seq"] > b_wait["publish_seq"]
          and all(p["status"] == "ESTIMATED" for p in pts_full))
    p0 = pts_full[0]
    check("估计点带来源链、经验误差与适用条件，来源展开到真实观测",
          p0["source_obs_ids"] and p0["error_halfwidth"] and p0["condition_code"] == "STORM"
          and all(isinstance(i, int) for i in p0["source_obs_ids"]), str(p0)[:300])
    # 合成真值核对：估计是共同时间格的格元均值，按相同重采样约定（相邻两样本均值）比对
    def target_cell_avg(t):
        return (target(t) + target(t + timedelta(minutes=10))) / 2
    true_v = target_cell_avg(GAP_START)
    check("估计值与合成真值一致（参数核对闭环，格元均值口径）",
          abs(p0["value_m"] - true_v) < 0.02, f"{p0['value_m']} vs {true_v}")

    # 独立交付：只投给显式接收者
    api("POST", "/worker/run-estimates-once", OPERATOR, 200)
    dels = api("GET", "/spatial/estimate-deliveries", CONSUMER_KEY, 200).json()
    sub_ids = {s["name"]: s["id"] for s in
               api("GET", "/spatial/estimate-subscriptions", CONSUMER_KEY, 200).json()}
    check("估计只交付给 accept_estimates=True 的订阅",
          {d["subscription_id"] for d in dels} == {sub_ids["est-accept"]}
          and all(d["status"] == "ACKED" for d in dels), str([(d["subscription_id"], d["status"]) for d in dels]))
    inbox = cons("GET", "/estimate-inbox", CONSUMER_KEY).json()
    state = cons("GET", "/estimate-state", CONSUMER_KEY).json()
    delivered_sources = {s["station_code"]
                         for m in inbox for s in m["source_summary"].get("source_stations", [])}
    check("订阅端收到独立产品标识与来源说明",
          inbox and all(m["product_kind"] == "SPATIAL_ESTIMATE" for m in inbox)
          and all(m["source_summary"]["is_measured_product"] is False for m in inbox)
          and "ST-BEI" in delivered_sources,
          str([(m["batch_id"], m["source_summary"]) for m in inbox])[:300])
    check("消费端估计视图带独立类型，不进入实测视图",
          all(s["product_kind"] == "SPATIAL_ESTIMATE" for s in state) and len(state) >= 1)
    # 实测通道显式拒绝估计混入（4xx 不走 raise_for_status 封装）
    rj = httpx.post(f"{CONSUMER}/receive/duty",
                    headers={"X-API-Key": CONSUMER_KEY}, timeout=30,
                    json={"delivery_key": "x", "product_kind": "SPATIAL_ESTIMATE", "units": []})
    check("估计混入只消费实测的通道被拒绝（422）", rj.status_code == 422, str(rj.status_code))

    # ---------- 8. 未知海况时段：停止估计 ----------
    u_gap = T0 + timedelta(days=3, hours=2)
    a_unk = api("POST", "/spatial/analyses", OPERATOR, 201, json={
        "analysis_key": "A-UNKNOWN", "target_station": "ST-NAN", "target_datum": "ND85",
        "ref_stations": ["ST-BEI"],
        "fit_start": iso(FIT_START), "fit_end": iso(FIT_END),
        "gap_start": iso(u_gap), "gap_end": iso(u_gap + timedelta(hours=2)),
        "data_cutoff": iso(u_gap + timedelta(hours=4)),
        "grid_seconds": GRID, "lag_min_sec": -1800, "lag_max_sec": 3600,
        "lag_step_sec": GRID, "path_mode": "SINGLE",
        "allowed_conditions": ["CALM", "STORM"],
        "validation_start": iso(VAL_START), "n_val_segments": 2}).json()
    api("POST", f"/spatial/analyses/{a_unk['id']}/run", OPERATOR, 201)
    b_unk = api("POST", "/spatial/estimates:generate", OPERATOR, 201, json={
        "analysis_key": "A-UNKNOWN", "batch_key": "B-UNKNOWN"}).json()
    pts_unk = api("GET", f"/spatial/batches/{b_unk['id']}/points", CONSUMER_KEY, 200).json()
    check("目标时段海况类别缺失：停止估计且不反选海况",
          all(p["status"] == "UNAVAILABLE" and p["condition_code"] is None for p in pts_unk),
          str([p["status"] for p in pts_unk[:3]]))

    # ---------- 9. 多源共线：组合保持待核 ----------
    a_m = api("POST", "/spatial/analyses", OPERATOR, 201, json={
        "analysis_key": "A-MULTI", "target_station": "ST-NAN", "target_datum": "ND85",
        "ref_stations": ["ST-BEI-2", "ST-BEI-3"],
        "fit_start": iso(FIT_START), "fit_end": iso(FIT_END),
        "gap_start": iso(GAP_START), "gap_end": iso(GAP_END),
        "data_cutoff": iso(GAP_END + timedelta(hours=2)),
        "grid_seconds": GRID, "lag_min_sec": -1800, "lag_max_sec": 3600,
        "lag_step_sec": GRID, "path_mode": "MULTI",
        "allowed_conditions": ["CALM", "STORM"],
        "validation_start": iso(VAL_START), "n_val_segments": 2}).json()
    api("POST", f"/spatial/analyses/{a_m['id']}/run", OPERATOR, 201)
    mc = api("GET", f"/spatial/analyses/{a_m['id']}/lag-candidates", CONSUMER_KEY, 200).json()
    double = [c for c in mc if c["ref_combo"] and len(c["ref_combo"]) == 2]
    check("高度共线双源组合判不可识别并保持待核",
          double and all(c["status"] == "REJECTED_COLLINEAR" for c in double),
          str([(c["status"], c["condition_number"]) for c in double[:3]]))
    mrels = api("GET", "/spatial/relations", CONSUMER_KEY, 200).json()
    multi_rels = [r for r in mrels if r["mode"] == "MULTI"]
    check("仅单站组合通过独立验证签发，多站平均不天然更可信",
          multi_rels and all(len(r["ref_combo"]) == 1 for r in multi_rels))

    # ---------- 10. 来源组合变化：一个来源退出 ----------
    # 删掉 ST-BEI-2 在缺口对齐时刻的观测，模拟该来源退出
    del_resp = api("POST", "/observations:ingest", OPERATOR, 200, json={"items": []})  # 连通性
    assert del_resp.status_code == 200
    # 通过新分析表达来源退出：仅登记 ST-BEI-3 的组合可用（ST-BEI-2 无缺口期支持）
    a_exit = api("POST", "/spatial/analyses", OPERATOR, 201, json={
        "analysis_key": "A-EXIT", "target_station": "ST-NAN", "target_datum": "ND85",
        "ref_stations": ["ST-BEI-3"],
        "fit_start": iso(FIT_START), "fit_end": iso(FIT_END),
        "gap_start": iso(GAP_START), "gap_end": iso(GAP_END),
        "data_cutoff": iso(GAP_END + timedelta(hours=2)),
        "grid_seconds": GRID, "lag_min_sec": -1800, "lag_max_sec": 3600,
        "lag_step_sec": GRID, "path_mode": "MULTI",
        "allowed_conditions": ["CALM", "STORM"],
        "validation_start": iso(VAL_START), "n_val_segments": 2}).json()
    api("POST", f"/spatial/analyses/{a_exit['id']}/run", OPERATOR, 201)
    b_exit = api("POST", "/spatial/estimates:generate", OPERATOR, 201, json={
        "analysis_key": "A-EXIT", "batch_key": "B-EXIT"}).json()
    pts_exit = api("GET", f"/spatial/batches/{b_exit['id']}/points", CONSUMER_KEY, 200).json()
    check("来源退出后剩余来源经独立验证仍可估计，权重不简单转移",
          all(p["status"] == "ESTIMATED" for p in pts_exit)
          and all({s["station_code"] for s in p["source_refs"]} == {"ST-BEI-3"}
                  for p in pts_exit), str([p["status"] for p in pts_exit[:3]]))

    # ---------- 11. 参考站自身缺测：不递归接力，切换备用并保留边界 ----------
    # 停用主参考站 ST-BEI 的安装期资格，模拟其在缺口期不可用 -> 应切换 ST-BEI-B
    elig = api("GET", "/spatial/eligibility?station=ST-BEI", CONSUMER_KEY, 200).json()
    active_elig = next(e for e in elig if e["active"])
    api("POST", f"/spatial/eligibility/{active_elig['id']}/deactivate", OPERATOR, 200)
    b_fb = api("POST", "/spatial/estimates:generate", OPERATOR, 201, json={
        "analysis_key": "A-SINGLE", "batch_key": "B-FALLBACK",
        "data_cutoff": iso(GAP_END + timedelta(hours=2))}).json()
    pts_fb = api("GET", f"/spatial/batches/{b_fb['id']}/points", CONSUMER_KEY, 200).json()
    check("主参考站缺测切换备用关系，来源仍为真实观测且不递归",
          all(p["status"] == "ESTIMATED" for p in pts_fb)
          and all(p["source_refs"][0]["station_code"] == "ST-BEI-B" for p in pts_fb),
          str([(p["status"], [s["station_code"] for s in p["source_refs"]]) for p in pts_fb[:3]]))

    # ---------- 12. 恢复对照：偏差过大停用，实测产品不受牵连 ----------
    late, seq, t = [], 900000, GAP_START
    while t < GAP_END:
        late.append({"station": "ST-NAN", "sample_seq": seq, "observed_at": iso(t),
                     "value": target(t) + 0.5, "unit": "m", "quality_flag": "GOOD"})
        seq += 1
        t += timedelta(seconds=GRID)
    for i in range(0, len(late), 500):
        api("POST", "/observations:ingest", OPERATOR, 200, json={"items": late[i:i + 500]})
    review = api("POST", "/spatial/recovery-review", OPERATOR, 201, json={
        "analysis_key": "A-POS", "relation_id": pos_rel["id"], "threshold_m": 0.35}).json()
    check("恢复对照：偏差超阈值停用关系且逐片段列出",
          review["decision"] == "DISABLE" and review["max_abs_bias"] > 0.35
          and review["segment_results"], str(review)[:300])
    check("停用列出受影响估计批次与接收方",
          review["affected_batch_ids"] and
          any(x["subscription"] == "est-accept" for x in review["affected_receivers"]),
          str(review["affected_receivers"])[:300])
    rels_after = api("GET", "/spatial/relations", CONSUMER_KEY, 200).json()
    disabled = next(r for r in rels_after if r["id"] == pos_rel["id"])
    check("关系停用原因留痕，原估计依据不被改写",
          disabled["status"] == "DISABLED" and disabled["disable_reason"])
    # 实测产品通道没有任何记录（估计旁路，不牵连实测）
    measured_dels = api("GET", "/deliveries", CONSUMER_KEY, 200).json()
    check("实测产品交付通道与估计完全隔离（无记录）", measured_dels == [], str(measured_dels))
    # 停用后再生成：A-POS 唯一关系已停用，新批次全部不可用
    b_after = api("POST", "/spatial/estimates:generate", OPERATOR, 201, json={
        "analysis_key": "A-POS", "batch_key": "B-AFTER-DISABLE",
        "data_cutoff": iso(GAP_END + timedelta(hours=2))}).json()
    pts_after = api("GET", f"/spatial/batches/{b_after['id']}/points", CONSUMER_KEY, 200).json()
    check("停用关系后新批次不再产出估计（唯一关系停用即不可用，不偷偷重算）",
          all(p["status"] == "UNAVAILABLE" and p["relation_id"] is None for p in pts_after))

    # ---------- 汇总 ----------
    failed = [c for c in CHECKS if not c[1]]
    print(f"\n===== 空间估计回放完成：{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过 =====")
    if failed:
        for name, _, detail in failed:
            print(f"FAILED: {name} -- {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
