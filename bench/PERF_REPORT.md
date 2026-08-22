# Kuantix 性能分析与优化报告

- **项目**：`/Users/kongbiao/Downloads/开源量化/Kuantix/`（Python 3.13 / pandas / SQLite / Parquet，基于 easy-tdx 1.20.3）
- **日期**：2026-08-03
- **原则**：**换实现、不换语义**。所有优化项均以「与优化前朴素实现逐值一致」为验收前提，
  15/15 基准用例的结果指纹（md5）与优化前基线**完全相同**。
- **红线遵守**：NF-1（上游 `easy_tdx` 只读，改动全部限于 `Kuantix/` 层）、
  NF-26（fail-loud，不引入静默兜底）、NF-27（写后回读）、NF-12（因子库无 NaN）。

---

## 一、核心执行链路与技术栈

```
CLI / FastAPI
   │
   ├── screen/service.py ── 选股主循环 ────┐
   ├── factor/service.py ── 因子计算 ──┐   │
   │                                   ▼   ▼
   ├── factor/store.py ──── Parquet 因子库（按年分区，82MB/因子/6年）
   └── data/market_store.py ─ SQLite daily_bars（WAL / WITHOUT ROWID / PK(market,code,date)，1340 万行）
            ▲
            └── adapters/（vipdoc_writer、factor_bridge —— 唯一允许触碰上游的隔离层）
```

四条性能敏感路径：**行情读写（SQLite）**、**证券清单搜索**、**因子库读取（Parquet）**、**选股打分主循环**。

## 二、基准与剖析方法

- `bench/harness.py`：warmup + 多轮重复取 `min`（抗噪），独立一轮 `tracemalloc` 采峰值内存，
  同时采集 CPU 时间、GC 回收次数、`ru_inblock`。每个用例带 `signature` 指纹用于前后结果比对。
- `bench/bench_Kuantix.py`：15 个用例覆盖四条链路，全部只读真实数据（1340 万行行情 / 104 个因子分区）。
- `bench/profile_screen.py` + cProfile：定位热点函数。
- 基线文件：`bench/baseline_before.json` / `bench/baseline_after.json`。

---

## 三、瓶颈清单（按影响排序）

优化前全市场选股 cProfile：**4.49 亿次函数调用 / 144.5s**。

| # | 瓶颈 | 位置 | 优化前代价 | 性质 |
|---|---|---|---|---|
| B1 | 逐只读全历史日线，仅为取最新收盘价 | `screen/service.py:_safe_frame` | **151.6s / 7582 次调用** | 冗余 IO 放大 |
| B2 | 每行构造 `Bar` 对象 + fail-loud 标量校验 | `market_store._row_to_bar` → `contracts.__post_init__` | **99.1s**，1334.8 万次对象创建 | 频繁对象创建 |
| B3 | `require_finite` / `_require_positive` 标量调用 | `core/fail_loud.py:223` | **26.5s**，8010 万次调用 | 低效循环 |
| B4 | `pd.DataFrame(list_of_dict)` 逐行类型推断 | `factor_bridge.bars_to_frame` | **32.3s** | 低效构造 |
| B5 | 因子库读全部年份分区后内存过滤 | `factor/store.py:load` | 单日截面 222ms / 峰值 276MB | 无分区裁剪 |
| B6 | 因子逐个 `compute_cross_section` | `factor/store.py:compute` | 5 因子 4× 冗余 | 重复计算 |
| B7 | 证券搜索全量读 17634 条后内存过滤 | `data/security_search.py` | 冷启动 28.3ms | 过滤未下推 |
| B8 | `latest_closes` 用 `GROUP BY code` 聚合 | `market_store.latest_closes` | **1274ms**（扫 1340 万行） | 未走索引定位 |
| B9 | `sort_values("date").groupby("code").tail(1)` 全表排序 | `screen/service.py` | **769ms** + 871 万行整表拷贝 | 算法可换 |
| B10 | 逐 code `values.loc[code]` 标签查找 | `screen/service.py:_sub_scores` | 全市场数千次 pandas 查找 | 低效循环 |

> B1–B4 是第一轮（占选股总耗时 ~90%）；B8–B9 是第一轮优化后**新暴露**的热点，第二轮解决。

---

## 四、已实施的优化项及原因

