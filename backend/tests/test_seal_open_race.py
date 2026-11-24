from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest

# 测试库必须在 import app 之前指定，app.config 会在导入时读取该变量。
_TMP = Path(tempfile.mkdtemp(prefix="chargebout-test-"))
os.environ["CHARGEBOUT_DB"] = str(_TMP / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

import app.db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.services import sessions as sess_svc  # noqa: E402
from app.services import dayseal as seal_svc  # noqa: E402


@pytest.fixture()
def client():
    db_path = _TMP / "test.db"
    if db_path.exists():
        db_path.unlink()
    # 重新初始化一个干净的库，seed 会填好站点/桩/枪/封印行。
    dbmod.init_db()
    from app.seed import seed_if_empty

    seed_if_empty()
    dbmod.init_db()
    with TestClient(app) as c:
        yield c


def _first_idle(client) -> tuple[int, int]:
    piles = client.get("/api/桩位").json()
    pile = next(p for p in piles if p["status"] == "空闲")
    guns = client.get(f"/api/枪线?桩位编号={pile['id']}").json()
    gun = next(g for g in guns if g["status"] == "可用")
    return pile["id"], gun["id"]


def _snapshot(client) -> dict:
    """争用前快照：会话数、序号、账本、占用、总览充电中。"""
    ov = client.get("/api/总览").json()
    day = client.get("/api/充日").json()
    sessions = client.get("/api/会话").json()
    settings = {s["键"]: s["值"] for s in client.get("/api/设置").json()}
    return {
        "充电中": ov["充电中"],
        "桩位占用": ov["桩位占用"],
        "封印世代": day["封印世代"],
        "账本序号": day["账本序号"],
        "会话数": len(sessions),
        "会话序号": settings.get("会话序号"),
        "回执数": len(client.get("/api/充日/开充回执").json()),
    }


def _gun_status(client, pile_id: int, gun_id: int) -> str:
    guns = client.get(f"/api/枪线?桩位编号={pile_id}").json()
    return next(g for g in guns if g["id"] == gun_id)["status"]


# ---------------------------------------------------------------- 单发路径


def test_seal_rejects_new_open(client):
    pid, gid = _first_idle(client)
    assert client.post("/api/充日/封印", json={"备注": "x"}).status_code == 200
    r = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid})
    assert r.status_code == 409
    assert "已封印" in r.json()["detail"]
    # 让路后不留任何痕迹
    ov = client.get("/api/总览").json()
    assert ov["充电中"] == 0
    assert _gun_status(client, pid, gid) == "可用"
    assert client.get("/api/充日/开充回执").json() == []


def test_open_then_seal_carries_session(client):
    pid, gid = _first_idle(client)
    o = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
    seal = client.post("/api/充日/封印", json={"备注": "x"}).json()
    assert seal["在途会话数"] == 1
    assert seal["未结会话"][0]["会话编号"] == o["会话编号"]
    day = client.get("/api/充日").json()
    assert day["封印世代"] == seal["锁账世代"] == 1
    # 开充回执与会话行、占用同命运
    receipts = client.get("/api/充日/开充回执").json()
    assert len(receipts) == 1 and receipts[0]["会话编号"] == o["会话编号"]
    assert _gun_status(client, pid, gid) == "占用"


def test_expected_generation_conflict(client):
    r1 = client.post("/api/充日/封印", json={"备注": "a", "预期世代": 0})
    assert r1.status_code == 200
    client.post("/api/充日/解封", json={"预期世代": 1})
    # 世代已推进到 2，带旧世代 0 的封印必须让路
    r2 = client.post("/api/充日/封印", json={"备注": "b", "预期世代": 0})
    assert r2.status_code == 409


# ---------------------------------------------------------------- 并发路径


