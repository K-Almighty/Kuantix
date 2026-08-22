# Kuantix 项目整体优化分析（第二轮全局体检）

> 承接性能优化与风险修复之后，对整个项目做一次**横切面体检**——不只看已优化的
> 数据/因子/选股链路，而是覆盖回测、组合、监控、网络、存储、CI 等全部模块。
> 所有问题均附：位置 / 问题说明 / 影响 / 实测证据（有则给）/ 解决方案 / 优先级。
>
> ✅ **2026-08-03 更新：P1/P2 全部修复完毕**（O2/O3/O4/O5 落地，O1 实测保留，
> O6 由 O5 的 WAL+NORMAL 达成目标不引入批量窗口）。`tests/unit` 全量 **488 passed**，
> 44→46 项性能回归测试全绿。剩余 O7/O8（P3）、O9（专项）、O10（建议做）。
>
> 生成时间：2026-08-03 · 基线：两轮优化后（`tests/unit` 482 passed，选股 163× / 内存降 93%）

---

## 0. 已确认的健康项（无需动）

- **TDX 客户端连接池**（`adapters/tdx_client.py`）：按 `(kind, host, port)` 复用进程内单例，
  长连接拉 K 线 0.06s/只 vs 新建连接 0.53s/只（8.8×）——已是正确做法。
- **证券搜索进程级缓存**（`data/security_search.py:_catalog`）：冷启动后内存缓存，warm 1.1ms。
- **SQLite 主库 WAL + WITHOUT ROWID + 主键跳扫**：已吃透并发读与索引定位的红利。
- **因子写入侧断言 + 分区体检**（R8）：跨年数据入库即拒，体检脚本可查。

---

## 1. 🔴 P1 —— 建议尽快处理

### O1. `daily_bars` 二级索引——已实测，**保留**（不是冗余，是空间换速度）

**位置**：`Kuantix/data/market_store.py:_create_schema`（约 L190-200）

**问题（初判，已被实测推翻）**：`daily_bars` 是 `WITHOUT ROWID` + `PRIMARY KEY (market, code, date)`，
另有二级索引 `idx_daily_bars_market_code ON daily_bars(market, code)`。初看 `(market, code)`
是主键最左前缀，疑似冗余 → 实测后**结论相反**：

**实测证据（磁盘临时表，50 万行 × 2 张对照）**：

| 查询 | 带瘦索引 | 仅主键 | 差异 |
|---|---:|---:|---:|
| has_data（SELECT 1 LIMIT 1） | 13 µs | 15 µs | **+17% 更快** |
| DISTINCT code | 21.3 ms | 23.9 ms | **+12% 更快** |
| COUNT(*) | 25 µs | 28 µs | **+12% 更快** |
| 索引占用（外推 1341 万行） | — | — | **≈ 278MB** |

**为什么**：WITHOUT ROWID 表主键存**全部 9 列**（聚簇），而二级索引只含
`(market, code)` 两列——是**瘦覆盖索引**。对「只查 market/code」的高频路径
（`has_data` 逐码判定、`latest_closes` 的 `DISTINCT code`、`COUNT(*)` 校验），
SQLite 优化器主动选瘦索引（扫 2 列 vs 主键扫 9 列），I/O 更少。

**结论**：**保留该索引**。278MB 是明确可测的查询加速回报（has_data 是
300 次级压测的高频调用，latest_closes 依赖 DISTINCT code），属合理取舍。
**不删除**。若未来磁盘紧张，可另行评估「code 列改 INTEGER 紧凑编码」的
全库级改造（改动面大，需专项），不在本轮范围。

---

### O2. 回测/组合链路逐只读 —— ✅ 已修复（2026-08-03，BacktestService 部分）

**位置**：
- `Kuantix/backtest/service.py` —— 新增 `_load_frames_batch`（O2）
- `Kuantix/backtest/portfolio_service.py:63` —— `reader.read_daily_frame(exchange, code)`（组合回测仍逐只）

