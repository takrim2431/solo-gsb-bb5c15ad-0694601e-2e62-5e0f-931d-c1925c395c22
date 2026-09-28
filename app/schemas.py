"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from .normalize import IssueCode, IssueSeverity, normalize_entry
from .mirror import MirrorOperation
from .audit import AuditStatus, ManifestSide
from .relocation import RelocationConflictType
from .overlay import (
    OverlayBlockReason,
    OverlayNodeSource,
    OverlayStatus,
    validate_existing_entries,
)
from .rollback import RollbackOperation
from .selection import SelectionAction, apply_selection_rules, validate_pattern


class EntryType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


class SelectionRuleIn(BaseModel):
    action: SelectionAction = Field(
        ..., description="命中后的筛选动作：include（实际解包）或 exclude（排除）"
    )
    pattern: str = Field(
        ...,
        description=(
            "按路径段解释的 glob 模式，匹配规范化后的归档路径："
            "* 不跨 /，? 匹配单段内一个字符，** 必须独占一段且可跨多级"
        ),
    )

    @model_validator(mode="after")
    def _validate_pattern(self) -> "SelectionRuleIn":
        validate_pattern(self.pattern)
        return self


class PreflightEntryIn(BaseModel):
    path: str = Field(..., description="待解包条目的原始路径")
    type: EntryType = Field(..., description="条目类型：file、directory 或 symlink")
    link_target: str | None = Field(
        default=None,
        description="符号链接目标（type 为 symlink 时必填；file/directory 不得携带）",
    )
    compressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description="声明的压缩大小（字节，非负；仅 file 条目可携带）",
    )
    uncompressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description="声明的解压大小（字节，非负；仅 file 条目可携带）",
    )

    @model_validator(mode="after")
    def _validate_link_target(self) -> "PreflightEntryIn":
        if self.type is EntryType.SYMLINK:
            if self.link_target is None:
                raise ValueError("type 为 symlink 时必须提供 link_target")
        elif self.link_target is not None:
            raise ValueError("仅 symlink 条目可携带 link_target")
        return self

    @model_validator(mode="after")
    def _validate_declared_sizes(self) -> "PreflightEntryIn":
        if self.type is not EntryType.FILE and (
            self.compressed_bytes is not None
            or self.uncompressed_bytes is not None
        ):
            raise ValueError(
                "仅 file 条目可携带 compressed_bytes/uncompressed_bytes"
            )
        return self


class ResourceLimits(BaseModel):
    """可选的解压资源预算；任一字段为 null 表示不启用该项限制。"""

    max_file_uncompressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description="单文件解压大小上限（字节）；启用时每个 file 条目必须提供 uncompressed_bytes",
    )
    max_total_uncompressed_bytes: int | None = Field(
        default=None,
        ge=0,
        description="整批解压总量上限（字节）；启用时每个 file 条目必须提供 uncompressed_bytes",
    )
    max_compression_ratio: int | None = Field(
        default=None,
        ge=1,
        description=(
            "最大压缩比（解压大小/压缩大小，整数）；启用时每个 file 条目必须同时提供 "
            "compressed_bytes 与 uncompressed_bytes"
        ),
    )


class PreflightRequest(BaseModel):
    entries: list[PreflightEntryIn] = Field(
        ..., min_length=1, description="待预检的条目列表（至少 1 条）"
    )
    repair: bool = Field(
        default=False,
        description="为 true 时生成确定性 repair_plan（同名碰撞与文件祖先冲突修复建议）",
    )
    resource_limits: ResourceLimits | None = Field(
        default=None,
        description="可选的解压资源预算校验；省略时行为与旧版完全一致",
    )
    selection_rules: list[SelectionRuleIn] | None = Field(
        default=None,
        description=(
            "可选的解包筛选规则，按规范化后的归档路径匹配；"
            "按输入顺序依次匹配，最后一条命中规则生效，未命中默认 include；"
            "省略时全部条目参与预检，行为与旧版完全一致"
        ),
    )

    @model_validator(mode="after")
    def _validate_required_sizes(self) -> "PreflightRequest":
        limits = self.resource_limits
        if limits is None:
            return self
        need_uncompressed = (
            limits.max_file_uncompressed_bytes is not None
            or limits.max_total_uncompressed_bytes is not None
        )
        need_ratio = limits.max_compression_ratio is not None
        if not need_uncompressed and not need_ratio:
            return self
        # 被 selection_rules 排除的文件不参与资源预算，无需声明大小。
        normalized = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.entries, start=1)
        ]
        decisions = apply_selection_rules(
            [n.canonical_parts for n in normalized],
            None
            if self.selection_rules is None
            else [(r.action.value, r.pattern) for r in self.selection_rules],
        )
        for index, entry in enumerate(self.entries, start=1):
            if entry.type is not EntryType.FILE or not decisions[index - 1].selected:
                continue
            if need_uncompressed and entry.uncompressed_bytes is None:
                raise ValueError(
                    f"启用单文件/整批解压大小限制时，文件条目 #{index} "
                    "必须提供 uncompressed_bytes"
                )
            if need_ratio and (
                entry.compressed_bytes is None
                or entry.uncompressed_bytes is None
            ):
                raise ValueError(
                    f"启用最大压缩比限制时，文件条目 #{index} 必须同时提供 "
                    "compressed_bytes 与 uncompressed_bytes"
                )
        return self


