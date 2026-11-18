"""可配置业务钟。

业务钟是「当前业务时刻」的唯一来源：装载闸按它分流暂缓/正式，
到达扫描按它判定暂缓点是否到期。业务钟变更只影响之后的分流，
不会回写、回滚已进入正式时间线的点。
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


def get_clock(conn) -> dict:
    row = conn.execute(
        "SELECT clock_ts, version, updated_at FROM clock_config WHERE id=1"
    ).fetchone()
    if row is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO clock_config(id, clock_ts, version, updated_at) VALUES (1,?,?,?)",
            (DEFAULT_CLOCK_TS, 1, now),
        )
        row = conn.execute(
            "SELECT clock_ts, version, updated_at FROM clock_config WHERE id=1"
        ).fetchone()
    return dict(row)


def _bump_version(conn, clock_ts: str) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE clock_config SET clock_ts=?, version=version+1, updated_at=? WHERE id=1",
        (clock_ts, now),
    )
    return get_clock(conn)


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
    """设置业务钟（允许回拨以观察分流差异），仅影响之后的分流/晋升。"""
    target = normalize_ts(clock_ts)
    current = get_clock(conn)
    if target == current["clock_ts"]:
        return current
    return _bump_version(conn, target)
