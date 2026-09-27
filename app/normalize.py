"""Windows 目标规则下的压缩包路径规范化（纯内存、不访问文件系统）。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

# Windows 禁止在文件名中出现的字符（反斜杠会先被统一成分隔符，故不在此列）。
_INVALID_CHARS = frozenset('<>:"/|?*')

# Windows 保留设备名（不含尾随数字，COM/LPT 另按 1-9 匹配）。
_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "COM", "LPT"}
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:[\\/]|$)")


class IssueSeverity(str, Enum):
    ERROR = "error"
    INFO = "info"


class IssueCode(str, Enum):
    ABSOLUTE_PATH = "ABSOLUTE_PATH"
    UNC_PATH = "UNC_PATH"
    DRIVE_PATH = "DRIVE_PATH"
    TRAVERSAL_ESCAPE = "TRAVERSAL_ESCAPE"
    RESERVED_NAME = "RESERVED_NAME"
    INVALID_CHARACTER = "INVALID_CHARACTER"
    EMPTY_TARGET = "EMPTY_TARGET"
    EMPTY_SEGMENT = "EMPTY_SEGMENT"
    UNICODE_NORMALIZED = "UNICODE_NORMALIZED"
    SEGMENT_TRIMMED = "SEGMENT_TRIMMED"
    COLLISION = "COLLISION"
    BLOCKED_BY_FILE_ANCESTOR = "BLOCKED_BY_FILE_ANCESTOR"
    BLOCKS_DESCENDANT = "BLOCKS_DESCENDANT"
    LINK_TARGET_ESCAPE = "LINK_TARGET_ESCAPE"
    LINK_TARGET_INVALID = "LINK_TARGET_INVALID"
    LINK_CYCLE = "LINK_CYCLE"
    SYMLINK_ANCESTOR = "SYMLINK_ANCESTOR"
    FILE_SIZE_LIMIT_EXCEEDED = "FILE_SIZE_LIMIT_EXCEEDED"
    COMPRESSION_RATIO_EXCEEDED = "COMPRESSION_RATIO_EXCEEDED"


_ERROR_CODES = frozenset(
    {
        IssueCode.ABSOLUTE_PATH,
        IssueCode.UNC_PATH,
        IssueCode.DRIVE_PATH,
        IssueCode.TRAVERSAL_ESCAPE,
        IssueCode.RESERVED_NAME,
        IssueCode.INVALID_CHARACTER,
        IssueCode.EMPTY_TARGET,
        IssueCode.LINK_TARGET_ESCAPE,
        IssueCode.LINK_TARGET_INVALID,
        IssueCode.LINK_CYCLE,
        IssueCode.SYMLINK_ANCESTOR,
        IssueCode.FILE_SIZE_LIMIT_EXCEEDED,
        IssueCode.COMPRESSION_RATIO_EXCEEDED,
    }
)


@dataclass(frozen=True)
class Issue:
    code: IssueCode
    severity: IssueSeverity
    message: str
    segment: int | None = None
    related_index: int | None = None


@dataclass
class NormalizedEntry:
    index: int
    entry_type: str  # "file" | "directory"
    original_path: str
    canonical_parts: list[str] = field(default_factory=list)
    key_parts: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def canonical_path(self) -> str:
        return "/".join(self.canonical_parts)

    @property
    def comparison_key(self) -> str | None:
        return "/".join(self.key_parts) if self.key_parts else None

    @property
    def unsafe(self) -> bool:
        return any(i.severity == IssueSeverity.ERROR for i in self.issues)


def _is_reserved(stem: str) -> bool:
    stem = stem.strip().upper()
    if not stem:
        return False
    if stem in _RESERVED_STEMS:
        return True
    if len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3].isdigit():
        return stem[3] != "0"  # 文档明确保留的是 1-9
    return False


def normalize_entry(
    index: int,
    entry_type: str,
    original_path: str,
    initial_parts: tuple[str, ...] = (),
) -> NormalizedEntry:
    """按 Windows 目标规则规范化单条条目路径。

    全部处理均为词法/内存计算，不读取文件系统。

    ``initial_parts`` 非空时作为解析基准目录（用于符号链接目标相对链接所在
    目录解析）：``..`` 先弹出基准段，基准弹空后再弹才算越界；此时结果为空
    表示解析到目标根本身，不视为 ``EMPTY_TARGET``。
    """
    result = NormalizedEntry(
        index=index, entry_type=entry_type, original_path=original_path
    )

    def err(code: IssueCode, message: str, segment: int | None = None) -> None:
        result.issues.append(Issue(code, IssueSeverity.ERROR, message, segment))

    def info(
        code: IssueCode,
        message: str,
        segment: int | None = None,
        related_index: int | None = None,
    ) -> None:
        result.issues.append(
            Issue(code, IssueSeverity.INFO, message, segment, related_index)
        )

    # 1) 统一分隔符：\ 和 / 都视为路径分隔符。
    unified = original_path.replace("\\", "/")

    # 2) 识别绝对/UNC/盘符路径（仅依据原始词法形态判定）。
    is_unc = unified.startswith("//")
    if is_unc:
        err(
            IssueCode.UNC_PATH,
            "UNC 网络路径（以 \\\\ 或 // 开头）不允许解包到目标目录",
        )
    elif unified.startswith("/"):
        err(IssueCode.ABSOLUTE_PATH, "绝对路径不允许解包到目标目录")

    drive = _DRIVE_RE.match(unified)
    is_drive = drive is not None
    if is_drive:
        drive_letter = unified[0].upper()
        err(
            IssueCode.DRIVE_PATH,
            f"盘符路径（{drive_letter}:）不允许解包到目标目录",
        )
        # 去掉盘符前缀（含其后的分隔符），剩余部分仅用于尽力规范化输出。
        unified = unified[drive.end():]

    # 3) 切段。前导分隔符产生的空段忽略；内部/结尾空段属于折叠分隔符。
    raw_segments = unified.split("/")
    stack: list[str] = list(initial_parts)
    collapsed = False

    for seg_index, raw_seg in enumerate(raw_segments, start=1):
        if raw_seg == "":
            # UNC 的主机前导 // 与绝对路径的前导 / 不在此重复报告。
            if seg_index > (2 if is_unc else 1):
                collapsed = True
            continue

        # 4) 词法遍历：. 原地；.. 弹栈；越过根则 unsafe。
        if raw_seg == ".":
            continue
        if raw_seg == "..":
            if stack:
                stack.pop()
            else:
                err(
                    IssueCode.TRAVERSAL_ESCAPE,
                    "路径包含越界的 ..，会逃逸出目标根目录",
                    seg_index,
                )
            continue

        # 5) Unicode NFC。
        nfc = unicodedata.normalize("NFC", raw_seg)
        nfc_changed = nfc != raw_seg

        # 6) 段尾空格/句点裁剪（Windows 会忽略末尾的点和空格）。
        trimmed = nfc.rstrip(" .")
        trim_changed = trimmed != nfc

        # 7) 非法字符与保留设备名（基于裁剪后的段判定）。
        lower_seg = trimmed.casefold()
        reserved_stem = lower_seg.split(".", 1)[0]
        if _is_reserved(reserved_stem):
            err(
                IssueCode.RESERVED_NAME,
                f"路径段 {trimmed!r} 命中 Windows 保留设备名（如 CON、PRN、NUL、COM1-9、LPT1-9）",
                seg_index,
            )

        invalid = {
            ch for ch in trimmed if ch in _INVALID_CHARS or ord(ch) < 32
        }
        if invalid:
            shown = " ".join(sorted(invalid))
            err(
                IssueCode.INVALID_CHARACTER,
                f"路径段 {trimmed!r} 含 Windows 非法字符: {shown}",
                seg_index,
            )

        if not trimmed:
            err(
                IssueCode.EMPTY_SEGMENT,
                "路径段经段尾空格/句点裁剪后为空，构成非法文件名",
                seg_index,
            )
            continue

        if nfc_changed:
            info(
                IssueCode.UNICODE_NORMALIZED,
                f"路径段 {raw_seg!r} 已规范化为 Unicode NFC",
                seg_index,
            )
        if trim_changed:
            info(
                IssueCode.SEGMENT_TRIMMED,
                f"路径段 {nfc!r} 的段尾空格/句点在 Windows 上会被忽略，已裁剪为 {trimmed!r}",
                seg_index,
            )

        stack.append(trimmed)

    if collapsed:
        info(
            IssueCode.EMPTY_SEGMENT,
            "路径中的重复/结尾分隔符已折叠（连续分隔符或结尾分隔符不产生额外目录段）",
        )

    result.canonical_parts = stack
    result.key_parts = [p.casefold() for p in stack]

    if not stack and not initial_parts:
        err(
            IssueCode.EMPTY_TARGET,
            "规范化后路径为空（无任何有效段），无法解包为有效目标",
        )

    return result


# 供测试与 API 层使用的便捷导出。
ERROR_CODES = _ERROR_CODES
