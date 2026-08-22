"""数据湖门面（L1 行情湖，T03 主入口）。

:class:`DataLake` 对外提供三个能力：
- :meth:`sync_full` —— 全市场回补（断点续传 / 后台运行 / 限速退避，NF-24）；
- :meth:`sync_incremental` —— 增量回补（按已有最后日期续拉）；
- :meth:`verify` —— 完整性校验（NF-27，SQLite 优先，镜像兜底）。

**SQLite 主存储装配（设计文档 08）**：
- :class:`~Kuantix.data.market_store.MarketStore` —— 行情主库（daily_bars /
  securities / sync_meta / sync_checkpoint）；
- :class:`~Kuantix.adapters.vipdoc_writer.SqliteBarWriter` —— 写侧主后端
  （四道闸门语义迁移）；
- :class:`~Kuantix.adapters.vipdoc_writer.VipdocWriter` —— **可选镜像**写后端
  （``[storage].vipdoc_mirror=true`` 时才双写，默认 false 纯 SQLite 主存储；
  镜像仅保证上游 SignalScanner / StrengthRanker 零改动，D1，P2）；
- 枚举结果落 ``securities`` 表（证券清单本地化，问题 1）—— **网络枚举是
  ``data sync`` 与 ``securities update`` 的专属动作**。

组装顺序（依赖注入便于离线测试）：
    ``UniverseEnumerator``（枚举）→ ``QuotationFetcher``（拉 K 线）
    → ``SqliteBarWriter``/``VipdocWriter``（写盘，四道闸门）
    → ``SyncEngine``（并发/续传）→ ``QuarantineStore``（隔离区）。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from Kuantix.adapters.quotation import QuotationFetcher
from Kuantix.adapters.tdx_client import TdxClientFactory
from Kuantix.adapters.universe import UniverseEnumerator
from Kuantix.adapters.vipdoc_writer import SqliteBarWriter, VipdocWriter
from Kuantix.config import Config, get_config
from Kuantix.core.contracts import VerifyReport
from Kuantix.core.fail_loud import NotSupportedError
from Kuantix.core.market import MarketProfile, get_market_profile, known_markets
from Kuantix.data.market_store import MarketStore
from Kuantix.data.quarantine import QuarantineStore
from Kuantix.data.sync_engine import SyncEngine, SyncHandle, SyncPlan, SyncResult
from Kuantix.data.verify import verify_market_store, verify_vipdoc

__all__ = ["DataLake", "CompositeBarWriter"]


def _date_int_to_iso(value: int) -> str:
    """``YYYYMMDD`` 整数 → ``YYYY-MM-DD`` ISO 字符串（D1 coverage 补齐用）。"""
    number = int(value)
    return f"{number // 10000:04d}-{(number // 100) % 100:02d}-{number % 100:02d}"


#: daily_bars 聚合统计的缓存 TTL（秒）。统计只在 ``data sync``/``migrate``
#: 写入后变化，状态页被前端频繁轮询；TTL 内命中缓存避免每次全表扫描。
_STATS_TTL_SECONDS = 30.0

#: 通达信 vipdoc ``.day`` 日线固定记录大小（字节/根）。未迁移（mirror_only）
#: 时据此从文件大小估算镜像 K 线条数，避免全量解析 508M 镜像。
_DAY_RECORD_BYTES = 32


class CompositeBarWriter:
    """双后端写盘器：SQLite 主存储 + 可选 vipdoc 镜像（D1，默认关闭）。

    - ``write_daily``：先写 SQLite（主），``vipdoc_mirror=true`` 时再写镜像
      （失败显式抛错，不静默降级）；默认 false 时只写 SQLite，同步路径无
      镜像写调用；
    - ``last_bar_date``：优先 SQLite，其次镜像（增量同步判定源）。

    Args:
        sqlite_writer: :class:`~Kuantix.adapters.vipdoc_writer.SqliteBarWriter`。
        mirror_writer: :class:`~Kuantix.adapters.vipdoc_writer.VipdocWriter`；
            ``None`` 表示镜像关闭（只写 SQLite，默认）。
    """

    def __init__(
        self,
        sqlite_writer: SqliteBarWriter,
        mirror_writer: VipdocWriter | None = None,
    ) -> None:
        self._sqlite = sqlite_writer
        self._mirror = mirror_writer

    @property
    def mirror_enabled(self) -> bool:
        """镜像是否启用（``[storage].vipdoc_mirror``）。"""
        return self._mirror is not None

    def write_daily(
        self, bars: Any, exchange: str, code: str
    ) -> Any:
        """写日线：SQLite 主写 + 镜像双写（镜像启用时）。

        Returns:
            SQLite 后端的 :class:`~Kuantix.adapters.vipdoc_writer.WriteReport`。
        """
        report = self._sqlite.write_daily(bars, exchange, code)
        if self._mirror is not None:
            self._mirror.write_daily(bars, exchange, code)
        return report

    def last_bar_date(self, exchange: str, code: str) -> int | None:
        """最后交易日：SQLite 优先，其次镜像（增量同步判定源）。"""
        sqlite_date = self._sqlite.last_bar_date(exchange, code)
        if sqlite_date is not None:
            return sqlite_date
        if self._mirror is not None:
            return self._mirror.last_bar_date(exchange, code)
        return None


class DataLake:
    """L1 行情湖门面。

    Args:
        config: 配置对象；``None`` 时取全局配置（:func:`Kuantix.config.get_config`）。
        factory: 客户端工厂；``None`` 时由配置构造。
        enumerator: 枚举器；``None`` 时由工厂构造。
        quarantine: 隔离区；``None`` 时用 ``~/.Kuantix/db``。
        writer: 写盘器（测试注入）；``None`` 时按配置装配
            :class:`CompositeBarWriter`（SQLite 主 + 可选镜像，默认镜像关闭）。
        store: 行情主存储（测试注入）；``None`` 时按配置构造。
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        factory: TdxClientFactory | None = None,
        enumerator: UniverseEnumerator | None = None,
        quarantine: QuarantineStore | None = None,
        writer: Any | None = None,
        store: MarketStore | None = None,
    ) -> None:
        self._config = config if config is not None else get_config()
        self._factory = factory if factory is not None else TdxClientFactory.from_config(self._config)
        self._enumerator = (
            enumerator if enumerator is not None else UniverseEnumerator(self._factory)
        )
        self._quarantine = (
            quarantine
            if quarantine is not None
            else QuarantineStore(self._config.paths.db)
        )
        # 测试注入的假 config（SimpleNamespace）可能没有 [storage] 节 —— 防御性
        # 默认 market.db / 镜像关闭，真实 Config 恒有 storage（配置缺失即报错）。
        storage_cfg = getattr(self._config, "storage", None)
        market_db_name = (
            getattr(storage_cfg, "market_db", "market.db")
            if storage_cfg is not None
            else "market.db"
        )
        self._store = (
            store
            if store is not None
            else MarketStore(self._config.paths.db / market_db_name)
        )
        if writer is not None:
            self._writer = writer
        else:
            sqlite_writer = SqliteBarWriter(
                self._store,
                verify_tail_bars=self._config.sync.verify_tail_bars,
                verify_price_tolerance=self._config.sync.verify_price_tolerance,
            )
            mirror_writer: VipdocWriter | None = None
            if storage_cfg is not None and getattr(storage_cfg, "vipdoc_mirror", False):
                mirror_writer = VipdocWriter(
                    self._config.paths.vipdoc,
                    verify_tail_bars=self._config.sync.verify_tail_bars,
                    verify_price_tolerance=self._config.sync.verify_price_tolerance,
                )
            self._writer = CompositeBarWriter(sqlite_writer, mirror_writer)

    # ------------------------------------------------------------------ #
    # 公开属性
    # ------------------------------------------------------------------ #

    @property
    def store(self) -> MarketStore:
        """行情主存储（market.db）。"""
        return self._store

    @property
    def writer(self) -> Any:
        """当前写盘器（测试注入或 CompositeBarWriter）。"""
        return self._writer

    # ------------------------------------------------------------------ #
    # 回补
    # ------------------------------------------------------------------ #

    def sync_full(
        self,
        market: str,
        years: int,
        workers: int | None = None,
        *,
        checkpoint_path: Path | str | None = None,
        force: bool = False,
    ) -> SyncHandle:
        """全市场日线回补（断点续传，后台运行不阻塞 CLI）。

        Args:
            market: 市场码（P0 仅 ``CN`` 已实现，HK/US 显式抛错）。
            years: 回溯年数。
            workers: 并发数；``None`` 用配置 ``[sync].workers``。
            checkpoint_path: 旧断点文件路径（兼容）；``None`` 时断点走
                market.db ``sync_checkpoint`` 表（D6，主路径）。
            force: ``True`` 时跳过「交易时段禁全量回补」的软限制（NF-28）。

        Returns:
            :class:`SyncHandle`（已启动后台线程）。

        Raises:
            NotSupportedError: 市场未实现，或交易时段内全量回补（NF-28）。
        """
        if not self._config.markets.is_enabled(market):
            return self._skip_disabled_market(market, years)
        profile = self._profile(market)
        if not force:
            self._assert_backfill_allowed(profile)
        result = self._enumerator.enumerate_full(market)
        securities = result.securities
        for entry in result.rejected:
            self._quarantine.add(entry)
        # 枚举结果落 securities 表（证券清单本地化，问题 1）
        if securities:
            self._store.upsert_securities(securities)

        if checkpoint_path is None:
            checkpoint_path = (
                self._config.paths.db / f"sync_checkpoint_{market}.json"
            )
        plan = SyncPlan(
            market=market,
            years=int(years),
            securities=tuple(securities),
            vipdoc_root=self._config.paths.vipdoc,
            workers=int(workers if workers is not None else self._config.sync.workers),
            min_request_interval=self._config.sync.min_request_interval,
            retry_backoff_seconds=self._config.sync.retry_backoff_seconds,
            retry_max_attempts=self._config.sync.retry_max_attempts,
            checkpoint_path=Path(checkpoint_path),
        )
        engine = self._build_engine()
        return engine.run(plan)

    def sync_incremental(
        self,
        market: str = "CN",
        *,
        workers: int | None = None,
        checkpoint_path: Path | str | None = None,
        force: bool = False,
    ) -> SyncHandle:
        """增量回补：按已有最后日期续拉，无数据则全量拉取。

        Args:
            market: 市场码。
            workers: 并发数。
            checkpoint_path: 断点文件路径（兼容）。
            force: 跳过交易时段软限制（NF-28）。

        Returns:
            :class:`SyncHandle`。
        """
        if not self._config.markets.is_enabled(market):
            return self._skip_disabled_market(market, self._config.sync.default_years)
        profile = self._profile(market)
        if not force:
            self._assert_backfill_allowed(profile)
        result = self._enumerator.enumerate_full(market)
        securities = result.securities
        for entry in result.rejected:
            self._quarantine.add(entry)
        if securities:
            self._store.upsert_securities(securities)

        if checkpoint_path is None:
            checkpoint_path = (
                self._config.paths.db / f"sync_checkpoint_{market}.json"
            )
        plan = SyncPlan(
            market=market,
            years=self._config.sync.default_years,
            securities=tuple(securities),
            vipdoc_root=self._config.paths.vipdoc,
            workers=int(workers if workers is not None else self._config.sync.workers),
            min_request_interval=self._config.sync.min_request_interval,
            retry_backoff_seconds=self._config.sync.retry_backoff_seconds,
            retry_max_attempts=self._config.sync.retry_max_attempts,
            checkpoint_path=Path(checkpoint_path),
        )
        engine = self._build_engine()
        return engine.run(plan)

    def sync_securities_only(self, market: str = "CN") -> dict[str, Any]:
        """只枚举证券清单并落 ``securities`` 表（``securities update`` 入口）。

        这是除 ``data sync`` 外**唯一允许网络枚举的显式入口**
        （设计文档 08 §2：请求路径零枚举，显式 CLI 例外）。

        Args:
            market: 市场码。

        Returns:
            ``{market, enumerated, rejected, count}``。
        """
        if not self._config.markets.is_enabled(market):
            logger.info(
                "跳过未启用的市场 %s（[markets].%s_enabled=false），行情湖不枚举其证券清单",
                market, market.lower(),
            )
            return {
                "market": market,
                "enabled": False,
                "skipped": True,
                "enumerated": 0,
                "rejected": 0,
                "count": 0,
                "profile": None,
            }
        profile = self._profile(market)
        result = self._enumerator.enumerate_full(market)
        securities = result.securities
        for entry in result.rejected:
            self._quarantine.add(entry)
        if securities:
            self._store.upsert_securities(securities)
        return {
            "market": market,
            "enumerated": len(securities),
            "rejected": len(result.rejected),
            "count": self._store.securities_count(market),
            "profile": profile.market,
        }

    # ------------------------------------------------------------------ #
    # 分钟线增量同步（扩展点：抓取源需先接入，F1 调度盘中分钟线）
    # ------------------------------------------------------------------ #

    def sync_minute_incremental(self, market: str = "CN"):
        """盘中分钟线增量同步（扩展点）。

        当前分钟线**抓取源尚未接入**——数据层已具备分钟线存储
        （``MarketStore.write_minute_bars`` 按分区文件落库）与写入侧
        （``VipdocWriter.write_min5_partitioned`` + fsync + 残尾修复），
        但缺少从行情源拉取分钟 K 线的客户端。

        要落地盘中同步，请在本方法内装配 vipdoc/tdx 分钟客户端，按标的循环
        拉取当日分钟线并写入 ``MarketStore.write_minute_bars``。在源接入前，
        调度器盘中任务会在此处得到明确报错而非静默空跑。

        Raises:
            NotImplementedError: 分钟线抓取源未接入。
        """
        raise NotImplementedError(
            "[sync] 分钟线增量同步的抓取源尚未接入：请在 DataLake.sync_minute_incremental "
            "内装配 vipdoc/tdx 分钟客户端后实现（存储侧 MarketStore.write_minute_bars "
            "与写入侧 VipdocWriter.write_min5_partitioned 已就绪）"
        )

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #

    def verify(self, market: str = "CN") -> VerifyReport:
        """完整性校验（NF-27）：SQLite 优先，镜像兜底。

        market.db 已有该市场日线 → 校验 SQLite；否则（未迁移/空库）回退
        vipdoc 镜像校验，保证迁移前 `verify` 仍可用。

        Args:
            market: 市场码。

        Returns:
            :class:`VerifyReport`（覆盖统计 / 缺失日 / 损坏 / 隔离区清单）。
        """
        profile = self._profile(market)
        if self._store.daily_bar_count(market) > 0:
            return verify_market_store(
                self._store,
                market,
                profile,
                self._quarantine,
            )
        return verify_vipdoc(
            self._config.paths.vipdoc,
            market,
            profile,
            self._quarantine,
        )

    def list_quarantine(
        self,
        market: str | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Any]:
        """P1-2：列出隔离区条目（支持 DB 级 LIMIT/OFFSET 分页）。"""
        return self._quarantine.list(market, limit=limit, offset=offset)

    def count_quarantine(self, market: str | None = None) -> int:
        """P1-2：隔离区匹配条目总数（配合 list_quarantine 的分页用）。"""
        return self._quarantine.count(market)

    def remove_quarantine(self, code: str, market: str | None = None) -> int:
        """从隔离区移除一条记录（D7）。

        Args:
            code: 标的代码。
            market: 市场过滤；``None`` 时只按 code 删除。

        Returns:
            实际删除条数（0 表示不存在，由路由层映射 404）。
        """
        return self._quarantine.remove(code, market)

    # ------------------------------------------------------------------ #
    # REST 载荷（D1 status / D5 verify）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _coverage_payload(report: VerifyReport) -> dict[str, Any]:
        """把 VerifyReport.coverage 规范成契约 §3.2 的形状。

        契约 coverage：``{securities, files, bars, disk_bytes, first_date,
        last_date}``（不含 market；``total_bars`` 改名为 ``bars``）。
        """
        coverage = dict(report.coverage)
        if "market" in coverage:
            coverage.pop("market")
        if "total_bars" in coverage:
            coverage["bars"] = coverage.pop("total_bars")
        return coverage

    # ------------------------------------------------------------------ #
    # D1 空湖判定：SQLite 主存储 + vipdoc 镜像的合并统计（UX/判定修复）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _mirror_exchanges(market: str) -> tuple[str, ...]:
        """市场码 → 镜像交易所目录（P0 仅 CN 有 ``sh``/``sz``；其余空）。

        与 :meth:`Migrator._exchanges_for_market` 同口径：A 股镜像布局
        ``<vipdoc>/<sh|sz>/lday/*.day``；港/美股走 ``ds/`` 目录，P1 占位。
        """
        code = str(market).upper()
        if code == "CN":
            return ("sh", "sz")
        return ()

    def _mirror_stats(self, market: str) -> tuple[int, int]:
        """轻量统计 vipdoc 镜像的 ``.day`` 文件数与字节数（**不做解析**）。

        仅扫 ``<vipdoc>/<exchange>/lday/*.day`` 的文件名与 ``stat`` 大小，
        O(文件数) 且不读文件内容 —— 与 :func:`verify_vipdoc` 的完整回读
        校验不同，供 D1/D5 的 ``storage.mirror_files`` 与空湖判定使用
        （508M/4730 只全量解析只发生在 ``data verify`` 显式调用时）。

        Args:
            market: 市场码。

        Returns:
            ``(文件数, 磁盘字节数)``；目录不存在返回 ``(0, 0)``。
        """
        now = time.monotonic()
        cache = getattr(self, "_mirror_stats_cache", None)
        if cache is None:
            cache = self._mirror_stats_cache = {}
        entry = cache.get(str(market).upper())
        if entry is not None and now - entry["ts"] < _STATS_TTL_SECONDS:
            return entry["value"]
        files = 0
        disk_bytes = 0
        for exchange in self._mirror_exchanges(market):
            lday = self._config.paths.vipdoc / exchange / "lday"
            if not lday.is_dir():
                continue
            for path in lday.glob("*.day"):
                files += 1
                disk_bytes += path.stat().st_size
        value = (files, disk_bytes)
        cache[str(market).upper()] = {"ts": now, "value": value}
        return value

    def _mirror_date_range(self, market: str) -> tuple[int, int] | None:
        """轻量扫描镜像 ``.day`` 文件首末记录，得全局 ``(first, last)`` 日期。

        仅读取每个 ``.day`` 的首条/末条记录（各 32 字节），不做全量解析 ——
        未迁移（mirror_only）时供 D1 coverage 补齐首末日，O(文件数) 且零
        全量 IO。日期为 ``YYYYMMDD`` 整数。

        Args:
            market: 市场码。

        Returns:
            ``(first_date, last_date)``；该市场镜像无文件返回 ``None``。
        """
        import struct

        now = time.monotonic()
        cache = getattr(self, "_mirror_date_range_cache", None)
        if cache is None:
            cache = self._mirror_date_range_cache = {}
        entry = cache.get(str(market).upper())
        if entry is not None and now - entry["ts"] < _STATS_TTL_SECONDS:
            return entry["value"]
        first_global: int | None = None
        last_global: int | None = None
        for exchange in self._mirror_exchanges(market):
            lday = self._config.paths.vipdoc / exchange / "lday"
            if not lday.is_dir():
                continue
            for path in lday.glob("*.day"):
                try:
                    size = path.stat().st_size
                    if size < 32:
                        continue
                    with open(path, "rb") as fh:
                        fh.seek(0)
                        first_date = struct.unpack("<I", fh.read(4))[0]
                        fh.seek(-32, 2)
                        last_date = struct.unpack("<I", fh.read(4))[0]
                except (OSError, struct.error):
                    # 损坏文件跳过（fail-loud：不静默，完整校验走 verify）
                    continue
                if first_global is None or first_date < first_global:
                    first_global = first_date
                if last_global is None or last_date > last_global:
                    last_global = last_date
        if first_global is None or last_global is None:
            value = None
        else:
            value = (first_global, last_global)
        cache[str(market).upper()] = {"ts": now, "value": value}
        return value

    @staticmethod
    def _classify_source(sqlite_bars: int, mirror_files: int) -> str:
        """按「SQLite 日线行数 + 镜像 .day 文件数」判定存储状态。

        四态（D1 新增 ``storage.source``，前端据此分三场景引导）：
        - ``"empty"``       —— 两者都空（真未建湖）→ 引导 data sync；
        - ``"mirror_only"`` —— 仅镜像有（未 migrate）→ 引导 data migrate；
        - ``"sqlite"``      —— 仅 SQLite 有（纯主存储，正常）→ 不引导；
        - ``"both"``        —— 两者都有（正常）→ 不引导。

        Args:
            sqlite_bars: daily_bars 表行数（``COUNT(*)``，廉价）。
            mirror_files: vipdoc 镜像 ``.day`` 文件数（轻量扫描）。

        Returns:
            四态之一。
        """
        if sqlite_bars == 0 and mirror_files == 0:
            return "empty"
        if sqlite_bars == 0 and mirror_files > 0:
            return "mirror_only"
        if sqlite_bars > 0 and mirror_files == 0:
            return "sqlite"
        return "both"

    def storage_status(self, market: str = "CN") -> dict[str, Any]:
        """存储摘要（D1/D5/CLI 共用）：SQLite 行数 + 镜像文件数 + 判定 source。

        向后兼容（只增字段）：``**self._store.summary()`` 原样保留旧字段
        （``db_path``/``backend``/``securities``/``daily_bars``/
        ``sync_checkpoint``/``sync_meta``），新增
        ``sqlite_bars``/``sqlite_securities``/``sqlite_codes``/
        ``mirror_files``/``mirror_disk_bytes``/``source`` 供前端区分三种状态。
        ``sqlite_codes`` 是 daily_bars 的去重代码数（securities 表可能为空）。

        **性能（数据量大优化）**：sqlite 三处聚合（条数 / 去重代码数 / 首末日）
        合并为一次 ``daily_bar_stats``（单表扫描），并走 TTL 缓存 —— 1338 万行
        上单次聚合仍要 ~3s，但该统计只在 ``data sync``/``migrate`` 后变化，
        状态页被前端频繁轮询时命中缓存，避免每次全表扫描。

        Args:
            market: 市场码。

        Returns:
            存储摘要字典。
        """
        stats = self._cached_daily_bar_stats(market)
        sqlite_bars = stats["bars"]
        sqlite_codes = stats["distinct_codes"]
        sqlite_securities = self._store.securities_count(market)
        mirror_files, mirror_disk_bytes = self._mirror_stats(market)
        source = self._classify_source(sqlite_bars, mirror_files)
        # 用缓存的聚合填充 daily_bars 计数，避免 :meth:`MarketStore.summary`
        # 内部的 ``daily_bar_count`` 再次全表 ``COUNT(*)``（1338 万行 ~3.8s）。
        return {
            "db_path": str(self._store.db_path),
            "backend": "sqlite",
            "securities": sqlite_securities,
            "daily_bars": sqlite_bars,
            "sync_checkpoint": self._store.checkpoint_count(market),
            "sync_meta": self._store._sync_meta_count(),
            "sqlite_bars": sqlite_bars,
            "sqlite_securities": sqlite_securities,
            "sqlite_codes": sqlite_codes,
            "mirror_files": mirror_files,
            "mirror_disk_bytes": mirror_disk_bytes,
            "source": source,
        }

    def _cached_daily_bar_stats(self, market: str) -> dict[str, int | None]:
        """带 TTL 的 daily_bars 聚合统计（数据不变则避免全表扫描）。

        统计结果只在 ``data sync`` / ``data migrate`` 写入时变化；这里用
        进程级缓存 + 固定 TTL，命中时直接返回，未命中才触发一次单表扫描。
        TTL 结束后下次请求重新扫描，保证数据一旦被外部写入也能在可接受
        延迟内反映（fail-loud：扫描失败显式抛，不静默返回过期数据）。
        """
        now = time.monotonic()
        cache = getattr(self, "_bar_stats_cache", None)
        if cache is None:
            cache = self._bar_stats_cache = {}
        entry = cache.get(market)
        if entry is not None and now - entry["ts"] < _STATS_TTL_SECONDS:
            return entry["stats"]
        stats = self._store.daily_bar_stats(market)
        cache[market] = {"ts": now, "stats": stats}
        return stats

    @staticmethod
    def _merge_coverage(
        coverage: dict[str, Any],
        storage: dict[str, Any],
        date_range: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        """合并 coverage：任一后端有数据即非空（D1 修复，只增语义）。

        背景：``verify()`` 是**单源**校验 —— SQLite 有日线（daily_bars>0）
        就走 :func:`verify_market_store`，而它遍历 ``securities`` 表计数；
        若迁移只写了 daily_bars 没写 securities（用户场景：daily_bars 2 行
        + 镜像 4730 只），coverage 全 0 → 前端误判"数据湖为空"并引导重拉
        508M。这里用 storage 的廉价统计补齐：

        - ``securities``/``files``：取 SQLite 与镜像的较大值（有效可用标的
          数，不重复计）；
        - ``bars``：SQLite 侧直接取 ``daily_bars`` 行数（不经 securities 表）；
        - ``disk_bytes``：SQLite 无文件概念，镜像有文件时取镜像字节数；
        - ``first_date``/``last_date``：由调用方从 ``daily_bars`` 表聚合
          传入（``date_range``，O(1) 聚合），覆盖缺失的首末日。

        Args:
            coverage: :meth:`_coverage_payload` 的原始覆盖。
            storage: :meth:`storage_status` 的摘要（含廉价统计）。
            date_range: SQLite daily_bars 的 ``(MIN, MAX)``；``None`` 不补齐。

        Returns:
            合并后的 coverage（新字典，不改入参）。
        """
        result = dict(coverage)
        sqlite_bars_value = storage.get("sqlite_bars")
        sqlite_securities_value = storage.get("sqlite_securities")
        sqlite_codes_value = storage.get("sqlite_codes")
        mirror_files_value = storage.get("mirror_files")
        mirror_disk_value = storage.get("mirror_disk_bytes")
        sqlite_bars = int(sqlite_bars_value) if sqlite_bars_value is not None else 0
        sqlite_securities = (
            int(sqlite_securities_value) if sqlite_securities_value is not None else 0
        )
        sqlite_codes = int(sqlite_codes_value) if sqlite_codes_value is not None else 0
        mirror_files = int(mirror_files_value) if mirror_files_value is not None else 0
        mirror_disk = int(mirror_disk_value) if mirror_disk_value is not None else 0

        # 有效可用标的数：securities 表、daily_bars 去重代码、镜像文件三者较大值
        # （不重复计；镜像通常含 SQLite 已有标的，max 是下界口径）
        effective_securities = max(sqlite_securities, sqlite_codes, mirror_files)
        current_securities = result.get("securities")
        if not current_securities:
            result["securities"] = effective_securities
        current_files = result.get("files")
        if not current_files:
            result["files"] = effective_securities
        current_bars = result.get("bars")
        if not current_bars and sqlite_bars > 0:
            result["bars"] = sqlite_bars
        current_disk = result.get("disk_bytes")
        if not current_disk:
            result["disk_bytes"] = mirror_disk

        if (result.get("first_date") is None or result.get("last_date") is None) and (
            date_range is not None
        ):
            first_int, last_int = date_range
            result["first_date"] = _date_int_to_iso(first_int)
            result["last_date"] = _date_int_to_iso(last_int)
        return result

    def merged_coverage_from_report(
        self, report: VerifyReport, market: str = "CN"
    ) -> dict[str, Any]:
        """把一次 verify 的 coverage 合并 SQLite+镜像（D1/D5/CLI 共用口径）。

        Args:
            report: :meth:`verify` 的返回（复用已校验结果，避免二次全量校验）。
            market: 市场码。

        Returns:
            合并后的 coverage（任一后端有数据即非空，首末日用
            ``daily_bars`` 聚合补齐）。
        """
        coverage = self._coverage_payload(report)
        storage = self.storage_status(market)
        return self._merge_coverage(
            coverage, storage, date_range=self._store.date_range(market)
        )

    def _status_coverage(self, market: str) -> dict[str, Any]:
        """用廉价聚合统计构造 D1 coverage（**不做全量回读校验**）。

        状态页只需要覆盖统计（条数 / 标数量 / 首末日 / 磁盘），这些可由
        daily_bars 的单表聚合 + 镜像轻量扫描得到，无需像 ``verify`` 那样逐
        标的回读全部 K 线做完整性检查。数据量大时把 status 从「几十秒全量
        校验」降为「一次缓存聚合」—— 全量校验只保留在显式 ``/data/verify``。

        Returns:
            契约 §3.2 形状的 coverage（``securities``/``files``/``bars``/
            ``disk_bytes``/``first_date``/``last_date``）。
        """
        stats = self._cached_daily_bar_stats(market)
        storage = self.storage_status(market)
        mirror_files = int(storage.get("mirror_files") or 0)
        mirror_disk = int(storage.get("mirror_disk_bytes") or 0)
        sqlite_securities = int(storage.get("sqlite_securities") or 0)
        sqlite_codes = int(storage.get("sqlite_codes") or 0)
        sqlite_bars = int(storage.get("sqlite_bars") or 0)

        effective_securities = max(sqlite_securities, sqlite_codes, mirror_files)
        # 镜像 `.day` 为固定 32 字节/根（通达信日线标准格式），未迁移
        # （mirror_only）时据此估算镜像 bars 数；SQLite 有数据则用其条数。
        mirror_bars = mirror_disk // _DAY_RECORD_BYTES if mirror_disk else 0
        bars = sqlite_bars if sqlite_bars > 0 else mirror_bars
        first_int = stats["first_date"]
        last_int = stats["last_date"]
        if sqlite_bars <= 0 and mirror_files > 0:
            # 未迁移（mirror_only）：首末日由镜像轻量扫描补齐，避免误判空湖
            date_range = self._mirror_date_range(market)
            if date_range is not None:
                first_int, last_int = date_range
        return {
            "securities": effective_securities,
            "files": effective_securities,
            "bars": bars,
            "disk_bytes": mirror_disk,
            "first_date": _date_int_to_iso(first_int) if first_int is not None else None,
            "last_date": _date_int_to_iso(last_int) if last_int is not None else None,
        }

    def status(self, market: str = "CN") -> dict[str, Any]:
        """D1 DataLakeStatus 载荷（latest_job 由 API 层从 JobManager 合并）。

        **只增字段（契约增量，设计文档 08）**：
        - ``storage``：SQLite 存储摘要（backend/db_path/行数）+ **新增**
          ``sqlite_bars``/``sqlite_securities``/``mirror_files``/
          ``mirror_disk_bytes``/``source``（D1 空湖判定修复：合并统计
          SQLite 主存储与 vipdoc 镜像，``source`` 四态
          ``empty``/``mirror_only``/``sqlite``/``both``）；
        - ``vipdoc_mirror``：二进制镜像是否启用（D1）。

        **性能（数据量大优化）**：本方法**不再调用全量 ``verify()``** ——
        coverage 由缓存聚合构造（见 :meth:`_status_coverage`），完整性和缺失
        交易日核对保留在显式 ``/data/verify``（:meth:`verify_payload`）。

        Args:
            market: 市场码。

        Returns:
            ``{market, data_date, coverage, quarantine_count, in_sync_window,
            last_sync, schedule, storage, vipdoc_mirror}``。
        """
        profile = self._profile(market)
        quarantined = self._quarantine.list(market)
        coverage = self._status_coverage(market)
        storage = self.storage_status(market)
        payload: dict[str, Any] = {
            "market": market,
            "data_date": coverage.get("last_date"),
            "coverage": coverage,
            "quarantine_count": len(quarantined),
            "in_sync_window": profile.is_open_now(),
            "storage": storage,
            "vipdoc_mirror": self._config.storage.vipdoc_mirror,
        }
        from Kuantix.data.sync_state import SyncStateStore

        state_store = SyncStateStore(self._config.paths.db)
        payload["last_sync"] = state_store.view()
        payload["schedule"] = {
            "enabled": self._config.sync.schedule_enabled,
            "time": self._config.sync.schedule_time,
            "startup_check": self._config.sync.schedule_startup_check,
        }
        return payload

    def verify_payload(self, market: str = "CN") -> dict[str, Any]:
        """D5 VerifyReport 载荷（契约 §3.2 + ``excluded_count``，NF-27）。

        **只增字段**：新增 ``storage``（同 D1 ``storage_status`` 摘要），
        让 ``data verify`` 报告能区分 SQLite 主存储与 vipdoc 镜像的覆盖
        （``storage.source`` 四态）；``coverage`` 与 D1 同口径合并
        （任一后端有数据即非空）。
        """
        profile = self._profile(market)
        report = self.verify(market)
        coverage = self.merged_coverage_from_report(report, market)
        storage = self.storage_status(market)
        return {
            "market": market,
            "coverage": coverage,
            "missing_days": [d.isoformat() for d in report.missing_days],
            "corrupt": list(report.corrupt),
            "quarantined": [q.to_dict() for q in report.quarantined],
            "excluded_count": len(report.quarantined),
            "generated_at": report.generated_at.isoformat(timespec="seconds"),
            "storage": storage,
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _skip_disabled_market(self, market: str, years: int) -> SyncHandle:
        """构造「未启用市场」的空操作句柄（不发起任何网络请求）。

        配合 ``[markets].hk_enabled`` / ``[markets].us_enabled=false``，
        行情湖回补入口对港美直接跳过，临时不获取港美数据（可由配置恢复）。
        """
        logger.info(
            "跳过未启用的市场 %s（[markets].%s_enabled=false），行情湖不获取该市场数据",
            market, market.lower(),
        )
        plan = SyncPlan(
            market=market,
            years=int(years),
            securities=(),
            vipdoc_root=self._config.paths.vipdoc,
            workers=0,
            min_request_interval=self._config.sync.min_request_interval,
            retry_backoff_seconds=self._config.sync.retry_backoff_seconds,
            retry_max_attempts=self._config.sync.retry_max_attempts,
            checkpoint_path=None,
            event_bus=False,
        )
        handle = SyncHandle(plan=plan)
        handle.status = "done"
        handle.result = SyncResult(
            market=market,
            total=0,
            done=0,
            failed=0,
            quarantined=0,
            skipped_resumed=0,
            elapsed_ms=0,
            completed_codes=(),
            quarantined_codes=(),
            failed_codes=(),
        )
        return handle

    def _profile(self, market: str) -> MarketProfile:
        """取市场档案；未实现市场显式抛错（NF-5/NF-7）。"""
        profile = get_market_profile(market)
        if not profile.is_implemented:
            raise NotSupportedError(
                f"[fail-loud/NF-7] 市场 {market} 尚未实现（P1 占位），"
                f"拒绝用 A 股规则代替。可用市场: {sorted(known_markets())}"
            )
        return profile

    @staticmethod
    def _assert_backfill_allowed(profile: MarketProfile) -> None:
        """交易时段禁全量回补（NF-28，错峰防资源争用）。

        仅拦截「交易时段内发起全量回补」；盘后/盘前/周末放行。
        用 :meth:`MarketProfile.is_open_now` 判定，不硬编码时段（NF-5）。
        """
        if profile.is_open_now():
            raise NotSupportedError(
                f"[fail-loud/NF-28] 交易时段内禁止全量回补（{profile.market} 正在撮合）。"
                f"请收盘后重试，或用 --force 显式跳过该限制（会与实时行情争用服务器）。"
            )

    def _build_engine(self) -> SyncEngine:
        """构造回补引擎（worker 级连接缓存 + SQLite 断点表，T03 性能）。"""

        def _fetcher_factory() -> QuotationFetcher:
            # 每个 worker 独立 QuotationFetcher（new_mac_client 非池化，连接不共享）
            return QuotationFetcher(self._factory, shared_connection=False)

        return SyncEngine(
            fetcher_factory=_fetcher_factory,
            writer=self._writer,
            quarantine=self._quarantine,
            checkpoint_store=self._store,
        )
