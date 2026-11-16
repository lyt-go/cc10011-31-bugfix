"""可视化模板 / 数据服务 / 导出链路的不变量测试。

运行：cd backend && .venv/bin/python -m pytest tests -q
（也支持直接 python tests/test_viztemplate_flow.py，不依赖 pytest 的断言）
"""
from __future__ import annotations

import copy
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.seed import SEED_ROWS  # noqa: E402
from app.services.snapshot_repository import snapshots  # noqa: E402
from app.store import store  # noqa: E402

client = TestClient(app)


def reset_state() -> None:
    snapshots.reset()
    for module, rows in SEED_ROWS.items():
        table = store.rows(module)
        table.clear()
        table.extend(copy.deepcopy(rows))


def create_template(**values) -> int:
    payload = {
        "模板编号": "VIZT-T001",
        "图表类型": "折线图",
        "数据绑定": {"服务编号": "DATA-0001", "字段": ["dt", "region", "active_users"]},
        "颜色方案": {"调色板": ["#123456", "#abcdef"], "字段配色": {"active_users": "#123456"}},
        "坐标轴配置": {"x轴": {"字段": "dt", "标题": "日期"}, "显示网格": True},
        "图例位置": "right",
        "交互行为": {"提示框": True, "缩放": False, "下钻": True},
        "导出格式": "csv",
    }
    payload.update(values)
    response = client.post("/api/viztemplate", json={"values": payload})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    return int(body["entry"]["id"])


def publish(entry_id: int, **values):
    return client.post(f"/api/viztemplate/{entry_id}/actions", json={"values": {"action": "发布模板", **values}})


def test_styles_survive_create() -> None:
    reset_state()
    entry_id = create_template()
    entry = client.get(f"/api/viztemplate/{entry_id}").json()
    assert entry["颜色方案"] == {"调色板": ["#123456", "#abcdef"], "字段配色": {"active_users": "#123456"}}
    assert entry["坐标轴配置"]["x轴"]["标题"] == "日期"
    assert entry["图例位置"] == "右侧"  # right 归一化为中文位置
    assert entry["交互行为"]["下钻"] is True
    assert entry["导出格式"] == "csv"
    # 渲染描述使用模板配色
    preview = client.get(f"/api/viztemplate/{entry_id}/preview").json()
    assert preview["图表"]["颜色"]["active_users"] == "#123456"
    assert preview["图表"]["图例位置"] == "右侧"
    assert preview["图表"]["交互行为"]["下钻"] is True


def test_publish_blocks_unknown_field_and_allows_alias() -> None:
    reset_state()
    bad = create_template(模板编号="VIZT-BAD", 数据绑定={"服务编号": "DATA-0001", "字段": ["dt", "not_a_field"]})
    assert client.get(f"/api/viztemplate/{bad}").json()["绑定状态"] == "冲突"
    response = publish(bad)
    assert response.json()["ok"] is False
    assert "not_a_field" in response.json()["message"]

    aliased = create_template(模板编号="VIZT-ALIAS", 数据绑定={"服务编号": "DATA-0001", "字段": ["day", "region"]})
    detail = client.get(f"/api/viztemplate/{aliased}").json()
    assert detail["绑定状态"] == "待核对"
    assert detail["绑定列"] == ["dt", "region"]
    assert detail["绑定解析"]["conflicts"][0]["新字段"] == "dt"
    assert publish(aliased).json()["ok"] is True


def test_preview_export_bundle_share_snapshot_and_column_order() -> None:
    reset_state()
    entry_id = create_template(数据绑定={"服务编号": "DATA-0001", "字段": ["order_amount", "dt", "active_users"]})
    assert publish(entry_id).json()["ok"] is True
    preview = client.get(f"/api/viztemplate/{entry_id}/preview").json()
    export = client.get(f"/api/viztemplate/{entry_id}/export")
    lines = export.text.splitlines()
    assert lines[0] == ",".join(preview["列顺序"]) == "order_amount,dt,active_users"
    assert preview["快照编号"] == export.headers["x-snapshot-code"]
    assert export.headers["x-contract-hash"] == preview["契约哈希"]

    bundle = client.get(f"/api/viztemplate/{entry_id}/bundle").json()
    assert bundle["清单"]["快照编号"] == preview["快照编号"]
    assert bundle["清单"]["列顺序"] == preview["列顺序"]
    assert bundle["清单"]["契约哈希"] == preview["契约哈希"]
    assert bundle["快照"]["列顺序"] == preview["列顺序"]

    archive = client.get(f"/api/viztemplate/{entry_id}/bundle?archive=true")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        manifest = json.loads(zipped.read("manifest.json"))
        data_lines = zipped.read("data.csv").decode("utf-8").splitlines()
    assert manifest["快照编号"] == preview["快照编号"]
    assert data_lines[0] == lines[0]
    assert client.post(f"/api/viztemplate/{entry_id}/verify").json()["导出核对"]["一致"] is True


