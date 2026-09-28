"""预检编排：规范化 → selection_rules 筛选 → 符号链接风险分析 → 资源预算校验 → 同名碰撞分组 → 层级冲突检测（纯内存）。"""

from __future__ import annotations

from .normalize import Issue, IssueCode, IssueSeverity, NormalizedEntry, normalize_entry
from .audit import AuditEntry, audit_manifests
from .disk_peak import simulate_disk_peak
from .mirror import MirrorOperation, plan_mirror
from .relocation import (
    RelocationConflictType,
    build_rules,
    detect_conflicts,
    relocate_entries,
)
from .overlay import (
    FILE_TYPE,
    OverlayNodeSource,
    OverlayStatus,
    apply_entry,
    build_existing_tree,
    build_manifest_tree,
)
from .repair import build_repair_plan
from .resource import DeclaredSizes, check_resource_budgets
from .rollback import RollbackOperation, plan_prefix_rollback
from .schemas import (
    CollisionGroup,
    CollisionMember,
    DiskPeakEntryOut,
    DiskPeakFirstExceedOut,
    DiskPeakPreviewRequest,
    DiskPeakPreviewResponse,
    DiskPeakSummary,
    EntryType,
    HierarchyConflict,
    ManifestAmbiguousKeyOut,
    ManifestAuditEntryOut,
    ManifestAuditRequest,
    ManifestAuditResponse,
    ManifestAuditSummary,
    ManifestBlockerOut,
    ManifestEntryRef,
    ManifestHierarchyBlockedOut,
    ManifestPairOut,
    MirrorPreviewRequest,
    MirrorPreviewResponse,
    MirrorStepOut,
    MirrorSummary,
    OverlayBlocker,
    OverlayEntryOut,
    OverlayEntryType,
    OverlayPreviewRequest,
    OverlayPreviewResponse,
    OverlaySummary,
    PreflightEntryOut,
    PreflightRepairResponse,
    PreflightRequest,
    PreflightResponse,
    RelocationConflictOut,
    RelocationEntryOut,
    RelocationPreviewRequest,
    RelocationPreviewResponse,
    RelocationSummary,
    RollbackBackupFileOut,
    RollbackPreviewRequest,
    RollbackPreviewResponse,
    RollbackStepOut,
    RollbackSummary,
    Summary,
)
from .selection import apply_selection_rules
from .symlink import analyze_symlinks


def _add_issue(entry: NormalizedEntry, issue: Issue) -> None:
    # 避免重复追加同一相关下标的问题。
    if issue.code in (IssueCode.BLOCKED_BY_FILE_ANCESTOR, IssueCode.BLOCKS_DESCENDANT):
        for existing in entry.issues:
            if (
                existing.code == issue.code
                and existing.related_index == issue.related_index
            ):
                return
    entry.issues.append(issue)


def _collision_groups(entries: list[NormalizedEntry]) -> dict[int, int]:
    """为安全条目按 comparison_key 分组，返回 entry.index -> group 编号。

    符号链接不参与同名碰撞分组（其路径冲突由符号链接风险分析覆盖）。
    """
    buckets: dict[str, list[NormalizedEntry]] = {}
    first_seen: dict[str, int] = {}
    for entry in entries:
        if (
            entry.unsafe
            or entry.comparison_key is None
            or entry.entry_type == EntryType.SYMLINK.value
        ):
            continue
        key = entry.comparison_key
        if key not in buckets:
            buckets[key] = []
            first_seen[key] = entry.index
        buckets[key].append(entry)

    # 组号按该组首次出现的下标排序分配。
    ordered_keys = sorted(first_seen, key=lambda k: first_seen[k])
    index_to_group: dict[int, int] = {}
    group_of_key: dict[str, int] = {}
    for group_no, key in enumerate(ordered_keys, start=1):
        if len(buckets[key]) < 2:
            continue
        group_of_key[key] = group_no
        for entry in buckets[key]:
            index_to_group[entry.index] = group_no
    return index_to_group


