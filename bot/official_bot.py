from __future__ import annotations

import asyncio
import threading
from typing import Any

import discord

from api.services.discord_outbound import notify_jeff, resolve_dm_channel_id, send_discord_message
from api.services.llm import LLMReply, generate_reply
from config import get_settings
from bot.agent_sdk import run_agent_reply

try:
    from watchdog import SENSITIVE_HINTS
    from router import _append_context, _connect, _enqueue_pending, _ensure_sender, _open_jeff_relay, _is_wants_jeff, _is_urgent
except ModuleNotFoundError:
    from bot.watchdog import SENSITIVE_HINTS
    from bot.router import _append_context, _connect, _enqueue_pending, _ensure_sender, _open_jeff_relay, _is_wants_jeff, _is_urgent

try:
    from admin import handle_admin_message, is_admin
except ModuleNotFoundError:
    from bot.admin import handle_admin_message, is_admin


# Estado para desambiguação multi-turn quando há múltiplos senders com nome similar
_pending_disambiguation: dict[str, list[dict[str, Any]]] = {}


# Keywords para detecção de query de Jeff
QUERY_KEYWORDS_SUMMARY_DAY = (
    "resumo do dia",
    "digest do dia",
    "o que aconteceu hoje",
    "o que rolou hoje",
    "conversas de hoje",
)

QUERY_KEYWORDS_SUMMARY_USER = (
    "resumo da conversa com",
    "conversa com",
    "me fala do",
    "me fala da",
    "como ta o",
    "como tá o",
    "como ta a",
    "como tá a",
)

QUERY_KEYWORDS_LAST_MESSAGE = (
    "última mensagem do",
    "ultima mensagem do",
    "última mensagem da",
    "ultima mensagem da",
    "o que o",
    "o que a",
)


def _is_admin_command_content(raw_content: str) -> bool:
    content = str(raw_content or "").strip()
    if "/" in content and not content.startswith("/"):
        first_slash = content.find("/")
        prefix = content[:first_slash].strip()
        if prefix.startswith("@") or prefix.startswith("<@"):
            content = content[first_slash:].strip()

    admin_commands = (
        "/add-channel",
        "/remove-channel",
        "/channels",
        "/senders",
        "/mode",
    )
    return any(content.startswith(cmd) for cmd in admin_commands)


def _detect_jeff_query(text: str) -> str | None:
    """Detecta se o texto é uma query de Jeff (summary_user, summary_day, last_message_user) ou None."""
    lower = text.lower()
    for keyword in QUERY_KEYWORDS_SUMMARY_DAY:
        if keyword in lower:
            return "summary_day"
    for keyword in QUERY_KEYWORDS_SUMMARY_USER:
        if keyword in lower:
            return "summary_user"
    for keyword in QUERY_KEYWORDS_LAST_MESSAGE:
        if keyword in lower:
            return "last_message_user"
    return None


def _extract_name_from_query(text: str, intent: str) -> str:
    """Extrai fragmento de nome da query após remover a keyword."""
    lower = text.lower()
    keywords = []
    if intent == "summary_day":
        return ""
    elif intent == "summary_user":
        keywords = list(QUERY_KEYWORDS_SUMMARY_USER)
    elif intent == "last_message_user":
        keywords = list(QUERY_KEYWORDS_LAST_MESSAGE)
    for keyword in keywords:
        if keyword in lower:
            remainder = lower.split(keyword, 1)[1]
            cleaned = remainder.strip().rstrip("?!,. ").strip()
            # Remove artigos no começo (o, a, os, as, um, uma, uns, umas)
            articles = ("o", "a", "os", "as", "um", "uma", "uns", "umas")
            for article in articles:
                if cleaned.startswith(article + " "):
                    cleaned = cleaned[len(article) + 1:]
                    break
            return cleaned
    return ""


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



async def _find_senders_by_name(conn: Any, name_fragment: str) -> list[dict[str, Any]]:
    """Busca senders no BD pelo nome (LIKE search)."""
    if len(name_fragment.strip()) < 2:
        return []
    cursor = await conn.execute(
        """
        SELECT id, discord_id, display_name
        FROM senders
        WHERE display_name LIKE ?
        ORDER BY display_name ASC
        LIMIT 10
        """,
        (f"%{name_fragment}%",),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows] if rows else []


async def _get_summary_for_sender(conn: Any, sender_id: int) -> str:
    """Retorna resumo da conversa para um sender específico."""
    cursor = await conn.execute(
        """
        SELECT summary, last_summarized_at
        FROM conversation_summaries
        WHERE sender_id = ?
        ORDER BY last_summarized_at DESC
        LIMIT 1
        """,
        (sender_id,),
    )
    row = await cursor.fetchone()
    if row and row["summary"]:
        return str(row["summary"]).strip()
    return "sem resumo disponível ainda"


