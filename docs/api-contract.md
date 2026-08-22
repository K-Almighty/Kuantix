# Kuantix REST API v1 契约（锁定版）

> 文档作者：software-architect-2　时间：2026-08-01
> 基线：`docs/system_design.md`（§3 classDiagram / §7 T05 / §8 NF-9、NF-12）＋ `docs/PRD.md`（NF-9/NF-10/NF-11/NF-12/NF-21/NF-22/NF-26/NF-27/NF-28）
> 实现核对：`Kuantix/core/envelope.py`、`Kuantix/core/contracts.py`、`Kuantix/main.py`（/health、/api/version、异常→信封映射）、`Kuantix/cli.py`（子命令骨架）、`Kuantix/config.py` + `config.toml`
> 约束：**纯设计文档**，后端 `api/routers/*` 与前端 `web/` 照此实现；不改任何现有文件、不碰 easy_tdx-main。
> 配套校验器：`tests/redlines/envelope_validator.py`（NF-9/NF-12/NF-6 自动检查，**所有 JSON 响应必须通过**）。

---

## 1. 全局约定

### 1.1 Base URL 与端口

```
Base URL = http://{host}:{port}
```

- `host` / `port` **完全来自 `config.toml [server]` 节**（`config.py:ServerConfig`），支持环境变量覆盖 `Kuantix__SERVER__PORT`。
- ⚠️ **端口裁决**：当前仓库 `config.toml` 实为 **`port = 8899`**（`[server]`）。早期口述"默认 8000"未落地到代码。**契约以 config 为准，前后端一律禁止硬编码端口**；前端从 `/api/version` 读取或由构建时注入。
- 与上游 easy_tdx web 独立（NF-17）：Kuantix 端口 ≠ 上游端口，可同时运行互不干扰。
- 默认仅监听 `127.0.0.1`（NF-23），**无鉴权**（P0 本地工具）。

### 1.2 统一信封（NF-9）

所有 **JSON 响应**（含错误响应、WebSocket 帧）均为：

```json
{
  "code": 0,
  "message": "ok",
  "data": { },
  "meta": {
    "generated_at": "2026-08-01T15:02:31+08:00",
    "data_date": "2026-08-01",
    "market": "CN",
    "elapsed_ms": 123,
    "version": "0.1.0"
  }
}
```

| meta 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| generated_at | string(ISO8601 秒级带时区) | 响应生成时刻（本地时区） | `2026-08-01T15:02:31+08:00` |
| data_date | string\|null | 业务数据基准日 `YYYY-MM-DD`；无基准为 null | `2026-08-01` |
| market | string | 市场码 CN/HK/US（NF-6），回显请求值或默认值 | `CN` |
| elapsed_ms | int | 服务端处理耗时（毫秒） | `123` |
| version | string | Kuantix 版本 | `0.1.0` |

- 顶层**只允许**这 4 个键（envelope_validator `allow_extra_top_keys=false`）。
- **错误信封**同样带完整 meta；`data` 承载错误明细（见 1.3）。

### 1.3 错误码表（NF-26 fail-loud）

业务码常量定义于 `core/envelope.py`，HTTP 映射定义于 `main.py:http_status_for`。**所有失败一律显式报错，禁止静默兜底**。

| code | HTTP | 名称 | 触发场景（fail-loud） |
|---|---|---|---|
| 0 | 200 | 成功 | — |
| 400 | 400 | CODE_INVALID_ARGUMENT | 请求参数校验失败、缺失必填键（MissingKeyError）、配置缺键（MissingConfigError）、未知枚举/用法错误；框架级 405 等也归 400 |
| 404 | 404 | CODE_NOT_FOUND | job/batch/model/rule/watchlist/position 不存在；未知路径 |
| 422 | 422 | CODE_DATA_ERROR | 数据完整性：UNKNOWN 证券类型（UnknownValueError）、回读不一致/NaN/越界（DataIntegrityError）、上游契约不符（UpstreamContractError） |
| 500 | 500 | CODE_INTERNAL_ERROR | 未预期异常（消息原样透传，不做"友好化"改写） |
| 501 | 501 | CODE_NOT_IMPLEMENTED | 能力未实现且拒绝静默降级（NotSupportedError）：HK/US 市场未启用、命令/端点未落地 |

错误信封 `data` 形态：

```json
{ "code": 422, "message": "[fail-loud/NF-26] Security(600001).security_type=UNKNOWN，未知证券类型禁止进入业务链路",
  "data": { "error_type": "UnknownValueError", "path": "/api/v1/data/verify" },
  "meta": { "...": "..." } }
```

- fail-loud 类异常：`data = {error_type, path}`（error_type 为异常类名）。
- 请求参数校验失败：`data = {errors: [pydantic 明细数组]}`。
- 业务上下文（标的/文件/字段）必须写进 `message`，便于定位。

### 1.4 数值安全（NF-12）

- JSON 中**禁止** `NaN` / `Infinity` / `-Infinity`，一律序列化为 `null`（`Envelope.sanitize` + `to_json(allow_nan=False)` 双保险）。
- 浮点统一 `round(x, 6)`，**最大 6 位小数**。
- 比例字段口径（易错点，见 §8）：`change_pct` / `pnl_pct` / `ic_positive_rate` / `turnover_rate` 等一律是**小数比例**（`0.05` = 5%），**不是** 5.0 这种百分比数字。

### 1.5 认证（NF-23）

- P0 本地工具**无认证**：默认监听 `127.0.0.1`，不做鉴权；远程访问由用户自行加反向代理并自担风险。
- 无 token、无 session、无 cookie；CORS 白名单来自 `config.toml [server].cors_origins`（默认含 `http://localhost:5173`）。

### 1.6 分页约定

所有列表端点统一：

| 请求参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| page | int | 1 | 从 1 开始 |
| page_size | int | 50 | 上限 500，超限报 400 |

响应 `data` 统一分页壳：

```json
{ "items": [ ... ], "page": 1, "page_size": 20, "total": 213, "total_pages": 11 }
```

### 1.7 时间与日期格式

| 类别 | JSON 格式 | 示例 | 说明 |
|---|---|---|---|
| 日期字段（date 语义） | `YYYY-MM-DD` 字符串 | `"2026-08-01"` | 所有 API 日期字段统一；`meta.data_date` 同 |
| 时间戳字段（datetime 语义） | ISO-8601 秒级带时区 | `"2026-08-01T15:02:31+08:00"` | `generated_at`、`created_at`、`ts` 等 |
| vipdoc 内部日期 | `YYYYMMDD` 整数 | `20260801` | **仅存在于存储层**（`Bar.date_int`），**不出现在 API** |
| 无值 | null | `"data_date": null` | 无基准日/未计算时用 null，不用空串 |

`meta.data_date` 按端点取值约定：

| 端点 | data_date 取值 |
|---|---|
| GET /api/v1/data/status / verify | 数据湖最新交易日（coverage.last_date） |
| GET /api/v1/factor/report | 因子数据 end_date |
| GET /api/v1/screen/results | 批次 as_of |
| GET /api/v1/monitor/positions / watchlist | 最近一次轮询快照的交易日 |
| 其余 | null 或最近交易日 |

### 1.8 market 参数贯穿（NF-6）

- 每个业务端点支持 query 参数 `market`（枚举 `CN`/`HK`/`US`，大小写不敏感，服务端规范化为大写），默认取 `config.toml [markets].default`（当前 `CN`）。
- `meta.market` 回显实际生效的市场码。
- HK/US 在 P0 `enabled=false`：任何对 HK/US 的调用返回 **501 信封**（`NotSupportedError`，`message` 注明"接口先行、拒绝静默降级"），**不得**静默按 A 股规则处理（NF-7/NF-26）。
- 跨市场数据不得混算；隔离区/因子/选股/告警/持仓全部带 market 字段。

### 1.9 长任务 Job 模型（sync / factor compute / screen run）

- 三类长任务一律 **异步后台运行**（T03 SyncEngine 后台运行、T04 FactorService/ScreenService 后台线程），REST **不阻塞**（PRD 数据层验收⑤）。
- 触发接口返回 **200 + 信封**，`data` 为 `Job` DTO（含 `job_id`、`status`）；**不做 202**（与现有 `http_status_for` 一致，code=0 → 200）。
- 前端轮询对应模块的进度端点（1–2s 间隔）；`status` 生命周期：`queued → running → done | failed | cancelled`。
- 断点续传：`cancelled`/中断后重新触发同参数 sync，自动跳过已完成标的（T03 验收）。

```json
{ "job_id": "job_01JX...", "module": "data", "action": "sync_full",
  "status": "running", "market": "CN",
  "progress": { "total": 5209, "done": 1200, "failed": 3, "quarantined": 2,
                "current": "sh600519", "percent": 23.13,
                "started_at": "2026-08-01T09:00:00+08:00", "updated_at": "2026-08-01T09:05:31+08:00" },
  "result_summary": null, "error": null,
  "created_at": "2026-08-01T09:00:00+08:00", "updated_at": "2026-08-01T09:05:31+08:00" }
```

### 1.10 文件导出约定（NF-22 / NF-21）

- **JSON 导出**：返回标准信封（与对应 GET 端点同一 schema），前端直接下载响应文本。
- **CSV 导出**（仅选股结果）：**非信封**，`text/csv; charset=gbk`（同花顺兼容），`Content-Disposition: attachment`；文件头含免责标注（NF-22）。
- **NF-21**：契约中**不存在任何下单/委托/交易接口**；CSV 是纯文本产物，仅供人工核对后手动导入交易软件。

---

## 2. 端点清单

路由前缀：Kuantix 自身业务 **`/api/v1`**（T05 四个 router 统一挂载，替代早期 `/api/data` 口述口径，见 §8）。基础设施端点保持现状。

### 2.0 基础设施端点（T01 已实现，勿改）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针：`data = {status, started_at, uptime_seconds, markets_enabled}`；`markets_enabled` 为**对象** `Record<market_code, bool>`（v1.1 锁定，见 §9 R1.1-2） |
| GET | `/api/version` | `data = {name, version, upstream_easy_tdx, config_source, market_default}` |
| GET | `/docs` | Swagger UI（NF-11） |
| GET | `/openapi.json` | OpenAPI 3 JSON Schema（NF-11，前端/Agent 自省用） |

`GET /health` 的 `data` 载荷（markets_enabled 形状 v1.1 锁定）：

```json
{ "status": "ok", "started_at": "2026-08-01T15:00:00+08:00", "uptime_seconds": 123.456,
  "markets_enabled": { "CN": true, "HK": false, "US": false } }
```

### 2.1 data 路由（`api/routers/data.py`，前缀 `/api/v1/data`）—— 共 8 端点

| # | 方法 | 路径 | 请求体/参数 | 响应 data | 错误场景 |
|---|---|---|---|---|---|
| D1 | GET | `/api/v1/data/status` | `market?` | `DataLakeStatus`（覆盖统计 + 最新 job + 隔离区计数） | 市场未启用→501 |
| D2 | POST | `/api/v1/data/sync` | `SyncRequest` | `Job`（action=sync_full/sync_incremental） | 交易时段强制全量回补→422（NF-28 需显式确认）；参数非法→400；市场未启用→501 |
| D3 | GET | `/api/v1/data/sync/{job_id}` | — | `Job`（含 progress） | job 不存在→404 |
| D4 | POST | `/api/v1/data/sync/{job_id}/cancel` | — | `Job`（status=cancelled） | job 不存在→404；已 done→422 |
| D5 | GET | `/api/v1/data/verify` | `market?` | `VerifyReport`（含隔离区清单，NF-27） | 数据目录缺失→422；市场未启用→501 |
| D6 | GET | `/api/v1/data/quarantine` | `market?` | `{items: [QuarantineEntry], page, page_size, total, total_pages}` | — |
| D7 | DELETE | `/api/v1/data/quarantine/{code}` | `market?` | `{removed: "600001", reason: "readback_mismatch"}` | 条目不存在→404 |
| D8 | GET | `/api/v1/data/search` | `q`(必填), `market?`, `limit?`(1..50, 默认20) | `{items: [SecurityHit], count}`（v1.2 增量：证券搜索） | q 为空→400；清单源不可用→422；市场未启用→501 |

请求体示例（D2）：

```json
{ "mode": "full", "market": "CN", "years": 10, "workers": 4 }
// mode=incremental 时忽略 years/workers；workers 缺省取 config [sync].workers
```

D8 说明（v1.2 增量）：
- `q` 支持**证券代码**（精确匹配优先 → 前缀匹配，至少 2 位）与**证券名称**（模糊子串，大小写不敏感）；
- 数据源：`UniverseEnumerator` 证券清单本地缓存（`~/.Kuantix/db/security_catalog.json`，首次枚举后落盘）；
- 无匹配 → **显式空数组**（`items: [], count: 0`，合法态）；`q` 为空 → 400；清单源不可用 → 422（fail-loud，不静默空返回）。

