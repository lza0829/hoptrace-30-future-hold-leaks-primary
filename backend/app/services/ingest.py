import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules.future_ts_hold import (
    get_clock,
    hold_key_of,
    insert_hold,
    split_by_clock,
    write_proof,
)
from app.services.timeline import recompute_projection


def run_batch(batch_path=None) -> dict:
    """装载闸：按业务钟把一批轨迹点分流为「正式」与「未来暂缓」。

    失败闭：分流在写入前完成，任何点的时间非法或写入失败都整批回滚，
    既不会让未来点污染正式投影，也不会留下写了一半的正式点。
    """
    path = batch_path or (FIXTURES_DIR / "ingest_batch.json")
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    cutoff = payload.get("cutoff_ts")
    hops = payload.get("hops", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        clock = get_clock(conn)
        formal, future = split_by_clock(hops, clock["clock_ts"])

        written = 0
        touched: set[str] = set()
        for hop in formal:
            wid = hop["waybill_id"]
            if not conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone():
                conn.execute(
                    "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                    (wid, hop.get("waybill_title") or wid, "未知", _now()),
                )
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hop["id"],
                    wid,
                    hop["event_ts"],
                    hop["location_code"],
                    hop["event_code"],
                    hop.get("seq"),
                ),
            )
            if cur.rowcount:
                written += 1
            touched.add(wid)

        held: list[str] = []
        skipped: list[str] = []
        for hop in future:
            key = insert_hold(conn, hop, batch_id, clock)
            if key:
                held.append(key)
            else:
                # 同键点已晋升（业务钟回拨后重放）：保持正式线不动，不退回暂缓。
                skipped.append(hold_key_of(hop))

        if held:
            # 未来点不进正式线，但运单索引需要能看到它们归属的运单。
            for hop in future:
                wid = hop["waybill_id"]
                if not conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone():
                    conn.execute(
                        "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                        (wid, hop.get("waybill_title") or wid, "未知", _now()),
                    )

        for wid in sorted(touched):
            recompute_projection(conn, wid)

        report = {
            "batch_id": batch_id,
            "cutoff_ts": cutoff,
            "clock_ts": clock["clock_ts"],
            "clock_version": clock["version"],
            "input": len(hops),
            "written": written,
            "held": len(held),
            "held_keys": held,
            "skipped_promoted": len(skipped),
            "skipped_keys": skipped,
        }
        conclusion = (
            f"业务钟 {clock['clock_ts']}(v{clock['version']})："
            f"正式写入 {written} 点，暂缓 {len(held)} 点"
            + (f"，{len(skipped)} 点已晋升不再回退" if skipped else "")
        )
        report["conclusion"] = conclusion
        write_proof(
            conn,
            scope="batch",
            scope_key=batch_id,
            clock_ts=clock["clock_ts"],
            clock_version=clock["version"],
            held=held,
            promoted=[],
            conclusion=conclusion,
        )
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, cutoff_ts, created_at, report_json) VALUES (?,?,?,?)",
            (batch_id, cutoff, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:  # noqa: BLE001 - 失败闭：整批回滚
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
