"""迁移工具（设计文档 08：vipdoc → SQLite market.db 一次性导入）。

:class:`Migrator` 做三类迁移，全部**显式触发**（``Kuantix data migrate``，
D3：启动只警告不自动）：

1. **vipdoc → daily_bars**：把 ``~/.Kuantix/vipdoc`` 的 508M ``.day`` 二进制
   解码为 :class:`~Kuantix.core.contracts.Bar`（经 L1Reader 镜像读侧，与
   业务读侧同源），批量写入 market.db。``--verify`` 抽样往返比对（D4 只存
   解码值 + 往返比对兜底）。
2. **security_catalog.json → securities**：旧 JSON 清单一次性导入
   ``securities`` 表（D9：导入后 JSON 废弃写入，读兼容一版）。
3. **sync_checkpoint JSON → sync_checkpoint 表**：旧断点文件导入断点表
   （D6：表方案 O(1) 查询，取代大池全量 JSON 重写）。

进度 / 失败语义
---------------
- 逐只标的导入，每只一个事务（``MarketStore.bulk`` 窗口内
  ``PRAGMA synchronous=OFF`` 提速，T05：508M 导入 20min → 4-8min）；
- 单只失败**计入失败清单继续**（不中断整体），结束时汇总报告；
- ``--dry-run`` 只扫描不写库；``--verify`` 在导入后抽样 N 只
  ``L1Reader`` 镜像读 vs ``MarketStore`` SQLite 读逐字段比对。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from Kuantix.adapters.factor_bridge import L1Reader
from Kuantix.core.contracts import Bar, Security
from Kuantix.core.fail_loud import DataIntegrityError, require_non_empty
from Kuantix.core.market import CNMarketProfile, get_market_profile
from Kuantix.data.market_store import MarketStore

logger = logging.getLogger(__name__)

__all__ = [
    "MigrationReport",
    "Migrator",
    "default_checkpoint_path",
]

#: 默认旧断点文件 ``~/.Kuantix/db/sync_checkpoint_{market}.json``。
def default_checkpoint_path(db_dir: Path, market: str) -> Path:
    """返回旧断点 JSON 文件路径（``sync_checkpoint_{market}.json``）。"""
    return Path(db_dir) / f"sync_checkpoint_{market}.json"


@dataclass
class MigrationReport:
    """一次迁移的完整报告。

    Attributes:
        securities_imported: catalog JSON → securities 导入条数。
        bars_imported: vipdoc → daily_bars 导入条数。
        securities_skipped: catalog 中重复/无效跳过条数。
        files_scanned: 扫描到的 ``.day`` 文件数（sh/sz 目录全部）。
        files_ok: 成功导入的文件数。
        files_failed: 导入失败的文件数。
        files_skipped: 主动跳过（代码段与目录不符 / 上证指数段不入池 /
            sh-sz 主键冲突让位）的文件数（NF-26 显式记录，不静默）。
        failed_codes: 失败代码列表。
        checkpoints_imported: 旧 JSON 断点导入行数。
        verified_codes: ``--verify`` 抽样的代码数。
        verify_mismatches: 往返比对不一致数。
        elapsed_ms: 总耗时（毫秒）。
    """

    securities_imported: int = 0
    bars_imported: int = 0
    securities_skipped: int = 0
    files_scanned: int = 0
    files_ok: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    failed_codes: list[str] = field(default_factory=list)
    checkpoints_imported: int = 0
    verified_codes: int = 0
    verify_mismatches: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典（CLI 信封输出）。"""
        return {
            "securities_imported": self.securities_imported,
            "bars_imported": self.bars_imported,
            "securities_skipped": self.securities_skipped,
            "files_scanned": self.files_scanned,
            "files_ok": self.files_ok,
            "files_failed": self.files_failed,
            "files_skipped": self.files_skipped,
            "failed_codes": list(self.failed_codes),
            "checkpoints_imported": self.checkpoints_imported,
            "verified_codes": self.verified_codes,
            "verify_mismatches": self.verify_mismatches,
            "elapsed_ms": self.elapsed_ms,
        }