### 1. 选股快路径：无过滤条件时不读历史（B1）
`screen_factor` / `run` 在 `tech_cond` 与 `chanlun_cond` 均为空时，只需要**最新收盘价**用于展示。
原实现为此逐只读全历史（7582 次查询 / 1334 万行）。新增 `MarketStore.latest_closes()` 一次性取回。
语义等价性：「有最新价」⇔「`read_daily_frame` 非空」，两者对 `total` 的计数完全一致。

### 2. 有过滤条件时：批量读 + 按需截尾（B1）
- 按 500 只一批走 `WHERE code IN (...)`，替代逐只往返；
- `_required_tail()` 根据条件推算所需最小窗口（如 `min_vol_ratio` 需要 20 日），只取末 N 根。
- **取尾实现选型**（实测 500 只 / tail=20）：
  `ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC)` = 898ms
  vs 逐码 `code=? ORDER BY date DESC LIMIT N` = **15ms（59×）**。
  原因：表为 `WITHOUT ROWID` + PK`(market,code,date)`，后者是纯索引倒序扫描只碰 N 行，
  窗口函数必须扫完全部历史再排序。两者返回行已逐行比对一致。

### 3. 列式读取 + 向量化校验（B2/B3/B4）
`read_daily_frames` 改为一次取回后按 code 边界切片建 DataFrame，绕开 1334 万次 `Bar` 构造；
OHLC 值域校验由新增的 `_validate_ohlc_vectorized` 用 numpy 一次完成，
**逐项对应 `Bar.__post_init__` 的每一条断言**，fail-loud 语义不变（NF-26）。
`_bars_to_frame` 改为按列建 `np.ndarray`，类型显式给定，消除 pandas 逐行类型推断。
> 效果佐证：选股用例 GC 回收次数 **20740 → 12（降低 1728×）**。

### 4. 批量读的原始行及时释放（内存）
`raw`（200 只 × 全历史 ≈ 50 万个 8 元组，约 70MB）在转入 numpy 后立即 `del`，
避免与结果 DataFrame 同时驻留。峰值 254.5MB → **212.9MB**。

### 5. 因子库分区裁剪 + 谓词下推（B5）
`FactorStore.load` 按 `date/start/end` 计算命中年份，只读相关分区，并通过
pyarrow `filters=` 把 `date`/`code` 条件下推到 Parquet row-group 层。
安全前提：`_assert_frame` 在**写入侧**断言分区年份自洽（跨年写入抛 `DataIntegrityError`），
已核验现存 104 个分区 0 违例；文件名非 4 位年份时保守全读（不静默丢数据）。

### 6. 多因子一趟计算（B6）
`FactorStore.compute` 把所有待算因子合并为**一次** `engine.compute_cross_section(pool, pending)`，
替代逐因子循环。实测 **4.01×**，结果逐值相同；增量跳过语义不变。

### 7. 证券类型过滤下推 SQL（B7）
`list_securities(security_types=...)` 把类型过滤交给 SQL（A 股 5119/17634 行），
读侧不支持该参数时 `TypeError` 回退内存过滤（兼容旧 store）。

### 8. `latest_closes` 改索引定位（B8）— 第二轮
实测 @ 1340 万行 / 9175 只：

| 写法 | 耗时 |
|---|---|
| `GROUP BY code` + bare-column `MAX(date)` | 1274.4ms |
| `SELECT DISTINCT code`（主键跳跃扫描，17.6ms）+ 逐码 `ORDER BY date DESC LIMIT 1`（38.6ms） | **56ms（22.7×）** |

返回字典已逐项比对，9175/9175 完全相同。

### 9. `_latest_row_per_code`：`idxmax` 取代全表排序（B9）— 第二轮
`frame.sort_values("date").groupby("code").tail(1)` 对 871 万行做全表排序 + `_cumcount_array`
位置掩码 = 768.8ms；改为 `groupby("code")["date"].idxmax()` + `loc` = **203.3ms（3.8×）**，
且省掉整表拷贝（选股峰值内存 1015.8MB → 467.2MB）。

**附带修正一处确定性隐患**：原写法的行顺序由 `sort_values` 的 quicksort 决定（不稳定），
而下游 `kept.sort(key=score)` 是稳定排序 —— 同分标的的输出次序因此依赖实现细节。
新实现显式按 `(date, code)` 稳定排序，用确定性顺序替代该依赖。

