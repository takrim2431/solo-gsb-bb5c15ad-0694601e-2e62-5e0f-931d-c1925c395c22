"""目录镜像同步预演：把现存布局同步为期望布局的操作序列（纯内存，不访问文件系统）。

两侧清单均通过 :func:`app.overlay.validate_existing_entries` 校验
（不安全路径、同键重复、文件占据其他条目祖先路径），故可视为两棵
合法的虚拟目录树：文件必为叶子，未显式列出的父目录为隐含目录。

期望布局 = 期望清单（incoming）的全部显式节点 + 其隐含父目录；
同步操作按以下三个阶段生成：

1. **删除（深 → 浅）**：现存树中位于期望布局之外的节点（多余节点），
   以及同键但类型互换（file ↔ directory）的旧节点，按路径深度从深到浅
   删除；同深度按 Windows 比较键排序。
2. **建目录（浅 → 深）**：期望布局中的目录节点，现存树里仍保留同键目录
   的标记保留（KEEP_DIRECTORY），否则标记新建（CREATE_DIRECTORY）；
   按深度从浅到深、同深度按比较键排序。
3. **写文件（按 incoming 顺序）**：同键文件已存在则覆盖
   （OVERWRITE_FILE），否则新建（CREATE_FILE）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .overlay import OverlayNodeSource, TreeNode


class MirrorOperation(str, Enum):
    DELETE = "DELETE"
    KEEP_DIRECTORY = "KEEP_DIRECTORY"
    CREATE_DIRECTORY = "CREATE_DIRECTORY"
    OVERWRITE_FILE = "OVERWRITE_FILE"
    CREATE_FILE = "CREATE_FILE"


@dataclass(frozen=True)
class MirrorStep:
    """单步镜像操作。"""

    order: int  # 从 1 开始的执行顺序
    operation: MirrorOperation
    node_type: str  # "file" | "directory"
    source: OverlayNodeSource
    index: int | None  # 来源清单下标（从 1 开始）；隐含目录为 null
    canonical_path: str
    comparison_key: str


def _depth(key: str) -> int:
    return key.count("/") + 1


def plan_mirror(
    existing_tree: dict[str, TreeNode],
    desired_tree: dict[str, TreeNode],
    incoming_files: list[tuple[int, str, str]],
) -> list[MirrorStep]:
    """生成把现存树同步为期望树的有序操作序列。

    两棵树均以 Windows 比较键为键。``incoming_files`` 为
    ``(下标, 规范路径, 比较键)`` 三元组，按 incoming 输入顺序排列，
    仅包含文件条目。
    """
    planned: list[MirrorStep] = []

    # 1) 删除：现存树中不在期望布局内、或同键类型互换的旧节点，深 → 浅。
    deletions: list[tuple[str, TreeNode]] = []
    for key, old in existing_tree.items():
        new = desired_tree.get(key)
        if new is None or new.kind != old.kind:
            deletions.append((key, old))
    deletions.sort(key=lambda item: (-_depth(item[0]), item[0]))
    for key, node in deletions:
        planned.append(
            MirrorStep(
                order=0,
                operation=MirrorOperation.DELETE,
                node_type=node.kind,
                source=node.source,
                index=node.index,
                canonical_path=node.canonical_path,
                comparison_key=key,
            )
        )

    # 2) 建目录：期望布局中的目录节点，浅 → 深、同深度按比较键排序。
    desired_dirs = [
        (key, node) for key, node in desired_tree.items() if node.kind == "directory"
    ]
    desired_dirs.sort(key=lambda item: (_depth(item[0]), item[0]))
    for key, node in desired_dirs:
        old = existing_tree.get(key)
        if old is not None and old.kind == "directory":
            # 保留现存目录（含隐含目录）：来源沿用现存侧节点。
            operation = MirrorOperation.KEEP_DIRECTORY
            source = old.source
            index = old.index
            canonical_path = old.canonical_path
        else:
            operation = MirrorOperation.CREATE_DIRECTORY
            source = node.source
            index = node.index
            canonical_path = node.canonical_path
        planned.append(
            MirrorStep(
                order=0,
                operation=operation,
                node_type="directory",
                source=source,
                index=index,
                canonical_path=canonical_path,
                comparison_key=key,
            )
        )

    # 3) 写文件：严格按 incoming 输入顺序（隐含目录不可能是文件）。
    for index, canonical_path, key in incoming_files:
        old = existing_tree.get(key)
        operation = (
            MirrorOperation.OVERWRITE_FILE
            if old is not None and old.kind == "file"
            else MirrorOperation.CREATE_FILE
        )
        planned.append(
            MirrorStep(
                order=0,
                operation=operation,
                node_type="file",
                source=OverlayNodeSource.INCOMING,
                index=index,
                canonical_path=canonical_path,
                comparison_key=key,
            )
        )

    return [
        MirrorStep(
            order=order,
            operation=step.operation,
            node_type=step.node_type,
            source=step.source,
            index=step.index,
            canonical_path=step.canonical_path,
            comparison_key=step.comparison_key,
        )
        for order, step in enumerate(planned, start=1)
    ]
