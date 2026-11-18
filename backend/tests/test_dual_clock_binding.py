"""暂缓/晋升双钟口径核证：装载钟与晋升钟同源、互斥可见、失败闭、快照不可变。"""

import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="hoptrace-dualclock-"))
os.environ.setdefault("FIXTURES_DIR", str(Path(__file__).resolve().parents[2] / "fixtures"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB_PATH, connect  # noqa: E402
from app.main import app  # noqa: E402

HELD_1001 = "WB-1001|2026-11-17T19:20:00Z|HZ-STATION|OUT_FOR_DELIVERY"
HELD_1002 = "WB-1002|2026-11-17T10:00:00Z|SH-LOCAL|OUT_FOR_DELIVERY"


@pytest.fixture()
def client():
    if DB_PATH.exists():
        DB_PATH.unlink()
    with TestClient(app) as c:
        yield c


def _set_clock(c, ts):
    r = c.post("/api/clock", json={"clock_ts": ts})
    assert r.status_code == 200, r.text
    return r.json()["clock"]


def _ingest_and_hold(c):
    clock = _set_clock(c, "2026-11-17T09:30:00Z")
    rep = c.post("/api/ingest/run").json()
    assert rep["ok"] is True and rep["report"]["held"] == 2
    return clock


def test_audit_reports_ok_when_formal_and_drawer_are_exclusive(client):
    _ingest_and_hold(client)
    audit = client.get("/api/clock-audit").json()
    assert audit["tear_free"] is True
    assert audit["summary"]["checked"] == 2
    assert audit["summary"]["ok"] == 2
    assert all(p["drawer_visible"] and not p["formal_visible"] for p in audit["points"])

    promo_clock = _set_clock(client, "2026-11-17T19:30:00Z")
    promoted = client.post("/api/holds/promote", json={}).json()
    assert sorted(promoted["promoted"]) == sorted([HELD_1001, HELD_1002])

    audit = client.get("/api/clock-audit").json()
    assert audit["tear_free"] is True
    assert audit["summary"]["ok"] == 2
    wb1001 = [p for p in audit["points"] if p["waybill_id"] == "WB-1001"][0]
    assert wb1001["formal_visible"] is True and wb1001["drawer_visible"] is False
    assert wb1001["promoted_clock_ts"] == "2026-11-17T19:30:00Z"
    assert wb1001["promoted_clock_version"] == promo_clock["version"]
    assert wb1001["promotion_batch_id"]


def test_promotion_batch_freezes_immutable_clock_reading(client):
    _ingest_and_hold(client)
    promo_clock = _set_clock(client, "2026-11-17T19:30:00Z")
    res = client.post("/api/holds/promote", json={}).json()
    batch_id = res["promotion_batch_id"]
    assert batch_id

    by_clock = client.get(
        "/api/promotion-batches", params={"clock_version": promo_clock["version"]}
    ).json()
    assert len(by_clock) == 1
    snap = by_clock[0]
    assert snap["id"] == batch_id
    assert snap["clock_ts"] == "2026-11-17T19:30:00Z"
    assert snap["clock_version"] == promo_clock["version"]
    assert snap["mode"] == "scan"
    assert set(snap["promoted_keys"]) == {HELD_1001, HELD_1002}

    # 业务钟回拨：晋升批次快照与已正式点都不被改写
    _set_clock(client, "2026-11-17T08:00:00Z")
    again = client.get(
        "/api/promotion-batches", params={"clock_version": promo_clock["version"]}
    ).json()
    assert again[0]["clock_ts"] == "2026-11-17T19:30:00Z"

    audit = client.get("/api/clock-audit").json()
    assert audit["tear_free"] is True
    assert audit["summary"]["ok"] == 2

    # 回拨后重放装载：已晋升点不回暂缓，不会同点双边可见
    replay = client.post("/api/ingest/run").json()
    assert replay["report"]["held"] == 0
    assert replay["report"]["skipped_promoted"] == 2
    audit = client.get("/api/clock-audit").json()
    assert audit["tear_free"] is True


def test_promote_rejects_foreign_clock_reading_and_leaves_no_half_batch(client):
    load_clock = _ingest_and_hold(client)
    promo_clock = _set_clock(client, "2026-11-17T19:30:00Z")

    # 用旧钟版本读数发起晋升：直接拒绝，不产生晋升批次与正式点
    r = client.post(
        "/api/holds/promote",
        json={
            "expected_clock_version": load_clock["version"],
            "expected_clock_ts": load_clock["clock_ts"],
        },
    )
    assert r.status_code == 409
    assert client.get("/api/promotion-batches").json() == []
    assert {h["hold_key"] for h in client.get("/api/holds", params={"status": "held"}).json()} == {
        HELD_1001,
        HELD_1002,
    }
    tl = client.get("/api/waybills/WB-1001/timeline").json()
    assert [h["id"] for h in tl["hops"]] == ["H-1", "H-2", "H-3"]

    # 读数绑定正确后才放行
    ok = client.post(
        "/api/holds/promote",
        json={
            "expected_clock_version": promo_clock["version"],
            "expected_clock_ts": promo_clock["clock_ts"],
        },
    )
    assert ok.status_code == 200
    assert sorted(ok.json()["promoted"]) == sorted([HELD_1001, HELD_1002])


def test_promote_uses_business_clock_not_wall_clock(client):
    """墙钟（2026-11-18）晚于夹具 event_ts，但业务钟未到就不得晋升。"""
    _ingest_and_hold(client)
    r = client.post("/api/holds/promote", json={})
    assert r.status_code == 200
    assert r.json()["promoted"] == []
    assert client.get("/api/promotion-batches").json() == []


def test_audit_detects_double_visible_and_double_invisible(client):
    _ingest_and_hold(client)

    # 人为制造撕裂：同一点同时出现在正式线与暂缓抽屉
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
            VALUES ('X-1','WB-1001','2026-11-17T19:20:00Z','HZ-STATION','OUT_FOR_DELIVERY',4,'primary')
            """
        )
        # 人为制造「已出暂缓但正式线查无此点」
        conn.execute(
            """
            UPDATE future_holds
            SET status='promoted', promoted_at='2026-11-17T09:40:00Z',
                promoted_clock_version=2, promoted_clock_ts='2026-11-17T09:30:00Z',
                promoted_promotion_batch='PROM-FAKE'
            WHERE hold_key=?
            """,
            (HELD_1002,),
        )
        conn.commit()
    finally:
        conn.close()

    audit = client.get("/api/clock-audit").json()
    assert audit["tear_free"] is False
    verdicts = {p["waybill_id"]: p["verdict"] for p in audit["points"]}
    assert verdicts["WB-1001"] == "double_visible"
    assert verdicts["WB-1002"] == "double_invisible"
    assert audit["summary"]["double_visible"] == 1
    assert audit["summary"]["double_invisible"] == 1

    # 只读接口不得改写任何状态
    holds = {h["hold_key"]: h["status"] for h in client.get("/api/holds").json()}
    assert holds[HELD_1001] == "held"
    assert holds[HELD_1002] == "promoted"


def test_ingest_settles_hold_drawer_when_load_clock_admits_point(client):
    """业务钟回拨后再前进重放装载：装载钟已放行的点不得同时挂在暂缓抽屉。"""
    _ingest_and_hold(client)
    _set_clock(client, "2026-11-17T08:00:00Z")  # 回拨，点仍在暂缓

    load_clock = _set_clock(client, "2026-11-17T20:00:00Z")  # 前进但不显式晋升
    rep = client.post("/api/ingest/run").json()
    assert rep["ok"] is True
    assert rep["report"]["held"] == 0
    assert sorted(rep["report"]["settled_holds"]) == sorted([HELD_1001, HELD_1002])

    audit = client.get("/api/clock-audit").json()
    assert audit["tear_free"] is True
    assert audit["summary"]["ok"] == 2
    for p in audit["points"]:
        assert p["formal_visible"] is True
        assert p["drawer_visible"] is False
        assert p["promoted_clock_version"] == load_clock["version"]

    # 收口也留下不可变钟读数快照，模式标记为 ingest
    batches = client.get("/api/promotion-batches").json()
    assert len(batches) == 2
    assert {b["mode"] for b in batches} == {"ingest"}
    assert all(b["clock_ts"] == load_clock["clock_ts"] for b in batches)


def test_ingest_does_not_settle_holds_when_clock_still_behind(client):
    _ingest_and_hold(client)
    rep = client.post("/api/ingest/run").json()
    assert rep["report"]["held"] == 2
    assert rep["report"]["settled_holds"] == []
    audit = client.get("/api/clock-audit").json()
    assert audit["tear_free"] is True
    assert audit["summary"]["ok"] == 2


def test_clock_rewind_then_same_clock_replay_keeps_single_side(client):
    """回拨后再回拨到同一读数：不得改写已晋升点，也不得产生双边不可见。"""
    _ingest_and_hold(client)
    promo_clock = _set_clock(client, "2026-11-17T19:30:00Z")
    promoted = client.post("/api/holds/promote", json={}).json()
    assert sorted(promoted["promoted"]) == sorted([HELD_1001, HELD_1002])

    _set_clock(client, "2026-11-17T09:30:00Z")
    replay = client.post("/api/ingest/run").json()
    assert replay["report"]["skipped_promoted"] == 2
    audit = client.get("/api/clock-audit").json()
    assert audit["tear_free"] is True
    for p in audit["points"]:
        assert p["promoted_clock_version"] == promo_clock["version"]
        assert p["formal_visible"] is True and p["drawer_visible"] is False


def test_audit_is_read_only(client):
    load_clock = _ingest_and_hold(client)
    conn = connect()
    try:
        before = {
            "holds": [dict(r) for r in conn.execute("SELECT * FROM future_holds").fetchall()],
            "hops": [dict(r) for r in conn.execute("SELECT * FROM hops").fetchall()],
            "batches": [
                dict(r) for r in conn.execute("SELECT * FROM promotion_batches").fetchall()
            ],
            "readings": [
                dict(r) for r in conn.execute("SELECT * FROM clock_readings").fetchall()
            ],
        }
    finally:
        conn.close()

    client.get("/api/clock-audit")
    client.get("/api/clock-audit", params={"waybill_id": "WB-1001"})

    conn = connect()
    try:
        after = {
            "holds": [dict(r) for r in conn.execute("SELECT * FROM future_holds").fetchall()],
            "hops": [dict(r) for r in conn.execute("SELECT * FROM hops").fetchall()],
            "batches": [
                dict(r) for r in conn.execute("SELECT * FROM promotion_batches").fetchall()
            ],
            "readings": [
                dict(r) for r in conn.execute("SELECT * FROM clock_readings").fetchall()
            ],
        }
    finally:
        conn.close()
    assert before == after


def test_audit_flags_promotion_beyond_business_clock(client):
    load_clock = _ingest_and_hold(client)
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE future_holds
            SET status='promoted', promoted_at='2026-11-17T09:40:00Z',
                promoted_clock_version=?, promoted_clock_ts=?,
                promoted_promotion_batch='PROM-EARLY'
            WHERE hold_key=?
            """,
            (load_clock["version"], load_clock["clock_ts"], HELD_1001),
        )
        conn.execute(
            """
            INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
            VALUES ('P-EARLY','WB-1001','2026-11-17T19:20:00Z','HZ-STATION','OUT_FOR_DELIVERY',4,'primary')
            """
        )
        conn.commit()
    finally:
        conn.close()

    audit = client.get("/api/clock-audit").json()
    assert audit["tear_free"] is False
    point = [p for p in audit["points"] if p["hold_key"] == HELD_1001][0]
    assert point["verdict"] == "clock_torn"
    assert any("越过业务钟" in r for r in point["reasons"])
    assert any("晋升批次" in r for r in point["reasons"])
