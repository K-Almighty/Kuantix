"""Phase 1.5 技术验证 Spike 总入口。

按顺序执行 S1→S5，每个 spike 独立 try/except，单个失败不影响其余。
前置：已在独立 venv 安装 easy-tdx[science] + fastapi + uvicorn。

用法：
    /Users/kongbiao/Downloads/开源量化/Kuantix/.venv/bin/python run_all.py
"""
from __future__ import annotations

import importlib
import traceback

SPIKES = ["S1_api_signature", "S5_factor_smoke", "S2_throughput", "S3_vipdoc_e2e", "S4_hk_us"]


def main() -> None:
    print("#" * 80)
    print("# Phase 1.5 技术验证 Spike — 总入口")
    print("#" * 80)
    summary = {}
    for name in SPIKES:
        print(f"\n>>>> 运行 {name}")
        try:
            mod = importlib.import_module(name)
            res = mod.main()
            status = res.get("status") if isinstance(res, dict) else "ran"
            summary[name] = status
            print(f"<<<< {name} 完成: {status}")
        except Exception as e:  # noqa: BLE001
            summary[name] = f"EXCEPTION: {type(e).__name__}: {e}"
            print(f"<<<< {name} 异常: {e}")
            traceback.print_exc()
    print("\n" + "=" * 80)
    print("汇总:")
    for k, v in summary.items():
        print(f"  {k:18s} -> {v}")


if __name__ == "__main__":
    main()