def _hierarchy_conflicts(
    entries: list[NormalizedEntry],
) -> list[tuple[NormalizedEntry, NormalizedEntry]]:
    """文件占据祖先路径 → 阻挡同名前缀后代，返回 (文件条目, 后代条目) 对。

    仅对安全条目计算。键为小写折叠后的路径段，故不同大小写/点空格变体也会匹配。
    """
    files_by_key: dict[str, list[NormalizedEntry]] = {}
    for entry in entries:
        if entry.unsafe or entry.entry_type != EntryType.FILE.value:
            continue
        files_by_key.setdefault(entry.comparison_key, []).append(entry)

    conflicts: list[tuple[NormalizedEntry, NormalizedEntry]] = []
    for descendant in entries:
        if descendant.unsafe:
            continue
        parts = descendant.key_parts
        for depth in range(1, len(parts)):  # 不含自身（depth == len(parts)）
            ancestor_key = "/".join(parts[:depth])
            for ancestor in files_by_key.get(ancestor_key, ()):
                conflicts.append((ancestor, descendant))
    # 稳定输出：按后代下标、再按祖先下标排序。
    conflicts.sort(key=lambda pair: (pair[1].index, pair[0].index))
    return conflicts


def run_preflight(request: PreflightRequest) -> PreflightResponse:
    normalized: list[NormalizedEntry] = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.entries, start=1)
    ]

    # 选择规则在规范化之后、一切风险/冲突分析之前应用：被排除条目仍返回
    # 规范化信息，但不参与后续符号链接、资源预算、碰撞、层级冲突与 repair。
    rules = None
    if request.selection_rules is not None:
        rules = [(r.action.value, r.pattern) for r in request.selection_rules]
    decisions = apply_selection_rules(
        [n.canonical_parts for n in normalized], rules
    )
    selection = {e.index: d for e, d in zip(normalized, decisions)}
    active = [e for e in normalized if selection[e.index].selected]

    # 符号链接风险分析先于碰撞/层级检测：被劫持条目会被标记为不安全，
    # 从而退出后续碰撞分组、层级冲突与 repair 复检。
    # all_link_targets 仅用于逐条回显；被排除条目不进入风险分析。
    all_link_targets = {
        i: e.link_target
        for i, e in enumerate(request.entries, start=1)
        if e.link_target is not None
    }
    link_targets = {i: t for i, t in all_link_targets.items() if selection[i].selected}
    symlink_analysis = analyze_symlinks(active, link_targets)

    # 资源预算校验紧随符号链接分析：超限文件被标记为不安全，
    # 从而退出后续碰撞分组、层级冲突与 repair 复检。
    limits = request.resource_limits
    declared_sizes = {
        i: DeclaredSizes(
            compressed=e.compressed_bytes, uncompressed=e.uncompressed_bytes
        )
        for i, e in enumerate(request.entries, start=1)
        if (
            selection[i].selected
            and (e.compressed_bytes is not None or e.uncompressed_bytes is not None)
        )
    }
    resource_outcome = check_resource_budgets(
        active,
        declared_sizes,
        max_file_uncompressed_bytes=(
            limits.max_file_uncompressed_bytes if limits else None
        ),
        max_total_uncompressed_bytes=(
            limits.max_total_uncompressed_bytes if limits else None
        ),
        max_compression_ratio=limits.max_compression_ratio if limits else None,
    )

    index_to_group = _collision_groups(active)

    conflicts = _hierarchy_conflicts(active)
    blocked_by: dict[int, list[int]] = {}
    blocks: dict[int, list[int]] = {}
    hierarchy_out: list[HierarchyConflict] = []
    for ancestor, descendant in conflicts:
        blocked_by.setdefault(descendant.index, []).append(ancestor.index)
        blocks.setdefault(ancestor.index, []).append(descendant.index)
        _add_issue(
            ancestor,
            Issue(
                IssueCode.BLOCKS_DESCENDANT,
                IssueSeverity.INFO,
                f"文件占据祖先路径，会阻挡条目 #{descendant.index}（{descendant.canonical_path}）解包",
                related_index=descendant.index,
            ),
        )
        _add_issue(
            descendant,
            Issue(
                IssueCode.BLOCKED_BY_FILE_ANCESTOR,
                IssueSeverity.INFO,
                f"祖先路径已被条目 #{ancestor.index} 的文件（{ancestor.canonical_path}）占据，无法创建目录",
                related_index=ancestor.index,
            ),
        )
        hierarchy_out.append(
            HierarchyConflict(
                file_index=ancestor.index,
                file_canonical_path=ancestor.canonical_path,
                descendant_index=descendant.index,
                descendant_canonical_path=descendant.canonical_path,
            )
        )

    # 碰撞组明细（仅包含真实发生碰撞的组）。
    groups_detail: dict[int, dict] = {}
    for entry in active:
        group_no = index_to_group.get(entry.index)
        if group_no is None:
            continue
        detail = groups_detail.setdefault(
            group_no,
            {
                "group": group_no,
                "canonical_path": entry.canonical_path,
                "members": [],
            },
        )
        detail["members"].append(
            CollisionMember(index=entry.index, canonical_path=entry.canonical_path)
        )

    collision_groups = [
        CollisionGroup(**groups_detail[no])
        for no in sorted(groups_detail)
    ]
    group_members: dict[int, list[int]] = {
        no: [m.index for m in groups_detail[no]["members"]]
        for no in sorted(groups_detail)
    }

    # 碰撞提示问题（仅选中条目参与碰撞分组）。
    for entry in active:
        group_no = index_to_group.get(entry.index)
        if group_no is not None:
            _add_issue(
                entry,
                Issue(
                    IssueCode.COLLISION,
                    IssueSeverity.INFO,
                    f"规范化后与组 {group_no} 内的其他条目同名，解包时互相覆盖",
                ),
            )

    entry_out = [
        PreflightEntryOut(
            index=e.index,
            type=EntryType(e.entry_type),
            original_path=e.original_path,
            link_target=all_link_targets.get(e.index),
            canonical_path=e.canonical_path,
            comparison_key=e.comparison_key,
            safe=not e.unsafe,
            collision_group=index_to_group.get(e.index),
            blocked_by_file_ancestors=blocked_by.get(e.index, []),
            blocks_descendants=blocks.get(e.index, []),
            selected=selection[e.index].selected,
            matched_rule_index=selection[e.index].matched_rule_index,
            issues=[
                {
                    "code": i.code,
                    "severity": i.severity,
                    "message": i.message,
                    "segment": i.segment,
                    "related_index": i.related_index,
                }
                for i in e.issues
            ],
        )
        for e in normalized
    ]

    unsafe_count = sum(1 for e in normalized if e.unsafe)
    selected_count = sum(1 for d in decisions if d.selected)
    summary = Summary(
        total=len(normalized),
        safe=len(normalized) - unsafe_count,
        unsafe=unsafe_count,
        selected=selected_count,
        excluded=len(normalized) - selected_count,
        collisions=len(collision_groups),
        collision_entries=len(index_to_group),
        hierarchy_conflicts=len(hierarchy_out),
        symlinks=len(symlink_analysis.links),
        symlink_cycles=len(symlink_analysis.cycles),
        symlink_hijacked=len(symlink_analysis.hijacked_entries),
        declared_compressed_bytes=resource_outcome.declared_compressed_bytes,
        declared_uncompressed_bytes=resource_outcome.declared_uncompressed_bytes,
        resource_limits_exceeded=resource_outcome.exceeded,
    )

    if request.repair:
        repair_plan = build_repair_plan(
            entries=active,
            collision_groups=group_members,
            blocks=blocks,
            collisions_total=len(collision_groups),
            hierarchy_total=len(hierarchy_out),
        )
        return PreflightRepairResponse(
            target="windows",
            entries=entry_out,
            collision_groups=collision_groups,
            hierarchy_conflicts=hierarchy_out,
            symlink_analysis=symlink_analysis,
            summary=summary,
            repair_plan=repair_plan,
        )

    return PreflightResponse(
        target="windows",
        entries=entry_out,
        collision_groups=collision_groups,
        hierarchy_conflicts=hierarchy_out,
        symlink_analysis=symlink_analysis,
        summary=summary,
    )


