# 潮位观测数据产品发布与订阅服务

面向沿海防潮指挥部门的潮位数据产品服务，包含两个相互独立的产品域：

1. **实测产品域**：把不同港区/海湾验潮站的读数统一到同一时空口径
   （统一目标高程基准 + 共同时间格 + 质量等级），以**分阶段发布**（初报 → 确认产品）方式
   向各消费系统交付，替代邮件交换表格、各自猜测基准与缺测含义的现有做法。
2. **缺测潮位空间参考估计域（本次新增）**：南湾站台风断联时，基于北湾等仍有连续读数的
   参考站，按经过验证的跨站滞后/尺度关系生成**带适用范围与验证误差的估计序列**。
   估计是独立产品类型 `SPATIAL_ESTIMATE`，不把邻站读数改名成本站实测，也不生成台风预报或
   防灾行动建议；原站缺测事实始终原样可查。

实测域能力：
- 站点目录：观测位置、垂直基准（站点零点）、读数单位、正方向、设备状态
- 产品生成：原始观测 → 逐样本基准换算（按生效区间）→ 共同时间格重采样 → 质量等级
- 分阶段发布：`PRELIMINARY` 初报供值守研判，`CONFIRMED` 确认产品才进入正式模型
- 订阅交付：按区域/阶段订阅，产品+清单+待交付任务同事务提交，消费者分别确认、独立重试

空间估计域能力：
- 统一高程后的虚构多站历史、海况分类、站点可参考关系、预先划定的拟合期与验证期
- 配对样本、候选滞后参数、连续片段验证、估计输出分别落库，全部结果实际计算
- 独立发布/订阅通道：订阅者显式声明能否接收，不能混入只消费实测确认值的通道

## 技术栈

Python 3.12 · FastAPI · PostgreSQL（SQLAlchemy 2）· 单容器即可运行（API + 模拟消费端 + 交付 worker）

## 快速开始（Docker Compose，一键）

```bash
docker compose up -d --build db api consumer worker
# 验收回放（实测产品 44 项 + 空间估计 34 项断言，退出码非 0 即失败）
docker compose run --rm replay
docker compose run --rm replay-spatial
```

- API 与 Swagger UI：http://127.0.0.1:8000/docs （OpenAPI 3.x：/openapi.json，含 /spatial 全部接口）
- 模拟消费端：http://127.0.0.1:8001/docs

## 本地开发启动

```bash
# 需要 PostgreSQL（开发库 ccdev/ccdev@127.0.0.1:5432）
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 终端 1：API（建表在启动时自动完成）
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 终端 2：模拟消费端
.venv/bin/python -m uvicorn app.consumer:app --host 127.0.0.1 --port 8001
# 终端 3：交付工作进程（同时轮询实测与估计两条交付队列，崩溃恢复语义见下）
.venv/bin/python -m app.worker
```

## 测试账号（演示用 API Key，请求头 `X-API-Key`）

| Key | 角色 | 权限 |
| --- | --- | --- |
| `admin-key` | admin | 基准/站点/订阅管理、/admin/reset |
| `operator-key` | operator | 观测接入、产品作业、确认签发、交付重试 |
| `consumer-key` | consumer | 读取产品、单元、交付记录；消费端接收交付 |

> 仅用于隔离开发与验收，可通过环境变量 `ADMIN_KEY/OPERATOR_KEY/CONSUMER_KEY` 覆盖。

## 验证方式（本题验收）

```bash
# 1) 单元与集成测试（36 项：基准图/重采样/接入去重/版本快照/同事务交付 + 空间估计 16 项）
.venv/bin/python -m pytest tests/ -q

# 2) 端到端验收回放（需 API + 消费端已启动）
.venv/bin/python scripts/replay.py            # 实测产品 44 项断言
.venv/bin/python scripts/replay_spatial.py    # 空间估计 34 项断言
```

实测回放 `scripts/replay.py` 会真实导入样例数据并核对：多基准站点换算值、重复报文去重、
迟到补传与质量更正生成的新版本、发布中途基准配置变更的版本隔离、路径冲突保留、
断档/插值/待核标记、回执丢失重送去重、消费者离线恢复、慢重试不恢复旧值、
订阅者分别确认与失败者独立重试。

空间估计回放 `scripts/replay_spatial.py` 用**可预知滞后（+1800s / −1200s）与尺度（1.2、
偏移 0.3）的合成潮汐序列**核对参数恢复，并覆盖：两湾相位不同、训练平稳而验证风暴、
共线参考站组合保持待核、来源组合变化、正向滞后尚无未来数据时返回等待、参考站自身缺测只
切换到带真实观测的备用关系（不递归接力）、恢复后偏差过大停用关系并追溯接收方、
独立产品标识与订阅端实际收到的来源说明。