```json
GET /api/v1/data/search?q=浦发
{ "code": 0, "message": "ok",
  "data": { "items": [ { "code": "600000", "name": "浦发银行", "exchange": "sh",
                          "market": "CN", "security_type": "SH_A_STOCK" } ], "count": 1 },
  "meta": { "generated_at": "...", "data_date": null, "market": "CN", "elapsed_ms": 5, "version": "0.1.0" } }
```

### 2.1b backtest 路由（`api/routers/backtest.py`，前缀 `/api/v1/backtest`）—— 共 4 端点（v1.2 增量）

| # | 方法 | 路径 | 请求体/参数 | 响应 data | 错误场景 |
|---|---|---|---|---|---|
| B1 | GET | `/api/v1/backtest/strategies` | — | `{items: [StrategySchema], count}`（上游预置策略 + 参数 schema） | 服务未装配→400 |
| B2 | POST | `/api/v1/backtest/run` | `BacktestRunRequest` | `Job`（module=backtest, action=run） | 标的池空/超限→400；市场未启用→501；标的池全部读不到数据→job failed 422 |
| B3 | GET | `/api/v1/backtest/jobs/{job_id}` | — | `Job`（done 时 result_summary 含绩效摘要） | job 不存在→404 |
| B4 | GET | `/api/v1/backtest/results/{job_id}` | — | `BacktestResult`（净值序列/回撤/绩效指标/成交明细 + 组合视图） | job 不存在→404；结果未就绪→404 |
| C1 | GET | `/api/v1/backtest/jobs` | `limit?`(1..50，默认 20), `status?`(可空=全部), `module?`(默认 backtest) | `{items: [Job], count}`（按 created_at 倒序；Job 含 result_summary 供 Compare 卡片展示） | limit 越界→400；status 非法→400 |
| B5 | GET | `/api/v1/backtest/kline/{code}` | `market?`(默认 CN), `start?`, `end?`, `strategy?`(默认 ma_cross) | `KlineWithSignals`（K 线数组 + buy_points/sell_points 信号标注，K 线图叠加用） | code 非法→400；无数据→404（显式）；市场未启用→501 |

B2 请求体示例：

```json
{ "market": "CN", "codes": ["600000", "600036"], "strategy": "ma_cross",
  "params": { "fast": 5, "slow": 20 },
  "start": "2024-01-01", "end": "2024-12-31",
  "cash": 1000000, "commission": 0.0003, "min_commission": 5.0,
  "stamp_tax": 0.001, "slippage": 0.0, "execution": "next_open" }
```

B1 策略列表响应（节选）：

```json
{ "code": 0, "message": "ok",
  "data": { "items": [ { "name": "ma_cross", "label": "双均线交叉",
    "description": "快线上穿慢线买入，快线下穿慢线卖出。最经典的趋势跟随策略。",
    "params": [ { "name": "fast", "type": "int", "default": 5, "label": "快线周期",
                  "min_value": 1, "max_value": 60 },
                { "name": "slow", "type": "int", "default": 20, "label": "慢线周期",
                  "min_value": 5, "max_value": 250 } ] } ],
    "count": 19 }, "meta": { "...": "..." } }
```

> 说明：回测引擎调用经 `Kuantix/adapters/backtest_bridge.py` 收敛到上游
> `easy_tdx.backtest`（BacktestEngine / PerformanceAnalyzer），Kuantix 侧为薄包装
> （NF-1/R2）；路径不含 order/trade/buy/sell（R5：回测是模拟撮合，不暴露下单端点）。

**C1 语义（v1.3 增量 P1）**：任务列表默认返回**全部 status**（`status` 参数可空），
前端过滤 `done` 对齐上游 fetchTaskList→filter（决策 D-7-A）；`limit` 越界 / `status`
非法 → 400（fail-loud，JobStore.list 校验）。

**B5 语义（v1.3 增量 P1）**：单标的 K 线下钻（D-2 方案 A）。K 线一律读本地 vipdoc
（L1Reader，零网络）；`buy_points` / `sell_points` 是**信号标注**数组
（`{date, price}`，数据结构非下单动作，R5 允许），由 `BacktestBridge.signal_points`
经策略回测成交信号序列计算（等价 SignalScanner 的单标的信号检测，返回完整信号
序列供 K 线图叠加）。无数据 → 404（显式，fail-loud）。

### 2.1c portfolio 路由（`api/routers/portfolio.py`，前缀 `/api/v1/portfolio`）—— 共 3 端点（v1.3 增量）

| # | 方法 | 路径 | 请求体/参数 | 响应 data | 错误场景 |
|---|---|---|---|---|---|
| P1 | POST | `/api/v1/portfolio/run` | `PortfolioRunRequest` | `Job`（module=backtest, action=portfolio） | 标的池空/超限→400；市场未启用→501；标的池全部读不到数据→job failed 422 |
| P2 | GET | `/api/v1/portfolio/jobs/{job_id}` | — | `Job`（done 时 result_summary 含组合绩效摘要） | job 不存在→404 |
| P3 | GET | `/api/v1/portfolio/results/{job_id}` | — | `PortfolioResult`（total_performance / individual_results / equity_allocation / combined_equity） | job 不存在→404；结果未就绪→404 |

P1 请求体示例：

```json
{ "market": "CN", "codes": ["600519", "000001"], "strategy": "ma_cross",
  "params": { "fast": 5, "slow": 20 },
  "start": "2020-01-01", "end": "2025-12-31",
  "cash": 1000000, "commission": 0.0003, "min_commission": 5.0,
  "stamp_tax": 0.001, "slippage": 0.0, "execution": "next_open" }
```

P1 语义（D-1/D-8）：组合 = **1 策略 × N 标的，总资金按 N 均分**（`cash / N` 到
各标的独立资金池），组合净值 = 各标的净值按日期并集 ffill 对齐后**金额求和**
（非 /backtest 的归一化等权平均）。引擎调用经 `BacktestBridge.run_portfolio_backtest`
直调上游 `PortfolioBacktestEngine`（R2）；K 线一律读本地 vipdoc（零网络）。

P3 响应 `PortfolioResult`：

```jsonc
{ "total_performance": { "total_return": 0.2643, "annual_return": 0.052,
    "total_stocks": 2, "total_cash": 1000000 },
  "individual_results": {
    "600519": { "performance": {...19项}, "equity_curve": [...], "trades": [...],
                "positions": [...], "config": {...} },
    "000001": { /* 同上 */ } },
  "equity_allocation": { "600519": 0.5, "000001": 0.5 },
  "combined_equity": [ { "datetime": "2020-01-02", "total": 1000000.0,
                         "drawdown": 0.0, "drawdown_pct": 0.0 }, ... ] }
```

> 说明：`individual_results` / `equity_allocation` 的 key 为 **6 位代码**
> （适配层把上游原生 key `{exchange}{code}` 归一化）；`combined_equity` 含
> `drawdown_pct`（组合评级 5 维的输入，前端可直接消费）。

### 2.1d strategies 路由（`api/routers/strategies.py`，前缀 `/api/v1/strategies`）—— 共 5 端点（v1.3 增量）

| # | 方法 | 路径 | 请求体/参数 | 响应 data | 错误场景 |
|---|---|---|---|---|---|
| S1 | GET | `/api/v1/strategies` | `kind?`(single/portfolio/multi，可空=全部), `page?`, `page_size?` | `{items: [SavedStrategy], page, page_size, total, total_pages}`（§1.6 分页壳） | kind 非法→400 |
| S2 | POST | `/api/v1/strategies` | `StrategyCreate` | **201** + `SavedStrategy`（含服务端生成 id/created_at/updated_at/app_version） | name 空/超长→400；kind 非法→400 |
| S3 | GET | `/api/v1/strategies/{strategy_id}` | — | `SavedStrategy` | 不存在→404 |
| S4 | DELETE | `/api/v1/strategies/{strategy_id}` | — | `{removed: strategy_id}` | 不存在→404（fail-loud，不静默成功） |
| S5 | POST | `/api/v1/strategies/run-multi` | `MultiStrategyRunRequest` | `Job`（module=backtest, action=multi） | items 空/超限→400；市场未启用→501；全部槽位读不到数据→job failed 422 |

S5 请求体示例（来源 = 策略库勾选，前端把勾选的 `strategy_id` 解析为 items 后提交）：

```json
{ "market": "CN",
  "items": [
    { "strategy": "ma_cross", "label": "双均线交叉", "code": "600519", "params": {"fast": 5, "slow": 20} },
    { "strategy": "macd", "label": "MACD", "code": "000001", "params": {} } ],
  "cash": 1000000, "commission": 0.0003, "min_commission": 5.0,
  "stamp_tax": 0.001, "slippage": 0.0, "execution": "next_open",
  "start": "2020-01-01", "end": "2025-12-31" }
```

S5 语义：多策略组合回测 = **N 策略 × 各自标的，总资金 1/N 均分**到各槽位，
各槽位独立回测，组合净值按日期并集 ffill 对齐后**金额求和**；结果结构与
`PortfolioResult` 一致（结果可经 P3 或 B4 读取），`individual_results` 的 key =
`"{label}@{symbol}"`（如 `双均线交叉@SH:600519`）。引擎调用经
`BacktestBridge.run_multi_strategy` 直调上游 `MultiStrategyEngine`（R2）。

> 说明：策略库落 `~/.Kuantix/db/strategies.db`（NF-15，单表，id 服务端生成
> `strat_<uuid12>`）；`context` 按 kind 语义：single=`{symbol,start_date,end_date}`、
> portfolio=`{stocks[]}`、multi=`{items[]}`；`snapshot` 为保存时关键绩效。
> 上游无编辑，**S4 为 DELETE（不做 PUT）**——改名/编辑 = 删除 + 重建（前端处理）。

### 2.1e optimize 路由（`api/routers/optimize.py`，前缀 `/api/v1/optimize`）—— 共 6 端点

| # | 方法 | 路径 | 请求体/参数 | 响应 data | 错误场景 |
|---|---|---|---|---|---|
| O1 | POST | `/api/v1/optimize/run` | `OptimizeRunRequest` | `Job`（module=backtest, action=optimize） | code 空→400；param_grid 空/参数数>2/取值列表空→400；网格（笛卡尔积）>200→400；市场未启用→501；K 线读取失败→job failed 422 |
| O2 | GET | `/api/v1/optimize/jobs/{job_id}` | — | `Job`（done 时 result_summary 含 best 摘要） | job 不存在→404 |
| O3 | GET | `/api/v1/optimize/results/{job_id}` | — | `OptimizeResult`（results/best/heatmap） | job 不存在→404；结果未就绪→404 |
| O4 | POST | `/api/v1/optimize/run-all` | `OptimizeAllRequest` | `Job`（module=backtest, action=optimize_all） | 同 O1 参数校验；市场未启用→501；单标的 K 线读取失败→对应策略标 failed |
| O5 | GET | `/api/v1/optimize/all/results/{job_id}` | — | `OptimizeAllResult`（results/ranking/per_strategy） | job 不存在→404；结果未就绪→404 |
| O6 | DELETE | `/api/v1/optimize/jobs/{job_id}` | — | `{ job_id, deleted_job, deleted_result }` | job 与结果均不存在→404；幂等（重复删除→404） |

O1 请求体示例：

```json
{ "market": "CN", "code": "600519", "strategy": "ma_cross",
  "param_grid": { "fast": [5, 10, 20], "slow": [10, 20, 30] },
  "start": "2020-01-01", "end": "2025-12-31",
  "cash": 1000000, "commission": 0.0003, "min_commission": 5.0,
  "stamp_tax": 0.001, "slippage": 0.0, "execution": "next_open" }
```

O3 响应 `OptimizeResult`：

```jsonc
{ "strategy": "ma_cross", "param_names": ["fast", "slow"],
  "results": [ // 按 total_return 降序
    { "params": {"fast": 5, "slow": 10}, "total_return": 0.32, "sharpe": 1.4,
      "max_drawdown": 0.21, "total_trades": 36, "win_rate": 0.5, "profit_factor": 1.8 } ],
  "best": { "params": {"fast": 5, "slow": 10}, "total_return": 0.32, "sharpe": 1.4,
            "max_drawdown": 0.21, "total_trades": 36, "win_rate": 0.5, "profit_factor": 1.8 },
  "heatmap": { "x_name": "fast", "y_name": "slow", "x": [5,10,20], "y": [10,20,30],
               "data": [[0, 0, 0.1], [1, 0, 0.2], ...] } }  // 2 参数时非 null；1 参数为 null
```

