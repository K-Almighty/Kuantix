"""Kuantix 命令行根骨架（Typer）—— ``Kuantix`` 可执行入口。

双入口契约（NF-10）
------------------
每条命令都支持全局 ``--json``：

* 不带 ``--json``：面向人的文本输出（表格化的键值对）。
* 带 ``--json``：输出统一信封 ``{code, message, data, meta}``（NF-9），
  经 :meth:`Envelope.to_json` 序列化，NaN/Inf → null、浮点 6 位（NF-12）。

无论哪种输出形态，**数据本身来自同一个 payload 构造函数**，杜绝两条入口
返回值漂移。

批次边界（T01）
--------------
本文件在批次 1 落地的是「根骨架 + 已具备能力的命令」：

===================  ==========================================
命令                  状态
===================  ==========================================
``Kuantix version``   ✅ 可用
``Kuantix doctor``    ✅ 可用（T01/T02 自检：上游版本/系数表/市场/路径）
``Kuantix serve``     ✅ 可用（起空 FastAPI，见 :mod:`Kuantix.main`）
``Kuantix config *``  ✅ 可用
``Kuantix data *``    ✅ 可用（T03：sync/verify/status/quarantine[list|remove]）
``Kuantix factor *``  ✅ 可用（T04/T05：list/compute/report/combine/models）
``Kuantix screen *``  ✅ 可用（T04/T05：run/list/results/export）
``Kuantix monitor *`` ✅ 可用（T05：start/stop/status/watchlist/rules/positions/alerts/channels）
===================  ==========================================

未落地的子命令**显式抛错并给出所属批次**，不返回空结果、不静默成功
（NF-26）。这样 ``Kuantix --help`` 能完整展示命令骨架，同时任何误用都会
立刻炸出来，而不是让上层以为"跑了但没数据"。

（T05 完成后 data/factor/screen/monitor 四组命令均已接入真实服务层；
``_pending`` 保留供未来新增未落地命令使用。）
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable, NoReturn

import typer

from Kuantix import UPSTREAM_EASY_TDX_VERSION, __version__
from Kuantix.config import Config, find_config_file, load_config, set_config
from Kuantix.core.envelope import (
    CODE_INTERNAL_ERROR,
    CODE_INVALID_ARGUMENT,
    CODE_NOT_FOUND,
    CODE_NOT_IMPLEMENTED,
    Envelope,
    Timer,
)
from Kuantix.core.fail_loud import (
    FailLoudError,
    NotSupportedError,
    KuantixError,
    UpstreamContractError,
)

__all__ = [
    "CLIState",
    "app",
    "build_doctor_payload",
    "main",
]

#: 未落地子命令 → 所属批次说明。集中登记，避免各命令各写各的话术。
_STAGE_OWNER: dict[str, str] = {
    "data": "T03（数据湖回补链路：DataLake / SyncOrchestrator / IntegrityVerifier）",
    "factor": "T04（因子库：FactorService / FactorStore / 评估与合成）",
    "screen": "T04（选股：ScreenService / 排序与导出）",
    "monitor": "T05（实时监控：MonitorLoop / RuleEngine / Notifier）",
}


# ---------------------------------------------------------------------------
# CLI 运行态
# ---------------------------------------------------------------------------


@dataclass
class CLIState:
    """一次 CLI 调用的运行态。

    Attributes:
        as_json: 是否输出 JSON 信封（``--json``）。
        market: 本次调用的市场码，贯穿到 ``meta.market``（NF-6）。
        config_path: ``--config`` 显式指定的配置文件路径。
        config: 已加载的配置；惰性加载，``version`` 这类命令不需要它。
    """

    as_json: bool = False
    market: str = "CN"
    config_path: Path | None = None
    config: Config | None = field(default=None, repr=False)

    def require_config(self) -> Config:
        """按需加载并缓存配置；加载成功后立即初始化 logging（P1-3）。

        Returns:
            已加载的 :class:`~Kuantix.config.Config`。

        Raises:
            MissingConfigError: 配置文件缺失或存在非法/缺失键（NF-26）。
        """
        if self.config is None:
            self.config = load_config(self.config_path)
            set_config(self.config)
            # P1-3：CLI 首次取配置时初始化 logging（幂等，不会重复）
            from Kuantix.logging_config import configure_logging

            configure_logging(self.config)
        return self.config


def _state(ctx: typer.Context) -> CLIState:
    """从 Typer 上下文取运行态；缺失时立即建一个默认态。

    Typer 在 ``--help`` 等路径上可能不经过根 callback，这里保证 ``ctx.obj``
    永远是 :class:`CLIState`，而不是让下游对 ``None`` 做属性访问。
    """
    if not isinstance(ctx.obj, CLIState):
        ctx.obj = CLIState()
    return ctx.obj


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _render_text(value: Any, indent: int = 0) -> list[str]:
    """把 payload 递归渲染成人类可读的缩进文本行。

    Args:
        value: 任意可序列化结构（dict / list / 标量）。
        indent: 当前缩进层级。

    Returns:
        文本行列表。
    """
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        if not value:
            lines.append(f"{pad}(空)")
            return lines
        width = max(len(str(k)) for k in value)
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.extend(_render_text(item, indent + 1))
            else:
                shown = "" if item is None else item
                if isinstance(item, list) and not item:
                    shown = "(空)"
                if isinstance(item, dict) and not item:
                    shown = "(空)"
                lines.append(f"{pad}{str(key).ljust(width)}  {shown}")
        return lines
    if isinstance(value, list):
        if not value:
            lines.append(f"{pad}(空)")
            return lines
        for item in value:
            if isinstance(item, (dict, list)):
                lines.extend(_render_text(item, indent + 1))
                lines.append("")
            else:
                lines.append(f"{pad}- {item}")
        return lines
    lines.append(f"{pad}{value}")
    return lines


def _emit(state: CLIState, envelope: Envelope) -> None:
    """按 ``--json`` 开关输出信封；失败信封写 stderr。

    Args:
        state: CLI 运行态。
        envelope: 待输出的信封。
    """
    stream = sys.stdout if envelope.is_ok else sys.stderr
    if state.as_json:
        print(envelope.to_json(indent=2), file=stream)
        return
    if not envelope.is_ok:
        print(f"[错误 {envelope.code}] {envelope.message}", file=stream)
        if envelope.data is not None:
            for line in _render_text(envelope.data):
                print(line, file=stream)
        return
    for line in _render_text(envelope.data):
        print(line, file=stream)


def _run(state: CLIState, builder: Callable[[], Any], *, data_date: str | None = None) -> None:
    """执行 payload 构造函数并输出成功信封；异常向上抛给 :func:`main` 统一处理。

    Args:
        state: CLI 运行态。
        builder: 无参 payload 构造函数。
        data_date: 数据基准日，写入 ``meta.data_date``。
    """
    with Timer() as timer:
        payload = builder()
    envelope = Envelope.ok(
        payload,
        market=state.market,
        version=__version__,
        elapsed_ms=timer.elapsed_ms,
        data_date=data_date,
    )
    _emit(state, envelope)


def _pending(group: str, command: str) -> NoReturn:
    """未落地子命令的统一显式失败（NF-26）。

    Args:
        group: 命令组名（``data`` / ``factor`` / ``screen`` / ``monitor``）。
        command: 完整命令串，用于错误消息。

    Raises:
        NotSupportedError: 总是抛出，说明该命令属于哪个批次。
    """
    if group not in _STAGE_OWNER:
        raise NotSupportedError(
            f"[fail-loud/NF-26] 命令 `{command}` 未登记所属批次，"
            f"已登记的命令组：{sorted(_STAGE_OWNER)}"
        )
    raise NotSupportedError(
        f"[未实现] 命令 `{command}` 属于 {_STAGE_OWNER[group]}，当前构建仅落地 "
        f"T01（项目基础设施）+ T02（核心契约层与适配层）。"
        f"这里显式报错而不是返回空结果，避免上层误判为「跑通但无数据」。"
    )


def _not_found(state: CLIState, message: str) -> NoReturn:
    """输出 404 失败信封并以非零码退出（CLI↔REST 对等，NF-10）。

    Args:
        state: CLI 运行态。
        message: 错误消息（含不存在的 job/batch/code 上下文）。

    Raises:
        typer.Exit: 总是以退出码 1 结束本次调用。
    """
    envelope = Envelope.fail(
        code=CODE_NOT_FOUND,
        message=message,
        market=state.market,
        version=__version__,
        data={"error_type": "NotFound"},
    )
    _emit(state, envelope)
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# 根命令
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="Kuantix",
    help=(
        "Kuantix —— 本地量化研究工作台（基于 easy-tdx 只读复用）。\n\n"
        "全局 --json 可让任意子命令输出统一信封 {code, message, data, meta}。"
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

config_app = typer.Typer(help="配置：查看解析结果 / 定位配置文件 / 生成模板。", no_args_is_help=True)
data_app = typer.Typer(help="数据湖（L1 vipdoc）：回补 / 校验 / 隔离区。[T03]", no_args_is_help=True)
factor_app = typer.Typer(help="因子库（L2 parquet）：计算 / 评估 / 合成。[T04]", no_args_is_help=True)
screen_app = typer.Typer(help="选股：条件筛选与排序导出。[T04]", no_args_is_help=True)
monitor_app = typer.Typer(help="实时监控：规则引擎与通知推送。[T05]", no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(data_app, name="data")
app.add_typer(factor_app, name="factor")
app.add_typer(screen_app, name="screen")
app.add_typer(monitor_app, name="monitor")


@app.callback()
def root(
    ctx: typer.Context,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="输出统一 JSON 信封（NF-9/NF-10）而非人类可读文本。"),
    ] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="显式指定 config.toml 路径（覆盖自动探测）。"),
    ] = None,
    market: Annotated[
        str,
        typer.Option("--market", "-m", help="市场码，贯穿全链路写入 meta.market（NF-6）。"),
    ] = "CN",
) -> None:
    """全局选项。所有子命令共享。"""
    ctx.obj = CLIState(
        as_json=as_json,
        market=market.strip().upper(),
        config_path=config_path,
    )


# ---------------------------------------------------------------------------
# version / doctor / serve
# ---------------------------------------------------------------------------


@app.command("version")
def version_cmd(ctx: typer.Context) -> None:
    """打印 Kuantix 版本与上游基座锁定版本。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        return {
            "Kuantix": __version__,
            "upstream_easy_tdx_pinned": UPSTREAM_EASY_TDX_VERSION,
            "python": sys.version.split()[0],
        }

    _run(state, build)


