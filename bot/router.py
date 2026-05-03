from __future__ import annotations

import asyncio
import json
from pathlib import Path
from collections import deque
from typing import Any, Awaitable, Callable, TYPE_CHECKING

import aiosqlite

from api.services.discord_outbound import notify_jeff, send_via_userbot
from api.services.llm import ask_error_triage_question, generate_error_diagnosis, generate_reply
from api.services.notion import create_task_card
from config import get_settings

if TYPE_CHECKING:
    from selfcord import Client, Message


INTENTS = {"routine_question", "error_report", "task_request", "greeting", "unknown"}
CONTEXT_WINDOW_MINUTES = 3

WANTS_JEFF_HINTS = (
    "avisa o jeff",
    "avisa jeff",
    "chama o jeff",
    "chama jeff",
    "fala com o jeff",
    "fala pro jeff",
    "preciso do jeff",
    "quero falar com o jeff",
    "pode chamar o jeff",
    "só o jeff",
    "so o jeff",
    "jeff que sabe",
    "jeff precisa ver",
    "deixa eu falar com o jeff",
    "quero o jeff",
)

URGENCY_HINTS = (
    "urgente",
    "é urgente",
    "é urgencia",
    "urgência",
    "urgencia",
    "emergência",
    "emergencia",
    "crítico",
    "critico",
    "fora do ar",
    "caindo",
    "caiu",
    "travado",
    "travou",
    "quebrado",
    "quebrou",
    "não funciona",
    "nao funciona",
    "parou de funcionar",
    "perdendo dinheiro",
    "cliente reclamando",
    "produção caiu",
    "producao caiu",
    "site fora",
    "sistema fora",
    "preciso agora",
    "precisa agora",
    "não pode esperar",
    "nao pode esperar",
    "o mais rapido",
    "o mais rápido",
    "socorro",
    "agora",
    "asap",
    "rapidão",
    "rapidao",
    "já",
    "ja",
)


def _is_wants_jeff(text: str) -> bool:
    lower = text.lower()
    normalized = " " + lower.replace("\n", " ").strip() + " "
    for hint in WANTS_JEFF_HINTS:
        if hint in normalized:
            return True
    return False


def _is_urgent(text: str) -> bool:
    lower = text.lower()
    # Tira espaços extras e normaliza
    normalized = " " + lower.replace("\n", " ").strip() + " "
    for hint in URGENCY_HINTS:
        if hint in normalized:
            return True
    return False


def _sqlite_path() -> Path:
    settings = get_settings()
    root = Path(__file__).resolve().parents[1]
    db_path = Path(settings.sqlite_path)
    if not db_path.is_absolute():
        db_path = root / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(_sqlite_path())
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON;")
    return conn


