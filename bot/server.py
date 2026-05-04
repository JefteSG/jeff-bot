"""Roteamento de mensagens em servidores/grupos para o Jeff Bot.

Verifica se o canal é um canal de discussão registrado e, caso seja,
encaminha a mensagem (com menção ao bot removida) para o pipeline normal.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import aiosqlite
from api.services.discord_outbound import send_discord_message, send_via_userbot
from config import get_settings


def _extract_channel_id(message: Any) -> str:
    """Extrai channel_id de forma robusta de mensagens selfcord."""
    # Tentativa 1: atributo direto
    direct = str(getattr(message, "channel_id", "") or "")
    if direct:
        return direct
    # Tentativa 2: via objeto channel
    channel = getattr(message, "channel", None)
    via_channel = str(getattr(channel, "id", "") or "")
    if via_channel:
        return via_channel
    # Tentativa 3: via _data (payload bruto do selfcord)
    data = getattr(message, "_data", None) or {}
    if isinstance(data, dict):
        via_data = str(data.get("channel_id", "") or "")
        if via_data:
            return via_data
    return ""


def _extract_guild_id(message: Any) -> str:
    """Extrai guild_id de forma robusta de mensagens selfcord."""
    direct = getattr(message, "guild_id", None)
    if direct:
        return str(direct)
    guild = getattr(message, "guild", None)
    via_guild = getattr(guild, "id", None)
    if via_guild:
        return str(via_guild)
    channel = getattr(message, "channel", None)
    via_channel_guild = getattr(getattr(channel, "guild", None), "id", None)
    if via_channel_guild:
        return str(via_channel_guild)
    data = getattr(message, "_data", None) or {}
    if isinstance(data, dict):
        via_data = str(data.get("guild_id", "") or "")
        if via_data:
            return via_data
    return ""


async def check_discussion_channel(channel_id: str, conn: aiosqlite.Connection) -> bool:
    """Retorna True se o channel_id está registrado como canal de discussão."""
    try:
        async with conn.execute(
            "SELECT 1 FROM discussion_channels WHERE channel_id = ? LIMIT 1",
            (channel_id,),
        ) as cur:
            row = await cur.fetchone()
        return row is not None
    except Exception as exc:
        print(f"[server] erro ao verificar canal de discussão: {exc}")
        return False


class _ContentPatchedMessage:
    """Wrapper fino que expõe a mesma interface do objeto selfcord Message,
    mas com o conteúdo substituído (menção ao bot removida)."""

    def __init__(self, original: Any, new_content: str) -> None:
        self._original = original
        self._patched_content = new_content

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)

    @property
    def content(self) -> str:
        return self._patched_content


def _strip_mention(content: str, client_id: str) -> str:
    """Remove menção ao bot do início do conteúdo."""
    stripped = re.sub(
        rf"^\s*<@!?{re.escape(client_id)}>\s*", "", content
    ).strip()
    return stripped or content.strip()


async def route_server_message(message: Any, client: Any) -> None:
    """Roteia mensagem de servidor/grupo e tenta responder no próprio canal."""
    # Importação local para evitar importação circular no nível do módulo.
    from router import route_payload_with_bot_reply  # noqa: PLC0415

    settings = get_settings()
    client_user = getattr(client, "user", None)
    selfbot_user_id = str(getattr(client_user, "id", "") or "")
    official_bot_id = str(settings.discord_bot_user_id or "")

    raw_content = str(getattr(message, "content", "") or "")
    mention_target_id = official_bot_id or selfbot_user_id
    clean_content = _strip_mention(raw_content, mention_target_id) if mention_target_id else raw_content.strip()

    patched = _ContentPatchedMessage(message, clean_content)
    author = getattr(patched, "author", None)
    sender_name = ""
    if author is not None:
        sender_name = getattr(author, "global_name", None) or getattr(author, "name", "")

    attachments = getattr(patched, "attachments", None) or []
    image_urls_from_msg = [
        str(getattr(att, "url", "") or "")
        for att in attachments
        if str(getattr(att, "content_type", "") or "").lower().startswith("image/")
        and str(getattr(att, "url", "") or "")
    ]

    payload = {
        "sender_discord_id": str(getattr(author, "id", "")),
        "sender_name": str(sender_name),
        "content": str(getattr(patched, "content", "")),
        "channel_id": _extract_channel_id(message),
        "message_id": str(getattr(patched, "id", "") or ""),
        "image_urls": image_urls_from_msg,
    }
    print(f"[server] payload montado: channel_id={payload['channel_id']!r} sender={payload['sender_discord_id']!r} content={payload['content'][:60]!r}")

    try:
        result = await route_payload_with_bot_reply(payload)
        if result.get("action") == "queued":
            channel_id = str(payload.get("channel_id") or "")
            if channel_id:
                ack_result = await asyncio.to_thread(
                    send_discord_message,
                    channel_id,
                    "Recebi. Estou processando e já te respondo.",
                    str(payload.get("message_id") or "") or None,
                )
                if not ack_result.success and ("403" in (ack_result.error or "") or "50001" in (ack_result.error or "")):
                    await asyncio.to_thread(send_via_userbot, channel_id, "Recebi. Estou processando e já te respondo.", "server_reply")
        print(f"[server] mensagem roteada: {result}")
    except Exception as exc:
        print(f"[server] erro ao rotear mensagem de servidor: {exc}")
