from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from api.services.discord_outbound import notify_jeff, resolve_dm_channel_id, send_discord_message, send_via_userbot
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

FAREWELL_HINTS = (
    "valeu",
    "obrigado",
    "obrigada",
    "vlw",
    "tmj",
    "falou",
    "entendi",
    "consegui",
    "resolveu",
    "resolvido",
    "era isso",
    "era só isso",
    "foi isso",
    "deu certo",
    "funcionou",
    "já vi",
    "ja vi",
    "já entendi",
    "ja entendi",
    "ok obg",
    "ok, obg",
    "tá bom",
    "ta bom",
    "tchau",
    "flw",
    "abs",
    "até mais",
)

_BOT_CLIENT_ID = "1499060956159016970"

# Se Jeff respondeu nessa conversa nos últimos N minutos, considera que ele ainda
# está ativo e o watchdog não intervém.
_JEFF_ACTIVE_GRACE_MINUTES = 20


def _is_farewell(message: str) -> bool:
    text = message.lower().strip()
    for hint in FAREWELL_HINTS:
        if hint in text:
            return True
    return False


def _jeff_replied_recently(watch: dict[str, Any]) -> bool:
    last_reply = str(watch.get("last_jeff_reply_at") or "")
    if not last_reply:
        return False
    try:
        replied_dt = datetime.fromisoformat(last_reply)
        if replied_dt.tzinfo is None:
            replied_dt = replied_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - replied_dt).total_seconds() / 60 < _JEFF_ACTIVE_GRACE_MINUTES
    except Exception:
        return False


