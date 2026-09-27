"""路径重定位预演：按源前缀→目标前缀规则改写条目路径并预检结果冲突（纯内存，不访问文件系统）。

语义概述：

* 条目（file/directory）与规则前缀均复用 Windows 路径规范化
  （见 :mod:`app.normalize`）；条目路径或规则前缀不安全时整个请求已被
  API 层拒绝（422），本模块只处理安全输入。
* 规则按规范化路径段的**比较键**（``casefold``）做前缀匹配：条目的规范
  路径段以规则的源前缀段开头即命中。多条命中时**最长源前缀优先**，
  同长度取**输入顺序的最后一条**；未命中任何规则时保留原（规范化）路径。
* 命中时新路径 = 目标前缀段 + 条目路径中未被源前缀覆盖的后缀段；
  条目路径恰好等于源前缀时新路径即目标前缀本身。
* 对全部重定位结果做两项冲突检查（均按结果比较键）：

  - 同键重复（``DUPLICATE_KEY``）：多条条目结果路径同键，互相冲突；
  - 文件占据祖先路径：文件条目结果路径是其他条目结果路径的祖先，
    文件侧记 ``FILE_ANCESTOR``，后代侧记 ``BLOCKED_BY_FILE_ANCESTOR``。

  涉及任一冲突的条目均不得标为可用（``usable`` 为 False）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .normalize import NormalizedEntry

FILE_TYPE = "file"
DIR_TYPE = "directory"


class RelocationConflictType(str, Enum):
    DUPLICATE_KEY = "DUPLICATE_KEY"  # 重定位结果与其他条目同键重复
    FILE_ANCESTOR = "FILE_ANCESTOR"  # 本文件结果路径占据其他条目的祖先路径
    BLOCKED_BY_FILE_ANCESTOR = "BLOCKED_BY_FILE_ANCESTOR"  # 本条目祖先路径被其他文件结果路径占据


# 冲突类型在每条目输出中的固定排列顺序。
_CONFLICT_ORDER = {
    RelocationConflictType.DUPLICATE_KEY: 0,
    RelocationConflictType.FILE_ANCESTOR: 1,
    RelocationConflictType.BLOCKED_BY_FILE_ANCESTOR: 2,
}


@dataclass(frozen=True)
class RelocationRule:
    """一条重定位规则（下标从 0 开始，与请求 rules 顺序一致）。"""

    index: int
    source_parts: tuple[str, ...]  # 源前缀规范段（展示用）
    target_parts: tuple[str, ...]  # 目标前缀规范段
    source_key_parts: tuple[str, ...]  # 源前缀比较键段
    target_key_parts: tuple[str, ...]  # 目标前缀比较键段


@dataclass(frozen=True)
class RelocationConflict:
    """单条条目涉及的一类冲突及其相关条目原下标（升序、去重）。"""

    type: RelocationConflictType
    related_indices: tuple[int, ...]


@dataclass
class RelocatedEntry:
    """单条条目的重定位结果与冲突标记。"""

    index: int  # 条目下标（从 1 开始，与输入顺序一致）
    entry_type: str  # "file" | "directory"
    original_path: str
    new_parts: list[str]
    new_key_parts: list[str]
    matched_rule_index: int | None  # 命中规则下标（从 0 开始）；未命中为 None
    conflicts: list[RelocationConflict] = field(default_factory=list)

    @property
    def new_path(self) -> str:
        return "/".join(self.new_parts)

    @property
    def new_key(self) -> str:
        return "/".join(self.new_key_parts)

    @property
    def usable(self) -> bool:
        return not self.conflicts


def build_rules(
    normalized_prefixes: list[tuple[NormalizedEntry, NormalizedEntry]],
) -> list[RelocationRule]:
    """把（已校验安全的）规范化源/目标前缀组装为重定位规则（下标从 0 开始）。"""
    return [
        RelocationRule(
            index=i,
            source_parts=tuple(source.canonical_parts),
            target_parts=tuple(target.canonical_parts),
            source_key_parts=tuple(source.key_parts),
            target_key_parts=tuple(target.key_parts),
        )
        for i, (source, target) in enumerate(normalized_prefixes)
    ]


def match_rule(
    key_parts: list[str], rules: list[RelocationRule]
) -> RelocationRule | None:
    """按比较键段做前缀匹配：最长源前缀优先，同长度取输入顺序的最后一条。"""
    best: RelocationRule | None = None
    best_len = -1
    for rule in rules:
        n = len(rule.source_key_parts)
        if n < best_len:
            continue
        if list(rule.source_key_parts) == key_parts[:n]:
            # 按输入顺序遍历，同长度时后出现的规则覆盖先出现的。
            best = rule
            best_len = n
    return best


def relocate_entries(
    entries: list[NormalizedEntry], rules: list[RelocationRule]
) -> list[RelocatedEntry]:
    """逐条应用重定位规则；未命中规则的条目保留原（规范化）路径。"""
    relocated: list[RelocatedEntry] = []
    for entry in entries:
        rule = match_rule(entry.key_parts, rules)
        if rule is None:
            new_parts = list(entry.canonical_parts)
            new_key_parts = list(entry.key_parts)
            matched = None
        else:
            n = len(rule.source_key_parts)
            new_parts = list(rule.target_parts) + entry.canonical_parts[n:]
            new_key_parts = list(rule.target_key_parts) + entry.key_parts[n:]
            matched = rule.index
        relocated.append(
            RelocatedEntry(
                index=entry.index,
                entry_type=entry.entry_type,
                original_path=entry.original_path,
                new_parts=new_parts,
                new_key_parts=new_key_parts,
                matched_rule_index=matched,
            )
        )
    return relocated


def detect_conflicts(relocated: list[RelocatedEntry]) -> None:
    """在重定位结果上检测同键重复与文件祖先冲突（就地写入各条目）。

    每条目的冲突先按下标集合去重累积，最后按固定类型顺序、相关下标升序
    写入 ``conflicts``。
    """
    related: dict[int, dict[RelocationConflictType, set[int]]] = {
        entry.index: {} for entry in relocated
    }

    def add(index: int, ctype: RelocationConflictType, other: int) -> None:
        related[index].setdefault(ctype, set()).add(other)

    # 1) 同键重复：结果比较键相同的多条条目互相冲突（不分类型）。
    by_key: dict[str, list[RelocatedEntry]] = {}
    for entry in relocated:
        by_key.setdefault(entry.new_key, []).append(entry)
    for members in by_key.values():
        if len(members) < 2:
            continue
        indices = [member.index for member in members]
        for member in members:
            for other in indices:
                if other != member.index:
                    add(member.index, RelocationConflictType.DUPLICATE_KEY, other)

    # 2) 文件占据祖先路径：文件结果路径是其他条目结果路径的祖先（不含自身）。
    files_by_key: dict[str, list[RelocatedEntry]] = {}
    for entry in relocated:
        if entry.entry_type == FILE_TYPE:
            files_by_key.setdefault(entry.new_key, []).append(entry)
    for descendant in relocated:
        parts = descendant.new_key_parts
        for depth in range(1, len(parts)):  # 不含自身（depth == len(parts)）
            ancestor_key = "/".join(parts[:depth])
            for ancestor in files_by_key.get(ancestor_key, ()):
                add(
                    descendant.index,
                    RelocationConflictType.BLOCKED_BY_FILE_ANCESTOR,
                    ancestor.index,
                )
                add(
                    ancestor.index,
                    RelocationConflictType.FILE_ANCESTOR,
                    descendant.index,
                )

    for entry in relocated:
        entry.conflicts = [
            RelocationConflict(ctype, tuple(sorted(indices)))
            for ctype, indices in sorted(
                related[entry.index].items(),
                key=lambda item: _CONFLICT_ORDER[item[0]],
            )
        ]
