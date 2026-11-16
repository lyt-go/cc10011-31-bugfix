"""数据服务接口：维护服务接口契约，覆盖评审、上线、下线与契约升级。

静态路径（/export）写在 /{entry_id} 之前，避免导出被当成 id 解析。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dataservice import DataserviceService

router = APIRouter(prefix="/api/dataservice", tags=["数据服务"])

service = DataserviceService()

LIST_FIELDS = ["服务编号", "服务名称", "接口路径", "查询参数", "返回字段", "调用频次限制", "缓存策略", "服务版本"]
STATUSES = ["草稿", "服务评审中", "已上线", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按服务编号或服务名称检索"),
    status: str | None = Query(default=None, description="草稿、服务评审中、已上线、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按服务编号与状态过滤数据服务列表。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据服务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dataservice", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条服务接口明细，含当前契约与契约历史。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"服务接口 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/contracts", response_model=dict)
def list_contracts(entry_id: int) -> dict[str, Any]:
    """读取服务的契约版本历史，供图表模板核对绑定版本。"""
    versions, error = service.contract_versions(entry_id)
    if error:
        raise HTTPException(status_code=404, detail=error)
    return {"items": versions, "total": len(versions)}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条服务接口，同时建立 v1 字段契约。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="服务接口已登记", entry=entry)


@router.put("/{entry_id}/contract", response_model=ActionResult)
def update_contract(entry_id: int, payload: EntryPayload) -> ActionResult:
    """升级返回字段/字段别名：生成新契约版本并同步下游图表模板绑定。"""
    entry, message = service.update_contract(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交服务评审、上线服务、下线服务；并发上线只保留一个有效契约版本。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
