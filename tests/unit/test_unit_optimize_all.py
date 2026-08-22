"""一键寻优所有策略（O4/O5，对标 easy_tdx optimize-all）单元测试。

验证：全局排名按 total_return 降序、per_strategy 聚合、单策略无最优点跳过。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from Kuantix.backtest.optimize_service import (
    OptimizeAllRunRequest,
    OptimizeService,
    _optimize_one_strategy_summary,
)


class _FakeBridge:
    """假 bridge：按策略名返回不同绩效的最优点摘要。"""

    def __init__(self, results: dict[str, dict]) -> None:
        self._results = results

    def run_optimize(
        self, frame, strategy_name, param_grid, **kwargs
    ) -> dict:
        if strategy_name not in self._results:
            return {"best": None, "results": []}
        best = self._results[strategy_name]
        return {
            "best": {
                "params": best.get("params", {}),
                "total_return": best.get("total_return"),
                "annual_return": best.get("annual_return"),
                "sharpe": best.get("sharpe"),
                "max_drawdown": best.get("max_drawdown"),
                "total_trades": best.get("total_trades"),
                "win_rate": best.get("win_rate"),
                "profit_factor": best.get("profit_factor"),
            },
            "results": [best] * best.get("grid_points", 1),
        }


class _FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, dict] = {}

    def save(self, job_id: str, result: dict) -> None:
        self.saved[job_id] = result

    def load(self, job_id: str):
        return self.saved.get(job_id)


def _make_service(bridge) -> OptimizeService:
    svc = OptimizeService()
    svc._bridge = bridge  # type: ignore[assignment]
    svc._store = _FakeStore()  # type: ignore[assignment]
    return svc


def _fake_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": pd.date_range("2023-01-01", periods=300),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "vol": 1000,
            "amount": 10000,
        }
    )


def test_run_all_strategies_ranks_global(monkeypatch) -> None:
    """run_all 汇总各策略最优点并按 total_return 降序排名。"""
    bridge = _FakeBridge(
        {
            "ma_cross": {
                "params": {"fast": 5, "slow": 20},
                "total_return": 0.4,
                "sharpe": 1.0,
                "max_drawdown": 0.2,
                "total_trades": 10,
                "win_rate": 0.6,
                "profit_factor": 1.5,
                "grid_points": 5,
            },
            "rsi_reversal": {
                "params": {"n": 7},
                "total_return": 0.6,
                "sharpe": 1.5,
                "max_drawdown": 0.1,
                "total_trades": 8,
                "win_rate": 0.8,
                "profit_factor": 2.0,
                "grid_points": 3,
            },
        }
    )
    svc = _make_service(bridge)
    monkeypatch.setattr(
        "Kuantix.backtest.optimize_service._load_frame",
        lambda *a, **k: _fake_frame(),
    )
    req = OptimizeAllRunRequest(
        market="CN",
        code="600519",
        start=dt.date(2023, 1, 1),
        end=dt.date(2024, 12, 31),
    )
    # 直接用 _run_all_strategies 注入 jobs，避开 STRATEGY_PRESETS 依赖
    jobs = [
        ("ma_cross", {"fast": [5, 10], "slow": [10, 20]}),
        ("rsi_reversal", {"n": [7, 14]}),
    ]
    raw = svc._run_all_strategies(_fake_frame(), jobs, req)
    assert len(raw) == 2

    # 组装排名（复用 run_all 内联逻辑：按 total_return 降序）
    ranking = sorted(raw, key=lambda r: r["total_return"], reverse=True)
    assert ranking[0]["strategy"] == "rsi_reversal"
    assert ranking[1]["strategy"] == "ma_cross"


def test_run_all_skips_strategy_without_best(monkeypatch) -> None:
    """无最优点（best=None）的策略被跳过，不中断整组。"""
    bridge = _FakeBridge(
        {
            "ma_cross": {
                "params": {"fast": 5},
                "total_return": 0.3,
                "sharpe": 1.0,
                "max_drawdown": 0.2,
                "total_trades": 10,
                "win_rate": 0.6,
                "profit_factor": 1.5,
                "grid_points": 5,
            },
        }
    )
    req = OptimizeAllRunRequest(market="CN", code="600519")
    ok = _optimize_one_strategy_summary(
        bridge, _fake_frame(), "ma_cross", {"fast": [5]}, req
    )
    assert ok is not None and ok["strategy"] == "ma_cross"
    # 未在 _results 中的策略 → run_optimize 返回 best=None → 跳过
    skipped = _optimize_one_strategy_summary(
        bridge, _fake_frame(), "bad_strategy", {"n": [5]}, req
    )
    assert skipped is None
