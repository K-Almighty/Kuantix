"""Kuantix —— 本地量化研究工作台。

在 ``easy-tdx==1.20.3`` 之上构建的本地化量化研究系统：
L1 行情湖（vipdoc 原生格式）→ L2 因子库（Parquet）→ 选股 / 监控 / REST。

架构约束
--------
- **上游只读（NF-1）**：仅 ``import easy_tdx``，绝不修改其源码，
  绝不写入 ``~/.easy_tdx/``；所有上游调用收敛在 :mod:`Kuantix.adapters`。
- **分层单向依赖（NF-4）**：``adapters → core → services → api``。
- **fail-loud（NF-26）**：一切不确定显式报错 + 跳过 + 记隔离区，
  禁止 ``dict.get(k, 默认)`` 兜底与 ``try/except: pass``。

本模块刻意保持零副作用：不 import 子包、不读配置、不建目录，
以避免 ``Kuantix.config`` ↔ ``Kuantix.core`` 的循环依赖。
"""

from __future__ import annotations

__all__ = ["__version__", "UPSTREAM_EASY_TDX_VERSION"]

#: Kuantix 版本号（与 pyproject.toml 的 project.version 保持一致）
__version__ = "0.1.0"

#: 锁定的上游 easy-tdx 版本。适配层启动时会校验实际安装版本是否一致，
#: 版本漂移会破坏 ``_SECURITY_COEFFICIENTS`` 的 import 引用契约（NF-25）。
UPSTREAM_EASY_TDX_VERSION = "1.20.3"