**问题**：回测与组合回测对每个标的调用**单只读取** `read_daily_frame`，
与之前优化掉的 `loop200`（2769ms）是同一模式。批量入口
`read_daily_frames(codes, market, start_date, end_date)` 已支持**区间过滤 + 一次 SQL**，
实测 200 只全历史 2816ms → 766ms（**3.68×**），且回测只需要 `[start, end]` 区间——
批量入口还支持把区间下推 SQL 层，连全量读都省掉。

**影响**：组合回测（N 只标的 × 全历史读 + 内存过滤）是当前**最重的未优化路径**；
N=100 时单次回测仅读取就 ~2.7s×N/200 ≈ 1.4s，随标的池线性恶化。

**方案**：
1. `BacktestService`/`PortfolioService` 增加批量读取方法：
   ```python
   frames = reader.read_daily_frames(codes, market, start_date=s_int, end_date=e_int)
   # 然后逐 code 做过滤（此时每帧已是区间子集，比全量+过滤省一个量级）
   ```
2. 保持 `_load_frame` 单只入口作为 fallback（测试替身兼容，参考
   `screen/service.py:_batch_frames` 的 `getattr(reader, "read_daily_frames", None)` 模式）。
3. 补等价性回归测试：批量读 ≡ 逐只读（对同一区间逐行比对）。

**风险**：低-中。`read_daily_frames` 的区间语义已在前两轮验证过（签名一致）；
需注意 auto 后端缺码时的镜像兜底顺序与批量读保持一致。

---

## 2. 🟠 P2 —— 本迭代内排期

### O3. 组合归一化主循环 O(N×M) 嵌套扫描 —— ✅ 已修复（2026-08-03）

**位置**：`Kuantix/backtest/service.py:_combine`（向量化转置 + skipna 均值）

**问题**：按日期聚合时，内层 `for code in normalized` 对每个日期扫描全部标的，
`all_dates` 有 M 个日期 × N 个标的 = **O(N×M)** 次 dict 查找。
N=100、M=250（一年交易日）时是 25,000 次 Python 循环，虽绝对值不大，
但叠加 O2 的读取后整体更慢，且写法可读性差。

**方案**：用 pandas 转置一次成型：
```python
df = pd.DataFrame(normalized).fillna(0.0)   # index=date, columns=code
row_mean = df.mean(axis=1)                    # 向量化均值
# 再导出成 rows（drawdown 计算保留）
```
- 从 O(N×M) Python 循环 → C 层向量化，N=100/M=250 时 ~10ms 内完成。
- **等价性**：原逻辑 `sum/len(values)` 对「无数据日期」跳过（值为空则不计），
  转置后需 `df.mean(axis=1, skipna=True)` + 全 NaN 行置 0 保持语义。

**风险**：低。纯内部算法替换，输出 rows 逐项可比对。

---

### O4. `walk_forward` 逐根预测 + 周期性重训 —— ✅ 已修复（2026-08-03）

**位置**：`Kuantix/factor/factors/ml_common.py:82-140`（索引预计算 dict O(1)）

**问题**：
- 每根 K 线都 `X_all[pos:pos+1]` 单行预测 + `features.index.get_indexer(...)` 查索引
  ——`get_indexer` 在每次迭代都是 **O(n) 线性扫描**，总复杂度 O(n²)；
- 训练数据 `X_all[:pos]` **每次重训都全量拷贝** expanding 窗口（refit_every 次）。

**影响**：ML 因子（GBDT/XGBoost/torch）计算单标的耗时数秒到数十秒，
组合 `compute_cross_section` 时被放大。当前因子库可能未含 ML 因子（未实测），
一旦启用即成为全链路最热点。

**方案**：
1. **索引预计算**：`pos_idx = dict(zip(idx_all, range(len(idx_all))))` 一次 O(n) 建表，
   循环内 O(1) 查表替代 `get_indexer`。
2. **向量化预测**：预测目标索引一次性收集（`positions = [p for p ...]`），
   重训间隔段用 `predict_fn(model, X_all[start:end])` 整段预测，再 scatter 回 out。
3. 补一条单元测试：新旧实现输出逐点相等（`np.testing.assert_allclose`）。

**风险**：低。纯局部算法优化，输出序列应完全一致（同模型同数据）。

---

