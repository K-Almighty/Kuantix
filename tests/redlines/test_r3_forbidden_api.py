"""R3 —— 上游禁用 API 清单（NF-1 / NF-20）。

约束来源
--------
- system_design §8：「上游只读（NF-1）：……禁 `from_best_host` / 写 `~/.easy_tdx`。」
- PRD NF-20：「可选读取 `~/.easy_tdx/config.json` 中已测速的最优节点作为默认连接
  （**只读不写**）。」
- system_design §7 T02：「客户端工厂（每进程单例、**显式 host/port、禁 from_best_host**）」
- PRD Q3 探测结论：macOS 本机**无通达信客户端、无任何 vipdoc 目录**
  → `detect_tdx_home()` 必然返回 None / 失败，业务链路不得依赖它。

为什么禁
--------
1. ``from_best_host``：上游实现会**自动改写用户的 ~/.easy_tdx/config.json**
   （回写测速结果）。Kuantix 对上游配置只读（NF-20），改写它属于越界副作用。
2. ``detect_tdx_home``：本机无通达信安装，调用必失败；依赖它等于埋一颗必炸的雷，
   且失败路径极易被 `try/except: pass` 兜底成静默降级（违反 NF-26）。
3. ``easy_tdx._df``：上游私有 DataFrame 便捷层，非稳定接口，且会隐式拉 pandas
   转换语义；Kuantix 的 DataFrame 转换应在 adapters 内自己控制。
4. **写 ~/.easy_tdx**：NF-20 明确「只读不写」；NF-15/18 要求 Kuantix 数据落在
   `~/.Kuantix/`，与 `~/.easy_tdx/` 完全隔离。

判定逻辑（一句话）
------------------
AST 扫描函数名/属性名/import 名，命中 ``from_best_host`` / ``detect_tdx_home`` /
``easy_tdx._df`` → FAIL；再用「污点传播 lite」把带 ``.easy_tdx`` 字面量的路径变量
标记为污点，凡污点对象上出现写操作（open('w')/write_text/json.dump/unlink/mkdir…）
→ FAIL（**读操作放行**，因为 NF-20 允许只读复用 known_hosts）。

细分子规则
----------
- **R3-A** 调用 / import ``from_best_host``
- **R3-B** 调用 / import ``detect_tdx_home``
- **R3-C** import 上游私有 ``_df`` 模块
- **R3-D** 对 ``~/.easy_tdx`` 路径的写操作（读放行）
"""

from __future__ import annotations

import ast
import re

from _scan import (
    RECORDER,
    Violation,
    dotted_name,
    fail_if,
    iter_py_files,
    parse_module,
    require_package_root,
)

FORBIDDEN_SYMBOLS = {
    "from_best_host": (
        "R3-A",
        "会自动回写用户的 ~/.easy_tdx/config.json（NF-20 要求只读）；"
        "请改用显式 host/port 构造客户端，节点从只读 known_hosts 里选",
    ),
    "detect_tdx_home": (
        "R3-B",
        "本机 macOS 无通达信安装（PRD Q3 实地探测结论），调用必失败；"
        "vipdoc 根路径应由 config.toml 显式配置为 ~/.Kuantix/vipdoc",
    ),
}

#: 上游私有 DataFrame 便捷层
PRIVATE_DF_MODULE = "_df"

EASYTDX_PATH_RE = re.compile(r"\.easy_tdx(\b|/|\\)", re.I)

#: 污点对象上的写方法
WRITE_METHODS = frozenset(
    {
        "write", "writelines", "write_text", "write_bytes",
        "touch", "mkdir", "makedirs", "unlink", "rmdir",
        "replace", "rename", "chmod", "symlink_to", "hardlink_to",
    }
)
WRITE_MODE_RE = re.compile(r"[waxWAX+]")

RECORDER.note_scope("R3", "全包扫描 —— 上游禁用 API + 禁写 ~/.easy_tdx（NF-1/NF-20）")


# --------------------------------------------------------------------------
# 污点传播 lite：识别「指向 ~/.easy_tdx 的路径变量」
# --------------------------------------------------------------------------


