"""红线静态检查器 —— 共享扫描基础设施。

本模块只提供「怎么扫」的能力，不含任何一条红线的判定规则；
判定规则各自写在 ``test_r1_*.py`` ~ ``test_r6_*.py`` 中，便于单条审阅。

设计要点
--------
1. **AST 优先**：所有结构性判定走 ``ast`` 标准库，正则仅用于 AST 拿不到的
   场景（如 docstring / 注释 / 字符串字面量内容匹配）。
2. **代码未落地时优雅跳过**：``require_package_root()`` 在源码目录不存在时
   调用 ``pytest.skip``，而不是报错。
3. **可豁免**：``Allowlist`` 提供 ``文件:行号:理由`` 三段式豁免，缺理由的条目
   本身会让 allowlist 自检用例失败（防止无脑加豁免）。
4. **可汇报**：所有命中经 ``RECORDER`` 汇总，由 conftest 的 terminal summary
   统一输出，方便 CI 抓取。
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# 路径锚点
# --------------------------------------------------------------------------

REDLINES_DIR = Path(__file__).resolve().parent          # <root>/tests/redlines
TESTS_DIR = REDLINES_DIR.parent                          # <root>/tests
PROJECT_ROOT = TESTS_DIR.parent                          # <root>  (= .../Kuantix)
SPIKES_DIR = PROJECT_ROOT / "spikes"

#: 扫描时永远跳过的目录名
EXCLUDE_DIR_NAMES = frozenset(
    {
        ".venv", "venv", "env", "__pycache__", ".git", ".hg", ".svn",
        "node_modules", "build", "dist", ".eggs", "site-packages",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
        "docs", "web", ".idea", ".vscode",
        # 测试代码不受红线约束（测试里写期望系数值是合法的）
        "tests", "test",
    }
)

#: 业务代码目录（NF-5 硬编码检查、NF-1 上游收敛检查的重点对象）
BUSINESS_DIRS = ("data", "factor", "screen", "monitor", "api")

#: 唯一允许触碰 easy_tdx 的目录（NF-1）
ADAPTER_DIR = "adapters"


def package_root() -> Path | None:
    """定位 Kuantix 源码包根目录，未落地时返回 None。

    支持两种常见布局：``<root>/Kuantix/`` 与 ``<root>/src/Kuantix/``。
    """
    for cand in (PROJECT_ROOT / "Kuantix", PROJECT_ROOT / "src" / "Kuantix"):
        if cand.is_dir() and any(_iter_py(cand)):
            return cand
    return None


def require_package_root() -> Path:
    """给测试用：源码未落地时优雅跳过，而不是 error。"""
    root = package_root()
    if root is None:
        pytest.skip(
            "Kuantix 源码包尚未落地（未找到 <root>/Kuantix/*.py 或 <root>/src/Kuantix/*.py），"
            "红线检查器待代码就位后自动生效"
        )
    return root


# --------------------------------------------------------------------------
# 文件遍历与解析
# --------------------------------------------------------------------------


def _iter_py(root: Path):
    for path in sorted(root.rglob("*.py")):
        if any(part in EXCLUDE_DIR_NAMES for part in path.parts):
            continue
        yield path


def iter_py_files(root: Path | None = None) -> list[Path]:
    """列出待扫描的 ``.py`` 文件（已剔除 venv/缓存/测试/前端等噪音目录）。"""
    if root is None:
        root = require_package_root()
    return list(_iter_py(root))


@lru_cache(maxsize=None)
def read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@lru_cache(maxsize=None)
def source_lines(path: Path) -> tuple[str, ...]:
    return tuple(read_source(path).splitlines())


@lru_cache(maxsize=None)
def parse_module(path: Path) -> ast.Module | None:
    """解析为 AST；语法错误返回 None（由 test_syntax 用例单独暴露）。"""
    try:
        return ast.parse(read_source(path), filename=str(path))
    except SyntaxError:
        return None


def syntax_errors(paths: list[Path]) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for p in paths:
        try:
            ast.parse(read_source(p), filename=str(p))
        except SyntaxError as exc:
            out.append((p, f"{exc.msg} (line {exc.lineno})"))
    return out


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def snippet_of(path: Path, lineno: int) -> str:
    lines = source_lines(path)
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


# --------------------------------------------------------------------------
# 违规记录
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    rule: str          # 例如 "R4-A"
    path: Path
    lineno: int
    message: str
    severity: str = "FAIL"   # FAIL / ADVISORY

    @property
    def rel_path(self) -> str:
        return rel(self.path)

    @property
    def key(self) -> str:
        return f"{self.rel_path}:{self.lineno}"

    def render(self) -> str:
        code = snippet_of(self.path, self.lineno)
        tail = f"\n        │ {code}" if code else ""
        return f"  {self.rel_path}:{self.lineno}  [{self.rule}] {self.message}{tail}"


def render_all(violations: list[Violation]) -> str:
    return "\n".join(v.render() for v in violations)


class Recorder:
    """跨用例汇总，供 conftest 输出总结报告。"""

    def __init__(self) -> None:
        self.violations: list[Violation] = []
        self.scopes: dict[str, str] = {}
        self.allowlists: dict[str, "Allowlist"] = {}

    def add(self, items: list[Violation]) -> None:
        self.violations.extend(items)

    def note_scope(self, rule: str, description: str) -> None:
        self.scopes[rule] = description

    def register_allowlist(self, allowlist: "Allowlist") -> None:
        self.allowlists[allowlist.name] = allowlist

    def to_json(self) -> dict:
        return {
            "project_root": str(PROJECT_ROOT),
            "package_root": str(package_root()) if package_root() else None,
            "scopes": self.scopes,
            "violations": [
                {
                    "rule": v.rule,
                    "severity": v.severity,
                    "file": v.rel_path,
                    "line": v.lineno,
                    "message": v.message,
                    "code": snippet_of(v.path, v.lineno),
                }
                for v in self.violations
            ],
            "allowlists": {
                name: [
                    {"pattern": e.pattern, "line": e.lineno, "reason": e.reason, "used": e.used}
                    for e in al.entries
                ]
                for name, al in self.allowlists.items()
            },
        }

    def dump(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


RECORDER = Recorder()


def fail_if(violations: list[Violation], rule: str, headline: str) -> None:
    """统一的断言出口：记录 + 失败。"""
    RECORDER.add(violations)
    hard = [v for v in violations if v.severity == "FAIL"]
    if hard:
        pytest.fail(
            f"\n{headline}\n共 {len(hard)} 处违规：\n{render_all(hard)}\n",
            pytrace=False,
        )


# --------------------------------------------------------------------------
# 豁免清单（allowlist）
# --------------------------------------------------------------------------


@dataclass
class AllowEntry:
    pattern: str        # 相对 PROJECT_ROOT 的路径，支持 fnmatch 通配
    lineno: int | None  # None 表示 "*"（整文件豁免）
    reason: str
    source_line: int    # 该条目在 allowlist 文件里的行号
    used: bool = False

    @property
    def raw(self) -> str:
        loc = "*" if self.lineno is None else str(self.lineno)
        return f"{self.pattern}:{loc}:{self.reason}"


@dataclass
class Allowlist:
    name: str
    path: Path
    entries: list[AllowEntry] = field(default_factory=list)
    malformed: list[tuple[int, str, str]] = field(default_factory=list)

    def match(self, v: Violation) -> AllowEntry | None:
        for e in self.entries:
            if not fnmatch.fnmatch(v.rel_path, e.pattern):
                continue
            if e.lineno is None or e.lineno == v.lineno:
                e.used = True
                return e
        return None

    def filter(self, violations: list[Violation]) -> list[Violation]:
        return [v for v in violations if self.match(v) is None]

    @property
    def unused(self) -> list[AllowEntry]:
        return [e for e in self.entries if not e.used]

    def inventory(self) -> str:
        if not self.entries:
            return f"  （{self.path.name} 当前为空 —— 零豁免）"
        rows = []
        for e in self.entries:
            flag = "✓已命中" if e.used else "·未命中"
            loc = "整文件" if e.lineno is None else f"L{e.lineno}"
            rows.append(f"  [{flag}] {e.pattern} {loc}\n           理由：{e.reason}")
        return "\n".join(rows)


_MIN_REASON_LEN = 15

#: 理由必须引用约束编号（NF-/R/T/PRD/§），或引用守卫断言（见 test_xxx），
#: 或包含可识别的语义前提关键词——挡掉「ok / 没问题 / 暂时豁免」这类敷衍理由。
_CONSTRAINT_RE = re.compile(r"(NF-\d{1,2}|R\d|T\d{1,2}|PRD|§\d)")
_GUARD_RE = re.compile(r"见\s*test_\w+")
_SEMANTIC_KEYWORDS = (
    "业务语义", "不承载", "合法", "已知", "契约", "参考", "隔离区", "零订阅",
    "空集合", "遍历", "安全", "显式", "前置", "合规", "正向", "守卫", "默认",
    "市场档案", "MarketProfile", "参考实现", "复用", "只读", "已知主题", "主题注册",
    "整文件豁免", "研发结论", "团队裁决",
)


def _reason_semantically_valid(reason: str) -> bool:
    """理由是否引用了约束编号 / 守卫断言，或给出了可识别的语义前提。"""
    if _CONSTRAINT_RE.search(reason):
        return True
    if _GUARD_RE.search(reason):
        return True
    return any(kw in reason for kw in _SEMANTIC_KEYWORDS)


def load_allowlist(filename: str, display_name: str) -> Allowlist:
    """解析 ``文件:行号:理由`` 三段式豁免清单。

    - ``#`` 开头与空行忽略
    - 行号可写 ``*`` 表示整文件豁免
    - 理由必须 >= 15 字，且须引用约束编号（NF-/R/T/PRD/§）或语义前提，
      或引用配套守卫断言（``见 test_xxx``）；否则记入 ``malformed``
      （由自检用例暴露，team-lead 裁决 3）
    """
    path = REDLINES_DIR / filename
    al = Allowlist(name=display_name, path=path)
    if not path.exists():
        return al
    for idx, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            al.malformed.append((idx, raw, "格式必须为 `文件:行号:理由`（三段，冒号分隔）"))
            continue
        pat, loc, reason = (p.strip() for p in parts)
        if not pat:
            al.malformed.append((idx, raw, "文件路径为空"))
            continue
        if loc == "*":
            lineno = None
        elif loc.isdigit():
            lineno = int(loc)
        else:
            al.malformed.append((idx, raw, f"行号必须是数字或 `*`，实际为 `{loc}`"))
            continue
        if len(reason) < _MIN_REASON_LEN:
            al.malformed.append(
                (idx, raw, f"理由过短（<{_MIN_REASON_LEN} 字），豁免必须写清为什么")
            )
            continue
        if not _reason_semantically_valid(reason):
            al.malformed.append(
                (idx, raw,
                 "理由须引用约束编号(NF-/R/T/PRD/§)或语义前提，或引用守卫断言(见 test_xxx)")
            )
            continue
        al.entries.append(AllowEntry(pattern=pat, lineno=lineno, reason=reason, source_line=idx))
    RECORDER.register_allowlist(al)
    return al


# --------------------------------------------------------------------------
# AST 小工具
# --------------------------------------------------------------------------


def dotted_name(node: ast.AST) -> str:
    """把 ``a.b.c`` / ``a`` 形态的 AST 还原成点分字符串，拿不到则返回 ""。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    if isinstance(cur, ast.Call):
        inner = dotted_name(cur.func)
        if inner:
            parts.append(inner)
            return ".".join(reversed(parts))
    return ""


def is_number(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def str_constants(node: ast.AST):
    """遍历子树里的所有字符串常量，产出 (值, 行号)。"""
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n.value, getattr(n, "lineno", 0)


def enclosing_class_ranges(tree: ast.Module, base_name_suffix: str) -> list[tuple[int, int]]:
    """返回「基类名以 ``base_name_suffix`` 结尾」的 ClassDef 的行号区间。

    用于 NF-5 豁免：``CNMarketProfile`` 内部写 A 股常量是合法的。
    """
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        names = [dotted_name(b).split(".")[-1] for b in node.bases]
        if node.name.endswith(base_name_suffix) or any(
            n.endswith(base_name_suffix) for n in names
        ):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            ranges.append((node.lineno, end))
    return ranges


def in_ranges(lineno: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= lineno <= hi for lo, hi in ranges)