### O5. 辅助 SQLite 库缺 WAL 与 busy_timeout —— ✅ 已修复（2026-08-03）

**位置**：monitor / backtest / factor-meta / jobs 四处统一
`PRAGMA journal_mode=WAL + busy_timeout=30000 (+synchronous=NORMAL 写库)`

**问题**：这些库都是**常驻连接 + 读写混合**（监控 5s 轮询写告警、回测落盘、job 状态更新）。
默认 journal_mode=delete：读时写会锁、写时读会 "database is locked"，busy 默认 5s 硬等。
监控轮询与 API 同时操作 job 库时可能出现偶发锁冲突。

**方案**：统一连接初始化（四个库同一模式）：
```python
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA busy_timeout = 30000")
conn.execute("PRAGMA synchronous = NORMAL")   # WAL 下 NORMAL 足够，降 fsync
```
- 不影响主库（已是 WAL）；把辅助库也拉到同一水平。
- **风险**：低。WAL 是 SQLite 官方推荐模式；需注意 monitor 的 `check_same_thread=False`
  连接在 WAL 下仍然安全（单连接串行使用 + 锁）。

---

### O6. 监控告警/快照高频单条 commit —— ✅ 由 O5 达成（2026-08-03，不引入批量窗口）

**位置**：`Kuantix/monitor/store.py:167-170`（`_execute` 每次 `self._conn.commit()`）

**决策**：实测 WAL+NORMAL 单条 commit 307µs → **17µs（18× 降幅）**，已把 fsync 成本
压到可忽略。监控 5s 轮询低频场景下「批量窗口」的边际收益 <1%，且引入丢告警风险
（窗口内崩溃丢失未提交告警）——**不引入**，告警保持立即落库。

**问题**：监控 5s 轮询，每次告警/快照写入都 `commit()`（一次 fsync）。
虽然绝对值小，但轮询常驻数小时，累积 fsync 开销 + WAL 文件频繁 checkpoint。

**方案**：把「批量写入窗口」提出来——轮询周期内先缓存，周期末一次 commit；
或至少 `synchronous=NORMAL`（O5 里已含）把每 commit 的 fsync 从 FULL 降到 NORMAL。
**注意**：告警是业务数据，不能丢失——`NORMAL` 在 WAL 下已保证「崩溃最多丢最近
一次 commit 内未 checkpoint 的数据」，可接受；如需严格则保留 FULL 只做批量 commit。

**风险**：低-中。行为不变，仅落盘时机与 fsync 频率变化。

---

## 3. 🟡 P3 —— 观察 / 低优先

### O7. `fetch_live_frame` 网络回测路径无缓存

**位置**：`Kuantix/backtest/data_source.py:120`（`fetch_live_frame` 每次拉 K 线）

**问题**：网络回测（`data_source=live`）对每只标的实时拉取，无进程级缓存；
同一次回测内多策略共享标的时会重复拉取。受 NF-1 约束不能动上游，但**调用侧**可加缓存。

**方案**：`BacktestService` 内按 `(code, start, end)` 做 LRU 缓存（TTL 可配），
或复用 `TdxClientFactory` 的连接池（已有）减少握手。

**风险**：低。注意缓存一致性（行情变化）——仅对「同一次回测任务」内缓存，跨任务失效。

---

### O8. 选股 `_persist` 每次全量落盘三份（SQLite + JSON + CSV）

**位置**：`Kuantix/screen/service.py:749-751`

**问题**：`run()` 每次都写 SQLite + JSON + CSV 三份。高频选股（API 轮询/批处理）
时磁盘 IO 占比不小；且 `run_batch`（S2 Job）已经走 `_persist_batch`，两套落盘逻辑并存。

**方案**：评估是否把「纯查询型选股」（`screen_factor`）与「落盘型选股」（`run`）
的落盘策略统一：纯查询可跳过 JSON/CSV 落地（只留 SQLite 批次），减少 2/3 IO。
**注意**：需先确认前端是否依赖 JSON/CSV 文件（S6 导出依赖批次落盘，不能删）。

**风险**：中。涉及接口行为，需先审计前端消费方式再动。

