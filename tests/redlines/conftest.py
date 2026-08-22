"""红线检查器的 pytest 配置。

职责：
- 注册 ``--redline-json`` 选项（把本次扫描结果落成机器可读报告，供 CI 消费）
- 注册 marker
- 在 terminal summary 里输出：扫描范围、豁免清单全量、ADVISORY（spike 预警）明细
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _scan import PROJECT_ROOT, RECORDER, package_root


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("redlines")
    group.addoption(
        "--redline-json",
        action="store",
        default=None,
        metavar="PATH",
        help="把红线扫描结果写成 JSON 报告到指定路径",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "redline: Kuantix 架构红线静态检查")
    config.addinivalue_line("markers", "advisory: 只预警不阻断（如 spike 代码扫描）")


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        item.add_marker(pytest.mark.redline)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:  # noqa: ARG001
    tr = terminalreporter
    pkg = package_root()

    tr.write_sep("=", "Kuantix 红线检查 · 扫描范围", bold=True)
    tr.write_line(f"项目根       : {PROJECT_ROOT}")
    tr.write_line(f"源码包根     : {pkg if pkg else '（尚未落地 —— 结构性用例已 skip）'}")
    for rule, desc in sorted(RECORDER.scopes.items()):
        tr.write_line(f"  {rule:<4} {desc}")

    if RECORDER.allowlists:
        tr.write_sep("=", "当前豁免清单（allowlist）", bold=True)
        for name, al in RECORDER.allowlists.items():
            tr.write_line(f"[{name}]  {al.path.name}  共 {len(al.entries)} 条")
            tr.write_line(al.inventory())
            if al.malformed:
                tr.write_line(f"  !! 格式错误 {len(al.malformed)} 条（见 allowlist 自检用例）")
            stale = al.unused
            if stale:
                tr.write_line(f"  ⚠ 有 {len(stale)} 条豁免本次未命中，建议清理：")
                for e in stale:
                    tr.write_line(f"      {e.raw}")

    advisory = [v for v in RECORDER.violations if v.severity == "ADVISORY"]
    if advisory:
        tr.write_sep("=", f"ADVISORY · spike 代码红线预警（不阻断，共 {len(advisory)} 处）", bold=True)
        by_rule: dict[str, list] = {}
        for v in advisory:
            by_rule.setdefault(v.rule, []).append(v)
        for rule in sorted(by_rule):
            group = by_rule[rule]
            tr.write_line(f"[{rule}] {len(group)} 处")
            for v in group:
                tr.write_line(v.render())

    out = config.getoption("--redline-json")
    if out:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        RECORDER.dump(target)
        tr.write_line(f"\n红线 JSON 报告已写入：{target}")