def build_doctor_payload(state: CLIState) -> dict[str, Any]:
    """构造 ``Kuantix doctor`` 的体检 payload（T01/T02 自检面）。

    体检项覆盖批次 1 的全部关键红线，任何一项不满足都会抛出对应的
    fail-loud 异常，由 :func:`main` 转成非零退出码：

    * 上游 easy-tdx 实际版本 vs 锁定版本（NF-25 系数表引用的前提）；
    * ``_SECURITY_COEFFICIENTS`` 是 import 引用且键集合完整（陷阱 T1）；
    * 市场注册表：CN 可用、HK/US 占位（NF-5/NF-7）；
    * 数据目录与 ``~/.easy_tdx`` 的隔离性（NF-1/NF-18）；
    * ``~/.easy_tdx/config.json`` 的只读指纹（NF-20）。

    Args:
        state: CLI 运行态（提供配置）。

    Returns:
        体检结果字典。
    """
    from Kuantix.adapters.coefficients import (
        assert_upstream_version,
        known_security_types,
        upstream_coefficient_table,
    )
    from Kuantix.adapters.known_hosts import EASY_TDX_CONFIG_PATH, fingerprint
    from Kuantix.core.market import MARKET_REGISTRY, known_markets

    config = state.require_config()
    upstream = assert_upstream_version()
    table = upstream_coefficient_table()

    markets: dict[str, Any] = {}
    for code in sorted(known_markets()):
        profile = MARKET_REGISTRY.get(code)
        detail = profile.describe()
        detail["enabled_in_config"] = config.markets.is_enabled(code)
        markets[code] = detail

    easy_tdx_home = (Path.home() / ".easy_tdx").resolve()
    paths: dict[str, Any] = {}
    for name in ("root", "vipdoc", "factors", "db", "logs", "reports", "exports"):
        directory = getattr(config.paths, name)
        paths[name] = {
            "path": str(directory),
            "exists": directory.is_dir(),
            "isolated_from_easy_tdx": (
                directory != easy_tdx_home and easy_tdx_home not in directory.parents
            ),
        }

    upstream_config = fingerprint(EASY_TDX_CONFIG_PATH)
    return {
        "Kuantix_version": __version__,
        "config_source": str(config.source),
        "upstream": {
            "package": "easy-tdx",
            "pinned": UPSTREAM_EASY_TDX_VERSION,
            "installed": upstream,
            "coefficient_source": "import from easy_tdx.offline.daily_bar（NF-25 引用非复制）",
            "coefficient_types": sorted(known_security_types()),
            "coefficient_count": len(table),
        },
        "markets": markets,
        "paths": paths,
        "upstream_config_readonly": upstream_config.to_dict(),
    }


@app.command("doctor")
def doctor_cmd(ctx: typer.Context) -> None:
    """环境体检：上游版本、系数表、市场注册表、数据目录隔离、上游配置只读性。"""
    state = _state(ctx)
    _run(state, lambda: build_doctor_payload(state))


@app.command("serve")
def serve_cmd(
    ctx: typer.Context,
    host: Annotated[
        str | None,
        typer.Option("--host", help="覆盖 [server].host。"),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", "-p", help="覆盖 [server].port。"),
    ] = None,
    no_reload: Annotated[
        bool,
        typer.Option("--no-reload", help="关闭热重载（生产/稳定模式，配合 --workers 多进程）。"),
    ] = False,
    workers: Annotated[
        int | None,
        typer.Option("--workers", help="uvicorn worker 进程数（多进程隔离重计算，避免单进程 GIL 饿死页面；reload 模式下被忽略）。"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="只构建应用并打印路由清单，不实际监听端口。"),
    ] = False,
) -> None:
    """启动 REST 服务（T01 骨架：/health、/api/version、/docs）。"""
    state = _state(ctx)
    config = state.require_config()

    from dataclasses import replace

    # --no-reload 显式关闭；否则沿用配置 [server].reload
    reload = False if no_reload else config.server.reload
    server = config.server
    effective = replace(
        server,
        host=server.host if host is None else host,
        port=server.port if port is None else port,
        reload=reload,
        workers=workers if workers is not None else server.workers,
    )
    config = replace(config, server=effective)
    state.config = config
    set_config(config)

    from Kuantix.main import create_app, run

    if dry_run:

        def build() -> dict[str, Any]:
            application = create_app(config)
            routes: list[str] = []
            # app.routes 里业务路由是 _IncludedRouter 惰性占位（path=None），
            # 直接遍历会漏掉业务端点；改从 OpenAPI schema 枚举（功能完整挂载的
            # 事实来源，TestClient/OpenAPI 实证可达，QA P2）。
            spec = application.openapi()
            # openapi() 恒含 "paths" 键（FastAPI 契约），直接取，不做静默兜底（R4）
            for path, operations in spec["paths"].items():
                methods = sorted(
                    m.upper()
                    for m in operations
                    if m in ("get", "post", "put", "delete", "patch", "options", "head")
                )
                routes.append(f"{','.join(methods)} {path}")
            return {
                "mode": "dry-run",
                "host": effective.host,
                "port": effective.port,
                "reload": effective.reload,
                "cors_origins": list(effective.cors_origins),
                "routes": sorted(routes),
            }

        _run(state, build)
        return

    banner = f"Kuantix {__version__} · http://{effective.host}:{effective.port} · docs /docs"
    print(banner, file=sys.stderr)
    run(config)


