"""The sidebar, header and footer must agree with the real registry and execution bindings."""

from __future__ import annotations

import re
from pathlib import Path

from viharbot.execution_bindings import default_bindings

INDEX = Path(__file__).resolve().parent.parent / "src" / "viharbot" / "static" / "index.html"
HTML = INDEX.read_text()
SIDEBAR = HTML[HTML.index('<section class="team-group"'):HTML.index('class="sidebar-foot"')]


def live_identities():
    return {b.identity_id for b in default_bindings().bindings if b.enabled}


def sidebar_entries():
    return dict(re.findall(
        r'<span class="agent-name">(\w+)(?: Chatwin)? <small>([^<]*)</small>', SIDEBAR))


def test_exactly_the_bound_identities_are_marked_live():
    marked = {name.lower() for name, status in sidebar_entries().items() if status.startswith("Live")}
    assert marked == live_identities()


def test_unbound_teammates_are_never_marked_live():
    for name, status in sidebar_entries().items():
        if name.lower() not in live_identities():
            assert not status.startswith("Live"), name


def test_counts_match_the_bindings():
    live = len(live_identities())
    words = {4: "Four"}
    assert f"{live} live · 10 in the crew" in HTML
    assert f"<em>{live} agents</em>" in HTML
    assert f"{words[live]} live agents" in HTML and f"{words[live]} already work together" in HTML


def test_the_live_status_text_names_the_real_capabilities():
    entries = sidebar_entries()
    assert "fact check" in entries["Scout"] and "sources" in entries["Scout"]
    assert "security" in entries["Cipher"] and "privacy" in entries["Cipher"]
