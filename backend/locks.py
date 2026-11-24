"""码头锁与争用登记（写入路径嵌入）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])

# 僵尸锁租期：持有超过该秒数的占锁可由后到接管并留痕，防止崩溃后永久锁死。
LOCK_TTL_SECONDS = 300


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def _age_seconds(acquired_at: str, now: str) -> float:
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(acquired_at)).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = now_iso()
    row = conn.execute(
        "SELECT holder FROM pier_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO pier_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now),
        )
        return True
    if row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder']}", now),
    )
    return False


def claim(conn, lock_key: str, holder: str, ttl_seconds: int = LOCK_TTL_SECONDS) -> bool:
    """原子占锁并立即提交：跨会话可见；后到只留争用痕，不写业务。

    须为该连接上的首个写语句调用，避免读快照升级写锁。
    同持有者重入视为已持有；超过 TTL 的僵尸锁由后到接管并留痕。
    """
    now = now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO pier_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
        (lock_key, holder, now),
    )
    conn.commit()
    if cur.rowcount == 1:
        return True
    row = conn.execute(
        "SELECT holder, acquired_at FROM pier_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        return False
    if row["holder"] == holder:
        return True
    if _age_seconds(row["acquired_at"], now) > ttl_seconds:
        conn.execute(
            "UPDATE pier_locks SET holder=?, acquired_at=? WHERE lock_key=?",
            (holder, now, lock_key),
        )
        conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, holder, "接管", f"过期接管（原持有者={row['holder']}）", now),
        )
        conn.commit()
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder']}", now),
    )
    conn.commit()
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM pier_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def release_claim(conn, lock_key: str, holder: str) -> None:
    """释放占锁并提交：跨会话可见。"""
    release(conn, lock_key, holder)
    conn.commit()


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pier_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(conn, user, "lock_acquire", body.lock_key, {"ok": ok})
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}