# ---------------------------------------------------------------------------
# config 组
# ---------------------------------------------------------------------------


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """打印解析后的完整配置（含环境变量覆盖结果）。"""
    state = _state(ctx)
    _run(state, lambda: state.require_config().to_dict())


@config_app.command("path")
def config_path_cmd(ctx: typer.Context) -> None:
    """打印本次将要加载的配置文件路径及探测顺序。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        resolved = find_config_file(state.config_path)
        return {
            "resolved": str(resolved),
            "explicit": str(state.config_path) if state.config_path else None,
            "env_var": "Kuantix_CONFIG",
        }

    _run(state, build)


@config_app.command("init")
def config_init(
    ctx: typer.Context,
    target: Annotated[
        Path,
        typer.Option("--target", "-t", help="模板输出路径。"),
    ] = Path("config.toml"),
    force: Annotated[
        bool,
        typer.Option("--force", help="目标已存在时覆盖。"),
    ] = False,
) -> None:
    """把内置配置模板写到指定路径。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        bundled = Path(__file__).resolve().parent / "resources" / "config.default.toml"
        if not bundled.is_file():
            raise KuantixError(
                f"[fail-loud] 内置配置模板缺失：{bundled}。"
                f"请检查打包配置 [tool.hatch.build.targets.wheel.force-include]"
            )
        destination = target.expanduser().resolve()
        if destination.exists() and not force:
            raise FailLoudError(
                f"[fail-loud/NF-26] 目标已存在：{destination}。"
                f"拒绝静默覆盖，确认后请加 --force"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
        return {
            "written": str(destination),
            "source": str(bundled),
            "bytes": destination.stat().st_size,
        }

    _run(state, build)


# ---------------------------------------------------------------------------
# data 组（T03）
# ---------------------------------------------------------------------------


@data_app.command("sync")
def data_sync(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
    years: Annotated[int, typer.Option("--years", help="回溯年数（仅全量）。")] = 10,
    workers: Annotated[int | None, typer.Option("--workers", help="并发 worker 数。")] = None,
    background: Annotated[
        bool, typer.Option("--background", help="后台运行，立即返回句柄不等待。")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="跳过交易时段禁全量回补的软限制（NF-28）。")
    ] = False,
    incremental: Annotated[
        bool,
        typer.Option("--incremental", help="增量回补（按已有文件最后日期续拉；契约 v1.4）。"),
    ] = False,
) -> None:
    """全市场日线回补到 ``~/.Kuantix/vipdoc/``（断点续传）；``--incremental`` 走增量。"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        import datetime as _dt

        from Kuantix.data.datalake import DataLake
        from Kuantix.data.sync_state import SyncStateStore

        lake = DataLake(config)
        if incremental:
            handle = lake.sync_incremental(market, workers=workers, force=force)
            sync_kind = "incremental"
        else:
            handle = lake.sync_full(market, years, workers=workers, force=force)
            sync_kind = "full"
        if background:
            return {
                "mode": "background",
                "sync_kind": sync_kind,
                "market": market,
                "years": years,
                "handle": handle.to_dict(),
            }
        result = handle.wait()
        if handle.status == "failed":
            raise FailLoudError(f"[fail-loud] 回补失败: {handle.error}")
        if handle.status == "done" and result is not None:
            # 契约 v1.4：手动同步成功后写 sync_state（trigger=manual），
            # 保证 D1 last_sync 是「任意来源最后一次同步」（D2.6）。
            SyncStateStore(config.paths.db).update(
                at=_dt.datetime.now().astimezone(),
                status="done",
                trigger="manual",
                result=result.to_dict(),
            )
        return {
            "mode": "foreground",
            "sync_kind": sync_kind,
            "market": market,
            "years": years,
            "status": handle.status,
            "result": result.to_dict() if result else None,
        }

    _run(state, build)


@data_app.command("verify")
def data_verify(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
) -> None:
    """完整性校验：回读比对 + 输出隔离区清单。[T03]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.data.datalake import DataLake

        lake = DataLake(config)
        report = lake.verify(market)
        return {
            **report.to_dict(),
            # D1/D5 同口径：区分 SQLite 主存储与 vipdoc 镜像覆盖（storage.source）
            "storage": lake.storage_status(market),
        }

    _run(state, build)


@data_app.command("status")
def data_status(ctx: typer.Context) -> None:
    """查看本地数据覆盖情况与隔离区数量。[T03]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.data.datalake import DataLake

        lake = DataLake(config)
        report = lake.verify()
        quarantined = lake.list_quarantine()
        return {
            # 合并 coverage：任一后端（SQLite/镜像）有数据即非空（D1 修复）
            "coverage": lake.merged_coverage_from_report(report, "CN"),
            # 存储摘要：SQLite 行数 + 镜像文件数 + source 四态
            "storage": lake.storage_status("CN"),
            "missing_days": len(report.missing_days),
            "corrupt": report.corrupt,
            "quarantine_count": len(quarantined),
            "quarantine_reasons": sorted({q.reason for q in quarantined}),
        }

    _run(state, build)


@data_app.command("migrate")
def data_migrate(
    ctx: typer.Context,
    catalog: Annotated[
        Path | None,
        typer.Option("--catalog", help="旧 security_catalog.json 路径（缺省自动探测）。"),
    ] = None,
    verify: Annotated[
        bool, typer.Option("--verify", help="导入后抽样往返比对（D4）。")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只扫描不写库（安全演练）。")
    ] = False,
    verify_sample: Annotated[
        int, typer.Option("--verify-sample", help="往返比对抽样条数。")
    ] = 5,
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
) -> None:
    """一次性迁移 vipdoc → SQLite market.db（显式触发，D3）。[T02]

    - 日线解码导入 ``daily_bars``（只存解码值，D4）；
    - ``--catalog`` 导入旧证券清单 JSON → ``securities`` 表（D9）；
    - 旧断点 JSON → ``sync_checkpoint`` 表（D6）；
    - ``--verify`` 抽样往返比对（镜像 vs SQLite）。
    """
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.data.market_store import MarketStore
        from Kuantix.data.migrate import Migrator

        store = MarketStore(config.paths.db / config.storage.market_db)
        migrator = Migrator(store, vipdoc_root=config.paths.vipdoc)
        # catalog：显式 --catalog 必须存在（缺省自动探测，不存在则跳过）
        catalog_path: Path | None = None
        if catalog is not None:
            catalog_path = Path(catalog).expanduser()
        else:
            default_catalog = config.paths.db / "security_catalog.json"
            if default_catalog.is_file():
                catalog_path = default_catalog
        report = migrator.migrate(
            catalog_path=catalog_path,
            dry_run=dry_run,
            verify=verify,
            verify_sample=verify_sample,
            market=market,
        )
        return report.to_dict()

    _run(state, build)


securities_app = typer.Typer(
    help="本地证券清单：查看 / 状态 / 显式更新（唯一允许枚举的另一显式入口）。",
    no_args_is_help=True,
)

data_app.add_typer(securities_app, name="securities")


@securities_app.command("list")
def securities_list(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
    limit: Annotated[int, typer.Option("--limit", help="返回条数上限。")] = 50,
    q: Annotated[str, typer.Option("--q", help="代码/名称过滤。")] = "",
) -> None:
    """列出本地证券清单（读 SQLite securities 表，零网络）。"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.data.market_store import MarketStore

        store = MarketStore(config.paths.db / config.storage.market_db)
        if q:
            secs = store.search_securities(q, market, limit=limit)
        else:
            secs = store.list_securities(market)[:limit]
        return {
            "count": len(secs),
            "items": [s.to_dict() for s in secs],
        }

    _run(state, build)