> O1 语义（D-4，单标的口径）：**单标的（1 code）× 1-2 参数网格**，笛卡尔积 ≤200
> （后端二次校验，超限 400 fail-loud，不依赖前端预校验）；引擎调用经
> `BacktestBridge.run_optimize` 直调上游 `ParamGridOptimizer`（R2，results/best/
> heatmap 原生结构）；结果以 job_id 为键落 `BacktestResultStore`（与 B4/P3 同库同
> schema，`action=optimize` 区分）。O4/O5 为「一键寻优所有策略」（遍历
> `strategies/` 下全部策略），O6 为「删除单个策略寻优」，三者均已落地。

### 2.1f settings 路由（`api/routers/settings.py`，前缀 `/api/v1/settings`）—— 共 2 端点（v1.3 增量 P2，**只读**）

| # | 方法 | 路径 | 请求体/参数 | 响应 data | 错误场景 |
|---|---|---|---|---|---|
| E1 | GET | `/api/v1/settings/status` | —（默认市场取自 config） | `SettingsStatus`（config 摘要 + known_hosts 只读列表 + 数据湖摘要 + 版本） | 上游 config.json 非法 JSON → 400（fail-loud）；默认市场 DataLake 未实现 → 422 |
| E2 | POST | `/api/v1/settings/test-connection` | `TestConnectionRequest` | `TestConnectionResult`（`{ok, host, port, kind, latency_ms, error?}`） | kind 非法→**400**；host 空/port 越界→400；组合根缺 tdx_factory→400；**连接失败→ code=0 信封 + `{ok:false, error}`（业务结果，非 HTTP 错误）** |

E1 响应 `SettingsStatus`（只读，NF-20）：

```jsonc
{
  "read_only": true,                 // 整页只读声明：无任何写操作能力
  "config": {
    "paths": { "root": "~/.Kuantix", "vipdoc": "~/.Kuantix/vipdoc", "db": "~/.Kuantix/db", "...": "..." },
    "default_market": "CN",
    "enabled_markets": ["CN"],
    "config_source": "/path/to/config.toml",
    "tdx": { "use_easy_tdx_known_hosts": true, "port": 7709, "ex_port": 7727,
             "timeout_seconds": 15.0, "mac_hosts": [...], "std_hosts": [...], "mac_ex_hosts": [...] }
  },
  "known_hosts": {
    "items": [ { "host": "123.60.47.136", "port": 7709, "kind": "mac", "read_only": true } ],
    "upstream_available": true,          // ~/.easy_tdx/config.json 是否存在（用户主目录视角）
    "upstream_source": "builtin",        // 实际来源: env(环境变量指定) | user(~/.easy_tdx) | builtin(项目内置兜底)
    "known_hosts_merged": true,          // 是否只读合入上游 known_hosts
    "upstream_config_untouched": true,   // 指纹校验通过（NF-20 自证：从未写上游）
    "upstream_config_path": "Kuantix/resources/easy_tdx_config.json"
  },
  "data": { "market": "CN", "data_date": "...", "coverage": { "securities": 2, "files": 2, "bars": 4800 },
            "quarantine_count": 0, "latest_job": null },   // 复用 D1 摘要
  "versions": { "Kuantix": "0.1.0", "upstream_easy_tdx": "1.20.3" }
}
```

E2 请求/响应示例：

```json
// POST /api/v1/settings/test-connection
{ "kind": "mac", "host": "123.60.47.136", "port": 7709 }

// 成功（code=0）
{ "ok": true, "host": "123.60.47.136", "port": 7709, "kind": "mac", "latency_ms": 42, "error": null }

// 失败（**仍是 code=0 信封，HTTP 200**；fail-loud 体现在 error 字段）
{ "ok": false, "host": "203.0.113.9", "port": 7709, "kind": "std", "latency_ms": null,
  "error": "ConnectionRefusedError: connect refused" }
```

> E1/E2 语义（D-5-B，NF-20 硬约束）：**Kuantix 绝不照搬上游 Settings 页的"服务器切换"**
> （其写 `~/.easy_tdx/config.json`）。E1 纯读 config + known_hosts（读前后 sha256
> 指纹自证无写）；E2 只测不写：经 `TdxClientFactory` 显式 host/port 新建连接，connect
> 即 close，禁 from_best_host / save_best_*，不落盘任何 best host。如需修改 host，
> 引导用户改 Kuantix 自己的 config.toml（E1 只展示路径，不写）。

### 2.2 factor 路由（`api/routers/factor.py`，前缀 `/api/v1/factor`）—— 共 6 端点

| # | 方法 | 路径 | 请求体/参数 | 响应 data | 错误场景 |
|---|---|---|---|---|---|
| F1 | GET | `/api/v1/factor` | `market?` | `{items: [FactorInfo], page, page_size, total, total_pages}`（上游内置 + `factor/factors/` 自定义自动发现，NF-2） | — |
| F2 | POST | `/api/v1/factor/compute` | `ComputeRequest` | `Job`（action=compute） | 因子不存在→404；读 L1 失败→422；市场未启用→501 |
| F3 | GET | `/api/v1/factor/jobs/{job_id}` | — | `Job` | job 不存在→404 |
| F4 | GET | `/api/v1/factor/report` | `name`(必填), `start?`, `end?`, `market?` | `FactorReport`（IC/IR/分层/换手/自相关 + ic_series） | 因子不存在→404；无已计算数据→404；参数非法→400 |
| F5 | POST | `/api/v1/factor/combine` | `CombineRequest` | `FactorModel`（save_model=true 时持久化） | 因子缺失→404；method 非法→400；样本不足→422 |
| F6 | GET | `/api/v1/factor/models` | `market?` | `{items: [FactorModel], page, page_size, total, total_pages}`（已保存合成模型） | — |

请求体示例：

```json
// F2
{ "factors": ["momentum_20d", "pe_inv"], "market": "CN",
  "start": "2021-01-01", "end": "2025-12-31", "pool": "all" }
// F5
{ "factors": ["momentum_20d", "pe_inv"], "method": "ir",
  "save_model": true, "model_name": "m1", "market": "CN" }
```

### 2.3 screen 路由（`api/routers/screen.py`，前缀 `/api/v1/screen`）—— 共 6 端点

| # | 方法 | 路径 | 请求体/参数 | 响应 data | 错误场景 |
|---|---|---|---|---|---|
| S1 | GET | `/api/v1/screen/filters` | `market?` | `{items: [FilterInfo]}`（技术/缠论条件插件，NF-2） | — |
| S2 | POST | `/api/v1/screen/run` | `ScreenRunRequest` | `Job`（action=run） | 模型不存在→404；过滤器非法→400；市场未启用→501 |
| S3 | GET | `/api/v1/screen/jobs/{job_id}` | — | `Job`（done 时 result_summary={batch_id, result_count, excluded_count, as_of}，v1.1 见 §9 R1.1-1） | job 不存在→404 |
| S4 | GET | `/api/v1/screen/batches` | `market?`, `page?`, `page_size?` | `{items: [ScreenBatch], page, page_size, total, total_pages}` | — |
| S5 | GET | `/api/v1/screen/results` | `batch_id`(必填), `page?`, `page_size?`, `sort_by?`(默认 score), `order?`(asc/desc) | `{items: [ScreenResultView], page, page_size, total, total_pages}` | batch 不存在→404 |
| S6 | GET | `/api/v1/screen/results/{batch_id}/export` | `format=json\|csv`(默认 json), `market?` | json→信封；csv→文件（GBK + NF-22 免责头） | batch 不存在→404；format 非法→400 |

### 2.4 monitor 路由（`api/routers/monitor.py`，前缀 `/api/v1/monitor`）—— 共 17 端点（含 WS）

| # | 方法 | 路径 | 请求体/参数 | 响应 data | 错误场景 |
|---|---|---|---|---|---|
| M1 | POST | `/api/v1/monitor/start` | `market?` | `MonitorStatus`（running=true） | 无 watchlist→422；市场未启用→501 |
| M2 | POST | `/api/v1/monitor/stop` | `market?` | `MonitorStatus`（running=false） | — |
| M3 | GET | `/api/v1/monitor/status` | `market?` | `MonitorStatus`（轮询健康度） | — |
| M4 | GET | `/api/v1/monitor/watchlist` | `market?`, `page?`, `page_size?` | `{items: [WatchlistItem], ...分页}` | — |
| M5 | POST | `/api/v1/monitor/watchlist` | `{market, codes: ["600519",...], source?}` | `{added: [codes], skipped: [{code, reason}]}` | 代码非法→400；超上限(默认100)→422 |
| M6 | DELETE | `/api/v1/monitor/watchlist/{code}` | `market?` | `{removed: "600519"}` | 不存在→404 |
| M7 | GET | `/api/v1/monitor/criteria` | — | `{items: [CriterionInfo]}`（price/indicator/stop_loss；缠论 P1 占位） | — |
| M8 | GET | `/api/v1/monitor/rules` | `market?`, `page?`, `page_size?` | `{items: [Rule], ...分页}` | — |
| M9 | POST | `/api/v1/monitor/rules` | `RuleInput` | `Rule`（含生成 id） | 判据/参数非法→400 |
| M10 | PUT | `/api/v1/monitor/rules/{id}` | `RuleInput`（部分字段可省） | `Rule` | 不存在→404 |
| M11 | DELETE | `/api/v1/monitor/rules/{id}` | — | `{removed: id}` | 不存在→404 |
| M11.1 | GET | `/api/v1/monitor/presets` | — | `PresetStatus[]`（key/name/description/level/default_enabled/applied/enabled/rule_id） | — |
| M11.2 | POST | `/api/v1/monitor/presets/{preset_key}` | — | `Rule`（注入后规则，默认开启；已存在则幂等返回） | 未知 preset_key→404 |
| M11.3 | POST | `/api/v1/monitor/presets/{preset_key}/toggle` | — | `Rule`（已应用则切换 enabled；未应用则注入并开启） | 未知 preset_key→404 |
| M12 | GET | `/api/v1/monitor/positions` | `market?` | `{items: [PositionView], ...分页}`（含实时盈亏） | — |
| M13 | POST | `/api/v1/monitor/positions` | `PositionInput` | `PositionView` | 参数非法→400 |
| M14 | DELETE | `/api/v1/monitor/positions/{code}` | `market?` | `{removed: "600519"}` | 不存在→404 |
| M15 | GET | `/api/v1/monitor/alerts` | `market?`, `level?`(info/warning/critical), `page?`, `page_size?` | `{items: [Alert], ...分页}` | level 非法→400 |
| M16 | GET | `/api/v1/monitor/channels` | — | `{items: [ChannelInfo]}`（desktop/webhook；其余 P1 置灰） | — |

> M16 澄清（2026-08-01，属说明非契约变更）：`channels` 内容由
> `config.toml [monitor]` 决定——`desktop` 零配置可用；`webhook` 通道在
> `[monitor].webhook_url` **非空**时启用（告警 POST 到该 URL），空串 =
> 显式未配置（不列出、不伪造 URL）。

| M17 | WS | `/api/v1/monitor/ws` | `market?`（query） | 见 2.4.1 WS 消息协议 | 握手失败；市场未启用→关闭帧 |

#### 2.4.1 WebSocket 消息协议（M17）

- URL：`ws://{host}:{port}/api/v1/monitor/ws?market=CN`（T05 在 `api/server.py` 挂载，`MonitorLoop` 经 `EventBus.publish("alert", alert)` 推送给 WS 订阅者）。
- **每条帧都是合法 NF-9 信封**（JSON 文本，经同一 `Envelope`/`sanitize` 序列化，NF-12），用 `data.type` 区分消息类型；前端复用统一解析器。

| 方向 | data.type | data 载荷 | 说明 |
|---|---|---|---|
| S→C | `hello` | `{market, subscribed: ["alert"], server_ts}` | 连接建立后立即发送 |
| S→C | `snapshot` | `{alerts: [Alert, ...最近50条]}` | 可选：订阅成功后回放历史（前端可关闭） |
| S→C | `alert` | `{alert: Alert}` | 实时告警推送（MonitorLoop→EventBus→WS） |
| C→S | `ping` | `{}` | 客户端心跳（可选） |
| S→C | `pong` | `{server_ts}` | 响应 ping；服务端亦每 30s 主动 ping 保活 |
| S→C | `bye` | `{reason}` | 服务端主动关闭前发送（如市场未启用→501 语义） |

示例帧（alert）：

