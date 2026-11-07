"""Split a pull request description into its headed sections. Pure text; the text stays data."""

from __future__ import annotations

import re

SECTION = re.compile(r"(?ms)^#{1,4}\s*(?P<h>[^\n]+)\n(?P<b>.*?)(?=^#{1,4}\s|\Z)")


def sections(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for m in SECTION.finditer(body):
        text = re.sub(r"<!--.*?-->", "", m["b"], flags=re.S).strip()
        if text:
            found[m["h"].strip().lower()] = text
    return found