@securities_app.command("status")
def securities_status(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
) -> None:
    """查看本地证券清单规模与更新时间（读 SQLite，零网络）。"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.data.market_store import MarketStore

        store = MarketStore(config.paths.db / config.storage.market_db)
        secs = store.list_securities(market)
        return {
            "market": market,
            "count": len(secs),
            "updated_at": store.securities_updated_at(market),
            "storage": store.summary(),
        }

    _run(state, build)


@securities_app.command("update")
def securities_update(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
) -> None:
    """显式触发一次网络枚举并把清单落 SQLite（等价 data sync 的清单部分）。

    这是除 ``data sync`` 外**唯一允许枚举的显式入口**（设计文档 08 §2）。
    """
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.data.datalake import DataLake

        lake = DataLake(config)
        result = lake.sync_securities_only(market)
        return result

    _run(state, build)


schedule_app = typer.Typer(
    help="增量同步调度：手动触发一次 / 查看调度状态（契约 v1.4，D2.7）。",
    no_args_is_help=True,
)

data_app.add_typer(schedule_app, name="schedule")


def _build_cli_scheduler(config: Config):
    """构造 CLI 用的调度器（不常驻；仅手动触发/状态查询，与 serve 内置解耦）。"""
    from Kuantix.data.datalake import DataLake
    from Kuantix.data.sync_state import SyncStateStore
    from Kuantix.scheduler import IncrementalSyncScheduler

    lake = DataLake(config)
    state = SyncStateStore(config.paths.db)
    return IncrementalSyncScheduler(config, lake, state)


@schedule_app.command("run-once")
def schedule_run_once(ctx: typer.Context) -> None:
    """手动触发一次「调度判定 + 增量同步」（等价测试钩子，D2.7）。

    - 非交易日 / 交易时段内 / 数据湖为空 → 记 skipped 并说明原因（不触网）；
    - 条件满足 → 执行 ``sync_incremental`` 并更新 ``sync_state``。
    """
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        scheduler = _build_cli_scheduler(config)
        return scheduler.run_once("manual")

    _run(state, build)


@schedule_app.command("status")
def schedule_status(ctx: typer.Context) -> None:
    """查看调度配置、运行态、下次触发时间与最近一次同步（D1 schedule 视图）。"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        scheduler = _build_cli_scheduler(config)
        return scheduler.status()

    _run(state, build)


quarantine_app = typer.Typer(
    help="隔离区管理：查看 / 移除（D6/D7 等价）。[T03/T05]",
    no_args_is_help=False,
    invoke_without_command=True,
)

data_app.add_typer(quarantine_app, name="quarantine")


def _quarantine_list_payload(state: CLIState) -> dict[str, Any]:
    """构造隔离区清单载荷（``Kuantix data quarantine`` / ``list`` 共用）。"""
    from Kuantix.data.datalake import DataLake

    lake = DataLake(state.require_config())
    entries = lake.list_quarantine(state.market)
    return {
        "count": len(entries),
        "entries": [e.to_dict() for e in entries],
    }


@quarantine_app.callback()
def quarantine_root(ctx: typer.Context) -> None:
    """无子命令时列出隔离区（兼容旧用法 ``Kuantix data quarantine``）。"""
    if ctx.invoked_subcommand is not None:
        return
    state = _state(ctx)
    _run(state, lambda: _quarantine_list_payload(state))


@quarantine_app.command("list")
def quarantine_list_cmd(ctx: typer.Context) -> None:
    """列出被隔离的标的及隔离原因（D6 等价）。"""
    state = _state(ctx)
    _run(state, lambda: _quarantine_list_payload(state))


@quarantine_app.command("remove")
def quarantine_remove(
    ctx: typer.Context,
    code: Annotated[str, typer.Option("--code", help="标的代码。")],
) -> None:
    """移除隔离区条目（D7 等价，NF-10）。[T05]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.data.datalake import DataLake

        lake = DataLake(config)
        entries = lake.list_quarantine(state.market)
        entry = None
        for candidate in entries:
            if candidate.code == code:
                entry = candidate
                break
        if entry is None:
            _not_found(state, f"隔离区条目不存在: {code}")
        removed = lake.remove_quarantine(code, state.market)
        return {"removed": code, "reason": entry.reason, "deleted": removed}

    _run(state, build)


# ---------------------------------------------------------------------------
# factor 组（T04）
# ---------------------------------------------------------------------------


@factor_app.command("list")
def factor_list(ctx: typer.Context) -> None:
    """列出已注册因子（含 ``factors/`` 目录下的自定义因子）。[T04]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.factor.service import FactorService

        service = FactorService(config)
        return {"factors": service.list_factors(), "models": service.list_models()}

    _run(state, build)


@factor_app.command("compute")
def factor_compute(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", help="因子名；留空表示全部。")] = "",
    pool: Annotated[str, typer.Option("--pool", help="代码池，逗号分隔；留空表示全部。")] = "",
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
    start: Annotated[str, typer.Option("--start", help="起始日期 YYYY-MM-DD。")] = "2020-01-01",
    end: Annotated[str, typer.Option("--end", help="结束日期 YYYY-MM-DD。")] = "2025-12-31",
    force: Annotated[bool, typer.Option("--force", help="忽略已算区间强制重算。")] = False,
) -> None:
    """计算因子并落 L2 parquet（全程读本地，不走网络）。[T04]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        import datetime as dt

        from Kuantix.factor.service import ComputeRequest, FactorService

        service = FactorService(config)
        factors = tuple(f.strip() for f in name.split(",")) if name else service.list_factors()
        codes = tuple(c.strip() for c in pool.split(",")) if pool else None
        req = ComputeRequest(
            market=market,
            factors=factors,
            start=dt.date.fromisoformat(start),
            end=dt.date.fromisoformat(end),
            codes=codes,
            force=force,
        )
        jobs = service.compute_factors(req)
        return {
            "market": market,
            "jobs": [j.to_dict() for j in jobs],
            "factors": factors,
        }

    _run(state, build)


@factor_app.command("report")
def factor_report(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", help="因子名。")] = "momentum_20d",
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
) -> None:
    """输出 IC / IR / 分层回测报告。[T04]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.factor.service import FactorService

        service = FactorService(config)
        report = service.report(name, market=market)
        return {"factor": name, "report": report}

    _run(state, build)


@factor_app.command("combine")
def factor_combine(
    ctx: typer.Context,
    factors: Annotated[str, typer.Option("--factors", help="待合成因子，逗号分隔。")] = "momentum_20d,reversal_5d",
    method: Annotated[str, typer.Option("--method", help="合成方法 equal/ic/ir。")] = "equal",
    name: Annotated[str | None, typer.Option("--name", help="模型名。")] = None,
    save_model: Annotated[bool, typer.Option("--save-model", help="保存合成模型。")] = False,
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
) -> None:
    """多因子合成并可选保存模型句柄。[T04]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.factor.service import FactorService

        service = FactorService(config)
        factor_list = tuple(f.strip() for f in factors.split(",") if f.strip())
        handle = service.combine(
            factor_list, method, name=name, save_model=save_model, market=market
        )
        return {
            "model": handle.to_dict(),
            "saved": save_model,
            "models": service.list_models(),
        }

    _run(state, build)


@factor_app.command("models")
def factor_models(ctx: typer.Context) -> None:
    """列出已保存的合成模型（F6 等价，NF-10）。[T05]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.factor.service import FactorService

        service = FactorService(config)
        items = [handle.to_dict() for handle in service.list_model_handles()]
        total = len(items)
        return {
            "items": items,
            "page": 1,
            "page_size": total if total else 1,
            "total": total,
            "total_pages": 1 if total else 0,
        }

    _run(state, build)


# ---------------------------------------------------------------------------
# screen 组（T04 / T05）
# ---------------------------------------------------------------------------


