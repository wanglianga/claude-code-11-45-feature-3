"""缺测潮位空间参考估计：纯计算部分（不接触数据库）。

设计约束（对应需求逐条落地）：
- 对齐按显式候选滞后移动参考站窗口，目标时刻 t 取参考站 t+lag；
  绝不允许“同一时刻最近读数直接相减”（lag=0 也必须作为显式候选参与验证）。
- 连续潮汐片段按配对支持的中断切分；邻近采样属于同一段，不允许随机打散到
  训练/验证两边泄漏同一段变化。
- 单源拟合带截距（实际偏移）与尺度：y = offset + scale * x；
  多源联合拟合：y = offset + Σ scale_k * x_k，先查设计矩阵可识别性，
  高度共线、条件数超阈的组合保持待核（REJECTED_COLLINEAR），
  不让求解器产生的巨大相反系数掩盖问题。
- 参数选择只看训练/留出验证；验证误差在未参与选择的连续验证片段上计算。
- 误差范围按声明方法（默认 RMSE×1.96）计算并保留样本量与适用条件，
  它是经验表现，不保证未出现过的极端天气必落其中。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# 设计矩阵条件数阈值：超过即判共线不可识别（纯 Python 求解，不引入 numpy）
COLLINEAR_COND_LIMIT = 30.0
# 系数相对量级告警阈值（仅记录，资格仍以条件数与验证为准）
HUGE_COEF_REL = 1e3


@dataclass(frozen=True)
class Pair:
    """一条对齐配对：目标时刻 t 的水位 y，参考时刻 t+lag 的水位 x。"""

    t: datetime
    target_value: float
    ref_value: float
    condition_code: str
    target_obs_ids: tuple = ()
    ref_obs_ids: tuple = ()


@dataclass
class Segment:
    index: int
    start: datetime
    end: datetime
    pairs: list[Pair] = field(default_factory=list)
    condition_codes: list[str] = field(default_factory=list)
    split: str = "TRAIN"  # TRAIN / VAL，划定后不变
    db_id: int | None = None  # 落库后的 TideSegment.id


@dataclass
class FitResult:
    offset: float | None = None
    scales: tuple[float, ...] = ()          # 单源 1 个；多源按参考站顺序
    train_rmse: float | None = None
    val_rmse: float | None = None
    identifiable: bool = True
    condition_number: float | None = None
    note: str = ""


# ---------------- 连续潮汐片段 ----------------

def split_segments(
    pairs: list[Pair],
    grid_seconds: int,
    max_break_sec: float,
) -> list[Segment]:
    """按配对支持的连续性切片段：相邻目标时刻间隔超过 max_break_sec 即断开。

    长缺口本身不会被插值成配对，配对序列里出现的时间跳跃就是支持中断边界。
    """
    if not pairs:
        return []
    ordered = sorted(pairs, key=lambda p: p.t)
    segments: list[Segment] = []
    cur: list[Pair] = [ordered[0]]
    for prev, nxt in zip(ordered, ordered[1:]):
        gap = (nxt.t - prev.t).total_seconds()
        if gap > max_break_sec + grid_seconds * 0.5:
            segments.append(cur)
            cur = [nxt]
        else:
            cur.append(nxt)
    segments.append(cur)

    out: list[Segment] = []
    for i, grp in enumerate(segments):
        codes = sorted({p.condition_code for p in grp})
        out.append(Segment(
            index=i,
            start=grp[0].t,
            end=grp[-1].t,
            pairs=grp,
            condition_codes=codes,
        ))
    return out


def assign_splits(segments: list[Segment], n_val_segments: int) -> list[Segment]:
    """预先划定拟合/验证片段：末尾 n_val_segments 个连续片段做验证。

    时间靠后的片段作为留出（贴近“先平稳训练、后风暴验证”的真实顺序），
    片段内所有配对同属一个 split，杜绝邻近采样跨边泄漏。
    """
    n_val_segments = max(1, min(n_val_segments, len(segments)))
    val_from = len(segments) - n_val_segments
    for seg in segments:
        seg.split = "VAL" if seg.index >= val_from else "TRAIN"
    return segments


def split_train_val(segments: list[Segment], validation_start: datetime) -> list[Segment]:
    """按预先划定的验证期时间边界划分 TRAIN/VAL。

    - 支持中断形成的片段边界保持不变（长缺口不会被插值连通）；
    - 跨过 validation_start 的连续片段在该时刻做一次干净的时间切割，
      相邻采样各居一侧，绝不随机打散；
    - 结束时刻早于边界的片段为 TRAIN，其余为 VAL。
    """
    out: list[Segment] = []
    for seg in segments:
        if seg.start < validation_start < seg.end:
            left = [p for p in seg.pairs if p.t < validation_start]
            right = [p for p in seg.pairs if p.t >= validation_start]
            if left:
                out.append(Segment(
                    index=len(out), start=left[0].t, end=left[-1].t, pairs=left,
                    condition_codes=sorted({p.condition_code for p in left}), split="TRAIN"))
            if right:
                out.append(Segment(
                    index=len(out), start=right[0].t, end=right[-1].t, pairs=right,
                    condition_codes=sorted({p.condition_code for p in right}), split="VAL"))
            continue
        seg.index = len(out)
        seg.split = "VAL" if seg.start >= validation_start else "TRAIN"
        out.append(seg)
    return out


# ---------------- 线性代数（纯 Python，带可识别性诊断） ----------------

def _mat_transpose(a: list[list[float]]) -> list[list[float]]:
    return [list(row) for row in zip(*a)]


def _mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    bt = _mat_transpose(b)
    return [[sum(x * y for x, y in zip(r, c)) for c in bt] for r in a]


def _mat_inv(a: list[list[float]]) -> list[list[float]] | None:
    """高斯-约旦求逆；奇异返回 None。"""
    n = len(a)
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        piv = m[col][col]
        m[col] = [v / piv for v in m[col]]
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col]
            if factor:
                m[r] = [rv - factor * cv for rv, cv in zip(m[r], m[col])]
    return [row[n:] for row in m]


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def condition_number(a: list[list[float]]) -> float | None:
    """设计矩阵（不含截距列）的条件数：列归一化后 (AᵀA) 最大/最小特征值开方。

    用幂迭代 + 逆迭代估计极端特征值，避免引入 numpy。
    """
    if not a:
        return None
    cols = _mat_transpose(a)
    scales = [_norm(c) or 1.0 for c in cols]
    normed = [[row[j] / scales[j] for j in range(len(scales))] for row in a]
    gram = _mat_mul(_mat_transpose(normed), normed)
    k = len(gram)
    eigmax = _power_eigenvalue(gram)
    inv = _mat_inv(gram)
    if inv is None or eigmax is None:
        return None
    eigmax_inv = _power_eigenvalue(inv)  # = 1/最小特征值
    if not eigmax_inv or eigmax_inv <= 0:
        return None
    eigmin = 1.0 / eigmax_inv
    if eigmin <= 1e-12:
        return float("inf")
    return math.sqrt(eigmax / eigmin)


def _power_eigenvalue(m: list[list[float]], iters: int = 200) -> float | None:
    n = len(m)
    v = [1.0 / math.sqrt(n)] * n
    lam = 0.0
    for _ in range(iters):
        w = [sum(m[i][j] * v[j] for j in range(n)) for i in range(n)]
        nrm = _norm(w)
        if nrm < 1e-14:
            return None
        v = [x / nrm for x in w]
        lam_new = sum(v[i] * sum(m[i][j] * v[j] for j in range(n)) for i in range(n))
        if abs(lam_new - lam) < 1e-12:
            return lam_new
        lam = lam_new
    return lam


def ols(x_rows: list[list[float]], y: list[float]) -> tuple[list[float] | None, float | None]:
    """带截距 OLS：β=(XᵀX)⁻¹Xᵀy，X 第一列为常数 1。返回 (β, 条件数)。"""
    if len(x_rows) <= len(x_rows[0]):
        return None, None  # 自由度不足，不可识别
    design = [[1.0, *row] for row in x_rows]
    xt = _mat_transpose(design)
    xty = [sum(xt[k][i] * y[i] for i in range(len(y))) for k in range(len(xt))]
    xtx = _mat_mul(xt, design)
    # 条件数基于不含截距的原始设计列
    cond = condition_number(x_rows)
    inv = _mat_inv(xtx)
    if inv is None:
        return None, cond
    beta = [sum(inv[r][c] * xty[c] for c in range(len(xty))) for r in range(len(inv))]
    return beta, cond


def rmse(y_true: list[float], y_pred: list[float]) -> float | None:
    if not y_true:
        return None
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(y_true, y_pred)) / len(y_true))


def fit_relation(
    train_rows: list[list[float]],
    train_y: list[float],
    val_rows: list[list[float]],
    val_y: list[float],
    cond_limit: float = COLLINEAR_COND_LIMIT,
) -> FitResult:
    """拟合并在留出片段上验证。多源时 x_rows 每行是各参考站对齐值。"""
    if len(train_rows) < len(train_rows[0]) + 2:
        return FitResult(identifiable=False, note="insufficient train samples for degrees of freedom")
    beta, cond = ols(train_rows, train_y)
    if beta is None:
        return FitResult(identifiable=False, condition_number=cond,
                         note="design matrix singular")
    if cond is not None and cond > cond_limit:
        return FitResult(identifiable=False, condition_number=cond,
                         note=f"condition number {cond:.1f} > {cond_limit}; collinear sources kept PENDING")
    offset, scales = beta[0], tuple(beta[1:])
    # 巨大相反系数是共线的典型症状，记录但不单独否决（条件数已把关）
    if any(abs(sc) > HUGE_COEF_REL for sc in scales):
        note = f"large coefficient(s) {scales}; verify against condition number"
    else:
        note = ""
    train_pred = [predict(offset, scales, row) for row in train_rows]
    val_pred = [predict(offset, scales, row) for row in val_rows]
    return FitResult(
        offset=offset,
        scales=scales,
        train_rmse=rmse(train_y, train_pred),
        val_rmse=rmse(val_y, val_pred),
        identifiable=True,
        condition_number=cond,
        note=note,
    )


def predict(offset: float, scales: tuple[float, ...], x_row: list[float]) -> float:
    return offset + sum(sc * x for sc, x in zip(scales, x_row))


# ---------------- 分海况验证指标 ----------------

@dataclass
class ConditionMetrics:
    condition_code: str
    n: int
    coverage_sec: float
    amplitude_min: float
    amplitude_max: float
    mean_bias: float
    mae: float
    rmse: float
    extreme_error: float
    segment_ids: list[int]


def metrics_by_condition(
    pairs: list[Pair],
    preds: list[float],
    segment_ids: list[int],
    grid_seconds: int,
) -> list[ConditionMetrics]:
    """按海况分别统计：覆盖时长、幅度范围、平均偏差、MAE、RMSE、极端水位误差。"""
    groups: dict[str, list[tuple[Pair, float, int]]] = {}
    for p, pred, seg_id in zip(pairs, preds, segment_ids):
        groups.setdefault(p.condition_code, []).append((p, pred, seg_id))

    out: list[ConditionMetrics] = []
    for code in sorted(groups):
        grp = groups[code]
        ys = [g[0].target_value for g in grp]
        errs = [g[1] - g[0].target_value for g in grp]  # 预测 - 实测
        # 极端水位误差：在实测最高水位样本处的绝对误差
        hi_idx = max(range(len(grp)), key=lambda i: ys[i])
        out.append(ConditionMetrics(
            condition_code=code,
            n=len(grp),
            coverage_sec=len(grp) * grid_seconds,
            amplitude_min=min(ys),
            amplitude_max=max(ys),
            mean_bias=sum(errs) / len(errs),
            mae=sum(abs(e) for e in errs) / len(errs),
            rmse=math.sqrt(sum(e * e for e in errs) / len(errs)),
            extreme_error=abs(errs[hi_idx]),
            segment_ids=sorted({g[2] for g in grp}),
        ))
    return out


def error_halfwidth(method: str, condition_rmse: float, n: int) -> float:
    """经验误差半宽。默认 RMSE×1.96（近似 95%），样本量随结果保留。"""
    if method.upper().startswith("RMSE*1.96"):
        return 1.96 * condition_rmse
    if method.upper().startswith("RMSE"):
        return condition_rmse
    return 1.96 * condition_rmse


def within_amplitude(value: float, amp_min: float, amp_max: float, margin: float = 0.0) -> bool:
    return (amp_min - margin) <= value <= (amp_max + margin)


def lag_window(lag_sec: int, grid_seconds: int) -> timedelta:
    return timedelta(seconds=abs(lag_sec) + grid_seconds)
