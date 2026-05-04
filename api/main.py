from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.routes import messages, knowledge, tasks
from api.services.db import init_db
from api.services.scheduler import MemoryScheduler
from bot.router import route_payload


memory_scheduler = MemoryScheduler()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    memory_scheduler.start()
    try:
        yield
    finally:
        await memory_scheduler.stop()


app = FastAPI(title="Jeff Bot API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
        "http://localhost:5176",
        "http://127.0.0.1:5176",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class IncomingMessagePayload(BaseModel):
    sender_discord_id: str = Field(min_length=1)
    sender_name: str = Field(default="")
    content: str = Field(min_length=1)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/ingest/message")
async def ingest_message(payload: IncomingMessagePayload) -> dict:
    return await route_payload(payload.model_dump())


app.include_router(messages.router, prefix="/api")
app.include_router(knowledge.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
