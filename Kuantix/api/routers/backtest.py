"""backtest 路由（契约 §3.6 B1–B4，v1.2 增量，前缀 ``/api/v1/backtest``）。

端点：
- B1 ``GET  /strategies`` —— 上游预置策略列表（name/label/description/params
  schema，供前端策略下拉 + 参数表单动态渲染）；
- B2 ``POST /run`` —— 触发回测（Job 信封，后台执行；标的池/时间/策略/
  资金/费用）；
- B3 ``GET  /jobs/{job_id}`` —— 回测进度（done 时 result_summary 含
  绩效摘要）；
- B4 ``GET  /results/{job_id}`` —— 完整结果（净值序列/回撤/绩效指标/
  交易明细 + 组合视图）。

红线遵循
--------
- R2：回测引擎调用收敛在 :class:`~Kuantix.adapters.backtest_bridge.BacktestBridge`
  （本模块不直接 import easy_tdx）；
- R5：路径不含 order/trade/buy/sell（回测是模拟撮合，不暴露任何下单端点）；
- R6：市场规则（代码→交易所）由 :class:`BacktestService` 经 MarketProfile 处理。
"""
from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import Response

from Kuantix.api.deps import ServiceContainer, get_services, resolve_market, respond
from Kuantix.api.jobs import JobStatus
from Kuantix.api.schemas import BacktestRunRequest
from Kuantix.backtest.service import BacktestRunRequest as BacktestDomainRequest
from Kuantix.core.envelope import Timer
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    MissingKeyError,
    require_key,
)

__all__ = ["router"]

router = APIRouter()

#: B2 标的池数量上限（与 BacktestRunRequest.codes max_length 对齐）
MAX_CODES = 20


def _services(request: Request) -> ServiceContainer:
    return get_services(request)


def _make_backtest_runner(
    container: ServiceContainer,
    job_id: str,
    req: BacktestDomainRequest,
) -> Callable[[Callable[[dict[str, Any]], None], Callable[[Any], None]], dict[str, Any]]:
    """构造 B2 后台执行体：调 BacktestService.run 并产出摘要。"""

    def runner(
        progress_cb: Callable[[dict[str, Any]], None],
        register_handle: Callable[[Any], None],
    ) -> dict[str, Any]:
        outcome = container.backtest_service.run(job_id, req, progress_cb)
        return outcome.summary

    return runner


@router.get("/strategies", summary="回测策略列表（B1）")
async def backtest_strategies(request: Request) -> Response:
    """枚举上游全部预置策略（含参数 schema）。"""
    container = _services(request)
    if container.backtest_service is None:
        raise MissingKeyError(
            "[fail-loud/NF-26] 回测服务未装配（组合根缺失 backtest_service）"
        )
    with Timer() as timer:
        items = container.backtest_service.list_strategies()
    payload = {"items": items, "count": len(items)}
    return respond(payload, "CN", elapsed_ms=timer.elapsed_ms)


@router.post("/run", summary="触发回测（B2）")
async def backtest_run(request: Request, body: BacktestRunRequest) -> Response:
    """触发回测，返回 Job 信封（后台执行，不阻塞）。"""
    container = _services(request)
    if container.backtest_service is None:
        raise MissingKeyError(
            "[fail-loud/NF-26] 回测服务未装配（组合根缺失 backtest_service）"
        )
    code = resolve_market(container.config, body.market)
    if len(body.codes) > MAX_CODES:
        raise MissingKeyError(
            f"[fail-loud/NF-26] 回测标的池最多 {MAX_CODES} 只，实际 {len(body.codes)}"
        )
    codes = tuple(dict.fromkeys(c.strip() for c in body.codes if c.strip()))
    if not codes:
        raise MissingKeyError("[fail-loud/NF-26] 回测标的池代码数组为空")
    # D-3：live 仅限单标的（多标的 → 422 显式拒绝，NF-26 fail-loud）。
    # 组合/多策略/寻优（D-8）本期不支持 live —— 其请求体无 data_source 字段，
    # 天然保持本地语义，无需在此重复拦截。
    if body.data_source == "live" and len(codes) > 1:
        raise DataIntegrityError(
            f"[fail-loud/NF-26] data_source=live 仅支持单标的回测"
            f"（收到 {len(codes)} 只：{sorted(codes)}）。"
            f"多标的/组合/多策略请用 local/auto（本地数据湖）"
        )
    req = BacktestDomainRequest(
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
        data_source=body.data_source,
    )
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    runner = _make_backtest_runner(container, job_id, req)
    params = {
        "market": code,
        "codes": list(codes),
        "strategy": body.strategy,
        "params": dict(body.params),
        "start": body.start.isoformat(),
        "end": body.end.isoformat(),
        "cash": body.cash,
        "execution": body.execution,
        "data_source": body.data_source,
    }
    with Timer() as timer:
        job = container.jobs.submit(
            "backtest", "run", code, params, runner, job_id=job_id
        )
    return respond(job, code, elapsed_ms=timer.elapsed_ms)


