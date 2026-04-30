from __future__ import annotations

from typing import Any

from selfcord.models.channels import DMChannel, GroupChannel

from router import route_message


async def handle_incoming_message(message: Any, client: Any) -> None:
    """Adapter fino para delegar roteamento de DM ao router principal."""
    await route_message(message, client)


def _is_dm_or_mention(message: Any, client: Any) -> bool:
    """Permite apenas DMs ou mensagens onde o usuário foi mencionado."""
    # Critério de DM/privado robusto para variações de payload da selfcord.
    if getattr(message, "guild_id", None) is None:
        return True

    channel = getattr(message, "channel", None)
    if isinstance(channel, (DMChannel, GroupChannel)):
        return True

    client_user = getattr(client, "user", None)
    client_id = getattr(client_user, "id", None)
    if not client_id:
        return False

    mentions = getattr(message, "mentions", None) or []
    for mentioned_user in mentions:
        # selfcord pode entregar menções como objeto User ou dict cru do payload.
        mention_id = getattr(mentioned_user, "id", None)
        if mention_id is None and isinstance(mentioned_user, dict):
            mention_id = mentioned_user.get("id")

        if str(mention_id) == str(client_id):
            return True

    # Fallback por conteúdo para casos em que payload de menções vem vazio/inconsistente.
    content = str(getattr(message, "content", "") or "")
    if f"<@{client_id}>" in content or f"<@!{client_id}>" in content:
        return True

    return False


def _reject_reason(message: Any, client: Any) -> str:
    if not getattr(message, "content", None):
        return "sem conteúdo"

    client_user = getattr(client, "user", None)
    msg_author = getattr(message, "author", None)
    if client_user and msg_author and getattr(client_user, "id", None) == getattr(msg_author, "id", None):
        return ""

    if not _is_dm_or_mention(message, client):
        guild_id = getattr(message, "guild_id", None)
        channel_id = getattr(message, "channel_id", None)
        return f"não é DM nem menção (guild_id={guild_id}, channel_id={channel_id})"

    return ""


def register_listeners(client: Any) -> None:
    """Registra listeners de mensagem no client selfcord."""

    async def _on_message(message: Any) -> None:
        reason = _reject_reason(message, client)
        if reason:
            return

        msg_author = getattr(message, "author", None)
        print(f"[listener] mensagem recebida de {getattr(msg_author, 'id', 'unknown')}")
        await handle_incoming_message(message, client)

    # selfcord.Bot usa .on("message"); mantemos fallback .event para compatibilidade.
    if hasattr(client, "on"):
        client.on("message")(_on_message)
    elif hasattr(client, "event"):
        @client.event
        async def on_message(message: Any) -> None:
            await _on_message(message)
