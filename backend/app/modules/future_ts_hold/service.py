"""future_ts_hold 核心：装载闸 → 暂缓落库 → 显式晋升 → 暂缓证明 → 双钟核证。

不变量
------
1. 只有 `event_ts <= 业务钟` 的点才允许进入 hops（正式有效时间线）。
2. 未来点一律先落 `future_holds`（状态 held），业务钟走到后再经显式晋升或
   到达扫描进入正式线。
3. 装载与晋升都绑定 `clock_config` 的同一份业务钟读数；晋升批次把该读数
   冻结成不可变快照（`promotion_batches` / `future_holds.promoted_clock_ts`），
   禁止用墙钟或另一套钟判定到期。
4. 每次晋升在单个事务里完成：分流失败整批回滚，晋升失败不留半截点；
   业务钟变更（含回拨）不回溯改写已正式点。
5. 任意轨迹点在任一时刻只能落在「正式线」或「暂缓抽屉」之一：
   既不允许双边可见，也不允许两边都不收；`audit_clock_binding` 只读核证。
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from app.modules.future_ts_hold.clock import (
    get_clock,
    normalize_ts,
    peek_clock,
    record_clock_reading,
    verify_clock_reading,
)


class HoldError(RuntimeError):
    pass


def now_iso() -> str:
    """审计元数据时间；不参与到期判定。"""
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
    """落暂缓。已晋升或已在正式线的同键点不再回退为 held，返回 None。

    同一点任一时刻只能落一边：若正式线已有该点（业务钟回拨后重放），
    绝不再进暂缓抽屉，避免同点双边可见。
    """
    key = hold_key_of(hop)
    already_formal = conn.execute(
        """
        SELECT 1 FROM hops
        WHERE waybill_id=? AND event_ts=? AND location_code=? AND event_code=?
          AND channel='primary' AND retracted=0
        """,
        (hop["waybill_id"], hop["event_ts"], hop["location_code"], hop["event_code"]),
    ).fetchone()
    if already_formal:
        return None
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
               created_at, promoted_at, promoted_clock_version, promoted_clock_ts,
               promoted_batch_id, promoted_promotion_batch
        FROM future_holds {where}
        ORDER BY status ASC, event_ts ASC, hold_key ASC
        """,
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def settle_formal_hold(conn, hop: dict, batch_id: str, clock: dict) -> str | None:
    """装载把某点写入正式线后，把同键的暂缓抽屉原子收口。

    业务钟回拨后重放装载时，原先暂缓的点可能已 `event_ts <= 装载钟` 直接落正式线；
    若此时暂缓抽屉仍挂着同一点，就会出现「同点双边可见」。这里用**装载钟读数**
    作为收口快照，并把该点标记为随装载收口（promotion_batches.mode='ingest'），
    保证正式线与暂缓抽屉任一点只在一侧可见。
    """
    key = hold_key_of(hop)
    row = conn.execute(
        "SELECT * FROM future_holds WHERE hold_key=? AND status='held'", (key,)
    ).fetchone()
    if row is None:
        return None

    settle_batch_id = f"PROM-{uuid.uuid4().hex[:10]}"
    clock_ts = normalize_ts(clock["clock_ts"])
    clock_version = int(clock["version"])
    record_clock_reading(conn, clock_ts, clock_version)
    now = now_iso()
    conn.execute(
        """
        INSERT INTO promotion_batches(
          id, mode, clock_ts, clock_version, requested_keys_json,
          promoted_keys_json, proof_id, created_at
        ) VALUES (?, 'ingest', ?,?,?,?, NULL, ?)
        """,
        (
            settle_batch_id,
            clock_ts,
            clock_version,
            json.dumps([key], ensure_ascii=False),
            json.dumps([key], ensure_ascii=False),
            now,
        ),
    )
    cur = conn.execute(
        """
        UPDATE future_holds
        SET status='promoted', promoted_at=?, promoted_clock_version=?,
            promoted_clock_ts=?, promoted_batch_id=?, promoted_promotion_batch=?
        WHERE hold_key=? AND status='held'
        """,
        (now, clock_version, clock_ts, batch_id, settle_batch_id, key),
    )
    if cur.rowcount != 1:
        raise HoldError(f"装载收口暂缓抽屉失败：{key}")
    return key


