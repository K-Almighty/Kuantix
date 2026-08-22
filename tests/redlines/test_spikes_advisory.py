"""spike 代码红线预警扫描（ADVISORY —— 只预警，不阻断）。

背景
----
``Kuantix/spikes/`` 下的 S1–S5 是架构师的技术验证脚本，**不是正式交付物**，
所以本文件的用例**永远通过**。

但它们是工程师实现 T01–T05 时最顺手的"参考实现"——一旦 spike 里踩了红线，
照抄时极易把违规写法一并带进生产代码。所以这里跑 R1 / R3 / R4 三条规则，
把结果以 ADVISORY 级别记进报告，由 conftest 在 terminal summary 里单独输出。

选这三条的理由
--------------
- **R1（系数）**：S3 里为复现 T1 陷阱**故意**写了错误系数，最容易被误抄。
- **R3（禁用 API）**：spike 直连真实节点、读 ``~/.easy_tdx/config.json``，
  是最可能出现越界写操作的地方。
- **R4（fail-loud）**：探测脚本天然大量 ``try/except: pass``，这是 NF-26
  的头号污染源。

R2/R5/R6 不在此扫描：spike 是平铺脚本，没有 adapters 分层、没有 REST 路由、
没有业务层目录，扫了只会产生无意义噪音。
"""

from __future__ import annotations

import pytest

from _scan import RECORDER, SPIKES_DIR, iter_py_files, render_all
from test_r3_forbidden_api import scan_r3
from test_r4_fail_loud import scan_r4

pytestmark = pytest.mark.advisory


def _spike_files():
    if not SPIKES_DIR.is_dir():
        return []
    return iter_py_files(SPIKES_DIR)


def _record(violations):
    RECORDER.add(violations)
    return violations


def test_spikes_r1_coefficient_advisory():
    """ADVISORY：spike 里的系数硬编码（S3 故意复现 T1 陷阱，属预期内）。"""
    files = _spike_files()
    if not files:
        pytest.skip("spikes/ 目录不存在")

    from test_r1_coefficients import _scan_file as scan_r1_file
    from test_r1_coefficients import _upstream_pairs
    from _scan import Violation, parse_module

    pairs = _upstream_pairs()
    found = []
    for path in files:
        tree = parse_module(path)
        if tree is None:
            continue
        for v in scan_r1_file(path, tree, pairs):
            found.append(Violation(v.rule, v.path, v.lineno, v.message, "ADVISORY"))

    _record(found)
    print(
        f"\n[ADVISORY R1] spikes/ 命中 {len(found)} 处系数硬编码"
        + ("\n" + render_all(found) if found else "")
    )
    assert True  # spike 非交付物，永不阻断


def test_spikes_r3_forbidden_api_advisory():
    """ADVISORY：spike 里的上游禁用 API / 对 ~/.easy_tdx 的写操作。"""
    files = _spike_files()
    if not files:
        pytest.skip("spikes/ 目录不存在")

    found = _record(scan_r3(files, severity="ADVISORY"))
    print(
        f"\n[ADVISORY R3] spikes/ 命中 {len(found)} 处禁用 API / 越界写"
        + ("\n" + render_all(found) if found else "")
    )
    assert True


def test_spikes_r4_fail_loud_advisory():
    """ADVISORY：spike 里的 try/except:pass 与 .get(k, 默认值)。"""
    files = _spike_files()
    if not files:
        pytest.skip("spikes/ 目录不存在")

    found = _record(scan_r4(files, severity="ADVISORY"))
    by_rule: dict[str, int] = {}
    for v in found:
        by_rule[v.rule] = by_rule.get(v.rule, 0) + 1
    print(
        f"\n[ADVISORY R4] spikes/ 命中 {len(found)} 处静默兜底 {by_rule}"
        + ("\n" + render_all(found) if found else "")
    )
    assert True
