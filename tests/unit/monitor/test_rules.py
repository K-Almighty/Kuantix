"""RuleEngine 白盒单测：三类判据 + 冷却去重 + 未知判据显式报错 + CRUD。"""

from __future__ import annotations

import datetime as dt

import pytest

from Kuantix.core.contracts import Bar, Quote
from Kuantix.core.fail_loud import MissingKeyError
from Kuantix.monitor import MonitorStore, Rule, RuleEngine

from tests.unit.monitor._helpers import make_quote


@pytest.fixture()
def engine(tmp_path):
    store = MonitorStore(tmp_path / "monitor.db")
    return RuleEngine(store=store)


def _bar_closes(values):
    """把收盘价序列变成 Bar 列表（date 递增）。"""
    bars = []
    base = dt.date(2026, 1, 1)
    for i, close in enumerate(values):
        bars.append(
            Bar(
                date=base + dt.timedelta(days=i),
                open=close,
                high=close,
                low=close,
                close=close,
                vol=100.0,
                amount=close * 1000.0,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# 判据：价格（突破 / 跌破）
# ---------------------------------------------------------------------------


def test_price_criterion_above_trigger(engine):
    rule = engine.create_rule(
        name="突破1600",
        market="CN",
        codes=["600519"],
        criterion_type="price",
        params={"op": "above", "threshold": 1600.0},
        level="warning",
        cooldown_seconds=300,
    )
    quote = make_quote(last=1610.0)
    alerts = engine.evaluate([quote], rules=[rule])
    assert len(alerts) == 1
    assert alerts[0].code == "600519"
    assert alerts[0].level.value == "warning"
    assert alerts[0].rule == "突破1600"
    assert alerts[0].payload["last"] == 1610.0


def test_price_criterion_below_trigger(engine):
    rule = engine.create_rule(
        name="跌破1500",
        market="CN",
        codes=["600519"],
        criterion_type="price",
        params={"op": "below", "threshold": 1500.0},
        level="critical",
        cooldown_seconds=0,
    )
    quote = make_quote(last=1490.0)
    alerts = engine.evaluate([quote], rules=[rule])
    assert len(alerts) == 1


def test_price_criterion_no_trigger(engine):
    rule = engine.create_rule(
        name="突破1600",
        market="CN",
        codes=["600519"],
        criterion_type="price",
        params={"op": "above", "threshold": 1600.0},
        level="info",
        cooldown_seconds=0,
    )
    quote = make_quote(last=1550.0)
    assert engine.evaluate([quote], rules=[rule]) == []


# ---------------------------------------------------------------------------
# 判据：指标（MA 金叉 / MACD / RSI，经 adapter 桥间接计算）
# ---------------------------------------------------------------------------


def test_indicator_criterion_ma_cross_above(engine):
    """MA 金叉：快线在慢线上方 → cross_above 命中。"""
    engine._bar_provider = lambda market, code, count: _bar_closes(
        [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    )
    rule = engine.create_rule(
        name="MA金叉",
        market="CN",
        codes=["600519"],
        criterion_type="indicator",
        params={"indicator": "ma", "op": "cross_above", "fast": 2, "slow": 5},
        level="warning",
        cooldown_seconds=0,
    )
    quote = make_quote(last=20.0)
    alerts = engine.evaluate([quote], rules=[rule])
    assert len(alerts) == 1


def test_indicator_criterion_ma_cross_below_not_trigger(engine):
    engine._bar_provider = lambda market, code, count: _bar_closes(
        [20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10]
    )
    rule = engine.create_rule(
        name="MA死叉",
        market="CN",
        codes=["600519"],
        criterion_type="indicator",
        params={"indicator": "ma", "op": "cross_above", "fast": 2, "slow": 5},
        level="warning",
        cooldown_seconds=0,
    )
    quote = make_quote(last=10.0)
    assert engine.evaluate([quote], rules=[rule]) == []


def test_indicator_criterion_requires_bar_provider(engine):
    """指标判据未注入 bar_provider → 显式报错（不静默跳过）。"""
    rule = engine.create_rule(
        name="MA",
        market="CN",
        codes=["600519"],
        criterion_type="indicator",
        params={"indicator": "ma", "op": "gt", "value": 50.0, "period": 5},
        level="info",
        cooldown_seconds=0,
    )
    quote = make_quote()
    with pytest.raises(Exception, match="bar_provider"):
        engine.evaluate([quote], rules=[rule])


# ---------------------------------------------------------------------------
# 判据：止损（成本价 / 区间最高价）
# ---------------------------------------------------------------------------


def test_stop_loss_criterion_cost(engine):
    """base=cost：最新价跌破成本价×(1-pct) 触发。"""
    engine._cost_provider = lambda code: 1680.0
    rule = engine.create_rule(
        name="止损-成本-8%",
        market="CN",
        codes=["600519"],
        criterion_type="stop_loss",
        params={"base": "cost", "pct": 0.08},
        level="critical",
        cooldown_seconds=0,
    )
    quote = make_quote(last=1545.6)  # 1680×0.92 = 1545.6 → <= 触发
    alerts = engine.evaluate([quote], rules=[rule])
    assert len(alerts) == 1
    assert alerts[0].payload["last"] == 1545.6


def test_stop_loss_criterion_cost_no_position(engine):
    """base=cost 且无持仓成本 → 不触发（合法状态，不是静默兜底）。"""
    engine._cost_provider = lambda code: None
    rule = engine.create_rule(
        name="止损-成本",
        market="CN",
        codes=["600519"],
        criterion_type="stop_loss",
        params={"base": "cost", "pct": 0.08},
        level="critical",
        cooldown_seconds=0,
    )
    quote = make_quote(last=1000.0)
    assert engine.evaluate([quote], rules=[rule]) == []


def test_stop_loss_criterion_peak(engine):
    """base=peak：区间最高价回撤超过 pct 触发。"""
    rule = engine.create_rule(
        name="止损-回撤-5%",
        market="CN",
        codes=["600519"],
        criterion_type="stop_loss",
        params={"base": "peak", "pct": 0.05},
        level="critical",
        cooldown_seconds=0,
    )
    # 先喂一个高点 1700，再喂 1600（回撤 5.88% > 5%）
    engine.evaluate([make_quote(last=1700.0)], rules=[rule])
    quote = make_quote(last=1600.0)
    alerts = engine.evaluate([quote], rules=[rule])
    assert len(alerts) == 1


# ---------------------------------------------------------------------------
# 冷却去重
# ---------------------------------------------------------------------------


def test_cooldown_dedup(engine):
    rule = engine.create_rule(
        name="突破1600",
        market="CN",
        codes=["600519"],
        criterion_type="price",
        params={"op": "above", "threshold": 1600.0},
        level="warning",
        cooldown_seconds=300,
    )
    quote = make_quote(last=1610.0)
    now = dt.datetime(2026, 8, 1, 10, 0, 0)
    first = engine.evaluate([quote], rules=[rule], now=now)
    assert len(first) == 1
    # 冷却期内不重复
    second = engine.evaluate([quote], rules=[rule], now=now + dt.timedelta(seconds=60))
    assert second == []
    # 冷却期后再次触发
    third = engine.evaluate([quote], rules=[rule], now=now + dt.timedelta(seconds=301))
    assert len(third) == 1


# ---------------------------------------------------------------------------
# 未知判据类型显式报错
# ---------------------------------------------------------------------------


def test_unknown_criterion_type_raises(engine):
    with pytest.raises(MissingKeyError):
        engine.create_rule(
            name="未知判据",
            market="CN",
            codes=["600519"],
            criterion_type="bogus",
            params={},
            level="info",
            cooldown_seconds=60,
        )


# ---------------------------------------------------------------------------
# CRUD + 持久化
# ---------------------------------------------------------------------------


def test_rule_crud_and_persistence(engine, tmp_path):
    rule = engine.create_rule(
        name="突破1600",
        market="CN",
        codes=["600519", "000001"],
        criterion_type="price",
        params={"op": "above", "threshold": 1600.0},
        level="warning",
        cooldown_seconds=300,
    )
    assert rule.id.startswith("rule_")
    assert engine.get_rule(rule.id) is not None
    assert len(engine.list_rules()) == 1

    updated = engine.update_rule(rule.id, enabled=False, params={"op": "below", "threshold": 1000.0})
    assert updated.enabled is False
    assert updated.params["op"] == "below"

    # 新引擎实例（同一 SQLite）重启不丢
    engine2 = RuleEngine(store=MonitorStore(tmp_path / "monitor.db"))
    assert len(engine2.list_rules()) == 1
    reloaded = engine2.get_rule(rule.id)
    assert reloaded.name == "突破1600"
    assert reloaded.enabled is False

    assert engine.delete_rule(rule.id) is True
    assert engine.get_rule(rule.id) is None


def test_rule_scope_star_applies_to_all_codes(engine):
    rule = engine.create_rule(
        name="全部突破",
        market="CN",
        codes=["*"],
        criterion_type="price",
        params={"op": "above", "threshold": 100.0},
        level="info",
        cooldown_seconds=0,
    )
    alerts = engine.evaluate([make_quote(code="600000", last=110.0)], rules=[rule])
    assert len(alerts) == 1


def test_rule_market_mismatch_ignored(engine):
    rule = engine.create_rule(
        name="CN only",
        market="CN",
        codes=["600519"],
        criterion_type="price",
        params={"op": "above", "threshold": 100.0},
        level="info",
        cooldown_seconds=0,
    )
    hk_quote = make_quote(code="00700", market="HK", last=500.0)
    assert engine.evaluate([hk_quote], rules=[rule]) == []


def test_rule_invalid_level_raises(engine):
    with pytest.raises(Exception, match="level"):
        engine.create_rule(
            name="bad",
            market="CN",
            codes=["600519"],
            criterion_type="price",
            params={"op": "above", "threshold": 100.0},
            level="fatal",
            cooldown_seconds=60,
        )
