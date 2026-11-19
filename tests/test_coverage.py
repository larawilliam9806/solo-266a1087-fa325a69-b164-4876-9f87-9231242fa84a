"""污染覆盖分析 API 的端到端测试。

覆盖：
- 圆形覆盖面积、超限严格大于限值、超限优先、同类重叠只计一次；
- 共边井覆盖两侧、区域外井不贡献、未采样不视为达标；
- 三类面积之和等于宗地面积；
- 未知井/重复井号整批拒绝；参数有限性校验；
- 过期修订号 409 且不留报告；已完成报告在边界更新后保持原值；
- 报告 ID 回查与输入留痕。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point

from app.main import app

QUAD_SEGS = 64


def circle(x, y, r):
    """与服务端一致的圆形缓冲几何（quad_segs=64）。"""
    return Point(x, y).buffer(r, quad_segs=QUAD_SEGS)


@pytest.fixture()
def env(tmp_path, monkeypatch):
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
    {"id": "WA", "geometry": point(25, 50)},     # P1 内部
    {"id": "WB", "geometry": point(50, 50)},     # 共边上
    {"id": "WC", "geometry": point(75, 50)},     # P2 内部
    {"id": "WOUT", "geometry": point(150, 50)},  # 区域外
]


def init_ok(client, wells=None):
    r = client.post("/v1/init", json={
        "region": REGION,
        "parcels": [{"id": "P1", "geometry": P1}, {"id": "P2", "geometry": P2}],
        "wells": WELLS if wells is None else wells,
    })
    assert r.status_code == 201, r.text
    return r.json()


def analyze(client, **over):
    body = {"base_revision": 1, "radius": 10.0, "limit": 5.0, "samples": []}
    body.update(over)
    return client.post("/v1/coverage/analyze", json=body)


def parcels_by_id(payload):
    return {p["parcel_id"]: p for p in payload["parcels"]}


def disk_area(r):
    return circle(0, 0, r).area


# ---------------------------------------------------------------- 基础计算

def test_unsampled_when_no_samples(env):
    """无采样：全部面积计入未采样，达标面积为 0（未采样井不视为达标）。"""
    client, _ = env
    init_ok(client)
    r = analyze(client)
    assert r.status_code == 201, r.text
    data = r.json()
    for p in data["parcels"]:
        assert p["exceed_area"] == 0.0
        assert p["compliant_area"] == 0.0
        assert p["unsampled_area"] == pytest.approx(5000.0)
        assert p["exceed_area"] + p["compliant_area"] + p["unsampled_area"] == p["area"]
    assert data["totals"]["unsampled_area"] == pytest.approx(10000.0)


def test_compliant_circle_inside_parcel(env):
    """单口达标井，圆完全落在 P1 内：P1 达标面积为整圆，其余未采样。"""
    client, _ = env
    init_ok(client)
    r = analyze(client, samples=[{"well_id": "WA", "concentration": 5.0}])  # 等于限值 → 达标
    assert r.status_code == 201, r.text
    data = r.json()
    p1 = parcels_by_id(data)["P1"]
    assert p1["compliant_area"] == pytest.approx(disk_area(10), abs=1e-6)
    assert p1["exceed_area"] == 0.0
    assert p1["compliant_area"] + p1["unsampled_area"] == pytest.approx(p1["area"])
    assert parcels_by_id(data)["P2"]["compliant_area"] == 0.0


def test_strict_greater_than_limit_is_exceed(env):
    """浓度严格大于限值才超限；等于限值算达标。"""
    client, _ = env
    init_ok(client)
    r = analyze(client, samples=[{"well_id": "WA", "concentration": 5.0000001}])
    assert r.status_code == 201, r.text
    p1 = parcels_by_id(r.json())["P1"]
    assert p1["exceed_area"] == pytest.approx(disk_area(10), abs=1e-6)
    assert p1["compliant_area"] == 0.0


def test_edge_well_covers_both_sides(env):
    """共边井的圆与两侧宗地分别求交，两块各得半个圆，合计一个整圆。"""
    client, _ = env
    init_ok(client)
    r = analyze(client, samples=[{"well_id": "WB", "concentration": 1.0}])
    assert r.status_code == 201, r.text
    data = r.json()
    by_id = parcels_by_id(data)
    half = disk_area(10) / 2
    assert by_id["P1"]["compliant_area"] == pytest.approx(half, abs=1e-6)
    assert by_id["P2"]["compliant_area"] == pytest.approx(half, abs=1e-6)
    assert data["totals"]["compliant_area"] == pytest.approx(disk_area(10), abs=1e-6)


def test_same_class_overlap_counted_once(env):
    """两口同类井的圆重叠：并集只计一次，不是两圆面积相加。"""
    client, _ = env
    wells = [
        {"id": "W1", "geometry": point(20, 50)},
        {"id": "W2", "geometry": point(24, 50)},  # 圆心相距 4，两圆明显重叠
    ]
    init_ok(client, wells=wells)
    r = analyze(client, samples=[
        {"well_id": "W1", "concentration": 1.0},
        {"well_id": "W2", "concentration": 1.0},
    ])
    assert r.status_code == 201, r.text
    p1 = parcels_by_id(r.json())["P1"]
    assert p1["compliant_area"] < 2 * disk_area(10) - 1.0
    # 并集面积按与服务端一致的缓冲几何求交验证
    expected = circle(20, 50, 10).union(circle(24, 50, 10)).area
    assert p1["compliant_area"] == pytest.approx(expected, abs=1e-6)


def test_exceed_takes_priority_over_compliant(env):
    """超限圆与达标圆重叠：重叠部分计入超限，达标只剩差集。"""
    client, _ = env
    wells = [
        {"id": "WEX", "geometry": point(20, 50)},
        {"id": "WOK", "geometry": point(28, 50)},  # 圆心距 8
    ]
    init_ok(client, wells=wells)
    r = analyze(client, samples=[
        {"well_id": "WEX", "concentration": 9.0},
        {"well_id": "WOK", "concentration": 1.0},
    ])
    assert r.status_code == 201, r.text
    p1 = parcels_by_id(r.json())["P1"]
    # 超限圆完整计入
    assert p1["exceed_area"] == pytest.approx(disk_area(10), abs=1e-6)
    # 达标只算未被超限盖住的月牙部分（必然小于整圆、大于 0）
    assert 0.0 < p1["compliant_area"] < disk_area(10)
    assert p1["exceed_area"] + p1["compliant_area"] + p1["unsampled_area"] \
        == pytest.approx(p1["area"], abs=1e-9)


def test_outside_region_well_contributes_nothing(env):
    """区域外井整圆不贡献面积；报告在 excluded_wells 中列出。"""
    client, _ = env
    init_ok(client)
    r = analyze(client, radius=100.0, samples=[
        {"well_id": "WOUT", "concentration": 9.0},
    ])
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["excluded_wells"] == ["WOUT"]
    assert data["included_wells"] == []
    for p in data["parcels"]:
        assert p["exceed_area"] == 0.0
        assert p["compliant_area"] == 0.0
        assert p["unsampled_area"] == p["area"]


def test_circle_crossing_region_boundary_clipped(env):
    """井在区域内、圆伸出区域：只计与宗地（区域）相交的部分。"""
    client, _ = env
    wells = [{"id": "WEDGE", "geometry": point(2, 50)}]  # 距左边界 2，r=10
    init_ok(client, wells=wells)
    r = analyze(client, samples=[{"well_id": "WEDGE", "concentration": 1.0}])
    assert r.status_code == 201, r.text
    p1 = parcels_by_id(r.json())["P1"]
    # 圆被 x=0 裁掉一个弓形，落在区域内的面积必然小于整圆
    assert 0.0 < p1["compliant_area"] < disk_area(10)
    assert p1["compliant_area"] + p1["unsampled_area"] == pytest.approx(p1["area"])


def test_three_areas_sum_to_parcel_area(env):
    """混合场景：每块宗地三类面积之和严格等于宗地面积（JSON 数字层面）。"""
    client, _ = env
    init_ok(client)
    r = analyze(client, samples=[
        {"well_id": "WA", "concentration": 9.0},    # P1 内超限
        {"well_id": "WB", "concentration": 1.0},    # 共边达标
        {"well_id": "WC", "concentration": 2.0},    # P2 内达标
    ])
    assert r.status_code == 201, r.text
    data = r.json()
    for p in data["parcels"]:
        assert p["exceed_area"] + p["compliant_area"] + p["unsampled_area"] == p["area"]
    t = data["totals"]
    assert t["exceed_area"] + t["compliant_area"] + t["unsampled_area"] == t["area"]
    assert t["area"] == pytest.approx(10000.0)


# ---------------------------------------------------------------- 校验

def test_unknown_well_rejects_whole_batch(env):
    """未知井号整批拒绝（422），且不留报告。"""
    client, _ = env
    init_ok(client)
    r = analyze(client, samples=[
        {"well_id": "WA", "concentration": 1.0},
        {"well_id": "GHOST", "concentration": 1.0},
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert detail["unknown_well_ids"] == ["GHOST"]
    assert any(x["code"] == "unknown_well_id" for x in detail["reasons"])
    # 没有任何报告落库
    import sqlite3
    conn = sqlite3.connect(env[1])
    try:
        assert conn.execute("SELECT COUNT(*) FROM coverage_reports").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM coverage_samples").fetchone()[0] == 0
    finally:
        conn.close()


def test_duplicate_well_id_rejected(env):
    client, _ = env
    init_ok(client)
    r = analyze(client, samples=[
        {"well_id": "WA", "concentration": 1.0},
        {"well_id": "WA", "concentration": 2.0},
    ])
    assert r.status_code == 422
    assert any(x["code"] == "duplicate_well_id" for x in r.json()["detail"]["reasons"])


def test_parameter_validation(env):
    client, _ = env
    init_ok(client)
    headers = {"Content-Type": "application/json"}

    def raw(body: str):
        # NaN/Infinity 不是合法 JSON token，绕过客户端编码直接发原始内容，
        # 验证服务端对非有限浮点的拒绝
        return client.post("/v1/coverage/analyze", content=body, headers=headers)

    # 半径必须为有限正数
    for bad_radius in (0.0, -1.0):
        r = analyze(client, radius=bad_radius)
        assert r.status_code == 422, bad_radius
    for token in ("NaN", "Infinity", "-Infinity"):
        r = raw(f'{{"base_revision":1,"radius":{token},"limit":5.0,"samples":[]}}')
        assert r.status_code == 422, token

    # 限值必须为有限非负数
    r = analyze(client, limit=-0.1)
    assert r.status_code == 422
    for token in ("NaN", "Infinity"):
        r = raw(f'{{"base_revision":1,"radius":1.0,"limit":{token},"samples":[]}}')
        assert r.status_code == 422, token

    # 浓度必须为有限非负数
    r = analyze(client, samples=[{"well_id": "WA", "concentration": -0.01}])
    assert r.status_code == 422
    for token in ("NaN", "Infinity"):
        r = raw('{"base_revision":1,"radius":1.0,"limit":5.0,'
                f'"samples":[{{"well_id":"WA","concentration":{token}}}]}}')
        assert r.status_code == 422, token


def test_more_than_100_samples_rejected(env):
    client, _ = env
    init_ok(client)
    r = analyze(client, samples=[{"well_id": "WA", "concentration": 1.0}] * 101)
    assert r.status_code == 422


def test_analyze_before_init_404(env):
    client, _ = env
    assert analyze(client).status_code == 404


# ---------------------------------------------------------------- 修订号与报告

def test_stale_revision_rejected_without_report(env):
    """边界已更新后提交旧修订号：409，不留报告。"""
    client, _ = env
    init_ok(client)
    move = [{"id": "P1", "geometry": square(0, 0, 60, 100)},
            {"id": "P2", "geometry": square(60, 0, 100, 100)}]
    assert client.put("/v1/parcels",
                      json={"base_revision": 1, "parcels": move}).status_code == 200
    r = analyze(client, base_revision=1,
                samples=[{"well_id": "WA", "concentration": 1.0}])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 2 and detail["base_revision"] == 1

    import sqlite3
    conn = sqlite3.connect(env[1])
    try:
        assert conn.execute("SELECT COUNT(*) FROM coverage_reports").fetchone()[0] == 0
    finally:
        conn.close()


def test_report_persists_and_survives_later_revision(env):
    """报告保存输入/修订号/结果；边界再修订后，已完成报告保持原值可查。"""
    client, _ = env
    init_ok(client)
    r = analyze(client, samples=[{"well_id": "WA", "concentration": 9.0}])
    assert r.status_code == 201, r.text
    saved = r.json()
    report_id = saved["report_id"]
    assert saved["base_revision"] == 1
    assert saved["samples"] == [{"well_id": "WA", "concentration": 9.0}]
    assert saved["radius"] == 10.0 and saved["limit"] == 5.0

    # 边界发布新版本
    move = [{"id": "P1", "geometry": square(0, 0, 60, 100)},
            {"id": "P2", "geometry": square(60, 0, 100, 100)}]
    assert client.put("/v1/parcels",
                      json={"base_revision": 1, "parcels": move}).status_code == 200

    # 历史报告按原值回查，不被新边界改写
    got = client.get(f"/v1/coverage/reports/{report_id}")
    assert got.status_code == 200
    assert got.json() == saved

    # 基于新修订号可再出一份报告
    r2 = analyze(client, base_revision=2,
                 samples=[{"well_id": "WA", "concentration": 9.0}])
    assert r2.status_code == 201
    assert r2.json()["base_revision"] == 2
    assert r2.json()["report_id"] != report_id


def test_get_unknown_report_404(env):
    client, _ = env
    init_ok(client)
    r = client.get("/v1/coverage/reports/nonexistent-id")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "report_not_found"


def test_concurrent_revision_during_analysis_rejected(env, monkeypatch):
    """计算期间边界被其他请求抢先发布：提交时复核失败，409 且不留报告。"""
    client, db = env
    init_ok(client)

    import app.main as main_mod
    original = main_mod.compute_coverage

    def flaky(*args, **kwargs):
        # 模拟分析计算期间另一个写者发布了修订 2
        with TestClient(app) as other:
            move = [{"id": "P1", "geometry": square(0, 0, 60, 100)},
                    {"id": "P2", "geometry": square(60, 0, 100, 100)}]
            assert other.put("/v1/parcels",
                             json={"base_revision": 1, "parcels": move}).status_code == 200
        return original(*args, **kwargs)

    monkeypatch.setattr(main_mod, "compute_coverage", flaky)
    r = analyze(client, samples=[{"well_id": "WA", "concentration": 1.0}])
    assert r.status_code == 409
    monkeypatch.setattr(main_mod, "compute_coverage", original)

    import sqlite3
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM coverage_reports").fetchone()[0] == 0
        assert conn.execute("SELECT current_revision FROM meta").fetchone()[0] == 2
    finally:
        conn.close()


def test_restart_keeps_reports(env):
    """重启后报告仍可回查（建表 IF NOT EXISTS 兼容旧库）。"""
    client, _ = env
    init_ok(client)
    saved = analyze(client, samples=[{"well_id": "WC", "concentration": 3.0}]).json()

    with TestClient(app) as client2:
        got = client2.get(f"/v1/coverage/reports/{saved['report_id']}")
        assert got.status_code == 200
        assert got.json() == saved
