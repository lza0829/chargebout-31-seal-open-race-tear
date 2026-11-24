from __future__ import annotations
from fastapi import HTTPException

from app.services.core import (
    current_generation,
    ledger_head,
    next_seq,
    now_str,
    seal_row,
    today,
)

OPEN_STATUSES = ("充电中", "结算中", "已超时")


def _open_sessions(conn, day: str):
    return conn.execute(
        "SELECT id, slip_no, status FROM sessions WHERE business_day=? AND status IN (?,?,?) ORDER BY id",
        (day, *OPEN_STATUSES),
    ).fetchall()


def _last_seal_event(conn, day: str):
    return conn.execute(
        "SELECT * FROM seal_events WHERE business_day=? ORDER BY seq DESC, id DESC LIMIT 1",
        (day,),
    ).fetchone()


def get_day(conn, day: str | None = None) -> dict:
    d = day or today()
    r = seal_row(conn, d)
    opens = _open_sessions(conn, d)
    ev = _last_seal_event(conn, d)
    gen = int(r["generation"]) if r else 0
    return {
        "营业日": d,
        "已封印": bool(r and r["sealed"]),
        "封印时间": r["sealed_at"] if r else None,
        "备注": r["note"] if r else "",
        "封印世代": gen,
        "未结会话数": len(opens),
        "未结会话": [{"会话编号": o["id"], "会话号": o["slip_no"], "状态": o["status"]} for o in opens],
        "锁账徽标": "已封印" if (r and r["sealed"]) else "未封印",
        "世代事件": {
            "序号": int(ev["seq"]) if ev else 0,
            "世代": int(ev["generation"]) if ev else 0,
            "动作": ev["action"] if ev else None,
            "时间": ev["sealed_at"] if ev else None,
            "未结快照": int(ev["open_count"]) if ev else 0,
        },
        "账本序号": ledger_head(conn),
    }


def _ensure_row(conn, d: str) -> None:
    conn.execute(
        "INSERT INTO day_seals(business_day,sealed,sealed_at,note,generation) VALUES(?,0,NULL,'',0) "
        "ON CONFLICT(business_day) DO NOTHING",
        (d,),
    )


def seal_day(conn, note: str = "", expected_generation: int | None = None) -> dict:
    """封印（锁账）提交口。

    在 BEGIN IMMEDIATE 事务内：读世代 -> 校验 -> 推进世代 -> 追加世代事件。
    与开充提交严格串行：本函数提交之后，任何在途开充再进临界区都读到已封印，
    整笔失败，不会留下会话行/占用/回执，所以不存在「已锁仍增充电中」。
    """
    d = today()
    _ensure_row(conn, d)
    r = seal_row(conn, d)
    if r["sealed"]:
        raise HTTPException(400, detail="本日已封印")
    if expected_generation is not None and int(r["generation"]) != int(expected_generation):
        raise HTTPException(
            409,
            detail=f"锁账世代已变（预期{expected_generation}，当前{int(r['generation'])}），请刷新后重试",
        )
    ts = now_str()
    gen = int(r["generation"]) + 1
    opens = _open_sessions(conn, d)
    open_ids = ",".join(str(o["id"]) for o in opens)
    seq = next_seq(conn)
    conn.execute(
        "UPDATE day_seals SET sealed=1, sealed_at=?, note=?, generation=? WHERE business_day=?",
        (ts, note, gen, d),
    )
    conn.execute(
        """INSERT INTO seal_events(seq,business_day,generation,action,open_count,open_session_ids,sealed_at,note)
           VALUES(?,?,?,?,?,?,?,?)""",
        (seq, d, gen, "封印", len(opens), open_ids, ts, note),
    )
    out = get_day(conn, d)
    out["锁账世代"] = gen
    out["事件序号"] = seq
    out["在途会话数"] = len(opens)
    return out


def unseal_day(conn, expected_generation: int | None = None) -> dict:
    d = today()
    r = seal_row(conn, d)
    if not r or not r["sealed"]:
        raise HTTPException(400, detail="本日未封印")
    if expected_generation is not None and int(r["generation"]) != int(expected_generation):
        raise HTTPException(
            409,
            detail=f"锁账世代已变（预期{expected_generation}，当前{int(r['generation'])}），请刷新后重试",
        )
    ts = now_str()
    gen = int(r["generation"]) + 1
    opens = _open_sessions(conn, d)
    seq = next_seq(conn)
    conn.execute(
        "UPDATE day_seals SET sealed=0, sealed_at=NULL, note='', generation=? WHERE business_day=?",
        (gen, d),
    )
    conn.execute(
        """INSERT INTO seal_events(seq,business_day,generation,action,open_count,open_session_ids,sealed_at,note)
           VALUES(?,?,?,?,?,?,?,?)""",
        (seq, d, gen, "解封", len(opens), ",".join(str(o["id"]) for o in opens), ts, ""),
    )
    return get_day(conn, d)


