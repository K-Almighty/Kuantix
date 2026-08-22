"""R6 —— 无硬编码 A 股常量（NF-5）。

约束来源
--------
PRD NF-5：「交易日历、交易时段（含午休）、涨跌幅限制、最小变动价位、每手股数、
货币单位、时区、复权口径、代码格式**一律由 `MarketProfile` 提供**，业务代码
**严禁硬编码** A 股假设（如 ±10%、100 股一手、252 交易日、CNY、UTC+8）。」
PRD NF-7：「P0 阶段三市场接口全部定义到位并有 A 股完整实现，港美股以"未实现"
显式抛错（**而非静默降级为 A 股规则**）。」
system_design §8：「市场抽象（NF-5）：……业务代码禁硬编码 A 股常量。」

扫描范围与豁免
--------------
- **扫**：``data/`` ``factor/`` ``screen/`` ``monitor/`` ``api/``（业务层）
- **不扫**：``core/`` ``adapters/``（``core/market.py`` 与 ``CNMarketProfile``
  本来就是 A 股常量的合法归属地）
- 业务层里若有 ``*MarketProfile`` 子类定义，其类体整体豁免（NF-5 允许实现档案）

判定逻辑（一句话）
------------------
**两档扫描（team-lead 裁决 2，字符串与时间字面量全域扫、裸数字保留语境收窄）**：

- **字符串 / 时间字面量**——``"CNY"`` ``"Asia/Shanghai"`` ``"09:30"`` ``"SSE"`` …
  → **业务层全域扫描，不收窄**。这些字面量出现在业务代码里，除了硬编码 A 股
  别无解释，误报率近零；收窄只会漏网。
- **裸数字**——``0.1`` ``0.2`` ``0.05`` ``0.3`` ``100`` … → **保留语境过滤**，
  仅当它被赋给/比较/传给「涨跌停」「每手」语义的标识符时才算。因子计算里
  阈值/权重/百分比满天飞，全域扫必然淹没在误报里。

剩余争议由 ``hardcoded_cn_allowlist.txt`` 逐条豁免。

细分子规则
----------
- **R6-A** 货币硬编码：``"CNY"`` / ``"RMB"`` / ``"人民币"`` / ``"¥"``
- **R6-B** 时区硬编码：``"Asia/Shanghai"`` / ``"PRC"`` / ``"CST"`` / ``"UTC+8"`` /
  ``"+08:00"`` / ``timedelta(hours=8)``
- **R6-C** 交易时段硬编码：``"09:15"`` ``"09:30"`` ``"11:30"`` ``"13:00"``
  ``"15:00"`` 等，或 ``time(9, 30)`` 形式
- **R6-D** 年交易日数硬编码：``252`` / ``250`` / ``244``
- **R6-E** 涨跌幅限制硬编码：``0.1`` / ``0.2`` / ``0.05``（ST 5%）/ ``0.3``
  （创业板/科创板 20%/30%）/**仅在涨跌停语境**（标识符含 limit/涨跌/pct_limit/…）
- **R6-F** 每手股数硬编码：``100`` **仅在每手语境**（标识符含 lot/board_lot/…）
- **R6-H** 交易所硬编码：``"SSE"`` / ``"SZSE"`` / ``"SH"`` / ``"SZ"``
  （业务层全域扫描，不收窄——交易所标识只应来自代码/MarketProfile）
"""

from __future__ import annotations

import ast
import re

import pytest

from _scan import (
    BUSINESS_DIRS,
    RECORDER,
    Violation,
    dotted_name,
    enclosing_class_ranges,
    fail_if,
    in_ranges,
    is_number,
    iter_py_files,
    load_allowlist,
    parse_module,
    require_package_root,
)

ALLOWLIST_FILE = "hardcoded_cn_allowlist.txt"

# --- 高特异性字符串常量 ----------------------------------------------------
CURRENCY_TOKENS = {"CNY", "RMB", "人民币", "￥", "¥"}
TZ_TOKENS = {
    "Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "PRC",
    "UTC+8", "UTC+08:00", "+08:00", "GMT+8", "CST",
}
SESSION_TOKENS = {
    "09:15", "09:25", "09:30", "11:30", "13:00", "14:57", "15:00",
    "9:15", "9:25", "9:30",
}
#: 交易所标识（业务层全域扫描，不收窄——team-lead 裁决 2 新增）
EXCHANGE_TOKENS = {"SSE", "SZSE", "SH", "SZ"}
#: 年交易日数
TRADING_DAYS_VALUES = {252, 250, 244}