def _overlay_entry_out(
    normalized: NormalizedEntry, decision
) -> OverlayEntryOut:
    """把单条 overlay 干跑判定结果构造为响应条目。"""
    blocker = None
    if decision.blocker is not None:
        blocker = OverlayBlocker(
            source=decision.blocker.source,
            index=decision.blocker.index,
            canonical_path=decision.blocker.canonical_path,
        )
    return OverlayEntryOut(
        index=normalized.index,
        type=OverlayEntryType(normalized.entry_type),
        original_path=normalized.original_path,
        canonical_path=normalized.canonical_path,
        comparison_key=normalized.comparison_key,
        safe=not normalized.unsafe,
        status=decision.status,
        blocked_reason=decision.reason,
        blocked_by=blocker,
        issues=[
            {
                "code": issue.code,
                "severity": issue.severity,
                "message": issue.message,
                "segment": issue.segment,
                "related_index": issue.related_index,
            }
            for issue in normalized.issues
        ],
    )


def run_overlay_preview(request: OverlayPreviewRequest) -> OverlayPreviewResponse:
    """目标目录覆盖预演：现存清单建树后按序干跑解包清单（纯内存）。

    现存清单已在请求模型校验阶段通过
    :func:`app.overlay.validate_existing_entries` 检查（否则请求以 422
    被拒绝），此处直接构建虚拟目录树。
    """
    existing = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.existing, start=1)
    ]
    tree = build_existing_tree(existing)

    entries_out: list[OverlayEntryOut] = []
    counts = {status: 0 for status in OverlayStatus}
    for i, entry_in in enumerate(request.incoming, start=1):
        normalized = normalize_entry(
            index=i, entry_type=entry_in.type.value, original_path=entry_in.path
        )
        decision = apply_entry(tree, normalized)
        counts[decision.status] += 1
        entries_out.append(_overlay_entry_out(normalized, decision))

    summary = OverlaySummary(
        total=len(entries_out),
        create=counts[OverlayStatus.CREATE],
        overwrite=counts[OverlayStatus.OVERWRITE],
        reuse_directory=counts[OverlayStatus.REUSE_DIRECTORY],
        blocked=counts[OverlayStatus.BLOCKED],
    )
    return OverlayPreviewResponse(
        target="windows", entries=entries_out, summary=summary
    )


