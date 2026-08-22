"""数据湖增量同步调度器（设计二：APScheduler 盘后 cron + 启动检查，v1.4 增量）。

:class:`IncrementalSyncScheduler` 提供「盘后自动增量 + 启动幂等检查」两条触发路径，
共用同一 :meth:`_dispatch` 判定逻辑（D2.1）：

- **cron**：交易日 16:30（``[sync].schedule_time``，Asia/Shanghai），
  ``max_instances=1`` + ``coalesce=True`` + ``misfire_grace_time=1800``；
- **startup**：serve 启动时（config 门控 ``schedule_startup_check``），幂等检查
  —— 湖非空且上次同步 < 今日才增量；空湖跳过（不自动全量，D2.1/D-6）；
- **manual**：CLI ``Kuantix data schedule run-once`` 手动触发（等价测试钩子）。

防重入（D2.4）
--------------
- 同进程：APScheduler ``max_instances=1`` + ``coalesce=True``；
- 跨进程：``fcntl.flock(LOCK_EX|LOCK_NB)`` 单例锁（``sync_scheduler.lock``），
  抢锁失败 → 记 ``skipped``（「另一实例正在同步」）。

交易日/时段判定（R6 / D2.2）
----------------------------
全部经 :class:`~Kuantix.core.market.MarketProfile`（``is_trading_day`` +
``is_open_now``），不硬编码节假日；16:30 是配置默认值，非代码常量。

失败重试（D2.5）
----------------
job 体捕获异常 → 记 ``sync_state``（``status=failed`` + ``error``）→ 不阻塞服务；
下次 cron / 下次启动 / 手动 ``run-once`` 自然重试（``sync_incremental`` 断点续传兜底）。
"""

from __future__ import annotations

import sys

import datetime as dt
import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from Kuantix.config import Config
from Kuantix.core.fail_loud import DataIntegrityError
from Kuantix.core.market import MarketProfile, get_market_profile
from Kuantix.data.datalake import DataLake
from Kuantix.data.sync_state import SyncStateStore

logger = logging.getLogger(__name__)

__all__ = ["IncrementalSyncScheduler", "SYNC_LOCK_FILENAME"]

#: 跨进程单例锁文件名（位于 ``[paths].db`` 下）
SYNC_LOCK_FILENAME = "sync_scheduler.lock"

#: cron 错过后 30 分钟内补跑（D2.5）
MISFIRE_GRACE_SECONDS = 1800


