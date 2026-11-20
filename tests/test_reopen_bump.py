"""BUGFIX-10 重开抬代：甲路径验收（见 docs/抬代修复卡.md，2026-11-21 择定）。

红绿节奏：先复现重开后世代不涨 → 抬代失败测见红 → 双人同时重开争用失败测见红
→ 两红后才准改 reopen_svc / windows_svc / materialize_svc。
"""
import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "reopen.db")
    init_db()
    yield


def _auth(c, username="admin", password="123456"):
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_reopen_bump_voids_old_materialization_five_way_consistent():
    """抬代失败测：重开须同事务抬代并作废旧物化，五处同态。"""
    c = TestClient(app)
    h = _auth(c)
    before = c.get("/api/windows/1", headers=h).json()["window"]
    assert before["generation"] == 1 and before["status"] == "已关闭"
    mats = c.get("/api/materializations", headers=h).json()["items"]
    assert any(m["window_id"] == 1 and m["status"] == "成功" for m in mats)

    r = c.post("/api/windows/1/reopen-stub", headers=h, json={"mode": "作废重算"})
    assert r.status_code == 200
    assert r.json()["to_generation"] == 2

    # 世代：抬代必须落库，且不得误开窗
    after = c.get("/api/windows/1", headers=h).json()["window"]
    assert after["generation"] == 2
    assert after["status"] == "已关闭"
    # 物化：旧代物化已作废，不再挂当前代
    mats = c.get("/api/materializations", headers=h).json()["items"]
    assert not [m for m in mats if m["window_id"] == 1]
    # 重开台账：已抬代 1→2
    led = c.get("/api/reopen-ledger", headers=h).json()["items"]
    assert led and led[0]["from_generation"] == 1 and led[0]["to_generation"] == 2
    assert led[0]["status"] == "已抬代"
    # 对账：新代未物化，不得判齐
    recon = c.get("/api/reconcile/1", headers=h).json()
    assert recon["materialized"] is None
    assert recon["aligned"] is False
    # 审计：reopen_bump 留痕，载荷含作废明细
    acts = c.get("/api/audit", headers=h).json()["items"]
    bump = [a for a in acts if a["action"] == "reopen_bump"]
    assert bump and "voided" in bump[0]["payload"]

    # 世代与物化同源：新代重物化，分区键跟世代走
    m = c.post("/api/windows/1/materialize", headers=h)
    assert m.status_code == 200
    assert m.json()["partition_key"] == "W-1101-g2"
    recon = c.get("/api/reconcile/1", headers=h).json()
    assert recon["aligned"] is True
    assert recon["materialized"]["partition_key"] == "W-1101-g2"


def test_concurrent_reopen_latecomer_records_contention():
    """争用失败测：两人同时重开同窗，先到占锁，后到记争用不写业务。"""
    c = TestClient(app)
    h1 = _auth(c)  # admin 先占重开锁
    h2 = _auth(c, "closer", "close123")
    assert (
        c.post("/api/locks/acquire", headers=h1, json={"lock_key": "reopen:1"}).status_code
        == 200
    )

    r = c.post("/api/windows/1/reopen-stub", headers=h2, json={"mode": "作废重算"})
    assert r.status_code == 409
    # 后到记争用（落库），且不写业务
    cont = c.get("/api/contention", headers=h1).json()["items"]
    assert any(x["lock_key"] == "reopen:1" and x["actor"] == "closer" for x in cont)
    w = c.get("/api/windows/1", headers=h1).json()["window"]
    assert w["generation"] == 1
    assert c.get("/api/reopen-ledger", headers=h1).json()["items"] == []
    mats = c.get("/api/materializations", headers=h1).json()["items"]
    assert any(m["window_id"] == 1 and m["status"] == "成功" for m in mats)


def test_reopen_rolls_back_generation_when_void_fails(monkeypatch):
    """甲路径原子性：作废失败则世代与作废整体回滚。"""

    def boom(conn, wid):
        raise RuntimeError("模拟作废失败")

    monkeypatch.setattr("reopen_svc._void_old_materializations", boom)
    c = TestClient(app, raise_server_exceptions=False)
    h = _auth(c)
    r = c.post("/api/windows/1/reopen-stub", headers=h, json={"mode": "作废重算"})
    assert r.status_code == 500
    w = c.get("/api/windows/1", headers=h).json()["window"]
    assert w["generation"] == 1
    mats = c.get("/api/materializations", headers=h).json()["items"]
    assert any(m["window_id"] == 1 and m["partition_key"] == "W-1101-g1" for m in mats)
    assert c.get("/api/reopen-ledger", headers=h).json()["items"] == []


def test_deviation_from_fix_card_refused_and_logged():
    """偏离抬代修复卡（乙路径「增量补丁」）拒写并记「抬代偏了」。"""
    c = TestClient(app)
    h = _auth(c)
    r = c.post("/api/windows/1/reopen-stub", headers=h, json={"mode": "增量补丁"})
    assert r.status_code == 409
    acts = c.get("/api/audit", headers=h).json()["items"]
    assert any(a["action"] == "抬代偏了" for a in acts)
    w = c.get("/api/windows/1", headers=h).json()["window"]
    assert w["generation"] == 1
    assert c.get("/api/reopen-ledger", headers=h).json()["items"] == []


def test_window_list_generation_not_faked():
    """世代账同源：窗口列表直出真实世代，不做 +1 假抬代。"""
    c = TestClient(app)
    h = _auth(c)
    items = c.get("/api/windows", headers=h).json()["items"]
    assert items
    for it in items:
        assert it.get("display_generation", it["generation"]) == it["generation"]