class IssueOut(BaseModel):
    code: IssueCode
    severity: IssueSeverity
    message: str
    segment: int | None = None
    related_index: int | None = None


class CollisionMember(BaseModel):
    index: int
    canonical_path: str


class CollisionGroup(BaseModel):
    group: int = Field(..., description="碰撞组编号（按首次出现顺序分配）")
    canonical_path: str = Field(..., description="该组对应的规范路径（展示用）")
    members: list[CollisionMember]


class HierarchyConflict(BaseModel):
    file_index: int = Field(..., description="作为祖先路径的文件条目下标")
    file_canonical_path: str
    descendant_index: int = Field(..., description="被阻挡的后代条目下标")
    descendant_canonical_path: str


class PreflightEntryOut(BaseModel):
    index: int = Field(..., description="条目下标（从 1 开始，与输入顺序一致）")
    type: EntryType
    original_path: str
    link_target: str | None = Field(
        default=None, description="符号链接目标（仅 symlink 条目有值）"
    )
    canonical_path: str = Field(..., description="规范化路径（NFC、段尾已裁剪，正斜杠分隔）")
    comparison_key: str | None = Field(
        ..., description="Windows 等价比较键（小写折叠，仅安全条目可用）"
    )
    safe: bool
    collision_group: int | None = Field(
        ..., description="所属同名碰撞组编号；无碰撞或不安全时为 null"
    )
    blocked_by_file_ancestors: list[int] = Field(
        default_factory=list, description="阻挡本条目的文件祖先下标列表"
    )
    blocks_descendants: list[int] = Field(
        default_factory=list, description="被本文件条目阻挡的后代下标列表（仅文件条目）"
    )
    selected: bool = Field(
        default=True,
        description=(
            "selection_rules 筛选结果：true 为实际解包条目；省略 "
            "selection_rules 时恒为 true"
        ),
    )
    matched_rule_index: int | None = Field(
        default=None,
        description=(
            "最后一条命中的 selection_rules 下标（从 0 开始，与请求下标一致）；"
            "未命中任何规则或省略 selection_rules 时为 null"
        ),
    )
    issues: list[IssueOut] = Field(default_factory=list)


class SymlinkChainStep(BaseModel):
    index: int | None = Field(
        ..., description="该步对应的符号链接条目下标；解析终点（非链接路径）为 null"
    )
    path: str = Field(..., description="该步的规范路径（正斜杠分隔）")


class SymlinkLinkOut(BaseModel):
    index: int = Field(..., description="符号链接条目下标（与输入顺序一致）")
    original_path: str
    link_target: str = Field(..., description="原始链接目标")
    resolved_target: str | None = Field(
        ...,
        description="自身目标相对链接所在目录解析后的根内路径；越界为 null",
    )
    final_path: str | None = Field(
        ...,
        description="沿链接链解析到的最终根内路径；目标越界或链存在循环时为 null",
    )
    chain: list[SymlinkChainStep] = Field(
        ...,
        description="解析链：依次为自身、途经的符号链接，以及最终解析终点（若有）",
    )
    risk_codes: list[IssueCode] = Field(
        default_factory=list,
        description=(
            "链接风险代码：LINK_TARGET_ESCAPE（目标或链越出目标根）、"
            "LINK_TARGET_INVALID（目标非法）、LINK_CYCLE（链存在循环）"
        ),
    )
    safe: bool = Field(..., description="无任何 error 级风险时为 true")


class SymlinkCycleOut(BaseModel):
    members: list[int] = Field(
        ..., description="构成循环的符号链接下标（按链顺序，从最小下标开始）"
    )


class SymlinkHijackOut(BaseModel):
    entry_index: int = Field(..., description="被劫持的文件/目录条目下标")
    canonical_path: str
    symlink_ancestors: list[int] = Field(
        ..., description="占据该条目祖先路径的符号链接下标（由近及远按深度升序）"
    )


class SymlinkAnalysisOut(BaseModel):
    links: list[SymlinkLinkOut] = Field(
        ..., description="逐链接解析链与风险代码（按输入顺序）"
    )
    cycles: list[SymlinkCycleOut] = Field(..., description="检测到的链接循环组")
    hijacked_entries: list[SymlinkHijackOut] = Field(
        ..., description="祖先路径被符号链接占据的文件/目录条目"
    )


