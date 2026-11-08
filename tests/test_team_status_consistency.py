"""The sidebar, header and footer must agree with the real registry and execution bindings, and the
sidebar and the Meet the team page must come from one profile source (epic #26)."""

from __future__ import annotations

import html
import re
from pathlib import Path

from viharbot import web_app
from viharbot.execution_bindings import default_bindings
from viharbot.identity_registry import default_registry
from viharbot.team_profiles import render_profiles, render_sidebar, team_profiles

INDEX = Path(__file__).resolve().parent.parent / "src" / "viharbot" / "static" / "index.html"
RAW = INDEX.read_text()
PROFILES = team_profiles()
HTML = RAW.replace(web_app.SIDEBAR_MARKER, render_sidebar(PROFILES)).replace(
    web_app.PROFILES_MARKER, render_profiles(PROFILES))
SIDEBAR = HTML[HTML.index('<section class="team-group"'):HTML.index('class="sidebar-foot"')]


def chat_identities():
    return {"nova", "scout", "cipher", "charlie", "pixel", "sage"}


def sidebar_entries():
    """identity id -> (display name, status text), read from the rendered sidebar."""
    return {m[0]: (html.unescape(m[1]), html.unescape(m[2])) for m in re.findall(
        r'<a class="agent-name" href="#team/(\w+)">(.*?) <small class="status-\w+">([^<]*)</small>', SIDEBAR)}


def test_the_sidebar_lists_all_eleven_identities_once_each():
    entries = sidebar_entries()
    assert set(entries) == {i.id for i in default_registry().list()} and len(entries) == 11
    assert SIDEBAR.count('class="agent"') == 11


def test_exactly_the_bound_chat_identities_are_marked_live():
    marked = {i for i, (_, status) in sidebar_entries().items() if status.startswith("Live")}
    bound = {b.identity_id for b in default_bindings().bindings
             if b.enabled and b.capability.value != "code_review"}
    assert marked == bound == chat_identities()


def test_unbound_identities_are_never_marked_live():
    for identity_id, (_, status) in sidebar_entries().items():
        if identity_id not in chat_identities():
            assert not status.startswith("Live"), identity_id


def test_counts_match_the_bindings():
    live = len(chat_identities())
    words = {4: "Four", 5: "Five", 6: "Six"}
    assert f"{live} live · 11 in the crew" in HTML
    assert f"<em>{live} agents</em>" in HTML and f"<em>5 identities</em>" in HTML
    assert f"{words[live]} live agents" in HTML and f"{words[live]} already work together" in HTML


def test_the_live_status_text_names_the_real_capabilities():
    entries = sidebar_entries()
    assert "fact check" in entries["scout"][1] and "sources" in entries["scout"][1]
    assert "security" in entries["cipher"][1] and "privacy" in entries["cipher"][1]


# one source: the sidebar and the Team page cannot disagree
def test_each_sidebar_name_deep_links_to_the_matching_team_profile():
    for identity_id in sidebar_entries():
        assert f'href="#team/{identity_id}"' in SIDEBAR
        assert f'<article class="profile-card" id="team-{identity_id}"' in HTML  # the anchor it targets
    anchors = re.findall(r'<article class="profile-card" id="(team-\w+)"', HTML)
    assert len(anchors) == len(set(anchors)) == 11


def test_sidebar_and_profile_show_the_same_name_title_and_status():
    for p in PROFILES:
        assert html.escape(p.title) in SIDEBAR and html.escape(p.title) in HTML
        card = re.search(rf'<article class="profile-card" id="team-{p.id}".*?</article>', HTML, re.S).group(0)
        assert html.escape(p.status) in card and html.escape(p.status) in SIDEBAR
        assert p.image_url in card and p.image_url in SIDEBAR


def test_every_sidebar_entry_is_expandable_and_keyboard_reachable():
    assert SIDEBAR.count('<details class="agent-more">') == 11
    assert SIDEBAR.count('class="agent-profile-link"') == 11
    assert "tabindex" not in SIDEBAR  # native links and summaries, no custom tab order


def test_profile_copy_is_escaped_when_rendered():
    p = PROFILES[0]
    evil = type(p)(**{**p.__dict__, "name": "<img src=x onerror=alert(1)>", "bio": "<script>x</script>"})
    out = render_sidebar((evil,)) + render_profiles((evil,))
    assert "<script>" not in out and "<img src=x" not in out and "&lt;script&gt;" in out


def test_the_raw_template_keeps_markers_and_the_served_page_fills_them(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    assert web_app.SIDEBAR_MARKER in RAW and web_app.PROFILES_MARKER in RAW
    served = TestClient(web_app.app).get("/").text
    assert web_app.SIDEBAR_MARKER not in served and web_app.PROFILES_MARKER not in served
    assert 'id="team-kepler"' in served and "href=\"#team/kepler\"" in served


def suggestion_prompts():
    return [re.sub(r"<[^>]+>", "", m) for m in re.findall(r'class="suggestion"[^>]*><b>[^<]*</b><span>(.*?)</span>', HTML)]


def test_suggested_prompts_fit_the_input_and_name_every_agents_work():
    prompts = suggestion_prompts()
    assert len(prompts) == 4 and all(0 < len(p) <= 3000 for p in prompts)
    full = prompts[0].lower()
    # The first prompt asks, in plain words, for every kind of review the team can run.
    for need in ("research", "fact-check", "source", "security", "privacy", "synthesized"):
        assert need in full, need


def test_together_the_prompts_ask_for_each_live_capability():
    text = " ".join(suggestion_prompts()).lower()
    for need in ("research", "fact-check", "reliable", "security", "privacy", "synthesi"):
        assert need in text, need
