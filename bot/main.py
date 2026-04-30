from __future__ import annotations

import asyncio

import selfcord

from handlers import register_listeners
from official_bot import start_official_bot_listener
from watchdog import run_watchdog
from api.services.db import init_db
from config import get_settings

settings = get_settings()

# Usa API atual do selfcord.py (classe Bot).
client = selfcord.Bot(prefixes=[settings.discord_command_prefix], userbot=True)


def register_background_tasks() -> None:
    async def _on_ready() -> None:
        if getattr(client, "_jeff_watchdog_started", False):
            return
        setattr(client, "_jeff_watchdog_started", True)
        asyncio.create_task(run_watchdog())
        print("[main] watchdog registrado")

    if hasattr(client, "on"):
        client.on("ready")(_on_ready)
    elif hasattr(client, "event"):
        @client.event
        async def on_ready() -> None:
            await _on_ready()


def main() -> None:
    # Garante tabelas existentes mesmo com bot rodando sem API.
    init_db()
    start_official_bot_listener()
    register_listeners(client)
    register_background_tasks()
    client.run(settings.discord_user_token)  # token da conta listener, não bot

if __name__ == "__main__":
    main()
