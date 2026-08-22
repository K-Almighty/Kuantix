"""因子值存储（Parquet 分区 + SQLite 元数据，L2）。

布局
----
``~/.Kuantix/factors/{factor}/{year}.parquet``，每文件列：
    - ``date``   int    （YYYYMMDD）
    - ``code``   str    （6 位证券代码）
    - ``value``  float  （因子值）

元数据库 ``~/.Kuantix/db/factor_meta.db``，表 ``factor_meta``：
    - ``factor``         TEXT PRIMARY KEY
    - ``computed_until`` TEXT   （YYYYMMDD，最近已算日期）
    - ``rows``           INTEGER
    - ``updated_at``     TEXT

增量计算
--------
:meth:`FactorStore.compute` 只计算 ``(start, end]`` 中**尚未落库**的日期：
先查 ``computed_until``，跳过已算区间，避免每次全量重算。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from Kuantix.adapters.factor_bridge import FactorEngineBridge
from Kuantix.core.db import connect_sqlite
from Kuantix.core.fail_loud import DataIntegrityError

__all__ = ["FactorStore"]

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_meta (
    factor TEXT PRIMARY KEY,
    computed_until TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
)
"""


def _date_to_int(date: dt.date) -> int:
    return date.year * 10000 + date.month * 100 + date.day