```json
{ "code": 0, "message": "ok",
  "data": { "type": "alert", "alert": { "id": "al_01JY...", "code": "600519", "market": "CN",
    "rule": "止损-成本-8%", "level": "critical", "message": "600519 跌破止损线（-8%）",
    "ts": "2026-08-01T14:52:11+08:00", "payload": { "last": 1545.6, "cost": 1680.0 } } },
  "meta": { "generated_at": "2026-08-01T14:52:11+08:00", "data_date": "2026-08-01",
            "market": "CN", "elapsed_ms": 0, "version": "0.1.0" } }
```

- 断线重连：前端指数退避（1s/2s/4s…上限 30s）；重连后先收 `hello` + `snapshot`（若开启），再续增量。
- CORS：WS 握手受 `cors_origins` 约束，前端 dev origin（localhost:5173）已在白名单；生产部署需同步更新。

---

## 3. 数据模型（DTO）

> 标 ⭐ 的 DTO 与 `core/contracts.py` 中 frozen dataclass 一一对应（`to_dict()` 输出即本 schema），API 层直接复用，**禁止**在 `api/schemas.py` 另起一套字段名。新增字段只在 API 层扩展。

### 3.1 通用

| 名称 | 字段 | 类型 | 单位/说明 | 示例 |
|---|---|---|---|---|
| Page | items | array | 元素数组 | — |
| | page | int | 页码(1起) | 1 |
| | page_size | int | 每页条数 | 20 |
| | total | int | 总数 | 213 |
| | total_pages | int | 总页数 | 11 |
| Job | job_id | string | 全局唯一 job id | `job_01JX...` |
| | module | string | `data`/`factor`/`screen` | `data` |
| | action | string | `sync_full`/`sync_incremental`/`compute`/`run` | `sync_full` |
| | status | string | `queued`/`running`/`done`/`failed`/`cancelled` | `running` |
| | market | string | 市场码 | `CN` |
| | progress | SyncProgress\|null | 进度快照 | — |
| | result_summary | object\|null | 完成摘要（按模块） | `{batch_id, result_count}` |
| | error | object\|null | `{code, message}` | null |
| | created_at / updated_at | string(ISO) | 创建/更新时刻 | — |

### 3.2 数据层

| 名称 | 字段 | 类型 | 单位/说明 | 示例 |
|---|---|---|---|---|
| SyncProgress ⭐ | total | int | 计划标的总数 | 5209 |
| | done | int | 成功数 | 1200 |
| | failed | int | 失败数 | 3 |
| | quarantined | int | 进隔离区数 | 2 |
| | current | string | 当前标的代码 | `sh600519` |
| | percent | float | 完成百分比 0–100（2位） | 23.13 |
| | started_at / updated_at | string(ISO) | 开始/最近更新 | — |
| DataLakeStatus | market | string | 市场码 | `CN` |
| | data_date | string\|null | 最新交易日 | `2026-08-01` |
| | coverage | object | `{securities, files, bars, disk_bytes, first_date, last_date}`；**v1.5 起合并 SQLite+镜像**（任一后端有数据即非空，`securities/files` 取两源较大值，`bars` 以 SQLite daily_bars 为准，首末日从 daily_bars 聚合补齐） | `{5209, 5209, 26045000, 4294967296, "2016-08-01", "2026-08-01"}` |
| | quarantine_count | int | 隔离区条目数 | 3 |
| | latest_job | Job\|null | 最近一次 sync job | — |
| | in_sync_window | bool | 是否处于交易时段（NF-28 提示） | false |
| | storage | object | 存储摘要（**v1.5 新增字段，旧字段不变只增**）：见下表 | — |
| | vipdoc_mirror | bool | 二进制镜像是否启用（[storage].vipdoc_mirror） | false |
| DataLakeStorage ⭐ | db_path | string | market.db 路径（旧字段） | `~/.Kuantix/db/market.db` |
| | backend | string | 主存储后端（旧字段） | `sqlite` |
| | securities / daily_bars / sync_checkpoint / sync_meta | int | 各表行数（旧字段，`summary()`） | — |
| | sqlite_bars | int | SQLite daily_bars 行数（新增） | 2 |
| | sqlite_securities | int | SQLite securities 表条数（新增） | 0 |
| | sqlite_codes | int | SQLite daily_bars 去重代码数（新增，securities 表可能为空） | 1 |
| | mirror_files | int | vipdoc 镜像 `.day` 文件数（新增，轻量扫描不解析） | 4730 |
| | mirror_disk_bytes | int | vipdoc 镜像磁盘字节数（新增） | 533000000 |
| | source | string | 存储状态四态（新增）：`empty` 都空 / `mirror_only` 仅镜像（未迁移）/ `sqlite` 仅主存储 / `both` 两者都有 | `mirror_only` |
| VerifyReport ⭐ | market | string | 市场码 | `CN` |
| | coverage | object | 同 DataLakeStatus.coverage（v1.5 起合并口径） | — |
| | storage | object | 同 DataLakeStatus.storage（**v1.5 新增**，区分镜像/SQLite 覆盖） | — |
| | missing_days | array[string] | 缺失交易日 `YYYY-MM-DD` | `["2026-02-03"]` |
| | corrupt | array[string] | 损坏文件（vipdoc 文件名） | `["sh600001.day"]` |
| | quarantined | array[QuarantineEntry] | 隔离区条目 | — |
| | excluded_count | int | = len(quarantined)，供前端提示 NF-27 | 3 |
| | generated_at | string(ISO) | 生成时刻 | — |
| QuarantineEntry ⭐ | code | string | 标的代码/文件名 | `600001` |
| | market | string | 市场码 | `CN` |
| | reason | string | 机器可读原因（见下） | `readback_mismatch` |
| | detail | string | 人类可读详情（含异常消息） | `回读价格差 0.012 > 容差 0.001` |
| | occurred_at / last_try | string(ISO) | 首次发生/最近重试 | — |
| | attempts | int | 累计尝试次数 | 2 |
| SecurityHit | code | string | 6 位证券代码 | `600000` |
| | name | string | 证券名称 | `浦发银行` |
| | exchange | string | 交易所前缀（sh/sz/bj/hk/us） | `sh` |
| | market | string | 市场码 | `CN` |
| | security_type | string | 上游证券类型（如 SH_A_STOCK） | `SH_A_STOCK` |

`QuarantineEntry.reason` 规范枚举：`unknown_security_type` / `readback_mismatch` / `fetch_failed` / `uint32_overflow` / `data_integrity` / `other`。

### 3.6 回测层（v1.2 增量）

| 名称 | 字段 | 类型 | 单位/说明 | 示例 |
|---|---|---|---|---|
| StrategySchema | name | string | 策略名（B1/B2 引用） | `ma_cross` |
| | label | string | 中文名 | `双均线交叉` |
| | description | string | 策略说明 | — |
| | params | array[ParamSchema] | 参数 schema（供前端表单渲染） | — |
| | preset_grid | object\|null | 寻优参数预设网格（v1.3 P1 声明；上游 `StrategyEntry.to_schema()` 已透传，`{param: [values]}`，供 Optimize 页 ParamGridPicker 消费） | `{"fast":[5,10,20]}` |
| ParamSchema | name | string | 参数名 | `fast` |
| | type | string | `int`/`float`/`bool`/`str` | `int` |
| | default | any | 默认值 | 5 |
| | label | string | 中文标签 | `快线周期` |
| | min_value / max_value | number\|null | 数值边界 | 1 / 60 |
| | choices | array[string]\|null | 可选值（str 型） | — |
| | description | string\|null | 参数说明 | — |
| BacktestRunRequest | market | string | 市场码（P0 仅 CN） | `CN` |
| | codes | array[string] | 标的池（6 位代码，1..20） | `["600000"]` |
| | strategy | string | 策略名 | `ma_cross` |
| | params | object | 策略参数（缺省取默认） | `{"fast":5}` |
| | start / end | string(date) | 回测区间 `YYYY-MM-DD` | `2024-01-01` |
| | cash | float | 初始资金 | 1000000 |
| | commission | float | 佣金费率（0.0003=0.03%） | 0.0003 |
| | min_commission | float | 单笔最低佣金 | 5.0 |
| | stamp_tax | float | 印花税（卖出） | 0.001 |
| | slippage | float | 滑点费率 | 0.0 |
| | execution | string | `next_open`/`next_close` | `next_open` |
| BacktestResult | strategy | string | 策略名 | `ma_cross` |
| | params | object | 实际参数 | — |
| | market | string | 市场码 | `CN` |
| | start_date / end_date | string(date) | 实际区间 | — |
| | config | object | `{cash, commission, min_commission, stamp_tax, slippage, execution}` | — |
| | codes | array[string] | 成功运行标的 | — |
| | skipped | array[{code, reason}] | 被跳过标的及原因（fail-loud 计数） | — |
| | per_code | object | `Record<code, PerCodeResult>` | — |
| | combined | object | 组合视图（见下） | — |
| PerCodeResult | performance | object | 上游 PerformanceAnalyzer 19 项指标（小数比例） | `{total_return: 0.12}` |
| | equity_curve | array[EquityPoint] | 净值序列（datetime/total/drawdown/drawdown_pct） | — |
| | trades | array[Trade] | 成交明细（datetime/direction/size/price/commission/slippage/pnl/cost_basis/rejected） | — |
| | positions | array | 持仓快照 | — |
| | config | object | 引擎配置 | — |
| | diagnostic | string\|null | 数据异常诊断 | — |
| CombinedResult | equity_curve | array[EquityPoint] | 组合等权净值（各标的归一化后均值） | — |
| | performance | object | 组合绩效（上游 PerformanceAnalyzer 计算） | — |
| | config | object | `{strategy, combine: "equal_weight"}` | — |

### 3.7 组合 / 策略库层（v1.3 增量）

| 名称 | 字段 | 类型 | 单位/说明 | 示例 |
|---|---|---|---|---|
| PortfolioRunRequest | market | string | 市场码（P0 仅 CN） | `CN` |
| | codes | array[string] | 组合标的池（6 位代码，1..20） | `["600519","000001"]` |
| | strategy | string | 单一策略名（组合=1策略×N标的） | `ma_cross` |
| | params | object | 策略参数 | `{"fast":5,"slow":20}` |
| | start / end | string(date) | 回测区间 `YYYY-MM-DD` | — |
| | cash | float | 组合总资金（按 N 均分） | 1000000 |
| | commission / min_commission / stamp_tax / slippage / execution | — | 成本配置（同 BacktestRunRequest） | — |
| PortfolioResult | total_performance | object | `{total_return, annual_return, total_stocks, total_cash}`（上游 Portfolio 口径；S5 多策略为完整 19 项 + total_stocks/total_cash） | — |
| | individual_results | object | `Record<code, PerCodeResult>`（P1 组合）/ `Record<"{label}@{symbol}", PerCodeResult>`（S5 多策略） | — |
| | equity_allocation | object | 每标的/槽位的资金分配比例（均分时各 1/N） | `{"600519":0.5}` |
| | combined_equity | array[{datetime, total, drawdown, drawdown_pct}] | 组合净值（金额求和，datetime 为 `YYYY-MM-DD`） | — |
| StrategyCreate | name | string | 名称（1..120） | `双均线-茅台` |
| | kind | string | `single`/`portfolio`/`multi` | `single` |
| | strategy | string | 策略名 | `ma_cross` |
| | strategy_label | string | 策略中文名 | `双均线交叉` |
| | params | object | 策略参数 | `{"fast":5,"slow":20}` |
| | context | object | 按 kind：single=`{symbol,start_date,end_date}`；portfolio=`{stocks[]}`；multi=`{items[]}` | `{"symbol":"SH:600519"}` |
| | trade_config | object | `{cash, commission, execution, ...}` | `{"cash":1000000}` |
| | snapshot | object | 保存时关键绩效（前端展示） | `{"total_return":0.26,"grade":"A"}` |
| | tags | array[string] | 标签 | `["优选"]` |
| | notes | string | 备注 | — |
| SavedStrategy | = StrategyCreate + `id` / `created_at` / `updated_at` / `app_version` | string | id 服务端生成 `strat_<uuid12>` | `strat_01J...` |
| MultiStrategyRunRequest | market | string | 市场码 | `CN` |
| | items | array[{strategy,label,code,params}] | N 个策略槽位（1..10） | — |
| | cash | float | 总资金（1/N 均分） | 1000000 |
| | commission / min_commission / stamp_tax / slippage / execution / start / end | — | 成本与区间配置（同 PortfolioRunRequest） | — |

### 3.8 寻优层 / 单标的 K 线增强层（v1.3 增量 P1）

