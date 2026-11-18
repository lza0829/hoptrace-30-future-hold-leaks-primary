from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import connect
from app.modules.future_ts_hold import (
    ClockError,
    HoldError,
    advance_clock,
    audit_clock_binding,
    get_clock,
    list_holds,
    list_promotion_batches,
    list_proofs,
    promote,
    set_clock,
)

router = APIRouter(prefix="/api")


class ClockBody(BaseModel):
    clock_ts: str
    advance: bool = False


class PromoteBody(BaseModel):
    keys: list[str] | None = None
    batch_id: str | None = None
    expected_clock_version: int | None = None
    expected_clock_ts: str | None = None


@router.get("/clock")
def read_clock():
    conn = connect()
    try:
        return get_clock(conn)
    finally:
        conn.close()


@router.post("/clock")
def write_clock(body: ClockBody):
    """配置业务钟。advance=true 只允许向前；否则可直接设定（用于对照分流）。

    无论前进还是回拨，都只影响之后的分流与晋升，不回写已正式点。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        apply = advance_clock if body.advance else set_clock
        clock = apply(conn, body.clock_ts)
        conn.commit()
        return {"ok": True, "clock": clock, "mode": "advance" if body.advance else "set"}
    except ClockError as e:
        conn.rollback()
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@router.get("/holds")
def read_holds(
    status: str | None = Query(default=None, pattern="^(held|promoted)$"),
    waybill_id: str | None = None,
):
    """暂缓集合只读查询；默认返回全部，页面按 status=held 看当前暂缓。"""
    conn = connect()
    try:
        return list_holds(conn, status=status, waybill_id=waybill_id)
    finally:
        conn.close()


@router.post("/holds/promote")
def promote_holds(body: PromoteBody | None = None):
    """显式晋升 / 到达扫描。到期判定绑定装载同一业务钟，业务钟未到的键拒绝晋升。

    可传 `expected_clock_version` / `expected_clock_ts` 固定扫描口径：
    与当前业务钟不一致时拒绝，防止读到另一套钟。
    """
    body = body or PromoteBody()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = promote(
            conn,
            batch_id=body.batch_id,
            keys=body.keys,
            expected_clock_version=body.expected_clock_version,
            expected_clock_ts=body.expected_clock_ts,
        )
        conn.commit()
        return {"ok": True, **result}
    except HoldError as e:
        conn.rollback()
        raise HTTPException(409, str(e)) from e
    except ClockError as e:
        conn.rollback()
        raise HTTPException(409, str(e)) from e
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@router.get("/hold-proofs")
def read_hold_proofs(
    batch_id: str | None = None,
    clock_version: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    """暂缓证明：按批次或钟版本可查，含业务钟、暂缓键、已晋升键、结论。"""
    conn = connect()
    try:
        return list_proofs(conn, batch_id=batch_id, clock_version=clock_version, limit=limit)
    finally:
        conn.close()


@router.get("/promotion-batches")
def read_promotion_batches(
    clock_version: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    """晋升批次不可变钟读数快照（只读）。"""
    conn = connect()
    try:
        return list_promotion_batches(conn, clock_version=clock_version, limit=limit)
    finally:
        conn.close()


@router.get("/clock-audit")
def read_clock_audit(waybill_id: str | None = None):
    """双钟核证（只读）：逐点核对装载钟与晋升钟同源，且正式线/暂缓抽屉互斥可见。

    基线所无的新增只读接口；任何写入路径都不会经过它。
    """
    conn = connect()
    try:
        return audit_clock_binding(conn, waybill_id=waybill_id)
    finally:
        conn.close()
