"""回测结果存储（SQLite，job_id → 完整结果 JSON）。

与 :mod:`Kuantix.api.jobs` 的 jobs 表解耦：jobs 表只存轻量
``result_summary``（绩效摘要），完整结果（净值序列 + 成交明细，可达数十
KB）落本表，避免大 JSON 塞进 jobs 表拖慢轮询。

- 表 ``backtest_results(job_id TEXT PRIMARY KEY, result_json TEXT,
  created_at TEXT)``；
- 读不到返回 ``None``（合法态，由路由层映射 404）；
- 全库禁 ``dict.get(k, 默认值)``（R4-B）。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from Kuantix.core.db import apply_sqlite_pragmas

__all__ = ["BacktestResultStore", "DEFAULT_DB_FILENAME"]

#: 数据库文件名（位于 ``config.paths.db`` 下）
DEFAULT_DB_FILENAME = "backtest_results.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_results (
    job_id TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class BacktestResultStore:
    """回测完整结果的 SQLite 存储（线程安全，单连接 + RLock）。

    Args:
        db_path: 数据库文件路径；``None`` 时取 ``config.paths.db``。
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            from Kuantix.config import get_config

            config = get_config()
            self._path = Path(config.paths.db) / DEFAULT_DB_FILENAME
        else:
            self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # P1-1：统一并发基线（替代 4 行散写 PRAGMA）
        apply_sqlite_pragmas(self._conn)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @property
    def path(self) -> Path:
        """数据库文件路径。"""
        return self._path

    def close(self) -> None:
        """关闭连接（幂等）。"""
        with self._lock:
            self._conn.close()

    def save(self, job_id: str, result: dict[str, Any]) -> None:
        """保存/覆盖某 job 的完整结果。

        Args:
            job_id: 关联的 Job id。
            result: JSON 安全的结果字典（已由适配层清洗 NaN/Inf）。
        """
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO backtest_results (job_id, result_json, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    str(job_id),
                    json.dumps(result, ensure_ascii=False),
                    _now_iso(),
                ),
            )
            self._conn.commit()

    def load(self, job_id: str) -> dict[str, Any] | None:
        """按 job_id 读取完整结果；不存在返回 ``None``（合法态）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM backtest_results WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["result_json"]))

    def delete(self, job_id: str) -> bool:
        """删除结果；返回是否确实删除了。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM backtest_results WHERE job_id = ?", (str(job_id),)
            )
            self._conn.commit()
        return cursor.rowcount > 0
