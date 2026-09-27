"""约定清单（agreed）与交付清单（delivered）核对（纯内存，不访问文件系统）。

只比较双方**声明**的信息（类型与 SHA-256），绝不读取文件内容。两侧清单
均已由 API 层完成校验：路径经 Windows 规则规范化且安全、文件必带合法
SHA-256、目录不带摘要，故本模块只做键级核对。

核对规则：

* 比较键为规范化路径的 ``casefold`` 折叠形式（见
  :mod:`app.normalize`），大小写/NFC/段尾点空格差异均被抹平。
* **单侧同键重复** → 整键列为歧义（:attr:`AuditStatus.AMBIGUOUS`），
  列出双方该键的全部成员，**不任意配对**；歧义键不参与其余任何核对
  （但其文件成员仍可作为另一侧后代的阻挡文件出现）。
* 其余键逐条核对：仅约定侧存在 → 缺失（MISSING）；仅交付侧存在 →
  额外（EXTRA）；两侧均有但类型不同 → 类型不符（TYPE_MISMATCH）；
  两侧均为文件但 SHA-256 不同 → 摘要不符（DIGEST_MISMATCH）；
  类型与摘要一致（目录不比较摘要）→ 一致（MATCHED）。
* **层级阻挡**：一侧的**文件**占据另一侧某条目比较键的祖先路径时，
  该后代条目录为层级阻挡（HIERARCHY_BLOCKED）并指明阻挡文件，而非
  普通缺失/额外；被阻挡条目不参与同键配对（另一侧该键按缺失/额外
  处理）。阻挡文件自身在其自身键上的核对结论保持不变。阻挡文件按
  祖先深度降序列出（最近的祖先在前）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .normalize import NormalizedEntry

AGREED_SIDE = "agreed"
DELIVERED_SIDE = "delivered"

FILE_TYPE = "file"
DIR_TYPE = "directory"


class ManifestSide(str, Enum):
    AGREED = "agreed"
    DELIVERED = "delivered"


class AuditStatus(str, Enum):
    MISSING = "MISSING"  # 约定清单有、交付清单无
    EXTRA = "EXTRA"  # 交付清单有、约定清单无
    TYPE_MISMATCH = "TYPE_MISMATCH"  # 同键两侧类型不符
    DIGEST_MISMATCH = "DIGEST_MISMATCH"  # 同键两侧文件 SHA-256 不符
    MATCHED = "MATCHED"  # 同键两侧类型与声明摘要一致
    HIERARCHY_BLOCKED = "HIERARCHY_BLOCKED"  # 祖先路径被另一侧文件占据
    AMBIGUOUS = "AMBIGUOUS"  # 单侧同键重复，整键歧义


@dataclass(frozen=True)
class AuditEntry:
    """单侧单条条目的核对视图（摘要仅文件有值）。"""

    side: str
    index: int  # 在原清单中的下标（从 1 开始）
    entry_type: str  # "file" | "directory"
    original_path: str
    canonical_path: str
    comparison_key: str
    sha256: str | None


@dataclass(frozen=True)
class PairFinding:
    """同键两侧配对核对结论（缺失/额外时另一侧为 None）。"""

    status: AuditStatus
    agreed: AuditEntry | None
    delivered: AuditEntry | None

    @property
    def comparison_key(self) -> str:
        present = self.agreed if self.agreed is not None else self.delivered
        assert present is not None
        return present.comparison_key


@dataclass(frozen=True)
class HierarchyBlocked:
    """单条被阻挡后代及其全部阻挡文件（祖先路径上的另一侧文件）。"""

    blocked: AuditEntry
    blockers: tuple[AuditEntry, ...]

    @property
    def comparison_key(self) -> str:
        return self.blocked.comparison_key


@dataclass(frozen=True)
class AmbiguousKey:
    """单侧（或双侧）同键重复的整键歧义记录。"""

    comparison_key: str
    agreed: tuple[AuditEntry, ...]
    delivered: tuple[AuditEntry, ...]


@dataclass
class AuditResult:
    agreed_entries: list[AuditEntry] = field(default_factory=list)
    delivered_entries: list[AuditEntry] = field(default_factory=list)
    missing: list[PairFinding] = field(default_factory=list)
    extra: list[PairFinding] = field(default_factory=list)
    type_mismatches: list[PairFinding] = field(default_factory=list)
    digest_mismatches: list[PairFinding] = field(default_factory=list)
    matched: list[PairFinding] = field(default_factory=list)
    hierarchy_blocked: list[HierarchyBlocked] = field(default_factory=list)
    ambiguous_keys: list[AmbiguousKey] = field(default_factory=list)


def _to_entries(
    side: str,
    normalized: list[NormalizedEntry],
    digests: dict[int, str],
) -> list[AuditEntry]:
    return [
        AuditEntry(
            side=side,
            index=entry.index,
            entry_type=entry.entry_type,
            original_path=entry.original_path,
            canonical_path=entry.canonical_path,
            comparison_key=entry.comparison_key,
            sha256=digests.get(entry.index),
        )
        for entry in normalized
    ]


def _group_by_key(
    entries: list[AuditEntry],
) -> dict[str, list[AuditEntry]]:
    grouped: dict[str, list[AuditEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.comparison_key, []).append(entry)
    return grouped


def _blockers_for(
    entry: AuditEntry, opposite_files: dict[str, list[AuditEntry]]
) -> tuple[AuditEntry, ...]:
    """返回占据 ``entry`` 祖先路径的另一侧文件，由近及远（祖先深度降序）、同深度按下标。"""
    parts = entry.comparison_key.split("/")
    found: list[tuple[int, int, AuditEntry]] = []
    for depth in range(1, len(parts)):  # 不含自身
        ancestor_key = "/".join(parts[:depth])
        for blocker in opposite_files.get(ancestor_key, ()):
            found.append((depth, blocker.index, blocker))
    # 最近祖先深度最大，故按深度降序；同一祖先深度可能有多个同键文件（歧义键），按下标。
    found.sort(key=lambda item: (-item[0], item[1]))
    return tuple(blocker for _, _, blocker in found)


def audit_manifests(
    agreed: list[NormalizedEntry],
    delivered: list[NormalizedEntry],
    agreed_digests: dict[int, str],
    delivered_digests: dict[int, str],
) -> AuditResult:
    """核对两份已通过 API 层校验的清单。

    ``digests`` 为条目下标到规范化（小写）SHA-256 的映射，仅文件条目有键。
    所有输出列表均稳定排序：配对类按比较键升序；层级阻挡按被阻挡后代比较键
    升序（同键时约定侧在前），阻挡文件按祖先深度降序（最近祖先在前）、
    同深度按下标；歧义键按比较键升序，成员保持原清单下标顺序。
    """
    agreed_entries = _to_entries(AGREED_SIDE, agreed, agreed_digests)
    delivered_entries = _to_entries(DELIVERED_SIDE, delivered, delivered_digests)

    agreed_map = _group_by_key(agreed_entries)
    delivered_map = _group_by_key(delivered_entries)

    # 两侧文件索引：歧义键成员同样参与层级阻挡（它们仍是一侧文件）。
    agreed_files = {
        key: [e for e in members if e.entry_type == FILE_TYPE]
        for key, members in agreed_map.items()
    }
    delivered_files = {
        key: [e for e in members if e.entry_type == FILE_TYPE]
        for key, members in delivered_map.items()
    }

    all_keys = set(agreed_map) | set(delivered_map)

    # 1) 单侧同键重复 → 整键歧义，不任意配对。
    ambiguous_keys = sorted(
        key
        for key in all_keys
        if len(agreed_map.get(key, ())) > 1
        or len(delivered_map.get(key, ())) > 1
    )
    ambiguous_set = set(ambiguous_keys)

    result = AuditResult(agreed_entries=agreed_entries, delivered_entries=delivered_entries)
    result.ambiguous_keys = [
        AmbiguousKey(
            comparison_key=key,
            agreed=tuple(agreed_map.get(key, ())),
            delivered=tuple(delivered_map.get(key, ())),
        )
        for key in ambiguous_keys
    ]

    pair_buckets: dict[AuditStatus, list[PairFinding]] = {
        AuditStatus.MISSING: result.missing,
        AuditStatus.EXTRA: result.extra,
        AuditStatus.TYPE_MISMATCH: result.type_mismatches,
        AuditStatus.DIGEST_MISMATCH: result.digest_mismatches,
        AuditStatus.MATCHED: result.matched,
    }

    # 2) 其余键：先判层级阻挡，再做同键配对。
    for key in sorted(all_keys - ambiguous_set):
        agreed_one = agreed_map.get(key, [None])[0]
        delivered_one = delivered_map.get(key, [None])[0]

        if agreed_one is not None:
            blockers = _blockers_for(agreed_one, delivered_files)
            if blockers:
                result.hierarchy_blocked.append(
                    HierarchyBlocked(blocked=agreed_one, blockers=blockers)
                )
                agreed_one = None  # 被阻挡条目不参与配对
        if delivered_one is not None:
            blockers = _blockers_for(delivered_one, agreed_files)
            if blockers:
                result.hierarchy_blocked.append(
                    HierarchyBlocked(blocked=delivered_one, blockers=blockers)
                )
                delivered_one = None

        if agreed_one is not None and delivered_one is not None:
            if agreed_one.entry_type != delivered_one.entry_type:
                status = AuditStatus.TYPE_MISMATCH
            elif (
                agreed_one.entry_type == FILE_TYPE
                and agreed_one.sha256 != delivered_one.sha256
            ):
                status = AuditStatus.DIGEST_MISMATCH
            else:
                status = AuditStatus.MATCHED
            pair_buckets[status].append(
                PairFinding(status=status, agreed=agreed_one, delivered=delivered_one)
            )
        elif agreed_one is not None:
            pair_buckets[AuditStatus.MISSING].append(
                PairFinding(status=AuditStatus.MISSING, agreed=agreed_one, delivered=None)
            )
        elif delivered_one is not None:
            pair_buckets[AuditStatus.EXTRA].append(
                PairFinding(status=AuditStatus.EXTRA, agreed=None, delivered=delivered_one)
            )
        # 两侧条目均被阻挡时只保留层级阻挡记录，不产生配对结论。

    return result
