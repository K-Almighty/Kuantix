"""factor 路由（契约 §2.2 F1–F6，前缀 ``/api/v1/factor``）。

端点：
- F1 ``GET  /factor`` —— 因子库列表（上游内置 + 自定义自动发现，NF-2）；
- F2 ``POST /factor/compute`` —— 触发因子计算（Job 信封，后台执行）；
- F3 ``GET  /factor/jobs/{job_id}`` —— 计算进度；
- F4 ``GET  /factor/report`` —— IC/IR/分层/换手/自相关报告；
- F5 ``POST /factor/combine`` —— 多因子合成（可选持久化模型）；
- F6 ``GET  /factor/models`` —— 已保存模型列表。

比例口径（契约 §8）：``ic_mean``/``ir``/``ic_positive_rate``/
``quantile_returns``/``top_minus_bottom``/``turnover_rate`` 一律小数
（``0.05`` = 5%）。日期 ``YYYY-MM-DD``（不泄漏 vipdoc ``YYYYMMDD``）。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import Response

from Kuantix.api.deps import (
    ServiceContainer,
    get_services,
    paginate,
    parse_pool,
    resolve_market,
    respond,
)
from Kuantix.api.schemas import (
    CombineRequest,
    ComputeRequest,
    RankingRequest,
)
from Kuantix.core.envelope import Timer
from Kuantix.core.fail_loud import (
    MissingKeyError,
    require_attr,
    require_key,
)
from Kuantix.factor.display_names import FACTOR_DISPLAY_NAMES
from Kuantix.factor.service import FactorService

__all__ = ["router"]


def _factor_display_name(name: str) -> str | None:
    """因子中文展示名兜底：查集中映射表，未登记返回 ``None``。"""
    return FACTOR_DISPLAY_NAMES.get(name)

router = APIRouter()

#: 上游英文分类 → 契约 §3.3 中文分类（纯展示映射，不做业务兜底）
_CATEGORY_CN: dict[str, str] = {
    "momentum": "动量",
    "value": "价值",
    "quality": "质量",
    "volatility": "波动",
    "technical": "技术",
    "volume": "量能",
    "chanlun": "缠论",
    "custom": "自定义",
}


def _services(request: Request) -> ServiceContainer:
    return get_services(request)


def _category_cn(category: str) -> str:
    """把上游英文分类映射成契约中文分类；未知分类显式归「其他」。"""
    if category in _CATEGORY_CN:
        return _CATEGORY_CN[category]
    return "其他"


def _factor_info(container: ServiceContainer, name: str) -> dict[str, Any]:
    """构造 FactorInfo（契约 §3.3）：从上游注册表读元数据 + 存储读已算年份。"""
    from Kuantix.adapters.factor_bridge import FACTORY_REGISTRY

    factor_cls = require_key(FACTORY_REGISTRY, name, "因子注册表")
    years = container.factor_service.store.years_for(name)
    module_name = str(require_attr(factor_cls, "__module__", f"因子 {name}"))
    source = "custom" if module_name.startswith("Kuantix") else "builtin"
    status = "computed" if years else "uncomputed"
    return {
        "name": name,
        "category": _category_cn(str(require_attr(factor_cls, "category", f"因子 {name}"))),
        "display_name": getattr(factor_cls, "display_name", None)
        or _factor_display_name(name),
        "description": str(require_attr(factor_cls, "description", f"因子 {name}")),
        "source": source,
        "status": status,
        "years": years,
    }


def _make_compute_runner(
    container: ServiceContainer,
    market: str,
    factors: list[str],
    start: dt.date,
    end: dt.date,
    pool: tuple[str, ...] | None,
    force: bool,
) -> Callable[[Callable[[dict[str, Any]], None], Callable[[Any], None]], dict[str, Any]]:
    """构造 F2 后台执行体：调用 FactorService.compute_factors。"""

    def runner(
        progress_cb: Callable[[dict[str, Any]], None],
        register_handle: Callable[[Any], None],
    ) -> dict[str, Any]:
        # 进程隔离执行：重计算（全市场/全区间）放到独立 spawn 子进程，
        # 避免数 GB 内存与长耗时占满 GIL 拖垮整个 API 进程（详见
        # Kuantix.factor.worker）。register_handle 用于取消时终止子进程。
        # 测试环境注入的是 FakeFactorService（非 FactorService 实例），
        # spawn 子进程在 pytest 下不可用 → 走同步路径，直接复用服务层。
        if not isinstance(container.factor_service, FactorService):
            results = container.factor_service.compute_factors(
                ComputeRequest(
                    market=market,
                    factors=tuple(factors),
                    start=start,
                    end=end,
                    codes=pool,
                    force=force,
                )
            )
            summary = {r.factor: r.to_dict() for r in results}
            return {"factors": summary, "count": len(results)}

        from Kuantix.factor.worker import run_compute_in_process

        return run_compute_in_process(
            market=market,
            factors=factors,
            start=start,
            end=end,
            pool=pool,
            force=force,
            register_handle=register_handle,
        )

    return runner


def _int_to_date(value: int) -> dt.date:
    """vipdoc ``YYYYMMDD`` 整数 → ``datetime.date``（仅服务层内部换算）。"""
    return dt.date(value // 10000, (value // 100) % 100, value % 100)


def _quantile_list(quantiles: Any) -> list[float]:
    """把上游分层收益转成契约 Q1..Q5 数组。

    兼容两种键形态（历史上两种都出现过）：
    - 整数键 ``{1: .., 5: ..}``；
    - 上游 ``FactorReport.quantile_returns`` 的字符串键 ``{'q1': .., 'q5': ..}``。

    值为 ``None``（上游 NaN 经 ``_safe_float`` 归一）的分层跳过，不参与数组
    （NF-12：JSON 不得含 NaN/Inf）。
    """
    if quantiles is None:
        return []
    if isinstance(quantiles, list):
        return [float(q) for q in quantiles if q is not None]

    def _key_int(key: Any) -> int:
        raw = str(key).strip().lower()
        # 'q1' / 'Q1' → 1；纯数字 '1' → 1
        if raw.startswith("q"):
            raw = raw[1:]
        try:
            return int(raw)
        except ValueError as exc:  # noqa: BLE001 - 未知键不静默
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 无法解析分层键 {key!r}，"
                f"quantile_returns 键应为整数或 qN"
            ) from exc

    keys = sorted(quantiles, key=_key_int)
    out: list[float] = []
    for k in keys:
        value = quantiles[k]
        if value is None:
            continue
        out.append(float(value))
    return out


def _report_payload(
    container: ServiceContainer,
    name: str,
    market: str,
    report: dict[str, Any],
    dates: list[int],
    sample_count: int,
    start: dt.date | None,
    end: dt.date | None,
) -> dict[str, Any]:
    """把 FactorService.report 输出规范成契约 §3.3 FactorReport。

    ``dates`` / ``sample_count`` 由报告子进程在隔离环境内计算后回传，
    避免在本进程（事件循环）内读取整个因子 parquet 造成阻塞。
    """
    actual_start = _int_to_date(min(dates)) if dates else None
    actual_end = _int_to_date(max(dates)) if dates else None
    quarantined = container.lake.list_quarantine(market)
    return {
        "factor": name,
        "market": market,
        "start_date": (
            start.isoformat()
            if start is not None
            else (actual_start.isoformat() if actual_start else None)
        ),
        "end_date": (
            end.isoformat()
            if end is not None
            else (actual_end.isoformat() if actual_end else None)
        ),
        "sample_count": int(sample_count),
        "excluded_count": len(quarantined),
        "ic_mean": require_key(report, "ic_mean", f"factor report {name}"),
        "ic_std": require_key(report, "ic_std", f"factor report {name}"),
        "ir": require_key(report, "ir", f"factor report {name}"),
        "ic_positive_rate": require_key(
            report, "ic_positive_rate", f"factor report {name}"
        ),
        "quantile_returns": _quantile_list(
            require_key(report, "quantile_returns", f"factor report {name}")
        ),
        "top_minus_bottom": require_key(report, "top_minus_bottom", f"factor report {name}"),
        "turnover_rate": require_key(report, "turnover_rate", f"factor report {name}"),
        "autocorr": require_key(report, "autocorr", f"factor report {name}"),
        "ic_series": require_key(report, "ic_series_tail", f"factor report {name}"),
    }


@router.get("", summary="因子库列表（F1）")
async def factor_list(
    request: Request,
    market: str = "CN",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Response:
    """列出全部可用因子（上游内置 + ``factor/factors/`` 自定义自动发现）。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        names = container.factor_service.list_factors()
        items = [_factor_info(container, name) for name in names]
        payload = paginate(items, page, page_size)
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.post("/compute", summary="触发因子计算（F2）")
async def factor_compute(request: Request, body: ComputeRequest) -> Response:
    """触发因子计算，返回 Job 信封（后台执行，全程读本地 L1）。"""
    container = _services(request)
    code = resolve_market(container.config, body.market)
    factors = list(dict.fromkeys(body.factors))
    if not factors:
        raise MissingKeyError("[fail-loud/NF-26] ComputeRequest.factors 不能为空")
    known = set(container.factor_service.list_factors())
    for factor in factors:
        if factor not in known:
            raise StarletteHTTPException(status_code=404, detail=f"因子不存在: {factor}")
    start = body.start if body.start is not None else dt.date(2020, 1, 1)
    end = body.end if body.end is not None else dt.date(2025, 12, 31)
    if start > end:
        raise MissingKeyError(
            f"[fail-loud/NF-26] start={start.isoformat()} 晚于 end={end.isoformat()}"
        )
    pool = parse_pool(body.pool)
    params = {
        "factors": factors,
        "market": code,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "pool": body.pool,
        "force": body.force,
    }
    runner = _make_compute_runner(container, code, factors, start, end, pool, body.force)
    with Timer() as timer:
        job = container.jobs.submit("factor", "compute", code, params, runner)
    return respond(job, code, elapsed_ms=timer.elapsed_ms)


