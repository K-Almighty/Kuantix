"""R5 —— 无下单接口（NF-21）。

约束来源
--------
PRD NF-21：「代码库中**不得包含**任何券商交易接口、委托下单、资金划转相关实现
或依赖。导出清单为纯文本文件产物。」
PRD Q2 决策：「**不对接券商 API**。采用「导出可下单清单」方案……**不做订单状态机、
不做资金校验、不触碰真实下单接口**。」

判定维度（关键：不能误伤）
--------------------------
下单相关词汇在量化项目里天然高频（回测撮合、信号方向、选股结果字段都会出现
buy/sell）。因此本规则**只在两个维度判定**，不扫任何数据结构字段名：

1. **函数/方法定义名**（``FunctionDef`` / ``AsyncFunctionDef`` 的 name）
   —— 定义一个叫 ``place_order`` 的函数才是「具备下单能力」的信号；
   ``ScreenResult.direction = "buy"`` 这种字段值不算。
2. **对外暴露的 API 路由路径**（FastAPI ``@router.post("/...")`` 装饰器里的路径串）
   —— 对外开一个 ``/api/v1/trade/order`` 端点才是真的把能力暴露出去。

外加第三个维度：**券商交易 SDK 的 import**（这个是零歧义的硬证据）。

回测/模拟撮合的区分
-------------------
路径中含 ``backtest`` / ``simul`` / ``paper`` 的文件（如
``adapters/backtest_bridge.py``）使用**收窄词表**：只禁真实下单 API 名
（place_order / submit_order / …），放行 ``buy`` / ``sell`` / ``order_target``
这类回测引擎惯用命名——上游 ``easy_tdx.backtest`` 的模拟撮合本就用这套词。

细分子规则
----------
- **R5-A** 定义了下单类函数/方法
- **R5-B** REST 路由路径暴露交易语义
- **R5-C** import 券商交易 SDK
"""

from __future__ import annotations

import ast
import re

from _scan import (
    RECORDER,
    Violation,
    dotted_name,
    fail_if,
    iter_py_files,
    load_allowlist,
    parse_module,
    require_package_root,
)

#: 真实下单 API —— 任何上下文（含回测文件）都禁止定义
HARD_ORDER_NAMES = frozenset(
    {
        "place_order", "submit_order", "send_order", "insert_order", "order_insert",
        "make_order", "create_order", "cancel_order", "revoke_order", "withdraw_order",
        "trade_execute", "execute_trade", "place_trade", "do_trade", "send_trade",
        "entrust", "entrust_order", "wt_order", "real_trade", "live_trade",
        "transfer_fund", "fund_transfer", "withdraw_cash", "deposit_cash",
        "connect_broker", "broker_login", "trade_login",
    }
)

#: 模拟撮合语境下可放行、生产代码里禁止定义的名字
SOFT_ORDER_NAMES = frozenset(
    {
        "buy", "sell", "short", "cover", "open_position", "close_position",
        "order_target", "order_target_percent", "order_target_value",
        "order_value", "order_shares", "order_percent",
    }
)

#: 路径中含这些片段的文件视为「模拟撮合语境」，使用收窄词表
SIMULATION_PATH_RE = re.compile(r"(backtest|simul|paper|mock|replay)", re.I)

#: 券商交易 SDK（零歧义硬证据）
BROKER_SDK_ROOTS = frozenset(
    {
        "easytrader", "vnpy", "ctpbee", "tqsdk", "xtquant", "miniqmt",
        "jqtrade", "ths_trader", "thstrader", "pywinauto", "gm",
        "trader_api", "ctp", "sinopac", "shioaji", "ib_insync", "ibapi",
        "alpaca_trade_api", "futu",
    }
)
#: pytdx 只有交易子模块是禁的（行情子模块由 R2 管）
BROKER_SDK_SUBMODULES = ("pytdx.trade", "pytdx.exhq_trade")

#: 路由路径里出现这些片段即视为暴露交易能力
ROUTE_TRADE_RE = re.compile(
    r"/(order|orders|trade|trading|buy|sell|entrust|position/open|position/close|fund/transfer)(/|$|\?)",
    re.I,
)
HTTP_METHOD_DECORATORS = frozenset({"post", "put", "patch", "delete", "get", "api_route"})

ALLOWLIST_FILE = "no_trading_allowlist.txt"

RECORDER.note_scope(
    "R5", "全包扫描 —— 函数定义名 / REST 路由 / 券商 SDK 三维度禁下单（NF-21）"
)