@screen_app.command("run")
def screen_run(
    ctx: typer.Context,
    top: Annotated[int, typer.Option("--top", help="输出前 N 名。")] = 50,
    market: Annotated[str, typer.Option("--market", help="市场码。")] = "CN",
    model: Annotated[str | None, typer.Option("--model", help="模型名；留空等权。")] = None,
    tech_cond: Annotated[str, typer.Option("--tech-cond", help="技术过滤条件 JSON。")] = "{}",
    chanlun_cond: Annotated[str, typer.Option("--chanlun-cond", help="缠论过滤条件 JSON。")] = "{}",
) -> None:
    """按条件选股并输出排序清单。[T04]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        import json

        from Kuantix.core.envelope import sanitize
        from Kuantix.factor.service import FactorService
        from Kuantix.screen.service import ScreenRequest, ScreenService

        tech = json.loads(tech_cond)
        chanlun = json.loads(chanlun_cond)
        factor_service = FactorService(config)
        screen = ScreenService(
            config,
            store=factor_service.store,
            model_loader=factor_service.load_model,
        )
        req = ScreenRequest(
            market=market,
            model_name=model,
            top_n=top,
            tech_cond=tech,
            chanlun_cond=chanlun,
        )
        results = screen.run(req)
        return {
            "market": market,
            "count": len(results),
            "results": [sanitize(r.to_dict()) for r in results],
        }

    _run(state, build)


@screen_app.command("list")
def screen_list(ctx: typer.Context) -> None:
    """列出内置筛选条件插件。[T04]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.screen.service import ScreenService

        screen = ScreenService(config)
        return screen.list_conditions()

    _run(state, build)


@screen_app.command("results")
def screen_results(
    ctx: typer.Context,
    batch_id: Annotated[
        str, typer.Option("--batch-id", "--batch", help="批次 id。")
    ],
) -> None:
    """查看某批次选股结果（S5 等价，一次返回全部，NF-10）。[T05]"""
    state = _state(ctx)
    config = state.require_config()

    def build() -> dict[str, Any]:
        from Kuantix.screen.service import ScreenService

        screen = ScreenService(config)
        payload = screen.export_json_payload(batch_id)
        if payload is None:
            _not_found(state, f"batch 不存在: {batch_id}")
        return payload

    _run(state, build)


@screen_app.command("export")
def screen_export(
    ctx: typer.Context,
    batch_id: Annotated[
        str, typer.Option("--batch-id", "--batch", help="批次 id。")
    ],
    format: Annotated[
        str, typer.Option("--format", help="导出格式 json/csv。")
    ] = "json",
) -> None:
    """导出批次结果（S6 等价，NF-10）。

    - ``--format json``：输出信封（与 ``screen results`` 同一 schema）；
    - ``--format csv``：直接输出 GBK 字节到 stdout（``> out.csv`` 落盘），
      ``--json`` 时输出元信息信封。
    """
    state = _state(ctx)
    config = state.require_config()

    from Kuantix.core.fail_loud import MissingKeyError
    from Kuantix.screen.service import ScreenService

    format_code = str(format).strip().lower()
    if format_code not in ("json", "csv"):
        raise MissingKeyError(
            f"[fail-loud/NF-26] 导出格式非法: {format_code!r}（期望 json/csv）"
        )
    screen = ScreenService(config)
    batch = screen.get_batch(batch_id)
    if batch is None:
        _not_found(state, f"batch 不存在: {batch_id}")

    if format_code == "json":
        def build() -> dict[str, Any]:
            payload = screen.export_json_payload(batch_id)
            if payload is None:
                _not_found(state, f"batch 不存在: {batch_id}")
            return payload

        _run(state, build, data_date=batch["as_of"])
        return

    data = screen.export_csv_bytes(batch_id)
    if state.as_json:
        envelope = Envelope.ok(
            {
                "batch_id": batch_id,
                "format": "csv",
                "bytes": len(data),
                "encoding": "gbk",
                "as_of": batch["as_of"],
            },
            market=state.market,
            version=__version__,
            data_date=batch["as_of"],
        )
        _emit(state, envelope)
        return
    sys.stdout.buffer.write(data)
    if not data.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")


# ---------------------------------------------------------------------------
# monitor 组（T05）
# ---------------------------------------------------------------------------


def _monitor_bundle(state: CLIState) -> tuple[Any, Any, Any, Any, Any]:
    """构造监控组件（P0 修复：复用 Kuantix.monitor.build_monitor_components 单一入口，
    与 REST 组合根同一装配逻辑，消除漂移风险）。

    Returns:
        ``(loop, engine, tracker, store, notifier)``。
    """
    from Kuantix.monitor import build_monitor_components

    config = state.require_config()
    return build_monitor_components(config, market=state.market)


@monitor_app.command("start")
def monitor_start_cmd(ctx: typer.Context) -> None:
    """启动实时监控轮询（后台线程，独立连接，NF-28）。[T05]"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        from Kuantix.core.fail_loud import DataIntegrityError

        loop = _monitor_bundle(state)[0]
        if not loop.watchlist_codes(state.market):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 无自选无法启动监控（{state.market}），"
                f"请先 Kuantix monitor watchlist add"
            )
        return loop.start()

    _run(state, build)


@monitor_app.command("stop")
def monitor_stop_cmd(ctx: typer.Context) -> None:
    """优雅停止监控循环。[T05]"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        loop = _monitor_bundle(state)[0]
        return loop.stop()

    _run(state, build)


@monitor_app.command("status")
def monitor_status_cmd(ctx: typer.Context) -> None:
    """查看监控循环状态与轮询健康度（M3 等价）。[T05]"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        loop = _monitor_bundle(state)[0]
        return loop.status().to_dict()

    _run(state, build)


@monitor_app.command("alerts")
def monitor_alerts_cmd(
    ctx: typer.Context,
    level: Annotated[
        str | None,
        typer.Option("--level", help="告警级别 info/warning/critical。"),
    ] = None,
) -> None:
    """查看告警历史（M15 等价，最近 200 条）。[T05]"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        from Kuantix.core.fail_loud import MissingKeyError

        level_value = str(level).strip().lower() if level else None
        if level_value is not None and level_value not in ("info", "warning", "critical"):
            raise MissingKeyError(
                f"[fail-loud/NF-26] 告警级别非法: {level!r}（期望 info/warning/critical）"
            )
        store = _monitor_bundle(state)[3]
        alerts = store.list_alerts(market=state.market, level=level_value, limit=200)
        return {"count": len(alerts), "items": [a.to_dict() for a in alerts]}

    _run(state, build)


@monitor_app.command("channels")
def monitor_channels_cmd(ctx: typer.Context) -> None:
    """查看推送通道状态（M16 等价）。[T05]"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        notifier = _monitor_bundle(state)[4]
        return {"items": notifier.channels_info()}

    _run(state, build)


# --- monitor watchlist 组 --------------------------------------------------


watchlist_app = typer.Typer(
    help="自选管理：查看 / 新增 / 删除（M4-M6 等价）。[T05]",
    no_args_is_help=False,
    invoke_without_command=True,
)

monitor_app.add_typer(watchlist_app, name="watchlist")


def _watchlist_payload(state: CLIState) -> dict[str, Any]:
    loop = _monitor_bundle(state)[0]
    items = [item.to_dict() for item in loop.list_watch(state.market)]
    return {"count": len(items), "items": items}


@watchlist_app.callback()
def watchlist_root(ctx: typer.Context) -> None:
    """无子命令时列出（兼容 ``Kuantix monitor watchlist``）。"""
    if ctx.invoked_subcommand is not None:
        return
    state = _state(ctx)
    _run(state, lambda: _watchlist_payload(state))


@watchlist_app.command("list")
def watchlist_list_cmd(ctx: typer.Context) -> None:
    """列出监控自选。"""
    state = _state(ctx)
    _run(state, lambda: _watchlist_payload(state))


@watchlist_app.command("add")
def watchlist_add_cmd(
    ctx: typer.Context,
    codes: Annotated[str, typer.Option("--code", "-c", help="代码，逗号分隔。")],
    name: Annotated[str, typer.Option("--name", help="证券名称（可选）。")] = "",
) -> None:
    """批量新增自选。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        loop = _monitor_bundle(state)[0]
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        if not code_list:
            raise FailLoudError("[fail-loud/NF-26] --code 不能为空")
        added: list[str] = []
        skipped: list[dict[str, Any]] = []
        for c in code_list:
            loop.add_watch(c, name=name, market=state.market, source="cli")
            added.append(c)
        return {"added": added, "skipped": skipped}

    _run(state, build)


