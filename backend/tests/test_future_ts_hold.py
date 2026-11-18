"""future_ts_hold 端到端测试：业务钟分流、暂缓落库、显式晋升、失败闭、证明接口。"""

import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="hoptrace-test-"))
os.environ.setdefault("FIXTURES_DIR", str(Path(__file__).resolve().parents[2] / "fixtures"))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB_PATH  # noqa: E402
from app.main import app  # noqa: E402
from app.services import ingest as ingest_svc  # noqa: E402

HELD_1001 = "WB-1001|2026-11-17T19:20:00Z|HZ-STATION|OUT_FOR_DELIVERY"
HELD_1002 = "WB-1002|2026-11-17T10:00:00Z|SH-LOCAL|OUT_FOR_DELIVERY"


@pytest.fixture()
def client():
    # 每个用例一份干净库：启动事件会建表并灌夹具。
    if DB_PATH.exists():
        DB_PATH.unlink()
    with TestClient(app) as c:
        yield c


def _set_clock(c, ts):
    r = c.post("/api/clock", json={"clock_ts": ts})
    assert r.status_code == 200, r.text
    return r.json()["clock"]


def test_clock_before_batch_defers_all(client):
    _set_clock(client, "2026-11-17T09:30:00Z")
    rep = client.post("/api/ingest/run").json()
    assert rep["ok"] is True
    assert rep["report"]["written"] == 0
    assert rep["report"]["held"] == 2

    tl = client.get("/api/waybills/WB-1001/timeline").json()
    assert [h["id"] for h in tl["hops"]] == ["H-1", "H-2", "H-3"]
    assert tl["waybill"]["status_proj"] == "到站"
    assert [h["hold_key"] for h in tl["held"]] == [HELD_1001]

    held = client.get("/api/holds", params={"status": "held"}).json()
    assert {h["hold_key"] for h in held} == {HELD_1001, HELD_1002}
    assert all(h["due_ts"] == h["event_ts"] for h in held)


def test_promote_rejected_before_clock_and_after_clock_promotes(client):
    _set_clock(client, "2026-11-17T09:30:00Z")
    client.post("/api/ingest/run")

    r = client.post("/api/holds/promote", json={"keys": [HELD_1001]})
    assert r.status_code == 409
    assert client.post("/api/holds/promote", json={}).json()["promoted"] == []

    _set_clock(client, "2026-11-17T19:30:00Z")
    scan = client.post("/api/holds/promote", json={}).json()
    assert sorted(scan["promoted"]) == sorted([HELD_1001, HELD_1002])

    tl = client.get("/api/waybills/WB-1001/timeline").json()
    assert [h["event_ts"] for h in tl["hops"]][-1] == "2026-11-17T19:20:00Z"
    assert tl["waybill"]["status_proj"] == "派送中"
    assert tl["held"] == []


def test_clock_rewind_does_not_rewrite_formal_timeline(client):
    _set_clock(client, "2026-11-17T09:30:00Z")
    client.post("/api/ingest/run")
    _set_clock(client, "2026-11-17T19:30:00Z")
    client.post("/api/holds/promote", json={})
    before = client.get("/api/waybills/WB-1001/timeline").json()

    _set_clock(client, "2026-11-17T08:00:00Z")
    after = client.get("/api/waybills/WB-1001/timeline").json()
    assert [h["id"] for h in before["hops"]] == [h["id"] for h in after["hops"]]
    assert after["waybill"]["status_proj"] == "派送中"

    replay = client.post("/api/ingest/run").json()
    assert replay["report"]["held"] == 0
    assert replay["report"]["skipped_promoted"] == 2
    again = client.get("/api/waybills/WB-1001/timeline").json()
    assert [h["id"] for h in again["hops"]] == [h["id"] for h in before["hops"]]


def test_invalid_ts_fails_closed_whole_batch(client, tmp_path):
    _set_clock(client, "2026-11-17T09:30:00Z")
    good = client.post("/api/ingest/run").json()
    assert good["ok"] is True

    bad = tmp_path / "bad_batch.json"
    bad.write_text(
        json.dumps(
            {
                "batch_id": "B-BAD",
                "hops": [
                    {
                        "id": "H-BAD-1",
                        "waybill_id": "WB-1001",
                        "event_ts": "2026-11-17T23:00:00Z",
                        "location_code": "X",
                        "event_code": "DELIVERED",
                    },
                    {
                        "id": "H-BAD-2",
                        "waybill_id": "WB-1001",
                        "event_ts": "not-a-time",
                        "location_code": "X",
                        "event_code": "DELIVERED",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    res = ingest_svc.run_batch(bad)
    assert res["ok"] is False

    held = client.get("/api/holds", params={"status": "held"}).json()
    assert all(h["hold_key"] != "WB-1001|2026-11-17T23:00:00Z|X|DELIVERED" for h in held)
    tl = client.get("/api/waybills/WB-1001/timeline").json()
    assert all(h["id"] not in ("H-BAD-1", "H-BAD-2") for h in tl["hops"])
    assert tl["waybill"]["status_proj"] == "到站"


def test_hold_proofs_query_by_batch_and_clock_version(client):
    clock = _set_clock(client, "2026-11-17T09:30:00Z")
    client.post("/api/ingest/run")

    by_batch = client.get("/api/hold-proofs", params={"batch_id": "B-DEMO-01"}).json()
    assert len(by_batch) == 1
    p = by_batch[0]
    assert p["scope"] == "batch"
    assert p["clock_version"] == clock["version"]
    assert set(p["held_keys"]) == {HELD_1001, HELD_1002}
    assert p["promoted_keys"] == []
    assert "暂缓 2 点" in p["conclusion"]

    _set_clock(client, "2026-11-17T19:30:00Z")
    client.post("/api/holds/promote", json={})
    by_clock = client.get("/api/hold-proofs", params={"clock_version": clock["version"] + 1}).json()
    assert by_clock
    assert by_clock[0]["promoted_count"] == 2
    assert "晋升 2 个暂缓点" in by_clock[0]["conclusion"]
