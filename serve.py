import asyncio
import uuid
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.consumer.consume import Consumer
from src.executor import calendar_auth
from src.ingestor.context_ingest import Ingestor
from src.ingestor.prompt_ingestor import PromptIngestor
from src.models.model_gate import ModelGate, settings
from src.prompts.speech_prompt import (
    AUTH_START_FAILED,
    SPEECH_FALLBACK,
    auth_required_spoken,
)
from src.utils.helpers import audio_output_path, get_logger

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
    request_id: str = Field(..., description="Server-generated id for this request.")
    ok: bool = Field(..., description="True when the task ran and exited cleanly.")
    task_id: Optional[str] = Field(None, description="The task the query routed to.")
    output: str = Field("", description="The task's stdout, whitespace-trimmed.")
    error: Optional[str] = Field(None, description="Failure reason, if any.")
    stderr: str = Field("", description="The task's stderr; why a failure happened.")
    routing_reply: str = Field(..., description="The model's raw routing reply.")
    auth_required: bool = Field(
        False, description="True when the answer is a request to sign in, not an answer."
    )
    user_code: Optional[str] = Field(
        None, description="Sign-in code to enter, when auth_required."
    )
    verification_uri: Optional[str] = Field(
        None, description="Where to enter the code, when auth_required."
    )
    spoken_text: str = Field("", description="The result rephrased to be read aloud.")
    audio_path: Optional[str] = Field(
        None, description="Path to the synthesised speech, if audio is enabled."
    )
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


class CalendarStatusResponse(BaseModel):
    signed_in: bool = Field(..., description="True when a cached Graph token exists.")