@watchlist_app.command("remove")
def watchlist_remove_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Option("--code", "-c", help="标的代码。")],
) -> None:
    """删除自选。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        loop = _monitor_bundle(state)[0]
        removed = loop.remove_watch(code)
        if not removed:
            _not_found(state, f"自选不存在: {code}")
        return {"removed": code}

    _run(state, build)


# --- monitor rules 组 ------------------------------------------------------


rules_app = typer.Typer(
    help="预警规则管理：查看 / 新增 / 更新 / 删除（M8-M11 等价）。[T05]",
    no_args_is_help=False,
    invoke_without_command=True,
)

monitor_app.add_typer(rules_app, name="rules")


def _rules_payload(state: CLIState) -> dict[str, Any]:
    loop = _monitor_bundle(state)[0]
    items = [rule.to_dict() for rule in loop.list_rules(state.market)]
    return {"count": len(items), "items": items}


@rules_app.callback()
def rules_root(ctx: typer.Context) -> None:
    """无子命令时列出（兼容旧 ``Kuantix monitor rules``）。"""
    if ctx.invoked_subcommand is not None:
        return
    state = _state(ctx)
    _run(state, lambda: _rules_payload(state))


@rules_app.command("list")
def rules_list_cmd(ctx: typer.Context) -> None:
    """列出预警规则。"""
    state = _state(ctx)
    _run(state, lambda: _rules_payload(state))


@rules_app.command("add")
def rules_add_cmd(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", help="规则名。")],
    criterion_type: Annotated[
        str, typer.Option("--criterion-type", help="判据类型 price/indicator/stop_loss。")
    ],
    level: Annotated[
        str, typer.Option("--level", help="告警级别 info/warning/critical。")
    ],
    codes: Annotated[str, typer.Option("--codes", help="适用代码，逗号分隔；* 表示全部。")],
    params_json: Annotated[str, typer.Option("--params-json", help="判据参数 JSON。")] = "{}",
    cooldown: Annotated[float, typer.Option("--cooldown", help="冷却秒数。")] = 300.0,
) -> None:
    """新增预警规则。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        import json

        from Kuantix.core.fail_loud import MissingKeyError

        engine = _monitor_bundle(state)[1]
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        if not code_list:
            raise MissingKeyError("[fail-loud/NF-26] --codes 不能为空")
        if str(criterion_type).strip() not in ("price", "indicator", "stop_loss"):
            raise MissingKeyError(
                f"[fail-loud/NF-26] 判据类型非法: {criterion_type!r}"
            )
        if str(level).strip().lower() not in ("info", "warning", "critical"):
            raise MissingKeyError(f"[fail-loud/NF-26] 级别非法: {level!r}")
        params = json.loads(params_json)
        rule = engine.create_rule(
            name=name,
            market=state.market,
            codes=code_list,
            criterion_type=str(criterion_type).strip(),
            params=params,
            level=str(level).strip().lower(),
            cooldown_seconds=cooldown,
        )
        return rule.to_dict()

    _run(state, build)


@rules_app.command("update")
def rules_update_cmd(
    ctx: typer.Context,
    rule_id: Annotated[str, typer.Option("--id", help="规则 id。")],
    name: Annotated[str | None, typer.Option("--name", help="规则名。")] = None,
    enabled: Annotated[
        bool | None, typer.Option("--enabled/--disabled", help="启用/停用。")
    ] = None,
    level: Annotated[
        str | None, typer.Option("--level", help="告警级别。")
    ] = None,
    params_json: Annotated[
        str | None, typer.Option("--params-json", help="判据参数 JSON。")
    ] = None,
) -> None:
    """更新预警规则（部分字段）。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        import json

        from Kuantix.core.fail_loud import MissingKeyError

        loop, engine = _monitor_bundle(state)[0], _monitor_bundle(state)[1]
        if loop.get_rule(rule_id) is None:
            _not_found(state, f"规则不存在: {rule_id}")
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name
        if enabled is not None:
            fields["enabled"] = enabled
        if level is not None:
            if str(level).strip().lower() not in ("info", "warning", "critical"):
                raise MissingKeyError(f"[fail-loud/NF-26] 级别非法: {level!r}")
            fields["level"] = str(level).strip().lower()
        if params_json is not None:
            fields["params"] = json.loads(params_json)
        updated = engine.update_rule(rule_id, **fields)
        return updated.to_dict()

    _run(state, build)


@rules_app.command("remove")
def rules_remove_cmd(
    ctx: typer.Context,
    rule_id: Annotated[str, typer.Option("--id", help="规则 id。")],
) -> None:
    """删除预警规则。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        loop = _monitor_bundle(state)[0]
        removed = loop.delete_rule(rule_id)
        if not removed:
            _not_found(state, f"规则不存在: {rule_id}")
        return {"removed": rule_id}

    _run(state, build)


# --- monitor positions 组 --------------------------------------------------


positions_app = typer.Typer(
    help="持仓管理：查看 / 新增 / 删除（M12-M14 等价）。[T05]",
    no_args_is_help=False,
    invoke_without_command=True,
)

monitor_app.add_typer(positions_app, name="positions")


def _positions_payload(state: CLIState) -> dict[str, Any]:
    from Kuantix.api.deps import flat_position_view

    tracker = _monitor_bundle(state)[2]
    items = [flat_position_view(record) for record in tracker.list_positions(state.market)]
    return {"count": len(items), "items": items}


@positions_app.callback()
def positions_root(ctx: typer.Context) -> None:
    """无子命令时列出。"""
    if ctx.invoked_subcommand is not None:
        return
    state = _state(ctx)
    _run(state, lambda: _positions_payload(state))


@positions_app.command("list")
def positions_list_cmd(ctx: typer.Context) -> None:
    """列出持仓盈亏视图。"""
    state = _state(ctx)
    _run(state, lambda: _positions_payload(state))


@positions_app.command("add")
def positions_add_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Option("--code", help="标的代码。")],
    shares: Annotated[float, typer.Option("--shares", help="持仓数量（股）。")],
    cost_price: Annotated[float, typer.Option("--cost-price", help="成本价（元）。")],
    name: Annotated[str, typer.Option("--name", help="证券名称（可选）。")] = "",
    opened_at: Annotated[
        str | None, typer.Option("--opened-at", help="建仓日期 YYYY-MM-DD。")
    ] = None,
) -> None:
    """登记一笔持仓。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        import datetime as _dt

        from Kuantix.api.deps import flat_position_view
        from Kuantix.core.contracts import Position

        tracker = _monitor_bundle(state)[2]
        opened = (
            _dt.date.fromisoformat(opened_at)
            if opened_at
            else _dt.date.today()
        )
        tracker.add_position(
            Position(
                code=code,
                market=state.market,
                shares=shares,
                cost_price=cost_price,
                opened_at=opened,
            ),
            name=name,
        )
        return flat_position_view(tracker.get_record(code))

    _run(state, build)


@positions_app.command("remove")
def positions_remove_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Option("--code", help="标的代码。")],
) -> None:
    """删除持仓。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        tracker = _monitor_bundle(state)[2]
        removed = tracker.remove_position(code)
        if not removed:
            _not_found(state, f"持仓不存在: {code}")
        return {"removed": code}

    _run(state, build)


    _run(state, build)


# ---------------------------------------------------------------------------
# Analysis（盘前/盘后）子命令组
# ---------------------------------------------------------------------------


analysis_app = typer.Typer(
    help="盘前分析 / 盘后复盘：报告生成、消息面列表、基本面画像、涨跌停列表、Markdown/JSON 导出。",
    no_args_is_help=True,
)


def _analysis_bundle(state: CLIState) -> dict[str, Any]:
    """构造盘前/盘后 5 个组件（Store × 3 + Pre/Post），与 REST 同入口。

    CLI 侧在需要运行 analysis 相关命令时调用，按需创建 DataLake +
    FactorService（避免 data/因子子命令未执行时的零副作用）。
    """
    from Kuantix.api.deps import build_analysis_components
    from Kuantix.data.datalake import DataLake
    from Kuantix.data.market_store import MarketStore
    from Kuantix.factor.service import FactorService

    config = state.require_config()
    lake = DataLake(config)
    factor_service = FactorService(config)
    # monitor_store 可选：若能构造则注入（供基本面自选过滤、自选 PnL 用）
    monitor_store = None
    try:
        from Kuantix.monitor import build_monitor_components
        monitor_store = build_monitor_components(config, market=state.market)[3]
    except Exception:  # noqa: BLE001 - 缺 monitor 不影响 analysis 核心功能
        monitor_store = None
    # jobs 参数 CLI 端暂不需要（仅 serve 生命周期 JobManager 展示），占位 None
    return build_analysis_components(
        config,
        lake=lake,
        jobs=None,  # type: ignore[arg-type]
        factor_service=factor_service,
        monitor_store=monitor_store,
    )


