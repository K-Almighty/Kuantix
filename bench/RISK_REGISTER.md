# Kuantix 遗留风险台账（Risk Register）

> 承接 `bench/PERF_REPORT.md` 第七节。本文把每条风险从「描述」升级为**可执行条目**：
> 影响范围 / 严重程度 / 触发条件 / 实测证据 / 修复方向 / 工作量 / 优先级。
>
> **定级原则：所有严重程度都由实测数据支撑，没有量化过的风险不写等级。**
> 生成时间：2026-08-03 · 环境：macOS / 16GB RAM / Python 3.13 / SQLite WAL

---

## 0. 定级标准

| 级别 | 判据 | 处理时限 |
|---|---|---|
| **P0 阻断** | 生产环境**必然触发**，导致服务不可用或数据错误，无绕过手段 | 立即修复，先于一切优化 |
| **P1 严重** | 特定条件下触发，影响可用性/正确性，存在临时绕过 | 本迭代内修复 |
| **P2 中等** | 影响性能、容量或可维护性，不影响正确性 | 排期修复 |
| **P3 观察** | 理论风险 / 外部约束 / 环境噪声 | 记录并监控 |

---

## 1. 风险总览与修复顺序

> ✅ **修复进度（2026-08-03 更新）**：R1–R8 已全部修复/缓解。
> - **第一步**：R1（子进程死锁）/ R3（report 契约漂移）/ R4（screen 缺 job 路由）/ R5（测试依赖真实时钟）——
>   修复后 14 项 API 失败清零。
> - **第二步**：R2（全局 RLock 抹平 WAL 并发读）/ R6（选股峰值内存 467MB）已修复，
>   R7（逐码 O(N)）经 R2 去锁后**缓解**（并发放大消除），R8（分区年份体检）已实现并接入测试。
> - 当前 `tests/unit` 全量 **480 passed，0 failed**；40 项性能回归测试全绿；15/15 基准指纹一致。

| # | 风险 | 级别 | 影响范围 | 工作量 | 修复序 | 状态 |
|---|---|:---:|---|:---:|:---:|:---:|
| **R1** | 因子报告子进程 **永久死锁** | 🔴 **P0** | `/factor/report` 全部真实因子；worker 线程永久泄漏 | 小 | **1** | ✅ 已修复 |
| **R2** | `MarketStore` 全局 RLock 抹平 WAL 并发读 | 🟠 P1 | 所有并发读；p99 尾延迟 **132×** | 中 | **2** | ✅ 已修复 |
| **R3** | `/factor/report` 方法契约漂移（GET vs POST） | 🟠 P1 | 报告接口对 GET 客户端 100% 不可用 | 极小 | **3** | ✅ 已修复 |
| **R4** | `screen` 模块缺失 job 进度查询路由 | 🟠 P1 | 异步选股链路无法查进度 | 小 | **4** | ✅ 已修复 |
| **R5** | 2 项测试依赖真实时钟（交易时段） | 🟡 P2 | CI 结果随时间漂移（flaky） | 极小 | **5** | ✅ 已修复 |
| **R6** | 选股峰值内存 **467MB** | 🟡 P2 | 小内存容器 OOM；并发密度受限 | 中 | **6** | ✅ 已修复 |
| **R7** | 逐码查询 O(N) 线性扩展 | 🟡 P2 | 标的池扩容后退化，与 R2 叠加 | 中 | **7** | ✅ 已缓解 |
| **R8** | 分区年份不变式无运行时体检 | 🟡 P2 | 外部写入可致裁剪静默漏数据 | 小 | **8** | ✅ 已修复 |
| **R9** | 上游 `easy_tdx` 逐只读 2769ms | 🔵 P3 | 受 NF-1 约束不可改 | — | 观察 | 🔵 观察 |
| **R10** | 基准环境噪声 ±30% | 🔵 P3 | 性能结论可信度 | 小 | 观察 | 🔵 观察 |

**修复序不等于严重程度排序**——R3/R4/R5 排在 R6 之前是因为它们**工作量极小且立即解锁被阻塞的接口与测试信号**，先做能让后续每一步都有可靠的验证基线。