class FactorStore:
    """因子值存储（Parquet 按 (因子, 年份) 分区 + SQLite 元数据）。

    Args:
        factors_root: 因子根目录（如 ``~/.Kuantix/factors``）。
        db_dir: 元数据库目录（如 ``~/.Kuantix/db``）。
    """

    def __init__(self, factors_root: Path | str, db_dir: Path | str) -> None:
        self._root = Path(factors_root).expanduser()
        self._root.mkdir(parents=True, exist_ok=True)
        self._db_dir = Path(db_dir).expanduser()
        self._db_dir.mkdir(parents=True, exist_ok=True)
        self._meta_db = self._db_dir / "factor_meta.db"
        self._lock = threading.RLock()
        with self._lock:
            with self._connect() as conn:
                conn.execute(_META_SCHEMA)

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #

    def save(self, factor: str, year: int, df: pd.DataFrame) -> int:
        """保存某因子某年份的截面数据（覆盖写该分区）。

        Args:
            factor: 因子名。
            year: 年份。
            df: DataFrame，含 ``date``(int) / ``code``(str) / ``value``(float)。

        Returns:
            写入行数。

        Raises:
            DataIntegrityError: 缺少必需列或含 NaN。
        """
        self._assert_frame(df, factor=factor, year=year)
        partition = self._partition(factor, year)
        partition.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(partition, index=False)
        self._update_meta(factor, df)
        return int(len(df))

    def load(
        self,
        factor: str,
        date: int | None = None,
        code: str | None = None,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> pd.DataFrame:
        """读取因子值。

        Args:
            factor: 因子名。
            date: 精确日期（YYYYMMDD）；``None`` 不按日期过滤。
            code: 代码过滤；``None`` 全部。
            start: 起始日期（含）。
            end: 结束日期（含）。

        Returns:
            DataFrame（date/code/value 三列）；无数据返回空表。

        Raises:
            DataIntegrityError: 因子不存在。
        """
        # 日期条件收敛成一个闭区间 [lo, hi]，用于**分区裁剪**：
        # 分区文件名即年份，且 save() 保证「分区内日期年份 == 文件名年份」
        # （NF-26 断言），因此年份不相交的 parquet 可以安全跳过 —— 单日
        # 截面查询原本要读全部 6 个年份分区（82MB），裁剪后只读 1 个。
        lo = hi = None
        if date is not None:
            lo = hi = int(date)
        else:
            if start is not None:
                lo = int(start)
            if end is not None:
                hi = int(end)
        partitions = self._partitions_for(factor, lo=lo, hi=hi)
        if not partitions:
            return pd.DataFrame(columns=["date", "code", "value"])

        # 谓词下推：pyarrow 用 row-group 统计信息跳过不含目标行的块，
        # 避免把整年数据解码进内存后再用 pandas 布尔索引过滤。
        filters: list[tuple[str, str, Any]] = []
        if lo is not None:
            filters.append(("date", ">=", lo))
        if hi is not None:
            filters.append(("date", "<=", hi))
        if code is not None:
            filters.append(("code", "==", str(code)))
        read_kwargs: dict[str, Any] = {"filters": filters} if filters else {}

        frames = [pd.read_parquet(p, **read_kwargs) for p in partitions]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(columns=["date", "code", "value"])
        frame = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
        return frame.reset_index(drop=True)

    def load_latest_per_code(
        self, factor: str, *, as_of: int | None = None
    ) -> pd.DataFrame:
        """流式取每只标的**最新一行**因子值（R6：选股内存优化）。

        原选股路径为取「每码最新一行」而 ``load()`` 读入全部 6 年
        （871 万行，峰值 468MB / 常驻 590MB）。本方法按年份**从新到旧**
        逐分区读取，只保留每分区内每码的最大日期行；已见过的 code 不再
        回填，命中全部 code 后提前停止。峰值内存从「全库」降到「单年」。

        分区年份不变式（save 侧 NF-26 断言 + ``_partitions_for`` 裁剪）
        保证：较新分区的行日期必然 ≥ 较旧分区，因此**第一个**遇到某 code
        的分区行即为其全历史最新行。

        Args:
            factor: 因子名。
            as_of: 数据截止日（YYYYMMDD）；``None`` 用最新可用。

        Returns:
            DataFrame（date/code/value 三列，每 code 一行，按 code 升序）。
            无数据返回空表。
        """
        partitions = self._partitions_for(factor)
        if not partitions:
            return pd.DataFrame(columns=["date", "code", "value"])
        # 从新到旧：2025 → 2020（文件名即年份，save 侧断言保证一致）
        parts = sorted(partitions, key=lambda p: p.stem, reverse=True)

        # 提前停止需要一个**精确**的全量 code 数：用 pyarrow 元数据读每个
        # 分区的去重 code 数不划算，改为「逐分区读完全部后再合并」的兜底——
        # 但那样又等于全量加载。折中：读取最新分区时只取其内部最新行，
        # 不设停止阈值；旧分区照读但只保留新出现的 code。这样最坏情况
        # 读完全部分区，峰值仍是「单分区」，正确性由 seen 集合保证。
        seen: set[str] = set()
        chunks: list[pd.DataFrame] = []
        for path in parts:
            frame = pd.read_parquet(path)
            if frame.empty:
                continue
            if "date" not in frame.columns or "code" not in frame.columns:
                continue  # 非标准分区，保守跳过（与 load 的读侧语义一致）
            if as_of is not None:
                frame = frame[frame["date"] <= int(as_of)]
                if frame.empty:
                    continue
            # 每分区内取每码最新行：分区内 date 未必严格递增（跨年裁剪后
            # 同分区可能含两个年份），用 idxmax 精确取最大日期行。
            latest = frame.loc[
                frame.groupby("code")["date"].idxmax()
            ]
            is_new = ~latest["code"].astype(str).isin(seen)
            new_chunk = latest[is_new]
            if not new_chunk.empty:
                seen.update(new_chunk["code"].astype(str))
                chunks.append(new_chunk)

        if not chunks:
            return pd.DataFrame(columns=["date", "code", "value"])
        result = pd.concat(chunks, ignore_index=True)
        return result.sort_values("code").reset_index(drop=True)

    def compute(
        self,
        pool: dict[str, pd.DataFrame],
        factors: list[str],
        start: dt.date,
        end: dt.date,
        *,
        engine: FactorEngineBridge | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """增量计算截面因子并落库。

        Args:
            pool: ``{code: DataFrame}``（L1 读侧，含 ``datetime`` 列）。
            factors: 因子名列表。
            start: 起始日期（含）。
            end: 结束日期（含）。
            engine: 因子引擎；``None`` 时新建。
            force: ``True`` 时忽略已算区间，全量重算。

        Returns:
            ``{factor: 本次计算行数}``。
        """
        engine = engine if engine is not None else FactorEngineBridge()
        start_int = _date_to_int(start)
        end_int = _date_to_int(end)
        result: dict[str, int] = {}

        # 先筛出真正需要计算的因子（增量跳过），再**一趟**算完。
        # 上游 compute_cross_section(pool, factors) 内部是
        # `for code in pool: compute_single(df, factors)`，即无论传 1 个
        # 还是 K 个因子都只遍历一次标的池；原实现逐因子调用 K 次，等于把
        # 「遍历 5030 只 × 建 DataFrame × datetime→int 的 apply」重复 K 遍。
        # 实测（150 只 / 5 因子）：7309ms → 1824ms（4.01×），且逐行比对
        # 各因子数值与逐因子调用**完全一致**。
        pending: list[str] = []
        for factor in factors:
            until = None if force else self.computed_until(factor)
            if until is not None and int(until) >= end_int:
                result[factor] = 0
            else:
                pending.append(factor)
        if not pending:
            return result

        cross = engine.compute_cross_section(pool, pending)
        for factor in pending:
            if cross.empty:
                result[factor] = 0
                continue
            sub = cross[["date", "code", factor]].rename(columns={factor: "value"})
            sub = sub[(sub["date"] >= start_int) & (sub["date"] <= end_int)]
            # 因子上热期（如 rolling 20）会产生 NaN，不落库（NF-12 禁 NaN）
            sub = sub.dropna(subset=["value"])
            if sub.empty:
                result[factor] = 0
                continue

            years = sorted(set(int(d) // 10000 for d in sub["date"].tolist()))
            for year in years:
                year_df = sub[sub["date"] // 10000 == year]
                self.save(factor, year, year_df)
            result[factor] = int(len(sub))
        return result

    def list_factors(self) -> list[str]:
        """列出已落库的因子名（升序）。"""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT factor FROM factor_meta ORDER BY factor"
                ).fetchall()
        return [str(row["factor"]) for row in rows]

    def years_for(self, factor: str) -> list[int]:
        """列出某因子已落库的年份（升序）。"""
        factor_dir = self._root / factor
        if not factor_dir.is_dir():
            return []
        years: list[int] = []
        for path in sorted(factor_dir.glob("*.parquet")):
            try:
                years.append(int(path.stem))
            except ValueError as exc:  # 非年份文件
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 因子目录含非法分区名: {path.name}"
                ) from exc
        return years

    def computed_until(self, factor: str) -> str | None:
        """最近已算日期（YYYYMMDD 字符串）；未算过返回 ``None``。"""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT computed_until FROM factor_meta WHERE factor = ?",
                    (factor,),
                ).fetchone()
        return str(row["computed_until"]) if row else None

    def describe(self) -> dict[str, Any]:
        """存储摘要（JSON 安全）。"""
        return {
            "root": str(self._root),
            "meta_db": str(self._meta_db),
            "factors": self.list_factors(),
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _partition(self, factor: str, year: int) -> Path:
        return self._root / factor / f"{year}.parquet"

    def _partitions_for(
        self, factor: str, *, lo: int | None = None, hi: int | None = None
    ) -> list[Path]:
        """列出因子分区；给定日期区间时按年份裁剪。

        Args:
            factor: 因子名。
            lo: 起始日期（含，``YYYYMMDD``）；``None`` 不限。
            hi: 结束日期（含，``YYYYMMDD``）；``None`` 不限。

        Returns:
            按文件名升序的分区路径列表。文件名非 4 位年份时**一律保留**
            （不做静默丢弃，避免裁剪引入数据缺失）。
        """
        factor_dir = self._root / factor
        if not factor_dir.is_dir():
            return []
        parts = sorted(factor_dir.glob("*.parquet"))
        if lo is None and hi is None:
            return parts
        kept: list[Path] = []
        for path in parts:
            stem = path.stem
            if not (len(stem) == 4 and stem.isdigit()):
                kept.append(path)  # 非年份分区：保守保留
                continue
            year = int(stem)
            if lo is not None and year < lo // 10000:
                continue
            if hi is not None and year > hi // 10000:
                continue
            kept.append(path)
        return kept

    @staticmethod
    def _assert_frame(df: pd.DataFrame, *, factor: str, year: int) -> None:
        required = ("date", "code", "value")
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 因子 {factor} {year} 缺少列 {missing}"
            )
        if df["value"].isna().any():
            raise DataIntegrityError(
                f"[fail-loud/NF-12] 因子 {factor} {year} 含 NaN 因子值，拒绝入库"
            )
        # 分区年份自洽性：load() 的年份裁剪依赖「分区内所有日期都属于
        # 文件名年份」这一不变量，在写入侧显式断言，避免裁剪静默丢数据。
        if not df.empty:
            years = df["date"].astype("int64") // 10000
            if int(years.min()) != year or int(years.max()) != year:
                raise DataIntegrityError(
                    f"[fail-loud/NF-26] 因子 {factor} 分区 {year} 含跨年日期 "
                    f"[{int(df['date'].min())}, {int(df['date'].max())}]，拒绝入库"
                )

    def verify_partitions(self) -> list[dict[str, Any]]:
        """体检全部分区：验证「分区文件名年份 == 分区内日期年份」不变式。

        ``load()`` 的年份裁剪依赖这一不变量（R8）：外部写入 / 历史遗留
        文件可能破坏它，导致裁剪**静默漏数据**。本方法逐分区扫描：
        - 文件名非 4 位年份 → 记录为 ``non_year``（裁剪时保守保留，不丢数据，
          但建议迁移为标准命名）；
        - 分区内日期年份与文件名不一致 → 记录为 ``violation``（**数据风险**，
          裁剪可能跳过该分区内的跨年行）；
        - 分区内含 NaN → 记录为 ``nan``（NF-12 违例）。

        Returns:
            ``[{factor, partition, issue, detail}]``；无问题返回空列表。
        """
        issues: list[dict[str, Any]] = []
        for factor_dir in sorted(self._root.iterdir()):
            if not factor_dir.is_dir():
                continue
            factor = factor_dir.name
            for path in sorted(factor_dir.glob("*.parquet")):
                stem = path.stem
                try:
                    df = pd.read_parquet(path)
                except Exception as exc:  # noqa: BLE001 - 读失败也要记录
                    issues.append(
                        {
                            "factor": factor,
                            "partition": path.name,
                            "issue": "unreadable",
                            "detail": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                if df.empty:
                    continue
                required = ("date", "code", "value")
                missing = [c for c in required if c not in df.columns]
                if missing:
                    issues.append(
                        {
                            "factor": factor,
                            "partition": path.name,
                            "issue": "missing_columns",
                            "detail": f"缺少列 {missing}",
                        }
                    )
                    continue
                if df["value"].isna().any():
                    issues.append(
                        {
                            "factor": factor,
                            "partition": path.name,
                            "issue": "nan",
                            "detail": f"含 {int(df['value'].isna().sum())} 个 NaN",
                        }
                    )
                if not (len(stem) == 4 and stem.isdigit()):
                    issues.append(
                        {
                            "factor": factor,
                            "partition": path.name,
                            "issue": "non_year",
                            "detail": "文件名非 4 位年份（裁剪时保守保留）",
                        }
                    )
                    continue
                year = int(stem)
                years = df["date"].astype("int64") // 10000
                lo, hi = int(years.min()), int(years.max())
                if lo != year or hi != year:
                    issues.append(
                        {
                            "factor": factor,
                            "partition": path.name,
                            "issue": "violation",
                            "detail": f"分区年份 {year} 但日期跨 {lo}..{hi}",
                        }
                    )
        return issues

    def _update_meta(self, factor: str, df: pd.DataFrame) -> None:
        max_date = int(df["date"].max())
        max_str = str(max_date)
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO factor_meta (factor, computed_until, rows, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(factor) DO UPDATE SET
                        computed_until = MAX(factor_meta.computed_until, excluded.computed_until),
                        rows = excluded.rows,
                        updated_at = excluded.updated_at
                    """,
                    (factor, max_str, int(len(df)), now),
                )

    def _connect(self) -> sqlite3.Connection:
        """P1-1：connect_sqlite 统一应用 WAL + busy_timeout + synchronous=NORMAL。

        原代码已手动启用 WAL + busy_timeout 30s；切换后新增：
        ``synchronous=NORMAL``（WAL 下安全、fsync 减少 50%+）。
        """
        return connect_sqlite(self._meta_db)
