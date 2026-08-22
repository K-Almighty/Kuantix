"""R1 —— 系数表无副本（NF-25，硬性不可协商，红线优先级最高）。

约束来源
--------
PRD NF-25：「任何写入 L1 的路径，其使用的价格/量系数必须与上游读侧
``_SECURITY_COEFFICIENTS`` 的判定结果一致。系数表**必须从上游 import 引用，
严禁在 Kuantix 侧复制、粘贴或硬编码任何副本**。」
system_design §8：「代码库不得存在系数表副本（CI 静态检查）。」

为什么这条排第一
----------------
上游 ``easy_tdx/offline/daily_bar.py::_SECURITY_COEFFICIENTS`` 是**读侧**解码表。
一旦 Kuantix 复制一份用于**写侧**编码，而上游后续修订代码段划分（补北交所、
新增 ETF 段），副本就变成错的**且不会报错**——写进去的价格会 ×10 / 量会 ×100，
所有因子值、回测结论、选股清单静默错误。这是 PRD §0.3.1 的 T1 陷阱。

判定逻辑（一句话）
------------------
源码里出现「上游系数值对」（如 ``(0.01, 0.01)``）被赋值进 dict / 变量 / 常量，
或出现名字含 ``COEFFICIENT`` 的本地字典字面量，或本地重定义
``_SECURITY_COEFFICIENTS`` → FAIL；同时正向要求 ``adapters/coefficients.py``
存在对上游 ``_SECURITY_COEFFICIENTS`` 的 import。

细分子规则
----------
- **R1-A** dict 字面量里出现 ≥1 个上游系数值对，且键形如证券类型 → 疑似系数表副本
- **R1-B** 名字匹配 ``*COEFFICIENT*`` / ``*_COEFF(S)`` 的变量被赋值为 dict 字面量
- **R1-C** 本地重新定义 ``_SECURITY_COEFFICIENTS``（任何形式）
- **R1-D** 上游系数值对作为元组字面量直接赋值给变量
- **R1-E** 写盘类函数调用中直接传入两个字面量系数（如 ``sync_xxx(f, bars, 0.01, 0.01)``）
- **R1-F**（正向）``adapters/coefficients.py`` 必须 import 上游 ``_SECURITY_COEFFICIENTS``
"""

from __future__ import annotations

import ast
import re

import pytest

from _scan import (
    RECORDER,
    Violation,
    dotted_name,
    fail_if,
    is_number,
    iter_py_files,
    parse_module,
    require_package_root,
)

# 系数值对**从上游动态派生**，而不是在测试里再抄一份——
# 检查器自己也必须遵守 NF-25 的精神。
try:
    from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS as _UPSTREAM_COEFFS
except ImportError:  # pragma: no cover - 上游未安装时整条规则跳过
    _UPSTREAM_COEFFS = None

COEFF_NAME_RE = re.compile(r"(COEFFICIENT|_COEFFS?$|COEFF_TABLE|SECURITY_COEFF)", re.I)
SEC_TYPE_KEY_RE = re.compile(
    r"^(SH|SZ|BJ|HK|US)_(A_STOCK|B_STOCK|INDEX|FUND|BOND|STOCK)$|"
    r"(A_STOCK|B_STOCK|INDEX|FUND|BOND|ETF|UNKNOWN)",
    re.I,
)
WRITE_FUNC_RE = re.compile(
    r"(sync|append|write|encode|save|dump).*(bar|daily|min|ex|vipdoc)|"
    r"(bar|daily|min|vipdoc).*(sync|append|write|encode|save)",
    re.I,
)

RECORDER.note_scope("R1", "全包扫描 —— 禁止任何形式的上游系数表副本（NF-25）")


def _upstream_pairs() -> frozenset[tuple[float, float]]:
    if _UPSTREAM_COEFFS is None:
        pytest.skip("上游 easy_tdx 未安装，无法派生系数基准值 —— R1 跳过")
    return frozenset(_UPSTREAM_COEFFS.values())


def _is_coeff_pair(node: ast.AST, pairs: frozenset[tuple[float, float]]) -> bool:
    """判断节点是否为形如 ``(0.01, 0.01)`` 的上游系数值对字面量。"""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return False
    if len(node.elts) != 2 or not all(is_number(e) for e in node.elts):
        return False
    pair = (float(node.elts[0].value), float(node.elts[1].value))  # type: ignore[attr-defined]
    return pair in pairs


