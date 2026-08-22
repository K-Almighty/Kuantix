"""进程隔离的因子计算 worker。

背景
----
F2 因子计算会对全市场 / 全区间做重算：读入全部标的历史行情、计算因子、
写 parquet + sqlite。单次可吃数 GB 内存并长时间占满 GIL。若与 uvicorn
事件循环同进程，会把整个后端拖垮（所有请求超时，表现为「后端卡死」）。

做法
----
把计算放进独立子进程（spawn）。子进程内存 / GIL 与 API 进程完全隔离，
子进程 OOM 或被终止都不影响后端存活。计算完成后经 multiprocessing.Queue
回传摘要；子进程末尾显式 ``queue.close()`` + ``queue.join_thread()``，
规避 spawn 下 Queue feeder 未 flush 导致结果丢失的经典问题。
"""
from __future__ import annotations

import datetime as dt
import multiprocessing as mp
from dataclasses import dataclass
from typing import Any

#: 子进程最长等待时间（秒）。因子计算/合成/报告都可能跑数分钟，
#: 用 1 小时做上限兜底：超过即视为异常，fail-loud 终止（NF-26，R1）。
_WORKER_TIMEOUT_S = 3600
#: 队列取数超时（秒）。仅在确认子进程已退出后使用；此时数据应已由
#: feeder flush 进管道，正常立即返回。此超时仅为极端兜底，不吞异常。
_QUEUE_GET_TIMEOUT_S = 60
#: join 探测间隔（秒）：周期检查子进程状态，同时用 ``queue.empty()``
#: 区分「仍在计算（队列空）」与「feeder 被大 payload 卡死（队列非空，
#: 数据已在管道中）」两种情况，前者继续等，后者先取数解除阻塞。
_JOIN_PROBE_S = 1.0


def _collect_child_result(
    queue: "mp.Queue",
    proc: "mp.Process",
    label: str,
) -> tuple[str, Any]:
    """排空队列 → join 子进程 → 归一化结果（R1：修复 join-then-get 死锁）。

    Python 官方文档明确警告：子进程 ``queue.put`` 由 feeder 线程异步写管道，
    若 payload 超过管道缓冲（实测约 64KB），feeder 阻塞在 ``join_thread()``，
    子进程永不退出；父进程若先 ``proc.join()`` 再 ``queue.get()`` 会**双向死锁**。

    修复策略（同时覆盖两种失败形态）：
    1. 正常/慢计算：子进程计算数分钟，期间队列为空 → 周期 join 探测等待，
       退出后 ``get()`` 立即可取（数据已 flush 进管道）；
    2. 大 payload 死锁：子进程 feeder 卡死在写管道（队列非空）→ 先
       ``get()`` 解除阻塞，子进程 ``join_thread()`` 随即返回并退出。
    """
    deadline = dt.datetime.now() + dt.timedelta(seconds=_WORKER_TIMEOUT_S)
    while True:
        # 队列已有数据：子进程卡在 feeder 写管道（死锁形态），先取数解除
        if not queue.empty():
            kind, payload = queue.get(timeout=_QUEUE_GET_TIMEOUT_S)
            proc.join(timeout=_JOIN_PROBE_S)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)
                raise RuntimeError(
                    f"{label}子进程取数后仍未退出，已强制终止（fail-loud/NF-26）"
                )
            return kind, payload
        # 队列为空：仍在计算或尚未写入，探测子进程是否已退出
        proc.join(timeout=_JOIN_PROBE_S)
        if not proc.is_alive():
            break
        if dt.datetime.now() >= deadline:
            proc.terminate()
            proc.join(timeout=5)
            raise RuntimeError(
                f"{label}子进程超时未退出（>{_WORKER_TIMEOUT_S}s），"
                f"已强制终止（fail-loud/NF-26）"
            )
    if proc.exitcode != 0:
        raise RuntimeError(f"{label}子进程异常退出（exitcode={proc.exitcode}）")
    kind, payload = queue.get(timeout=_QUEUE_GET_TIMEOUT_S)
    return kind, payload
@dataclass(frozen=True)
class _ProcHandle:
    """可被 JobManager 取消句柄：cancel 时终止计算子进程。"""

    proc: "mp.Process"

    def cancel(self) -> None:
        if self.proc.is_alive():
            self.proc.terminate()


def _compute_entry(
    queue: "mp.Queue",
    market: str,
    factors: list[str],
    start_iso: str,
    end_iso: str,
    pool: list[str] | None,
    force: bool,
) -> None:
    """子进程入口：重建 FactorService 并执行计算，结果经队列回传。"""
    try:
        from Kuantix.config import get_config
        from Kuantix.factor.factors import discover_factors
        from Kuantix.factor.service import ComputeRequest, FactorService

        discover_factors()
        req = ComputeRequest(
            market=market,
            factors=tuple(factors),
            start=dt.date.fromisoformat(start_iso),
            end=dt.date.fromisoformat(end_iso),
            codes=tuple(pool) if pool else None,
            force=force,
        )
        service = FactorService(get_config())
        results = service.compute_factors(req)
        summary = {r.factor: r.to_dict() for r in results}
        queue.put(("done", {"factors": summary, "count": len(results)}))
    except Exception as exc:  # noqa: BLE001 - 异常透传，由父进程映射为 500
        queue.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        queue.close()
        queue.join_thread()


