"""个股详情路由（前缀 ``/api/v1/stock``）。

端点：
- ``GET /detail/{code}`` —— 个股某周期 K 线 + 技术指标（MA/MACD/KDJ/RSI）
  + 核心数据（成交量/成交额/换手率），通达信风格详情页后端支柱。

红线遵循
--------
- 本模块只做行情展示，路径不含 order/trade/buy/sell（R5）；与回测路由分离，
  互不污染 B5 契约；非法周期/代码走 fail-loud（R4）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from Kuantix.api.deps import ServiceContainer, get_services, respond
from Kuantix.analysis.stock_detail import PERIODS

__all__ = ["router"]

router = APIRouter()

#: 默认计算全部指标；前端可按需关闭
DEFAULT_INDICATORS = "ma,macd,kdj,rsi"


def _services(request: Request) -> ServiceContainer:
    return get_services(request)


@router.get("/detail/{code}")
async def stock_detail(
    request: Request,
    code: str,
    market: Annotated[str, Query(description="市场码（CN/US/HK）")] = "CN",
    period: Annotated[str, Query(description="周期键")] = "day",
    limit: Annotated[int, Query(description="返回 bar 上限", ge=1, le=2000)] = 500,
    indicators: Annotated[
        str, Query(description="逗号分隔的指标子集（ma,macd,kdj,rsi）")
    ] = DEFAULT_INDICATORS,
):
    """个股详情：多周期 K 线 + 技术指标 + 核心数据。

    Args:
        code: 6 位证券代码。
        market: 市场码。
        period: 周期（day/week/month/year/min5/min15）。
        limit: bar 上限。
        indicators: 需计算的指标（默认全算）。

    Returns:
        信封 ``{code, market, period, available, turnover_estimated,
        bars, indicators}``。
    """
    svc = _services(request).stock_detail_service
    indicator_list = [i.strip() for i in indicators.split(",") if i.strip()]
    payload = svc.get_detail(
        code,
        market=market,
        period=period,
        limit=limit,
        indicators=indicator_list,
    )
    return respond(payload, market)