| 名称 | 字段 | 类型 | 单位/说明 | 示例 |
|---|---|---|---|---|
| OptimizeRunRequest | market | string | 市场码（P1 仅 CN） | `CN` |
| | code | string | 单标的代码（6 位，非空） | `600519` |
| | strategy | string | 策略名 | `ma_cross` |
| | param_grid | object | 参数取值网格（1-2 个参数，笛卡尔积 ≤200） | `{"fast":[5,10,20]}` |
| | start / end | string(date) | 回测区间 `YYYY-MM-DD` | — |
| | cash | float | 初始资金（每网格点共用） | 1000000 |
| | commission / min_commission / stamp_tax / slippage / execution | — | 成本配置（同 BacktestRunRequest） | — |
| OptimizeGridPoint | params | object | 该点参数取值 | `{"fast":5,"slow":10}` |
| | total_return | float\|null | 总收益率（小数比例；NaN/Inf→null） | 0.32 |
| | sharpe | float\|null | 夏普比率 | 1.4 |
| | max_drawdown | float\|null | 最大回撤 | 0.21 |
| | total_trades | int | 总交易笔数 | 36 |
| | win_rate | float\|null | 胜率（0–1） | 0.5 |
| | profit_factor | float\|null | 盈亏比 | 1.8 |
| OptimizeResult | strategy | string | 策略名 | `ma_cross` |
| | param_names | array[string] | 寻优参数名（1-2 个，决定热力图维度） | `["fast","slow"]` |
| | results | array[OptimizeGridPoint] | 全部网格点，按 total_return 降序 | — |
| | best | OptimizeGridPoint\|null | 最优点（results[0]） | — |
| | heatmap | object\|null | 2 参数时 `{x_name, y_name, x, y, data}`（data=`[x_idx,y_idx,value]`）；1 参数/空时为 null | — |
| KlineBar | date | string(date) | 交易日 `YYYY-MM-DD` | `2024-01-02` |
| | open / high / low / close | float | OHLC（元） | 10.0 |
| | vol | float | 成交量（手，RD-8 口径） | 10000 |
| | amount | float | 成交额（元） | 1e7 |
| SignalPoint | date | string(date) | 信号日期 `YYYY-MM-DD` | `2024-01-29` |
| | price | float\|null | 信号价（成交价；NaN→null） | 13.25 |
| KlineWithSignals | code | string | 6 位代码 | `600519` |
| | market | string | 市场码 | `CN` |
| | start_date / end_date | string(date) | 实际区间 | — |
| | strategy | string | 买卖点标注所用策略 | `ma_cross` |
| | kline | array[KlineBar] | K 线数组（升序） | — |
| | buy_points | array[SignalPoint] | 买入**信号标注**（非下单动作，R5） | — |
| | sell_points | array[SignalPoint] | 卖出**信号标注**（非下单动作，R5） | — |

### 3.9 Settings 层（v1.3 增量 P2，**只读**数据源状态，NF-20）

| 名称 | 字段 | 类型 | 单位/说明 | 示例 |
|---|---|---|---|---|
| SettingsStatus | read_only | boolean | 整页只读声明（恒 true，无写操作能力） | `true` |
| | config | object | config 摘要（§2.1f：paths/default_market/enabled_markets/config_source/tdx） | — |
| | known_hosts | object | 节点清单（items + upstream_available + known_hosts_merged + upstream_config_untouched + upstream_config_path） | — |
| | data | object | 数据湖摘要（复用 D1 DataLakeStatus：coverage/latest_job） | — |
| | versions | object | `{Kuantix, upstream_easy_tdx}` | `{"0.1.0","1.20.3"}` |
| SettingsKnownHostItem | host | string | 服务器 IP/域名 | `123.60.47.136` |
| | port | int | 端口（std/mac=7709，mac_ex=7727） | 7709 |
| | kind | enum | `std` / `mac` / `mac_ex` | `mac` |
| | read_only | boolean | **只读展示**标注（恒 true） | `true` |
| TestConnectionRequest | kind | enum | `std` / `mac` / `mac_ex`（非法→400） | `mac` |
| | host | string | 服务器 IP/域名（显式，禁 from_best_host） | `123.60.47.136` |
| | port | int | 端口（1..65535，越界→400） | 7709 |
| TestConnectionResult | ok | boolean | 连接是否成功；`false` 是**业务结果**（code=0 信封，非 HTTP 错误） | `true` |
| | host / port / kind | — | 回显请求参数 | — |
| | latency_ms | int\|null | connect 握手耗时（毫秒）；失败为 null | 42 |
| | error | string\|null | 失败原因（fail-loud 明细）；成功为 null | `ConnectionRefusedError: ...` |

### 3.3 因子层

| 名称 | 字段 | 类型 | 单位/说明 | 示例 |
|---|---|---|---|---|
| FactorInfo | name | string | 因子名（注册名） | `momentum_20d` |
| | category | string | 分类（动量/价值/质量/波动/技术/量能/缠论/自定义） | `动量` |
| | display_name | string\|null | 展示名 | `20日动量` |
| | description | string | 自解释描述（NF-13） | `过去20日累计收益率` |
| | source | string | `builtin`/`custom`（目录自动发现） | `builtin` |
| | status | string | `computed`/`uncomputed`/`failed`（I-1 衰减态 P1 追加 `decaying`/`invalid`） | `computed` |
| | years | array[int] | 已计算年份 | `[2021,2022,2023,2024,2025]` |
| FactorReport | factor | string | 因子名 | `momentum_20d` |
| | market | string | 市场码 | `CN` |
| | start_date / end_date | string | 计算区间 `YYYY-MM-DD` | `2021-01-01` / `2025-12-31` |
| | sample_count | int | 因子值样本数（标的×交易日） | 125000 |
| | excluded_count | int | 因隔离区排除标的数（NF-27） | 0 |
| | ic_mean | float | IC 均值（比率） | 0.043 |
| | ic_std | float | IC 标准差 | 0.084 |
| | ir | float | IR = ic_mean / ic_std（上游口径） | 0.51 |
| | ic_positive_rate | float | IC 胜率（0–1） | 0.58 |
| | quantile_returns | array[float] | 分层平均收益 Q1..Q5（比率，层数=config.factor.quantiles） | `[0.021, 0.028, 0.031, 0.037, 0.052]` |
| | top_minus_bottom | float | 多空收益 = Q5−Q1（比率） | 0.031 |
| | turnover_rate | float | 分层调仓换手率（0–1） | 0.32 |
| | autocorr | float | IC 自相关 | 0.71 |
| | ic_series | array[object] | `[{date, ic}]` 时间序列（绘图：柱状+累计IC） | `[{"date":"2024-01-05","ic":0.031}]` |
| FactorModel | name | string | 模型名 | `m1` |
| | method | string | `equal`/`ic`/`ir` | `ir` |
| | weights | object | 因子→权重（和为1） | `{"momentum_20d":0.4,"pe_inv":0.35}` |
| | created_at | string(ISO) | 创建时刻 | — |

**IC/IR 口径（评审重点）**：IC 按上游 `FactorAnalyzer.compute_ic` 口径——截面秩相关，前瞻周期 = `config.factor.forward_period`（默认 5 交易日）；`ir = ic_mean / ic_std`；分层按 `config.factor.quantiles`（默认 5）等分，`quantile_returns[i]` = 第 i 层所有样本前瞻收益的均值（比率）。所有字段浮点 6 位（NF-12）。

### 3.4 选股层

| 名称 | 字段 | 类型 | 单位/说明 | 示例 |
|---|---|---|---|---|
| ScreenRunRequest | model | string | 因子模型名 | `m1` |
| | market | string | 市场码 | `CN` |
| | pool | string\|array | `all`/`watchlist`/代码数组（自动排除隔离区，NF-27） | `all` |
| | top_n | int | 输出前 N 名 | 20 |
| | filters | array[object] | `[{type: "tech"\|"chanlun", condition, params}]` | `[{"type":"tech","condition":"ma_cross","params":{"fast":20,"slow":60}}]` |
| | combine | string | `and`/`or`（条件组合） | `and` |
| | exclude_st / exclude_suspended / exclude_new | bool | 剔除 ST/停牌/次新 | true |
| | as_of | string\|null | 数据基准日，缺省=最新交易日 | `2026-08-01` |
| ScreenBatch | batch_id | string | 批次 id | `batch_01JZ...` |
| | market / model / top_n | — | 运行参数回显 | — |
| | filters / combine | — | 条件回显 | — |
| | status | string | `running`/`done`/`failed` | `done` |
| | result_count | int | 命中数 | 20 |
| | excluded_count | int | 本批次被过滤剔除的标的数（fail-loud 显式计数而非静默丢弃，NF-26/NF-27；v1.1 见 §9 R1.1-1） | 3 |
| | as_of | string | 数据基准日 | `2026-08-01` |
| | created_at | string(ISO) | 创建时刻 | — |
| | elapsed_ms | int | 耗时 | 192000 |
| ScreenResult ⭐ | code | string | 证券代码 | `600519` |
| | name | string | 证券名称 | `贵州茅台` |
| | market | string | 市场码 | `CN` |
| | score | float | 综合得分 | 92.4 |
| | sub_scores | object | 因子→分项得分 | `{"momentum_20d":88.0,"pe_inv":95.0}` |
| | conditions | string | 命中的过滤条件描述 | `MA金叉+二买` |
| | price | float | 打分时点收盘价（元） | 1680.0 |
| | as_of | string | 数据基准日 | `2026-08-01` |
| ScreenResultView | = ScreenResult + `rank` | int | 排名（1 起） | 1 |
| FilterInfo | type / condition | string | 插件标识 | `tech`/`ma_cross` |
| | display_name / description | string | 展示名/描述（NF-13） | — |
| | params_schema | object | 参数 JSON Schema（供前端表单） | — |

**CSV 导出格式（同花顺，GBK）**：

```
# Kuantix 选股结果 2026-08-01 · 仅供人工核对参考，非自动交易指令 (NF-22)
代码,名称,最新价,综合得分,触发条件,数据日期
600519,贵州茅台,1680.00,92.40,"MA金叉+二买",2026-08-01
600000,浦发银行,11.20,88.10,"MA金叉",2026-08-01
```

文件名：`screen_{batch_id}.csv`（JSON 导出为 `screen_{batch_id}.json`）。

### 3.5 监控层

| 名称 | 字段 | 类型 | 单位/说明 | 示例 |
|---|---|---|---|---|
| MonitorStatus | running | bool | 循环是否运行 | true |
| | started_at | string(ISO)\|null | 启动时刻 | — |
| | poll_interval_seconds | float | 轮询间隔（config） | 5.0 |
| | trading_hours_only | bool | 仅交易时段轮询 | true |
| | in_trading_session | bool | 当前是否交易时段（MarketProfile） | true |
| | last_poll_at | string(ISO)\|null | 最近轮询时刻 | — |
| | last_poll_ok | bool\|null | 最近轮询是否成功 | true |
| | consecutive_errors | int | 连续失败次数（健康度：0绿/>0黄/≥3红） | 0 |
| | watchlist_count / rules_enabled_count | int | 监控数/启用规则数 | 6 / 11 |
| | channels | array[ChannelInfo] | 推送通道状态 | — |
| WatchlistItem | code / name / market | string | 标的 | `600519`/`贵州茅台`/`CN` |
| | added_at | string(ISO) | 加入时刻 | — |
| | source | string | `manual`/`screen`/`cli` | `manual` |
| Rule | id | string | 规则 id | `rule_01K...` |
| | name | string | 规则名 | `止损-成本-8%` |
| | scope | object | `{market, codes: ["*"]\|[...]}`（`*`=全部） | `{"market":"CN","codes":["600519"]}` |
| | criterion_type | string | `price`/`indicator`/`stop_loss`（缠论 P1） | `stop_loss` |
| | params | object | 判据参数（见下） | `{"base":"cost","pct":0.08}` |
| | level | string | `info`/`warning`/`critical` | `critical` |
| | cooldown_seconds | float | 冷却期（config 默认 300） | 300.0 |
| | enabled | bool | 是否启用 | true |
| | source | string | 规则来源：`manual`（用户自建）/`preset`（预设注入） | `preset` |
| | preset_key | string\|null | 预设规则的 key（仅 `source=preset` 时非空；见 M11.1） | `limit_up` |
| | created_at / updated_at / last_triggered_at | string(ISO)\|null | 时间戳 | — |
| PresetStatus | key | string | 预设唯一 key | `limit_up` |
| | name / description | string | 展示名/描述 | `股票涨停` |
| | criterion_type | string | 判据类型（`change_pct`/`volume`/`indicator`/...，插件式可扩展） | `change_pct` |
| | params | object | 判据参数 | `{"op":"above","threshold":0.095}` |
| | level | string | 默认告警级别（`info`/`warning`/`critical`） | `warning` |
| | default_enabled | bool | 是否默认开启（统一为 `true`） | true |
| | applied | bool | 是否已注入为真实规则 | true |
| | enabled | bool\|null | 当前是否启用（未注入为 `null`） | true |
| | rule_id | string\|null | 已注入规则 id（未注入为 `null`） | `rule_preset_limit_up` |
| CriterionInfo | type | string | 判据类型 | `price` |
| | display_name / description | string | 展示名/描述 | — |
| | params_schema | object | 参数 Schema | — |
| PositionInput | code / market | string | 标的 | `600519`/`CN` |
| | shares | float | 持仓数量（**股**，非手） | 100 |
| | cost_price | float | 成本价（元） | 1680.0 |
| | opened_at | string\|null | 建仓日期 `YYYY-MM-DD` | `2026-01-05` |
| PositionView | code / name / market | string | 标的 | — |
| | shares / cost_price | float | 数量（股）/成本价（元） | 100 / 1680.0 |
| | last | float | 最新价（元） | 1545.6 |
| | change_pct | float | 当日涨跌幅（**比例**，0.05=5%） | -0.08 |
| | market_value | float | 市值=shares×last（元） | 154560.0 |
| | pnl | float | 浮动盈亏=(last−cost)×shares（元） | -13440.0 |
| | pnl_pct | float | 盈亏比例=pnl/(cost×shares)（**比例**） | -0.08 |
| | as_of | string | 数据基准日 | `2026-08-01` |
| Alert ⭐ | id | string | 告警 id | `al_01JY...` |
| | code / market | string | 标的/市场 | `600519`/`CN` |
| | rule | string | 触发规则名 | `止损-成本-8%` |
| | level | string | `info`/`warning`/`critical` | `critical` |
| | message | string | 告警正文 | `600519 跌破止损线（-8%）` |
| | ts | string(ISO) | 触发时刻 | — |
| | payload | object | 求值上下文快照（复盘） | `{"last":1545.6,"cost":1680.0}` |
| ChannelInfo | name | string | 通道插件名 | `desktop` |
| | display_name | string | 展示名 | `桌面通知` |
| | enabled | bool | 配置是否启用 | true |
| | healthy | bool\|null | 最近投递是否成功 | true |