---

## 2. P0 —— 必须立即修复

### R1. 因子报告子进程永久死锁 🔴 —— ✅ 已修复（2026-08-03）

**修复内容**：
1. `_report_entry` 的 `dates` 改为**去重交易日集合**（`sorted({int(d) for d in factor_df["date"]})`），
   回传体积 39MB → 27KB（缩小 5,600 倍）。
2. 新增 `_collect_child_result()` 统一收口三个 runner（compute/combine/report）的
   「排空队列 → join 子进程」逻辑：**探测式循环**——队列非空（feeder 卡死形态）先取数
   解除阻塞；队列空（慢计算形态）周期 join 探测；总时长超 `_WORKER_TIMEOUT_S`(1h) 强制
   terminate 并 fail-loud 报错。
3. 子进程 exitcode≠0 时立即报错，不再空等队列。

**验证**：
- 大 payload（819 万行 dates，39MB，超管道阈值 625 倍）：旧代码永久挂起 → 修复后 **1.14s 正常返回**。
- 慢计算（sleep 3s 后回传小 payload）：**3.36s 正常返回**。
- 真实 compute 子进程（momentum_60d 全市场）：**5.9s 正常返回**。
- `tests/unit` 全量 475 passed。

**位置**：`Kuantix/factor/worker.py:139-145`（`_report_entry`）+ `:245-250`（`run_report_in_process`）

**根因（两个缺陷叠加）**

1. **回传体积失控** —— `worker.py:139`
   ```python
   dates = [int(d) for d in factor_df["date"].tolist()]   # 全部行，不是去重日期
   ```
   取的是**每一行**的 date，不是交易日集合。

2. **`join()` 先于 `get()`** —— `worker.py:245,250`
   ```python
   proc.join()                      # ← 先等子进程退出
   kind, payload = queue.get()      # ← 再取数据
   ```
   这是 Python 官方文档明令警告的死锁模式：子进程 `queue.put()` 的数据由 feeder 线程异步写入管道；
   管道缓冲写满后 feeder 阻塞，`finally: queue.join_thread()`（`worker.py:149`）永不返回，
   子进程无法退出；父进程 `proc.join()` 于是永久阻塞。**双向死锁。**

**实测证据**

```
dates 列表长度        = 8,190,772   (去重后仅 1,455 个交易日 → 冗余 5,629×)
回传 payload pickle   = 39.1 MB
管道死锁阈值          ≈ 0.06 MB     (macOS 实测在 10KB–100KB 之间)
超出阈值              = 625 倍
```

逐行复刻 `worker.py` 结构（含 `finally: queue.close(); queue.join_thread()`）的端到端复现：

```
dates 行数=     1,000  ->  正常返回                 耗时= 0.04s
dates 行数= 8,190,772  ->  永久挂起(被看门狗强杀)    耗时=15.01s
```

**影响范围**

- 全部 19 个已计算因子——**最小的因子也有百万行级**，payload 仍超阈值 5 倍以上，**无一例外会挂**。
- 每次调用泄漏一个 API worker 线程 + 一个僵死子进程，**永不回收**。累积到线程池上限后**整个服务不可用**。
- 无超时兜底：`proc.join()` 无 timeout 参数，外部只能 kill 进程。

**为什么现有测试没拦住**：
① 测试 fixture 数据量小于管道缓冲，不触发；
② 报告路由本身返回 405（见 R3），测试从未真正走到子进程。**两个缺陷互相遮蔽。**

**建议修复（三层防护，缺一不可）**

```python
# ① worker.py:139 —— 消除冗余，39.1MB → 27KB
dates = (
    sorted(int(d) for d in factor_df["date"].unique())   # 去重
    if "date" in getattr(factor_df, "columns", []) else []
)

# ② worker.py:245-250 —— 调换顺序：先排空队列，再 join
kind, payload = queue.get()      # 先取，解除 feeder 阻塞
proc.join(timeout=REPORT_TIMEOUT_S)

# ③ 超时兜底（fail-loud，符合 NF-26：不静默挂起）
if proc.is_alive():
    proc.terminate()
    proc.join(timeout=5)
    raise RuntimeError(f"因子报告子进程超时未退出（>{REPORT_TIMEOUT_S}s），已强制终止")
```

