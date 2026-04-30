from __future__ import annotations

import asyncio
import json
from typing import Any

from api.services.discord_outbound import resolve_dm_channel_id, send_discord_message
from api.services.llm import generate_reply
from config import get_settings

try:
    from router import _append_context, _connect, _enqueue_pending
except ModuleNotFoundError:
    from bot.router import _append_context, _connect, _enqueue_pending


SENSITIVE_HINTS = (
    "senha",
    "token",
    "secret",
    "credencial",
    "credential",
    "api key",
    "chave",
    "pix",
    "cartao",
    "cartão",
    "banco de dados",
    "drop",
    "delete",
    "deletar",
    "produção",
    "prod",
)


async def _overdue_watches(limit: int = 20) -> list[dict[str, Any]]:
    settings = get_settings()
    conn = await _connect()
    try:
        rows = await conn.execute_fetchall(
            """
            SELECT
                cw.id,
                cw.sender_id,
                cw.channel_id,
                cw.last_incoming_message,
                cw.last_incoming_message_id,
                cw.last_incoming_at,
                cw.meta_json,
                s.discord_id,
                s.display_name
            FROM conversation_watch cw
            JOIN senders s ON s.id = cw.sender_id
            WHERE cw.status = 'watching'
              AND cw.last_incoming_at <= datetime('now', ?)
              AND (
                  cw.last_jeff_reply_at IS NULL
                  OR cw.last_jeff_reply_at < cw.last_incoming_at
              )
            ORDER BY cw.last_incoming_at ASC
            LIMIT ?
            """,
            (f"-{settings.auto_reply_delay_minutes} minutes", limit),
        )
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def _mark_needs_human(watch: dict[str, Any]) -> int:
    conn = await _connect()
    try:
        raw_meta = str(watch.get("meta_json") or "")
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except json.JSONDecodeError:
            meta = {}

        meta.update(
            {
                "watchdog": True,
                "needs_human": True,
                "reason": "jeff_inactive_timeout",
                "channel_id": str(watch.get("channel_id") or ""),
                "message_id": str(watch.get("last_incoming_message_id") or ""),
                "sender_discord_id": str(watch.get("discord_id") or ""),
                "sender_name": str(watch.get("display_name") or ""),
            }
        )

        queue_id = await _enqueue_pending(
            conn,
            sender_id=int(watch["sender_id"]),
            intent="unknown",
            original_msg=str(watch.get("last_incoming_message") or ""),
            suggested_reply="",
            confidence_score=0.0,
            meta=meta,
        )
        await conn.execute(
            """
            UPDATE conversation_watch
            SET status = 'needs_human',
                needs_human_reason = 'jeff_inactive_timeout',
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (int(watch["id"]),),
        )
        await conn.commit()
        return queue_id
    finally:
        await conn.close()


def _needs_human_reason(message: str) -> str:
    text = message.lower()
    for hint in SENSITIVE_HINTS:
        if hint in text:
            return f"mensagem sensivel: {hint}"
    if len(message.strip()) < 8:
        return "contexto insuficiente"
    return ""


def _auto_reply_prompt(message: str, sender_name: str) -> str:
    return (
        "Você responde no Discord no lugar do Jeff enquanto ele está ocupado. "
        "Responda em pt-BR, curto, informal, sem emoji, sem dizer que é IA. "
        "Comece exatamente com: Coe coe, o Jeff ta meio cheio de coisas pra fazer agora e acho que ele n viu tua mensagem. "
        "Depois diga qual problema você entendeu e dê no máximo um próximo passo seguro. "
        "Não peça senha, token ou credencial. Não sugira apagar dados, reiniciar produção ou ações destrutivas. "
        "Se faltar contexto, faça uma pergunta curta.\n\n"
        f"global_name da pessoa no Discord: {sender_name or '(vazio)'}\n"
        f"nome preferido para tratar a pessoa: {sender_name or 'desconhecida'}\n"
        f"Mensagem: {message}"
    )


async def _mark_auto_replied(watch: dict[str, Any], reply_text: str, sent_message_id: str | None) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            """
            UPDATE conversation_watch
            SET status = 'auto_replied',
                auto_reply_sent_at = datetime('now'),
                needs_human_reason = NULL,
                meta_json = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                json.dumps(
                    {
                        "watchdog": True,
                        "auto_replied": True,
                        "bot_message_id": sent_message_id,
                        "sender_discord_id": str(watch.get("discord_id") or ""),
                    }
                ),
                int(watch["id"]),
            ),
        )
        await _append_context(
            conn,
            sender_id=int(watch["sender_id"]),
            role="assistant",
            intent="unknown",
            message=reply_text,
        )
        await conn.commit()
    finally:
        await conn.close()


