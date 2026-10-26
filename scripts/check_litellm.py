#!/usr/bin/env python3
"""Check LiteLLM's OpenAI-compatible models endpoint without generating text."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from viharbot.litellm_client import LiteLLMError, check_connection  # noqa: E402


def main() -> int:
    try:
        result = check_connection()
    except LiteLLMError as exc:
        print(f"LiteLLM connectivity check failed: {exc}", file=sys.stderr)
        return 1
    models = result.get("data", [])
    print(f"LiteLLM reachable; /models returned {len(models)} model entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