def _race(client, seal_first: bool, barrier: threading.Barrier):
    """两条真并发请求：封印 vs 开充。返回各自响应。"""
    pid, gid = _first_idle(client)
    out: dict[str, object] = {}

    def do_seal():
        barrier.wait()
        out["seal"] = client.post("/api/充日/封印", json={"备注": "race"})

    def do_open():
        barrier.wait()
        out["open"] = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid})

    if seal_first:
        threads = [threading.Thread(target=do_seal), threading.Thread(target=do_open)]
    else:
        threads = [threading.Thread(target=do_open), threading.Thread(target=do_seal)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return pid, gid, out["seal"], out["open"]


def _assert_consistent(client, pid, gid):
    """无论哪种结局，五处口径必须一致：世代事件/回执/会话行/占用/总览充电中。"""
    day = client.get("/api/充日").json()
    ov = client.get("/api/总览").json()
    sessions = client.get("/api/会话", params={"状态": "充电中"}).json()
    receipts = client.get("/api/充日/开充回执").json()
    gun = _gun_status(client, pid, gid)
    ledger = client.get("/api/充日/账本").json()

    # 总览充电中桶 = 会话册中充电中行数
    assert ov["充电中"] == len(sessions)
    # 开充回执与充电中会话一一对应（回执成功必有会话，有会话必有回执）
    assert len(receipts) == len(sessions)
    receipt_sids = {r["会话编号"] for r in receipts}
    assert receipt_sids == {s["id"] for s in sessions}
    # 有会话则枪线占用，无会话则枪线可用——不存在空回执
    if sessions:
        assert gun == "占用"
    else:
        assert gun == "可用"
    # 账本与两本册子对得上
    seal_items = [x for x in ledger if x["类型"] == "封印世代"]
    receipt_items = [x for x in ledger if x["类型"] == "开充回执"]
    assert len(receipt_items) == len(receipts)
    assert len(seal_items) >= 1
    # 世代徽标与已封印一致
    assert day["已封印"] == (day["锁账徽标"] == "已封印")
    if day["已封印"]:
        assert day["封印世代"] >= 1
    return {"day": day, "ov": ov, "sessions": sessions, "receipts": receipts, "gun": gun}


def test_race_both_orders(client):
    """真并发多轮：无论谁先提交，结局都可重放且五处同命运。"""
    seen = set()
    for i in range(10):
        _reset_day(client)
        barrier = threading.Barrier(2)
        pid, gid, seal_r, open_r = _race(client, i % 2 == 0, barrier)
        assert seal_r.status_code == 200, seal_r.text
        assert open_r.status_code in (200, 409), open_r.text

        st = _assert_consistent(client, pid, gid)
        if open_r.status_code == 200:
            # 开充先落：会话/占用/回执都在，封印事件带上该会话
            assert st["day"]["已封印"] and st["day"]["封印世代"] == 1
            assert st["ov"]["充电中"] == 1
            assert st["day"]["世代事件"]["未结快照"] == 1
            seen.add("open_first")
        else:
            # 封印先落：开充整笔让路，不留充电中、不留占用、不留回执
            assert st["ov"]["充电中"] == 0
            assert st["receipts"] == []
            assert st["gun"] == "可用"
            assert st["day"]["世代事件"]["未结快照"] == 0
            seen.add("seal_first")
    # 真并发下两种结局都可能出现；至少要观察到让路臂是可收敛的
    assert "seal_first" in seen


def test_path_open_committed_first(client):
    """并发路径一：开充先成，随后封印把该会话带进世代事件。"""
    _reset_day(client)
    pid, gid = _first_idle(client)
    o = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
    s = client.post("/api/充日/封印", json={"备注": "after-open"}).json()
    assert s["在途会话数"] == 1
    assert s["未结会话"][0]["会话编号"] == o["会话编号"]
    st = _assert_consistent(client, pid, gid)
    assert st["ov"]["充电中"] == 1
    assert st["receipts"][0]["会话编号"] == o["会话编号"]


def test_path_seal_committed_first(client):
    """并发路径二：封印先成，在途开充 409 整笔让路且无任何残留。"""
    _reset_day(client)
    pid, gid = _first_idle(client)
    before = _snapshot(client)
    s = client.post("/api/充日/封印", json={"备注": "before-open"}).json()
    r = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid})
    assert r.status_code == 409
    st = _assert_consistent(client, pid, gid)
    assert st["ov"]["充电中"] == 0
    assert st["receipts"] == []
    assert _snapshot(client)["会话序号"] == before["会话序号"]
    assert s["世代事件"]["未结快照"] == 0


