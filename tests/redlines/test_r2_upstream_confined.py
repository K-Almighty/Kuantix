"""R2 —— 上游调用收敛在 adapters（NF-1）。

约束来源
--------
PRD NF-1：「所有上游调用需**收敛到 `Kuantix/adapters/` 单一适配层**，上游升级时
只改这一层。」
system_design §1.3：「门面 + 适配（Adapter）：所有 easy-tdx 调用收敛到
`Kuantix/adapters/`。」§8：「上游只读（NF-1）：仅 import easy-tdx；所有调用收敛
`adapters/`。」

判定逻辑（一句话）
------------------
AST 遍历 ``Import`` / ``ImportFrom`` / ``importlib.import_module`` / ``__import__``，
凡是引用 ``easy_tdx`` 顶级包、且文件不在 ``<pkg>/adapters/`` 下 → FAIL。

细分子规则
----------
- **R2-A** ``import easy_tdx...`` 出现在非 adapters 目录
- **R2-B** ``from easy_tdx... import ...`` 出现在非 adapters 目录
- **R2-C** 动态导入 ``importlib.import_module("easy_tdx...")`` / ``__import__("easy_tdx")``
- **R2-D** 上游包（pytdx 等 easy-tdx 的底层协议库）在非 adapters 目录被直接 import

本规则**不设 allowlist**：NF-1 的分层是架构地基，一旦开口子就失去意义。
"""

from __future__ import annotations

import ast

from _scan import (
    ADAPTER_DIR,
    RECORDER,
    Violation,
    dotted_name,
    fail_if,
    iter_py_files,
    parse_module,
    require_package_root,
)

#: 视为「上游」的顶级包名
UPSTREAM_ROOTS = ("easy_tdx",)
#: 上游底层协议库，业务层同样不许直接摸
UPSTREAM_LOWLEVEL_ROOTS = ("pytdx",)

RECORDER.note_scope(
    "R2", f"全包扫描 —— 仅 <pkg>/{ADAPTER_DIR}/ 允许 import easy_tdx（NF-1）"
)


def _root_of(module: str) -> str:
    return (module or "").split(".")[0]


def _is_adapter(path, pkg) -> bool:
    try:
        parts = path.relative_to(pkg).parts
    except ValueError:
        return False
    return len(parts) > 1 and parts[0] == ADAPTER_DIR


def _layer_of(path, pkg) -> str:
    try:
        parts = path.relative_to(pkg).parts
    except ValueError:
        return "?"
    return parts[0] if len(parts) > 1 else "<包根>"


def test_r2_upstream_imports_confined_to_adapters():
    """R2-A~D：easy_tdx / pytdx 的 import 只允许出现在 adapters 层。"""
    pkg = require_package_root()
    files = iter_py_files(pkg)
    violations: list[Violation] = []

    for path in files:
        if _is_adapter(path, pkg):
            continue
        tree = parse_module(path)
        if tree is None:
            continue
        layer = _layer_of(path, pkg)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = _root_of(alias.name)
                    if root in UPSTREAM_ROOTS:
                        violations.append(
                            Violation(
                                "R2-A", path, node.lineno,
                                f"`{layer}` 层直接 `import {alias.name}`；NF-1 要求上游调用"
                                f"全部收敛到 {ADAPTER_DIR}/，业务层只能依赖 core 契约",
                            )
                        )
                    elif root in UPSTREAM_LOWLEVEL_ROOTS:
                        violations.append(
                            Violation(
                                "R2-D", path, node.lineno,
                                f"`{layer}` 层直接 `import {alias.name}`（上游底层协议库）；"
                                f"必须经 {ADAPTER_DIR}/ 封装",
                            )
                        )

            elif isinstance(node, ast.ImportFrom):
                # 相对 import（level>0）不涉及上游
                if node.level and node.level > 0:
                    continue
                root = _root_of(node.module or "")
                if root in UPSTREAM_ROOTS:
                    names = ", ".join(a.name for a in node.names)
                    violations.append(
                        Violation(
                            "R2-B", path, node.lineno,
                            f"`{layer}` 层直接 `from {node.module} import {names}`；"
                            f"NF-1 要求收敛到 {ADAPTER_DIR}/",
                        )
                    )
                elif root in UPSTREAM_LOWLEVEL_ROOTS:
                    violations.append(
                        Violation(
                            "R2-D", path, node.lineno,
                            f"`{layer}` 层直接 from-import 上游底层协议库 `{node.module}`；"
                            f"必须经 {ADAPTER_DIR}/ 封装",
                        )
                    )

            elif isinstance(node, ast.Call):
                fn = dotted_name(node.func)
                short = fn.split(".")[-1]
                if short in ("import_module", "__import__") and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        if _root_of(first.value) in UPSTREAM_ROOTS + UPSTREAM_LOWLEVEL_ROOTS:
                            violations.append(
                                Violation(
                                    "R2-C", path, node.lineno,
                                    f"`{layer}` 层用动态导入绕过分层："
                                    f"{short}(\"{first.value}\")；NF-1 同样禁止",
                                )
                            )

    fail_if(
        violations,
        "R2",
        f"【R2 / NF-1 上游调用收敛】扫描 {len(files)} 个文件后发现 adapters 层之外的上游依赖。\n"
        f"整改方向：把上游调用下沉到 Kuantix/{ADAPTER_DIR}/，业务层只依赖 Kuantix/core/ 的契约类型。",
    )


def test_r2_adapters_layer_exists_when_upstream_used():
    """R2-E：一旦包内出现 easy_tdx 依赖，adapters 目录必须存在（分层不能只写在文档里）。"""
    pkg = require_package_root()
    uses_upstream = False
    for path in iter_py_files(pkg):
        tree = parse_module(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and _root_of(node.module or "") in UPSTREAM_ROOTS:
                uses_upstream = True
            elif isinstance(node, ast.Import) and any(
                _root_of(a.name) in UPSTREAM_ROOTS for a in node.names
            ):
                uses_upstream = True
        if uses_upstream:
            break

    if not uses_upstream:
        import pytest

        pytest.skip("包内尚无 easy_tdx 依赖，分层断言暂不适用")

    assert (pkg / ADAPTER_DIR).is_dir(), (
        f"【R2-E / NF-1】包内已使用 easy_tdx，但 Kuantix/{ADAPTER_DIR}/ 目录不存在。"
        "上游调用必须有单一适配层承载。"
    )
