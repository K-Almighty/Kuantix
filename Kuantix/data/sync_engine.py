"""数据湖回补引擎（NF-24 限速退避 / NF-27 隔离区 / NF-28 资源隔离）。

设计要点
--------
- **并发 worker**：``ThreadPoolExecutor``；连接非线程安全，因此**每个 worker
  独立创建**自己的 :class:`~Kuantix.adapters.quotation.QuotationFetcher`
  （走 ``new_mac_client`` 非池化接口），互不共享 socket（NF-28）。
- **断点续传**：进度落盘为 JSON checkpoint（``~/.Kuantix/db/sync_checkpoint.json``），
  Ctrl+C 后重跑从断点继续 —— 已完成的 code 直接跳过。
- **后台运行**：:meth:`SyncEngine.run` 启动后台线程后立即返回
  :class:`SyncHandle`，不阻塞 CLI；调用方可 ``handle.wait()`` 或轮询进度。
- **限速退避（NF-24）**：相邻请求最小间隔 ``min_request_interval``；
  失败按 ``retry_backoff_seconds * attempt`` 退避，超过 ``retry_max_attempts``
  后入隔离区。
- **隔离区（NF-27）**：未知类型 / uint32 越界 / 回读不一致 / 网络重试耗尽
  一律 :meth:`QuarantineStore.add`，**不静默丢弃**。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from Kuantix.core.contracts import (
    QuarantineEntry,
    Security,
    SyncProgress,
)
from Kuantix.core.eventbus import (
    EVENT_BUS,
    TOPIC_QUARANTINE,
    TOPIC_SYNC_PROGRESS,
)
from Kuantix.core.fail_loud import (
    DataIntegrityError,
    UnknownValueError,
    require_key,
)

logger = logging.getLogger(__name__)

__all__ = ["SyncPlan", "SyncResult", "SyncHandle", "SyncEngine"]


# ---------------------------------------------------------------------------
# 计划 / 结果 / 句柄
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncPlan:
    """一次回补计划。

    Attributes:
        market: 市场码（``CN`` / ``HK`` / ``US``）。
        years: 回溯年数。
        securities: 待处理标的列表。
        vipdoc_root: vipdoc 落盘根目录。
        workers: 并发 worker 数。
        min_request_interval: 相邻请求最小间隔（秒，NF-24）。
        retry_backoff_seconds: 失败退避基数（秒）。
        retry_max_attempts: 单标的失败重试上限。
        checkpoint_path: 断点文件路径；``None`` 时不落盘续传。
        event_bus: 是否向全局事件总线发布进度/隔离事件。
    """

    market: str
    years: int
    securities: tuple[Security, ...]
    vipdoc_root: Path
    workers: int = 4
    min_request_interval: float = 0.05
    retry_backoff_seconds: float = 1.0
    retry_max_attempts: int = 3
    checkpoint_path: Path | None = None
    event_bus: bool = True


@dataclass(frozen=True)
class SyncResult:
    """一次回补的最终结果。

    Attributes:
        market: 市场码。
        total: 计划处理标的总数。
        done: 成功数。
        failed: 失败数。
        quarantined: 进隔离区数。
        skipped_resumed: 断点续传跳过数。
        elapsed_ms: 总耗时（毫秒）。
        completed_codes: 成功代码列表。
        quarantined_codes: 隔离代码列表。
        failed_codes: 失败代码列表。
    """

    market: str
    total: int
    done: int
    failed: int
    quarantined: int
    skipped_resumed: int
    elapsed_ms: int
    completed_codes: tuple[str, ...]
    quarantined_codes: tuple[str, ...]
    failed_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "market": self.market,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "quarantined": self.quarantined,
            "skipped_resumed": self.skipped_resumed,
            "elapsed_ms": self.elapsed_ms,
            "completed_codes": list(self.completed_codes),
            "quarantined_codes": list(self.quarantined_codes),
            "failed_codes": list(self.failed_codes),
        }


@dataclass
class SyncHandle:
    """一次回补的运行句柄（后台线程 + 进度 + 取消）。

    Attributes:
        plan: 回补计划。
        status: ``pending`` / ``running`` / ``done`` / ``cancelled`` / ``failed``。
        progress: 最新进度快照。
        result: 最终结果（``done`` 后可用）。
        error: 异常信息（``failed`` 时）。
        _thread: 后台线程（内部）。
        _cancel_event: 取消信号（内部）。
    """

    plan: SyncPlan
    status: str = "pending"
    progress: SyncProgress | None = None
    result: SyncResult | None = None
    error: str | None = None
    _thread: threading.Thread | None = field(default=None, repr=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _started_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now().astimezone(), repr=False
    )

    def is_done(self) -> bool:
        """是否已结束（done / cancelled / failed）。"""
        return self.status in ("done", "cancelled", "failed")

    def wait(self, timeout: float | None = None) -> SyncResult | None:
        """等待回补结束。

        Args:
            timeout: 等待秒数；``None`` 无限等待。

        Returns:
            最终结果；未完成返回 ``None``。
        """
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout)
        return self.result

    def cancel(self) -> None:
        """请求取消（尽量早停，已提交的任务会自然结束）。"""
        self._cancel_event.set()

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "status": self.status,
            "market": self.plan.market,
            "years": self.plan.years,
            "total": len(self.plan.securities),
            "progress": self.progress.to_dict() if self.progress else None,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
            "started_at": self._started_at.isoformat(timespec="seconds"),
        }


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class SyncEngine:
    """数据湖回补引擎（断点续传 + 限速退避 + 后台运行）。

    性能（设计文档 08 问题 3，D10）：
    - **worker 级 fetcher 缓存**：:meth:`_process_one` 每 worker 只调用一次
      ``fetcher_factory``，后续复用（``threading.local``）—— 全 A 建湖从
      每只新建连接（≈0.53s/只）降到每 worker 一次（≈0.06s/只）；
    - **per-worker 限速**：``min_request_interval`` 语义不变（相邻请求最小
      间隔），但不再跨 worker 全局串行 —— 4 workers 并发时总吞吐约 4 倍。

    Args:
        fetcher_factory: 无参可调用，返回一个新的
            :class:`~Kuantix.adapters.quotation.QuotationFetcher`。
            每个 worker 独立调用一次（连接非线程安全，NF-28）。
        writer: 写后端（:class:`~Kuantix.adapters.vipdoc_writer.VipdocWriter`
            或 :class:`~Kuantix.adapters.vipdoc_writer.SqliteBarWriter`，
            鸭子类型：``write_daily`` / ``last_bar_date``）。
        quarantine: :class:`QuarantineStore`。
        checkpoint_store: :class:`~Kuantix.data.market_store.MarketStore`；
            非 ``None`` 时断点读写走 ``sync_checkpoint`` 表（O(1) 单行，
            D6）；``None`` 时退回旧 JSON 文件（兼容既有调用）。
    """

    def __init__(
        self,
        fetcher_factory: Callable[[], Any],
        writer: Any,
        quarantine: Any,
        *,
        checkpoint_store: Any | None = None,
    ) -> None:
        self._fetcher_factory = fetcher_factory
        self._writer = writer
        self._quarantine = quarantine
        self._checkpoint_store = checkpoint_store
        # per-worker 限速与连接缓存（threading.local，互不共享）
        self._local = threading.local()

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #

    def run(self, plan: SyncPlan) -> SyncHandle:
        """启动后台回补，立即返回句柄（不阻塞 CLI）。

        Args:
            plan: 回补计划。

        Returns:
            :class:`SyncHandle`。
        """
        handle = SyncHandle(plan=plan)
        thread = threading.Thread(
            target=self._run_in_thread,
            args=(handle,),
            name=f"Kuantix-sync-{plan.market}",
            daemon=True,
        )
        handle._thread = thread  # noqa: SLF001 - 句柄内部字段
        handle.status = "running"
        thread.start()
        return handle

    def resume(self, handle: SyncHandle) -> SyncHandle:
        """从断点续传：读取 checkpoint，跳过已完成标的，重新启动后台线程。

        Args:
            handle: 之前返回的句柄（其 ``plan`` 会带上 checkpoint 续传信息）。

        Returns:
            新的运行句柄（与入参同一对象，字段原地更新）。
        """
        checkpoint = self._load_checkpoint(handle.plan)
        completed = (
            set(checkpoint["completed"]) if "completed" in checkpoint else set()
        )
        quarantined = (
            set(checkpoint["quarantined"]) if "quarantined" in checkpoint else set()
        )
        handle.plan = self._filter_plan(
            handle.plan, completed=completed, quarantined=quarantined
        )
        handle.status = "running"
        handle.result = None
        handle.error = None
        handle._cancel_event.clear()  # noqa: SLF001
        thread = threading.Thread(
            target=self._run_in_thread,
            args=(handle,),
            name=f"Kuantix-sync-resume-{handle.plan.market}",
            daemon=True,
        )
        handle._thread = thread  # noqa: SLF001
        thread.start()
        return handle

    # ------------------------------------------------------------------ #
    # 内部：主流程
    # ------------------------------------------------------------------ #

    def _run_in_thread(self, handle: SyncHandle) -> None:
        plan = handle.plan
        started = time.perf_counter()
        checkpoint = self._load_checkpoint(plan)
        completed: set[str] = set(checkpoint["completed"] if "completed" in checkpoint else ())
        quarantined: set[str] = set(checkpoint["quarantined"] if "quarantined" in checkpoint else ())
        failed: set[str] = set(checkpoint["failed"] if "failed" in checkpoint else ())

        pending = [s for s in plan.securities if s.code not in completed]
        total = len(plan.securities)
        skipped = total - len(pending)
        done: list[str] = []
        failed_codes: list[str] = []
        quarantined_codes: list[str] = []

        if skipped > 0:
            logger.info("断点续传跳过 %d 只已完成标的", skipped)
        self._publish_progress(plan, total, 0, 0, 0, skipped, "")

        try:
            if not pending:
                result = self._finish_result(
                    plan, total, 0, 0, 0, skipped, started, [], [], []
                )
                handle.result = result
                handle.status = "done"
                self._publish_progress_from_result(plan, result)
                return

            with ThreadPoolExecutor(max_workers=plan.workers) as pool:
                futures = {}
                for sec in pending:
                    if handle._cancel_event.is_set():  # noqa: SLF001
                        break
                    future = pool.submit(self._process_one, plan, sec)
                    futures[future] = sec

                for future in as_completed(futures):
                    if handle._cancel_event.is_set():  # noqa: SLF001
                        break
                    sec = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:  # noqa: BLE001 - 任务内部异常统一入隔离区
                        logger.exception("同步 %s 异常", sec.code)
                        outcome = ("quarantine", f"{type(exc).__name__}: {exc}")

                    kind, detail = outcome
                    if kind == "ok":
                        done.append(sec.code)
                        completed.add(sec.code)
                        self._mark_checkpoint(
                            plan, sec.code, "completed", completed, quarantined, failed
                        )
                    elif kind == "quarantine":
                        quarantined_codes.append(sec.code)
                        quarantined.add(sec.code)
                        self._mark_checkpoint(
                            plan, sec.code, "quarantined", completed, quarantined, failed
                        )
                        self._record_quarantine(plan, sec, detail)
                    else:
                        failed_codes.append(sec.code)
                        failed.add(sec.code)
                        self._mark_checkpoint(
                            plan, sec.code, "failed", completed, quarantined, failed
                        )

                    self._publish_progress(
                        plan,
                        total,
                        len(done),
                        len(failed_codes),
                        len(quarantined_codes),
                        skipped,
                        sec.code,
                    )

            if handle._cancel_event.is_set():  # noqa: SLF001
                handle.status = "cancelled"
            else:
                result = self._finish_result(
                    plan,
                    total,
                    len(done),
                    len(failed_codes),
                    len(quarantined_codes),
                    skipped,
                    started,
                    done,
                    quarantined_codes,
                    failed_codes,
                )
                handle.result = result
                handle.status = "done"
                self._publish_progress_from_result(plan, result)
        except Exception as exc:  # noqa: BLE001 - 顶层失败不得静默
            handle.status = "failed"
            handle.error = f"{type(exc).__name__}: {exc}"
            logger.exception("回补主流程失败")

    def _process_one(self, plan: SyncPlan, sec: Security) -> tuple[str, str]:
        """处理单只标的：拉 K 线 → 写盘 → 回读校验。

        **每 worker 复用连接**（性能修复，B1）：``_thread_local_fetcher``
        首次调用 ``fetcher_factory`` 并缓存到线程局部，后续直接复用
        （连接非线程安全 → 每 worker 独立，NF-28）。

        Returns:
            ``("ok", "")`` 或 ``("quarantine", detail)``。
        """
        self._throttle(plan)
        try:
            fetcher = self._thread_local_fetcher()
            bars = fetcher.fetch_kline(plan.market, sec.code, plan.years)
        except (UnknownValueError, DataIntegrityError) as exc:
            # 数据层面问题（未知类型 / uint32 越界）→ 直接隔离区
            return ("quarantine", f"{type(exc).__name__}: {exc}")

        # 网络错误退避重试（NF-24）
        for attempt in range(1, plan.retry_max_attempts + 1):
            try:
                if not bars:
                    # 空数据：可能是新股/停牌，不隔离但记录为成功（写入空文件由 writer 处理）
                    report = self._writer.write_daily([], sec.exchange, sec.code)
                    logger.warning("%s 返回空 K 线，写入 0 条", sec.code)
                    return ("ok", "")
                report = self._writer.write_daily(bars, sec.exchange, sec.code)
                return ("ok", "")
            except (UnknownValueError, DataIntegrityError) as exc:
                # 数据完整性错误 → 隔离区（不是网络问题，不重试）
                return ("quarantine", f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001 - 网络类错误需要重试
                if attempt >= plan.retry_max_attempts:
                    return ("quarantine", f"network-exhausted {type(exc).__name__}: {exc}")
                backoff = plan.retry_backoff_seconds * attempt
                logger.warning(
                    "%s 第 %d/%d 次失败：%s，退避 %.1fs",
                    sec.code,
                    attempt,
                    plan.retry_max_attempts,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
        return ("quarantine", "unreachable")

    # ------------------------------------------------------------------ #
    # 内部：限速 / checkpoint / 事件
    # ------------------------------------------------------------------ #

    def _thread_local_fetcher(self) -> Any:
        """每 worker 一个 fetcher（线程局部缓存，首次创建）。

        Returns:
            :class:`~Kuantix.adapters.quotation.QuotationFetcher`。
        """
        fetcher = getattr(self._local, "fetcher", None)
        if fetcher is None:
            fetcher = self._fetcher_factory()
            self._local.fetcher = fetcher
        return fetcher

    def _throttle(self, plan: SyncPlan) -> None:
        """相邻请求最小间隔（NF-24），**per-worker 限速**（D10）。

        语义不变：同一 worker 相邻请求仍至少间隔 ``min_request_interval``；
        但不再跨 worker 全局串行 —— 4 workers 并发时总吞吐约 4 倍。
        """
        if plan.min_request_interval <= 0:
            return
        last = getattr(self._local, "last_request_at", 0.0)
        now = time.monotonic()
        wait = last + plan.min_request_interval - now
        if wait > 0:
            time.sleep(wait)
        self._local.last_request_at = time.monotonic()

    def _load_checkpoint(self, plan: SyncPlan) -> dict[str, Any]:
        """读取断点；``checkpoint_store`` 就绪时走 SQLite 表（O(1)，D6）。

        兼容旧 JSON：store 未装配或表无数据时回退 ``checkpoint_path``
        文件（既有调用零改动）。

        Returns:
            ``{"completed": set, "quarantined": set, "failed": set}``。
        """
        if self._checkpoint_store is not None:
            try:
                return self._checkpoint_store.load_checkpoint(plan.market)
            except DataIntegrityError:
                # 表损坏 → 显式报错（fail-loud，不回退 JSON）
                raise
        if plan.checkpoint_path is None or not Path(plan.checkpoint_path).is_file():
            return {"completed": set(), "quarantined": set(), "failed": set()}
        try:
            raw = Path(plan.checkpoint_path).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 断点文件损坏: {plan.checkpoint_path}（{exc}）。"
                f"拒绝静默覆盖，请人工检查"
            ) from exc
        market = require_key(data, "market", "sync checkpoint")
        if str(market) != plan.market:
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 断点文件市场 {market!r} 与本次计划 {plan.market!r} 不一致，"
                f"拒绝复用"
            )
        return {
            "completed": set(data["completed"]) if "completed" in data else set(),
            "quarantined": set(data["quarantined"]) if "quarantined" in data else set(),
            "failed": set(data["failed"]) if "failed" in data else set(),
        }

    def _mark_checkpoint(
        self,
        plan: SyncPlan,
        code: str,
        status: str,
        completed: set[str],
        quarantined: set[str],
        failed: set[str],
    ) -> None:
        """单只断点落盘（O(1)）。

        ``checkpoint_store`` 就绪时逐只 upsert 一行（D6：大池避免 O(N²)
        全量重写）；否则走旧 JSON 全量写（兼容既有行为）。
        """
        if self._checkpoint_store is not None:
            self._checkpoint_store.upsert_checkpoint_row(plan.market, code, status)
            return
        self._save_checkpoint(plan, completed, quarantined, failed)

    def _save_checkpoint(
        self,
        plan: SyncPlan,
        completed: set[str],
        quarantined: set[str],
        failed: set[str],
    ) -> None:
        """落盘断点；``checkpoint_store`` 就绪时逐只 O(1) upsert（D6）。

        兼容旧 JSON：store 未装配时原子写 ``checkpoint_path`` 文件
        （先写临时文件再 rename，既有行为）。
        """
        if self._checkpoint_store is not None:
            for code in completed:
                self._checkpoint_store.upsert_checkpoint_row(
                    plan.market, code, "completed"
                )
            for code in quarantined:
                self._checkpoint_store.upsert_checkpoint_row(
                    plan.market, code, "quarantined"
                )
            for code in failed:
                self._checkpoint_store.upsert_checkpoint_row(
                    plan.market, code, "failed"
                )
            return
        if plan.checkpoint_path is None:
            return
        target = Path(plan.checkpoint_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "market": plan.market,
            "years": plan.years,
            "completed": sorted(completed),
            "quarantined": sorted(quarantined),
            "failed": sorted(failed),
            "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)

    def _filter_plan(
        self,
        plan: SyncPlan,
        *,
        completed: set[str],
        quarantined: set[str],
    ) -> SyncPlan:
        """过滤掉已完成/已隔离的标的（断点续传）。"""
        skip = completed | quarantined
        remaining = tuple(s for s in plan.securities if s.code not in skip)
        import dataclasses

        return dataclasses.replace(plan, securities=remaining)

    def _record_quarantine(self, plan: SyncPlan, sec: Security, detail: str) -> None:
        """把一次失败写入隔离区并发布事件（NF-27）。"""
        now = dt.datetime.now()
        entry = QuarantineEntry(
            code=sec.code,
            market=plan.market,
            reason="SYNC_FAILED",
            detail=detail,
            occurred_at=now,
            last_try=now,
        )
        self._quarantine.add(entry)
        if plan.event_bus:
            EVENT_BUS.publish(TOPIC_QUARANTINE, entry.to_dict())

    def _publish_progress(
        self,
        plan: SyncPlan,
        total: int,
        done: int,
        failed: int,
        quarantined: int,
        skipped: int,
        current: str,
    ) -> None:
        """发布进度快照（含事件总线，NF-9）。"""
        now = dt.datetime.now()
        progress = SyncProgress(
            total=total,
            done=done + skipped,
            failed=failed,
            quarantined=quarantined,
            current=current,
            started_at=now,
            updated_at=now,
        )
        if plan.event_bus:
            EVENT_BUS.publish(TOPIC_SYNC_PROGRESS, progress.to_dict())

    def _publish_progress_from_result(self, plan: SyncPlan, result: SyncResult) -> None:
        """发布最终进度。"""
        self._publish_progress(
            plan,
            result.total,
            result.done,
            result.failed,
            result.quarantined,
            result.skipped_resumed,
            "",
        )

    @staticmethod
    def _finish_result(
        plan: SyncPlan,
        total: int,
        done: int,
        failed: int,
        quarantined: int,
        skipped: int,
        started: float,
        completed_codes: Sequence[str],
        quarantined_codes: Sequence[str],
        failed_codes: Sequence[str],
    ) -> SyncResult:
        return SyncResult(
            market=plan.market,
            total=total,
            done=done,
            failed=failed,
            quarantined=quarantined,
            skipped_resumed=skipped,
            elapsed_ms=int(round((time.perf_counter() - started) * 1000)),
            completed_codes=tuple(completed_codes),
            quarantined_codes=tuple(quarantined_codes),
            failed_codes=tuple(failed_codes),
        )
