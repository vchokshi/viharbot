#!/usr/bin/env python3
"""Read a Cobertura coverage.xml and report the measured line coverage.

The percentage always comes from the report. It is rounded down, so it never overstates.
--badge-json writes a Shields.io endpoint document for the measured value.
"""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

COLORS = ((90, "brightgreen"), (80, "green"), (70, "yellowgreen"), (60, "yellow"), (50, "orange"))


def measured(path: Path) -> dict[str, float | int]:
    root = ET.parse(path).getroot()
    valid = int(root.get("lines-valid", 0))
    covered = int(root.get("lines-covered", 0))
    if valid <= 0:
        raise ValueError("the coverage report measured no lines")
    return {"lines_valid": valid, "lines_covered": covered, "percent": 100.0 * covered / valid}


def badge(percent: float) -> dict:
    whole = math.floor(percent)
    color = next((name for floor, name in COLORS if whole >= floor), "red")
    return {"schemaVersion": 1, "label": "coverage", "message": f"{whole}%", "color": color,
            "cacheSeconds": 300}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path)
    parser.add_argument("--badge-json", type=Path)
    args = parser.parse_args(argv)
    result = measured(args.xml)
    print(f"| Coverage | {math.floor(result['percent'])}% | "
          f"{result['lines_covered']} of {result['lines_valid']} lines (src/viharbot) |")
    if args.badge_json:
        args.badge_json.write_text(json.dumps(badge(result["percent"])) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
