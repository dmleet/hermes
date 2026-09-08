from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent


def load(name: str) -> Any:
    """Load tests/fixtures/<name>.json (captured from the real services)."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