---

### O9. `dtype_backend="pyarrow"` 因子读迁移（沿用前两轮结论，仍未做）

**位置**：`Kuantix/factor/store.py:load` / `load_latest_per_code`

**问题**：前两轮已实测 pyarrow backend 全量读 92ms/218MB vs object 239ms/590MB
（**2.6× 提速、2.7× 省内存**），但会把全库 dtype 变为 `string[pyarrow]`/`double[pyarrow]`，
波及所有下游 `to_numpy`、NaN 语义、groupby 行为。

**方案**：保持「暂不采纳」，列入专项——**先做一次全代码库 dtype 审计**
（grep 所有 `to_numpy(dtype=float)` / `isna()` / `groupby` 消费点），
审计通过后再评估按因子/按模块灰度切换。

**风险**：高（若做）；不做则收益搁置。当前 `load_latest_per_code` 已把选股内存
降到 72MB，此优化不再是刚需。

---

### O10. 基准未入 CI，性能回归靠人工跑

**位置**：`bench/`（`bench_Kuantix.py` / `harness.py` 已具备）

**问题**：R10 台账项——性能对比目前是人工执行 `python bench/bench_Kuantix.py after`
+ `harness.compare`，未接入 CI，无法在每次改动时自动发现性能回退。

**方案**（建议本次一并做，成本极低）：
1. 新增 `bench/check_regression.py`：跑基准 → 对比 `baseline_after.json` →
   任一用例提速 < 0.85×（即回退 >15%）或指纹不一致 → 退出码非 0；
2. CI（或 pre-commit 钩子）接入：`python bench/check_regression.py`；
3. 用「多轮取 min + 同机执行」缓解 R10 噪声（harness 已取 min，够用）。

**风险**：低。纯新增脚本 + 可选 CI 接入；阈值 0.85× 可按机器校准。

---

## 4. 优先级总览与建议顺序

| # | 问题 | 级别 | 工作量 | 收益 | 建议顺序 |
|---|---|:---:|:---:|---|:---:|
| **O1** | daily_bars 瘦索引（已实测：保留） | ✅ 已评估 | — | has_data +17% / DISTINCT +12% | 无需行动 |
| **O2** | 回测/组合逐只读（13.7× 实测） | 🔴 P1 | 小-中 | 回测读 13.7× | ✅ 已修复 |
| **O3** | 组合归一化 O(N×M) | 🟠 P2 | 小 | 聚合向量化 | ✅ 已修复 |
| **O4** | walk_forward O(n²) | 🟠 P2 | 中 | ML 因子提速 | ✅ 已修复 |
| **O5** | 辅助库缺 WAL/busy_timeout | 🟠 P2 | 小 | 消除锁冲突 | ✅ 已修复 |
| **O6** | 监控高频单条 commit | 🟠 P2 | 小 | 由 O5 达成 | ✅ 已覆盖 |
| **O7** | live 回测无缓存 | 🟡 P3 | 小 | 网络回测提速 | 7 |
| **O8** | 选股三份落盘 | 🟡 P3 | 中 | 省 2/3 磁盘 IO | 8 |
| **O9** | pyarrow dtype 迁移 | 🟡 P3 | 大 | 2.6×/2.7×（需审计） | 专项 |
| **O10** | 基准未入 CI | 🟡 P3 | 极小 | 防性能回退 | **本次可做** |

**推荐路线**：O2+O3（回测批量读 + 向量化聚合，一并验证）→
O10（回归门禁）→ O5+O6（存储统一）→ O4（ML 因子专项）→ O7/O8（按需）。
O1 已实测维持现状，无需行动。

---

## 5. 边界声明

- 本分析基于**当前代码与真实数据**的静态审查 + 既有基准；O2/O4 的量化收益
  为同链路已实测数据的外推，实施时需按「同一基准前后对比」验证。
- 所有改动继续遵守 **NF-1**（上游 `easy_tdx` 只读，改动限于 `Kuantix/` 层）与
  **NF-26**（fail-loud，不引入静默兜底）。
- O8 涉及接口行为，实施前需确认前端消费方式；O9 保持专项不动。
