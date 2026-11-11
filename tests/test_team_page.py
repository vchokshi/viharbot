"""The 'Meet the team' page: every agent has a bio, the link works, and the page agrees with the
real agent registry and bindings. The script-driven tests run in Node and skip without it."""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest

from viharbot.execution_bindings import IN_PROCESS_CAPABILITIES
from fastapi.testclient import TestClient

from viharbot import web_app
from viharbot.execution_bindings import default_bindings
from viharbot.identity_registry import default_registry
from viharbot.team_profiles import render_profiles, render_sidebar, team_profiles

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "src" / "viharbot" / "static" / "index.html"


def rendered_page() -> str:
    """The page as the server sends it: the shared profile source fills both placeholders."""
    profiles = team_profiles()
    return (INDEX.read_text(encoding="utf-8")
            .replace(web_app.SIDEBAR_MARKER, render_sidebar(profiles))
            .replace(web_app.PROFILES_MARKER, render_profiles(profiles)))


HARNESS = Path(__file__).resolve().parent / "js" / "run_page.mjs"
PAGE = rendered_page()
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

EXTERNAL_DISPLAY = {"Professor Archibald": "archie", "Dr. Ada Lovewell": "ada"}
DISPLAY_TO_ID = {"Charlie Chatwin": "charlie", "Nova": "nova", "Atlas": "atlas", "Pixel": "pixel",
                 "Sage": "sage", "Cipher": "cipher", "Scout": "scout", "Orbit": "orbit",
                 "Kepler": "kepler"}
CAPABILITY_WORD = {"research": "Research", "fact_check": "Fact Check", "source_quality": "Sources",
                   "security_assessment": "Security", "privacy_review": "Privacy",
                   "synthesis": "Synthesis", "slide_generation": "Slide Decks",
                   "philosophical_analysis": "Perspective"}


def cards() -> dict[str, dict[str, str]]:
    found = {}
    for block in re.findall(r'<article class="profile-card"[^>]*>(.*?)</article>', PAGE, re.S):
        name = html.unescape(re.search(r"<h3[^>]*>(.*?)</h3>", block).group(1))
        found[name] = {
            "status": html.unescape(re.search(r'<span class="profile-status[^"]*">(.*?)</span>', block).group(1)),
            "title": html.unescape(re.search(r'<p class="profile-title">(.*?)</p>', block).group(1)),
            "bio": html.unescape(re.search(r'<p class="profile-bio">(.*?)</p>', block).group(1)),
            "skills": re.findall(r"<li>(.*?)</li>", block),
            "image": (lambda m: m.groups() if m else (None, None))(
                re.search(r'<img class="profile-portrait" src="([^"]+)" alt="([^"]*)"', block)),
        }
    return found


CARDS = cards()


# every agent has a complete bio
PORTRAIT_FILE = {"charlie": "Charlie_Chatwin_headshot.png", "kepler": "Kepler, Director of Analytics & Foresight.png"}


def test_every_registered_identity_has_a_profile_card_with_a_bio_and_a_real_portrait():
    registry = default_registry()
    assert len(registry) == 9 and len(CARDS) == 11  # nine identities plus two external leads
    for display, identity_id in DISPLAY_TO_ID.items():
        assert display in CARDS, display
        card = CARDS[display]
        assert card["title"] == registry.get(identity_id).title  # one source of truth for titles
        assert len(card["bio"]) >= 60 and card["bio"].endswith(".")
        assert len(card["skills"]) >= 3
        expected = PORTRAIT_FILE.get(identity_id, f"{display}_headshot.png")
        assert urllib.parse.unquote(card["image"][0]) == f"/images/{expected}"
        assert card["image"][1].strip()  # meaningful alt text


def test_no_stale_roster_claims_or_proposed_kepler_coding_agent_remain():
    for stale in ("Eight distinct personalities", "Eight minds", "Proposed: Kepler", "Beacon", "Echo",
                  "proposed coding agent", "draft pull request for Sage"):
        assert stale not in PAGE, stale
    assert "Historical artwork" in PAGE  # the old eight-character grid is labelled, not the roster


def test_kepler_matches_the_steward_mission_and_is_not_called_live():
    card = CARDS["Kepler"]
    assert card["status"] == "Deployed on AWS" and "Live" not in card["status"]
    text = card["bio"].lower()
    for need in ("first-contributor steward", "agentcore", "cla", "alerts the founder"):
        assert need in text, need
    assert "coding" not in text
    block = re.search(r'<article class="profile-card" id="team-kepler".*?</article>', PAGE, re.S).group(0)
    assert "does not write code" in block and "does not run in the chat" in block
    assert default_registry().get("kepler").description.startswith("Governed first-contributor steward")


def test_bios_do_not_claim_abilities_the_agents_do_not_have():
    text = " ".join(c["bio"] for c in CARDS.values()).lower()
    for claim in ("browses the web", "executes commands", "has access to your", "verified externally",
                  "independently verified"):
        assert claim not in text


