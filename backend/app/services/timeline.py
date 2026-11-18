from app.seed import map_status


def list_primary(conn, waybill_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts ASC, id ASC
        """,
        (waybill_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def recompute_projection(conn, waybill_id: str) -> dict | None:
    """按正式有效时间线的末 hop 重算投影，并记录一条状态证明。

    只有正式线（channel='primary' 且未撤回）参与，暂缓点不影响投影。
    """
    row = conn.execute(
        """
        SELECT id, event_code
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts DESC, id DESC
        LIMIT 1
        """,
        (waybill_id,),
    ).fetchone()
    if row is None:
        return None
    proj = map_status(row["event_code"])
    current = conn.execute(
        "SELECT status_proj FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()
    conn.execute(
        "UPDATE waybills SET status_proj=? WHERE id=?", (proj, waybill_id)
    )
    if current is None or current["status_proj"] != proj:
        conn.execute(
            """
            INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at)
            VALUES (?,?,?,?,datetime('now'))
            """,
            (waybill_id, row["id"], row["event_code"], proj),
        )
    return {"last_hop_id": row["id"], "event_code": row["event_code"], "proj_value": proj}


def projection_summary(conn, waybill_id: str) -> dict:
    """给检查器用：正式线末 hop 与暂缓数。"""
    hops = list_primary(conn, waybill_id)
    held = conn.execute(
        "SELECT COUNT(*) AS c FROM future_holds WHERE waybill_id=? AND status='held'",
        (waybill_id,),
    ).fetchone()["c"]
    return {
        "primary_count": len(hops),
        "last_hop": hops[-1] if hops else None,
        "held_count": held,
    }