`Rule.params` 按 criterion_type：

```json
// price（价格阈值）
{ "op": "above" | "below", "threshold": 1600.0 }
// indicator（技术指标，复用 MyTT）
{ "indicator": "ma" | "macd" | "rsi", "op": "cross_above" | "cross_below" | "gt" | "lt",
  "value": 70.0, "period": 14 }
// stop_loss（回撤止损）
{ "base": "cost" | "peak", "pct": 0.08 }   // 相对成本价或区间最高价回撤 pct
```

---

## 4. CLI ↔ REST 映射表（NF-10 双入口对等）

> 规则：CLI `--json` 输出 = 对应 REST 端点响应体（同一 schema，同一 payload 构造函数）。触发类命令（sync/compute/run）输出 Job 信封，进度/结果通过 status/report/results 等命令获取。**CLI 现有骨架见 `cli.py`；标注「T05 补全」的命令需在 T05 一并落地以满足 NF-10**。

| CLI 命令 | REST 端点 | 说明 |
|---|---|---|
| `Kuantix data sync --market CN --years 10` | `POST /api/v1/data/sync` | --json 输出 = Job 信封；进度用 `data status`/轮询 D3 |
| `Kuantix data status` | `GET /api/v1/data/status` | 数据湖状态 + 最新进度 |
| `Kuantix data verify --market CN` | `GET /api/v1/data/verify` | verify 报告（含隔离区） |
| `Kuantix data quarantine` | `GET /api/v1/data/quarantine` | 隔离区清单 |
| `Kuantix data quarantine remove <code>`（T05 补全） | `DELETE /api/v1/data/quarantine/{code}` | 移除隔离区 |
| `Kuantix factor list` | `GET /api/v1/factor` | 因子库（含自定义自动发现） |
| `Kuantix factor compute --name momentum_20d` | `POST /api/v1/factor/compute` | Job；--name 空=全部 |
| `Kuantix factor report --name momentum_20d` | `GET /api/v1/factor/report?name=momentum_20d` | IC/IR/分层/换手 |
| `Kuantix factor combine --factors ... --method ir --save-model m1` | `POST /api/v1/factor/combine` | 合成 + 可选保存模型 |
| `Kuantix factor models`（T05 补全） | `GET /api/v1/factor/models` | 模型列表（F5 持久化后可见） |
| `Kuantix screen run --model m1 --top 20` | `POST /api/v1/screen/run` | Job；结果用 `screen results` |
| `Kuantix screen list` | `GET /api/v1/screen/filters` | 条件插件清单 |
| `Kuantix screen results --batch <id>`（T05 补全） | `GET /api/v1/screen/results?batch_id=...` | 分页结果 |
| `Kuantix screen export --batch <id> --format csv`（T05 补全） | `GET /api/v1/screen/results/{batch_id}/export?format=csv` | 同花顺 CSV（GBK + 免责头） |
| `Kuantix monitor start` | `POST /api/v1/monitor/start` | 启动监控 |
| `Kuantix monitor status` | `GET /api/v1/monitor/status` | 监控状态 |
| `Kuantix monitor rules` | `GET /api/v1/monitor/rules` | 规则列表 |
| `Kuantix monitor watchlist add/remove`（T05 补全） | `POST/DELETE /api/v1/monitor/watchlist...` | 自选增删 |
| `Kuantix monitor alerts --level warning`（T05 补全） | `GET /api/v1/monitor/alerts?level=warning` | 告警历史 |
| `Kuantix monitor positions`（T05 补全） | `GET /api/v1/monitor/positions` | 持仓盈亏 |
| `Kuantix monitor channels`（T05 补全） | `GET /api/v1/monitor/channels` | 推送通道状态 |

---

## 5. 前端页面 ↔ 端点对照

前端策略总则：**实时告警走 WS，其余走轮询 + 交互触发**；所有页面顶部数据湖指示器/市场切换器共享基础设施端点。

### 5.1 全局（顶栏）

| UI 元素 | 端点 | 刷新策略 |
|---|---|---|
| 市场切换器（A股可用/港美股置灰） | `GET /api/version` + `GET /health`（markets_enabled） | 进入页面 + 手动 |
| 数据湖状态指示器（最新数据日期 + 同步按钮） | `GET /api/v1/data/status`；`POST /api/v1/data/sync`；`GET /api/v1/data/sync/{job_id}` | 30s 轮询 + 同步后轮询进度 1–2s |

### 5.2 因子分析页 `/factors`

| UI 区块 | 端点 | 刷新策略 |
|---|---|---|
| 因子库树（搜索/分类/状态点） | `GET /api/v1/factor` | 进入页面 + 计算完成后刷新 |
| [计算] 弹窗 → 顶部进度条 | `POST /api/v1/factor/compute` → `GET /api/v1/factor/jobs/{job_id}` | 触发 + 轮询 1–2s |
| 指标卡（IC均值/IR/胜率/换手/多空/自相关） | `GET /api/v1/factor/report?name=...` | 选中因子后拉取 + 手动刷新 |
| IC 时间序列 / 分层收益 / 衰减曲线（ECharts） | 同 report（ic_series/quantile_returns/autocorr） | 同 report |
| 合成区（等权/IC/IR + 模型名） | `POST /api/v1/factor/combine` | 交互触发 |
| 模型下拉 | `GET /api/v1/factor/models` | 进入页面 + 保存后刷新 |
| 各区块 `[导出JSON]` | 下载对应 GET 响应（report/list 均已是信封 JSON） | 点击下载 |

### 5.3 监控看板页 `/monitor`

| UI 区块 | 端点 | 刷新策略 |
|---|---|---|
| 顶部状态灯/运行时间 | `GET /api/v1/monitor/status` | 轮询 5s（与 poll_interval 对齐） |
| 持仓/监控列表（现价/盈亏） | `GET /api/v1/monitor/positions`、`GET /api/v1/monitor/watchlist` | 轮询 5s |
| 持仓增删 / 监控增删 | `POST/DELETE /api/v1/monitor/positions...`、`POST/DELETE /api/v1/monitor/watchlist...` | 交互触发 |
| 实时告警流 | `WS /api/v1/monitor/ws` | **常驻 WS**，断线指数退避重连；历史用 alerts |
| 告警历史（分页/过滤） | `GET /api/v1/monitor/alerts?level=&page=` | 进入页面拉最近一页 + 分页操作 |
| 预警规则列表/新建/启停/删除 | `GET/POST/PUT/DELETE /api/v1/monitor/rules...`；判据下拉用 `GET /api/v1/monitor/criteria` | 进入页面 + 变更后 |
| 推送通道状态 | `GET /api/v1/monitor/channels` | 进入页面 + 变更后 |
| `[导出JSON]` | 下载 alerts/positions/watchlist 对应 GET 响应 | 点击下载 |

### 5.4 选股结果页 `/screen`

| UI 区块 | 端点 | 刷新策略 |
|---|---|---|
| 条件配置（模型/池/过滤/TopN） | 模型 `GET /api/v1/factor/models`；条件 `GET /api/v1/screen/filters` | 进入页面 |
| [立即执行] → 执行状态 | `POST /api/v1/screen/run` → `GET /api/v1/screen/jobs/{job_id}` | 触发 + 轮询 1–2s |
| 结果表（任意列排序） | `GET /api/v1/screen/results?batch_id=&sort_by=&order=` | batch done 后拉取 + 排序刷新 |
| 历史批次（回看/对比） | `GET /api/v1/screen/batches` | 进入页面 + 执行后 |
| `[导出JSON]` / `[导出CSV]` | `GET /api/v1/screen/results/{batch_id}/export?format=json\|csv` | 点击下载（CSV 为文件） |

### 5.5 选股回测页 `/backtest`（v1.2 增量）

| UI 区块 | 端点 | 刷新策略 |
|---|---|---|
| 标的池搜索（代码/名称下拉确认） | `GET /api/v1/data/search?q=`（D8） | 输入防抖 300ms |
| 策略下拉 + 参数表单 | `GET /api/v1/backtest/strategies`（B1） | 进入页面 |
| [开始回测] → 进度 | `POST /api/v1/backtest/run`（B2）→ `GET /api/v1/backtest/jobs/{job_id}`（B3） | 触发 + 轮询 1s |
| 组合净值曲线/回撤 + 指标卡 + 逐标的绩效/成交明细 | `GET /api/v1/backtest/results/{job_id}`（B4） | done 后拉取 |
| `[导出JSON]` | B4 响应（完整结果） | 点击下载 |

### 5.6 参数寻优页 `/optimize` + 结果对比页 `/compare`（v1.3 增量 P1）

| UI 区块 | 端点 | 刷新策略 |
|---|---|---|
| 策略下拉 + 参数网格（ParamGridPicker，消费 preset_grid） | `GET /api/v1/backtest/strategies`（B1） | 进入页面 |
| [开始寻优] → 进度 | `POST /api/v1/optimize/run`（O1）→ `GET /api/v1/optimize/jobs/{job_id}`（O2） | 触发 + 轮询 1s |
| 寻优结果（参数排名表 + 热力图） | `GET /api/v1/optimize/results/{job_id}`（O3） | done 后拉取 |
| Compare 任务卡片列表（默认全状态，前端过滤 done） | `GET /api/v1/backtest/jobs`（C1） | 进入页面 + 轮询 2s |
| 单标的 K 线下钻 + 买卖点标注叠加 | `GET /api/v1/backtest/kline/{code}`（B5） | 点击代码下钻 |

### 5.7 服务器设置页 `/settings`（v1.3 增量 P2，**只读**，NF-20）

| UI 区块 | 端点 | 刷新策略 |
|---|---|---|
| 数据源状态卡片（数据路径/默认市场/端口/版本） | `GET /api/v1/settings/status`（E1） | 进入页面 + 手动刷新 |
| known_hosts 只读表格（host/port/kind + 只读标注） | `GET /api/v1/settings/status`（E1） | 同上 |
| 数据湖覆盖率卡片（securities/files/bars） | `GET /api/v1/settings/status`（E1） | 同上（同步按钮在顶栏，本页仅只读） |
| 连通性测试表单（host/port/kind + [测试]） | `POST /api/v1/settings/test-connection`（E2） | 点击触发（2s 超时） |
| [导出JSON] | `GET /api/v1/settings/status`（E1）信封 JSON | — |

> 本页**无任何写操作 UI**：不提供「切换服务器 / 保存配置」（上游该能力会写
> `~/.easy_tdx/config.json`，Kuantix 不照搬，D-5-B）；配置修改引导用户编辑
> Kuantix 自己的 config.toml（页面只展示路径）。

---

## 6. 版本与扩展

### 6.1 契约版本与变更规则