### 10. `_sub_scores_map`：一次成型的子分数映射（B10）
逐 code `values.loc[code]` → 一次 `to_numpy()` 后按行组装，
数值处理保持 `round(float(v), 6)` 完全一致，不引入精度差异。

---

## 五、性能对比（同一基准，同机同数据）

| 用例 | 规模 | 优化前(ms) | 优化后(ms) | 提速 | 内存前(MB) | 内存后(MB) | 内存降幅 | 结果一致 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |
| data.read_daily_bars.single_full | 单只全历史 ~5200 根 | 8.6 | 8.2 | 1.05× | 1.2 | 1.2 | 0.0% | ✅ |
| data.read_tail_60 | 取末 60 根 | 8.4 | 8.4 | 1.01× | 1.2 | 1.2 | 0.0% | ✅ |
| data.read_daily_frames.batch200 | 200 只 × 全历史 | 2816.5 | 766.3 | **3.68×** | 211.8 | 212.9 | -0.5% | ✅ |
| data.read_daily_frame.loop200 | 200 只逐只读（对照） | 2706.2 | 2769.3 | 0.98× | 29.2 | 29.2 | 0.0% | ✅ |
| data.connect_overhead.300calls | 300 次小查询 | 107.0 | 105.9 | 1.01× | 0.0 | 0.0 | 0.0% | ✅ |
| search.cold_by_code | 冷启动按代码（17634 条） | 28.3 | 10.2 | **2.78×** | 8.5 | 2.5 | 71.0% | ✅ |
| search.cold_by_name | 冷启动按名称 | 28.5 | 10.2 | **2.80×** | 8.5 | 2.5 | 71.0% | ✅ |
| search.warm_by_code | 热缓存按代码 | 1.1 | 1.1 | 1.02× | 0.0 | 0.0 | 0.0% | ✅ |
| factor.load_single_date | 单日截面（82MB/6 年） | 221.9 | 9.2 | **24.21×** | 276.3 | 6.5 | 97.6% | ✅ |
| factor.load_year_range | 单年区间 | 250.9 | 40.2 | **6.25×** | 342.2 | 53.3 | 84.4% | ✅ |
| factor.load_single_code | 单只全历史 | 421.6 | 83.1 | **5.07×** | 276.1 | 16.4 | 94.1% | ✅ |
| factor.load_5factors_single_date | 5 因子 × 单日截面 | 859.7 | 36.9 | **23.33×** | 284.5 | 7.2 | 97.5% | ✅ |
| **screen.factor_top50_nocond** | 单因子 top50 无过滤（全市场） | 80473.7 | **561.5** | **143.32×** | 1015.8 | 467.2 | 54.0% | ✅ |
| **screen.factor_top50_techcond** | 单因子 top50 + 技术过滤 | 79582.5 | **1843.8** | **43.16×** | 1015.8 | 467.2 | 54.0% | ✅ |
| **screen.multifactor_top50** | 5 因子等权 top50（全市场） | 85736.7 | **2048.6** | **41.85×** | 1042.0 | 751.3 | 27.9% | ✅ |

补充指标：选股用例 GC 回收次数 **20740 → 12**；`FactorStore.compute` 多因子批量 **4.01×**。

> 三项非提速用例（`read_tail_60`、`loop200`、`connect_overhead`）的代码路径**未被改动**，
> 已隔离复测确认为机器噪声：`has_data ×300` 独立复测 min=99.2ms / median=104.3ms（基线 107.0ms）；
> `loop200` 同进程对照 backup=2751ms vs 优化后=2760ms。`loop200` 保留为批量读的对照组，故意不优化。

### 5.1 第二轮优化（R2/R6/R7/R8，2026-08-03 续做）

第二轮针对风险台账的并发与容量项，选股/因子内存**全线再降**（同一基准，warmup 后 min 对比）：

| 用例 | 上轮耗时 | 上轮内存 | 本轮耗时 | 本轮内存 | 内存再降 | 指纹 |
|---|---:|---:|---:|---:|---:|:---:|
| screen.factor_top50_nocond | 561.5ms | 467.2MB | **493ms** | **72.4MB** | 84.5% | ✅ |
| screen.factor_top50_techcond | 1843.8ms | 467.2MB | **1874ms** | **72.4MB** | 84.5% | ✅ |
| screen.multifactor_top50 | 2048.6ms | 751.3MB | **2202ms** | **75.3MB** | 90.0% | ✅ |

