"""跨平台压缩包路径预检 API。

仅对路径做内存中的词法分析，绝不访问文件系统。
"""

from __future__ import annotations

from fastapi import FastAPI

from .schemas import (
    DiskPeakPreviewRequest,
    DiskPeakPreviewResponse,
    HealthResponse,
    ManifestAuditRequest,
    ManifestAuditResponse,
    MirrorPreviewRequest,
    MirrorPreviewResponse,
    OverlayPreviewRequest,
    OverlayPreviewResponse,
    PreflightRepairResponse,
    PreflightRequest,
    RelocationPreviewRequest,
    RelocationPreviewResponse,
    RollbackPreviewRequest,
    RollbackPreviewResponse,
)
from .service import (
    run_disk_peak_preview,
    run_manifest_audit,
    run_mirror_preview,
    run_overlay_preview,
    run_preflight,
    run_relocation_preview,
    run_rollback_preview,
)

app = FastAPI(
    title="Archive Path Preflight API",
    description=(
        "压缩包路径预检：按 Windows 目标规则规范化路径、识别保留设备名，"
        "检测同名碰撞与文件-目录层级冲突；符号链接风险分析（目标越界、"
        "链接链循环、符号链接祖先路径劫持）；可选的解压资源预算校验"
        "（单文件解压大小、整批解压总量、最大压缩比）；可选的 selection_rules"
        " 在预检前按规范化路径 glob 筛选实际解包条目；目标目录覆盖预演"
        "（按现存清单与解包顺序干跑 CREATE/OVERWRITE/REUSE_DIRECTORY/BLOCKED）；"
        "目录镜像同步预演（把现存布局同步为期望归档布局，纯内存生成"
        " DELETE/KEEP_DIRECTORY/CREATE_DIRECTORY/OVERWRITE_FILE/CREATE_FILE 操作序列）；"
        "清单核对（约定清单 vs 交付清单：缺失、额外、类型不符、摘要不符、"
        "一致、层级阻挡与同键歧义，仅比较声明 SHA-256）；路径重定位预演"
        "（按源前缀→目标前缀规则改写条目路径，最长源前缀优先、同长度取最后一条，"
        "并预检结果中的同键重复与文件祖先冲突）；前缀解包回退预演"
        "（仅模拟按序解包清单的前 stop_after 条，给出执行前需备份的现存文件与"
        "可还原该前缀变化的有序回退步骤：删新建文件、恢复被覆盖文件、由深到浅"
        "删除新建目录）；磁盘暂存峰值预演（给定现存清单、按序解包清单与初始"
        "可用字节数，模拟同盘备份一次、先暂存新文件再替换释放旧占用的空间曲线，"
        "逐条给出暂存峰值与提交后新增占用、整批峰值及首次超额阶段与条目，"
        "超额也继续预演）。"
        "纯内存计算，不访问文件系统。"
    ),
    version="1.9.0",
)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="archive-path-preflight")


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {
        "service": "archive-path-preflight",
        "docs": "/docs",
        "health": "/health",
        "preflight": "POST /api/v1/preflight",
        "overlay_preview": "POST /api/v1/overlay-preview",
        "mirror_preview": "POST /api/v1/mirror-preview",
        "manifest_audit": "POST /api/v1/manifest-audit",
        "relocation_preview": "POST /api/v1/relocation-preview",
        "rollback_preview": "POST /api/v1/rollback-preview",
        "disk_peak_preview": "POST /api/v1/disk-peak-preview",
    }


@app.post(
    "/api/v1/preflight",
    response_model=None,  # repair=false 时响应不含 repair_plan；由 service 层返回具体模型
    responses={200: {"model": PreflightRepairResponse}},  # 仅供 OpenAPI 文档展示超集
    tags=["preflight"],
    summary="批量预检压缩包条目路径（repair=true 时附修复计划）",
)
def preflight(request: PreflightRequest):
    return run_preflight(request)


@app.post(
    "/api/v1/overlay-preview",
    response_model=OverlayPreviewResponse,
    tags=["overlay"],
    summary="目标目录覆盖预演：按现存清单与解包顺序干跑（纯内存）",
)
def overlay_preview(request: OverlayPreviewRequest) -> OverlayPreviewResponse:
    return run_overlay_preview(request)


@app.post(
    "/api/v1/disk-peak-preview",
    response_model=DiskPeakPreviewResponse,
    tags=["overlay"],
    summary=(
        "磁盘暂存峰值预演：按现存清单、按序解包清单与初始可用字节数模拟"
        "同盘备份/暂存/替换的占用曲线（纯内存，超额也继续预演）"
    ),
)
def disk_peak_preview(request: DiskPeakPreviewRequest) -> DiskPeakPreviewResponse:
    return run_disk_peak_preview(request)


@app.post(
    "/api/v1/mirror-preview",
    response_model=MirrorPreviewResponse,
    tags=["overlay"],
    summary="目录镜像同步预演：生成把现存布局同步为期望布局的操作序列（纯内存）",
)
def mirror_preview(request: MirrorPreviewRequest) -> MirrorPreviewResponse:
    return run_mirror_preview(request)


@app.post(
    "/api/v1/manifest-audit",
    response_model=ManifestAuditResponse,
    tags=["audit"],
    summary="清单核对：约定清单 vs 交付清单（缺失/额外/类型/摘要/层级阻挡/歧义，仅比较声明摘要）",
)
def manifest_audit(request: ManifestAuditRequest) -> ManifestAuditResponse:
    return run_manifest_audit(request)


@app.post(
    "/api/v1/relocation-preview",
    response_model=RelocationPreviewResponse,
    tags=["relocation"],
    summary="路径重定位预演：按源前缀→目标前缀规则改写路径并预检结果冲突（纯内存）",
)
def relocation_preview(request: RelocationPreviewRequest) -> RelocationPreviewResponse:
    return run_relocation_preview(request)


@app.post(
    "/api/v1/rollback-preview",
    response_model=RollbackPreviewResponse,
    tags=["overlay"],
    summary="前缀解包回退预演：仅模拟前 stop_after 条并生成备份清单与有序回退步骤（纯内存）",
)
def rollback_preview(request: RollbackPreviewRequest) -> RollbackPreviewResponse:
    return run_rollback_preview(request)
