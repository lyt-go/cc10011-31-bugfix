"""数据绑定契约：把模板的“数据绑定”解析成服务 + 契约版本 + 字段顺序。

模板只负责声明“绑到哪个服务、要哪些列、按页面顺序排列”，
字段是否存在、旧列名指向哪个新列，都由数据服务的契约版本裁决。
页面预览、图表快照和导出文件共用这里的同一份解析结果，避免三套列顺序。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from app.store import store
from app.services import contracts

MODULE = "dataservice"
SERVICE_NO_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*-\d+")

# 数据绑定键的别名：小写比较。
BINDING_KEYS = {
    "service_no": ("服务编号", "service_no", "serviceNo", "service", "服务"),
    "service_id": ("服务id", "service_id", "serviceId", "dataservice_id", "数据服务id"),
    "contract_version": ("契约版本", "contract_version", "contractVersion", "服务版本", "service_version", "version"),
    "fields": ("字段", "fields", "列", "columns", "返回字段"),
    "x": ("x", "X", "维度", "dimension", "category"),
    "y": ("y", "Y", "度量", "measure", "value", "metrics"),
    "series": ("系列", "series", "分组", "group", "group_by"),
    "filters": ("过滤", "filters", "筛选", "where"),
    "limit": ("limit", "条数"),
}

# 绑定指纹只覆盖“影响取数结果”的字段。
BINDING_FINGERPRINT_KEYS = ("服务编号", "服务id", "契约版本", "字段", "x", "y", "系列", "过滤", "limit")


class BindingError(ValueError):
    """绑定无法解析或与契约冲突；message 可直接展示给用户。"""


def _find(binding: dict[str, Any], key: str) -> Any:
    for alias in BINDING_KEYS[key]:
        if alias in binding and binding[alias] not in (None, ""):
            return binding[alias]
    lowered = {str(k).lower(): v for k, v in binding.items()}
    for alias in BINDING_KEYS[key]:
        if alias.lower() in lowered and lowered[alias.lower()] not in (None, ""):
            return lowered[alias.lower()]
    return None


def parse_binding(raw: Any) -> dict[str, Any]:
    """兼容对象、JSON 字符串和 “服务编号|字段1,字段2” 这类历史文本写法。"""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    text = str(raw).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            pass
    parts = re.split(r"[|｜]", text, maxsplit=1)
    binding: dict[str, Any] = {"服务编号": parts[0].strip()}
    if len(parts) > 1:
        fields = [item.strip() for item in re.split(r"[,，;；\s]+", parts[1]) if item.strip()]
        if fields:
            binding["字段"] = fields
    return binding


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("字段") or item.get("name") or item.get("key")
                if name:
                    result.append(str(name))
            else:
                result.append(str(item))
        return [item for item in result if item]
    return [part.strip() for part in re.split(r"[,，;；|\s]+", str(value)) if part.strip()]


def _as_filter_list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, dict):
        return [dict(value)]
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    return []


def find_service(binding: dict[str, Any]) -> dict[str, Any] | None:
    rows = store.rows(MODULE)
    service_id = _find(binding, "service_id")
    if service_id is not None:
        try:
            found = store.find(MODULE, int(service_id))
        except (TypeError, ValueError):
            found = None
        if found is not None:
            return found
    service_no = _find(binding, "service_no")
    if service_no:
        for row in rows:
            if str(row.get("服务编号") or "") == str(service_no):
                return row
        for row in rows:
            if str(row.get("服务名称") or "") == str(service_no):
                return row
    # 绑定的服务编号字段本身可能写了服务编号之外的文本，最后用正则兜底。
    for row in rows:
        row_no = str(row.get("服务编号") or "")
        if row_no and SERVICE_NO_RE.fullmatch(row_no) and row_no in str(binding):
            return row
    return None


def resolve_contract(binding: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """实时解析一律指向服务当前生效契约；绑定里的版本号只作为追溯信息保留。

    历史快照自带冻结的契约副本，不会因为服务升级被重算，所以这里不需要按旧版本取数。
    """
    service = find_service(binding)
    if service is None:
        return None
    contract = contracts.active_contract(service)
    if contract is None:
        return None
    return service, contract


def binding_fingerprint(binding: dict[str, Any]) -> str:
    payload = {key: binding.get(key) for key in BINDING_FINGERPRINT_KEYS if key in binding}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_binding(row: dict[str, Any]) -> dict[str, Any]:
    """解析一条模板的数据绑定。

    返回结构：
      ok            是否可用（不 ok 时 error 说明原因）
      binding       原始绑定
      service       绑定的数据服务
      contract      命中的契约版本
      fields        契约内的可用字段（契约顺序）
      columns       页面/快照/导出统一使用的列顺序
      column_map    每个输出列 -> 契约字段
      x / y / series 绑定引用的维度/度量
      filters / limit
      conflicts     旧列名与新契约冲突的清单（不静默改名）
      warnings      可以继续但需要提示的项
    """
    binding = parse_binding(row.get("数据绑定"))
    result: dict[str, Any] = {
        "ok": False,
        "binding": binding,
        "service": None,
        "contract": None,
        "fields": [],
        "columns": [],
        "column_map": {},
        "x": None,
        "y": [],
        "series": None,
        "filters": [],
        "limit": None,
        "conflicts": [],
        "warnings": [],
        "error": None,
        "binding_fingerprint": binding_fingerprint(binding),
    }
    if not binding:
        result["error"] = "图表模板没有数据绑定"
        return result

    resolved = resolve_contract(binding)
    if resolved is None:
        service_no = _find(binding, "service_no") or _find(binding, "service_id") or "未指定"
        result["error"] = f"数据绑定指向的数据服务「{service_no}」不存在或没有可用契约"
        return result

    service, contract = resolved
    result["service"] = {
        "id": service.get("id"),
        "服务编号": service.get("服务编号"),
        "服务名称": service.get("服务名称"),
        "status": service.get("status"),
    }
    result["contract"] = contract
    result["warnings"].append(
        f"绑定契约为数据服务「{service.get('服务编号')}」当前生效版本 v{contract.get('契约版本')}，字段以该版本为准"
    )
    requested_version = _find(binding, "contract_version")
    if requested_version not in (None, "") and str(requested_version) != str(contract.get("契约版本")):
        result["warnings"].append(
            f"绑定登记时引用的契约版本 v{requested_version} 已不是当前生效版本，"
            f"页面/快照/导出统一按当前契约 v{contract.get('契约版本')} 解析；历史快照保留原契约副本"
        )
    if not contract.get("生效"):
        result["warnings"].append(
            f"数据服务「{service.get('服务编号')}」当前状态为「{service.get('status')}」，契约 v{contract.get('契约版本')} 不是已上线版本"
        )

    canonical, aliases = contracts.field_lookup(contract)
    result["fields"] = list(contract.get("字段") or [])
    result["aliases"] = aliases
    result["canonical"] = sorted(canonical)

    requested = _as_list(_find(binding, "fields"))
    if not requested:
        x = _find(binding, "x")
        y = _as_list(_find(binding, "y"))
        series = _find(binding, "series")
        requested = [str(item) for item in ([x] if x else []) + y + ([series] if series else []) if item]
    if not requested:
        requested = list(result["fields"])
        result["warnings"].append("数据绑定未指定字段，按服务契约的返回字段顺序取数")

    columns: list[str] = []
    column_map: dict[str, str] = {}
    for name in requested:
        name = str(name).strip()
        if not name:
            continue
        if name in canonical:
            target = name
        elif name in aliases:
            target = aliases[name]
            if target in canonical:
                result["conflicts"].append({
                    "旧字段": name,
                    "新字段": target,
                    "说明": f"绑定里的旧列名「{name}」在契约 v{contract.get('契约版本')} 已改名为「{target}」",
                })
            else:
                result["conflicts"].append({
                    "旧字段": name,
                    "新字段": target,
                    "说明": f"绑定里的旧列名「{name}」映射到「{target}」，但该字段不在契约 v{contract.get('契约版本')} 中",
                })
                continue
        else:
            result["conflicts"].append({
                "旧字段": name,
                "新字段": None,
                "说明": f"绑定字段「{name}」不在数据服务「{service.get('服务编号')}」契约 v{contract.get('契约版本')} 中",
            })
            continue
        if target not in column_map:
            column_map[target] = target
            columns.append(target)

    result["columns"] = columns
    result["column_map"] = column_map
    x = _find(binding, "x")
    y_list = _as_list(_find(binding, "y"))
    series = _find(binding, "series")

    def normalize_ref(value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        if text in canonical:
            return text
        if text in aliases and aliases[text] in canonical:
            return aliases[text]
        return None

    result["x"] = normalize_ref(x) if x else (columns[0] if columns else None)
    result["y"] = [item for item in (normalize_ref(item) for item in y_list) if item]
    result["series"] = normalize_ref(series) if series else None
    if x and result["x"] is None:
        result["conflicts"].append({"旧字段": str(x), "新字段": None, "说明": f"绑定的维度字段「{x}」不在契约中"})
    for raw_item in y_list:
        if normalize_ref(raw_item) is None:
            result["conflicts"].append({"旧字段": str(raw_item), "新字段": None, "说明": f"绑定的度量字段「{raw_item}」不在契约中"})
    if not result["y"]:
        result["y"] = [column for column in columns if column not in {result["x"]}]
    result["filters"] = _as_filter_list(_find(binding, "filters"))
    try:
        limit = _find(binding, "limit")
        result["limit"] = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        result["limit"] = None

    result["ok"] = bool(columns)
    if not result["ok"]:
        result["error"] = "数据绑定与数据服务契约没有交集，取不到任何列"
    return result


def binding_view(resolved: dict[str, Any]) -> dict[str, Any]:
    """面向前端/快照的稳定视图（去掉 service 全量字段）。"""
    return {
        "ok": resolved.get("ok", True),
        "error": resolved.get("error"),
        "binding": copy.deepcopy(resolved["binding"]),
        "service": copy.deepcopy(resolved["service"]),
        "contract_version": (resolved["contract"] or {}).get("契约版本"),
        "contract_hash": (resolved["contract"] or {}).get("契约哈希"),
        "fields": list(resolved["fields"]),
        "columns": list(resolved["columns"]),
        "x": resolved.get("x"),
        "y": list(resolved.get("y") or []),
        "series": resolved.get("series"),
        "filters": copy.deepcopy(resolved.get("filters") or []),
        "limit": resolved.get("limit"),
        "conflicts": copy.deepcopy(resolved.get("conflicts") or []),
        "warnings": list(resolved.get("warnings") or []),
        "binding_fingerprint": resolved.get("binding_fingerprint"),
    }