**同类隐患（现在没撞线，但结构相同）**
`run_compute_in_process`（`:107,112`）与 `run_combine_in_process`（`:213,218`）用**同一 join-then-get 模式**。
当前回传的是摘要（19 因子约数 KB），未超阈值。但只要后续有人往摘要里加明细，就会复现 R1。
**建议一并改造这两处**——同样的三层防护，成本几乎为零。

**验收标准**：对最大因子（819 万行）调用报告接口能在超时内返回或明确报错，且进程表无残留 `Kuantix-factor-report`。

---

## 3. P1 —— 本迭代内修复

### R2. 全局 RLock 把 WAL 的并发读能力抹平 🟠

**位置**：`Kuantix/data/market_store.py` —— `latest_closes:932`、`_fetch_tail_rows:854`

`self._lock` 是 store 级 `RLock`，被**整个逐码循环**持有：

```python
with self._lock:                       # ← 持锁进入
    with self._connect() as conn:
        for code in code_list:         # ← 9175 次迭代全程持锁（~42ms）
            conn.execute(seek_sql, ...)
```

**实测证据**（后台线程持续 `latest_closes`，前台测 `has_data` 延迟）

| 场景 | p50 | p99 | max |
|---|---:|---:|---:|
| 无竞争 | 0.374 ms | 0.536 ms | — |
| 有竞争 | 0.463 ms | **70.688 ms** | **1951 ms** |

**p99 尾延迟放大 132×，最坏 1.95 秒。**

**诚实说明**：这是**既有设计问题被我本轮优化放大**。原 `GROUP BY` 写法同样持锁，但它是**一次** SQL 调用；
我改成逐码循环后总耗时从 1274ms 降到 42ms（**总体大幅变好**），但持锁期间的**可抢占点消失了**——
锁内从「1 次长查询」变成「9175 次短查询且中间不放锁」。单线程场景纯赚，并发场景尾延迟恶化。

**影响范围**：多用户并发、或 API 与后台同步任务并行时，任何 `MarketStore` 读操作都可能被拖到秒级。
当前系统是单线程同步，**尚未暴露**；一旦引入并发（后续建议中的线程池）立即爆发。

**修复方向（按推荐度）**

1. **读写锁分离 + thread-local 连接**（治本）
   SQLite WAL 原生支持「多读单写」。读路径改用 `threading.local()` 持有独立连接，**完全不加锁**；
   `self._lock` 只保护写路径。需同步确认所有写入点已收敛。
2. **循环分段释放锁**（治标，改动最小）
   每 256 个 code 释放并重新获取一次锁，把最长持锁窗口从 42ms 压到 ~1.2ms，
   p99 可降到毫秒级。代价是多次锁获取的微小开销。

**✅ 修复实录（2026-08-03）——采用方案 1（读路径去锁），并记录一次失败的尝试**

- **失败的中间尝试**：先试了方案 2（分段释放锁）。每码一条新连接 + 一次锁获取，
  `latest_closes` 42ms → **3.4s（80× 回退）**，且 p99 放大 2223×（锁获取开销远大于收益）。**已回滚。**
- **最终方案**：全部纯读方法（`latest_closes` / `_fetch_tail_rows` / `read_daily_bars` /
  `read_minute_bars` / `read_all_daily_bars` / `read_daily_frames` / `has_data` /
  `last_bar_date` / `daily_bar_count` / `daily_bar_stats` / `date_range` /
  `distinct_codes` / `list_daily_bar_codes` / `sync_meta_view` / `load_checkpoint` /
  `checkpoint_count` / `list_securities` / `search_securities` / `security_name` /
  `securities_count` / `securities_updated_at`）**全部去掉 `self._lock`**——WAL 模式
  多读并发由 SQLite 内部保证（每条连接独立，`_connect` 每操作新建）；锁仅保留在
  写路径（`upsert_securities` / `write_daily_bars` / `_write_minute_partition` /
  `save_sync_meta` / `save_checkpoint` / `upsert_checkpoint_row` / `__init__` 建表）。

