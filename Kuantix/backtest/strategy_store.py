"""策略库存储（SQLite，``~/.Kuantix/db/strategies.db``，NF-15）。

供 S1–S5（策略库 CRUD + 多策略组合回测的"勾选来源"）持久化用户保存的
策略/组合/多策略方案。仿 :class:`~Kuantix.backtest.store.BacktestResultStore`
的单连接 + RLock + 原子写模式（线程安全）。

- 表 ``strategies`` 单表，字段对齐 `docs/06-后端支撑设计-v13草案.md` §4：
  ``id``（服务端生成 ``strat_<uuid12>``）、``name``、``kind``、``strategy``、
  ``strategy_label``、``params``/``context``/``trade_config``/``snapshot``/
  ``tags``（JSON 字段）、``notes``、``created_at``/``updated_at``、
  ``app_version``；
- 全库禁 ``dict.get(k, 默认值)``（R4-B），一律用
  :func:`~Kuantix.core.fail_loud.require_key` / 显式 ``in`` 判断；
- 不存在即返回 ``None``（合法态，由路由层映射 404）。
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from Kuantix.core.db import apply_sqlite_pragmas
from Kuantix.core.fail_loud import MissingKeyError, require_key

__all__ = ["StrategyStore", "DEFAULT_DB_FILENAME", "STRATEGY_KINDS"]

#: 策略库文件名（位于 ``config.paths.db`` 下，NF-15）
DEFAULT_DB_FILENAME = "strategies.db"

#: 策略 kind 枚举（single=单标的 / portfolio=组合 / multi=多策略）
STRATEGY_KINDS = ("single", "portfolio", "multi")

#: name 长度上限（草案 §2.2 S2：1..120）
NAME_MAX_LENGTH = 120

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_label TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    context TEXT NOT NULL DEFAULT '{}',
    trade_config TEXT NOT NULL DEFAULT '{}',
    snapshot TEXT NOT NULL DEFAULT '{}',
    tags TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT ''
)
"""


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"strat_{uuid.uuid4().hex[:12]}"


def _dumps(value: Any) -> str:
    """JSON 字段序列化（``None`` 按空对象处理，字段 NOT NULL）。"""
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


