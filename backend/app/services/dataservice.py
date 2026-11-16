"""数据服务业务规则：状态流转、字段契约版本、发布与下线的并发控制。

契约取舍（场景：旧字段别名与新契约冲突）：
  绑定里只保留“要哪些列、按页面顺序”，不使用旧列名去猜数据；
  命中的服务契约版本是唯一裁决者，页面预览、图表快照和导出文件全部取同一份解析结果。
  旧列名如果能在当前契约的别名表里唯一映射到新字段，会记入冲突清单并提示，
  但不会改写页面已提交的列顺序；映射不到就直接拦下，不允许带着空列发布。
"""
from __future__ import annotations

import copy
from typing import Any

from app.store import store
from app.services import contracts
from app.services.snapshot_repository import GLOBAL_LOCK

MODULE = "dataservice"
REQUIRED_FIELDS = ["服务编号", "服务名称", "接口路径"]
STATUS_ORDER = ["草稿", "服务评审中", "已上线", "已下线"]
ACTION_RULES = {"提交服务评审": "服务评审中", "上线服务": "已上线", "下线服务": "已下线"}
NEGATIVE_ACTIONS = ["下线服务"]
READ_ONLY_STATUS = "已下线"


def _contract_input(values: dict[str, Any]) -> dict[str, Any] | None:
    raw_fields = values.get("返回字段")
    raw_aliases = values.get("字段别名")
    if raw_fields is None and raw_aliases is None:
        return None
    return {"返回字段": raw_fields, "字段别名": raw_aliases}


