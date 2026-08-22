# Kuantix 架构红线静态检查器

> 维护：QA（software-qa-engineer）　目录：`Kuantix/tests/redlines/`
> 需求基线：`docs/PRD.md` v2.2（NF-1 ~ NF-28）　设计基线：`docs/system_design.md` §8 共享知识

这套检查器守的是**结构性红线**——不依赖任何业务逻辑实现细节，只看代码形态。
它们的作用不是"测功能对不对"，而是**在错误写法进主干之前就拦住**。

所有规则都遵守两条原则：

1. **AST 优先**：判定走 `ast` 标准库，不用脆弱正则硬扫；正则只用于字符串字面量内容匹配。
2. **代码未落地时优雅 skip**：源码目录不存在时 `pytest.skip`，绝不 error。
   （检查器自身报 error = 检查器有 bug，必须修。）

---

## 快速开始

```bash
cd /Users/kongbiao/Downloads/开源量化

# 跑全部红线
Kuantix/.venv/bin/python -m pytest Kuantix/tests/redlines/ -v

# 只跑某条红线
Kuantix/.venv/bin/python -m pytest Kuantix/tests/redlines/test_r4_fail_loud.py -v

# 输出机器可读报告（供 CI 消费）
Kuantix/.venv/bin/python -m pytest Kuantix/tests/redlines/ --redline-json=/tmp/redlines.json
```

运行结束后，terminal summary 会额外打印三块内容：
**扫描范围** / **当前豁免清单（含未命中的僵尸条目）** / **spike 代码 ADVISORY 预警**。

---

## 六条红线一览

| 规则 | 约束编号 | 判定逻辑（一句话） | 豁免 |
|------|---------|-------------------|------|
| **R1** 系数表无副本 | NF-25 | 源码出现上游系数值对（如 `(0.01, 0.01)`）被赋进 dict/变量，或本地 `*COEFFICIENT*` 字典字面量 → FAIL；同时正向要求 `adapters/coefficients.py` import 上游 `_SECURITY_COEFFICIENTS` | ❌ 不可豁免 |
| **R2** 上游调用收敛 | NF-1 | AST 遍历 import 节点，`easy_tdx`/`pytdx` 出现在 `adapters/` 之外 → FAIL（含 `importlib.import_module` 动态绕道） | ❌ 不可豁免 |
| **R3** 上游禁用 API | NF-1 / NF-20 | 命中 `from_best_host` / `detect_tdx_home` / `easy_tdx._df` → FAIL；用污点传播识别 `~/.easy_tdx` 路径变量，其上的**写**操作 → FAIL（**读放行**） | ❌ 不可豁免 |
| **R4** fail-loud 无兜底 | NF-26 | `except ...: pass` 与 `x.get(key, 默认值)` 双位置参 → 默认全 FAIL | ✅ `faillloud_allowlist.txt` |
| **R5** 无下单接口 | NF-21 | 只看三个维度：**函数定义名** / **REST 路由路径** / **券商 SDK import**，不扫数据结构字段名 | ✅ `no_trading_allowlist.txt`（应永远为空） |
| **R6** 无硬编码 A 股常量 | NF-5 | 业务层（`data/factor/screen/monitor/api`）出现 `"CNY"`/`"Asia/Shanghai"`/`"09:30"`/`"SSE"` 等字符串字面量 → **全域扫** FAIL；`0.1`/`0.2`/`100` 等裸数字加**语境过滤**才判 | ✅ `hardcoded_cn_allowlist.txt` |

外加两组辅助用例：

| 规则 | 作用 |
|------|------|
| **R0** 扫描器健康检查 | 防检查器**静默失效**：上游可 import？扫描范围非空？全量语法可解析？上游目录零污染？ |
| **自测** `test_checker_selftest.py` | 每条规则都有**阳性样本**（必须命中）+ **阴性样本**（必须放行），证明检查器真的会开火、且不误伤 |

---

## 逐条详解

### R1 — 系数表无副本（NF-25，优先级最高）

