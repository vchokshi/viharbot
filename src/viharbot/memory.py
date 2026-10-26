"""Minimal per-agent append-only JSONL memory."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def remember(agent: str, request_id: str, result: Any) -> None:
    root = Path(os.getenv("MEMORY_DIR", "data")) / agent
    root.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "result": result,
    }
    with (root / "memory.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
