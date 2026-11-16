"""数据服务契约：解析返回字段、维护版本历史，保证同一服务同一时刻只有一个有效契约。

图表模板的数据绑定最终都落到这里：绑定只描述“要哪些字段、按什么顺序”，
字段名与别名的解释由这份契约统一完成，快照与导出复用同一个解析结果。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

FIELD_SPLIT_RE = re.compile(r"[,，;；|\n]")
SERVICE_NO_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*-\d+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_field_list(raw: Any) -> list[str]:
    """把“返回字段”的多种历史写法统一成字段名列表。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        for key in ("字段", "fields", "返回字段", "items"):
            if key in raw:
                return parse_field_list(raw[key])
        return [str(key) for key in raw]
    if isinstance(raw, (list, tuple)):
        fields: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                name = item.get("字段") or item.get("name") or item.get("原名")
                if name:
                    fields.append(str(name).strip())
            else:
                fields.extend(parse_field_list(item))
        return [field for field in fields if field]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("[") or text.startswith("{"):
        try:
            return parse_field_list(json.loads(text))
        except (TypeError, ValueError):
            pass
    return [part.strip() for part in FIELD_SPLIT_RE.split(text) if part.strip()]


def parse_aliases(raw: Any) -> dict[str, str]:
    """别名统一成 {旧字段/别名: 契约中的标准字段}。"""
    aliases: dict[str, str] = {}
    if not raw:
        return aliases
    if isinstance(raw, dict):
        for alias, canonical in raw.items():
            alias_text = str(alias).strip()
            canonical_text = str(canonical).strip()
            if alias_text and canonical_text and alias_text != canonical_text:
                aliases[alias_text] = canonical_text
        return aliases
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                alias = item.get("别名") or item.get("alias") or item.get("旧字段")
                canonical = item.get("字段") or item.get("原名") or item.get("canonical")
                if alias and canonical:
                    aliases[str(alias).strip()] = str(canonical).strip()
                continue
            text = str(item)
            for separator in ("=", "->", "：", ":"):
                if separator in text:
                    alias, canonical = text.split(separator, 1)
                    if alias.strip() and canonical.strip():
                        aliases[alias.strip()] = canonical.strip()
                    break
        return {key: value for key, value in aliases.items() if key != value}
    text = str(raw).strip()
    if not text:
        return aliases
    for part in re.split(r"[,，;；|\n]", text):
        part = part.strip()
        if not part:
            continue
        for separator in ("=", "->", "：", ":"):
            if separator in part:
                alias, canonical = part.split(separator, 1)
                alias = alias.strip()
                canonical = canonical.strip()
                if alias and canonical and alias != canonical:
                    aliases[alias] = canonical
                break
    return aliases


def contract_hash(entry: dict[str, Any]) -> str:
    """契约哈希只覆盖“服务编号 + 版本 + 字段 + 别名”，状态变化不会改变字段口径。"""
    payload = {
        "服务编号": entry.get("服务编号"),
        "契约版本": entry.get("契约版本"),
        "字段": list(entry.get("字段") or []),
        "别名": dict(sorted((entry.get("别名") or {}).items())),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def make_contract(
    service_no: str,
    fields: list[str],
    aliases: dict[str, str],
    *,
    version: int,
    status: str,
    effective: bool,
) -> dict[str, Any]:
    entry = {
        "服务编号": service_no,
        "契约版本": int(version),
        "字段": list(fields),
        "别名": dict(sorted(aliases.items())),
        "状态": status,
        "生效": bool(effective),
        "更新时间": now_iso(),
    }
    entry["契约哈希"] = contract_hash(entry)
    return entry


def ensure_contract(row: dict[str, Any]) -> list[dict[str, Any]]:
    """老记录没有契约历史时，按当时的返回字段补一份 v1，只读不改写字段值。

    已有历史也做一次轻量归一化（补字段/别名/哈希），保证示例数据和历史数据都能被读取。
    """
    history = row.get("契约历史")
    if isinstance(history, list) and history:
        for entry in history:
            entry.setdefault("字段", list(row.get("字段") or []))
            entry.setdefault("别名", dict(row.get("别名") or {}))
            entry.setdefault("契约版本", int(row.get("契约版本") or 1))
            entry.setdefault("状态", str(row.get("status") or "草稿"))
            entry.setdefault("生效", bool(entry.get("生效")))
            if not entry.get("契约哈希"):
                entry["契约哈希"] = contract_hash(entry)
        return history
    fields = parse_field_list(row.get("返回字段"))
    aliases = parse_aliases(row.get("字段别名"))
    status = str(row.get("status") or "草稿")
    entry = make_contract(
        str(row.get("服务编号") or f"SERVICE-{row.get('id', 0)}"),
        fields,
        aliases,
        version=int(row.get("契约版本") or 1),
        status=status,
        effective=status == "已上线",
    )
    row["契约历史"] = [entry]
    row["契约版本"] = entry["契约版本"]
    row["字段契约"] = dict(entry)
    return row["契约历史"]


def contract_versions(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(entry) for entry in ensure_contract(row)]


def contract_for(row: dict[str, Any], version: int | None = None) -> dict[str, Any] | None:
    history = ensure_contract(row)
    if version is not None:
        for entry in history:
            if int(entry.get("契约版本", 0)) == int(version):
                return dict(entry)
        return None
    for entry in reversed(history):
        if entry.get("生效"):
            return dict(entry)
    return dict(history[-1])


def active_contract(row: dict[str, Any]) -> dict[str, Any] | None:
    return contract_for(row, None)


def fields_signature(fields: list[str], aliases: dict[str, str]) -> str:
    payload = {"字段": fields, "别名": dict(sorted(aliases.items()))}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def field_lookup(contract: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    canonical = {str(field) for field in contract.get("字段") or []}
    aliases = {str(key): str(value) for key, value in (contract.get("别名") or {}).items()}
    return canonical, aliases


def next_contract_version(row: dict[str, Any]) -> int:
    history = ensure_contract(row)
    return max((int(entry.get("契约版本", 0)) for entry in history), default=0) + 1