class Summary(BaseModel):
    total: int
    safe: int
    unsafe: int
    selected: int = Field(
        default=0,
        description=(
            "selection_rules 判定为实际解包（include）的条目数量；"
            "省略 selection_rules 时等于 total"
        ),
    )
    excluded: int = Field(
        default=0,
        description="selection_rules 判定为排除（exclude）的条目数量"
    )
    collisions: int = Field(..., description="同名碰撞组数量")
    collision_entries: int
    hierarchy_conflicts: int
    symlinks: int = Field(..., description="符号链接条目数量")
    symlink_cycles: int = Field(..., description="检测到的链接循环组数")
    symlink_hijacked: int = Field(
        ..., description="被符号链接祖先劫持的文件/目录条目数量"
    )
    declared_compressed_bytes: int = Field(
        default=0, description="批次文件条目声明的压缩字节总数"
    )
    declared_uncompressed_bytes: int = Field(
        default=0, description="批次文件条目声明的解压字节总数"
    )
    resource_limits_exceeded: bool = Field(
        default=False,
        description="任一启用的资源限制（单文件解压大小、整批解压总量、压缩比）被超出时为 true",
    )


class RepairReason(str, Enum):
    COLLISION = "COLLISION"
    BLOCKS_DESCENDANT = "BLOCKS_DESCENDANT"
    UNSAFE = "UNSAFE"
    SYMLINK = "SYMLINK"


class RepairEntry(BaseModel):
    index: int = Field(..., description="条目下标（与输入顺序一致，从 1 开始）")
    original_path: str = Field(..., description="原始输入路径")
    repaired_path: str | None = Field(
        ...,
        description=(
            "修复后路径（正斜杠分隔）。安全且未改名的条目等于 canonical_path；"
            "skipped 条目为 null"
        ),
    )
    reasons: list[RepairReason] = Field(
        default_factory=list,
        description=(
            "修复原因：COLLISION（同名碰撞改让）、"
            "BLOCKS_DESCENDANT（文件阻挡后代而改名）；skipped 条目为 "
            "[UNSAFE]（不安全，含被符号链接祖先劫持）或 [SYMLINK]（符号链接）"
        ),
    )
    skipped: bool = Field(
        ..., description="为 true 表示不参与改名与复检（不安全条目与全部符号链接）"
    )


class RepairPlan(BaseModel):
    entries: list[RepairEntry] = Field(
        ..., description="按输入顺序排列的逐条修复计划"
    )
    renamed_count: int = Field(..., description="实际改名的安全条目数量")
    skipped_count: int = Field(
        ..., description="被跳过的条目数量（不安全条目与全部符号链接）"
    )
    collisions_remaining: int = Field(
        ..., description="按 repaired_path 复检后的同名碰撞组数（恒为 0）"
    )
    hierarchy_conflicts_remaining: int = Field(
        ..., description="按 repaired_path 复检后的文件祖先冲突对数（恒为 0）"
    )
    collisions_resolved: int = Field(
        ..., description="修复消除的同名碰撞组数（等于原 summary.collisions）"
    )
    hierarchy_conflicts_resolved: int = Field(
        ..., description="修复消除的文件祖先冲突对数（等于原 summary.hierarchy_conflicts）"
    )


class PreflightResponse(BaseModel):
    target: str = "windows"
    entries: list[PreflightEntryOut]
    collision_groups: list[CollisionGroup]
    hierarchy_conflicts: list[HierarchyConflict]
    symlink_analysis: SymlinkAnalysisOut
    summary: Summary


class PreflightRepairResponse(PreflightResponse):
    repair_plan: RepairPlan


class OverlayEntryType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


class OverlayEntryIn(BaseModel):
    path: str = Field(..., description="目标目录内的相对路径（原始形式）")
    type: OverlayEntryType = Field(..., description="条目类型：file 或 directory")


class OverlayPreviewRequest(BaseModel):
    """目标目录覆盖预演请求：现存清单 + 按顺序解包清单（纯内存干跑）。"""

    existing: list[OverlayEntryIn] = Field(
        default_factory=list,
        description=(
            "目标目录现存的 file/directory 清单（可为空，表示空目录）；"
            "含不安全路径、同键重复或文件占据其他条目祖先路径时返回 422"
        ),
    )
    incoming: list[OverlayEntryIn] = Field(
        ..., min_length=1, description="按顺序解包的 file/directory 清单（至少 1 条）"
    )

    @model_validator(mode="after")
    def _validate_existing_list(self) -> "OverlayPreviewRequest":
        normalized = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.existing, start=1)
        ]
        validate_existing_entries(normalized)
        return self


class OverlayBlocker(BaseModel):
    source: OverlayNodeSource = Field(
        ...,
        description=(
            "阻挡来源：existing（现存清单条目）、incoming（解包清单条目）"
            "或 implicit（未显式列出的隐含目录）"
        ),
    )
    index: int | None = Field(
        ..., description="阻挡条目在来源清单中的下标（从 1 开始）；隐含目录为 null"
    )
    canonical_path: str = Field(..., description="阻挡节点的规范路径（正斜杠分隔）")


