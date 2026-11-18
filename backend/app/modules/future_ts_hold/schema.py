"""future_ts_hold 的落库结构。

落库形态选择：
1. 暂缓表（`future_holds`）挂住「暂缓键 + 应晋升时刻」，而不是在 hops 上加
   hold 标记列——hops 保持「只装正式点」的不变量。
2. **晋升批次不可变钟读数快照列**（`promotion_batches.clock_ts` /
   `promotion_batches.clock_version`，以及 `future_holds.promoted_clock_ts` /
   `promoted_clock_version`）。晋升判定与写入正式线共用同一份业务钟读数，
   批次落库时把该读数冻结成快照，之后业务钟再怎么回拨/推进都不能改写已晋升点。
3. `clock_readings` 为每个业务钟版本保留不可变读数记录，晋升前校验扫描所用
   读数确实来自业务钟服务，而不是墙钟或伪造读数。
"""

HOLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS clock_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  clock_ts TEXT NOT NULL,
  version INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_readings (
  version INTEGER PRIMARY KEY,
  clock_ts TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'clock_config',
  recorded_at TEXT NOT NULL
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
  promoted_clock_ts TEXT,
  promoted_batch_id TEXT,
  promoted_promotion_batch TEXT
);

CREATE INDEX IF NOT EXISTS idx_future_holds_status
  ON future_holds(status, event_ts);

CREATE TABLE IF NOT EXISTS promotion_batches (
  id TEXT PRIMARY KEY,
  mode TEXT NOT NULL CHECK (mode IN ('explicit', 'scan', 'ingest')),
  clock_ts TEXT NOT NULL,
  clock_version INTEGER NOT NULL,
  requested_keys_json TEXT NOT NULL DEFAULT '[]',
  promoted_keys_json TEXT NOT NULL DEFAULT '[]',
  proof_id INTEGER,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_promotion_batches_clock
  ON promotion_batches(clock_version);

CREATE TABLE IF NOT EXISTS hold_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL CHECK (scope IN ('batch', 'clock')),
  scope_key TEXT NOT NULL,
  clock_ts TEXT NOT NULL,
  clock_version INTEGER NOT NULL,
  held_keys_json TEXT NOT NULL DEFAULT '[]',
  promoted_keys_json TEXT NOT NULL DEFAULT '[]',
  promotion_batch_id TEXT,
  conclusion TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hold_proofs_scope
  ON hold_proofs(scope, scope_key);
CREATE INDEX IF NOT EXISTS idx_hold_proofs_clock
  ON hold_proofs(clock_version);
"""

# 旧库补列：`CREATE TABLE IF NOT EXISTS` 不会给既有表加列。
_FUTURE_HOLDS_COLUMNS = {
    "promoted_clock_ts": "TEXT",
    "promoted_promotion_batch": "TEXT",
}
_HOLD_PROOFS_COLUMNS = {
    "promotion_batch_id": "TEXT",
}


def migrate_hold_schema(conn) -> None:
    for table, columns in (
        ("future_holds", _FUTURE_HOLDS_COLUMNS),
        ("hold_proofs", _HOLD_PROOFS_COLUMNS),
    ):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # 旧库回填不可变钟读数：只补缺，绝不覆盖已有读数。
    conn.execute(
        """
        INSERT OR IGNORE INTO clock_readings(version, clock_ts, source, recorded_at)
        SELECT clock_version, clock_ts, 'migrated_from_hold',
               COALESCE(MIN(created_at), '1970-01-01T00:00:00Z')
        FROM future_holds
        GROUP BY clock_version, clock_ts
        """
    )
