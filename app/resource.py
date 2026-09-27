"""解压资源预算校验：单文件解压大小、整批解压总量与最大压缩比（纯内存）。

仅在请求携带 ``resource_limits`` 时启用对应限制；全部判定基于条目声明的
字节数，不访问文件系统。压缩比用整数交叉乘法判定，避免浮点误差：

* ``compressed`` 为 0 且 ``uncompressed`` 大于 0 时视为超限；
* 二者均为 0 时通过；
* 其余情形等价于 ``uncompressed > max_ratio * compressed``（整数运算）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .normalize import Issue, IssueCode, IssueSeverity, NormalizedEntry

FILE_TYPE = "file"


@dataclass(frozen=True)
class DeclaredSizes:
    """单个文件条目声明的字节数。"""

    compressed: int | None
    uncompressed: int | None


@dataclass(frozen=True)
class ResourceOutcome:
    """整批资源预算校验结果。"""

    declared_compressed_bytes: int  # 批次声明的压缩字节总数
    declared_uncompressed_bytes: int  # 批次声明的解压字节总数
    exceeded: bool  # 任一启用的限制被超出（含整批总量）


def ratio_exceeded(uncompressed: int, compressed: int, max_ratio: int) -> bool:
    """整数交叉乘法判定解压/压缩比是否超过 ``max_ratio``。

    等价于 ``uncompressed / compressed > max_ratio``，但只做整数乘法：
    ``uncompressed > max_ratio * compressed``。``compressed`` 为 0 且
    ``uncompressed`` 大于 0 时视为超限；二者均为 0 时通过。
    """
    if compressed == 0:
        return uncompressed > 0
    return uncompressed > max_ratio * compressed


def check_resource_budgets(
    entries: list[NormalizedEntry],
    declared: dict[int, DeclaredSizes],
    *,
    max_file_uncompressed_bytes: int | None = None,
    max_total_uncompressed_bytes: int | None = None,
    max_compression_ratio: int | None = None,
) -> ResourceOutcome:
    """对整批条目做资源预算校验，向超限文件条目追加 error 级问题。

    副作用：超限文件条目被追加 ``FILE_SIZE_LIMIT_EXCEEDED`` /
    ``COMPRESSION_RATIO_EXCEEDED`` 问题（error，直接影响 ``safe`` 判定，
    并随之退出碰撞分组、层级冲突与 repair 复检）。整批总量超限不归属单个
    条目，仅体现在返回的 ``exceeded`` 中。
    """
    declared_compressed = 0
    declared_uncompressed = 0
    exceeded = False

    for entry in entries:
        sizes = declared.get(entry.index)
        if sizes is None or entry.entry_type != FILE_TYPE:
            continue
        if sizes.compressed is not None:
            declared_compressed += sizes.compressed
        if sizes.uncompressed is not None:
            declared_uncompressed += sizes.uncompressed

        if (
            max_file_uncompressed_bytes is not None
            and sizes.uncompressed is not None
            and sizes.uncompressed > max_file_uncompressed_bytes
        ):
            exceeded = True
            entry.issues.append(
                Issue(
                    IssueCode.FILE_SIZE_LIMIT_EXCEEDED,
                    IssueSeverity.ERROR,
                    f"声明解压大小 {sizes.uncompressed} 字节超过单文件解压上限 "
                    f"{max_file_uncompressed_bytes} 字节",
                )
            )

        if (
            max_compression_ratio is not None
            and sizes.compressed is not None
            and sizes.uncompressed is not None
            and ratio_exceeded(
                sizes.uncompressed, sizes.compressed, max_compression_ratio
            )
        ):
            exceeded = True
            if sizes.compressed == 0:
                message = (
                    f"声明压缩大小为 0 字节但解压大小为 {sizes.uncompressed} "
                    f"字节，视为超过最大压缩比 {max_compression_ratio}"
                )
            else:
                message = (
                    f"声明压缩比 {sizes.uncompressed}/{sizes.compressed} "
                    f"超过最大压缩比 {max_compression_ratio}"
                )
            entry.issues.append(
                Issue(
                    IssueCode.COMPRESSION_RATIO_EXCEEDED,
                    IssueSeverity.ERROR,
                    message,
                )
            )

    if (
        max_total_uncompressed_bytes is not None
        and declared_uncompressed > max_total_uncompressed_bytes
    ):
        exceeded = True

    return ResourceOutcome(
        declared_compressed_bytes=declared_compressed,
        declared_uncompressed_bytes=declared_uncompressed,
        exceeded=exceeded,
    )
