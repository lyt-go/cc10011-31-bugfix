"""可视化模板业务规则：样式持久化、绑定核对、发布快照、导出与回填。

调用链只有一条：
  模板配置 -> 数据绑定解析（数据服务契约） -> 图表快照 -> 页面渲染 / 导出文件 / 图表包
页面预览、导出文件、图表包和重新载入核对都读同一份快照，避免三套列顺序。
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import zipfile
from typing import Any

from app.store import store
from app.services import bindings, chartdata, styles
from app.services.snapshot_repository import GLOBAL_LOCK, RULE_VERSION, now_iso, snapshots

MODULE = "viztemplate"
REQUIRED_FIELDS = ["模板编号", "图表类型", "数据绑定"]
STATUS_ORDER = ["草稿", "模板评审中", "已发布", "已废弃"]
ACTION_RULES = {"提交模板评审": "模板评审中", "发布模板": "已发布", "废弃模板": "已废弃"}
NEGATIVE_ACTIONS = ["废弃模板"]
READ_ONLY_STATUS = "已废弃"
# 可发布的来源状态：允许草稿直接发布（登记后立即联调），但只允许向前流转。
PUBLISH_SOURCES = {"草稿", "模板评审中", "已发布"}
EXPORT_FORMATS = {"csv", "json", "tsv", "ndjson"}


def _now() -> str:
    return now_iso()


def _expected_version(values: dict[str, Any]) -> int | None:
    for key in ("expected_version", "模板版本", "expected_template_version", "version", "版本"):
        if key in values and values[key] not in (None, ""):
            try:
                return int(values[key])
            except (TypeError, ValueError):
                return None
    return None


def _as_int_list(raw: Any) -> list[int]:
    if raw in (None, ""):
        return []
    if isinstance(raw, (list, tuple, set)):
        result: list[int] = []
        for item in raw:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result
    result = []
    for part in str(raw).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


def _stable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


class ViztemplateService:
    # ---- 读取 -----------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        binding_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("模板编号", "")) or keyword in str(row.get("图表类型", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        decorated = [self.decorate(row) for row in rows]
        if binding_status:
            decorated = [item for item in decorated if item.get("绑定状态") == binding_status]
        total = len(decorated)
        start = max(page - 1, 0) * size
        return decorated[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self.decorate(row) if row else None

    def get_row(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表与详情共用的读视图；绑定状态按最新契约实时解析，不返回缓存的旧列。"""
        view = copy.deepcopy(row)
        resolved = bindings.resolve_binding(row)
        view["绑定解析"] = bindings.binding_view(resolved)
        view["绑定状态"] = self._binding_status(resolved)
        view["绑定列"] = list(resolved["columns"])
        view["绑定服务"] = (resolved["service"] or {}).get("服务编号")
        view["绑定契约版本"] = (resolved["contract"] or {}).get("契约版本")
        view["绑定契约哈希"] = (resolved["contract"] or {}).get("契约哈希")
        view["绑定版本"] = resolved["binding_fingerprint"]
        view["样式视图"] = styles.normalize_style(row)
        view["样式指纹"] = styles.style_fingerprint(row)
        view["只读"] = row.get("status") == READ_ONLY_STATUS
        self._ensure_seed_history(row)
        current = snapshots.current(int(row.get("id", 0))) or snapshots.latest(int(row.get("id", 0)))
        view["快照版本"] = current.get("快照版本") if current else None
        view["快照编号"] = current.get("快照编号") if current else None
        view["快照列顺序"] = list(current.get("列顺序", [])) if current else []
        view["快照配置指纹"] = current.get("配置指纹") if current else None
        if current is None:
            view["快照一致"] = None
        else:
            view["快照一致"] = self._snapshot_matches(row, resolved, current)
            if not view["快照一致"]:
                view["快照差异"] = self._snapshot_diff(row, resolved, current)
        return view

    @staticmethod
    def _snapshot_diff(row: dict[str, Any], resolved: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
        differences: list[str] = []
        if snapshot.get("样式指纹") != styles.style_fingerprint(row):
            differences.append("样式配置")
        if list(snapshot.get("列顺序", [])) != list(resolved["columns"]):
            differences.append("绑定列顺序")
        if snapshot.get("契约哈希") != (resolved["contract"] or {}).get("契约哈希"):
            differences.append("数据服务契约")
        if int(snapshot.get("模板版本", 0)) != int(row.get("模板版本", 0)):
            differences.append("模板版本")
        return differences

    @staticmethod
    def _binding_status(resolved: dict[str, Any]) -> str:
        unresolved = [item for item in resolved["conflicts"] if not item.get("新字段")]
        if unresolved:
            return "冲突"
        if not resolved["ok"]:
            return "不可用"
        if resolved["conflicts"]:
            return "待核对"
        if not (resolved["contract"] or {}).get("生效"):
            return "待核对"
        return "可用"

    # ---- 登记 / 更新 -----------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        canonical = styles.canonical_input(values)
        missing = [field for field in REQUIRED_FIELDS if canonical.get(field) in (None, "", {}, [])]
        if missing:
            return None, missing

        with GLOBAL_LOCK:
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "模板编号": str(canonical["模板编号"]).strip(),
                "图表类型": canonical["图表类型"],
                "数据绑定": copy.deepcopy(canonical["数据绑定"]),
                "模板版本": 1,
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
            }
            defaults = styles.default_style_values()
            for field in styles.STYLE_FIELDS:
                value = canonical.get(field)
                entry[field] = copy.deepcopy(value) if value not in (None, "", [], {}) else copy.deepcopy(defaults[field])
            # 图例位置/导出格式是单值枚举，登记时就归一化，避免列表和渲染各显示一套写法。
            entry["图例位置"] = styles.normalize_legend(entry["图例位置"])
            entry["导出格式"] = styles.normalize_export(entry["导出格式"])
            if canonical.get("备注"):
                entry["备注"] = canonical["备注"]
            # 未知但提交上来的字段也保留，避免“登记后配置丢失”。
            for key, value in canonical.items():
                if key not in entry and key != "action":
                    entry[key] = copy.deepcopy(value)
            self._refresh_binding(row=entry)
            entry["配置指纹"] = styles.style_fingerprint(entry)
            rows.append(entry)
        return self.decorate(entry), []

    def update_entry(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"图表模板 {entry_id} 不存在或已归档"
        if row.get("status") == READ_ONLY_STATUS:
            return None, "图表模板已废弃，只读，不能再修改配置或重新发布"
        canonical = styles.canonical_input(values)
        expected = _expected_version(values)
        with GLOBAL_LOCK:
            if expected is not None and expected != int(row.get("模板版本", 1)):
                return None, (
                    f"模板版本已变化（当前 v{row.get('模板版本', 1)}，提交 v{expected}），请刷新后重试"
                )
            backup = copy.deepcopy(row)
            for field in styles.STYLE_FIELDS:
                if field in canonical and canonical[field] not in (None, "", [], {}):
                    row[field] = copy.deepcopy(canonical[field])
            row["图例位置"] = styles.normalize_legend(row.get("图例位置"))
            row["导出格式"] = styles.normalize_export(row.get("导出格式"))
            for key in ("图表类型", "数据绑定", "备注"):
                if key in canonical and canonical[key] not in (None, "", [], {}):
                    row[key] = copy.deepcopy(canonical[key])
            for key, value in canonical.items():
                if key not in row and key not in ("action", "expected_version", "模板版本", "version", "版本"):
                    row[key] = copy.deepcopy(value)
            row["模板版本"] = int(row.get("模板版本", 1)) + 1
            self._refresh_binding(row=row)
            row["配置指纹"] = styles.style_fingerprint(row)
            if row.get("status") == "已发布":
                # 已发布模板的修改必须原子地产生新快照，否则页面预览和导出会读到两套配置。
                resolved = bindings.resolve_binding(row)
                if not self._publishable(resolved):
                    row.clear()
                    row.update(backup)
                    return None, self._binding_error(resolved)
                self._write_snapshot(row, reason="模板配置更新自动重新发布")
                row["pending"] = False
                row["abnormal"] = False
        return self.decorate(row), "图表模板已更新"

    # ---- 状态动作 -------------------------------------------------------
    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"图表模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于可视化模板可执行范围"
        if row.get("status") == READ_ONLY_STATUS and action != "废弃模板":
            return None, "图表模板已废弃，只读，不能发布或再次流转"

        with GLOBAL_LOCK:
            expected = _expected_version(values)
            if expected is not None and expected != int(row.get("模板版本", 1)):
                return None, (
                    f"模板版本已变化（当前 v{row.get('模板版本', 1)}，提交 v{expected}），请刷新后重试"
                )
            requested_contract = values.get("契约版本", values.get("expected_contract_version"))
            if action == "发布模板" and requested_contract not in (None, ""):
                active = (bindings.resolve_binding(row).get("contract") or {})
                try:
                    requested_int = int(requested_contract)
                except (TypeError, ValueError):
                    requested_int = None
                if requested_int is not None and requested_int != int(active.get("契约版本", 0)):
                    return None, (
                        f"数据服务契约已变化（当前 v{active.get('契约版本')}，提交 v{requested_int}），"
                        "请刷新后重新核对绑定再发布"
                    )
            if action == "提交模板评审":
                if row.get("status") == "已发布":
                    return None, "图表模板已发布，不能退回评审中"
                if row.get("status") == "草稿":
                    row["status"] = "模板评审中"
                row["pending"] = True
                row["abnormal"] = False
                return self.decorate(row), "图表模板已提交模板评审"

            if action == "废弃模板":
                if row.get("status") == READ_ONLY_STATUS:
                    return self.decorate(row), "图表模板已是废弃状态，历史快照保持只读可查"
                row["status"] = "已废弃"
                row["pending"] = False
                row["abnormal"] = True
                row["废弃时间"] = _now()
                for snapshot in snapshots.history(entry_id):
                    if snapshot.get("有效"):
                        snapshots.deactivate(int(snapshot["模板id"]), int(snapshot["快照版本"]))
                return self.decorate(row), "图表模板已废弃"

            # 发布模板
            if row.get("status") not in PUBLISH_SOURCES:
                return None, f"图表模板当前状态为「{row.get('status')}」，不能发布"
            resolved = bindings.resolve_binding(row)
            if not self._publishable(resolved):
                row["abnormal"] = True
                return None, self._binding_error(resolved)

            self._refresh_binding(row=row, resolved=resolved)
            current = snapshots.current(entry_id)
            if row.get("status") == "已发布" and current and self._snapshot_matches(row, resolved, current):
                # 并发重复发布 / 重复点击：复用已有有效快照，不产生第二份。
                return self.decorate(row), "图表模板已发布，当前配置与快照一致，复用原快照"

            snapshot = self._write_snapshot(row, resolved=resolved, reason="发布模板")
            row["status"] = "已发布"
            row["pending"] = False
            row["abnormal"] = False
            row["发布时间"] = _now()
            row["发布快照编号"] = snapshot["快照编号"]
        return self.decorate(row), f"图表模板已发布（快照 {snapshot['快照编号']}）"

    @staticmethod
    def _publishable(resolved: dict[str, Any]) -> bool:
        unresolved = [item for item in resolved["conflicts"] if not item.get("新字段")]
        return bool(resolved["ok"]) and not unresolved

    @staticmethod
    def _binding_error(resolved: dict[str, Any]) -> str:
        unresolved = [item for item in resolved["conflicts"] if not item.get("新字段")]
        if resolved["error"]:
            return f"发布前核对数据绑定失败：{resolved['error']}"
        if unresolved:
            names = "、".join(str(item.get("旧字段")) for item in unresolved)
            return f"发布前核对数据绑定失败：字段「{names}」不在数据服务契约中，请先调整绑定"
        return "发布前核对数据绑定失败：绑定不可用"

    def _refresh_binding(
        self, *, row: dict[str, Any], resolved: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resolved = resolved or bindings.resolve_binding(row)
        row["绑定版本"] = resolved["binding_fingerprint"]
        row["绑定状态"] = self._binding_status(resolved)
        row["绑定列"] = list(resolved["columns"])
        row["绑定服务"] = (resolved["service"] or {}).get("服务编号")
        row["绑定契约版本"] = (resolved["contract"] or {}).get("契约版本")
        row["绑定契约哈希"] = (resolved["contract"] or {}).get("契约哈希")
        row["绑定错误"] = resolved["error"]
        row["绑定冲突"] = copy.deepcopy(resolved["conflicts"])
        if isinstance(row.get("数据绑定"), dict) and resolved["columns"]:
            # 绑定本身保留用户声明；解析后的列顺序写在派生字段上，不回写用户配置。
            row["绑定列顺序"] = list(resolved["columns"])
        return resolved

    # ---- 快照 -----------------------------------------------------------
    def build_snapshot(
        self,
        row: dict[str, Any],
        *,
        resolved: dict[str, Any] | None = None,
        reason: str = "手动生成",
    ) -> dict[str, Any]:
        resolved = resolved or bindings.resolve_binding(row)
        if not resolved["ok"]:
            raise bindings.BindingError(resolved["error"] or "数据绑定不可用")
        style = styles.normalize_style(row)
        data = chartdata.render_data(resolved)
        chart = chartdata.build_chart(style, resolved, data)
        chart["图表类型"] = row.get("图表类型")
        contract = resolved["contract"] or {}
        palette = style["颜色方案"].get("调色板") or []
        field_colors = style["颜色方案"].get("字段配色") or {}
        template_id = int(row["id"])
        return {
            "快照编号": f"SNAP-{row.get('模板编号')}-P{row.get('模板版本', 1)}",
            "模板id": template_id,
            "模板编号": row.get("模板编号"),
            "模板版本": int(row.get("模板版本", 1)),
            "图表类型": row.get("图表类型"),
            "样式": {
                "颜色方案": style["颜色方案"],
                "坐标轴配置": style["坐标轴配置"],
                "图例位置": style["图例位置"],
                "交互行为": style["交互行为"],
                "导出格式": style["导出格式"],
            },
            "原始样式": style["原始"],
            "配色": {
                "调色板": list(palette),
                "字段配色": copy.deepcopy(field_colors),
                "配色指纹": chartdata.payload_fingerprint(style["颜色方案"]),
            },
            "绑定": bindings.binding_view(resolved),
            "契约版本": contract.get("契约版本"),
            "契约哈希": contract.get("契约哈希"),
            "绑定指纹": resolved["binding_fingerprint"],
            "服务编号": (resolved["service"] or {}).get("服务编号"),
            "列顺序": list(resolved["columns"]),
            "列来源": "数据服务契约 + 模板绑定声明",
            "数据": data,
            "图表": chart,
            "样式指纹": styles.style_fingerprint(row),
            "数据指纹": chartdata.payload_fingerprint({"columns": data["columns"], "rows": data["rows"]}),
            "配置指纹": self._config_fingerprint(row, resolved),
            "规则版本": RULE_VERSION,
            "生成原因": reason,
            "生成时间": _now(),
        }

    @staticmethod
    def _config_fingerprint(row: dict[str, Any], resolved: dict[str, Any]) -> str:
        payload = {
            "模板编号": row.get("模板编号"),
            "模板版本": row.get("模板版本"),
            "图表类型": row.get("图表类型"),
            "样式": {field: row.get(field) for field in styles.STYLE_FIELDS},
            "绑定": bindings.binding_view(resolved),
        }
        return chartdata.payload_fingerprint(payload)

    def _write_snapshot(
        self,
        row: dict[str, Any],
        *,
        resolved: dict[str, Any] | None = None,
        reason: str = "手动生成",
    ) -> dict[str, Any]:
        resolved = resolved or bindings.resolve_binding(row)
        snapshot = self.build_snapshot(row, resolved=resolved, reason=reason)
        # 废弃模板的只读历史快照仍可读出用于核对，但不会再有“有效快照”。
        existing = snapshots.current(int(row["id"]))
        if existing and existing.get("配置指纹") == snapshot.get("配置指纹"):
            return existing
        stored = snapshots.append(int(row["id"]), snapshot)
        if row.get("status") == READ_ONLY_STATUS:
            snapshots.deactivate(int(row["id"]), int(stored["快照版本"]))
        stored["快照编号"] = f"SNAP-{row.get('模板编号')}-V{stored['快照版本']}"
        snapshots.replace(int(row["id"]), int(stored["快照版本"]), stored)
        row["快照版本"] = stored["快照版本"]
        row["快照编号"] = stored["快照编号"]
        row["快照配置指纹"] = stored["配置指纹"]
        return stored

    @staticmethod
    def _snapshot_matches(row: dict[str, Any], resolved: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        return (
            snapshot.get("配置指纹") == ViztemplateService._config_fingerprint(row, resolved)
            and int(snapshot.get("模板版本", 0)) == int(row.get("模板版本", 0))
            and list(snapshot.get("列顺序", [])) == list(resolved["columns"])
            and snapshot.get("绑定指纹") == resolved["binding_fingerprint"]
            and snapshot.get("契约哈希") == (resolved["contract"] or {}).get("契约哈希")
        )

    def list_snapshots(self, entry_id: int) -> tuple[list[dict[str, Any]], str | None]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return [], f"图表模板 {entry_id} 不存在或已归档"
        self._ensure_seed_history(row)
        # 已发布但没有快照的历史数据：只读兼容，按当前规则补一份，不改写历史。
        if row.get("status") == "已发布" and snapshots.current(entry_id) is None:
            resolved = bindings.resolve_binding(row)
            if resolved["ok"] and self._publishable(resolved):
                self._write_snapshot(row, resolved=resolved, reason="历史模板补建快照")
        return snapshots.history(entry_id), None

    def _ensure_seed_history(self, row: dict[str, Any]) -> None:
        """示例数据里声明的历史快照：按旧规则一次性落库，之后只读，不回算。"""
        seed = row.get("历史快照")
        if not isinstance(seed, dict) or snapshots.history(int(row["id"])):
            return
        columns = list(seed.get("列顺序") or [])
        if not columns:
            return
        frozen = {
            "ok": True,
            "binding": copy.deepcopy(row.get("数据绑定") or {}),
            "service": {"服务编号": row.get("绑定服务"), "服务名称": None},
            "contract": {
                "契约版本": seed.get("契约版本"),
                "契约哈希": seed.get("契约哈希"),
                "字段": list(seed.get("字段") or columns),
                "别名": dict(seed.get("别名") or {}),
            },
            "fields": list(seed.get("字段") or columns),
            "columns": columns,
            "x": seed.get("x") or columns[0],
            "y": [item for item in (seed.get("y") or []) if item in columns] or columns[1:],
            "series": seed.get("series"),
            "filters": [],
            "limit": None,
            "binding_fingerprint": seed.get("绑定指纹"),
            "conflicts": [],
            "warnings": [],
        }
        data = chartdata.render_data(frozen)
        snapshot = {
            "快照编号": seed.get("快照编号") or f"SNAP-{row.get('模板编号')}-V1",
            "模板id": int(row["id"]),
            "模板编号": row.get("模板编号"),
            "模板版本": int(seed.get("模板版本") or 1),
            "图表类型": seed.get("图表类型") or row.get("图表类型"),
            "样式": copy.deepcopy(seed.get("样式") or styles.normalize_style(row)),
            "原始样式": copy.deepcopy(seed.get("样式") or {}),
            "配色": copy.deepcopy(seed.get("配色") or {
                "调色板": [],
                "字段配色": {},
                "配色指纹": chartdata.payload_fingerprint(seed.get("配色") or {}),
            }),
            "绑定": bindings.binding_view(frozen),
            "契约版本": seed.get("契约版本"),
            "契约哈希": seed.get("契约哈希"),
            "绑定指纹": seed.get("绑定指纹") or chartdata.payload_fingerprint(frozen["binding"]),
            "服务编号": seed.get("服务编号") or row.get("绑定服务"),
            "列顺序": columns,
            "列来源": "历史快照（按生成时的契约冻结）",
            "数据": data,
            "图表": chartdata.build_chart(seed.get("样式") or styles.normalize_style(row), frozen, data),
            "样式指纹": seed.get("样式指纹") or chartdata.payload_fingerprint(seed.get("样式") or {}),
            "数据指纹": chartdata.payload_fingerprint({"columns": data["columns"], "rows": data["rows"]}),
            "配置指纹": seed.get("配置指纹") or chartdata.payload_fingerprint(seed),
            "规则版本": seed.get("规则版本") or "2026.09-snapshot-v0",
            "生成原因": "历史快照导入",
            "生成时间": seed.get("生成时间") or _now(),
        }
        snapshots.append(int(row["id"]), snapshot)
        snapshots.deactivate(int(row["id"]), 1)

    def snapshot(self, entry_id: int, version: int | None = None) -> dict[str, Any] | None:
        if version is None:
            return snapshots.current(entry_id)
        return snapshots.get(entry_id, version)

    def create_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"图表模板 {entry_id} 不存在或已归档"
        if row.get("status") == READ_ONLY_STATUS:
            return None, "图表模板已废弃，只读，不能再生成快照"
        with GLOBAL_LOCK:
            resolved = bindings.resolve_binding(row)
            if not resolved["ok"]:
                return None, f"生成快照失败：{resolved['error']}"
            snapshot = self._write_snapshot(row, resolved=resolved, reason="手动生成快照")
            self._refresh_binding(row=row, resolved=resolved)
        return self.decorate(row), f"已生成快照 {snapshot.get('快照编号')}"

    def ensure_snapshot(self, row: dict[str, Any]) -> dict[str, Any]:
        """页面预览/导出/图表包的统一入口：优先复用与当前配置一致的快照。

        已发布模板返回其有效快照；快照与当前配置不一致（例如服务契约升级）时，
        已发布模板会原子地生成新快照，保证三处读取同一份配置。
        若契约升级后绑定字段已无法解析，保留上一份完整快照继续可读，
        而不是悄悄丢掉列、导出一张空图。
        """
        current = snapshots.current(int(row["id"]))
        resolved = bindings.resolve_binding(row)
        if current and self._snapshot_matches(row, resolved, current):
            return current
        unresolved = [item for item in resolved["conflicts"] if not item.get("新字段")]
        if unresolved and current and row.get("status") == "已发布":
            return current
        if unresolved or not resolved["ok"]:
            names = "、".join(str(item.get("旧字段")) for item in unresolved)
            detail = f"字段「{names}」不在数据服务契约中" if names else (resolved["error"] or "数据绑定不可用")
            raise bindings.BindingError(detail)
        if row.get("status") == "已发布":
            with GLOBAL_LOCK:
                current = snapshots.current(int(row["id"]))
                if current and self._snapshot_matches(row, resolved, current):
                    return current
                return self._write_snapshot(row, resolved=resolved, reason="已发布模板按当前契约补建快照")
        return self.build_snapshot(row, resolved=resolved, reason="页面预览")

    def preview(
        self, entry_id: int, snapshot_version: int | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"图表模板 {entry_id} 不存在或已归档"
        if snapshot_version is not None:
            snapshot = snapshots.get(entry_id, snapshot_version)
            if snapshot is None:
                return None, f"图表模板 {entry_id} 没有快照 v{snapshot_version}"
            return self._render_payload(row, snapshot, f"历史快照 v{snapshot_version}"), None
        try:
            snapshot = self.ensure_snapshot(row)
        except bindings.BindingError as error:
            return None, f"图表渲染失败：{error}"
        return self._render_payload(row, snapshot, snapshot.get("生成原因") or "页面预览"), None

    @staticmethod
    def _render_payload(row: dict[str, Any], snapshot: dict[str, Any], mode: str) -> dict[str, Any]:
        data = copy.deepcopy(snapshot["数据"])
        chart = copy.deepcopy(snapshot["图表"])
        return {
            "模式": mode,
            "模板id": row.get("id"),
            "模板编号": row.get("模板编号"),
            "模板版本": row.get("模板版本"),
            "status": row.get("status"),
            "只读": row.get("status") == READ_ONLY_STATUS,
            "快照编号": snapshot.get("快照编号"),
            "快照版本": snapshot.get("快照版本"),
            "快照": snapshot,
            "样式": copy.deepcopy(snapshot["样式"]),
            "配色": copy.deepcopy(snapshot["配色"]),
            "绑定": copy.deepcopy(snapshot["绑定"]),
            "契约版本": snapshot.get("契约版本"),
            "契约哈希": snapshot.get("契约哈希"),
            "列顺序": list(snapshot.get("列顺序", [])),
            "数据": data,
            "图表": chart,
            "指纹": {
                "样式": snapshot.get("样式指纹"),
                "绑定": snapshot.get("绑定指纹"),
                "配色": (snapshot.get("配色") or {}).get("配色指纹"),
                "数据": snapshot.get("数据指纹"),
                "配置": snapshot.get("配置指纹"),
                "规则": snapshot.get("规则版本"),
            },
        }

    # ---- 导出文件 / 图表包 ---------------------------------------------
    def export_data(
        self, entry_id: int, export_format: str | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"图表模板 {entry_id} 不存在或已归档"
        if row.get("status") == READ_ONLY_STATUS:
            return None, "图表模板已废弃，只读，不能导出使用"
        try:
            snapshot = self.ensure_snapshot(row)
        except bindings.BindingError as error:
            return None, f"导出失败：{error}"
        columns = list(snapshot.get("列顺序") or [])
        fmt = styles.normalize_export(export_format or (snapshot.get("样式") or {}).get("导出格式"))
        if fmt not in EXPORT_FORMATS:
            fmt = "csv"
        payload = {
            "模板编号": snapshot.get("模板编号"),
            "模板版本": snapshot.get("模板版本"),
            "快照编号": snapshot.get("快照编号"),
            "快照版本": snapshot.get("快照版本"),
            "图表类型": snapshot.get("图表类型"),
            "契约版本": snapshot.get("契约版本"),
            "契约哈希": snapshot.get("契约哈希"),
            "绑定指纹": snapshot.get("绑定指纹"),
            "样式指纹": snapshot.get("样式指纹"),
            "配色": copy.deepcopy(snapshot.get("配色")),
            "列顺序": columns,
            "格式": fmt,
            "数据": snapshot.get("数据"),
            "快照": snapshot,
        }
        if fmt == "json":
            content = json.dumps(
                {
                    "模板编号": payload["模板编号"],
                    "快照编号": payload["快照编号"],
                    "契约版本": payload["契约版本"],
                    "契约哈希": payload["契约哈希"],
                    "列顺序": columns,
                    "rows": snapshot["数据"]["rows"],
                },
                ensure_ascii=False,
                indent=2,
            )
        elif fmt == "ndjson":
            content = "\n".join(
                json.dumps({column: item.get(column) for column in columns}, ensure_ascii=False)
                for item in snapshot["数据"]["rows"]
            )
        else:
            buffer = io.StringIO()
            delimiter = "\t" if fmt == "tsv" else ","
            writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
            # 列顺序严格取快照，不重新遍历数据行字典（dict 顺序会随数据源变化）。
            writer.writerow(columns)
            for item in snapshot["数据"]["rows"]:
                writer.writerow([item.get(column) for column in columns])
            content = buffer.getvalue()
        payload["内容"] = content
        payload["文件名"] = f"{snapshot.get('模板编号')}-{snapshot.get('快照编号')}.{fmt}"
        payload["内容指纹"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return payload, None

    def bundle(self, entry_id: int) -> tuple[dict[str, Any] | None, str | None]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"图表模板 {entry_id} 不存在或已归档"
        if row.get("status") == READ_ONLY_STATUS:
            return None, "图表模板已废弃，只读，不能下载图表包"
        try:
            snapshot = self.ensure_snapshot(row)
        except bindings.BindingError as error:
            return None, f"下载图表包失败：{error}"
        manifest = {
            "包格式": "chart-template-bundle",
            "包版本": 1,
            "模板编号": snapshot.get("模板编号"),
            "模板版本": snapshot.get("模板版本"),
            "快照编号": snapshot.get("快照编号"),
            "规则版本": snapshot.get("规则版本"),
            "契约版本": snapshot.get("契约版本"),
            "契约哈希": snapshot.get("契约哈希"),
            "绑定指纹": snapshot.get("绑定指纹"),
            "样式指纹": snapshot.get("样式指纹"),
            "配置指纹": snapshot.get("配置指纹"),
            "配色": copy.deepcopy(snapshot.get("配色")),
            "列顺序": list(snapshot.get("列顺序", [])),
        }
        return {
            "清单": manifest,
            "快照": copy.deepcopy(snapshot),
            "校验": self.verify_snapshot(row, snapshot),
            "文件名": f"{snapshot.get('模板编号')}-{snapshot.get('快照编号')}-chart-bundle.json",
        }, None

    def bundle_zip(self, entry_id: int) -> tuple[bytes | None, str | None, str | None]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, None, f"图表模板 {entry_id} 不存在或已归档"
        bundle, error = self.bundle(entry_id)
        if bundle is None:
            return None, None, error
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(bundle["清单"], ensure_ascii=False, indent=2))
            archive.writestr("snapshot.json", json.dumps(bundle["快照"], ensure_ascii=False, indent=2))
            exported, _ = self.export_data(entry_id, "csv")
            if exported:
                archive.writestr("data.csv", exported["内容"])
        filename = bundle["文件名"].replace(".json", ".zip")
        return buffer.getvalue(), filename, None

    # ---- 重新载入核对 ---------------------------------------------------
    def verify_snapshot(
        self, row: dict[str, Any], snapshot: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """重新载入时核对：样式/配色/绑定契约/列顺序/数据指纹是否与快照一致。"""
        snapshot = snapshot or snapshots.current(int(row["id"]))
        if snapshot is None:
            # 草稿还没有持久化快照：用当前配置确定性生成一份做核对，不写库。
            try:
                snapshot = self.ensure_snapshot(row)
            except bindings.BindingError as error:
                return {"一致": False, "检查项": [], "说明": f"模板没有可核对的快照：{error}"}
        checks: list[dict[str, Any]] = []

        def check(name: str, expected: Any, actual: Any) -> None:
            checks.append({"检查项": name, "一致": expected == actual, "快照值": expected, "当前值": actual})

        style = styles.normalize_style(row)
        check("图表类型", snapshot.get("图表类型"), row.get("图表类型"))
        check("样式指纹", snapshot.get("样式指纹"), styles.style_fingerprint(row))
        check("配色指纹", (snapshot.get("配色") or {}).get("配色指纹"), chartdata.payload_fingerprint(style["颜色方案"]))
        check("图例位置", (snapshot.get("样式") or {}).get("图例位置"), style["图例位置"])
        check("交互行为", (snapshot.get("样式") or {}).get("交互行为"), style["交互行为"])
        check("导出格式", (snapshot.get("样式") or {}).get("导出格式"), style["导出格式"])
        check("模板版本", snapshot.get("模板版本"), row.get("模板版本"))

        resolved = bindings.resolve_binding(row)
        check("绑定指纹", snapshot.get("绑定指纹"), resolved["binding_fingerprint"])
        check("契约版本", snapshot.get("契约版本"), (resolved["contract"] or {}).get("契约版本"))
        check("契约哈希", snapshot.get("契约哈希"), (resolved["contract"] or {}).get("契约哈希"))
        check("列顺序", list(snapshot.get("列顺序", [])), list(resolved["columns"]))

        # 用快照自带的契约重算数据，确认历史快照仍然可读且数据未被改写。
        frozen_resolved = self._resolved_from_snapshot(snapshot)
        if frozen_resolved is not None:
            data = chartdata.render_data(frozen_resolved)
            check("数据列顺序", list(snapshot.get("列顺序", [])), list(data["columns"]))
            check("数据指纹", snapshot.get("数据指纹"), chartdata.payload_fingerprint({
                "columns": data["columns"], "rows": data["rows"],
            }))
        else:
            check("数据指纹", snapshot.get("数据指纹"), None)
        ok = all(item["一致"] for item in checks)
        return {
            "一致": ok,
            "模板编号": row.get("模板编号"),
            "快照编号": snapshot.get("快照编号"),
            "快照版本": snapshot.get("快照版本"),
            "检查项": checks,
            "说明": "模板配置、配色、数据绑定契约、列顺序和数据与快照一致" if ok else "模板或服务契约已变化，快照与当前配置不一致",
        }

    @staticmethod
    def _resolved_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
        binding = snapshot.get("绑定") or {}
        contract = {
            "服务编号": snapshot.get("服务编号") or (binding.get("service") or {}).get("服务编号"),
            "契约版本": snapshot.get("契约版本"),
            "契约哈希": snapshot.get("契约哈希"),
            "字段": list(binding.get("fields") or snapshot.get("列顺序") or []),
            "别名": dict(((binding.get("binding") or {}).get("别名") or {})),
        }
        if not contract["字段"]:
            return None
        service = {
            "服务编号": contract["服务编号"],
            "服务名称": (binding.get("service") or {}).get("服务名称"),
        }
        return {
            "ok": True,
            "binding": copy.deepcopy(binding.get("binding") or {}),
            "service": service,
            "contract": contract,
            "fields": list(contract["字段"]),
            "columns": list(snapshot.get("列顺序") or contract["字段"]),
            "x": binding.get("x"),
            "y": list(binding.get("y") or []),
            "series": binding.get("series"),
            "filters": copy.deepcopy(binding.get("filters") or []),
            "limit": binding.get("limit"),
            "binding_fingerprint": snapshot.get("绑定指纹"),
            "conflicts": [],
            "warnings": [],
        }

    # ---- 回填批次 -------------------------------------------------------
    def backfill(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        batch_id = str(
            values.get("batch_id")
            or values.get("批次号")
            or values.get("批次")
            or ""
        ).strip()
        template_ids = _as_int_list(
            values.get("模板ids") or values.get("template_ids") or values.get("ids") or values.get("模板编号")
        )
        stop_after_raw = values.get("stop_after") or values.get("max_items") or values.get("limit")
        try:
            stop_after = int(stop_after_raw) if stop_after_raw not in (None, "") else None
        except (TypeError, ValueError):
            stop_after = None
        requested = {
            "batch_id": batch_id,
            "模板ids": template_ids,
            "规则版本": RULE_VERSION,
        }
        with GLOBAL_LOCK:
            existing = snapshots.batch(batch_id)
            if existing is not None:
                if _stable(existing.get("requested")) != _stable(requested):
                    return None, f"回填批次 {batch_id} 已存在且内容不同，拒绝重复写入"
                if existing.get("status") == "已完成":
                    return self._batch_view(existing), f"回填批次 {batch_id} 已完成，重复提交不重复写入"
                batch = existing  # 中断后续跑
            else:
                batch = snapshots.start_batch(batch_id, requested)

            rows = store.rows(MODULE)
            if template_ids:
                rows = [row for row in rows if int(row.get("id", 0)) in set(template_ids)]
            processed_ids = {int(item) for item in batch.get("processed", [])}
            failed_ids = {int(item.get("模板id", 0)) for item in batch.get("failed", [])}
            cursor = int(batch.get("cursor", 0))
            started = 0
            while cursor < len(rows):
                row = rows[cursor]
                template_id = int(row.get("id", 0))
                if template_id in processed_ids or template_id in failed_ids:
                    cursor += 1
                    batch["cursor"] = cursor
                    continue
                if stop_after is not None and started >= stop_after:
                    batch["status"] = "中断"
                    snapshots.save_batch(batch)
                    return self._batch_view(batch), f"回填批次 {batch_id} 已中断，可从上一完整条目继续"
                started += 1
                try:
                    resolved = bindings.resolve_binding(row)
                    if not resolved["ok"]:
                        raise bindings.BindingError(resolved["error"] or "数据绑定不可用")
                    current = snapshots.current(template_id)
                    if current and self._snapshot_matches(row, resolved, current):
                        batch["skipped"].append({"模板id": template_id, "模板编号": row.get("模板编号"), "原因": "已有相同快照"})
                    else:
                        snapshot = self._write_snapshot(row, resolved=resolved, reason=f"回填批次 {batch_id}")
                        batch["created_snapshot_ids"].append(snapshot["id"])
                        batch["processed"].append(template_id)
                        processed_ids.add(template_id)
                except bindings.BindingError as error:
                    batch["failed"].append({"模板id": template_id, "模板编号": row.get("模板编号"), "原因": str(error)})
                    failed_ids.add(template_id)
                cursor += 1
                batch["cursor"] = cursor
                snapshots.save_batch(batch)
            batch["status"] = "已完成"
            batch["completed_at"] = _now()
            snapshots.save_batch(batch)
            return self._batch_view(batch), f"回填批次 {batch_id} 已完成"

    @staticmethod
    def _batch_view(batch: dict[str, Any]) -> dict[str, Any]:
        view = copy.deepcopy(batch)
        view["写入数量"] = len(view.get("created_snapshot_ids", []))
        view["跳过数量"] = len(view.get("skipped", []))
        view["失败数量"] = len(view.get("failed", []))
        return view

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        batch = snapshots.batch(batch_id)
        return self._batch_view(batch) if batch else None

    # ---- 与数据服务同步 -------------------------------------------------
    def sync_binding(
        self, entry_id: int, *, strict: bool = True
    ) -> tuple[dict[str, Any] | None, str]:
        """另一个入口（数据服务页）改动契约后，模板绑定版本随模板/契约版本同步。

        契约升级导致绑定无法解析时：模板保留上一份有效快照继续可读，标记为冲突，
        不写入半截快照；strict=False 时用于契约升级的批量下游同步，不阻断升级本身。
        """
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"图表模板 {entry_id} 不存在或已归档"
        if row.get("status") == READ_ONLY_STATUS:
            return None, "图表模板已废弃，只读，不能同步绑定"
        with GLOBAL_LOCK:
            resolved = bindings.resolve_binding(row)
            self._refresh_binding(row=row, resolved=resolved)
            if not self._publishable(resolved):
                message = self._binding_error(resolved)
                row["abnormal"] = True
                if strict:
                    return None, message
                return self.decorate(row), message
            if row.get("status") == "已发布":
                self._write_snapshot(row, resolved=resolved, reason="数据服务契约同步")
            return self.decorate(row), "数据绑定已按数据服务当前契约同步"
