from __future__ import annotations

from dataclasses import dataclass


INTENTS = {
    "routine_question",
    "error_report",
    "task_request",
    "greeting",
    "unknown",
}


@dataclass
class ClassificationResult:
    intent: str
    confidence_score: float


ERROR_HINTS = (
    "erro",
    "error",
    "exception",
    "stack trace",
    "traceback",
    "bug",
    "falha",
)
TASK_HINTS = (
    "task",
    "tarefa",
    "anota",
    "anotar",
    "cria",
    "criar",
    "abrir card",
)
GREET_HINTS = ("oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "hello")


def classify_intent(message_text: str) -> ClassificationResult:
    """Classificador leve local; em produção deve delegar para LLM."""
    text = message_text.lower()
    if any(h in text for h in ERROR_HINTS):
        return ClassificationResult(intent="error_report", confidence_score=0.8)
    if any(h in text for h in TASK_HINTS):
        return ClassificationResult(intent="task_request", confidence_score=0.78)
    if any(h in text for h in GREET_HINTS):
        return ClassificationResult(intent="greeting", confidence_score=0.9)
    if "?" in text:
        return ClassificationResult(intent="routine_question", confidence_score=0.72)
    return ClassificationResult(intent="unknown", confidence_score=0.5)