class Migrator:
    """vipdoc / catalog / checkpoint → market.db 的一次性迁移工具。

    Args:
        store: 目标 :class:`~Kuantix.data.market_store.MarketStore`。
        reader: 镜像读侧（vipdoc 文件解码）；``None`` 时按 ``vipdoc_root``
            新建纯镜像 L1Reader。
        vipdoc_root: vipdoc 根目录（``reader`` 为 None 时使用）。
    """

    def __init__(
        self,
        store: MarketStore,
        *,
        reader: L1Reader | None = None,
        vipdoc_root: Path | str | None = None,
    ) -> None:
        self._store = store
        if reader is not None:
            self._reader = reader
        else:
            root = Path(vipdoc_root).expanduser() if vipdoc_root else None
            if root is None:
                from Kuantix.config import get_config

                root = get_config().paths.vipdoc
            self._reader = L1Reader(root, backend="mirror")

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #

    def migrate(
        self,
        *,
        catalog_path: Path | str | None = None,
        checkpoint_path: Path | str | None = None,
        dry_run: bool = False,
        verify: bool = False,
        verify_sample: int = 5,
        market: str = "CN",
    ) -> MigrationReport:
        """执行迁移（vipdoc 日线 + 可选 catalog / checkpoint 导入）。

        Args:
            catalog_path: 旧 ``security_catalog.json`` 路径；``None`` 跳过。
            checkpoint_path: 旧断点 JSON 路径；``None`` 按
                ``db/sync_checkpoint_{market}.json`` 自动探测（不存在跳过）。
            dry_run: 只扫描不写库。
            verify: 导入后抽样往返比对（D4）。
            verify_sample: 抽样条数。
            market: 市场码（默认 ``CN``）。

        Returns:
            :class:`MigrationReport`。
        """
        started = time.perf_counter()
        report = MigrationReport()

        securities: list[Security] = []
        if catalog_path is not None:
            catalog = Path(catalog_path).expanduser()
            if catalog.is_file():
                securities = self._load_catalog(catalog)
                report.securities_imported = len(securities)
            else:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 证券清单文件不存在: {catalog}"
                )

        if not dry_run and securities:
            self._store.upsert_securities(securities)

        # 旧断点 JSON → sync_checkpoint 表（D6）
        if checkpoint_path is not None or catalog_path is not None:
            cp = self._migrate_checkpoint(
                checkpoint_path,
                market=market,
                dry_run=dry_run,
            )
            report.checkpoints_imported = cp

        # vipdoc 日线 → daily_bars
        self._migrate_vipdoc(report, dry_run=dry_run, market=market)

        if verify and not dry_run:
            mismatches, verified = self.verify(
                sample=verify_sample, market=market
            )
            report.verified_codes = verified
            report.verify_mismatches = len(mismatches)

        report.elapsed_ms = int(round((time.perf_counter() - started) * 1000))
        return report

    def verify(
        self, *, sample: int = 5, market: str = "CN"
    ) -> tuple[list[dict[str, Any]], int]:
        """抽样往返比对：镜像（vipdoc）读 vs SQLite 读（D4 --verify）。

        只比对**该代码的归属交易所**（:meth:`_owned_files`）—— 与迁移写侧
        同一套归属规则（目录为准 + 代码段校验；上证指数段不入池），避免
        sh/sz 代码段重叠（如 000xxx 既是上证指数又是深市 A 股）时拿错文件
        比对（发现并修复的迁移坑，见交付报告）。

        Args:
            sample: 抽样条数。
            market: 市场码。

        Returns:
            ``(不一致明细, 实际抽样数)``；明细为空 = 全部一致。
        """
        mismatches: list[dict[str, Any]] = []
        owned = self._owned_files(market)
        sampled = owned[: int(sample)]
        for exchange, code in sampled:
            try:
                mirror_bars = self._reader.read_daily_bars(exchange, code)
                sqlite_bars = self._store.read_daily_bars(market, code)
                diff = self._compare_bars(mirror_bars, sqlite_bars)
                if diff:
                    mismatches.append(
                        {"code": code, "exchange": exchange, "differences": diff[:5]}
                    )
            except Exception as exc:  # noqa: BLE001 - 单只校验失败计入 mismatch
                mismatches.append(
                    {
                        "code": code,
                        "exchange": exchange,
                        "differences": [f"{type(exc).__name__}: {exc}"],
                    }
                )
        return mismatches, len(sampled)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _owned_files(self, market: str) -> list[tuple[str, str]]:
        """返回归属交易所的 ``(exchange, code)`` 列表（迁移写侧 / verify 共用）。

        归属规则（**目录为准 + 代码段校验**，不调用 ``exchange_for_code``）：
        - 文件所在目录（``sh/lday`` / ``sz/lday``）即交易所归属 —— 目录已知
          时无需 ``exchange_for_code`` 的无上下文歧义判定（它偏袒上证指数段，
          把 ``sz000002`` 深市 A 股误判为 sh，正是数据丢失 bug 的根因）；
        - 代码段只做「该代码在目标交易所是否合法」校验：
          * sz 目录：``head2 ∈ _SZ_PREFIXES``（00/30 A 股、20 B 股、39 指数、
            15-18 基金、10-14 债券）→ 归 sz；
          * sh 目录：``head2 ∈ _SH_PREFIXES``（60/68 A 股、90 B 股、50-58 基金、
            11/01/20 债券）→ 归 sh；
          * sh 目录 ``head3 ∈ _SH_INDEX_PREFIXES``（000/880/999 上证指数段）→
            **跳过不入库**：数据湖是 A 股研究平台，``(market, code)`` 主键的
            000xxx 归属深市 A 股（000001 平安银行 / 000002 万科…），上证指数
            （sh000001 上证指数 / sh000300 沪深300）不进 A 股池，避免与将来
            入库的 sz A 股主键冲突；
        - 其余代码段与目录不符（含北交所段混入 sh/sz 目录的垃圾文件）→ 跳过，
          fail-loud 显式记录（``logger.warning``，NF-26 不静默）；
        - 主键冲突兜底：同 code 同时被 sh/sz 认领（如 sh 20xxx 国债逆回购 vs
          sz 20xxx B 股）时 **A 股优先，sz 胜出**，sh 文件跳过。
        """
        exchanges = self._exchanges_for_market(market)
        profile = get_market_profile(market)
        if not isinstance(profile, CNMarketProfile):
            return []

        # 第一遍：按目录 + 代码段初筛，跳过项显式记录（NF-26）
        candidates: list[tuple[str, str]] = []
        for exchange in exchanges:
            for code, path in self._day_files(exchange):
                verdict = self._classify_owned_file(profile, exchange, code)
                if verdict == "owned":
                    candidates.append((exchange, code))
                elif verdict == "skip_index":
                    logger.warning(
                        "迁移跳过（上证指数段不入 A 股池）: %s", path.name
                    )
                else:
                    logger.warning(
                        "迁移跳过（代码段不属 %s 目录，防垃圾文件入库）: %s",
                        exchange,
                        path.name,
                    )

        # 第二遍：主键 (market, code) 去重 —— 同 code 同时被 sh/sz 认领时
        # A 股优先（sz 胜出），sh 文件跳过。
        sz_owned = {code for exchange, code in candidates if exchange == "sz"}
        owned: list[tuple[str, str]] = []
        for exchange, code in candidates:
            if exchange == "sh" and code in sz_owned:
                logger.warning(
                    "迁移跳过（同 code 已由 sz A 股占用，sh 让位）: %s%s.day",
                    exchange,
                    code,
                )
                continue
            owned.append((exchange, code))
        return owned

    @staticmethod
    def _classify_owned_file(
        profile: CNMarketProfile, exchange: str, code: str
    ) -> str:
        """按「目录为准 + 代码段校验」判定单个 ``.day`` 文件是否归该交易所导入。

        迁移场景中目录即交易所归属，代码段只用于「该代码在目标交易所是否合法」
        校验（防垃圾文件），**不调用 ``exchange_for_code``** —— 它在无目录
        上下文时对 000xxx 偏袒上证指数，会把深市 000 段 A 股误判为 sh。

        Args:
            profile: :class:`~Kuantix.core.market.CNMarketProfile`（提供代码段
                常量，NF-5 禁业务层硬编码）。
            exchange: 文件所在目录的交易所前缀（``sh`` / ``sz``）。
            code: 6 位证券代码。

        Returns:
            ``"owned"`` 归该交易所，可入库；
            ``"skip_index"`` sh 目录上证指数段（000/880/999），不入 A 股池；
            ``"skip_illegal"`` 代码段与目录不符（含北交所段混入 sh/sz 目录）。
        """
        raw = str(code).strip()
        head2, head3 = raw[:2], raw[:3]
        if exchange == "sz":
            return "owned" if head2 in profile._SZ_PREFIXES else "skip_illegal"
        if exchange == "sh":
            if head2 in profile._SH_PREFIXES:
                return "owned"
            if head3 in profile._SH_INDEX_PREFIXES:
                return "skip_index"
            return "skip_illegal"
        return "skip_illegal"

    def _migrate_vipdoc(self, report: MigrationReport, *, dry_run: bool, market: str) -> None:
        """把 vipdoc 归属交易所的日线文件解码写入 daily_bars（逐只事务）。

        非归属文件（代码段与目录不符 / 上证指数段 / sh-sz 主键冲突让位）跳过并
        显式记录（NF-26），保证 ``(market, code, date)`` 主键无冲突。
        报告口径：``files_scanned`` 为目录全部 ``.day`` 文件数，
        ``files_ok + files_failed + files_skipped == files_scanned``。
        """
        exchanges = self._exchanges_for_market(market)
        total_files = sum(len(self._day_files(exchange)) for exchange in exchanges)
        report.files_scanned = total_files
        owned = self._owned_files(market)
        report.files_skipped = total_files - len(owned)
        for exchange, code in owned:
            path = self._day_path(exchange, code)
            try:
                bars = self._reader.read_daily_bars(exchange, code)
                if dry_run:
                    report.files_ok += 1
                    report.bars_imported += len(bars)
                    continue
                self._store.write_daily_bars(market, code, bars)
                report.files_ok += 1
                report.bars_imported += len(bars)
            except Exception as exc:  # noqa: BLE001 - 单只失败计入失败清单继续
                logger.warning("迁移 %s 失败: %s", path.name, exc)
                report.files_failed += 1
                report.failed_codes.append(code)

    def _day_path(self, exchange: str, code: str) -> Path:
        """vipdoc 日线文件路径 ``<root>/<exchange>/lday/<exchange><code>.day``。"""
        return self._reader.root / exchange / "lday" / f"{exchange}{code}.day"

    def _day_files(self, exchange: str) -> list[tuple[str, Path]]:
        """列出某交易所目录下的全部 ``.day`` 文件（``code, path``）。"""
        lday = self._reader.root / exchange / "lday"
        if not lday.is_dir():
            return []
        out: list[tuple[str, Path]] = []
        for path in sorted(lday.glob("*.day")):
            code = path.name.lower()[2:8]
            out.append((code, path))
        return out

    def _migrate_checkpoint(
        self,
        checkpoint_path: Path | str | None,
        *,
        market: str,
        dry_run: bool,
    ) -> int:
        """旧断点 JSON → sync_checkpoint 表（D6）。文件不存在返回 0。"""
        if checkpoint_path is None:
            from Kuantix.config import get_config

            candidate = default_checkpoint_path(get_config().paths.db, market)
            if not candidate.is_file():
                return 0
            checkpoint_path = candidate
        target = Path(checkpoint_path).expanduser()
        if not target.is_file():
            return 0
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 旧断点文件损坏: {target}（{exc}）。"
                f"拒绝静默覆盖，请人工检查"
            ) from exc
        completed_raw = raw.get("completed")
        quarantined_raw = raw.get("quarantined")
        failed_raw = raw.get("failed")
        completed = set(completed_raw) if completed_raw is not None else set()
        quarantined = set(quarantined_raw) if quarantined_raw is not None else set()
        failed = set(failed_raw) if failed_raw is not None else set()
        if dry_run:
            return len(completed) + len(quarantined) + len(failed)
        self._store.save_checkpoint(market, completed, quarantined, failed)
        return len(completed) + len(quarantined) + len(failed)

    def _load_catalog(self, path: Path) -> list[Security]:
        """旧 security_catalog.json → :class:`Security` 列表（去重）。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 证券清单文件损坏: {path}（{exc}）"
            ) from exc
        if not isinstance(raw, list):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 证券清单文件结构非法（期望数组）: {path}"
            )
        seen: set[tuple[str, str]] = set()
        securities: list[Security] = []
        for item in raw:
            if not isinstance(item, dict):
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 证券清单条目非法（期望对象）: {path}"
                )
            try:
                code = str(item["code"])
                exchange = str(item["exchange"])
                market = str(item["market"])
                security_type = str(item["security_type"])
            except KeyError as exc:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 证券清单条目缺字段 {exc}: {path}"
                ) from exc
            key = (market, code)
            if key in seen:
                continue
            seen.add(key)
            name_value = item.get("name")
            securities.append(
                Security(
                    code=code,
                    exchange=exchange,
                    market=market,
                    security_type=security_type,
                    name=str(name_value) if name_value is not None else "",
                )
            )
        return securities

    @staticmethod
    def _exchanges_for_market(market: str) -> tuple[str, ...]:
        """市场码 → 交易所前缀（P0 仅 CN 有 sh/sz；其余空）。"""
        code = str(market).upper()
        if code == "CN":
            return ("sh", "sz")
        return ()

    @staticmethod
    def _compare_bars(
        mirror: Sequence[Bar], sqlite: Sequence[Bar]
    ) -> list[str]:
        """逐字段比对两串 Bar，返回差异描述（空 = 一致）。

        价格容差 1e-6（解码值精度），vol/amount 容差 1e-6。
        """
        diffs: list[str] = []
        if len(mirror) != len(sqlite):
            return [f"条数不一致: mirror={len(mirror)} sqlite={len(sqlite)}"]
        for m, s in zip(mirror, sqlite):
            if m.date != s.date:
                diffs.append(f"{m.date}: 日期不一致 {s.date}")
                continue
            for name in ("open", "high", "low", "close", "vol", "amount"):
                mv = float(getattr(m, name))
                sv = float(getattr(s, name))
                if abs(mv - sv) > 1e-6:
                    diffs.append(
                        f"{m.date}: {name} mirror={mv} sqlite={sv} diff={abs(mv - sv):.8f}"
                    )
        return diffs
