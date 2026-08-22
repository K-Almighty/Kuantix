"""数据层（L1 行情湖，NF-15/NF-18/NF-27）。

职责边界
--------
- ``datalake`` —— :class:`DataLake` 门面（sync_full / sync_incremental / verify）；
- ``sync_engine`` —— :class:`SyncEngine`（并发 worker / 断点续传 / 进度 / 后台 / 限速退避）；
- ``verify`` —— 完整性校验 + 缺失交易日 + 隔离区报告；
- ``quarantine`` —— :class:`QuarantineStore`（SQLite 持久化隔离区，NF-27）。

跨模块约束（NF-3/NF-4）
----------------------
- 本包**不 import** ``factor`` / ``screen`` / ``monitor``；
- 上游 easy_tdx 只经 :mod:`Kuantix.adapters` 间接访问（NF-1）；
- 所有市场规则（货币/时区/交易时段/每手股数）经
  :mod:`Kuantix.core.market` 的 ``MarketProfile`` 获取，**不硬编码**（NF-5）。
"""

from __future__ import annotations

from Kuantix.data.datalake import DataLake
from Kuantix.data.quarantine import QuarantineStore
from Kuantix.data.sync_engine import (
    SyncEngine,
    SyncHandle,
    SyncPlan,
    SyncResult,
)
from Kuantix.data.verify import verify_vipdoc

__all__ = [
    "DataLake",
    "SyncEngine",
    "SyncHandle",
    "SyncPlan",
    "SyncResult",
    "QuarantineStore",
    "verify_vipdoc",
]
