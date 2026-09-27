"""按规范化后的归档路径筛选实际解包条目的 ``selection_rules``（纯内存）。

规则语义：

* 每条规则由动作（``include`` / ``exclude``）与 glob ``pattern`` 组成，
  按输入顺序依次匹配，**最后一条命中的规则生效**；没有任何规则命中时
  默认 ``include``。
* glob 按路径段解释（分隔符固定为 ``/``，匹配目标为规范化后的归档路径）：
  ``*`` 匹配单段内任意数量（含 0）的非 ``/`` 字符，**不跨路径段**；
  ``?`` 匹配单段内恰好一个非 ``/`` 字符；``**`` 必须独占一个路径段，
  匹配零或任意多个完整路径段。
* 匹配区分大小写，基于 NFC 规范化后保留原始大小写的展示路径；模式本身
  不做 Unicode 规范化或大小写折叠（需要同时覆盖大小写变体时请显式列出）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class SelectionAction(str, Enum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


@dataclass(frozen=True)
class SelectionDecision:
    """单条目的筛选结果。"""

    selected: bool
    # 最后一条命中规则的下标（从 0 开始，与请求 selection_rules 下标一致）；
    # 没有任何规则命中（默认 include）时为 None。
    matched_rule_index: int | None


def validate_pattern(pattern: str) -> None:
    """校验 glob 模式合法性；非法时抛出 ``ValueError``（API 层映射为 422）。"""
    if not isinstance(pattern, str) or pattern == "":
        raise ValueError("selection_rules[].pattern 不能为空")
    for seg in pattern.split("/"):
        if seg == "":
            raise ValueError(
                f"selection_rules 规则 {pattern!r} 非法：路径段不能为空"
                "（不得以 / 开头或结尾，也不得出现连续的 //）"
            )
        if "**" in seg and seg != "**":
            raise ValueError(
                f"selection_rules 规则 {pattern!r} 非法：** 必须独占一个路径段"
                "（如 **/*.tmp、a/**/b），不得与其他字符出现在同一段内"
            )


def _compile_segment(seg: str) -> re.Pattern[str]:
    """把单段 glob（不含 **）编译为正则：* → [^/]*，? → [^/]，其余转义。"""
    parts: list[str] = []
    for ch in seg:
        if ch == "*":
            parts.append("[^/]*")
        elif ch == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts))


def _compile_rule(
    index: int, action: str, pattern: str
) -> tuple[int, SelectionAction, tuple[re.Pattern[str] | None, ...]]:
    validate_pattern(pattern)
    # None 元素代表独占一段的 **。
    segments: tuple[re.Pattern[str] | None, ...] = tuple(
        None if seg == "**" else _compile_segment(seg)
        for seg in pattern.split("/")
    )
    return index, SelectionAction(action), segments


def _rule_matches(
    segments: tuple[re.Pattern[str] | None, ...], parts: Sequence[str]
) -> bool:
    """回溯匹配：``segments`` 为逐段正则（None 表示 **），``parts`` 为规范路径段。

    位于模式末尾的 ``**`` 至少吞掉一个路径段（``tmp/**`` 表示 tmp 目录下
    *至少一层*的内容，不匹配 ``tmp`` 自身）；位于模式中间的 ``**`` 可匹配
    零段（``a/**/b`` 可匹配 ``a/b``）。
    """

    def rec(rule_pos: int, path_pos: int) -> bool:
        if rule_pos == len(segments):
            return path_pos == len(parts)
        segment = segments[rule_pos]
        if segment is None:  # **：吞掉 min_segments 至剩余全部路径段
            trailing = rule_pos == len(segments) - 1
            min_segments = 1 if trailing else 0
            return any(
                rec(rule_pos + 1, consumed)
                for consumed in range(
                    path_pos + min_segments, len(parts) + 1
                )
            )
        if path_pos == len(parts):
            return False
        return (
            segment.fullmatch(parts[path_pos]) is not None
            and rec(rule_pos + 1, path_pos + 1)
        )

    return rec(0, 0)


def apply_selection_rules(
    canonical_parts: Sequence[Sequence[str]],
    rules: Sequence[tuple[str, str]] | None,
) -> list[SelectionDecision]:
    """对一批规范化路径段应用筛选规则。

    ``rules`` 为按输入顺序排列的 ``(action, pattern)`` 序列；为 None/空时
    全部条目默认选中（保持无 selection_rules 时的行为）。
    """
    if not rules:
        return [SelectionDecision(True, None) for _ in canonical_parts]

    compiled = [
        _compile_rule(i, action, pattern)
        for i, (action, pattern) in enumerate(rules)
    ]

    decisions: list[SelectionDecision] = []
    for parts in canonical_parts:
        selected = True
        matched: int | None = None
        for index, action, segments in compiled:  # 最后一条命中规则生效
            if _rule_matches(segments, parts):
                selected = SelectionAction(action) is SelectionAction.INCLUDE
                matched = index
        decisions.append(SelectionDecision(selected, matched))
    return decisions
