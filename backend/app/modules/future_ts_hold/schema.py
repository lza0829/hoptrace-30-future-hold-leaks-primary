"""future_ts_hold 的落库结构。

落库形态选择：暂缓表（`future_holds`）挂住「暂缓键 + 应晋升时刻」，
而不是在 hops 上加 hold 标记列。理由：
- hops 保持「只装正式点」的不变量，正式时间线查询无需额外过滤；
- 暂缓集合天然可查、可审计，晋升时按 `event_ts <= 业务钟` 扫描；
- 晋升失败整趟回滚，不会在 hops 留下半截点。
"""

HOLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS clock_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  clock_ts TEXT NOT NULL,
  version INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS future_holds (
  hold_key TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  seq INTEGER,
  batch_id TEXT,
  clock_ts TEXT NOT NULL,
  clock_version INTEGER NOT NULL,
  due_ts TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'held' CHECK (status IN ('held', 'promoted')),
  created_at TEXT NOT NULL,
  promoted_at TEXT,
  promoted_clock_version INTEGER,
  promoted_batch_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_future_holds_status
  ON future_holds(status, event_ts);

CREATE TABLE IF NOT EXISTS hold_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL CHECK (scope IN ('batch', 'clock')),
  scope_key TEXT NOT NULL,
  clock_ts TEXT NOT NULL,
  clock_version INTEGER NOT NULL,
  held_keys_json TEXT NOT NULL DEFAULT '[]',
  promoted_keys_json TEXT NOT NULL DEFAULT '[]',
  conclusion TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hold_proofs_scope
  ON hold_proofs(scope, scope_key);
CREATE INDEX IF NOT EXISTS idx_hold_proofs_clock
  ON hold_proofs(clock_version);
"""