def _allowlist():
    return load_allowlist(ALLOWLIST_FILE, "R5 无下单接口")


def _forbidden_names_for(path) -> frozenset[str]:
    if SIMULATION_PATH_RE.search(str(path)):
        return HARD_ORDER_NAMES
    return HARD_ORDER_NAMES | SOFT_ORDER_NAMES


def _scan_file(path, tree: ast.Module) -> list[Violation]:
    out: list[Violation] = []
    forbidden = _forbidden_names_for(path)
    simulated = bool(SIMULATION_PATH_RE.search(str(path)))

    for node in ast.walk(tree):
        # --- R5-A: 函数/方法定义 -----------------------------------------
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bare = node.name.lstrip("_")
            if bare in forbidden:
                tier = "真实下单 API" if bare in HARD_ORDER_NAMES else "下单动作"
                out.append(
                    Violation(
                        "R5-A", path, node.lineno,
                        f"定义了{tier}函数 `{node.name}()`；NF-21 明确 Kuantix 不得包含任何"
                        "委托下单/资金划转实现。交易意图只能落成「导出可下单清单」纯文本产物",
                    )
                )

            # --- R5-B: 路由装饰器 ---------------------------------------
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                dname = dotted_name(deco.func)
                if dname.split(".")[-1] not in HTTP_METHOD_DECORATORS:
                    continue
                for arg in deco.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if ROUTE_TRADE_RE.search(arg.value):
                            out.append(
                                Violation(
                                    "R5-B", path, deco.lineno,
                                    f"REST 路由 `{arg.value}` 对外暴露交易语义；"
                                    "NF-21 禁止提供任何下单端点",
                                )
                            )

        # --- R5-C: 券商 SDK import --------------------------------------
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BROKER_SDK_ROOTS or alias.name.startswith(BROKER_SDK_SUBMODULES):
                    out.append(
                        Violation(
                            "R5-C", path, node.lineno,
                            f"import 券商交易 SDK `{alias.name}`；NF-21 禁止任何交易接口依赖",
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                continue
            root = mod.split(".")[0]
            if root in BROKER_SDK_ROOTS or mod.startswith(BROKER_SDK_SUBMODULES):
                out.append(
                    Violation(
                        "R5-C", path, node.lineno,
                        f"from-import 券商交易 SDK `{mod}`；NF-21 禁止任何交易接口依赖",
                    )
                )

    if simulated and out:
        for i, v in enumerate(out):
            out[i] = Violation(
                v.rule, v.path, v.lineno,
                v.message + "（注：本文件属模拟撮合语境，已放行 buy/sell 类命名，此处命中的是真实下单 API）",
                v.severity,
            )
    return out


def test_r5_no_order_placement():
    """R5-A~C：函数定义名 / REST 路由 / 券商 SDK 三维度扫描。"""
    require_package_root()
    al = _allowlist()
    files = iter_py_files()

    raw: list[Violation] = []
    for path in files:
        tree = parse_module(path)
        if tree is None:
            continue
        raw.extend(_scan_file(path, tree))

    violations = al.filter(raw)
    fail_if(
        violations,
        "R5",
        f"【R5 / NF-21 严禁下单】扫描 {len(files)} 个文件后发现下单能力痕迹。\n"
        "判定维度只有三个（不扫数据结构字段名，避免误伤 ScreenResult.direction 等）：\n"
        "  ① 函数/方法定义名  ② REST 路由路径  ③ 券商交易 SDK import\n"
        "PRD Q2 决策：交易意图只能导出为券商通用 CSV 纯文本，人工确认后手动导入交易软件。",
    )


def test_r5_no_broker_dependency_declared():
    """R5-D：pyproject.toml 依赖里不得出现券商交易 SDK。"""
    import pytest

    from _scan import PROJECT_ROOT, read_source

    pyproject = PROJECT_ROOT / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("pyproject.toml 尚未落地（T01 交付物）")

    text = read_source(pyproject).lower()
    hits = sorted({sdk for sdk in BROKER_SDK_ROOTS if re.search(rf"^\s*[\"']?{re.escape(sdk)}\b", text, re.M)})
    assert not hits, (
        f"【R5-D / NF-21】pyproject.toml 声明了券商交易 SDK 依赖：{hits}。\n"
        "NF-21 要求代码库不得包含任何券商交易接口相关**依赖**。"
    )
