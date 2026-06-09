"""FastAPI entrypoint for the Syzygy launchpad AI service."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .auth import Principal, current_principal
from .config import get_settings
from .destination_client import shutdown_destinations
from .orchestrator import orchestrator
from .schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    ToolCallTrace,
)

VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    # Warm the orchestrator (registers agents).
    orchestrator()
    try:
        yield
    finally:
        await shutdown_destinations()


app = FastAPI(
    title="Syzygy Launchpad AI Service",
    version=VERSION,
    lifespan=lifespan,
)

# CORS is permissive only because the approuter is the single entrypoint
# in production; locally this enables iframe / file:// access for testing.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=VERSION)


@app.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    principal: Principal = Depends(current_principal),
) -> ChatResponse:
    if req.messages[-1].role != "user":
        raise HTTPException(
            status_code=400,
            detail="Last message must be from the user.",
        )

    logging.getLogger(__name__).info(
        "chat user=%s msgs=%d", principal.user_id, len(req.messages)
    )

    history = [m.model_dump() for m in req.messages]
    result = await orchestrator().handle(history)

    return ChatResponse(
        reply=result.reply,
        agent=result.agent,
        tool_calls=[ToolCallTrace(**tc) for tc in result.tool_calls],
    )