async def _fetchone(conn: aiosqlite.Connection, query: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
    cursor = await conn.execute(query, params)
    return await cursor.fetchone()


async def _ensure_sender(conn: aiosqlite.Connection, discord_id: str, display_name: str) -> dict[str, Any]:
    row = await _fetchone(conn, "SELECT * FROM senders WHERE discord_id = ?", (discord_id,))
    if row:
        # Atualiza display_name se mudou e não está vazio
        current_name = str(row["display_name"] or "")
        if display_name and display_name != current_name:
            await conn.execute(
                "UPDATE senders SET display_name = ?, updated_at = datetime('now') WHERE discord_id = ?",
                (display_name, discord_id),
            )
            await conn.commit()
            row = await _fetchone(conn, "SELECT * FROM senders WHERE discord_id = ?", (discord_id,))
        return dict(row)

    await conn.execute(
        """
        INSERT INTO senders (discord_id, display_name, mode, trust_score, confidence_threshold)
        VALUES (?, ?, 'approval', 0.5, ?)
        """,
        (discord_id, display_name, get_settings().sender_default_threshold),
    )
    await conn.commit()
    created = await _fetchone(conn, "SELECT * FROM senders WHERE discord_id = ?", (discord_id,))
    return dict(created) if created else {}


async def _next_sequence(conn: aiosqlite.Connection, sender_id: int) -> int:
    row = await _fetchone(
        conn,
        "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_seq FROM conversation_context WHERE sender_id = ?",
        (sender_id,),
    )
    return int(row["next_seq"]) if row else 1


async def _append_context(
    conn: aiosqlite.Connection,
    sender_id: int,
    role: str,
    intent: str,
    message: str,
) -> None:
    # Salva contexto incremental para triagem e histórico de conversa.
    sequence_no = await _next_sequence(conn, sender_id)
    await conn.execute(
        """
        INSERT INTO conversation_context (sender_id, role, intent, message, sequence_no)
        VALUES (?, ?, ?, ?, ?)
        """,
        (sender_id, role, intent if intent in INTENTS else "unknown", message, sequence_no),
    )
    await conn.commit()


async def _sender_history(conn: aiosqlite.Connection, sender_id: int, limit: int = 8) -> list[dict[str, str]]:
    rows = await conn.execute_fetchall(
        """
        SELECT role, message
        FROM conversation_context
        WHERE sender_id = ?
          AND created_at >= datetime('now', ?)
        ORDER BY sequence_no ASC
        LIMIT ?
        """,
        (sender_id, f"-{CONTEXT_WINDOW_MINUTES} minutes", limit),
    )
    history = [{"role": str(row["role"]), "content": str(row["message"])} for row in rows]
    return history


async def _find_recent_pre_ai_queue(
    conn: aiosqlite.Connection,
    sender_id: int,
    channel_id: str,
) -> dict[str, Any] | None:
    rows = await conn.execute_fetchall(
        """
        SELECT id, original_msg, meta_json
        FROM message_queue
        WHERE sender_id = ?
          AND status = 'pending'
          AND created_at >= datetime('now', ?)
        ORDER BY created_at DESC
        """,
        (sender_id, f"-{CONTEXT_WINDOW_MINUTES} minutes"),
    )

    for row in rows:
        raw_meta = str(row["meta_json"] or "")
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except json.JSONDecodeError:
            meta = {}

        same_channel = not channel_id or not str(meta.get("channel_id") or "") or str(meta.get("channel_id")) == channel_id
        if bool(meta.get("pre_ai")) and not bool(meta.get("approved_for_ai")) and same_channel:
            return {
                "id": int(row["id"]),
                "original_msg": str(row["original_msg"] or ""),
                "meta": meta,
            }

    return None


async def _append_grouped_pending_message(
    conn: aiosqlite.Connection,
    queue_id: int,
    existing_text: str,
    incoming_text: str,
    meta: dict[str, Any],
    channel_id: str,
    message_id: str,
) -> None:
    grouped_text = f"{existing_text}\n{incoming_text}" if existing_text else incoming_text

    if channel_id and not str(meta.get("channel_id") or ""):
        meta["channel_id"] = channel_id
    if message_id:
        # Mantem referência para a mensagem mais recente quando houver agrupamento.
        meta["message_id"] = message_id

    meta["grouped_count"] = int(meta.get("grouped_count") or 1) + 1

    await conn.execute(
        """
        UPDATE message_queue
        SET original_msg = ?,
            suggested_reply = '',
            confidence_score = 0.0,
            meta_json = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (grouped_text, json.dumps(meta), queue_id),
    )
    await conn.commit()


def _keyword_overlap_score(message_text: str, keywords_csv: str) -> float:
    msg_tokens = {token.strip(".,:;!?()[]{}\"'\n\t").lower() for token in message_text.split() if token.strip()}
    key_tokens = {token.strip().lower() for token in keywords_csv.split(",") if token.strip()}
    if not msg_tokens or not key_tokens:
        return 0.0
    return len(msg_tokens.intersection(key_tokens)) / max(len(key_tokens), 1)


async def _search_knowledge(conn: aiosqlite.Connection, message_text: str) -> dict[str, Any] | None:
    rows = await conn.execute_fetchall("SELECT * FROM knowledge_base WHERE is_active = 1")
    best: tuple[float, dict[str, Any]] | None = None
    for row in rows:
        candidate = dict(row)
        score = _keyword_overlap_score(message_text, str(candidate.get("keywords") or ""))
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, candidate)

    if not best:
        return None
    chosen = best[1]
    chosen["match_score"] = best[0]
    return chosen


async def _enqueue_pending(
    conn: aiosqlite.Connection,
    sender_id: int,
    intent: str,
    original_msg: str,
    suggested_reply: str,
    confidence_score: float,
    meta: dict[str, Any] | None = None,
) -> int:
    print(f"[enqueue_pending] sender_id={sender_id}, intent={intent}, original_msg={original_msg}, suggested_reply={suggested_reply}, confidence_score={confidence_score}, meta={meta}")
    try:
        cursor = await conn.execute(
            """
            INSERT INTO message_queue (sender_id, status, intent, original_msg, suggested_reply, confidence_score, meta_json)
            VALUES (?, 'pending', ?, ?, ?, ?, ?)
            """,
            (sender_id, intent, original_msg, suggested_reply, confidence_score, json.dumps(meta or {})),
        )
        await conn.commit()
        print(f"[enqueue_pending] Inserido com sucesso, queue_id={cursor.lastrowid}")
        return int(cursor.lastrowid)
    except Exception as exc:
        print(f"[enqueue_pending][ERRO] Falha ao inserir: {exc}")
        raise


async def _upsert_conversation_watch(
    conn: aiosqlite.Connection,
    sender_id: int,
    content: str,
    channel_id: str,
    message_id: str,
    meta: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO conversation_watch (
            sender_id,
            channel_id,
            status,
            last_incoming_message,
            last_incoming_message_id,
            last_incoming_at,
            needs_human_reason,
            meta_json
        )
        VALUES (?, ?, 'watching', ?, ?, datetime('now'), NULL, ?)
        ON CONFLICT(sender_id, channel_id) DO UPDATE SET
            status = 'watching',
            last_incoming_message = excluded.last_incoming_message,
            last_incoming_message_id = excluded.last_incoming_message_id,
            last_incoming_at = datetime('now'),
            needs_human_reason = NULL,
            meta_json = excluded.meta_json,
            updated_at = datetime('now')
        """,
        (sender_id, channel_id, content, message_id, json.dumps(meta or {})),
    )
    await conn.commit()


async def _mark_jeff_reply_for_channel(
    conn: aiosqlite.Connection,
    channel_id: str,
    content: str,
    message_id: str,
) -> int:
    cursor = await conn.execute(
        """
        UPDATE conversation_watch
        SET status = 'resolved',
            last_jeff_reply_message = ?,
            last_jeff_reply_message_id = ?,
            last_jeff_reply_at = datetime('now'),
            needs_human_reason = NULL,
            updated_at = datetime('now')
        WHERE channel_id = ?
          AND status IN ('watching', 'needs_human')
          AND (
              last_jeff_reply_at IS NULL
              OR last_jeff_reply_at < last_incoming_at
          )
        """,
        (content, message_id, channel_id),
    )
    pending_rows = await conn.execute_fetchall(
        """
        SELECT id, meta_json
        FROM message_queue
        WHERE status = 'pending'
        ORDER BY id DESC
        LIMIT 100
        """
    )
    resolved_queue_ids: list[int] = []
    for row in pending_rows:
        raw_meta = str(row["meta_json"] or "")
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except json.JSONDecodeError:
            continue
        if str(meta.get("channel_id") or "") == channel_id and bool(meta.get("watchdog")):
            resolved_queue_ids.append(int(row["id"]))

    if resolved_queue_ids:
        placeholders = ",".join("?" for _ in resolved_queue_ids)
        await conn.execute(
            f"""
            UPDATE message_queue
            SET status = 'self_replied',
                final_reply = ?,
                updated_at = datetime('now')
            WHERE id IN ({placeholders})
            """,
            (content, *resolved_queue_ids),
        )
    await conn.commit()
    return int(cursor.rowcount or 0)


def _normalize_intent(raw_text: str) -> str:
    normalized = raw_text.strip().lower().replace("`", "")
    for token in normalized.replace("\n", " ").split():
        candidate = token.strip(".,:;!?()[]{}\"'")
        if candidate in INTENTS:
            return candidate
    return "unknown"


async def _classify_intent_via_llm(content: str, history: list[dict[str, str]]) -> tuple[str, float]:
    prompt = (
        "Classifique a intenção desta mensagem em apenas UMA label: "
        "routine_question, error_report, task_request, greeting, unknown. "
        "Responda só com a label.\n\n"
        f"Mensagem: {content}"
    )
    llm_reply = await asyncio.to_thread(generate_reply, prompt, history)
    intent = _normalize_intent(llm_reply.text)
    return intent, float(llm_reply.confidence_score)


async def _llm_triage_question(content: str, history: list[dict[str, str]], asked_count: int) -> str:
    prompt = (
        "Você está em triagem de erro. Faça apenas UMA pergunta curta e objetiva para avançar diagnóstico. "
        f"Esta é a pergunta número {asked_count + 1} de no máximo 2 perguntas antes de diagnóstico.\n"
        f"Mensagem atual: {content}"
    )
    llm_reply = await asyncio.to_thread(generate_reply, prompt, history)
    text = llm_reply.text.strip()
    if text:
        return text
    return ask_error_triage_question(asked_count)


async def _llm_extract_task_description(content: str, history: list[dict[str, str]]) -> str:
    prompt = (
        "Extraia a descrição da tarefa em UMA frase curta e acionável. "
        "Responda só com a descrição, sem prefixos.\n"
        f"Mensagem: {content}"
    )
    llm_reply = await asyncio.to_thread(generate_reply, prompt, history)
    return llm_reply.text.strip() or content


async def _safe_reply(send_reply: Callable[[str], Awaitable[None]] | None, text: str) -> None:
    if not send_reply:
        return
    try:
        await send_reply(text)
    except Exception:
        # Falha de envio não deve derrubar o roteador.
        return


async def _route_core(
    payload: dict[str, str],
    send_reply: Callable[[str], Awaitable[None]] | None,
) -> dict[str, Any]:
    sender_discord_id = str(payload.get("sender_discord_id") or "")
    sender_name = str(payload.get("sender_name") or "")
    content = str(payload.get("content") or "").strip()
    image_urls: list[str] = [str(u) for u in (payload.get("image_urls") or []) if u]

    if not sender_discord_id or not content:
        return {"action": "ignored", "intent": "unknown"}

    conn = await _connect()
    try:
        sender = await _ensure_sender(conn, sender_discord_id, sender_name)
        sender_id = int(sender["id"])
        sender_mode = str(sender["mode"])
        sender_threshold = float(sender["confidence_threshold"])

        if sender_mode == "always_me":
            await _enqueue_pending(
                conn,
                sender_id=sender_id,
                intent="unknown",
                original_msg=content,
                suggested_reply="",
                confidence_score=0.0,
                meta={"reason": "always_me"},
            )
            return {"action": "queued", "intent": "unknown"}

        history = await _sender_history(conn, sender_id)
        intent, intent_confidence = await _classify_intent_via_llm(content, history)

        await _append_context(conn, sender_id, role="user", intent=intent, message=content)

        if intent == "greeting":
            greeting = "fala! manda o contexto que eu te ajudo rapidinho."
            await _safe_reply(send_reply, greeting)
            await _append_context(conn, sender_id, role="assistant", intent=intent, message=greeting)
            return {"action": "replied", "intent": intent}

        if intent == "routine_question":
            try:
                kb_item = await _search_knowledge(conn, content)
                if kb_item:
                    required = max(float(kb_item["confidence_threshold"]), sender_threshold)
                    if float(kb_item["match_score"]) >= required:
                        answer = str(kb_item["answer"])
                        await _safe_reply(send_reply, answer)
                        await _append_context(conn, sender_id, role="assistant", intent=intent, message=answer)
                        return {"action": "replied", "intent": intent, "source": "knowledge_base"}

                llm_reply = await asyncio.to_thread(generate_reply, content, history, image_urls or None)
                meta: dict[str, Any] = {"source": "llm_suggestion"}
                if image_urls:
                    meta["image_urls"] = image_urls
                await _enqueue_pending(
                    conn,
                    sender_id=sender_id,
                    intent=intent,
                    original_msg=content,
                    suggested_reply=llm_reply.text,
                    confidence_score=llm_reply.confidence_score,
                    meta=meta,
                )
                return {"action": "queued", "intent": intent}
            except Exception as exc:
                await _enqueue_pending(
                    conn,
                    sender_id=sender_id,
                    intent=intent,
                    original_msg=content,
                    suggested_reply="",
                    confidence_score=0.0,
                    meta={"error": str(exc), "stage": "routine_question"},
                )
                return {"action": "queued", "intent": intent, "error": str(exc)}

        if intent == "error_report":
            try:
                asked_row = await _fetchone(
                    conn,
                    """
                    SELECT COUNT(*) AS asked_count
                    FROM conversation_context
                    WHERE sender_id = ? AND intent = 'error_report' AND role = 'assistant'
                    """,
                    (sender_id,),
                )
                asked_count = int(asked_row["asked_count"]) if asked_row else 0

                error_rows = await conn.execute_fetchall(
                    """
                    SELECT message
                    FROM conversation_context
                    WHERE sender_id = ? AND intent = 'error_report'
                    ORDER BY sequence_no ASC
                    LIMIT 20
                    """,
                    (sender_id,),
                )
                error_history = [str(row["message"]) for row in error_rows]

                if asked_count < 2:
                    question = await _llm_triage_question(content, history, asked_count)
                    await _safe_reply(send_reply, question)
                    await _append_context(conn, sender_id, role="assistant", intent=intent, message=question)
                    return {"action": "replied", "intent": intent, "phase": "triage"}

                diagnosis_reply = await asyncio.to_thread(
                    generate_error_diagnosis, error_history + [content], image_urls or None
                )
                diag_meta: dict[str, Any] = {"phase": "diagnosis"}
                if image_urls:
                    diag_meta["image_urls"] = image_urls
                await _enqueue_pending(
                    conn,
                    sender_id=sender_id,
                    intent=intent,
                    original_msg=content,
                    suggested_reply=diagnosis_reply.text,
                    confidence_score=diagnosis_reply.confidence_score,
                    meta=diag_meta,
                )
                await _append_context(conn, sender_id, role="assistant", intent=intent, message=diagnosis_reply.text)
                return {"action": "queued", "intent": intent, "phase": "diagnosis"}
            except Exception as exc:
                await _enqueue_pending(
                    conn,
                    sender_id=sender_id,
                    intent=intent,
                    original_msg=content,
                    suggested_reply="",
                    confidence_score=0.0,
                    meta={"error": str(exc), "stage": "error_report"},
                )
                return {"action": "queued", "intent": intent, "error": str(exc)}

        if intent == "task_request":
            try:
                description = await _llm_extract_task_description(content, history)
                notion_result = await asyncio.to_thread(
                    create_task_card,
                    description,
                    sender_name or sender_discord_id,
                )

                task_status = "synced" if notion_result.success else "failed"
                await conn.execute(
                    "INSERT INTO tasks (notion_id, status, sender, description) VALUES (?, ?, ?, ?)",
                    (notion_result.notion_id, task_status, sender_name or sender_discord_id, description),
                )
                await conn.commit()

                confirmation = "anotado!"
                await _safe_reply(send_reply, confirmation)
                await _append_context(conn, sender_id, role="assistant", intent=intent, message=confirmation)
                return {"action": "replied", "intent": intent, "task_status": task_status}
            except Exception as exc:
                await _enqueue_pending(
                    conn,
                    sender_id=sender_id,
                    intent=intent,
                    original_msg=content,
                    suggested_reply="",
                    confidence_score=0.0,
                    meta={"error": str(exc), "stage": "task_request"},
                )
                return {"action": "queued", "intent": intent, "error": str(exc)}

        await _enqueue_pending(
            conn,
            sender_id=sender_id,
            intent="unknown",
            original_msg=content,
            suggested_reply="",
            confidence_score=intent_confidence,
            meta={"reason": "unknown_intent"},
        )
        return {"action": "queued", "intent": "unknown"}
    finally:
        await conn.close()


async def route_message(message: Message, client: Client) -> None:
    """Entrada de mensagem do Discord: enfileira para aprovação antes de enviar para IA."""

    def _deep_find_value(root: Any, key_names: set[str], max_depth: int = 3) -> str:
        visited: set[int] = set()
        queue: deque[tuple[Any, int]] = deque([(root, 0)])

        while queue:
            current, depth = queue.popleft()
            if current is None:
                continue

            obj_id = id(current)
            if obj_id in visited:
                continue
            visited.add(obj_id)

            if isinstance(current, dict):
                for key, value in current.items():
                    if str(key).lower() in key_names and value is not None and str(value).strip():
                        return str(value)
                if depth < max_depth:
                    for value in current.values():
                        queue.append((value, depth + 1))
                continue

            if isinstance(current, (list, tuple, set)):
                if depth < max_depth:
                    for value in current:
                        queue.append((value, depth + 1))
                continue

            if depth >= max_depth:
                continue

            attrs: dict[str, Any] = {}
            try:
                attrs = vars(current)
            except TypeError:
                attrs = {}

            for key, value in attrs.items():
                if str(key).lower() in key_names and value is not None and str(value).strip():
                    return str(value)
                queue.append((value, depth + 1))

        return ""

    def _from_raw_message(msg: Any, key: str) -> str:
        raw_candidates: list[Any] = [
            getattr(msg, "_data", None),
            getattr(msg, "data", None),
            getattr(msg, "raw", None),
        ]
        for candidate in raw_candidates:
            if isinstance(candidate, dict):
                value = candidate.get(key)
                if value is not None and str(value).strip():
                    return str(value)
        return ""

    def _message_id(msg: Any) -> str:
        direct = str(getattr(msg, "id", "") or "")
        if direct:
            return direct

        if isinstance(msg, dict):
            value = msg.get("id")
            if value is not None and str(value).strip():
                return str(value)

        from_raw = _from_raw_message(msg, "id")
        if from_raw:
            return from_raw

        deep = _deep_find_value(msg, {"id", "message_id", "messageid", "msg_id", "msgid"})
        if deep:
            return deep

        return ""

    def _channel_id(msg: Any) -> str:
        direct = str(getattr(msg, "channel_id", "") or "")
        if direct:
            return direct

        if isinstance(msg, dict):
            value = msg.get("channel_id")
            if value is not None and str(value).strip():
                return str(value)

        channel_obj = getattr(msg, "channel", None)
        from_obj = str(getattr(channel_obj, "id", "") or "")
        if from_obj:
            return from_obj

        from_obj_channel_id = str(getattr(channel_obj, "channel_id", "") or "")
        if from_obj_channel_id:
            return from_obj_channel_id

        if isinstance(channel_obj, dict):
            from_channel_dict = str(channel_obj.get("id") or channel_obj.get("channel_id") or "")
            if from_channel_dict:
                return from_channel_dict

        from_raw = _from_raw_message(msg, "channel_id")
        if from_raw:
            return from_raw

        deep = _deep_find_value(msg, {"channel_id", "channelid", "channel", "channelid"})
        if deep:
            return deep

        return ""

    author = getattr(message, "author", None)
    sender_name = ""
    if author is not None:
        sender_name = getattr(author, "global_name", None)
        if not sender_name:
            sender_name = getattr(author, "name", "")

    attachments = getattr(message, "attachments", None) or []
    image_urls_from_msg = [
        str(getattr(att, "url", "") or "")
        for att in attachments
        if str(getattr(att, "content_type", "") or "").lower().startswith("image/")
        and str(getattr(att, "url", "") or "")
    ]

    payload = {
        "sender_discord_id": str(getattr(author, "id", "")),
        "sender_name": str(sender_name),
        "content": str(getattr(message, "content", "")),
        "channel_id": _channel_id(message),
        "message_id": _message_id(message),
        "image_urls": image_urls_from_msg,
    }

    client_user = getattr(client, "user", None)
    if client_user and author and str(getattr(client_user, "id", "")) == str(getattr(author, "id", "")):
        try:
            result = await record_jeff_reply(payload)
            print(f"[router] resposta manual registrada: {result}")
        except Exception as exc:
            print(f"[router] erro ao registrar resposta manual: {exc}")
        return

    if not payload["channel_id"]:
        channel_probe = getattr(message, "channel", None)
        print(
            "[router] aviso: channel_id vazio no payload",
            {
                "msg_type": type(message).__name__,
                "msg_attrs": list(vars(message).keys()) if hasattr(message, "__dict__") else [],
                "channel_type": type(channel_probe).__name__ if channel_probe is not None else None,
                "channel_attrs": list(vars(channel_probe).keys()) if hasattr(channel_probe, "__dict__") else [],
            },
        )

    try:
        result = await record_incoming_message(payload)
        print(f"[router] mensagem observada: {result}")
    except Exception as exc:
        # Protege o loop do bot contra exceções não tratadas e expõe erro para diagnóstico.
        print(f"[router] erro ao rotear mensagem: {exc}")
        return


async def route_payload(payload: dict[str, str]) -> dict[str, Any]:
    """Roteia payload canônico sem dependência de objeto de mensagem do selfcord."""
    return await _route_core(payload, send_reply=None)


async def _open_jeff_relay(conn: aiosqlite.Connection, sender_id: int, user_channel_id: str, context_msg: str, trigger: str) -> None:
    """Registra relay pendente Jeff → usuário, ignorando se já existe um aberto."""
    existing = await conn.execute_fetchall(
        "SELECT id FROM jeff_relays WHERE sender_id = ? AND status = 'waiting' LIMIT 1",
        (sender_id,),
    )
    if existing:
        return
    await conn.execute(
        "INSERT INTO jeff_relays (sender_id, user_channel_id, status, trigger, context_msg) VALUES (?, ?, 'waiting', ?, ?)",
        (sender_id, user_channel_id, trigger, context_msg),
    )
    await conn.commit()


async def _has_recent_notification(conn: aiosqlite.Connection, sender_id: int) -> bool:
    """Verifica se já notificamos Jeff sobre este sender nos últimos 2 minutos para evitar duplicação."""
    row = await conn.execute_fetchone(
        """
        SELECT id FROM jeff_relays
        WHERE sender_id = ? AND status = 'waiting'
        AND created_at >= datetime('now', '-2 minutes')
        LIMIT 1
        """,
        (sender_id,),
    )
    return bool(row)


async def record_incoming_message(payload: dict[str, str]) -> dict[str, Any]:
    """Registra mensagem recebida sem chamar IA nem abrir fila imediata."""
    sender_discord_id = str(payload.get("sender_discord_id") or "")
    sender_name = str(payload.get("sender_name") or "")
    content = str(payload.get("content") or "").strip()
    channel_id = str(payload.get("channel_id") or "")
    message_id = str(payload.get("message_id") or "")

    if not sender_discord_id or not content:
        return {"action": "ignored", "reason": "invalid_payload"}

    image_urls = [str(u) for u in (payload.get("image_urls") or []) if u]
    effective_channel = channel_id or f"sender:{sender_discord_id}"

    conn = await _connect()
    try:
        sender = await _ensure_sender(conn, sender_discord_id, sender_name)
        sender_id = int(sender["id"])

        await _append_context(conn, sender_id, role="user", intent="unknown", message=content)
        watch_meta: dict[str, Any] = {
            "source": "discord_listener",
            "sender_discord_id": sender_discord_id,
            "sender_name": sender_name,
        }
        if image_urls:
            watch_meta["image_urls"] = image_urls
        await _upsert_conversation_watch(
            conn,
            sender_id=sender_id,
            content=content,
            channel_id=effective_channel,
            message_id=message_id,
            meta=watch_meta,
        )

        # Detecção imediata: pessoa pediu pra chamar o Jeff OU mensagem é urgente.
        wants_jeff = _is_wants_jeff(content)
        urgent = _is_urgent(content)
        print(f"[router] análise da mensagem: wants_jeff={wants_jeff}, urgent={urgent}, sender={sender_discord_id}")
        if (wants_jeff or urgent) and effective_channel:
            trigger = "user_request" if wants_jeff else "urgency"
            await _open_jeff_relay(conn, sender_id, effective_channel, content, trigger)
            reply_text = "Vou chamar o Jeff agora!" if wants_jeff else "Entendido, vou avisar o Jeff que é urgente!"
            await _append_context(conn, sender_id, role="assistant", intent="unknown", message=reply_text)

            # Verifica deduplicação antes de notificar
            already_notified = await _has_recent_notification(conn, sender_id)
            if already_notified:
                print(f"[router] notificação recente já existe para este sender, ignorando duplicata")
                return {"action": "jeff_called", "trigger": trigger, "sender_id": sender_id, "deduplicated": True}

            print(f"[router] notificando Jeff — trigger={trigger}, sender={sender_discord_id}")
            asyncio.create_task(_notify_jeff_background(
                sender_name=sender_name,
                content=content,
                channel_id=effective_channel,
                reply_text=reply_text,
                trigger=trigger,
            ))
            return {"action": "jeff_called", "trigger": trigger, "sender_id": sender_id}

        return {"action": "watching", "sender_id": sender_id}
    finally:
        await conn.close()


async def _notify_jeff_background(
    sender_name: str,
    content: str,
    channel_id: str,
    reply_text: str,
    trigger: str = "user_request",
) -> None:
    """Envia resposta ao usuário e notifica Jeff em background."""
    try:
        result = await asyncio.to_thread(send_via_userbot, channel_id, reply_text)
        if not result.success:
            print(f"[router] falha ao responder usuario (jeff_call): {result.error}")
    except Exception as exc:
        print(f"[router] erro ao responder usuario (jeff_call): {exc}")

    try:
        result = await asyncio.to_thread(notify_jeff, sender_name, content, trigger, "")
        if result.success:
            print(f"[router] Jeff notificado com sucesso — trigger={trigger}, sender={sender_name}")
        else:
            print(f"[router] FALHA ao notificar Jeff — trigger={trigger}, erro={result.error}")
    except Exception as exc:
        print(f"[router] erro inesperado ao notificar Jeff: {exc}")


async def record_jeff_reply(payload: dict[str, str]) -> dict[str, Any]:
    """Registra uma resposta manual do Jeff para impedir auto-atendimento indevido."""
    content = str(payload.get("content") or "").strip()
    channel_id = str(payload.get("channel_id") or "")
    message_id = str(payload.get("message_id") or "")

    if not content or not channel_id:
        return {"action": "ignored", "reason": "invalid_jeff_reply_payload"}

    conn = await _connect()
    try:
        updated = await _mark_jeff_reply_for_channel(conn, channel_id, content, message_id)
        return {"action": "jeff_reply_recorded", "resolved_watches": updated}
    finally:
        await conn.close()


async def queue_payload_for_approval(payload: dict[str, str]) -> dict[str, Any]:
    """Enfileira mensagem para aprovação humana antes de qualquer chamada para IA."""
    sender_discord_id = str(payload.get("sender_discord_id") or "")
    sender_name = str(payload.get("sender_name") or "")
    content = str(payload.get("content") or "").strip()
    channel_id = str(payload.get("channel_id") or "")
    message_id = str(payload.get("message_id") or "")

    if not sender_discord_id or not content:
        return {"action": "ignored", "reason": "invalid_payload"}

    conn = await _connect()
    try:
        sender = await _ensure_sender(conn, sender_discord_id, sender_name)
        sender_id = int(sender["id"])

        await _append_context(conn, sender_id, role="user", intent="unknown", message=content)
        await _upsert_conversation_watch(
            conn,
            sender_id=sender_id,
            content=content,
            channel_id=channel_id or f"sender:{sender_discord_id}",
            message_id=message_id,
            meta={
                "source": "discord_listener",
                "sender_discord_id": sender_discord_id,
                "sender_name": sender_name,
            },
        )

        recent_queue = await _find_recent_pre_ai_queue(conn, sender_id, channel_id)
        if recent_queue:
            await _append_grouped_pending_message(
                conn,
                queue_id=int(recent_queue["id"]),
                existing_text=str(recent_queue["original_msg"]),
                incoming_text=content,
                meta=dict(recent_queue["meta"]),
                channel_id=channel_id,
                message_id=message_id,
            )
            return {"action": "grouped_pre_ai", "queue_id": int(recent_queue["id"])}

        queue_id = await _enqueue_pending(
            conn,
            sender_id=sender_id,
            intent="unknown",
            original_msg=content,
            suggested_reply="",
            confidence_score=0.0,
            meta={
                "pre_ai": True,
                "response_approval": False,
                "approved_for_ai": False,
                "channel_id": channel_id,
                "message_id": message_id,
                "grouped_count": 1,
            },
        )
        return {"action": "queued_pre_ai", "queue_id": queue_id}
    finally:
        await conn.close()