def run_disk_peak_preview(request: DiskPeakPreviewRequest) -> DiskPeakPreviewResponse:
    """磁盘暂存峰值预演：现存清单建树后按序模拟“同盘备份 → 暂存 → 替换”（纯内存）。

    现存清单已在请求模型校验阶段通过
    :func:`app.overlay.validate_existing_entries` 检查，文件 size_bytes 的
    非负与按类型携带也已在 schema 层保证（否则请求以 422 被拒绝）。
    即使整批峰值超过初始可用字节数也照常模拟完全部条目，仅记录首次超额点。
    """
    existing = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.existing, start=1)
    ]
    tree = build_existing_tree(existing)
    sizes = {
        entry.comparison_key: request.existing[entry.index - 1].size_bytes
        for entry in existing
        if entry.entry_type == FILE_TYPE
    }

    incoming = [
        (
            normalize_entry(
                index=i, entry_type=entry_in.type.value, original_path=entry_in.path
            ),
            entry_in.size_bytes,
        )
        for i, entry_in in enumerate(request.incoming, start=1)
    ]
    simulation = simulate_disk_peak(
        tree,
        sizes,
        incoming,
        initial_available_bytes=request.initial_available_bytes,
    )

    entries_out: list[DiskPeakEntryOut] = []
    counts = {status: 0 for status in OverlayStatus}
    for (normalized, _), result in zip(incoming, simulation.results):
        counts[result.decision.status] += 1
        base = _overlay_entry_out(normalized, result.decision).model_dump()
        entries_out.append(
            DiskPeakEntryOut(
                **base,
                staging_peak_bytes=result.staging_peak,
                committed_bytes=result.committed,
            )
        )

    first_exceed = simulation.first_exceed
    summary = DiskPeakSummary(
        total=len(entries_out),
        create=counts[OverlayStatus.CREATE],
        overwrite=counts[OverlayStatus.OVERWRITE],
        reuse_directory=counts[OverlayStatus.REUSE_DIRECTORY],
        blocked=counts[OverlayStatus.BLOCKED],
        batch_peak_bytes=simulation.batch_peak,
        committed_bytes=simulation.final_committed,
        first_exceed=(
            None
            if first_exceed is None
            else DiskPeakFirstExceedOut(
                phase=first_exceed.phase,
                index=first_exceed.index,
                canonical_path=first_exceed.canonical_path,
            )
        ),
    )
    return DiskPeakPreviewResponse(
        target="windows",
        initial_available_bytes=request.initial_available_bytes,
        entries=entries_out,
        summary=summary,
    )