def _parse_date_option(date_s: str | None, *, context: str) -> dt.date:
    if not date_s:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(str(date_s).strip())
    except ValueError as exc:
        from Kuantix.core.fail_loud import MissingKeyError
        raise MissingKeyError(
            f"[fail-loud/NF-26] {context}: --date 非法 {date_s!r}（期望 YYYY-MM-DD）"
        ) from exc


@analysis_app.command("pre-open")
def analysis_pre_open_run_cmd(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码（默认 CN）。")] = "CN",
    date: Annotated[str | None, typer.Option("--date", help="交易日 YYYY-MM-DD，默认今日。")] = None,
    codes: Annotated[str, typer.Option("--codes", help="显式代码集合（逗号分隔）；默认自选 + 抽样。")] = "",
) -> None:
    """运行盘前分析并输出报告。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        target_day = _parse_date_option(date, context="analysis pre-open run")
        comps = _analysis_bundle(state)
        codes_list = [c.strip() for c in str(codes).split(",") if c.strip()]
        report = comps["pre_open_service"].run_report(
            str(market).strip().upper(),
            target_day,
            codes=codes_list or None,
        )
        return report.to_dict()

    _run(state, build)


@analysis_app.command("post-close")
def analysis_post_close_run_cmd(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码（默认 CN）。")] = "CN",
    date: Annotated[str | None, typer.Option("--date", help="交易日 YYYY-MM-DD，默认今日。")] = None,
    force: Annotated[bool, typer.Option("--force", help="跳过收盘L1等待逻辑（不检查 lake 最新日期）。")] = False,
    codes: Annotated[str, typer.Option("--codes", help="显式代码集合（逗号分隔）；默认自选 + 抽样。")] = "",
) -> None:
    """运行盘后复盘并输出报告。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        target_day = _parse_date_option(date, context="analysis post-close run")
        comps = _analysis_bundle(state)
        codes_list = [c.strip() for c in str(codes).split(",") if c.strip()]
        report = comps["post_close_service"].run_report(
            str(market).strip().upper(),
            target_day,
            force=force,
            codes=codes_list or None,
        )
        return report.to_dict()

    _run(state, build)


@analysis_app.command("news")
def analysis_news_list_cmd(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码（默认 CN）。")] = "CN",
    date: Annotated[str | None, typer.Option("--date", help="交易日 YYYY-MM-DD，默认今日。")] = None,
    category: Annotated[str | None, typer.Option("--category", help="news / announcement / policy。")] = None,
    page: Annotated[int, typer.Option("--page", min=1, help="页码（1 起）。")] = 1,
    page_size: Annotated[int, typer.Option("--page-size", min=1, max=500, help="每页条数。")] = 20,
) -> None:
    """列出盘前消息面条目（分页）。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        from Kuantix.api.deps import db_page, page_limits

        target_day = _parse_date_option(date, context="analysis news list")
        comps = _analysis_bundle(state)
        store = comps["news_store"]
        market_code = str(market).strip().upper()
        limit, offset = page_limits(page, page_size)
        total = store.count(market_code, target_day, category=category)
        items = [
            i.to_dict()
            for i in store.list(
                market_code, target_day,
                category=category,
                limit=limit, offset=offset,
            )
        ]
        return db_page(items, total, page, page_size)

    _run(state, build)


@analysis_app.command("fundamentals")
def analysis_fundamentals_list_cmd(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码（默认 CN）。")] = "CN",
    date: Annotated[str | None, typer.Option("--date", help="画像基准日 YYYY-MM-DD，默认今日。")] = None,
    codes: Annotated[str, typer.Option("--codes", help="代码过滤（逗号分隔）。")] = "",
    grade: Annotated[str | None, typer.Option("--grade", help="等级过滤 A/B/C/D。")] = None,
    page: Annotated[int, typer.Option("--page", min=1, help="页码（1 起）。")] = 1,
    page_size: Annotated[int, typer.Option("--page-size", min=1, max=500, help="每页条数。")] = 20,
) -> None:
    """列出基本面画像（分页）。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        from Kuantix.api.deps import db_page, page_limits

        target_day = _parse_date_option(date, context="analysis fundamentals list")
        comps = _analysis_bundle(state)
        store = comps["fundamental_store"]
        market_code = str(market).strip().upper()
        codes_list = [c.strip() for c in str(codes).split(",") if c.strip()] or None
        limit, offset = page_limits(page, page_size)
        total = store.count(market_code, target_day, codes=codes_list, grade=grade)
        items = [
            p.to_dict()
            for p in store.list(
                market_code, target_day,
                codes=codes_list, grade=grade,
                limit=limit, offset=offset,
            )
        ]
        return db_page(items, total, page, page_size)

    _run(state, build)


# --- analysis post-close limit 列表子命令 ---

limit_app = typer.Typer(
    help="涨跌停条目：列出（分页，类型/行业过滤）。",
    no_args_is_help=True,
)

analysis_app.add_typer(limit_app, name="limit")


