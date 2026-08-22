"""data 路由（契约 §2.1 D1–D7 + v1.2 增量 D8，前缀 ``/api/v1/data``）。

端点：
- D1 ``GET  /status`` —— 数据湖状态（覆盖 + 最新 job + 隔离区计数）；
- D2 ``POST /sync`` —— 触发回补（Job 信封，后台执行）；
- D3 ``GET  /sync/{job_id}`` —— 回补进度；
- D4 ``POST /sync/{job_id}/cancel`` —— 取消回补；
- D5 ``GET  /verify`` —— 完整性校验（NF-27 隔离区可见）；
- D6 ``GET  /quarantine`` —— 隔离区清单（分页）；
- D7 ``DELETE /quarantine/{code}`` —— 移除隔离区条目；
- D8 ``GET  /search`` —— 证券搜索（v1.2 增量：代码/名称，本地清单缓存）。

全部走统一信封（NF-9）；市场未启用 → 501（NF-7）；job 不存在 → 404；
交易时段内未确认的全量回补 → 422（NF-28）。
"""
from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import Response

from Kuantix.api.deps import (
    ServiceContainer,
    db_page,
    get_services,
    page_limits,
    paginate,
    resolve_market,
    respond,
)
from Kuantix.api.jobs import JobCancelledError
from Kuantix.api.schemas import SyncRequest
from Kuantix.core.envelope import Timer
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    FailLoudError,
    NotSupportedError,
)
from Kuantix.core.market import get_market_profile
from Kuantix.data.sync_state import SyncStateStore

__all__ = ["router"]

#: D2 数据回补的兜底总超时（秒）。行情服务器不可达时，easy_tdx 会逐个标的
#: 在内部连接/心跳超时，导致 sync 长时间无进展（前端表现为「点击没反应」）。
#: 超过该时限仍未完成即标记失败并给出网络提示，避免 Job 无限 running。
_SYNC_TIMEOUT_SECONDS = 600

router = APIRouter()


def _services(request: Request) -> ServiceContainer:
    """从应用状态取组合根（首个业务请求时惰性装配）。"""
    return get_services(request)


def _sync_runner(
    container: ServiceContainer,
    market: str,
    mode: str,
    years: int,
    workers: int | None,
    force: bool,
) -> Callable[[Callable[[dict[str, Any]], None], Callable[[Any], None]], dict[str, Any]]:
    """构造 D2 后台执行体：桥接 SyncHandle 的进度到 Job。"""

    def runner(
        progress_cb: Callable[[dict[str, Any]], None],
        register_handle: Callable[[Any], None],
    ) -> dict[str, Any]:
        lake = container.lake
        if mode == "full":
            handle = lake.sync_full(market, years, workers=workers, force=force)
        else:
            handle = lake.sync_incremental(market, workers=workers, force=force)
        register_handle(handle)
        # 总超时保护：行情服务器不可达时，easy_tdx 逐个标的在内部连接/心跳
        # 超时（本环境 7709 全超时），handle 可能长时间不 done 导致 Job 无限
        # running「没反应」。这里加一个兜底超时，超时即标记失败并给出明确
        # 网络提示（fail-loud/NF-27：不静默卡死）。
        sync_deadline = time.monotonic() + _SYNC_TIMEOUT_SECONDS
        last_progress: dict[str, Any] | None = None
        while not handle.is_done():
            if handle.progress is not None:
                last_progress = handle.progress.to_dict()
                progress_cb(last_progress)
            if time.monotonic() > sync_deadline:
                raise FailLoudError(
                    "[fail-loud/NF-27] 数据回补超过时限仍未完成，疑似行情服务器"
                    "连接不可达或过慢。请检查网络/防火墙后重试"
                )
            time.sleep(0.2)
        if handle.progress is not None:
            progress_cb(handle.progress.to_dict())
        if handle.status == "cancelled":
            raise JobCancelledError()
        if handle.status == "failed":
            raise FailLoudError(f"[fail-loud] 数据回补失败: {handle.error}")
        result = handle.result
        # 设计二 D2.6：D2 手动同步成功后写 sync_state（trigger=manual），
        # 保证 last_sync 是「任意来源最后一次同步」。
        SyncStateStore(container.config.paths.db).update(
            at=dt.datetime.now().astimezone(),
            status="done",
            trigger="manual",
            result={
                "total": result.total,
                "done": result.done,
                "failed": result.failed,
                "quarantined": result.quarantined,
                "skipped_resumed": result.skipped_resumed,
                "elapsed_ms": result.elapsed_ms,
            },
        )
        return {
            "status": handle.status,
            "market": market,
            "total": result.total,
            "done": result.done,
            "failed": result.failed,
            "quarantined": result.quarantined,
            "skipped_resumed": result.skipped_resumed,
            "elapsed_ms": result.elapsed_ms,
        }

    return runner


