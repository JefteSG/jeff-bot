from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services import db
from api.services.discord_outbound import resolve_dm_channel_id, send_discord_message
from api.services.llm import generate_reply


router = APIRouter(prefix="/messages", tags=["messages"])
CONTEXT_WINDOW_MINUTES = 3


class ApprovalActionPayload(BaseModel):
    final_reply: str | None = Field(default=None, max_length=4000)


def _recent_sender_history(sender_id: int) -> list[dict[str, str]]:
    rows = db.fetch_all(
        """
        SELECT role, message
        FROM conversation_context
        WHERE sender_id = ?
          AND created_at >= datetime('now', ?)
        ORDER BY sequence_no ASC
        LIMIT 50
        """,
        (sender_id, f"-{CONTEXT_WINDOW_MINUTES} minutes"),
    )
    return [{"role": str(r["role"]), "content": str(r["message"])} for r in rows]


def _append_assistant_context(sender_id: int, intent: str, message: str) -> None:
    row = db.fetch_one(
        "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_seq FROM conversation_context WHERE sender_id = ?",
        (sender_id,),
    )
    next_seq = int(row["next_seq"]) if row else 1
    db.execute(
        """
        INSERT INTO conversation_context (sender_id, role, intent, message, sequence_no)
        VALUES (?, 'assistant', ?, ?, ?)
        """,
        (sender_id, intent or "unknown", message, next_seq),
    )


def _find_sender_channel_id_from_queue(sender_id: int) -> str:
    rows = db.fetch_all(
        """
        SELECT meta_json
        FROM message_queue
        WHERE sender_id = ?
        ORDER BY id DESC
        LIMIT 50
        """,
        (sender_id,),
    )
    for row in rows:
        try:
            meta = json.loads(str(row["meta_json"] or "")) if row["meta_json"] else {}
        except json.JSONDecodeError:
            continue
        channel_id = str(meta.get("channel_id") or "").strip()
        if channel_id:
            return channel_id
    return ""


@router.get("/queue")
def list_queue(status: str = "pending") -> list[dict]:
    rows = db.fetch_all(
        """
        SELECT mq.id, s.display_name AS sender_name, s.discord_id, mq.status, mq.intent,
               mq.original_msg, mq.suggested_reply, mq.final_reply, mq.confidence_score, mq.meta_json, mq.created_at
        FROM message_queue mq
        JOIN senders s ON s.id = mq.sender_id
        WHERE mq.status = ?
        ORDER BY mq.created_at DESC
        """,
        (status,),
    )
    return [dict(r) for r in rows]


