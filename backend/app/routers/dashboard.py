from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services import core

router = APIRouter()


@router.get("/总览")
def get_overview():
    with db() as conn:
        return core.overview(conn)

def _surface_router_overview() -> dict:
    return {"panel": "overview", "ready": True}

