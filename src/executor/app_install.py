"""State and rules for the "shall I install that for you?" exchange.

Deputy offers an install, the user says yes, and only then does anything get
installed. That needs two things this module owns: the offer has to survive
until the next question, and "yes" has to be recognised strictly enough that
nothing gets installed by accident.
"""

import re
import threading
import time

from src.prompts.app_prompt import PACKAGE_ID_PATTERN
from src.utils.helpers import get_logger

logger = get_logger(__name__)

# An offer the user never answered should not still be live ten minutes later,
# waiting to turn an unrelated "yes" into an install.
OFFER_TTL_SECONDS = 300

# Deliberately a closed list of unambiguous agreements, matched whole. Asking a
# model to judge consent would mean a misread "no" installs software nobody
# asked for; the cost of being strict is only that the user repeats themselves.
_AFFIRMATIVES = frozenset(
    {
        "y",
        "ok",
        "okay",
        "yes",
        "yep",
        "yeah",
        "yup",
        "sure",
        "please",
        "do it",
        "go on",
        "go ahead",
        "yes please",
        "install it",
        "please install it",
        "yes do it",
        "go for it",
    }
)

_PACKAGE_ID_RE = re.compile(PACKAGE_ID_PATTERN)

_offer = None
_offer_lock = threading.Lock()


def is_affirmative(text) -> bool:
    """True only for a clear, whole-message yes."""
    if not isinstance(text, str):
        return False

    # Strip the punctuation people end a one-word reply with, and nothing else:
    # matching a "yes" buried inside a longer sentence would be a guess.
    cleaned = re.sub(r"[.!,\s]+$", "", text.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned in _AFFIRMATIVES


def is_valid_package_id(package_id) -> bool:
    """Guard the one value the model gets to influence."""
    return isinstance(package_id, str) and bool(_PACKAGE_ID_RE.fullmatch(package_id))


def offer(app_name: str, package_id: str) -> None:
    """Record that we asked whether to install this."""
    global _offer

    with _offer_lock:
        _offer = {
            "app_name": app_name,
            "package_id": package_id,
            "expires_at": time.monotonic() + OFFER_TTL_SECONDS,
        }
    logger.info("offered to install %s (%s)", app_name, package_id)


def take_offer():
    """Return the live offer and clear it, or None.

    Cleared on read so one "yes" can only ever install one thing.
    """
    global _offer

    with _offer_lock:
        pending = _offer
        _offer = None

    if pending is None:
        return None
    if time.monotonic() >= pending["expires_at"]:
        logger.info("install offer for %s expired unanswered", pending["app_name"])
        return None
    return pending


def clear() -> None:
    global _offer

    with _offer_lock:
        _offer = None
