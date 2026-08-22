"""盘前分析 / 盘后复盘路由（前缀 /api/v1/analysis）。

端点
----
- ``GET /pre-open/report``：获取盘前报告（可 ``&export=json|md`` 下载）；
- ``POST /pre-open/run``：手动重算盘前报告；
- ``GET /pre-open/news``：分页消息面（NewsStore.list + db_page）；
- ``GET /pre-open/fundamentals``：分页基本面画像；
- ``POST /pre-open/fundamentals/run``：强制重算若干标的基本面画像。

- ``GET /post-close/report``：获取盘后报告（可 export）；
- ``POST /post-close/run``：手动重算盘后报告（可选 force=True 跳过等待）；
- ``GET /post-close/limit-up-down``：分页涨跌停条目 + summary；
- ``GET /post-close/technical``：复用 PreOpenService.scan_technical 扫
  描今日技术面（codes 或默认自选 + sample）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from Kuantix.api.deps import (
    MAX_PAGE_SIZE,
    ServiceContainer,
    db_page,
    get_services_from_app,
    page_limits,
    resolve_market,
    respond,
)
from Kuantix.core.envelope import Timer
from Kuantix.core.fail_loud import MissingKeyError

from Kuantix.analysis.report import report_json_dict, report_markdown

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()


ExportFmt = Literal["json", "md"]


def _services(request: Request) -> ServiceContainer:
    return get_services_from_app(request.app)


def _parse_date(date_s: str | None, context: str) -> dt.date:
    if not date_s:
        return dt.date.today()
    text = str(date_s).strip()
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise MissingKeyError(
            f"[fail-loud/NF-26] {context} date 参数非法: {date_s!r}（期望 YYYY-MM-DD）"
        ) from exc


def _parse_codes_csv(codes_s: str | None) -> list[str]:
    if not codes_s:
        return []
    return [c.strip() for c in str(codes_s).split(",") if c.strip()]


def _resolve_components(container: ServiceContainer) -> dict[str, Any]:
    """从 ServiceContainer 取 5 个 analysis 组件；缺装配 → fail-loud 501。"""
    required = (
        "pre_open_service",
        "post_close_service",
        "news_store",
        "fundamental_store",
        "limit_store",
    )
    out: dict[str, Any] = {}
    for key in required:
        value = getattr(container, key, None)
        if value is None:
            raise MissingKeyError(
                f"[fail-loud/NF-26] ServiceContainer 缺少 {key}，"
                "请在 create_app 时调用 build_analysis_components 装配分析模块。"
            )
        out[key] = value
    return out


def _attachment_response(
    body: str | bytes,
    *,
    filename: str,
    media_type: str,
) -> Response:
    """构造带 Content-Disposition 的下载响应（UTF-8 文件名）。"""
    content_disposition = f"attachment; filename*=UTF-8''{filename}"
    if isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = bytes(body)
    return Response(
        content=body_bytes,
        media_type=media_type,
        headers={"Content-Disposition": content_disposition},
    )


def _export_report(
    report: Any,
    *,
    export: ExportFmt | None,
    filename_prefix: str,
    date_str: str,
    market: str,
) -> Response | None:
    """若 export 非空 → 返回下载响应；否则 None 表示走正常信封响应。"""
    if export is None:
        return None
    if export == "json":
        payload = {"report": report_json_dict(report)}
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        return _attachment_response(
            body,
            filename=f"{filename_prefix}-{market.lower()}-{date_str}.json",
            media_type="application/json; charset=utf-8",
        )
    if export == "md":
        md = report_markdown(report)
        return _attachment_response(
            md,
            filename=f"{filename_prefix}-{market.lower()}-{date_str}.md",
            media_type="text/markdown; charset=utf-8",
        )
    raise MissingKeyError(
        f"[fail-loud/NF-26] export 仅支持 json|md，实际 {export!r}"
    )


# ===========================================================================
# 盘前
# ===========================================================================


@router.get("/pre-open/report", summary="获取盘前分析报告")
async def pre_open_report(
    request: Request,
    market: str = "CN",
    date: str | None = None,
    codes: str | None = None,
    export: Annotated[ExportFmt | None, Query()] = None,
) -> Response:
    container = _services(request)
    market_code = resolve_market(container.config, market)
    target_day = _parse_date(date, "pre-open/report")
    comps = _resolve_components(container)
    loop = asyncio.get_running_loop()
    with Timer() as timer:
        report = await loop.run_in_executor(
            None,
            lambda: comps["pre_open_service"].run_report(
                market_code,
                target_day,
                codes=_parse_codes_csv(codes) or None,
            ),
        )
    if export:
        rsp = _export_report(
            report,
            export=export,
            filename_prefix="pre-open-report",
            date_str=target_day.isoformat(),
            market=market_code,
        )
        if rsp is not None:
            return rsp
    return respond(report.to_dict(), market_code, elapsed_ms=timer.elapsed_ms)


@router.post("/pre-open/run", summary="手动重算盘前分析报告")
async def pre_open_run(
    request: Request,
    market: str = "CN",
    date: str | None = None,
) -> Response:
    container = _services(request)
    market_code = resolve_market(container.config, market)
    target_day = _parse_date(date, "pre-open/run")
    comps = _resolve_components(container)
    with Timer() as timer:
        report = comps["pre_open_service"].run_report(market_code, target_day)
    return respond(report.to_dict(), market_code, elapsed_ms=timer.elapsed_ms)


@router.get("/pre-open/news", summary="分页：盘前消息面列表")
async def pre_open_news(
    request: Request,
    market: str = "CN",
    date: str | None = None,
    category: str | None = None,
    keywords: Annotated[list[str] | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> Response:
    container = _services(request)
    market_code = resolve_market(container.config, market)
    target_day = _parse_date(date, "pre-open/news")
    comps = _resolve_components(container)
    with Timer() as timer:
        limit, offset = page_limits(page, page_size)
        store = comps["news_store"]
        total = store.count(
            market_code, target_day,
            category=category,
            keywords=keywords,
        )
        items = [
            i.to_dict()
            for i in store.list(
                market_code, target_day,
                category=category, keywords=keywords,
                limit=limit, offset=offset,
            )
        ]
        payload = db_page(items, total, page, page_size)
    return respond(payload, market_code, elapsed_ms=timer.elapsed_ms)


@router.get("/pre-open/fundamentals", summary="分页：盘前基本面画像列表")
async def pre_open_fundamentals(
    request: Request,
    market: str = "CN",
    date: str | None = None,
    codes: str | None = None,
    grade: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> Response:
    container = _services(request)
    market_code = resolve_market(container.config, market)
    target_day = _parse_date(date, "pre-open/fundamentals")
    code_list = _parse_codes_csv(codes) or None
    comps = _resolve_components(container)
    with Timer() as timer:
        limit, offset = page_limits(page, page_size)
        store = comps["fundamental_store"]
        total = store.count(
            market_code, target_day, codes=code_list, grade=grade,
        )
        items = [
            p.to_dict()
            for p in store.list(
                market_code, target_day,
                codes=code_list, grade=grade,
                limit=limit, offset=offset,
            )
        ]
        payload = db_page(items, total, page, page_size)
    return respond(payload, market_code, elapsed_ms=timer.elapsed_ms)


@router.post("/pre-open/fundamentals/run", summary="强制重算 N 只标的基本面画像")
async def pre_open_fundamentals_run(
    request: Request,
    market: str = "CN",
    date: str | None = None,
    codes: str = "",
) -> Response:
    container = _services(request)
    market_code = resolve_market(container.config, market)
    target_day = _parse_date(date, "pre-open/fundamentals/run")
    code_list = _parse_codes_csv(codes)
    if not code_list:
        raise MissingKeyError(
            "[fail-loud/NF-26] pre-open/fundamentals/run 需要 codes=a,b,c 指定标的"
        )
    comps = _resolve_components(container)
    with Timer() as timer:
        profiles = comps["pre_open_service"].build_fundamental_profiles(
            market_code, code_list, date=target_day,
        )
        payload = {
            "count": len(profiles),
            "items": [p.to_dict() for p in profiles],
        }
    return respond(payload, market_code, elapsed_ms=timer.elapsed_ms)


# ===========================================================================
# 盘后
# ===========================================================================


@router.get("/post-close/report", summary="获取盘后复盘报告")
async def post_close_report(
    request: Request,
    market: str = "CN",
    date: str | None = None,
    codes: str | None = None,
    export: Annotated[ExportFmt | None, Query()] = None,
) -> Response:
    container = _services(request)
    market_code = resolve_market(container.config, market)
    target_day = _parse_date(date, "post-close/report")
    comps = _resolve_components(container)
    # 全市场分析 + tdx 实时补全属 CPU/IO 密集同步重活；offload 到线程池，
    # 避免阻塞事件循环导致同进程其他请求（页面导航/健康检查）饿死。
    loop = asyncio.get_running_loop()
    with Timer() as timer:
        report = await loop.run_in_executor(
            None,
            lambda: comps["post_close_service"].run_report(
                market_code,
                target_day,
                codes=_parse_codes_csv(codes) or None,
            ),
        )
    if export:
        rsp = _export_report(
            report,
            export=export,
            filename_prefix="post-close-report",
            date_str=target_day.isoformat(),
            market=market_code,
        )
        if rsp is not None:
            return rsp
    return respond(report.to_dict(), market_code, elapsed_ms=timer.elapsed_ms)


@router.post("/post-close/run", summary="手动重算盘后复盘报告")
async def post_close_run(
    request: Request,
    market: str = "CN",
    date: str | None = None,
    force: bool = False,
) -> Response:
    container = _services(request)
    market_code = resolve_market(container.config, market)
    target_day = _parse_date(date, "post-close/run")
    comps = _resolve_components(container)
    loop = asyncio.get_running_loop()
    with Timer() as timer:
        report = await loop.run_in_executor(
            None,
            lambda: comps["post_close_service"].run_report(
                market_code, target_day, force=force,
            ),
        )
    return respond(report.to_dict(), market_code, elapsed_ms=timer.elapsed_ms)


@router.get("/post-close/limit-up-down", summary="分页：盘后涨跌停条目 + 汇总")
async def post_close_limit(
    request: Request,
    market: str = "CN",
    date: str | None = None,
    limit_type: str | None = None,
    sector: str | None = None,
    only_up: str | None = None,  # 'true' | 'false' | None
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> Response:
    container = _services(request)
    market_code = resolve_market(container.config, market)
    target_day = _parse_date(date, "post-close/limit-up-down")
    comps = _resolve_components(container)
    only_up_flag: bool | None = None
    if only_up is not None:
        v = str(only_up).strip().lower()
        if v in {"1", "true", "yes", "y", "up"}:
            only_up_flag = True
        elif v in {"0", "false", "no", "n", "down"}:
            only_up_flag = False
        else:
            raise MissingKeyError(
                f"[fail-loud/NF-26] only_up 取值非法: {only_up!r}（期望 true/false）"
            )
    with Timer() as timer:
        store = comps["limit_store"]
        summary = store.get_summary(market_code, target_day)
        limit, offset = page_limits(page, page_size)
        total = store.count(
            market_code, target_day,
            limit_type=limit_type, sector=sector, only_up=only_up_flag,
        )
        items = [
            e.to_dict()
            for e in store.list(
                market_code, target_day,
                limit_type=limit_type, sector=sector, only_up=only_up_flag,
                limit=limit, offset=offset,
            )
        ]
        page_payload = db_page(items, total, page, page_size)
        payload = {
            "summary": None if summary is None else summary.to_dict(),
            "entries": page_payload,
        }
    return respond(payload, market_code, elapsed_ms=timer.elapsed_ms)


@router.get("/post-close/technical", summary="盘后技术扫描（复用盘前 scan_technical）")
async def post_close_technical(
    request: Request,
    market: str = "CN",
    codes: str | None = None,
    workers: Annotated[int | None, Query(ge=1, le=64)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> Response:
    container = _services(request)
    market_code = resolve_market(container.config, market)
    comps = _resolve_components(container)
    with Timer() as timer:
        code_list = _parse_codes_csv(codes)
        if code_list:
            results = comps["pre_open_service"].scan_technical(
                market_code, code_list, workers=workers,
            )
        else:
            watch, sample = comps["pre_open_service"]._expand_codes(  # noqa: SLF001
                market_code, None,
            )
            scan_codes = list(dict.fromkeys(watch + sample))
            results = comps["pre_open_service"].scan_technical(
                market_code, scan_codes, workers=workers,
            )
        # 分页：在已扫描的结果上 paginate（典型只看 Top，避免内存从 1000 条爆增）
        total = len(results)
        start = (page - 1) * page_size
        page_items = [r.to_dict() for r in results[start:start + page_size]]
        payload = db_page(page_items, total, page, page_size)
    return respond(payload, market_code, elapsed_ms=timer.elapsed_ms)
