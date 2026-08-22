"""``Kuantix serve`` 的 FastAPI 应用工厂与启动入口（T01 骨架 + T05 路由挂载）。

职责边界
--------
本模块负责三件事：

1. :func:`create_app` —— 应用工厂。装配 CORS、异常处理器、``/health``、
   ``/api/version`` 三个基础设施级端点，并调用
   :func:`Kuantix.api.server.register_routes` 挂载 T05 业务路由
   （``/api/v1/data``、``/api/v1/factor``、``/api/v1/screen``；
   monitor 由 T05b2 补挂）。
2. :func:`run` —— 用 uvicorn 拉起应用，端口/host/reload 全部来自配置
   （``[server]`` 节，支持 ``Kuantix__SERVER__PORT`` 环境变量覆盖，NF-16）。
3. 全局异常 → 统一信封（NF-9）的映射，保证 REST 与 CLI 的错误形状一致。

红线遵循
--------
* **NF-9**：所有响应（含异常）都是 ``{code, message, data, meta}``。
* **NF-12**：JSON 序列化统一走 :meth:`Envelope.to_dict`，NaN/Inf → null，
  浮点 6 位；这里用 ``Response(content=envelope.to_json())`` 而不是
  ``JSONResponse(dict)``，避免 starlette 的默认 ``json.dumps`` 放行 ``NaN``。
* **NF-26**：不吞异常。:class:`FailLoudError` 被翻译成明确的 4xx/5xx 信封，
  错误消息原样透传；未预期异常返回 500，同样带完整消息，不做"友好化"改写。
* **NF-28**：serve 只提供只读查询面，不在应用启动时触发任何回补/轮询。
"""

from __future__ import annotations

import datetime as dt
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from Kuantix import __version__
from Kuantix.config import Config, get_config
from Kuantix.core.envelope import (
    CODE_DATA_ERROR,
    CODE_INTERNAL_ERROR,
    CODE_INVALID_ARGUMENT,
    CODE_NOT_FOUND,
    CODE_NOT_IMPLEMENTED,
    Envelope,
)
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    FailLoudError,
    MissingConfigError,
    MissingKeyError,
    NotSupportedError,
    UnknownValueError,
    UpstreamContractError,
)

__all__ = [
    "JSON_MEDIA_TYPE",
    "create_app",
    "envelope_response",
    "http_status_for",
    "run",
]

#: 统一 JSON media type（显式声明 charset，避免中文错误消息在部分客户端乱码）
JSON_MEDIA_TYPE = "application/json; charset=utf-8"

#: 业务错误码 → HTTP 状态码。显式全量映射，未登记的码走 500（不静默取 200）。
_HTTP_STATUS_BY_CODE: dict[int, int] = {
    CODE_INVALID_ARGUMENT: 400,
    CODE_NOT_FOUND: 404,
    CODE_DATA_ERROR: 422,
    CODE_INTERNAL_ERROR: 500,
    CODE_NOT_IMPLEMENTED: 501,
}

#: fail-loud 异常类型 → 业务错误码。顺序敏感：子类必须排在父类之前。
_CODE_BY_EXCEPTION: tuple[tuple[type[Exception], int], ...] = (
    (NotSupportedError, CODE_NOT_IMPLEMENTED),
    (UnknownValueError, CODE_DATA_ERROR),
    (DataIntegrityError, CODE_DATA_ERROR),
    (UpstreamContractError, CODE_DATA_ERROR),
    (MissingKeyError, CODE_INVALID_ARGUMENT),
    (MissingConfigError, CODE_INVALID_ARGUMENT),
    (FailLoudError, CODE_DATA_ERROR),
)


def http_status_for(code: int) -> int:
    """把业务错误码翻译成 HTTP 状态码。

    Args:
        code: :mod:`Kuantix.core.envelope` 中的业务状态码。

    Returns:
        对应的 HTTP 状态码；``code=0`` 返回 200，未登记的非零码返回 500。
    """
    if code == 0:
        return 200
    if code in _HTTP_STATUS_BY_CODE:
        return _HTTP_STATUS_BY_CODE[code]
    return 500


