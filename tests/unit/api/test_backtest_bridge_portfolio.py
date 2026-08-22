"""BacktestBridge 组合/多策略新方法单测。

- 假上游引擎注入（monkeypatch bridge 模块属性）验证参数透传与结果清洗
  （NF-12：NaN/Inf → null、key 归一化、datetime 截断）；
- 真上游引擎（合成 K 线，零网络）验证数据链路与金额求和语义（D-8）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from Kuantix.adapters import backtest_bridge as bridge_module
from Kuantix.adapters.backtest_bridge import BacktestBridge
from Kuantix.core.fail_loud import DataIntegrityError, UpstreamContractError


def _frame(n: int = 120) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = np.linspace(10, 20, n)
    return pd.DataFrame(
        {
            "datetime": dates,
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "vol": np.full(n, 10000.0),
            "amount": np.full(n, 1e7),
        }
    )


class _FakeEngineResult:
    """假上游 PortfolioResult.to_dict 输出（含待清洗的 NaN/Inf）。

    与真实 ``PortfolioResult.to_dict`` 对齐：``combined_equity`` 先转
    ``orient="records"``（真实引擎在 ``to_dict`` 内完成 DataFrame → records）。
    """

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        payload = dict(self._payload)
        combined = payload["combined_equity"]
        if hasattr(combined, "to_dict"):
            payload["combined_equity"] = combined.to_dict(orient="records")
        return payload


class _FakePortfolioEngine:
    """假 PortfolioBacktestEngine：记录构造参数并返回固定结果。"""

    last_instance: _FakePortfolioEngine | None = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        type(self).last_instance = self

    def run(self):
        stocks = self.kwargs["stocks"]
        total_cash = self.kwargs["total_cash"]
        n = len(stocks)
        per = total_cash / n if n else 0
        individual = {
            f"{s.market}{s.code}": {
                "performance": {"total_return": 0.1},
                "equity_curve": [
                    {"datetime": pd.Timestamp("2024-01-02"), "total": per}
                ],
                "trades": [],
                "positions": [],
            }
            for s in stocks
        }
        return _FakeEngineResult(
            {
                "total_performance": {
                    "total_return": float("nan"),  # 待清洗 → null
                    "annual_return": float("inf"),  # 待清洗 → null
                    "total_stocks": n,
                    "total_cash": total_cash,
                },
                "individual_results": individual,
                "equity_allocation": {f"{s.market}{s.code}": 1.0 / n for s in stocks},
                "combined_equity": pd.DataFrame(
                    {
                        "datetime": [pd.Timestamp("2024-01-02")],
                        "total": [per],
                        "drawdown": [0.0],
                        "drawdown_pct": [0.0],
                    }
                ),
            }
        )


# ---------------------------------------------------------------------------
# run_portfolio_backtest：假引擎注入
# ---------------------------------------------------------------------------


def test_run_portfolio_params_passthrough_and_clean(monkeypatch) -> None:
    monkeypatch.setattr(bridge_module, "PortfolioBacktestEngine", _FakePortfolioEngine)
    bridge = BacktestBridge()
    result = bridge.run_portfolio_backtest(
        [("600000", "sh", _frame()), ("600036", "sz", _frame())],
        "ma_cross",
        {"fast": 5, "slow": 20},
        1_000_000.0,
        commission=0.0003,
        min_commission=5.0,
        stamp_tax=0.001,
        slippage=0.0,
        execution="next_open",
    )
    # 金额求和 / 引擎参数透传（构造时 total_cash + stocks）
    assert result["total_performance"]["total_stocks"] == 2
    assert result["total_performance"]["total_cash"] == 1_000_000.0
    # NF-12：NaN / Inf → None
    assert result["total_performance"]["total_return"] is None
    assert result["total_performance"]["annual_return"] is None
    # key 归一化：SH600519 → 600000
    assert set(result["individual_results"].keys()) == {"600000", "600036"}
    assert result["equity_allocation"] == {"600000": 0.5, "600036": 0.5}
    # combined_equity datetime 截断到 YYYY-MM-DD
    assert result["combined_equity"][0]["datetime"] == "2024-01-02"
    # 策略实例被构造并传入引擎
    engine_cls = bridge_module.PortfolioBacktestEngine
    strategy = engine_cls.last_instance.kwargs["strategy"]
    assert strategy is not None


def test_run_portfolio_unknown_strategy_fails(monkeypatch) -> None:
    monkeypatch.setattr(bridge_module, "PortfolioBacktestEngine", _FakePortfolioEngine)
    bridge = BacktestBridge()
    with pytest.raises(DataIntegrityError):
        bridge.run_portfolio_backtest(
            [("600000", "sh", _frame())], "no_such_strategy", {}, 1_000_000.0
        )


def test_run_portfolio_empty_stocks_fails(monkeypatch) -> None:
    monkeypatch.setattr(bridge_module, "PortfolioBacktestEngine", _FakePortfolioEngine)
    bridge = BacktestBridge()
    with pytest.raises(DataIntegrityError):
        bridge.run_portfolio_backtest([], "ma_cross", {}, 1_000_000.0)


def test_run_portfolio_key_mapping_failure_is_fail_loud(monkeypatch) -> None:
    """上游返回无法映射的 key → UpstreamContractError（不静默保留）。"""

    class _WeirdEngine(_FakePortfolioEngine):
        def run(self):
            result = super().run()
            payload = result._payload
            # 篡改 individual_results 的 key，使其无法映射回标的代码
            payload = dict(payload)
            payload["individual_results"] = {"SH999999": {"performance": {}}}
            return _FakeEngineResult(payload)

    monkeypatch.setattr(bridge_module, "PortfolioBacktestEngine", _WeirdEngine)
    bridge = BacktestBridge()
    with pytest.raises(UpstreamContractError):
        bridge.run_portfolio_backtest(
            [("600000", "sh", _frame())], "ma_cross", {}, 1_000_000.0
        )


# ---------------------------------------------------------------------------
# run_multi_strategy：假引擎注入
# ---------------------------------------------------------------------------


class _FakeMultiEngine:
    """假 MultiStrategyEngine：记录构造参数并返回固定结果。"""

    last_instance: _FakeMultiEngine | None = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        type(self).last_instance = self

    def run(self):
        slots = self.kwargs["strategies"]
        n = len(slots)
        individual = {}
        for slot in slots:
            key = f"{slot.label}@{slot.symbol}"
            individual[key] = {
                "performance": {"total_return": 0.1},
                "equity_curve": [{"datetime": "2024-01-02", "total": 100.0}],
                "trades": [],
                "positions": [],
            }
        return _FakeEngineResult(
            {
                "total_performance": {
                    "total_return": 0.1,
                    "annual_return": 0.1,
                    "total_stocks": n,
                    "total_cash": self.kwargs["total_cash"],
                },
                "individual_results": individual,
                "equity_allocation": dict.fromkeys(individual, 1.0 / n),
                "combined_equity": [
                    {
                        "datetime": pd.Timestamp("2024-01-02"),
                        "total": 200.0,
                        "drawdown": 0.0,
                        "drawdown_pct": 0.0,
                    }
                ],
            }
        )


def test_run_multi_params_passthrough(monkeypatch) -> None:
    monkeypatch.setattr(bridge_module, "MultiStrategyEngine", _FakeMultiEngine)
    bridge = BacktestBridge()
    slots = [
        {
            "label": "双均线交叉",
            "symbol": "SH:600000",
            "strategy_name": "ma_cross",
            "params": {"fast": 5},
            "df": _frame(),
        },
        {
            "label": "MACD",
            "symbol": "SZ:600036",
            "strategy_name": "macd",
            "params": {},
            "df": _frame(),
        },
    ]
    result = bridge.run_multi_strategy(slots, 1_000_000.0)
    assert set(result["individual_results"].keys()) == {
        "双均线交叉@SH:600000",
        "MACD@SZ:600036",
    }
    assert result["equity_allocation"]["双均线交叉@SH:600000"] == 0.5
    assert result["combined_equity"][0]["datetime"] == "2024-01-02"
    # 策略实例按 name+params 构造并传入引擎
    engine_slots = bridge_module.MultiStrategyEngine.last_instance.kwargs["strategies"]
    assert len(engine_slots) == 2
    assert engine_slots[0].label == "双均线交叉"


def test_run_multi_empty_slots_fails(monkeypatch) -> None:
    monkeypatch.setattr(bridge_module, "MultiStrategyEngine", _FakeMultiEngine)
    bridge = BacktestBridge()
    with pytest.raises(DataIntegrityError):
        bridge.run_multi_strategy([], 1_000_000.0)


def test_run_multi_unknown_strategy_fails(monkeypatch) -> None:
    monkeypatch.setattr(bridge_module, "MultiStrategyEngine", _FakeMultiEngine)
    bridge = BacktestBridge()
    with pytest.raises(DataIntegrityError):
        bridge.run_multi_strategy(
            [
                {
                    "label": "a",
                    "symbol": "SH:600000",
                    "strategy_name": "nope",
                    "params": {},
                    "df": _frame(),
                }
            ],
            1_000_000.0,
        )


# ---------------------------------------------------------------------------
# 真上游引擎（零网络，合成 K 线）
# ---------------------------------------------------------------------------


def test_real_portfolio_engine_money_sum_semantics() -> None:
    """D-8 金额求和：组合净值首点 = 总资金（各标的现金分仓之和）。"""
    bridge = BacktestBridge()
    result = bridge.run_portfolio_backtest(
        [("600000", "sh", _frame()), ("600036", "sz", _frame())],
        "ma_cross",
        {"fast": 5, "slow": 20},
        1_000_000.0,
    )
    assert result["total_performance"]["total_stocks"] == 2
    assert result["total_performance"]["total_cash"] == 1_000_000.0
    first = result["combined_equity"][0]
    assert first["total"] == 1_000_000.0
    assert first["datetime"] == "2024-01-01"


def test_real_multi_engine_money_sum_semantics() -> None:
    bridge = BacktestBridge()
    result = bridge.run_multi_strategy(
        [
            {
                "label": "双均线交叉",
                "symbol": "SH:600000",
                "strategy_name": "ma_cross",
                "params": {"fast": 5},
                "df": _frame(),
            },
            {
            "label": "MACD",
            "symbol": "SZ:600036",
            "strategy_name": "macd",
            "params": {},
            "df": _frame(),
        },
        ],
        1_000_000.0,
    )
    assert result["total_performance"]["total_stocks"] == 2
    first = result["combined_equity"][0]
    assert first["total"] == 1_000_000.0
    assert set(result["individual_results"].keys()) == {
        "双均线交叉@SH:600000",
        "MACD@SZ:600036",
    }