@router.get("/jobs/{job_id}", summary="因子计算进度（F3）")
async def factor_job(request: Request, job_id: str) -> Response:
    """轮询因子计算进度。"""
    container = _services(request)
    job = container.jobs.get(job_id)
    if job is None:
        raise StarletteHTTPException(
            status_code=404, detail=f"factor job 不存在: {job_id}"
        )
    return respond(job, job["market"])


@router.get("/report", summary="因子有效性报告（F4，同步）")
async def factor_report(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    market: str = "CN",
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> Response:
    """输出 IC / IR / 分层收益 / 换手率 / IC 序列（**同步**返回完整报告）。

    契约 §2.2 F4 定义为 ``GET /factor/report?name=...``，返回完整
    FactorReport 契约对象（非 Job 信封）。报告是幂等只读计算，直接在
    事件循环内完成；全样本分析较重，属 F4 的已知成本（见 PERF_REPORT
    风险台账 R3：同步化修复 405 契约漂移）。
    """
    container = _services(request)
    code = resolve_market(container.config, market)
    name = str(name).strip()
    if not name:
        raise MissingKeyError("[fail-loud/NF-26] report 缺少因子名 name")
    known = set(container.factor_service.list_factors())
    if name not in known:
        raise StarletteHTTPException(status_code=404, detail=f"因子不存在: {name}")
    factor_df = container.factor_service.store.load(name)
    if factor_df is None or (hasattr(factor_df, "empty") and factor_df.empty):
        raise StarletteHTTPException(
            status_code=404, detail=f"因子 {name} 无已计算数据，请先 compute"
        )
    dates = (
        sorted({int(d) for d in factor_df["date"]})
        if "date" in getattr(factor_df, "columns", [])
        else []
    )
    with Timer() as timer:
        report = container.factor_service.report(name, market=code)
        payload = _report_payload(
            container,
            name,
            code,
            report,
            dates,
            int(len(factor_df)),
            start,
            end,
        )
    return respond(
        payload,
        code,
        data_date=payload["end_date"],
        elapsed_ms=timer.elapsed_ms,
    )


@router.post("/combine", summary="多因子合成（F5，同步）")
async def factor_combine(request: Request, body: CombineRequest) -> Response:
    """多因子合成，返回 ModelHandle（**同步**，非 Job 信封）。

    契约 §2.2 F5 定义为 ``POST /factor/combine`` 并**同步返回合成模型**。
    测试与前端均直接消费 ``data.name``/``data.weights``（FactorModel 契约）。
    全样本矩阵计算较重，属 F5 的已知成本（与 F4 同步化同理）。
    """
    container = _services(request)
    code = resolve_market(container.config, body.market)
    factors = list(dict.fromkeys(body.factors))
    if not factors:
        raise MissingKeyError("[fail-loud/NF-26] CombineRequest.factors 不能为空")
    known = set(container.factor_service.list_factors())
    for factor in factors:
        if factor not in known:
            raise StarletteHTTPException(status_code=404, detail=f"因子不存在: {factor}")
    with Timer() as timer:
        handle = container.factor_service.combine(
            factors,
            body.method,
            name=body.model_name,
            save_model=body.save_model,
            market=code,
        )
        payload = handle.to_dict()
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.get("/models", summary="已保存模型列表（F6）")
async def factor_models(
    request: Request,
    market: str = "CN",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Response:
    """列出已保存的合成模型（F5 持久化后可见）。"""
    container = _services(request)
    code = resolve_market(container.config, market)
    with Timer() as timer:
        handles = container.factor_service.list_model_handles()
        items = [handle.to_dict() for handle in handles]
        payload = paginate(items, page, page_size)
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)


@router.post("/ranking", summary="多因子综合排名（F7）")
async def factor_ranking(request: Request, body: RankingRequest) -> Response:
    """对多个因子做回测绩效 + 有效性综合排名（对标 easy_tdx 多因子对比）。

    每个因子构建 top 分位等权组合回测，推导收益率/夏普/最大回撤/胜率/换手，
    合并 IC/IR/多空差，按综合评分降序排名。
    """
    container = _services(request)
    code = resolve_market(container.config, body.market)
    factors = list(dict.fromkeys(body.factors))
    if len(factors) < 2:
        raise MissingKeyError(
            "[fail-loud/NF-26] 多因子排名至少选择 2 个因子"
        )
    known = set(container.factor_service.list_factors())
    for factor in factors:
        if factor not in known:
            raise StarletteHTTPException(status_code=404, detail=f"因子不存在: {factor}")

    from Kuantix.factor.ranking import FactorRankingService, RankingConfig

    ranking_svc = FactorRankingService(container.factor_service)
    cfg = RankingConfig(
        forward_period=body.forward_period,
        n_quantiles=body.n_quantiles,
        top_fraction=body.top_fraction,
    )
    with Timer() as timer:
        payload = ranking_svc.rank(
            factors,
            market=code,
            start=body.start,
            end=body.end,
            config=cfg,
        )
    return respond(payload, code, elapsed_ms=timer.elapsed_ms)
