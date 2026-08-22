"""API 层共享工具（组合根类型 / 市场门禁 / 分页 / 信封渲染）。

- :class:`ServiceContainer` —— 组合根：把数据/因子/选股服务与 JobManager
  装配在一起，供三个 router 按需取用；
- :func:`resolve_market` —— 市场码规范化 + 501 门禁（NF-6/NF-7）；
- :func:`parse_pool` —— 选股/因子计算的 pool 参数解析（``all`` /
  代码数组 / ``watchlist``→501）；
- :func:`paginate` —— 分页壳（契约 §1.6，page/page_size 由 FastAPI Query 校验）；
- :func:`respond` —— 统一信封渲染（NF-9/NF-12）。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from Kuantix import __version__
from Kuantix.config import Config
from Kuantix.core.envelope import Envelope
from Kuantix.core.fail_loud import (
    MissingConfigError,
    MissingKeyError,
    NotSupportedError,
    require_known,
)
from Kuantix.core.market import known_markets
from Kuantix.main import envelope_response

__all__ = [
    "ServiceContainer",
    "MAX_PAGE_SIZE",
    "build_analysis_components",
    "flat_position_view",
    "get_services",
    "get_services_from_app",
    "paginate",
    "parse_pool",
    "resolve_market",
    "respond",
]

#: 契约 §1.6：page_size 上限 500，超限报 400
MAX_PAGE_SIZE = 500


@dataclass
class ServiceContainer:
    """REST 组合根（依赖注入便于离线测试）。

    Attributes:
        config: 配置对象。
        lake: 数据湖门面（鸭子类型：status / verify_payload / sync_full /
            sync_incremental / list_quarantine / remove_quarantine）。
        factor_service: 因子服务门面（鸭子类型：list_factors /
            list_model_handles / compute_factors / report / combine /
            list_models / load_model / store）。
        screen_service: 选股服务门面（鸭子类型：run_batch / list_batches /
            get_batch / get_batch_results / export_json_payload /
            export_csv_bytes）。
        jobs: :class:`Kuantix.api.jobs.JobManager`。
        monitor: 监控主循环门面（鸭子类型：start / stop / status /
            add_watch / remove_watch / list_watch / watchlist_codes /
            add_rule / list_rules / delete_rule / get_rule）。
        monitor_engine: 规则引擎（M7/M9/M10：criteria_info / create_rule /
            update_rule）。
        monitor_tracker: 持仓追踪器（M12/M13/M14）。
        monitor_store: 监控存储（M15/M17 snapshot：list_alerts）。
        monitor_notifier: 推送器（M16：channels_info）。
        security_search: 证券搜索服务（D8：search / catalog_size）。
        backtest_service: 回测服务（B1–B4：list_strategies / run / get_result /
            delete_result）。
        portfolio_service: 组合回测服务（P1/P3：run / get_result）。
        multi_strategy_service: 多策略回测服务（S5：run / get_result）。
        optimize_service: 参数寻优服务（O1/O3：run / get_result）。
        strategy_store: 策略库存储（S1–S4：create / get / list / update / delete）。
        tdx_factory: easy-tdx 客户端工厂（E2 连通性测试：probe_connection，
            **只测不写**；缺省 None 时 E2 显式 400，fail-loud）。
        pre_open_service: 盘前分析服务（消息面/基本面/技术面）。
        post_close_service: 盘后复盘服务（涨跌停/技术亮点/自选PnL）。
        news_store: 消息面条目 SQLite 存储。
        fundamental_store: 基本面画像 SQLite 存储。
        limit_store: 涨跌停条目 + 汇总 SQLite 存储。
        stock_detail_service: 个股详情服务（多周期 K 线 + 技术指标 + 核心数据）。
    """

    config: Config
    lake: Any
    factor_service: Any
    screen_service: Any
    jobs: Any
    monitor: Any = None
    monitor_engine: Any = None
    monitor_tracker: Any = None
    monitor_store: Any = None
    monitor_notifier: Any = None
    security_search: Any = None
    backtest_service: Any = None
    portfolio_service: Any = None
    multi_strategy_service: Any = None
    optimize_service: Any = None
    strategy_store: Any = None
    tdx_factory: Any = None
    pre_open_service: Any = None
    post_close_service: Any = None
    news_store: Any = None
    fundamental_store: Any = None
    limit_store: Any = None
    stock_detail_service: Any = None


def get_services_from_app(app: Any) -> ServiceContainer:
    """从 FastAPI 应用状态取组合根；未装配时触发惰性工厂。

    Args:
        app: FastAPI 应用实例（HTTP 用 ``request.app``，WebSocket 用
            ``websocket.app``）。

    Returns:
        组合根（可能由惰性工厂在此刻首次装配）。

    Raises:
        MissingConfigError: 应用未装配服务容器工厂。
    """
    services = getattr(app.state, "services", None)
    if services is None:
        factory = getattr(app.state, "services_factory", None)
        if factory is None:
            raise MissingConfigError(
                "[fail-loud/NF-26] 应用未装配服务容器（services_factory 缺失）"
            )
        services = factory()
        app.state.services = services
    return services


def get_services(request: Request) -> ServiceContainer:
    """从应用状态取组合根；未装配时触发惰性工厂（首个业务请求装配）。"""
    return get_services_from_app(request.app)


def flat_position_view(record: dict[str, Any]) -> dict[str, Any]:
    """由持仓记录构造 PositionView（契约 §3.5，M12/M13/CLI 共用）。

    说明：P0 监控轮询不缓存实时报价，新增/列出持仓时若无实时 quote，
    ``last`` 以成本价占位、``change_pct``/``pnl``/``pnl_pct`` 为 0 ——
    这是显式的「暂无报价」初始态（供前端先渲染，实时盈亏由 P1 报价缓存
    提供），不是静默兜底。
    """
    shares = float(record["shares"])
    cost_price = float(record["cost_price"])
    return {
        "code": record["code"],
        "name": record["name"],
        "market": record["market"],
        "shares": shares,
        "cost_price": cost_price,
        "last": cost_price,
        "change_pct": 0.0,
        "market_value": shares * cost_price,
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "as_of": dt.date.today().isoformat(),
    }


def resolve_market(config: Config, market: str) -> str:
    """规范化市场码并做「未启用 → 501」门禁（NF-6/NF-7）。

    Args:
        config: 配置（``[markets]`` 开关）。
        market: 请求里的市场码（大小写不敏感）。

    Returns:
        规范化后的大写市场码（如 ``CN``）。

    Raises:
        UnknownValueError: 市场码未注册（→ 422）。
        NotSupportedError: 市场已注册但未在 config 启用（→ 501，契约 §1.8）。
    """
    code = require_known(
        str(market).strip().upper(), "market 参数", allowed=set(known_markets())
    )
    if not config.markets.is_enabled(code):
        raise NotSupportedError(
            f"[fail-loud/NF-7] 市场 {code} 未启用（P0 仅 CN）。"
            f"接口先行、拒绝静默降级：不要用 A 股规则代替 {code}。"
        )
    return code


def parse_pool(pool: str | list[str] | None) -> tuple[str, ...] | None:
    """把 ``pool`` 参数解析成代码元组（契约 §3.4 ScreenRunRequest.pool）。

    - ``"all"`` / ``None`` → ``None``（全市场）；
    - 代码数组 → 去重后的元组；
    - ``"watchlist"`` → 依赖 monitor 模块，尚未接入 → 501（fail-loud）。

    Raises:
        NotSupportedError: pool=watchlist（P0 未接入）。
        MissingKeyError: pool 为空数组或取值非法（→ 400）。
    """
    if pool is None:
        return None
    if isinstance(pool, str):
        text = pool.strip().upper()
        if text == "ALL":
            return None
        if text == "WATCHLIST":
            raise NotSupportedError(
                "[fail-loud/NF-26] pool=watchlist 依赖 monitor 模块，"
                "P0 尚未接入。拒绝静默按全市场处理"
            )
        raise MissingKeyError(
            f"[fail-loud/NF-26] pool 取值非法: {pool!r}（期望 all / watchlist / 代码数组）"
        )
    codes = [str(c).strip() for c in pool if str(c).strip()]
    if not codes:
        raise MissingKeyError("[fail-loud/NF-26] pool 代码数组为空")
    return tuple(dict.fromkeys(codes))


def paginate(items: list[Any], page: int, page_size: int) -> dict[str, Any]:
    """构造契约 §1.6 分页壳。

    注意：P1-2 建议走 :func:`page_limits` + :func:`db_page`，把 LIMIT/OFFSET
    下推到 SQLite。本函数仅用于：

    * 非 DB 源的短列表（因子名集合、preset 列表等 < 500 条）；
    * 尚未接入 DB 分页的过渡路由。
    """
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if total else 0
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


def page_limits(page: int, page_size: int) -> tuple[int, int]:
    """P1-2：把 ``(page, page_size)`` 翻译成 SQLite 层 ``(LIMIT, OFFSET)``。

    调用方务必先校验 ``page >= 1``、``1 <= page_size <= 500``；
    FastAPI 的 ``Query(..., ge=1, le=500)`` 已在路由端保证。

    Returns:
        ``(limit: int, offset: int)``
    """
    return int(page_size), int((page - 1) * page_size)


def db_page(
    page_items: list[Any],
    total: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """P1-2：从**已经下推 LIMIT/OFFSET 的一页** + 独立 COUNT 的 total 构造分页壳。

    与 :func:`paginate` 的区别：**不再对 page_items 切片**，直接视为一页。
    适用于底层存储已执行 ``SELECT ... LIMIT ? OFFSET ?`` +
    ``SELECT COUNT(*)`` 的场景（内存量从 O(total) 降到 O(page_size)）。

    Args:
        page_items: 已按 LIMIT/OFFSET 取出的一页条目（路由端做 ``to_dict()``
            / 展平视图等转换后传入）。
        total: 独立 COUNT 查询得到的**全部匹配**条目数。
        page: 页码（1 起）。
        page_size: 每页条数。
    """
    total_pages = (int(total) + page_size - 1) // page_size if total else 0
    return {
        "items": list(page_items),
        "page": int(page),
        "page_size": int(page_size),
        "total": int(total),
        "total_pages": int(total_pages),
    }


def respond(
    data: Any,
    market: str,
    *,
    data_date: str | None = None,
    elapsed_ms: int = 0,
) -> Response:
    """渲染成功信封（NF-9/NF-12，数值经 Envelope.to_json 清洗）。"""
    return envelope_response(
        Envelope.ok(
            data,
            market=market,
            version=__version__,
            elapsed_ms=elapsed_ms,
            data_date=data_date,
        )
    )


# ---------------------------------------------------------------------------
# Analysis 模块装配
# ---------------------------------------------------------------------------


def build_analysis_components(
    config: Config,
    *,
    lake: Any,
    jobs: Any,
    factor_service: Any,
    monitor_store: Any = None,
) -> dict[str, Any]:
    """构造盘前/盘后 5 个组件（Pre/Post + 3 Stores），返回字典。

    约定三库文件路径：``config.paths.db / analysis_{news,fundamental,limit_up_down}.db``。
    NewsProvider 使用 ``create_news_provider(cfg.analysis.news_provider, cfg)``
    工厂构造；用户后续可按需注入自定义 provider。

    Returns:
        ``{'pre_open_service','post_close_service','news_store','fundamental_store',
        'limit_store'}`` 字典，供 router / scheduler / CLI / container 复用。
    """
    # 延迟 import（避免 core 层 → analysis 循环）
    from Kuantix.adapters.news_provider import create_news_provider
    from Kuantix.analysis.post_close import PostCloseService
    from Kuantix.analysis.pre_open import PreOpenService
    from Kuantix.analysis.stores import (
        FundamentalStore,
        LimitUpDownStore,
        NewsStore,
    )

    db_dir = config.paths.db
    news_store = NewsStore(db_dir / "analysis_news.db")
    fundamental_store = FundamentalStore(db_dir / "analysis_fundamental.db")
    limit_store = LimitUpDownStore(db_dir / "analysis_limit_up_down.db")

    news_provider = create_news_provider(
        str(config.analysis.news_provider), config,
    )

    pre_open_service = PreOpenService(
        config,
        lake=lake,
        factor_service=factor_service,
        news_store=news_store,
        fundamental_store=fundamental_store,
        news_provider=news_provider,
        monitor_store=monitor_store,
    )

    post_close_service = PostCloseService(
        config,
        lake=lake,
        factor_service=factor_service,
        limit_store=limit_store,
        pre_open=pre_open_service,
        monitor_store=monitor_store,
    )

    from Kuantix.analysis.stock_detail import StockDetailService
    from Kuantix.data.market_store import MarketStore

    stock_detail_service = StockDetailService(store=MarketStore(), config=config)

    return {
        "news_store": news_store,
        "fundamental_store": fundamental_store,
        "limit_store": limit_store,
        "pre_open_service": pre_open_service,
        "post_close_service": post_close_service,
        "stock_detail_service": stock_detail_service,
    }