class StrategyStore:
    """策略库的 SQLite 存储（线程安全，单连接 + RLock）。

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
        # P1-1：策略库裸连修复 —— 新增 WAL + busy_timeout + synchronous=NORMAL
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

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    def create(
        self,
        payload: dict[str, Any],
        *,
        app_version: str = "",
    ) -> dict[str, Any]:
        """保存一条策略并返回含服务端字段的完整视图。

        Args:
            payload: 客户端提交字段（name/kind/strategy/strategy_label/
                params/context/trade_config/snapshot/tags/notes）。
            app_version: 当前应用版本（写入 ``app_version``）。

        Returns:
            :class:`SavedStrategy` 字典（含生成 ``id`` / ``created_at`` /
            ``updated_at`` / ``app_version``）。

        Raises:
            MissingKeyError: 缺少必填字段（name/kind/strategy）或字段非法
                （fail-loud，不静默补默认）。
        """
        name = require_key(payload, "name", "strategies.create.name")
        if not isinstance(name, str) or not name.strip():
            raise MissingKeyError(
                "[fail-loud/NF-26] strategies.create.name 必须是非空字符串"
            )
        if len(name.strip()) > NAME_MAX_LENGTH:
            raise MissingKeyError(
                f"[fail-loud/NF-26] strategies.create.name 长度 "
                f"{len(name.strip())} 超过上限 {NAME_MAX_LENGTH}"
            )
        kind = require_key(payload, "kind", "strategies.create.kind")
        if kind not in STRATEGY_KINDS:
            raise MissingKeyError(
                f"[fail-loud/NF-26] strategies.create.kind 非法: {kind!r}"
                f"（允许 {list(STRATEGY_KINDS)}）"
            )
        strategy = require_key(payload, "strategy", "strategies.create.strategy")
        if not isinstance(strategy, str) or not strategy.strip():
            raise MissingKeyError(
                "[fail-loud/NF-26] strategies.create.strategy 必须是非空字符串"
            )
        strategy_label = payload.get("strategy_label")
        params = payload.get("params")
        context = payload.get("context")
        trade_config = payload.get("trade_config")
        snapshot = payload.get("snapshot")
        tags = payload.get("tags")
        notes = payload.get("notes")

        strategy_id = _new_id()
        now = _now_iso()
        row = {
            "id": strategy_id,
            "name": name.strip(),
            "kind": kind,
            "strategy": strategy.strip(),
            "strategy_label": (
                strategy_label if isinstance(strategy_label, str) else ""
            ),
            "params": _dumps(params),
            "context": _dumps(context),
            "trade_config": _dumps(trade_config),
            "snapshot": _dumps(snapshot),
            "tags": _dumps(tags),
            "notes": notes if isinstance(notes, str) else "",
            "created_at": now,
            "updated_at": now,
            "app_version": app_version if isinstance(app_version, str) else "",
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO strategies
                    (id, name, kind, strategy, strategy_label, params, context,
                     trade_config, snapshot, tags, notes, created_at, updated_at,
                     app_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(row[k] for k in (
                    "id", "name", "kind", "strategy", "strategy_label", "params",
                    "context", "trade_config", "snapshot", "tags", "notes",
                    "created_at", "updated_at", "app_version",
                )),
            )
            self._conn.commit()
        return self._row_to_view(row)

    def update(self, strategy_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """按 id 更新策略字段；不存在返回 ``None``（路由层映射 404）。

        只更新客户端显式携带的字段（``key in payload`` 判断，R4 语义）：
        name/kind/strategy/strategy_label/params/context/trade_config/
        snapshot/tags/notes 任一可省；`name`/`kind`/`strategy` 若提供则做
        与 :meth:`create` 相同的合法性校验。
        """
        existing = self.get(strategy_id)
        if existing is None:
            return None

        merged = dict(existing)
        for key in (
            "name", "kind", "strategy", "strategy_label", "params", "context",
            "trade_config", "snapshot", "tags", "notes",
        ):
            if key in payload:
                merged[key] = payload[key]
        # 与 create 相同的 fail-loud 校验（必填字段若被覆盖仍须合法）
        name = require_key(merged, "name", "strategies.update.name")
        if not isinstance(name, str) or not name.strip():
            raise MissingKeyError(
                "[fail-loud/NF-26] strategies.update.name 必须是非空字符串"
            )
        if len(name.strip()) > NAME_MAX_LENGTH:
            raise MissingKeyError(
                f"[fail-loud/NF-26] strategies.update.name 长度 "
                f"{len(name.strip())} 超过上限 {NAME_MAX_LENGTH}"
            )
        kind = require_key(merged, "kind", "strategies.update.kind")
        if kind not in STRATEGY_KINDS:
            raise MissingKeyError(
                f"[fail-loud/NF-26] strategies.update.kind 非法: {kind!r}"
                f"（允许 {list(STRATEGY_KINDS)}）"
            )
        strategy = require_key(merged, "strategy", "strategies.update.strategy")
        if not isinstance(strategy, str) or not strategy.strip():
            raise MissingKeyError(
                "[fail-loud/NF-26] strategies.update.strategy 必须是非空字符串"
            )

        now = _now_iso()
        values = {
            "name": name.strip(),
            "kind": kind,
            "strategy": strategy.strip(),
            "strategy_label": (
                merged["strategy_label"]
                if isinstance(merged["strategy_label"], str)
                else ""
            ),
            "params": _dumps(merged.get("params")),
            "context": _dumps(merged.get("context")),
            "trade_config": _dumps(merged.get("trade_config")),
            "snapshot": _dumps(merged.get("snapshot")),
            "tags": _dumps(merged.get("tags")),
            "notes": merged["notes"] if isinstance(merged["notes"], str) else "",
            "updated_at": now,
        }
        with self._lock:
            self._conn.execute(
                """
                UPDATE strategies SET
                    name = ?, kind = ?, strategy = ?, strategy_label = ?,
                    params = ?, context = ?, trade_config = ?, snapshot = ?,
                    tags = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    values["name"], values["kind"], values["strategy"],
                    values["strategy_label"], values["params"], values["context"],
                    values["trade_config"], values["snapshot"], values["tags"],
                    values["notes"], values["updated_at"], strategy_id,
                ),
            )
            self._conn.commit()
        return self.get(strategy_id)

    def delete(self, strategy_id: str) -> bool:
        """按 id 删除策略；返回是否确实删除了（False = 不存在，路由层映射 404）。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM strategies WHERE id = ?", (strategy_id,)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def get(self, strategy_id: str) -> dict[str, Any] | None:
        """按 id 取策略视图；不存在返回 ``None``（合法态）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
        return self._row_to_view(row) if row is not None else None

    def list(
        self,
        *,
        kind: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """策略列表（分页，可选 kind 过滤）。

        Args:
            kind: ``single``/``portfolio``/``multi``；``None`` 表示全部。
            page: 页码（1 起）。
            page_size: 每页条数（调用方已校验 1..500）。

        Returns:
            契约 §1.6 分页壳 ``{items, page, page_size, total, total_pages}``。
        """
        if kind is not None and kind not in STRATEGY_KINDS:
            raise MissingKeyError(
                f"[fail-loud/NF-26] strategies.list.kind 非法: {kind!r}"
                f"（允许 {list(STRATEGY_KINDS)}）"
            )
        page = int(page) if page is not None else 1
        page_size = int(page_size) if page_size is not None else 50
        offset = (page - 1) * page_size
        with self._lock:
            if kind is not None:
                total_row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM strategies WHERE kind = ?", (kind,)
                ).fetchone()
                rows = self._conn.execute(
                    "SELECT * FROM strategies WHERE kind = ? "
                    "ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?",
                    (kind, page_size, offset),
                ).fetchall()
            else:
                total_row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM strategies"
                ).fetchone()
                rows = self._conn.execute(
                    "SELECT * FROM strategies "
                    "ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?",
                    (page_size, offset),
                ).fetchall()
        total = int(total_row["c"])
        items = [self._row_to_view(r) for r in rows]
        total_pages = (total + page_size - 1) // page_size if total else 0
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_to_view(row: sqlite3.Row) -> dict[str, Any]:
        """把存储行转成 SavedStrategy 视图（JSON 字段反序列化）。"""
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "strategy": str(row["strategy"]),
            "strategy_label": str(row["strategy_label"]),
            "params": json.loads(str(row["params"])),
            "context": json.loads(str(row["context"])),
            "trade_config": json.loads(str(row["trade_config"])),
            "snapshot": json.loads(str(row["snapshot"])),
            "tags": json.loads(str(row["tags"])),
            "notes": str(row["notes"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "app_version": str(row["app_version"]),
        }
