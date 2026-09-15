import asyncio
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.consumer.consume import Consumer
from src.ingestor.context_ingest import Ingestor
from src.ingestor.prompt_ingestor import PromptIngestor
from src.models.model_gate import ModelGate, settings
from src.utils.helpers import get_logger

logger = get_logger(__name__)

app = FastAPI(
    title="Task Router Agent",
    description="Routes a natural-language query to one registered task and runs it.",
    version="1.0.0",
)

model_gate = ModelGate()
consumer = Consumer()
ingestor = Ingestor()


class ModelRequest(BaseModel):
    content: str = Field(..., description="The user's natural-language query.")


class ModelResponse(BaseModel):
    ok: bool = Field(..., description="True when the task ran and exited cleanly.")
    task_id: Optional[str] = Field(None, description="The task the query routed to.")
    output: str = Field("", description="The task's stdout, whitespace-trimmed.")
    error: Optional[str] = Field(None, description="Failure reason, if any.")
    stderr: str = Field("", description="The task's stderr; why a failure happened.")
    routing_reply: str = Field(..., description="The model's raw routing reply.")
    execution: dict[str, Any] = Field(..., description="Full execution envelope.")


class TaskInfo(BaseModel):
    task_id: str
    description: str
    allowed_args: list[str] = []


class TasksResponse(BaseModel):
    available: list[TaskInfo] = Field(..., description="Tasks the router can select.")
    unavailable: list[str] = Field(
        ..., description="Registered tasks whose script is not on disk yet."
    )


class ErrorResponse(BaseModel):
    detail: str


class HealthResponse(BaseModel):
    status: str


@app.post(
    "/active_window",
    response_model=ModelResponse,
    summary="Route a query to a task and run it",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request body."},
        500: {"model": ErrorResponse, "description": "Inference or execution failure."},
    },
)
async def get_active_window(payload: ModelRequest):
    if not payload.content or not payload.content.strip():
        raise HTTPException(status_code=400, detail="Missing 'content' in request body.")

    try:
        prompt = PromptIngestor(payload.content).ingest_routes()

        # Both calls block: one on the network, one on a subprocess. Off the
        # event loop, or a single request stalls every other request.
        routing_reply = await asyncio.to_thread(model_gate.route, prompt)
        execution = await asyncio.to_thread(consumer.consume, routing_reply)
    except Exception as exc:
        logger.exception("request failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = execution.get("result") or {}

    return ModelResponse(
        ok=execution["ok"],
        task_id=result.get("task_id"),
        output=result.get("output", ""),
        error=execution.get("error"),
        stderr=result.get("stderr", ""),
        routing_reply=routing_reply,
        execution=execution,
    )


@app.get(
    "/tasks",
    response_model=TasksResponse,
    summary="Show the task catalog",
    responses={500: {"model": ErrorResponse, "description": "Task catalog unreadable."}},
)
async def list_tasks():
    """What this agent can currently do, straight from the registry."""
    try:
        available = ingestor.ingest_tasks()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return TasksResponse(
        available=[TaskInfo(**task) for task in available],
        unavailable=ingestor.unavailable_tasks(),
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