def _has_easytdx_literal(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if EASYTDX_PATH_RE.search(n.value):
                return True
    return False


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _binding(node: ast.AST) -> tuple[list[str], ast.AST] | None:
    """把一个 AST 节点归一化成「绑定了哪些名字 ← 什么表达式」。"""
    targets: list[ast.AST] = []
    value: ast.AST | None = None
    if isinstance(node, ast.Assign):
        targets, value = list(node.targets), node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets, value = [node.target], node.value
    elif isinstance(node, ast.NamedExpr):
        targets, value = [node.target], node.value
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        targets, value = [node.optional_vars], node.context_expr
    if value is None:
        return None
    names = [t.id for t in targets if isinstance(t, ast.Name)]
    return (names, value) if names else None


def _param_names(scope: ast.AST) -> set[str]:
    args = getattr(scope, "args", None)
    if args is None:
        return set()
    collected = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if args.vararg:
        collected.append(args.vararg)
    if args.kwarg:
        collected.append(args.kwarg)
    return {a.arg for a in collected}


def _build_scopes(tree: ast.Module):
    """按函数作用域切分 AST。

    这一步是必须的：污点分析若在模块层拍平，``f = open(cfg)`` 里的 ``f``
    会污染**另一个函数**里同名的 ``f = open(结果文件, 'w')``，产生误报。
    Python 里 ``f`` / ``fp`` / ``path`` 这类文件句柄名复用极其普遍。
    """
    order: list[tuple[ast.AST, ast.AST | None]] = [(tree, None)]
    owner: dict[int, ast.AST] = {id(tree): tree}

    def descend(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, SCOPE_TYPES):
                owner[id(child)] = scope          # def 语句本身属于外层作用域
                order.append((child, scope))
                descend(child, child)
            else:
                owner[id(child)] = scope
                descend(child, scope)

    descend(tree, tree)
    return order, owner


class TaintMap:
    """作用域感知的污点查询表。"""

    def __init__(self, taint: dict[int, set[str]], owner: dict[int, ast.AST], root: ast.AST):
        self._taint = taint
        self._owner = owner
        self._root = root

    def for_node(self, node: ast.AST) -> set[str]:
        scope = self._owner.get(id(node), self._root)
        return self._taint.get(id(scope), set())


def _collect_tainted(tree: ast.Module) -> TaintMap:
    """按作用域计算污点集合。

    规则：
    - 子作用域**继承**父作用域的污点
    - 但在本作用域内被重新绑定的名字（含函数参数）**不继承**，由本地赋值重新判定
      —— 这正是 Python 的名字遮蔽语义，也是消除跨函数误报的关键
    """
    order, owner = _build_scopes(tree)

    own_pairs: dict[int, list[tuple[list[str], ast.AST]]] = {}
    for node in ast.walk(tree):
        bind = _binding(node)
        if bind is None:
            continue
        scope = owner.get(id(node), tree)
        own_pairs.setdefault(id(scope), []).append(bind)

    taint: dict[int, set[str]] = {}
    for scope, parent in order:
        pairs = own_pairs.get(id(scope), [])
        local_names = {n for names, _ in pairs for n in names} | _param_names(scope)
        inherited = taint.get(id(parent), set()) if parent is not None else set()
        cur = set(inherited) - local_names

        changed = True
        while changed:
            changed = False
            for names, value in pairs:
                if _has_easytdx_literal(value) or (_names_in(value) & cur):
                    for n in names:
                        if n not in cur:
                            cur.add(n)
                            changed = True
        taint[id(scope)] = cur

    return TaintMap(taint, owner, tree)


def _expr_is_tainted(node: ast.AST | None, tainted: TaintMap) -> bool:
    if node is None:
        return False
    return _has_easytdx_literal(node) or bool(_names_in(node) & tainted.for_node(node))


def _mode_is_write(node: ast.Call) -> bool:
    """open()/Path.open() 的 mode 是否含写语义。"""
    mode_node = None
    if len(node.args) >= 2:
        mode_node = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode_node = kw.value
    if mode_node is None:
        return False  # 默认 'r'，只读放行
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return bool(WRITE_MODE_RE.search(mode_node.value))
    return True  # mode 是变量，无法确定 → fail-loud 精神：视为可写


def _scan_writes(path, tree: ast.Module) -> list[Violation]:
    tainted = _collect_tainted(tree)
    out: list[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = dotted_name(node.func)
        short = fn.split(".")[-1]

        # open(tainted, 'w')
        if short == "open" and node.args:
            recv = node.func.value if isinstance(node.func, ast.Attribute) else None
            target_expr = node.args[0] if not isinstance(node.func, ast.Attribute) else recv
            if _expr_is_tainted(target_expr, tainted) and _mode_is_write(node):
                out.append(
                    Violation(
                        "R3-D", path, node.lineno,
                        "以写模式打开 ~/.easy_tdx 下的文件；NF-20 规定上游配置**只读不写**，"
                        "Kuantix 自己的状态请落在 ~/.Kuantix/（NF-15/18 隔离）",
                    )
                )
            continue

        # tainted.write_text(...) / tainted.unlink() / ...
        if isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_METHODS:
            if _expr_is_tainted(node.func.value, tainted):
                out.append(
                    Violation(
                        "R3-D", path, node.lineno,
                        f"对 ~/.easy_tdx 路径执行写操作 `.{node.func.attr}(...)`；"
                        "NF-20 只读不写",
                    )
                )
            continue

        # json.dump(obj, tainted_fp) / os.remove(tainted) / shutil.copy(src, tainted)
        if fn in ("json.dump",) and len(node.args) >= 2:
            if _expr_is_tainted(node.args[1], tainted):
                out.append(
                    Violation(
                        "R3-D", path, node.lineno,
                        "json.dump 写入 ~/.easy_tdx 目标；NF-20 只读不写",
                    )
                )
        elif short in ("remove", "unlink", "rmtree", "makedirs", "mkdir") and node.args:
            if _expr_is_tainted(node.args[0], tainted):
                out.append(
                    Violation(
                        "R3-D", path, node.lineno,
                        f"`{fn}(...)` 修改 ~/.easy_tdx 目录结构；NF-20 只读不写",
                    )
                )
        elif short in ("copy", "copy2", "copyfile", "move") and len(node.args) >= 2:
            if _expr_is_tainted(node.args[1], tainted):
                out.append(
                    Violation(
                        "R3-D", path, node.lineno,
                        f"`{fn}(...)` 向 ~/.easy_tdx 写入文件；NF-20 只读不写",
                    )
                )
    return out


def _scan_forbidden_symbols(path, tree: ast.Module) -> list[Violation]:
    out: list[Violation] = []
    for node in ast.walk(tree):
        # 属性访问 / 直接调用
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_SYMBOLS:
            rule, why = FORBIDDEN_SYMBOLS[node.attr]
            out.append(Violation(rule, path, node.lineno, f"禁用上游 API `{node.attr}`：{why}"))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_SYMBOLS:
            rule, why = FORBIDDEN_SYMBOLS[node.id]
            out.append(Violation(rule, path, node.lineno, f"禁用上游 API `{node.id}`：{why}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                if alias.name in FORBIDDEN_SYMBOLS:
                    rule, why = FORBIDDEN_SYMBOLS[alias.name]
                    out.append(
                        Violation(rule, path, node.lineno, f"import 禁用上游 API `{alias.name}`：{why}")
                    )
                # R3-C: from easy_tdx import _df
                if mod.startswith("easy_tdx") and alias.name == PRIVATE_DF_MODULE:
                    out.append(
                        Violation(
                            "R3-C", path, node.lineno,
                            "import 上游私有模块 `easy_tdx._df`：非稳定接口，"
                            "DataFrame 转换请在 adapters 内自行控制",
                        )
                    )
            # from easy_tdx._df import xxx
            if mod.startswith("easy_tdx") and (
                mod.endswith(f".{PRIVATE_DF_MODULE}") or f".{PRIVATE_DF_MODULE}." in mod
            ):
                out.append(
                    Violation(
                        "R3-C", path, node.lineno,
                        f"import 上游私有模块 `{mod}`：非稳定接口，禁止依赖",
                    )
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("easy_tdx") and (
                    alias.name.endswith(f".{PRIVATE_DF_MODULE}")
                    or f".{PRIVATE_DF_MODULE}." in alias.name
                ):
                    out.append(
                        Violation(
                            "R3-C", path, node.lineno,
                            f"import 上游私有模块 `{alias.name}`：非稳定接口，禁止依赖",
                        )
                    )
        elif isinstance(node, ast.Call):
            # getattr(client, "from_best_host")
            fn = dotted_name(node.func).split(".")[-1]
            if fn == "getattr" and len(node.args) >= 2:
                second = node.args[1]
                if isinstance(second, ast.Constant) and second.value in FORBIDDEN_SYMBOLS:
                    rule, why = FORBIDDEN_SYMBOLS[second.value]
                    out.append(
                        Violation(
                            rule, path, node.lineno,
                            f"通过 getattr 绕道调用禁用 API `{second.value}`：{why}",
                        )
                    )
    return out


def scan_r3(paths, severity: str = "FAIL") -> list[Violation]:
    """对给定文件列表跑 R3 全部子规则（供 spike 预警复用）。"""
    out: list[Violation] = []
    for path in paths:
        tree = parse_module(path)
        if tree is None:
            continue
        out.extend(_scan_forbidden_symbols(path, tree))
        out.extend(_scan_writes(path, tree))
    if severity != "FAIL":
        out = [Violation(v.rule, v.path, v.lineno, v.message, severity) for v in out]
    return out


def test_r3_no_forbidden_upstream_api():
    """R3-A~D：禁用 API 清单 + 禁写 ~/.easy_tdx。"""
    require_package_root()
    files = iter_py_files()
    violations = scan_r3(files)

    fail_if(
        violations,
        "R3",
        f"【R3 / NF-1+NF-20 上游禁用 API】扫描 {len(files)} 个文件后发现禁用调用。\n"
        "禁用清单：from_best_host（会改写用户配置）、detect_tdx_home（本机必失败）、"
        "easy_tdx._df（私有接口）、对 ~/.easy_tdx 的任何写操作。\n"
        "注意：对 ~/.easy_tdx/config.json 的**读**是允许的（NF-20 known_hosts 只读复用）。",
    )