def run_rollback_preview(request: RollbackPreviewRequest) -> RollbackPreviewResponse:
    """前缀解包回退预演：只模拟前 stop_after 条并生成备份清单与回退步骤（纯内存）。

    现存清单已在请求模型校验阶段通过
    :func:`app.overlay.validate_existing_entries` 检查，stop_after 取值也已
    约束在 0 至清单长度之间（否则请求以 422 被拒绝）。
    """
    existing = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.existing, start=1)
    ]
    tree = build_existing_tree(existing)

    prefix = [
        normalize_entry(index=i, entry_type=entry_in.type.value, original_path=entry_in.path)
        for i, entry_in in enumerate(request.incoming[: request.stop_after], start=1)
    ]
    decisions, backups, steps = plan_prefix_rollback(tree, prefix)

    entries_out = [
        _overlay_entry_out(normalized, decision) for normalized, decision in decisions
    ]
    counts = {status: 0 for status in OverlayStatus}
    for _, decision in decisions:
        counts[decision.status] += 1

    op_counts = {op: 0 for op in RollbackOperation}
    for step in steps:
        op_counts[step.operation] += 1

    backup_out = [
        RollbackBackupFileOut(
            existing_index=backup.existing_index,
            canonical_path=backup.canonical_path,
            comparison_key=backup.comparison_key,
            first_overwrite_index=backup.first_overwrite_index,
        )
        for backup in backups
    ]
    step_out = [
        RollbackStepOut(
            order=step.order,
            operation=step.operation,
            node_type=OverlayEntryType(step.node_type),
            source=step.source,
            index=step.index,
            canonical_path=step.canonical_path,
            comparison_key=step.comparison_key,
        )
        for step in steps
    ]

    summary = RollbackSummary(
        total=len(entries_out),
        create=counts[OverlayStatus.CREATE],
        overwrite=counts[OverlayStatus.OVERWRITE],
        reuse_directory=counts[OverlayStatus.REUSE_DIRECTORY],
        blocked=counts[OverlayStatus.BLOCKED],
        backups=len(backup_out),
        rollback_steps=len(step_out),
        delete_created_file=op_counts[RollbackOperation.DELETE_CREATED_FILE],
        restore_file=op_counts[RollbackOperation.RESTORE_FILE],
        remove_directory=op_counts[RollbackOperation.REMOVE_DIRECTORY],
    )
    return RollbackPreviewResponse(
        target="windows",
        stop_after=request.stop_after,
        entries=entries_out,
        backup_files=backup_out,
        rollback_steps=step_out,
        summary=summary,
    )