def _code_for_exception(exc: BaseException) -> int:
    """按登记顺序把异常映射为业务错误码。"""
    for exc_type, code in _CODE_BY_EXCEPTION:
        if isinstance(exc, exc_type):
            return code
    return CODE_INTERNAL_ERROR


def envelope_response(envelope: Envelope) -> Response:
    """把信封渲染成 HTTP 响应（NF-9 / NF-12）。

    直接使用 :meth:`Envelope.to_json`（``allow_nan=False``）序列化，绕开
    starlette ``JSONResponse`` 默认允许 ``NaN`` 的行为。

    Args:
        envelope: 待渲染的信封。

    Returns:
        ``application/json`` 响应，状态码由 :func:`http_status_for` 决定。
    """
    return Response(
        content=envelope.to_json(),
        status_code=http_status_for(envelope.code),
        media_type=JSON_MEDIA_TYPE,
    )


def _default_market(config: Config) -> str:
    """取默认市场码，供异常信封的 ``meta.market`` 使用（NF-6）。"""
    return config.markets.default


def _build_scheduler(config: Config) -> Any:
    """装配增量同步调度器（独立于 REST 组合根，避免测试 TestClient 触发网络）。

    设计二 D2.3：lifespan 内用 ``config`` 构造 ``DataLake`` 与
    ``SyncStateStore``（与 ``build_container`` 同一服务层）；调度器独立装配，
    不依赖组合根 —— 测试环境经 conftest 注入 ``schedule_enabled=false`` +
    空湖守卫双保险，保证零网络。
    """
    from Kuantix.data.datalake import DataLake
    from Kuantix.data.sync_state import SyncStateStore
    from Kuantix.scheduler import IncrementalSyncScheduler

    lake = DataLake(config)
    state = SyncStateStore(config.paths.db)
    return IncrementalSyncScheduler(config, lake, state)


def _warn_if_migration_pending(config: Config) -> None:
    """启动检查（设计文档 08 D3）：market.db 空 & vipdoc 非空 → 警告不自动迁移。

    只打日志、不阻塞启动、不自动执行 ``Kuantix data migrate``
    （``migrate_on_startup=false`` 默认）；避免 serve 首启卡在迁移上。
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from Kuantix.data.market_store import MarketStore

        store = MarketStore(config.paths.db / config.storage.market_db)
        if store.daily_bar_count() > 0:
            return
        vipdoc = config.paths.vipdoc
        count = 0
        for exchange in ("sh", "sz"):
            lday = vipdoc / exchange / "lday"
            if lday.is_dir():
                count += len(list(lday.glob("*.day")))
        if count > 0:
            logger.warning(
                "检测到 vipdoc 有 %d 个 .day 文件，但 market.db 为空 —— "
                "行情主存储尚未迁移。请执行 `Kuantix data migrate` 一次性导入 "
                "（启动不自动迁移，migrate_on_startup=false，设计文档 08 D3）",
                count,
            )
    except Exception as exc:  # noqa: BLE001 - 启动检查失败只警告，不阻断服务
        logger.warning("market.db 启动检查跳过（%s: %s）", type(exc).__name__, exc)


def _ensure_preset_rules(app: FastAPI, resolved: Config) -> None:
    """启动注入预设监控规则（默认开启、幂等）。

    - 优先复用 ``app.state.services``（create_app 已装配，单组合根、无副作用）；
    - 若 services 尚未就绪（如纯配置构造期），回退 ``build_container(resolved)``；
    - 仅打日志、不阻断启动；异常（如存储不可用）只警告。
    """
    import logging

    logger = logging.getLogger(__name__)
    container = getattr(app.state, "services", None)
    if container is None:
        try:
            from Kuantix.api.server import build_container

            container = build_container(resolved)
        except Exception as exc:  # noqa: BLE001
            logger.warning("预设监控规则注入跳过（组合根装配失败 %s: %s）", type(exc).__name__, exc)
            return
    try:
        injected = container.monitor_engine.ensure_presets()
        if injected:
            logger.info("已注入预设监控规则: %s", injected)
    except Exception as exc:  # noqa: BLE001 - 启动注入失败只警告，不阻断服务
        logger.warning("预设监控规则注入跳过（%s: %s）", type(exc).__name__, exc)


def _make_lifespan(resolved: Config):
    """构造 FastAPI lifespan（config 门控调度器 start/stop + 启动检查 daemon）。

    - ``[sync].schedule_enabled=true`` 才挂载调度器（否则零副作用）；
    - ``[sync].schedule_startup_check=true`` 才在启动时跑幂等检查（daemon 线程，
      非阻塞；空湖 skip / 今日已同步 skip，不自动全量，D2.1/D-6）；
    - 启动始终执行「迁移待办」警告检查（D3：只警告不自动）。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _warn_if_migration_pending(resolved)
        # 首次启动注入预设监控规则（默认开启，幂等；异常只日志不阻断启动）
        _ensure_preset_rules(app, resolved)
        scheduler = None
        if resolved.sync.schedule_enabled:
            scheduler = _build_scheduler(resolved)
            scheduler.start()
            if resolved.sync.schedule_startup_check:
                threading.Thread(
                    target=scheduler.startup_check,
                    name="Kuantix-startup-sync",
                    daemon=True,
                ).start()
        yield
        if scheduler is not None:
            scheduler.stop()

    return lifespan


