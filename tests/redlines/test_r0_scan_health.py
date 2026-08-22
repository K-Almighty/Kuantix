"""R0 —— 扫描器自身的健康检查（元用例）。

红线检查器最危险的失效模式是**静默失效**：源码目录路径写错、文件全被
过滤掉、语法错误导致 AST 解析失败……结果全绿，但其实一个文件都没扫到。
这本身就是 NF-26 反模式。R0 就是防这个。

包含：
- **R0-A** 上游基座只读校验：easy_tdx 可 import 且系数表可取
- **R0-B** 扫描范围非空校验：源码落地后必须真的扫到文件
- **R0-C** 全量语法可解析：任何 .py 解析失败都要显式暴露（否则该文件被静默跳过）
- **R0-D** 上游目录零污染：确认 easy_tdx-main 下没有 Kuantix 产生的文件
"""

from __future__ import annotations

import pytest

from _scan import (
    PROJECT_ROOT,
    RECORDER,
    iter_py_files,
    package_root,
    rel,
    syntax_errors,
)

RECORDER.note_scope("R0", "元检查 —— 确保红线扫描器本身没有静默失效")


def test_r0a_upstream_base_importable():
    """R0-A：上游基座必须可 import，否则 R1 的系数基准无从派生。"""
    try:
        from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS
    except ImportError as exc:  # pragma: no cover
        pytest.fail(
            f"上游 easy_tdx 不可 import：{exc}\n"
            "R1（NF-25 系数表无副本）依赖从上游动态派生系数基准值，"
            "上游缺失会导致该红线静默失效。请确认 `pip install easy-tdx==1.20.3`。",
            pytrace=False,
        )
    assert isinstance(_SECURITY_COEFFICIENTS, dict) and _SECURITY_COEFFICIENTS, (
        "上游 _SECURITY_COEFFICIENTS 为空或类型异常，R1 基准不可信"
    )
    # 上游共 10 种证券类型、4 种系数值对；变了要知道（NF-25 的意义正在于跟随上游演进）
    assert len(_SECURITY_COEFFICIENTS) >= 10, (
        f"上游系数表条目数 {len(_SECURITY_COEFFICIENTS)} 少于预期 10，"
        "上游可能有结构性变更，请复核 adapters/coefficients.py 的引用方式"
    )


def test_r0b_scan_scope_not_empty():
    """R0-B：源码落地后，扫描范围不能是空的（防路径写错导致全绿假象）。"""
    pkg = package_root()
    if pkg is None:
        pytest.skip("Kuantix 源码包尚未落地 —— 扫描范围校验待代码就位后生效")
    files = iter_py_files(pkg)
    assert files, (
        f"源码包 {rel(pkg)} 存在但扫描到 0 个 .py 文件。\n"
        "这意味着所有红线检查都在空集上通过 —— 属于检查器静默失效，必须排查过滤规则。"
    )


def test_r0c_all_sources_parseable():
    """R0-C：任何语法错误都要显式报出（解析失败的文件会被红线扫描静默跳过）。"""
    pkg = package_root()
    if pkg is None:
        pytest.skip("Kuantix 源码包尚未落地")
    files = iter_py_files(pkg)
    errors = syntax_errors(files)
    if errors:
        detail = "\n".join(f"  {rel(p)}: {msg}" for p, msg in errors)
        pytest.fail(
            f"\n{len(errors)} 个文件存在语法错误，AST 红线检查会静默跳过它们：\n{detail}\n",
            pytrace=False,
        )


def test_r0d_upstream_dir_untouched():
    """R0-D：上游基座目录零污染（NF-1 上游只读）。"""
    upstream = PROJECT_ROOT.parent / "easy_tdx-main"
    if not upstream.is_dir():
        pytest.skip("未找到上游源码目录 easy_tdx-main，跳过污染检查")

    strays = [
        p
        for p in upstream.rglob("*")
        if p.is_file()
        and (
            p.name.startswith("Kuantix")
            or "Kuantix" in p.parts
            or p.suffix in (".Kuantix",)
        )
    ]
    assert not strays, (
        "【NF-1 上游只读】在上游基座目录里发现疑似 Kuantix 产生的文件：\n"
        + "\n".join(f"  {p}" for p in strays[:20])
    )