**验证结论（实际执行）**：本仓库交付前已在容器内完成 `pytest`（36/36 通过）、
`scripts/replay.py`（44/44 通过）与 `scripts/replay_spatial.py`（34/34 通过，
本地 uvicorn + 本机 PostgreSQL 18）。

## 基准样例（回放使用，体现换算语义）

目标高程：`ND85`（1985 国家高程）。转换边语义 `height(to) = height(from) + offset_m`，
单位米，水位向上为正，每条边带生效区间 `[valid_from, valid_to)`：

| 边 | 偏移(m) | 生效区间 | 说明 |
| --- | --- | --- | --- |
| PORT_A → ND85 | +0.832 | 2025-01-01 ~ 2026-06-01T00:00Z | 重测前 |
| PORT_A → ND85 | +0.815 | 2026-06-01T00:00Z ~ | 重测后（跨零点重测日） |
| BAY_B → ND85 | +1.204 | 2025-01-01 ~ | |
| LOCAL_SG → PORT_A | −0.310 | 2025-01-01 ~ | 多段路径第一段 |
| LOCAL_SG → ND85 | +0.520 | 2025-01-01 ~ | 与多段路径（−0.310+0.832=0.522）不相容 → 保留冲突 |
| ND85 → PORT_A | −0.832 | ~ 2026-06-01T00:00Z | 反向边构成环，简单路径约束下不重复加偏移 |

站点样例（读数单位/正方向/站点零点）：

| 站 | 区域 | 单位 | 正方向 | 零点基准 | 场景 |
| --- | --- | --- | --- | --- | --- |
| ST-A1 | gangqu-a | m | up（水位向上为正） | PORT_A | 10min 规则采样，跨重测日 |
| ST-A2 | gangqu-a | cm | up | PORT_A→BAY_B（中途变更） | 不规则采样按代表时段加权 |
| ST-C1 | gangqu-a | m | up | LOCAL_SG | 两条路径不相容 → CONFLICT |
| ST-NP | gangqu-a | m | up | ISOLATED | 无目标基准路径 → NO_PATH（不用邻站补齐） |
| ST-B1 | haiwan-b | m | up | BAY_B | 长断档/短断档/突跳待核/疑似更正 |
| ST-D1 | haiwan-b | mm | down（距水面距离向下为正） | BAY_B | 单位与符号换算（500mm → −0.5+1.204=0.704） |

产品实际按上述关系计算换算值（如 ST-A1 读数 1.000m → 重测前 1.832m、重测后 1.815m），
而非只替换标签。

## 关键设计语义

- **逐样本换算再上格**：跨重测日窗口先按各样本时刻的适用区间换算，再形成共同时间格；
  边界格元为两偏移的加权混合，不做“先平均再套一个偏移”。
- **基准版本快照**：每次基准/站点零点变更产生新 `datum_version`；作业创建时固定版本，
  运行全程使用该快照——发布中途配置变更不会出现一半站点按旧基准、一半按新基准。
- **多段路径与冲突**：转换边构成有向图，DFS 枚举简单路径（节点不重复，循环路径不重复加偏移）；
  两条有效路径结果不相容时保留全部候选并标记 `CONFLICT`；无路径站点标记 `NO_PATH`，
  不用邻站偏移补齐。
- **重采样**：格元边界明确；不规则采样按代表时段（Voronoi 区间，单侧最多 max_rep/2）加权；
  重复序号不增加权重；覆盖率不足时仅短断档（≤30min 且两侧均为 GOOD）允许线性插值；
  长断档记 `MISSING`；突跳待核段记 `PENDING`，不插成平滑实测曲线。
- **可溯源产品单元**：每格元记录参与的原始样本 id、覆盖时长、换算路径（含各边偏移与
  生效区间）、质量等级与原因、基准版本，接收方能分清实测/允许的插值/缺测/待核/冲突。
- **幂等接入**：同一站点重复送达同一采样序号且内容相同 → 去重；内容不同 → 更正，
  生成新观测版本并触发后续新产品版本。
- **分阶段发布**：作业只能生成 `PRELIMINARY`；确认产品经 `/products/{id}/confirm` 单独签发
  （系列内新版本），初报不能伪装成确认；订阅按阶段过滤，区域模型只收 CONFIRMED。
