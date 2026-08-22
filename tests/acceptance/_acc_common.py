"""T02 验收台共享工具（不含任何验收判定，判定各 test_acc_*.py 自持）。

唯一职责：把「模块未落地 → 优雅 skip」这件事集中处理，让验收用例的
主体只关心「代码对了没有」，不被 import 细节污染。
"""
from __future__ import annotations

import importlib

import pytest


def import_optional(module_name: str):
    """导入模块；T02 未落地（ImportError / ModuleNotFoundError）则优雅 skip。

    用法（在测试函数体内调用，不要在模块顶层）：
        C = import_optional("Kuantix.adapters.vipdoc_writer")
    """
    try:
        return importlib.import_module(module_name)
    except ImportError:
        pytest.skip(
            f"{module_name} 尚未落地（T02 交付物），本验收用例待代码就位后生效"
        )
