"""假锁账对撞收口测试（架构 A：锁账世代优先）。

覆盖题目给出的五个夹具与对账要求：
  1. 已锁仍开出新会话——封印后开充必须 409 且零残留；
  2. 世代写入开充半截——开充事务任何点崩溃，世代/会话/序号/回执整体回滚；
  3. 总览双亮——封印后总览不得同时出现「已封印」与新增占用/充电中；
  4. 两按钮对撞双终态——真并发下只许单终态，世代、拒写、会话册、审计同命运；
  5. 幽灵会话仍可脉冲——回滚掉的会话号脉冲必须 404，审计面能抓到幽灵脉冲。
另含急停/关账清理在途会话时枪线释放与金额去向的对账。
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="chargebout-race-audit-"))
os.environ["CHARGEBOUT_DB"] = str(_TMP / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

import app.db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.services import sessions as sess_svc  # noqa: E402
from app.services import dayseal as seal_svc  # noqa: E402


@pytest.fixture()
def client():
    # 与同目录其它测试模块共享进程级 CHARGEBOUT_DB：必须按 config 实际生效的
    # DB_PATH 重置，删本模块自己的临时路径会打空，导致封印/会话状态跨文件残留。
    from app.config import DB_PATH

    if DB_PATH.exists():
        DB_PATH.unlink()
    dbmod.init_db()
    from app.seed import seed_if_empty

    seed_if_empty()
    dbmod.init_db()
    with TestClient(app) as c:
        yield c


def _first_idle(client) -> tuple[int, int]:
    pile = next(p for p in client.get("/api/桩位").json() if p["status"] == "空闲")
    gun = next(
        g for g in client.get(f"/api/枪线?桩位编号={pile['id']}").json() if g["status"] == "可用"
    )
    return pile["id"], gun["id"]


def _gun_status(client, pile_id: int, gun_id: int) -> str:
    guns = client.get(f"/api/枪线?桩位编号={pile_id}").json()
    return next(g for g in guns if g["id"] == gun_id)["status"]


def _reset_day(client) -> None:
    day = client.get("/api/充日").json()
    if day["已封印"]:
        client.post("/api/充日/解封", json={"预期世代": day["封印世代"]})
    for s in client.get("/api/会话", params={"状态": "充电中"}).json():
        client.post(f"/api/会话/{s['id']}/中断", json={"原因": "重置"})
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
        # 旁路徽标键若被人重新塞回，也必须清掉，审计口径只有一份。
        conn.execute("DELETE FROM settings WHERE key LIKE 'surface%' OR key='sealrace_surface'")


# ---------------------------------------------------------------- 夹具 1：已锁仍开


def test_sealed_day_cannot_open_new_session(client):
    """已封印后开充必须整笔拒写：无新会话、无占用、序号不动、无回执。"""
    pid, gid = _first_idle(client)
    seal = client.post("/api/充日/封印", json={"备注": "日终"}).json()
    assert seal["已封印"] and seal["锁账徽标"] == "已封印"

    r = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid})
    assert r.status_code == 409
    assert "已封印" in r.json()["detail"]

    ov = client.get("/api/总览").json()
    assert ov["充日已封印"] is True
    assert ov["锁账徽标"] == "已封印"
    assert ov["充电中"] == 0
    assert ov["桩位占用"] == 0
    assert client.get("/api/会话", params={"状态": "充电中"}).json() == []
    assert client.get("/api/充日/开充回执").json() == []
    assert _gun_status(client, pid, gid) == "可用"
    settings = {s["键"]: s["值"] for s in client.get("/api/设置").json()}
    assert settings.get("会话序号") == "1000"

    audit = client.get("/api/充日/对账").json()
    assert audit["同命运"] is True
    assert audit["违例"] == []


# ---------------------------------------------------------------- 夹具 2：世代写入开充半截


def test_half_open_with_generation_writes_rolls_back(client):
    """开充事务在世代已落、会话已写之后崩溃：整笔回滚，世代/会话/序号/回执不分叉。"""
    pid, gid = _first_idle(client)
    before = client.get("/api/充日").json()["账本序号"]
    from app.db import db

    captured: dict[str, int] = {}
    original_open = sess_svc.open_session

    def open_then_crash(conn, pile_id, gun_id=None):
        out = original_open(conn, pile_id, gun_id)
        captured["sid"] = out["会话编号"]
        # 事务内此刻：会话行、占用、序号、回执都已写，但尚未 COMMIT。
        assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 1
        raise RuntimeError("模拟开充提交前崩溃")

    sess_svc.open_session = open_then_crash
    try:
        with pytest.raises(RuntimeError):
            with db(immediate=True) as conn:
                sess_svc.open_session(conn, pid, gid)
    finally:
        sess_svc.open_session = original_open

    assert captured, "应当已执行到半截写入点"
    # 半截全部回滚：会话册无行、枪线释放、账本序号不动、无回执。
    assert client.get("/api/会话").json() == []
    assert client.get("/api/充日/开充回执").json() == []
    assert client.get("/api/充日/账本").json() == []
    assert client.get("/api/充日").json()["账本序号"] == before
    assert _gun_status(client, pid, gid) == "可用"
    # 回滚掉的会话号再被脉冲入口引用，必须 404，而不是写进幽灵会话。
    pr = client.post(f"/api/会话/{captured['sid']}/脉冲", json={"电量": 1.0})
    assert pr.status_code == 404
    audit = client.get("/api/充日/对账").json()
    assert audit["同命运"] is True
    assert audit["幽灵脉冲会话"] == []


def test_half_seal_rolls_back_generation(client):
    """封印半截：世代推进与世代事件必须随事务回滚，解封状态可重放。"""
    from app.db import db

    gen_before = client.get("/api/充日").json()["封印世代"]
    with pytest.raises(RuntimeError):
        with db(immediate=True) as conn:
            seal_svc.seal_day(conn, "半截封印")
            raise RuntimeError("封印提交前崩溃")

    day = client.get("/api/充日").json()
    assert day["已封印"] is False
    assert day["封印世代"] == gen_before
    assert client.get("/api/充日/历史").json() == []
    assert client.get("/api/充日/对账").json()["同命运"] is True


# ---------------------------------------------------------------- 夹具 3：总览双亮


def test_overview_never_double_lit_after_seal(client):
    """封印成功后总览只能亮锁账灯，绝不允许同时出现新增占用/充电中。"""
    pid, gid = _first_idle(client)
    client.post("/api/充日/封印", json={"备注": "x"})
    # 封印后再点开充（哪怕前端按钮没禁用，后端也必须拒写）。
    r = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid})
    assert r.status_code == 409

    ov = client.get("/api/总览").json()
    assert ov["充日已封印"] is True
    assert ov["锁账徽标"] == "已封印"
    assert ov["充电中"] == 0
    assert ov["桩位占用"] == 0
    # 不存在旁路 surface 口径覆盖真实快照。
    assert "surface_ok" not in ov
    assert "surface_充电中" not in ov

    # 解封后开充应当恢复正常，且只亮占用灯、不亮锁账灯。
    day = client.get("/api/充日").json()
    client.post("/api/充日/解封", json={"预期世代": day["封印世代"]})
    o = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
    ov2 = client.get("/api/总览").json()
    assert ov2["充日已封印"] is False
    assert ov2["充电中"] == 1 and ov2["桩位占用"] == 1
    assert o["状态"] == "充电中"


# ---------------------------------------------------------------- 夹具 4：两按钮对撞


def test_race_converges_to_single_terminal_state(client):
    """两按钮真并发对撞：只许单终态，审计面始终同命运、无双终态挂账。

    线程调度不保证每次都出现「封印先提交」臂，因此让路臂的正确性由
    test_sealed_day_cannot_open_new_session 确定性覆盖；这里只断言：无论谁先，
    开充 200 与对撞拒写不会同时出现，且世代/会话册/占用桶/审计收敛到同一结局。
    """
    for i in range(8):
        _reset_day(client)
        pid, gid = _first_idle(client)
        barrier = threading.Barrier(2)
        out: dict[str, object] = {}

        def do_seal():
            barrier.wait()
            out["seal"] = client.post("/api/充日/封印", json={"备注": "race"})

        def do_open():
            barrier.wait()
            out["open"] = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid})

        threads = [threading.Thread(target=do_seal), threading.Thread(target=do_open)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        seal_r, open_r = out["seal"], out["open"]
        assert seal_r.status_code == 200
        assert open_r.status_code in (200, 409)

        audit = client.get("/api/充日/对账").json()
        assert audit["同命运"] is True, audit["违例"]
        day = client.get("/api/充日").json()
        ov = client.get("/api/总览").json()
        receipts = client.get("/api/充日/开充回执").json()
        charging = client.get("/api/会话", params={"状态": "充电中"}).json()

        # 单终态：开充 200 与对撞拒写不可能同时出现；双灯也不可能同时亮。
        if open_r.status_code == 200:
            assert day["已封印"] is True
            assert len(charging) == 1 and len(receipts) == 1
            assert ov["充电中"] == 1 and _gun_status(client, pid, gid) == "占用"
            assert receipts[0]["会话编号"] == charging[0]["id"]
            assert ov["充日已封印"] is True  # 开充先成后封印成功：占用灯与锁账灯可同亮且有账本背书
        else:
            assert day["已封印"] is True
            assert charging == [] and receipts == []
            assert ov["充电中"] == 0 and _gun_status(client, pid, gid) == "可用"
            assert ov["充日已封印"] is True and ov["桩位占用"] == 0

    # 确定性补一刀：封印先提交后，紧随其后的开充必须走让路臂且零残留。
    _reset_day(client)
    pid, gid = _first_idle(client)
    client.post("/api/充日/封印", json={"备注": "deterministic-first"})
    r = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid})
    assert r.status_code == 409
    assert client.get("/api/充日/对账").json()["同命运"] is True
    assert client.get("/api/会话", params={"状态": "充电中"}).json() == []
    assert _gun_status(client, pid, gid) == "可用"


# ---------------------------------------------------------------- 夹具 5：幽灵会话脉冲


def test_pulse_rejects_ghost_session_and_audit_flags_dirty_pulse(client):
    """脉冲入口对不存在/已回滚会话必须拒写；直接灌库的幽灵脉冲会被审计抓到。"""
    pid, gid = _first_idle(client)
    # 不存在的会话号：脉冲 404，不产生电量/金额脏账。
    r = client.post("/api/会话/999999/脉冲", json={"电量": 2.5})
    assert r.status_code == 404

    # 正常开充 -> 脉冲 -> 急停回滚整条链路后，旧脉冲随会话仍可对账、无悬挂。
    o = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
    client.post(f"/api/会话/{o['会话编号']}/脉冲", json={"电量": 3.0})
    detail = client.get(f"/api/会话/{o['会话编号']}").json()
    assert detail["kwh"] == 3.0

    # 直接在库里构造一条指向幽灵会话的脉冲，审计面必须能定位。
    # PRAGMA foreign_keys 是连接级设置，db() 新连接会强制打开，故用独立连接关 FK 灌脏数据。
    raw = dbmod.connect()
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(999999,1.0,7,'t')"
        )
        raw.commit()
    finally:
        raw.close()
    audit = client.get("/api/充日/对账").json()
    assert audit["同命运"] is False
    assert 999999 in audit["幽灵脉冲会话"]
    assert any("幽灵会话仍被记脉冲" in v for v in audit["违例"])


# ---------------------------------------------------------------- 急停/关账对账


def test_abort_releases_gun_and_amount_is_auditable(client):
    """急停清理在途会话：枪线释放、会话终态唯一、金额去向可在会话册与审计核对。"""
    pid, gid = _first_idle(client)
    o = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
    client.post(f"/api/会话/{o['会话编号']}/脉冲", json={"电量": 2.0})
    before = client.get(f"/api/会话/{o['会话编号']}").json()

    r = client.post(f"/api/会话/{o['会话编号']}/中断", json={"原因": "急停"})
    assert r.status_code == 200
    assert r.json()["状态"] == "已中断"
    # 急停不可重复终态：再急停必须被拒。
    assert client.post(f"/api/会话/{o['会话编号']}/中断").status_code == 400
    assert _gun_status(client, pid, gid) == "可用"

    after = client.get(f"/api/会话/{o['会话编号']}").json()
    assert after["status"] == "已中断"
    # 电量与金额保留在会话行上，金额去向可核对，不会凭空消失。
    assert after["kwh"] == before["kwh"] and after["amount"] == before["amount"]
    assert any(e["action"] == "异常中断" for e in after["事件"])
    audit = client.get("/api/充日/对账").json()
    assert audit["同命运"] is True
    assert audit["占用枪线数"] == 0


def test_settle_closes_amount_and_frees_gun(client):
    """关账入口：终态唯一、金额按锁定电价落账、枪线释放且审计同命运。"""
    pid, gid = _first_idle(client)
    o = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
    client.post(f"/api/会话/{o['会话编号']}/脉冲", json={"电量": 4.0})
    charged = client.get(f"/api/会话/{o['会话编号']}").json()

    r = client.post(f"/api/会话/{o['会话编号']}/结算").json()
    assert r["状态"] == "已关账"
    expected = round(4.0 * float(charged["price_locked"]), 2)
    assert r["金额"] == expected
    # 双终态防护：已关账不可再关、不可急停。
    assert client.post(f"/api/会话/{o['会话编号']}/结算").status_code == 400
    assert client.post(f"/api/会话/{o['会话编号']}/中断").status_code == 400
    assert _gun_status(client, pid, gid) == "可用"

    closed = client.get(f"/api/会话/{o['会话编号']}").json()
    assert closed["status"] == "已关账" and closed["amount"] == expected
    audit = client.get("/api/充日/对账").json()
    assert audit["同命运"] is True
    assert audit["占用枪线数"] == 0 and audit["在途会话数"] == 0


def test_audit_detects_orphan_receipt_and_occupancy_drift(client):
    """审计面主动构造撕裂：孤儿回执与占用漂移必须被同命运核对抓到。"""
    pid, gid = _first_idle(client)
    o = client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
    from app.services.core import today

    # 用独立连接关 FK 灌脏数据：模拟「回执成功、会话已回滚」的孤儿回执撕裂。
    raw = dbmod.connect()
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO open_receipts(seq,receipt_no,session_id,slip_no,business_day,generation,created_at)"
            " VALUES(999990,'开999990',888888,'充幽灵',?,0,'t')",
            (today(),),
        )
        raw.commit()
    finally:
        raw.close()
    audit = client.get("/api/充日/对账").json()
    assert audit["同命运"] is False
    assert 888888 in audit["孤儿回执会话"]
    assert any("孤儿开充回执" in v for v in audit["违例"])

    # 再制造占用漂移：把在途会话的桩位改回空闲，占用桶与会话册即拧花。
    from app.db import db

    with db(immediate=True) as conn:
        conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (pid,))
        conn.execute("UPDATE guns SET status='可用' WHERE id=?", (gid,))
    audit = client.get("/api/充日/对账").json()
    assert audit["同命运"] is False
    assert any("桩位占用桶" in v or "枪线占用" in v for v in audit["违例"])