# status chips agree with the real bindings
def test_status_chips_match_which_agents_are_actually_live():
    bound: dict[str, set[str]] = {}
    for binding in default_bindings().bindings:
        if binding.capability in IN_PROCESS_CAPABILITIES:  # a repository workflow, not a chat role
            continue
        bound.setdefault(binding.identity_id, set()).add(binding.capability.value)
    for display, identity_id in DISPLAY_TO_ID.items():
        status = CARDS[display]["status"]
        if identity_id in bound:
            assert status.startswith("Live"), (display, status)
            for capability in bound[identity_id]:  # every live role is named
                assert CAPABILITY_WORD[capability].lower() in status.lower(), (display, capability, status)
        else:
            assert "Live" not in status, (display, status)  # no live role, so never "Live"


def test_the_identities_without_a_live_role_say_planned_support_or_deployed():
    assert CARDS["Atlas"]["status"] == "Planned"
    assert CARDS["Orbit"]["status"] == "Platform support"
    assert CARDS["Kepler"]["status"] == "Deployed on AWS"


# the page structure and the link
def test_the_link_page_and_back_link_are_wired_up_in_the_markup():
    assert re.search(r'<a class="team-link" id="team-link" href="#team">', PAGE)
    assert re.search(r'<main class="team-page" id="team-page" hidden>', PAGE)  # hidden until chosen
    assert 'id="chat-page"' in PAGE and 'class="back-chat" href="#home"' in PAGE
    assert re.search(r'<a class="brand" href="#home"', PAGE)
    page = re.search(r'<main class="team-page".*?</main>', PAGE, re.S).group(0)
    assert page.count("<h1>") == 1 and "Meet the ViharBot" in page
    assert 'aria-label="ViharBot agent profiles"' in page


def test_the_page_script_registers_the_router_and_uses_no_markup_strings():
    script = re.search(r"<script>\n(.*?)</script>", PAGE, re.S).group(1)
    assert 'addEventListener("hashchange"' in script and "showPage(true)" in script
    assert "innerHTML" not in script


# images: every one the page requests is allowed and served; nothing else is. The files live in
# the public image store, not in git, so the route tests use stand-in files.
def requested_images() -> list[str]:
    return sorted({urllib.parse.unquote(m) for m in re.findall(r'src="/images/([^"]+)"', PAGE)})


def test_every_requested_image_is_allowlisted():
    images = requested_images()
    assert "AI Robot Team Portrait Grid.png" in images and len(images) >= 9
    for name in images:
        assert name in web_app.TEAM_IMAGES, name


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name in web_app.TEAM_IMAGES:
        (tmp_path / name).write_bytes(b"stand-in")
    monkeypatch.setenv("WEB_AUTH_ENABLED", "false")
    monkeypatch.setenv("WEB_IMAGES_DIR", str(tmp_path))
    return TestClient(web_app.app)


def test_the_images_route_serves_the_page_images_including_the_portrait(client):
    for name in requested_images():
        reply = client.get("/images/" + urllib.parse.quote(name))
        assert reply.status_code == 200 and reply.headers["content-type"] == "image/png", name


def test_the_images_route_still_refuses_everything_off_the_allowlist(client):
    for name in ("Meet the Viharbot team.rtf", "../pyproject.toml", "..%2Fpyproject.toml",
                 "%2e%2e/compose.yaml", "unknown.png", ".DS_Store"):
        assert client.get("/images/" + name).status_code == 404, name
    for name in web_app.TEAM_IMAGES:
        assert "/" not in name and not name.startswith(".")


def test_the_images_route_still_requires_authentication(client, monkeypatch):
    monkeypatch.setenv("WEB_AUTH_ENABLED", "true")
    monkeypatch.setenv("WEB_USERNAME", "u")
    monkeypatch.setenv("WEB_PASSWORD", "p")
    assert client.get("/images/Nova_headshot.png").status_code == 401
    assert client.get("/images/Nova_headshot.png", auth=("u", "p")).status_code == 200