def seal_history(conn, day: str | None = None, limit: int = 50) -> list[dict]:
    d = day or today()
    return [
        {
            "序号": int(e["seq"]),
            "世代": int(e["generation"]),
            "动作": e["action"],
            "未结快照": int(e["open_count"]),
            "未结会话": [int(x) for x in e["open_session_ids"].split(",") if x],
            "时间": e["sealed_at"],
            "备注": e["note"],
        }
        for e in conn.execute(
            "SELECT * FROM seal_events WHERE business_day=? ORDER BY seq DESC, id DESC LIMIT ?",
            (d, limit),
        ).fetchall()
    ]


def open_receipts(conn, day: str | None = None, limit: int = 200) -> list[dict]:
    d = day or today()
    return [
        {
            "序号": int(r["seq"]),
            "回执号": r["receipt_no"],
            "会话编号": r["session_id"],
            "会话号": r["slip_no"],
            "封印世代": int(r["generation"]),
            "时间": r["created_at"],
        }
        for r in conn.execute(
            "SELECT * FROM open_receipts WHERE business_day=? ORDER BY seq DESC, id DESC LIMIT ?",
            (d, limit),
        ).fetchall()
    ]


def ledger(conn, day: str | None = None, limit: int = 100) -> list[dict]:
    """可重放的争用结局账本：封印世代事件与开充回执按账本全序合流。"""
    d = day or today()
    items: list[dict] = []
    for e in conn.execute(
        "SELECT seq, generation, action, sealed_at FROM seal_events WHERE business_day=?",
        (d,),
    ).fetchall():
        items.append(
            {
                "序号": int(e["seq"]),
                "类型": "封印世代",
                "动作": e["action"],
                "世代": int(e["generation"]),
                "时间": e["sealed_at"],
            }
        )
    for r in conn.execute(
        "SELECT seq, receipt_no, session_id, slip_no, generation, created_at FROM open_receipts WHERE business_day=?",
        (d,),
    ).fetchall():
        items.append(
            {
                "序号": int(r["seq"]),
                "类型": "开充回执",
                "回执号": r["receipt_no"],
                "会话编号": r["session_id"],
                "会话号": r["slip_no"],
                "世代": int(r["generation"]),
                "时间": r["created_at"],
            }
        )
    items.sort(key=lambda x: x["序号"], reverse=True)
    return items[:limit]


def seal_state(conn, day: str | None = None) -> dict:
    """开充闸/锁账徽标共用的单一读口径。"""
    d = day or today()
    r = seal_row(conn, d)
    gen = int(r["generation"]) if r else 0
    return {
        "营业日": d,
        "已封印": bool(r and r["sealed"]),
        "封印世代": gen,
        "锁账徽标": "已封印" if (r and r["sealed"]) else "未封印",
        "封印时间": r["sealed_at"] if r else None,
        "账本序号": ledger_head(conn),
        "当前世代": current_generation(conn, d),
    }