class OverlayEntryOut(BaseModel):
    index: int = Field(..., description="条目下标（从 1 开始，与 incoming 输入顺序一致）")
    type: OverlayEntryType
    original_path: str
    canonical_path: str = Field(..., description="规范化路径（NFC、段尾已裁剪，正斜杠分隔）")
    comparison_key: str | None = Field(
        ..., description="Windows 等价比较键（小写折叠，仅安全条目可用）"
    )
    safe: bool = Field(..., description="路径自身是否安全（与是否被阻挡无关）")
    status: OverlayStatus = Field(
        ...,
        description=(
            "干跑结果：CREATE（新建）、OVERWRITE（覆盖同键文件）、"
            "REUSE_DIRECTORY（复用同键目录）或 BLOCKED（被阻挡）"
        ),
    )
    blocked_reason: OverlayBlockReason | None = Field(
        ...,
        description=(
            "BLOCKED 原因：UNSAFE_PATH（路径不安全）、TYPE_CONFLICT（同键类型冲突）、"
            "FILE_ANCESTOR（祖先路径被文件占据）；非 BLOCKED 为 null"
        ),
    )
    blocked_by: OverlayBlocker | None = Field(
        ..., description="阻挡来源及下标（仅冲突/阻挡类 BLOCKED 有值）"
    )
    issues: list[IssueOut] = Field(default_factory=list)


class OverlaySummary(BaseModel):
    total: int
    create: int
    overwrite: int
    reuse_directory: int
    blocked: int


class OverlayPreviewResponse(BaseModel):
    target: str = "windows"
    entries: list[OverlayEntryOut]
    summary: OverlaySummary


class DiskPeakEntryIn(BaseModel):
    path: str = Field(..., description="目标目录内的相对路径（原始形式）")
    type: OverlayEntryType = Field(..., description="条目类型：file 或 directory")
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="文件大小（字节，非负）；type 为 file 时必须提供，directory 不得携带",
    )

    @model_validator(mode="after")
    def _validate_size_bytes(self) -> "DiskPeakEntryIn":
        if self.type is OverlayEntryType.FILE:
            if self.size_bytes is None:
                raise ValueError("type 为 file 时必须提供非负的 size_bytes")
        elif self.size_bytes is not None:
            raise ValueError("仅 file 条目可携带 size_bytes")
        return self


class DiskPeakPreviewRequest(BaseModel):
    """磁盘暂存峰值预演请求：现存清单 + 按序解包清单 + 初始可用字节数（纯内存干跑）。"""

    existing: list[DiskPeakEntryIn] = Field(
        default_factory=list,
        description=(
            "目标目录现存的 file/directory 清单（可为空，表示空目录）；"
            "文件必须声明非负 size_bytes，目录不带大小；"
            "含不安全路径、同键重复或文件占据其他条目祖先路径时返回 422"
        ),
    )
    incoming: list[DiskPeakEntryIn] = Field(
        ..., min_length=1, description="按顺序解包的 file/directory 清单（至少 1 条）"
    )
    initial_available_bytes: int = Field(
        ..., ge=0, description="解包开始时同一磁盘上的初始可用字节数（非负）"
    )

    @model_validator(mode="after")
    def _validate_existing_list(self) -> "DiskPeakPreviewRequest":
        normalized = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.existing, start=1)
        ]
        validate_existing_entries(normalized)
        return self


class DiskPeakPhase(str, Enum):
    BACKUP = "BACKUP"
    STAGING = "STAGING"


class DiskPeakEntryOut(OverlayEntryOut):
    staging_peak_bytes: int | None = Field(
        ...,
        description=(
            "该条目暂存新文件时整批占用相对解包前的累计峰值（字节）；"
            "仅成功写入的文件（CREATE/OVERWRITE）有值，目录与被阻挡条目为 null"
        ),
    )
    committed_bytes: int | None = Field(
        ...,
        description=(
            "该条目替换提交后整批相对解包前的累计净新增占用（字节）；"
            "仅成功写入的文件有值，目录与被阻挡条目为 null"
        ),
    )


class DiskPeakFirstExceedOut(BaseModel):
    phase: DiskPeakPhase = Field(
        ...,
        description=(
            "首次超过初始可用字节数的阶段：BACKUP（首次覆盖现存文件前备份原始大小）"
            "或 STAGING（暂存新文件完整大小）；等于额度不算超出"
        ),
    )
    index: int = Field(..., description="首次超额时处理的 incoming 条目下标（从 1 开始）")
    canonical_path: str = Field(..., description="该条目的规范路径（正斜杠分隔）")


class DiskPeakSummary(BaseModel):
    total: int
    create: int
    overwrite: int
    reuse_directory: int
    blocked: int
    batch_peak_bytes: int = Field(
        ...,
        description=(
            "整批暂存峰值（字节）：解包全程整批占用相对解包前的最大增量，"
            "含同盘备份占用与暂存新文件占用"
        ),
    )
    committed_bytes: int = Field(
        ..., description="全部条目提交后相对解包前的累计净新增占用（字节）"
    )
    first_exceed: DiskPeakFirstExceedOut | None = Field(
        ...,
        description=(
            "首次超过初始可用字节数的阶段与条目下标（备份或暂存）；"
            "全程未超出（含恰好等于额度）时为 null；即使超额预演也继续完成"
        ),
    )


