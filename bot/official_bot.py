from __future__ import annotations

import asyncio
import threading
from typing import Any

import discord

from api.services.llm import LLMReply, generate_reply
from config import get_settings

try:
    from agent_sdk import run_agent_reply
except ModuleNotFoundError:
    from bot.agent_sdk import run_agent_reply

try:
    from watchdog import SENSITIVE_HINTS
    from router import _append_context, _connect, _enqueue_pending, _ensure_sender
except ModuleNotFoundError:
    from bot.watchdog import SENSITIVE_HINTS
    from bot.router import _append_context, _connect, _enqueue_pending, _ensure_sender


def _needs_human_reason(message: str) -> str:
    text = message.lower()
    for hint in SENSITIVE_HINTS:
        if hint in text:
            return f"mensagem sensivel: {hint}"
    if len(message.strip()) < 4:
        return "contexto insuficiente"
    return ""


def _bot_dm_prompt(content: str, sender_global_name: str, sender_username: str) -> str:
    display_name = sender_global_name or sender_username or "desconhecida"
    return (
        "Você é o bot de auto-resposta do Jeff no Discord. "
        "A pessoa está respondendo uma conversa que você iniciou porque o Jeff estava ocupado. "
        "Continue ajudando de forma curta, informal, em pt-BR, sem emoji e sem dizer que é IA. "
        "Dê no máximo um próximo passo seguro. "
        "Se faltar contexto, faça uma pergunta curta. "
        "Se a pessoa perguntar sobre contexto, histórico ou última pergunta, responda usando o histórico fornecido. "
        "Não peça senha, token ou credencial. "
        "Não sugira apagar dados, reiniciar produção ou ações destrutivas.\n\n"
        f"global_name da pessoa no Discord: {sender_global_name or '(vazio)'}\n"
        f"username da pessoa no Discord: {sender_username or '(vazio)'}\n"
        f"nome preferido para tratar a pessoa: {display_name}\n"
        f"Mensagem recebida: {content}"
    )


async def _raw_sender_history(conn: Any, sender_id: int, limit: int = 100) -> list[dict[str, Any]]:
    rows = await conn.execute_fetchall(
        """
        SELECT sequence_no, role, message
        FROM conversation_context
        WHERE sender_id = ?
        ORDER BY sequence_no DESC
        LIMIT ?
        """,
        (sender_id, limit),
    )
    ordered_rows = list(reversed(rows))
    return [
        {
            "sequence_no": int(row["sequence_no"]),
            "role": str(row["role"]),
            "content": str(row["message"]),
        }
        for row in ordered_rows
    ]


async def _summary_row(conn: Any, sender_id: int, channel_id: str) -> dict[str, Any]:
    row = await conn.execute_fetchall(
        """
        SELECT *
        FROM conversation_summaries
        WHERE sender_id = ? AND channel_id = ?
        LIMIT 1
        """,
        (sender_id, channel_id),
    )
    if row:
        return dict(row[0])

    await conn.execute(
        """
        INSERT INTO conversation_summaries (sender_id, channel_id, summary, source_message_count)
        VALUES (?, ?, '', 0)
        """,
        (sender_id, channel_id),
    )
    await conn.commit()
    created = await conn.execute_fetchall(
        """
        SELECT *
        FROM conversation_summaries
        WHERE sender_id = ? AND channel_id = ?
        LIMIT 1
        """,
        (sender_id, channel_id),
    )
    return dict(created[0]) if created else {"summary": "", "source_message_count": 0}


def _format_history_lines(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in messages:
        role = "Pessoa" if item["role"] == "user" else "Bot"
        lines.append(f"{role}: {item['content']}")
    return "\n".join(lines)


def _compress_prompt(existing_summary: str, messages: list[dict[str, Any]]) -> str:
    return (
        "Atualize a memória comprimida desta conversa de Discord. "
        "Mantenha fatos úteis, problemas, soluções já sugeridas, preferências do usuário, decisões e perguntas recentes. "
        "Remova cumprimentos, agradecimentos e repetição. "
        "Não invente. Não salve senhas, tokens ou credenciais. "
        "Responda em tópicos curtos, no máximo 180 palavras.\n\n"
        f"Resumo atual:\n{existing_summary or '(vazio)'}\n\n"
        f"Novas mensagens:\n{_format_history_lines(messages)}"
    )


async def _update_summary_if_needed(
    conn: Any,
    sender_id: int,
    channel_id: str,
    tail_size: int = 12,
    min_new_messages: int = 20,
) -> str:
    summary = await _summary_row(conn, sender_id, channel_id)
    source_count = int(summary.get("source_message_count") or 0)
    all_history = await _raw_sender_history(conn, sender_id, limit=500)
    total_count = len(all_history)
    new_count = total_count - source_count

    if total_count <= tail_size or new_count < min_new_messages:
        return str(summary.get("summary") or "")

    cutoff_count = max(total_count - tail_size, 0)
    messages_to_compress = all_history[source_count:cutoff_count]
    if not messages_to_compress:
        return str(summary.get("summary") or "")

    llm_reply: LLMReply = await asyncio.to_thread(
        generate_reply,
        _compress_prompt(str(summary.get("summary") or ""), messages_to_compress),
        [],
    )
    compressed = llm_reply.text.strip() or str(summary.get("summary") or "")
    await conn.execute(
        """
        UPDATE conversation_summaries
        SET summary = ?,
            source_message_count = ?,
            last_summarized_at = datetime('now'),
            updated_at = datetime('now')
        WHERE sender_id = ? AND channel_id = ?
        """,
        (compressed, cutoff_count, sender_id, channel_id),
    )
    await conn.commit()
    return compressed


async def _compressed_sender_history(
    conn: Any,
    sender_id: int,
    channel_id: str,
    sender_global_name: str,
    sender_username: str,
    sender_discord_id: str,
    tail_size: int = 30,
) -> list[dict[str, str]]:
    summary = await _update_summary_if_needed(conn, sender_id, channel_id)
    tail = await _raw_sender_history(conn, sender_id, limit=tail_size)

    history: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Identidade da pessoa nesta conversa: "
                f"global_name={sender_global_name or '(vazio)'}, "
                f"username={sender_username or '(vazio)'}, "
                f"discord_id={sender_discord_id}."
            ),
        }
    ]
    if summary:
        history.append(
            {
                "role": "system",
                "content": (
                    "Memória comprimida da conversa com esta pessoa. "
                    "Use como contexto estável, mas dê prioridade às mensagens recentes quando houver conflito.\n"
                    f"{summary}"
                ),
            }
        )
    history.extend({"role": item["role"], "content": item["content"]} for item in tail)
    return history