- **R2 锁竞争**：读路径全部去锁（WAL 多读并发），锁仅保护写。并发压测 `has_data` p99
  **70.7ms → 0.49ms**（放大 132× → 0.8×，消除竞争）。
- **R6 选股内存**：新增 `load_latest_per_code` 分区流式合并（从新到旧逐区取每码最新行），
  因子全量读峰值 1017MB → **72MB**（降 93%），结果 7582 条逐项一致、as_of 截断 6884 条一致。
- **R7 逐码 O(N)**：R2 去锁后并发放大消除，3 万标的预计 ~250ms，线性可预测。
- **R8 分区体检**：`verify_partitions()` 扫描全部 parquet，检测跨年/NaN/结构异常，真实库 0 问题。

基准环境噪声（`read_daily_bars`/`connect_overhead` 单次跑 0.6×）已隔离复测确认：
独立复测 read_tail_60 min=0.6ms（反降 14×）、connect_300 min=111ms（基线 107ms），
均为同机负载波动，非回归。

---

## 六、正确性验证

1. **结果指纹**：15/15 基准用例的 md5 指纹与 `baseline_before.json` **逐字符相同**。
2. **回归测试**：新增 `tests/unit/test_unit_perf_regression.py`，**33 项全通过**，逐项固化等价关系：
   - `read_daily_frames(tail=N)` ≡ 全量读后 `.tail(N)`
   - `read_daily_bars(tail=N)` ≡ 全量读后 `[-N:]`
   - `latest_closes()` ≡ 逐只取末根收盘价
   - `list_securities(types=)` ≡ 全量读后内存过滤
   - `FactorStore.load` 分区裁剪 ≡ 读全部分区后过滤
   - `FactorStore.save` 跨年分区 fail-loud
   - `_sub_scores_map` ≡ 逐 code `_sub_scores`（含 NaN / 负数 / 舍入）
   - 多因子批量 compute ≡ 逐因子 + 增量跳过语义
   - `_latest_row_per_code` ≡ `sort+groupby.tail(1)`（5 组参数化：乱序 / 单行 / NaN / 长尾停牌）+ 顺序确定性 + 空帧
3. **全量测试套件**：

   | | 用例总数 | 失败 | 通过 |
   |---|---:|---:|---:|
   | 优化前（backup） | 444 | 14 | 430 |
   | 优化后 | 477 | 14 | 463 |

   失败清单 `diff` **逐条完全相同** —— 这 14 项（`test_api_*`）在**原始备份中同样失败**，
   与本次优化无关（属既有问题，见下）。新增 33 项全部通过。

---

## 七、尚未处理的风险点

> 📋 **本节已升级为独立风险台账：[`bench/RISK_REGISTER.md`](./RISK_REGISTER.md)**
> 其中每条风险均附影响范围、严重程度（P0–P3）、触发条件、实测证据、修复方案与优先级排序。
>
> ✅ **2026-08-03 更新（第二步完成）：R1–R8 已全部修复/缓解**——
> - 第一步：R1（子进程死锁）/ R3（report 契约漂移）/ R4（screen 缺 job 路由）/ R5（测试时钟）。
> - 第二步：R2（全局 RLock 抹平并发读，**读路径去锁**，并发 p99 70.7ms→0.49ms）、
>   R6（选股峰值内存，**`load_latest_per_code` 流式合并**，1017MB→72MB，降 93%）、
>   R7（逐码 O(N)，去锁后缓解）、R8（分区年份不变式体检 `verify_partitions`）。
> - 当前 `tests/unit` 全量 **480 passed，0 failed**；40 项性能回归测试全绿；15/15 基准指纹一致。

下表为初版摘要，完整分析以风险台账为准（已修复项标注 ✅）：