class IncrementalSyncScheduler:
    """增量同步调度器（独立装配，不依赖 REST 组合根）。

    Args:
        config: 配置对象（``[sync]`` 节驱动调度）。
        lake: 数据湖门面（需 ``sync_incremental``）。
        state: 同步状态存储。
        profile: 市场档案；``None`` 时取 ``get_market_profile("CN")``（测试可注入假档案）。
    """

    def __init__(
        self,
        config: Config,
        lake: DataLake,
        state: SyncStateStore,
        profile: MarketProfile | None = None,
    ) -> None:
        self._config = config
        self._lake = lake
        self._state = state
        self._profile_override = profile
        self._scheduler: BackgroundScheduler | None = None
        self._started = False
        self._lock_path = config.paths.db / SYNC_LOCK_FILENAME
        self._lock_handle: Any = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """启动盘后 cron 调度（幂等）。

        Raises:
            DataIntegrityError: ``[sync].schedule_time`` 非法（config 已校验，双保险）。
        """
        if self._started:
            return
        # P1-5：使用 SyncConfig 已解析好的 schedule_hour / schedule_minute，
        # 避免与 config 层重复实现 HH:MM 解析。__post_init__ 保证不会为 None。
        hour = self._config.sync.schedule_hour
        minute = self._config.sync.schedule_minute
        assert hour is not None and minute is not None, "SyncConfig.__post_init__ 未正确派生 schedule_hour/schedule_minute"
        timezone = self._profile().timezone
        scheduler = BackgroundScheduler(timezone=timezone)
        scheduler.add_job(
            self._cron_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=hour,
                minute=minute,
                timezone=timezone,
            ),
            id="incremental_sync_cron",
            name="Kuantix-incremental-sync",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        scheduler.start()
        self._scheduler = scheduler
        self._started = True
        logger.info(
            "增量同步调度器已启动（交易日 %s %s）", self._config.sync.schedule_time, timezone
        )
        # 盘前/盘后分析 cron（复用同一 scheduler 实例；单例锁已在 sync_scheduler 层
        # 共享，集群部署下仅一个 worker 实际跑）。分析组件延迟 import，避免
        # P1 之前阶段的依赖未就绪（analysis 是新增模块）。
        self._register_analysis_crons()
        # 盘中分钟线同步（默认关闭，仅当 [sync].intraday_enabled=true）
        self.start_intraday()

    def _register_analysis_crons(self) -> None:
        """将盘前/盘后分析 cron 注册到 self._scheduler。"""
        try:
            from Kuantix.analysis.scheduler import register_analysis_jobs
        except Exception as exc:  # noqa: BLE001 - 分析模块缺失不得阻塞 sync 主流程
            logger.info(
                "[scheduler] 分析模块未就绪，跳过盘前/盘后 cron 注册 (%s: %s)",
                type(exc).__name__, exc,
            )
            return
        # [analysis] 节开关：默认 enabled（若后续新增 analysis.enabled 可在此判定）
        ana = getattr(self._config, "analysis", None)
        if ana is None:
            logger.info("[scheduler] 未配置 [analysis] 节，跳过分析 cron 注册")
            return
        profile = self._profile()
        # analysis_components = 依赖 lake + analysis services：此处 sync_scheduler
        # 只持有 lake/state，并无 factor/monitor → 退化为 "按需在 serve 进程内
        # 由 create_app 的 start 注册"。这里提供最简实现：如果 build_analysis_components
        # 能在当前 context 装配（例如用户通过 CLI schedule 主入口）则注册；否则
        # 记录 INFO 不注册，让 serve 层自行调度（serve 层建议在 lifespan 注册）。
        try:
            from Kuantix.api.deps import build_analysis_components
            # sync scheduler 没有 factor_service/jobs/monitor_store → 传 None 让
            # build 失败；捕获后跳过当前注册。
            ana_comps = build_analysis_components(
                self._config,
                lake=self._lake,
                jobs=None,  # type: ignore[arg-type]
                factor_service=None,  # type: ignore[arg-type]
                monitor_store=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "[scheduler] 当前进程未装配 factor/monitor 组合根，分析 cron 不在 "
                "sync_scheduler 中注册（将在 serve 启动时由 API 层注册）。详情: %s: %s",
                type(exc).__name__, exc,
            )
            return
        try:
            register_analysis_jobs(
                self._scheduler, self._config, ana_comps, profile, market="CN",
            )
        except Exception:  # noqa: BLE001
            logger.exception("[scheduler] 注册分析 cron 失败，不影响增量同步主流程")

    def start_intraday(self) -> None:
        """注册盘中分钟线增量同步 cron（交易时段内周期触发，默认关闭）。

        依赖 ``[sync].intraday_enabled``；未启用则仅记录日志不注册。
        实际分钟线抓取需先在 ``DataLake`` 接入分钟线源（见 ``sync_minute_incremental``）。
        """
        if not self._config.sync.intraday_enabled:
            logger.info("盘中分钟线同步未启用（[sync].intraday_enabled=false）")
            return
        interval = max(1, min(59, int(self._config.sync.intraday_interval_minutes)))
        timezone = self._profile().timezone
        self._scheduler.add_job(
            self._intraday_job,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9-15",
                minute=f"*/{interval}",
                timezone=timezone,
            ),
            id="intraday_minute_sync",
            name="Kuantix-intraday-minute-sync",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        logger.info("盘中分钟线同步已注册（每 %d 分钟，仅交易时段）", interval)

    def stop(self) -> None:
        """停止调度器（幂等，reload 模式每次重建无状态泄漏）。"""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        self._started = False

    # ------------------------------------------------------------------ #
    # 公开入口
    # ------------------------------------------------------------------ #

    def run_once(self, trigger: str = "manual") -> dict[str, Any]:
        """手动触发一次「调度判定 + 同步」（CLI ``data schedule run-once``）。

        Args:
            trigger: 触发来源标记（默认 ``manual``）。

        Returns:
            ``{dispatched, status/reason/result/error}`` 判定与执行结果。
        """
        return self._dispatch(trigger)

    def startup_check(self) -> dict[str, Any]:
        """服务启动检查入口（幂等：湖空 / 今日已同步 → skip，不自动全量）。"""
        return self._dispatch("startup")

    def status(self) -> dict[str, Any]:
        """调度状态视图（CLI ``data schedule status`` / D1 ``schedule`` 上游）。

        Returns:
            ``{enabled, started, schedule_time, startup_check, next_run, last_sync}``。
        """
        state = self._state.view()
        next_run: str | None = None
        if self._scheduler is not None:
            job = self._scheduler.get_job("incremental_sync_cron")
            if job is not None and job.next_run_time is not None:
                next_run = job.next_run_time.isoformat(timespec="seconds")
        intraday_next: str | None = None
        if self._scheduler is not None:
            ijob = self._scheduler.get_job("intraday_minute_sync")
            if ijob is not None and ijob.next_run_time is not None:
                intraday_next = ijob.next_run_time.isoformat(timespec="seconds")
        return {
            "enabled": self._config.sync.schedule_enabled,
            "started": self._started,
            "schedule_time": self._config.sync.schedule_time,
            "startup_check": self._config.sync.schedule_startup_check,
            "intraday": {
                "enabled": self._config.sync.intraday_enabled,
                "interval_minutes": self._config.sync.intraday_interval_minutes,
                "next_run": intraday_next,
            },
            "next_run": next_run,
            "last_sync": state,
        }

    # ------------------------------------------------------------------ #
    # 内部：触发入口
    # ------------------------------------------------------------------ #

    def _cron_job(self) -> None:
        """盘后 cron 触发入口（精确判定在 :meth:`_dispatch`，D2.2）。"""
        self._dispatch("cron")

    def _dispatch(
        self,
        trigger: str,
        *,
        should_run_fn=None,
        sync_fn=None,
    ) -> dict[str, Any]:
        """统一判定 + 执行：``_should_run`` → flock 单例锁 → 增量同步 → 记状态。

        Args:
            trigger: 触发来源标记（``cron`` / ``startup`` / ``manual`` / ``intraday``）。
            should_run_fn: 自定义「是否运行」判定（默认日常盘后判定）。
            sync_fn: 自定义同步调用（默认 ``sync_incremental("CN")``，盘中传分钟线同步）。

        Returns:
            ``{dispatched: bool, ...}``（未触发时含 ``reason``，触发时含 ``status``）。
        """
        reason = (should_run_fn or self._should_run)(trigger)
        if reason is not None:
            logger.info("增量同步跳过（%s）: %s", trigger, reason)
            self._state.update(
                at=dt.datetime.now().astimezone(),
                status="skipped",
                trigger=trigger,
                reason=reason,
            )
            return {"dispatched": False, "reason": reason}
        if not self._acquire_lock():
            reason = "另一实例正在同步（flock 单例锁被占用）"
            logger.warning("增量同步跳过（%s）: %s", trigger, reason)
            self._state.update(
                at=dt.datetime.now().astimezone(),
                status="skipped",
                trigger=trigger,
                reason=reason,
            )
            return {"dispatched": False, "reason": reason}
        try:
            sync = sync_fn or (lambda: self._lake.sync_incremental("CN"))
            handle = sync()
            result = handle.wait()
            if handle.status == "failed":
                error = handle.error or "同步失败（无错误详情）"
                self._state.update(
                    at=dt.datetime.now().astimezone(),
                    status="failed",
                    trigger=trigger,
                    error=error,
                )
                logger.error("增量同步失败（%s）: %s", trigger, error)
                return {"dispatched": True, "status": "failed", "error": error}
            if handle.status == "cancelled":
                self._state.update(
                    at=dt.datetime.now().astimezone(),
                    status="skipped",
                    trigger=trigger,
                    reason="同步被取消",
                )
                return {"dispatched": True, "status": "skipped", "reason": "同步被取消"}
            result_dict = result.to_dict() if result is not None else None
            self._state.update(
                at=dt.datetime.now().astimezone(),
                status="done",
                trigger=trigger,
                result=result_dict,
            )
            logger.info("增量同步完成（%s）: %s", trigger, result_dict)
            return {"dispatched": True, "status": "done", "result": result_dict}
        except Exception as exc:  # noqa: BLE001 - 顶层失败记状态，不阻塞服务（D2.5）
            logger.exception("增量同步异常（%s）", trigger)
            error = f"{type(exc).__name__}: {exc}"
            self._state.update(
                at=dt.datetime.now().astimezone(),
                status="failed",
                trigger=trigger,
                error=error,
            )
            return {"dispatched": True, "status": "failed", "error": error}
        finally:
            self._release_lock()

    # ------------------------------------------------------------------ #
    # 内部：判定
    # ------------------------------------------------------------------ #

    def _should_run(self, trigger: str) -> str | None:
        """判定是否应触发增量同步；返回跳过原因（``None`` = 应运行）。

        规则（D2.1/D2.2/D2.4，全部经 MarketProfile，R6）：
        - 非交易日 → skip；
        - 交易时段内（``is_open_now``）→ skip（盘后判定）；
        - 数据湖为空 → skip（「请先全量回补」；**任何触发来源都不自动全量**，
          避免空机首启全市场网络风暴，D2.1/D-6）；
        - startup 且今日已同步（``last_sync_date >= 今日``）→ skip（幂等）。

        Args:
            trigger: ``cron`` / ``startup`` / ``manual``。

        Returns:
            跳过原因；``None`` 表示应运行。
        """
        profile = self._profile()
        now = profile.now()
        if not profile.is_trading_day(now.date()):
            return f"非交易日 {now.date().isoformat()}，跳过增量同步"
        if profile.is_open_now(now):
            return (
                f"交易时段内（{now.time().isoformat(timespec='minutes')}），"
                f"盘后（{self._config.sync.schedule_time}）再同步"
            )
        if self._lake_is_empty():
            return "数据湖为空，请先全量回补（Kuantix data sync 或 D2 mode=full）"
        if trigger == "startup":
            last_date = self._state.last_sync_date()
            today = now.date()
            if last_date is not None and last_date >= today:
                return f"今日已同步（{last_date.isoformat()}），跳过启动检查"
        return None

    def _should_run_intraday(self, trigger: str) -> str | None:
        """盘中分钟线同步判定；返回跳过原因（``None`` = 应运行）。

        与盘后判定相反：仅在交易时段（``is_open_now``）运行，休市/午休跳过。
        仍要求数据湖非空（先有日线再补分钟线），且非交易日跳过。

        Args:
            trigger: 固定为 ``intraday``。

        Returns:
            跳过原因；``None`` 表示应运行。
        """
        profile = self._profile()
        now = profile.now()
        if not profile.is_trading_day(now.date()):
            return f"非交易日 {now.date().isoformat()}，跳过盘中分钟线同步"
        if self._lake_is_empty():
            return "数据湖为空，请先完成日线全量回补（盘中分钟线依赖标的列表）"
        if not profile.is_open_now(now):
            return (
                f"当前休市（{now.time().isoformat(timespec='minutes')}），"
                f"盘中分钟线仅交易时段同步"
            )
        return None

    def _intraday_job(self) -> None:
        """盘中分钟线 cron 触发入口（精确判定在 :meth:`_dispatch`）。"""
        self._dispatch_intraday()

    def _dispatch_intraday(self, trigger: str = "intraday") -> dict[str, Any]:
        """盘中分钟线增量同步：判定 + 执行（复用 :meth:`_dispatch`）。

        实际同步调用 ``DataLake.sync_minute_incremental``（分钟线抓取源需先接入）。

        Returns:
            ``{dispatched: bool, ...}`` 判定与执行结果。
        """
        return self._dispatch(
            trigger,
            should_run_fn=self._should_run_intraday,
            sync_fn=lambda: self._lake.sync_minute_incremental("CN"),
        )

    def _lake_is_empty(self) -> bool:
        """数据湖是否为空：vipdoc 下无任何 ``.day`` 文件（D2.1 空湖守卫）。

        Returns:
            ``True`` 表示湖为空（应跳过自动增量，避免空湖触发全量）。
        """
        vipdoc = self._config.paths.vipdoc
        if not vipdoc.is_dir():
            return True
        for exchange in ("sh", "sz"):
            lday = vipdoc / exchange / "lday"
            if lday.is_dir() and any(lday.glob("*.day")):
                return False
        return True

    def _profile(self) -> MarketProfile:
        """取市场档案（测试注入优先，否则 CN 档案）。"""
        if self._profile_override is not None:
            return self._profile_override
        return get_market_profile("CN")

    # ------------------------------------------------------------------ #
    # 内部：单例锁
    # ------------------------------------------------------------------ #

    def _acquire_lock(self) -> bool:
        """跨进程单例锁（P1-4：POSIX fcntl + Windows msvcrt 双实现）。

        * POSIX（Linux/macOS）：``fcntl.flock(LOCK_EX|LOCK_NB)``
        * Windows：``msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)``（对 1 字节非阻塞加锁）

        原代码仅支持 fcntl，在 Windows 桌面用户场景直接抛
        :class:`DataIntegrityError`（fail-loud 但功能不可用）。P1-4 改为
        双实现，保持「抢锁失败返回 False，平台错误仍 DataIntegrityError」
        的语义一致。

        Returns:
            ``True`` 抢锁成功；``False`` 另一实例持有（跳过本轮）。

        Raises:
            DataIntegrityError: 当前平台两种锁实现都不可用。
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._lock_path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                # 非阻塞排他锁：锁住文件的第 0 字节（长度 1）即可
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        except ModuleNotFoundError as exc:  # pragma: no cover - 极端平台组合
            handle.close()
            raise DataIntegrityError(
                f"[fail-loud/NF-26] 跨进程锁在当前平台不支持（sys.platform={sys.platform!r}）。"
                f"请改用 POSIX 或 Windows，或把 [sync].schedule_enabled=false 关闭调度器。"
            ) from exc
        self._lock_handle = handle
        return True

    def _release_lock(self) -> None:
        """释放单例锁（关闭句柄即释放 flock / msvcrt 锁）。"""
        if self._lock_handle is not None:
            try:
                handle = self._lock_handle
                # Windows msvcrt.locking 需要显式解锁（close 会自动解锁，
                # 但为了双实现一致性，在关闭前先解锁；fcntl 解锁可忽略重复 close）
                if sys.platform == "win32":
                    try:
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError as exc:  # noqa: BLE001 - 解锁失败不影响关闭，但要显式记录（R4-A）
                        logger.warning(
                            "[scheduler] Windows msvcrt 显式解锁失败（忽略，close 自动释放）：%s",
                            exc,
                        )
                handle.close()
            finally:
                self._lock_handle = None

    # ------------------------------------------------------------------ #
    # 内部：时间解析（P1-5：已下沉至 config 层 _parse_hhmm + SyncConfig.__post_init__）
    # ------------------------------------------------------------------ #

