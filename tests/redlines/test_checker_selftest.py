"""检查器自测：用合成样本证明 R1–R6 真的会开火，且不会误伤。

为什么必须有这个文件
--------------------
一个**永远不报警的静态检查器**比没有检查器更危险——它会给团队"红线已守住"
的虚假安全感。这正是 NF-26 说的「错得很自信」。所以每条规则都要有：

- **阳性样本**：明确违规的代码片段 → 必须命中
- **阴性样本**：形似但合规的代码片段 → 必须放行（防误伤）

样本以字符串形式内联，经 ``ast.parse`` 后直接喂给各规则的 ``_scan_file``，
不落任何临时文件，也不依赖业务源码是否落地。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

FAKE = Path("/tmp/Kuantix_selftest_sample.py")


def _rules(src: str, scanner, *args):
    tree = ast.parse(src)
    return scanner(FAKE, tree, *args)


def _codes(violations) -> set[str]:
    return {v.rule for v in violations}


# ==========================================================================
# R1 —— 系数表无副本（NF-25）
# ==========================================================================


@pytest.fixture(scope="module")
def r1():
    from test_r1_coefficients import _scan_file, _upstream_pairs

    return _scan_file, _upstream_pairs()


def test_r1_catches_dict_copy(r1):
    scan, pairs = r1
    src = (
        "_SECURITY_COEFFICIENTS = {\n"
        '    "SH_A_STOCK": (0.01, 0.01),\n'
        '    "SZ_FUND": (0.001, 0.01),\n'
        "}\n"
    )
    hits = _codes(_rules(src, scan, pairs))
    assert "R1-A" in hits or "R1-C" in hits, hits


def test_r1_catches_renamed_copy(r1):
    """换个名字照抄，一样要抓到。"""
    scan, pairs = r1
    src = 'MY_COEFF_TABLE = {"SH_A_STOCK": (0.01, 0.01), "SH_INDEX": (0.01, 1.0)}\n'
    hits = _codes(_rules(src, scan, pairs))
    assert hits & {"R1-A", "R1-B"}, hits


def test_r1_catches_bare_pair_assignment(r1):
    scan, pairs = r1
    src = "DEFAULT_COEFF = (0.01, 0.01)\n"
    assert "R1-D" in _codes(_rules(src, scan, pairs))


def test_r1_catches_literal_coeff_in_write_call(r1):
    """S3 里那句 `sync_daily_bars_from_security_bars(f, bars, 0.01, 0.01)`。"""
    scan, pairs = r1
    src = "sync_daily_bars_from_security_bars(etf_file, bars, 0.01, 0.01)\n"
    assert "R1-E" in _codes(_rules(src, scan, pairs))


def test_r1_allows_upstream_import_and_lookup(r1):
    """合规写法：从上游 import 后按类型查表 —— 必须零命中。"""
    scan, pairs = r1
    src = (
        "from easy_tdx.offline.daily_bar import _SECURITY_COEFFICIENTS\n"
        "def coeff_for(sec_type: str):\n"
        "    return _SECURITY_COEFFICIENTS[sec_type]\n"
    )
    assert _rules(src, scan, pairs) == []


def test_r1_allows_reexport_alias(r1):
    """`_SECURITY_COEFFICIENTS = <import 来的名字>` 属再导出，不是副本。"""
    scan, pairs = r1
    src = (
        "from easy_tdx.offline import daily_bar\n"
        "_SECURITY_COEFFICIENTS = daily_bar._SECURITY_COEFFICIENTS\n"
    )
    assert _rules(src, scan, pairs) == []


def test_r1_ignores_unrelated_float_pairs(r1):
    """(0.5, 0.5) 不是上游系数值，不该误报。"""
    scan, pairs = r1
    src = "WEIGHTS = (0.5, 0.5)\n"
    assert _rules(src, scan, pairs) == []


# ==========================================================================
# R2 —— 上游调用收敛在 adapters（NF-1）
# ==========================================================================


def test_r2_catches_upstream_import_outside_adapters(tmp_path):
    """在 factor/ 下 import easy_tdx 必须命中。"""
    import test_r2_upstream_confined as r2

    pkg = tmp_path / "Kuantix"
    (pkg / "factor").mkdir(parents=True)
    (pkg / "adapters").mkdir()
    bad = pkg / "factor" / "service.py"
    bad.write_text("from easy_tdx.factor.engine import FactorEngine\n", encoding="utf-8")
    good = pkg / "adapters" / "factor_bridge.py"
    good.write_text("from easy_tdx.factor.engine import FactorEngine\n", encoding="utf-8")

    from _scan import iter_py_files, parse_module

    found = []
    for path in iter_py_files(pkg):
        if path.parts[-2] == "adapters":
            continue
        tree = parse_module(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("easy_tdx"):
                found.append(path.name)
    assert found == ["service.py"], f"应只命中 factor/service.py，实际 {found}"


def test_r2_dynamic_import_detected():
    """动态 import 绕道也要抓住。"""
    src = 'mod = importlib.import_module("easy_tdx.mac.client")\n'
    tree = ast.parse(src)
    from _scan import dotted_name

    hit = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and dotted_name(node.func).endswith("import_module"):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and arg.value.startswith("easy_tdx"):
                hit = True
    assert hit


# ==========================================================================
# R3 —— 上游禁用 API（NF-1 / NF-20）
# ==========================================================================


@pytest.fixture(scope="module")
def r3():
    from test_r3_forbidden_api import _scan_forbidden_symbols, _scan_writes

    def scan(path, tree):
        return _scan_forbidden_symbols(path, tree) + _scan_writes(path, tree)

    return scan


def test_r3_catches_from_best_host(r3):
    src = "client = MacClient.from_best_host()\n"
    assert "R3-A" in _codes(_rules(src, r3))


def test_r3_catches_detect_tdx_home(r3):
    src = "from easy_tdx.offline.paths import detect_tdx_home\nhome = detect_tdx_home()\n"
    assert "R3-B" in _codes(_rules(src, r3))


def test_r3_catches_getattr_bypass(r3):
    """getattr 绕道调用也要抓。"""
    src = 'f = getattr(MacClient, "from_best_host")\n'
    assert "R3-A" in _codes(_rules(src, r3))


def test_r3_catches_private_df_import(r3):
    src = "from easy_tdx._df import to_dataframe\n"
    assert "R3-C" in _codes(_rules(src, r3))


def test_r3_catches_write_to_easytdx_config(r3):
    src = (
        "from pathlib import Path\n"
        "cfg = Path.home() / '.easy_tdx' / 'config.json'\n"
        "with open(cfg, 'w') as f:\n"
        "    json.dump(data, f)\n"
    )
    assert "R3-D" in _codes(_rules(src, r3))


def test_r3_catches_write_text_on_tainted_path(r3):
    src = (
        "cfg = Path('~/.easy_tdx/config.json').expanduser()\n"
        "cfg.write_text('{}')\n"
    )
    assert "R3-D" in _codes(_rules(src, r3))


def test_r3_catches_taint_propagation_through_variables(r3):
    """污点要能跨变量传播两跳。"""
    src = (
        "base = Path.home() / '.easy_tdx'\n"
        "target = base / 'config.json'\n"
        "target.unlink()\n"
    )
    assert "R3-D" in _codes(_rules(src, r3))


def test_r3_allows_reading_easytdx_config(r3):
    """NF-20 明确允许只读复用 known_hosts —— 读操作必须放行。"""
    src = (
        "from pathlib import Path\n"
        "cfg = Path.home() / '.easy_tdx' / 'config.json'\n"
        "if cfg.exists():\n"
        "    data = json.loads(cfg.read_text())\n"
        "with open(cfg) as f:\n"
        "    data2 = json.load(f)\n"
    )
    assert _rules(src, r3) == [], _rules(src, r3)


def test_r3_allows_write_to_Kuantix_dir(r3):
    """写自己的 ~/.Kuantix/ 是完全合规的（NF-15/18）。"""
    src = (
        "cfg = Path.home() / '.Kuantix' / 'db' / 'meta.json'\n"
        "cfg.write_text('{}')\n"
    )
    assert _rules(src, r3) == []


# ==========================================================================
# R4 —— fail-loud（NF-26）
# ==========================================================================


@pytest.fixture(scope="module")
def r4():
    from test_r4_fail_loud import _scan_file

    return _scan_file


def test_r4_catches_except_pass(r4):
    src = "try:\n    risky()\nexcept Exception:\n    pass\n"
    assert "R4-A" in _codes(_rules(src, r4))


def test_r4_catches_bare_except_pass(r4):
    src = "try:\n    risky()\nexcept:\n    pass\n"
    assert "R4-A" in _codes(_rules(src, r4))


def test_r4_catches_except_ellipsis(r4):
    src = "try:\n    risky()\nexcept ValueError:\n    ...\n"
    assert "R4-A" in _codes(_rules(src, r4))


def test_r4_catches_two_arg_get(r4):
    """上游 T2 陷阱的原型写法。"""
    src = "coeff = _SECURITY_COEFFICIENTS.get(sec_type, (0.01, 0.01))\n"
    assert "R4-B" in _codes(_rules(src, r4))


def test_r4_catches_contextlib_suppress(r4):
    src = "with contextlib.suppress(Exception):\n    risky()\n"
    assert "R4-C" in _codes(_rules(src, r4))


def test_r4_allows_except_with_handling(r4):
    """记日志 + 计入隔离区 + 重抛，都是合规的。"""
    src = (
        "try:\n"
        "    risky()\n"
        "except Exception as exc:\n"
        "    logger.exception('failed')\n"
        "    quarantine.add(code, reason=str(exc))\n"
        "    raise\n"
    )
    assert _rules(src, r4) == []


def test_r4_allows_single_arg_get(r4):
    """单参 .get() + 显式判空是推荐写法，不该命中。"""
    src = (
        "coeff = table.get(sec_type)\n"
        "if coeff is None:\n"
        "    raise UnknownSecurityType(sec_type)\n"
    )
    assert _rules(src, r4) == []


def test_r4_allows_finally_cleanup(r4):
    src = "try:\n    conn.use()\nfinally:\n    conn.close()\n"
    assert _rules(src, r4) == []


# ==========================================================================
# R5 —— 无下单接口（NF-21）
# ==========================================================================


@pytest.fixture(scope="module")
def r5():
    from test_r5_no_trading import _scan_file

    return _scan_file


def test_r5_catches_place_order_def(r5):
    src = "def place_order(code, qty):\n    ...\n"
    assert "R5-A" in _codes(_rules(src, r5))


def test_r5_catches_buy_method_def(r5):
    src = "class Broker:\n    def buy(self, code, qty):\n        ...\n"
    assert "R5-A" in _codes(_rules(src, r5))


def test_r5_catches_broker_sdk_import(r5):
    src = "import easytrader\n"
    assert "R5-C" in _codes(_rules(src, r5))


def test_r5_catches_trade_route(r5):
    src = (
        '@router.post("/api/v1/trade/order")\n'
        "async def submit(payload):\n"
        "    ...\n"
    )
    assert "R5-B" in _codes(_rules(src, r5))


def test_r5_does_not_flag_data_structure_fields(r5):
    """ScreenResult.direction = "buy" 这类字段不该误伤（关键防误报用例）。"""
    src = (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class ScreenResult:\n"
        "    code: str\n"
        "    direction: str = 'buy'\n"
        "    signal: str = 'sell'\n"
        "SIGNALS = {'buy': 1, 'sell': -1}\n"
    )
    assert _rules(src, r5) == [], _rules(src, r5)


def test_r5_does_not_flag_normal_read_routes(r5):
    src = '@router.get("/api/v1/screen/run")\ndef run():\n    ...\n'
    assert _rules(src, r5) == []


def test_r5_backtest_context_allows_buy_but_not_place_order():
    """回测语境放行 buy/sell，但真实下单 API 照样禁。"""
    from test_r5_no_trading import _scan_file

    bt = Path("/tmp/Kuantix/adapters/backtest_bridge.py")
    tree = ast.parse("def buy(code, qty):\n    ...\ndef place_order(code, qty):\n    ...\n")
    hits = _scan_file(bt, tree)
    msgs = [v.message for v in hits]
    assert len(hits) == 1, f"回测语境应只命中 place_order，实际 {msgs}"
    assert "place_order" in msgs[0]


# ==========================================================================
# R6 —— 无硬编码 A 股常量（NF-5）
# ==========================================================================


@pytest.fixture(scope="module")
def r6():
    from test_r6_hardcoded_cn import _scan_file

    return _scan_file


def test_r6_catches_currency(r6):
    src = 'payload = {"currency": "CNY"}\n'
    assert "R6-A" in _codes(_rules(src, r6))


def test_r6_catches_timezone_string(r6):
    src = 'tz = ZoneInfo("Asia/Shanghai")\n'
    assert "R6-B" in _codes(_rules(src, r6))


def test_r6_catches_utc8_timedelta(r6):
    src = "tz = timezone(timedelta(hours=8))\n"
    assert "R6-B" in _codes(_rules(src, r6))


def test_r6_catches_session_string(r6):
    src = 'OPEN = "09:30"\n'
    assert "R6-C" in _codes(_rules(src, r6))


def test_r6_catches_exchange_token(r6):
    """R6-H（裁决 2 新增）：业务层硬编码交易所标识须命中，且全域扫描不收窄。"""
    src = 'market = "SZSE"\nexch = "SH"\n'
    hits = _codes(_rules(src, r6))
    assert "R6-H" in hits, hits


def test_r6_catches_session_time_call(r6):
    src = "if now.time() >= time(9, 30):\n    pass\n"
    assert "R6-C" in _codes(_rules(src, r6))


def test_r6_catches_trading_days(r6):
    src = "annualized = ret * 252\n"
    assert "R6-D" in _codes(_rules(src, r6))


def test_r6_catches_price_limit_in_context(r6):
    src = "price_limit = 0.1\n"
    assert "R6-E" in _codes(_rules(src, r6))


def test_r6_catches_lot_size_in_context(r6):
    src = "lot_size = 100\n"
    assert "R6-F" in _codes(_rules(src, r6))


def test_r6_ignores_bare_0_1_without_context(r6):
    """0.1 作为普通阈值/权重不该误报（关键防误报用例）。"""
    src = "ic_threshold = 0.1\nweight = 0.2\nalpha = 0.05\n"
    assert _rules(src, r6) == [], _rules(src, r6)


def test_r6_ignores_bare_100_without_context(r6):
    src = "top_n = 100\nbatch_size = 100\npercent = value * 100\n"
    assert _rules(src, r6) == [], _rules(src, r6)


def test_r6_exempts_market_profile_class_body(r6):
    """CNMarketProfile 内部写 A 股常量是合法的（NF-5 允许）。"""
    src = (
        "class CNMarketProfile(MarketProfile):\n"
        '    currency = "CNY"\n'
        '    timezone = "Asia/Shanghai"\n'
        "    price_limit = 0.1\n"
        "    lot_size = 100\n"
        "    trading_days_per_year = 252\n"
    )
    assert _rules(src, r6) == [], _rules(src, r6)


def test_r6_still_flags_outside_profile_class(r6):
    """同一文件里，profile 类外的硬编码照样命中。"""
    src = (
        "class CNMarketProfile(MarketProfile):\n"
        '    currency = "CNY"\n'
        "\n"
        'FALLBACK_CURRENCY = "CNY"\n'
    )
    hits = _rules(src, r6)
    assert len(hits) == 1 and hits[0].lineno == 4, [(v.lineno, v.message) for v in hits]


# ==========================================================================
# allowlist 机制
# ==========================================================================


def test_allowlist_parsing_and_matching(tmp_path, monkeypatch):
    import _scan

    content = (
        "# 注释行\n"
        "\n"
        "Kuantix/config.py:42:配置项缺省值来自 config.toml 模板，不承载业务语义\n"
        "Kuantix/api/routers/*.py:*:路由模块为通用契约层，不承载 A 股业务语义（NF-5）\n"
        "bad_line_without_reason\n"
        "Kuantix/x.py:abc:行号非法\n"
        "Kuantix/y.py:1:短\n"
    )
    f = tmp_path / "sample_allowlist.txt"
    f.write_text(content, encoding="utf-8")
    monkeypatch.setattr(_scan, "REDLINES_DIR", tmp_path)

    al = _scan.load_allowlist("sample_allowlist.txt", "selftest")
    assert len(al.entries) == 2, [e.raw for e in al.entries]
    assert len(al.malformed) == 3, al.malformed

    v_hit = _scan.Violation("R4-B", _scan.PROJECT_ROOT / "Kuantix" / "config.py", 42, "x")
    v_miss = _scan.Violation("R4-B", _scan.PROJECT_ROOT / "Kuantix" / "config.py", 43, "x")
    v_glob = _scan.Violation(
        "R4-B", _scan.PROJECT_ROOT / "Kuantix" / "api" / "routers" / "data.py", 7, "x"
    )
    assert al.match(v_hit) is not None
    assert al.match(v_miss) is None, "行号不同不应被豁免"
    assert al.match(v_glob) is not None, "通配 + `*` 行号应命中"

    remaining = al.filter([v_hit, v_miss, v_glob])
    assert [v.lineno for v in remaining] == [43]
