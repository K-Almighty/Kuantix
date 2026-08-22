"""因子模块（L2 因子库，T04）。

- ``store`` —— :class:`FactorStore`：Parquet 分区 + SQLite 元数据；
- ``service`` —— :class:`FactorService`：compute / report / combine；
- ``combiner`` —— :class:`FactorCombiner`：等权 / IC / IR 加权合成；
- ``factors`` —— 自定义因子自动发现目录（注册到上游 ``FACTORY_REGISTRY``）。

跨模块约束（NF-3/NF-4）
----------------------
- 本包**不 import** ``data`` / ``screen`` / ``monitor``；
- 上游 easy_tdx 只经 :mod:`Kuantix.adapters` 间接访问（NF-1/R2）；
- L1 读取经 :mod:`Kuantix.adapters.factor_bridge`（数据湖读侧桥），**不走网络**；
- 市场规则经 :class:`~Kuantix.core.market.MarketProfile`（NF-5，不硬编码）。
"""

from __future__ import annotations

from Kuantix.factor.combiner import FactorCombiner
from Kuantix.factor.service import FactorService
from Kuantix.factor.store import FactorStore

__all__ = ["FactorStore", "FactorService", "FactorCombiner"]
