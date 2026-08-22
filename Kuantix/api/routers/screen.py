"""screen 路由（契约 §2.3 S1–S6，前缀 ``/api/v1/screen``）。

端点：
- S1 ``GET  /filters`` —— 条件插件清单（技术/缠论，NF-2）；
- S2 ``POST /run`` —— 触发选股（Job 信封，后台执行）；
- S3 ``GET  /jobs/{job_id}`` —— 选股进度（done 时 result_summary 含
  ``{batch_id, result_count, excluded_count, as_of}``，v1.1 R1.1-1）；
- S4 ``GET  /batches`` —— 历史批次（分页）；
- S5 ``GET  /results`` —— 批次结果（分页 + 排序，ScreenResultView 含 rank）；
- S6 ``GET  /results/{batch_id}/export`` —— JSON 信封下载 / CSV GBK 文件
  （NF-22 免责头，非信封）。

CSV 导出：``text/csv; charset=gbk``（同花顺兼容），**不含信封包装**；
表头与 ScreenService 落盘格式一致：代码,名称,现价,评分,条件（NF-22）。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import Response

from Kuantix.api.deps import (
    ServiceContainer,
    get_services,
    parse_pool,
    resolve_market,
    respond,
)
from Kuantix.api.schemas import (
    PageModel,
    ScreenFactorRunRequest,
    ScreenResultViewModel,
    ScreenRunRequest,
)
from Kuantix.core.envelope import Timer
from Kuantix.core.fail_loud import MissingKeyError, require_key
from Kuantix.screen.service import ScreenRequest

__all__ = ["router"]

router = APIRouter()

#: 技术过滤条件键（与 ScreenFilter.tech_filter 的 allowed 一致）
_TECH_CONDITIONS: tuple[str, ...] = (
    "ma_fast",
    "ma_slow",
    "min_close",
    "max_close",
    "min_vol_ratio",
)
#: 缠论过滤条件键（与 ScreenFilter.chanlun_filter 的 allowed 一致）
_CHANLUN_CONDITIONS: tuple[str, ...] = ("require_buy_point",)

#: S1 条件插件元数据（展示名 / 描述 / 参数 Schema，供前端表单）
_TECH_FILTER_META: dict[str, dict[str, Any]] = {
    "ma_fast": {
        "display_name": "快线上穿慢线（多头）",
        "description": "快周期均线高于慢周期均线（多头排列）",
        "params_schema": {
            "type": "object",
            "properties": {"fast": {"type": "integer"}, "slow": {"type": "integer"}},
        },
    },
    "ma_slow": {
        "display_name": "慢线支撑",
        "description": "收盘价高于慢周期均线",
        "params_schema": {"type": "object", "properties": {"slow": {"type": "integer"}}},
    },
    "min_close": {
        "display_name": "最低收盘价",
        "description": "最新收盘价不低于阈值",
        "params_schema": {"type": "object", "properties": {"value": {"type": "number"}}},
    },
    "max_close": {
        "display_name": "最高收盘价",
        "description": "最新收盘价不高于阈值",
        "params_schema": {"type": "object", "properties": {"value": {"type": "number"}}},
    },
    "min_vol_ratio": {
        "display_name": "放量下限",
        "description": "当日量 / 20 日均量不低于阈值",
        "params_schema": {"type": "object", "properties": {"value": {"type": "number"}}},
    },
}
_CHANLUN_FILTER_META: dict[str, dict[str, Any]] = {
    "require_buy_point": {
        "display_name": "出现买点",
        "description": "最近 K 线出现缠论买点",
        "params_schema": {"type": "object", "properties": {}},
    },
}


def _services(request: Request) -> ServiceContainer:
    return get_services(request)


def _filter_items() -> list[dict[str, Any]]:
    """S1 载荷：把条件元数据展开成 FilterInfo 列表（契约 §3.4）。"""
    items: list[dict[str, Any]] = []
    for condition in _TECH_CONDITIONS:
        meta = require_key(_TECH_FILTER_META, condition, "tech 过滤条件元数据")
        items.append(
            {
                "type": "tech",
                "condition": condition,
                "display_name": require_key(meta, "display_name", f"tech {condition}"),
                "description": require_key(meta, "description", f"tech {condition}"),
                "params_schema": require_key(meta, "params_schema", f"tech {condition}"),
            }
        )
    for condition in _CHANLUN_CONDITIONS:
        meta = require_key(_CHANLUN_FILTER_META, condition, "chanlun 过滤条件元数据")
        items.append(
            {
                "type": "chanlun",
                "condition": condition,
                "display_name": require_key(meta, "display_name", f"chanlun {condition}"),
                "description": require_key(meta, "description", f"chanlun {condition}"),
                "params_schema": require_key(meta, "params_schema", f"chanlun {condition}"),
            }
        )
    return items


def _param_value(condition: str, params: dict[str, Any]) -> Any:
    """把 filter params 转成 ScreenFilter 条件值（契约 §3.4 的 params 对象）。

    兼容两种写法：``{"value": 10.0}`` 或 ``{condition: 10.0}``；
    - ``params`` 为空 dict → ``True``（布尔条件，如 require_buy_point）；
    - ``params`` 非空但缺少这两个取值键 → 显式抛 :class:`MissingKeyError`
      （NF-26：**禁止静默退回 True**，否则前端按 schema 填的参数会被丢弃）。
    """
    if not params:
        return True
    if "value" in params:
        return params["value"]
    if condition in params:
        return params[condition]
    raise MissingKeyError(
        f"[fail-loud/NF-26] 条件 {condition!r} 的 params 缺少取值键"
        f"（期望 \"value\" 或 {condition!r}，实际可用键: {sorted(params)}）"
    )


def _apply_tech(tech_cond: dict[str, Any], item: Any) -> None:
    """解析一个 tech 过滤器到 tech_cond。

    - ``ma_cross`` / ``ma_fast``：params 需含 ``{fast, slow}``，映射为
      ``ma_fast`` + ``ma_slow`` 两个后端条件（ScreenFilter.tech_filter
      需要两者同时在场才生效）；
    - ``ma_slow``：params 需含 ``slow``（单独一条时由 ScreenFilter 语义决定）；
    - 其余条件（min_close/max_close/min_vol_ratio）走 :func:`_param_value`。
    """
    condition = item.condition
    if condition == "ma_cross":
        tech_cond["ma_fast"] = int(require_key(item.params, "fast", "ma_cross 参数"))
        tech_cond["ma_slow"] = int(require_key(item.params, "slow", "ma_cross 参数"))
        return
    if condition == "ma_fast":
        tech_cond["ma_fast"] = int(require_key(item.params, "fast", "ma_fast 参数"))
        tech_cond["ma_slow"] = int(require_key(item.params, "slow", "ma_fast 参数"))
        return
    if condition == "ma_slow":
        tech_cond["ma_slow"] = int(require_key(item.params, "slow", "ma_slow 参数"))
        return
    if condition not in _TECH_CONDITIONS:
        raise MissingKeyError(
            f"[fail-loud/NF-26] tech 过滤条件非法: {condition!r}"
            f"（期望 {sorted(_TECH_CONDITIONS)}，或 ma_cross）"
        )
    tech_cond[condition] = _param_value(condition, item.params)


def _apply_chanlun(chanlun_cond: dict[str, Any], item: Any) -> None:
    """解析一个 chanlun 过滤器到 chanlun_cond。"""
    condition = item.condition
    if condition not in _CHANLUN_CONDITIONS:
        raise MissingKeyError(
            f"[fail-loud/NF-26] chanlun 过滤条件非法: {condition!r}"
            f"（期望 {sorted(_CHANLUN_CONDITIONS)}）"
        )
    chanlun_cond[condition] = _param_value(condition, item.params)


def _translate_filters(
    filters: list[Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把 ScreenRunRequest.filters 转成 ScreenRequest.tech_cond/chanlun_cond。"""
    tech_cond: dict[str, Any] = {}
    chanlun_cond: dict[str, Any] = {}
    for item in filters:
        if item.type == "tech":
            _apply_tech(tech_cond, item)
        elif item.type == "chanlun":
            _apply_chanlun(chanlun_cond, item)
        else:  # pragma: no cover - Pydantic Literal 已拦截
            raise MissingKeyError(f"[fail-loud/NF-26] 过滤器类型非法: {item.type!r}")
    return tech_cond, chanlun_cond


