"""过磅票：轴重采集与裁定入闸。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, no_bl_state, now_iso, write_audit
from locks import claim, release_claim
from models import TicketIn
from validators import (
    adjudicate_overload,
    validate_axle_count,
    validate_axle_weights,
    validate_gross_cap,
    validate_lane_open,
)

router = APIRouter(tags=["tickets"])


def _next_ticket_code(conn, lane_id: int) -> str:
    lane = conn.execute(
        "SELECT lane_code FROM weigh_lanes WHERE id=?", (lane_id,)
    ).fetchone()
    prefix = lane["lane_code"] if lane else f"LN-{lane_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM weigh_tickets WHERE lane_id=?", (lane_id,)
    ).fetchone()["c"]
    return f"WT-{prefix}-{n + 1:03d}"


NO_BL_REJECT = "ticket_reject_no_bl"
NO_BL_REASON = "提单号在提单册无匹配，无单禁建；正式过磅票零增"


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except Exception:  # pragma: no cover - 连接已断时静默
        pass


def _today_ban(conn, bl_ref: str) -> dict | None:
    """当日已生效的禁建判定（命运锁死口径）。"""
    day = now_iso()[:10]
    row = conn.execute(
        """SELECT target, MIN(created_at) first_at, MAX(created_at) last_at,
                  COUNT(*) attempts
           FROM audit_trail
           WHERE action=? AND target=? AND created_at LIKE ?
           GROUP BY target""",
        (NO_BL_REJECT, bl_ref, day + "%"),
    ).fetchone()
    return dict(row) if row else None


def _reject_no_bl(conn, user: str, bl_ref: str) -> None:
    """甲路径：无匹配提单 → 正式票零增拒写，并当日锁死禁建命运。

    首次拒写写入审计；同一自然日对同一提单号的后续请求一律沿用首次判定，
    不再重新裁决，仅累加尝试次数——当日命运锁死，不得因重试或补登漂移。
    """
    now = now_iso()
    day = now[:10]
    prior = _today_ban(conn, bl_ref)
    locked = prior is not None
    write_audit(
        conn,
        user,
        NO_BL_REJECT,
        bl_ref,
        {
            "day": day,
            "reason": NO_BL_REASON,
            "verdict": "禁建",
            "locked_today": True,
            "attempts": (prior["attempts"] if prior else 0) + 1,
            "replayed": locked,
            "first_at": prior["first_at"] if prior else now,
        },
    )
    conn.commit()
    detail = NO_BL_REASON
    if locked:
        detail += "（当日命运已锁死，沿用首次拒写判定）"
    raise HTTPException(422, detail)


@router.get("/api/tickets")
def list_tickets(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT t.*, l.lane_code, b.bridge_code FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id"""
        if status:
            rows = conn.execute(
                q + " WHERE t.status=? ORDER BY t.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY t.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["axle_weights"] = json.loads(d["axle_weights"] or "[]")
            items.append(d)
        state = no_bl_state(conn)
        return {
            "items": items,
            "banned_today": state["banned_today"],
            "banned_codes": state["banned_codes"],
            "orphan_count": state["orphan_count"],
            "orphan_gross_t": state["orphan_gross_t"],
        }


@router.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT t.*, l.lane_code, l.overload_threshold_t, b.name bridge_name
               FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id
               WHERE t.id=?""",
            (ticket_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "过磅票不存在")
        data = dict(row)
        data["axle_weights"] = json.loads(data["axle_weights"] or "[]")
        holds = conn.execute(
            "SELECT * FROM overload_holds WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM gate_seals WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM gate_releases WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        return {
            "ticket": data,
            "overload_holds": [dict(h) for h in holds],
            "gate_seals": [dict(s) for s in seals],
            "gate_releases": [dict(r) for r in releases],
        }


@router.post("/api/tickets")
def create_ticket(body: TicketIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    bl_lock = f"bl:{body.bl_ref}"
    lane_lock = f"lane:{body.lane_id}"
    with connect() as conn:
        if not claim(conn, bl_lock, user):
            raise HTTPException(409, "争用：该提单正在建票，后到只记争用，不写业务")
        lane_held = False
        try:
            lane_held = claim(conn, lane_lock, user)
            if not lane_held:
                raise HTTPException(409, "争用：车道被占用，后到只记争用，不写业务")
            lane = conn.execute(
                """SELECT l.*, b.max_gross_t, b.axle_limit FROM weigh_lanes l
                   JOIN weigh_bridges b ON b.id=l.bridge_id WHERE l.id=?""",
                (body.lane_id,),
            ).fetchone()
            if not lane:
                raise HTTPException(404, "车道不存在")
            validate_lane_open(dict(lane))
            axles = [a.model_dump() for a in body.axle_weights]
            validate_axle_count(axles, int(lane["axle_limit"]))
            gross, _peak, _spread = validate_axle_weights(axles)
            validate_gross_cap(gross, float(lane["max_gross_t"]))
            # 禁建修复（甲）：命运当日锁死优先；提单册无匹配即拒写，正式票零增
            if _today_ban(conn, body.bl_ref):
                _reject_no_bl(conn, user, body.bl_ref)
            bl = conn.execute(
                "SELECT id FROM bl_manifests WHERE bl_code=?", (body.bl_ref,)
            ).fetchone()
            if not bl:
                _reject_no_bl(conn, user, body.bl_ref)
            is_over, overweight = adjudicate_overload(
                gross, float(lane["overload_threshold_t"])
            )
            code = body.ticket_code or _next_ticket_code(conn, body.lane_id)
            if conn.execute(
                "SELECT id FROM weigh_tickets WHERE ticket_code=?", (code,)
            ).fetchone():
                raise HTTPException(409, "票号已存在")
            now = now_iso()
            if is_over:
                status = "旁路挂起"
            else:
                status = "已裁定"
            cur = conn.execute(
                """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
                   axle_weights,gross_t,generation,status,adjudicated_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.lane_id,
                    body.bl_ref,
                    body.vehicle_plate,
                    json.dumps(axles, ensure_ascii=False),
                    gross,
                    1,
                    status,
                    now if status == "已裁定" else None,
                    now,
                ),
            )
            ticket_id = cur.lastrowid
            if is_over:
                hold_key = f"HOLD-{code}"
                conn.execute(
                    """INSERT INTO overload_holds(ticket_id,hold_key,overweight_t,threshold_t,
                       status,opened_at,side_lane_note) VALUES (?,?,?,?,?,?,?)""",
                    (
                        ticket_id,
                        hold_key,
                        overweight,
                        float(lane["overload_threshold_t"]),
                        "开放",
                        now,
                        lane["side_lane_ref"] or "旁路待驳",
                    ),
                )
            write_audit(
                conn,
                user,
                "ticket_weigh",
                code,
                {
                    "ticket_id": ticket_id,
                    "gross_t": gross,
                    "status": status,
                    "overload": is_over,
                },
            )
            conn.commit()
            return {
                "id": ticket_id,
                "ticket_code": code,
                "gross_t": gross,
                "status": status,
                "overload": is_over,
            }
        except BaseException:
            # 业务写中途失败须回滚，避免半张票落库；占锁随后释放
            _rollback_quietly(conn)
            raise
        finally:
            if lane_held:
                release_claim(conn, lane_lock, user)
            release_claim(conn, bl_lock, user)