def _market_for_request(request: Request, config: Config) -> str:
    """解析请求里显式携带的市场码（query 参数），非法/缺失回落到默认值。

    契约 §1.8：``meta.market`` 回显实际生效的市场码。错误信封同样遵循
    （附录 A 错误示例：HK 请求的 501 信封 ``meta.market = "HK"``）。
    """
    from Kuantix.core.market import known_markets

    raw = request.query_params.get("market")
    if raw:
        candidate = str(raw).strip().upper()
        if candidate in known_markets():
            return candidate
    return config.markets.default


def create_app(config: Config | None = None) -> FastAPI:
    """构造 FastAPI 应用（T01 骨架：基础设施端点 + 统一信封异常处理）。

    Args:
        config: 显式配置；``None`` 时取进程级全局配置
            （:func:`Kuantix.config.get_config`）。

    Returns:
        已装配好 CORS、异常处理器与基础端点的 :class:`fastapi.FastAPI` 实例。

    Note:
        业务路由（``/api/v1/data``、``/api/v1/factor``、``/api/v1/screen``、
        ``/api/v1/monitor``）在 T05 挂载：:func:`create_app` 会调用
        :func:`Kuantix.api.server.register_routes` 装配 data/factor/screen
        三个 router（组合根惰性装配，首个业务请求时才创建数据目录）；
        monitor router 由 T05b2 补挂。
    """
    resolved = config if config is not None else get_config()

    # P1-3：REST 入口统一初始化 logging（在 CORS / 路由 / 异常处理之前，
    # 确保首个请求前日志格式、文件滚动已就位）。
    from Kuantix.logging_config import configure_logging

    configure_logging(resolved)

    app = FastAPI(
        title=resolved.app.name,
        version=__version__,
        description=(
            "Kuantix 本地量化研究工作台 REST 接口。"
            "所有响应遵循统一信封 {code, message, data, meta}（NF-9）。"
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=_make_lifespan(resolved),
    )
    app.state.config = resolved
    app.state.started_at = dt.datetime.now().astimezone()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.server.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    market = _default_market(resolved)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        """把 404/405 等框架级错误也包成统一信封。"""
        code = CODE_NOT_FOUND if exc.status_code == 404 else CODE_INVALID_ARGUMENT
        envelope = Envelope.fail(
            code=code,
            message=f"{request.method} {request.url.path}: {exc.detail}",
            market=_market_for_request(request, resolved),
            version=__version__,
        )
        return Response(
            content=envelope.to_json(),
            status_code=exc.status_code,
            media_type=JSON_MEDIA_TYPE,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        """请求参数校验失败：原样回传 pydantic 明细，不做压缩改写（NF-26）。"""
        envelope = Envelope.fail(
            code=CODE_INVALID_ARGUMENT,
            message=f"{request.method} {request.url.path}: 请求参数校验失败",
            market=_market_for_request(request, resolved),
            version=__version__,
            data={"errors": exc.errors()},
        )
        return envelope_response(envelope)

    @app.exception_handler(FailLoudError)
    async def _handle_fail_loud(request: Request, exc: FailLoudError) -> Response:
        """fail-loud 异常：错误消息原样透传，附异常类型便于定位（NF-26）。"""
        envelope = Envelope.fail(
            code=_code_for_exception(exc),
            message=str(exc),
            market=_market_for_request(request, resolved),
            version=__version__,
            data={"error_type": type(exc).__name__, "path": request.url.path},
        )
        return envelope_response(envelope)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> Response:
        """未预期异常：返回 500，消息不做"友好化"改写（NF-26 不掩盖问题）。"""
        envelope = Envelope.fail(
            code=CODE_INTERNAL_ERROR,
            message=f"{type(exc).__name__}: {exc}",
            market=_market_for_request(request, resolved),
            version=__version__,
            data={"path": request.url.path},
        )
        return envelope_response(envelope)

    @app.get("/health", summary="存活探针")
    async def health() -> Response:
        """返回服务存活状态与已启用市场。

        ``markets_enabled`` 为**对象** ``Record<market_code, bool>``
        （契约 v1.1 R1.1-2 锁定）：config 中启用的市场为 ``true``，
        未启用为 ``false``。
        """
        from Kuantix.core.market import known_markets

        uptime = (dt.datetime.now().astimezone() - app.state.started_at).total_seconds()
        payload: dict[str, Any] = {
            "status": "ok",
            "started_at": app.state.started_at.isoformat(timespec="seconds"),
            "uptime_seconds": round(uptime, 3),
            "markets_enabled": {
                code: resolved.markets.is_enabled(code) for code in sorted(known_markets())
            },
        }
        return envelope_response(
            Envelope.ok(payload, market=market, version=__version__)
        )

    @app.get("/api/version", summary="版本与上游基座信息")
    async def version() -> Response:
        """返回 Kuantix 版本、上游 easy-tdx 锁定版本与配置来源。"""
        from Kuantix import UPSTREAM_EASY_TDX_VERSION

        payload: dict[str, Any] = {
            "name": resolved.app.name,
            "version": __version__,
            "upstream_easy_tdx": UPSTREAM_EASY_TDX_VERSION,
            "config_source": str(resolved.source),
            "market_default": resolved.markets.default,
        }
        return envelope_response(
            Envelope.ok(payload, market=market, version=__version__)
        )

    # 挂载 T05 业务路由（/api/v1/data、/api/v1/factor、/api/v1/screen）。
    # 延迟 import 避免 main ↔ api.server 的模块级循环依赖；
    # 组合根惰性装配，首个业务请求才创建数据目录（/health 零副作用）。
    from Kuantix.api.server import register_routes

    register_routes(app, resolved)

    return app


def run(config: Config | None = None) -> None:
    """按配置启动 uvicorn 服务（``Kuantix serve`` 的实现体）。

    Args:
        config: 显式配置；``None`` 时取进程级全局配置。

    Note:
        ``reload=True`` 时 uvicorn 要求以 import string 方式加载应用，
        因此这里传 ``"Kuantix.main:app_factory"`` 并开启 ``factory=True``；
        非 reload 模式直接传实例，避免重复加载配置。
    """
    import uvicorn

    resolved = config if config is not None else get_config()
    resolved.paths.ensure()

    if resolved.server.reload:
        uvicorn.run(
            "Kuantix.main:app_factory",
            factory=True,
            host=resolved.server.host,
            port=resolved.server.port,
            reload=True,
            log_level=resolved.app.log_level.lower(),
        )
        return

    uvicorn.run(
        create_app(resolved),
        host=resolved.server.host,
        port=resolved.server.port,
        workers=resolved.server.workers,
        log_level=resolved.app.log_level.lower(),
    )


def app_factory() -> FastAPI:
    """uvicorn ``--reload`` 模式使用的零参工厂。"""
    return create_app(get_config())
