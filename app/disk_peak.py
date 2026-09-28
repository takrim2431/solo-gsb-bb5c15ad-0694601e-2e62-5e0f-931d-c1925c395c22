"""磁盘暂存峰值预演：在 overlay 干跑规则之上按序模拟写盘的磁盘占用变化（纯内存，不访问文件系统）。

逐条判定完全沿用 :mod:`app.overlay` 的规则（CREATE / OVERWRITE /
REUSE_DIRECTORY / BLOCKED，成功条目立即进入虚拟目录树），仅**成功写入的
文件**计空间，目录（CREATE / REUSE_DIRECTORY）与被阻挡条目（BLOCKED）均不
占空间。单个文件的写盘过程分两步记账：

1. **备份（backup）**：首次覆盖**解包前现存文件**前，在同一磁盘备份该文件
   的原始大小一次（同键只备份一次；先新建后覆盖、重复覆盖均不再备份）。
2. **暂存（staging）**：先暂存新文件的完整大小，再替换并**释放被覆盖旧文件
   的占用**。新建文件没有旧占用可释放。

记单条处理前磁盘相对初始状态的新增占用为 ``S``、新文件大小为 ``N``、旧文件
当前大小为 ``O``：

* 新建文件：备份 0 → 暂存 ``S + N`` → 提交后 ``S + N``（净增 ``N``）；
* 首次覆盖现存文件：备份后 ``S + O`` → 暂存后 ``S + O + N`` → 释放后
  ``S + N``（净增 ``N``，备份与旧文件占用相抵）；
* 覆盖批次内文件（先前新建或已覆盖过）：备份 0 → 暂存 ``S + N`` →
  释放后 ``S + (N - O)``（净增可正可负）。

整批峰值取所有条目的备份/暂存阶段水位的全局最大值（等于额度不算超出）；
一旦某阶段水位**严格大于**初始可用字节数，记录首次超额的阶段（备份/暂存）
与条目下标，但预演继续直至全部条目处理完毕。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .normalize import NormalizedEntry
from .overlay import (
    FILE_TYPE,
    OverlayDecision,
    OverlayStatus,
    TreeNode,
    apply_entry,
)


class DiskPeakPhase(str, Enum):
    BACKUP = "BACKUP"  # 覆盖前备份现存文件
    STAGING = "STAGING"  # 暂存新文件完整大小


@dataclass(frozen=True)
class EntrySpace:
    """单条条目的磁盘记账结果（目录与被阻挡条目各项恒为 0）。"""

    staging_peak_bytes: int  # 本条目造成的瞬时新增峰值（备份 + 新文件暂存大小）
    committed_delta_bytes: int  # 本条目提交后留下的净新增占用（覆盖缩容时可为负）
    backup_bytes: int  # 本条目前实际发生的备份字节（仅首次覆盖现存文件时非 0）
    released_bytes: int  # 替换时释放的旧文件字节（仅覆盖时非 0）


@dataclass(frozen=True)
class FirstExceedance:
    """磁盘新增占用首次严格超过初始可用字节数的阶段。"""

    index: int  # incoming 条目下标（从 1 开始）
    phase: DiskPeakPhase  # BACKUP（备份）或 STAGING（暂存）
    level_bytes: int  # 超额发生时磁盘相对初始状态的新增占用字节


@dataclass(frozen=True)
class DiskPeakResult:
    """整批磁盘峰值预演结果。"""

    decisions: list[tuple[NormalizedEntry, OverlayDecision]]
    spaces: list[EntrySpace]
    batch_peak_bytes: int  # 全程备份/暂存阶段的磁盘新增占用高水位
    first_exceedance: FirstExceedance | None  # 首次超额阶段；从未超额为 None


def simulate_disk_peak(
    tree: dict[str, TreeNode],
    sizes: dict[str, int],
    incoming: list[tuple[NormalizedEntry, int]],
    initial_available_bytes: int,
) -> DiskPeakResult:
    """在（现存清单构建的）虚拟目录树上模拟按序写盘的磁盘占用。

    ``tree`` 为现存清单建树结果；``sizes`` 为现存文件比较键 -> 当前大小
    （目录与隐含目录不在其中）。``incoming`` 为 ``(规范化条目, 声明大小)``
    列表，文件条目的声明大小由调用方保证非负，目录条目传 0。
    ``tree`` 与 ``sizes`` 会被原地更新为模拟结束后的状态。
    """
    existing_keys = set(tree)

    decisions: list[tuple[NormalizedEntry, OverlayDecision]] = []
    spaces: list[EntrySpace] = []
    backed_up: set[str] = set()

    used = 0  # 已提交部分相对初始状态的新增占用（提交后水位）
    batch_peak = 0
    first_exceedance: FirstExceedance | None = None

    def _check(index: int, phase: DiskPeakPhase, level: int) -> None:
        # 等于额度不算超出；只记录全批首次超额，预演不中断。
        nonlocal first_exceedance
        if first_exceedance is None and level > initial_available_bytes:
            first_exceedance = FirstExceedance(
                index=index, phase=phase, level_bytes=level
            )

    for entry, size in incoming:
        key = entry.comparison_key
        decision = apply_entry(tree, entry)
        decisions.append((entry, decision))

        backup_bytes = 0
        released_bytes = 0

        if decision.status == OverlayStatus.OVERWRITE:
            # OVERWRITE 成功时 apply_entry 会原地改写节点身份；旧大小以
            # sizes 中的覆盖前值为准，在更新前立即取定。
            old_size = sizes[key]
            # 仅备份解包前现存文件的原始大小，且同键只备份一次；
            # 覆盖批次内新建文件不备份（回退只需删除新建文件）。
            if key in existing_keys and key not in backed_up:
                backup_bytes = old_size
                backed_up.add(key)
            released_bytes = old_size
            sizes[key] = size
        elif decision.status == OverlayStatus.CREATE and entry.entry_type == FILE_TYPE:
            sizes[key] = size
        # CREATE 目录 / REUSE_DIRECTORY / BLOCKED：不写文件、不占空间。

        writes_file = decision.status in (
            OverlayStatus.CREATE,
            OverlayStatus.OVERWRITE,
        ) and entry.entry_type == FILE_TYPE

        if backup_bytes:
            used += backup_bytes
            batch_peak = max(batch_peak, used)
            _check(entry.index, DiskPeakPhase.BACKUP, used)
        if writes_file:
            used += size  # 先完整暂存新文件
            batch_peak = max(batch_peak, used)
            _check(entry.index, DiskPeakPhase.STAGING, used)
            used -= released_bytes  # 替换并释放旧文件占用
        # 本条目提交后留下的净新增占用：备份留存 + 暂存新文件 - 释放旧文件；
        # 覆盖缩容（重复覆盖为更小文件）时可为负，目录与被阻挡条目为 0。
        committed_delta = (
            backup_bytes + (size if writes_file else 0) - released_bytes
        )
        # 无备份/无写文件的条目（目录、阻挡）水位不变。

        spaces.append(
            EntrySpace(
                staging_peak_bytes=(backup_bytes + size) if writes_file else 0,
                committed_delta_bytes=committed_delta,
                backup_bytes=backup_bytes,
                released_bytes=released_bytes,
            )
        )

    return DiskPeakResult(
        decisions=decisions,
        spaces=spaces,
        batch_peak_bytes=batch_peak,
        first_exceedance=first_exceedance,
    )
