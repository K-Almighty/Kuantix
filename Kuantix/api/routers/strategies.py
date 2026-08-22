"""strategies 路由（契约 §2.2 S1–S5，v1.3 增量，前缀 ``/api/v1/strategies``）。

端点：
- S1 ``GET  /`` —— 策略库列表（分页，可选 ``kind`` 过滤）；
- S2 ``POST /`` —— 保存策略/组合/多策略方案（201 + SavedStrategy）；
- S3 ``GET  /{strategy_id}`` —— 策略详情；不存在 → 404；
- S4 ``DELETE /{strategy_id}`` —— 删除策略；不存在 → 404（fail-loud，
  不静默成功）；
- S5 ``POST /run-multi`` —— 多策略组合回测（N 策略 × 各自标的，资金 1/N）
  → Job 信封（module=``backtest``，action=``multi``）。

红线遵循
--------
- R2：多策略回测引擎调用收敛在 :class:`~Kuantix.adapters.backtest_bridge.BacktestBridge`
  （本模块不直接 import easy_tdx）；
- R5：路径不含 order/trade/buy/sell；
- R6：代码→交易所映射由 :class:`MultiStrategyService` 经 MarketProfile 处理。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import Response

from Kuantix import __version__
from Kuantix.api.deps import ServiceContainer, get_services, resolve_market, respond
from Kuantix.api.schemas import MultiStrategyRunRequest, StrategyCreate
from Kuantix.backtest.portfolio_service import (
    MultiStrategyItem,
)
from Kuantix.backtest.portfolio_service import (
    MultiStrategyRunRequest as MultiStrategyDomainRequest,
)
from Kuantix.backtest.strategy_store import STRATEGY_KINDS
from Kuantix.core.envelope import Envelope, Timer
from Kuantix.core.fail_loud import MissingKeyError
from Kuantix.main import envelope_response

__all__ = ["router"]

router = APIRouter()


def _services(request: Request) -> ServiceContainer:
    return get_services(request)


def _require_strategy_store(container: ServiceContainer) -> Any:
    """取策略库存储；未装配时显式 400（fail-loud，不静默）。"""
    if container.strategy_store is None:
        raise MissingKeyError(
            "[fail-loud/NF-26] 策略库未装配（组合根缺失 strategy_store）"
        )
    return container.strategy_store


def _require_multi_service(container: ServiceContainer) -> Any:
    """取多策略回测服务；未装配时显式 400（fail-loud，不静默）。"""
    if container.multi_strategy_service is None:
        raise MissingKeyError(
            "[fail-loud/NF-26] 多策略回测服务未装配（组合根缺失 multi_strategy_service）"
        )
    return container.multi_strategy_service


def _make_multi_runner(
    container: ServiceContainer,
    job_id: str,
    req: MultiStrategyDomainRequest,
) -> Callable[[Callable[[dict[str, Any]], None], Callable[[Any], None]], dict[str, Any]]:
    """构造 S5 后台执行体：调 MultiStrategyService.run 并产出摘要。"""

    def runner(
        progress_cb: Callable[[dict[str, Any]], None],
        register_handle: Callable[[Any], None],
    ) -> dict[str, Any]:
        service = _require_multi_service(container)
        return service.run(job_id, req, progress_cb)

    return runner


# ---------------------------------------------------------------------------
# S1 列表 / S2 创建
# ---------------------------------------------------------------------------


@router.get("", summary="策略库列表（S1）")
async def strategies_list(
    request: Request,
    kind: Annotated[
        str | None,
        Query(description="kind 过滤（single/portfolio/multi，可空=全部）"),
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Response:
    """策略列表（分页壳，契约 §1.6）。"""
    container = _services(request)
    store = _require_strategy_store(container)
    if kind is not None and kind not in STRATEGY_KINDS:
        raise MissingKeyError(
            f"[fail-loud/NF-26] strategies.list.kind 非法: {kind!r}"
            f"（允许 {list(STRATEGY_KINDS)}）"
        )
    with Timer() as timer:
        payload = store.list(kind=kind, page=page, page_size=page_size)
    return respond(
        payload, container.config.markets.default, elapsed_ms=timer.elapsed_ms
    )


@router.post("", summary="保存策略（S2）", status_code=201)
async def strategies_create(request: Request, body: StrategyCreate) -> Response:
    """保存策略/组合/多策略方案，返回含服务端生成字段的 SavedStrategy。"""
    container = _services(request)
    store = _require_strategy_store(container)
    with Timer() as timer:
        view = store.create(body.model_dump(), app_version=__version__)
    envelope = Envelope.ok(
        view,
        market=container.config.markets.default,
        version=__version__,
        elapsed_ms=timer.elapsed_ms,
    )
    response = envelope_response(envelope)
    response.status_code = 201
    return response


# ---------------------------------------------------------------------------
# S3 详情 / S4 删除
# ---------------------------------------------------------------------------


@router.get("/{strategy_id}", summary="策略详情（S3）")
async def strategies_get(request: Request, strategy_id: str) -> Response:
    """读取单个策略；不存在 → 404。"""
    container = _services(request)
    store = _require_strategy_store(container)
    view = store.get(strategy_id)
    if view is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"策略不存在: {strategy_id}"
        )
    return respond(view, container.config.markets.default)


@router.delete("/{strategy_id}", summary="删除策略（S4）")
async def strategies_delete(request: Request, strategy_id: str) -> Response:
    """删除策略；不存在 → 404（fail-loud，不静默成功）。"""
    container = _services(request)
    store = _require_strategy_store(container)
    removed = store.delete(strategy_id)
    if not removed:
        raise StarletteHTTPException(
            status_code=404, detail=f"策略不存在: {strategy_id}"
        )
    return respond(
        {"removed": strategy_id}, container.config.markets.default
    )


# ---------------------------------------------------------------------------
# S5 多策略组合回测
# ---------------------------------------------------------------------------


@router.post("/run-multi", summary="多策略组合回测（S5）")
async def strategies_run_multi(
    request: Request, body: MultiStrategyRunRequest
) -> Response:
    """触发多策略组合回测（N 策略 × 各自标的，资金 1/N）→ Job 信封。"""
    container = _services(request)
    _require_multi_service(container)
    code = resolve_market(container.config, body.market)
    if not body.items:
        raise MissingKeyError("[fail-loud/NF-26] 多策略回测 items 为空")
    items = tuple(
        MultiStrategyItem(
            strategy=item.strategy.strip(),
            label=item.label.strip(),
            code=item.code.strip(),
            params=dict(item.params),
        )
        for item in body.items
        if item.strategy.strip() and item.label.strip() and item.code.strip()
    )
    if not items:
        raise MissingKeyError(
            "[fail-loud/NF-26] 多策略回测 items 全部为空（strategy/label/code 必填）"
        )
    req = MultiStrategyDomainRequest(
        market=code,
        items=items,
        cash=body.cash,
        commission=body.commission,
        min_commission=body.min_commission,
        stamp_tax=body.stamp_tax,
        slippage=body.slippage,
        execution=body.execution,
        start=body.start,
        end=body.end,
    )
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    runner = _make_multi_runner(container, job_id, req)
    params = {
        "market": code,
        "items": [
            {"strategy": item.strategy, "label": item.label, "code": item.code}
            for item in items
        ],
        "cash": body.cash,
        "start": body.start.isoformat(),
        "end": body.end.isoformat(),
        "execution": body.execution,
    }
    with Timer() as timer:
        job = container.jobs.submit(
            "backtest", "multi", code, params, runner, job_id=job_id
        )
    return respond(job, code, elapsed_ms=timer.elapsed_ms)