def run_mirror_preview(request: MirrorPreviewRequest) -> MirrorPreviewResponse:
    """目录镜像同步预演：生成把现存布局同步为期望布局的操作序列（纯内存）。

    两侧清单已在请求模型校验阶段通过
    :func:`app.overlay.validate_existing_entries` 检查（否则请求以 422
    被拒绝），此处直接构建两棵虚拟目录树并交给
    :func:`app.mirror.plan_mirror` 排定操作。
    """
    existing = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.existing, start=1)
    ]
    incoming = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.incoming, start=1)
    ]

    existing_tree = build_existing_tree(existing)
    desired_tree = build_manifest_tree(incoming, OverlayNodeSource.INCOMING)
    incoming_files = [
        (entry.index, entry.canonical_path, entry.comparison_key)
        for entry in incoming
        if entry.entry_type == "file"
    ]

    planned = plan_mirror(existing_tree, desired_tree, incoming_files)

    steps_out = [
        MirrorStepOut(
            order=step.order,
            operation=step.operation,
            node_type=OverlayEntryType(step.node_type),
            source=step.source,
            index=step.index,
            canonical_path=step.canonical_path,
            comparison_key=step.comparison_key,
        )
        for step in planned
    ]

    counts = {op: 0 for op in MirrorOperation}
    for step in planned:
        counts[step.operation] += 1
    summary = MirrorSummary(
        total=len(steps_out),
        delete=counts[MirrorOperation.DELETE],
        keep_directory=counts[MirrorOperation.KEEP_DIRECTORY],
        create_directory=counts[MirrorOperation.CREATE_DIRECTORY],
        create_file=counts[MirrorOperation.CREATE_FILE],
        overwrite_file=counts[MirrorOperation.OVERWRITE_FILE],
    )
    return MirrorPreviewResponse(
        target="windows", steps=steps_out, summary=summary
    )


def _normalize_audit_side(
    entries,
) -> tuple[list[NormalizedEntry], dict[int, str]]:
    """规范化单侧清单并收集文件声明摘要（统一小写）。

    路径安全性与摘要存在性/格式已在请求模型校验阶段保证（否则 422）。
    """
    normalized = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(entries, start=1)
    ]
    digests = {
        i: entry.sha256.lower()
        for i, entry in enumerate(entries, start=1)
        if entry.sha256 is not None
    }
    return normalized, digests


def _audit_entry_ref(entry: AuditEntry) -> ManifestEntryRef:
    return ManifestEntryRef(
        side=entry.side,
        index=entry.index,
        type=OverlayEntryType(entry.entry_type),
        original_path=entry.original_path,
        canonical_path=entry.canonical_path,
        comparison_key=entry.comparison_key,
        sha256=entry.sha256,
    )


def _audit_pair_out(finding) -> ManifestPairOut:
    return ManifestPairOut(
        status=finding.status,
        comparison_key=finding.comparison_key,
        agreed=_audit_entry_ref(finding.agreed) if finding.agreed is not None else None,
        delivered=(
            _audit_entry_ref(finding.delivered)
            if finding.delivered is not None
            else None
        ),
    )


