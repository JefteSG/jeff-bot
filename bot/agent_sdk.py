"""
Módulo de integração com o SDK openai-agents.

Substitui as chamadas diretas à API DeepSeek/OpenAI pelo pattern Agent + Runner,
com gerenciamento automático de histórico via SQLiteSession.

Instalação:
    pip install openai-agents

Uso básico (múltiplas interações mantendo a mesma sessão):

    import asyncio
    from bot.agent_sdk import run_agent_reply

    async def main():
        session_id = "discord_user_123"

        reply1 = await run_agent_reply("Estou tendo erro 502 no nginx", session_id)
        print("Bot:", reply1)

        reply2 = await run_agent_reply("Acontece só de madrugada", session_id)
        print("Bot:", reply2)  # contexto da msg anterior é mantido automaticamente

    asyncio.run(main())

Migração para Redis em produção:
    Implemente a interface `agents.SessionABC` usando redis-py ou aioredis,
    serializando os itens em JSON da mesma forma que SQLiteSession faz.
    Troque `SQLiteSession(session_id, db_path)` por `RedisSession(session_id, redis_client)`.
    Veja a classe SQLiteSession abaixo como referência de implementação.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from agents import (
    Agent,
    ModelSettings,
    OpenAIProvider,
    RunConfig,
    Runner,
    SessionABC,
    SessionSettings,
    enable_verbose_stdout_logging,
    function_tool,
    set_tracing_disabled,
    trace,
)

from config import get_settings


# ---------------------------------------------------------------------------
# Utilitário de path/conexão SQLite
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    # Import local para reaproveitar a lógica centralizada sem introduzir
    # dependência em nível de módulo.
    from api.services.db import _sqlite_path

    return _sqlite_path()


def _db_connection() -> sqlite3.Connection:
    # Reutiliza a factory compartilhada para manter a conexão configurada
    # de forma consistente com o restante da aplicação (incluindo PRAGMAs).
    from api.services.db import get_connection

    return get_connection()


# ---------------------------------------------------------------------------
# SQLiteSession — implementa o protocolo agents.Session usando SQLite local
#
# A tabela `agent_sessions` e seu índice são definidos em db/schema.sql e
# criados no startup via api/services/db.py:init_db().  Não há DDL aqui
# para evitar I/O síncrono dentro do loop de evento.
#
# Para migrar para Redis em produção, implemente a mesma interface
# (session_id, get_items, add_items, pop_item, clear_session) sobre
# um cliente Redis e passe a nova classe no lugar de SQLiteSession.
# ---------------------------------------------------------------------------

class SQLiteSession(SessionABC):
    """Sessão persistida em SQLite.

    Implementa o protocolo `agents.Session` armazenando cada item de conversa
    (mensagens, tool calls e resultados) serializado em JSON, em uma tabela
    dedicada do banco local do bot.

    O schema da tabela `agent_sessions` é gerenciado centralmente em
    `db/schema.sql` e criado no startup por `api/services/db.py:init_db()`.

    Args:
        session_id: Identificador único da sessão (ex: discord_id do usuário).
        db_path: Caminho para o arquivo SQLite. Se omitido, usa o padrão do bot.
        max_items: Número máximo de itens retidos por sessão. Itens mais antigos
            são removidos automaticamente após cada ``add_items``. Padrão: 200
            (≈ 100 turnos de conversa).
    """

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        session_id: str,
        db_path: str | Path | None = None,
        max_items: int = 200,
    ) -> None:
        if max_items < 1:
            raise ValueError(f"max_items must be >= 1, got {max_items}")
        self.session_id = session_id
        self._db_path = str(db_path or _db_path())
        self._max_items = max_items

    # ------------------------------------------------------------------
    # helpers síncronos (executados via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _sync_get(self, limit: int | None) -> list[Any]:
        with self._conn() as conn:
            if limit is not None:
                rows = conn.execute(
                    "SELECT item_json FROM agent_sessions "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (self.session_id, limit),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = conn.execute(
                    "SELECT item_json FROM agent_sessions "
                    "WHERE session_id = ? ORDER BY id",
                    (self.session_id,),
                ).fetchall()
            return [json.loads(r["item_json"]) for r in rows]

    def _sync_add(self, items: list[Any]) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO agent_sessions (session_id, item_json) VALUES (?, ?)",
                [(self.session_id, json.dumps(item)) for item in items],
            )
            # Prune: keep only the most recent max_items rows for this session.
            conn.execute(
                "DELETE FROM agent_sessions WHERE session_id = ? AND id < ("
                "  SELECT MIN(id) FROM ("
                "    SELECT id FROM agent_sessions WHERE session_id = ?"
                "    ORDER BY id DESC LIMIT ?"
                "  )"
                ")",
                (self.session_id, self.session_id, self._max_items),
            )

    def _sync_pop(self) -> Any | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, item_json FROM agent_sessions "
                "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM agent_sessions WHERE id = ?", (row["id"],))
            return json.loads(row["item_json"])

    def _sync_clear(self) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM agent_sessions WHERE session_id = ?",
                (self.session_id,),
            )

    # ------------------------------------------------------------------
    # interface assíncrona exigida pelo SDK
    # ------------------------------------------------------------------

    async def get_items(self, limit: int | None = None) -> list[Any]:
        return await asyncio.to_thread(self._sync_get, limit)

    async def add_items(self, items: list[Any]) -> None:
        await asyncio.to_thread(self._sync_add, items)

    async def pop_item(self) -> Any | None:
        return await asyncio.to_thread(self._sync_pop)

    async def clear_session(self) -> None:
        await asyncio.to_thread(self._sync_clear)


# ---------------------------------------------------------------------------
# Ferramentas (tools) do agente
# ---------------------------------------------------------------------------

@function_tool
def lookup_knowledge_base(query: str) -> str:
    """Busca na base de conhecimento erros conhecidos e suas soluções.

    Use esta ferramenta quando o usuário relatar um erro ou problema técnico
    que possa estar catalogado. Retorna artigos relevantes da base interna.

    Args:
        query: Palavra-chave, mensagem de erro ou nome de serviço para pesquisar.
    """
    db = str(_db_path())
    try:
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT category, keywords, answer
                FROM knowledge_base
                WHERE is_active = 1
                  AND (
                      LOWER(keywords) LIKE LOWER(?)
                      OR LOWER(category) LIKE LOWER(?)
                      OR LOWER(answer)   LIKE LOWER(?)
                  )
                LIMIT 3
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%"),
            ).fetchall()
    except Exception as exc:
        print(f"[agent_sdk] lookup_knowledge_base: erro ao consultar DB: {exc}")
        return "Base de conhecimento indisponível no momento."

    if not rows:
        return f"Nenhum artigo encontrado para: '{query}'. Tente termos mais específicos."

    parts = [f"[{r['category']}] {r['answer']}" for r in rows]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Provider e Agent (criados sob demanda para refletir settings atuais)
# ---------------------------------------------------------------------------

def _build_provider() -> OpenAIProvider:
    """Cria um OpenAIProvider apontando para a API DeepSeek (compatível OpenAI)."""
    settings = get_settings()
    return OpenAIProvider(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        use_responses=False,  # DeepSeek suporta Chat Completions, não Responses API
    )


def _build_agent() -> Agent[None]:
    """Cria o Agent com instruções de sistema e ferramentas registradas."""
    settings = get_settings()
    return Agent(
        name="JeffBot",
        instructions=(
            "Você é o bot de auto-resposta do Jeff no Discord, especialista em erros de servidor. "
            "Diagnostique erros técnicos (HTTP, banco de dados, rede, permissões, logs) "
            "de forma direta, concisa e prática. "
            "Responda em pt-BR quando o usuário escrever em português. "
            "Não use emoji. Não revele que é uma IA. "
            "Nunca peça senha, token ou credencial. "
            "Nunca sugira apagar dados, reiniciar produção ou ações destrutivas. "
            "Dê no máximo um próximo passo seguro por resposta. "
            "Use a ferramenta lookup_knowledge_base antes de responder sobre erros específicos."
        ),
        model=settings.deepseek_model,
        tools=[lookup_knowledge_base],
        model_settings=ModelSettings(temperature=0.2),
    )


# ---------------------------------------------------------------------------
# Função principal — substitui generate_reply() para o fluxo do bot oficial
# ---------------------------------------------------------------------------

async def run_agent_reply(
    user_message: str,
    session_id: str,
    image_urls: list[str] | None = None,
    sender_display_name: str | None = None,
) -> str:
    """Executa o agente e devolve a resposta final em texto.

    O histórico de conversa é gerenciado automaticamente pela SQLiteSession:
    cada chamada acumula o contexto sem precisar recuperar e repassar manualmente.

    Args:
        user_message: Conteúdo bruto da mensagem enviada pelo usuário.
        session_id: Identificador da sessão (ex: discord_id). Mantém o contexto
                    entre chamadas com o mesmo session_id.
        image_urls: URLs de imagens anexadas (usadas se vision_enabled=True).
        sender_display_name: Nome de exibição do remetente. Quando fornecido,
                             é acrescentado como prefixo de contexto à mensagem.

    Returns:
        Texto da resposta do agente.
    """
    settings = get_settings()

    if not settings.deepseek_api_key:
        print("[agent_sdk] DEEPSEEK_API_KEY não configurada; resposta desabilitada")
        return "Serviço temporariamente indisponível"

    # Configura tracing de forma simétrica: sempre define o estado globalmente
    # e habilita verbose logging separadamente quando tracing está ativo.
    set_tracing_disabled(settings.agents_tracing_disabled)
    if not settings.agents_tracing_disabled:
        enable_verbose_stdout_logging()

    # Monta o conteúdo de entrada com identidade do remetente, se disponível
    base_text = user_message
    if sender_display_name:
        base_text = f"[usuário: {sender_display_name}] {user_message}"

    if image_urls and settings.vision_enabled:
        input_content: Any = [{"type": "text", "text": base_text}]
        for url in image_urls:
            input_content.append({"type": "image_url", "image_url": {"url": url}})
    else:
        input_content = base_text
        if image_urls:
            input_content = (
                f"{base_text}\n"
                f"[{len(image_urls)} print(s) de erro anexado(s) — visão desabilitada]"
            )

    session = SQLiteSession(session_id=session_id)
    provider = _build_provider()
    agent = _build_agent()

    run_cfg = RunConfig(
        model_provider=provider,
        workflow_name="jeff-bot-dm",
        group_id=session_id,
    )

    with trace("jeff-bot-dm", group_id=session_id):
        result = await Runner.run(
            starting_agent=agent,
            input=input_content,
            session=session,
            run_config=run_cfg,
        )

    return str(result.final_output or "").strip() or "me manda mais um pouco de contexto"

