"""QuoteFeed 白盒单测：非交易时段跳过 / 批量轮询 / 限速 / 重试 / 单位口径。"""

from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest

from Kuantix.core.contracts import Quote
from Kuantix.core.fail_loud import MissingConfigError
from Kuantix.monitor import QuoteFeed

from tests.unit.monitor._helpers import make_quote


class _FakeProfile:
    """可编程 MarketProfile 替身（只测 feed 用到的接口）。"""

    def __init__(self, open_now: bool = True, exchange: str = "sh"):
        self._open = open_now
        self._exchange = exchange

    def is_open_now(self, moment=None):
        return self._open

    def exchange_for_code(self, code: str) -> str:
        return self._exchange


class _FakeFetcher:
    """可编程 QuotationFetcher 替身。

    - ``quotes``：每次调用返回的报价（或按 call 序号）；
    - ``fail_until``：前 N 次抛网络异常（模拟重试）。
    """

    def __init__(self, quotes=None, fail_until: int = 0):
        self.quotes = list(quotes) if quotes is not None else []
        self.fail_until = fail_until
        self.calls: list[tuple[str, list]] = []
        self.sleeps: list[float] = []

    def fetch_quotes(self, market, codes):
        self.calls.append((market, list(codes)))
        if len(self.calls) <= self.fail_until:
            raise ConnectionError("network down")
        return list(self.quotes)


def _feed(open_now=True, fetcher=None, **kwargs):
    profile = _FakeProfile(open_now=open_now)
    fetcher = fetcher if fetcher is not None else _FakeFetcher()
    return QuoteFeed(
        fetcher=fetcher,
        profile=profile,
        market="CN",
        batch_size=kwargs.pop("batch_size", 80),
        min_request_interval=kwargs.pop("min_request_interval", 0.0),
        retry_backoff_seconds=kwargs.pop("retry_backoff_seconds", 0.01),
        retry_max_attempts=kwargs.pop("retry_max_attempts", 3),
        trading_hours_only=kwargs.pop("trading_hours_only", True),
    )


def test_poll_skipped_outside_trading_hours():
    feed = _feed(open_now=False)
    with mock.patch("Kuantix.monitor.feed.logger") as logger:
        result = feed.poll(["600519"])
    assert result == []
    logger.info.assert_called_once()


def test_poll_passthroughs_adapter_change_pct():
    """适配层已返回小数比例 change_pct（-0.08），feed 幂等透传、不做换算。

    team-lead 裁决 1：适配层根因修复后，全链路只有小数比例一种口径。
    """
    raw = make_quote(last=1545.6, prev_close=1680.0, change_pct=-0.08)  # 适配层比例口径
    fetcher = _FakeFetcher(quotes=[raw])
    feed = _feed(open_now=True, fetcher=fetcher)
    result = feed.poll(["600519"])
    assert len(result) == 1
    quote = result[0]
    assert quote.code == "600519"
    assert quote.change_pct == pytest.approx(-0.08)  # 小数比例（契约 §3.5），原样透传
    assert quote.last == 1545.6
    assert fetcher.calls[0][0] == "CN"
    assert fetcher.calls[0][1] == [("sh", "600519")]


def test_poll_batches_and_rate_limits():
    codes = [f"600{i:03d}" for i in range(10)]
    fetcher = _FakeFetcher(quotes=[make_quote(code=c) for c in codes[:2]])
    feed = _feed(open_now=True, fetcher=fetcher, batch_size=3, min_request_interval=0.05)
    with mock.patch("time.sleep") as sleep:
        feed.poll(codes)
    # 10 个代码，batch_size=3 → 4 批
    assert len(fetcher.calls) == 4
    assert sleep.call_count >= 3  # 批间限速


def test_poll_retry_then_success():
    fetcher = _FakeFetcher(
        quotes=[make_quote(last=1610.0)],
        fail_until=2,  # 前 2 次失败，第 3 次成功
    )
    feed = _feed(open_now=True, fetcher=fetcher, retry_max_attempts=3)
    result = feed.poll(["600519"])
    assert len(result) == 1
    assert len(fetcher.calls) == 3


def test_poll_fails_after_max_attempts():
    fetcher = _FakeFetcher(quotes=[], fail_until=999)
    feed = _feed(open_now=True, fetcher=fetcher, retry_max_attempts=2)
    with pytest.raises(Exception, match="重试"):
        feed.poll(["600519"])
    assert len(fetcher.calls) == 2


def test_poll_empty_codes_returns_empty():
    feed = _feed(open_now=True)
    assert feed.poll([]) == []


def test_poll_unknown_exchange_raises():
    class _BadProfile(_FakeProfile):
        def exchange_for_code(self, code):
            raise MissingConfigError(f"无法识别代码 {code}")

    feed = QuoteFeed(
        fetcher=_FakeFetcher(),
        profile=_BadProfile(open_now=True),
        market="CN",
    )
    with pytest.raises(MissingConfigError):
        feed.poll(["999999"])


def test_feed_builds_independent_fetcher_by_default():
    """默认构造走 QuotationFetcher(shared_connection=False)（NF-28 独立连接）。"""
    feed = QuoteFeed(profile=_FakeProfile(open_now=True), market="CN")
    assert feed._fetcher._shared is False
