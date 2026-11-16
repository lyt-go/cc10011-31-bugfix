"""图表数据渲染：按绑定解析出的列顺序生成数据与图表 payload。

数据源目前是确定性的示例数据（同一个服务/契约版本/列顺序永远得到同一批数据），
这样页面预览、图表快照和导出文件可以逐字节对齐，也便于重新载入时核对快照。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

CATEGORY_VALUES = ["华东", "华南", "华北", "西南", "东北"]
SERIES_VALUES = ["线上", "线下"]
CATEGORICAL_TOKENS = (
    "日期", "时间", "名称", "地区", "区域", "渠道", "维度", "分组", "类别", "省份", "城市",
    "date", "day", "time", "name", "region", "channel", "dimension", "category", "group",
)


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], 16)


def _value(seed: int, row_index: int, column_index: int) -> int:
    mixed = (seed + (row_index + 1) * 7919 + (column_index + 1) * 104729) % 10000
    return 100 + mixed % 900


def _looks_numeric_field(name: str) -> bool:
    lowered = name.lower()
    return not any(token in lowered for token in CATEGORICAL_TOKENS)


def _looks_rate_field(name: str) -> bool:
    lowered = name.lower()
    return "率" in lowered or "rate" in lowered or "ratio" in lowered or "percent" in lowered


def render_data(resolved: dict[str, Any]) -> dict[str, Any]:
    """按解析结果生成 {columns, rows, row_count}；columns 与页面/导出共用。"""
    contract = resolved.get("contract") or {}
    columns = list(resolved.get("columns") or [])
    service_no = str((resolved.get("service") or {}).get("服务编号") or "SERVICE")
    seed = _seed(f"{service_no}:{contract.get('契约版本')}:{contract.get('契约哈希')}")
    if not columns:
        return {"columns": [], "rows": [], "row_count": 0}

    x_field = resolved.get("x")
    series_field = resolved.get("series")
    grouped: dict[str, Any] = {}
    for item in resolved.get("filters") or []:
        grouped.update(item)

    row_count = 12
    categories = [CATEGORY_VALUES[index % len(CATEGORY_VALUES)] for index in range(row_count)]
    rows: list[dict[str, Any]] = []
    for row_index, category in enumerate(categories):
        row: dict[str, Any] = {}
        for column_index, column in enumerate(columns):
            lowered = column.lower()
            if column == x_field or any(token in lowered for token in ("日期", "时间", "date", "day", "time")):
                row[column] = f"2026-11-{row_index + 1:02d}"
            elif column in ("地区", "区域", "region", "维度", "dimension"):
                row[column] = category
            elif column == series_field or column in ("渠道", "系列", "channel", "series"):
                row[column] = SERIES_VALUES[row_index % len(SERIES_VALUES)]
            elif _looks_numeric_field(column):
                base = _value(seed, row_index, column_index)
                if _looks_rate_field(column):
                    row[column] = round((base % 1000) / 1000, 4)
                else:
                    row[column] = base
            else:
                row[column] = f"{column}-{category}"
        if x_field and x_field in row:
            row["__x__"] = row[x_field]
        if series_field and series_field in row:
            row["__series__"] = row[series_field]
        rows.append(row)
        if grouped:
            row.update({str(k): v for k, v in grouped.items() if str(k) in columns})
    return {"columns": columns, "rows": rows, "row_count": len(rows)}


def build_chart(style: dict[str, Any], resolved: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """给前端的渲染描述：颜色、坐标轴、图例、交互都来自模板快照。"""
    columns = data["columns"]
    x = resolved.get("x") or (columns[0] if columns else None)
    y_candidates = [item for item in resolved.get("y") or [] if item in columns]
    palette = list((style.get("颜色方案") or {}).get("调色板") or [])
    field_colors = dict((style.get("颜色方案") or {}).get("字段配色") or {})
    series_colors: dict[str, str] = {}
    for index, column in enumerate(y_candidates or columns):
        color = field_colors.get(column) or (palette[index % len(palette)] if palette else "#1f6feb")
        series_colors[column] = color
    if not y_candidates:
        y_candidates = [column for column in columns if column != x]
    axes = copy.deepcopy(style.get("坐标轴配置") or {})
    return {
        "图表类型": None,
        "维度": x,
        "度量": y_candidates,
        "系列": resolved.get("series"),
        "颜色": series_colors,
        "坐标轴": axes,
        "图例位置": style.get("图例位置"),
        "交互行为": copy.deepcopy(style.get("交互行为") or {}),
        "导出格式": style.get("导出格式"),
        "数据列": columns,
    }


def payload_fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
