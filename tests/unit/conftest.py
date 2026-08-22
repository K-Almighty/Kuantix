"""T02 工程师单测 conftest（白盒，与验收台/红线检查器刻意错开）。

职责：
- 把项目根加入 sys.path，使 ``import Kuantix`` 与 ``import easy_tdx`` 可用；
- 不引入任何验收台断言（tests/acceptance/ 独立思路）；
- 不引入任何红线判定（tests/redlines/ 静态扫描）；
- 只做「白盒单测」：贴近模块内部实现，例如直接调用
  ``VipdocWriter._check_bounds`` / ``UniverseEnumerator._fetch_page`` /
  ``coefficients._VALIDATED_TABLE`` 等私有成员，验证内部正确性。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
