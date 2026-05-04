from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import request, error

from config import get_settings


_PERSONALITY_PROMPTS: dict[str, str] = {
    "jeff_direct": (
        "You are Jeff, a technical assistant. "
        "Tone: direct, concise, practical, low-formality. "
        "Return short answers in pt-BR when user writes in pt-BR."
    ),
    "friendly_mentor": (
        "You are Jeff, a technical assistant. "
        "Tone: friendly and didactic, still concise and practical. "
        "Explain quickly why a step matters when relevant, in pt-BR when user writes in pt-BR."
    ),
    "strict_sre": (
        "You are Jeff, a technical assistant focused on operations reliability. "
        "Tone: objective, risk-aware, and practical. "
        "Prioritize safe diagnostics, explicit assumptions, and one clear next step. "
        "Answer in pt-BR when user writes in pt-BR."
    ),
}

_logged_personality = False


def get_active_personality_name() -> str:
    settings = get_settings()
    configured = str(getattr(settings, "bot_personality", "") or "").strip().lower()
    return configured if configured in _PERSONALITY_PROMPTS else "jeff_direct"


def get_system_prompt() -> str:
    settings = get_settings()
    personality_name = get_active_personality_name()
    base_prompt = _PERSONALITY_PROMPTS[personality_name]
    custom = str(getattr(settings, "bot_personality_custom", "") or "").strip()
    if custom:
        return f"{base_prompt}\nAdditional persona instructions: {custom}"
    return base_prompt


def _log_personality_once() -> None:
    global _logged_personality
    if _logged_personality:
        return
    settings = get_settings()
    personality_name = get_active_personality_name()
    custom_enabled = bool(str(getattr(settings, "bot_personality_custom", "") or "").strip())
    print(
        f"[llm] personalidade ativa: {personality_name}"
        f" (custom={'on' if custom_enabled else 'off'})"
    )
    _logged_personality = True

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


def _request_payload(
    user_message: str,
    history: list[dict[str, str]],
    image_urls: list[str] | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    messages = [{"role": "system", "content": get_system_prompt()}]
    messages.extend(history)

    if image_urls and settings.vision_enabled:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_message}]
        for url in image_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages.append({"role": "user", "content": content})
    else:
        text = user_message
        if image_urls:
            text = f"{user_message}\n[{len(image_urls)} print(s) de erro anexado(s) — visão desabilitada]"
        messages.append({"role": "user", "content": text})

    payload: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.2,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def _fallback_reply(user_message: str) -> LLMReply:
    text = (
        "Vi tua mensagem. Posso responder direto se tu confirmar o contexto em 1 linha."
        if len(user_message) > 20
        else "Recebi. Me manda mais contexto técnico em 1 linha."
    )
    return LLMReply(text=text, confidence_score=0.55, tool_calls=[])


def generate_reply(
    user_message: str,
    history: list[dict[str, str]] | None = None,
    image_urls: list[str] | None = None,
    max_tokens: int | None = None,
) -> LLMReply:
    """Integração DeepSeek em formato compatível OpenAI Chat Completions."""
    settings = get_settings()
    _log_personality_once()
    if not settings.deepseek_api_key:
        return _fallback_reply(user_message)

    payload = _request_payload(user_message, history or [], image_urls, max_tokens)
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


def generate_error_diagnosis(history: list[str], image_urls: list[str] | None = None) -> LLMReply:
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
    if image_urls:
        llm_with_image = generate_reply("\n".join(history[-6:]), image_urls=image_urls)
        if llm_with_image.text:
            return llm_with_image
    return LLMReply(text=reply, confidence_score=0.74, tool_calls=[])
