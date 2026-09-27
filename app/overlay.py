"""目标目录覆盖预演：现存清单校验与按序解包干跑（纯内存，不访问文件系统）。

语义概述：

* 现存清单（existing）先整体校验：不安全路径、同键（Windows 比较键）重复、
  文件占据其他条目的祖先路径，任一命中即拒绝（API 层映射为 422）。
* 校验通过后把现存清单插入**虚拟目录树**；未显式列出的父目录按
  **隐含目录**处理（目标目录里它们必然存在，只是未被列出）。
* 解包清单（incoming）按输入顺序逐条判定：
  - 路径不安全 → ``BLOCKED``（仅标记，不进入虚拟目录树）；
  - 任一祖先路径被虚拟树中的**文件**占据 → ``BLOCKED``（文件祖先阻挡）；
  - 同键已有节点：类型相同则文件 ``OVERWRITE`` / 目录 ``REUSE_DIRECTORY``，
    类型不同 → ``BLOCKED``（类型冲突）；
  - 同键不存在 → ``CREATE``。

  成功条目（``CREATE`` / ``OVERWRITE`` / ``REUSE_DIRECTORY``）立即写入
  虚拟目录树，影响后续条目的判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .normalize import IssueSeverity, NormalizedEntry

FILE_TYPE = "file"
DIR_TYPE = "directory"


class OverlayStatus(str, Enum):
    CREATE = "CREATE"
    OVERWRITE = "OVERWRITE"
    REUSE_DIRECTORY = "REUSE_DIRECTORY"
    BLOCKED = "BLOCKED"


class OverlayBlockReason(str, Enum):
    UNSAFE_PATH = "UNSAFE_PATH"  # 新条目自身路径不安全
    TYPE_CONFLICT = "TYPE_CONFLICT"  # 同键节点类型冲突（文件 vs 目录）
    FILE_ANCESTOR = "FILE_ANCESTOR"  # 祖先路径被文件占据


class OverlayNodeSource(str, Enum):
    EXISTING = "existing"  # 现存清单显式条目
    INCOMING = "incoming"  # 解包清单条目
    IMPLICIT = "implicit"  # 未显式列出、由父路径推导出的隐含目录


@dataclass
class TreeNode:
    """虚拟目录树节点（按 Windows 比较键定位，全树文件必为叶子）。"""

    kind: str  # "file" | "directory"
    source: OverlayNodeSource
    index: int | None  # 来源清单中的下标（从 1 开始）；隐含目录为 None
    canonical_path: str  # 展示用规范路径（正斜杠分隔）


@dataclass(frozen=True)
class OverlayDecision:
    """单条解包条目的干跑判定结果。"""

    status: OverlayStatus
    reason: OverlayBlockReason | None = None  # 仅 BLOCKED 时有值
    blocker: TreeNode | None = None  # 仅冲突/阻挡类 BLOCKED 时有值


def validate_existing_entries(
    entries: list[NormalizedEntry], label: str = "现存清单"
) -> None:
    """校验清单；发现问题时抛出 ``ValueError``（API 层映射为 422）。

    依次检查：不安全路径 → 同键重复 → 文件占据其他条目的祖先路径。
    调用方保证 ``entries`` 的下标与清单一一对应（从 1 开始）。

    ``label`` 仅用于错误信息中标注清单名称（如现存清单、期望清单）。
    """
    for entry in entries:
        if entry.unsafe:
            codes = ", ".join(
                i.code.value for i in entry.issues if i.severity == IssueSeverity.ERROR
            )
            raise ValueError(
                f"{label}条目 #{entry.index}（{entry.original_path!r}）"
                f"路径不安全：{codes}"
            )

    seen: dict[str, int] = {}
    for entry in entries:
        key = entry.comparison_key
        if key in seen:
            raise ValueError(
                f"{label}条目 #{seen[key]} 与 #{entry.index} 规范化后同键重复"
                f"（{entry.canonical_path}）"
            )
        seen[key] = entry.index

    files = {e.comparison_key: e for e in entries if e.entry_type == FILE_TYPE}
    for entry in entries:
        for depth in range(1, len(entry.key_parts)):
            ancestor = files.get("/".join(entry.key_parts[:depth]))
            if ancestor is not None:
                raise ValueError(
                    f"{label}文件条目 #{ancestor.index}（{ancestor.canonical_path}）"
                    f"占据了条目 #{entry.index}（{entry.canonical_path}）的祖先路径"
                )


def _ensure_implicit_ancestors(
    tree: dict[str, TreeNode], entry: NormalizedEntry
) -> None:
    """为条目补齐缺失的父目录节点（隐含目录，不覆盖已有节点）。"""
    for depth in range(1, len(entry.key_parts)):
        ancestor_key = "/".join(entry.key_parts[:depth])
        tree.setdefault(
            ancestor_key,
            TreeNode(
                DIR_TYPE,
                OverlayNodeSource.IMPLICIT,
                None,
                "/".join(entry.canonical_parts[:depth]),
            ),
        )


def build_manifest_tree(
    entries: list[NormalizedEntry], source: OverlayNodeSource
) -> dict[str, TreeNode]:
    """把（已通过 :func:`validate_existing_entries` 校验的）清单插入虚拟目录树。

    未显式列出的父目录按隐含目录（:attr:`OverlayNodeSource.IMPLICIT`）补齐；
    显式条目的来源为调用方给定的 ``source``，并覆盖由其他条目推导出的同名
    隐含目录。
    """
    tree: dict[str, TreeNode] = {}
    for entry in entries:
        _ensure_implicit_ancestors(tree, entry)
        # 显式条目覆盖可能先由其他条目推导出的同名隐含目录。
        tree[entry.comparison_key] = TreeNode(
            entry.entry_type,
            source,
            entry.index,
            entry.canonical_path,
        )
    return tree


def build_existing_tree(entries: list[NormalizedEntry]) -> dict[str, TreeNode]:
    """把现存清单（已通过 :func:`validate_existing_entries` 校验）插入虚拟目录树。"""
    return build_manifest_tree(entries, OverlayNodeSource.EXISTING)


def apply_entry(tree: dict[str, TreeNode], entry: NormalizedEntry) -> OverlayDecision:
    """对单条解包条目做干跑判定；成功条目立即写入虚拟目录树。

    判定顺序：不安全 → 文件祖先阻挡 → 同键节点（覆盖/复用/类型冲突）→ 新建。
    """
    if entry.unsafe:
        return OverlayDecision(OverlayStatus.BLOCKED, OverlayBlockReason.UNSAFE_PATH)

    for depth in range(1, len(entry.key_parts)):
        node = tree.get("/".join(entry.key_parts[:depth]))
        if node is not None and node.kind == FILE_TYPE:
            return OverlayDecision(
                OverlayStatus.BLOCKED, OverlayBlockReason.FILE_ANCESTOR, node
            )

    key = entry.comparison_key
    node = tree.get(key)
    if node is None:
        _ensure_implicit_ancestors(tree, entry)
        tree[key] = TreeNode(
            entry.entry_type,
            OverlayNodeSource.INCOMING,
            entry.index,
            entry.canonical_path,
        )
        return OverlayDecision(OverlayStatus.CREATE)

    if node.kind == entry.entry_type:
        # 成功条目接管该节点：后续阻挡/冲突的来源指向最新的解包条目。
        node.source = OverlayNodeSource.INCOMING
        node.index = entry.index
        node.canonical_path = entry.canonical_path
        status = (
            OverlayStatus.OVERWRITE
            if entry.entry_type == FILE_TYPE
            else OverlayStatus.REUSE_DIRECTORY
        )
        return OverlayDecision(status)

    return OverlayDecision(
        OverlayStatus.BLOCKED, OverlayBlockReason.TYPE_CONFLICT, node
    )