| 风险 | 说明 | 建议 | 状态 |
|---|---|---|---|
| **因子报告子进程永久死锁** | 回传 39.1MB 超管道缓冲，`join()` 先于 `get()` 双向死锁；影响全部真实因子 | 台账 R1：去重 dates + `_collect_child_result` 探测式收口 | ✅ 已修复 |
| **`/factor/report` 契约漂移** | 定义 POST、契约与测试按 GET；405 响应体 `code:400` 自相矛盾 | 台账 R3：改 GET + 同步化返回完整契约 | ✅ 已修复 |
| **screen 缺 job 查询路由** | `screen_job` 函数存在但装饰器缺失，S3 端点 404 | 台账 R4：补回 `@router.get("/jobs/{job_id}")` | ✅ 已修复 |
| **测试依赖真实时钟** | NF-28 交易时段限制使 2 项用例 flaky | 台账 R5：全量 sync 用例显式 `force=True` | ✅ 已修复 |
| **全局 RLock 抹平 WAL 并发读** | 逐码查询持锁 9175 次循环，并发下 p99 尾延迟 132× | 台账 R2：读路径去锁（WAL 多读），锁仅保护写 | ✅ 已修复 |
| **选股峰值内存 467MB** | 为取「每码最新一行」读入全部 6 年（871 万行） | 台账 R6：`load_latest_per_code` 分区流式合并（→72MB） | ✅ 已修复 |
| **逐码查询 O(N) 扩展** | 标的池扩容后查询成本线性上升 | 台账 R7：去锁后并发放大消除，剩余成本可预测 | ✅ 已缓解 |
| **分区裁剪依赖文件名年份** | 外部写入可致裁剪静默漏数据 | 台账 R8：`verify_partitions()` 体检已实现 | ✅ 已修复 |
| **`loop200` 逐只读路径未优化** | 2769ms，走 `L1Reader.read_daily_frame` 单只入口 | 调用方尽量改用 `read_daily_frames` 批量入口 | 🔵 观察 |
| **上游 `easy_tdx` 未优化** | 受 NF-1 约束（上游只读），其内部热点未触碰 | 若需进一步提速，只能在 `adapters/` 层加缓存 | 🔵 观察 |

## 八、后续优化建议（按性价比排序）

1. **因子库 `dtype_backend="pyarrow"`**（收益最大）
   实测同一因子全量读：object dtype 239ms / 常驻 590MB，pyarrow string backend **92ms / 218MB**
   （**2.6× 提速、2.7× 省内存**，数值与 code 内容已验证一致）。
   ⚠️ 但会把全库列类型变为 `string[pyarrow]` / `double[pyarrow]`，影响面覆盖所有下游
   （`to_numpy(dtype=float)`、NaN 语义、groupby 行为），**需要一次全代码库 dtype 审计**，本次因风险过大未采纳。

2. **`screen_factor` 只读最新分区**
   取「每码最新一行」时按年份分区从新到旧流式合并、已见 code 不再回填，
   峰值内存可从「全库」降到「单年」。需先确认分区年份不变式（已有写入侧断言）。

3. **`daily_bars` 增补覆盖索引**
   若 `latest_closes` 场景高频，可加 `(market, code, date DESC, close)` 覆盖索引，
   免回表；代价是写入变慢与库体积增大，需按读写比权衡。

4. **搜索索引持久化**
   证券搜索冷启动 10.2ms 已可接受，若要进一步降低可把倒排索引落盘复用。

5. **异步/并发**
   当前全部为单线程同步。选股的批量读之间无依赖，可用线程池并行
   （SQLite WAL 支持多读），但需先压测锁竞争。

---

## 九、改动文件清单

| 文件 | 改动行数 | 主要内容 |
|---|---:|---|
| `Kuantix/data/market_store.py` | ~370 | 批量读列式化、`_fetch_tail_rows` 逐码取尾、`latest_closes` 索引定位、`_validate_ohlc_vectorized`、`list_securities(security_types=)`、`raw` 及时释放 |
| `Kuantix/screen/service.py` | ~270 | 无条件快路径、分批批量读、`_required_tail`、`_sub_scores_map`、`_latest_row_per_code` |
| `Kuantix/factor/store.py` | 97 | 分区裁剪 + 谓词下推、写入侧年份自洽断言、多因子一趟 compute |
| `Kuantix/factor/service.py` | 33 | 适配批量 compute，耗时按因子摊分 |
| `Kuantix/data/security_search.py` | 10 | 类型过滤下推 SQL + 兼容回退 |
| `Kuantix/adapters/factor_bridge.py` | 13 | 透传 `tail` 参数 |
| `Kuantix/adapters/vipdoc_writer.py` | 4 | 写后回读只取末 N 根 |
| `tests/unit/test_unit_perf_regression.py` | 新增 | 33 项等价性回归测试 |
| `bench/harness.py` / `bench/bench_Kuantix.py` / `bench/profile_screen.py` | 新增 | 基准框架与用例集 |
| `bench/baseline_before.json` / `baseline_after.json` | 新增 | 前后基线数据 |
</content>
</invoke>