async def _try_auto_reply(watch: dict[str, Any]) -> bool:
    message = str(watch.get("last_incoming_message") or "").strip()
    reason = _needs_human_reason(message)
    if reason:
        watch["auto_reply_blocked_reason"] = reason
        return False

    recipient_id = str(watch.get("discord_id") or "").strip()
    if not recipient_id:
        watch["auto_reply_blocked_reason"] = "sender discord_id ausente"
        return False

    sender_name = str(watch.get("display_name") or "").strip()
    prompt = _auto_reply_prompt(message, sender_name)

    raw_meta = str(watch.get("meta_json") or "")
    try:
        watch_meta = json.loads(raw_meta) if raw_meta else {}
    except json.JSONDecodeError:
        watch_meta = {}
    image_urls = [str(u) for u in (watch_meta.get("image_urls") or []) if u]

    llm_reply = await asyncio.to_thread(generate_reply, prompt, [], image_urls or None)
    reply_text = llm_reply.text.strip()
    if not reply_text:
        watch["auto_reply_blocked_reason"] = "LLM sem resposta"
        return False

    resolved = await asyncio.to_thread(resolve_dm_channel_id, recipient_id)
    if not resolved.success or not resolved.channel_id:
        watch["auto_reply_blocked_reason"] = f"falha ao abrir DM do bot: {resolved.error}"
        return False

    sent = await asyncio.to_thread(send_discord_message, resolved.channel_id, reply_text, None)
    if not sent.success:
        watch["auto_reply_blocked_reason"] = f"falha ao enviar DM do bot: {sent.error}"
        return False

    await _mark_auto_replied(watch, reply_text, sent.message_id)
    return True


async def run_watchdog() -> None:
    """Marca conversas sem resposta do Jeff para aparecerem na UI."""
    settings = get_settings()
    print(
        "[watchdog] iniciado",
        {
            "delay_minutes": settings.auto_reply_delay_minutes,
            "poll_seconds": settings.watchdog_poll_seconds,
            "auto_reply_enabled": settings.auto_reply_enabled,
        },
    )

    while True:
        try:
            watches = await _overdue_watches()
            for watch in watches:
                if settings.auto_reply_enabled and await _try_auto_reply(watch):
                    print(
                        "[watchdog] auto resposta enviada",
                        {
                            "watch_id": watch.get("id"),
                            "sender": watch.get("discord_id"),
                        },
                    )
                    continue

                if settings.auto_reply_enabled and watch.get("auto_reply_blocked_reason"):
                    watch["meta_json"] = json.dumps(
                        {
                            "source": "discord_listener",
                            "auto_reply_failed": True,
                            "failure_reason": watch["auto_reply_blocked_reason"],
                        }
                    )
                queue_id = await _mark_needs_human(watch)
                print(
                    "[watchdog] conversa marcada para UI",
                    {
                        "watch_id": watch.get("id"),
                        "queue_id": queue_id,
                        "sender": watch.get("discord_id"),
                        "channel_id": watch.get("channel_id"),
                    },
                )
        except Exception as exc:
            print(f"[watchdog] erro: {exc}")

        await asyncio.sleep(max(settings.watchdog_poll_seconds, 5))