@limit_app.command("list")
def limit_list_cmd(
    ctx: typer.Context,
    market: Annotated[str, typer.Option("--market", help="市场码（默认 CN）。")] = "CN",
    date: Annotated[str | None, typer.Option("--date", help="交易日 YYYY-MM-DD，默认今日。")] = None,
    limit_type: Annotated[str | None, typer.Option("--type", help="涨停类型：业绩驱动 / 概念炒作 / 技术突破 / 新股上市 / ST摘帽 / 其他。")] = None,
    sector: Annotated[str | None, typer.Option("--sector", help="行业过滤。")] = None,
    only_up: Annotated[str, typer.Option("--side", help="up/down/all，默认 all。")] = "all",
    page: Annotated[int, typer.Option("--page", min=1, help="页码（1 起）。")] = 1,
    page_size: Annotated[int, typer.Option("--page-size", min=1, max=500, help="每页条数。")] = 50,
) -> None:
    """列出涨跌停条目（分页），附带当日汇总。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        from Kuantix.api.deps import db_page, page_limits

        target_day = _parse_date_option(date, context="analysis limit list")
        comps = _analysis_bundle(state)
        store = comps["limit_store"]
        market_code = str(market).strip().upper()
        only_flag: bool | None = None
        if str(only_up).strip().lower() in {"up", "1", "true", "y"}:
            only_flag = True
        elif str(only_up).strip().lower() in {"down", "0", "false", "n"}:
            only_flag = False
        limit, offset = page_limits(page, page_size)
        summary = store.get_summary(market_code, target_day)
        total = store.count(
            market_code, target_day,
            limit_type=limit_type, sector=sector, only_up=only_flag,
        )
        items = [
            e.to_dict()
            for e in store.list(
                market_code, target_day,
                limit_type=limit_type, sector=sector, only_up=only_flag,
                limit=limit, offset=offset,
            )
        ]
        return {
            "summary": None if summary is None else summary.to_dict(),
            "entries": db_page(items, total, page, page_size),
        }

    _run(state, build)


# --- analysis export 子命令 ---

export_app = typer.Typer(
    help="报告导出：盘前/盘后 Markdown 或 JSON 文件。",
    no_args_is_help=True,
)

analysis_app.add_typer(export_app, name="export")


@export_app.command("pre-open")
def export_pre_open_cmd(
    ctx: typer.Context,
    date: Annotated[str | None, typer.Option("--date", help="交易日 YYYY-MM-DD，默认今日。")] = None,
    market: Annotated[str, typer.Option("--market", help="市场码（默认 CN）。")] = "CN",
    fmt: Annotated[str, typer.Option("--format", help="md 或 json。")] = "md",
    output: Annotated[str, typer.Option("--output", help="输出目录，默认当前目录。")] = ".",
) -> None:
    """导出盘前分析报告为 Markdown 或 JSON 文件。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        from pathlib import Path

        from Kuantix.analysis.report import report_json_dict, report_markdown

        target_day = _parse_date_option(date, context="analysis export pre-open")
        comps = _analysis_bundle(state)
        report = comps["pre_open_service"].run_report(
            str(market).strip().upper(), target_day,
        )
        fmt_value = str(fmt).strip().lower()
        out_dir = Path(output).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        market_code = str(market).strip().upper()
        if fmt_value == "md":
            path = out_dir / f"pre-open-report-{market_code.lower()}-{target_day.isoformat()}.md"
            path.write_text(report_markdown(report), encoding="utf-8")
        elif fmt_value == "json":
            import json
            path = out_dir / f"pre-open-report-{market_code.lower()}-{target_day.isoformat()}.json"
            path.write_text(
                json.dumps(report_json_dict(report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            from Kuantix.core.fail_loud import MissingKeyError
            raise MissingKeyError(
                f"[fail-loud/NF-26] --format 仅支持 md|json，实际 {fmt!r}"
            )
        return {
            "file": str(path),
            "bytes": path.stat().st_size,
        }

    _run(state, build)


@export_app.command("post-close")
def export_post_close_cmd(
    ctx: typer.Context,
    date: Annotated[str | None, typer.Option("--date", help="交易日 YYYY-MM-DD，默认今日。")] = None,
    market: Annotated[str, typer.Option("--market", help="市场码（默认 CN）。")] = "CN",
    fmt: Annotated[str, typer.Option("--format", help="md 或 json。")] = "md",
    output: Annotated[str, typer.Option("--output", help="输出目录，默认当前目录。")] = ".",
    force: Annotated[bool, typer.Option("--force", help="跳过收盘L1等待逻辑。")] = False,
) -> None:
    """导出盘后复盘报告为 Markdown 或 JSON 文件。"""
    state = _state(ctx)

    def build() -> dict[str, Any]:
        from pathlib import Path

        from Kuantix.analysis.report import report_json_dict, report_markdown

        target_day = _parse_date_option(date, context="analysis export post-close")
        comps = _analysis_bundle(state)
        report = comps["post_close_service"].run_report(
            str(market).strip().upper(), target_day, force=force,
        )
        fmt_value = str(fmt).strip().lower()
        out_dir = Path(output).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        market_code = str(market).strip().upper()
        if fmt_value == "md":
            path = out_dir / f"post-close-report-{market_code.lower()}-{target_day.isoformat()}.md"
            path.write_text(report_markdown(report), encoding="utf-8")
        elif fmt_value == "json":
            import json
            path = out_dir / f"post-close-report-{market_code.lower()}-{target_day.isoformat()}.json"
            path.write_text(
                json.dumps(report_json_dict(report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            from Kuantix.core.fail_loud import MissingKeyError
            raise MissingKeyError(
                f"[fail-loud/NF-26] --format 仅支持 md|json，实际 {fmt!r}"
            )
        return {
            "file": str(path),
            "bytes": path.stat().st_size,
        }

    _run(state, build)


# analysis_app 定义在文件末尾，需在 main() 之前注册到 app
app.add_typer(analysis_app, name="analysis")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _click_exception_types(name: str) -> tuple[type[BaseException], ...]:
    """解析 click 异常类，同时覆盖 typer 自带的 vendored 副本。

    typer ``>=0.16`` 把 click 内联为 ``typer._click``，其 ``UsageError`` 与
    顶层 ``click.UsageError`` **不是同一个类对象**，直接 ``isinstance(exc,
    click.UsageError)`` 会漏判。这里从 ``typer.BadParameter``（公开符号）的
    MRO 反查 vendored 类，再并上顶层 ``click`` 的同名类。

    Args:
        name: 目标异常类名，如 ``"UsageError"`` / ``"ClickException"``。

    Returns:
        可用于 ``isinstance`` 的类元组。

    Raises:
        UpstreamContractError: typer 与 click 都找不到该异常类（依赖版本
            发生了不兼容变更，必须显式暴露而不是静默降级为通用 500）。
    """
    found: list[type[BaseException]] = []
    for cls in typer.BadParameter.__mro__:
        if cls.__name__ == name and issubclass(cls, BaseException):
            found.append(cls)
    try:
        import click as _click
    except ImportError:  # pragma: no cover - click 是 typer 的硬依赖
        _click = None  # type: ignore[assignment]
    if _click is not None and hasattr(_click, name):
        candidate = getattr(_click, name)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            if candidate not in found:
                found.append(candidate)
    if not found:
        raise UpstreamContractError(
            f"[fail-loud] 无法在 typer/click 中定位异常类 {name!r}；"
            f"typer={typer.__version__}。依赖版本不兼容，请修正 pyproject 约束"
        )
    return tuple(found)


#: click 用法错误（未知命令 / 缺参 / 参数类型错）→ 退出码 2
USAGE_ERRORS: tuple[type[BaseException], ...] = _click_exception_types("UsageError")
#: click 其余可展示异常 → 退出码 1
CLICK_ERRORS: tuple[type[BaseException], ...] = _click_exception_types("ClickException")


def _fail(state: CLIState, code: int, message: str, data: Any = None) -> int:
    """输出失败信封并返回进程退出码。"""
    envelope = Envelope.fail(
        code=code,
        message=message,
        market=state.market,
        version=__version__,
        data=data,
    )
    _emit(state, envelope)
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI 进程入口（``[project.scripts] Kuantix = "Kuantix.cli:main"``）。

    统一异常处理：把 fail-loud 异常翻译成失败信封 + 非零退出码，
    保证 ``--json`` 模式下即使失败也吐合法 JSON（NF-9/NF-10）。

    Args:
        argv: 参数列表；``None`` 表示取 ``sys.argv[1:]``。

    Returns:
        进程退出码：``0`` 成功，``1`` 业务/数据错误，``2`` 参数用法错误，
        ``130`` 用户 Ctrl+C。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    # 提前解析全局 --json：参数解析阶段就失败时（callback 尚未执行）也要能成型输出。
    state = CLIState(as_json="--json" in args)
    command = typer.main.get_command(app)
    try:
        # standalone_mode=False：click 不自行 sys.exit，把控制权交回这里统一成信封。
        # 正常返回值为命令返回值（本 CLI 全部返回 None）；命令内调用 ctx.exit(n)
        # 时 click 会把 n 作为返回值回传，故对 int 返回值做显式透传。
        result = command.main(args=args, prog_name="Kuantix", standalone_mode=False)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0
    except typer.Exit as exc:
        return int(exc.exit_code)
    except typer.Abort:
        return _fail(state, CODE_INVALID_ARGUMENT, "操作已中止")
    except KeyboardInterrupt:
        print("\n已被用户中断（Ctrl+C）", file=sys.stderr)
        return 130
    except NotSupportedError as exc:
        return _fail(state, CODE_NOT_IMPLEMENTED, str(exc),
                     data={"error_type": type(exc).__name__})
    except FailLoudError as exc:
        from Kuantix.main import _code_for_exception

        return _fail(state, _code_for_exception(exc), str(exc),
                     data={"error_type": type(exc).__name__})
    except KuantixError as exc:
        return _fail(state, CODE_INTERNAL_ERROR, str(exc),
                     data={"error_type": type(exc).__name__})
    except USAGE_ERRORS as exc:
        _fail(state, CODE_INVALID_ARGUMENT, f"用法错误：{exc}")
        return 2
    except CLICK_ERRORS as exc:
        return _fail(state, CODE_INVALID_ARGUMENT, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 —— 顶层兜底：转信封后仍以非零码退出
        return _fail(state, CODE_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}",
                     data={"error_type": type(exc).__name__})
    if isinstance(result, int) and result != 0:
        return result
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
