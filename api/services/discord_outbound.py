from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, request

from config import get_settings


@dataclass
class DiscordSendResult:
    success: bool
    message_id: str | None
    error: str | None = None


@dataclass
class DiscordChannelResolveResult:
    success: bool
    channel_id: str | None
    error: str | None = None


def _auth_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bot {settings.discord_bot_token}",
        "Content-Type": "application/json",
        "User-Agent": "JeffBot (https://localhost, 0.1)",
    }


def _find_existing_dm_channel_id(recipient_discord_id: str) -> DiscordChannelResolveResult:
    req = request.Request(
        url="https://discord.com/api/v9/users/@me/channels",
        method="GET",
        headers={
            "Authorization": f"Bot {get_settings().discord_bot_token}",
            "User-Agent": "JeffBot (https://localhost, 0.1)",
        },
    )

    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return DiscordChannelResolveResult(success=False, channel_id=None, error=f"GET channels HTTP {exc.code}: {detail}")
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return DiscordChannelResolveResult(success=False, channel_id=None, error=f"GET channels failed: {exc}")

    if not isinstance(data, list):
        return DiscordChannelResolveResult(success=False, channel_id=None, error="GET channels retornou formato inválido")

    target = str(recipient_discord_id)
    for channel in data:
        if not isinstance(channel, dict):
            continue
        recipients = channel.get("recipients")
        if not isinstance(recipients, list):
            continue

        for recipient in recipients:
            if not isinstance(recipient, dict):
                continue
            if str(recipient.get("id") or "") != target:
                continue

            channel_id = str(channel.get("id") or "").strip()
            if channel_id:
                return DiscordChannelResolveResult(success=True, channel_id=channel_id, error=None)

    return DiscordChannelResolveResult(success=False, channel_id=None, error="DM existente não encontrada")


def resolve_dm_channel_id(recipient_discord_id: str) -> DiscordChannelResolveResult:
    settings = get_settings()
    if not settings.discord_bot_token:
        return DiscordChannelResolveResult(success=False, channel_id=None, error="DISCORD_BOT_TOKEN ausente")

    # Primeiro tenta encontrar DM já existente (mais estável para alguns tokens/userbot).
    existing = _find_existing_dm_channel_id(recipient_discord_id)
    if existing.success and existing.channel_id:
        return existing

    payload = {"recipient_id": recipient_discord_id}
    req = request.Request(
        url="https://discord.com/api/v9/users/@me/channels",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=_auth_headers(),
    )

    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return DiscordChannelResolveResult(success=False, channel_id=None, error=f"HTTP {exc.code}: {detail}")
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return DiscordChannelResolveResult(success=False, channel_id=None, error=str(exc))

    channel_id = str(data.get("id") or "").strip()
    if not channel_id:
        return DiscordChannelResolveResult(success=False, channel_id=None, error="Sem channel id")

    return DiscordChannelResolveResult(success=True, channel_id=channel_id, error=None)


def _send_once(channel_id: str, content: str, reply_to_message_id: str | None = None) -> DiscordSendResult:
    settings = get_settings()
    if not settings.discord_bot_token:
        return DiscordSendResult(success=False, message_id=None, error="DISCORD_BOT_TOKEN ausente")

    payload: dict[str, object] = {"content": content}
    if reply_to_message_id:
        payload["message_reference"] = {
            "channel_id": channel_id,
            "message_id": reply_to_message_id,
        }

    req = request.Request(
        url=f"https://discord.com/api/v9/channels/{channel_id}/messages",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bot {settings.discord_bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "JeffBot (https://localhost, 0.1)",
        },
    )

    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return DiscordSendResult(success=False, message_id=None, error=f"HTTP {exc.code}: {detail}")
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return DiscordSendResult(success=False, message_id=None, error=str(exc))

    message_id = data.get("id")
    return DiscordSendResult(success=bool(message_id), message_id=message_id, error=None if message_id else "Sem id")


def send_discord_message(channel_id: str, content: str, reply_to_message_id: str | None = None) -> DiscordSendResult:
    """Envia mensagem para um canal Discord; tenta fallback sem referência se reply falhar."""
    first_try = _send_once(channel_id=channel_id, content=content, reply_to_message_id=reply_to_message_id)
    if first_try.success:
        return first_try

    if reply_to_message_id:
        second_try = _send_once(channel_id=channel_id, content=content, reply_to_message_id=None)
        if second_try.success:
            return second_try
        return DiscordSendResult(success=False, message_id=None, error=second_try.error or first_try.error)

    return first_try
