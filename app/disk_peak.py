"""磁盘暂存峰值预演：在 overlay 干跑规则之上模拟“同盘备份 → 暂存 → 替换”的空间占用曲线（纯内存，不访问文件系统）。

逐条判定完全沿用 :mod:`app.overlay`（CREATE / OVERWRITE / REUSE_DIRECTORY /
BLOCKED，成功条目立即入虚拟目录树），仅对**成功写入的文件条目**计空间，
相对解包前磁盘占用的增量（``used_delta``）按下列阶段推进：

* **首次覆盖现存文件**：在同一磁盘先备份原始大小一次（占用 +旧大小），
  同键只备份一次；前缀中新建后再覆盖的文件不在现存快照中，**不备份**。
* **每次写入**：先暂存新文件的完整大小（占用 +新大小），暂存点即该条目的
  占用峰值；随后替换原文件并释放旧文件占用（占用 -旧大小）。
* 目录（CREATE / REUSE_DIRECTORY）与被阻挡条目（BLOCKED）完全不占空间。

即使 ``used_delta`` 超过初始可用字节数，预演也照常继续（仅记录首次超出的
阶段与条目下标）；**等于额度不算超出**。
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
    BACKUP = "BACKUP"  # 首次覆盖现存文件前备份原始大小
    STAGING = "STAGING"  # 暂存新文件完整大小


@dataclass(frozen=True)
class DiskFirstExceed:
    """首次超过初始可用字节数的阶段定位。"""

    phase: DiskPeakPhase
    index: int  # incoming 条目下标（从 1 开始）
    canonical_path: str


@dataclass(frozen=True)
class DiskEntryResult:
    """单条解包条目的干跑判定与空间结果。

    ``staging_peak`` / ``committed`` 仅成功写入的文件条目有值（该条目暂存点、
    提交后整批相对解包前的累计净新增占用）；目录与被阻挡条目均为 None。
    """

    decision: OverlayDecision
    staging_peak: int | None
    committed: int | None


@dataclass
class DiskPeakSimulation:
    results: list[DiskEntryResult]
    batch_peak: int  # 整批暂存峰值（含备份占用）
    final_committed: int  # 全部条目提交后的累计净新增占用
    first_exceed: DiskFirstExceed | None  # 首次超过初始可用字节数；从未超出为 None


def simulate_disk_peak(
    tree: dict[str, TreeNode],
    sizes: dict[str, int],
    incoming: list[tuple[NormalizedEntry, int]],
    initial_available_bytes: int,
) -> DiskPeakSimulation:
    """在（现存清单构建的）虚拟目录树上模拟解包全程的空间占用曲线。

    ``sizes`` 为现存文件的 comparison_key -> 当前磁盘大小；``incoming`` 每项
    为 ``(规范化条目, 声明 size_bytes)``（目录与不安全文件的大小虽经 schema
    要求存在，但不会被读取）。``tree`` 与 ``sizes`` 均会被原地更新为模拟结束
    后的状态。
    """
    # 解包前快照键集合：覆盖快照外的同键文件属于“新建后再覆盖”，不备份。
    existing_keys = set(tree)
    backed_up: set[str] = set()

    used_delta = 0
    batch_peak = 0
    first_exceed: DiskFirstExceed | None = None
    results: list[DiskEntryResult] = []

    for entry, new_size in incoming:
        key = entry.comparison_key
        # OVERWRITE 成功时 apply_entry 会原地改写节点身份；大小以 key 取即可，
        # 但旧大小必须在替换 sizes[key] 之前读取。
        decision = apply_entry(tree, entry)
        staging_peak: int | None = None
        committed: int | None = None

        is_file_write = entry.entry_type == FILE_TYPE and decision.status in (
            OverlayStatus.CREATE,
            OverlayStatus.OVERWRITE,
        )
        if is_file_write:
            # 阶段 1：首次覆盖现存文件前，在同一磁盘备份原始大小（同键一次）。
            if (
                decision.status == OverlayStatus.OVERWRITE
                and key in existing_keys
                and key not in backed_up
            ):
                used_delta += sizes[key]
                backed_up.add(key)
                if (
                    first_exceed is None
                    and used_delta > initial_available_bytes
                ):
                    first_exceed = DiskFirstExceed(
                        DiskPeakPhase.BACKUP, entry.index, entry.canonical_path
                    )

            # 阶段 2：暂存新文件完整大小（先占后换）。
            used_delta += new_size
            staging_peak = used_delta
            batch_peak = max(batch_peak, used_delta)
            if first_exceed is None and used_delta > initial_available_bytes:
                first_exceed = DiskFirstExceed(
                    DiskPeakPhase.STAGING, entry.index, entry.canonical_path
                )

            # 阶段 3：替换并释放旧文件占用（新建时旧大小为 0）。
            old_size = sizes.get(key, 0)
            used_delta -= old_size
            sizes[key] = new_size
            committed = used_delta

        results.append(DiskEntryResult(decision, staging_peak, committed))

    return DiskPeakSimulation(
        results=results,
        batch_peak=batch_peak,
        final_committed=used_delta,
        first_exceed=first_exceed,
    )
