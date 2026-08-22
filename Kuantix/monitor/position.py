"""PositionTracker —— 持仓登记 / 更新 / 移除 / 盈亏计算（内存 + SQLite）。

持仓数据同时存在内存（供轮询热路径）与 SQLite（``~/.Kuantix/db/monitor.db``），
重启不丢（M12/M13/M14 语义）。

盈亏口径（契约 §3.5 PositionView）
----------------------------------
- ``market_value = shares × last``（元）；
- ``pnl = (last - cost_price) × shares``（元）；
- ``pnl_pct = pnl / (cost_price × shares)`` —— **小数比例**（``-0.08`` = -8%）；
- ``change_pct`` 直接取 :class:`Quote.change_pct`（小数比例，0.05 = 5%）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from Kuantix.core.contracts import Position, Quote
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingKeyError,
    require_finite,
    require_key,
)

from Kuantix.monitor.store import MonitorStore

__all__ = ["PositionTracker"]


class PositionTracker:
    """持仓追踪器。

    Args:
        store: 持久化存储；``None`` 时使用默认 ``~/.Kuantix/db/monitor.db``。
    """

    def __init__(self, store: MonitorStore | None = None) -> None:
        self._store = store if store is not None else MonitorStore()
        self._positions: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        """启动时从 SQLite 恢复全部持仓（重启不丢）。"""
        for record in self._store.list_positions():
            self._positions[record["code"]] = record

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def add_position(self, position: Position, *, name: str = "") -> Position:
        """登记一笔持仓。

        Args:
            position: 持仓（code/market/shares/cost_price/opened_at）。
            name: 证券名称（可选，展示用）。

        Raises:
            DataIntegrityError: 持仓校验失败。
        """
        record = {
            "code": position.code,
            "name": str(name),
            "market": position.market,
            "shares": position.shares,
            "cost_price": position.cost_price,
            "opened_at": position.opened_at,
        }
        self._positions[position.code] = record
        self._store.add_position(position, name=str(name))
        return position

    def update_position(
        self,
        code: str,
        *,
        shares: float | None = None,
        cost_price: float | None = None,
        opened_at: dt.date | None = None,
        name: str | None = None,
    ) -> Position:
        """更新持仓（只更新非 None 字段）。

        Raises:
            MissingKeyError: 持仓不存在。
        """
        current = require_key(self._positions, code, "position.update_position")
        new_shares = current["shares"] if shares is None else require_finite(
            shares, f"position.{code}.shares"
        )
        new_cost = current["cost_price"] if cost_price is None else require_finite(
            cost_price, f"position.{code}.cost_price"
        )
        new_opened = current["opened_at"] if opened_at is None else opened_at
        new_name = current["name"] if name is None else str(name)

        record = {
            "code": current["code"],
            "name": new_name,
            "market": current["market"],
            "shares": new_shares,
            "cost_price": new_cost,
            "opened_at": new_opened,
        }
        self._positions[code] = record
        self._store.update_position(
            code,
            shares=new_shares,
            cost_price=new_cost,
            opened_at=new_opened,
            name=new_name,
        )
        return self._record_to_position(record)

    def remove_position(self, code: str) -> bool:
        """移除持仓；返回是否确实存在。"""
        if code not in self._positions:
            return False
        del self._positions[code]
        return self._store.delete_position(code)

    def get_position(self, code: str) -> Position | None:
        """按代码取持仓。"""
        record = self._positions.get(code)
        if record is None:
            return None
        return self._record_to_position(record)

    def get_record(self, code: str) -> dict[str, Any] | None:
        """按代码取持仓记录（含 name）。"""
        record = self._positions.get(code)
        return dict(record) if record is not None else None

    def list_positions(
        self,
        market: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """列出全部持仓记录（含 name）。P1-2：支持 DB 级 LIMIT/OFFSET。

        从持久化层读取（避免对全部内存记录过滤 + 切片）；内存缓存仍与
        ``add_position`` / ``remove_position`` 同步更新，用于实时 PnL 计算。
        """
        return [
            dict(r)
            for r in self._store.list_positions(market, limit=limit, offset=offset)
        ]

    def count_positions(self, market: str | None = None) -> int:
        """P1-2：持仓匹配条目总数。"""
        return self._store.count_positions(market)

    def position_codes(self) -> list[str]:
        """返回全部持仓代码。"""
        return sorted(self._positions)

    # ------------------------------------------------------------------ #
    # 盈亏计算（M12 PositionView）
    # ------------------------------------------------------------------ #

    def pnl(self, quotes: Mapping[str, Quote] | Sequence[Quote]) -> list[dict[str, Any]]:
        """按最新报价计算全部持仓的盈亏视图。

        Args:
            quotes: ``{code: Quote}`` 映射或 :class:`Quote` 列表。

        Returns:
            PositionView 字典列表（契约 §3.5）。

        Raises:
            MissingKeyError: 某持仓缺少对应报价（**不静默跳过**，NF-26）。
        """
        quote_map = self._to_quote_map(quotes)
        views: list[dict[str, Any]] = []
        for code, record in sorted(self._positions.items()):
            quote = quote_map.get(code)
            if quote is None:
                raise MissingKeyError(
                    f"[fail-loud/NF-26] 持仓 {code} 缺少报价，无法计算盈亏；"
                    f"请在监控轮询自选中包含该代码"
                )
            views.append(self._view(record, quote))
        return views

    def pnl_for(self, code: str, quote: Quote) -> dict[str, Any]:
        """计算单笔持仓的盈亏视图。

        Raises:
            MissingKeyError: 持仓不存在。
        """
        record = require_key(self._positions, code, "position.pnl_for")
        return self._view(record, quote)

    @staticmethod
    def _view(record: dict[str, Any], quote: Quote) -> dict[str, Any]:
        """由持仓记录 + 报价构造 PositionView（契约 §3.5）。"""
        shares = float(record["shares"])
        cost_price = float(record["cost_price"])
        last = require_finite(quote.last, f"position.{record['code']}.last")
        market_value = shares * last
        pnl = (last - cost_price) * shares
        cost_value = cost_price * shares
        pnl_pct = pnl / cost_value if cost_value != 0 else 0.0
        return {
            "code": record["code"],
            "name": record["name"],
            "market": record["market"],
            "shares": shares,
            "cost_price": cost_price,
            "last": last,
            "change_pct": quote.change_pct,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "as_of": quote.ts.date().isoformat(),
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_quote_map(quotes: Mapping[str, Quote] | Sequence[Quote]) -> dict[str, Quote]:
        if isinstance(quotes, Mapping):
            return dict(quotes)
        result: dict[str, Quote] = {}
        for quote in quotes:
            result[quote.code] = quote
        return result

    @staticmethod
    def _record_to_position(record: dict[str, Any]) -> Position:
        return Position(
            code=record["code"],
            market=record["market"],
            shares=float(record["shares"]),
            cost_price=float(record["cost_price"]),
            opened_at=record["opened_at"],
        )
