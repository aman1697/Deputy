import json
import logging

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # We attach our own handler, so letting the record bubble to the root
        # logger as well (uvicorn installs one) would print every line twice.
        logger.propagate = False
    return logger


def _find_object_end(text, start):
    """Index just past the `}` closing the object opened at `start`, or None.

    Brace counting has to ignore braces inside string literals, and quotes
    inside those strings can be backslash-escaped.
    """
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1

    return None


def extract_json_object(text):
    """Pull the first well-formed JSON object out of model output, or None.

    The router prompt demands a bare object, but models drift: code fences,
    a "Sure, here you go:" preamble, or a trailing explanation are all common.
    Scanning for the first balanced `{...}` that parses recovers every one of
    those without letting us accept arbitrary prose.
    """
    if not isinstance(text, str):
        return None

    for start, char in enumerate(text):
        if char != "{":
            continue

        end = _find_object_end(text, start)
        if end is None:
            break  # unbalanced from here on; no later brace can close either

        try:
            payload = json.loads(text[start:end])
        except json.JSONDecodeError:
            continue  # not valid JSON; try the next opening brace

        if isinstance(payload, dict):
            return payload

    return None
