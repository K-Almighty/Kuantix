"""PositionTracker 白盒单测：持仓增删改 + pnl 计算 + 持久化。"""

from __future__ import annotations

import datetime as dt

import pytest

from Kuantix.core.contracts import Position
from Kuantix.core.fail_loud import MissingKeyError
from Kuantix.monitor import MonitorStore, PositionTracker

from tests.unit.monitor._helpers import make_quote


@pytest.fixture()
def tracker(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    return PositionTracker(store=store)


def _position(code="600519", shares=100.0, cost=1680.0):
    return Position(
        code=code,
        market="CN",
        shares=shares,
        cost_price=cost,
        opened_at=dt.date(2026, 1, 5),
    )


def test_add_and_get(tracker):
    tracker.add_position(_position(), name="贵州茅台")
    pos = tracker.get_position("600519")
    assert pos.code == "600519"
    assert pos.shares == 100.0
    record = tracker.get_record("600519")
    assert record["name"] == "贵州茅台"


def test_update_position(tracker):
    tracker.add_position(_position())
    updated = tracker.update_position("600519", shares=200.0, cost_price=1700.0)
    assert updated.shares == 200.0
    assert updated.cost_price == 1700.0
    assert tracker.get_position("600519").shares == 200.0


def test_update_missing_position_raises(tracker):
    with pytest.raises(MissingKeyError):
        tracker.update_position("NOPE", shares=100.0)


def test_remove_position(tracker):
    tracker.add_position(_position())
    assert tracker.remove_position("600519") is True
    assert tracker.get_position("600519") is None
    # 再次删除返回 False（合法状态）
    assert tracker.remove_position("600519") is False


def test_list_positions_market_filter(tracker):
    tracker.add_position(_position(code="600519"))
    tracker.add_position(_position(code="000001", shares=50.0, cost=10.0))
    assert len(tracker.list_positions()) == 2
    assert len(tracker.list_positions(market="CN")) == 2
    assert len(tracker.list_positions(market="HK")) == 0


def test_pnl_view(tracker):
    tracker.add_position(_position(code="600519", shares=100.0, cost=1680.0), name="贵州茅台")
    quote = make_quote(last=1545.6, prev_close=1680.0)  # change_pct = -0.08
    views = tracker.pnl({quote.code: quote})
    assert len(views) == 1
    view = views[0]
    # 契约 §3.5 PositionView
    assert view["code"] == "600519"
    assert view["name"] == "贵州茅台"
    assert view["shares"] == 100.0
    assert view["cost_price"] == 1680.0
    assert view["last"] == 1545.6
    assert view["change_pct"] == pytest.approx(-0.08)  # 小数比例
    assert view["market_value"] == pytest.approx(154560.0)
    assert view["pnl"] == pytest.approx(-13440.0)
    assert view["pnl_pct"] == pytest.approx(-0.08)  # 小数比例
    assert view["as_of"] == "2026-08-01"


def test_pnl_missing_quote_raises(tracker):
    tracker.add_position(_position(code="600519"))
    with pytest.raises(MissingKeyError):
        tracker.pnl({})


def test_persistence_reload(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    tracker = PositionTracker(store=store)
    tracker.add_position(_position(code="600519", shares=100.0, cost=1680.0), name="贵州茅台")

    # 新实例（同一 SQLite）重启不丢
    tracker2 = PositionTracker(store=MonitorStore(tmp_path / "monitor.db"))
    positions = tracker2.list_positions()
    assert len(positions) == 1
    assert positions[0]["code"] == "600519"
    assert positions[0]["name"] == "贵州茅台"