**约束原文**（PRD NF-25，标注为"硬性，不可协商"）：
> 系数表**必须从上游 import 引用，严禁在 Kuantix 侧复制、粘贴或硬编码任何副本**。
> 一旦复制一份，上游哪天更新代码段划分（例如补上北交所、新增 ETF 代码段），
> 我们这份副本就变成**错的，而且照样不报错**。

**上游基准**：`easy_tdx/offline/daily_bar.py::_SECURITY_COEFFICIENTS`（10 种证券类型 → 4 种系数值对）。

**关键设计**：检查器**不抄写**系数值，而是运行时 `from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS`
再取 `set(values())` 作为基准 —— 检查器自己也遵守 NF-25，且能自动跟随上游演进。

| 子规则 | 命中形态 |
|--------|---------|
| R1-A | dict 字面量含上游系数值对，且键形如证券类型（`SH_A_STOCK` 等） |
| R1-B | 名字匹配 `*COEFFICIENT*` / `*_COEFF(S)` 的变量被赋值为 dict 字面量 |
| R1-C | 本地重新定义 `_SECURITY_COEFFICIENTS`（`= <import 来的名字>` 的再导出放行） |
| R1-D | 上游系数值对作为元组字面量直接赋给变量 |
| R1-E | 写盘类函数调用直接传入两个字面量系数，如 `sync_daily_bars_from_security_bars(f, bars, 0.01, 0.01)` |
| R1-F | **正向**：`adapters/coefficients.py` 必须存在对上游 `_SECURITY_COEFFICIENTS` 的引用 |

---

### R2 — 上游调用收敛在 adapters（NF-1）

**约束原文**：
> 所有上游调用需**收敛到 `Kuantix/adapters/` 单一适配层**，上游升级时只改这一层。

只有 `<pkg>/adapters/` 允许 `import easy_tdx`。`core/` `data/` `factor/` `screen/`
`monitor/` `api/` 以及包根模块一律禁止。

| 子规则 | 命中形态 |
|--------|---------|
| R2-A | `import easy_tdx...` 出现在 adapters 之外 |
| R2-B | `from easy_tdx... import ...` 出现在 adapters 之外 |
| R2-C | `importlib.import_module("easy_tdx...")` / `__import__` 动态绕道 |
| R2-D | 直接 import 上游底层协议库 `pytdx` |
| R2-E | 包内已用 easy_tdx 但 `adapters/` 目录不存在 |

**本规则不设 allowlist** —— 分层是架构地基，开一个口子就失去意义。

---

### R3 — 上游禁用 API 清单（NF-1 / NF-20）

| 子规则 | 禁什么 | 为什么 |
|--------|--------|--------|
| R3-A | `from_best_host` | 上游实现会**自动回写用户的 `~/.easy_tdx/config.json`**；NF-20 规定只读不写。改用显式 host/port 构造客户端 |
| R3-B | `detect_tdx_home` | PRD Q3 实地探测：本机 macOS **无通达信客户端、无任何 vipdoc 目录**，调用必失败；且失败路径极易被 `except: pass` 兜底成静默降级 |
| R3-C | `easy_tdx._df` | 上游私有 DataFrame 便捷层，非稳定接口 |
| R3-D | 对 `~/.easy_tdx` 的**写操作** | NF-20「只读不写」；NF-15/18 要求 Kuantix 数据落 `~/.Kuantix/`，与 `~/.easy_tdx/` 完全隔离 |

**R3-D 的实现方式 —— 污点传播 lite**：

1. 迭代到不动点，收集所有「RHS 含 `.easy_tdx` 字面量」或「引用了已污染变量」的变量名
   （能跨 `x = Path.home()/'.easy_tdx'` → `y = x/'config.json'` 两跳传播，也覆盖 `with ... as f`）
2. 对污点对象上的写操作判 FAIL：`open(x,'w'/'a'/'x')`、`.write_text/.write_bytes/.write/.unlink/.mkdir/...`、
   `json.dump(obj, 污点fp)`、`os.remove/shutil.copy` 到污点目标
