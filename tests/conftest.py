import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Config is validated at import time. Provide placeholders so the suite runs
# without real credentials; no test performs a live inference call.
os.environ.setdefault("MODEL_NAME", "test-model")
os.environ.setdefault("AUDIO_MODEL_NAME", "test-audio-model")
os.environ.setdefault("HF_TOKEN", "test-token")
os.environ.setdefault("ROLE", "user")
# Synthesis needs a GPU and real weights. Tests that cover it enable it
# explicitly and stub the model; everything else must not reach for it.
os.environ.setdefault("AUDIO_ENABLED", "false")
