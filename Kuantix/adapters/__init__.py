"""Kuantix 适配层 —— **全项目唯一** import ``easy_tdx`` 的地方（NF-1）。

分层单向依赖（NF-4）：``adapters → core → services → api``。
``core`` 层不得 import ``easy_tdx``；所有上游调用必须收敛在本包内，
这样"上游只读复用"这条红线才有一个可静态审计的边界。

本包内各模块与红线的对应关系
----------------------------

==================  =====================================================
模块                 红线 / 陷阱
==================  =====================================================
``coefficients``    NF-25 系数 import 引用（禁复制）；陷阱 T1 UNKNOWN 兜底
``tdx_client``      NF-1 禁 ``from_best_host``；NF-20 不写上游目录；陷阱 T5 三链路
``universe``        RD-10 每页新连接（512× 差距）；陷阱 T4
``quotation``       RD-8 vol 股→手 ÷100；陷阱 T2；RD-5 只存未复权
``vipdoc_writer``   RD-1 系数一致 / RD-2 自补 fsync / RD-9 uint32 上界 / NF-27 回读
``known_hosts``     NF-20 只读 ``~/.easy_tdx/config.json``
==================  =====================================================
"""

from __future__ import annotations

from Kuantix.adapters.coefficients import (
    UNKNOWN_SECURITY_TYPE,
    CoefficientResolver,
    Coefficients,
    assert_upstream_version,
    detect_security_type,
    known_security_types,
    resolve_coefficients,
    upstream_coefficient_table,
)
from Kuantix.adapters.known_hosts import (
    EASY_TDX_CONFIG_PATH,
    FileFingerprint,
    HostBook,
    assert_untouched,
    build_host_book,
    fingerprint,
    read_easy_tdx_config,
)
from Kuantix.adapters.quotation import (
    EX_MARKET_CODES,
    KlineRoute,
    QuotationFetcher,
    VolUnit,
    VolUnitProbe,
)
from Kuantix.adapters.tdx_client import (
    FORBIDDEN_UPSTREAM_FACTORIES,
    ClientKind,
    ClientSpec,
    TdxClientFactory,
)
from Kuantix.adapters.universe import (
    A_SHARE_TYPES,
    CN_EXCHANGE_BY_MARKET,
    EnumerationResult,
    EnumerationStats,
    UniverseEnumerator,
    group_by_exchange,
)
from Kuantix.adapters.vipdoc_writer import (
    FLOAT32_MAX,
    UINT32_MAX,
    UINT32_MIN,
    BoundCheck,
    VipdocWriter,
    WriteReport,
)

__all__ = [
    # coefficients（NF-25）
    "UNKNOWN_SECURITY_TYPE",
    "Coefficients",
    "CoefficientResolver",
    "upstream_coefficient_table",
    "known_security_types",
    "detect_security_type",
    "resolve_coefficients",
    "assert_upstream_version",
    # known_hosts（NF-20）
    "EASY_TDX_CONFIG_PATH",
    "FileFingerprint",
    "HostBook",
    "fingerprint",
    "assert_untouched",
    "read_easy_tdx_config",
    "build_host_book",
    # tdx_client（NF-1 / T5）
    "ClientKind",
    "ClientSpec",
    "TdxClientFactory",
    "FORBIDDEN_UPSTREAM_FACTORIES",
    # universe（RD-10）
    "UniverseEnumerator",
    "EnumerationResult",
    "EnumerationStats",
    "CN_EXCHANGE_BY_MARKET",
    "A_SHARE_TYPES",
    "group_by_exchange",
    # quotation（RD-8）
    "QuotationFetcher",
    "KlineRoute",
    "VolUnit",
    "VolUnitProbe",
    "EX_MARKET_CODES",
    # vipdoc_writer（RD-1/2/9 + NF-27）
    "VipdocWriter",
    "WriteReport",
    "BoundCheck",
    "UINT32_MAX",
    "UINT32_MIN",
    "FLOAT32_MAX",
]