- **事务性发布**：产品、清单（产品单元）与待交付任务在同一事务提交；产品本体完整提交后
  才进入交付队列。
- **交付与恢复**：每订阅者一条交付任务，`delivery_key` 全局唯一；worker 发送前崩溃 → 任务
  保持 PENDING 重发；发送后崩溃/确认包丢失 → 重送保留同一标识，消费端按标识去重后再确认；
  消费端按 `publish_seq` 应用，慢重试的旧版本不会把接收方恢复成旧值；已确认消费者无需重收。

- **交付与恢复**：每订阅者一条交付任务，`delivery_key` 全局唯一；worker 发送前崩溃 → 任务
  保持 PENDING 重发；发送后崩溃/确认包丢失 → 重送保留同一标识，消费端按标识去重后再确认；
  消费端按 `publish_seq` 应用，慢重试的旧版本不会把接收方恢复成旧值；已确认消费者无需重收。

## 缺测潮位空间参考估计：设计语义（/spatial）

估计始终是**独立旁路产品**：不把邻站读数改名成本站实测，原站缺测事实（`station_gap_facts`）
原样保留；不生成台风预报或防灾行动建议。

- **一次分析固定边界**：`SpatialAnalysis` 创建时即固定目标站、拟合期、缺测时段、资料截止点、
  允许海况、来源站与滞后范围；配对样本、候选参数、验证结果、估计输出分别挂在该分析下，
  边界不可中途改写，分析键幂等。
- **输入筛选**：按站点有效安装期与质量资格（`station_eligibility`）筛选；水位先统一基准
  （复用基准转换图）与单位（米、向上为正）；只取共同时间格上的 `MEASURED` 真实观测支持。
  两站窗口不同时只用共同有观测的时间段；**长缺口、初报不明（SUSPECT/PENDING）、基准冲突
  （CONFLICT/NO_PATH）不会被插值成训练配对**。
- **候选滞后对齐，禁止同时刻直接相减**：在允许的滞后范围内逐候选把参考站窗口移动
  `ref(t)=target(t+lag)` 对齐；lag=0 也只是显式候选之一，必须参与验证，不能直接取同时刻
  最近读数相减。单源拟合实际偏移与尺度 `y = offset + scale·x`。
- **连续片段划开，不随机打散**：配对支持中断即切成连续潮汐片段；拟合/验证按预先划定的
  验证期时间边界（`validation_start`）切分，跨边界的连续片段只做一次干净的时间切割，
  邻近采样不会被随机分到训练/验证两边泄漏同一段变化。
- **参数选择只用拟合资料，误差在留出片段计算**：滞后/参数按 **TRAIN** 误差选择，
  最终误差在未参与选择的 **VAL** 片段上分海况统计。验证记录逐海况列出覆盖时长、幅度范围、
  平均偏差、MAE/RMSE 与极端水位（最高水位处）误差；样本再多但只覆盖平稳期，也**不签发
  风暴期间适用资格**。
- **单源 vs 多源（按样例的同步覆盖与海湾关联决定）**：
  - 单源路径为每个目标站维护经独立验证的单参考站关系，可登记多个备用；缺测时按验证 RMSE
    选来源、参考站异常则切换下一个备用，解释与运行都简单；每个备用关系独立给出结果与切换
    依据，不把相位差很大的两段结果在边界直接连成一条平滑实测曲线；代价是需逐对维护、
    参考站自身异常时可能暂时无估计。
  - 多源路径用多个已对齐序列联合拟合 `y = offset + Σ scale_k·x_k`，对局部缺测有更多替代；
    但每个**来源组合都要独立验证**（枚举全部非空组合），一站退出不能把其权重简单分给
    剩余站。拟合前检查设计矩阵可识别性（列归一化后的条件数），高度共线、条件数超阈的组合
    标记 `REJECTED_COLLINEAR` 保持待核，不让求解器给出的巨大相反系数掩盖问题；多站平均
    不天然比单源更可信。
- **海况只来自明确资料**：海况类别由 `sea_condition_windows` 显式提供，系统不自行推断；
  目标时段类别缺失或不在验证范围时**停止估计**（`UNAVAILABLE`），绝不先看哪个候选接近
  期望水位再反选海况。
- **截止点与未来数据**：正向滞后需要目标时刻之后的参考读数。实时运行只使用截止点前已取得
  的资料，未来数据未到时该格返回 `WAITING_FUTURE_DATA`（附 `ready_at`）而非伪造；
  离线回补用新 `batch_key` 生成**新批次**，原批次依据永久保留，不用后来读数伪装当时可知。
