"""禁建修复第20号（BUGFIX-20）三红测试 —— 无单禁建 · 甲路径。

红测链（缺一不可）：
  甲  test_jia_no_bl_ticket_must_be_rejected
      锁定 POST /api/tickets：提单号在提单册无匹配时，不得落正式过磅票。
      正式票零增、拒写、留审计 ticket_reject_no_bl。
  乙  test_yi_reconcile_must_not_count_no_bl_ticket
      钉住 /api/reconcile：无提单票（历史孤儿）不得被对账计平/计入任何提单；
      无过磅票的提单不得回报 balanced=True。
  争用 test_contention_dual_sessions_same_bl
      两路会话同提单号并发抢建：须占 bl:<提单号> 锁，后到只留争用痕、零业务写。

先以本文件在未修复代码上见红，再动服务。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _lane(c: TestClient, h: dict, code: str) -> dict:
    lanes = c.get("/api/lanes", headers=h).json()["items"]
    return next(x for x in lanes if x["lane_code"] == code)


def _payload(lane_id: int, bl_ref: str, code: str) -> dict:
    return {
        "lane_id": lane_id,
        "bl_ref": bl_ref,
        "vehicle_plate": "测N·0020",
        "axle_weights": [{"axle": 1, "w": 6.0}, {"axle": 2, "w": 6.0}],
        "ticket_code": code,
    }


def test_jia_no_bl_ticket_must_be_rejected():
    """甲：提单无匹配 → 正式过磅票零增、拒写、留审计。"""
    c = TestClient(app)
    h = _auth(c)
    lane = _lane(c, h, "LN-N-01-IN")
    before = c.get("/api/tickets", headers=h).json()["items"]

    res = c.post(
        "/api/tickets",
        headers=h,
        json=_payload(lane["id"], "BL-GHOST-0020", "WT-NOBL-0020"),
    )
    assert res.status_code == 422, res.text

    after = c.get("/api/tickets", headers=h).json()["items"]
    assert len(after) == len(before), "无单禁建不得新增任何过磅票"
    assert not [t for t in after if t["bl_ref"] == "BL-GHOST-0020"]
    assert not [t for t in after if t["ticket_code"] == "WT-NOBL-0020"]

    audit = c.get("/api/audit?limit=80", headers=h).json()["items"]
    assert any(
        x["action"] == "ticket_reject_no_bl" and x["target"] == "BL-GHOST-0020"
        for x in audit
    ), "拒写须留审计痕"


def test_yi_reconcile_must_not_count_no_bl_ticket():
    """乙：对账不得计入无提单票；无过磅票的提单不得计平。"""
    c = TestClient(app)
    h = _auth(c)

    r = c.post(
        "/api/bl",
        headers=h,
        json={
            "bl_code": "BL-EMPTY-0020",
            "cargo_desc": "空单验证",
            "declared_gross_t": 30.0,
            "vessel_voyage": "验/020",
        },
    )
    assert r.status_code == 200, r.text

    # 历史遗留：修复前可落库的无提单正式票（夹具直接落库复现）
    with connect() as conn:
        lane = conn.execute(
            "SELECT id FROM weigh_lanes WHERE lane_code='LN-N-01-IN'"
        ).fetchone()
        conn.execute(
            """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
               axle_weights,gross_t,generation,status,adjudicated_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "WT-ORPHAN-0020",
                lane["id"],
                "BL-ORPHAN-0020",
                "测O·0020",
                '[{"axle": 1, "w": 3.75}, {"axle": 2, "w": 3.75}]',
                7.5,
                1,
                "已裁定",
                "2026-11-24T10:00:00",
                "2026-11-24T10:00:00",
            ),
        )
        conn.commit()

    allr = c.get("/api/reconcile", headers=h).json()
    assert "orphans" in allr, "无提单票须显式列出，不得静默吞掉"
    orphan_codes = [o["ticket_code"] for o in allr["orphans"]]
    assert "WT-ORPHAN-0020" in orphan_codes
    assert abs(float(allr["orphan_gross_t"]) - 7.5) < 1e-9
    for item in allr["items"]:
        assert all(
            t["ticket_code"] != "WT-ORPHAN-0020" for t in item["tickets"]
        ), "无提单票不得计入任何提单对账合计"

    one = c.get("/api/reconcile/BL-EMPTY-0020", headers=h).json()
    assert one["balanced"] is False, "无过磅票不得计平"
    assert float(one["ticket_sum_t"]) == 0.0
    assert "无过磅票" in one["reason"]