- 本契约版本：**v1**（`/api/v1/` 前缀即版本标识）。
- **向后兼容规则（只增不改）**：
  - ✅ 允许：新增端点、新增可选请求参数、新增响应字段（客户端须容忍未知字段）、新增枚举值（客户端须显式处理未知枚举而非崩溃）。
  - ❌ 禁止：修改既有字段类型/单位/语义、删除字段、改变 `code` 语义、顶层信封加键（envelope_validator 会拒绝）。
  - 破坏性变更必须升 v2（新前缀 `/api/v2/`）并经 team 评审；v1 与 v2 可并存过渡。
- 枚举扩展举例：`QuarantineEntry.reason`、`Alert.level`、`Rule.criterion_type` 未来可增，前端按未知值 fail-loud 处理（不静默忽略）。

### 6.2 上游 easy_tdx 边界（/tdx 挂载点）

- Kuantix 作为宿主 FastAPI，将上游 easy_tdx 应用 `mount('/tdx', easy_tdx_app)`（方案见 `01-源码勘察报告.md` §③扩展点5 / §④推荐方案 B，需手动驱动子应用 lifespan）。
- 路径边界：

| 前缀 | 归属 | 说明 |
|---|---|---|
| `/api/v1/*` | **Kuantix 自身** | 本契约全部业务端点（data/factor/screen/monitor） |
| `/health`、`/api/version`、`/docs`、`/openapi.json` | Kuantix | 基础设施（已实现） |
| `/tdx/api/v1/*` | 上游 easy_tdx | 上游 17 个路由（backtest/strategies、optimize、ws/realtime 等），**只读使用、不改路由** |
| `/tdx/` | 上游 SPA | 上游 web-ui 图表页；前端仅**新窗口外链**（只读不侵入，PRD §6.3） |

- 约束：Kuantix 前端三页面**只消费 Kuantix `/api/v1/*`**，不直连 `/tdx/api/v1/*`（Q7 决策）；`/tdx` 子树内不得挂 Kuantix 路由（NF-1，避免破坏上游路由语义）。

---

## 7. 自检结论（T05 验收覆盖）

| T05 验收项 | 覆盖情况 |
|---|---|
| `Kuantix serve` 起三页面 + OpenAPI | 三页面端点齐全（§5）；`/docs`、`/openapi.json` 已由 `main.py:create_app` 提供 |
| 各页 `[导出JSON]` 可用 | factor report/list、screen results、monitor alerts/positions/watchlist、data status/verify 全部返回信封 JSON，前端可直接下载（§1.10） |
| NF-21 无下单接口 | 契约全表**无任何 order/place/trade/委托/资金划转端点**；唯一写类导出为 CSV 纯文本产物 + NF-22 免责标注（§2.3 S6、§3.4） |
| 与 NF-9 信封一致性 | 所有 JSON 端点（含错误响应、WS 帧）均走 `Envelope`；CSV 为显式标注的**非 JSON 例外**；全部输出须过 `tests/redlines/envelope_validator.py` |
| market 贯穿（NF-6） | 每个业务端点带 market 参数，`meta.market` 回显；HK/US → 501（NF-7/NF-26） |
| 隔离区可见（NF-27） | verify/quarantine/status 端点 + factor report 的 `excluded_count` + screen batch 的 `excluded_count`（R1.1-1）提示 |

**端点统计（业务路由模块口径，唯一口径）**：

| 模块 | 数量 | 明细 |
|---|---|---|
| data | 8 | D1–D8 |
| factor | 6 | F1–F6 |
| screen | 6 | S1–S6 |
| monitor | 17 | M1–M17（M1–M16 为 REST，M17 为 WebSocket） |
| backtest | 6 | B1–B5 + C1（v1.2/v1.3 增量） |
| portfolio | 3 | P1–P3（v1.3 增量） |
| strategies | 5 | S1–S5（v1.3 增量） |
| optimize | 3 | O1–O3（v1.3 增量 P1） |
| settings | 2 | E1–E2（v1.3 增量 P2，**只读**，NF-20） |
| **业务合计** | **56** | REST 55 + WS 1 |
| 基础设施 | 4 | `/health`、`/api/version`、`/docs`、`/openapi.json` |
| **总计可访问端点** | **60** | — |

> 口径说明：按 OpenAPI 路径去重统计（同一路径的 GET/POST 合并计数）；v1.3 P1
> 新增 backtest C1/B5 与 optimize O1–O3 后由 49 更新为 54；v1.3 P2 新增
> settings E1–E2 后更新为 56。

---

## 8. 前后端最容易对不齐的 3 个点（评审重点）

1. **端口**：口述"默认 8000" vs 实际 `config.toml [server].port = 8899`。契约裁定：端口**只认 config**（环境变量可覆盖），前后端禁止硬编码；前端经 `/api/version` 读取或构建期注入。后端若改默认端口，必须同步改 `config.toml` 与 `config.default.toml`，并知会前端。
2. **路由前缀 `/api/v1`**：`main.py` 顶部注释残留 `/api/data`、`/api/factor` 等旧口径；本契约锁定 **`/api/v1/*`**（与 `system_design.md` §2 文件清单注释一致）。T05 挂载 router 时必须统一 `prefix="/api/v1"`，避免出现"文档 `/api/v1/factor`、代码 `/api/factor`"两头猜。
3. **数值与时间口径**：
   - 比例字段（`change_pct`、`pnl_pct`、`ic_positive_rate`、`turnover_rate`、`quantile_returns`、`top_minus_bottom`）一律**小数比例**（`0.05` = 5%），前端展示 ×100 加 %；禁止后端返回"5.0"式百分比。
   - 浮点**最多 6 位**（NF-12），NaN/Inf → null。
   - 日期 `YYYY-MM-DD`（**不是** vipdoc 的 `YYYYMMDD` 整数）；时间戳 ISO-8601 带时区。前端不得把 `data_date` 当时间戳解析，后端不得把 `YYYYMMDD` 泄漏到 API。

---

## 附录 A：完整响应示例

`GET /api/v1/factor/report?name=momentum_20d`：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "factor": "momentum_20d",
    "market": "CN",
    "start_date": "2021-01-01",
    "end_date": "2025-12-31",
    "sample_count": 125000,
    "excluded_count": 0,
    "ic_mean": 0.043,
    "ic_std": 0.084,
    "ir": 0.511905,
    "ic_positive_rate": 0.58,
    "quantile_returns": [0.021, 0.028, 0.031, 0.037, 0.052],
    "top_minus_bottom": 0.031,
    "turnover_rate": 0.32,
    "autocorr": 0.71,
    "ic_series": [ { "date": "2024-01-05", "ic": 0.031 } ]
  },
  "meta": {
    "generated_at": "2026-08-01T15:02:31+08:00",
    "data_date": "2025-12-31",
    "market": "CN",
    "elapsed_ms": 312,
    "version": "0.1.0"
  }
}
```

`GET /api/v1/data/verify`（隔离区可见，NF-27）：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "market": "CN",
    "coverage": { "securities": 5209, "files": 5206, "bars": 26045000,
                  "disk_bytes": 4294967296, "first_date": "2016-08-01", "last_date": "2026-08-01" },
    "missing_days": ["2026-02-03"],
    "corrupt": ["sh600001.day"],
    "quarantined": [
      { "code": "600001", "market": "CN", "reason": "readback_mismatch",
        "detail": "回读价格差 0.012 > 容差 0.001",
        "occurred_at": "2026-08-01T09:05:31+08:00", "last_try": "2026-08-01T09:06:00+08:00",
        "attempts": 2 }
    ],
    "excluded_count": 1,
    "generated_at": "2026-08-01T15:05:00+08:00",
    "storage": { "db_path": "~/.Kuantix/db/market.db", "backend": "sqlite",
                 "securities": 0, "daily_bars": 2, "sync_checkpoint": 0, "sync_meta": 0,
                 "sqlite_bars": 2, "sqlite_securities": 0, "sqlite_codes": 1,
                 "mirror_files": 4730, "mirror_disk_bytes": 533000000,
                 "source": "both" }
  },
  "meta": { "generated_at": "2026-08-01T15:05:00+08:00", "data_date": "2026-08-01",
            "market": "CN", "elapsed_ms": 8421, "version": "0.1.0" }
}
```

错误示例（HK 未启用）：

```json
{ "code": 501, "message": "[fail-loud/NF-7] HK 市场尚未实现（P1）。接口先行、拒绝静默降级：不要用 A 股规则代替 HK。",
  "data": { "error_type": "NotSupportedError", "path": "/api/v1/data/status" },
  "meta": { "generated_at": "2026-08-01T15:06:00+08:00", "data_date": null,
            "market": "HK", "elapsed_ms": 1, "version": "0.1.0" } }
```

---

## 9. 契约 v1.1 修订记录（2026-08-01）

> 修订背景：前端 `web/` 已按 v1.0 契约实现（46 文件，构建通过），提出 6 条契约歧义；team-lead 逐条裁决。本节记录需落进契约的 3 条修订；其余 3 条（CSV 编码实现细节、WS 客户端 ping 信封一致性、`VITE_USE_MOCK` 门面）裁决为无需修订。
> 性质：**纯增量、向后兼容、v1.1 语义**；v1.0 客户端可安全忽略新增字段/形状变化（前端已做双形态兼容）。

### R1.1-1 — ScreenBatch 新增 `excluded_count` 字段（采纳）

- **内容**：
  - `ScreenBatch` DTO 新增 `excluded_count: int` —— 本批次被过滤剔除的标的数（§3.4 已落字段）。
  - `screen run` job 完成时 `result_summary` 同步携带：`{batch_id, result_count, excluded_count, as_of}`（§2.3 S3 已更新）。
  - §7 自检表措辞补全：factor report 与 screen batch 均提供 `excluded_count` 提示。
- **语义**：fail-loud（NF-26/NF-27）——UNKNOWN 类型、数据异常等被显式剔除的标的**显式计数**而非静默丢弃；前端据此提示"本次结果已排除 N 只标的"。
- **兼容性**：纯增量（新增字段），v1.0 客户端忽略即可。

### R1.1-2 — `/health` `markets_enabled` 锁定为对象形状（采纳）

- **内容**：§2.0 明确 `GET /health` 的 `markets_enabled` 为**对象** `Record<market_code, bool>`，示例 `{"CN": true, "HK": false, "US": false}`；`true` 表示该市场在 `config.toml [markets].*_enabled` 中启用。
- **⚠️ 后端注意**：T01 `main.py` 当前实现返回**列表**（`["CN"]`），与 v1.1 不一致；前端已做双形态兼容，但**契约以 v1.1 对象形状为唯一口径，后端 T05 必须改为对象形状**（含 `/health` 所在 `main.py`）。
- **兼容性**：形状收敛（list → object）；前端双形态兼容期内无破坏，最终以对象为准。

### R1.1-3 — 因子衰减曲线不补字段（裁决维持现状）

- **内容**：v1 **不新增**衰减序列字段；`FactorReport.autocorr` 单值作为"衰减参考"（前端已用"IC 自相关（衰减参考）"指标卡呈现）。
- **理由**：因子衰减监控（PRD I-1）为 P1 创新项；P1 实现 I-1 时随 FactorReport v2 扩展（如 `decay_series`、滚动 IC 窗口等），届时按 §6.1"只增不改"规则升版。
- **兼容性**：无字段变更，v1.0/v1.1 完全兼容。

---

## 10. 契约 v1.2 增量修订记录（2026-08-02）

> 修订背景：用户反馈 4 个增量问题（选股回测缺失 / 因子库数量不足 / 自选股刷新重置 / 搜索只支持代码），
> 本次按「v1 只增不改」规则追加增量端点与 DTO。性质：**纯增量、向后兼容**；v1.0/v1.1 客户端可安全忽略。

### R1.2-1 — D8 证券搜索端点（新增）

- **内容**：`GET /api/v1/data/search?q=&market=&limit=`（§2.1 D8）。
  - `q` 支持代码（精确/前缀）与名称（模糊）；返回 `{items: [SecurityHit], count}`；
  - 无匹配 → 显式空数组；`q` 空 → 400；清单源不可用 → 422（fail-loud）；
  - 数据源：`UniverseEnumerator` 证券清单本地缓存（`~/.Kuantix/db/security_catalog.json`）。
- **DTO**：§3.2 新增 `SecurityHit`（code/name/exchange/market/security_type）。
- **兼容性**：纯新增端点 + 新增 DTO，v1.0/v1.1 无影响。

### R1.2-2 — backtest 回测端点（新增）

