from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from api.services.memory import close_conversation
from config import get_settings


def _sqlite_path() -> Path:
    settings = get_settings()
    root = Path(__file__).resolve().parents[2]
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


class MemoryScheduler:
    def __init__(self, poll_seconds: int = 60, inactivity_minutes: int = 10) -> None:
        self.poll_seconds = max(poll_seconds, 5)
        self.inactivity_minutes = max(inactivity_minutes, 1)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[scheduler] erro ao encerrar scheduler: {exc}")

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._process_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[scheduler] erro no ciclo principal: {exc}")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _process_cycle(self) -> None:
        conn = await _connect()
        try:
            stale_senders = await conn.execute_fetchall(
                """
                SELECT sender_id, MAX(created_at) AS last_message_at
                FROM conversation_context
                GROUP BY sender_id
                HAVING MAX(created_at) <= datetime('now', ?)
                """,
                (f"-{self.inactivity_minutes} minutes",),
            )
        except Exception as exc:
            print(f"[scheduler] erro ao listar senders inativos: {exc}")
            try:
                await conn.close()
            except Exception:
                pass
            return

        for row in stale_senders:
            try:
                await close_conversation(int(row["sender_id"]), conn)
            except Exception as exc:
                print(f"[scheduler] erro ao fechar conversa do sender {row['sender_id']}: {exc}")

        try:
            await conn.close()
        except Exception as exc:
            print(f"[scheduler] erro ao fechar conexão do scheduler: {exc}")