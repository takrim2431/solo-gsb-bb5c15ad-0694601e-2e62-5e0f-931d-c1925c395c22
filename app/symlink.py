"""符号链接风险分析：目标越界、链接链循环与路径劫持（纯内存）。

分析规则：

* 链接目标按**链接所在目录**为基准，复用 ``normalize_entry`` 的 Windows
  规范化规则做词法解析；解析结果越出目标根（绝对/UNC/盘符路径，或 ``..``
  越过根）记为 ``LINK_TARGET_ESCAPE``；目标为空、含非法字符或命中保留
  设备名记为 ``LINK_TARGET_INVALID``。
* 基于整批条目追踪链接链：链接目标（或其中间祖先段）落在另一条链接的
  路径上时继续跟随；链中再次遇到已访问链接即构成循环（``LINK_CYCLE``）；
  链途经目标越界的链接时，越界沿链传播（``LINK_TARGET_ESCAPE``）。
* 普通文件/目录条目的任一祖先路径被符号链接占据即构成路径劫持
  （``SYMLINK_ANCESTOR``），被劫持条目标记为不安全。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import (
    Issue,
    IssueCode,
    IssueSeverity,
    NormalizedEntry,
    normalize_entry,
)
from .schemas import (
    SymlinkAnalysisOut,
    SymlinkChainStep,
    SymlinkCycleOut,
    SymlinkHijackOut,
    SymlinkLinkOut,
)

SYMLINK_TYPE = "symlink"

# 目标越出目标根对应的规范化错误码。
_ESCAPE_CODES = frozenset(
    {
        IssueCode.ABSOLUTE_PATH,
        IssueCode.UNC_PATH,
        IssueCode.DRIVE_PATH,
        IssueCode.TRAVERSAL_ESCAPE,
    }
)

# 目标自身非法（保留名/非法字符/裁剪后空段）对应的规范化错误码。
# 注：EMPTY_TARGET 不在此列——非空目标解析到目标根本身（如 "."）是合法的；
# 空目标字符串由 _resolve_target 显式判定。
_INVALID_CODES = frozenset(
    {
        IssueCode.RESERVED_NAME,
        IssueCode.INVALID_CHARACTER,
        IssueCode.EMPTY_SEGMENT,
    }
)


@dataclass
class _LinkState:
    """单条符号链接的分析中间状态。"""

    entry: NormalizedEntry
    target: str
    path_safe: bool  # 链接自身路径是否安全（此刻仅含路径规范化问题）
    target_parts: list[str] | None  # 目标相对链接所在目录解析后的根内路径段；越界为 None
    own_escape: bool  # 自身目标越界
    invalid: bool  # 自身目标非法
    escape: bool = False  # 越界（含沿链传播）
    cycle: bool = False
    cycle_members: list[int] = field(default_factory=list)
    chain: list[int] = field(default_factory=list)  # 依次经过的链接下标（含自身）
    final_parts: list[str] | None = None


def _resolve_target(
    entry: NormalizedEntry, target: str
) -> tuple[list[str] | None, bool, bool]:
    """相对链接所在目录解析目标，返回 (根内路径段|None, 是否越界, 是否非法)。"""
    parent = tuple(entry.canonical_parts[:-1])
    res = normalize_entry(entry.index, "link-target", target, initial_parts=parent)
    escape = any(
        i.code in _ESCAPE_CODES and i.severity == IssueSeverity.ERROR
        for i in res.issues
    )
    invalid = target == "" or any(
        i.code in _INVALID_CODES and i.severity == IssueSeverity.ERROR
        for i in res.issues
    )
    return (None if escape else res.canonical_parts), escape, invalid


def _follow(state: _LinkState, link_by_key: dict[str, _LinkState]) -> None:
    """沿链接链解析，填充 chain / final_parts / cycle / escape。"""
    state.chain = [state.entry.index]
    if state.target_parts is None:
        state.escape = True
        return
    visited = {state.entry.index}
    queue = list(state.target_parts)
    stack: list[str] = []
    while queue:
        stack.append(queue.pop(0))
        key = "/".join(p.casefold() for p in stack)
        nxt = link_by_key.get(key)
        if nxt is None:
            continue
        if nxt.entry.index in visited:
            state.cycle = True
            state.cycle_members = state.chain[state.chain.index(nxt.entry.index):]
            return
        visited.add(nxt.entry.index)
        state.chain.append(nxt.entry.index)
        if nxt.target_parts is None:
            state.escape = True  # 链途经目标越界的链接，越界沿链传播
            return
        # 用该链接的根内目标段替换已解析部分，剩余段继续解析。
        stack = []
        queue = list(nxt.target_parts) + queue
    state.final_parts = stack


def analyze_symlinks(
    entries: list[NormalizedEntry],
    link_targets: dict[int, str],
) -> SymlinkAnalysisOut:
    """对整批条目做符号链接风险分析。

    副作用：向链接条目追加 ``LINK_*`` 问题、向被劫持的文件/目录条目追加
    ``SYMLINK_ANCESTOR`` 问题（均为 error，直接影响 ``safe`` 判定）。
    返回按输入顺序排列的逐链接解析链与风险代码。
    """
    by_index = {e.index: e for e in entries}

    # 1) 每条链接的目标词法解析（相对链接所在目录，复用 Windows 规范化规则）。
    states: dict[int, _LinkState] = {}
    for entry in entries:
        if entry.entry_type != SYMLINK_TYPE:
            continue
        target = link_targets[entry.index]
        parts, escape, invalid = _resolve_target(entry, target)
        states[entry.index] = _LinkState(
            entry=entry,
            target=target,
            path_safe=not entry.unsafe,
            target_parts=parts,
            own_escape=escape,
            invalid=invalid,
            escape=escape,
        )

    # 2) 仅路径安全的链接可被其他链接的目标引用（键为小写折叠比较键）。
    link_by_key: dict[str, _LinkState] = {}
    for state in states.values():
        if state.path_safe and state.entry.comparison_key:
            link_by_key[state.entry.comparison_key] = state

    # 3) 逐链接追踪解析链，识别循环与链上越界传播。
    for state in states.values():
        _follow(state, link_by_key)

    # 循环去重：同一循环可能被多条链接的链发现，按最小下标旋转规范化。
    cycles: list[list[int]] = []
    seen_cycles: set[tuple[int, ...]] = set()
    for state in states.values():
        if not state.cycle:
            continue
        members = state.cycle_members
        start = members.index(min(members))
        canon = tuple(members[start:] + members[:start])
        if canon not in seen_cycles:
            seen_cycles.add(canon)
            cycles.append(list(canon))

    # 4) 向链接条目追加风险问题（error → 链接不安全）。
    for state in states.values():
        entry = state.entry
        if state.escape:
            if state.own_escape:
                message = (
                    f"符号链接目标 {state.target!r} 相对链接所在目录解析后"
                    "越出目标根目录"
                )
            else:
                message = "链接链途经目标越界的符号链接，解析结果越出目标根目录"
            entry.issues.append(
                Issue(IssueCode.LINK_TARGET_ESCAPE, IssueSeverity.ERROR, message)
            )
        if state.invalid:
            entry.issues.append(
                Issue(
                    IssueCode.LINK_TARGET_INVALID,
                    IssueSeverity.ERROR,
                    f"符号链接目标 {state.target!r} 为空、含 Windows 非法字符"
                    "或命中保留设备名",
                )
            )
        if state.cycle:
            entry.issues.append(
                Issue(
                    IssueCode.LINK_CYCLE,
                    IssueSeverity.ERROR,
                    "链接链存在循环，无法解析到最终目标",
                )
            )

    # 5) 路径劫持：文件/目录的任一祖先路径被符号链接占据。
    hijacked: list[SymlinkHijackOut] = []
    for victim in entries:
        if victim.entry_type == SYMLINK_TYPE or victim.unsafe:
            continue
        hits: list[int] = []
        parts = victim.key_parts
        for depth in range(1, len(parts)):  # 不含自身（depth == len(parts)）
            ancestor = link_by_key.get("/".join(parts[:depth]))
            if ancestor is not None:
                hits.append(ancestor.entry.index)
                victim.issues.append(
                    Issue(
                        IssueCode.SYMLINK_ANCESTOR,
                        IssueSeverity.ERROR,
                        f"祖先路径已被条目 #{ancestor.entry.index} 的符号链接"
                        f"（{ancestor.entry.canonical_path}）占据，"
                        "解包会写入链接指向的位置",
                        related_index=ancestor.entry.index,
                    )
                )
        if hits:
            hijacked.append(
                SymlinkHijackOut(
                    entry_index=victim.index,
                    canonical_path=victim.canonical_path,
                    symlink_ancestors=hits,
                )
            )

    # 6) 逐链接输出（states 按 entries 顺序构建，即输入顺序）。
    links_out: list[SymlinkLinkOut] = []
    for state in states.values():
        risk_codes: list[IssueCode] = []
        if state.escape:
            risk_codes.append(IssueCode.LINK_TARGET_ESCAPE)
        if state.invalid:
            risk_codes.append(IssueCode.LINK_TARGET_INVALID)
        if state.cycle:
            risk_codes.append(IssueCode.LINK_CYCLE)
        chain_steps = [
            SymlinkChainStep(index=i, path=by_index[i].canonical_path)
            for i in state.chain
        ]
        if state.final_parts is not None:
            chain_steps.append(
                SymlinkChainStep(index=None, path="/".join(state.final_parts))
            )
        links_out.append(
            SymlinkLinkOut(
                index=state.entry.index,
                original_path=state.entry.original_path,
                link_target=state.target,
                resolved_target=(
                    "/".join(state.target_parts)
                    if state.target_parts is not None
                    else None
                ),
                final_path=(
                    "/".join(state.final_parts)
                    if state.final_parts is not None
                    else None
                ),
                chain=chain_steps,
                risk_codes=risk_codes,
                safe=not state.entry.unsafe,
            )
        )

    return SymlinkAnalysisOut(
        links=links_out,
        cycles=[SymlinkCycleOut(members=m) for m in cycles],
        hijacked_entries=hijacked,
    )