async def _get_last_user_message(conn: Any, sender_id: int) -> str:
    """Retorna a última mensagem do usuário."""
    cursor = await conn.execute(
        """
        SELECT message, created_at
        FROM conversation_context
        WHERE sender_id = ? AND role = 'user'
        ORDER BY sequence_no DESC
        LIMIT 1
        """,
        (sender_id,),
    )
    row = await cursor.fetchone()
    if row:
        created_at = str(row["created_at"] or "")
        if created_at:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M")
            except Exception:
                time_str = "?"
        else:
            time_str = "?"
        message = str(row["message"] or "")
        return f"[{time_str}] {message}"
    return "nenhuma mensagem encontrada"


async def _build_day_digest(conn: Any) -> str:
    """Agregação de atividades do dia de todos os senders."""
    rows = await conn.execute_fetchall(
        """
        SELECT
            s.display_name,
            cs.summary,
            MAX(cc.created_at) AS last_activity
        FROM conversation_context cc
        JOIN senders s ON s.id = cc.sender_id
        LEFT JOIN conversation_summaries cs ON cs.sender_id = cc.sender_id
        WHERE cc.created_at >= date('now')
        GROUP BY cc.sender_id
        ORDER BY last_activity DESC
        """,
    )
    if not rows:
        return "nenhuma conversa registrada hoje"
    lines = []
    for row in rows:
        name = str(row["display_name"] or "contato")
        summary = str(row["summary"] or "sem resumo")[:120]
        last_activity = str(row["last_activity"] or "")
        if last_activity:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M")
            except Exception:
                time_str = "?"
        else:
            time_str = "?"
        lines.append(f"- {name} ({time_str}): {summary}")
    return "\n".join(lines)


async def _has_recent_notification(conn: Any, sender_id: int) -> bool:
    """Verifica se já notificamos Jeff sobre este sender nos últimos 2 minutos para evitar duplicação."""
    cursor = await conn.execute(
        """
        SELECT id FROM jeff_relays
        WHERE sender_id = ? AND status = 'waiting'
        AND created_at >= datetime('now', '-2 minutes')
        LIMIT 1
        """,
        (sender_id,),
    )
    row = await cursor.fetchone()
    return bool(row)


async def _handle_jeff_query(content: str, channel_id: str) -> str | None:
    """
    Orquestrador: processa queries de Jeff (resumos, últimas mensagens, digest).
    Retorna string de resposta ou None se não for uma query reconhecida.
    """
    # 1. Verifica se está em fluxo de desambiguação
    candidates = _pending_disambiguation.get(channel_id)
    if candidates:
        stripped = content.strip()
        if stripped.isdigit():
            idx = int(stripped) - 1
            if 0 <= idx < len(candidates):
                chosen = candidates[idx]
                _pending_disambiguation.pop(channel_id, None)
                conn = await _connect()
                try:
                    intent = _detect_jeff_query("")  # Dummy para determinar se era summary ou last_message
                    sender_id = int(chosen["id"])
                    summary = await _get_summary_for_sender(conn, sender_id)
                finally:
                    await conn.close()
                name = chosen["display_name"] or "contato"
                return f"{name}: {summary}"
            else:
                return f"numero invalido, manda um numero entre 1 e {len(candidates)}"
        else:
            # Jeff digitou algo novo — limpa desambiguação e prossegue
            _pending_disambiguation.pop(channel_id, None)

    # 2. Detecta intenção
    intent = _detect_jeff_query(content)
    if intent is None:
        return None

    # 3. Digest do dia
    if intent == "summary_day":
        conn = await _connect()
        try:
            digest = await _build_day_digest(conn)
        finally:
            await conn.close()
        return f"resumo do dia:\n{digest}"

    # 4. Busca por contato nomeado
    name_fragment = _extract_name_from_query(content, intent)
    if not name_fragment or len(name_fragment.strip()) < 2:
        return "qual contato voce quer consultar?"

    conn = await _connect()
    try:
        matches = await _find_senders_by_name(conn, name_fragment)
        if not matches:
            return f"nao encontrei ninguem com '{name_fragment}' no nome"
        if len(matches) > 1:
            _pending_disambiguation[channel_id] = matches
            lines = [f"{i+1}. {m['display_name']}" for i, m in enumerate(matches)]
            return "encontrei mais de um contato:\n" + "\n".join(lines) + "\nresponde com o numero"
        # Single match
        match = matches[0]
        sender_id = int(match["id"])
        if intent == "last_message_user":
            last_msg = await _get_last_user_message(conn, sender_id)
            name = match["display_name"] or "contato"
            return f"ultima mensagem de {name}: {last_msg}"
        else:  # summary_user
            summary = await _get_summary_for_sender(conn, sender_id)
            name = match["display_name"] or "contato"
            return f"{name}: {summary}"
    finally:
        await conn.close()


