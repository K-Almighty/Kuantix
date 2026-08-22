# Kuantix v1.5 收口验收报告（QA 独立验收）

> 验收人：Edward（QA Engineer）　日期：2026-08-02
> 范围：系统验收 8 项问题（工程师已修复声明）的**最终独立验收**
> 立场：不信任工程师自测 —— 样本、注入、断言全部独立构造（`tests/acceptance/test_acc_v15_sqlite.py`）
> 约束遵循：不改业务代码 / 不碰 `easy_tdx-main` / 全部离线（tmp 目录 + 假 fetcher/引擎/服务）

---

## 0. 运行基线（全量复跑，收口证据）

| 套件 | 命令 | 结果 |
|------|------|------|
| 全量测试 | `pytest tests/` | **720 passed / 0 failed / 0 error**（exit 0） |
| ├─ 工程师既有基线 | （未含本次新增） | 693 passed |
| └─ 本次新增验收 | `tests/acceptance/test_acc_v15_sqlite.py` | **27 passed**（693+27=720） |
| 红线套件 | `pytest tests/redlines/` | **111 passed / 0 failed**（exit 0，R1–R6 + envelope_validator） |
| 前端构建 | `npm run build`（web/） | **✓ built in 2.70s**（665 modules transformed，空态引导改动编译通过） |

---

## 1. 八项验收矩阵

| # | 验收项 | 结论 | 关键断言 / 复现 |
|---|--------|------|-----------------|
| 1 | 数据依赖解耦（问题 1/7/8） | **✓** | `GET /api/v1/data/search?q=600000`→浦发银行命中；`q=6000`→前缀命中；`q=浦发/银行`→名称命中（200 + 信封全绿）；空清单→**422** 且消息含 "data sync"/"data migrate"（非 NameError/500）；注入 `ExplodingProvider` 断言 **零调用**（本地 SQLite 命中不回退网络）；AST+grep：`security_search.py` 无 `UniverseEnumerator` import、`_default_provider` 已移除 |
| 2 | SQLite 迁移（问题 2） | **✓** | 见 §2 归属规则复验表；`migrate` 造 3 只 .day（含深市 sz000002）→ `daily_bars` 入库 7 条 → L1Reader `sqlite` 后端与镜像逐字段一致（close/vol 抽查）；写侧 `vipdoc_mirror=false` → SyncEngine 写后仅 market.db 有数据、vipdoc 目录零新增 .day；auto 后端 SQLite 优先（同 code 不同值读 SQLite）、无数据 fallback 镜像、双无→显式 DataIntegrityError |
| 3 | 性能优化（问题 3） | **✓** | `write_daily_bars` 单事务 executemany+upsert 幂等：520 条批量写实测 **1.72ms**（上界 10s），重写后仍 520 条无重复；WAL 运行时确认 `journal_mode=wal`；SyncEngine 静态+行为：`threading.local` per-worker 限速、`_thread_local_fetcher` 连接缓存（40 标的 4 workers → 恰好 4 个 fetcher）；`sync_checkpoint` 表方案：O(1) 单行 upsert、运行后**无 JSON 文件**产生 |
| 4 | factor report 404（问题 4） | **✓** | 未 compute → `GET /factor/report` **404** 消息含 "compute"（report 不被调用，非 500）；假 FactorEngine 注入 compute 后 → **200** + 信封全绿（sample_count=3、ic_mean=0.05）；前端 `Factors.vue` 含空态 "尚未计算，请先运行 compute"；前端 build 通过 |
| 5 | WS 1006（问题 5） | **✓** | TestClient `websocket_connect`：正常握手收到 **hello + snapshot**（含历史告警）；`market=HK`（未启用）→ 先发 bye 帧后关闭 **1008**（非 1006）；handler 异常（monitor_store 抛错）→ bye + 关闭 **1011**（非 1006）；前端 `api/ws.ts` 静态核对：断线指数退避重连（1s→2s→…30s 封顶）+ onclose 触发重连；前端 build 通过 |
| 6 | 模型下拉空（问题 6） | **✓** | F6 空 → API 200 空列表 + `Screen.vue` 空态 "暂无合成模型：请先到「因子」页合成并保存模型"；有模型（假 store 注入 ModelHandle）→ F6 返回 1 条（name/method）；`Screen.vue` 下拉 `v-for="m in screen.models"` 消费 |
| 7 | 策略下拉 + 搜索（问题 7） | **✓** | B1 `GET /backtest/strategies` → **count==19**，含 ma_cross/macd/boll_breakout，每条含 name/label/description；`Backtest.vue` 下拉 `v-for="s in store.strategies"` 消费 + 空态 "策略列表为空"；D8 本地命中同问题 1 |
| 8 | 参数寻优（问题 8） | **✓** | O1 `POST /optimize/run` → 200 Job 信封（module=backtest/action=optimize）；O3 结果可读（假服务注入）；网格 15×15=225 > 200 → **400** fail-loud；`OptimizeView.vue` 标的输入复用 `SecuritySearchBox`（本地搜索，零网络） |

