"""确定性修复计划：同名碰撞与文件祖先冲突的改名规划（纯内存）。

规则（repair=true 时启用）：

* 仅处理安全的 file/directory 条目；不安全条目标记 skipped，repaired_path
  为 null 且不改名。符号链接一律 skipped（reasons 为 [SYMLINK]），不参与
  改名与复检；被符号链接祖先劫持的条目已标记为不安全，随之一并 skipped。
* 同名碰撞组中保留最先出现且自身不阻挡后代的条目（含目录），其余条目在末段
  扩展名前插入 `` (n)``（n 从 1 递增）。若组内全部阻挡后代，则全部改名。
* 文件占据祖先路径阻挡后代时，重命名该文件（而不是被阻挡的后代）。
* 每个候选名都按 Windows comparison_key（``str.casefold()``）同时检查同名碰撞
  与文件/目录层级冲突（候选不得与已占用路径同名，文件候选不得占据已占用路径的
  祖先、任何候选也不得落在已占用文件路径之下），递增 n 直至整批无冲突。
"""

from __future__ import annotations

from dataclasses import dataclass

from .normalize import NormalizedEntry
from .schemas import EntryType, RepairEntry, RepairPlan, RepairReason

# 改名次数理论上受条目数约束，留一定余量防止意外死循环。
_MAX_ATTEMPTS = 1_000_000


@dataclass(frozen=True)
class _Claim:
    """一条已被占用的修复后路径。"""

    key_parts: tuple[str, ...]
    is_file: bool


def _split_ext(last: str) -> tuple[str, str]:
    """按末段扩展名切分。

    约定：以 ``.`` 开头的段（点文件，如 ``.gitignore``）整体视为词干；
    其余取最后一个 ``.`` 作为扩展名分隔点。
    """
    dot = last.rfind(".")
    if dot <= 0:
        return last, ""
    return last[:dot], last[dot:]


def _has_hierarchy_clash(
    candidate_parts: tuple[str, ...], is_file: bool, claim: _Claim
) -> bool:
    a, b = candidate_parts, claim.key_parts
    common = min(len(a), len(b))
    if a[:common] != b[:common] or len(a) == len(b):
        # 共同部分不一致：无关；等长且共同部分全等：同名碰撞。
        return len(a) == len(b) and a == b
    if len(a) < len(b):
        # 候选更短：候选是已占用路径的祖先，候选为文件时阻挡已占用后代。
        return is_file
    # 候选更长：候选位于已占用路径之下，已占用祖先为文件时阻挡候选。
    return claim.is_file