3. **读操作全部放行** —— NF-20 明确允许只读复用 known_hosts

`mode` 是变量无法静态确定时，按 fail-loud 精神**视为可写**（宁可误报也不漏报）。

---

### R4 — fail-loud 无兜底（NF-26，全项目第一原则）

**约束原文**（PRD §5.0 总则）：
> **NF-26 是全项目通用原则，优先级高于本章其余所有条目。**
> 禁止各模块自行 `try/except: pass` 或 `dict.get(key, 默认值)` 兜底。
> 对可能取不到值的查表操作，**禁止提供"看起来合理"的兜底默认值**。

**上游 T2 陷阱回顾**：上游 `_detect_security_type` 的 docstring 明确写了"不要误判"的
防御意图，却被调用点一个 `.get(..., (0.01, 0.01))` 原样抵消 —— **防御意图在调用点失效**，
`bj` 前缀同时绕过 sz/sh 两个分支，整个北交所静默按 A 股系数解码。
R4 就是把这类"调用点抵消防御意图"的写法在 CI 里钉死。

| 子规则 | 命中形态 |
|--------|---------|
| R4-A | `except ...: pass` / `except ...: ...`（含裸 `except:`，允许前置 docstring） |
| R4-B | `x.get(key, default)` **双位置参**（单参 `.get(k)` 放行，那是推荐写法） |
| R4-C | `contextlib.suppress(...)`（与 R4-A 等价） |
| R4-D | **正向**：`core/fail_loud.py` 必须提供 `require_known` / `reject_unknown` |

**豁免机制**：`faillloud_allowlist.txt`（注：文件名沿用 team-lead 下发的拼写，三个 `l`）

```
文件路径:行号:豁免理由
```
- 行号可写 `*` 表示整文件豁免（**上限 3 条**，超过 `test_r4_allowlist_inventory_is_reviewable` 直接失败）
- 理由必须 **≥ 15 字**，且须引用约束编号（`NF-`/`R`/`T`/`PRD`/`§`）或语义前提，
  或引用配套守卫断言（`见 test_xxx`）；否则 `test_r4_allowlist_wellformed` 失败
  （team-lead 裁决 3）
- 本次未命中的豁免条目会在报告里列为「建议清理」，防止 allowlist 变僵尸清单

### R4 豁免通用要求：凡豁免必须有配套守卫断言（team-lead 裁决 1）

豁免**不是无条件放行**。每一条 R4 豁免都必须在理由里用 `见 test_xxx` 指向一条
**真实存在**的守卫断言（如 `test_r4_eventbus_guard_present`），由该断言担保「被豁免的
代码确实已经用别的机制守住了 fail-loud 语义」。由 `test_r4_allowlist_guard_assertion_exists`
校验被引用的 `test_xxx` 函数确实存在。

> 设计要点：**豁免由配套测试担保**。哪天有人把守卫断言删了，断言先红，
> 豁免自动失效——不会出现「断言没了但豁免还在」的真空地带。

以 eventbus 的 `.get(key, ())` 为例（team-lead 裁决 1 / 方案 C）：
1. 工程师先重构 eventbus——引入显式主题注册表，`publish`/`subscribe` 两侧都过
   `require_known(topic, self._known_topics)`，未声明主题立刻报错；
2. 新增 `test_r4_eventbus_guard_present` 守卫断言，AST 证明 `publish`/`subscribe`
   **都**存在 `require_known` 调用且位于 `.get(key, 默认)` 之前；
3. **只有该守卫断言通过**，才在 `faillloud_allowlist.txt` 加豁免，理由写：
   `已由 publish/subscribe 两侧 require_known 前置校验主题合法性，空元组仅表示已声明主题的零订阅合法态（团队裁决 C，见 test_r4_eventbus_guard_present）`

> 当前 `faillloud_allowlist.txt` **保持为空**——eventbus 尚未按方案 C 重构，
> 故 `.get(key, ())` 仍按 NF-26 字面判 FAIL，红线套件在此保持红灯直至重构落地。

---

### R5 — 无下单接口（NF-21）

