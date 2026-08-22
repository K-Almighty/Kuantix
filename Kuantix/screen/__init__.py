"""选股模块（T04）。

- ``service`` —— :class:`ScreenService`：加载模型 → 全市场打分 → 过滤 → TopN
  → 落盘 SQLite + JSON/CSV（同花顺兼容、GBK）；
- ``filters`` —— :class:`ScreenFilter`：技术 / 缠论条件过滤器
  （经 :mod:`Kuantix.adapters.backtest_bridge` 调上游
  SignalScanner / ChanlunAnalyser / StrengthRanker）。

跨模块约束（NF-3/NF-4）
----------------------
- 本包**不 import** ``data`` / ``factor`` / ``monitor``；
- 上游 easy_tdx 只经 :mod:`Kuantix.adapters` 间接访问（NF-1/R2）；
- 市场规则经 :class:`~Kuantix.core.market.MarketProfile`（NF-5，不硬编码）。
"""

from __future__ import annotations

from Kuantix.screen.filters import ScreenFilter
from Kuantix.screen.service import ScreenService

__all__ = ["ScreenService", "ScreenFilter"]
