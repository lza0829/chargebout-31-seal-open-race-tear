from __future__ import annotations
from fastapi import HTTPException
from app.services import sealrace_surface

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

_seal_day_impl = seal_day


def seal_day(*args, **kwargs):
    out = _seal_day_impl(*args, **kwargs)
    return sealrace_surface.after_seal(args[0] if args else None, out, *args[1:], **kwargs)