@router.get("/status", summary="数据湖状态（D1）")
async def data_status(request: Request, market: str = "CN") -> Response:
    """返回覆盖统计 + 最近 sync job + 隔离区计数。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        payload = container.lake.status(code)
        payload["latest_job"] = container.jobs.latest("data", code)
    return respond(
        payload, code, data_date=payload.get("data_date"), elapsed_ms=timer.elapsed_ms
    )


@router.post("/sync", summary="触发数据回补（D2）")
async def data_sync(request: Request, body: SyncRequest) -> Response:
    """触发全量/增量回补，返回 Job 信封（后台执行，不阻塞）。"""
    container = _services(request)
    code = resolve_market(container.config, body.market)
    if body.mode == "full" and not body.force:
        profile = get_market_profile(code)
        if profile.is_open_now():
            raise DataIntegrityError(
                f"[fail-loud/NF-28] 交易时段内禁止全量回补（{code} 正在撮合）。"
                f"请设置 force=true 显式确认"
            )
    action = "sync_full" if body.mode == "full" else "sync_incremental"
    runner = _sync_runner(container, code, body.mode, body.years, body.workers, body.force)
    params = {
        "mode": body.mode,
        "market": code,
        "years": body.years,
        "workers": body.workers,
        "force": body.force,
    }
    with Timer() as timer:
        job = container.jobs.submit("data", action, code, params, runner)
    return respond(job, code, elapsed_ms=timer.elapsed_ms)


@router.get("/sync/{job_id}", summary="数据回补进度（D3）")
async def data_sync_job(request: Request, job_id: str) -> Response:
    """轮询回补进度（1–2s 间隔，契约 §1.9）。"""
    container = _services(request)
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(status_code=404, detail=f"sync job 不存在: {job_id}")
    return respond(job, job["market"])


@router.post("/sync/{job_id}/cancel", summary="取消数据回补（D4）")
async def data_sync_cancel(request: Request, job_id: str) -> Response:
    """取消回补任务；已结束任务 → 422。"""
    container = _services(request)
    with Timer() as timer:
        job = container.jobs.cancel(job_id)
    if job is None:
        raise StarletteHTTPException(status_code=404, detail=f"sync job 不存在: {job_id}")
    return respond(job, job["market"], elapsed_ms=timer.elapsed_ms)


@router.get("/verify", summary="数据完整性校验（D5）")
async def data_verify(request: Request, market: str = "CN") -> Response:
    """完整性校验：回读比对 + 缺失交易日 + 隔离区清单（NF-27）。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        payload = container.lake.verify_payload(code)
    coverage = payload["coverage"]
    return respond(
        payload, code, data_date=coverage.get("last_date"), elapsed_ms=timer.elapsed_ms
    )


@router.get("/quarantine", summary="隔离区清单（D6）")
async def data_quarantine(
    request: Request,
    market: str = "CN",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Response:
    """分页列出隔离区条目（NF-27）。P1-2：LIMIT/OFFSET 下推到 SQLite。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        limit, offset = page_limits(page, page_size)
        total = container.lake.count_quarantine(code)
        entries = container.lake.list_quarantine(code, limit=limit, offset=offset)
        items = [entry.to_dict() for entry in entries]
        payload = db_page(items, total, page, page_size)
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.delete("/quarantine/{code}", summary="移除隔离区条目（D7）")
async def data_quarantine_remove(
    request: Request, code: str, market: str = "CN"
) -> Response:
    """按代码移除隔离区条目；不存在 → 404。"""
    container = _services(request)
    market_code = resolve_market(container.config, market)
    with Timer() as timer:
        entries = container.lake.list_quarantine(market_code)
        entry = None
        for candidate in entries:
            if candidate.code == code:
                entry = candidate
                break
        if entry is None:
            raise StarletteHTTPException(
                status_code=404, detail=f"隔离区条目不存在: {code}"
            )
        container.lake.remove_quarantine(code, market_code)
    payload = {"removed": code, "reason": entry.reason}
    return respond(payload, market_code, elapsed_ms=timer.elapsed_ms)


@router.get("/search", summary="证券搜索（D8，v1.2 增量）")
async def data_search(
    request: Request,
    q: str = Query(..., description="搜索关键词：证券代码（精确/前缀）或名称（模糊）"),
    market: str = "CN",
    limit: int = Query(default=20, ge=1, le=50, description="返回条数上限（1..50）"),
) -> Response:
    """按代码/名称搜索证券，返回基本信息（供前端下拉确认选择）。

    - ``q`` 为空 → 400（fail-loud）；
    - 无匹配 → 显式空数组（合法态）；
    - 清单源不可用（缓存缺失且枚举失败）→ 422。
    """
    container = _services(request)
    code = resolve_market(container.config, market)
    if container.security_search is None:
        raise NotSupportedError(
            "[fail-loud/NF-26] 证券搜索服务未装配（组合根缺失 security_search）"
        )
    with Timer() as timer:
        hits = container.security_search.search(q, code, limit=limit)
    payload = {
        "items": [hit.to_dict() for hit in hits],
        "count": len(hits),
    }
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)
