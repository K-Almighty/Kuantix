"""盘前分析 / 盘后复盘 模块包入口。

本层暴露业务类（PreOpenService / PostCloseService / LimitClassifier）与
数据持久化类（NewsStore / FundamentalStore / LimitUpDownStore）。
adapters 层之外**不应**直接触碰 provider 或网络，所有外部访问经
:mod:`Kuantix.adapters.news_provider` 注入。
"""

from __future__ import annotations

from .stores import FundamentalStore, LimitUpDownStore, NewsStore

__all__ = [
    "NewsStore",
    "FundamentalStore",
    "LimitUpDownStore",
]

# 业务类在对应文件实现后延迟导入（避免 T4/T5 阶段的循环 import）。
# 实际使用前必须已实现对应文件：
try:  # pragma: no cover - 可选阶段导入
    from .pre_open import PreOpenService  # noqa: F401
    from .post_close import LimitClassifier, PostCloseService  # noqa: F401
    from .report import report_markdown, report_json_dict  # noqa: F401
    from .scheduler import build_analysis_components, register_analysis_jobs  # noqa: F401

    __all__.extend([
        "PreOpenService",
        "LimitClassifier",
        "PostCloseService",
        "report_markdown",
        "report_json_dict",
        "build_analysis_components",
        "register_analysis_jobs",
    ])
except ImportError:
    pass