def list_promotion_batches(conn, clock_version: int | None = None, limit: int = 100) -> list[dict]:
    """晋升批次不可变钟读数快照（只读）。"""
    params: list = []
    where = ""
    if clock_version is not None:
        where = "WHERE clock_version=?"
        params.append(clock_version)
    rows = conn.execute(
        f"""
        SELECT id, mode, clock_ts, clock_version, requested_keys_json,
               promoted_keys_json, proof_id, created_at
        FROM promotion_batches {where}
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        tuple(params) + (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["requested_keys"] = json.loads(d.pop("requested_keys_json") or "[]")
        d["promoted_keys"] = json.loads(d.pop("promoted_keys_json") or "[]")
        d["promoted_count"] = len(d["promoted_keys"])
        out.append(d)
    return out


def promote(
    conn,
    *,
    batch_id: str | None = None,
    keys: list[str] | None = None,
    expected_clock_version: int | None = None,
    expected_clock_ts: str | None = None,
) -> dict:
    """显式晋升 / 到达扫描：把业务钟已走到的暂缓点写入正式线并重算投影。

    - 到期判定与写入使用 `clock_config` 的**同一份业务钟读数**（非墙钟）。
    - 调用方可传 `expected_clock_version` / `expected_clock_ts` 固定扫描口径；
      与当前业务钟不一致时拒绝晋升，避免读到不同版本的钟。
    - 未指定 keys 时按到达扫描：所有 `event_ts <= 当前业务钟` 的 held 点。
    - 全程单事务；任一点写入失败即整体回滚，正式线不会留半截点。
    """
    from app.services.timeline import recompute_projection

    clock = get_clock(conn)
    clock_ts = clock["clock_ts"]
    clock_version = int(clock["version"])
    record_clock_reading(conn, clock_ts, clock_version)

    if expected_clock_version is not None and int(expected_clock_version) != clock_version:
        raise HoldError(
            f"晋升钟版本不一致：期望 v{expected_clock_version}，当前 v{clock_version}"
        )
    if expected_clock_ts is not None and normalize_ts(expected_clock_ts) != clock_ts:
        raise HoldError(
            f"晋升钟读数不一致：期望 {normalize_ts(expected_clock_ts)}，当前 {clock_ts}"
        )
    # 核心防线：晋升读到的钟必须能对上不可变业务钟读数，墙钟/伪造读数直接拒绝。
    verify_clock_reading(conn, clock_ts, clock_version)

    mode = "explicit" if keys is not None else "scan"
    requested_keys = list(keys or [])
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

    promotion_batch_id = f"PROM-{uuid.uuid4().hex[:10]}" if candidates else None
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
        # 原子切换：正式点落库与暂缓出抽屉必须同时生效；
        # 条件 status='held' 确保不会被重复晋升，切换失败立即整趟回滚。
        cur = conn.execute(
            """
            UPDATE future_holds
            SET status='promoted', promoted_at=?, promoted_clock_version=?,
                promoted_clock_ts=?, promoted_batch_id=?, promoted_promotion_batch=?
            WHERE hold_key=? AND status='held'
            """,
            (
                now_iso(),
                clock_version,
                clock_ts,
                batch_id,
                promotion_batch_id,
                row["hold_key"],
            ),
        )
        if cur.rowcount != 1:
            raise HoldError(f"晋升切换暂缓状态失败：{row['hold_key']}")
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
        promotion_batch_id=promotion_batch_id,
    )
    if promotion_batch_id is not None:
        conn.execute(
            """
            INSERT INTO promotion_batches(
              id, mode, clock_ts, clock_version, requested_keys_json,
              promoted_keys_json, proof_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                promotion_batch_id,
                mode,
                clock_ts,
                clock_version,
                json.dumps(requested_keys, ensure_ascii=False),
                json.dumps(promoted, ensure_ascii=False),
                proof_id,
                now_iso(),
            ),
        )
    return {
        "clock_ts": clock_ts,
        "clock_version": clock_version,
        "clock_reading": {"clock_ts": clock_ts, "version": clock_version, "source": "clock_config"},
        "mode": mode,
        "promotion_batch_id": promotion_batch_id,
        "promoted": promoted,
        "remaining_held": held_keys(conn),
        "proof_id": proof_id,
        "conclusion": conclusion,
    }


