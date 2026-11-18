"""future_ts_hold 核心：装载闸 → 暂缓落库 → 显式晋升 → 暂缓证明。

不变量
------
1. 只有 `event_ts <= 业务钟` 的点才允许进入 hops（正式有效时间线）。
2. 未来点一律先落 `future_holds`（状态 held），可在页面/接口查到，
   业务钟走到后再经显式晋升或到达扫描进入正式线。
3. 装载与晋升都在单个事务里完成：分流失败整批回滚，晋升失败不留半截点；
   业务钟变更不回溯改写已正式点。
"""

import json
from datetime import datetime, timezone

from app.modules.future_ts_hold.clock import normalize_ts


class HoldError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hold_key_of(hop: dict) -> str:
    """暂缓键与 hops 的唯一键同构，保证幂等与晋升去重。"""
    return "|".join(
        [
            str(hop["waybill_id"]),
            str(hop["event_ts"]),
            str(hop["location_code"]),
            str(hop["event_code"]),
        ]
    )


def split_by_clock(hops: list[dict], clock_ts: str) -> tuple[list[dict], list[dict]]:
    """按业务钟分流：返回 (正式点, 未来点)。ts 非法直接抛错，由调用方整批回滚。"""
    formal: list[dict] = []
    future: list[dict] = []
    for hop in hops:
        try:
            ts = normalize_ts(hop["event_ts"])
        except Exception as exc:  # noqa: BLE001 - 统一收敛成装载失败
            raise HoldError(f"轨迹点 {hop.get('id')} 的 event_ts 非法：{exc}") from exc
        hop = {**hop, "event_ts": ts}
        if ts > clock_ts:
            future.append(hop)
        else:
            formal.append(hop)
    return formal, future


def insert_hold(conn, hop: dict, batch_id: str, clock: dict) -> str | None:
    """落暂缓。已晋升的同键点不再回退为 held，返回 None。"""
    key = hold_key_of(hop)
    conn.execute(
        """
        INSERT INTO future_holds(
          hold_key, waybill_id, event_ts, location_code, event_code, seq,
          batch_id, clock_ts, clock_version, due_ts, status, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?, 'held', ?)
        ON CONFLICT(hold_key) DO UPDATE SET
          batch_id=excluded.batch_id,
          clock_ts=excluded.clock_ts,
          clock_version=excluded.clock_version,
          due_ts=excluded.due_ts
        WHERE future_holds.status='held'
        """,
        (
            key,
            hop["waybill_id"],
            hop["event_ts"],
            hop["location_code"],
            hop["event_code"],
            hop.get("seq"),
            batch_id,
            clock["clock_ts"],
            clock["version"],
            hop["event_ts"],
            now_iso(),
        ),
    )
    return key if is_held(conn, key) else None


def is_held(conn, key: str) -> bool:
    row = conn.execute(
        "SELECT status FROM future_holds WHERE hold_key=?", (key,)
    ).fetchone()
    return bool(row and row["status"] == "held")