def run_compute_in_process(
    market: str,
    factors: list[str],
    start: dt.date,
    end: dt.date,
    pool: tuple[str, ...] | None,
    force: bool,
    register_handle: Any = None,
) -> dict[str, Any]:
    """在独立子进程中执行因子计算，返回与 ``compute_factors`` 一致的摘要。

    Args:
        register_handle: 可选，传入 JobManager 的 register_handle，用于在子进程
            启动后登记 cancel 句柄（命中取消时终止子进程）。

    Raises:
        RuntimeError: 子进程异常或非零退出（由调用方映射为 fail-loud）。
    """
    ctx = mp.get_context("spawn")
    queue: "mp.Queue" = ctx.Queue()
    proc = ctx.Process(
        target=_compute_entry,
        args=(
            queue,
            market,
            list(factors),
            start.isoformat(),
            end.isoformat(),
            list(pool) if pool else None,
            force,
        ),
        daemon=False,
        name="Kuantix-factor-compute",
    )
    proc.start()
    if register_handle is not None:
        register_handle(_ProcHandle(proc))
    kind, payload = _collect_child_result(queue, proc, "因子计算")
    if kind == "error":
        raise RuntimeError(payload)
    return payload


def _report_entry(
    queue: "mp.Queue",
    name: str,
    market: str,
) -> None:
    """子进程入口：重建 FactorService 并计算因子报告，结果经队列回传。

    与计算同理，报告的「读整张 parquet + 全样本 FactorAnalyzer」极重，
    必须放在独立子进程，避免拖垮 API。空数据 / 异常均经队列透传。
    """
    try:
        from Kuantix.config import get_config
        from Kuantix.factor.factors import discover_factors
        from Kuantix.factor.service import FactorService

        discover_factors()
        service = FactorService(get_config())
        factor_df = service.store.load(name)
        if factor_df is None or (hasattr(factor_df, "empty") and factor_df.empty):
            queue.put(("error", f"因子 {name} 无已计算数据，请先 compute"))
            return
        # 只回传交易日集合（去重），不传全部行的 date（R1）：
        # 全量行列表对 819 万行因子约 39MB，超过 multiprocessing 管道缓冲
        # （实测约 64KB）会触发 feeder 线程阻塞 → 与父进程 proc.join() 双向死锁。
        # 去重后仅 ~1.4 千个交易日，体积缩小 5,600 倍。
        dates = (
            sorted({int(d) for d in factor_df["date"]})
            if "date" in getattr(factor_df, "columns", [])
            else []
        )
        report = service.report(name, market=market)
        queue.put(("done", {"report": report, "dates": dates, "n_rows": int(len(factor_df))}))
    except Exception as exc:  # noqa: BLE001 - 异常透传，由父进程映射为 500
        queue.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        queue.close()
        queue.join_thread()


def _combine_entry(
    queue: "mp.Queue",
    factors: list[str],
    method: str,
    name: str | None,
    save_model: bool,
    market: str,
) -> None:
    """子进程入口：重建 FactorService 并执行多因子合成，结果经队列回传。

    合成的「读整张因子 parquet + 全样本矩阵/IC-IR 加权」极重（含 ML 因子时
    还会 import sklearn/lightgbm/torch），必须放在独立子进程，避免拖垮 API
    进程（F5 历史卡死根因）。
    """
    try:
        from Kuantix.config import get_config
        from Kuantix.factor.factors import discover_factors
        from Kuantix.factor.service import FactorService

        discover_factors()
        service = FactorService(get_config())
        handle = service.combine(
            factors,
            method,
            name=name,
            save_model=save_model,
            market=market,
        )
        queue.put(("done", handle.to_dict()))
    except Exception as exc:  # noqa: BLE001 - 异常透传，由父进程映射为 500
        queue.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        queue.close()
        queue.join_thread()


def run_combine_in_process(
    factors: list[str],
    method: str,
    name: str | None,
    save_model: bool,
    market: str,
    register_handle: Any = None,
) -> dict[str, Any]:
    """在独立子进程中执行多因子合成，返回 ``ModelHandle.to_dict()``。

    Raises:
        RuntimeError: 子进程异常或非零退出（由调用方映射为 fail-loud）。
    """
    ctx = mp.get_context("spawn")
    queue: "mp.Queue" = ctx.Queue()
    proc = ctx.Process(
        target=_combine_entry,
        args=(queue, list(factors), method, name, save_model, market),
        daemon=False,
        name="Kuantix-factor-combine",
    )
    proc.start()
    if register_handle is not None:
        register_handle(_ProcHandle(proc))
    kind, payload = _collect_child_result(queue, proc, "因子合成")
    if kind == "error":
        raise RuntimeError(payload)
    return payload


def run_report_in_process(
    name: str,
    market: str,
    register_handle: Any = None,
) -> dict[str, Any]:
    """在独立子进程中计算因子报告，返回 ``{"report", "dates", "n_rows"}``。

    Raises:
        RuntimeError: 子进程异常或非零退出（由调用方映射为 fail-loud）。
    """
    ctx = mp.get_context("spawn")
    queue: "mp.Queue" = ctx.Queue()
    proc = ctx.Process(
        target=_report_entry,
        args=(queue, name, market),
        daemon=False,
        name="Kuantix-factor-report",
    )
    proc.start()
    if register_handle is not None:
        register_handle(_ProcHandle(proc))
    kind, payload = _collect_child_result(queue, proc, "因子报告")
    if kind == "error":
        raise RuntimeError(payload)
    return payload
