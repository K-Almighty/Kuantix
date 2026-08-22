"""Kuantix 性能基准测量框架。

提供可复现的计时 / 内存 / GC / IO 采集能力，输出 JSON 基线供优化前后比对。

设计要点：
  - 每个用例 warmup 后多轮重复，取 min / median / mean（min 抗噪声最好）。
  - tracemalloc 单独一轮采集峰值内存，避免影响计时轮次。
  - GC 计数与 ru_inblock（IO 读块数）在计时轮内采集。
  - 所有用例只读真实数据；写入类用例走临时副本，绝不污染 ~/.Kuantix。
"""

from __future__ import annotations

import gc
import json
import resource
import statistics
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BenchResult:
    """单个基准用例的测量结果。"""

    name: str
    group: str
    scale: str
    repeat: int
    wall_min_ms: float
    wall_median_ms: float
    wall_mean_ms: float
    cpu_median_ms: float
    peak_mem_mb: float
    gc_collections: int
    io_read_blocks: int
    result_signature: str = ""
    note: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Bench:
    """基准测试收集器。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.results: list[BenchResult] = []

    # ------------------------------------------------------------------ #

    def run(
        self,
        name: str,
        fn: Callable[[], Any],
        *,
        group: str = "misc",
        scale: str = "",
        repeat: int = 5,
        warmup: int = 1,
        signature: Callable[[Any], str] | None = None,
        note: str = "",
        measure_mem: bool = True,
    ) -> BenchResult:
        """执行一个基准用例。

        Args:
            name: 用例名。
            fn: 被测可调用对象（无参）。
            group: 分组（data / factor / screen / backtest / io）。
            scale: 数据规模描述。
            repeat: 计时轮数。
            warmup: 预热轮数（不计入统计）。
            signature: 从返回值提取一致性指纹的函数（用于优化前后结果比对）。
            note: 备注。
            measure_mem: 是否单独跑一轮 tracemalloc 采集峰值内存。

        Returns:
            :class:`BenchResult`；异常时 error 字段非空且耗时为 -1。
        """
        print(f"  [{group}] {name} ({scale}) ...", end="", flush=True)
        try:
            for _ in range(warmup):
                fn()
        except Exception as exc:  # noqa: BLE001 - 基准用例失败需记录而非中断整轮
            res = BenchResult(
                name=name, group=group, scale=scale, repeat=0,
                wall_min_ms=-1, wall_median_ms=-1, wall_mean_ms=-1,
                cpu_median_ms=-1, peak_mem_mb=-1, gc_collections=0,
                io_read_blocks=0, error=f"{type(exc).__name__}: {exc}"[:300],
            )
            self.results.append(res)
            print(f" ERROR: {type(exc).__name__}")
            return res

        walls: list[float] = []
        cpus: list[float] = []
        gc.collect()
        gc0 = sum(s["collections"] for s in gc.get_stats())
        io0 = resource.getrusage(resource.RUSAGE_SELF).ru_inblock
        out: Any = None

        for _ in range(repeat):
            t_cpu0 = time.process_time()
            t0 = time.perf_counter()
            out = fn()
            walls.append((time.perf_counter() - t0) * 1000.0)
            cpus.append((time.process_time() - t_cpu0) * 1000.0)

        gc1 = sum(s["collections"] for s in gc.get_stats())
        io1 = resource.getrusage(resource.RUSAGE_SELF).ru_inblock

        peak_mb = -1.0
        if measure_mem:
            gc.collect()
            tracemalloc.start()
            fn()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peak_mb = peak / 1024 / 1024

        sig = ""
        if signature is not None:
            try:
                sig = signature(out)
            except Exception as exc:  # noqa: BLE001 - 指纹失败不影响计时结果
                sig = f"<sig-error:{type(exc).__name__}>"

        res = BenchResult(
            name=name, group=group, scale=scale, repeat=repeat,
            wall_min_ms=round(min(walls), 3),
            wall_median_ms=round(statistics.median(walls), 3),
            wall_mean_ms=round(statistics.fmean(walls), 3),
            cpu_median_ms=round(statistics.median(cpus), 3),
            peak_mem_mb=round(peak_mb, 3),
            gc_collections=gc1 - gc0,
            io_read_blocks=io1 - io0,
            result_signature=sig,
            note=note,
        )
        self.results.append(res)
        print(f" {res.wall_min_ms:.1f}ms (peak {peak_mb:.1f}MB) sig={sig[:40]}")
        return res

    # ------------------------------------------------------------------ #

    def save(self, path: Path | str) -> Path:
        """把结果落成 JSON 基线文件。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "label": self.label,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "results": [asdict(r) for r in self.results],
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n基线已写入: {p}")
        return p


def compare(before_path: Path | str, after_path: Path | str) -> str:
    """生成优化前后对比表（Markdown）。"""
    before = json.loads(Path(before_path).read_text(encoding="utf-8"))
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))
    b_map = {r["name"]: r for r in before["results"]}
    a_map = {r["name"]: r for r in after["results"]}

    lines = [
        "| 用例 | 规模 | 优化前(ms) | 优化后(ms) | 提速 | 内存前(MB) | 内存后(MB) | 内存降幅 | 结果一致 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for name, b in b_map.items():
        a = a_map.get(name)
        if a is None:
            continue
        bt, at = b["wall_min_ms"], a["wall_min_ms"]
        bm, am = b["peak_mem_mb"], a["peak_mem_mb"]
        if bt <= 0 or at <= 0:
            speed = "n/a"
        elif at < bt:
            speed = f"**{bt / at:.2f}×**"
        else:
            speed = f"{bt / at:.2f}×"
        mem = f"{(1 - am / bm) * 100:.1f}%" if bm > 0 and am > 0 else "n/a"
        same = "n/a"
        if b["result_signature"] or a["result_signature"]:
            same = "✅" if b["result_signature"] == a["result_signature"] else "❌"
        lines.append(
            f"| {name} | {b['scale']} | {bt:.1f} | {at:.1f} | {speed} | "
            f"{bm:.1f} | {am:.1f} | {mem} | {same} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3:
        print(compare(sys.argv[1], sys.argv[2]))
