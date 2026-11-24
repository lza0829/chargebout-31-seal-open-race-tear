"""端到端冒烟：锁账/开充对撞收口全链路（独立临时库，不碰项目数据）。

覆盖定位面：锁账入口、开充入口、脉冲入口、急停入口、关账入口、会话册、
总览（徽标+占用桶）、世代事件、开充回执、争用账本、审计对账面。
用法：python -I scripts/smoke_race.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CHARGEBOUT_DB"] = str(Path(tempfile.mkdtemp(prefix="chargebout-smoke-")) / "smoke.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402

init_db()
seed_if_empty()
init_db()
c = TestClient(app)
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


pile = next(p for p in c.get("/api/桩位").json() if p["status"] == "空闲")
gun = next(g for g in c.get(f"/api/枪线?桩位编号={pile['id']}").json() if g["status"] == "可用")
pid, gid = pile["id"], gun["id"]

# 1) 开充先成：会话/占用/回执/世代同命运
o = c.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
sid = o["会话编号"]
ov = c.get("/api/总览").json()
check("开充后总览充电中+1", ov["充电中"] == 1 and ov["桩位占用"] == 1)
check("开充后锁账灯未亮", ov["充日已封印"] is False and ov["锁账徽标"] == "未封印")
check("无旁路徽标字段", "surface_ok" not in ov and "surface_充电中" not in ov)

# 2) 脉冲只进真实会话
p = c.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0}).json()
check("脉冲入账", p["累计电量"] == 2.0)
check("幽灵脉冲被拒", c.post("/api/会话/424242/脉冲", json={"电量": 1.0}).status_code == 404)

# 3) 封印：把在途会话带进世代事件
s = c.post("/api/充日/封印", json={"备注": "冒烟封印"}).json()
check("封印成功灯亮", s["已封印"] and s["锁账徽标"] == "已封印" and s["锁账世代"] == 1)
check("世代事件带上在途会话", s["世代事件"]["未结快照"] == 1 and s["未结会话"][0]["会话编号"] == sid)
ov = c.get("/api/总览").json()
check("封印后徽标与真实行一致", ov["充日已封印"] is True and ov["锁账徽标"] == "已封印")
check("封印未误杀在途会话", ov["充电中"] == 1)

# 4) 已锁再开充：整笔拒写、零残留
r = c.post("/api/开会话", json={"桩位编号": next(
    p for p in c.get("/api/桩位").json() if p["status"] == "空闲"
)["id"]})
check("已锁开充409", r.status_code == 409 and "已封印" in r.json()["detail"])
ov = c.get("/api/总览").json()
check("拒写后无新增占用/充电中", ov["充电中"] == 1 and ov["桩位占用"] == 1)
check("无新增回执", len(c.get("/api/充日/开充回执").json()) == 1)

# 5) 账本与审计同命运
ledger = c.get("/api/充日/账本").json()
check("账本含世代事件+回执两类", {x["类型"] for x in ledger} == {"封印世代", "开充回执"})
audit = c.get("/api/充日/对账").json()
check("审计同命运", audit["同命运"] is True, str(audit["违例"]))
check("审计占用桶与在途一致", audit["占用桩位数"] == 1 and audit["在途会话数"] == 1)

# 6) 急停：终态唯一、枪线释放、金额留存可对账
ab = c.post(f"/api/会话/{sid}/中断", json={"原因": "急停"}).json()
check("急停终态", ab["状态"] == "已中断")
check("急停不可重复", c.post(f"/api/会话/{sid}/中断").status_code == 400)
guns = c.get(f"/api/枪线?桩位编号={pid}").json()
check("急停释放枪线", next(g for g in guns if g["id"] == gid)["status"] == "可用")
detail = c.get(f"/api/会话/{sid}").json()
check("金额电量留存可对账", detail["kwh"] == 2.0 and detail["amount"] > 0)
check("急停后审计收敛", c.get("/api/充日/对账").json()["同命运"] is True)

# 7) 解封 -> 新会话 -> 关账：终态唯一、金额按锁定电价、枪线释放
d = c.get("/api/充日").json()
check("解封成功", c.post("/api/充日/解封", json={"预期世代": d["封印世代"]}).status_code == 200)
o2 = c.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).json()
c.post(f"/api/会话/{o2['会话编号']}/脉冲", json={"电量": 4.0})
st = c.post(f"/api/会话/{o2['会话编号']}/结算").json()
detail2 = c.get(f"/api/会话/{o2['会话编号']}").json()
expected = round(4.0 * float(detail2["price_locked"]), 2)
check("关账金额正确", st["状态"] == "已关账" and st["金额"] == expected, f"{st.get('金额')} vs {expected}")
check("关账不可重复", c.post(f"/api/会话/{o2['会话编号']}/结算").status_code == 400)
check("关账释放枪线", next(g for g in c.get(f'/api/枪线?桩位编号={pid}').json()
                            if g["id"] == gid)["status"] == "可用")
check("关账后审计收敛", c.get("/api/充日/对账").json()["同命运"] is True)

# 8) 世代乐观锁：旧预期世代必须 409
r = c.post("/api/充日/封印", json={"备注": "旧世代", "预期世代": 0})
check("旧预期世代409", r.status_code == 409)

print("\n结果：", "全部通过" if not failures else f"失败 {failures}")
raise SystemExit(1 if failures else 0)