def test_contention_dual_sessions_same_bl():
    """争用：两路会话同提单号抢建 → 一成一拒，后到只留争用痕。

    以受控阻塞把首路会话钉在建票临界区内，第二路在锁被占期间并发打同一
    提单号，验证后到被判争用、零业务写、争用痕落在 bl:<提单号> 上。
    """
    c = TestClient(app)
    h_admin = _auth(c, "admin", "admin123")
    h_writer = _auth(c, "writer", "writer123")
    lane_a = _lane(c, h_admin, "LN-N-01-IN")
    lane_b = _lane(c, h_admin, "LN-N-02-IN")

    import tickets_svc

    real_adjudicate = tickets_svc.adjudicate_overload
    started = threading.Event()
    proceed = threading.Event()
    blocked_once = {"done": False}

    def gated_adjudicate(gross, threshold):
        if not blocked_once["done"]:
            blocked_once["done"] = True
            started.set()
            assert proceed.wait(timeout=10), "首路建票未被放行"
        return real_adjudicate(gross, threshold)

    tickets_svc.adjudicate_overload = gated_adjudicate

    results: dict[str, int] = {}

    def attempt(name: str, headers: dict, payload: dict) -> None:
        client = TestClient(app)
        results[name] = client.post(
            "/api/tickets", headers=headers, json=payload
        ).status_code

    t1 = threading.Thread(
        target=attempt,
        args=(
            "admin",
            h_admin,
            _payload(lane_a["id"], "BL-2026-0101", "WT-RACE-A-0020"),
        ),
    )
    t1.start()
    assert started.wait(timeout=10), "首路建票未进入临界区"

    # 首路正持 bl:BL-2026-0101 锁在建票；第二路同提单号并发抢建
    t2 = threading.Thread(
        target=attempt,
        args=(
            "writer",
            h_writer,
            _payload(lane_b["id"], "BL-2026-0101", "WT-RACE-B-0020"),
        ),
    )
    t2.start()
    t2.join(timeout=10)
    proceed.set()
    t1.join(timeout=10)
    tickets_svc.adjudicate_overload = real_adjudicate

    assert results == {"admin": 200, "writer": 409}, results

    cont = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == "bl:BL-2026-0101" and x["actor"] == "writer"
        for x in cont
    ), "后到须留提单锁争用痕"

    tickets = c.get("/api/tickets", headers=h_admin).json()["items"]
    raced = [
        t
        for t in tickets
        if t["ticket_code"] in ("WT-RACE-A-0020", "WT-RACE-B-0020")
    ]
    assert len(raced) == 1, "同提单号抢建只许落一张正式票"
    assert raced[0]["ticket_code"] == "WT-RACE-A-0020"


