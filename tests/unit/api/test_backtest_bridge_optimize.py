"""BacktestBridge.run_optimize / signal_points 单测（P1，v1.3 增量）。

- 假 ParamGridOptimizer 注入（monkeypatch bridge 模块属性）验证参数透传、
  NaN/Inf → null 清洗与结构完整性（NF-12）；
- 网格超限 / 空网格 / 参数数越界 / 未知策略 → DataIntegrityError（fail-loud）；
- 真上游引擎（合成 K 线，零网络）验证 best/heatmap 结构；
- signal_points：假信号源注入验证 BUY/SELL 拆分、日期规整、异常方向 fail-loud。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from Kuantix.adapters import backtest_bridge as bridge_module
from Kuantix.adapters.backtest_bridge import BacktestBridge
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingKeyError,
    UpstreamContractError,
)


def _frame(n: int = 60) -> pd.DataFrame:
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


class _FakeOptimizeResult:
    """假 OptimizeResult.to_dict 输出（含待清洗的 NaN/Inf）。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)


class _FakeParamGridOptimizer:
    """假 ParamGridOptimizer：记录构造参数并返回固定结果。"""

    last_instance: _FakeParamGridOptimizer | None = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        type(self).last_instance = self

    def run(self):
        grid = self.kwargs["param_grid"]
        names = list(grid.keys())
        return _FakeOptimizeResult(
            {
                "strategy": self.kwargs["strategy_name"],
                "param_names": names,
                "results": [
                    {
                        "params": {"fast": 5, "slow": 20},
                        "total_return": float("nan"),  # 待清洗 → null
                        "sharpe": float("inf"),  # 待清洗 → null
                        "max_drawdown": 0.2,
                        "total_trades": 36,
                        "win_rate": 0.5,
                        "profit_factor": 1.8,
                    },
                    {
                        "params": {"fast": 10, "slow": 20},
                        "total_return": 0.2,
                        "sharpe": 1.1,
                        "max_drawdown": 0.25,
                        "total_trades": 20,
                        "win_rate": 0.4,
                        "profit_factor": 1.5,
                    },
                ],
                "best": {
                    "params": {"fast": 5, "slow": 20},
                    "total_return": float("nan"),
                    "sharpe": float("inf"),
                    "max_drawdown": 0.2,
                    "total_trades": 36,
                    "win_rate": 0.5,
                    "profit_factor": 1.8,
                },
                "heatmap": None,
            }
        )


# ---------------------------------------------------------------------------
# run_optimize：假引擎注入
# ---------------------------------------------------------------------------


def test_run_optimize_params_passthrough_and_clean(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge_module, "ParamGridOptimizer", _FakeParamGridOptimizer
    )
    bridge = BacktestBridge()
    df = _frame()
    result = bridge.run_optimize(
        df,
        "ma_cross",
        {"fast": [5, 10], "slow": [20]},
        cash=500_000.0,
        commission=0.0003,
        min_commission=5.0,
        stamp_tax=0.001,
        slippage=0.0,
        execution="next_open",
    )
    # 结构完整性
    assert result["strategy"] == "ma_cross"
    assert result["param_names"] == ["fast", "slow"]
    assert len(result["results"]) == 2
    assert result["best"] is not None
    assert result["heatmap"] is None
    # NF-12：NaN / Inf → None
    assert result["best"]["total_return"] is None
    assert result["best"]["sharpe"] is None
    assert result["results"][0]["total_return"] is None
    # 参数透传
    engine = bridge_module.ParamGridOptimizer.last_instance
    assert engine.kwargs["strategy_name"] == "ma_cross"
    assert engine.kwargs["param_grid"] == {"fast": [5, 10], "slow": [20]}
    assert engine.kwargs["cash"] == 500_000.0
    assert engine.kwargs["commission"] == 0.0003
    assert engine.kwargs["execution"] == "next_open"
    assert engine.kwargs["df"] is df


def test_run_optimize_heatmap_passthrough(monkeypatch) -> None:
    class _WithHeatmap(_FakeParamGridOptimizer):
        def run(self):
            payload = super().run().to_dict()
            payload["heatmap"] = {
                "x_name": "fast",
                "y_name": "slow",
                "x": [5, 10],
                "y": [20],
                "data": [[0, 0, 0.1], [1, 0, 0.2]],
            }
            return _FakeOptimizeResult(payload)

    monkeypatch.setattr(bridge_module, "ParamGridOptimizer", _WithHeatmap)
    bridge = BacktestBridge()
    result = bridge.run_optimize(_frame(), "ma_cross", {"fast": [5, 10], "slow": [20]})
    assert result["heatmap"] == {
        "x_name": "fast",
        "y_name": "slow",
        "x": [5, 10],
        "y": [20],
        "data": [[0, 0, 0.1], [1, 0, 0.2]],
    }


def test_run_optimize_grid_too_large_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge_module, "ParamGridOptimizer", _FakeParamGridOptimizer
    )
    bridge = BacktestBridge()
    with pytest.raises(DataIntegrityError):
        bridge.run_optimize(
            _frame(), "ma_cross", {"fast": list(range(15)), "slow": list(range(15))}
        )  # 15*15 = 225 > 200


