"""上游 known_hosts **只读**加载（NF-20 / NF-1）。

用户的 ``~/.easy_tdx/config.json`` 里存着已测速的节点列表（52 个 known_hosts、
mac_hosts、mac_ex_hosts、best_host…）。Kuantix 可以读它来复用测速成果，
但**绝不写回**：

- 禁止 ``TdxClient.from_best_host()`` / ``MacClient.from_best_host()`` ——
  它们内部会调 ``save_best_host()``，直接改写用户的 ``config.json``；
- 禁止 import ``easy_tdx.config`` 的任何 ``save_*`` 函数；
- 本模块只用 :func:`json.load` 读文件，不持有可写句柄。

为了让"只读"可被验证，本模块提供 :func:`fingerprint` /
:func:`assert_untouched`，测试与 CI 可在操作前后比对文件指纹。

自给自足（部署解耦）
--------------------
容器 / 干净主机上往往没有 ``~/.easy_tdx/``（那是另一个项目 easy-tdx 的目录）。
为此本项目**内置一份兜底节点清单** ``Kuantix/resources/easy_tdx_config.json``，
经 ``pyproject.toml`` 的 ``force-include`` 打进 wheel 与镜像。

当 :data:`EASY_TDX_CONFIG_PATH` 不存在时（本机未用过 easy-tdx，或云容器里根本没有
这个目录），本模块**自动回退到内置兜底资源**，不再 fail-loud 中断启动 —— 这样
Kuantix 的部署就不再依赖任何外部项目。

覆盖优先级（高 -> 低）：

1. 环境变量 ``Kuantix__TDX__KNOWN_HOSTS_PATH``：运维可显式指定任何外部文件
   （例如挂载的 CFS 上的最新测速结果），优先级最高；
2. 用户主目录 ``~/.easy_tdx/config.json``：本机自己测速过的节点（若有）；
3. 项目内置 ``Kuantix/resources/easy_tdx_config.json``：永远存在，作为最终兜底。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from Kuantix.core.fail_loud import MissingConfigError, require_non_empty

__all__ = [
    "EASY_TDX_CONFIG_PATH",
    "BUILTIN_KNOWN_HOSTS_PATH",
    "FileFingerprint",
    "HostBook",
    "fingerprint",
    "assert_untouched",
    "read_easy_tdx_config",
    "build_host_book",
]

logger = logging.getLogger(__name__)

#: 上游配置文件路径（只读）。本机用过 easy-tdx 才会有；容器/干净主机上通常不存在。
EASY_TDX_CONFIG_PATH = Path.home() / ".easy_tdx" / "config.json"

#: 项目内置兜底节点清单（随 wheel / 镜像打包，永远存在）。
#: 与 Kuantix/config.py 加载 config.default.toml 的约定一致：
#: resources 不是 Python 包，直接按 __file__ 相对路径定位。
def _resolve_builtin_path() -> Path:
    """定位内置兜底节点清单（兼容源码直跑与 wheel 安装后）。

    本文件位于 ``Kuantix/adapters/``，内置资源位于 ``Kuantix/resources/``，
    故取 ``parent.parent / resources``。
    """
    return Path(__file__).resolve().parent.parent / "resources" / "easy_tdx_config.json"


BUILTIN_KNOWN_HOSTS_PATH = _resolve_builtin_path()

#: 环境变量名：运维可显式指定外部节点清单，覆盖内置兜底。
KNOWN_HOSTS_PATH_ENV = "Kuantix__TDX__KNOWN_HOSTS_PATH"


def resolve_upstream_path() -> tuple[Path, str]:
    """按优先级解析实际使用的上游节点清单路径。

    Returns:
        ``(path, source)``：``source`` 取值 ``env`` / ``user`` / ``builtin``，
        分别表示来自环境变量、用户主目录、项目内置兜底。
    """
    env_path = os.environ.get(KNOWN_HOSTS_PATH_ENV, "").strip()
    if env_path:
        return Path(env_path).expanduser(), "env"
    if EASY_TDX_CONFIG_PATH.expanduser().is_file():
        return EASY_TDX_CONFIG_PATH, "user"
    return BUILTIN_KNOWN_HOSTS_PATH, "builtin"


@dataclass(frozen=True)
class FileFingerprint:
    """文件指纹，用于证明"我们没动过它"。

    Attributes:
        path: 文件路径。
        exists: 文件是否存在。
        size: 字节数（不存在时为 ``-1``）。
        mtime_ns: 修改时间纳秒（不存在时为 ``-1``）。
        sha256: 内容摘要（不存在时为空串）。
    """

    path: Path
    exists: bool
    size: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "path": str(self.path),
            "exists": self.exists,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


def fingerprint(path: Path | str = EASY_TDX_CONFIG_PATH) -> FileFingerprint:
    """计算文件指纹（大小 + mtime + sha256）。

    Args:
        path: 目标文件。

    Returns:
        :class:`FileFingerprint`；文件不存在时 ``exists=False``。
    """
    target = Path(path).expanduser()
    if not target.is_file():
        return FileFingerprint(path=target, exists=False, size=-1, mtime_ns=-1, sha256="")
    stat = target.stat()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return FileFingerprint(
        path=target,
        exists=True,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=digest,
    )


def assert_untouched(before: FileFingerprint) -> None:
    """断言文件自 ``before`` 之后未被修改（NF-20 的可验证保证）。

    Args:
        before: 操作前的指纹。

    Raises:
        AssertionError: 文件内容或存在性发生了变化。
    """
    after = fingerprint(before.path)
    if before.exists != after.exists or before.sha256 != after.sha256:
        raise AssertionError(
            f"[NF-20 违规] 上游只读文件被修改: {before.path}\n"
            f"  before: exists={before.exists} sha256={before.sha256[:16]} size={before.size}\n"
            f"  after : exists={after.exists} sha256={after.sha256[:16]} size={after.size}"
        )


def read_easy_tdx_config(path: Path | str = EASY_TDX_CONFIG_PATH) -> dict[str, Any]:
    """**只读**加载上游 ``~/.easy_tdx/config.json``（或内置兜底清单）。

    Args:
        path: 配置文件路径。

    Returns:
        解析后的字典。

    Raises:
        MissingConfigError: 文件不是合法 JSON 对象。
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise MissingConfigError(
            f"[fail-loud/NF-26] 上游节点配置不存在: {target}。"
            f"请检查路径，或在 config.toml 中设置 "
            f"[tdx].use_easy_tdx_known_hosts = false 并显式填写节点列表"
        )
    with target.open("r", encoding="utf-8") as fh:  # 只读句柄，绝不 "w"/"a"/"r+"
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise MissingConfigError(
            f"[fail-loud/NF-26] 上游节点配置 {target} 的根节点必须是 JSON 对象，"
            f"实际 {type(payload).__name__}"
        )
    return payload


