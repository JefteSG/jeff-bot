"""Módulo de administração do Jeff Bot.

Gerencia comandos de admin enviados via DM pelo próprio Jeff (Jefte).
Todos os métodos recebem objetos do selfcord e uma conexão aiosqlite já aberta.
"""
from __future__ import annotations

from typing import Any
import asyncio

import aiosqlite
from api.services.discord_outbound import resolve_dm_channel_id, send_discord_message, send_via_userbot

ADMIN_ID = "252486297799622657"


def is_admin(user_id: Any) -> bool:
    """Retorna True se o user_id pertence ao administrador do bot."""
    return str(user_id) == ADMIN_ID


async def handle_admin_message(message: Any, client: Any, conn: aiosqlite.Connection) -> None:
    """Despacha comandos de admin recebidos via DM."""
    raw_content = str(getattr(message, "content", "") or "").strip()
    content = raw_content
    # Aceita formatos como "@Demon lord /add-channel" ou "<@id> /add-channel".
    if "/" in content and not content.startswith("/"):
        first_slash = content.find("/")
        prefix = content[:first_slash].strip()
        if prefix.startswith("@") or prefix.startswith("<@"):
            content = content[first_slash:].strip()

    channel = getattr(message, "channel", None)
    # Extração robusta de channel_id e guild_id (selfcord pode expor via .channel.id)
    channel_id = (
        str(getattr(message, "channel_id", "") or "")
        or str(getattr(channel, "id", "") or "")
        or str((getattr(message, "_data", None) or {}).get("channel_id", "") or "")
    )
    guild_id_raw = (
        getattr(message, "guild_id", None)
        or getattr(getattr(message, "guild", None), "id", None)
        or getattr(getattr(channel, "guild", None), "id", None)
        or (getattr(message, "_data", None) or {}).get("guild_id")
    )
    guild_id = str(guild_id_raw) if guild_id_raw else None
    print(f"[admin] canal={channel_id!r} guild={guild_id!r}")

    async def reply(text: str) -> None:
        if not channel_id:
            print("[admin] erro ao responder admin: channel_id ausente")
            return
        try:
            sent = await asyncio.to_thread(send_discord_message, channel_id, text)
            if sent.success:
                return
            err = sent.error or ""
            print(f"[admin] bot falhou ao responder ({err}), tentando userbot")
            fallback = await asyncio.to_thread(send_via_userbot, channel_id, text, "server_reply")
            if not fallback.success:
                print(f"[admin] userbot também falhou: {fallback.error}")
        except Exception as exc:
            print(f"[admin] erro ao responder admin: {exc}")

    # --- /add-channel <channel_id> <guild_id> [nome] ---
    if content.startswith("/add-channel"):
        parts = content.split()
        ch_id = ""
        g_id = ""
        ch_name: str | None = None

        # /add-channel sem args: usa o canal atual (se for servidor)
        if len(parts) == 1:
            ch_id = channel_id
            g_id = guild_id or ""
            channel_name = getattr(channel, "name", None)
            ch_name = str(channel_name) if channel_name else None
            if not ch_id or not g_id:
                await reply(f"Uso: /add-channel <channel_id> <guild_id> [nome] (canal={ch_id!r} guild={g_id!r})")
                return
        elif len(parts) == 2:
            ch_id = parts[1]
            g_id = guild_id or ""
            if not g_id:
                await reply("Faltou guild_id. Use: /add-channel <channel_id> <guild_id> [nome]")
                return
            channel_name = getattr(channel, "name", None)
            ch_name = str(channel_name) if channel_name else None
        else:
            ch_id = parts[1]
            g_id = parts[2]
            ch_name = " ".join(parts[3:]) if len(parts) > 3 else None

        try:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO discussion_channels (channel_id, guild_id, channel_name) VALUES (?, ?, ?)",
                (ch_id, g_id, ch_name),
            )
            await conn.commit()
            if cursor.rowcount == 0:
                await reply(f"Canal `{ch_id}` já estava registrado como discussão.")
            else:
                await reply(f"Canal `{ch_id}` adicionado como canal de discussão.")
        except Exception as exc:
            await reply(f"Erro ao adicionar canal: {exc}")
        return

    # --- /remove-channel <channel_id> ---
    if content.startswith("/remove-channel"):
        parts = content.split()
        if len(parts) < 2:
            await reply("Uso: /remove-channel <channel_id>")
            return
        ch_id = parts[1]
        try:
            await conn.execute(
                "DELETE FROM discussion_channels WHERE channel_id = ?", (ch_id,)
            )
            await conn.commit()
            await reply(f"Canal `{ch_id}` removido dos canais de discussão.")
        except Exception as exc:
            await reply(f"Erro ao remover canal: {exc}")
        return

    # --- /channels ---
    if content.startswith("/channels"):
        try:
            async with conn.execute(
                "SELECT channel_id, guild_id, channel_name, added_at FROM discussion_channels ORDER BY added_at DESC"
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                await reply("Nenhum canal de discussão registrado.")
            else:
                lines = ["**Canais de discussão:**"]
                for row in rows:
                    name_part = f" ({row[2]})" if row[2] else ""
                    lines.append(f"• `{row[0]}`{name_part} — guild `{row[1]}`")
                await reply("\n".join(lines))
        except Exception as exc:
            await reply(f"Erro ao listar canais: {exc}")
        return

    # --- /senders ---
    if content.startswith("/senders"):
        try:
            async with conn.execute(
                "SELECT id, discord_id, display_name, mode, trust_score FROM senders ORDER BY id"
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                await reply("Nenhum sender registrado.")
            else:
                lines = ["**Senders registrados:**"]
                for row in rows:
                    lines.append(
                        f"• [{row[0]}] `{row[2] or row[1]}` — mode=`{row[3]}` trust={row[4]:.2f}"
                    )
                await reply("\n".join(lines))
        except Exception as exc:
            await reply(f"Erro ao listar senders: {exc}")
        return

    # --- /mode <@nome_ou_id> <modo> ---
    if content.startswith("/mode"):
        parts = content.split()
        if len(parts) < 3:
            await reply("Uso: /mode <nome_ou_@mention> <auto|approval|always_me>")
            return
        target_raw = parts[1].lstrip("@")
        new_mode = parts[2].lower()
        if new_mode not in ("auto", "approval", "always_me"):
            await reply("Modos válidos: auto, approval, always_me")
            return
        try:
            # Tenta por discord_id primeiro, depois por display_name
            async with conn.execute(
                "SELECT id, display_name FROM senders WHERE discord_id = ? OR display_name LIKE ? LIMIT 1",
                (target_raw, f"%{target_raw}%"),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await reply(f"Sender `{target_raw}` não encontrado.")
                return
            await conn.execute(
                "UPDATE senders SET mode = ?, updated_at = datetime('now') WHERE id = ?",
                (new_mode, row[0]),
            )
            await conn.commit()
            await reply(f"Modo de `{row[1] or target_raw}` atualizado para `{new_mode}`.")
        except Exception as exc:
            await reply(f"Erro ao atualizar modo: {exc}")
        return

    # --- @nome mensagem (encaminhar mensagem para usuário via DM) ---
    if content.startswith("@") and " " in content:
        space_idx = content.index(" ")
        target_raw = content[1:space_idx].strip()
        forward_text = content[space_idx + 1:].strip()
        if not forward_text:
            await reply("Mensagem vazia para encaminhar.")
            return
        try:
            async with conn.execute(
                "SELECT discord_id, display_name FROM senders WHERE discord_id = ? OR display_name LIKE ? LIMIT 1",
                (target_raw, f"%{target_raw}%"),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                await reply(f"Sender `{target_raw}` não encontrado.")
                return
            discord_id = str(row[0])
            display_name = row[1] or target_raw
            resolved = await asyncio.to_thread(resolve_dm_channel_id, discord_id)
            if not resolved.success or not resolved.channel_id:
                await reply(f"Falha ao abrir DM pelo bot para `{display_name}`: {resolved.error}")
                return
            sent = await asyncio.to_thread(send_discord_message, resolved.channel_id, forward_text)
            if not sent.success:
                await reply(f"Falha ao enviar para `{display_name}`: {sent.error}")
                return
            await reply(f"Mensagem encaminhada para `{display_name}` pelo bot oficial.")
        except Exception as exc:
            await reply(f"Erro ao encaminhar mensagem: {exc}")
        return

    # Em servidor/grupo, comando desconhecido não deve gerar painel automático.
    if guild_id is not None:
        return

    # --- Sem comando reconhecido: mostra fila pendente + tarefas do dia (apenas DM) ---
    try:
        lines: list[str] = []

        async with conn.execute(
            """
            SELECT mq.id, s.display_name, mq.original_msg, mq.intent, mq.created_at
            FROM message_queue mq
            JOIN senders s ON s.id = mq.sender_id
            WHERE mq.status = 'pending'
            ORDER BY mq.created_at ASC
            LIMIT 10
            """
        ) as cur:
            pending = await cur.fetchall()

        if pending:
            lines.append(f"**Fila pendente ({len(pending)}):**")
            for row in pending:
                short_msg = (row[2] or "")[:60].replace("\n", " ")
                lines.append(f"• [{row[0]}] `{row[1]}` ({row[3]}): {short_msg}")
        else:
            lines.append("Fila vazia.")

        async with conn.execute(
            """
            SELECT id, sender, description, status, created_at
            FROM tasks
            WHERE date(created_at) = date('now')
            ORDER BY created_at ASC
            """
        ) as cur:
            today_tasks = await cur.fetchall()

        if today_tasks:
            lines.append(f"\n**Tarefas de hoje ({len(today_tasks)}):**")
            for row in today_tasks:
                lines.append(f"• [{row[0]}] `{row[1]}` [{row[3]}]: {row[2][:60]}")

        await reply("\n".join(lines) if lines else "Nada a exibir.")
    except Exception as exc:
        await reply(f"Erro ao carregar painel: {exc}")
