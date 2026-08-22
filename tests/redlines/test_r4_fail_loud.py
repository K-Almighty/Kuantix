"""R4 —— fail-loud 无兜底（NF-26，全项目第一原则）。

约束来源
--------
PRD §5.0 总则：「**NF-26 是全项目通用原则，不限于 DataLake，优先级高于本章其余
所有条目。**」「一切无法确定正确性的情形……一律显式报错 + 跳过 + 记录异常日志与
隔离区，**严禁静默兜底、静默降级、静默使用默认值**。」
落地要求：「禁止各模块自行 `try/except: pass` 或 `dict.get(key, 默认值)` 兜底。」
代码审查红线：「对可能取不到值的查表操作，**禁止提供"看起来合理"的兜底默认值**
（这正是上游 T2 陷阱的成因）。」

上游 T2 陷阱回顾（PRD §0.3.1）
------------------------------
上游 `_detect_security_type` 的 docstring 明确写了「不要误判」的防御意图，
却被调用点一个 `.get(..., (0.01, 0.01))` 原样抵消——**防御意图在调用点失效**，
`bj` 前缀同时绕过 sz/sh 两个分支，导致整个北交所静默按 A 股系数解码。
R4 就是把这类「调用点抵消防御意图」的写法在 CI 里钉死。

判定逻辑（一句话）
------------------
AST 扫描两类反模式——① ``ExceptHandler`` 的 body 只有 ``pass``/``...``；
② 双位置参 ``x.get(key, default)``——**默认全部 FAIL**，只能通过
``faillloud_allowlist.txt`` 按 ``文件:行号:理由`` 显式豁免。

细分子规则
----------
- **R4-A** ``except ...: pass`` / ``except ...: ...``（吞掉异常，零处理）
- **R4-B** ``x.get(key, default)`` 双位置参兜底取值
- **R4-C** ``contextlib.suppress(...)``（与 R4-A 等价的静默吞异常写法）

豁免机制
--------
``tests/redlines/faillloud_allowlist.txt``，格式 ``文件:行号:理由``：
- 行号可写 ``*`` 表示整文件豁免（强烈不建议，会在报告里标出来，且上限 3 条）
- 理由 >= 15 字，且须引用约束编号（NF-/R/T/PRD/§）或语义前提，
  或引用配套守卫断言（``见 test_xxx``）；否则 ``test_r4_allowlist_wellformed`` 失败
- **凡 R4 豁免必须有配套守卫断言**（team-lead 裁决 1/3）：理由里 ``见 test_xxx``
  指向的测试函数必须真实存在，否则 ``test_r4_allowlist_guard_assertion_exists`` 失败。
  豁免不是无条件的——哪天守卫断言被删，豁免自动失效。
- 本次运行未命中的豁免条目会在 terminal summary 里列为「建议清理」
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from _scan import (
    REDLINES_DIR,
    RECORDER,
    Violation,
    dotted_name,
    fail_if,
    iter_py_files,
    load_allowlist,
    parse_module,
    require_package_root,
)

ALLOWLIST_FILE = "faillloud_allowlist.txt"

#: 从「见 test_xxx」里抓出被引用的守卫断言函数名
_GUESS_GUARD_RE = re.compile(r"见\s*(test_\w+)")

RECORDER.note_scope(
    "R4", "全包扫描 —— 禁 try/except:pass 与 .get(k, 默认值)（NF-26），支持 allowlist 豁免"
)


def _allowlist():
    return load_allowlist(ALLOWLIST_FILE, "R4 fail-loud")


def _body_is_silent(body: list[ast.stmt]) -> bool:
    """body 是否等价于「什么都不做」（允许前置 docstring）。"""
    stmts = list(body)
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]
    if len(stmts) != 1:
        return False
    only = stmts[0]
    if isinstance(only, ast.Pass):
        return True
    if isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant):
        return only.value.value is Ellipsis
    return False


def _handler_label(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "except:"
    name = dotted_name(handler.type)
    if not name and isinstance(handler.type, ast.Tuple):
        name = ", ".join(dotted_name(e) for e in handler.type.elts)
    return f"except {name or '<expr>'}:"


def _scan_file(path, tree: ast.Module) -> list[Violation]:
    out: list[Violation] = []

    for node in ast.walk(tree):
        # --- R4-A: except ...: pass -------------------------------------
        if isinstance(node, ast.ExceptHandler) and _body_is_silent(node.body):
            out.append(
                Violation(
                    "R4-A", path, node.lineno,
                    f"`{_handler_label(node)} pass` 静默吞异常（NF-26 禁止）。"
                    "整改：要么向上抛，要么记录异常日志 + 计入隔离区（NF-27），"
                    "要么用 core.fail_loud 的 require_known/reject_unknown 显式拒绝",
                )
            )

        # --- R4-B: x.get(key, default) ----------------------------------
        elif isinstance(node, ast.Call):
            fn = dotted_name(node.func)
            short = fn.split(".")[-1]

            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) == 2
            ):
                recv = dotted_name(node.func.value) or "<expr>"
                default_src = ast.unparse(node.args[1])
                out.append(
                    Violation(
                        "R4-B", path, node.lineno,
                        f"`{recv}.get(..., {default_src})` 双参兜底取值（NF-26 禁止静默默认值）。"
                        "这正是上游 T2 陷阱的成因：查表取不到时给一个'看起来合理'的默认值，"
                        "错误会一路静默流进因子/回测/选股。"
                        "整改：改用单参 .get() + 显式判空报错，或 core.fail_loud.require_known()",
                    )
                )

            # --- R4-C: contextlib.suppress ------------------------------
            if short == "suppress" and ("contextlib" in fn or fn == "suppress"):
                out.append(
                    Violation(
                        "R4-C", path, node.lineno,
                        f"`{fn}(...)` 与 `except: pass` 等价，同样静默吞异常（NF-26 禁止）",
                    )
                )

    return out


def scan_r4(paths, severity: str = "FAIL") -> list[Violation]:
    """对给定文件列表跑 R4 全部子规则（供 spike 预警复用）。"""
    out: list[Violation] = []
    for path in paths:
        tree = parse_module(path)
        if tree is None:
            continue
        out.extend(_scan_file(path, tree))
    if severity != "FAIL":
        out = [Violation(v.rule, v.path, v.lineno, v.message, severity) for v in out]
    return out


def test_r4_allowlist_wellformed():
    """R4 豁免清单自检：格式必须是 `文件:行号:理由`，理由 >=15 字且非敷衍。"""
    al = _allowlist()
    if al.malformed:
        detail = "\n".join(
            f"  {al.path.name}:{ln}  {reason}\n        │ {raw}" for ln, raw, reason in al.malformed
        )
        pytest.fail(
            f"\n【R4 豁免清单格式错误】{len(al.malformed)} 条：\n{detail}\n"
            "格式要求：`文件:行号:理由`（行号可为 * 表示整文件）；\n"
            "理由 >= 15 字，且须引用约束编号(NF-/R/T/PRD/§)或语义前提，"
            "或引用守卫断言(见 test_xxx)（team-lead 裁决 3）。\n",
            pytrace=False,
        )


def test_r4_allowlist_guard_assertion_exists():
    """R4 豁免通用要求（裁决 1/3）：每条豁免须引用一个真实存在的守卫断言。

    理由里用 ``见 test_xxx`` 指向某条测试函数；该函数必须能在
    ``tests/redlines/test_*.py`` 里找到定义，否则豁免失去担保、应视为无效。
    """
    import glob

    # 收集 redlines 目录下所有 test_ 函数名
    defined: set[str] = set()
    for path in glob.glob(str(REDLINES_DIR / "test_*.py")):
        tree = parse_module(Path(path))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                defined.add(node.name)

    al = _allowlist()
    bad = []
    for e in al.entries:
        m = _GUESS_GUARD_RE.search(e.reason)
        if not m:
            bad.append((e, "R4 豁免理由必须引用配套守卫断言（见 test_xxx），不接受纯文字理由"))
            continue
        name = m.group(1)
        if name not in defined:
            bad.append((e, f"理由引用的守卫断言 {name} 在 tests/redlines/ 中不存在"))
    if bad:
        detail = "\n".join(f"  {e.raw}\n        │ {msg}" for e, msg in bad)
        pytest.fail(
            f"\n【R4 豁免缺少守卫断言】{len(bad)} 条：\n{detail}\n"
            "凡 R4 豁免必须有配套守卫断言（team-lead 裁决 1）：理由用「见 test_xxx」"
            "指向一条真实存在的断言；守卫断言存在，豁免才有担保。\n",
            pytrace=False,
        )


def test_r4_no_silent_fallback():
    """R4-A~C：禁止 try/except:pass 与 .get(k, 默认值) 静默兜底。"""
    require_package_root()
    al = _allowlist()
    files = iter_py_files()

    raw = scan_r4(files)
    violations = al.filter(raw)
    exempted = len(raw) - len(violations)

    headline = (
        f"【R4 / NF-26 fail-loud 无兜底】扫描 {len(files)} 个文件，"
        f"命中 {len(raw)} 处反模式，其中 {exempted} 处已豁免。\n"
        "NF-26 是全项目第一原则：宁可少一只标的，不可多一条错数据。\n"
        f"确有正当理由的，在 tests/redlines/{ALLOWLIST_FILE} 里按 `文件:行号:理由` 显式登记。"
    )
    fail_if(violations, "R4", headline)


def test_r4_allowlist_inventory_is_reviewable():
    """R4 豁免不得失控：整文件豁免（`*`）需要单独盯，数量超阈值直接告警。"""
    al = _allowlist()
    whole_file = [e for e in al.entries if e.lineno is None]
    assert len(whole_file) <= 3, (
        f"【R4 豁免治理】整文件豁免（行号写 `*`）共 {len(whole_file)} 条，超过阈值 3。\n"
        "整文件豁免会让该文件此后所有新增兜底代码全部免检，等于给 NF-26 开天窗。\n"
        "请改为逐行豁免：\n"
        + "\n".join(f"  {e.raw}" for e in whole_file)
    )


def test_r4_core_fail_loud_module_exists():
    """R4-D（正向）：core 层必须提供统一 fail-loud 工具，否则各模块必然各自兜底。"""
    pkg = require_package_root()
    target = pkg / "core" / "fail_loud.py"
    if not target.exists():
        pytest.skip("core/fail_loud.py 尚未落地（T02 交付物），正向断言待代码就位后生效")

    tree = parse_module(target)
    assert tree is not None, f"{target} 语法错误"
    funcs = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = {"require_known", "reject_unknown"} - funcs
    assert not missing, (
        f"【R4-D / NF-26 落地要求】core/fail_loud.py 缺少 {sorted(missing)}。\n"
        "PRD §5.0 明确：core 层必须提供统一的 fail-loud 工具"
        "（require_known(value, context) / reject_unknown(...)），所有模块强制使用。"
    )


def test_r4_eventbus_guard_present():
    """R4 豁免前置守卫（team-lead 裁决 1 / 方案 C）。

    eventbus 的 ``publish`` 与 ``subscribe`` 两侧都必须先用 ``require_known`` 校验
    主题合法性（未声明主题立刻报错），空元组 ``.get(key, ())`` 才仅是「已声明主题的
    零订阅」合法态。本断言是 ``faillloud_allowlist.txt`` 中 eventbus 豁免条目的担保：
    一旦有人删掉 ``require_known``，本条先红，豁免自动失效（凡豁免必有守卫）。

    在工程师按方案 C 重构 eventbus 之前，本测试优雅跳过——不强行制造红灯，
    也暂不往 allowlist 加 eventbus 豁免（裁决原文：现在先不要加豁免）。
    """
    pkg = require_package_root()
    target = pkg / "core" / "eventbus.py"
    if not target.exists():
        pytest.skip("core/eventbus.py 尚未落地，eventbus 守卫断言待重构后生效")

    tree = parse_module(target)
    assert tree is not None, f"{target} 语法错误"

    # 方案 C 的落地标志：eventbus 真正使用了 require_known 做主题校验
    if "require_known" not in ast.unparse(tree):
        pytest.skip(
            "eventbus 尚未按方案 C 重构（未使用 require_known 做主题校验），"
            "守卫断言待重构落地后生效；faillloud_allowlist.txt 的 eventbus 豁免暂不添加"
        )

    def _func_named(name):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        return None

    def _require_known_calls(node):
        return [
            n for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and dotted_name(n.func).split(".")[-1] == "require_known"
        ]

    def _two_arg_get_calls(node):
        return [
            n for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and len(n.args) == 2
        ]

    for meth in ("publish", "subscribe"):
        fn = _func_named(meth)
        assert fn is not None, (
            f"【eventbus 守卫缺失】{meth}() 方法不存在；方案 C 要求 publish/subscribe "
            "两侧都过 require_known 校验主题合法性"
        )
        reqs = _require_known_calls(fn)
        assert reqs, (
            f"【eventbus 守卫缺失】{meth}() 函数体内未找到 require_known 调用；"
            "方案 C 要求 publish/subscribe 两侧都先校验主题合法性，再允许 .get(key, ()) 的"
            "零订阅默认态（team-lead 裁决 1）"
        )
        # 守卫必须位于静默默认值之前：主题认不认识 > 空不空
        gets = _two_arg_get_calls(fn)
        if gets:
            assert min(c.lineno for c in reqs) < min(g.lineno for g in gets), (
                f"【eventbus 守卫顺序错误】{meth}() 中 require_known 必须出现在 "
                ".get(key, 默认) 之前；否则未声明主题仍会静默落到空默认"
                "（正是 NF-26 头号场景：告警 publish 到拼错 topic 后无声蒸发）"
            )


def test_r4_report_exemptions():
    """把当前豁免清单打进用例输出，方便直接在 CI 日志里审阅（永远通过）。"""
    al = _allowlist()
    lines = [f"R4 fail-loud 豁免清单（{al.path.name}）：共 {len(al.entries)} 条"]
    lines.append(al.inventory())
    print("\n".join(lines))
    assert True