class DiskPeakPreviewResponse(BaseModel):
    target: str = "windows"
    initial_available_bytes: int = Field(..., description="回显请求的初始可用字节数")
    entries: list[DiskPeakEntryOut]
    summary: DiskPeakSummary


class RollbackPreviewRequest(BaseModel):
    """前缀解包回退预演请求：现存清单 + 按顺序解包清单 + 仅模拟前 stop_after 条。"""

    existing: list[OverlayEntryIn] = Field(
        default_factory=list,
        description=(
            "目标目录现存的 file/directory 清单（可为空，表示空目录）；"
            "沿用 overlay-preview 的现存清单校验，含不安全路径、同键重复或"
            "文件占据其他条目祖先路径时返回 422"
        ),
    )
    incoming: list[OverlayEntryIn] = Field(
        default_factory=list,
        description=(
            "按顺序解包的 file/directory 清单（可为空，此时 stop_after 必须为 0）；"
            "与 overlay-preview 相同，不安全条目不返回 422 而在干跑中记为 BLOCKED"
        ),
    )
    stop_after: int = Field(
        ...,
        ge=0,
        description=(
            "仅模拟解包清单的前 stop_after 条（前缀）；取值范围 0 至 incoming "
            "清单长度（含端点），越界返回 422"
        ),
    )

    @model_validator(mode="after")
    def _validate_request(self) -> "RollbackPreviewRequest":
        if self.stop_after > len(self.incoming):
            raise ValueError(
                f"stop_after（{self.stop_after}）不得超过 incoming 清单长度"
                f"（{len(self.incoming)}）"
            )
        normalized = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.existing, start=1)
        ]
        validate_existing_entries(normalized)
        return self


class RollbackBackupFileOut(BaseModel):
    existing_index: int = Field(
        ..., description="被覆盖现存文件在 existing 清单中的下标（从 1 开始）"
    )
    canonical_path: str = Field(..., description="解包前现存文件的规范路径（正斜杠分隔）")
    comparison_key: str = Field(..., description="Windows 等价比较键（小写折叠）")
    first_overwrite_index: int = Field(
        ..., description="首次覆盖该文件的 incoming 下标（从 1 开始）"
    )


class RollbackStepOut(BaseModel):
    order: int = Field(..., description="回退执行顺序（从 1 开始）")
    operation: RollbackOperation = Field(
        ...,
        description=(
            "回退操作：DELETE_CREATED_FILE（删除前缀新建的文件）、"
            "RESTORE_FILE（被覆盖现存文件从执行前备份恢复）、"
            "REMOVE_DIRECTORY（删除前缀新建的目录，含隐含父目录）"
        ),
    )
    node_type: OverlayEntryType = Field(..., description="节点类型：file 或 directory")
    source: OverlayNodeSource = Field(
        ...,
        description=(
            "回退节点来源：incoming（前缀新建条目）、existing（解包前现存文件）"
            "或 implicit（前缀推导出的隐含父目录）"
        ),
    )
    index: int | None = Field(
        ...,
        description=(
            "相关清单下标（从 1 开始）：RESTORE_FILE 指向 existing，"
            "DELETE_CREATED_FILE / 显式目录的 REMOVE_DIRECTORY 指向 incoming；"
            "隐含父目录删除为 null"
        ),
    )
    canonical_path: str = Field(..., description="规范路径（正斜杠分隔）")
    comparison_key: str = Field(..., description="Windows 等价比较键（小写折叠）")


class RollbackSummary(BaseModel):
    total: int = Field(..., description="实际模拟的前缀条目数（等于 stop_after）")
    create: int
    overwrite: int
    reuse_directory: int
    blocked: int
    backups: int = Field(..., description="执行前需备份的现存文件数（同键只计一次）")
    rollback_steps: int = Field(..., description="有序回退步骤总数")
    delete_created_file: int = Field(..., description="DELETE_CREATED_FILE 步骤数")
    restore_file: int = Field(..., description="RESTORE_FILE 步骤数")
    remove_directory: int = Field(..., description="REMOVE_DIRECTORY 步骤数")


class RollbackPreviewResponse(BaseModel):
    target: str = "windows"
    stop_after: int = Field(..., description="实际模拟的前缀长度（回显请求值）")
    entries: list[OverlayEntryOut] = Field(
        ...,
        description=(
            "前缀（前 stop_after 条）的 overlay 干跑判定，字段与 "
            "overlay-preview 的 entries 相同，严格保持 incoming 输入顺序"
        ),
    )
    backup_files: list[RollbackBackupFileOut] = Field(
        ...,
        description=(
            "执行前需备份的现存文件清单（同键只列一次，按首次覆盖的 incoming "
            "下标升序）"
        ),
    )
    rollback_steps: list[RollbackStepOut] = Field(
        ...,
        description=(
            "可把该前缀造成的变化还原的有序回退步骤：先删除新建文件，再从"
            "备份恢复被覆盖现存文件，最后由深到浅删除新建目录（含隐含父目录）"
        ),
    )
    summary: RollbackSummary


