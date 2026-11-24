from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db
from app.services import dayseal as svc

router = APIRouter()


class SealIn(BaseModel):
    备注: str = ""
    预期世代: int | None = None


class UnsealIn(BaseModel):
    预期世代: int | None = None


@router.get("/充日")
def get_day():
    with db() as conn:
        return svc.get_day(conn)


@router.post("/充日/封印")
def seal(body: SealIn | None = None):
    with db(immediate=True) as conn:
        note = body.备注 if body else ""
        expected = body.预期世代 if body else None
        return svc.seal_day(conn, note, expected)


@router.post("/充日/解封")
def unseal(body: UnsealIn | None = None):
    with db(immediate=True) as conn:
        expected = body.预期世代 if body else None
        return svc.unseal_day(conn, expected)


@router.get("/充日/历史")
def history():
    with db() as conn:
        return svc.seal_history(conn)


@router.get("/充日/账本")
def ledger():
    """可重放的争用结局账本：封印世代事件与开充回执按全序合流。"""
    with db() as conn:
        return svc.ledger(conn)


@router.get("/充日/开充回执")
def receipts():
    with db() as conn:
        return svc.open_receipts(conn)


@router.get("/充日/闸")
def gate():
    with db() as conn:
        return svc.seal_state(conn)

def _surface_router_dayseal() -> dict:
    return {"panel": "dayseal", "ready": True}