def held_keys(conn, waybill_id: str | None = None) -> list[str]:
    if waybill_id:
        rows = conn.execute(
            "SELECT hold_key FROM future_holds WHERE status='held' AND waybill_id=? ORDER BY event_ts, hold_key",
            (waybill_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT hold_key FROM future_holds WHERE status='held' ORDER BY event_ts, hold_key"
        ).fetchall()
    return [r["hold_key"] for r in rows]


def list_holds(conn, status: str | None = None, waybill_id: str | None = None) -> list[dict]:
    clauses = []
    params: list[str] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if waybill_id:
        clauses.append("waybill_id=?")
        params.append(waybill_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT hold_key, waybill_id, event_ts, location_code, event_code, seq,
               batch_id, clock_ts, clock_version, due_ts, status,
               created_at, promoted_at, promoted_clock_version, promoted_batch_id
        FROM future_holds {where}
        ORDER BY status ASC, event_ts ASC, hold_key ASC
        """,
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def promote(conn, *, batch_id: str | None = None, keys: list[str] | None = None) -> dict:
    """显式晋升：把业务钟已走到的暂缓点写入正式线并重算投影。

    - 未指定 keys 时按到达扫描：所有 `event_ts <= 当前业务钟` 的 held 点。
    - 全程单事务；任一点写入失败即整体回滚，正式线不会留半截点。
    """
    from app.services.timeline import recompute_projection

    clock = conn.execute(
        "SELECT clock_ts, version FROM clock_config WHERE id=1"
    ).fetchone()
    if clock is None:
        raise HoldError("业务钟未初始化")
    from datetime import datetime, timezone
    clock_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clock_version = clock["version"]

    if keys is not None:
        if not keys:
            raise HoldError("显式晋升必须给出至少一个暂缓键")
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT * FROM future_holds WHERE status='held' AND hold_key IN ({placeholders})",
            tuple(keys),
        ).fetchall()
        missing = set(keys) - {r["hold_key"] for r in rows}
        if missing:
            raise HoldError(f"暂缓键不可晋升：{sorted(missing)}")
        not_due = [r["hold_key"] for r in rows if r["event_ts"] > clock_ts]
        if not_due:
            raise HoldError(f"业务钟未到，拒绝晋升：{sorted(not_due)}")
        candidates = [dict(r) for r in rows]
    else:
        rows = conn.execute(
            "SELECT * FROM future_holds WHERE status='held' AND event_ts <= ? ORDER BY event_ts, hold_key",
            (clock_ts,),
        ).fetchall()
        candidates = [dict(r) for r in rows]

    promoted: list[str] = []
    touched: set[str] = set()
    for row in candidates:
        conn.execute(
            """
            INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
            VALUES (?,?,?,?,?,?, 'primary')
            """,
            (
                _hop_id_for(row),
                row["waybill_id"],
                row["event_ts"],
                row["location_code"],
                row["event_code"],
                row["seq"],
            ),
        )
        landed = conn.execute(
            """
            SELECT 1 FROM hops
            WHERE waybill_id=? AND event_ts=? AND location_code=? AND event_code=?
              AND channel='primary' AND retracted=0
            """,
            (row["waybill_id"], row["event_ts"], row["location_code"], row["event_code"]),
        ).fetchone()
        if not landed:
            raise HoldError(f"晋升写入正式线失败：{row['hold_key']}")
        conn.execute(
            """
            UPDATE future_holds
            SET status='promoted', promoted_at=?, promoted_clock_version=?, promoted_batch_id=?
            WHERE hold_key=? AND status='held'
            """,
            (now_iso(), clock_version, batch_id, row["hold_key"]),
        )
        promoted.append(row["hold_key"])
        touched.add(row["waybill_id"])

    for wid in sorted(touched):
        recompute_projection(conn, wid)

    conclusion = (
        f"业务钟 {clock_ts}(v{clock_version}) 下晋升 {len(promoted)} 个暂缓点；"
        "未到期点保持暂缓，未被写入正式投影"
        if promoted
        else f"业务钟 {clock_ts}(v{clock_version}) 下无到期待晋升点"
    )
    proof_id = write_proof(
        conn,
        scope="clock",
        scope_key=f"v{clock_version}",
        clock_ts=clock_ts,
        clock_version=clock_version,
        held=held_keys(conn),
        promoted=promoted,
        conclusion=conclusion,
    )
    return {
        "clock_ts": clock_ts,
        "clock_version": clock_version,
        "promoted": promoted,
        "remaining_held": held_keys(conn),
        "proof_id": proof_id,
        "conclusion": conclusion,
    }


def _hop_id_for(row: dict) -> str:
    """晋升落正式线的 hop id：由暂缓键派生，稳定且不与既有 id 冲突。"""
    import hashlib

    digest = hashlib.sha1(row["hold_key"].encode("utf-8")).hexdigest()[:12]
    return f"P-{digest}"


def write_proof(
    conn,
    *,
    scope: str,
    scope_key: str,
    clock_ts: str,
    clock_version: int,
    held: list[str],
    promoted: list[str],
    conclusion: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO hold_proofs(
          scope, scope_key, clock_ts, clock_version,
          held_keys_json, promoted_keys_json, conclusion, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            scope,
            scope_key,
            clock_ts,
            clock_version,
            json.dumps(held, ensure_ascii=False),
            json.dumps(promoted, ensure_ascii=False),
            conclusion,
            now_iso(),
        ),
    )
    return int(cur.lastrowid)


def list_proofs(
    conn,
    *,
    batch_id: str | None = None,
    clock_version: int | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses = []
    params: list = []
    if batch_id:
        clauses.append("scope='batch' AND scope_key=?")
        params.append(batch_id)
    if clock_version is not None:
        clauses.append("clock_version=?")
        params.append(clock_version)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM hold_proofs {where} ORDER BY id DESC LIMIT ?",
        tuple(params) + (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["held_keys"] = json.loads(d.pop("held_keys_json") or "[]")
        d["promoted_keys"] = json.loads(d.pop("promoted_keys_json") or "[]")
        d["held_count"] = len(d["held_keys"])
        d["promoted_count"] = len(d["promoted_keys"])
        out.append(d)
    return out