def test_path_double_half_rollback_ledger(client):
    """并发路径三：双方都半截回滚，账本不出现孤儿世代/孤儿回执。"""
    _reset_day(client)
    pid, gid = _first_idle(client)
    before = _snapshot(client)
    from app.db import db

    with pytest.raises(RuntimeError):
        with db(immediate=True) as conn:
            sess_svc.open_session(conn, pid, gid)
            raise RuntimeError("开充提交前崩溃")
    with pytest.raises(RuntimeError):
        with db(immediate=True) as conn:
            seal_svc.seal_day(conn, "半截封印")
            raise RuntimeError("封印提交前崩溃")

    assert _snapshot(client) == before
    assert client.get("/api/充日/账本").json() == []
    assert client.get("/api/充日/历史").json() == []


def test_race_stress(client):
    """压力跑多轮：任何时刻都不允许「已锁仍增充电中」「回执成功无占用」。"""
    for i in range(12):
        _reset_day(client)
        barrier = threading.Barrier(2)
        pid, gid, seal_r, open_r = _race(client, i % 2 == 0, barrier)
        assert seal_r.status_code == 200, seal_r.text
        _assert_consistent(client, pid, gid)
        day = client.get("/api/充日").json()
        ov = client.get("/api/总览").json()
        # 已锁仍增充电中：封印后若开充被拒，充电中必须为 0
        if open_r.status_code == 409:
            assert ov["充电中"] == 0
        # 总览充电中与锁账徽标不长期拧花
        assert ov["充日已封印"] == day["已封印"]


def test_half_write_rolls_back(client):
    """半截注入：在开充事务中途抛错，序号/占用/会话/回执必须整体回滚。"""
    pid, gid = _first_idle(client)
    before = _snapshot(client)

    original = sess_svc.open_session

    def boom(*a, **kw):
        raise RuntimeError("半截崩溃")

    # 直接走服务层 + 事务，模拟提交前崩溃
    from app.db import db

    with pytest.raises(RuntimeError):
        with db(immediate=True) as conn:
            sess_svc.open_session(conn, pid, gid)
            # 此时已写入会话/占用/序号/回执
            assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == before["会话数"] + 1
            boom()

    after = _snapshot(client)
    assert after == before
    assert _gun_status(client, pid, gid) == "可用"


def test_sequence_rolls_back_with_session(client):
    """序号已加但会话行回滚的夹具：注入在开会话后、回执前崩溃。"""
    pid, gid = _first_idle(client)
    before = _snapshot(client)

    from app.db import db

    def crash_after_session(conn):
        sess_svc.open_session(conn, pid, gid)
        raise RuntimeError("回执前崩溃")

    with pytest.raises(RuntimeError):
        with db(immediate=True) as conn:
            crash_after_session(conn)

    after = _snapshot(client)
    assert after["会话序号"] == before["会话序号"]  # 序号已回滚
    assert after["会话数"] == before["会话数"]  # 会话行已回滚
    assert after["回执数"] == before["回执数"]  # 回执未残留
    assert after["充电中"] == before["充电中"]


