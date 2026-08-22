"""配置加载（NF-16 / NF-20）。

加载顺序
--------
1. 显式传入的路径；
2. 环境变量 ``Kuantix_CONFIG`` 指向的文件；
3. 当前工作目录下的 ``config.toml``；
4. 源码树根目录的 ``config.toml``（``pip install -e .`` 场景）；
5. 包内置模板 ``Kuantix/resources/config.default.toml``（wheel 安装场景）。

环境变量覆盖
------------
``Kuantix__<SECTION>__<KEY>``（双下划线分隔，大小写不敏感）。
取值按模板中同名键的类型强制转换，转换失败**直接报错**（NF-26），
不会静默退化成字符串。

    Kuantix__SERVER__PORT=9001
    Kuantix__PATHS__ROOT=/tmp/Kuantix
    Kuantix__MARKETS__HK_ENABLED=false
    Kuantix__TDX__MAC_HOSTS=1.2.3.4,5.6.7.8   # 列表用逗号分隔

fail-loud 约定
--------------
配置项**缺失即报错**。本模块不提供任何"字段级默认值"：
模板 ``config.toml`` 就是唯一的默认值来源，缺键说明模板被改坏了，
必须显式修复而不是让程序带着未知配置继续跑。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from Kuantix import __version__
from Kuantix.core.fail_loud import (
    MissingConfigError,
    require_key,
    require_non_empty,
)

if sys.version_info >= (3, 11):  # pragma: no cover - 版本分支
    import tomllib
else:  # pragma: no cover - 版本分支
    import tomli as tomllib

__all__ = [
    "ENV_PREFIX",
    "ENV_CONFIG_PATH",
    "AppConfig",
    "PathsConfig",
    "ServerConfig",
    "TdxConfig",
    "SyncConfig",
    "MonitorConfig",
    "MarketsConfig",
    "FactorConfig",
    "ScreenConfig",
    "StorageConfig",
    "AnalysisConfig",
    "Config",
    "load_config",
    "get_config",
    "set_config",
    "reset_config",
    "find_config_file",
]

#: 环境变量覆盖前缀
ENV_PREFIX = "Kuantix__"
#: 指定配置文件路径的环境变量
ENV_CONFIG_PATH = "Kuantix_CONFIG"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_TEMPLATE = Path(__file__).resolve().parent / "resources" / "config.default.toml"


# ---------------------------------------------------------------------------
# 分节数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppConfig:
    """``[app]`` 节。"""

    name: str
    version: str
    log_level: str


#: PathsConfig 子路径名 → 默认子目录名（缺省时相对于 root 派生）
_PATHS_SUBDIR_NAMES: tuple[tuple[str, str], ...] = (
    ("vipdoc", "vipdoc"),
    ("factors", "factors"),
    ("db", "db"),
    ("logs", "logs"),
    ("reports", "reports"),
    ("exports", "exports"),
)


@dataclass(frozen=True)
class PathsConfig:
    """``[paths]`` 节：数据落盘根（NF-15/NF-18）。

    派生规则（P0 修复：子路径冗余消除）
    ----------------------------------
    ``root`` 必须显式提供；其余 6 个子路径（vipdoc/factors/db/logs/reports/exports）
    在 TOML / 环境变量中**可省略**。省略时自动派生为 ``root / <子目录名>``：

    * ``root = ~/.Kuantix`` → ``vipdoc = ~/.Kuantix/vipdoc`` 等。

    显式填写的子路径仍按原值生效（允许指向 root 之外，例如把 db 挂到独立 SSD）。
    全部路径在构造时已 ``expanduser()`` + ``resolve()``。
    """

    root: Path
    vipdoc: Path | None = None
    factors: Path | None = None
    db: Path | None = None
    logs: Path | None = None
    reports: Path | None = None
    exports: Path | None = None

    def __post_init__(self) -> None:
        """缺省子路径 → 从 root 派生；全部路径最终 expanduser + resolve。

        ``root`` 在传入前已由 ``_as_path`` 规范化；显式填写的子路径同样。
        需要派生的子路径在 frozen dataclass 中通过 ``object.__setattr__`` 写入。
        """
        # root 已由 _as_path 做过 expanduser/resolve，但再做一次双保险
        resolved_root = Path(self.root).expanduser().resolve()
        if resolved_root != self.root:
            object.__setattr__(self, "root", resolved_root)
        for attr_name, subdir in _PATHS_SUBDIR_NAMES:
            current = object.__getattribute__(self, attr_name)
            if current is None:
                derived = resolved_root / subdir
            else:
                derived = Path(current).expanduser().resolve()
            if derived != current:
                object.__setattr__(self, attr_name, derived)

    def all_dirs(self) -> tuple[Path, ...]:
        """返回全部受管目录。"""
        return (
            self.root,
            self.vipdoc,  # type: ignore[return-value]
            self.factors,
            self.db,
            self.logs,
            self.reports,
            self.exports,
        )

    def ensure(self) -> None:
        """创建全部受管目录（幂等）。

        Raises:
            MissingConfigError: 任一路径落在 ``~/.easy_tdx`` 之内（NF-1 红线）。
        """
        forbidden = (Path.home() / ".easy_tdx").resolve()
        for directory in self.all_dirs():
            if directory == forbidden or forbidden in directory.parents:
                raise MissingConfigError(
                    f"[fail-loud/NF-1] Kuantix 数据目录 {directory} 落在上游 {forbidden} 内，"
                    f"上游目录只读，拒绝写入"
                )
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class ServerConfig:
    """``[server]`` 节。"""

    host: str
    port: int
    reload: bool
    cors_origins: list[str]
    workers: int = 1


@dataclass(frozen=True)
class TdxConfig:
    """``[tdx]`` 节：上游行情服务器（NF-1 / NF-20）。"""

    use_easy_tdx_known_hosts: bool
    port: int
    ex_port: int
    timeout_seconds: float
    mac_hosts: list[str]
    std_hosts: list[str]
    mac_ex_hosts: list[str]


@dataclass(frozen=True)
class SyncConfig:
    """``[sync]`` 节：数据回补（NF-24 / NF-27 / NF-28）+ 盘后增量调度（v1.4）。"""

    workers: int
    default_years: int
    page_size: int
    min_request_interval: float
    retry_backoff_seconds: float
    retry_max_attempts: int
    verify_tail_bars: int
    verify_price_tolerance: float
    #: 盘后自动增量调度开关（serve 启动时挂载；测试环境置 false 保证零网络）
    schedule_enabled: bool
    #: 每日盘后触发时间（``HH:MM``，Asia/Shanghai；交易日/时段由 MarketProfile 判定）
    schedule_time: str
    #: 服务启动时是否执行幂等启动检查（湖非空且上次同步<今日才增量；空湖跳过）
    schedule_startup_check: bool
    #: 盘中分钟线增量同步是否启用（默认关闭；需先接入分钟线抓取源）
    intraday_enabled: bool
    #: 盘中分钟线同步间隔（分钟，1-59），仅 ``intraday_enabled`` 时生效
    intraday_interval_minutes: int
    #: P1-5：从 ``schedule_time`` 派生的小时（0-23），避免 scheduler 再解析字符串。
    #: 注意：必须放在字段末尾（dataclass 有默认值字段必须放在无默认值之后）。
    schedule_hour: int | None = None
    #: P1-5：从 ``schedule_time`` 派生的分钟（0-59）。
    schedule_minute: int | None = None

    def __post_init__(self) -> None:
        """P1-5：从 ``schedule_time`` 拆出 ``(schedule_hour, schedule_minute)``。

        ``schedule_hour`` / ``schedule_minute`` 若显式填 None（默认值），在此派生；
        若已被调用方传入（load_config 理论上不传，但 dataclass replace 可能），
        则验证与 ``schedule_time`` 一致。
        """
        h, m = _parse_hhmm(self.schedule_time, context="[sync].schedule_time", exc_type=MissingConfigError)
        current_h = object.__getattribute__(self, "schedule_hour")
        current_m = object.__getattribute__(self, "schedule_minute")
        if current_h is None:
            object.__setattr__(self, "schedule_hour", h)
        elif int(current_h) != h:
            raise MissingConfigError(
                f"[fail-loud/NF-26] [sync].schedule_hour={current_h!r} 与"
                f" [sync].schedule_time={self.schedule_time!r} 的小时不符"
            )
        if current_m is None:
            object.__setattr__(self, "schedule_minute", m)
        elif int(current_m) != m:
            raise MissingConfigError(
                f"[fail-loud/NF-26] [sync].schedule_minute={current_m!r} 与"
                f" [sync].schedule_time={self.schedule_time!r} 的分钟不符"
            )


@dataclass(frozen=True)
class MonitorConfig:
    """``[monitor]`` 节。"""

    poll_interval_seconds: float
    batch_size: int
    alert_cooldown_seconds: float
    trading_hours_only: bool
    #: Webhook 推送地址；**空串 = 不启用 webhook 通道**（显式语义，非错误）
    webhook_url: str = ""


@dataclass(frozen=True)
class MarketsConfig:
    """``[markets]`` 节（NF-5 / NF-7）。"""

    default: str
    cn_enabled: bool
    hk_enabled: bool
    us_enabled: bool

    def enabled(self) -> tuple[str, ...]:
        """返回已启用的市场码元组。"""
        result: list[str] = []
        if self.cn_enabled:
            result.append("CN")
        if self.hk_enabled:
            result.append("HK")
        if self.us_enabled:
            result.append("US")
        return tuple(result)

    def is_enabled(self, market: str) -> bool:
        """判断某市场是否启用。"""
        return str(market).strip().upper() in self.enabled()


@dataclass(frozen=True)
class FactorConfig:
    """``[factor]`` 节。"""

    partition: str
    forward_period: int
    quantiles: int


@dataclass(frozen=True)
class ScreenConfig:
    """``[screen]`` 节。"""

    top_n: int


@dataclass(frozen=True)
class StorageConfig:
    """``[storage]`` 节：行情主存储（SQLite market.db，设计文档 08）。

    Attributes:
        market_db: 行情主库文件名（位于 ``[paths].db`` 下）。
        vipdoc_mirror: 二进制 vipdoc 是否保留为**可选镜像**（D1）。默认
            ``false`` —— SQLite market.db 是唯一行情主存储，同步直接写
            SQLite、不默认双写镜像；置 ``true`` 时额外镜像双写（上游
            ``SignalScanner``/``StrengthRanker`` 只认文件系统，需改造
            scan_signals / strength_rank 桥内编排后按需开启，P2）。
        migrate_on_startup: 启动时是否自动执行 ``data migrate``（D3）。默认
            ``false`` —— 迁移是显式一次性操作，启动只警告不自动。
        write_batch_size: 迁移/写侧单事务批大小（T05 性能，迁移期配合
            ``PRAGMA synchronous=OFF`` 使用）。
    """

    market_db: str
    vipdoc_mirror: bool
    migrate_on_startup: bool
    write_batch_size: int


@dataclass(frozen=True)
class AnalysisConfig:
    """``[analysis]`` 节：盘前分析 / 盘后复盘调度与默认参数（设计文档 10）。

    派生字段（均通过 ``__post_init__`` 从 ``*_time`` 拆出，避免调度层字符串解析漂移）：
    ``pre_open_hour / pre_open_minute``、``post_close_hour / post_close_minute``、
    ``wait_until_hour / wait_until_minute``。
    """

    #: 盘前 cron HH:MM（Asia/Shanghai；交易日由 MarketProfile 判定）
    pre_open_time: str
    #: 盘后 cron HH:MM
    post_close_time: str
    #: 盘后等待当日 K 线就绪的兜底截止 HH:MM
    post_close_wait_until: str
    #: 技术扫描默认抽样标的数（0 = 全量，不推荐但允许）
    scan_sample_size: int
    #: 技术扫描并发 worker 数（1-16，越界 fail-loud）
    scan_workers: int
    #: 消息面 provider 名称（目前内置 ``local_json``）
    news_provider: str
    #: 本地示例消息面样本目录（None = 由构造时派生到 reports/news-samples）
    news_path: Path | None = None
    # 以下均为派生字段，必须放 dataclass 尾部（有默认值字段必须在后）
    pre_open_hour: int | None = None
    pre_open_minute: int | None = None
    post_close_hour: int | None = None
    post_close_minute: int | None = None
    wait_until_hour: int | None = None
    wait_until_minute: int | None = None

    def __post_init__(self) -> None:
        # 派生 3 个 (hour, minute) 对 —— 复用 P1-5 公共 HH:MM 解析
        self._assign_hhmm("pre_open_time", "pre_open_hour", "pre_open_minute", MissingConfigError)
        self._assign_hhmm("post_close_time", "post_close_hour", "post_close_minute", MissingConfigError)
        self._assign_hhmm("post_close_wait_until", "wait_until_hour", "wait_until_minute", MissingConfigError)
        # scan_sample_size / scan_workers 边界 fail-loud
        size = object.__getattribute__(self, "scan_sample_size")
        if not isinstance(size, int) or size < 0:
            raise MissingConfigError(
                f"[fail-loud/NF-26] [analysis].scan_sample_size 必须 >= 0，实际 {size!r}"
            )
        workers = object.__getattribute__(self, "scan_workers")
        if not isinstance(workers, int) or not (1 <= workers <= 64):
            raise MissingConfigError(
                f"[fail-loud/NF-26] [analysis].scan_workers 必须 ∈ [1,64]，实际 {workers!r}"
            )
        provider = object.__getattribute__(self, "news_provider")
        if not provider or not str(provider).strip():
            raise MissingConfigError(
                "[fail-loud/NF-26] [analysis].news_provider 不能为空"
            )
        object.__setattr__(self, "news_provider", str(provider).strip())

    def _assign_hhmm(
        self,
        time_field: str,
        hour_field: str,
        minute_field: str,
        exc_type: type[Exception],
    ) -> None:
        from dataclasses import fields as _dc_fields

        time_value = object.__getattribute__(self, time_field)
        h, m = _parse_hhmm(time_value, context=f"[analysis].{time_field}", exc_type=exc_type)
        current_h = object.__getattribute__(self, hour_field)
        current_m = object.__getattribute__(self, minute_field)
        if current_h is None:
            object.__setattr__(self, hour_field, h)
        elif int(current_h) != h:
            raise MissingConfigError(
                f"[fail-loud/NF-26] [analysis].{hour_field}={current_h!r} 与"
                f" [analysis].{time_field}={time_value!r} 的小时不符"
            )
        if current_m is None:
            object.__setattr__(self, minute_field, m)
        elif int(current_m) != m:
            raise MissingConfigError(
                f"[fail-loud/NF-26] [analysis].{minute_field}={current_m!r} 与"
                f" [analysis].{time_field}={time_value!r} 的分钟不符"
            )


@dataclass(frozen=True)
class Config:
    """Kuantix 全局配置聚合根。

    Attributes:
        source: 实际加载的配置文件路径。
        app: ``[app]`` 节。
        paths: ``[paths]`` 节。
        server: ``[server]`` 节。
        tdx: ``[tdx]`` 节。
        sync: ``[sync]`` 节。
        monitor: ``[monitor]`` 节。
        markets: ``[markets]`` 节。
        factor: ``[factor]`` 节。
        screen: ``[screen]`` 节。
        storage: ``[storage]`` 节（SQLite 行情主存储配置）。
        raw: 环境变量覆盖后的原始字典（调试用）。
    """

    source: Path
    app: AppConfig
    paths: PathsConfig
    server: ServerConfig
    tdx: TdxConfig
    sync: SyncConfig
    monitor: MonitorConfig
    markets: MarketsConfig
    factor: FactorConfig
    screen: ScreenConfig
    storage: StorageConfig
    analysis: AnalysisConfig
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def version(self) -> str:
        """Kuantix 版本号（以包内 ``__version__`` 为准）。"""
        return __version__

    def to_dict(self) -> dict[str, Any]:
        """导出为 JSON 安全字典（供 ``Kuantix config show --json``）。"""
        return {
            "source": str(self.source),
            "version": self.version,
            "app": {"name": self.app.name, "version": self.app.version,
                    "log_level": self.app.log_level},
            "paths": {name: str(getattr(self.paths, name))
                      for name in ("root", "vipdoc", "factors", "db", "logs", "reports", "exports")},
            "server": {"host": self.server.host, "port": self.server.port,
                       "reload": self.server.reload, "cors_origins": list(self.server.cors_origins)},
            "tdx": {
                "use_easy_tdx_known_hosts": self.tdx.use_easy_tdx_known_hosts,
                "port": self.tdx.port,
                "ex_port": self.tdx.ex_port,
                "timeout_seconds": self.tdx.timeout_seconds,
                "mac_hosts": list(self.tdx.mac_hosts),
                "std_hosts": list(self.tdx.std_hosts),
                "mac_ex_hosts": list(self.tdx.mac_ex_hosts),
            },
            "sync": {
                "workers": self.sync.workers,
                "default_years": self.sync.default_years,
                "page_size": self.sync.page_size,
                "min_request_interval": self.sync.min_request_interval,
                "retry_backoff_seconds": self.sync.retry_backoff_seconds,
                "retry_max_attempts": self.sync.retry_max_attempts,
                "verify_tail_bars": self.sync.verify_tail_bars,
                "verify_price_tolerance": self.sync.verify_price_tolerance,
                "schedule_enabled": self.sync.schedule_enabled,
                "schedule_time": self.sync.schedule_time,
                "schedule_hour": self.sync.schedule_hour,
                "schedule_minute": self.sync.schedule_minute,
                "schedule_startup_check": self.sync.schedule_startup_check,
                "intraday_enabled": self.sync.intraday_enabled,
                "intraday_interval_minutes": self.sync.intraday_interval_minutes,
            },
            "monitor": {
                "poll_interval_seconds": self.monitor.poll_interval_seconds,
                "batch_size": self.monitor.batch_size,
                "alert_cooldown_seconds": self.monitor.alert_cooldown_seconds,
                "trading_hours_only": self.monitor.trading_hours_only,
                "webhook_url": self.monitor.webhook_url,
            },
            "markets": {
                "default": self.markets.default,
                "cn_enabled": self.markets.cn_enabled,
                "hk_enabled": self.markets.hk_enabled,
                "us_enabled": self.markets.us_enabled,
                "enabled": list(self.markets.enabled()),
            },
            "factor": {"partition": self.factor.partition,
                       "forward_period": self.factor.forward_period,
                       "quantiles": self.factor.quantiles},
            "screen": {"top_n": self.screen.top_n},
            "storage": {
                "market_db": self.storage.market_db,
                "vipdoc_mirror": self.storage.vipdoc_mirror,
                "migrate_on_startup": self.storage.migrate_on_startup,
                "write_batch_size": self.storage.write_batch_size,
            },
            "analysis": {
                "pre_open_time": self.analysis.pre_open_time,
                "pre_open_hour": self.analysis.pre_open_hour,
                "pre_open_minute": self.analysis.pre_open_minute,
                "post_close_time": self.analysis.post_close_time,
                "post_close_hour": self.analysis.post_close_hour,
                "post_close_minute": self.analysis.post_close_minute,
                "post_close_wait_until": self.analysis.post_close_wait_until,
                "wait_until_hour": self.analysis.wait_until_hour,
                "wait_until_minute": self.analysis.wait_until_minute,
                "scan_sample_size": self.analysis.scan_sample_size,
                "scan_workers": self.analysis.scan_workers,
                "news_provider": self.analysis.news_provider,
                "news_path": str(self.analysis.news_path) if self.analysis.news_path is not None else None,
            },
        }


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------


def _as_bool(value: Any, context: str) -> bool:
    """把配置值转成布尔，非法值 fail-loud。"""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise MissingConfigError(
        f"[fail-loud/NF-26] 配置项 {context} 期望布尔值，实际 {value!r}"
        f"（合法：true/false/1/0/yes/no/on/off）"
    )


def _as_int(value: Any, context: str) -> int:
    """把配置值转成整数，非法值 fail-loud。"""
    if isinstance(value, bool):
        raise MissingConfigError(f"[fail-loud/NF-26] 配置项 {context} 期望整数，实际布尔 {value!r}")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise MissingConfigError(
            f"[fail-loud/NF-26] 配置项 {context} 期望整数，实际 {value!r}"
        ) from exc


def _as_float(value: Any, context: str) -> float:
    """把配置值转成浮点，非法值 fail-loud。"""
    if isinstance(value, bool):
        raise MissingConfigError(f"[fail-loud/NF-26] 配置项 {context} 期望浮点，实际布尔 {value!r}")
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise MissingConfigError(
            f"[fail-loud/NF-26] 配置项 {context} 期望浮点数，实际 {value!r}"
        ) from exc


def _as_str(value: Any, context: str) -> str:
    """把配置值转成非空字符串，空值 fail-loud。"""
    text = str(value).strip()
    if not text:
        raise MissingConfigError(f"[fail-loud/NF-26] 配置项 {context} 不能为空字符串")
    return text


#: P1-5：公共 HH:MM 解析（config 层 + scheduler 层共享，消除重复实现）。
#: config 层抛 MissingConfigError，scheduler 层抛 DataIntegrityError，由调用方选。
def _parse_hhmm(
    value: Any,
    *,
    context: str,
    exc_type: type[Exception] = MissingConfigError,
) -> tuple[int, int]:
    """把 ``HH:MM`` 字符串解析为 ``(hour, minute)`` 元组（P1-5 单一实现）。

    Args:
        value: 输入值（字符串或可 str 化）。
        context: 错误上下文（例如 ``[sync].schedule_time``）。
        exc_type: 抛错类型；config 层用 MissingConfigError，scheduler 层
            可以传 DataIntegrityError（两者都带 fail-loud 消息）。
    """
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise exc_type(
            f"[fail-loud/NF-26] 配置项 {context} 期望 HH:MM（如 16:30），实际 {value!r}"
        )
    hour_text, minute_text = parts[0].strip(), parts[1].strip()
    try:
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as exc:
        raise exc_type(
            f"[fail-loud/NF-26] 配置项 {context} 期望 HH:MM（如 16:30），实际 {value!r}"
        ) from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise exc_type(
            f"[fail-loud/NF-26] 配置项 {context} 时间越界: {value!r}（小时 0-23，分钟 0-59）"
        )
    return hour, minute


# 供 scheduler / 业务层直接复用（不必知道内部 _parse_hhmm，且显式使用 DataIntegrityError）
def parse_hhmm_strict(value: Any, context: str) -> tuple[int, int]:
    """P1-5：scheduler 层共用的严格解析（抛 DataIntegrityError，与 config 同源逻辑）。"""
    from Kuantix.core.fail_loud import DataIntegrityError

    return _parse_hhmm(value, context=context, exc_type=DataIntegrityError)


__all__.extend(["parse_hhmm_strict"])  # type: ignore[attr-defined]


def _as_schedule_time(value: Any, context: str) -> str:
    """把配置值校验为 ``HH:MM``（24 小时制，用于盘后调度 cron）。

    P1-5：逻辑委托给 :func:`_parse_hhmm`，避免与 scheduler 层重复实现。

    Args:
        value: 配置值。
        context: 错误上下文。

    Returns:
        规范化后的 ``HH:MM``（小时/分钟补零，如 ``"16:30"``）。

    Raises:
        MissingConfigError: 格式非法（非 ``HH:MM`` / 小时越界 / 分钟越界）。
    """
    hour, minute = _parse_hhmm(value, context=context, exc_type=MissingConfigError)
    return f"{hour:02d}:{minute:02d}"


def _as_str_list(value: Any, context: str) -> list[str]:
    """把配置值转成字符串列表（支持逗号分隔的环境变量写法）。"""
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [part.strip() for part in str(value).split(",")]
    items = [item for item in items if item]
    require_non_empty(items, f"配置项 {context}")
    return items


def _as_path(value: Any, context: str) -> Path:
    """把配置值转成绝对路径（展开 ``~``）。"""
    return Path(_as_str(value, context)).expanduser().resolve()


def _section(raw: Mapping[str, Any], name: str, source: Path) -> Mapping[str, Any]:
    """取配置分节，缺失即报错。"""
    value = require_key(raw, name, f"配置文件 {source} 缺少分节 [{name}]")
    if not isinstance(value, Mapping):
        raise MissingConfigError(
            f"[fail-loud/NF-26] 配置文件 {source} 的 [{name}] 必须是表（table），实际 {type(value).__name__}"
        )
    return value


def _pick(section: Mapping[str, Any], key: str, section_name: str, source: Path) -> Any:
    """从分节取键，缺失即报错（**替代 dict.get(k, 默认)**）。"""
    return require_key(section, key, f"配置文件 {source} 的 [{section_name}] 缺少键 {key!r}")


def _pick_opt(section: Mapping[str, Any], key: str, default: Any) -> Any:
    """从分节取键（可选，缺失回退默认值，向后兼容老配置）。"""
    return section[key] if key in section else default


# ---------------------------------------------------------------------------
# 环境变量覆盖
# ---------------------------------------------------------------------------


def _apply_env_overrides(
    raw: dict[str, Any],
    env: Mapping[str, str],
    source: Path,
) -> dict[str, Any]:
    """把 ``Kuantix__SECTION__KEY`` 形式的环境变量覆盖进配置字典。

    Args:
        raw: 从 TOML 解析出的原始配置。
        env: 环境变量映射。
        source: 配置文件路径（仅用于错误消息）。

    Returns:
        覆盖后的新字典（不修改入参）。

    Raises:
        MissingConfigError: 环境变量指向不存在的分节或键（拼错了要立刻发现，
            而不是"设了没生效"）。
    """
    merged: dict[str, Any] = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in raw.items()}
    for env_key, env_value in env.items():
        if not env_key.upper().startswith(ENV_PREFIX):
            continue
        remainder = env_key[len(ENV_PREFIX):]
        parts = [p for p in remainder.split("__") if p]
        if len(parts) != 2:
            raise MissingConfigError(
                f"[fail-loud/NF-26] 环境变量 {env_key} 格式非法，"
                f"期望 {ENV_PREFIX}<SECTION>__<KEY>（恰好两段）"
            )
        section_name, key_name = parts[0].lower(), parts[1].lower()
        if section_name not in merged or not isinstance(merged[section_name], dict):
            raise MissingConfigError(
                f"[fail-loud/NF-26] 环境变量 {env_key} 指向不存在的配置分节 [{section_name}]"
                f"（{source} 中可用分节：{sorted(k for k, v in merged.items() if isinstance(v, dict))}）"
            )
        section = merged[section_name]
        if key_name not in section:
            raise MissingConfigError(
                f"[fail-loud/NF-26] 环境变量 {env_key} 指向不存在的配置键 "
                f"[{section_name}].{key_name}（可用键：{sorted(section)}）"
            )
        section[key_name] = env_value
    return merged


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------


def find_config_file(explicit: Path | str | None = None, env: Mapping[str, str] | None = None) -> Path:
    """按既定顺序定位配置文件。

    Args:
        explicit: 显式指定的路径。
        env: 环境变量映射，``None`` 使用 :data:`os.environ`。

    Returns:
        存在的配置文件路径。

    Raises:
        MissingConfigError: 全部候选位置都不存在配置文件。
    """
    environ = os.environ if env is None else env
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit).expanduser())
    env_path = environ.get(ENV_CONFIG_PATH)
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(Path.cwd() / "config.toml")
    candidates.append(_PROJECT_ROOT / "config.toml")
    candidates.append(_BUNDLED_TEMPLATE)

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise MissingConfigError(
        "[fail-loud/NF-26] 未找到 Kuantix 配置文件，已尝试："
        + "、".join(str(c) for c in candidates)
        + f"。可用 {ENV_CONFIG_PATH} 环境变量显式指定。"
    )


def load_config(
    path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    ensure_dirs: bool = True,
) -> Config:
    """加载配置。

    Args:
        path: 显式配置文件路径；``None`` 走 :func:`find_config_file` 的搜索顺序。
        env: 环境变量映射；``None`` 使用 :data:`os.environ`。
        ensure_dirs: 是否创建 ``[paths]`` 下的全部目录。

    Returns:
        :class:`Config` 实例。

    Raises:
        MissingConfigError: 文件缺失、分节缺失、键缺失或取值类型非法。
    """
    source = find_config_file(path, env)
    with source.open("rb") as fh:
        raw = tomllib.load(fh)
    environ = os.environ if env is None else env
    merged = _apply_env_overrides(raw, environ, source)

    app_s = _section(merged, "app", source)
    paths_s = _section(merged, "paths", source)
    server_s = _section(merged, "server", source)
    tdx_s = _section(merged, "tdx", source)
    sync_s = _section(merged, "sync", source)
    monitor_s = _section(merged, "monitor", source)
    markets_s = _section(merged, "markets", source)
    factor_s = _section(merged, "factor", source)
    screen_s = _section(merged, "screen", source)
    storage_s = _section(merged, "storage", source)
    analysis_s = _section(merged, "analysis", source)

    app = AppConfig(
        name=_as_str(_pick(app_s, "name", "app", source), "[app].name"),
        version=_as_str(_pick(app_s, "version", "app", source), "[app].version"),
        log_level=_as_str(_pick(app_s, "log_level", "app", source), "[app].log_level").upper(),
    )
    paths = PathsConfig(
        root=_as_path(_pick(paths_s, "root", "paths", source), "[paths].root"),
        # 6 个子路径可选（P0 修复）：缺省时由 PathsConfig.__post_init__ 从 root 派生
        vipdoc=_as_path(
            _pick_opt(paths_s, "vipdoc", None), "[paths].vipdoc"
        ) if _pick_opt(paths_s, "vipdoc", None) is not None else None,
        factors=_as_path(
            _pick_opt(paths_s, "factors", None), "[paths].factors"
        ) if _pick_opt(paths_s, "factors", None) is not None else None,
        db=_as_path(
            _pick_opt(paths_s, "db", None), "[paths].db"
        ) if _pick_opt(paths_s, "db", None) is not None else None,
        logs=_as_path(
            _pick_opt(paths_s, "logs", None), "[paths].logs"
        ) if _pick_opt(paths_s, "logs", None) is not None else None,
        reports=_as_path(
            _pick_opt(paths_s, "reports", None), "[paths].reports"
        ) if _pick_opt(paths_s, "reports", None) is not None else None,
        exports=_as_path(
            _pick_opt(paths_s, "exports", None), "[paths].exports"
        ) if _pick_opt(paths_s, "exports", None) is not None else None,
    )
    server = ServerConfig(
        host=_as_str(_pick(server_s, "host", "server", source), "[server].host"),
        port=_as_int(_pick(server_s, "port", "server", source), "[server].port"),
        reload=_as_bool(_pick(server_s, "reload", "server", source), "[server].reload"),
        cors_origins=_as_str_list(
            _pick(server_s, "cors_origins", "server", source), "[server].cors_origins"
        ),
        workers=_as_int(_pick_opt(server_s, "workers", 1), "[server].workers"),
    )
    tdx = TdxConfig(
        use_easy_tdx_known_hosts=_as_bool(
            _pick(tdx_s, "use_easy_tdx_known_hosts", "tdx", source),
            "[tdx].use_easy_tdx_known_hosts",
        ),
        port=_as_int(_pick(tdx_s, "port", "tdx", source), "[tdx].port"),
        ex_port=_as_int(_pick(tdx_s, "ex_port", "tdx", source), "[tdx].ex_port"),
        timeout_seconds=_as_float(
            _pick(tdx_s, "timeout_seconds", "tdx", source), "[tdx].timeout_seconds"
        ),
        mac_hosts=_as_str_list(_pick(tdx_s, "mac_hosts", "tdx", source), "[tdx].mac_hosts"),
        std_hosts=_as_str_list(_pick(tdx_s, "std_hosts", "tdx", source), "[tdx].std_hosts"),
        mac_ex_hosts=_as_str_list(
            _pick(tdx_s, "mac_ex_hosts", "tdx", source), "[tdx].mac_ex_hosts"
        ),
    )
    sync = SyncConfig(
        workers=_as_int(_pick(sync_s, "workers", "sync", source), "[sync].workers"),
        default_years=_as_int(_pick(sync_s, "default_years", "sync", source), "[sync].default_years"),
        page_size=_as_int(_pick(sync_s, "page_size", "sync", source), "[sync].page_size"),
        min_request_interval=_as_float(
            _pick(sync_s, "min_request_interval", "sync", source), "[sync].min_request_interval"
        ),
        retry_backoff_seconds=_as_float(
            _pick(sync_s, "retry_backoff_seconds", "sync", source), "[sync].retry_backoff_seconds"
        ),
        retry_max_attempts=_as_int(
            _pick(sync_s, "retry_max_attempts", "sync", source), "[sync].retry_max_attempts"
        ),
        verify_tail_bars=_as_int(
            _pick(sync_s, "verify_tail_bars", "sync", source), "[sync].verify_tail_bars"
        ),
        verify_price_tolerance=_as_float(
            _pick(sync_s, "verify_price_tolerance", "sync", source), "[sync].verify_price_tolerance"
        ),
        schedule_enabled=_as_bool(
            _pick(sync_s, "schedule_enabled", "sync", source), "[sync].schedule_enabled"
        ),
        schedule_time=_as_schedule_time(
            _pick(sync_s, "schedule_time", "sync", source), "[sync].schedule_time"
        ),
        schedule_startup_check=_as_bool(
            _pick(sync_s, "schedule_startup_check", "sync", source),
            "[sync].schedule_startup_check",
        ),
        intraday_enabled=_as_bool(
            _pick_opt(sync_s, "intraday_enabled", False),
            "[sync].intraday_enabled",
        ),
        intraday_interval_minutes=_as_int(
            _pick_opt(sync_s, "intraday_interval_minutes", 5),
            "[sync].intraday_interval_minutes",
        ),
    )
    monitor = MonitorConfig(
        poll_interval_seconds=_as_float(
            _pick(monitor_s, "poll_interval_seconds", "monitor", source),
            "[monitor].poll_interval_seconds",
        ),
        batch_size=_as_int(_pick(monitor_s, "batch_size", "monitor", source), "[monitor].batch_size"),
        alert_cooldown_seconds=_as_float(
            _pick(monitor_s, "alert_cooldown_seconds", "monitor", source),
            "[monitor].alert_cooldown_seconds",
        ),
        trading_hours_only=_as_bool(
            _pick(monitor_s, "trading_hours_only", "monitor", source),
            "[monitor].trading_hours_only",
        ),
        # webhook_url 允许空串（= 不启用通道），不能用 _as_str（其拒绝空串）
        webhook_url=str(
            _pick(monitor_s, "webhook_url", "monitor", source)
        ).strip(),
    )
    markets = MarketsConfig(
        default=_as_str(_pick(markets_s, "default", "markets", source), "[markets].default").upper(),
        cn_enabled=_as_bool(_pick(markets_s, "cn_enabled", "markets", source), "[markets].cn_enabled"),
        hk_enabled=_as_bool(_pick(markets_s, "hk_enabled", "markets", source), "[markets].hk_enabled"),
        us_enabled=_as_bool(_pick(markets_s, "us_enabled", "markets", source), "[markets].us_enabled"),
    )
    factor = FactorConfig(
        partition=_as_str(_pick(factor_s, "partition", "factor", source), "[factor].partition"),
        forward_period=_as_int(
            _pick(factor_s, "forward_period", "factor", source), "[factor].forward_period"
        ),
        quantiles=_as_int(_pick(factor_s, "quantiles", "factor", source), "[factor].quantiles"),
    )
    screen = ScreenConfig(
        top_n=_as_int(_pick(screen_s, "top_n", "screen", source), "[screen].top_n"),
    )
    storage = StorageConfig(
        market_db=_as_str(
            _pick(storage_s, "market_db", "storage", source), "[storage].market_db"
        ),
        vipdoc_mirror=_as_bool(
            _pick(storage_s, "vipdoc_mirror", "storage", source),
            "[storage].vipdoc_mirror",
        ),
        migrate_on_startup=_as_bool(
            _pick(storage_s, "migrate_on_startup", "storage", source),
            "[storage].migrate_on_startup",
        ),
        write_batch_size=_as_int(
            _pick(storage_s, "write_batch_size", "storage", source),
            "[storage].write_batch_size",
        ),
    )
    analysis = AnalysisConfig(
        pre_open_time=_as_schedule_time(
            _pick(analysis_s, "pre_open_time", "analysis", source),
            "[analysis].pre_open_time",
        ),
        post_close_time=_as_schedule_time(
            _pick(analysis_s, "post_close_time", "analysis", source),
            "[analysis].post_close_time",
        ),
        post_close_wait_until=_as_schedule_time(
            _pick(analysis_s, "post_close_wait_until", "analysis", source),
            "[analysis].post_close_wait_until",
        ),
        scan_sample_size=_as_int(
            _pick(analysis_s, "scan_sample_size", "analysis", source),
            "[analysis].scan_sample_size",
        ),
        scan_workers=_as_int(
            _pick(analysis_s, "scan_workers", "analysis", source),
            "[analysis].scan_workers",
        ),
        news_provider=_as_str(
            _pick(analysis_s, "news_provider", "analysis", source),
            "[analysis].news_provider",
        ),
        # news_path 可选：显式填写 → 解析路径；否则派生到 [paths].reports/news-samples
        news_path=(
            _as_path(
                _pick_opt(analysis_s, "news_path", None),
                "[analysis].news_path",
            )
            if _pick_opt(analysis_s, "news_path", None) not in (None, "")
            else (paths.reports / "news-samples")
        ),
    )

    config = Config(
        source=source,
        app=app,
        paths=paths,
        server=server,
        tdx=tdx,
        sync=sync,
        monitor=monitor,
        markets=markets,
        factor=factor,
        screen=screen,
        storage=storage,
        analysis=analysis,
        raw=merged,
    )
    if ensure_dirs:
        config.paths.ensure()
    return config


_ACTIVE: Config | None = None


def get_config() -> Config:
    """返回进程内单例配置（首次调用时加载）。

    Returns:
        :class:`Config` 实例。
    """
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = load_config()
    return _ACTIVE


def set_config(config: Config) -> None:
    """替换进程内单例配置（供测试与 CLI ``--config`` 使用）。"""
    global _ACTIVE
    _ACTIVE = config


def reset_config() -> None:
    """清空进程内单例配置（供测试使用）。"""
    global _ACTIVE
    _ACTIVE = None
