"""图表模板快照仓库：历史快照只追加、不重算，新记录按当前规则写入。

快照一旦生成就带自己的样式、绑定契约和列顺序，后续服务契约升级不会回写历史快照。
回填批次按 batch_id 幂等：重复提交同一批次不会重复写入，中断后从上一个完整条目继续。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any

RULE_VERSION = "2026.11-snapshot-v1"

# 全平台共用一把锁：模板发布、服务契约升级、快照写入之间需要互斥，
# 否则“同一模板/同一服务只能留一个有效版本”无法保证。
GLOBAL_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SnapshotRepository:
    def __init__(self) -> None:
        self._by_template: dict[int, list[dict[str, Any]]] = {}
        self._by_id: dict[int, dict[str, Any]] = {}
        self._batches: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    # ---- 快照读写 -------------------------------------------------------
    def append(self, template_id: int, snapshot: dict[str, Any]) -> dict[str, Any]:
        with GLOBAL_LOCK:
            history = self._by_template.setdefault(template_id, [])
            version = max((int(item.get("快照版本", 0)) for item in history), default=0) + 1
            for item in history:
                item["有效"] = False
            stored = copy.deepcopy(snapshot)
            stored["id"] = self._next_id
            stored["模板id"] = template_id
            stored["快照版本"] = version
            stored["有效"] = True
            stored["规则版本"] = stored.get("规则版本") or RULE_VERSION
            stored["生成时间"] = stored.get("生成时间") or now_iso()
            self._next_id += 1
            history.append(stored)
            self._by_id[stored["id"]] = stored
            return copy.deepcopy(stored)

    def history(self, template_id: int) -> list[dict[str, Any]]:
        with GLOBAL_LOCK:
            return [copy.deepcopy(item) for item in self._by_template.get(template_id, [])]

    def current(self, template_id: int) -> dict[str, Any] | None:
        with GLOBAL_LOCK:
            for item in reversed(self._by_template.get(template_id, [])):
                if item.get("有效"):
                    return copy.deepcopy(item)
            return None

    def latest(self, template_id: int) -> dict[str, Any] | None:
        """最新一份快照，无论是否仍然有效（废弃模板的历史快照仍保持可读）。"""
        with GLOBAL_LOCK:
            history = self._by_template.get(template_id, [])
            return copy.deepcopy(history[-1]) if history else None

    def get(self, template_id: int, version: int) -> dict[str, Any] | None:
        with GLOBAL_LOCK:
            for item in self._by_template.get(template_id, []):
                if int(item.get("快照版本", 0)) == int(version):
                    return copy.deepcopy(item)
            return None

    def get_by_id(self, snapshot_id: int) -> dict[str, Any] | None:
        with GLOBAL_LOCK:
            found = self._by_id.get(snapshot_id)
            return copy.deepcopy(found) if found else None

    def replace(self, template_id: int, version: int, snapshot: dict[str, Any]) -> None:
        with GLOBAL_LOCK:
            history = self._by_template.get(template_id, [])
            for index, item in enumerate(history):
                if int(item.get("快照版本", 0)) == int(version):
                    history[index] = copy.deepcopy(snapshot)
                    self._by_id[int(snapshot["id"])] = history[index]
                    return

    def deactivate(self, template_id: int, version: int) -> None:
        """废弃模板时保留历史快照可读，但不再有“有效快照”。"""
        with GLOBAL_LOCK:
            for item in self._by_template.get(template_id, []):
                if int(item.get("快照版本", 0)) == int(version):
                    item["有效"] = False

    def find_for_revision(self, template_id: int, revision: int) -> dict[str, Any] | None:
        """同一模板版本最新的一份快照（契约升级后可能再生成，因此取最后一份）。"""
        with GLOBAL_LOCK:
            for item in reversed(self._by_template.get(template_id, [])):
                if int(item.get("模板版本", 0)) == int(revision):
                    return copy.deepcopy(item)
            return None

    # ---- 回填批次 -------------------------------------------------------
    def batch(self, batch_id: str) -> dict[str, Any] | None:
        with GLOBAL_LOCK:
            found = self._batches.get(batch_id)
            return copy.deepcopy(found) if found else None

    def start_batch(self, batch_id: str, requested: dict[str, Any]) -> dict[str, Any]:
        with GLOBAL_LOCK:
            batch = {
                "batch_id": batch_id,
                "status": "进行中",
                "rule_version": RULE_VERSION,
                "requested": copy.deepcopy(requested),
                "cursor": 0,
                "processed": [],
                "created_snapshot_ids": [],
                "skipped": [],
                "failed": [],
                "started_at": now_iso(),
                "updated_at": now_iso(),
                "completed_at": None,
            }
            self._batches[batch_id] = batch
            return copy.deepcopy(batch)

    def save_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        with GLOBAL_LOCK:
            batch["updated_at"] = now_iso()
            self._batches[batch["batch_id"]] = copy.deepcopy(batch)
            return copy.deepcopy(batch)

    def batches(self) -> list[dict[str, Any]]:
        with GLOBAL_LOCK:
            return [copy.deepcopy(item) for item in self._batches.values()]

    def reset(self) -> None:
        """仅供测试与本地重置使用：清空快照与回填批次。"""
        with GLOBAL_LOCK:
            self._by_template.clear()
            self._by_id.clear()
            self._batches.clear()
            self._next_id = 1


snapshots = SnapshotRepository()