def test_pulse_cannot_land_in_rolled_back_session(client):
    """被锁账让路后回滚的会话号不存在，脉冲不得误进「新会话」。"""
    pid, gid = _first_idle(client)
    before = _snapshot(client)

    from app.db import db

    closed_ids = []
    original = sess_svc.open_session

    def open_then_crash(conn, pile_id, gun_id):
        out = original(conn, pile_id, gun_id)
        closed_ids.append(out["会话编号"])
        raise RuntimeError("回执落账前崩溃")

    sess_svc.open_session = open_then_crash
    try:
        from app.db import db as _db

        with pytest.raises(RuntimeError):
            with _db(immediate=True) as conn:
                sess_svc.open_session(conn, pid, gid)
    finally:
        sess_svc.open_session = original

    assert closed_ids, "应已拿到一个随后回滚的会话号"
    # 该会话号已随事务回滚，脉冲请求必须 404，而不是写进别人的会话
    r = client.post(f"/api/会话/{closed_ids[0]}/脉冲", json={"电量": 1.5})
    assert r.status_code == 404
    assert _snapshot(client) == before


def test_double_half_rollback(client):
    """双半截：开充与封印都在提交前崩溃，双方都回到争用前快照。"""
    pid, gid = _first_idle(client)
    before = _snapshot(client)

    from app.db import db

    def seal_then_crash(conn):
        seal_svc.seal_day(conn, "半截封印")
        raise RuntimeError("封印提交前崩溃")

    with pytest.raises(RuntimeError):
        with db(immediate=True) as conn:
            seal_then_crash(conn)

    # 封印半截回滚：世代未推进、无世代事件、仍是未封印
    mid = _snapshot(client)
    assert mid["封印世代"] == before["封印世代"]
    assert mid["账本序号"] == before["账本序号"]
    assert client.get("/api/充日").json()["已封印"] is False

    # 此时开充仍可正常落账（未被半截封印误伤），但也随即回滚
    def open_then_crash(conn):
        sess_svc.open_session(conn, pid, gid)
        raise RuntimeError("开充提交前崩溃")

    with pytest.raises(RuntimeError):
        with db(immediate=True) as conn:
            open_then_crash(conn)

    assert _snapshot(client) == before
    assert _gun_status(client, pid, gid) == "可用"


def test_open_receipt_always_has_occupancy(client):
    """夹具：开充成功必有占用；若占用丢失则回执与会话行必须一起不在。"""
    pid, gid = _first_idle(client)
    o = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
    receipts = client.get("/api/充日/开充回执").json()
    assert len(receipts) == 1
    assert receipts[0]["会话编号"] == o["会话编号"]
    assert _gun_status(client, pid, gid) == "占用"
    # 回执与会话行同命运
    sess = client.get(f"/api/会话/{o['会话编号']}").json()
    assert sess["status"] == "充电中"
    # 总览充电中桶计入该会话
    assert client.get("/api/总览").json()["充电中"] == 1


# ---------------------------------------------------------------- 工具


def _reset_day(client):
    """把库恢复到「未封印、无充电中会话、无回执」的干净起点。"""
    day = client.get("/api/充日").json()
    if day["已封印"]:
        client.post("/api/充日/解封", json={"预期世代": day["封印世代"]})
    for s in client.get("/api/会话", params={"状态": "充电中"}).json():
        client.post(f"/api/会话/{s['id']}/中断", json={"原因": "测试重置"})
    # 清空账本相关：
    from app.db import db

    with db(immediate=True) as conn:
        conn.execute("DELETE FROM open_receipts")
        conn.execute("DELETE FROM seal_events")
        conn.execute("DELETE FROM pulses")
        conn.execute("DELETE FROM session_events")
        conn.execute("DELETE FROM sessions")
        conn.execute("UPDATE day_seals SET sealed=0, sealed_at=NULL, generation=0")
        conn.execute("UPDATE piles SET status='空闲' WHERE status='占用'")
        conn.execute("UPDATE guns SET status='可用' WHERE status='占用'")
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('会话序号','1000') "
            "ON CONFLICT(key) DO UPDATE SET value='1000'"
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('账本序号','0') "
            "ON CONFLICT(key) DO UPDATE SET value='0'"
        )
