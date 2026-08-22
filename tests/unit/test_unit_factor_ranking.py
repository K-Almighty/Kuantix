"""多因子排名服务（F7）单元测试。

验证：排名返回结构、综合分排序、top 组合回测指标、缺因子跳过语义。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from Kuantix.factor.ranking import (
    DEFAULT_WEIGHTS,
    FactorRank,
    FactorRankingService,
    RankingConfig,
    _composite_score,
    _performance_metrics,
    _top_portfolio_returns_from_merged,
)


def _merged_df() -> pd.DataFrame:
    """构造 3 日 × 6 只的因子×收益长表（因子与收益正相关，便于断言 IC>0）。"""
    rows: list[dict] = []
    dates = [20240102, 20240103, 20240104]
    # 因子值递增对应收益递增（强正相关）
    for date in dates:
        for code, factor, ret in [
            ("000001", 1.0, 0.01),
            ("000002", 2.0, 0.02),
            ("300001", 3.0, 0.03),
            ("300002", 4.0, 0.04),
            ("600001", 5.0, 0.05),
            ("600002", 6.0, 0.06),
        ]:
            rows.append({"date": date, "code": code, "mom": factor, "_r": ret})
    return pd.DataFrame(rows)


def test_top_portfolio_returns_top_fraction() -> None:
    """top 20% 组合应取每日期因子值最高的一只（6 只 × 0.2 ≈ 1 只）。"""
    merged = _merged_df()
    series = _top_portfolio_returns_from_merged(merged, "mom", 0.2)
    assert len(series) == 3
    # 最高因子值 600002 的收益 0.06
    assert all(abs(v - 0.06) < 1e-9 for v in series.values)


def test_performance_metrics_positive_returns() -> None:
    """全正收益：总收益为正、回撤为 0、胜率 1、夏普为正。"""
    metrics = _performance_metrics(
        pd.Series([0.01, 0.02, 0.03]), periods_per_year=242
    )
    assert metrics["total_return"] is not None and metrics["total_return"] > 0
    assert metrics["max_drawdown"] is not None and metrics["max_drawdown"] == 0
    assert metrics["win_rate"] == 1.0
    assert metrics["sharpe"] is not None and metrics["sharpe"] > 0


def test_performance_metrics_empty() -> None:
    """空序列 → 全部指标 None（不抛错）。"""
    metrics = _performance_metrics(pd.Series(dtype=float))
    assert metrics["total_return"] is None
    assert metrics["sharpe"] is None


def test_composite_score_drawdown_penalty() -> None:
    """回撤越小（正数越小）综合分越高（回撤 z-score 取负）。"""
    base = {"total_return": 0.1, "sharpe": 1.0, "ic_mean": 0.05, "ir": 0.5, "top_minus_bottom": 0.02}
    low_dd = _composite_score({**base, "max_drawdown": 0.1}, DEFAULT_WEIGHTS)
    high_dd = _composite_score({**base, "max_drawdown": 0.5}, DEFAULT_WEIGHTS)
    assert low_dd > high_dd


def test_composite_score_missing_keys() -> None:
    """部分指标缺失（None）时按剩余权重归一化，不抛错。"""
    score = _composite_score(
        {"total_return": 0.1, "sharpe": None, "max_drawdown": 0.2, "ic_mean": None, "ir": None, "top_minus_bottom": None},
        DEFAULT_WEIGHTS,
    )
    assert 0.0 <= score <= 100.0


def test_rank_service_missing_data_fails_loud(
    tmp_path, monkeypatch
) -> None:
    """全部因子无数据 → 显式 DataIntegrityError。"""
    from Kuantix.core.fail_loud import DataIntegrityError

    # 用假 factor_service：store.load 恒空
    class _FakeStore:
        def load(self, *args, **kwargs):
            return pd.DataFrame(columns=["date", "code", "value"])

    class _FakeEngine:
        def compute_forward_returns(self, pool, period):
            return pd.DataFrame(columns=["date", "code", f"forward_{period}d"])

    class _FakeSvc:
        _store = _FakeStore()
        _engine = _FakeEngine()

        def _load_pool(self, req, profile):
            return {"000001": pd.DataFrame()}

    svc = FactorRankingService(_FakeSvc())  # type: ignore[arg-type]
    with pytest.raises(DataIntegrityError):
        svc.rank(["mom", "turn"], "CN")


def test_rank_service_returns_sorted(
    monkeypatch, tmp_path
) -> None:
    """含数据因子时返回按综合分降序的排名，字段齐全。"""
    dates = [20240102, 20240103, 20240104]
    rows = []
    for d in dates:
        for code, f, r in [("000001", 1.0, 0.01), ("000002", 3.0, 0.03), ("600001", 5.0, 0.06)]:
            rows.append({"date": d, "code": code, "value": f})
    factor_df = pd.DataFrame(rows)

    class _FakeStore:
        def load(self, factor, start=None, end=None):
            if factor == "other":
                return pd.DataFrame(columns=["date", "code", "value"])
            return factor_df.copy()

    class _FakeEngine:
        def compute_forward_returns(self, pool, period):
            rows2 = []
            for d in dates:
                for code, _, r in [
                    ("000001", 1.0, 0.01), ("000002", 3.0, 0.03), ("600001", 5.0, 0.06),
                ]:
                    rows2.append({"date": d, "code": code, f"forward_{period}d": r})
            return pd.DataFrame(rows2)

    class _FakeSvc:
        _store = _FakeStore()
        _engine = _FakeEngine()

        def _load_pool(self, req, profile):
            return {"000001": pd.DataFrame(), "000002": pd.DataFrame(), "600001": pd.DataFrame()}

    svc = FactorRankingService(_FakeSvc())  # type: ignore[arg-type]
    payload = svc.rank(
        ["mom", "other"], "CN", config=RankingConfig(forward_period=1, top_fraction=0.5)
    )
    assert len(payload["ranking"]) >= 1
    # 只有 "mom" 有数据，"other" 应被跳过
    assert payload["ranking"][0]["factor"] == "mom"
    assert "other" in payload["skipped"]
    for item in payload["ranking"]:
        assert "factor" in item
        assert "score" in item
        assert "total_return" in item
        assert "rank" in item
    # 综合分降序
    scores = [item["score"] for item in payload["ranking"]]
    assert scores == sorted(scores, reverse=True)