async def _pop_jeff_relay() -> dict[str, Any] | None:
    """Retorna e marca como 'replied' o relay pendente mais antigo de Jeff."""
    conn = await _connect()
    try:
        rows = await conn.execute_fetchall(
            """
            SELECT jr.id, jr.sender_id, jr.user_channel_id, s.discord_id AS sender_discord_id
            FROM jeff_relays
            JOIN senders s ON s.id = jr.sender_id
            WHERE status = 'waiting'
            ORDER BY created_at ASC
            LIMIT 1
            """,
        )
        if not rows:
            return None
        relay = dict(rows[0])
        await conn.execute(
            "UPDATE jeff_relays SET status = 'replied', updated_at = datetime('now') WHERE id = ?",
            (relay["id"],),
        )
        await conn.commit()
        return relay
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
    settings = get_settings()

    # Jeff está respondendo no DM do bot → repassa ao usuário que estava esperando.
    if settings.jeff_discord_id and sender_discord_id == settings.jeff_discord_id:
        relay = await _pop_jeff_relay()
        if relay:
            resolved = await asyncio.to_thread(resolve_dm_channel_id, str(relay.get("sender_discord_id") or ""))
            if resolved.success and resolved.channel_id:
                sent = await asyncio.to_thread(send_discord_message, resolved.channel_id, content)
                ack = "Enviado!" if sent.success else f"Falha ao enviar: {sent.error}"
            else:
                ack = f"Falha ao abrir DM do bot para o usuario: {resolved.error}"
            await message.channel.send(ack)
            return
        # Sem relay pendente: Jeff pode estar fazendo queries sobre conversas ou testando.
        reply = await _handle_jeff_query(content, channel_id)
        if reply is not None:
            await message.channel.send(reply)
            return

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

    # Pessoa pediu explicitamente para chamar o Jeff OU enviou mensagem urgente.
    wants_jeff = _is_wants_jeff(content)
    urgent = _is_urgent(content)

    if wants_jeff or urgent:
        conn = await _connect()
        try:
            summary = await _update_summary_if_needed(conn, sender_id, channel_id)
            trigger = "user_request" if wants_jeff else "urgency"

            # Verifica deduplicação: router.py pode já ter notificado Jeff
            already_notified = await _has_recent_notification(conn, sender_id)
            if already_notified:
                print(f"[official_bot] notificação recente já existe para sender_id={sender_id}, ignorando duplicata")
                reply = "Vou chamar o Jeff agora!" if wants_jeff else "Entendido, vou avisar o Jeff que é urgente!"
                await _append_context(conn, sender_id, role="assistant", intent="unknown", message=reply)
                await message.channel.send(reply)
                await conn.close()
                return

            await _open_jeff_relay(conn, sender_id, channel_id, content, trigger)
        finally:
            await conn.close()

        trigger_label = "user_request" if wants_jeff else "urgency"
        notified = await asyncio.to_thread(notify_jeff, sender_name, content, trigger_label, summary)
        reply = "Vou chamar o Jeff agora!" if wants_jeff else "Entendido, vou avisar o Jeff que é urgente!"
        if not notified.success:
            reply = "Tentei chamar o Jeff mas deu um problema, ele vai ver em breve."
        await message.channel.send(reply)

        conn = await _connect()
        try:
            await _append_context(conn, sender_id, role="assistant", intent="unknown", message=reply)
        finally:
            await conn.close()
        return

    # Mensagem sensível → fila para Jeff.
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

    # Resposta normal via LLM.
    if settings.use_agents_sdk:
        # Usa Agent + Runner + SQLiteSession: o histórico é gerenciado automaticamente.
        # O session_id é o discord_id do remetente para persistir entre reinicializações.
        # Passa o conteúdo bruto + identidade do remetente; as instruções de comportamento
        # estão nas `instructions` do Agent e não precisam ser repetidas a cada turno.
        display_name = sender_global_name or sender_username or None
        reply_text = await run_agent_reply(
            user_message=content,
            session_id=sender_discord_id,
            image_urls=image_urls or None,
            sender_display_name=display_name,
        )
    else:
        prompt = _bot_dm_prompt(content, sender_global_name, sender_username)
        llm_reply = await asyncio.to_thread(generate_reply, prompt, history, image_urls or None)
        reply_text = llm_reply.text.strip() or "me manda mais um pouco de contexto"

    await message.channel.send(reply_text)

    conn = await _connect()
    try:
        await _append_context(conn, sender_id, role="assistant", intent="unknown", message=reply_text)

        # Confiança baixa → notifica Jeff e abre relay para ele poder complementar.
        # Só aplicável no fluxo generate_reply; o agents SDK não expõe confidence_score.
        if not settings.use_agents_sdk and llm_reply.confidence_score < 0.5:
            summary = await _update_summary_if_needed(conn, sender_id, channel_id)
            await _open_jeff_relay(conn, sender_id, channel_id, content, "low_confidence")
            await asyncio.to_thread(notify_jeff, sender_name, content, "low_confidence", summary)
    finally:
        await conn.close()


