from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import aiosqlite

from api.services.llm import generate_reply


INTENTS = {"routine_question", "error_report", "task_request", "greeting", "unknown"}
VALID_ROLES = {"user", "assistant", "system"}
MAX_DAILY_CONTEXT_MESSAGES = 30
ERROR_HINTS = (
    "erro",
    "error",
    "exception",
    "traceback",
    "stack trace",
    "timeout",
    "falha",
    "bug",
    "502",
    "403",
    "404",
    "500",
)


def _normalize_role(role: str) -> str:
    if role in VALID_ROLES:
        return role
    return "user"


def _normalize_intent(intent: str) -> str:
    if intent in INTENTS:
        return intent
    return "unknown"


def _extract_json_block(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        pass

    if not text:
        return {}

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}

    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def _normalize_keywords(raw_keywords: Any) -> list[str]:
    if isinstance(raw_keywords, list):
        candidates = raw_keywords
    else:
        candidates = re.split(r"[,;\n|]", str(raw_keywords or ""))

    seen: set[str] = set()
    normalized: list[str] = []
    for item in candidates:
        keyword = re.sub(r"\s+", " ", str(item).strip().lower())
        keyword = re.sub(r"[^a-z0-9_ ./:-]", "", keyword)
        if len(keyword) < 2 or keyword in seen:
            continue
        seen.add(keyword)
        normalized.append(keyword)
    return normalized[:8]


def _keyword_score(candidate_keywords: str, target_keywords: list[str]) -> tuple[int, int]:
    candidate_set = set(_normalize_keywords(candidate_keywords))
    target_set = set(target_keywords)
    overlap = len(candidate_set.intersection(target_set))
    return overlap, len(candidate_set)


def _build_transcript(rows: list[aiosqlite.Row]) -> str:
    lines: list[str] = []
    for row in rows:
        role = "Usuário" if str(row["role"]) == "user" else "Bot"
        lines.append(f"{role}: {str(row['message'])}")
    return "\n".join(lines)


def _fallback_closure(rows: list[aiosqlite.Row]) -> dict[str, Any]:
    summary_lines: list[str] = []
    if rows:
        first_message = str(rows[0]["message"] or "")
        last_message = str(rows[-1]["message"] or "")
        if first_message:
            summary_lines.append(f"Início: {first_message[:140]}")
        if last_message and last_message != first_message:
            summary_lines.append(f"Fim: {last_message[:140]}")

    transcript = " ".join(str(row["message"] or "") for row in rows).lower()
    is_error_related = any(str(row["intent"] or "") == "error_report" for row in rows) or any(
        hint in transcript for hint in ERROR_HINTS
    )
    return {
        "summary": "\n".join(summary_lines[:2]) or "Conversa encerrada sem resumo estruturado.",
        "useful": 0,
        "resolved": 0,
        "is_error_related": 1 if is_error_related else 0,
        "keywords": [],
        "solution": "",
    }


async def _next_sequence(sender_id: int, db: aiosqlite.Connection) -> int:
    try:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_seq FROM conversation_context WHERE sender_id = ?",
            (sender_id,),
        )
        row = await cursor.fetchone()
        return int(row["next_seq"]) if row else 1
    except Exception as exc:
        print(f"[memory] erro ao calcular sequence_no: {exc}")
        return 1


async def _delete_old_context(sender_id: int, db: aiosqlite.Connection) -> None:
    try:
        await db.execute(
            "DELETE FROM conversation_context WHERE sender_id = ? AND date(created_at) != date('now')",
            (sender_id,),
        )
    except Exception as exc:
        print(f"[memory] erro ao limpar contexto antigo: {exc}")