class DataserviceService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("服务编号", "")) or keyword in str(row.get("服务名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.decorate(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self.decorate(row) if row else None

    def decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        view = copy.deepcopy(row)
        contracts.ensure_contract(row)
        active = contracts.active_contract(row)
        view["契约版本"] = (active or {}).get("契约版本")
        view["契约哈希"] = (active or {}).get("契约哈希")
        view["字段"] = list((active or {}).get("字段") or [])
        view["别名"] = dict((active or {}).get("别名") or {})
        view["契约历史"] = contracts.contract_versions(row)
        view["只读"] = row.get("status") == READ_ONLY_STATUS
        return view

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        if values.get("返回字段") in (None, "", [], {}) and not values.get("字段"):
            missing.append("返回字段")
            return None, missing
        with GLOBAL_LOCK:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            for field in REQUIRED_FIELDS:
                entry[field] = values.get(field)
            for field in ("查询参数", "调用频次限制", "缓存策略", "服务版本"):
                if values.get(field) not in (None, ""):
                    entry[field] = values.get(field)
            fields = contracts.parse_field_list(values.get("返回字段") or values.get("字段"))
            aliases = contracts.parse_aliases(values.get("字段别名") or values.get("别名"))
            entry["返回字段"] = fields
            entry["字段别名"] = aliases
            contract = contracts.make_contract(
                str(entry["服务编号"]), fields, aliases,
                version=1, status=STATUS_ORDER[0], effective=False,
            )
            entry["契约版本"] = 1
            entry["契约历史"] = [contract]
            entry["字段契约"] = copy.deepcopy(contract)
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
        return self.decorate(entry), []

    def update_contract(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """升级返回字段/别名：生成新契约版本，旧契约保留在历史里供历史快照读取。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"服务接口 {entry_id} 不存在或已归档"
        if row.get("status") == READ_ONLY_STATUS:
            return None, "服务接口已下线，只读，不能再修改契约"
        payload = _contract_input(values)
        if payload is None:
            return None, "没有提交需要变更的契约字段（返回字段/字段别名/契约版本）"
        with GLOBAL_LOCK:
            expected = values.get("expected_contract_version", values.get("契约版本"))
            active = contracts.active_contract(row)
            if expected not in (None, "") and active is not None:
                try:
                    expected_int = int(expected)
                except (TypeError, ValueError):
                    expected_int = None
                if expected_int is not None and expected_int != int(active.get("契约版本", 0)):
                    return None, (
                        f"服务契约已变化（当前 v{active.get('契约版本')}，提交 v{expected_int}），请刷新后重试"
                    )
            fields = (
                contracts.parse_field_list(payload["返回字段"])
                if payload["返回字段"] is not None
                else list((active or {}).get("字段") or [])
            )
            aliases = (
                contracts.parse_aliases(payload["字段别名"])
                if payload["字段别名"] is not None
                else dict((active or {}).get("别名") or {})
            )
            if not fields:
                return None, "返回字段不能为空"
            history = contracts.ensure_contract(row)
            if active and fields == list(active.get("字段") or []) and aliases == dict(active.get("别名") or {}):
                return self.decorate(row), "服务契约没有变化，保留当前版本"
            version = contracts.next_contract_version(row)
            for entry in history:
                entry["生效"] = False
            contract = contracts.make_contract(
                str(row.get("服务编号")), fields, aliases,
                version=version,
                status=str(row.get("status") or "草稿"),
                effective=str(row.get("status")) == "已上线",
            )
            history.append(contract)
            row["返回字段"] = fields
            row["字段别名"] = aliases
            row["契约版本"] = version
            row["字段契约"] = copy.deepcopy(contract)
            row["契约更新时间"] = contract["更新时间"]
            self._sync_templates(row)
        return self.decorate(row), f"服务契约已升级到 v{version}"

    def _sync_templates(self, row: dict[str, Any]) -> None:
        """模板是契约的下游：契约变化时同步模板的绑定版本（另一个入口随模板版本更新）。"""
        from app.services import viztemplate

        service = viztemplate.ViztemplateService()
        service_no = str(row.get("服务编号"))
        for template in store.rows("viztemplate"):
            binding = template.get("数据绑定")
            if service_no not in str(binding):
                continue
            if template.get("status") == viztemplate.READ_ONLY_STATUS:
                continue
            service.sync_binding(int(template.get("id", 0)), strict=False)

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        row = store.find(MODULE, entry_id)
        if row is None:
            return None, f"服务接口 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据服务可执行范围"
        if row.get("status") == READ_ONLY_STATUS and action != "下线服务":
            return None, "服务接口已下线，只读，不能重新上线或流转"
        target = ACTION_RULES[action]

        with GLOBAL_LOCK:
            expected = values.get("expected_version", values.get("契约版本"))
            active = contracts.active_contract(row)
            if expected not in (None, "") and active is not None and action == "上线服务":
                try:
                    expected_int = int(expected)
                except (TypeError, ValueError):
                    expected_int = None
                if expected_int is not None and expected_int != int(active.get("契约版本", 0)):
                    return None, (
                        f"服务契约已变化（当前 v{active.get('契约版本')}，提交 v{expected_int}），请刷新后重试"
                    )

            if action == "提交服务评审":
                if row.get("status") == "已上线":
                    return None, "服务接口已上线，不能退回评审中"
                if row.get("status") == "草稿":
                    row["status"] = "服务评审中"
                row["pending"] = True
                row["abnormal"] = False
                self._refresh_contract_status(row, effective=False)
                return self.decorate(row), "服务接口已提交服务评审"

            if action == "上线服务":
                if row.get("status") == "已上线":
                    # 并发重复上线：只保留一个有效契约版本，不做第二次生效。
                    return self.decorate(row), "服务接口已上线，当前契约版本已生效，无需重复上线"
                active = contracts.active_contract(row)
                if active is None or not active.get("字段"):
                    return None, "服务契约没有返回字段，不能上线"
                self._refresh_contract_status(row, effective=True)
                row["status"] = "已上线"
                row["pending"] = False
                row["abnormal"] = False
                self._sync_templates(row)
                return self.decorate(row), f"服务接口已上线（契约 v{(contracts.active_contract(row) or {}).get('契约版本')}）"

            # 下线服务
            row["status"] = target
            row["pending"] = False
            row["abnormal"] = True
            self._refresh_contract_status(row, effective=False)
            self._sync_templates(row)
            return self.decorate(row), "服务接口已下线"

    @staticmethod
    def _refresh_contract_status(row: dict[str, Any], *, effective: bool) -> None:
        history = contracts.ensure_contract(row)
        version = int(row.get("契约版本") or (contracts.active_contract(row) or {}).get("契约版本") or 1)
        for entry in history:
            entry["状态"] = str(row.get("status"))
            if int(entry.get("契约版本", 0)) == version:
                entry["生效"] = effective
            else:
                entry["生效"] = False
            entry["契约哈希"] = contracts.contract_hash(entry)

    def contract_versions(self, entry_id: int) -> tuple[list[dict[str, Any]], str | None]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return [], f"服务接口 {entry_id} 不存在或已归档"
        active = contracts.active_contract(row)
        versions = contracts.contract_versions(row)
        for entry in versions:
            entry["生效"] = bool(active and entry.get("契约版本") == active.get("契约版本") and entry.get("生效"))
        return versions, None
