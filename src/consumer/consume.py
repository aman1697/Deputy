import json
import time

from src.executor.task_runner import TaskRunner
from src.executor.task_validator import TaskValidator
from src.utils.helpers import extract_json_object, get_logger

logger = get_logger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024

# Errors worth retrying: the task was legitimate, the environment failed.
# Anything else is a permanent reject and should go straight to the DLQ.
TRANSIENT_ERRORS = ("timeout", "spawn_failed", "powershell_not_available")


class Consumer:

    def __init__(self, validator=None, runner=None):
        self.validator = validator or TaskValidator()
        # Share the one validator so the registry cache is warm for both.
        self.runner = runner or TaskRunner(validator=self.validator)

    # ----------------------------------------------------------------------
    # Entry point
    # ----------------------------------------------------------------------

    def consume(self, message):
        received_at = time.monotonic()

        payload, decode_error = self._decode(message)
        if decode_error is not None:
            logger.warning("rejecting message: %s", decode_error)
            return self._reply(None, False, error=decode_error, received_at=received_at)

        request_id = payload.get("request_id")
        task_id, task_type = self.validator.normalize(payload)

        entry = self.validator.resolve(payload)
        if entry is None:
            logger.warning(
                "rejecting unknown task: request_id=%s task_id=%r task_type=%r",
                request_id,
                task_id,
                task_type,
            )
            return self._reply(
                request_id, False, error="validation_failed", received_at=received_at
            )

        logger.info("executing: request_id=%s task_id=%s", request_id, entry["id"])

        try:
            result = self.runner.executor(payload)
        except Exception as exc:
            # The runner catches its own handler failures; this is the last
            # line of defence so one bad message cannot kill the consumer.
            logger.exception("runner raised for request_id=%s", request_id)
            return self._reply(
                request_id,
                False,
                error=f"runner_crash: {type(exc).__name__}",
                received_at=received_at,
            )

        logger.info(
            "finished: request_id=%s ok=%s exit_code=%s duration_ms=%s error=%s",
            request_id,
            result["ok"],
            result["exit_code"],
            result["duration_ms"],
            result["error"],
        )

        return self._reply(request_id, result["ok"], result=result, received_at=received_at)

    # ----------------------------------------------------------------------
    # Decoding
    # ----------------------------------------------------------------------

    @staticmethod
    def _decode(message):
        """Return (payload_dict, error). Exactly one is None."""
        if isinstance(message, dict):
            return message, None

        if isinstance(message, (bytes, bytearray)):
            if len(message) > MAX_MESSAGE_BYTES:
                return None, "message_too_large"
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:
                return None, "invalid_encoding"

        if not isinstance(message, str):
            return None, "unsupported_message_type"

        if len(message) > MAX_MESSAGE_BYTES:
            return None, "message_too_large"

        # Strict parse first; fall back to extracting the first balanced object
        # so a code fence or a chatty preamble does not cost a whole task run.
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            payload = extract_json_object(message)
            if payload is None:
                return None, "malformed_json"
            return payload, None

        if not isinstance(payload, dict):
            return None, "payload_not_object"

        return payload, None

    # ----------------------------------------------------------------------
    # Reply envelope
    # ----------------------------------------------------------------------

    @staticmethod
    def _reply(request_id, ok, result=None, error=None, received_at=None):
        error = error or (result or {}).get("error")
        retryable = bool(error) and error.startswith(TRANSIENT_ERRORS)

        return {
            "request_id": request_id,
            "ok": ok,
            "error": error,
            "retryable": retryable,
            "result": result,
            "handled_ms": (
                None if received_at is None else int((time.monotonic() - received_at) * 1000)
            ),
        }