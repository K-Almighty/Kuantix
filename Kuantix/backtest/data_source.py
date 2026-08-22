"""实时回测数据源（设计一：``data_source`` 三模式 + auto 优先级，v1.4 增量）。

本模块是「读数据」这一步的数据源分支工具，**不涉及策略/绩效/存储链路**：

- :func:`parse_data_source` —— 校验 ``auto|local|live``，非法 → 400（fail-loud）；
- :func:`local_has_data` —— 本地存在性探测（auto 分支的判定依据）；
- :func:`fetch_live_frame` —— 经 :class:`~Kuantix.adapters.quotation.QuotationFetcher`
  实时拉取单标的日线并转成与本地同构的 DataFrame；
- :func:`_live_years_for` —— 按 ``[start, end]`` 计算回溯年数（近似覆盖 + 缓冲）。

红线遵循
--------
- **R2**：本模块只 import adapters（``QuotationFetcher`` / ``bars_to_frame``），
  **不直接 import easy_tdx**；实时拉取统一经 :class:`QuotationFetcher`。
- **NF-26（D1.2/D1.5）**：``auto`` 是**数据源优先级**不是错误兜底 —— 本地文件
  存在但读失败 → 显式抛错，绝不静默降级到 live；live 拉取失败/返回空 →
  :class:`~Kuantix.core.fail_loud.DataIntegrityError`（Job 以 422 + 明确原因失败）。
- **R4**：全显式异常，禁止 ``except: pass`` 与双参 ``.get``。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from Kuantix.adapters.factor_bridge import bars_to_frame
from Kuantix.adapters.quotation import QuotationFetcher
from Kuantix.core.fail_loud import DataIntegrityError, MissingKeyError
from Kuantix.core.market import MarketProfile

__all__ = [
    "DATA_SOURCE_VALUES",
    "parse_data_source",
    "local_has_data",
    "fetch_live_frame",
    "_live_years_for",
]

#: data_source 合法取值（契约 v1.4 B2/B5，默认 ``auto``）
DATA_SOURCE_VALUES: tuple[str, ...] = ("auto", "local", "live")


def parse_data_source(value: str) -> str:
    """解析并校验 ``data_source`` 取值。

    Args:
        value: 原始取值（大小写不敏感）。

    Returns:
        规范化后的小写取值（``auto`` / ``local`` / ``live``）。

    Raises:
        MissingKeyError: 取值非法（→ 400，fail-loud，D1.6）。
    """
    text = str(value).strip().lower()
    if text not in DATA_SOURCE_VALUES:
        raise MissingKeyError(
            f"[fail-loud/NF-26] data_source 取值非法: {value!r}"
            f"（允许 {list(DATA_SOURCE_VALUES)}）"
        )
    return text


def local_has_data(reader: Any, profile: MarketProfile, code: str) -> bool:
    """探测本地是否已有该标的的日线（auto 分支的存在性判定，D1.2）。

    语义（设计文档 08 T04：SQLite 优先）：
    - 先经 ``profile.exchange_for_code`` 解析交易所（代码非法在此显式抛）；
    - reader 具备 ``has_data(exchange, code)``（双后端 L1Reader）→ 优先用它
      （SQLite 主存储 O(1) 判定，镜像兜底）；
    - reader 无 ``has_data`` 但具备 ``day_path``（旧版/鸭子类型）→ 以
      文件存在性为准；
    - reader 两者都没有（测试注入的鸭子类型 reader）→ 无法探测，
      **保守视为本地有数据**（走 local 读，零网络，保持既有测试语义）。

    Args:
        reader: L1 读侧（鸭子类型，需 ``read_daily_frame``）。
        profile: 市场档案（提供 ``exchange_for_code``）。
        code: 6 位证券代码。

    Returns:
        本地是否已有该标的日线。
    """
    exchange = profile.exchange_for_code(code)
    has_data = getattr(reader, "has_data", None)
    if has_data is not None:
        return bool(has_data(exchange, code))
    day_path = getattr(reader, "day_path", None)
    if day_path is None:
        return True
    return day_path(exchange, code).is_file()


def _live_years_for(start: dt.date, end: dt.date) -> int:
    """按 ``[start, end]`` 计算实时拉取的回溯年数。

    公式：``(end.year - start.year + 1) + 1``（跨年份数 + 1 年缓冲），
    内部再经 ``trading_days_per_year + 20`` 缓冲换算成条数（D1.4），
    随后统一按 ``[start, end]`` 过滤，保证区间完整覆盖。

    Args:
        start: 起始日期（含）。
        end: 结束日期（含）。

    Returns:
        回溯年数（≥ 2）。

    Raises:
        DataIntegrityError: ``start > end``（区间非法）。
    """
    if start > end:
        raise DataIntegrityError(
            f"[fail-loud/NF-26] 实时拉取区间非法: start {start} > end {end}"
        )
    return (end.year - start.year + 1) + 1


def fetch_live_frame(
    fetcher: QuotationFetcher,
    profile: MarketProfile,
    code: str,
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """实时拉取单标的日线并转成与本地同构的 DataFrame（D1.4/D1.5）。

    口径与本地 L1 一致（RD-5/RD-8 已在适配层保证）：
    - ``adjust=Adjust.NONE`` 未复权（``fetch_kline`` 默认，不传）；
    - vol 单位=手（``_frame_to_bars`` 已 ÷lot_size）；
    - 列格式与 :func:`~Kuantix.adapters.factor_bridge.bars_to_frame` 一致。

    Args:
        fetcher: 在线拉取器（测试可注入假实现）。
        profile: 市场档案（提供 ``exchange_for_code`` / ``market`` / 时区）。
        code: 6 位证券代码。
        start: 起始日期（含）。
        end: 结束日期（含）。

    Returns:
        过滤 ``[start, end]`` 后的 DataFrame（``datetime/open/high/low/close/vol/amount``）。

    Raises:
        DataIntegrityError: 代码非法 / 拉取失败 / 返回空 —— 统一 fail-loud，
            消息含 code 与原因，绝不静默回退。
    """
    exchange = profile.exchange_for_code(code)
    years = _live_years_for(start, end)
    try:
        bars = fetcher.fetch_kline(
            profile.market, code, years=years, exchange=exchange
        )
    except DataIntegrityError:
        raise
    except Exception as exc:  # noqa: BLE001 - 上游网络/协议异常统一包装（D1.5）
        raise DataIntegrityError(
            f"[fail-loud/NF-26] 实时拉取失败 {code}: {type(exc).__name__}: {exc}"
        ) from exc
    if not bars:
        raise DataIntegrityError(
            f"[fail-loud/NF-26] 实时拉取返回空: {code}"
            f"（无此标的/退市/区间无数据），拒绝以空数据继续"
        )
    frame = bars_to_frame(bars)
    dt_series = pd.to_datetime(frame["datetime"])
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    mask = (dt_series >= start_ts) & (dt_series <= end_ts)
    return frame[mask].reset_index(drop=True)