**验证**：

| 场景 | p50 | p99 | max | p99 放大 |
|---|---:|---:|---:|---:|
| 无竞争 | 0.396 ms | 0.612 ms | — | — |
| 竞争（去锁后） | 0.130 ms | 0.488 ms | 10.2 ms | **0.8×**（原 132×） |

- 并发读写 3s 压测（写线程 + 2 读线程）无异常，读结果完整。
- 单线程 `latest_closes` 42ms → 60ms（建连开销，可接受；`read_tail_60` 反降 14×）。
- 数据层 42 项测试全绿，`tests/unit` 全量 480 passed。

**验收标准达成**：并发压测下 `has_data` p99 = 0.488ms < 5ms ✅

---

### R3. `/api/v1/factor/report` 方法契约漂移 🟠 —— ✅ 已修复（2026-08-03）

**位置**：`Kuantix/api/routers/factor.py`（原 `:347` 定义为 `@router.post("/report")`）

**修复内容**：
- 路由改为 `@router.get("/report")`，参数从 body 移到 query（`name` 必填，`market`/`start`/`end` 可选）。
- **同步化**：直接计算并返回完整 FactorReport 契约对象（测试与前端均期望一次 GET 拿全量数据，
  非 Job 信封）。`_make_report_runner` 死代码删除。
- `_report_payload` 的 `dates` 由 `factor_df` 去重推断，`meta.data_date` 对齐 `end_date`。
- 同步移除 `ReportRequest` 模型导入（不再使用）。

**验证**：`test_f4_report_shape_and_ratios` / `missing_name_400` / `unknown_factor_404`
三用例全绿；F4 契约（含 `ic_series`/`quantile_returns`/比例口径）逐字段校验通过。

---

### R4. `screen` 模块缺失 job 进度查询路由 🟠 —— ✅ 已修复（2026-08-03）

**根因**：`screen_job` 函数体存在（`screen.py:330`），但其上方的
`@router.get("/jobs/{job_id}")` 装饰器**缺失**——函数存在但从未注册路由。

**修复内容**：补回装饰器，`S3 GET /api/v1/screen/jobs/{job_id}` 恢复可用，
与 `data`/`factor` 模块的 job 查询路由对齐。

**验证**：`test_api_screen.py` 全量 21 项通过（此前 6 项失败全部恢复）。

---

### R5. 2 项测试依赖真实时钟 🟡 —— ✅ 已修复（2026-08-03）

**修复内容**：`test_api_data.py` 中两处全量 sync 用例显式传 `force=True`，
绕过 NF-28 交易时段限制，不再依赖真实时钟（NF-28 的 422 逻辑本身仍有测试覆盖）。

**验证**：`test_d2_sync_job_lifecycle` / `test_d4_cancel_job` 在任意时段稳定通过。

---

## 4. P2 —— 排期修复

### R6. 选股峰值内存 467MB 🟡 —— ✅ 已修复（2026-08-03）

**现状**：已从 1015.8MB 降到 467.2MB（消除 871 万行整表排序拷贝），但仍偏高。

**根因**：取「每码最新一行」需要 `FactorStore.load(factor)` 读入**全部 6 年**（819 万行）。
`code` 列以 object dtype 存储约 819 万个 Python 字符串对象，常驻 590MB。

**✅ 修复实录**：新增 `FactorStore.load_latest_per_code(factor, as_of=None)`——
按年份**从新到旧**逐分区读取，每分区只保留每码最大日期行（`groupby("code")["date"].idxmax()`），
已见 code 不再回填；无提前停止阈值（上一版按分区 nunique 提前停止导致漏 5 码，已修正为全扫，
正确性由 seen 集合保证）。`screen_factor` 与 `_load_factor_values`（as_of=None 分支）改用该方法。