def _make_screen_runner(
    container: ServiceContainer,
    req: ScreenRequest,
    pool_codes: tuple[str, ...] | None,
    excluded: set[str],
    filters: list[Any],
    combine: str,
) -> Callable[[Callable[[dict[str, Any]], None], Callable[[Any], None]], dict[str, Any]]:
    """构造 S2 后台执行体：调用 ScreenService.run_batch 并产出 result_summary。"""

    def runner(
        progress_cb: Callable[[dict[str, Any]], None],
        register_handle: Callable[[Any], None],
    ) -> dict[str, Any]:
        outcome = container.screen_service.run_batch(
            req,
            pool_codes=pool_codes,
            excluded_codes=excluded,
            filters=filters,
            combine=combine,
        )
        return {
            "batch_id": outcome.batch_id,
            "result_count": outcome.result_count,
            "excluded_count": outcome.excluded_count,
            "as_of": outcome.as_of.isoformat(),
        }

    return runner


@router.get("/filters", summary="选股条件插件清单（S1）")
async def screen_filters(request: Request, market: str = "CN") -> Response:
    """列出技术/缠论条件插件（NF-2）。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        payload = {"items": _filter_items()}
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.post("/run", summary="触发选股（S2）")
async def screen_run(request: Request, body: ScreenRunRequest) -> Response:
    """触发选股，返回 Job 信封（后台执行）。"""
    container = _services(request)
    code = resolve_market(container.config, body.market)
    if body.model is not None:
        models = set(container.factor_service.list_models())
        if body.model not in models:
            raise StarletteHTTPException(status_code=404, detail=f"模型不存在: {body.model}")
    tech_cond, chanlun_cond = _translate_filters(body.filters)
    pool_codes = parse_pool(body.pool)
    entries = container.lake.list_quarantine(code)
    excluded = {str(e.code) for e in entries}
    filters_payload = [f.model_dump() for f in body.filters]
    req = ScreenRequest(
        market=code,
        model_name=body.model,
        top_n=body.top_n,
        tech_cond=tech_cond,
        chanlun_cond=chanlun_cond,
        as_of=body.as_of,
    )
    params = {
        "model": body.model,
        "market": code,
        "pool": body.pool,
        "top_n": body.top_n,
        "filters": filters_payload,
        "combine": body.combine,
        "as_of": body.as_of.isoformat() if body.as_of is not None else None,
    }
    runner = _make_screen_runner(
        container, req, pool_codes, excluded, filters_payload, body.combine
    )
    with Timer() as timer:
        job = container.jobs.submit("screen", "run", code, params, runner)
    return respond(job, code, elapsed_ms=timer.elapsed_ms)


@router.post("/factor-run", summary="单因子选股（同步，基于最新数据，非回测）")
async def screen_factor_run(request: Request, body: ScreenFactorRunRequest) -> Response:
    """单因子快速筛选：取一个因子的最新截面，按取值排序取 TopN。

    同步返回结果（非 Job），不加载全部因子、不做模型打分，速度远快于
    ``/run``。``days_back`` 与 ``as_of`` 二选一：``days_back`` 表示取
    ``基准日 - N 天`` 的数据（基准日为因子库最新日）。
    """
    container = _services(request)
    code = resolve_market(container.config, body.market)
    if not body.factor:
        raise StarletteHTTPException(status_code=422, detail="factor 不能为空")
    tech_cond, chanlun_cond = _translate_filters(body.filters)
    pool_codes = parse_pool(body.pool)
    excluded = {str(e.code) for e in container.lake.list_quarantine(code)}

    as_of = body.as_of
    if as_of is None and body.days_back is not None:
        as_of = date.today() - timedelta(days=body.days_back)

    with Timer() as timer:
        results, total, as_of_eff = container.screen_service.screen_factor(
            factor=body.factor,
            market=code,
            pool_codes=pool_codes,
            excluded_codes=excluded,
            top_n=body.top_n,
            order=body.order,
            as_of=as_of,
            tech_cond=tech_cond,
            chanlun_cond=chanlun_cond,
            combine=body.combine,
        )
        views = [ScreenResultViewModel(**r.model_dump()) for r in results]
        for i, view in enumerate(views, 1):
            view.rank = i
        payload = PageModel[ScreenResultViewModel](
            items=views,
            total=total,
            page=1,
            page_size=max(total, 1),
            pages=1,
        )
    return respond(payload, code, data_date=as_of_eff, elapsed_ms=timer.elapsed_ms)


@router.get("/jobs/{job_id}", summary="选股进度（S3）")
async def screen_job(request: Request, job_id: str) -> Response:
    """轮询选股进度；done 时 result_summary 含 batch 信息（R1.1-1）。"""
    container = _services(request)
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"screen job 不存在: {job_id}"
        )
    return respond(job, job["market"])


@router.get("/batches", summary="历史批次列表（S4）")
async def screen_batches(
    request: Request,
    market: str = "CN",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Response:
    """分页列出选股历史批次。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        payload = container.screen_service.list_batches(
            market=code, page=page, page_size=page_size
        )
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.get("/results", summary="批次结果（S5）")
async def screen_results(
    request: Request,
    batch_id: str,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
    sort_by: Literal["score", "code", "name", "price"] = "score",
    order: Literal["asc", "desc"] = "desc",
) -> Response:
    """分页返回批次结果（含 rank，可排序）。"""
    container = _services(request)
    batch = container.screen_service.get_batch(batch_id)
    if batch is None:
        raise StarletteHTTPException(status_code=404, detail=f"batch 不存在: {batch_id}")
    with Timer() as timer:
        payload = container.screen_service.get_batch_results(
            batch_id, page=page, page_size=page_size, sort_by=sort_by, order=order
        )
    return respond(payload, batch["market"], data_date=batch["as_of"], elapsed_ms=timer.elapsed_ms)


@router.get("/results/{batch_id}/export", summary="批次导出（S6）")
async def screen_export(
    request: Request,
    batch_id: str,
    format: Literal["json", "csv"] = "json",
    market: str = "CN",
) -> Response:
    """导出批次：json → 信封下载；csv → GBK 文件（NF-22 免责头，非信封）。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    batch = container.screen_service.get_batch(batch_id)
    if batch is None:
        raise StarletteHTTPException(status_code=404, detail=f"batch 不存在: {batch_id}")
    if format == "csv":
        data = container.screen_service.export_csv_bytes(batch_id)
        filename = f"screen_{batch_id}.csv"
        return Response(
            content=data,
            media_type="text/csv; charset=gbk",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    payload = container.screen_service.export_json_payload(batch_id)
    response = respond(payload, code, data_date=batch["as_of"])
    filename = f"screen_{batch_id}.json"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