def test_deprecated_template_is_read_only() -> None:
    reset_state()
    entry_id = create_template()
    assert publish(entry_id).json()["ok"] is True
    assert client.post(
        f"/api/viztemplate/{entry_id}/actions", json={"values": {"action": "废弃模板"}}
    ).json()["ok"] is True
    assert client.put(f"/api/viztemplate/{entry_id}", json={"values": {"图例位置": "左侧"}}).json()["ok"] is False
    assert publish(entry_id).json()["ok"] is False
    assert client.get(f"/api/viztemplate/{entry_id}/export").status_code == 400
    assert client.get(f"/api/viztemplate/{entry_id}/bundle").status_code == 400
    # 历史快照仍可读取核对
    snapshots_list = client.get(f"/api/viztemplate/{entry_id}/snapshots").json()["items"]
    assert snapshots_list and client.get(
        f"/api/viztemplate/{entry_id}/snapshots/{snapshots_list[-1]['快照版本']}"
    ).status_code == 200


def test_contract_upgrade_keeps_one_contract_for_page_snapshot_export() -> None:
    reset_state()
    entry_id = create_template(
        数据绑定={"服务编号": "DATA-0001", "契约版本": 1, "字段": ["dt", "region", "order_amount"]}
    )
    assert publish(entry_id).json()["ok"] is True
    upgrade = client.put(
        "/api/dataservice/1/contract",
        json={"values": {"返回字段": ["dt", "region", "amount"], "字段别名": {"order_amount": "amount"}}},
    ).json()
    assert upgrade["ok"] is True
    detail = client.get(f"/api/viztemplate/{entry_id}").json()
    assert detail["绑定契约版本"] == 2
    assert detail["绑定列"] == ["dt", "region", "amount"]
    # 自动同步后，页面/快照/导出同一契约、同一列顺序
    preview = client.get(f"/api/viztemplate/{entry_id}/preview").json()
    export = client.get(f"/api/viztemplate/{entry_id}/export")
    assert preview["列顺序"] == ["dt", "region", "amount"]
    assert export.text.splitlines()[0] == "dt,region,amount"
    assert str(preview["契约版本"]) == export.headers["x-contract-version"] == "2"
    assert preview["快照"]["绑定"]["contract_hash"] == export.headers["x-contract-hash"]
    # 旧契约版本的历史快照依旧可读
    history = client.get(f"/api/viztemplate/{entry_id}/snapshots").json()["items"]
    assert len(history) >= 2
    first = client.get(f"/api/viztemplate/{entry_id}/snapshots/{history[0]['快照版本']}").json()
    assert first["契约版本"] == 1
    assert first["列顺序"] == history[0]["列顺序"]


def test_unresolvable_binding_keeps_last_snapshot_readable() -> None:
    reset_state()
    entry_id = create_template(数据绑定={"服务编号": "DATA-0001", "字段": ["dt", "region"]})
    assert publish(entry_id).json()["ok"] is True
    snapshot_before = client.get(f"/api/viztemplate/{entry_id}/preview").json()["快照编号"]
    client.put(
        "/api/dataservice/1/contract",
        json={"values": {"返回字段": ["period", "area"], "字段别名": {}}},
    )
    detail = client.get(f"/api/viztemplate/{entry_id}").json()
    assert detail["绑定状态"] == "冲突"
    preview = client.get(f"/api/viztemplate/{entry_id}/preview").json()
    assert preview["快照编号"] == snapshot_before
    assert preview["列顺序"] == ["dt", "region"]
    assert preview["只读"] is False


