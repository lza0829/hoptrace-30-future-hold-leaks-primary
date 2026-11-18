"""可配置业务钟（唯一业务时刻来源）。

业务钟是「当前业务时刻」的唯一来源：装载闸按它分流暂缓/正式，
晋升扫描必须绑定**同一个** `clock_config` 读数来判定到期与写入晋升批次快照。
业务钟变更只影响之后的分流，不会回写、回滚已进入正式时间线的点。

每次业务钟版本变更都会追加一条不可变的 `clock_readings` 读数，
供双钟核证接口逐点验证「装载钟读数」与「晋升钟读数」是否同源。
"""

from datetime import datetime, timezone

DEFAULT_CLOCK_TS = "2026-11-17T20:00:00Z"


class ClockError(ValueError):
    pass


def normalize_ts(value: str) -> str:
    """把 ISO 时间规整成 `YYYY-MM-DDTHH:MM:SSZ`，便于字符串比较与落库。"""
    if not isinstance(value, str) or not value.strip():
        raise ClockError("业务钟必须是 ISO 时间字符串")
    raw = value.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClockError(f"业务钟格式非法：{value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wall_now() -> str:
    """仅用于审计元数据（updated_at / recorded_at），不参与任何到期判定。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def peek_clock(conn) -> dict:
    """只读业务钟；未初始化时返回默认读数，绝不落库（供只读核证接口使用）。"""
    row = conn.execute(
        "SELECT clock_ts, version, updated_at FROM clock_config WHERE id=1"
    ).fetchone()
    if row is None:
        return {
            "clock_ts": DEFAULT_CLOCK_TS,
            "version": 0,
            "updated_at": None,
            "initialized": False,
        }
    return {**dict(row), "initialized": True}


def record_clock_reading(conn, clock_ts: str, version: int) -> str:
    """追加一条不可变业务钟读数；同版本读数不一致时拒绝覆盖。"""
    ts = normalize_ts(clock_ts)
    existing = conn.execute(
        "SELECT clock_ts FROM clock_readings WHERE version=?", (version,)
    ).fetchone()
    if existing is not None:
        if existing["clock_ts"] != ts:
            raise ClockError(
                f"业务钟版本 v{version} 已有读数 {existing['clock_ts']}，"
                f"拒绝改写为 {ts}"
            )
        return ts
    conn.execute(
        """
        INSERT INTO clock_readings(version, clock_ts, source, recorded_at)
        VALUES (?,?, 'clock_config', ?)
        """,
        (version, ts, _wall_now()),
    )
    return ts


def verify_clock_reading(conn, clock_ts: str, version: int) -> None:
    """校验某版本业务钟读数是真实业务钟读数，而不是墙钟/伪造读数。"""
    row = conn.execute(
        "SELECT clock_ts FROM clock_readings WHERE version=?", (version,)
    ).fetchone()
    if row is None:
        raise ClockError(f"业务钟版本 v{version} 无不可变读数记录，拒绝晋升")
    expected = row["clock_ts"]
    actual = normalize_ts(clock_ts)
    if expected != actual:
        raise ClockError(
            f"晋升钟读数 {actual} 与业务钟版本 v{version} 的读数 {expected} 不一致，"
            "拒绝晋升"
        )


def get_clock(conn) -> dict:
    row = conn.execute(
        "SELECT clock_ts, version, updated_at FROM clock_config WHERE id=1"
    ).fetchone()
    if row is None:
        now = _wall_now()
        conn.execute(
            "INSERT INTO clock_config(id, clock_ts, version, updated_at) VALUES (1,?,?,?)",
            (DEFAULT_CLOCK_TS, 1, now),
        )
        record_clock_reading(conn, DEFAULT_CLOCK_TS, 1)
        row = conn.execute(
            "SELECT clock_ts, version, updated_at FROM clock_config WHERE id=1"
        ).fetchone()
    return dict(row)


def _bump_version(conn, clock_ts: str) -> dict:
    now = _wall_now()
    conn.execute(
        "UPDATE clock_config SET clock_ts=?, version=version+1, updated_at=? WHERE id=1",
        (clock_ts, now),
    )
    clock = get_clock(conn)
    record_clock_reading(conn, clock["clock_ts"], clock["version"])
    return clock


def advance_clock(conn, clock_ts: str) -> dict:
    """显式推进业务钟；版本 +1，不触碰正式时间线。"""
    target = normalize_ts(clock_ts)
    current = get_clock(conn)
    if target <= current["clock_ts"]:
        raise ClockError(
            f"业务钟只能向前推进：当前 {current['clock_ts']}，目标 {target}"
        )
    return _bump_version(conn, target)


def set_clock(conn, clock_ts: str) -> dict:
    """设置业务钟（允许回拨以观察分流差异），仅影响之后的分流/晋升。

    回拨只产生新的钟版本与读数，不会删除或改写任何已正式点。
    """
    target = normalize_ts(clock_ts)
    current = get_clock(conn)
    if target == current["clock_ts"]:
        return current
    return _bump_version(conn, target)
