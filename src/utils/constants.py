from pathlib import Path

# Single source of truth for on-disk locations. Everything else imports these
# rather than recomputing paths relative to its own file.
ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "data"
TASKS_FILE = DATA_DIR / "tasks.json"
SCRIPTS_DIR = (ROOT / "scripts").resolve()