def _scan_file(path, tree: ast.Module, pairs) -> list[Violation]:
    out: list[Violation] = []

    for node in ast.walk(tree):
        # --- R1-A / R1-B: dict 字面量 -------------------------------------
        if isinstance(node, ast.Dict):
            coeff_values = [v for v in node.values if _is_coeff_pair(v, pairs)]
            if coeff_values:
                keylike = any(
                    isinstance(k, ast.Constant)
                    and isinstance(k.value, str)
                    and SEC_TYPE_KEY_RE.search(k.value)
                    for k in node.keys
                    if k is not None
                )
                if keylike or len(coeff_values) >= 2:
                    out.append(
                        Violation(
                            "R1-A",
                            path,
                            node.lineno,
                            f"疑似系数表副本：dict 字面量含 {len(coeff_values)} 个上游系数值对。"
                            "必须改为 `from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS`",
                        )
                    )

        # --- R1-B/C/D: 赋值语句 -------------------------------------------
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value

        for tgt in targets:
            name = dotted_name(tgt) or getattr(tgt, "id", "")
            short = name.split(".")[-1]
            if not short:
                continue
            if short == "_SECURITY_COEFFICIENTS" and value is not None:
                # 允许 `_SECURITY_COEFFICIENTS = <import 来的名字>` 这种再导出
                if not isinstance(value, (ast.Name, ast.Attribute)):
                    out.append(
                        Violation(
                            "R1-C",
                            path,
                            node.lineno,
                            "本地重新定义 `_SECURITY_COEFFICIENTS`：NF-25 要求从上游 import 引用，"
                            "禁止在 Kuantix 侧构造同名表",
                        )
                    )
            elif COEFF_NAME_RE.search(short) and isinstance(value, (ast.Dict,)):
                out.append(
                    Violation(
                        "R1-B",
                        path,
                        node.lineno,
                        f"本地系数字典字面量 `{short}`：NF-25 禁止在 Kuantix 侧维护系数表副本",
                    )
                )
            elif value is not None and _is_coeff_pair(value, pairs):
                out.append(
                    Violation(
                        "R1-D",
                        path,
                        node.lineno,
                        f"硬编码上游系数值对赋给 `{short}`：必须经 "
                        "`adapters.coefficients` 由上游表查得",
                    )
                )

        # --- R1-E: 写盘调用直接传字面量系数 --------------------------------
        if isinstance(node, ast.Call):
            fname = dotted_name(node.func).split(".")[-1]
            if fname and WRITE_FUNC_RE.search(fname):
                nums = [a for a in node.args if is_number(a)]
                for i in range(len(nums) - 1):
                    pair = (float(nums[i].value), float(nums[i + 1].value))  # type: ignore[attr-defined]
                    if pair in pairs:
                        out.append(
                            Violation(
                                "R1-E",
                                path,
                                node.lineno,
                                f"写盘调用 `{fname}(...)` 直接传入字面量系数 {pair}："
                                "系数必须由上游表按证券类型查得（NF-25 编解码对称）",
                            )
                        )
                        break
    return out


def test_r1_no_coefficient_table_copy():
    """R1-A~E：全包扫描，任何系数表副本 / 硬编码系数值对一律 FAIL。"""
    require_package_root()
    pairs = _upstream_pairs()
    files = iter_py_files()

    violations: list[Violation] = []
    for path in files:
        tree = parse_module(path)
        if tree is None:
            continue
        violations.extend(_scan_file(path, tree, pairs))

    fail_if(
        violations,
        "R1",
        f"【R1 / NF-25 系数表无副本】扫描 {len(files)} 个文件后发现系数表副本或硬编码系数。\n"
        "上游基准（只读引用）：easy_tdx.offline.daily_bar._SECURITY_COEFFICIENTS\n"
        "整改方向：统一经 Kuantix/adapters/coefficients.py 从上游 import 引用。",
    )


def test_r1f_adapters_coefficients_imports_upstream():
    """R1-F（正向断言）：adapters/coefficients.py 必须引用上游系数表。"""
    pkg = require_package_root()
    target = pkg / "adapters" / "coefficients.py"
    if not target.exists():
        pytest.skip(f"{target.name} 尚未落地（T02 交付物），正向断言待代码就位后生效")

    tree = parse_module(target)
    assert tree is not None, f"{target} 存在语法错误，无法解析"

    imported_symbol = False
    imported_module = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("easy_tdx"):
                imported_module = True
                if any(a.name == "_SECURITY_COEFFICIENTS" for a in node.names):
                    imported_symbol = True
                if any(a.name == "daily_bar" for a in node.names):
                    imported_module = True
        elif isinstance(node, ast.Import):
            if any(a.name.startswith("easy_tdx") for a in node.names):
                imported_module = True

    # 允许 `import easy_tdx.offline.daily_bar as m` + `m._SECURITY_COEFFICIENTS`
    if not imported_symbol and imported_module:
        imported_symbol = any(
            isinstance(n, ast.Attribute) and n.attr == "_SECURITY_COEFFICIENTS"
            for n in ast.walk(tree)
        )

    assert imported_symbol, (
        "【R1-F / NF-25】adapters/coefficients.py 未见对上游 `_SECURITY_COEFFICIENTS` 的引用。\n"
        "要求写成：`from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS`\n"
        "（NF-25 不可协商：系数必须引用上游，不得复制。）"
    )
