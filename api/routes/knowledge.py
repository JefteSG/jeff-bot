from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services import db


router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class KnowledgePayload(BaseModel):
    category: str = Field(min_length=1, max_length=120)
    keywords: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=4000)
    confidence_threshold: float = Field(ge=0.0, le=1.0)


@router.get("")
def list_knowledge() -> list[dict]:
    rows = db.fetch_all("SELECT * FROM knowledge_base ORDER BY created_at DESC")
    return [dict(r) for r in rows]


@router.post("")
def create_knowledge(payload: KnowledgePayload) -> dict:
    row_id = db.execute(
        """
        INSERT INTO knowledge_base (category, keywords, answer, confidence_threshold)
        VALUES (?, ?, ?, ?)
        """,
        (payload.category, payload.keywords, payload.answer, payload.confidence_threshold),
    )
    return {"ok": True, "id": row_id}


@router.put("/{entry_id}")
def update_knowledge(entry_id: int, payload: KnowledgePayload) -> dict:
    row = db.fetch_one("SELECT id FROM knowledge_base WHERE id = ?", (entry_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")

    db.execute(
        """
        UPDATE knowledge_base
        SET category = ?, keywords = ?, answer = ?, confidence_threshold = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (payload.category, payload.keywords, payload.answer, payload.confidence_threshold, entry_id),
    )
    return {"ok": True, "id": entry_id}


@router.delete("/{entry_id}")
def delete_knowledge(entry_id: int) -> dict:
    row = db.fetch_one("SELECT id FROM knowledge_base WHERE id = ?", (entry_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")

    db.execute("DELETE FROM knowledge_base WHERE id = ?", (entry_id,))
    return {"ok": True, "id": entry_id}