def _hop_id_for(row: dict) -> str:
    """晋升落正式线的 hop id：由暂缓键派生，稳定且不与既有 id 冲突。"""
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
    promotion_batch_id: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO hold_proofs(
          scope, scope_key, clock_ts, clock_version,
          held_keys_json, promoted_keys_json, promotion_batch_id, conclusion, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            scope,
            scope_key,
            clock_ts,
            clock_version,
            json.dumps(held, ensure_ascii=False),
            json.dumps(promoted, ensure_ascii=False),
            promotion_batch_id,
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


# --------------------------------------------------------------------------
# 双钟核证（只读）
# --------------------------------------------------------------------------


def _formal_hop(conn, row: dict) -> dict | None:
    """正式线可见点（primary 且未撤回）；旁路/已撤回点不算可见。"""
    hit = conn.execute(
        """
        SELECT id, event_ts, event_code, channel, retracted
        FROM hops
        WHERE waybill_id=? AND event_ts=? AND location_code=? AND event_code=?
          AND channel='primary' AND retracted=0
        ORDER BY id ASC
        LIMIT 1
        """,
        (row["waybill_id"], row["event_ts"], row["location_code"], row["event_code"]),
    ).fetchone()
    return dict(hit) if hit else None


def audit_clock_binding(conn, waybill_id: str | None = None) -> dict:
    """双钟核证：逐点验证「装载钟读数」与「晋升钟读数」同源且互斥可见。

    只读，绝不写库。判定（按严重度取最高）：
    - double_visible：同一点既在正式线，又仍挂在暂缓抽屉；
    - double_invisible：暂缓已标记晋升，但正式线查无此点（两边都不收）；
    - clock_torn：装载/晋升读数对不上不可变业务钟读数，或晋升越过了业务钟；
    - unverifiable：缺少钟读数快照，无法核证；
    - ok：正式与暂缓恰好一边可见，且读数同源。
    """
    readings_rows = conn.execute(
        "SELECT version, clock_ts, source, recorded_at FROM clock_readings ORDER BY version"
    ).fetchall()
    readings = {int(r["version"]): dict(r) for r in readings_rows}

    clauses = []
    params: list[str] = []
    if waybill_id:
        clauses.append("waybill_id=?")
        params.append(waybill_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM future_holds {where} ORDER BY event_ts, hold_key",
        tuple(params),
    ).fetchall()

    points: list[dict] = []
    summary = {
        "checked": 0,
        "ok": 0,
        "double_visible": 0,
        "double_invisible": 0,
        "clock_torn": 0,
        "unverifiable": 0,
    }
    for row in rows:
        row = dict(row)
        reasons: list[str] = []
        hop = _formal_hop(conn, row)
        formal_visible = hop is not None
        drawer_visible = row["status"] == "held"

        # 1) 装载钟读数核证
        load_v = row.get("clock_version")
        load_reading = readings.get(int(load_v)) if load_v is not None else None
        if load_reading is None:
            reasons.append(f"装载钟版本 v{load_v} 无不可变读数")
        elif load_reading["clock_ts"] != row["clock_ts"]:
            reasons.append(
                f"装载钟读数不一致：抽屉记 {row['clock_ts']}，业务钟 v{load_v} 为 {load_reading['clock_ts']}"
            )
        elif row["status"] == "held" and row["event_ts"] <= row["clock_ts"]:
            reasons.append(
                f"暂缓点 event_ts {row['event_ts']} 不晚于装载钟 {row['clock_ts']}，不应被暂缓"
            )

        # 2) 晋升钟读数核证（快照列）
        if row["status"] == "promoted":
            prom_v = row.get("promoted_clock_version")
            prom_ts = row.get("promoted_clock_ts")
            prom_reading = readings.get(int(prom_v)) if prom_v is not None else None
            if prom_ts is None or prom_v is None:
                reasons.append("晋升快照缺钟读数（promoted_clock_ts/version）")
            elif prom_reading is None:
                reasons.append(f"晋升钟版本 v{prom_v} 无不可变读数")
            elif prom_reading["clock_ts"] != prom_ts:
                reasons.append(
                    f"晋升钟读数与业务钟 v{prom_v} 不一致：晋升记 {prom_ts}，业务钟为 {prom_reading['clock_ts']}"
                )
            elif row["event_ts"] > prom_ts:
                reasons.append(
                    f"晋升越过业务钟：event_ts {row['event_ts']} > 晋升钟 {prom_ts}"
                )
            batch = row.get("promoted_promotion_batch")
            if batch:
                b = conn.execute(
                    "SELECT clock_ts, clock_version FROM promotion_batches WHERE id=?",
                    (batch,),
                ).fetchone()
                if b is None:
                    reasons.append(f"晋升批次 {batch} 不存在")
                elif b["clock_ts"] != prom_ts or int(b["clock_version"]) != int(prom_v):
                    reasons.append(f"晋升批次 {batch} 快照与点快照不一致")
            elif row.get("promoted_at"):
                reasons.append("已晋升点缺少晋升批次号")

        # 3) 可见性互斥核证
        if formal_visible and drawer_visible:
            reasons.append("同点双边可见：正式线已收，暂缓抽屉仍挂点")
            verdict = "double_visible"
        elif not formal_visible and not drawer_visible:
            reasons.append("同点双边不可见：已出暂缓，但正式线查无此点")
            verdict = "double_invisible"
        elif reasons:
            torn = any("读数不一致" in r or "越过业务钟" in r or "不应被暂缓" in r for r in reasons)
            verdict = "clock_torn" if torn else "unverifiable"
        else:
            verdict = "ok"

        summary["checked"] += 1
        summary[verdict] = summary.get(verdict, 0) + 1
        points.append(
            {
                "hold_key": row["hold_key"],
                "waybill_id": row["waybill_id"],
                "event_ts": row["event_ts"],
                "status": row["status"],
                "load_clock_ts": row["clock_ts"],
                "load_clock_version": row["clock_version"],
                "load_clock_source": load_reading["source"] if load_reading else None,
                "promoted_clock_ts": row.get("promoted_clock_ts"),
                "promoted_clock_version": row.get("promoted_clock_version"),
                "promotion_batch_id": row.get("promoted_promotion_batch"),
                "formal_hop_id": hop["id"] if hop else None,
                "formal_visible": formal_visible,
                "drawer_visible": drawer_visible,
                "verdict": verdict,
                "reasons": reasons,
            }
        )

    counts = summary
    tear_free = not any(
        counts.get(k) for k in ("double_visible", "double_invisible", "clock_torn")
    )
    return {
        "clock": peek_clock(conn),
        "clock_readings": [dict(r) for r in readings_rows],
        "promotion_batches": list_promotion_batches(conn, limit=50),
        "summary": summary,
        "tear_free": tear_free,
        "conclusion": (
            "双钟同源，正式线与暂缓抽屉互斥可见"
            if tear_free
            else "检出暂缓/晋升双钟口径撕裂，详见 points"
        ),
        "points": points,
    }