**约束原文**：
> 代码库中**不得包含**任何券商交易接口、委托下单、资金划转相关实现或依赖。

**防误伤是本规则的核心难点**：下单词汇在量化项目里天然高频（回测撮合、信号方向、
选股结果字段都会出现 buy/sell）。因此只在**三个零歧义维度**判定：

| 子规则 | 维度 |
|--------|------|
| R5-A | **函数/方法定义名**（`FunctionDef.name`）—— 定义 `place_order()` 才是"具备下单能力" |
| R5-B | **REST 路由路径**（`@router.post("/...")` 的路径串）—— 对外开 `/api/v1/trade/order` 才是把能力暴露出去 |
| R5-C | **券商交易 SDK import**（easytrader / vnpy / tqsdk / xtquant / pytdx.trade …） |
| R5-D | `pyproject.toml` 依赖声明里出现券商 SDK |

**明确不扫**：数据结构字段名、字符串常量、dict 键。
所以 `ScreenResult.direction = "buy"`、`SIGNALS = {"buy": 1, "sell": -1}` **不会命中**
（已有专门的阴性样本用例守着）。

**回测语境区分**：路径含 `backtest`/`simul`/`paper`/`mock`/`replay` 的文件使用**收窄词表**，
放行 `buy`/`sell`/`order_target` 等回测引擎惯用命名，但 `place_order`/`submit_order`
这类真实下单 API 照样禁。

---

### R6 — 无硬编码 A 股常量（NF-5）

**约束原文**：
> 交易日历、交易时段（含午休）、涨跌幅限制、最小变动价位、每手股数、货币单位、
> 时区、复权口径、代码格式**一律由 `MarketProfile` 提供**，业务代码**严禁硬编码** A 股假设。

**扫描范围**：`data/` `factor/` `screen/` `monitor/` `api/`
**天然豁免**（内置，无需登记）：`core/**`、`adapters/**`、任何 `class *MarketProfile` 的**类体**

| 子规则 | 命中形态 | 判定方式 |
|--------|---------|---------|
| R6-A | `"CNY"` / `"RMB"` / `"人民币"` / `"¥"` | 高特异性，业务层**全域扫** |
| R6-B | `"Asia/Shanghai"` / `"PRC"` / `"CST"` / `"UTC+8"` / `"+08:00"` / `timedelta(hours=8)` | 高特异性，业务层**全域扫** |
| R6-C | `"09:15"` `"09:30"` `"11:30"` `"13:00"` `"15:00"` 等 / `time(9, 30)` | 高特异性，业务层**全域扫** |
| R6-D | `252` / `250` / `244`（年交易日数） | 高特异性，无条件 |
| R6-E | `0.1` / `0.2` / `0.05`（ST 5%）/ `0.3`（创业板/科创板 20%/30%） | **需语境**：同语句标识符含 `price_limit`/`limit_up`/`涨停`/… |
| R6-F | `100`（每手股数） | **需语境**：同语句标识符含 `lot_size`/`board_lot`/`每手`/… |
| R6-H | `"SSE"` / `"SZSE"` / `"SH"` / `"SZ"`（交易所标识） | 高特异性，业务层**全域扫** |
| R6-G | **正向**：`core/market.py` 必须覆盖 NF-5 全部要素 | |

**两档扫描（team-lead 裁决 2）**：
- **字符串 / 时间字面量（R6-A/B/C/H）业务层全域扫描、不收窄**——这些字面量在业务代码里
  出现，除了硬编码 A 股别无解释，误报率近零；收窄只会漏网。
- **裸数字（R6-E/F）保留语境过滤**——裸 `0.1`（IC 阈值）、裸 `100`（TopN / batch_size）
  在因子代码里到处都是，无条件判定会产生海量误报，最终导致大家给整个目录加豁免。
  只有当数字确实出现在"涨跌停 / 每手"语义的标识符旁边时才判 FAIL。

---

## 文件清单