# the router, running the page's own script
def run_page(scenario: dict) -> dict:
    done = subprocess.run([NODE, str(HARNESS), str(INDEX)], input=json.dumps(scenario),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_the_chat_is_shown_by_default_and_focus_is_not_stolen_on_load():
    page = run_page({"skipSubmit": True})
    assert page["hasHashchangeListener"]
    initial = page["initialPage"]
    assert initial["chatHidden"] is False and initial["teamHidden"] is True
    assert initial["linkCurrent"] is None and initial["title"].startswith("ViharBot")
    assert initial["focusedTag"] is None


@needs_node
def test_the_meet_the_team_link_shows_the_team_page_and_hides_the_chat():
    page = run_page({"skipSubmit": True, "navigate": ["#team"]})
    team = page["navigation"][0]
    assert (team["chatHidden"], team["teamHidden"]) == (True, False)
    assert team["linkCurrent"] == "page" and team["title"] == "Meet the team · ViharBot"
    assert team["focusedIsTeamHeading"] and team["teamHeadingTabindex"] == "-1"
    assert page["scrolls"][-1] == [0, 0]


@needs_node
def test_a_deep_link_opens_the_team_page_directly():
    initial = run_page({"skipSubmit": True, "initialHash": "#team"})["initialPage"]
    assert (initial["chatHidden"], initial["teamHidden"]) == (True, False)
    assert initial["focusedIsTeamHeading"] and initial["linkCurrent"] == "page"


@needs_node
@pytest.mark.parametrize("hash_value", ["#home", "", "#", "#nonsense", "#TEAM", "#team/extra"])
def test_the_back_link_and_any_other_hash_return_to_the_chat(hash_value):
    page = run_page({"skipSubmit": True, "initialHash": "#team", "navigate": [hash_value]})
    back = page["navigation"][0]
    assert (back["chatHidden"], back["teamHidden"]) == (False, True)
    assert back["linkCurrent"] is None and back["title"].startswith("ViharBot")
    assert back["focusedIsInput"]  # focus returns to the message box


@needs_node
def test_going_back_and_forth_keeps_the_conversation_and_the_unsent_draft():
    body = {"request_id": "r", "agent_activity": [],
            "findings": {"summary": "Kept summary", "findings": [], "sources": [], "uncertainties": []},
            "assessment": {"overall_risk": "low", "implications": [], "recommendations": [],
                           "uncertainties": []}}
    page = run_page({"message": "first question", "response": {"status": 200, "body": body},
                     "navigate": ["#team", "#home", "#team", "#home"],
                     "draftBeforeNavigate": "half-typed follow-up"})
    counts = {n["conversationChildren"] for n in page["navigation"]}
    assert len(counts) == 1 and counts.pop() >= 2  # the user message and the answer stay in the DOM
    assert all(n["draft"] == "half-typed follow-up" for n in page["navigation"])
    assert "Kept summary" in json.dumps(page["final"]["tree"])


@needs_node
def test_the_team_page_does_not_interfere_with_sending_a_message():
    body = {"request_id": "r", "agent_activity": [],
            "findings": {"summary": "Answer", "findings": [], "sources": [], "uncertainties": []},
            "assessment": {"overall_risk": "low", "implications": [], "recommendations": [],
                           "uncertainties": []}}
    page = run_page({"message": "hi", "response": {"status": 200, "body": body}, "initialHash": "#team"})
    assert page["final"]["sendDisabled"] is False and page["fetchCalls"][0]["url"] == "/api/chat"


# three applications on one platform
def application_sections() -> dict[str, str]:
    return {html.unescape(m[1]): m[2] for m in re.findall(
        r'<section class="app-group" id="(app-[\w-]+)"[^>]*><header class="app-head"><h2[^>]*>(.*?)</h2>'
        r'(.*?)</section>', PAGE, re.S)}


def test_the_team_page_is_framed_as_three_applications_on_one_platform():
    sections = application_sections()
    assert list(sections) == ["Super Consulting", "Knowledge and Learning", "Training and Instruction"]
    assert "agentic-AI platform" in PAGE and "led by Ada" in PAGE


def test_every_card_sits_in_exactly_one_application():
    platform_ids = {"Kepler", "Orbit"}  # these two sit in the platform group, checked below
    names = {n: [app for app, body in application_sections().items() if f"<h3 id=\"team-{i}-name\">" in body]
             for n, i in {**DISPLAY_TO_ID, **EXTERNAL_DISPLAY}.items() if n not in platform_ids}
    assert all(len(apps) == 1 for apps in names.values()), names
    assert names["Charlie Chatwin"] == names["Atlas"] == ["Super Consulting"]
    platform = re.search(r'<section class="app-group platform-group".*?</section>', PAGE, re.S).group(0)
    assert "ViharBot Platform" in platform
    assert 'id="team-kepler"' in platform and 'id="team-orbit"' in platform
    assert 'id="team-atlas"' not in platform
    assert names["Professor Archibald"] == ["Knowledge and Learning"]
    assert names["Dr. Ada Lovewell"] == ["Training and Instruction"]


def test_archie_and_ada_are_external_applications_never_live_or_registry_identities():
    registry = default_registry()
    for display, identity_id in EXTERNAL_DISPLAY.items():
        assert identity_id not in {i.id for i in registry.list()}  # #293: not ViharBot identities
        card = CARDS[display]
        assert card["status"] == "Separate application" and "Live" not in card["status"]
        assert len(card["bio"]) >= 60 and len(card["skills"]) >= 3
        assert card["image"][0] == "/images/Professor_and_Assistant.png" and card["image"][1].strip()
    assert "Professor_and_Assistant.png" in web_app.TEAM_IMAGES
    assert "ViharBot does not run it" in PAGE
