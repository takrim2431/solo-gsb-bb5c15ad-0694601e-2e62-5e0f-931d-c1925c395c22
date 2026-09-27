"""前缀解包回退预演：在 overlay 干跑规则之上为前 N 条解包生成执行前备份清单与有序回退步骤（纯内存，不访问文件系统）。

只模拟解包清单的前 ``stop_after`` 条（前缀），逐条判定完全沿用
:mod:`app.overlay` 的规则（CREATE / OVERWRITE / REUSE_DIRECTORY /
BLOCKED，成功条目立即进入虚拟目录树）。前缀模拟开始时对虚拟树做一份
**解包前快照**，回退只撤销前缀相对该快照造成的净变化：

* 被覆盖的**现存文件**：执行前需备份（同键只备份一次，取首次覆盖）；
  回退时从备份恢复一次。重复覆盖同一路径仍以**解包前状态**为恢复目标。
* 前缀**新建的文件**：回退时删除一次（同键一次），不涉及备份。
* 前缀**新建的目录**（显式 CREATE 的目录与由新条目推导出的隐含父目录，
  快照中不存在者）：回退时在子项处理完后按深度由深到浅删除。
* 复用目录（REUSE_DIRECTORY，无论复用现存/隐含目录）与被阻挡条目
  （BLOCKED）不产生任何回退步骤。

回退步骤按三个阶段顺序排定：

1. ``DELETE_CREATED_FILE``：新建文件删除，按首次新建的 incoming 下标逆序；
2. ``RESTORE_FILE``：被覆盖现存文件从备份恢复，按首次覆盖的 incoming
   下标逆序；
3. ``REMOVE_DIRECTORY``：新建目录（含隐含父目录）删除，深 → 浅、同深度按
   Windows 比较键升序，保证先删子项后删父目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .normalize import NormalizedEntry
from .overlay import (
    DIR_TYPE,
    FILE_TYPE,
    OverlayDecision,
    OverlayNodeSource,
    TreeNode,
    apply_entry,
)


class RollbackOperation(str, Enum):
    DELETE_CREATED_FILE = "DELETE_CREATED_FILE"
    RESTORE_FILE = "RESTORE_FILE"
    REMOVE_DIRECTORY = "REMOVE_DIRECTORY"


@dataclass(frozen=True)
class BackupTarget:
    """执行前需备份的一个现存文件（同键只列一次）。"""

    existing_index: int  # 现存清单中的下标（从 1 开始）
    canonical_path: str  # 解包前现存文件的规范路径
    comparison_key: str
    first_overwrite_index: int  # 首次覆盖该文件的 incoming 下标（从 1 开始）


@dataclass(frozen=True)
class RollbackStep:
    """单步有序回退操作。"""

    order: int  # 从 1 开始的执行顺序
    operation: RollbackOperation
    node_type: str  # "file" | "directory"
    source: OverlayNodeSource
    index: int | None  # incoming/existing 下标（从 1 开始）；隐含目录为 null
    canonical_path: str
    comparison_key: str


@dataclass(frozen=True)
class CreatedNode:
    """前缀新建节点的首次落盘身份（用于回退步骤引用）。"""

    source: OverlayNodeSource  # INCOMING（显式新建）或 IMPLICIT（隐含父目录）
    index: int | None
    canonical_path: str


def _depth(key: str) -> int:
    return key.count("/") + 1


def plan_prefix_rollback(
    tree: dict[str, TreeNode],
    incoming: list[NormalizedEntry],
) -> tuple[
    list[tuple[NormalizedEntry, OverlayDecision]],
    list[BackupTarget],
    list[RollbackStep],
]:
    """在（现存清单构建的）虚拟目录树上模拟解包前缀并生成备份清单与回退步骤。

    ``incoming`` 为调用方按 ``stop_after`` 截取后的前缀条目（下标已与完整
    清单对齐，从 1 开始）。返回 ``(逐条判定, 备份清单, 有序回退步骤)``；
    判定列表保持前缀输入顺序，备份清单按首次覆盖下标升序，回退步骤按三阶段
    顺序排列。``tree`` 会被原地改写为前缀模拟结束后的状态。
    """
    # 解包前快照：仅记录键集合，回退只撤销相对该快照的净变化。
    existing_keys = set(tree)

    decisions: list[tuple[NormalizedEntry, OverlayDecision]] = []
    created_files: dict[str, CreatedNode] = {}
    created_dirs: dict[str, CreatedNode] = {}
    # comparison_key -> (首次覆盖 incoming 下标, 现存清单下标, 解包前规范路径)。
    # apply_entry 覆盖时会原地改写 TreeNode 身份，故只能在调用前立即取标量留存。
    first_overwrite: dict[str, tuple[int, int, str]] = {}

    for entry in incoming:
        key = entry.comparison_key
        # OVERWRITE 成功时 apply_entry 会把节点身份原地改写为最新 incoming，
        # 故须在调用前把解包前身份取成标量留存（仅引用节点本身不够）。
        previous = tree.get(key)
        previous_index = previous.index if previous is not None else None
        previous_path = (
            previous.canonical_path if previous is not None else None
        )
        decision = apply_entry(tree, entry)
        decisions.append((entry, decision))

        if decision.status.value == "CREATE":
            node = CreatedNode(
                OverlayNodeSource.INCOMING, entry.index, entry.canonical_path
            )
            if entry.entry_type == FILE_TYPE:
                created_files.setdefault(key, node)
            else:
                created_dirs.setdefault(key, node)
            # 成功 CREATE 时 apply_entry 已补齐隐含父目录；登记快照中不存在、
            # 且此前未登记过的新隐含目录（setdefault 保留首次落盘身份）。
            for depth in range(1, len(entry.key_parts)):
                ancestor_key = "/".join(entry.key_parts[:depth])
                if ancestor_key in existing_keys:
                    continue
                created_dirs.setdefault(
                    ancestor_key,
                    CreatedNode(
                        OverlayNodeSource.IMPLICIT,
                        None,
                        "/".join(entry.canonical_parts[:depth]),
                    ),
                )
        elif decision.status.value == "OVERWRITE":
            # 仅覆盖**快照中存在的同键文件**才需要备份；覆盖前缀自身新建的
            # 文件属于“新建文件删除一次”，不产生备份/恢复。重复覆盖只计首次。
            if key in existing_keys and key not in first_overwrite:
                first_overwrite[key] = (
                    entry.index,
                    previous_index,
                    previous_path,
                )

    # 备份清单：按首次覆盖的 incoming 下标升序（解包执行前按此顺序备份）。
    backups = [
        BackupTarget(
            existing_index=existing_index,
            canonical_path=existing_canonical_path,
            comparison_key=key,
            first_overwrite_index=incoming_index,
        )
        for key, (incoming_index, existing_index, existing_canonical_path) in sorted(
            first_overwrite.items(), key=lambda item: item[1][0]
        )
    ]

    planned: list[RollbackStep] = []

    # 阶段 1：新建文件删除，按首次新建下标逆序。
    for key, node in sorted(
        created_files.items(), key=lambda item: item[1].index, reverse=True
    ):
        planned.append(
            RollbackStep(
                order=0,
                operation=RollbackOperation.DELETE_CREATED_FILE,
                node_type=FILE_TYPE,
                source=node.source,
                index=node.index,
                canonical_path=node.canonical_path,
                comparison_key=key,
            )
        )

    # 阶段 2：被覆盖现存文件从备份恢复，按首次覆盖下标逆序。
    for key, (incoming_index, existing_index, existing_canonical_path) in sorted(
        first_overwrite.items(), key=lambda item: item[1][0], reverse=True
    ):
        planned.append(
            RollbackStep(
                order=0,
                operation=RollbackOperation.RESTORE_FILE,
                node_type=FILE_TYPE,
                source=OverlayNodeSource.EXISTING,
                index=existing_index,
                canonical_path=existing_canonical_path,
                comparison_key=key,
            )
        )

    # 阶段 3：新建目录（含隐含父目录）深 → 浅删除，同深度按比较键升序。
    for key, node in sorted(
        created_dirs.items(), key=lambda item: (-_depth(item[0]), item[0])
    ):
        planned.append(
            RollbackStep(
                order=0,
                operation=RollbackOperation.REMOVE_DIRECTORY,
                node_type=DIR_TYPE,
                source=node.source,
                index=node.index,
                canonical_path=node.canonical_path,
                comparison_key=key,
            )
        )

    steps = [
        RollbackStep(
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
    return decisions, backups, steps