async def _trim_daily_context(sender_id: int, db: aiosqlite.Connection) -> None:
    try:
        await db.execute(
            """
            DELETE FROM conversation_context
            WHERE sender_id = ?
              AND date(created_at) = date('now')
              AND id NOT IN (
                  SELECT id
                  FROM conversation_context
                  WHERE sender_id = ?
                    AND date(created_at) = date('now')
                  ORDER BY datetime(created_at) DESC, id DESC
                  LIMIT ?
              )
            """,
            (sender_id, sender_id, MAX_DAILY_CONTEXT_MESSAGES),
        )
    except Exception as exc:
        print(f"[memory] erro ao podar contexto diário: {exc}")


async def _clear_context(sender_id: int, db: aiosqlite.Connection) -> None:
    try:
        await db.execute("DELETE FROM conversation_context WHERE sender_id = ?", (sender_id,))
    except Exception as exc:
        print(f"[memory] erro ao limpar contexto do sender {sender_id}: {exc}")


async def save_message_with_intent(
    sender_id: int,
    role: str,
    content: str,
    db: aiosqlite.Connection,
    intent: str = "unknown",
) -> None:
    try:
        sequence_no = await _next_sequence(sender_id, db)
        await db.execute(
            """
            INSERT INTO conversation_context (sender_id, role, intent, message, sequence_no)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sender_id, _normalize_role(role), _normalize_intent(intent), content, sequence_no),
        )
        await _delete_old_context(sender_id, db)
        await _trim_daily_context(sender_id, db)
        await db.commit()
    except Exception as exc:
        print(f"[memory] erro ao salvar mensagem no contexto: {exc}")
        try:
            await db.rollback()
        except Exception:
            pass


async def get_short_term_context(sender_id: int, db: aiosqlite.Connection) -> list[dict[str, str]]:
    try:
        cursor = await db.execute(
            """
            SELECT role, message
            FROM (
                SELECT id, role, message, created_at
                FROM conversation_context
                WHERE sender_id = ?
                  AND date(created_at) = date('now')
                  AND role IN ('user', 'assistant')
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
            ) recent_context
            ORDER BY datetime(created_at) ASC, id ASC
            """,
            (sender_id, MAX_DAILY_CONTEXT_MESSAGES),
        )
        rows = await cursor.fetchall()
        return [{"role": str(row["role"]), "content": str(row["message"])} for row in rows]
    except Exception as exc:
        print(f"[memory] erro ao buscar contexto curto: {exc}")
        return []


async def save_message_to_context(sender_id: int, role: str, content: str, db: aiosqlite.Connection) -> None:
    try:
        await save_message_with_intent(sender_id, role, content, db)
    except Exception as exc:
        print(f"[memory] erro no wrapper save_message_to_context: {exc}")


async def _generate_closure_payload(rows: list[aiosqlite.Row]) -> dict[str, Any]:
    try:
        prompt = (
            "Resuma a conversa abaixo em 2 ou 3 linhas e responda SOMENTE em JSON com as chaves: "
            "summary, useful, resolved, is_error_related, keywords, solution. "
            "useful e resolved devem ser 0 ou 1. "
            "keywords deve ser array curto. solution deve ficar vazio se não houver solução clara.\n\n"
            f"Conversa:\n{_build_transcript(rows)}"
        )
        reply = await asyncio.to_thread(generate_reply, prompt, [], None, 220)
        payload = _extract_json_block(reply.text)
        if not payload:
            return _fallback_closure(rows)
        return {
            "summary": str(payload.get("summary") or _fallback_closure(rows)["summary"]).strip(),
            "useful": 1 if int(payload.get("useful") or 0) else 0,
            "resolved": 1 if int(payload.get("resolved") or 0) else 0,
            "is_error_related": 1 if int(payload.get("is_error_related") or 0) else 0,
            "keywords": _normalize_keywords(payload.get("keywords") or []),
            "solution": str(payload.get("solution") or "").strip(),
        }
    except Exception as exc:
        print(f"[memory] erro ao gerar fechamento via LLM: {exc}")
        return _fallback_closure(rows)


async def close_conversation(sender_id: int, db: aiosqlite.Connection) -> dict[str, Any] | None:
    try:
        cursor = await db.execute(
            """
            SELECT id, role, intent, message, created_at
            FROM conversation_context
            WHERE sender_id = ?
              AND date(created_at) = date('now')
            ORDER BY datetime(created_at) ASC, id ASC
            """,
            (sender_id,),
        )
        rows = await cursor.fetchall()

        if len(rows) < 2:
            await _clear_context(sender_id, db)
            await db.commit()
            return None

        closure = await _generate_closure_payload(rows)
        turns = len(rows)
        await db.execute(
            """
            INSERT INTO conversation_closures (sender_id, summary, useful, resolved, turns)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sender_id, closure["summary"], closure["useful"], closure["resolved"], turns),
        )

        if closure["resolved"] and closure["is_error_related"] and closure["solution"]:
            user_messages = [str(row["message"] or "") for row in rows if str(row["role"] or "") == "user"]
            error_pattern = user_messages[-1] if user_messages else str(rows[-1]["message"] or "")
            await db.execute(
                """
                INSERT INTO error_solutions (keywords, error_pattern, solution, source)
                VALUES (?, ?, ?, 'ia')
                """,
                (
                    ", ".join(closure["keywords"]),
                    error_pattern,
                    closure["solution"],
                ),
            )

        await _clear_context(sender_id, db)
        await db.commit()
        return closure | {"turns": turns}
    except Exception as exc:
        print(f"[memory] erro ao fechar conversa do sender {sender_id}: {exc}")
        try:
            await db.rollback()
        except Exception:
            pass
        return None


