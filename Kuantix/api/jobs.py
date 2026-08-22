"""REST 长任务 Job 模型：持久化 + 后台执行（契约 §1.9）。

三类长任务（data sync / factor compute / screen run）统一走
:class:`JobManager`：

- 触发接口返回 **200 + Job 信封**（``queued`` → ``running`` → ``done``），
  不做 202（与 ``Kuantix.main.http_status_for`` 一致）；
- 进度经 :class:`JobStore` 持久化到 SQLite（``~/.Kuantix/db/jobs.db``），
  **重启不丢**；未知 job_id 查询返回 ``None``，由路由层映射为 404 信封
  （契约 D3/F3/S3）；
- ``status`` 生命周期：``queued → running → done | failed | cancelled``。

fail-loud（NF-26）：
- 任务异常**不允许静默**——:class:`FailLoudError` 按业务码记入
  ``error.code``（422/501 等），未预期异常按 500 记录，消息原样透传；
- 取消只允许在 ``running``/``queued`` 态发生，已结束任务取消抛
  :class:`~Kuantix.core.fail_loud.DataIntegrityError`（→ 422，契约 D4）。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from Kuantix.core.db import connect_sqlite
from Kuantix.core.envelope import CODE_INTERNAL_ERROR
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    FailLoudError,
    MissingKeyError,
)
from Kuantix.main import _code_for_exception

__all__ = [
    "JobCancelledError",
    "JobStore",
    "JobManager",
    "JobStatus",
]

#: job 状态生命周期
JobStatus = ("queued", "running", "done", "failed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    module TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    market TEXT NOT NULL,
    progress TEXT,
    result_summary TEXT,
    error_code INTEGER,
    error_message TEXT,
    params TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class JobCancelledError(Exception):
    """内部哨兵：runner 检测到任务被取消，交给 JobManager 落 ``cancelled`` 态。"""


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _ensure_tz(value: str) -> str:
    """把 ISO 字符串规范为带时区形式（naive 按本地时区解释，契约 §1.7）。"""
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed.isoformat(timespec="seconds")


def _normalize_progress(progress: dict[str, Any]) -> dict[str, Any]:
    """进度快照里的时间戳统一补时区（SyncProgress.to_dict 输出 naive）。"""
    out: dict[str, Any] = dict(progress)
    for key in ("started_at", "updated_at"):
        raw = out.get(key)
        if isinstance(raw, str) and raw:
            out[key] = _ensure_tz(raw)
    return out


class JobStore:
    """Job 的 SQLite 持久化（``~/.Kuantix/db/jobs.db``）。

    Args:
        db_dir: 数据库目录（如 ``~/.Kuantix/db``）。
        db_name: 数据库文件名，默认 ``jobs.db``。
    """

    def __init__(self, db_dir: Path | str, *, db_name: str = "jobs.db") -> None:
        self._dir = Path(db_dir).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._dir / db_name
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    @property
    def db_path(self) -> Path:
        """数据库文件路径。"""
        return self._db_path

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    def create(
        self,
        job_id: str,
        module: str,
        action: str,
        market: str,
        params: dict[str, Any],
    ) -> None:
        """写入一条 ``queued`` 状态的新 job。"""
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs
                    (job_id, module, action, status, market, progress,
                     result_summary, error_code, error_message, params,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    module,
                    action,
                    "queued",
                    market,
                    None,
                    None,
                    None,
                    None,
                    json.dumps(params, ensure_ascii=False),
                    now,
                    now,
                ),
            )

    def set_status(self, job_id: str, status: str) -> None:
        """更新状态（running / cancelled）。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (status, _now_iso(), job_id),
            )

    def set_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        """更新进度快照。"""
        normalized = _normalize_progress(progress)
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET progress = ?, updated_at = ? WHERE job_id = ?",
                (json.dumps(normalized, ensure_ascii=False), _now_iso(), job_id),
            )

    def set_done(self, job_id: str, result_summary: dict[str, Any] | None) -> None:
        """标记完成并写入结果摘要。"""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET status = 'done', result_summary = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    json.dumps(result_summary, ensure_ascii=False)
                    if result_summary is not None
                    else None,
                    _now_iso(),
                    job_id,
                ),
            )

    def set_error(self, job_id: str, code: int, message: str) -> None:
        """标记失败并记录错误（``error = {code, message}``）。"""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET status = 'failed', error_code = ?, error_message = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (int(code), message, _now_iso(), job_id),
            )

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def get(self, job_id: str) -> dict[str, Any] | None:
        """按 id 取 Job 字典；不存在返回 ``None``。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def delete(self, job_id: str) -> bool:
        """按 id 删除 job 行；不存在返回 ``False``，存在并已删除返回 ``True``。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        return cur.rowcount > 0

    def latest(self, module: str, market: str | None = None) -> dict[str, Any] | None:
        """最近一次指定模块（可选市场）的 job；无记录返回 ``None``。"""
        with self._connect() as conn:
            if market is None:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE module = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (module,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE module = ? AND market = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (module, market),
                ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def list(
        self,
        module: str | None = None,
        market: str | None = None,
        limit: int = 20,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """按条件列出 job（C1 依赖，``/backtest/jobs`` 列表端点）。

        Args:
            module: 模块过滤（``data``/``factor``/``screen``/``monitor``/
                ``backtest``）；``None`` 表示全部。
            market: 市场码过滤；``None`` 表示全部。
            limit: 返回条数上限（1..50，超限报 400，fail-loud）。
            status: 状态过滤（``queued``/``running``/``done``/``failed``/
                ``cancelled``）；``None`` 表示全部。

        Returns:
            按 ``created_at`` 倒序的 Job 字典列表。

        Raises:
            MissingKeyError: ``limit`` 越界或 ``status`` 非法（→ 400）。
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise MissingKeyError(
                f"[fail-loud/NF-26] jobs.list.limit 必须是整数，实际为 {limit!r}"
            )
        if limit < 1 or limit > 50:
            raise MissingKeyError(
                f"[fail-loud/NF-26] jobs.list.limit 越界 [{1}, {50}]，"
                f"实际 {limit}（拒绝静默截断）"
            )
        if status is not None and status not in JobStatus:
            raise MissingKeyError(
                f"[fail-loud/NF-26] jobs.list.status 非法: {status!r}"
                f"（允许 {list(JobStatus)}）"
            )
        where: list[str] = []
        params_list: list[Any] = []
        if module is not None:
            where.append("module = ?")
            params_list.append(str(module))
        if market is not None:
            where.append("market = ?")
            params_list.append(str(market))
        if status is not None:
            where.append("status = ?")
            params_list.append(str(status))
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params_list.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params_list)).fetchall()
        return [self._row_to_job(row) for row in rows]

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
        progress_raw = row["progress"]
        result_raw = row["result_summary"]
        error = None
        if row["error_code"] is not None:
            error = {
                "code": int(row["error_code"]),
                "message": str(row["error_message"]),
            }
        return {
            "job_id": str(row["job_id"]),
            "module": str(row["module"]),
            "action": str(row["action"]),
            "status": str(row["status"]),
            "market": str(row["market"]),
            "progress": json.loads(str(progress_raw)) if progress_raw else None,
            "result_summary": json.loads(str(result_raw)) if result_raw else None,
            "error": error,
            "created_at": _ensure_tz(str(row["created_at"])),
            "updated_at": _ensure_tz(str(row["updated_at"])),
        }

    def _connect(self) -> sqlite3.Connection:
        """P1-1：connect_sqlite 统一应用 WAL + busy_timeout 30s + synchronous=NORMAL。

        原代码已有前两项；切换后新增 ``synchronous=NORMAL``（WAL 下安全，
        fsync 次数大幅降低，Job 状态频繁写更流畅）。
        """
        return connect_sqlite(self._db_path)


class JobManager:
    """Job 编排：提交后台任务 + 持久化状态 + 取消。

    Args:
        store: :class:`JobStore` 实例。
    """

    def __init__(self, store: JobStore) -> None:
        self._store = store
        self._handles: dict[str, Any] = {}
        self._lock = threading.RLock()

    @property
    def store(self) -> JobStore:
        """底层持久化。"""
        return self._store

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #

    def submit(
        self,
        module: str,
        action: str,
        market: str,
        params: dict[str, Any],
        runner: Callable[
            [Callable[[dict[str, Any]], None], Callable[[Any], None]],
            dict[str, Any] | None,
        ],
        *,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """提交一个后台任务并立即返回 Job 信封（``queued``）。

        Args:
            module: ``data`` / ``factor`` / ``screen`` / ``backtest``。
            action: ``sync_full`` / ``sync_incremental`` / ``compute`` / ``run``。
            market: 市场码。
            params: 触发参数（写入 job 行，供排障/断点续传）。
            runner: 后台执行体 ``runner(progress_cb, register_handle)``：
                - ``progress_cb(dict)`` —— 上报进度快照；
                - ``register_handle(handle)`` —— 注册可取消句柄（如 SyncHandle）；
                - 返回值作为 ``result_summary``；抛 :class:`JobCancelledError`
                  表示取消；抛 :class:`FailLoudError` 按业务码记 error；
                  其余异常按 500 记。
            job_id: 显式指定 job id（回测等需要 runner 内按 id 落结果时使用）；
                ``None`` 时自动生成。向后兼容，现有调用方不受影响。

        Returns:
            Job 字典（status 为 ``queued`` 或已快速推进到 ``running``）。
        """
        if job_id is None:
            job_id = f"job_{uuid.uuid4().hex[:12]}"
        self._store.create(job_id, module, action, market, params)
        thread = threading.Thread(
            target=self._execute,
            args=(job_id, runner),
            name=f"Kuantix-job-{module}-{action}",
            daemon=True,
        )
        thread.start()
        return self._store.get(job_id)

    def get(self, job_id: str) -> dict[str, Any] | None:
        """取 Job；不存在返回 ``None``。"""
        return self._store.get(job_id)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """取消一个仍在运行的任务。

        Returns:
            取消后的 Job 字典；job 不存在返回 ``None``（路由层映射 404）。

        Raises:
            DataIntegrityError: 任务已结束（done/failed/cancelled），不可取消（→ 422）。
        """
        job = self._store.get(job_id)
        if job is None:
            return None
        if job["status"] in ("done", "failed", "cancelled"):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] job {job_id} 已结束（{job['status']}），不能取消"
            )
        with self._lock:
            handle = self._handles.get(job_id)
        if handle is not None:
            cancel = getattr(handle, "cancel", None)
            if callable(cancel):
                cancel()
        self._store.set_status(job_id, "cancelled")
        return self._store.get(job_id)

    def delete_job(self, job_id: str) -> bool:
        """删除指定 job：清理内存句柄 + 持久化行；不存在返回 ``False``。

        与 :meth:`cancel` 不同，删除会直接移除记录（含已结束任务），不触发
        取消逻辑。仍可能运行的后台线程在下次写库会发现 job 已不存在并自然结束。
        幂等：重复删除返回 ``False``（路由层据此映射 404）。
        """
        with self._lock:
            self._handles.pop(job_id, None)
            return self._store.delete(job_id)

    def latest(self, module: str, market: str | None = None) -> dict[str, Any] | None:
        """最近一次指定模块的 job（D1 的 ``latest_job``）。"""
        return self._store.latest(module, market)

    def list_jobs(
        self,
        module: str | None = None,
        market: str | None = None,
        limit: int = 20,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """按条件列出 job（转发到 :meth:`JobStore.list`）。

        Raises:
            MissingKeyError: ``limit`` 越界或 ``status`` 非法（→ 400）。
        """
        return self._store.list(
            module=module, market=market, limit=limit, status=status
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _execute(
        self,
        job_id: str,
        runner: Callable[
            [Callable[[dict[str, Any]], None], Callable[[Any], None]],
            dict[str, Any] | None,
        ],
    ) -> None:
        """后台线程主流程：running → done/failed/cancelled。"""
        self._store.set_status(job_id, "running")

        def progress_cb(progress: dict[str, Any]) -> None:
            self._store.set_progress(job_id, progress)

        def register_handle(handle: Any) -> None:
            with self._lock:
                self._handles[job_id] = handle

        try:
            result = runner(progress_cb, register_handle)
            self._store.set_done(job_id, result)
        except JobCancelledError:
            self._store.set_status(job_id, "cancelled")
        except FailLoudError as exc:
            self._store.set_error(job_id, _code_for_exception(exc), str(exc))
        except Exception as exc:  # noqa: BLE001 - 未预期异常统一 500，不静默
            self._store.set_error(
                job_id, CODE_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
            )
        finally:
            with self._lock:
                self._handles.pop(job_id, None)
