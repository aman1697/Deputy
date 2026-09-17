import os
from pathlib import Path

# Single source of truth for on-disk locations. Everything else imports these
# rather than recomputing paths relative to its own file.
ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "data"
TASKS_FILE = DATA_DIR / "tasks.json"
SCRIPTS_DIR = (ROOT / "scripts").resolve()

CUDA_CONFIG = {
    "device": "cuda:0",
    "dtype": "bfloat16",
    "attn_implementation": "flash_attention_2",
}

# Where synthesised speech lands. Generated output, not source: gitignored and
# created on first write rather than committed as an empty directory.
AUDIO_DIR = ROOT / "audio"
AUDIO_FORMAT = "wav"
AUDIO_LANGUAGE = "English"

# Graph tokens live outside the repo, under the user's profile: they are
# per-user credentials, not project files, and must never land in a working
# tree someone might commit. get-calendar.ps1 writes this path; both sides
# have to agree on it.
_LOCAL_APPDATA = os.environ.get("LOCALAPPDATA")
GRAPH_TOKEN_FILE = (
    Path(_LOCAL_APPDATA) / "Deputy" / "graph-token.xml"
    if _LOCAL_APPDATA
    else Path.home() / ".deputy" / "graph-token.xml"
)