async def _queue_needs_human(
    sender_id: int,
    content: str,
    channel_id: str,
    message_id: str,
    sender_discord_id: str,
    sender_name: str,
    reason: str,
) -> int:
    conn = await _connect()
    try:
        queue_id = await _enqueue_pending(
            conn,
            sender_id=sender_id,
            intent="unknown",
            original_msg=content,
            suggested_reply="",
            confidence_score=0.0,
            meta={
                "source": "bot_dm_listener",
                "needs_human": True,
                "reason": reason,
                "channel_id": channel_id,
                "message_id": message_id,
                "sender_discord_id": sender_discord_id,
                "sender_name": sender_name,
            },
        )
        return queue_id
    finally:
        await conn.close()


async def _handle_bot_dm(message: discord.Message) -> None:
    content = str(message.content or "").strip()
    if not content:
        return

    author = message.author
    sender_discord_id = str(author.id)
    sender_global_name = str(getattr(author, "global_name", None) or "")
    sender_username = str(getattr(author, "name", "") or "")
    sender_name = sender_global_name or sender_username
    channel_id = str(message.channel.id)
    message_id = str(message.id)

    conn = await _connect()
    try:
        sender = await _ensure_sender(conn, sender_discord_id, sender_name)
        sender_id = int(sender["id"])
        await _append_context(conn, sender_id, role="user", intent="unknown", message=content)
        history = await _compressed_sender_history(
            conn,
            sender_id,
            channel_id,
            sender_global_name,
            sender_username,
            sender_discord_id,
            tail_size=30,
        )
    finally:
        await conn.close()

    image_urls = [
        str(att.url)
        for att in message.attachments
        if str(getattr(att, "content_type", "") or "").lower().startswith("image/")
    ]

    reason = _needs_human_reason(content)
    if reason:
        await _queue_needs_human(
            sender_id=sender_id,
            content=content,
            channel_id=channel_id,
            message_id=message_id,
            sender_discord_id=sender_discord_id,
            sender_name=sender_name,
            reason=reason,
        )
        notice = "vou chamar o Jeff pra ver isso melhor"
        await message.channel.send(notice)
        conn = await _connect()
        try:
            await _append_context(conn, sender_id, role="assistant", intent="unknown", message=notice)
        finally:
            await conn.close()
        return

    prompt = _bot_dm_prompt(content, sender_global_name, sender_username)
    settings = get_settings()
    if settings.use_agents_sdk:
        # Usa Agent + Runner + SQLiteSession: o histórico é gerenciado automaticamente.
        # O session_id é o discord_id do remetente para persistir entre reinicializações.
        reply_text = await run_agent_reply(
            user_message=prompt,
            session_id=sender_discord_id,
            image_urls=image_urls or None,
        )
    else:
        llm_reply = await asyncio.to_thread(generate_reply, prompt, history, image_urls or None)
        reply_text = llm_reply.text.strip() or "me manda mais um pouco de contexto"

    await message.channel.send(reply_text)

    conn = await _connect()
    try:
        await _append_context(conn, sender_id, role="assistant", intent="unknown", message=reply_text)
    finally:
        await conn.close()


async def _run_bot_client(token: str) -> None:
    intents = discord.Intents.default()
    intents.dm_messages = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        user = client.user
        print(f"[official_bot] conectado como {getattr(user, 'id', 'unknown')}")

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return
        try:
            await _handle_bot_dm(message)
        except Exception as exc:
            print(f"[official_bot] erro ao processar DM: {exc}")

    await client.start(token)


def start_official_bot_listener() -> None:
    settings = get_settings()
    if not settings.discord_bot_token:
        print("[official_bot] DISCORD_BOT_TOKEN ausente; listener do bot desativado")
        return

    def _runner() -> None:
        try:
            asyncio.run(_run_bot_client(settings.discord_bot_token))
        except Exception as exc:
            print(f"[official_bot] listener encerrou com erro: {exc}")

    thread = threading.Thread(target=_runner, name="official-discord-bot", daemon=True)
    thread.start()
    print("[official_bot] listener iniciado em background")