class MirrorPreviewRequest(BaseModel):
    """目录镜像同步预演请求：现存清单 + 期望归档清单（纯内存生成操作序列）。"""

    existing: list[OverlayEntryIn] = Field(
        default_factory=list,
        description=(
            "目标目录现存的 file/directory 清单（可为空，表示空目录）；"
            "沿用 overlay-preview 的现存清单校验，含不安全路径、同键重复或"
            "文件祖先冲突时返回 422"
        ),
    )
    incoming: list[OverlayEntryIn] = Field(
        default_factory=list,
        description=(
            "期望归档的 file/directory 清单（可为空，表示清空目标目录）；"
            "含不安全路径、同键（Windows 比较键）重复或文件占据其他条目"
            "祖先路径时返回 422。未列出的父目录视为需要保留的隐含目录"
        ),
    )

    @model_validator(mode="after")
    def _validate_manifests(self) -> "MirrorPreviewRequest":
        existing = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.existing, start=1)
        ]
        validate_existing_entries(existing, label="现存清单")
        incoming = [
            normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
            for i, e in enumerate(self.incoming, start=1)
        ]
        validate_existing_entries(incoming, label="期望清单")
        return self


class MirrorStepOut(BaseModel):
    order: int = Field(..., description="执行顺序（从 1 开始）")
    operation: MirrorOperation = Field(
        ...,
        description=(
            "操作类型：DELETE（删除期望布局外或类型互换处的旧节点）、"
            "KEEP_DIRECTORY（保留现存目录）、CREATE_DIRECTORY（新建目录）、"
            "OVERWRITE_FILE（覆盖同键文件）、CREATE_FILE（新建文件）"
        ),
    )
    node_type: OverlayEntryType = Field(
        ..., description="节点类型：file 或 directory"
    )
    source: OverlayNodeSource = Field(
        ...,
        description=(
            "操作节点来源：existing（现存清单显式条目）、incoming（期望清单"
            "条目）或 implicit（未显式列出的隐含目录）"
        ),
    )
    index: int | None = Field(
        ...,
        description=(
            "原清单下标（从 1 开始）：DELETE/KEEP_DIRECTORY 指向 existing，"
            "CREATE_*/OVERWRITE_FILE 指向 incoming；隐含目录为 null"
        ),
    )
    canonical_path: str = Field(..., description="规范路径（正斜杠分隔）")
    comparison_key: str = Field(
        ..., description="Windows 等价比较键（小写折叠）"
    )


class MirrorSummary(BaseModel):
    total: int
    delete: int
    keep_directory: int
    create_directory: int
    create_file: int
    overwrite_file: int


class MirrorPreviewResponse(BaseModel):
    target: str = "windows"
    steps: list[MirrorStepOut]
    summary: MirrorSummary


class HealthResponse(BaseModel):
    status: str
    service: str


# ---------------------------------------------------------------------------
# manifest-audit：约定清单 vs 交付清单核对
# ---------------------------------------------------------------------------

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ManifestAuditEntryIn(BaseModel):
    path: str = Field(..., description="清单条目的相对路径（原始形式）")
    type: OverlayEntryType = Field(..., description="条目类型：file 或 directory")
    sha256: str | None = Field(
        default=None,
        description="声明的 SHA-256（64 位十六进制）；type 为 file 时必填，directory 不得携带",
    )

    @model_validator(mode="after")
    def _validate_sha256(self) -> "ManifestAuditEntryIn":
        if self.type is OverlayEntryType.FILE:
            if self.sha256 is None:
                raise ValueError("type 为 file 时必须提供 sha256")
            if not _SHA256_RE.fullmatch(self.sha256):
                raise ValueError("sha256 必须为 64 位十六进制字符串")
        elif self.sha256 is not None:
            raise ValueError("仅 file 条目可携带 sha256")
        return self


class ManifestAuditRequest(BaseModel):
    """约定清单与交付清单核对请求（两侧均可为空，空清单视为零条目）。"""

    agreed: list[ManifestAuditEntryIn] = Field(
        default_factory=list,
        description=(
            "约定（期望）清单的 file/directory 条目；文件必须声明 sha256；"
            "含不安全路径时整个请求返回 422"
        ),
    )
    delivered: list[ManifestAuditEntryIn] = Field(
        default_factory=list,
        description=(
            "交付（实际）清单的 file/directory 条目；文件必须声明 sha256；"
            "含不安全路径时整个请求返回 422"
        ),
    )

    @model_validator(mode="after")
    def _validate_paths(self) -> "ManifestAuditRequest":
        self._validate_side(self.agreed, "约定清单")
        self._validate_side(self.delivered, "交付清单")
        return self

    @staticmethod
    def _validate_side(
        entries: list[ManifestAuditEntryIn], label: str
    ) -> None:
        # sha256 已在条目模型中校验；此处只复用 Windows 规范化并拒绝不安全路径。
        # 单侧同键重复与文件祖先在本接口不拒绝，而由核对逻辑分别归为歧义和层级阻挡。
        for i, entry in enumerate(entries, start=1):
            normalized = normalize_entry(
                index=i, entry_type=entry.type.value, original_path=entry.path
            )
            if normalized.unsafe:
                codes = ", ".join(
                    issue.code.value
                    for issue in normalized.issues
                    if issue.severity == IssueSeverity.ERROR
                )
                raise ValueError(
                    f"{label}条目 #{i}（{entry.path!r}）路径不安全：{codes}"
                )