async def find_error_solution(keywords: list[str] | str, db: aiosqlite.Connection) -> dict[str, Any] | None:
    try:
        normalized_keywords = _normalize_keywords(keywords)
        if not normalized_keywords:
            return None

        where_clauses = " OR ".join("keywords LIKE ?" for _ in normalized_keywords)
        params = tuple(f"%{keyword}%" for keyword in normalized_keywords)
        cursor = await db.execute(
            f"""
            SELECT id, keywords, error_pattern, solution, source, success_count, fail_count, created_at
            FROM error_solutions
            WHERE {where_clauses}
            ORDER BY success_count DESC, fail_count ASC, created_at DESC
            """,
            params,
        )
        rows = await cursor.fetchall()
        if not rows:
            return None

        ranked = sorted(
            (dict(row) for row in rows),
            key=lambda item: (
                _keyword_score(str(item.get("keywords") or ""), normalized_keywords)[0],
                int(item.get("success_count") or 0),
                -int(item.get("fail_count") or 0),
            ),
            reverse=True,
        )
        best = ranked[0]
        overlap, _ = _keyword_score(str(best.get("keywords") or ""), normalized_keywords)
        if overlap <= 0:
            return None
        best["match_keywords"] = normalized_keywords
        return best
    except Exception as exc:
        print(f"[memory] erro ao buscar solução aprendida: {exc}")
        return None


async def increment_solution_score(solution_id: int, success: bool, db: aiosqlite.Connection) -> None:
    try:
        if success:
            await db.execute(
                "UPDATE error_solutions SET success_count = success_count + 1 WHERE id = ?",
                (solution_id,),
            )
            await db.commit()
            return

        await db.execute(
            "UPDATE error_solutions SET fail_count = fail_count + 1 WHERE id = ?",
            (solution_id,),
        )
        cursor = await db.execute(
            "SELECT fail_count FROM error_solutions WHERE id = ?",
            (solution_id,),
        )
        row = await cursor.fetchone()
        if row and int(row["fail_count"] or 0) >= 3:
            await db.execute("DELETE FROM error_solutions WHERE id = ?", (solution_id,))
        await db.commit()
    except Exception as exc:
        print(f"[memory] erro ao atualizar score da solução {solution_id}: {exc}")
        try:
            await db.rollback()
        except Exception:
            pass