def build_repair_plan(
    entries: list[NormalizedEntry],
    collision_groups: dict[int, list[int]],
    blocks: dict[int, list[int]],
    collisions_total: int,
    hierarchy_total: int,
) -> RepairPlan:
    """根据碰撞分组与层级阻挡关系构造确定性修复计划。

    ``collision_groups``: 碰撞组编号 -> 组成员下标列表。
    ``blocks``: 阻挡后代的文件下标 -> 被其阻挡的成员下标列表。
    """
    by_index = {e.index: e for e in entries}

    # 每个条目至多属于一个碰撞组（comparison_key 唯一），先建下标 -> 组号映射。
    group_of: dict[int, int] = {}
    for group_no, members in collision_groups.items():
        for idx in members:
            group_of[idx] = group_no

    # 组内保留者：最先出现且自身不阻挡任何后代的成员。
    winners: dict[int, int] = {}
    for group_no, members in collision_groups.items():
        for idx in sorted(members):
            if idx not in blocks:
                winners[group_no] = idx
                break

    renamers: list[int] = []
    chosen_n: dict[int, int] = {}
    reasons: dict[int, list[RepairReason]] = {}
    repaired_parts: dict[int, tuple[str, ...]] = {}

    # 保留原始路径的条目（碰撞保留者 + 无冲突条目）预占路径，
    # 使改名条目同时避开“已处理条目”和“未来确定保留的条目”。
    claims: list[_Claim] = []
    claim_keys: set[tuple[str, ...]] = set()

    def claim(index: int, parts: tuple[str, ...]) -> None:
        entry = by_index[index]
        claims.append(
            _Claim(
                key_parts=parts,
                is_file=entry.entry_type == EntryType.FILE.value,
            )
        )
        claim_keys.add(parts)

    for entry in entries:
        if entry.entry_type == EntryType.SYMLINK.value:
            # 符号链接不参与改名与复检，恒为 skipped。
            reasons[entry.index] = [RepairReason.SYMLINK]
            continue
        if entry.unsafe:
            reasons[entry.index] = [RepairReason.UNSAFE]
            continue
        parts = tuple(entry.key_parts)
        entry_reasons: list[RepairReason] = []
        group_no = group_of.get(entry.index)
        if group_no is not None and winners.get(group_no) != entry.index:
            entry_reasons.append(RepairReason.COLLISION)
        if entry.index in blocks:
            entry_reasons.append(RepairReason.BLOCKS_DESCENDANT)
        reasons[entry.index] = entry_reasons

        if entry_reasons:
            renamers.append(entry.index)
        else:
            repaired_parts[entry.index] = parts
            claim(entry.index, parts)

    def candidate_parts(index: int, n: int) -> tuple[str, ...]:
        original = tuple(by_index[index].key_parts)
        stem, ext = _split_ext(original[-1])
        new_last = f"{stem} ({n}){ext}"
        return (*original[:-1], new_last)

    # 按输入顺序为每个改名条目挑选最小可用 n。
    for index in renamers:
        entry = by_index[index]
        is_file = entry.entry_type == EntryType.FILE.value
        n = 1
        while True:
            if n > _MAX_ATTEMPTS:  # pragma: no cover - 理论上不可达
                raise RuntimeError("repair 候选 n 超出上限")
            cand = candidate_parts(index, n)
            clash = cand in claim_keys or any(
                _has_hierarchy_clash(cand, is_file, held) for held in claims
            )
            if not clash:
                repaired_parts[index] = cand
                chosen_n[index] = n
                claim(index, cand)
                break
            n += 1

    def display_path(index: int) -> str:
        """用原始规范段的大小写展示修复后路径。"""
        canonical = by_index[index].canonical_parts
        stem, ext = _split_ext(canonical[-1])
        return "/".join(
            (*canonical[:-1], f"{stem} ({chosen_n[index]}){ext}")
        )

    plan_entries: list[RepairEntry] = []
    for entry in entries:
        idx = entry.index
        if entry.entry_type == EntryType.SYMLINK.value or entry.unsafe:
            plan_entries.append(
                RepairEntry(
                    index=idx,
                    original_path=entry.original_path,
                    repaired_path=None,
                    reasons=reasons[idx],
                    skipped=True,
                )
            )
            continue
        if reasons[idx]:
            repaired = display_path(idx)
        else:
            repaired = entry.canonical_path
        plan_entries.append(
            RepairEntry(
                index=idx,
                original_path=entry.original_path,
                repaired_path=repaired,
                reasons=reasons[idx],
                skipped=False,
            )
        )

    # 按修复后路径复检：不安全条目与符号链接不参与（二者本就不在 repaired_parts 中）。
    final_keys = {
        idx: repaired_parts[idx]
        for idx in repaired_parts
        if not by_index[idx].unsafe
    }
    key_buckets: dict[tuple[str, ...], bool] = {}
    collisions_remaining = 0
    for idx in sorted(final_keys):
        key = final_keys[idx]
        if key not in key_buckets:
            key_buckets[key] = False  # 该键目前仅出现一次
        elif not key_buckets[key]:
            key_buckets[key] = True  # 第二次出现：形成一个碰撞组
            collisions_remaining += 1

    files_by_key: dict[tuple[str, ...], list[int]] = {}
    for idx, key in final_keys.items():
        if by_index[idx].entry_type == EntryType.FILE.value:
            files_by_key.setdefault(key, []).append(idx)
    hierarchy_remaining = 0
    for idx in sorted(final_keys):
        parts = final_keys[idx]
        for depth in range(1, len(parts)):
            hierarchy_remaining += len(files_by_key.get(parts[:depth], ()))

    skipped_count = sum(
        1
        for e in entries
        if e.unsafe or e.entry_type == EntryType.SYMLINK.value
    )
    return RepairPlan(
        entries=plan_entries,
        renamed_count=len(renamers),
        skipped_count=skipped_count,
        collisions_remaining=collisions_remaining,
        hierarchy_conflicts_remaining=hierarchy_remaining,
        collisions_resolved=collisions_total,
        hierarchy_conflicts_resolved=hierarchy_total,
    )