def test_run_optimize_empty_grid_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge_module, "ParamGridOptimizer", _FakeParamGridOptimizer
    )
    bridge = BacktestBridge()
    with pytest.raises(DataIntegrityError):
        bridge.run_optimize(_frame(), "ma_cross", {})


def test_run_optimize_empty_values_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge_module, "ParamGridOptimizer", _FakeParamGridOptimizer
    )
    bridge = BacktestBridge()
    with pytest.raises(DataIntegrityError):
        bridge.run_optimize(_frame(), "ma_cross", {"fast": []})


def test_run_optimize_too_many_params_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge_module, "ParamGridOptimizer", _FakeParamGridOptimizer
    )
    bridge = BacktestBridge()
    with pytest.raises(DataIntegrityError):
        bridge.run_optimize(
            _frame(),
            "ma_cross",
            {"fast": [5, 10], "slow": [20], "extra": [1]},
        )


def test_run_optimize_unknown_strategy_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge_module, "ParamGridOptimizer", _FakeParamGridOptimizer
    )
    bridge = BacktestBridge()
    with pytest.raises(DataIntegrityError):
        bridge.run_optimize(_frame(), "no_such_strategy", {"fast": [5, 10]})


# ---------------------------------------------------------------------------
# run_optimize：真上游引擎（零网络，合成 K 线）
# ---------------------------------------------------------------------------


def test_real_optimizer_best_heatmap_structure() -> None:
    bridge = BacktestBridge()
    result = bridge.run_optimize(
        _frame(), "ma_cross", {"fast": [5, 10], "slow": [20, 30]}
    )
    assert result["param_names"] == ["fast", "slow"]
    assert len(result["results"]) == 4
    assert result["best"] is not None
    assert result["best"]["params"] in (
        {"fast": 5, "slow": 20},
        {"fast": 5, "slow": 30},
        {"fast": 10, "slow": 20},
        {"fast": 10, "slow": 30},
    )
    heatmap = result["heatmap"]
    assert heatmap is not None
    assert heatmap["x_name"] == "fast"
    assert heatmap["y_name"] == "slow"
    assert heatmap["x"] == [5, 10]
    assert heatmap["y"] == [20, 30]
    assert len(heatmap["data"]) == 4


# ---------------------------------------------------------------------------
# signal_points：假信号源注入
# ---------------------------------------------------------------------------


def _fake_run_result(trades: list[dict]) -> dict:
    return {
        "performance": {"total_return": 0.1},
        "equity_curve": [],
        "trades": trades,
        "positions": [],
        "config": {},
        "diagnostic": None,
    }


def test_signal_points_split_buy_sell(monkeypatch) -> None:
    bridge = BacktestBridge()
    trades = [
        {"datetime": 20240129, "direction": "BUY", "price": 13.25},
        {"datetime": 20240215, "direction": "SELL", "price": 14.5},
        {"datetime": 20240301, "direction": "BUY", "price": 15.1},
    ]

    def fake_run(df, strategy_name, params=None):
        assert strategy_name == "ma_cross"
        assert params == {"fast": 5, "slow": 20}
        return _fake_run_result(trades)

    monkeypatch.setattr(bridge, "run_strategy_backtest", fake_run)
    points = bridge.signal_points(_frame(), "ma_cross", {"fast": 5, "slow": 20})
    assert points["buy_points"] == [
        {"date": "2024-01-29", "price": 13.25},
        {"date": "2024-03-01", "price": 15.1},
    ]
    assert points["sell_points"] == [{"date": "2024-02-15", "price": 14.5}]


def test_signal_points_empty_trades(monkeypatch) -> None:
    bridge = BacktestBridge()
    monkeypatch.setattr(
        bridge, "run_strategy_backtest", lambda df, s, p=None: _fake_run_result([])
    )
    points = bridge.signal_points(_frame(), "ma_cross")
    assert points == {"buy_points": [], "sell_points": []}


def test_signal_points_iso_datetime_normalized(monkeypatch) -> None:
    bridge = BacktestBridge()
    trades = [
        {"datetime": pd.Timestamp("2024-01-29"), "direction": "BUY", "price": 13.25}
    ]
    monkeypatch.setattr(
        bridge, "run_strategy_backtest", lambda df, s, p=None: _fake_run_result(trades)
    )
    points = bridge.signal_points(_frame(), "ma_cross")
    assert points["buy_points"][0]["date"] == "2024-01-29"


def test_signal_points_unknown_direction_fails(monkeypatch) -> None:
    bridge = BacktestBridge()
    trades = [
        {"datetime": 20240129, "direction": "HOLD", "price": 13.25}
    ]
    monkeypatch.setattr(
        bridge, "run_strategy_backtest", lambda df, s, p=None: _fake_run_result(trades)
    )
    with pytest.raises(UpstreamContractError):
        bridge.signal_points(_frame(), "ma_cross")


def test_signal_points_missing_trades_key_fails(monkeypatch) -> None:
    bridge = BacktestBridge()
    monkeypatch.setattr(
        bridge,
        "run_strategy_backtest",
        lambda df, s, p=None: {"performance": {}, "equity_curve": []},
    )
    with pytest.raises(MissingKeyError):
        bridge.signal_points(_frame(), "ma_cross")
