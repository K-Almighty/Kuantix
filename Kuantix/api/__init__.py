"""Kuantix REST API 层（T05b1：data / factor / screen 三路由）。

分层约束（NF-4）：``adapters → core → services → api``。本包**不直接
import easy_tdx**（R2），一切业务能力经 :mod:`Kuantix.data` /
:mod:`Kuantix.factor` / :mod:`Kuantix.screen` 的服务门面获取。

路由前缀统一 ``/api/v1``（契约 §8 裁决 2，替代早期 ``/api/data`` 旧口径）：
- ``/api/v1/data/*``   —— data 路由（D1–D7）；
- ``/api/v1/factor/*`` —— factor 路由（F1–F6）；
- ``/api/v1/screen/*`` —— screen 路由（S1–S6）；
- monitor 路由（M1–M17）由另一工程师在 T05b2 补挂，不在本批次。

统一信封（NF-9）：所有 JSON 响应经 :class:`Kuantix.core.envelope.Envelope`
序列化；错误由 :mod:`Kuantix.main` 的异常处理器映射为 4xx/5xx 信封。
"""
