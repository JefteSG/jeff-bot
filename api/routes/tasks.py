from __future__ import annotations

from fastapi import APIRouter

from api.services import db


router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
def list_tasks() -> list[dict]:
    rows = db.fetch_all("SELECT * FROM tasks ORDER BY created_at DESC")
    return [dict(r) for r in rows]
