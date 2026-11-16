"""可视化模板接口：模板配置、绑定核对、快照、渲染、导出与回填。

注意路由顺序：`/export`、`/backfill` 这类静态路径必须写在 `/{entry_id}` 之前，
否则会被当成 entry_id 解析，导出入口直接 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.viztemplate import ViztemplateService

router = APIRouter(prefix="/api/viztemplate", tags=["可视化模板"])

service = ViztemplateService()

LIST_FIELDS = ["模板编号", "图表类型", "数据绑定", "颜色方案", "坐标轴配置", "图例位置", "交互行为", "导出格式"]
STATUSES = ["草稿", "模板评审中", "已发布", "已废弃"]
CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "tsv": "text/tab-separated-values; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "ndjson": "application/x-ndjson; charset=utf-8",
}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号或图表类型检索"),
    status: str | None = Query(default=None, description="草稿、模板评审中、已发布、已废弃"),
    binding_status: str | None = Query(default=None, description="按绑定状态过滤：可用、冲突、不可用、待核对"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号、状态与绑定状态过滤列表；列表和详情使用同一份绑定解析。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, binding_status=binding_status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """导出可视化模板清单（配置清单，不是图表数据）。"""
    items, total = service.list_entries(keyword=keyword, status=status, page=1, size=10000)
    return {"module": "viztemplate", "total": total, "items": items}


@router.get("/backfill/{batch_id}", response_model=dict)
def get_backfill_batch(batch_id: str) -> dict[str, Any]:
    """读取回填批次进度；批次不存在时返回空结果而不是 500。"""
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"回填批次 {batch_id} 不存在")
    return batch


@router.post("/backfill", response_model=ActionResult)
def run_backfill(payload: EntryPayload) -> ActionResult:
    """按批回填图表模板快照：同一 batch_id 幂等，中断后从上一完整条目继续。"""
    batch, message = service.backfill(payload.values)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条图表模板明细，同时返回绑定解析、快照版本和核对结果。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"图表模板 {entry_id} 不存在或已归档")
    snapshots, _ = service.list_snapshots(entry_id)
    entry["快照清单"] = [
        {
            "快照编号": item.get("快照编号"),
            "快照版本": item.get("快照版本"),
            "模板版本": item.get("模板版本"),
            "有效": item.get("有效"),
            "契约版本": item.get("契约版本"),
            "列顺序": item.get("列顺序"),
            "生成时间": item.get("生成时间"),
        }
        for item in snapshots
    ]
    entry["核对"] = service.verify_snapshot(service.get_row(entry_id) or {})
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条图表模板：样式、交互、导出格式与数据绑定一起持久化。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="图表模板已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改模板配置；已发布模板的修改会原子生成新快照，已废弃模板只读。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交模板评审、发布模板、废弃模板；发布前核对绑定字段。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/sync-binding", response_model=ActionResult)
def sync_binding(entry_id: int) -> ActionResult:
    """数据服务契约变化后，从另一个入口同步模板绑定版本与快照。"""
    entry, message = service.sync_binding(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/preview", response_model=dict)
def preview(entry_id: int, version: int | None = Query(default=None, description="读取指定历史快照")) -> dict[str, Any]:
    """页面预览：读取模板配置并渲染数据图表，与导出共用同一份快照。"""
    payload, error = service.preview(entry_id, version)
    if payload is None:
        raise HTTPException(status_code=400, detail=error)
    return payload


@router.get("/{entry_id}/snapshots", response_model=dict)
def list_snapshots(entry_id: int) -> dict[str, Any]:
    """图表模板快照列表；历史快照保持可读，新记录按当前规则写入。"""
    snapshots, error = service.list_snapshots(entry_id)
    if error:
        raise HTTPException(status_code=404, detail=error)
    return {"items": snapshots, "total": len(snapshots)}


@router.post("/{entry_id}/snapshots", response_model=ActionResult)
def create_snapshot(entry_id: int) -> ActionResult:
    """手动生成一份图表模板数据快照（草稿联调时使用）。"""
    row = service.get_row(entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"图表模板 {entry_id} 不存在或已归档")
    entry, message = service.create_snapshot(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/snapshots/{version}", response_model=dict)
def get_snapshot(entry_id: int, version: int) -> dict[str, Any]:
    snapshot = service.snapshot(entry_id, version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"图表模板 {entry_id} 不存在快照 v{version}")
    return snapshot


@router.get("/{entry_id}/export")
def export_chart_data(entry_id: int, format: str | None = Query(default=None)) -> Response:
    """导出图表数据文件；列顺序严格取图表快照，与页面预览一致。"""
    payload, error = service.export_data(entry_id, format)
    if payload is None:
        raise HTTPException(status_code=400, detail=error)
    headers = {
        "Content-Disposition": f'attachment; filename="{payload["文件名"]}"',
        "X-Snapshot-Code": str(payload["快照编号"] or ""),
        "X-Contract-Version": str(payload["契约版本"] or ""),
        "X-Contract-Hash": str(payload["契约哈希"] or ""),
        "X-Binding-Fingerprint": str(payload["绑定指纹"] or ""),
        "X-Style-Fingerprint": str(payload["样式指纹"] or ""),
        "X-Content-SHA256": str(payload["内容指纹"] or ""),
    }
    return PlainTextResponse(
        payload["内容"],
        media_type=CONTENT_TYPES.get(payload["格式"], "text/plain; charset=utf-8"),
        headers=headers,
    )


@router.get("/{entry_id}/bundle")
def download_bundle(entry_id: int, archive: bool = Query(default=False)) -> Response:
    """下载图表包（manifest + snapshot + data.csv）；archive=true 时返回 zip。"""
    if archive:
        content, filename, error = service.bundle_zip(entry_id)
        if content is None:
            raise HTTPException(status_code=400, detail=error)
        return Response(
            content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    bundle, error = service.bundle(entry_id)
    if bundle is None:
        raise HTTPException(status_code=400, detail=error)
    import json

    return PlainTextResponse(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{bundle["文件名"]}"'},
    )


@router.post("/{entry_id}/verify", response_model=dict)
def verify(entry_id: int, payload: EntryPayload | None = None) -> dict[str, Any]:
    """重新载入时核对快照：模板配置、配色、绑定契约、列顺序与导出文件是否一致。"""
    row = service.get_row(entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"图表模板 {entry_id} 不存在或已归档")
    version = (payload.values.get("version") if payload else None) or None
    snapshot = service.snapshot(entry_id, int(version)) if version is not None else None
    result = service.verify_snapshot(row, snapshot)
    try:
        expected_snapshot = snapshot or service.ensure_snapshot(row)
    except Exception:  # noqa: BLE001 - 绑定不可用时下面按导出结果说明
        expected_snapshot = None
    exported, error = service.export_data(entry_id)
    if exported is None:
        result["导出核对"] = {"一致": False, "说明": error}
    else:
        expected = expected_snapshot or {}
        result["导出核对"] = {
            "一致": (
                list(exported["列顺序"]) == list(expected.get("列顺序", []))
                and exported.get("契约版本") == expected.get("契约版本")
                and exported.get("契约哈希") == expected.get("契约哈希")
            ),
            "导出列顺序": list(exported["列顺序"]),
            "导出契约版本": exported["契约版本"],
            "导出契约哈希": exported["契约哈希"],
            "导出快照编号": exported["快照编号"],
        }
    return result