def run_manifest_audit(request: ManifestAuditRequest) -> ManifestAuditResponse:
    """核对约定清单与交付清单（纯内存，只比较声明摘要，不读文件内容）。

    两侧清单已在请求模型校验阶段完成路径安全与文件摘要校验（否则 422）。
    """
    agreed, agreed_digests = _normalize_audit_side(request.agreed)
    delivered, delivered_digests = _normalize_audit_side(request.delivered)

    result = audit_manifests(
        agreed=agreed,
        delivered=delivered,
        agreed_digests=agreed_digests,
        delivered_digests=delivered_digests,
    )

    agreed_out = [
        ManifestAuditEntryOut(**_audit_entry_ref(entry).model_dump())
        for entry in result.agreed_entries
    ]
    delivered_out = [
        ManifestAuditEntryOut(**_audit_entry_ref(entry).model_dump())
        for entry in result.delivered_entries
    ]

    hierarchy_out = [
        ManifestHierarchyBlockedOut(
            blocked=_audit_entry_ref(item.blocked),
            blockers=[
                ManifestBlockerOut(
                    side=blocker.side,
                    index=blocker.index,
                    canonical_path=blocker.canonical_path,
                    comparison_key=blocker.comparison_key,
                    sha256=blocker.sha256,
                )
                for blocker in item.blockers
            ],
        )
        for item in result.hierarchy_blocked
    ]

    ambiguous_out = [
        ManifestAmbiguousKeyOut(
            comparison_key=item.comparison_key,
            agreed=[_audit_entry_ref(entry) for entry in item.agreed],
            delivered=[_audit_entry_ref(entry) for entry in item.delivered],
        )
        for item in result.ambiguous_keys
    ]

    ambiguous_entry_count = sum(
        len(item.agreed) + len(item.delivered) for item in result.ambiguous_keys
    )
    summary = ManifestAuditSummary(
        agreed_total=len(result.agreed_entries),
        delivered_total=len(result.delivered_entries),
        missing=len(result.missing),
        extra=len(result.extra),
        type_mismatches=len(result.type_mismatches),
        digest_mismatches=len(result.digest_mismatches),
        matched=len(result.matched),
        hierarchy_blocked=len(result.hierarchy_blocked),
        ambiguous_keys=len(result.ambiguous_keys),
        ambiguous_entries=ambiguous_entry_count,
    )

    return ManifestAuditResponse(
        target="windows",
        agreed_entries=agreed_out,
        delivered_entries=delivered_out,
        missing=[_audit_pair_out(item) for item in result.missing],
        extra=[_audit_pair_out(item) for item in result.extra],
        type_mismatches=[_audit_pair_out(item) for item in result.type_mismatches],
        digest_mismatches=[_audit_pair_out(item) for item in result.digest_mismatches],
        matched=[_audit_pair_out(item) for item in result.matched],
        hierarchy_blocked=hierarchy_out,
        ambiguous_keys=ambiguous_out,
        summary=summary,
    )


def run_relocation_preview(
    request: RelocationPreviewRequest,
) -> RelocationPreviewResponse:
    """路径重定位预演：按规则改写条目路径并预检结果冲突（纯内存）。

    条目路径与规则前缀已在请求模型校验阶段确认安全（否则 422），
    此处直接规范化并应用规则；同键重复与文件祖先冲突在重定位结果上
    逐条检测，冲突条目不得标为可用。
    """
    normalized = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.entries, start=1)
    ]
    rules = build_rules(
        [
            (
                normalize_entry(
                    index=i, entry_type="directory", original_path=rule.source_prefix
                ),
                normalize_entry(
                    index=i, entry_type="directory", original_path=rule.target_prefix
                ),
            )
            for i, rule in enumerate(request.rules)
        ]
    )

    relocated = relocate_entries(normalized, rules)
    detect_conflicts(relocated)

    entries_out = [
        RelocationEntryOut(
            index=entry.index,
            type=OverlayEntryType(entry.entry_type),
            original_path=entry.original_path,
            new_path=entry.new_path,
            matched_rule_index=entry.matched_rule_index,
            usable=entry.usable,
            conflicts=[
                RelocationConflictOut(
                    type=conflict.type,
                    related_indices=list(conflict.related_indices),
                )
                for conflict in entry.conflicts
            ],
        )
        for entry in relocated
    ]

    key_counts: dict[str, int] = {}
    for entry in relocated:
        key_counts[entry.new_key] = key_counts.get(entry.new_key, 0) + 1
    duplicate_key_entries = sum(
        1 for entry in relocated if key_counts[entry.new_key] > 1
    )
    file_ancestor_pairs = sum(
        len(conflict.related_indices)
        for entry in relocated
        for conflict in entry.conflicts
        if conflict.type is RelocationConflictType.FILE_ANCESTOR
    )
    conflicted = sum(1 for entry in relocated if not entry.usable)
    summary = RelocationSummary(
        total=len(relocated),
        usable=len(relocated) - conflicted,
        conflicted=conflicted,
        duplicate_key_groups=sum(1 for count in key_counts.values() if count > 1),
        duplicate_key_entries=duplicate_key_entries,
        file_ancestor_conflicts=file_ancestor_pairs,
    )
    return RelocationPreviewResponse(
        target="windows", entries=entries_out, summary=summary
    )