# --- 语境正则（用于低特异性数字） ------------------------------------------
LIMIT_CTX_RE = re.compile(
    r"(price_limit|limit_up|limit_down|up_limit|down_limit|pct_limit|limit_pct|"
    r"zdf|zhangdie|涨跌|涨停|跌停|daily_limit|change_limit|max_change|"
    r"limit_ratio|price_cap|updown)",
    re.I,
)
LOT_CTX_RE = re.compile(
    r"(lot_size|board_lot|round_lot|min_lot|lot|每手|一手|shares_per|"
    r"min_qty|qty_step|trade_unit|hand_size)",
    re.I,
)
LIMIT_VALUES = {0.1, 0.2, 0.05, 0.3, 10.0, 20.0}
LOT_VALUES = {100}

RECORDER.note_scope(
    "R6",
    f"业务层扫描（{'/'.join(BUSINESS_DIRS)}）—— 禁硬编码 A 股常量，"
    "core/ 与 adapters/ 及 *MarketProfile 子类豁免（NF-5）",
)


def _allowlist():
    return load_allowlist(ALLOWLIST_FILE, "R6 硬编码 A 股常量")


def _business_files(pkg) -> list:
    out = []
    for d in BUSINESS_DIRS:
        sub = pkg / d
        if sub.is_dir():
            out.extend(iter_py_files(sub))
    return out


def _ctx_names(node: ast.AST) -> str:
    """把一个语句里出现的标识符拼成一串，供语境正则匹配。"""
    bits: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            bits.append(n.id)
        elif isinstance(n, ast.Attribute):
            bits.append(n.attr)
        elif isinstance(n, ast.keyword) and n.arg:
            bits.append(n.arg)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            bits.append(n.value)
    return " ".join(bits)


def _num_value(node: ast.AST) -> float | None:
    return float(node.value) if is_number(node) else None  # type: ignore[attr-defined]


def _scan_strings(path, tree: ast.Module, exempt) -> list[Violation]:
    out: list[Violation] = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            continue
        lineno = getattr(n, "lineno", 0)
        if in_ranges(lineno, exempt):
            continue
        val = n.value.strip()
        if val in CURRENCY_TOKENS:
            out.append(
                Violation(
                    "R6-A", path, lineno,
                    f'硬编码货币 "{val}"；NF-5 要求经 MarketProfile.currency 取得',
                )
            )
        elif val in TZ_TOKENS:
            out.append(
                Violation(
                    "R6-B", path, lineno,
                    f'硬编码时区 "{val}"；NF-5/NF-8 要求经 MarketProfile.timezone 取得'
                    "（内部统一 UTC 存储，仅展示层按市场时区渲染）",
                )
            )
        elif val in SESSION_TOKENS:
            out.append(
                Violation(
                    "R6-C", path, lineno,
                    f'硬编码 A 股交易时段 "{val}"；NF-5 要求经 MarketProfile 的'
                    "交易时段/session 定义取得（港美股时段完全不同）",
                )
            )
        elif val in EXCHANGE_TOKENS:
            out.append(
                Violation(
                    "R6-H", path, lineno,
                    f'硬编码交易所标识 "{val}"；NF-5 要求交易所来源自代码本身/MarketProfile，'
                    "业务层不得写死 SSE/SZSE/SH/SZ",
                )
            )
    return out


def _scan_numbers(path, tree: ast.Module, exempt) -> list[Violation]:
    out: list[Violation] = []

    # 年交易日数：特异性足够高，无条件判定
    for n in ast.walk(tree):
        if is_number(n) and not in_ranges(getattr(n, "lineno", 0), exempt):
            if n.value in TRADING_DAYS_VALUES and isinstance(n.value, int):
                out.append(
                    Violation(
                        "R6-D", path, n.lineno,
                        f"硬编码年交易日数 {n.value}；NF-5 要求经 "
                        "MarketProfile.trading_days_per_year 取得（美股 252、港股 247 各不同）",
                    )
                )

    # 涨跌停 / 每手：需要语境
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Compare, ast.Call, ast.Return, ast.BinOp)):
            continue
        lineno = getattr(node, "lineno", 0)
        if in_ranges(lineno, exempt):
            continue
        ctx = _ctx_names(node)
        has_limit_ctx = bool(LIMIT_CTX_RE.search(ctx))
        has_lot_ctx = bool(LOT_CTX_RE.search(ctx))
        if not (has_limit_ctx or has_lot_ctx):
            continue
        for n in ast.walk(node):
            if not is_number(n):
                continue
            v = _num_value(n)
            if has_limit_ctx and v in LIMIT_VALUES:
                out.append(
                    Violation(
                        "R6-E", path, getattr(n, "lineno", lineno),
                        f"涨跌幅限制硬编码 {v!r}（语境：`{ctx[:60]}`）；"
                        "NF-5 要求经 MarketProfile.price_limit 取得"
                        "（科创板/创业板 20%、ST 5%、港美股无涨跌停）",
                    )
                )
            elif has_lot_ctx and v in LOT_VALUES:
                out.append(
                    Violation(
                        "R6-F", path, getattr(n, "lineno", lineno),
                        f"每手股数硬编码 {int(v)}（语境：`{ctx[:60]}`）；"
                        "NF-5 要求经 MarketProfile.lot_size 取得（港股每手股数按标的而异）",
                    )
                )

    # 时区偏移：timedelta(hours=8) / timezone(timedelta(hours=8))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        lineno = getattr(node, "lineno", 0)
        if in_ranges(lineno, exempt):
            continue
        fname = dotted_name(node.func).split(".")[-1]
        if fname == "timedelta":
            for kw in node.keywords:
                if kw.arg == "hours" and is_number(kw.value) and kw.value.value == 8:
                    out.append(
                        Violation(
                            "R6-B", path, lineno,
                            "硬编码 UTC+8 偏移 `timedelta(hours=8)`；"
                            "NF-5/NF-8 要求经 MarketProfile.timezone 取得",
                        )
                    )
        elif fname in ("time", "datetime"):
            nums = [n for n in node.args if is_number(n)]
            if len(nums) >= 2:
                hm = (int(nums[0].value), int(nums[1].value))  # type: ignore[attr-defined]
                if hm in {(9, 15), (9, 25), (9, 30), (11, 30), (13, 0), (14, 57), (15, 0)}:
                    out.append(
                        Violation(
                            "R6-C", path, lineno,
                            f"硬编码 A 股交易时段 `{fname}({hm[0]}, {hm[1]})`；"
                            "NF-5 要求经 MarketProfile 的交易时段定义取得",
                        )
                    )
    return out


