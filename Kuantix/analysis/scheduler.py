"""盘前/盘后分析调度集成（APScheduler + 交易日判定 + 非交易日跳过）。

暴露两个入口：

* :func:`register_analysis_jobs` —— 把 ``analysis.pre_open.{market}`` /
  ``analysis.post_close.{market}`` 两个 cron job 注册到传入的
  ``BackgroundScheduler``（与 ``IncrementalSyncScheduler`` 共用同一个
  scheduler 实例，避免重复进程与时区配置）。
* :func:`run_pre_open_report_pending` / :func:`run_post_close_report_pending`
  —— 判定交易日 → 执行对应报告；周末/节假日仅记录 INFO，不 fail-loud。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from Kuantix.config import Config

__all__ = [
    "register_analysis_jobs",
    "run_pre_open_report_pending",
    "run_post_close_report_pending",
    "MISFIRE_GRACE_SECONDS",
]

logger = logging.getLogger(__name__)

#: cron 错过后 30 分钟内补跑（与 sync scheduler 口径一致）
MISFIRE_GRACE_SECONDS = 1800


def _today(profile: Any) -> dt.date:
    # 按市场时区取今日（近似：若 profile.timezone 有 tzinfo 用 astimezone；
    # 字符串时区交给 zoneinfo；都失败则本地 date）。
    tz = getattr(profile, "timezone", None)
    if tz is not None and not isinstance(tz, str):
        try:
            return dt.datetime.now().astimezone(tz).date()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(tz, str) and tz:
        try:
            from zoneinfo import ZoneInfo

            tzinfo = ZoneInfo(tz)
            return dt.datetime.now().astimezone(tzinfo).date()
        except Exception:  # noqa: BLE001
            pass
    return dt.date.today()


def _is_trading_day(profile: Any, day: dt.date) -> bool:
    """交易日判定：优先 profile.is_trading_day(day)，兜底 mon-fri。"""
    method = getattr(profile, "is_trading_day", None)
    if callable(method):
        try:
            return bool(method(day))
        except Exception:  # noqa: BLE001 - 调度器不得因判定失败崩溃
            logger.exception("[analysis.scheduler] is_trading_day 判定失败，兜底按工作日")
    return day.weekday() < 5


def run_pre_open_report_pending(
    market: str,
    config: Config,
    services: Any,
    *,
    profile: Any | None = None,
) -> dict[str, Any]:
    """盘前 cron 执行体：非交易日仅 INFO 记录后跳过。

    Args:
        market: 市场码（CN）。
        config: 全局配置。
        services: ``ServiceContainer`` 或 duck-type，要求具备属性
            ``pre_open_service``。
        profile: 市场档案；``None`` 时取 ``get_market_profile(market)``。

    Returns:
        ``{skipped, reason?, generated_at?}``。
    """
    if profile is None:
        from Kuantix.core.market import get_market_profile

        profile = get_market_profile(market)
    day = _today(profile)
    if not _is_trading_day(profile, day):
        logger.info(
            "[analysis.pre_open] %s %s 非交易日，跳过盘前报告生成",
            market, day.isoformat(),
        )
        return {"skipped": True, "reason": "非交易日", "date": day.isoformat()}
    pre_svc = getattr(services, "pre_open_service", None)
    if pre_svc is None:
        logger.error(
            "[analysis.pre_open] services.pre_open_service 未装配，跳过 %s %s",
            market, day.isoformat(),
        )
        return {"skipped": True, "reason": "pre_open_service missing", "date": day.isoformat()}
    try:
        report = pre_svc.run_report(market, day)
    except Exception:  # noqa: BLE001 - 调度器吞错，避免连带影响其他 cron
        logger.exception(
            "[analysis.pre_open] 生成报告异常 market=%s date=%s",
            market, day.isoformat(),
        )
        return {
            "skipped": False,
            "ok": False,
            "date": day.isoformat(),
            "error": "exception",
        }
    logger.info(
        "[analysis.pre_open] 报告已生成 market=%s date=%s news_total=%s watch_profiles=%d scan_top=%d",
        market, day.isoformat(),
        report.news_feed_summary.get("total"),
        len(report.watchlist_profiles),
        len(report.broad_market_scan_top),
    )
    return {
        "skipped": False,
        "ok": True,
        "date": day.isoformat(),
        "generated_at": report.generated_at.isoformat(timespec="seconds"),
    }


def run_post_close_report_pending(
    market: str,
    config: Config,
    services: Any,
    *,
    profile: Any | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """盘后 cron 执行体：非交易日 INFO 跳过；交易日进入 wait-until 等待。"""
    if profile is None:
        from Kuantix.core.market import get_market_profile

        profile = get_market_profile(market)
    day = _today(profile)
    if not _is_trading_day(profile, day):
        logger.info(
            "[analysis.post_close] %s %s 非交易日，跳过盘后复盘",
            market, day.isoformat(),
        )
        return {"skipped": True, "reason": "非交易日", "date": day.isoformat()}
    post_svc = getattr(services, "post_close_service", None)
    if post_svc is None:
        logger.error(
            "[analysis.post_close] services.post_close_service 未装配，跳过 %s %s",
            market, day.isoformat(),
        )
        return {"skipped": True, "reason": "post_close_service missing", "date": day.isoformat()}
    try:
        report = post_svc.run_report(market, day, force=force)
    except Exception:  # noqa: BLE001 - 调度器吞错
        logger.exception(
            "[analysis.post_close] 生成报告异常 market=%s date=%s",
            market, day.isoformat(),
        )
        return {
            "skipped": False,
            "ok": False,
            "date": day.isoformat(),
            "error": "exception",
        }
    summary = report.limit_summary
    logger.info(
        "[analysis.post_close] 报告已生成 market=%s date=%s up=%s down=%s highlights=%d signals_today=%d",
        market, day.isoformat(),
        summary.get("up_count"), summary.get("down_count"),
        len(report.tech_highlights), len(report.signals_today),
    )
    return {
        "skipped": False,
        "ok": True,
        "date": day.isoformat(),
        "generated_at": report.generated_at.isoformat(timespec="seconds"),
    }


def register_analysis_jobs(
    scheduler: BackgroundScheduler,
    config: Config,
    analysis_components: dict[str, Any] | Any,
    market_profile: Any,
    *,
    market: str = "CN",
) -> list[str]:
    """注册盘前/盘后两个 cron job（复用现有 BackgroundScheduler）。

    Args:
        scheduler: APScheduler 实例（必须已设定正确时区，如 Asia/Shanghai）。
        config: 全局配置；hour/minute 来自 ``config.analysis.pre_open_hour`` 等。
        analysis_components: 容器字典或 ServiceContainer 实例，最终需暴露
            ``pre_open_service`` / ``post_close_service``；当是 dict 时会构造
            一个 ``types.SimpleNamespace`` 适配给 run_*_pending。
        market_profile: 市场档案（is_trading_day + timezone）。
        market: 目标市场码（P0 仅 CN；HK/US 未开放 → 只注册 CN）。

    Returns:
        注册成功的 job id 列表（``[analysis.pre_open.CN, analysis.post_close.CN]``）。
    """
    from types import SimpleNamespace

    market_code = str(market).strip().upper()
    # HK/US 未开放 —— 注册前校验（非 CN 只记录 warning 不再注册，避免 fail-loud 干扰启动）
    if market_code != "CN":
        logger.warning(
            "[analysis.scheduler] 仅 CN 市场已开放盘前/盘后调度，跳过 %s", market_code
        )
        return []
    ana_cfg = config.analysis
    pre_hour = int(ana_cfg.pre_open_hour)
    pre_minute = int(ana_cfg.pre_open_minute)
    post_hour = int(ana_cfg.post_close_hour)
    post_minute = int(ana_cfg.post_close_minute)
    tz = getattr(market_profile, "timezone", None)

    # 构造 services duck：允许 dict（build_analysis_components 返回值）或
    # ServiceContainer 实例（已有 pre_open_service / post_close_service）。
    if isinstance(analysis_components, dict):
        svc = SimpleNamespace(
            pre_open_service=analysis_components["pre_open_service"],
            post_close_service=analysis_components["post_close_service"],
        )
    else:
        svc = analysis_components

    def _pre_job() -> None:
        run_pre_open_report_pending(market_code, config, svc, profile=market_profile)

    def _post_job() -> None:
        run_post_close_report_pending(market_code, config, svc, profile=market_profile)

    ids: list[str] = []
    pre_id = f"analysis.pre_open.{market_code}"
    scheduler.add_job(
        _pre_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=pre_hour,
            minute=pre_minute,
            timezone=tz,
        ),
        id=pre_id,
        name=f"Kuantix-analysis-pre-open-{market_code}",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        replace_existing=True,
    )
    ids.append(pre_id)

    post_id = f"analysis.post_close.{market_code}"
    scheduler.add_job(
        _post_job,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=post_hour,
            minute=post_minute,
            timezone=tz,
        ),
        id=post_id,
        name=f"Kuantix-analysis-post-close-{market_code}",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        replace_existing=True,
    )
    ids.append(post_id)

    logger.info(
        "[analysis.scheduler] 已注册 cron jobs %s（pre=%02d:%02d post=%02d:%02d tz=%s）",
        ids, pre_hour, pre_minute, post_hour, post_minute, tz,
    )
    return ids
