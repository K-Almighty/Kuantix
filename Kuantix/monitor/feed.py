"""QuoteFeed —— 仅交易时段批量轮询实时报价（NF-24 / NF-28）。

职责
----
- 经 :class:`MarketProfile` 判定交易时段，非交易时段**跳过**（``trading_hours_only``）；
- 批量行情经 :class:`QuotationFetcher` 拉取，使用 **独立连接**
  （``QuotationFetcher(shared_connection=False)``，NF-28 监控链路与回补链路隔离）；
- 批量轮询之间按 ``min_request_interval`` 限速退避（NF-24），失败显式报错/重试，
  不静默吞掉。

单位口径（契约 §3.5 / §1.4）
----------------------------
- ``Quote.change_pct`` 为**小数比例**（``0.05`` = 5%）。
  适配层 :meth:`QuotationFetcher.fetch_quotes` 已保证该口径（team-lead 裁决 1），
  本模块**幂等透传**、不做换算，全链路只有一种口径。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Sequence

from Kuantix.adapters.quotation import QuotationFetcher
from Kuantix.adapters.tdx_client import TdxClientFactory
from Kuantix.core.contracts import Quote
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    NotSupportedError,
    UpstreamContractError,
    require_finite,
)
from Kuantix.core.market import MarketProfile, get_market_profile

__all__ = ["QuoteFeed", "QuoteFetchError", "DEFAULT_BATCH_SIZE"]

logger = logging.getLogger(__name__)

#: 单次批量报价上限（上游协议限制 80，见 config.toml [monitor].batch_size）
DEFAULT_BATCH_SIZE = 80


class QuoteFetchError(DataIntegrityError):
    """批量报价拉取在重试后仍失败（fail-loud，不静默）。"""


class QuoteFeed:
    """实时报价轮询器。

    Args:
        fetcher: 行情拉取器；``None`` 时从配置构建独立连接的 :class:`QuotationFetcher`
            （``shared_connection=False``，NF-28）。
        profile: 市场档案；``None`` 时按 ``market`` 从注册表取。
        market: 默认市场码（``CN``）。
        batch_size: 单次批量上限。
        min_request_interval: 相邻请求最小间隔（秒，NF-24 限速）。
        retry_backoff_seconds: 失败退避初始秒数。
        retry_max_attempts: 单批最大重试次数。
        trading_hours_only: 是否仅在交易时段轮询。
    """

    def __init__(
        self,
        fetcher: QuotationFetcher | None = None,
        profile: MarketProfile | None = None,
        *,
        market: str = "CN",
        batch_size: int = DEFAULT_BATCH_SIZE,
        min_request_interval: float = 0.05,
        retry_backoff_seconds: float = 1.0,
        retry_max_attempts: int = 3,
        trading_hours_only: bool = True,
    ) -> None:
        self._market = str(market).strip().upper()
        self._profile = profile if profile is not None else get_market_profile(self._market)
        self._fetcher = fetcher if fetcher is not None else self._default_fetcher()
        if batch_size <= 0:
            raise DataIntegrityError(f"[fail-loud/NF-26] batch_size 必须为正，实际 {batch_size!r}")
        self._batch_size = int(batch_size)
        self._min_request_interval = require_finite(min_request_interval, "min_request_interval")
        self._retry_backoff = require_finite(retry_backoff_seconds, "retry_backoff_seconds")
        self._retry_max_attempts = int(retry_max_attempts)
        if self._retry_max_attempts <= 0:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] retry_max_attempts 必须为正，实际 {retry_max_attempts!r}"
            )
        self._trading_hours_only = bool(trading_hours_only)
        #: 上次请求时刻（限速依据，NF-24）
        self._last_request_ts: float = 0.0

    @staticmethod
    def _default_fetcher() -> QuotationFetcher:
        """构建独立连接（非池化）的行情拉取器（NF-28 监控链路专用）。"""
        factory = TdxClientFactory.from_config()
        return QuotationFetcher(factory, shared_connection=False)

    @property
    def profile(self) -> MarketProfile:
        """当前市场档案。"""
        return self._profile

    @property
    def market(self) -> str:
        """默认市场码。"""
        return self._market

    def is_trading_session(self, moment: dt.datetime | None = None) -> bool:
        """当前（或给定时刻）是否处于连续竞价交易时段。"""
        return self._profile.is_open_now(moment)

    # ------------------------------------------------------------------ #
    # 轮询
    # ------------------------------------------------------------------ #

    def poll(self, codes: Sequence[str], *, market: str | None = None) -> list[Quote]:
        """批量轮询实时报价（仅交易时段）。

        Args:
            codes: 证券代码列表（不含交易所前缀，如 ``["600519", "000001"]``）。
            market: 市场码；``None`` 使用默认市场。

        Returns:
            :class:`Quote` 列表（``change_pct`` 为**小数比例**）。

        Raises:
            QuoteFetchError: 重试后仍失败（不静默）。
            UnknownValueError: 市场未注册。
            NotSupportedError: 市场未实现（如 HK/US 占位）。
        """
        target_market = str(market if market is not None else self._market).strip().upper()
        profile = get_market_profile(target_market) if market is not None else self._profile

        if self._trading_hours_only and not profile.is_open_now():
            logger.info(
                "监控轮询跳过：非交易时段 market=%s", target_market
            )
            return []

        code_list = [str(c).strip() for c in codes]
        if not code_list:
            # 空代码列表是合法状态（如空自选），直接返回空，不触发网络
            return []
        # 解析交易所前缀（A 股代码 → sh/sz/bj），失败显式抛（NF-26）
        pairs = self._resolve_exchange_pairs(profile, code_list)

        quotes: list[Quote] = []
        for offset in range(0, len(pairs), self._batch_size):
            batch = pairs[offset : offset + self._batch_size]
            self._throttle()
            batch_quotes = self._fetch_batch_with_retry(target_market, batch)
            # 适配层已返回契约小数比例 change_pct（§1.4/§3.5，team-lead 裁决 1），
            # 这里幂等透传，不再做任何换算（全链路只有一种口径）。
            quotes.extend(self._passthrough_change_pct(batch_quotes))
        return quotes

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_exchange_pairs(profile: MarketProfile, codes: Sequence[str]) -> list[tuple[str, str]]:
        """把纯代码解析成 ``(exchange, code)`` 对，供上游批量接口使用。"""
        pairs: list[tuple[str, str]] = []
        for code in codes:
            exchange = profile.exchange_for_code(code)
            pairs.append((exchange, code))
        return pairs

    def _throttle(self) -> None:
        """NF-24 限速：相邻请求间隔不得小于 ``min_request_interval``。"""
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)

    def _fetch_batch_with_retry(
        self, market: str, pairs: Sequence[tuple[str, str]]
    ) -> list[Quote]:
        """拉取一批报价，失败退避重试，重试耗尽显式抛错（不静默）。"""
        last_error: Exception | None = None
        for attempt in range(1, self._retry_max_attempts + 1):
            try:
                fetched = self._fetcher.fetch_quotes(market, list(pairs))
                self._last_request_ts = time.monotonic()
                return list(fetched)
            except (UpstreamContractError, NotSupportedError) as exc:
                # 契约/能力类错误不重试（重试也不会变好），显式抛
                raise QuoteFetchError(
                    f"[fail-loud/NF-26] 批量报价拉取失败 market={market} codes={pairs}: {exc}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 网络类错误退避重试，但绝不静默
                last_error = exc
                logger.warning(
                    "批量报价拉取失败（第 %s/%s 次）market=%s codes=%s: %s",
                    attempt,
                    self._retry_max_attempts,
                    market,
                    [p[1] for p in pairs],
                    exc,
                )
                if attempt < self._retry_max_attempts:
                    time.sleep(self._retry_backoff * attempt)
        assert last_error is not None  # 至少尝试过一次
        raise QuoteFetchError(
            f"[fail-loud/NF-26] 批量报价拉取重试 {self._retry_max_attempts} 次仍失败 "
            f"market={market} codes={[p[1] for p in pairs]}: {last_error}"
        ) from last_error

    @staticmethod
    def _passthrough_change_pct(quotes: Sequence[Quote]) -> list[Quote]:
        """幂等透传报价（适配层已保证 ``change_pct`` 为小数比例）。

        历史背景（team-lead 裁决 1）：早期 ``QuotationFetcher._frame_to_quotes``
        返回百分数（``* 100``），本模块曾在边界 ÷100 归一。裁决改为修适配层
        根源后，全链路只有**小数比例**一种口径（0.05 = 5%），此处仅透传。

        保留该函数是为了调用点语义清晰：万一未来适配层口径被误改，
        可在这一处集中加回防御，而不必改 poll 主体。
        """
        return list(quotes)
