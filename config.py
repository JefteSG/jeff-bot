from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import base64
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()


def _decode_discord_token_id(token: str) -> str:
    raw = str(token or "").strip().strip('"').strip("'")
    if not raw or "." not in raw:
        return ""

    head = raw.split(".", 1)[0]
    padding = "=" * (-len(head) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{head}{padding}").decode("utf-8")
    except Exception:
        return ""
    return decoded if decoded.isdigit() else ""


@dataclass(frozen=True)
class Settings:
    """Centraliza variáveis de ambiente para bot e API."""

    app_env: str = os.getenv("APP_ENV", "development")
    sqlite_path: str = os.getenv("SQLITE_PATH", "db/jeff_bot.db")

    discord_user_token: str = os.getenv("DISCORD_USER_TOKEN", os.getenv("DISCORD_TOKEN", ""))
    discord_bot_token: str = os.getenv("DISCORD_BOT_TOKEN", "")
    discord_bot_user_id: str = os.getenv("DISCORD_BOT_USER_ID", _decode_discord_token_id(os.getenv("DISCORD_BOT_TOKEN", "")))
    discord_command_prefix: str = os.getenv("DISCORD_COMMAND_PREFIX", "!")
    selfcord_dm_only: bool = os.getenv("SELFCORD_DM_ONLY", "true").lower() in {"1", "true", "yes", "on"}
    jeff_discord_id: str = os.getenv("JEFF_DISCORD_ID", "")

    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    bot_personality: str = os.getenv("BOT_PERSONALITY", "jeff_direct")
    bot_personality_custom: str = os.getenv("BOT_PERSONALITY_CUSTOM", "")

    notion_api_key: str = os.getenv("NOTION_API_KEY", "")
    notion_database_id: str = os.getenv("NOTION_DATABASE_ID", "")

    sender_default_threshold: float = float(os.getenv("SENDER_DEFAULT_THRESHOLD", "0.7"))
    llm_timeout_seconds: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
    auto_reply_delay_minutes: int = int(os.getenv("AUTO_REPLY_DELAY_MINUTES", "5"))
    watchdog_poll_seconds: int = int(os.getenv("WATCHDOG_POLL_SECONDS", "30"))
    auto_reply_enabled: bool = os.getenv("AUTO_REPLY_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    inactivity_close_minutes: int = int(os.getenv("INACTIVITY_CLOSE_MINUTES", "30"))
    # Habilita envio de imagens para o LLM (requer modelo com suporte a visão, ex: gpt-4o)
    vision_enabled: bool = os.getenv("VISION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    # Usa o SDK openai-agents (Agent + Runner + SQLiteSession) no bot oficial
    use_agents_sdk: bool = os.getenv("USE_AGENTS_SDK", "false").lower() in {"1", "true", "yes", "on"}
    # Desativa envio de traces para a API da OpenAI (recomendado ao usar DeepSeek)
    agents_tracing_disabled: bool = os.getenv("AGENTS_TRACING_DISABLED", "true").lower() in {"1", "true", "yes", "on"}
    print(f"Settings loaded: app_env={app_env}, sqlite_path={sqlite_path}, "
          f"discord_user_token={'***' if discord_user_token else '(none)'}, "
          f"discord_bot_token={'***' if discord_bot_token else '(none)'}, "
            f"discord_bot_user_id={discord_bot_user_id or '(none)'}, "
          f"discord_command_prefix={discord_command_prefix}, "
          f"deepseek_api_key={'***' if deepseek_api_key else '(none)'}, "
          f"deepseek_base_url={deepseek_base_url}, deepseek_model={deepseek_model}, "
          f"bot_personality={bot_personality}, "
          f"bot_personality_custom={'(set)' if bot_personality_custom else '(none)'}, "
          f"notion_api_key={'***' if notion_api_key else '(none)'}, notion_database_id={notion_database_id}, "
          f"sender_default_threshold={sender_default_threshold}, llm_timeout_seconds={llm_timeout_seconds}, "
          f"auto_reply_delay_minutes={auto_reply_delay_minutes}, watchdog_poll_seconds={watchdog_poll_seconds}, "
          f"auto_reply_enabled={auto_reply_enabled}"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
