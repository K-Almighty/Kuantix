"""optimize 路由（契约 §2.1e O1–O3，v1.3 增量 P1，前缀 ``/api/v1/optimize``）。

端点：
- O1 ``POST /run`` —— 触发单策略参数网格寻优（单标的 × 1-2 参数）→ Job 信封
  （module=``backtest``，action=``optimize``）；网格 >200 → **400**（fail-loud，
  后端二次校验，不依赖前端预校验）；
- O2 ``GET  /jobs/{job_id}`` —— 寻优进度（与 B3 同逻辑，薄转发）；
- O3 ``GET  /results/{job_id}`` —— 完整寻优结果（OptimizeResult：
  results/best/heatmap）；
- O4 ``POST /all/run`` —— 一键寻优所有策略（单标的 × 全策略预设网格）→ Job
  信封（action=``optimize-all``）；
- O5 ``GET  /all/results/{job_id}`` —— 全局策略排名（ranking/best/per_strategy）。

红线遵循
--------
- R2：寻优引擎调用收敛在 :class:`~Kuantix.adapters.backtest_bridge.BacktestBridge`
  （本模块不直接 import easy_tdx）；
- R5：路径不含 order/trade/buy/sell；
- R6：代码→交易所映射由 :class:`OptimizeService` 经 MarketProfile 处理。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import Response

from Kuantix.api.deps import ServiceContainer, get_services, resolve_market, respond
from Kuantix.api.schemas import OptimizeAllRunRequest, OptimizeRunRequest
from Kuantix.backtest.optimize_service import (
    MAX_GRID_PARAMS,
    MAX_GRID_POINTS,
    OptimizeAllRunRequest as OptimizeAllDomainRequest,
    OptimizeRunRequest as OptimizeDomainRequest,
    grid_size,
)
from Kuantix.core.envelope import Timer
from Kuantix.core.fail_loud import MissingKeyError

__all__ = ["router"]

router = APIRouter()


def _services(request: Request) -> ServiceContainer:
    return get_services(request)


def _require_optimize_service(container: ServiceContainer) -> Any:
    """取寻优服务；未装配时显式 400（fail-loud，不静默）。"""
    if container.optimize_service is None:
        raise MissingKeyError(
            "[fail-loud/NF-26] 寻优服务未装配（组合根缺失 optimize_service）"
        )
    return container.optimize_service


def _make_optimize_runner(
    container: ServiceContainer,
    job_id: str,
    req: OptimizeDomainRequest,
) -> Callable[[Callable[[dict[str, Any]], None], Callable[[Any], None]], dict[str, Any]]:
    """构造 O1 后台执行体：调 OptimizeService.run 并产出摘要。"""

    def runner(
        progress_cb: Callable[[dict[str, Any]], None],
        register_handle: Callable[[Any], None],
    ) -> dict[str, Any]:
        service = _require_optimize_service(container)
        return service.run(job_id, req, progress_cb)

    return runner


def _make_optimize_all_runner(
    container: ServiceContainer,
    job_id: str,
    req: OptimizeAllDomainRequest,
) -> Callable[[Callable[[dict[str, Any]], None], Callable[[Any], None]], dict[str, Any]]:
    """构造 O4 后台执行体：调 OptimizeService.run_all 并产出摘要。"""

    def runner(
        progress_cb: Callable[[dict[str, Any]], None],
        register_handle: Callable[[Any], None],
    ) -> dict[str, Any]:
        service = _require_optimize_service(container)
        return service.run_all(job_id, req, progress_cb)

    return runner


@router.post("/run", summary="触发参数网格寻优（O1）")
async def optimize_run(request: Request, body: OptimizeRunRequest) -> Response:
    """触发单策略参数网格寻优（单标的 × 1-2 参数），返回 Job 信封。"""
    container = _services(request)
    _require_optimize_service(container)
    code = resolve_market(container.config, body.market)
    code_str = body.code.strip()
    if not code_str:
        raise MissingKeyError("[fail-loud/NF-26] 寻优 code 为空")
    if not isinstance(body.param_grid, dict) or not body.param_grid:
        raise MissingKeyError("[fail-loud/NF-26] 寻优 param_grid 为空")
    if len(body.param_grid) > MAX_GRID_PARAMS:
        raise MissingKeyError(
            f"[fail-loud/NF-26] 寻优最多 {MAX_GRID_PARAMS} 个参数，"
            f"实际 {len(body.param_grid)} 个"
        )
    for name, values in body.param_grid.items():
        if not isinstance(values, (list, tuple)) or len(values) == 0:
            raise MissingKeyError(
                f"[fail-loud/NF-26] 寻优参数 {name!r} 的取值列表为空"
            )
    size = grid_size(body.param_grid)
    if size > MAX_GRID_POINTS:
        raise MissingKeyError(
            f"[fail-loud/NF-26] 网格大小 {size} 超过上限 {MAX_GRID_POINTS}"
            f"（笛卡尔积 {list(body.param_grid.keys())}），拒绝组合爆炸"
        )
    req = OptimizeDomainRequest(
        market=code,
        code=code_str,
        strategy=body.strategy,
        param_grid={str(k): list(v) for k, v in body.param_grid.items()},
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
    runner = _make_optimize_runner(container, job_id, req)
    params = {
        "market": code,
        "code": code_str,
        "strategy": body.strategy,
        "param_grid": {str(k): list(v) for k, v in body.param_grid.items()},
        "start": body.start.isoformat(),
        "end": body.end.isoformat(),
        "cash": body.cash,
        "execution": body.execution,
    }
    with Timer() as timer:
        job = container.jobs.submit(
            "backtest", "optimize", code, params, runner, job_id=job_id
        )
    return respond(job, code, elapsed_ms=timer.elapsed_ms)


@router.post("/all/run", summary="一键寻优所有策略（O4）")
async def optimize_all_run(
    request: Request, body: OptimizeAllRunRequest
) -> Response:
    """一键寻优所有已注册策略的预设网格（单标的），汇总全局策略排名。

    对标 easy_tdx ``/backtest/optimize-all``：逐策略用预设网格寻优，取各
    策略最优点，按 total_return 降序汇总成 ``ranking/best/per_strategy``。
    """
    container = _services(request)
    _require_optimize_service(container)
    code = resolve_market(container.config, body.market)
    code_str = body.code.strip()
    if not code_str:
        raise MissingKeyError("[fail-loud/NF-26] 寻优 code 为空")
    req = OptimizeAllDomainRequest(
        market=code,
        code=code_str,
        start=body.start,
        end=body.end,
        cash=body.cash,
        commission=body.commission,
        min_commission=body.min_commission,
        stamp_tax=body.stamp_tax,
        slippage=body.slippage,
        execution=body.execution,
        workers=body.workers,
    )
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    runner = _make_optimize_all_runner(container, job_id, req)
    params = {
        "market": code,
        "code": code_str,
        "strategy": "all",
        "start": body.start.isoformat(),
        "end": body.end.isoformat(),
        "cash": body.cash,
        "execution": body.execution,
        "workers": body.workers,
    }
    with Timer() as timer:
        job = container.jobs.submit(
            "backtest", "optimize-all", code, params, runner, job_id=job_id
        )
    return respond(job, code, elapsed_ms=timer.elapsed_ms)


@router.get("/jobs/{job_id}", summary="寻优进度（O2）")
async def optimize_job(request: Request, job_id: str) -> Response:
    """轮询寻优进度（done 时 result_summary 含 best 摘要）。"""
    container = _services(request)
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"optimize job 不存在: {job_id}"
        )
    return respond(job, job["market"])


@router.get("/results/{job_id}", summary="寻优完整结果（O3）")
async def optimize_result(request: Request, job_id: str) -> Response:
    """读取完整寻优结果（OptimizeResult）。

    - job 不存在 → 404；
    - job 存在但结果未落库（如尚未 done）→ 404（显式，不静默空结果）。
    """
    container = _services(request)
    service = _require_optimize_service(container)
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"optimize job 不存在: {job_id}"
        )
    with Timer() as timer:
        result = service.get_result(job_id)
    if result is None:
        raise StarletteHTTPException(
            status_code=404,
            detail=f"optimize 结果未就绪: {job_id}（job 状态 {job['status']}）",
        )
    return respond(result, job["market"], elapsed_ms=timer.elapsed_ms)


@router.get("/all/results/{job_id}", summary="一键寻优所有策略结果（O5）")
async def optimize_all_result(request: Request, job_id: str) -> Response:
    """读取一键寻优所有策略的完整结果（全局策略排名）。

    - job 不存在 → 404；
    - job 存在但结果未落库（未 done）→ 404（显式，不静默空结果）。
    """
    container = _services(request)
    service = _require_optimize_service(container)
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"optimize job 不存在: {job_id}"
        )
    with Timer() as timer:
        result = service.get_result(job_id)
    if result is None:
        raise StarletteHTTPException(
            status_code=404,
            detail=f"optimize-all 结果未就绪: {job_id}（job 状态 {job['status']}）",
        )
    return respond(result, job["market"], elapsed_ms=timer.elapsed_ms)


@router.delete("/jobs/{job_id}", summary="删除单个策略寻优（O6）")
async def delete_optimize_job(request: Request, job_id: str) -> Response:
    """删除单个策略寻优任务及其完整结果（jobs + backtest_results）。

    - job 与结果均不存在 → 404；
    - 幂等：已删除的 job 重复删除返回 404。
    """
    container = _services(request)
    service = _require_optimize_service(container)
    with Timer() as timer:
        job_existed = container.jobs.delete_job(job_id)
        result_existed = service.delete_result(job_id)
    if not job_existed and not result_existed:
        raise StarletteHTTPException(
            status_code=404, detail=f"optimize job 不存在: {job_id}"
        )
    return respond(
        {
            "job_id": job_id,
            "deleted_job": job_existed,
            "deleted_result": result_existed,
        },
        request.query_params.get("market", "Unknown"),
        elapsed_ms=timer.elapsed_ms,
    )
