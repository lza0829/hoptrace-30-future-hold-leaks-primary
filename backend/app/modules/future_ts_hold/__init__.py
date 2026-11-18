"""future_ts_hold — 业务钟闸门与未来点暂缓/晋升。

对外入口：
- split_by_clock / insert_hold：装载闸分流，未来点落暂缓表。
- promote：显式晋升或到达扫描，把到期点写入正式线并重算投影。
- list_holds / list_proofs：暂缓集合与暂缓证明只读查询。
"""

from app.modules.future_ts_hold.schema import HOLD_SCHEMA
from app.modules.future_ts_hold.clock import (
    ClockError,
    DEFAULT_CLOCK_TS,
    advance_clock,
    get_clock,
    normalize_ts,
    set_clock,
)
from app.modules.future_ts_hold.service import (
    HoldError,
    held_keys,
    hold_key_of,
    insert_hold,
    list_holds,
    list_proofs,
    promote,
    split_by_clock,
    write_proof,
)

__all__ = [
    "HOLD_SCHEMA",
    "ClockError",
    "DEFAULT_CLOCK_TS",
    "HoldError",
    "advance_clock",
    "get_clock",
    "held_keys",
    "hold_key_of",
    "insert_hold",
    "list_holds",
    "list_proofs",
    "normalize_ts",
    "promote",
    "set_clock",
    "split_by_clock",
    "write_proof",
]
