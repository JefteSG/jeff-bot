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
    """Envia mensagem para um canal Discord; tenta fallback sem referência se reply falhar.
    Implementa retry com backoff exponencial para 429 (rate limit).
    """
    import time

    max_retries = 2
    for attempt in range(max_retries):
        first_try = _send_once(channel_id=channel_id, content=content, reply_to_message_id=reply_to_message_id)
        if first_try.success:
            return first_try

        # Se foi rate limit, aguarda e tenta novamente
        if "429" in (first_try.error or ""):
            if attempt < max_retries - 1:
                wait_time = 1.5 ** attempt  # 1s, 1.5s
                time.sleep(wait_time)
                continue

        # Se reply_to falhou, tenta sem a referência
        if reply_to_message_id:
            second_try = _send_once(channel_id=channel_id, content=content, reply_to_message_id=None)
            if second_try.success:
                return second_try
            return DiscordSendResult(success=False, message_id=None, error=second_try.error or first_try.error)

        return first_try

    return first_try


def notify_jeff(user_name: str, user_message: str, trigger: str = "", summary: str = "") -> DiscordSendResult:
    """Abre DM do bot com Jeff e envia notificação sobre uma conversa.

    Requer JEFF_DISCORD_ID no .env e que Jeff tenha autorizado o bot.
    Implementa retry com backoff exponencial para 429 (rate limit).
    """
    import time
    settings = get_settings()
    if not settings.jeff_discord_id:
        print("[notify_jeff] JEFF_DISCORD_ID ausente no .env")
        return DiscordSendResult(success=False, message_id=None, error="JEFF_DISCORD_ID ausente")

    print(f"[notify_jeff] abrindo DM com Jeff (id={settings.jeff_discord_id}), trigger={trigger}")
    resolved = resolve_dm_channel_id(settings.jeff_discord_id)
    if not resolved.success or not resolved.channel_id:
        print(f"[notify_jeff] falha ao resolver DM: {resolved.error}")
        return DiscordSendResult(success=False, message_id=None, error=f"falha ao abrir DM com Jeff: {resolved.error}")

    trigger_label = {
        "user_request": "A pessoa pediu pra te chamar",
        "urgency": "Mensagem urgente recebida",
        "low_confidence": "Fiquei na duvida e precisei da sua ajuda",
        "watchdog": "Ninguém respondeu essa pessoa ainda",
        "conversation_closed": "Conversa encerrada por inatividade",
    }.get(trigger, "Notificação")

    lines = [f"[{trigger_label}]", f"De: {user_name}", f"Mensagem: {user_message}"]
    if summary:
        lines.append(f"\nResumo da conversa:\n{summary}")
    lines.append("\nResponda aqui que eu repasso pra ela.")

    # Retry com backoff exponencial para 429 (rate limit)
    max_retries = 3
    for attempt in range(max_retries):
        result = send_discord_message(resolved.channel_id, "\n".join(lines))
        if result.success:
            return result

        # Se foi rate limit, aguarda e tenta novamente
        if "429" in (result.error or ""):
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 1s, 2s, 4s
                print(f"[notify_jeff] rate limited, aguardando {wait_time}s antes de retry (tentativa {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                print(f"[notify_jeff] falha ao enviar após {max_retries} tentativas: {result.error}")
                return result
        else:
            # Erro não-429, não vale a pena retry
            print(f"[notify_jeff] falha ao enviar mensagem: {result.error}")
            return result

    # Nunca deve chegar aqui
    return DiscordSendResult(success=False, message_id=None, error="Erro desconhecido")


def send_via_userbot(channel_id: str, content: str, purpose: str = "message") -> DiscordSendResult:
    """Envia mensagem via userbot.

    Usos permitidos:
    - 'config': link OAuth de autorização
    - 'server_reply': fallback quando o bot oficial não tem acesso ao canal de servidor
    Outros usos são bloqueados por política.
    """
    if purpose not in ("config", "server_reply"):
        return DiscordSendResult(
            success=False,
            message_id=None,
            error="Envio via userbot bloqueado por politica (somente config/server_reply)",
        )

    settings = get_settings()
    if not settings.discord_user_token:
        return DiscordSendResult(success=False, message_id=None, error="DISCORD_USER_TOKEN ausente")

    req = request.Request(
        url=f"https://discord.com/api/v9/channels/{channel_id}/messages",
        method="POST",
        data=json.dumps({"content": content}).encode("utf-8"),
        headers={
            "Authorization": settings.discord_user_token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
