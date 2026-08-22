"""portfolio 路由（契约 §2.1 P1–P3，v1.3 增量，前缀 ``/api/v1/portfolio``）。

端点：
- P1 ``POST /run`` —— 触发组合回测（1 策略 × N 标的，资金分仓）→ Job 信封
  （module=``backtest``，action=``portfolio``）；
- P2 ``GET  /jobs/{job_id}`` —— 组合回测进度（与 B3 同逻辑，薄转发）；
- P3 ``GET  /results/{job_id}`` —— 完整组合结果（PortfolioResult：
  total_performance / individual_results / equity_allocation /
  combined_equity）。

红线遵循
--------
- R2：组合回测引擎调用收敛在 :class:`~Kuantix.adapters.backtest_bridge.BacktestBridge`
  （本模块不直接 import easy_tdx）；
- R5：路径不含 order/trade/buy/sell；
- R6：代码→交易所映射由 :class:`PortfolioService` 经 MarketProfile 处理。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import Response

from Kuantix.api.deps import ServiceContainer, get_services, resolve_market, respond
from Kuantix.api.schemas import PortfolioRunRequest
from Kuantix.backtest.portfolio_service import (
    PortfolioRunRequest as PortfolioDomainRequest,
)
from Kuantix.core.envelope import Timer
from Kuantix.core.fail_loud import MissingKeyError

__all__ = ["router"]

router = APIRouter()

#: P1 标的池数量上限（与 PortfolioRunRequest.codes max_length 对齐）
MAX_CODES = 20


def _services(request: Request) -> ServiceContainer:
    return get_services(request)


def _require_portfolio_service(
    container: ServiceContainer,
) -> Any:
    """取组合回测服务；未装配时显式 400（fail-loud，不静默）。"""
    if container.portfolio_service is None:
        raise MissingKeyError(
            "[fail-loud/NF-26] 组合回测服务未装配（组合根缺失 portfolio_service）"
        )
    return container.portfolio_service


def _make_portfolio_runner(
    container: ServiceContainer,
    job_id: str,
    req: PortfolioDomainRequest,
) -> Callable[[Callable[[dict[str, Any]], None], Callable[[Any], None]], dict[str, Any]]:
    """构造 P1 后台执行体：调 PortfolioService.run 并产出摘要。"""

    def runner(
        progress_cb: Callable[[dict[str, Any]], None],
        register_handle: Callable[[Any], None],
    ) -> dict[str, Any]:
        service = _require_portfolio_service(container)
        return service.run(job_id, req, progress_cb)

    return runner


@router.post("/run", summary="触发组合回测（P1）")
async def portfolio_run(request: Request, body: PortfolioRunRequest) -> Response:
    """触发组合回测，返回 Job 信封（后台执行，不阻塞）。"""
    container = _services(request)
    _require_portfolio_service(container)
    code = resolve_market(container.config, body.market)
    if len(body.codes) > MAX_CODES:
        raise MissingKeyError(
            f"[fail-loud/NF-26] 组合回测标的池最多 {MAX_CODES} 只，"
            f"实际 {len(body.codes)}"
        )
    codes = tuple(dict.fromkeys(c.strip() for c in body.codes if c.strip()))
    if not codes:
        raise MissingKeyError("[fail-loud/NF-26] 组合回测标的池代码数组为空")
    req = PortfolioDomainRequest(
        market=code,
        codes=codes,
        strategy=body.strategy,
        params=dict(body.params),
        start=body.start,
        end=body.end,
        cash=body.cash,
        commission=body.commission,
        min_commission=body.min_commission,
        stamp_tax=body.stamp_tax,
        slippage=body.slippage,
        execution=body.execution,
    )
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    runner = _make_portfolio_runner(container, job_id, req)
    params = {
        "market": code,
        "codes": list(codes),
        "strategy": body.strategy,
        "params": dict(body.params),
        "start": body.start.isoformat(),
        "end": body.end.isoformat(),
        "cash": body.cash,
        "execution": body.execution,
    }
    with Timer() as timer:
        job = container.jobs.submit(
            "backtest", "portfolio", code, params, runner, job_id=job_id
        )
    return respond(job, code, elapsed_ms=timer.elapsed_ms)


@router.get("/jobs/{job_id}", summary="组合回测进度（P2）")
async def portfolio_job(request: Request, job_id: str) -> Response:
    """轮询组合回测进度（done 时 result_summary 含组合绩效摘要）。"""
    container = _services(request)
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"portfolio job 不存在: {job_id}"
        )
    return respond(job, job["market"])


@router.get("/results/{job_id}", summary="组合回测完整结果（P3）")
async def portfolio_result(request: Request, job_id: str) -> Response:
    """读取完整组合结果（PortfolioResult）。

    - job 不存在 → 404；
    - job 存在但结果未落库（如尚未 done）→ 404（显式，不静默空结果）。
    """
    container = _services(request)
    service = _require_portfolio_service(container)
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"portfolio job 不存在: {job_id}"
        )
    with Timer() as timer:
        result = service.get_result(job_id)
    if result is None:
        raise StarletteHTTPException(
            status_code=404,
            detail=f"portfolio 结果未就绪: {job_id}（job 状态 {job['status']}）",
        )
    return respond(result, job["market"], elapsed_ms=timer.elapsed_ms)