async def _should_handle_server_message(message: discord.Message, client: discord.Client) -> tuple[bool, bool, bool]:
    """Decide se o bot oficial deve processar a mensagem de servidor.

    Retorna: (deve_processar, is_mentioned, in_discussion)
    """
    settings = get_settings()
    client_user = getattr(client, "user", None)
    live_bot_id = str(getattr(client_user, "id", "") or "")
    cfg_bot_id = str(settings.discord_bot_user_id or "")
    target_ids = {bot_id for bot_id in (live_bot_id, cfg_bot_id) if bot_id}

    is_mentioned = False
    for mentioned_user in (message.mentions or []):
        mention_id = str(getattr(mentioned_user, "id", "") or "")
        if mention_id and mention_id in target_ids:
            is_mentioned = True
            break

    if not is_mentioned and target_ids:
        content = str(message.content or "")
        for bot_id in target_ids:
            if f"<@{bot_id}>" in content or f"<@!{bot_id}>" in content:
                is_mentioned = True
                break

    in_discussion = False
    channel_id = str(getattr(message.channel, "id", "") or "")
    if channel_id:
        try:
            from server import check_discussion_channel  # type: ignore
        except ModuleNotFoundError:
            from bot.server import check_discussion_channel  # import local para evitar ciclo no módulo

        conn = await _connect()
        try:
            in_discussion = await check_discussion_channel(channel_id, conn)
        finally:
            await conn.close()

    return (is_mentioned or in_discussion), is_mentioned, in_discussion


async def _handle_bot_server_message(message: discord.Message, client: discord.Client) -> None:
    """Processa mensagens em servidor no bot oficial (menção ou canal de discussão)."""
    author_id = str(getattr(message.author, "id", "") or "")
    if is_admin(author_id) and _is_admin_command_content(str(message.content or "")):
        conn = await _connect()
        try:
            await handle_admin_message(message, client, conn)
        finally:
            await conn.close()
        return

    try:
        from server import route_server_message  # type: ignore
    except ModuleNotFoundError:
        from bot.server import route_server_message  # import local para evitar ciclo no módulo

    should_handle, is_mentioned, in_discussion = await _should_handle_server_message(message, client)
    if not should_handle:
        return

    print(
        "[official_bot] mensagem de servidor recebida",
        {
            "author": str(getattr(message.author, "id", "") or ""),
            "channel": str(getattr(message.channel, "id", "") or ""),
            "guild": str(getattr(message.guild, "id", "") or ""),
            "mentioned": is_mentioned,
            "discussion": in_discussion,
        },
    )
    await route_server_message(message, client)


async def _run_bot_client(token: str, *, enable_message_content: bool) -> None:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.dm_messages = True
    intents.message_content = enable_message_content
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        user = client.user
        print(
            f"[official_bot] conectado como {getattr(user, 'id', 'unknown')} "
            f"(message_content={enable_message_content})"
        )

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        try:
            if message.guild is None:
                await _handle_bot_dm(message)
            else:
                await _handle_bot_server_message(message, client)
        except Exception as exc:
            print(f"[official_bot] erro ao processar mensagem: {exc}")

    await client.start(token)


def start_official_bot_listener() -> None:
    settings = get_settings()
    if not settings.discord_bot_token:
        print("[official_bot] DISCORD_BOT_TOKEN ausente; listener do bot desativado")
        return

    def _runner() -> None:
        try:
            asyncio.run(_run_bot_client(settings.discord_bot_token, enable_message_content=True))
        except Exception as exc:
            msg = str(exc)
            if "privileged intents" in msg.lower() or "message content" in msg.lower():
                print("[official_bot] intent privilegiada indisponível; reiniciando sem message_content")
                try:
                    asyncio.run(_run_bot_client(settings.discord_bot_token, enable_message_content=False))
                    return
                except Exception as fallback_exc:
                    print(f"[official_bot] listener encerrou com erro no fallback: {fallback_exc}")
                    return
            print(f"[official_bot] listener encerrou com erro: {exc}")

    thread = threading.Thread(target=_runner, name="official-discord-bot", daemon=True)
    thread.start()
    print("[official_bot] listener iniciado em background")