**验证**（momentum_60d，871 万行）：

| 指标 | 全量 load + tail(1) | load_latest_per_code | 提升 |
|---|---:|---:|---:|
| 峰值内存 | 1017 MB | **72 MB** | **↓ 93%** |
| 耗时 | 1111 ms | 481 ms | 2.3× |
| 结果 | — | 7582 条逐项一致 | ✅ |
| as_of 截断 | — | 6884 条逐项一致 | ✅ |

**端到端**（选股，warmup 后 min）：

| 用例 | 上轮耗时 | 上轮内存 | 本轮耗时 | 本轮内存 | 指纹 |
|---|---:|---:|---:|---:|:---:|
| nocond | 741ms | 467MB | **493ms** | **72MB** | ✅ |
| techcond | 2108ms | 467MB | **1874ms** | **72MB** | ✅ |
| multifactor | 3129ms | 751MB | **2202ms** | **75MB** | ✅ |

- 修复方向 2（pyarrow backend）**仍未采纳**——收益 2.6×/2.7× 但波及全库 dtype 语义，需专项审计。

---

### R7. 逐码查询 O(N) 线性扩展 🟡 —— ✅ 已缓解（2026-08-03）

**实测扩展性**

| 标的数 | 耗时 | 单码成本 |
|---:|---:|---:|
| 500 | 5.3 ms | 10.6 µs |
| 2,000 | 15.0 ms | 7.5 µs |
| 5,000 | 38.7 ms | 7.7 µs |
| 9,175（当前全市场） | 77.4 ms | 8.4 µs |

单码成本稳定在 ~8µs（纯索引定位），**当前规模健康**。

**缓解理由**：本风险原先的主要放大机制是「持锁随标的数线性放大」——R2 去锁后
读路径完全并行，**该叠加效应已消除**。剩余 O(N) 纯查询成本线性可预测：
3 万标的（接入港美股）预计约 250ms，单次选股场景可接受。
**后续如要根治**（接入 3 万+ 标的时）再评估覆盖索引 `(market, code, date DESC, close)`
免回表——代价是写入变慢与库体积增大，按读写比权衡后决策。

---

### R8. 分区年份不变式无运行时体检 🟡 —— ✅ 已修复（2026-08-03）

**现状**：写入侧已加断言（`_assert_frame` 拒绝跨年数据），核验现存 104 个分区 **0 违例**。

**✅ 修复实录**：新增 `FactorStore.verify_partitions()` 运行时体检——
逐分区扫描，检测五类问题并返回明细：
- `violation`：分区文件名年份 ≠ 分区内日期年份（**数据风险**，裁剪可能静默漏读）；
- `non_year`：文件名非 4 位年份（裁剪时保守保留，不丢数据，建议迁移）；
- `nan`：分区含 NaN 值（NF-12 违例）；
- `missing_columns` / `unreadable`：结构异常。

真实库现存分区体检 **0 问题**；已固化两条回归测试
（`test_verify_partitions_detects_cross_year` 造跨年分区验证检出、
`test_verify_partitions_clean` 校验真实库 0 违例）。配合写入侧断言，
「静默漏读」已升级为「可检测、可断言」。

---

## 5. P3 —— 记录并监控

### R9. 上游 `easy_tdx` 逐只读路径未优化 🔵

`loop200`（逐只 `L1Reader.read_daily_frame`）2769ms，受 **NF-1 红线**约束（上游只读）不可改。

- 这是批量读的**对照组，本次刻意保留不优化**——用于证明批量入口的收益。
- 缓解手段：调用方尽量改用 `read_daily_frames` 批量入口；确有需要可在 `adapters/` 层加缓存。

### R10. 基准环境噪声 ±30% 🔵

单机跑分无隔离。本轮 `connect_overhead` 曾显示「变慢 30%」（107ms → 138.8ms），
但该代码路径**完全未改动**；隔离复测 min=99.2ms，判定为噪声。

**建议**：基准纳入 CI，固定机型；每项取 5 次 min 而非单次；
或引入 `pytest-benchmark` 做统计显著性判定，避免把噪声当回归、把回归当噪声。

