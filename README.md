# 潮位观测数据产品发布与订阅服务

面向沿海防潮指挥部门的潮位数据产品服务：把不同港区/海湾验潮站的读数统一到同一时空口径
（统一目标高程基准 + 共同时间格 + 质量等级），以**分阶段发布**（初报 → 确认产品）方式
向各消费系统交付，替代邮件交换表格、各自猜测基准与缺测含义的现有做法。

- 站点目录：观测位置、垂直基准（站点零点）、读数单位、正方向、设备状态
- 产品生成：原始观测 → 逐样本基准换算（按生效区间）→ 共同时间格重采样 → 质量等级
- 分阶段发布：`PRELIMINARY` 初报供值守研判，`CONFIRMED` 确认产品才进入正式模型
- 订阅交付：按区域/阶段订阅，产品+清单+待交付任务同事务提交，消费者分别确认、独立重试

## 技术栈

Python 3.12 · FastAPI · PostgreSQL（SQLAlchemy 2）· 单容器即可运行（API + 模拟消费端 + 交付 worker）

## 快速开始（Docker Compose，一键）

```bash
docker compose up -d --build db api consumer worker
# 验收回放（44 项断言，退出码非 0 即失败）
docker compose run --rm replay
```

- API 与 Swagger UI：http://127.0.0.1:8000/docs （OpenAPI 3.x：/openapi.json）
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
# 终端 3：交付工作进程（崩溃恢复语义见下）
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
# 1) 单元与集成测试（基准图/重采样/接入去重/版本快照/同事务交付，20 项）
.venv/bin/python -m pytest tests/ -q

# 2) 端到端验收回放（需 API + 消费端已启动；44 项断言）
.venv/bin/python scripts/replay.py
```

回放脚本 `scripts/replay.py` 会真实导入样例数据并核对：多基准站点换算值、重复报文去重、
迟到补传与质量更正生成的新版本、发布中途基准配置变更的版本隔离、路径冲突保留、
断档/插值/待核标记、回执丢失重送去重、消费者离线恢复、慢重试不恢复旧值、
订阅者分别确认与失败者独立重试。

**验证结论（实际执行）**：本仓库交付前已在容器内完成 `pytest`（20/20 通过）与
`scripts/replay.py`（44/44 通过，本地 uvicorn + 本机 PostgreSQL 18）。

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

## API 概览（详见 Swagger UI /docs）

- 目录：`POST/GET /datums`、`POST/GET /datum-conversions`、`GET /datum-versions`、
  `POST/GET /stations`、`PUT /stations/{id}/datum`
- 接入：`POST /observations:ingest`、`GET /observations`
- 产品：`POST /jobs`、`POST /jobs/{id}/run`、`POST /products/{id}/confirm`、
  `GET /products`、`GET /products/{id}/units`、`GET /products/{id}/superseded-deliveries`
- 订阅交付：`POST/GET /subscriptions`、`GET /deliveries`、`POST /deliveries/{id}/retry`、
  `POST /worker/run-once`
- 模拟消费端（8001）：`POST /receive/{name}`、`GET /state`、`GET /inbox`、
  `PUT /flags/{drop_ack|offline}`、`POST /reset`

## 目录结构

```
app/
  main.py       # API（FastAPI，Swagger UI 于 /docs）
  consumer.py   # 模拟消费端（去重、按 publish_seq 应用、故障注入）
  worker.py     # 交付工作进程（崩溃可恢复）
  services.py   # 接入去重/更正、产品生成、确认签发、同事务交付
  datum.py      # 基准转换图：简单路径、冲突保留、生效区间
  grid.py       # 重采样：代表时段加权、覆盖、插值/缺测/待核
  models.py     # 站点/基准版本/观测版本链/产品/订阅/交付
scripts/replay.py  # 验收回放（44 项断言）
tests/             # pytest（20 项）
```
