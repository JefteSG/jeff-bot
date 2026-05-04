from __future__ import annotations

from typing import Any

from selfcord.models.channels import DMChannel, GroupChannel

from admin import handle_admin_message, is_admin
from config import get_settings
from router import _connect, route_message
from server import check_discussion_channel, route_server_message


async def handle_incoming_message(message: Any, client: Any) -> None:
    """Adapter fino para delegar roteamento de DM ao router principal."""
    await route_message(message, client)


def _normalized_admin_content(raw_content: str) -> str:
    content = str(raw_content or "").strip()
    if "/" in content and not content.startswith("/"):
        first_slash = content.find("/")
        prefix = content[:first_slash].strip()
        if prefix.startswith("@") or prefix.startswith("<@"):
            return content[first_slash:].strip()
    return content


def _is_admin_command_message(message: Any) -> bool:
    """Retorna True quando a mensagem contém um comando admin explícito."""
    content = _normalized_admin_content(str(getattr(message, "content", "") or ""))
    if not content:
        return False

    admin_commands = (
        "/add-channel",
        "/remove-channel",
        "/channels",
        "/senders",
        "/mode",
    )
    return any(content.startswith(cmd) for cmd in admin_commands)


def _is_dm_or_mention(message: Any, client: Any) -> bool:
    """Permite apenas DMs ou mensagens onde o usuário foi mencionado."""
    channel = getattr(message, "channel", None)
    guild_id = (
        getattr(message, "guild_id", None)
        or getattr(getattr(message, "guild", None), "id", None)
        or getattr(getattr(channel, "guild", None), "id", None)
        or (getattr(message, "_data", None) or {}).get("guild_id")
    )

    # Só é DM quando realmente não há guild e o canal é de DM/Group.
    if not guild_id and isinstance(channel, (DMChannel, GroupChannel)):
        return True

    if isinstance(channel, (DMChannel, GroupChannel)):
        return True

    settings = get_settings()
    client_user = getattr(client, "user", None)
    selfbot_user_id = getattr(client_user, "id", None)
    official_bot_id = settings.discord_bot_user_id
    target_ids = {str(value) for value in (official_bot_id, selfbot_user_id) if value}
    if not target_ids:
        return False

    mentions = getattr(message, "mentions", None) or []
    for mentioned_user in mentions:
        # selfcord pode entregar menções como objeto User ou dict cru do payload.
        mention_id = getattr(mentioned_user, "id", None)
        if mention_id is None and isinstance(mentioned_user, dict):
            mention_id = mentioned_user.get("id")

        if str(mention_id) in target_ids:
            return True

    # Fallback por conteúdo para casos em que payload de menções vem vazio/inconsistente.
    content = str(getattr(message, "content", "") or "")
    for target_id in target_ids:
        if f"<@{target_id}>" in content or f"<@!{target_id}>" in content:
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
        msg_author = getattr(message, "author", None)
        author_id = getattr(msg_author, "id", None)

        # Debug: confirma que o listener está ativo
        _ch = (
            str(getattr(message, "channel_id", "") or "")
            or str(getattr(getattr(message, "channel", None), "id", "") or "")
        )
        _gid = (
            str(getattr(message, "guild_id", "") or "")
            or str(getattr(getattr(message, "guild", None), "id", "") or "")
        )
        _content_preview = str(getattr(message, "content", "") or "")[:60]
        print(f"[listener] _on_message: author={author_id} channel={_ch!r} guild={_gid!r} content={_content_preview!r}")
        if not getattr(message, "content", None):
            return

        # Evita loop: não processa mensagens enviadas por contas marcadas como bot.
        if bool(getattr(msg_author, "bot", False)):
            return

        # Extrai contexto de canal/guild cedo para permitir bloqueio DM-only.
        channel = getattr(message, "channel", None)
        guild_id = (
            getattr(message, "guild_id", None)
            or getattr(getattr(message, "guild", None), "id", None)
            or getattr(getattr(channel, "guild", None), "id", None)
            or (getattr(message, "_data", None) or {}).get("guild_id")
        )
        is_actual_dm = (not guild_id or isinstance(channel, (DMChannel, GroupChannel)))

        settings = get_settings()
        if settings.selfcord_dm_only and not is_actual_dm:
            # Em modo DM-only, ainda permite exceções para:
            # 1) comandos admin explícitos
            # 2) menção ao bot oficial/selfbot
            if not (_is_admin_command_message(message) or _is_dm_or_mention(message, client)):
                return

        # --- Admin: processa apenas comandos admin explícitos (DM ou servidor) ---
        is_admin_user = is_admin(author_id)
        if is_admin_user and _is_admin_command_message(message):
            print(f"[listener] mensagem do admin recebida (canal={getattr(message, 'channel_id', 'unknown')})")
            conn = await _connect()
            try:
                await handle_admin_message(message, client, conn)
            except Exception as exc:
                print(f"[listener] erro no handler admin: {exc}")
            finally:
                await conn.close()
            return

        # Mensagens próprias não-admin seguem para registro de resposta manual.
        # Para admin, deixamos seguir para o fluxo de servidor/discussão quando não for comando.
        client_user = getattr(client, "user", None)
        if (
            client_user
            and msg_author
            and str(getattr(client_user, "id", "")) == str(author_id)
            and not is_admin_user
        ):
            await route_message(message, client)
            return

        # --- Determina se é DM real ou mensagem em servidor ---

        if is_actual_dm:
            # Admin em DM recebe respostas automáticas; usuários comuns apenas enfileiram
            if is_admin_user:
                print(f"[listener] DM do admin recebida de {author_id}, respondendo automaticamente")
                from server import route_server_message
                await route_server_message(message, client)
            else:
                print(f"[listener] DM recebida de {author_id}")
                await handle_incoming_message(message, client)
            return

        # --- Canal de servidor: responde se mencionado OU canal de discussão ---
        channel_id = (
            str(getattr(message, "channel_id", "") or "")
            or str(getattr(channel, "id", "") or "")
            or str((getattr(message, "_data", None) or {}).get("channel_id", "") or "")
        )
        is_mentioned = _is_dm_or_mention(message, client)  # True se bot foi mencionado

        in_discussion = False
        if channel_id:
            conn = await _connect()
            try:
                in_discussion = await check_discussion_channel(channel_id, conn)
            finally:
                await conn.close()

        if in_discussion or is_mentioned:
            print(f"[listener] mensagem em servidor de {author_id} (discussion={in_discussion}, mention={is_mentioned}, canal={channel_id})")
            await route_server_message(message, client)
            return

        # Canal não registrado e sem menção — ignora

    # selfcord.Bot usa .on("message"); mantemos fallback .event para compatibilidade.
    if hasattr(client, "on"):
        client.on("message")(_on_message)
    elif hasattr(client, "event"):
        @client.event
        async def on_message(message: Any) -> None:
            await _on_message(message)