def test_concurrent_publish_leaves_single_snapshot() -> None:
    reset_state()
    entry_id = create_template(模板编号="VIZT-RACE", 数据绑定={"服务编号": "DATA-0003", "字段": ["dt", "metric_value"]})
    first = publish(entry_id).json()
    second = publish(entry_id).json()
    assert first["ok"] is True and second["ok"] is True
    assert client.get(f"/api/viztemplate/{entry_id}/snapshots").json()["total"] == 1
    assert client.get(f"/api/viztemplate/{entry_id}").json()["模板版本"] == 1

    # 服务重复上线：有效契约版本仍只有一个
    client.post("/api/dataservice/3/actions", json={"values": {"action": "上线服务"}})
    contracts = client.get("/api/dataservice/3/contracts").json()["items"]
    assert sum(1 for item in contracts if item["生效"]) == 1


def test_stale_version_conflicts_are_rejected() -> None:
    reset_state()
    entry_id = create_template()
    assert publish(entry_id, expected_version=99).json()["ok"] is False
    stale_contract = publish(entry_id, expected_contract_version=99)
    assert stale_contract.json()["ok"] is False
    assert "契约" in stale_contract.json()["message"]


def test_backfill_is_idempotent_and_resumable() -> None:
    reset_state()
    partial = client.post(
        "/api/viztemplate/backfill", json={"values": {"batch_id": "BATCH-T", "stop_after": 2}}
    ).json()
    assert partial["ok"] is True
    assert partial["entry"]["status"] == "中断"
    written_first = len(partial["entry"]["created_snapshot_ids"])

    resumed = client.post("/api/viztemplate/backfill", json={"values": {"batch_id": "BATCH-T"}}).json()
    assert resumed["entry"]["status"] == "已完成"
    total_written = len(resumed["entry"]["created_snapshot_ids"])
    assert total_written > written_first
    assert len(set(resumed["entry"]["processed"])) == len(resumed["entry"]["processed"])

    repeat = client.post("/api/viztemplate/backfill", json={"values": {"batch_id": "BATCH-T"}}).json()
    assert repeat["entry"]["status"] == "已完成"
    assert len(repeat["entry"]["created_snapshot_ids"]) == total_written

    conflict = client.post(
        "/api/viztemplate/backfill", json={"values": {"batch_id": "BATCH-T", "模板ids": [1]}}
    ).json()
    assert conflict["ok"] is False


def test_legacy_snapshot_remains_readable() -> None:
    reset_state()
    legacy = client.get("/api/viztemplate/3/snapshots").json()["items"]
    assert any(item["规则版本"] != "2026.11-snapshot-v1" for item in legacy)
    old = legacy[0]
    assert old["列顺序"] == ["日期", "区域", "指标值", "渠道"]
    rendered = client.get(f"/api/viztemplate/3/preview?version={old['快照版本']}").json()
    assert rendered["列顺序"] == old["列顺序"]
    assert rendered["快照"]["规则版本"] == old["规则版本"]


def test_list_and_detail_agree_on_binding_state() -> None:
    reset_state()
    entry_id = create_template(模板编号="VIZT-LIST", 数据绑定={"服务编号": "DATA-0002", "字段": ["dt", "pay_amount"]})
    listed = client.get("/api/viztemplate", params={"keyword": "VIZT-LIST"}).json()["items"][0]
    detail = client.get(f"/api/viztemplate/{entry_id}").json()
    assert listed["绑定状态"] == detail["绑定状态"]
    assert listed["绑定列"] == detail["绑定列"]
    assert listed["绑定契约版本"] == detail["绑定契约版本"]
    filtered = client.get("/api/viztemplate", params={"binding_status": detail["绑定状态"]}).json()
    assert any(item["id"] == entry_id for item in filtered["items"])


def test_dataservice_contract_versions_are_monotonic() -> None:
    reset_state()
    before = client.get("/api/dataservice/2").json()
    first = client.put(
        "/api/dataservice/2/contract",
        json={"values": {"返回字段": ["dt", "region", "pay_amount"]}},
    ).json()
    assert first["ok"] is True and first["entry"]["契约版本"] == before["契约版本"] + 1
    noop = client.put(
        "/api/dataservice/2/contract",
        json={"values": {"返回字段": ["dt", "region", "pay_amount"]}},
    ).json()
    assert noop["entry"]["契约版本"] == first["entry"]["契约版本"]
    versions = client.get("/api/dataservice/2/contracts").json()["items"]
    assert [item["契约版本"] for item in versions] == sorted({item["契约版本"] for item in versions})


if __name__ == "__main__":
    failures = 0
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
                print(f"PASS {name}")
            except AssertionError as error:
                failures += 1
                print(f"FAIL {name}: {error}")
    raise SystemExit(1 if failures else 0)
