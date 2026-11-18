"""future_ts_hold — 业务钟闸门与未来点暂缓/晋升。

对外入口：
- split_by_clock / insert_hold：装载闸分流，未来点落暂缓表。
- promote：显式晋升或到达扫描，把到期点写入正式线并重算投影；
  晋升与装载绑定同一业务钟读数，并把读数冻结进晋升批次快照。
- list_holds / list_proofs / list_promotion_batches：暂缓、证明与晋升批次只读查询。
- audit_clock_binding：双钟核证只读接口（装载钟 vs 晋升钟）。
"""

from app.modules.future_ts_hold.schema import HOLD_SCHEMA, migrate_hold_schema
from app.modules.future_ts_hold.clock import (
    ClockError,
    DEFAULT_CLOCK_TS,
    advance_clock,
    get_clock,
    normalize_ts,
    peek_clock,
    record_clock_reading,
    set_clock,
    verify_clock_reading,
)
from app.modules.future_ts_hold.service import (
    HoldError,
    audit_clock_binding,
    held_keys,
    hold_key_of,
    insert_hold,
    list_holds,
    list_promotion_batches,
    list_proofs,
    promote,
    settle_formal_hold,
    split_by_clock,
    write_proof,
)

__all__ = [
    "HOLD_SCHEMA",
    "ClockError",
    "DEFAULT_CLOCK_TS",
    "HoldError",
    "advance_clock",
    "audit_clock_binding",
    "get_clock",
    "held_keys",
    "hold_key_of",
    "insert_hold",
    "list_holds",
    "list_promotion_batches",
    "list_proofs",
    "migrate_hold_schema",
    "normalize_ts",
    "peek_clock",
    "promote",
    "record_clock_reading",
    "set_clock",
    "settle_formal_hold",
    "split_by_clock",
    "verify_clock_reading",
    "write_proof",
]
