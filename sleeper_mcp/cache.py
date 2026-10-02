import json
from pathlib import Path
from typing import Optional

CACHE_DIR = Path.home() / ".cache" / "sleeper-mcp"
CONTEXT_FILE = CACHE_DIR / "context.json"


def load_context() -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if CONTEXT_FILE.exists():
        try:
            return json.loads(CONTEXT_FILE.read_text())
        except Exception:
            pass
    return {}


def save_context(ctx: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CONTEXT_FILE.write_text(json.dumps(ctx, indent=2))
