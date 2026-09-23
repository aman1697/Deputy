import asyncio
import json
import uuid
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from src.consumer.consume import Consumer
from src.executor import app_install, calendar_auth
from src.ingestor.context_ingest import Ingestor
from src.ingestor.prompt_ingestor import PromptIngestor
from src.models.model_gate import ModelGate, settings
from src.models.speech_to_text import SpeechToText
from src.prompts.speech_prompt import (
    AUTH_START_FAILED,
    SPEECH_FALLBACK,
    auth_required_spoken,
    install_failed_spoken,
    install_offer_spoken,
    install_success_spoken,
    install_unknown_spoken,
)
from src.utils.constants import MAX_AUDIO_UPLOAD_BYTES
from src.utils.helpers import audio_output_path, extract_json_object, get_logger

logger = get_logger(__name__)

app = FastAPI(
    title="Task Router Agent",
    description="Routes a natural-language query to one registered task and runs it.",
    version="1.0.0",
)

model_gate = ModelGate()
consumer = Consumer()
ingestor = Ingestor()
speech_to_text = SpeechToText(settings)

# Downloading and installing a package is nothing like running a local script,
# so it gets its own budget rather than the runner's 60s default.
INSTALL_TIMEOUT_SECONDS = 600


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
    transcript: Optional[str] = Field(
        None, description="What speech-to-text heard, when the query came in as audio."
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

    return await _answer_query(payload.content)


@app.post(
    "/listen",
    response_model=ModelResponse,
    summary="Transcribe a spoken query, then route and run it",
    responses={
        400: {"model": ErrorResponse, "description": "Bad or unintelligible recording."},
        500: {"model": ErrorResponse, "description": "Inference or execution failure."},
        503: {"model": ErrorResponse, "description": "Speech-to-text is disabled."},
    },
)
async def listen(file: UploadFile = File(..., description="A short voice recording.")):
    """The voice door into the same pipeline `/active_window` uses.

    Transcribes the upload, then hands the text to `_answer_query` exactly as
    a typed request would: everything downstream of routing has no idea, and
    no reason to care, whether the question was typed or spoken.
    """
    if not settings.stt_enabled:
        raise HTTPException(status_code=503, detail="Speech-to-text is disabled (STT_ENABLED).")

    audio_bytes = await file.read()
    if len(audio_bytes) > MAX_AUDIO_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Recording is larger than the allowed limit.")

    try:
        transcript = await asyncio.to_thread(speech_to_text.transcribe, audio_bytes)
    except ValueError as exc:
        # Empty, too short, or no words made out - a bad recording, not a
        # server fault.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("transcription failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.info("transcribed %d bytes -> %r", len(audio_bytes), transcript)

    response = await _answer_query(transcript)
    response.transcript = transcript
    return response


async def _answer_query(content: str) -> ModelResponse:
    """Route a query to a task, run it, and phrase the result.

    Shared by /active_window and /listen: from here down, text is text,
    regardless of whether it arrived typed or spoken.
    """
    ingestor_for_query = PromptIngestor(content)
    # Generated here, not asked of the model: it has to be present and unique
    # even when routing fails, and it names this request's audio file.
    request_id = uuid.uuid4().hex[:12]

    # "yes" is an answer to the install we offered, not a new question. Checked
    # before routing, because the router has no idea what it refers to.
    accepted = await _accepted_install(content, request_id)
    if accepted is not None:
        return accepted

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

    # An app the user asked for but does not have is likewise a conversation,
    # not an error: resolve what they meant, then offer to install it.
    missing_app = None
    if auth is None and execution.get("error") == "app_not_installed":
        execution, missing_app = await _handle_missing_app(execution, request_id)
        result = execution.get("result") or {}

    if auth is not None:
        spoken_text = auth["spoken"]
    elif missing_app is not None:
        spoken_text = missing_app
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


def _model_json(reply: str, key: str):
    """Pull one key out of a model's JSON reply, or None.

    The model is asked for a bare object, but the same lenient recovery the
    router relies on applies here: fences and preambles are common.
    """
    payload = extract_json_object(reply or "")
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


async def _handle_missing_app(execution, request_id):
    """Work out what app was meant, then launch it or offer to install it.

    Returns (execution, spoken_or_None). A spoken string means the exchange
    ended here; None means `execution` now holds a real result to narrate.
    """
    result = execution.get("result") or {}
    payload = extract_json_object(result.get("output") or "") or {}
    requested = payload.get("requested") or ""
    inventory = payload.get("inventory") or []

    # Literal matching already failed. "vs code" is not a substring of "Visual
    # Studio Code", so ask the model to match it against what is installed.
    if inventory:
        try:
            prompt = PromptIngestor.ingest_app_match(requested, inventory)
            match = _model_json(await asyncio.to_thread(model_gate.lookup, prompt), "match")
        except Exception:
            logger.exception("app name resolution failed for %r", requested)
            match = None

        # Only a name the machine actually reported counts; a model that
        # invents one would have us launch something nobody has.
        if match in inventory:
            logger.info("resolved %r to installed app %r", requested, match)
            retry = await asyncio.to_thread(
                consumer.consume,
                json.dumps({"task_id": "launch-app", "args": ["-Name", match]}),
                request_id,
            )
            return retry, None

    return execution, await _offer_install(requested)


async def _offer_install(app_name: str) -> str:
    """Find the package for an app we do not have, and offer to install it."""
    if not app_name:
        return install_unknown_spoken("that")

    try:
        prompt = PromptIngestor.ingest_package_id(app_name)
        package_id = _model_json(
            await asyncio.to_thread(model_gate.lookup, prompt), "package_id"
        )
    except Exception:
        logger.exception("package lookup failed for %r", app_name)
        package_id = None

    # The model's answer is untrusted: it reaches the install script only if it
    # looks like an identifier and nothing else.
    if not app_install.is_valid_package_id(package_id):
        if package_id:
            logger.warning("refusing implausible package id %r", package_id)
        return install_unknown_spoken(app_name)

    app_install.offer(app_name, package_id)
    return install_offer_spoken(app_name)


async def _accepted_install(content: str, request_id: str):
    """Run the install the user just agreed to, or return None.

    Returns None unless there is a live offer *and* this message is a plain
    yes, so an unrelated question is never mistaken for consent.
    """
    if not app_install.is_affirmative(content):
        return None

    pending = app_install.take_offer()
    if pending is None:
        return None

    app_name = pending["app_name"]
    logger.info("installing %s (%s) on the user's say-so", app_name, pending["package_id"])

    execution = await asyncio.to_thread(
        consumer.consume,
        json.dumps(
            {
                "task_id": "install-app",
                "args": ["-PackageId", pending["package_id"]],
                # Installs are slow; the 60s default would kill most of them
                # partway through.
                "timeout": INSTALL_TIMEOUT_SECONDS,
            }
        ),
        request_id,
    )

    if execution["ok"]:
        spoken = install_success_spoken(app_name)
    else:
        spoken = install_failed_spoken(app_name, execution.get("error"))

    result = execution.get("result") or {}
    audio_path = await _synthesize(spoken, request_id)

    return ModelResponse(
        request_id=request_id,
        ok=execution["ok"],
        task_id="install-app",
        output=result.get("output", ""),
        error=execution.get("error"),
        stderr=result.get("stderr", ""),
        routing_reply="",  # no routing happened; this answered a standing offer
        spoken_text=spoken,
        audio_path=audio_path,
        execution=execution,
    )


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
