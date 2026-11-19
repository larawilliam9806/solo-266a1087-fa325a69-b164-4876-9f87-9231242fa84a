"""端到端测试：首次提交、整批替换核查、井归属重算、并发与崩溃原子性。"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app


# ----------------------------------------------------------------  fixtures

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """每个用例一个全新数据库文件；返回 (client, db_path)。"""
    db = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db


def square(x0, y0, x1, y1):
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


REGION = square(0, 0, 100, 100)
P1 = square(0, 0, 50, 100)     # 左半
P2 = square(50, 0, 100, 100)   # 右半（与 P1 共边 x=50）

WELLS = [
    {"id": "W-in", "geometry": point(25, 50)},      # P1 内部
    {"id": "W-edge", "geometry": point(50, 50)},    # P1/P2 共边上
    {"id": "W-out", "geometry": point(150, 50)},    # 区域外
    {"id": "W-move", "geometry": point(60, 50)},    # v1 在 P2 内部；v2 边界移到 60 后在新共边上
]


def init_ok(client):
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": P1}, {"id": "P2", "geometry": P2}],
        "wells": WELLS,
    })
    assert r.status_code == 201, r.text
    return r.json()


def move_boundary_to(x):
    """把 P1/P2 的共边整体移到 x（相邻宗地联改）。"""
    return [{"id": "P1", "geometry": square(0, 0, x, 100)},
            {"id": "P2", "geometry": square(x, 0, 100, 100)}]


def wells_by_id(payload):
    return {w["id"]: w for w in payload["wells"]}


# ---------------------------------------------------------------- 首次提交

def test_init_and_well_attribution(env):
    client, _ = env
    body = init_ok(client)
    assert body["revision"] == 1
    assert body["well_summary"] == {"assigned": 2, "pending": 1, "uncovered": 1}

    r = client.get("/v1/wells")
    assert r.status_code == 200
    assert r.json()["revision"] == 1
    wells = wells_by_id(r.json())
    assert wells["W-in"]["status"] == "assigned" and wells["W-in"]["parcel_id"] == "P1"
    assert wells["W-edge"]["status"] == "pending"
    assert wells["W-edge"]["candidates"] == ["P1", "P2"]   # 共边井列出所有候选
    assert wells["W-out"]["status"] == "uncovered" and wells["W-out"]["candidates"] == []
    assert wells["W-move"]["status"] == "assigned" and wells["W-move"]["parcel_id"] == "P2"


def test_get_before_init_returns_404(env):
    client, _ = env
    assert client.get("/v1/current").status_code == 404
    assert client.get("/v1/parcels").status_code == 404
    assert client.get("/v1/wells").status_code == 404


def test_init_twice_rejected(env):
    client, _ = env
    init_ok(client)
    r = client.post("/v1/init", json={"region": REGION,
                                      "parcels": [{"id": "P1", "geometry": P1},
                                                  {"id": "P2", "geometry": P2}]})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "already_initialized"


def test_init_with_gap_rejected(env):
    client, _ = env
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": P1},
                    {"id": "P2", "geometry": square(50, 0, 90, 100)}],  # 缺 10x100
    })
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["gap_area"] == pytest.approx(1000.0)
    assert any(x["code"] == "gap" for x in detail["reasons"])
    # 失败不留任何版本
    assert client.get("/v1/current").status_code == 404


def test_init_with_overlap_rejected(env):
    client, _ = env
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": square(0, 0, 60, 100)},
                    {"id": "P2", "geometry": square(40, 0, 100, 100)}],  # 重叠 20x100
    })
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["overlap_parcel_ids"] == ["P1", "P2"]
    assert detail["overlaps"][0]["area"] == pytest.approx(2000.0)


def test_init_geometry_rules(env):
    client, _ = env
    holey = {"type": "Polygon", "coordinates": [
        [[0, 0], [50, 0], [50, 100], [0, 100], [0, 0]],
        [[10, 10], [20, 10], [20, 20], [10, 20], [10, 10]],  # 内环（洞）
    ]}
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": holey}, {"id": "P2", "geometry": P2}],
    })
    assert r.status_code == 422
    assert any(x["code"] == "hole_not_allowed" for x in r.json()["detail"]["reasons"])

    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": square(0, 0, 50.5, 100)},
                    {"id": "P2", "geometry": P2}],
    })
    assert r.status_code == 422
    assert any(x["code"] == "non_integer_coordinate" for x in r.json()["detail"]["reasons"])

    bowtie = {"type": "Polygon",
              "coordinates": [[[0, 0], [40, 40], [40, 0], [0, 40], [0, 0]]]}
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": bowtie}, {"id": "P2", "geometry": P2}],
    })
    assert r.status_code == 422
    assert any(x["code"] == "invalid_geometry" for x in r.json()["detail"]["reasons"])


def test_init_limits(env):
    client, _ = env
    # 41 块宗地 → 结构校验直接拒绝
    parcels = [{"id": f"P{i}", "geometry": square(i, 0, i + 1, 1)} for i in range(41)]
    r = client.post("/v1/init", json={"region": REGION, "parcels": parcels})
    assert r.status_code == 422
    # 101 口井 → 拒绝
    wells = [{"id": f"W{i}", "geometry": point(1, 1)} for i in range(101)]
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": P1}, {"id": "P2", "geometry": P2}],
        "wells": wells,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------- 整批替换

def test_replace_success_recomputes_wells(env):
    client, _ = env
    init_ok(client)
    r = client.put("/v1/parcels", json={"base_revision": 1,
                                        "parcels": move_boundary_to(60)})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2

    wells = wells_by_id(client.get("/v1/wells").json())
    assert wells["W-edge"]["status"] == "assigned"          # x=50 现在在 P1 内部
    assert wells["W-edge"]["parcel_id"] == "P1"
    assert wells["W-move"]["status"] == "pending"           # x=60 变成新共边
    assert wells["W-move"]["candidates"] == ["P1", "P2"]
    assert wells["W-out"]["status"] == "uncovered"

    # 边界与井归属来自同一完整版本
    current = client.get("/v1/current").json()
    assert current["revision"] == 2
    assert {p["id"] for p in current["parcels"]} == {"P1", "P2"}
    assert wells_by_id(current)["W-move"]["status"] == "pending"

    # 历史版本仍可读
    old = client.get("/v1/revisions/1").json()
    assert old["revision"] == 1
    assert wells_by_id(old)["W-move"]["parcel_id"] == "P2"


def test_replace_partial_batch_ok(env):
    """批次里未提到的宗地沿用旧边界：只提交 P1（不变替换）也能发布新版本。"""
    client, _ = env
    init_ok(client)
    r = client.put("/v1/parcels", json={
        "base_revision": 1,
        "parcels": [{"id": "P1", "geometry": P1}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2
    parcels = {p["id"]: p["geometry"] for p in client.get("/v1/parcels").json()["parcels"]}
    assert parcels["P2"] == P2   # P2 未被提及，保持原边界


def test_replace_with_gap_rejected_old_version_readable(env):
    client, _ = env
    init_ok(client)
    r = client.put("/v1/parcels", json={
        "base_revision": 1,
        "parcels": [{"id": "P2", "geometry": square(50, 0, 90, 100)}],  # 空缺 10x100
    })
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["gap_area"] == pytest.approx(1000.0)
    assert any(x["code"] == "gap" for x in detail["reasons"])
    # 原版继续可读
    current = client.get("/v1/current").json()
    assert current["revision"] == 1
    parcels = {p["id"]: p["geometry"] for p in current["parcels"]}
    assert parcels["P2"] == P2


def test_replace_with_overlap_rejected(env):
    client, _ = env
    init_ok(client)
    r = client.put("/v1/parcels", json={
        "base_revision": 1,
        "parcels": [{"id": "P2", "geometry": square(40, 0, 100, 100)}],  # 与 P1 重叠 10x100
    })
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["overlap_parcel_ids"] == ["P1", "P2"]
    assert detail["overlaps"][0]["area"] == pytest.approx(1000.0)
    assert client.get("/v1/current").json()["revision"] == 1


def test_replace_outside_region_rejected(env):
    client, _ = env
    init_ok(client)
    r = client.put("/v1/parcels", json={
        "base_revision": 1,
        "parcels": [{"id": "P2", "geometry": square(50, 0, 110, 100)}],
    })
    assert r.status_code == 422
    codes = {x["code"] for x in r.json()["detail"]["reasons"]}
    assert "outside_region" in codes


def test_replace_unknown_or_duplicate_id(env):
    client, _ = env
    init_ok(client)
    r = client.put("/v1/parcels", json={
        "base_revision": 1,
        "parcels": [{"id": "P9", "geometry": square(0, 0, 1, 1)}],
    })
    assert r.status_code == 422
    assert any(x["code"] == "unknown_parcel_id" for x in r.json()["detail"]["reasons"])

    r = client.put("/v1/parcels", json={
        "base_revision": 1,
        "parcels": [{"id": "P1", "geometry": P1}, {"id": "P1", "geometry": P1}],
    })
    assert r.status_code == 422
    assert any(x["code"] == "duplicate_parcel_id" for x in r.json()["detail"]["reasons"])


def test_replace_stale_base_revision(env):
    client, _ = env
    init_ok(client)
    assert client.put("/v1/parcels", json={"base_revision": 1,
                                          "parcels": move_boundary_to(60)}).status_code == 200
    r = client.put("/v1/parcels", json={"base_revision": 1,
                                        "parcels": move_boundary_to(40)})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revision_conflict" and detail["current_revision"] == 2


def test_concurrent_same_base_only_one_wins(env):
    """同版并发修订：两个客户端同时基于 v1 提交，恰好一份成功。"""
    client, _ = env
    init_ok(client)

    results = []

    def worker(c, x):
        r = c.put("/v1/parcels", json={"base_revision": 1, "parcels": move_boundary_to(x)})
        results.append(r.status_code)

    with TestClient(app) as c1, TestClient(app) as c2:
        barrier = threading.Barrier(2)

        def run(c, x):
            barrier.wait()
            worker(c, x)

        t1 = threading.Thread(target=run, args=(c1, 60))
        t2 = threading.Thread(target=run, args=(c2, 40))
        t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 409], results
    assert client.get("/v1/current").json()["revision"] == 2
    rows = client.get("/v1/revisions").json()["revisions"]
    assert [r["revision"] for r in rows] == [1, 2]   # 没有半个第三版


# ---------------------------------------------------------------- 原子性 / 重启

def test_failed_commit_leaves_no_partial_version(env, monkeypatch):
    """提交中途崩溃：回滚干净，当前版本不变，也没有残留版本行。"""
    client, db = env
    init_ok(client)

    import app.main as main_mod

    original = main_mod._insert_version

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash mid-commit")

    monkeypatch.setattr(main_mod, "_insert_version", boom)
    r = client.put("/v1/parcels", json={"base_revision": 1,
                                      "parcels": move_boundary_to(60)})
    assert r.status_code == 500
    monkeypatch.setattr(main_mod, "_insert_version", original)

    assert client.get("/v1/current").json()["revision"] == 1
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM parcels WHERE revision > 1").fetchone()[0] == 0
        assert conn.execute("SELECT current_revision FROM meta").fetchone()[0] == 1
    finally:
        conn.close()

    # 崩溃后服务照常可用
    r = client.put("/v1/parcels", json={"base_revision": 1,
                                        "parcels": move_boundary_to(60)})
    assert r.status_code == 200 and r.json()["revision"] == 2


def test_restart_keeps_committed_version(env):
    """重启（重新启动应用、复用同一数据库文件）后版本完整可读。"""
    client, db = env
    init_ok(client)
    assert client.put("/v1/parcels", json={"base_revision": 1,
                                           "parcels": move_boundary_to(60)}).status_code == 200

    with TestClient(app) as client2:   # lifespan 重新执行 init_db（含防御性清理）
        current = client2.get("/v1/current").json()
        assert current["revision"] == 2
        wells = wells_by_id(current)
        assert wells["W-move"]["status"] == "pending"
        rows = client2.get("/v1/revisions").json()["revisions"]
        assert [r["revision"] for r in rows] == [1, 2]


# ---------------------------------------------------------------- 同版一致性

def test_current_is_one_consistent_version(env):
    client, _ = env
    init_ok(client)
    client.put("/v1/parcels", json={"base_revision": 1, "parcels": move_boundary_to(60)})
    current = client.get("/v1/current").json()
    rev = current["revision"]
    assert client.get("/v1/parcels").json()["revision"] == rev
    assert client.get("/v1/wells").json()["revision"] == rev
    # 同一响应里边界与井归属同版
    assert {w["id"] for w in current["wells"]} == {"W-in", "W-edge", "W-out", "W-move"}
    assert {p["id"] for p in current["parcels"]} == {"P1", "P2"}