class ManifestAuditEntryOut(BaseModel):
    side: ManifestSide = Field(
        ..., description="条目所属清单：agreed（约定）或 delivered（交付）"
    )
    index: int = Field(..., description="在原清单中的下标（从 1 开始，与输入顺序一致）")
    type: OverlayEntryType
    original_path: str = Field(..., description="原始输入路径")
    canonical_path: str = Field(..., description="规范化路径（NFC、段尾已裁剪，正斜杠分隔）")
    comparison_key: str = Field(..., description="Windows 等价比较键（小写折叠）")
    sha256: str | None = Field(
        ..., description="声明的 SHA-256（规范化为小写）；directory 为 null"
    )


class ManifestEntryRef(BaseModel):
    side: ManifestSide
    index: int = Field(..., description="在所属清单中的下标（从 1 开始）")
    type: OverlayEntryType
    original_path: str
    canonical_path: str
    comparison_key: str
    sha256: str | None = Field(
        ..., description="声明的 SHA-256（小写）；directory 为 null"
    )


class ManifestPairOut(BaseModel):
    status: AuditStatus = Field(
        ...,
        description=(
            "MISSING（约定有交付无）、EXTRA（交付有约定无）、"
            "TYPE_MISMATCH（同键类型不符）、DIGEST_MISMATCH（同键文件摘要不符）、"
            "MATCHED（一致）"
        ),
    )
    comparison_key: str = Field(..., description="Windows 等价比较键")
    agreed: ManifestEntryRef | None = Field(
        ..., description="约定侧条目；缺失（MISSING）时为 null"
    )
    delivered: ManifestEntryRef | None = Field(
        ..., description="交付侧条目；额外（EXTRA）时为 null"
    )


class ManifestBlockerOut(BaseModel):
    side: ManifestSide = Field(..., description="阻挡文件所属清单")
    index: int = Field(..., description="阻挡文件在所属清单中的下标（从 1 开始）")
    canonical_path: str = Field(..., description="阻挡文件的规范路径")
    comparison_key: str
    sha256: str = Field(..., description="阻挡文件声明的 SHA-256（小写）")


class ManifestHierarchyBlockedOut(BaseModel):
    blocked: ManifestEntryRef = Field(..., description="被阻挡的后代条目")
    blockers: list[ManifestBlockerOut] = Field(
        ...,
        description=(
            "占据后代祖先路径的另一侧文件，由近及远（祖先深度降序）、"
            "同深度按下标排列"
        ),
    )


class ManifestAmbiguousKeyOut(BaseModel):
    comparison_key: str = Field(..., description="重复出现的 Windows 等价比较键")
    agreed: list[ManifestEntryRef] = Field(
        ..., description="约定侧该键的全部成员（按下标顺序）；无则为空列表"
    )
    delivered: list[ManifestEntryRef] = Field(
        ..., description="交付侧该键的全部成员（按下标顺序）；无则为空列表"
    )


class ManifestAuditSummary(BaseModel):
    agreed_total: int = Field(..., description="约定清单条目总数")
    delivered_total: int = Field(..., description="交付清单条目总数")
    missing: int = Field(..., description="缺失（约定有、交付无且未被阻挡）条目数")
    extra: int = Field(..., description="额外（交付有、约定无且未被阻挡）条目数")
    type_mismatches: int = Field(..., description="同键类型不符条目数")
    digest_mismatches: int = Field(..., description="同键文件 SHA-256 不符条目数")
    matched: int = Field(..., description="类型与声明摘要一致的配对数")
    hierarchy_blocked: int = Field(..., description="被另一侧文件祖先阻挡的条目数")
    ambiguous_keys: int = Field(..., description="单侧同键重复的歧义键数量")
    ambiguous_entries: int = Field(..., description="歧义键涉及的双侧条目总数")


class ManifestAuditResponse(BaseModel):
    target: str = "windows"
    agreed_entries: list[ManifestAuditEntryOut] = Field(
        ..., description="约定侧全部条目（保持原下标顺序）"
    )
    delivered_entries: list[ManifestAuditEntryOut] = Field(
        ..., description="交付侧全部条目（保持原下标顺序）"
    )
    missing: list[ManifestPairOut] = Field(..., description="缺失清单（按比较键升序）")
    extra: list[ManifestPairOut] = Field(..., description="额外清单（按比较键升序）")
    type_mismatches: list[ManifestPairOut] = Field(
        ..., description="类型不符清单（按比较键升序）"
    )
    digest_mismatches: list[ManifestPairOut] = Field(
        ..., description="摘要不符清单（按比较键升序）"
    )
    matched: list[ManifestPairOut] = Field(
        ..., description="一致清单（按比较键升序）"
    )
    hierarchy_blocked: list[ManifestHierarchyBlockedOut] = Field(
        ...,
        description=(
            "层级阻挡清单（按被阻挡后代比较键升序，同键时约定侧在前），"
            "每条指明阻挡文件"
        ),
    )
    ambiguous_keys: list[ManifestAmbiguousKeyOut] = Field(
        ..., description="歧义键清单（按比较键升序），整键不任意配对"
    )
    summary: ManifestAuditSummary