@router.get("/jobs/{job_id}", summary="回测进度（B3）")
async def backtest_job(request: Request, job_id: str) -> Response:
    """轮询回测进度（done 时 result_summary 含绩效摘要）。"""
    container = _services(request)
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"backtest job 不存在: {job_id}"
        )
    return respond(job, job["market"])


@router.get("/results/{job_id}", summary="回测完整结果（B4）")
async def backtest_result(request: Request, job_id: str) -> Response:
    """读取完整回测结果（净值序列/回撤/绩效指标/交易明细 + 组合视图）。

    - job 不存在 → 404；
    - job 存在但结果未落库（如尚未 done）→ 404（显式，不静默空结果）。
    """
    container = _services(request)
    if container.backtest_service is None:
        raise MissingKeyError(
            "[fail-loud/NF-26] 回测服务未装配（组合根缺失 backtest_service）"
        )
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"backtest job 不存在: {job_id}"
        )
    with Timer() as timer:
        result = container.backtest_service.get_result(job_id)
    if result is None:
        raise StarletteHTTPException(
            status_code=404,
            detail=f"backtest 结果未就绪: {job_id}（job 状态 {job['status']}）",
        )
    return respond(result, job["market"], elapsed_ms=timer.elapsed_ms)


@router.get("/jobs", summary="回测任务列表（C1）")
async def backtest_jobs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50, description="返回条数上限 1..50")] = 20,
    status: Annotated[
        str | None,
        Query(description="状态过滤（queued/running/done/failed/cancelled，可空=全部）"),
    ] = None,
    module: Annotated[
        str | None, Query(description="模块过滤（默认 backtest）")
    ] = "backtest",
) -> Response:
    """回测任务列表（Compare 页，契约 §2.1b C1）。

    - 默认返回**全部 status**（前端过滤 done 对齐上游 fetchTaskList→filter，
      决策 D-7-A）；``status`` 参数仍提供；
    - ``limit`` 越界 / ``status`` 非法 → 400（fail-loud，JobStore.list 校验）。
    """
    container = _services(request)
    if status is not None and status not in JobStatus:
        raise MissingKeyError(
            f"[fail-loud/NF-26] jobs.list.status 非法: {status!r}"
            f"（允许 {list(JobStatus)}）"
        )
    with Timer() as timer:
        items = container.jobs.list_jobs(
            module=module, limit=limit, status=status
        )
    payload = {"items": items, "count": len(items)}
    return respond(
        payload, container.config.markets.default, elapsed_ms=timer.elapsed_ms
    )


@router.get("/kline/{code}", summary="单标的 K 线 + 买卖点标注（B5）")
async def backtest_kline(
    request: Request,
    code: str,
    market: Annotated[str, Query(description="市场码")] = "CN",
    start: Annotated[dt.date | None, Query(description="起始日期 YYYY-MM-DD")] = None,
    end: Annotated[dt.date | None, Query(description="结束日期 YYYY-MM-DD")] = None,
    strategy: Annotated[
        str, Query(description="策略名（默认 ma_cross，用于买卖点标注）")
    ] = "ma_cross",
    data_source: Annotated[
        Literal["auto", "local", "live"],
        Query(description="数据源 auto/local/live（默认 auto，v1.4）"),
    ] = "auto",
) -> Response:
    """单标的 K 线 + 策略买卖点标注（契约 §2.1b B5，v1.3 增量 P1，v1.4 增 data_source）。

    - K 线经与 B2 同一数据源分支（auto/local/live，保证下钻图与回测口径一致）；
    - 买卖点是**信号标注**（``buy_points``/``sell_points`` 为 ``{date, price}``
      数组，供 K 线图叠加；数据结构非下单动作，R5）；
    - code 非法 → 400；data_source 非法 → 400（Literal 校验）；无数据 → 404
      （显式）；market=HK → 501。
    """
    container = _services(request)
    if container.backtest_service is None:
        raise MissingKeyError(
            "[fail-loud/NF-26] 回测服务未装配（组合根缺失 backtest_service）"
        )
    code_resolved = resolve_market(container.config, market)
    code_str = code.strip()
    if not code_str.isdigit() or len(code_str) != 6:
        raise MissingKeyError(
            f"[fail-loud/NF-26] K 线代码格式非法: {code!r}（期望 6 位数字）"
        )
    strategy_name = strategy.strip()
    if not strategy_name:
        raise MissingKeyError("[fail-loud/NF-26] K 线 strategy 为空")
    start_date = start or dt.date(2020, 1, 1)
    end_date = end or dt.date(2025, 12, 31)
    if start_date > end_date:
        raise MissingKeyError(
            f"[fail-loud/NF-26] K 线区间非法: start {start_date} > end {end_date}"
        )
    with Timer() as timer:
        try:
            payload = container.backtest_service.get_kline_with_signals(
                code_str, code_resolved, start_date, end_date, strategy_name,
                data_source=data_source,
            )
        except DataIntegrityError as exc:
            raise StarletteHTTPException(
                status_code=404,
                detail=f"无 K 线数据: {code_str}（{exc}）",
            ) from exc
    return respond(payload, code_resolved, elapsed_ms=timer.elapsed_ms)