def _string_list(payload: Mapping[str, Any], key: str) -> list[str]:
    """从上游配置中取字符串列表；键缺失或类型不符时返回空列表。

    这里返回空列表**不是**业务兜底：上游 config.json 的字段是可选的
    （用户可能只测过 mac_hosts 没测过 ex_hosts），缺失属于正常情况。
    真正的节点来源是 Kuantix 自己的 ``config.toml``，本函数只做"锦上添花"。
    """
    if key not in payload:
        return []
    value = payload[key]
    if not isinstance(value, (list, tuple)):
        logger.warning("上游节点配置的 %s 字段不是数组（实际 %s），已忽略", key, type(value).__name__)
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _merge_unique(*groups: Iterable[str]) -> list[str]:
    """按出现顺序合并多组主机名并去重。"""
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for host in group:
            if host and host not in seen:
                seen.add(host)
                merged.append(host)
    return merged


@dataclass(frozen=True)
class HostBook:
    """可用节点清单（config.toml 为主，上游 known_hosts 为辅）。

    Attributes:
        std_hosts: 标准协议节点（``TdxClient``，用于证券列表枚举）。
        mac_hosts: MAC 协议节点（``MacClient``，A 股 K 线/报价）。
        mac_ex_hosts: MAC 扩展协议节点（``MacExClient``，港美股）。
        port: A 股端口（标准协议与 MAC 协议共用）。
        ex_port: 港美股扩展协议端口。
        timeout_seconds: 连接超时。
        upstream_used: 是否实际合入了上游 known_hosts。
        upstream_source: 上游清单来源，``env`` / ``user`` / ``builtin``；
            未合入时为 ``None``。
        upstream_fingerprint: 读取时刻的上游文件指纹（未读取时为 ``None``）。
    """

    std_hosts: list[str]
    mac_hosts: list[str]
    mac_ex_hosts: list[str]
    port: int
    ex_port: int
    timeout_seconds: float
    upstream_used: bool = False
    upstream_source: str | None = None
    upstream_fingerprint: FileFingerprint | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        require_non_empty(self.std_hosts, "HostBook.std_hosts")
        require_non_empty(self.mac_hosts, "HostBook.mac_hosts")
        require_non_empty(self.mac_ex_hosts, "HostBook.mac_ex_hosts")
        if self.port <= 0 or self.ex_port <= 0:
            raise MissingConfigError(
                f"[fail-loud/NF-26] 端口必须为正整数，实际 port={self.port} ex_port={self.ex_port}"
            )

    @property
    def primary_std_host(self) -> str:
        """首选标准协议节点。"""
        return self.std_hosts[0]

    @property
    def primary_mac_host(self) -> str:
        """首选 MAC 协议节点。"""
        return self.mac_hosts[0]

    @property
    def primary_mac_ex_host(self) -> str:
        """首选 MAC 扩展协议节点。"""
        return self.mac_ex_hosts[0]

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全字典。"""
        return {
            "std_hosts": list(self.std_hosts),
            "mac_hosts": list(self.mac_hosts),
            "mac_ex_hosts": list(self.mac_ex_hosts),
            "port": self.port,
            "ex_port": self.ex_port,
            "timeout_seconds": self.timeout_seconds,
            "upstream_used": self.upstream_used,
            "upstream_source": self.upstream_source,
            "upstream_fingerprint": (
                self.upstream_fingerprint.to_dict() if self.upstream_fingerprint else None
            ),
        }


def build_host_book(
    *,
    std_hosts: Sequence[str],
    mac_hosts: Sequence[str],
    mac_ex_hosts: Sequence[str],
    port: int,
    ex_port: int,
    timeout_seconds: float,
    use_easy_tdx_known_hosts: bool,
    upstream_path: Path | str | None = None,
) -> HostBook:
    """构建节点清单：``config.toml`` 显式配置在前，上游 known_hosts 追加在后。

    Args:
        std_hosts: config.toml 中的标准协议节点。
        mac_hosts: config.toml 中的 MAC 协议节点。
        mac_ex_hosts: config.toml 中的扩展协议节点。
        port: A 股端口。
        ex_port: 港美股端口。
        timeout_seconds: 连接超时。
        use_easy_tdx_known_hosts: 是否合入上游 known_hosts（只读）。
        upstream_path: 上游配置路径（默认走三级回退解析，见
            :func:`resolve_upstream_path`）。

    Returns:
        :class:`HostBook`。

    Raises:
        MissingConfigError: 最终任一节点列表为空。
    """
    if not use_easy_tdx_known_hosts:
        return HostBook(
            std_hosts=list(std_hosts),
            mac_hosts=list(mac_hosts),
            mac_ex_hosts=list(mac_ex_hosts),
            port=port,
            ex_port=ex_port,
            timeout_seconds=timeout_seconds,
            upstream_used=False,
            upstream_fingerprint=None,
        )

    # 三级回退：环境变量 > 用户主目录 ~/.easy_tdx/config.json > 项目内置兜底。
    # 目的是让部署不再依赖外部项目 easy-tdx 的目录存在。
    if upstream_path is not None:
        # 调用方显式指定了路径（如运维挂载的外部文件、或 settings 已解析的来源）
        resolved, source = Path(upstream_path).expanduser(), "explicit"
    else:
        resolved, source = resolve_upstream_path()
    if source == "builtin":
        logger.info(
            "未检测到用户 ~/.easy_tdx/config.json，回退到项目内置兜底节点清单: %s",
            resolved,
        )
    else:
        logger.info("使用上游节点清单（来源=%s）: %s", source, resolved)

    before = fingerprint(resolved)
    payload = read_easy_tdx_config(resolved)  # 内置兜底永远存在；用户/环境变量指定缺失才报错

    upstream_std = _merge_unique(
        [str(payload["best_host"]).strip()] if "best_host" in payload else [],
        _string_list(payload, "known_hosts"),
    )
    upstream_mac = _merge_unique(
        [str(payload["best_mac_host"]).strip()] if "best_mac_host" in payload else [],
        _string_list(payload, "mac_hosts"),
    )
    upstream_mac_ex = _merge_unique(
        [str(payload["best_mac_ex_host"]).strip()] if "best_mac_ex_host" in payload else [],
        _string_list(payload, "mac_ex_hosts"),
    )

    book = HostBook(
        std_hosts=_merge_unique(std_hosts, upstream_std),
        mac_hosts=_merge_unique(mac_hosts, upstream_mac),
        mac_ex_hosts=_merge_unique(mac_ex_hosts, upstream_mac_ex),
        port=port,
        ex_port=ex_port,
        timeout_seconds=timeout_seconds,
        upstream_used=True,
        upstream_source=source,
        upstream_fingerprint=before,
    )
    # 立刻自证：读取过程没有产生任何写入（NF-20）
    assert_untouched(before)
    logger.info(
        "已只读合入上游 known_hosts: std=%d mac=%d mac_ex=%d（源文件未修改）",
        len(book.std_hosts),
        len(book.mac_hosts),
        len(book.mac_ex_hosts),
    )
    return book