def reconcile(conn, day: str | None = None) -> dict:
    """审计对账面：在同一只读快照内核对锁账世代、开充拒写、会话册、占用桶与
    可重放账本是否同命运。架构 A 下任何一项不一致即视为对撞收口失败。

    核对项：
      - 回执孤儿：开充回执必须指向真实会话，且会话必带同号回执（同命运）；
      - 枪线占用：占用桩/枪必有在途会话，在途会话必占桩/枪；
      - 世代违例：已封印后仍落账的在途会话不会出现在封印事件未结快照中（已锁仍开充）；
      - 世代快照：封印事件 open_session_ids 必须与当时在途会话逐一对上；
      - 幽灵脉冲/事件：脉冲与会话事件必须指向真实会话；
      - 账本全序：回执/世代事件 seq 与账本序号单调一致、无孤儿序号；
      - 总览一致：充电中桶、锁账徽标与各册子取自同一快照，不得双亮或拧花。
    """
    d = day or today()
    r = seal_row(conn, d)
    sealed = bool(r and r["sealed"])
    seal_gen = int(r["generation"]) if r else 0

    sessions = conn.execute(
        "SELECT id, slip_no, status, seal_generation, pile_id, gun_id "
        "FROM sessions WHERE business_day=? ORDER BY id",
        (d,),
    ).fetchall()
    open_rows = [s for s in sessions if s["status"] in OPEN_STATUSES]
    open_ids = {s["id"] for s in open_rows}
    receipts = conn.execute(
        "SELECT seq, receipt_no, session_id, slip_no, generation FROM open_receipts WHERE business_day=?",
        (d,),
    ).fetchall()
    receipt_sids = {x["session_id"] for x in receipts}
    session_ids = {s["id"] for s in sessions}

    violations: list[str] = []

    # 1) 回执 ↔ 会话同命运：无孤儿回执，也不允许有会话却无回执。
    orphan_receipts = sorted(receipt_sids - session_ids)
    if orphan_receipts:
        violations.append(f"孤儿开充回执（指向已回滚/不存在会话）：{orphan_receipts}")
    missing_receipts = sorted(session_ids - receipt_sids)
    if missing_receipts:
        violations.append(f"会话缺开充回执（会话与回执未同命运）：{missing_receipts}")

    # 2) 占用桶 ↔ 在途会话：占用桩/枪必有在途会话，反之亦然。
    busy_piles = {
        x["id"]
        for x in conn.execute("SELECT id FROM piles WHERE status='占用'").fetchall()
    }
    sess_piles = {s["pile_id"] for s in open_rows}
    if busy_piles != sess_piles:
        violations.append(
            f"桩位占用桶与在途会话不一致：多占{sorted(busy_piles - sess_piles)}，"
            f"漏占{sorted(sess_piles - busy_piles)}"
        )
    busy_guns = {x["id"] for x in conn.execute("SELECT id FROM guns WHERE status='占用'").fetchall()}
    sess_guns = {s["gun_id"] for s in open_rows}
    if busy_guns != sess_guns:
        violations.append(
            f"枪线占用与在途会话不一致：多占{sorted(busy_guns - sess_guns)}，"
            f"漏占{sorted(sess_guns - busy_guns)}"
        )

    # 3) 已锁仍开充的唯一合法判定：当前在途会话必须全部出现在最近一次封印
    #    事件的未结快照里（它们是封印先于开充提交时被带进世代事件的）。
    #    封印之后才落账的会话不会出现在任何封印事件快照中，即「已锁仍开出新会话」。
    last_seal = conn.execute(
        "SELECT seq, generation, open_session_ids FROM seal_events "
        "WHERE business_day=? AND action='封印' ORDER BY seq DESC, id DESC LIMIT 1",
        (d,),
    ).fetchone()
    if sealed:
        snapshotted = (
            {int(x) for x in (last_seal["open_session_ids"] or "").split(",") if x}
            if last_seal
            else set()
        )
        gen_violations = sorted(open_ids - snapshotted)
        if gen_violations:
            violations.append(
                f"已锁仍开出新会话（封印世代{seal_gen}）：{gen_violations}"
            )

    # 4) 封印世代事件的未结快照必须自洽：计数与ID集合一致、引用的会话都真实存在。
    events = conn.execute(
        "SELECT id, seq, generation, action, open_count, open_session_ids "
        "FROM seal_events WHERE business_day=? ORDER BY seq, id",
        (d,),
    ).fetchall()
    for e in events:
        ids = {int(x) for x in (e["open_session_ids"] or "").split(",") if x}
        if len(ids) != int(e["open_count"]):
            violations.append(
                f"世代事件序号{e['seq']}未结快照计数与ID集合不一致：{e['open_count']} vs {sorted(ids)}"
            )
        dangling = sorted(ids - session_ids)
        if dangling:
            violations.append(f"世代事件序号{e['seq']}引用了不存在的会话：{dangling}")

    # 5) 幽灵脉冲 / 幽灵会话事件：不允许继续给不存在（已回滚）的会话记账。
    ghost_pulses = [
        x["session_id"]
        for x in conn.execute(
            "SELECT DISTINCT p.session_id AS session_id FROM pulses p "
            "LEFT JOIN sessions s ON s.id=p.session_id WHERE s.id IS NULL"
        ).fetchall()
    ]
    if ghost_pulses:
        violations.append(f"幽灵会话仍被记脉冲：{sorted(set(ghost_pulses))}")
    ghost_events = [
        x["session_id"]
        for x in conn.execute(
            "SELECT DISTINCT e.session_id AS session_id FROM session_events e "
            "LEFT JOIN sessions s ON s.id=e.session_id WHERE s.id IS NULL"
        ).fetchall()
    ]
    if ghost_events:
        violations.append(f"幽灵会话仍挂事件流水：{sorted(set(ghost_events))}")

    # 6) 账本全序：所有 seq 落在 [1, 账本序号] 且不重复。
    head = ledger_head(conn)
    all_seqs = [int(x["seq"]) for x in receipts] + [int(x["seq"]) for x in events]
    if any(s <= 0 or s > head for s in all_seqs):
        violations.append(f"账本序号越界（账本头{head}）：{sorted(all_seqs)}")
    if len(all_seqs) != len(set(all_seqs)):
        violations.append("账本序号出现重复，全序被破坏")

    # 7) 同快照总览：充电中桶 == 会话册在途数；徽标 == day_seals.sealed。
    overview_charging = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE status='充电中'"
    ).fetchone()["n"]
    book_charging = sum(1 for s in sessions if s["status"] == "充电中")
    if overview_charging != book_charging:
        violations.append(
            f"总览充电中桶{overview_charging} != 会话册充电中{book_charging}"
        )

    return {
        "营业日": d,
        "口径": "锁账世代优先（世代号∪开充拒写∪无新增会话可重放）",
        "已封印": sealed,
        "封印世代": seal_gen,
        "账本序号": head,
        "在途会话数": len(open_rows),
        "充电中会话数": book_charging,
        "开充回执数": len(receipts),
        "世代事件数": len(events),
        "占用桩位数": len(busy_piles),
        "占用枪线数": len(busy_guns),
        "幽灵脉冲会话": sorted(set(ghost_pulses)),
        "孤儿回执会话": orphan_receipts,
        "同命运": not violations,
        "违例": violations,
    }

