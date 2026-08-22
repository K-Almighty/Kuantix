"""个股详情路由（前缀 ``/api/v1/stock``）。

端点：
- ``GET /detail/{code}`` —— 个股某周期 K 线 + 技术指标（MA/BOLL/ENE/SAR/
  MACD/KDJ/RSI/WR/BIAS/OBV）+ 核心数据，支持复权切换（none/qfq/hfq）；
- ``GET /order-book/{code}`` —— 五档盘口 + 实时快照（换手/量比/PE/股本）；
- ``GET /transactions/{code}`` —— 当日逐笔成交明细；
- ``GET /capital-flow/{code}`` —— 资金流向（主力/散户 + 5 日大中小单）。

红线遵循
--------
- 本模块只做行情展示，路径不含 order/trade/buy/sell（R5）；与回测路由分离，
  互不污染 B5 契约；非法周期/代码/复权走 fail-loud（R4）。
- 实时面板端点依赖 tdx 在线链路；未配置 tdx 时 501 语义报错，不静默降级。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from Kuantix.api.deps import ServiceContainer, get_services, respond

__all__ = ["router"]

router = APIRouter()

#: 默认计算全部指标；前端可按需关闭
DEFAULT_INDICATORS = (
    "ma,boll,ene,sar,macd,kdj,rsi,wr,bias,obv"
)


def _services(request: Request) -> ServiceContainer:
    return get_services(request)


@router.get("/detail/{code}")
def stock_detail(
    request: Request,
    code: str,
    market: Annotated[str, Query(description="市场码（CN/US/HK）")] = "CN",
    period: Annotated[str, Query(description="周期键")] = "day",
    adjust: Annotated[
        str, Query(description="复权方式（none=不复权/qfq=前复权/hfq=后复权）")
    ] = "none",
    limit: Annotated[int, Query(description="返回 bar 上限", ge=1, le=2000)] = 500,
    indicators: Annotated[
        str,
        Query(
            description=(
                "逗号分隔的指标子集"
                "（ma,boll,ene,sar,macd,kdj,rsi,wr,bias,obv）"
            ),
        ),
    ] = DEFAULT_INDICATORS,
    ma: Annotated[
        str, Query(description="MA 均线窗口（逗号分隔，如 5,10,20,60）")
    ] = "5,10,20,60",
):
    """个股详情：多周期 K 线 + 技术指标 + 核心数据。

    同步 def 路由（非 async）：内部是 pandas 重采样 + SQLite IO 的同步
    重计算，FastAPI 会自动放入线程池执行，避免阻塞事件循环。

    Args:
        code: 6 位证券代码。
        market: 市场码。
        period: 周期（min1/min5/min15/min30/min60/day/week/month/quarter/year）。
        adjust: 复权方式（仅日基周期生效）。
        limit: bar 上限。
        indicators: 需计算的指标（默认全算）。
        ma: MA 窗口自定义（通达信参数面板语义）。

    Returns:
        信封 ``{code, market, period, adjust, available, turnover_estimated,
        bars, indicators}``。
    """
    svc = _services(request).stock_detail_service
    indicator_list = [i.strip() for i in indicators.split(",") if i.strip()]
    ma_windows: list[int] = []
    for w in ma.split(","):
        w = w.strip()
        if w.isdigit():
            ma_windows.append(int(w))
    payload = svc.get_detail(
        code,
        market=market,
        period=period,
        limit=limit,
        indicators=indicator_list,
        adjust=adjust,
        ma_windows=ma_windows or (5, 10, 20, 60),
    )
    return respond(payload, market)


@router.get("/quotes")
def stock_quotes(
    request: Request,
    market: Annotated[str, Query(description="市场码（当前仅 CN）")] = "CN",
    codes: Annotated[str, Query(description="逗号分隔的证券代码（≤80 只）")] = "",
):
    """批量实时报价（自选股侧栏；tdx 在线直连）。"""
    svc = _services(request).stock_detail_service
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    payload = {"items": svc.get_quotes(code_list, market=market)}
    return respond(payload, market)


@router.get("/order-book/{code}")
def stock_order_book(
    request: Request,
    code: str,
    market: Annotated[str, Query(description="市场码（当前仅 CN）")] = "CN",
):
    """五档盘口 + 实时快照（tdx 在线直连，含换手/量比/PE/股本字段）。"""
    svc = _services(request).stock_detail_service
    payload = svc.get_order_book(code, market=market)
    return respond(payload, market)


@router.get("/transactions/{code}")
def stock_transactions(
    request: Request,
    code: str,
    market: Annotated[str, Query(description="市场码（当前仅 CN）")] = "CN",
    count: Annotated[
        int, Query(description="拉取条数", ge=1, le=2000)
    ] = 300,
):
    """当日逐笔成交明细（时间升序；bs: 0=买 1=卖 2=中性）。"""
    svc = _services(request).stock_detail_service
    payload = svc.get_transactions(code, market=market, count=count)
    return respond(payload, market)


@router.get("/capital-flow/{code}")
def stock_capital_flow(
    request: Request,
    code: str,
    market: Annotated[str, Query(description="市场码（当前仅 CN）")] = "CN",
):
    """资金流向（今日主力/散户净额 + 5 日大中小单净额，单位元）。"""
    svc = _services(request).stock_detail_service
    payload = svc.get_capital_flow(code, market=market)
    return respond(payload, market)