| 文件 | 作用 |
|------|------|
| `_scan.py` | 共享扫描基础设施：路径锚点、文件遍历、AST 解析缓存、`Violation`、`Recorder`、`Allowlist`、AST 小工具 |
| `conftest.py` | pytest 配置：`--redline-json` 选项、marker 注册、terminal summary（扫描范围 / 豁免清单 / ADVISORY） |
| `envelope_validator.py` | **JSON 信封校验器**（NF-9/NF-12），供后续所有验收用例复用 |
| `test_r0_scan_health.py` | R0 元检查：防检查器静默失效 |
| `test_r1_coefficients.py` | R1 系数表无副本（NF-25） |
| `test_r2_upstream_confined.py` | R2 上游调用收敛（NF-1） |
| `test_r3_forbidden_api.py` | R3 上游禁用 API（NF-1/NF-20） |
| `test_r4_fail_loud.py` | R4 fail-loud 无兜底（NF-26） |
| `test_r5_no_trading.py` | R5 无下单接口（NF-21） |
| `test_r6_hardcoded_cn.py` | R6 无硬编码 A 股常量（NF-5） |
| `test_envelope_validator.py` | 信封校验器自检（37 例） |
| `test_checker_selftest.py` | **检查器自测**：R1–R6 阳性/阴性样本（46 例） |
| `test_spikes_advisory.py` | spike 代码 R1/R3/R4 预警（ADVISORY，不阻断） |
| `faillloud_allowlist.txt` | R4 豁免清单 |
| `hardcoded_cn_allowlist.txt` | R6 豁免清单 |
| `no_trading_allowlist.txt` | R5 豁免清单（应永远为空） |

---

## JSON 信封校验器（NF-9 / NF-12）

```python
from envelope_validator import validate_envelope, validate_envelope_json, assert_envelope

problems = validate_envelope(obj)        # -> list[str]，空列表表示通过
problems = validate_envelope_json(text)  # 额外抓裸 NaN/Infinity token
assert_envelope(obj)                     # 便捷断言，不通过时列出全部违规
```

校验内容：

1. **顶层结构**：`{code, message, data, meta}` 四键齐全；默认**不容忍额外键**
   （`allow_extra_top_keys=True` 可放宽）；`code` 必须是 int（`True` 会被拒——
   bool 是 int 子类，这是个真实坑）；`message` 必须是 str
2. **meta 必填**：`generated_at`（ISO8601 可解析）/ `data_date`（`YYYY-MM-DD` 或 null）/
   `market`（非空 str，NF-6）/ `elapsed_ms`（非负数值）/ `version`（str）
3. **NF-12 全树递归**：任意深度的 `NaN` / `Infinity` / `-Infinity` → 违规
   （含把它们转成字符串 `"NaN"` 的伪装写法）；原始文本模式还会抓
   `json.dumps` 默认写出的**裸 token**（标准 JSON 解析器根本读不了）
4. **浮点精度**：小数位 > 6 → 违规。用 `repr` 判位数，避开二进制浮点误差误报

违规描述带 JSONPath 定位，例如：

```
[NF-12] $.data.factors[0].series[2] = NaN：JSON 不允许非有限数值，必须序列化为 null
```

**设计取向**：返回列表而非抛异常 —— 既能在单测里 `assert not validate_envelope(r)`，
也能在批量验收里聚合统计"多少个接口违反了 NF-9"。

---

## 给工程师的话

红线报警**不是找茬**，每一条背后都有一个 PRD 里写明的、会导致真金白银损失的失效场景。
如果你认为某条报警是误报：

1. 先看报警文案里的「整改方向」——多数情况有现成的合规写法
2. 确实是误报的，在对应 allowlist 里按 `文件:行号:理由` 登记；**理由 ≥ 15 字**，
   且须引用约束编号（`NF-`/`R`/`T`/`PRD`/`§`）或语义前提，或引用配套守卫断言
   （`见 test_xxx`）。R4 豁免**必须有**守卫断言（见上文"凡豁免必须有配套守卫断言"）
3. 认为规则本身定义有问题的，找 team-lead 讨论，**不要改检查器代码来让它闭嘴**
