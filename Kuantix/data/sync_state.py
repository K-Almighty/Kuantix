"""数据湖同步状态存储（设计二 D2.6：``~/.Kuantix/db/sync_state.json`` 原子写）。

状态文件记录**任意来源**（cron / startup / manual）最近一次同步事件：

.. code-block:: json

    {
      "last_sync_at": "2026-08-03T16:30:12+08:00",
      "last_sync_status": "done",
      "last_sync_trigger": "cron",
      "last_sync_error": null,
      "last_skip_reason": null,
      "last_result": {"total": 5400, "done": 5398, "failed": 2,
                      "quarantined": 0, "skipped_resumed": 0, "elapsed_ms": 12345},
      "updated_at": "2026-08-03T16:30:12+08:00"
    }

设计约束
--------
- **原子写**：先写 ``.tmp`` 再 ``rename``（复用 :class:`SyncEngine` checkpoint
  同模式，断点/状态文件不会因进程中断而损坏）；
- **fail-loud**：文件损坏 / 结构非法 → :class:`~Kuantix.core.fail_loud.DataIntegrityError`，
  **拒绝静默覆盖或返回空**（NF-26）；
- **R4**：全部显式异常与 :func:`~Kuantix.core.fail_loud.require_key`，禁止双参 ``.get``。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from Kuantix.core.fail_loud import DataIntegrityError

__all__ = ["SyncStateStore", "SYNC_STATE_FILENAME"]

#: 状态文件名（位于 ``[paths].db`` 下）
SYNC_STATE_FILENAME = "sync_state.json"

#: 状态文件持久键（写入时全部显式落盘，读回按此校验结构）
_FILE_KEYS: tuple[str, ...] = (
    "last_sync_at",
    "last_sync_status",
    "last_sync_trigger",
    "last_sync_error",
    "last_skip_reason",
    "last_result",
    "updated_at",
)


class SyncStateStore:
    """同步状态存储（原子写 + fail-loud 读）。

    Args:
        db_dir: ``[paths].db`` 目录（如 ``~/.Kuantix/db``）。
    """

    def __init__(self, db_dir: Path | str) -> None:
        self._path = Path(db_dir).expanduser() / SYNC_STATE_FILENAME

    @property
    def path(self) -> Path:
        """状态文件路径。"""
        return self._path

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def load(self) -> dict[str, Any] | None:
        """读取完整状态；文件不存在返回 ``None``。

        Returns:
            状态字典（含 ``last_sync_*`` / ``last_result`` / ``updated_at``）；
            无记录返回 ``None``。

        Raises:
            DataIntegrityError: 文件损坏 / JSON 非法 / 结构非对象（fail-loud）。
        """
        if not self._path.is_file():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 同步状态文件损坏: {self._path}（{exc}）。"
                f"拒绝静默覆盖，请人工检查或删除后重试"
            ) from exc
        if not isinstance(data, dict):
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 同步状态文件结构非法: {self._path}"
                f"（期望 JSON 对象，实际 {type(data).__name__}）"
            )
        return data

    def view(self) -> dict[str, Any] | None:
        """导出 D1 可观测视图 ``{at, status, trigger, error, result}``（契约 v1.4）。

        - 无记录 → ``None``（D1 ``last_sync`` 可空）；
        - 跳过事件附带 ``reason``（来自 ``last_skip_reason``，仅 skipped 时非空）；
        - ``error`` 仅 failed 时非空。

        Returns:
            D1 形状字典；无记录返回 ``None``。
        """
        state = self.load()
        if state is None:
            return None
        view: dict[str, Any] = {
            "at": state.get("last_sync_at"),
            "status": state.get("last_sync_status"),
            "trigger": state.get("last_sync_trigger"),
            "error": state.get("last_sync_error"),
            "result": state.get("last_result"),
        }
        skip_reason = state.get("last_skip_reason")
        if skip_reason is not None:
            view["reason"] = skip_reason
        return view

    def last_sync_date(self) -> dt.date | None:
        """返回最近一次同步事件的日期；无记录返回 ``None``。

        Returns:
            同步日期（``last_sync_at`` 的前 10 位）。

        Raises:
            DataIntegrityError: ``last_sync_at`` 无法解析为日期（文件被改坏）。
        """
        state = self.load()
        if state is None:
            return None
        at = state.get("last_sync_at")
        if not at:
            return None
        try:
            return dt.date.fromisoformat(str(at)[:10])
        except ValueError as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 同步状态文件 {self._path} 的 last_sync_at 非法: {at!r}"
            ) from exc

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    def update(
        self,
        *,
        at: dt.datetime,
        status: str,
        trigger: str,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """原子更新同步状态（tmp + rename），返回新状态字典。

        Args:
            at: 事件发生时刻（带时区）。
            status: ``done`` / ``failed`` / ``skipped``。
            trigger: ``cron`` / ``startup`` / ``manual``。
            error: 失败原因（``status=failed`` 时）。
            result: 同步结果摘要（``status=done`` 时，形如
                ``{total, done, failed, quarantined, skipped_resumed, elapsed_ms}``）。
            reason: 跳过原因（``status=skipped`` 时，如「数据湖为空」）。

        Returns:
            写入后的完整状态字典。
        """
        state = self.load() or {}
        now = at.astimezone().isoformat(timespec="seconds")
        payload = {
            "last_sync_at": now,
            "last_sync_status": status,
            "last_sync_trigger": trigger,
            "last_sync_error": error,
            "last_skip_reason": reason,
            "last_result": result,
            "updated_at": now,
        }
        self._write(payload)
        return payload

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _write(self, payload: dict[str, Any]) -> None:
        """原子写：先写临时文件再 rename（避免中断损坏状态文件）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)