# ---------------------------------------------------------------------------
# relocation-preview：路径重定位预演
# ---------------------------------------------------------------------------


class RelocationRuleIn(BaseModel):
    source_prefix: str = Field(
        ..., description="源前缀：按规范化路径段的比较键做前缀匹配，命中部分将被替换"
    )
    target_prefix: str = Field(
        ..., description="目标前缀：替换命中条目的源前缀部分，与未匹配的后缀拼成新路径"
    )


def _relocation_error_codes(normalized) -> str:
    return ", ".join(
        issue.code.value
        for issue in normalized.issues
        if issue.severity == IssueSeverity.ERROR
    )


class RelocationPreviewRequest(BaseModel):
    """路径重定位预演请求：条目 + 按顺序排列的源/目标前缀规则（纯内存）。"""

    entries: list[OverlayEntryIn] = Field(
        ..., min_length=1, description="待重定位的 file/directory 条目（至少 1 条）"
    )
    rules: list[RelocationRuleIn] = Field(
        default_factory=list,
        description=(
            "按顺序排列的源前缀→目标前缀规则（可省略或为空，表示全部条目保留"
            "原路径）；按规范化路径段的比较键做前缀匹配，最长源前缀优先、"
            "同长度取最后一条"
        ),
    )

    @model_validator(mode="after")
    def _validate_paths(self) -> "RelocationPreviewRequest":
        # 条目路径与规则前缀均复用 Windows 规范化；不安全即拒绝（422）。
        # 同键重复与文件祖先冲突不在此拒绝，而在重定位结果上逐条报告。
        for i, entry in enumerate(self.entries, start=1):
            normalized = normalize_entry(
                index=i, entry_type=entry.type.value, original_path=entry.path
            )
            if normalized.unsafe:
                raise ValueError(
                    f"条目 #{i}（{entry.path!r}）路径不安全："
                    f"{_relocation_error_codes(normalized)}"
                )
        for i, rule in enumerate(self.rules):
            for label, prefix in (
                ("源前缀", rule.source_prefix),
                ("目标前缀", rule.target_prefix),
            ):
                normalized = normalize_entry(
                    index=i, entry_type="directory", original_path=prefix
                )
                if normalized.unsafe:
                    raise ValueError(
                        f"规则 #{i} {label}（{prefix!r}）路径不安全："
                        f"{_relocation_error_codes(normalized)}"
                    )
        return self


class RelocationConflictOut(BaseModel):
    type: RelocationConflictType = Field(
        ...,
        description=(
            "冲突类型：DUPLICATE_KEY（重定位结果与其他条目同键重复）、"
            "FILE_ANCESTOR（本文件结果路径占据其他条目的祖先路径）、"
            "BLOCKED_BY_FILE_ANCESTOR（本条目祖先路径被其他文件结果路径占据）"
        ),
    )
    related_indices: list[int] = Field(
        ..., description="与本条目冲突的其他条目原下标（从 1 开始，升序）"
    )


class RelocationEntryOut(BaseModel):
    index: int = Field(..., description="条目下标（从 1 开始，与输入顺序一致）")
    type: OverlayEntryType
    original_path: str = Field(..., description="原始输入路径")
    new_path: str = Field(
        ...,
        description=(
            "重定位后的新路径（正斜杠分隔、NFC、段尾已裁剪）；"
            "未命中任何规则时等于规范化后的原路径"
        ),
    )
    matched_rule_index: int | None = Field(
        ...,
        description="命中的规则下标（从 0 开始，与请求 rules 下标一致）；未命中为 null",
    )
    usable: bool = Field(
        ..., description="结果路径未涉及任何冲突时为 true；冲突条目恒为 false"
    )
    conflicts: list[RelocationConflictOut] = Field(
        default_factory=list,
        description="本条目涉及的冲突（按类型分组，相关下标升序）；为空即可用",
    )


class RelocationSummary(BaseModel):
    total: int = Field(..., description="条目总数")
    usable: int = Field(..., description="结果无冲突、可用的条目数量")
    conflicted: int = Field(..., description="涉及至少一项冲突的条目数量")
    duplicate_key_groups: int = Field(..., description="结果同键重复的组数")
    duplicate_key_entries: int = Field(..., description="涉及同键重复的条目数量")
    file_ancestor_conflicts: int = Field(
        ..., description="文件占据其他条目祖先路径的冲突对数"
    )


class RelocationPreviewResponse(BaseModel):
    target: str = "windows"
    entries: list[RelocationEntryOut] = Field(
        ..., description="按输入顺序逐条给出重定位结果与冲突明细"
    )
    summary: RelocationSummary
