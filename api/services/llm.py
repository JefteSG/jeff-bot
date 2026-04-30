from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import request, error

from config import get_settings


SYSTEM_PROMPT = (
    "You are Jeff, a technical assistant. "
    "Tone: direct, concise, practical, low-formality. "
    "Return short answers in pt-BR when user writes in pt-BR."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_credential",
            "description": "Buscar credencial segura para serviço interno.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_knowledge",
            "description": "Consultar base de conhecimento existente.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Criar uma tarefa quando o usuário solicitar acompanhamento.",
            "parameters": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Fazer pergunta curta para completar contexto técnico.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
]


@dataclass
class LLMReply:
    text: str
    confidence_score: float
    tool_calls: list[dict[str, Any]]


def _request_payload(user_message: str, history: list[dict[str, str]]) -> dict[str, Any]:
    settings = get_settings()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return {
        "model": settings.deepseek_model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.2,
    }


def _fallback_reply(user_message: str) -> LLMReply:
    text = (
        "Vi tua mensagem. Posso responder direto se tu confirmar o contexto em 1 linha."
        if len(user_message) > 20
        else "Recebi. Me manda mais contexto técnico em 1 linha."
    )
    return LLMReply(text=text, confidence_score=0.55, tool_calls=[])


def generate_reply(user_message: str, history: list[dict[str, str]] | None = None) -> LLMReply:
    """Integração DeepSeek em formato compatível OpenAI Chat Completions."""
    settings = get_settings()
    if not settings.deepseek_api_key:
        return _fallback_reply(user_message)

    payload = _request_payload(user_message, history or [])
    url = f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"
    req = request.Request(
        url=url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with request.urlopen(req, timeout=settings.llm_timeout_seconds) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError):
        return _fallback_reply(user_message)

    choices = body.get("choices") or []
    if not choices:
        return _fallback_reply(user_message)

    msg = choices[0].get("message", {})
    content = msg.get("content") or "Sem resposta da LLM."
    tool_calls = msg.get("tool_calls") or []

    # Heurística simples: penaliza confiança quando há pedido de clarificação.
    confidence = 0.78
    if tool_calls:
        confidence = 0.66
    if "não sei" in str(content).lower():
        confidence = 0.5

    return LLMReply(text=str(content).strip(), confidence_score=confidence, tool_calls=tool_calls)


def ask_error_triage_question(history_size: int) -> str:
    questions = [
        "Qual foi a última mudança antes do erro aparecer?",
        "Consegue mandar trecho do log/stack trace principal?",
        "Esse problema acontece em qual ambiente (dev, staging ou prod)?",
    ]
    idx = min(history_size, len(questions) - 1)
    return questions[idx]


def generate_error_diagnosis(history: list[str]) -> LLMReply:
    joined = "\n".join(history[-6:])
    base = "Hipótese inicial: regressão após alteração recente ou configuração de ambiente."
    if "timeout" in joined.lower():
        base = "Hipótese inicial: gargalo de rede/serviço dependente causando timeout."
    elif "permission" in joined.lower() or "forbidden" in joined.lower():
        base = "Hipótese inicial: credencial/permissão inválida no ambiente atual."

    reply = (
        f"{base} Próximo passo: validar logs do serviço dependente e confirmar credenciais ativas. "
        "Se quiser, eu já monto checklist de correção."
    )
    return LLMReply(text=reply, confidence_score=0.74, tool_calls=[])
