"""图表样式配置：颜色、坐标轴、图例、交互与导出格式的归一化。

模板行里保留用户提交的原始结构（不做裁剪），渲染时再从这里取归一化视图。
归一化和指纹计算放在同一处，保证“页面预览 / 图表快照 / 导出文件”读到的是同一份样式。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

DEFAULT_PALETTE = ["#1f6feb", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2"]

STYLE_FIELDS = ["颜色方案", "坐标轴配置", "图例位置", "交互行为", "导出格式"]

# 中文名是写入仓库时的主字段，这些别名兼容前端或外部调用习惯。
INPUT_ALIASES = {
    "template_no": "模板编号",
    "code": "模板编号",
    "template_id": "模板编号",
    "chart_type": "图表类型",
    "type": "图表类型",
    "binding": "数据绑定",
    "data_binding": "数据绑定",
    "color_scheme": "颜色方案",
    "colors": "颜色方案",
    "palette": "颜色方案",
    "axes": "坐标轴配置",
    "axis": "坐标轴配置",
    "axis_config": "坐标轴配置",
    "legend": "图例位置",
    "legend_position": "图例位置",
    "interactions": "交互行为",
    "interaction": "交互行为",
    "export_format": "导出格式",
    "export_type": "导出格式",
    "style": "样式",
    "remark": "备注",
}

LEGEND_ALIASES = {
    "top": "顶部",
    "bottom": "底部",
    "left": "左侧",
    "right": "右侧",
    "none": "隐藏",
    "hidden": "隐藏",
    "顶部": "顶部",
    "底部": "底部",
    "左侧": "左侧",
    "右侧": "右侧",
    "隐藏": "隐藏",
}

EXPORT_ALIASES = {
    "csv": "csv",
    "text/csv": "csv",
    "json": "json",
    "application/json": "json",
    "tsv": "tsv",
    "xlsx": "xlsx",
    "excel": "xlsx",
    "xls": "xlsx",
    "ndjson": "ndjson",
}

DEFAULT_INTERACTIONS = {
    "提示框": True,
    "缩放": True,
    "下钻": False,
    "图例筛选": True,
    "高亮联动": False,
}
DEFAULT_AXES = {"x轴": {"字段": None, "标题": "维度"}, "y轴": {"字段": None, "标题": "数值"}, "显示网格": True}
DEFAULT_EXPORT = "csv"


def hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_input(values: dict[str, Any] | None) -> dict[str, Any]:
    """把外部提交的英文字段名折叠成中文字段名；未知字段原样保留。"""
    values = dict(values or {})
    canonical: dict[str, Any] = {}
    nested_style = values.pop("样式", None) or values.pop("style", None)
    for key, value in values.items():
        canonical[INPUT_ALIASES.get(key, key)] = value
    if isinstance(nested_style, dict):
        for key, value in nested_style.items():
            canonical.setdefault(INPUT_ALIASES.get(key, key), value)
    if "备注" not in canonical and values.get("remark"):
        canonical["备注"] = values["remark"]
    return canonical


def _pick(raw: Any, *keys: str) -> Any:
    if not isinstance(raw, dict):
        return None
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def normalize_colors(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return {"调色板": list(DEFAULT_PALETTE), "字段配色": {}, "来源": "默认"}
    if isinstance(raw, list):
        return {"调色板": [str(item) for item in raw] or list(DEFAULT_PALETTE), "字段配色": {}, "原始": copy.deepcopy(raw)}
    if isinstance(raw, dict):
        result: dict[str, Any] = copy.deepcopy(raw)
        palette = _pick(raw, "调色板", "palette", "colors", "色板")
        mapping = _pick(raw, "字段配色", "映射", "mapping", "field_colors")
        if isinstance(palette, list):
            result["调色板"] = [str(item) for item in palette]
        elif isinstance(palette, str):
            result["调色板"] = split_palette(palette)
        else:
            result.setdefault("调色板", list(DEFAULT_PALETTE))
        scalable = {key: str(value) for key, value in raw.items() if isinstance(value, str) and looks_like_color(value)}
        if isinstance(mapping, dict):
            result["字段配色"] = {str(key): str(value) for key, value in mapping.items()}
        elif scalable and not palette:
            # 形如 {"region": "#1f6feb"} 的简单映射也接受。
            result["字段配色"] = scalable
        else:
            result.setdefault("字段配色", {})
        result.setdefault("来源", "模板")
        return result
    text = str(raw).strip()
    palette = split_palette(text)
    if palette:
        return {"调色板": palette, "字段配色": {}, "原始": text}
    return {"调色板": [text], "字段配色": {}, "原始": text}


def split_palette(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[,，;；|\s]+", text) if part.strip()]
    colors = [part for part in parts if looks_like_color(part)]
    return colors if colors else ([] if not parts else [parts[0]])


def looks_like_color(text: str) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{3,8}", text.strip())) or text.strip().lower().startswith(("rgb(", "hsl("))


def normalize_axes(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return copy.deepcopy(DEFAULT_AXES)
    if isinstance(raw, dict):
        result = copy.deepcopy(raw)
        result.setdefault("显示网格", True)
        return result
    if isinstance(raw, list):
        return {"轴": copy.deepcopy(raw), "显示网格": True}
    return {"说明": str(raw), "显示网格": True}


def normalize_legend(raw: Any) -> str:
    if raw is None or raw == "":
        return "顶部"
    if isinstance(raw, dict):
        position = _pick(raw, "位置", "position", "图例位置", "legend_position")
        return LEGEND_ALIASES.get(str(position).strip().lower(), str(position or "顶部"))
    return LEGEND_ALIASES.get(str(raw).strip().lower(), str(raw).strip())


def normalize_interactions(raw: Any) -> dict[str, Any]:
    if raw is None or raw == "":
        return copy.deepcopy(DEFAULT_INTERACTIONS)
    if isinstance(raw, dict):
        result = copy.deepcopy(raw)
        for key, value in DEFAULT_INTERACTIONS.items():
            result.setdefault(key, value)
        return result
    if isinstance(raw, list):
        enabled = {str(item) for item in raw}
        result = {key: (key in enabled or key in {str(item).lower() for item in raw}) for key in DEFAULT_INTERACTIONS}
        result["自定义"] = copy.deepcopy(raw)
        return result
    text = str(raw).strip()
    enabled = {part.strip() for part in re.split(r"[,，;；|\s]+", text) if part.strip()}
    result = {key: key in enabled for key in DEFAULT_INTERACTIONS}
    result["原始"] = text
    return result


def normalize_export(raw: Any) -> str:
    if raw is None or raw == "":
        return DEFAULT_EXPORT
    if isinstance(raw, dict):
        value = _pick(raw, "格式", "format", "类型", "type", "导出格式")
        return normalize_export(value)
    return EXPORT_ALIASES.get(str(raw).strip().lower(), str(raw).strip().lower())


def normalize_style(row: dict[str, Any]) -> dict[str, Any]:
    """返回快照里使用的样式视图：既有归一化结果，也保留原始配置。"""
    raw = {field: copy.deepcopy(row.get(field)) for field in STYLE_FIELDS}
    colors = normalize_colors(raw["颜色方案"])
    axes = normalize_axes(raw["坐标轴配置"])
    legend = normalize_legend(raw["图例位置"])
    interactions = normalize_interactions(raw["交互行为"])
    export_format = normalize_export(raw["导出格式"])
    return {
        "颜色方案": colors,
        "坐标轴配置": axes,
        "图例位置": legend,
        "交互行为": interactions,
        "导出格式": export_format,
        "原始": raw,
    }


def style_fingerprint(row: dict[str, Any]) -> str:
    """配置指纹覆盖图表类型、数据绑定原文和五项样式配置。"""
    payload = {
        "图表类型": row.get("图表类型"),
        "数据绑定": row.get("数据绑定"),
        "样式": {field: row.get(field) for field in STYLE_FIELDS},
    }
    return hash_payload(payload)


def default_style_values() -> dict[str, Any]:
    """登记模板时补齐样式默认值，避免列表里出现空白的样式字段。"""
    return {
        "颜色方案": {"调色板": list(DEFAULT_PALETTE), "字段配色": {}},
        "坐标轴配置": copy.deepcopy(DEFAULT_AXES),
        "图例位置": "顶部",
        "交互行为": copy.deepcopy(DEFAULT_INTERACTIONS),
        "导出格式": DEFAULT_EXPORT,
    }