def _oauth_invite_message() -> str:
    return (
        "Oi! Para eu conseguir te responder pelo bot aqui, precisa de uma autorização rápida:\n"
        f"1. Clica nesse link: https://discord.com/oauth2/authorize?client_id={_BOT_CLIENT_ID}\n"
        "2. Clica em **Adicionar aos meus aplicativos** e autoriza\n"
        "Depois manda sua mensagem de novo que o bot te responde!"
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
                cw.last_jeff_reply_at,
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


async def _mark_ignored(watch: dict[str, Any], reason: str) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            """
            UPDATE conversation_watch
            SET status = 'ignored',
                needs_human_reason = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (reason, int(watch["id"])),
        )
        await conn.commit()
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

    # Se Jeff já respondeu ao menos uma vez e a última mensagem é despedida,
    # não intervém — a conversa já foi atendida.
    jeff_replied_once = bool(watch.get("last_jeff_reply_at"))
    if jeff_replied_once and _is_farewell(message):
        watch["auto_reply_blocked_reason"] = "farewell_resolved"
        return False

    reason = _needs_human_reason(message)
    if reason:
        watch["auto_reply_blocked_reason"] = reason
        return False

    recipient_discord_id = str(watch.get("discord_id") or "").strip()
    channel_id = str(watch.get("channel_id") or "").strip()
    if not recipient_discord_id or not channel_id:
        watch["auto_reply_blocked_reason"] = "dados insuficientes no watch"
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

    # Tenta primeiro pelo bot oficial (DM dedicada do bot com o usuário).
    resolved = await asyncio.to_thread(resolve_dm_channel_id, recipient_discord_id)
    if resolved.success and resolved.channel_id:
        sent = await asyncio.to_thread(send_discord_message, resolved.channel_id, reply_text, None)
        if sent.success:
            await _mark_auto_replied(watch, reply_text, sent.message_id)
            return True
        bot_error = sent.error or ""
    else:
        bot_error = resolved.error or ""

    # Bot falhou por falta de servidor em comum (50278) → manda link OAuth via
    # userbot no canal original da DM, para a pessoa autorizar o bot.
    no_mutual_guilds = "50278" in bot_error or "mutual" in bot_error.lower()
    if no_mutual_guilds:
        oauth_sent = await asyncio.to_thread(send_via_userbot, channel_id, _oauth_invite_message())
        if oauth_sent.success:
            watch["auto_reply_blocked_reason"] = "oauth_link_enviado"
            return False
        watch["auto_reply_blocked_reason"] = f"falha ao enviar oauth via userbot: {oauth_sent.error}"
    else:
        watch["auto_reply_blocked_reason"] = f"falha ao enviar via bot: {bot_error}"

    return False


async def _maybe_notify_jeff_summary(watch: dict[str, Any]) -> None:
    """Envia resumo ao Jeff quando a conversa já tem histórico significativo."""
    conn = await _connect()
    try:
        rows = await conn.execute_fetchall(
            "SELECT COUNT(*) AS cnt FROM conversation_context WHERE sender_id = ?",
            (int(watch["sender_id"]),),
        )
        msg_count = int(rows[0]["cnt"]) if rows else 0
        if msg_count < 6:
            return

        summary_rows = await conn.execute_fetchall(
            "SELECT summary FROM conversation_summaries WHERE sender_id = ? AND channel_id = ? LIMIT 1",
            (int(watch["sender_id"]), str(watch.get("channel_id") or "")),
        )
        summary = str(summary_rows[0]["summary"]) if summary_rows else ""

        sender_name = str(watch.get("display_name") or str(watch.get("discord_id") or ""))
        last_msg = str(watch.get("last_incoming_message") or "")
        await asyncio.to_thread(notify_jeff, sender_name, last_msg, "watchdog", summary)
    except Exception as exc:
        print(f"[watchdog] erro ao notificar Jeff com resumo: {exc}")
    finally:
        await conn.close()


async def _inactive_finished_watches(limit: int = 20) -> list[dict[str, Any]]:
    """Conversas onde alguém respondeu (Jeff ou bot) e a pessoa ficou inativa 30+ min."""
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
                cw.last_incoming_at,
                cw.last_jeff_reply_at,
                cw.auto_reply_sent_at,
                cw.meta_json,
                s.discord_id,
                s.display_name
            FROM conversation_watch cw
            JOIN senders s ON s.id = cw.sender_id
            WHERE cw.status IN ('watching', 'auto_replied', 'needs_human')
              AND cw.last_incoming_at <= datetime('now', ?)
              AND (
                  (cw.last_jeff_reply_at IS NOT NULL AND cw.last_jeff_reply_at > cw.last_incoming_at)
                  OR
                  (cw.auto_reply_sent_at IS NOT NULL AND cw.auto_reply_sent_at > cw.last_incoming_at)
              )
            ORDER BY cw.last_incoming_at ASC
            LIMIT ?
            """,
            (f"-{settings.inactivity_close_minutes} minutes", limit),
        )
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def _close_and_summarize(watch: dict[str, Any]) -> None:
    """Fecha conversa inativa e envia resumo ao Jeff via DM do bot."""
    sender_id = int(watch["sender_id"])
    channel_id = str(watch.get("channel_id") or "")
    sender_name = str(watch.get("display_name") or str(watch.get("discord_id") or ""))

    conn = await _connect()
    try:
        await conn.execute(
            """
            UPDATE conversation_watch
            SET status = 'resolved',
                needs_human_reason = 'inactivity_timeout',
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (int(watch["id"]),),
        )

        summary_rows = await conn.execute_fetchall(
            "SELECT summary FROM conversation_summaries WHERE sender_id = ? AND channel_id = ? LIMIT 1",
            (sender_id, channel_id),
        )
        existing_summary = str(summary_rows[0]["summary"]) if summary_rows else ""

        tail_rows = await conn.execute_fetchall(
            """
            SELECT role, message FROM conversation_context
            WHERE sender_id = ?
            ORDER BY sequence_no DESC
            LIMIT 30
            """,
            (sender_id,),
        )
        tail = list(reversed([dict(r) for r in tail_rows]))
        await conn.commit()
    finally:
        await conn.close()

    closing_summary = existing_summary
    if tail:
        history_text = "\n".join(
            f"{'Pessoa' if r['role'] == 'user' else 'Bot'}: {r['message']}"
            for r in tail
        )
        prompt = (
            "Gere um resumo conciso desta conversa de suporte que foi encerrada por inatividade. "
            "Inclua: qual era o problema, o que foi discutido, se foi resolvido e como ficou. "
            "Máximo 5 linhas, em pt-BR.\n\n"
            f"Contexto anterior: {existing_summary or '(sem resumo prévio)'}\n\n"
            f"Conversa:\n{history_text}"
        )
        try:
            llm_reply = await asyncio.to_thread(generate_reply, prompt, [])
            closing_summary = llm_reply.text.strip() or existing_summary
        except Exception as exc:
            print(f"[watchdog] erro ao gerar resumo de fechamento: {exc}")

    last_msg = str(watch.get("last_incoming_message") or "")
    result = await asyncio.to_thread(
        notify_jeff,
        sender_name,
        f"[Conversa encerrada]\nÚltima mensagem da pessoa: {last_msg}",
        "conversation_closed",
        closing_summary,
    )
    print(
        "[watchdog] conversa fechada por inatividade",
        {
            "watch_id": watch.get("id"),
            "sender": sender_name,
            "notificou_jeff": result.success,
        },
    )


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
            # Fecha conversas inativas (pessoa sumiu após ser atendida).
            finished = await _inactive_finished_watches()
            for watch in finished:
                try:
                    await _close_and_summarize(watch)
                except Exception as exc:
                    print(f"[watchdog] erro ao fechar conversa inativa: {exc}")

            watches = await _overdue_watches()
            for watch in watches:
                # Jeff está ativo nessa conversa (respondeu nos últimos N min) → não intervém.
                if _jeff_replied_recently(watch):
                    print(
                        "[watchdog] Jeff ativo na conversa, ignorando",
                        {"watch_id": watch.get("id"), "sender": watch.get("discord_id")},
                    )
                    continue

                if settings.auto_reply_enabled and await _try_auto_reply(watch):
                    print(
                        "[watchdog] auto resposta enviada",
                        {"watch_id": watch.get("id"), "sender": watch.get("discord_id")},
                    )
                    # Envia resumo ao Jeff se a conversa já tem histórico relevante.
                    await _maybe_notify_jeff_summary(watch)
                    continue

                blocked_reason = watch.get("auto_reply_blocked_reason", "")

                # Despedida detectada após Jeff ter atendido → marca como ignorado.
                if blocked_reason == "farewell_resolved":
                    await _mark_ignored(watch, "despedida_apos_atendimento")
                    print(
                        "[watchdog] conversa encerrada por despedida",
                        {"watch_id": watch.get("id"), "sender": watch.get("discord_id")},
                    )
                    continue

                # Link OAuth enviado → aguarda o usuário autorizar; não vai pra fila ainda.
                if blocked_reason == "oauth_link_enviado":
                    print(
                        "[watchdog] oauth link enviado, aguardando autorização",
                        {"watch_id": watch.get("id"), "sender": watch.get("discord_id")},
                    )
                    continue

                if settings.auto_reply_enabled and blocked_reason:
                    watch["meta_json"] = json.dumps(
                        {
                            "source": "discord_listener",
                            "auto_reply_failed": True,
                            "failure_reason": blocked_reason,
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
