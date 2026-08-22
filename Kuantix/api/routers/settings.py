"""settings 路由（契约 §2.1f E1–E2，前缀 ``/api/v1/settings``，P2 只读）。

端点：
- E1 ``GET  /status`` —— 只读"数据源状态"（config 摘要 + known_hosts 只读列表
  + 数据湖摘要 + 版本信息），零网络；
- E2 ``POST /test-connection`` —— 主机连通性测试（**只测不写**：经
  :class:`~Kuantix.adapters.tdx_client.TdxClientFactory` 显式 host/port 新建连接，
  connect 即 close，不落盘任何 best host）。

NF-20（上游只读）硬约束（决策 D-5-B）
--------------------------------------
- 本路由**绝不提供**"切换服务器 / 保存配置"能力：上游 Settings 页的服务器切换会
  写 ``~/.easy_tdx/config.json``，Kuantix 不照搬；
- E1 只读 ``~/.easy_tdx/config.json``（NF-20 允许只读），读前后以 sha256 指纹
  （:func:`Kuantix.adapters.known_hosts.fingerprint` /
  :func:`~Kuantix.adapters.known_hosts.assert_untouched`）自证无写；
- E2 禁 from_best_host / save_best_*（R3），新建连接 ping 即关；
- 如需修改 host，引导用户改 Kuantix 自己的 config.toml（E1 只展示路径，不写）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from Kuantix import UPSTREAM_EASY_TDX_VERSION, __version__
from Kuantix.adapters import known_hosts as kh
from Kuantix.api.deps import ServiceContainer, get_services, respond
from Kuantix.api.schemas import TestConnectionRequest
from Kuantix.core.envelope import Timer
from Kuantix.core.fail_loud import MissingKeyError

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()

#: E2 连通性测试超时（秒）：短超时快速失败，不阻塞设置页
TEST_CONNECTION_TIMEOUT_S = 2.0


def _services(request: Request) -> ServiceContainer:
    """从应用状态取组合根（首个业务请求时惰性装配）。"""
    return get_services(request)


def _upstream_path() -> Any:
    """上游 ``~/.easy_tdx/config.json`` 路径。

    动态读取模块属性而非在 import 期绑定，便于测试注入临时路径。
    """
    return kh.EASY_TDX_CONFIG_PATH


def _tdx_summary(config: Any) -> dict[str, Any]:
    """config.toml ``[tdx]`` 段摘要（不含任何敏感信息）。"""
    tdx = config.tdx
    return {
        "use_easy_tdx_known_hosts": tdx.use_easy_tdx_known_hosts,
        "port": tdx.port,
        "ex_port": tdx.ex_port,
        "timeout_seconds": tdx.timeout_seconds,
        "mac_hosts": list(tdx.mac_hosts),
        "std_hosts": list(tdx.std_hosts),
        "mac_ex_hosts": list(tdx.mac_ex_hosts),
    }


def _config_summary(config: Any) -> dict[str, Any]:
    """config 摘要：数据路径 + 默认市场 + 端口（只展示，不写）。"""
    paths = config.paths
    return {
        "paths": {
            "root": str(paths.root),
            "vipdoc": str(paths.vipdoc),
            "factors": str(paths.factors),
            "db": str(paths.db),
            "logs": str(paths.logs),
            "reports": str(paths.reports),
            "exports": str(paths.exports),
        },
        "default_market": config.markets.default,
        "enabled_markets": list(config.markets.enabled()),
        "config_source": str(config.source),
        "tdx": _tdx_summary(config),
    }


def _host_rows(config: Any) -> dict[str, Any]:
    """**只读**收集节点清单：config.toml 显式节点为准，上游 known_hosts 追加。

    - 开启 ``use_easy_tdx_known_hosts`` 时，先按三级回退解析上游清单：
      环境变量 ``Kuantix__TDX__KNOWN_HOSTS_PATH`` > 用户主目录
      ``~/.easy_tdx/config.json`` > 项目内置兜底
      ``Kuantix/resources/easy_tdx_config.json``（容器 / 干净主机没有前者时
      自动回退，让部署不再依赖外部项目）；
    - 每次读取前后做 sha256 指纹自证（NF-20，读前 :func:`fingerprint`，
      读后 :func:`assert_untouched`），并在结果中如实标注上游来源与是否可用。
    """
    kwargs = {
        "std_hosts": config.tdx.std_hosts,
        "mac_hosts": config.tdx.mac_hosts,
        "mac_ex_hosts": config.tdx.mac_ex_hosts,
        "port": config.tdx.port,
        "ex_port": config.tdx.ex_port,
        "timeout_seconds": config.tdx.timeout_seconds,
    }
    upstream_path = _upstream_path()
    before = kh.fingerprint(upstream_path)
    upstream_available = before.exists
    if config.tdx.use_easy_tdx_known_hosts:
        # 不传 upstream_path：由 build_host_book 内部做三级回退
        # （env > 用户主目录 > 项目内置兜底），source 正确反映实际来源
        book = kh.build_host_book(use_easy_tdx_known_hosts=True, **kwargs)
    else:
        book = kh.build_host_book(use_easy_tdx_known_hosts=False, **kwargs)
    # 读前后指纹自证：本次读取过程没有产生任何写入（NF-20）
    kh.assert_untouched(before)
    rows: list[dict[str, Any]] = []
    for host in book.std_hosts:
        rows.append({"host": host, "port": book.port, "kind": "std", "read_only": True})
    for host in book.mac_hosts:
        rows.append({"host": host, "port": book.port, "kind": "mac", "read_only": True})
    for host in book.mac_ex_hosts:
        rows.append(
            {"host": host, "port": book.ex_port, "kind": "mac_ex", "read_only": True}
        )
    return {
        "items": rows,
        "upstream_available": upstream_available,
        "upstream_source": book.upstream_source,
        "known_hosts_merged": book.upstream_used,
        "upstream_config_untouched": True,
        "upstream_config_path": str(book.upstream_fingerprint.path)
        if book.upstream_fingerprint
        else str(upstream_path),
    }


def _require_tdx_factory(container: ServiceContainer) -> Any:
    """取客户端工厂；未装配时显式 400（fail-loud，不静默）。"""
    if container.tdx_factory is None:
        raise MissingKeyError(
            "[fail-loud/NF-26] 客户端工厂未装配（组合根缺失 tdx_factory）"
        )
    return container.tdx_factory


@router.get("/status", summary="数据源状态（E1，只读）")
async def settings_status(request: Request) -> Response:
    """返回**只读**数据源状态：config 摘要 + known_hosts + 数据湖摘要 + 版本。"""
    container = _services(request)
    market = container.config.markets.default
    with Timer() as timer:
        lake_status = container.lake.status(market)
        lake_status["latest_job"] = container.jobs.latest("data", market)
        payload: dict[str, Any] = {
            "read_only": True,
            "config": _config_summary(container.config),
            "known_hosts": _host_rows(container.config),
            "data": lake_status,
            "versions": {
                "Kuantix": __version__,
                "upstream_easy_tdx": UPSTREAM_EASY_TDX_VERSION,
            },
        }
    return respond(
        payload,
        market,
        data_date=lake_status.get("data_date"),
        elapsed_ms=timer.elapsed_ms,
    )


@router.post("/test-connection", summary="主机连通性测试（E2，只测不写）")
async def settings_test_connection(
    request: Request, body: TestConnectionRequest
) -> Response:
    """测试主机连通性：新建连接 ping 即关（**只测不写**）。

    - kind 非法 → Pydantic ``Literal`` 校验 → **400**（fail-loud）；
    - 连接失败 → 返回 ``{ok: false, error}`` 的 **code=0 信封**（业务结果，
      不是 HTTP 错误；fail-loud 体现在 ``error`` 字段）；
    - 全程不调用 from_best_host / save_best_*，不落盘任何 best host（NF-20）。
    """
    container = _services(request)
    factory = _require_tdx_factory(container)
    with Timer() as timer:
        result = factory.probe_connection(
            kind=body.kind,
            host=body.host,
            port=body.port,
            timeout=TEST_CONNECTION_TIMEOUT_S,
        )
    payload = {
        "ok": result["ok"],
        "host": body.host,
        "port": body.port,
        "kind": body.kind,
        "latency_ms": result.get("latency_ms"),
        "error": result.get("error"),
    }
    return respond(payload, container.config.markets.default, elapsed_ms=timer.elapsed_ms)