@router.post("/queue/{queue_id}/approve")
async def approve_message(queue_id: int, payload: ApprovalActionPayload) -> dict:
    row = db.fetch_one(
        """
        SELECT mq.id, mq.sender_id, mq.intent, mq.suggested_reply, mq.original_msg, mq.meta_json, s.discord_id, s.display_name
        FROM message_queue mq
        JOIN senders s ON s.id = mq.sender_id
        WHERE mq.id = ?
        """,
        (queue_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")

    meta_json = str(row["meta_json"] or "")
    try:
        meta = json.loads(meta_json) if meta_json else {}
    except json.JSONDecodeError:
        meta = {}

    # Etapa 1: aprovação pré-IA -> gera sugestão e mantém na fila para segunda aprovação.
    if bool(meta.get("pre_ai")):
        sender_id = int(row["sender_id"])
        history = _recent_sender_history(sender_id)
        # Adiciona system prompt ao início do contexto
        system_prompt = {
            "role": "system",
            "content": (
                "Você responde no lugar de Jefte no Discord. "
                "Siga os exemplos abaixo à risca — esse é o único estilo aceito.\n\n"

                "EXEMPLOS:\n"
                "Pergunta: oi tudo bom?\n"
                "Resposta: oi, tudo ss\n\n"

                "Pergunta: qual a senha do admin do erp?\n"
                "Resposta: admin@123\n\n"

                "Pergunta: como vejo o log do serviço x?\n"
                "Resposta: journalctl -u x -f\n\n"

                "Pergunta: tá dando erro 502 no site\n"
                "Resposta: qual site?\n\n"

                "Pergunta: o site taltal\n"
                "Resposta: reinicia o nginx lá e me fala\n\n"

                "REGRAS:\n"
                "- máximo 10 palavras por resposta\n"
                "- se precisar de mais info, faz UMA pergunta curta\n"
                "- só manda o comando, sem explicar\n"
                "- nunca cumprimente de volta se já cumprimentou antes na conversa\n"
                "- zero formalidade, zero emojis, zero 'claro!', zero 'com certeza!'\n"
                "- se não souber, responde: deixa eu verificar\n"
            )
        }
        history.insert(0, system_prompt)
        llm_reply = await asyncio.to_thread(generate_reply, str(row["original_msg"]), history)

        meta["pre_ai"] = False
        meta["approved_for_ai"] = True
        meta["response_approval"] = True

        db.execute(
            """
            UPDATE message_queue
            SET intent = COALESCE(NULLIF(intent, 'unknown'), 'routine_question'),
                suggested_reply = ?,
                confidence_score = ?,
                meta_json = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (llm_reply.text, llm_reply.confidence_score, json.dumps(meta), queue_id),
        )

        return {
            "ok": True,
            "queue_id": queue_id,
            "status": "pending",
            "stage": "response_approval",
            "suggested_reply": llm_reply.text,
        }

    # Etapa 2: aprovação da resposta -> envia para Discord e conclui.
    reply_text = (payload.final_reply or str(row["suggested_reply"] or "")).strip()
    if not reply_text:
        raise HTTPException(status_code=400, detail="No reply text to send")

    channel_id = str(meta.get("channel_id") or "")
    message_id = str(meta.get("message_id") or "")
    if not channel_id:
        channel_id = _find_sender_channel_id_from_queue(int(row["sender_id"]))

    if not channel_id:
        sender_discord_id = str(row["discord_id"] or "")
        if not sender_discord_id:
            raise HTTPException(status_code=400, detail="Missing sender discord_id for fallback channel resolution")

        resolved = await asyncio.to_thread(resolve_dm_channel_id, sender_discord_id)
        if not resolved.success or not resolved.channel_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Missing channel_id for Discord send and failed to resolve DM channel. "
                    f"Fallback error: {resolved.error}"
                ),
            )

        channel_id = resolved.channel_id
        meta["channel_id"] = channel_id
        db.execute(
            "UPDATE message_queue SET meta_json = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(meta), queue_id),
        )

    send_result = await asyncio.to_thread(
        send_discord_message,
        channel_id,
        reply_text,
        message_id or None,
    )
    if not send_result.success:
        raise HTTPException(status_code=502, detail=f"Discord send failed: {send_result.error}")

    db.execute(
        "UPDATE message_queue SET status = 'approved', final_reply = ?, updated_at = datetime('now') WHERE id = ?",
        (reply_text, queue_id),
    )
    _append_assistant_context(
        sender_id=int(row["sender_id"]),
        intent=str(row["intent"] or "unknown"),
        message=reply_text,
    )
    return {
        "ok": True,
        "queue_id": queue_id,
        "status": "approved",
        "sent_message_id": send_result.message_id,
    }


@router.post("/queue/{queue_id}/reject")
def reject_message(queue_id: int) -> dict:
    row = db.fetch_one("SELECT id FROM message_queue WHERE id = ?", (queue_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")

    db.execute("UPDATE message_queue SET status = 'rejected', updated_at = datetime('now') WHERE id = ?", (queue_id,))
    return {"ok": True, "queue_id": queue_id, "status": "rejected"}


@router.post("/queue/{queue_id}/self-replied")
def self_replied(queue_id: int) -> dict:
    row = db.fetch_one("SELECT id FROM message_queue WHERE id = ?", (queue_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Queue item not found")

    db.execute(
        "UPDATE message_queue SET status = 'self_replied', updated_at = datetime('now') WHERE id = ?",
        (queue_id,),
    )
    return {"ok": True, "queue_id": queue_id, "status": "self_replied"}