- **内容**：新增 `/api/v1/backtest/*` 四个端点（§2.1b B1–B4）：
  - `GET /backtest/strategies` —— 上游预置策略列表 + 参数 schema（19 个）；
  - `POST /backtest/run` —— 触发回测（Job 信封，后台执行；标的池/时间/策略/资金/费用）；
  - `GET /backtest/jobs/{job_id}` —— 进度；
  - `GET /backtest/results/{job_id}` —— 完整结果（净值序列/回撤/绩效指标/成交明细 + 组合视图）。
- **DTO**：§3.6 新增 `StrategySchema` / `ParamSchema` / `BacktestRunRequest` / `BacktestResult` / `PerCodeResult` / `CombinedResult`。
- **运行引擎**：经 `Kuantix/adapters/backtest_bridge.py` 调上游 `BacktestEngine` / `PerformanceAnalyzer`（薄包装，NF-1/R2）；
  组合视图 = 各标的归一化净值等权平均（聚合已算结果，绩效仍由上游计算）。
- **Job 模块**：`Job.module` 增补 `backtest`（§3.1 通用表）。
- **兼容性**：纯新增，v1.0/v1.1 无影响。

### R1.2-3 — 因子库数量口径（澄清，非契约变更）

- 上游 `FACTORY_REGISTRY` 实测 **19 个**注册因子（chanlun 2 / momentum 3 / quality 3 / technical 3 /
  value 2 / volatility 3 / volume 3），非早期估计的 ~45；
- Kuantix `GET /api/v1/factor` 已全量暴露 **21 个**（19 上游 + `volume_ratio_5d` + `close_position_20d` 自定义）；
- 前端 mock 因子库此前只有 9 个且含 6 个编造名字，已改为与后端一致的 21 个（本次前端修复，契约无字段变更）。

### R1.2-4 — 自选股持久化（澄清，非契约变更）

- 真接口模式：`MonitorStore` SQLite 已持久化（`~/.Kuantix/db/monitor.db` 表 `watchlist`），无 bug；
- mock 模式（默认 `VITE_USE_MOCK=true`）：此前为内存态数组，刷新重置；本次给 mock 的
  watchlist/positions/rules 加 **localStorage 持久化**（模拟持久化语义）。契约端点不变。

---

## 11. 契约 v1.3 增量修订记录（2026-08-02，P0 + P1 部分）

> 修订背景：功能对齐用户 3 点需求（组合回测 / 策略库），按「v1 只增不改」规则
> 追加 P1–P3（组合回测）、S1–S5（策略库 + 多策略组合回测）端点与 DTO。
> 性质：**纯增量、向后兼容**；v1.0/v1.1/v1.2 客户端可安全忽略。
> 引擎语义：组合/多策略均为**资金分仓 + 金额求和**（D-8），引擎调用收敛在
> `Kuantix/adapters/backtest_bridge.py`（R2）；K 线一律读本地 vipdoc（零网络）。

### R1.3-1 — 组合回测端点（新增，P0）

- **内容**：新增 `/api/v1/portfolio/*` 三个端点（§2.1c）：
  - `POST /portfolio/run` —— 触发组合回测（1 策略 × N 标的，资金分仓）→ Job；
  - `GET /portfolio/jobs/{job_id}` —— 进度（与 B3 同逻辑，薄转发）；
  - `GET /portfolio/results/{job_id}` —— 完整结果（PortfolioResult）。
- **DTO**：§3.7 新增 `PortfolioRunRequest` / `PortfolioResult`。
- **引擎**：`BacktestBridge.run_portfolio_backtest` 直调上游
  `PortfolioBacktestEngine`（金额求和原生语义）；`individual_results` /
  `equity_allocation` key 归一化为 6 位代码。
- **Job**：`Job.module=backtest`，`action=portfolio`。

### R1.3-2 — 策略库端点（新增，P0）

- **内容**：新增 `/api/v1/strategies/*` 五个端点（§2.1d）：
  - `GET /strategies` —— 列表（分页，kind 过滤）；
  - `POST /strategies` —— 保存策略（201 + SavedStrategy）；
  - `GET /strategies/{strategy_id}` —— 详情；
  - `DELETE /strategies/{strategy_id}` —— 删除（不存在 404，fail-loud）；
  - `POST /strategies/run-multi` —— 多策略组合回测（N 策略 × 各自标的，
    资金 1/N）→ Job。
- **DTO**：§3.7 新增 `StrategyCreate` / `SavedStrategy` / `MultiStrategyRunRequest`。
- **存储**：`~/.Kuantix/db/strategies.db`（NF-15，单表，id 服务端生成
  `strat_<uuid12>`，JSON 字段序列化）。
- **引擎**：`BacktestBridge.run_multi_strategy` 直调上游 `MultiStrategyEngine`；
  结果 key = `"{label}@{symbol}"`。
- **Job**：`Job.module=backtest`，`action=multi`。

### R1.3-3 — JobStore.list（新增，P0 一并落地，C1 依赖）

- `JobStore.list(module=None, market=None, limit=20, status=None) -> list[Job]`
  （按 created_at 倒序；limit 1..50，越界/status 非法 → 400）；
- `JobManager.list_jobs(...)` 转发。**C1 列表端点（`/api/v1/backtest/jobs`）本身
  为 P1**，本批次只落 store/manager 能力。

### R1.3-4 — 与草案/上游的偏差说明（P0 落地时的裁决记录）

1. **S4 为 DELETE**：`docs/06-后端支撑设计-v13草案.md` 明确「上游无编辑，草案
   不做 PUT（如需改名=删除+重建）」，故 v1.3 只提供 DELETE，不提供 PUT。
2. **S5 请求体为 items 内联**：草案 S5 请求体即 `items: [{strategy,label,code,params}]`；
   「来源=策略库勾选」由前端把勾选的 strategy_id 解析为 items 后提交，服务端不
   额外做 id → 策略解析。
3. **`PortfolioResult.individual_results` key 口径**：上游原生为
   `{exchange}{code}`（如 `SH600519`）；适配层归一化为 6 位 code（草案 P3 DTO
   口径），S5 多策略保持 `"{label}@{symbol}"` 不变。

### 后续（P1）补充说明

- **Optimize（O1–O4）**：`BacktestBridge.run_optimize` 与 `/api/v1/optimize/*`
  端点 **P1 落地**，本批次不含（避免半成品）。
- **Compare（C1）**：`/api/v1/backtest/jobs` 列表端点 **P1 落地**；本批次已补
  `JobStore.list` 依赖。
- **评级（D-3）**：纯前端 TS 模块（后端零新增），见 `docs/06` §3.4。

### R1.3-5 — 参数寻优端点（新增，P1 落地）

- **内容**：新增 `/api/v1/optimize/*` 三个端点（§2.1e）：
  - `POST /optimize/run` —— 触发单策略参数网格寻优（**单标的 × 1-2 参数**，
    笛卡尔积 ≤200）→ Job（module=backtest, action=optimize）；
  - `GET /optimize/jobs/{job_id}` —— 进度（与 B3 同逻辑，薄转发）；
  - `GET /optimize/results/{job_id}` —— 完整结果（OptimizeResult：
    results/best/heatmap）。
- **DTO**：§3.8 新增 `OptimizeRunRequest` / `OptimizeGridPoint` / `OptimizeResult`。
- **网格上限**：>200 → **400**（后端二次校验，fail-loud，不依赖前端预校验）；
  桥内 `BacktestBridge.run_optimize` 以 `DataIntegrityError` 兜底（直调场景）。
- **引擎**：`BacktestBridge.run_optimize` 直调上游 `ParamGridOptimizer`
  （R2，results/best/heatmap 原生结构 + `_clean_value` 清洗 NaN/Inf）。
- **Job**：`Job.module=backtest`，`action=optimize`；结果落 `BacktestResultStore`
  （job_id 键，`action=optimize` 区分）。

### R1.3-6 — C1 回测任务列表端点（新增，P1 落地）

- **内容**：`GET /api/v1/backtest/jobs?limit=&status=&module=`（§2.1b C1）：
  - 返回 `{items: [Job], count}`，按 created_at 倒序；Job 含 result_summary
    供 Compare 卡片展示；
  - **默认返回全部 status**（前端过滤 done，对齐上游 fetchTaskList→filter，
    决策 D-7-A）；`status` 参数仍提供；
  - `limit` 越界（>50 / <1）/ `status` 非法 → 400（fail-loud）。
- **依赖**：`JobStore.list` / `JobManager.list_jobs`（P0 已落地，R1.3-3）。

### R1.3-7 — 单标的 K 线 + 买卖点标注端点（新增，P1 落地）

- **内容**：`GET /api/v1/backtest/kline/{code}?market=&start=&end=&strategy=`
  （§2.1b B5，D-2 单代码下钻方案 A）：
  - K 线读本地 vipdoc（L1Reader，零网络），返回 `KlineWithSignals`；
  - `buy_points` / `sell_points` 是**信号标注**数组（`{date, price}`，供 K 线图
    叠加；数据结构非下单动作，R5 允许），由 `BacktestBridge.signal_points`
    经策略回测成交信号序列计算（等价 SignalScanner 单标的信号检测）；
  - code 非法 → 400；无数据 → 404（显式，fail-loud）；market=HK → 501。
- **DTO**：§3.8 新增 `KlineBar` / `SignalPoint` / `KlineWithSignals`。

### R1.3-8 — P1 落地偏差与澄清（裁决记录）

1. **网格超限错误码**：框架将 `DataIntegrityError` 映射为 422；为满足 O1 契约
   「网格>200→400」，路由层以 `MissingKeyError` 预校验（→400），桥内仍以
   `DataIntegrityError` 兜底（直调 OptimizeService/桥的场景 → job failed 422）。
2. **买卖点来源**：采用策略回测成交信号序列（`BacktestBridge.signal_points`），
   等价 SignalScanner 的单标的信号检测——Scanner.scan() 是全市场扫描（每标的
   仅末笔信号），单标的完整信号序列即策略回测 trades 的 direction 序列。
3. **B1 preset_grid 声明**：上游 `StrategyEntry.to_schema()` 已透传
   `preset_grid`（§3.6 补声明字段），后端零改动，仅供前端 ParamGridPicker 消费。
4. **§7 端点统计**：按 OpenAPI 路径去重口径更新（v1.3 P0/P1 全部计入，业务 54）。

### R1.3-9 — 服务器设置只读端点（新增，P2 落地）

- **内容**：新增 `/api/v1/settings/*` 两个端点（§2.1f）：
  - `GET /settings/status` —— 只读数据源状态（config 摘要 + known_hosts 只读列表
    + 数据湖摘要 + 版本信息），零网络；
  - `POST /settings/test-connection` —— 主机连通性测试（只测不写：新建连接
    connect 即 close）。
- **DTO**：§3.9 新增 `SettingsStatus` / `SettingsKnownHostItem` /
  `TestConnectionRequest` / `TestConnectionResult`。
- **路径偏差说明**：草案 `docs/06` §2.5 预写路径为 `/api/v1/settings/datasource`
  与 `/api/v1/settings/datasource/test`；落地裁决（team-lead）改为更短的
  `/status` + `/test-connection`（`datasource` 前缀冗余），语义不变。
- **§7 端点统计**：业务 54 → **56**。

### R1.3-10 — 只读声明与 NF-20 硬约束（P2 落地裁决）

1. **Kuantix 绝不照搬上游 Settings 页的"服务器切换"**：上游该功能会写
   `~/.easy_tdx/config.json`（NF-20 红线）。本批次 E1/E2 全部**只读**：
   - E1 读 `~/.easy_tdx/config.json` 的 known_hosts（NF-20 允许只读），
     读前后以 sha256 指纹自证无写（`known_hosts.upstream_config_untouched=true`）；
   - E2 连通性测试**只测不写**：经 `TdxClientFactory` 显式 host/port 新建连接
     ping 即关，**禁 from_best_host / save_best_* / 禁写任何文件**；
   - 前端 `/settings` 页**不含任何写操作 UI**，配置修改引导用户编辑 Kuantix
     自己的 config.toml（页面只展示路径）。
2. **E2 连接失败是业务结果而非 HTTP 错误**：返回 `code=0` 信封 +
   `{ok:false, error}`（fail-loud 体现在 error 字段）；kind 非法 / host 空 /
   port 越界 / 组合根缺 tdx_factory → **400**。
3. **E2 短超时**：`TEST_CONNECTION_TIMEOUT_S = 2.0`（新建连接 socket 超时），
   快速失败不阻塞设置页；不写入任何 best host。
4. **红线自查**：R2（easy_tdx 只在 adapters：E2 经工厂 `probe_connection`，
   路由不 import easy_tdx）、R3（无写 `~/.easy_tdx`：指纹自证 + 测试断言）、
   R4（无 except:pass / 双参 .get）、R5（settings 路径无 order/trade/buy/sell）、
   NF-20（零写入上游）。