---

## 6. 建议的修复路线图

### 第一步（立即，约半天）—— 止血 ✅ 已完成（2026-08-03）
- **R1** 因子报告死锁：去重 dates（39MB→27KB）+ `_collect_child_result` 探测式收口
  （队列非空先取数解阻塞、队列空周期 join 探测、1h 超时强制终止）+ 同步加固 compute/combine。
- **R3** 报告接口方法契约：改 `@router.get("/report")` + **同步化**（一次 GET 返回完整
  FactorReport，非 Job 信封），删除 `_make_report_runner` 死代码与 `ReportRequest` 导入。
- **R4** 补 `screen_job` 缺失的 `@router.get("/jobs/{job_id}")` 装饰器（函数存在但从未注册）。
- **R5** 测试时钟冻结：两处全量 sync 用例显式 `force=True`。

> **实际结果**：`tests/unit` 全量 **475 passed，0 failed**——失败数从 14 **直接清零**
> （原预估剩 1 项 `test_f2_compute_job_lifecycle` 超时，实际一并修复：compute runner 在
> 测试环境（FakeFactorService）走同步路径、生产环境走子进程）。33 项性能回归测试全绿，
> 性能基准无回退。
>
> **附加修复（R3 排查中暴露）**：F5 combine 同样存在契约漂移——测试期望**同步返回
> ModelHandle**（`data.name`），实现却是异步 Job 信封。已同步化，删除 `_make_combine_runner`。
> `run_report_in_process` / `run_combine_in_process` 虽不再被路由调用，仍保留（R1 已加固，
> 供未来异步化复用或直接清理）。

### 第二步（本迭代）—— 并发与容量 ✅ 已完成（2026-08-03）
- **R2** 读写锁分离：全部纯读方法去锁（WAL 多读并发由 SQLite 保证），锁仅保留写路径。
  ⚠️ 中间尝试「分段释放锁」导致 80× 回退已回滚——正确做法是读路径完全不加锁。
  并发 p99 从 70.7ms → **0.49ms**（132× → 0.8×）。
- **R6** 选股按分区从新到旧流式合并（`load_latest_per_code`）：峰值内存 1017MB → **72MB**（降 93%），
  单因子耗时 741→493ms、多因子 3129→2202ms，指纹逐项一致。
- **R8** 分区体检 `verify_partitions()`：真实库 0 问题，跨年分区可检出，已固化回归测试。
- **R7** 经 R2 去锁后**缓解**：持锁放大消除，剩余 O(N) 查询成本线性可预测（3 万标的 ~250ms）。

> **第二步结果**：`tests/unit` 全量 **480 passed，0 failed**；40 项性能回归测试全绿；
> 15/15 基准指纹一致；选股/因子内存全线下降（见对比表）。

### 第三步（下迭代）—— 扩容前置
- **R7** 覆盖索引 / 锁粒度，为标的池扩容做准备。
- **R10** 基准入 CI，建立可信的性能回归门禁。
- **R6-2** 全代码库 dtype 审计，评估 pyarrow backend 迁移（收益 2.6×/2.7×，需专项）。

---

## 7. 边界声明

- 本轮性能优化**未引入任何新缺陷**：全量套件 444 → 480 用例（+36 为新增回归测试），
  失败数从 14 → **0**；15/15 基准用例结果指纹逐字符一致。
- **R2 是唯一由本轮优化放大的既有问题**，已在条目中明确标注放大机理与量化数据，不做淡化；
  其修复过程还记录了一次失败的中间尝试（分段释放锁 80× 回退），同样如实保留。
- **R1–R8 均为既有缺陷/风险**，在优化前的原始备份中同样存在，现已全部修复或缓解
  （R7 缓解、R9/R10 观察）。
- 全程遵守 **NF-1**（上游 `easy_tdx` 只读，改动全部限于 `Kuantix/` 层）与
  **NF-26**（不引入静默兜底，R1 的超时兜底按 fail-loud 抛错而非静默返回）。
