"""T02 独立验收台 conftest。

与红线检查器（tests/redlines/）和工程师自带单测**刻意错开**：
- 不 import 工程师的任何测试模块，不复用他的断言；
- 只用设计文档（docs/system_design.md §T02）与已落地契约层作为事实来源，
  用不同思路重新证明代码正确（独立验收台的价值所在）；
- 工程师代码（VipdocWriter / UniverseEnumerator / QuarantineStore 等）未落地时，
  相关用例优雅 skip，绝不 error。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 项目根（含 Kuantix/ 包与 tests/）加入 sys.path，使 `import Kuantix` 可用
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