class CalendarLoginResponse(BaseModel):
    stage: str = Field(..., description="'pending' while awaiting approval, or 'signed-in'.")
    user_code: Optional[str] = Field(None, description="The code to enter on the sign-in page.")
    verification_uri: Optional[str] = Field(None, description="Where to enter the code.")
    expires_in_seconds: Optional[int] = Field(None, description="How long the code stays valid.")
    browser_opened: bool = Field(False, description="True when a browser was launched locally.")
    message: str = Field("", description="What the user should do next.")


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

    ingestor_for_query = PromptIngestor(payload.content)
    # Generated here, not asked of the model: it has to be present and unique
    # even when routing fails, and it names this request's audio file.
    request_id = uuid.uuid4().hex[:12]

    try:
        prompt = ingestor_for_query.ingest_routes()

        # Both calls block: one on the network, one on a subprocess. Off the
        # event loop, or a single request stalls every other request.
        routing_reply = await asyncio.to_thread(model_gate.route, prompt)
        execution = await asyncio.to_thread(consumer.consume, routing_reply, request_id)
    except Exception as exc:
        logger.exception("request failed: request_id=%s", request_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = execution.get("result") or {}

    # A calendar question asked before signing in is not a failure to report,
    # it is a prerequisite to walk the user through. Start the sign-in and
    # answer with the code, rather than telling them "that didn't work".
    auth = await _offer_sign_in(execution)

    if auth is not None:
        spoken_text = auth["spoken"]
    else:
        # Speech is presentation, not the answer. The task has already run by
        # this point, so a failure here degrades the reply rather than
        # discarding a result the caller can still use.
        spoken_text = await _speak(ingestor_for_query, execution)

    audio_path = await _synthesize(spoken_text, request_id)

    return ModelResponse(
        request_id=request_id,
        ok=execution["ok"],
        task_id=result.get("task_id"),
        output=result.get("output", ""),
        error=execution.get("error"),
        stderr=result.get("stderr", ""),
        routing_reply=routing_reply,
        auth_required=auth is not None,
        user_code=(auth or {}).get("user_code"),
        verification_uri=(auth or {}).get("verification_uri"),
        spoken_text=spoken_text,
        audio_path=audio_path,
        execution=execution,
    )


async def _offer_sign_in(execution: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Start the calendar sign-in when that is what the query really needs.

    Returns the code and the words to say, or None when this query had nothing
    to do with signing in.
    """
    if execution.get("error") != "not_signed_in_to_calendar":
        return None

    logger.info("calendar query arrived unauthenticated; starting sign-in")

    try:
        payload = await asyncio.to_thread(calendar_auth.start_login)
    except calendar_auth.CalendarAuthError:
        logger.exception("could not start calendar sign-in")
        return {"spoken": AUTH_START_FAILED, "user_code": None, "verification_uri": None}

    if payload.get("stage") == "signed-in":
        # Signed in between the task running and now: say so rather than
        # inventing a code, and the next question will answer properly.
        return {
            "spoken": "Looks like your calendar just got connected. Ask me again and I'll check it.",
            "user_code": None,
            "verification_uri": None,
        }

    code = payload.get("userCode")
    return {
        "spoken": auth_required_spoken(code),
        "user_code": code,
        "verification_uri": payload.get("verificationUri"),
    }


async def _speak(prompt_ingestor: PromptIngestor, execution: dict[str, Any]) -> str:
    """Rephrase the execution result for the ear, or fall back to a spoken apology."""
    try:
        speech_prompt = prompt_ingestor.ingest_speech(execution)
        return await asyncio.to_thread(model_gate.speak, speech_prompt)
    except Exception:
        logger.exception("speech rephrase failed for request_id=%s", execution.get("request_id"))
        return SPEECH_FALLBACK


async def _synthesize(spoken_text: str, request_id) -> Optional[str]:
    """Render spoken text to a .wav and return its path, or None."""
    if not settings.audio_enabled or not spoken_text.strip():
        return None

    try:
        path = await asyncio.to_thread(
            model_gate.synthesize, spoken_text, audio_output_path(request_id)
        )
        return str(path)
    except Exception:
        # The caller still has spoken_text and can synthesise it elsewhere.
        logger.exception("audio synthesis failed for request_id=%s", request_id)
        return None


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


@app.get(
    "/calendar/status",
    response_model=CalendarStatusResponse,
    summary="Whether the calendar is signed in",
)
async def calendar_status():
    return CalendarStatusResponse(signed_in=calendar_auth.is_signed_in())


@app.post(
    "/calendar/login",
    response_model=CalendarLoginResponse,
    summary="Start calendar sign-in and return the code to enter",
    responses={
        500: {"model": ErrorResponse, "description": "Sign-in could not be started."},
    },
)
async def calendar_login(open_browser: bool = True):
    """Start the device-code flow and hand back the code.

    Returns as soon as the code exists; the flow keeps polling in the
    background, so poll `/calendar/status` to see when approval lands. Any
    browser opens on the machine running this service, which is the same
    machine for the intended local single-user setup.
    """
    try:
        payload = await asyncio.to_thread(calendar_auth.start_login, open_browser)
    except calendar_auth.CalendarAuthError as exc:
        logger.exception("calendar sign-in failed to start")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if payload.get("stage") == "signed-in":
        return CalendarLoginResponse(
            stage="signed-in",
            message="Already signed in; nothing to do.",
        )

    code = payload.get("userCode")
    uri = payload.get("verificationUri")

    return CalendarLoginResponse(
        stage="pending",
        user_code=code,
        verification_uri=uri,
        expires_in_seconds=payload.get("expiresInSeconds"),
        browser_opened=bool(payload.get("browserOpened")),
        message=f"Enter {code} at {uri} to finish signing in.",
    )


@app.post(
    "/calendar/logout",
    response_model=CalendarStatusResponse,
    summary="Delete the cached calendar tokens",
)
async def calendar_logout():
    try:
        await asyncio.to_thread(calendar_auth.sign_out)
    except calendar_auth.CalendarAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return CalendarStatusResponse(signed_in=calendar_auth.is_signed_in())


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