- **不递归接力，来源展开到真实观测**：参考站自身处于缺测时不引用其估计产品，更不允许
  甲引乙、乙引甲互相支持（关系目录建边即做环检测）；每个估计点把来源展开到真实观测 id，
  重复使用同一物理记录不增加独立支持。
- **经验误差与外推标记**：误差半宽按声明方法（默认 RMSE×1.96）计算并保留样本量与适用
  条件，它表示经验表现、不保证未出现过的极端天气必落其中；估计值超出验证幅度范围时标记
  `extrapolation_limited`，不沿一条直线无限延伸。
- **恢复对照与停用追溯**：目标站恢复后，用迟到实测逐片段对照**原估计（不重新拟合）**，
  统计偏差发生片段与是否超停用阈值；超阈则停用关系并找出引用它的估计批次与接收方，
  实测产品不受牵连；后续补齐资料生成的是新关系，不改变原估计的依据。
- **独立发布通道**：估计用独立发布序号 `estimate_publish_seq`、独立订阅
  （`accept_estimates` 显式声明）、独立队列表 `estimate_deliveries` 与消费端独立信箱；
  估计载荷带 `product_kind=SPATIAL_ESTIMATE`，只消费实测确认值的通道收到估计会返回 422
  显式拒绝，两类产品绝不混流。

## API 概览（详见 Swagger UI /docs）

实测域：
- 目录：`POST/GET /datums`、`POST/GET /datum-conversions`、`GET /datum-versions`、
  `POST/GET /stations`、`PUT /stations/{id}/datum`
- 接入：`POST /observations:ingest`、`GET /observations`
- 产品：`POST /jobs`、`POST /jobs/{id}/run`、`POST /products/{id}/confirm`、
  `GET /products`、`GET /products/{id}/units`、`GET /products/{id}/superseded-deliveries`
- 订阅交付：`POST/GET /subscriptions`、`GET /deliveries`、`POST /deliveries/{id}/retry`、
  `POST /worker/run-once`
- 模拟消费端（8001）：`POST /receive/{name}`、`GET /state`、`GET /inbox`、
  `PUT /flags/{drop_ack|offline}`、`POST /reset`

空间估计域（前缀 `/spatial`）：
- 资料目录：`POST/GET /spatial/sea-conditions`、`POST/GET /spatial/sea-windows`、
  `POST/GET /spatial/eligibility`、`POST /spatial/eligibility/{id}/deactivate`、
  `POST/GET /spatial/station-references`
- 配对资料导入/滞后试算/连续片段验证：`POST /spatial/analyses`、
  `POST /spatial/analyses/{id}/run`、`GET .../segments|pairs|lag-candidates|validations`
- 适用性与缺测事实：`GET /spatial/applicability`、`GET /spatial/gap-facts`、
  `POST /spatial/gap-facts/{id}/resolve`、`GET /spatial/relations`
- 估计生成与序列：`POST /spatial/estimates:generate`、`GET /spatial/batches`、
  `GET /spatial/batches/{id}/points`
- 独立订阅/交付：`POST/GET /spatial/estimate-subscriptions`、
  `GET /spatial/estimate-deliveries`、`POST /worker/run-estimates-once`
- 恢复对照：`POST /spatial/recovery-review`、`GET /spatial/reviews`
- 模拟消费端（8001）：`POST /receive-estimates/{name}`、`GET /estimate-inbox`、
  `GET /estimate-state`

## 目录结构

```
app/
  main.py             # 实测 API（FastAPI，Swagger UI 于 /docs）
  spatial_api.py      # 空间估计 API 路由（/spatial，独立产品域）
  consumer.py         # 模拟消费端（实测 + 估计两套独立信箱/通道）
  worker.py           # 交付工作进程（实测与估计两条独立队列，崩溃可恢复）
  services.py         # 接入去重/更正、产品生成、确认签发、同事务交付
  spatial_services.py # 空间估计编排：配对/试算/片段验证/关系/估计/恢复停用
  spatial.py          # 纯计算：片段划分、OLS、条件数诊断、分海况指标、误差
  datum.py            # 基准转换图：简单路径、冲突保留、生效区间
  grid.py             # 重采样：代表时段加权、覆盖、插值/缺测/待核
  models.py           # 实测域 + 空间估计域全部表模型
scripts/replay.py          # 实测产品验收回放（44 项断言）
scripts/replay_spatial.py  # 空间估计验收回放（34 项断言）
tests/                     # pytest（36 项，含 test_spatial.py 16 项）
```
