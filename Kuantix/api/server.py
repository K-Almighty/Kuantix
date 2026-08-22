"""REST 应用工厂与组合根（T05b1 + T05b2）。

:func:`create_app` 复用 :func:`Kuantix.main.create_app` 的基础设施
（``/health``、``/api/version``、``/docs``、``/openapi.json`` 与统一信封
异常映射），再挂载 data / factor / screen / monitor / backtest / portfolio /
optimize / strategies / settings 业务 router（``/api/v1/*``）。

:func:`register_routes` 幂等挂载各 router，并在**首个业务请求**时惰性装配
组合根 :class:`~Kuantix.api.deps.ServiceContainer`（避免 ``/health`` 等基础
端点触发数据目录创建）。
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI

from Kuantix.api.deps import ServiceContainer, build_analysis_components
from Kuantix.api.jobs import JobManager, JobStore
from Kuantix.api.routers import analysis as analysis_router
from Kuantix.api.routers import backtest as backtest_router
from Kuantix.api.routers import data as data_router
from Kuantix.api.routers import factor as factor_router
from Kuantix.api.routers import monitor as monitor_router
from Kuantix.api.routers import optimize as optimize_router
from Kuantix.api.routers import portfolio as portfolio_router
from Kuantix.api.routers import screen as screen_router
from Kuantix.api.routers import settings as settings_router
from Kuantix.api.routers import stock as stock_router
from Kuantix.api.routers import strategies as strategies_router
from Kuantix.config import Config, get_config

__all__ = [
    "ServiceContainer",
    "build_container",
    "build_monitor",
    "create_app",
    "register_routes",
]


def build_monitor(
    config: Config, *, name_lookup: object = None
) -> tuple[object, object, object, object, object]:
    """P0 修复：组合根 → :func:`Kuantix.monitor.build_monitor_components` 单一入口。

    与 CLI 共用装配逻辑，消除重复代码与漂移风险。参数语义同被调用函数：
    ``name_lookup`` 注入已存在的 ``MarketStore.security_name``，避免重连 SQLite。
    """
    from collections.abc import Callable

    from Kuantix.monitor import build_monitor_components

    _nl: Callable[[str, str], str | None] | None = name_lookup  # type: ignore[assignment]
    return build_monitor_components(config, name_lookup=_nl)


def build_container(config: Config) -> ServiceContainer:
    """组合根：从配置装配 DataLake / FactorService / ScreenService / JobManager /
    MonitorLoop。

    ScreenService 复用 FactorService 的 store 与模型加载器（组合根注入，
    NF-3 解耦），并注入真正的 :class:`FactorCombiner`，使 ic/ir 合成方法
    在选股链路可用。监控组件经 :func:`build_monitor` 装配。
    """
    from Kuantix.backtest.optimize_service import OptimizeService
    from Kuantix.backtest.portfolio_service import (
        MultiStrategyService,
        PortfolioService,
    )
    from Kuantix.backtest.service import BacktestService
    from Kuantix.backtest.strategy_store import StrategyStore
    from Kuantix.data.datalake import DataLake
    from Kuantix.data.market_store import MarketStore
    from Kuantix.data.security_search import SecuritySearchService
    from Kuantix.factor.combiner import FactorCombiner
    from Kuantix.factor.service import FactorService
    from Kuantix.screen.service import ScreenService
    from Kuantix.adapters.tdx_client import TdxClientFactory

    lake = DataLake(config)
    factor_service = FactorService(config)
    screen_service = ScreenService(
        config,
        store=factor_service.store,
        model_loader=factor_service.load_model,
        combiner=FactorCombiner(),
    )
    jobs = JobManager(JobStore(config.paths.db))
    # D8 搜索走本地 SQLite 清单（设计文档 08 §2：请求路径零网络枚举）。
    market_store = MarketStore(config.paths.db / config.storage.market_db)
    # 监控自选补全名称：把证券清单的名称查询回调注入 MonitorLoop。
    loop, engine, tracker, store, notifier = build_monitor(
        config, name_lookup=market_store.security_name
    )
    security_search = SecuritySearchService(config, store=market_store)
    backtest_service = BacktestService(config)
    portfolio_service = PortfolioService(config)
    multi_strategy_service = MultiStrategyService(config)
    optimize_service = OptimizeService(config)
    strategy_store = StrategyStore(config.paths.db / "strategies.db")
    # 盘前/盘后分析组件
    analysis_components = build_analysis_components(
        config,
        lake=lake,
        jobs=jobs,
        factor_service=factor_service,
        monitor_store=store,  # monitor_store = 5-tuple 中第 4 位 store
    )
    return ServiceContainer(
        config=config,
        lake=lake,
        factor_service=factor_service,
        screen_service=screen_service,
        jobs=jobs,
        monitor=loop,
        monitor_engine=engine,
        monitor_tracker=tracker,
        monitor_store=store,
        monitor_notifier=notifier,
        security_search=security_search,
        backtest_service=backtest_service,
        portfolio_service=portfolio_service,
        multi_strategy_service=multi_strategy_service,
        optimize_service=optimize_service,
        strategy_store=strategy_store,
        tdx_factory=TdxClientFactory.from_config(config),
        pre_open_service=analysis_components["pre_open_service"],
        post_close_service=analysis_components["post_close_service"],
        news_store=analysis_components["news_store"],
        fundamental_store=analysis_components["fundamental_store"],
        limit_store=analysis_components["limit_store"],
        stock_detail_service=analysis_components["stock_detail_service"],
    )


def register_routes(app: FastAPI, config: Config | None = None) -> None:
    """幂等挂载各业务 router，并注册惰性组合根工厂。

    Args:
        app: 目标 FastAPI 应用。
        config: 配置；``None`` 时取全局配置。仅用于首个业务请求时
            装配组合根。
    """
    if getattr(app.state, "services_factory", None) is None:
        resolved = config if config is not None else get_config()

        def _factory() -> ServiceContainer:
            return build_container(resolved)

        app.state.services_factory = _factory
        app.state.services = None
    app.include_router(data_router.router, prefix="/api/v1/data", tags=["data"])
    app.include_router(factor_router.router, prefix="/api/v1/factor", tags=["factor"])
    app.include_router(screen_router.router, prefix="/api/v1/screen", tags=["screen"])
    app.include_router(monitor_router.router, prefix="/api/v1/monitor", tags=["monitor"])
    app.include_router(backtest_router.router, prefix="/api/v1/backtest", tags=["backtest"])
    app.include_router(portfolio_router.router, prefix="/api/v1/portfolio", tags=["portfolio"])
    app.include_router(optimize_router.router, prefix="/api/v1/optimize", tags=["optimize"])
    app.include_router(strategies_router.router, prefix="/api/v1/strategies", tags=["strategies"])
    app.include_router(settings_router.router, prefix="/api/v1/settings", tags=["settings"])
    app.include_router(analysis_router.router, prefix="/api/v1/analysis", tags=["analysis"])
    app.include_router(stock_router.router, prefix="/api/v1/stock", tags=["stock"])


def create_app(
    config: Config | None = None,
    services: ServiceContainer | None = None,
) -> FastAPI:
    """构造完整 Kuantix REST 应用（基础设施 + 四业务路由）。

    Args:
        config: 显式配置；``None`` 时取全局配置。
        services: 显式组合根（测试注入假服务）；``None`` 时按配置装配。

    Returns:
        已挂载全部路由的 :class:`fastapi.FastAPI` 实例。
    """
    from Kuantix.main import create_app as infra_create_app

    app = infra_create_app(config)
    if services is not None:
        app.state.services = services
        app.state.services_factory = None
    return app