def test_day_lock_repeat_reject_same_day():
    """禁建命运当日锁死：同日同提单号再次尝试仍拒写，审计标记沿用首次判定。"""
    c = TestClient(app)
    h = _auth(c)
    lane = _lane(c, h, "LN-N-01-IN")
    payload = _payload(lane["id"], "BL-GHOST-LOCK-0020", "WT-NOBL-LOCK-0020")

    first = c.post("/api/tickets", headers=h, json=payload)
    assert first.status_code == 422
    second = c.post("/api/tickets", headers=h, json=payload)
    assert second.status_code == 422
    assert "锁死" in second.json()["detail"]

    audit = c.get("/api/audit?limit=80", headers=h).json()["items"]
    hits = [
        x for x in audit
        if x["action"] == "ticket_reject_no_bl" and x["target"] == "BL-GHOST-LOCK-0020"
    ]
    assert len(hits) == 2
    assert all(x["payload"]["verdict"] == "禁建" for x in hits)
    assert all(x["payload"]["locked_today"] is True for x in hits)
    assert sorted(x["payload"]["attempts"] for x in hits) == [1, 2]
    assert any(x["payload"]["replayed"] is True for x in hits)

    tickets = c.get("/api/tickets", headers=h).json()["items"]
    assert not [t for t in tickets if t["bl_ref"] == "BL-GHOST-LOCK-0020"]


def test_five_pages_same_source_order():
    """五页同源：总览→车道→对账→提单→过磅 读到同一无单口径与禁建判定。"""
    c = TestClient(app)
    h = _auth(c)
    lane = _lane(c, h, "LN-N-01-IN")
    assert (
        c.post(
            "/api/tickets",
            headers=h,
            json=_payload(lane["id"], "BL-GHOST-ORDER-0020", "WT-NOBL-ORDER-0020"),
        ).status_code
        == 422
    )
    with connect() as conn:
        conn.execute(
            """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
               axle_weights,gross_t,generation,status,adjudicated_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "WT-ORPHAN-ORDER-0020",
                lane["id"],
                "BL-ORPHAN-ORDER-0020",
                "测P·0020",
                '[{"axle": 1, "w": 2.5}, {"axle": 2, "w": 2.5}]',
                5.0,
                1,
                "已裁定",
                "2026-11-24T11:00:00",
                "2026-11-24T11:00:00",
            ),
        )
        conn.commit()

    # 同源顺序：总览 → 车道 → 对账 → 提单 → 过磅
    dash = c.get("/api/dashboard", headers=h).json()
    lanes = c.get("/api/lanes", headers=h).json()
    recon = c.get("/api/reconcile", headers=h).json()
    bl = c.get("/api/bl", headers=h).json()
    tk = c.get("/api/tickets", headers=h).json()

    views = [dash, lanes, recon, bl, tk]
    for v in views:
        assert v["orphan_count"] == 1, v
        assert abs(float(v["orphan_gross_t"]) - 5.0) < 1e-9, v

    assert dash["banned_today"] == lanes["banned_today"] == recon["banned_today"]
    assert bl["banned_today"] == tk["banned_today"] == dash["banned_today"]
    assert [b["target"] for b in dash["banned_today"]] == ["BL-GHOST-ORDER-0020"]

    lane_row = next(x for x in lanes["orphan_by_lane"] if x["lane_id"] == lane["id"])
    assert lane_row["count"] == 1
    assert abs(float(lane_row["gross_t"]) - 5.0) < 1e-9


def test_day_lock_survives_late_bl_registration():
    """禁建命运当日锁死：当日禁止后补登记提单，仍不得建正式票。"""
    c = TestClient(app)
    h = _auth(c)
    lane = _lane(c, h, "LN-N-01-IN")
    payload = _payload(lane["id"], "BL-LATE-0020", "WT-NOBL-LATE-0020")

    assert c.post("/api/tickets", headers=h, json=payload).status_code == 422

    # 当日补登同号提单：已锁死的命运不因补登记而漂移
    reg = c.post(
        "/api/bl",
        headers=h,
        json={
            "bl_code": "BL-LATE-0020",
            "cargo_desc": "迟登验证",
            "declared_gross_t": 12.0,
            "vessel_voyage": "迟/020",
        },
    )
    assert reg.status_code == 200, reg.text

    again = c.post("/api/tickets", headers=h, json=payload)
    assert again.status_code == 422
    assert "锁死" in again.json()["detail"]

    tickets = c.get("/api/tickets", headers=h).json()["items"]
    assert not [t for t in tickets if t["bl_ref"] == "BL-LATE-0020"]
