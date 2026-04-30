from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import request, error

from config import get_settings


@dataclass
class NotionCreateResult:
    success: bool
    notion_id: str | None
    error: str | None = None


def create_task_card(description: str, sender: str) -> NotionCreateResult:
    """Cria tarefa no Notion; se credenciais não existirem, retorna fallback local."""
    settings = get_settings()
    if not settings.notion_api_key or not settings.notion_database_id:
        return NotionCreateResult(success=False, notion_id=None, error="Notion não configurado")

    url = "https://api.notion.com/v1/pages"
    payload = {
        "parent": {"database_id": settings.notion_database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": description[:120]}}]},
            "Sender": {"rich_text": [{"text": {"content": sender[:120]}}]},
            "Status": {"select": {"name": "Todo"}},
        },
    }
    req = request.Request(
        url=url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.notion_api_key}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
    )

    try:
        with request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return NotionCreateResult(success=False, notion_id=None, error=str(exc))

    notion_id = data.get("id")
    return NotionCreateResult(success=bool(notion_id), notion_id=notion_id, error=None if notion_id else "Sem ID")