def _scan_file(path, tree: ast.Module) -> list[Violation]:
    # *MarketProfile 子类的类体整体豁免（NF-5 允许在市场档案里写各市场常量）
    exempt = enclosing_class_ranges(tree, "MarketProfile")
    return _scan_strings(path, tree, exempt) + _scan_numbers(path, tree, exempt)


def test_r6_allowlist_wellformed():
    """R6 豁免清单自检：格式必须是 `文件:行号:理由`。"""
    al = _allowlist()
    if al.malformed:
        detail = "\n".join(
            f"  {al.path.name}:{ln}  {reason}\n        │ {raw}" for ln, raw, reason in al.malformed
        )
        pytest.fail(
            f"\n【R6 豁免清单格式错误】{len(al.malformed)} 条：\n{detail}\n", pytrace=False
        )


def test_r6_no_hardcoded_cn_constants():
    """R6-A~F：业务层禁止硬编码 A 股特化常量。"""
    pkg = require_package_root()
    files = _business_files(pkg)
    if not files:
        pytest.skip(
            f"业务层目录（{'/'.join(BUSINESS_DIRS)}）尚未落地，NF-5 硬编码检查暂不适用"
        )

    al = _allowlist()
    raw: list[Violation] = []
    for path in files:
        tree = parse_module(path)
        if tree is None:
            continue
        raw.extend(_scan_file(path, tree))

    violations = al.filter(raw)
    exempted = len(raw) - len(violations)
    fail_if(
        violations,
        "R6",
        f"【R6 / NF-5 市场规则外置】扫描业务层 {len(files)} 个文件，"
        f"命中 {len(raw)} 处硬编码，其中 {exempted} 处已豁免。\n"
        "整改方向：一律改为从 MarketProfile 取（currency / timezone / price_limit /\n"
        "lot_size / trading_days_per_year / 交易时段），A 股具体值只允许写在\n"
        "core/market.py 的 CNMarketProfile 里。",
    )


def test_r6_market_profile_contract_complete():
    """R6-G（正向）：core/market.py 的 MarketProfile 必须覆盖 NF-5 全部要素。"""
    pkg = require_package_root()
    target = pkg / "core" / "market.py"
    if not target.exists():
        pytest.skip("core/market.py 尚未落地（T02 交付物），正向断言待代码就位后生效")

    tree = parse_module(target)
    assert tree is not None, f"{target} 语法错误"

    src = ast.unparse(tree)
    required = {
        "currency": "货币单位",
        "timezone": "时区",
        "price_limit": "涨跌幅限制",
        "lot_size": "每手股数",
        "tick_size": "最小变动价位",
        "trading_days_per_year": "年交易日数",
        "is_trading_day": "交易日历",
    }
    missing = {k: v for k, v in required.items() if k not in src}
    assert not missing, (
        f"【R6-G / NF-5】core/market.py 的市场档案缺少要素："
        f"{ {k: v for k, v in missing.items()} }。\n"
        "NF-5 列举的要素（交易日历/交易时段/涨跌幅/最小变动价位/每手股数/货币/时区/"
        "复权口径/代码格式）必须由 MarketProfile 提供，否则业务层必然回退到硬编码。"
    )