---

## 2. 迁移归属规则复验表（程序化断言，P0）

场景样本（独立构造）：`sh600000` A 股 / `sz000002` 深市 000 A 股 / `sh000001` 上证指数 /
`sz000001` 平安银行 / `sh200001` 上交所债券 / `sz200001` 深市 B 股 / `sh430047` 北交所段垃圾文件（0 字节）

| 规则 | 期望 | 实测 | 断言 |
|------|------|------|------|
| 深市 000xxx A 股入库 | sz000002 有数据 | `has_data("CN","000002")=True` | ✓ |
| sh 上证指数段不入 A 股池 | sh000001 跳过 | `files_skipped` 计入、`read_daily_bars("CN","000001")` 值为深市 20.3 而非指数 3001.0 | ✓ |
| sh/sz 同 code 冲突 sz 胜出 | 200001 取 sz B 股 | `close==7.2`（sh 债券 100.5 被让位） | ✓ |
| 非法代码段显式跳过计数 | sh430047 跳过 | `report.files_skipped==3`（指数+冲突+非法），`has_data("CN","430047")=False`，`files_failed==0` | ✓ |
| 报告口径 | scanned=ok+skipped+failed | `files_scanned=7, files_ok=4, files_skipped=3, files_failed=0` | ✓ |

实现核验：`migrate.py::_owned_files` 以**目录为准 + 代码段校验**（`_classify_owned_file`），
不调用 `exchange_for_code`（其无目录上下文时对 000xxx 偏袒上证指数 —— 正是原 P0 数据丢失根因）；
`_SH_INDEX_PREFIXES=("000","880","999")` 在 sh 目录段先行跳过。

---

## 3. 问题清单

| 严重度 | 问题 | 复现路径 | 说明 |
|--------|------|----------|------|
| P3（信息） | `security_search.py` 保留 `_enumerate_and_persist`/`_write_cache` 内部方法（设计文档 08 §2.4 文字上列为「移除」） | 静态阅读 | **不构成验收阻断**：两方法仅在被显式注入 `provider`（测试别名）时可达；生产组合根 `build_container` 传 `provider=None`，请求路径零网络、无 NameError、空清单 422 均满足。属设计文档与实现间的文字偏差，建议后续清理或更新文档。 |

无 P0/P1/P2 问题。8 项验收项全部通过。

---

## 4. 最终验收结论

**✅ 通过 —— 8 项系统验收问题整体可作为交付依据。**

- 全量 720 passed（693 基线 + 27 独立验收新增）、红线 111 passed、前端 build 通过；
- 8 项问题逐项独立复验全部 ✓，未发现工程师自测遗漏的缺陷；
- 迁移归属 P0（深市 000xxx 入库 / 指数跳过 / sz 胜出 / 非法段跳过）程序化断言全绿；
- 唯一 P3 备注（`_write_cache` 保留）不阻塞交付，建议后续文档同步。
