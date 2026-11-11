"""One source for how the website presents every established agent identity (epic #26).

The sidebar and the Meet the team page are both rendered from ``team_profiles()``, so names,
roles, portraits, skills and status labels cannot drift apart. Names, titles, personality and
expertise come from the identity registry. The public biography, the portrait and the plain
"what this is not" note live here, as proposed website copy for founder review.

Status is **derived from the approved execution bindings**, never from the existence of an
identity file, a portrait or a service name. An identity with no chat binding is never shown as
live. Visible identity is not operational capability, and this module grants no permission.

Everything is escaped with ``html.escape`` before it is placed in markup.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from urllib.parse import quote

from .execution_bindings import IN_PROCESS_CAPABILITIES, default_bindings
from .identity_registry import default_registry

# Words shown for each live chat role. A capability missing here is not shown as a live role.
CAPABILITY_WORD = {
    "research": "Research",
    "fact_check": "Fact check",
    "source_quality": "Sources",
    "security_assessment": "Security",
    "privacy_review": "Privacy",
    "synthesis": "Synthesis",
    "slide_generation": "Slide decks",
    "philosophical_analysis": "Perspective",
}

# Not a runtime fact: how an identity without a chat binding is described. The kind drives the
# colour only; the words are what a reader sees.
NOT_LIVE_STATUS = {
    "atlas": ("planned", "Planned"),
    "orbit": ("support", "Platform support"),
    "kepler": ("development", "Deployed on AWS"),
}

# Archie and Ada are separate applications (issue #293): they are not in the identity registry, run
# nothing here, and appear only as the leads of applications two and three.
EXTERNAL_STATUS = ("external", "Separate application")
EXTERNAL_IDS = ("archie", "ada")

# Roster order on the page: live chat roles first, then everyone else.
ORDER = ("charlie", "nova", "scout", "cipher", "pixel", "sage",
         "atlas", "orbit", "kepler")


@dataclass(frozen=True)
class Portrait:
    file: str
    alt: str
    position: str = ""  # CSS object-position, for a shared image


KEPLER_PORTRAIT = "Kepler, Director of Analytics & Foresight.png"
SHARED_LEARNING_PORTRAIT = "Professor_and_Assistant.png"

PORTRAITS = {
    "charlie": Portrait("Charlie_Chatwin_headshot.png", "Charlie Chatwin portrait"),
    "nova": Portrait("Nova_headshot.png", "Nova portrait"),
    "scout": Portrait("Scout_headshot.png", "Scout portrait"),
    "cipher": Portrait("Cipher_headshot.png", "Cipher portrait"),
    "pixel": Portrait("Pixel_headshot.png", "Pixel portrait"),
    "sage": Portrait("Sage_headshot.png", "Sage portrait"),
    "atlas": Portrait("Atlas_headshot.png", "Atlas portrait"),
    "orbit": Portrait("Orbit_headshot.png", "Orbit portrait"),
    "kepler": Portrait(KEPLER_PORTRAIT, "Kepler portrait: a robed robot beside a telescope and "
                                        "an orbit chart, wearing a First-Contributor Steward badge"),
    "archie": Portrait(SHARED_LEARNING_PORTRAIT, "Professor Archibald, from the shared portrait "
                                                 "of Archie and Ada", "0% 20%"),
    "ada": Portrait(SHARED_LEARNING_PORTRAIT, "Dr. Ada Lovewell, from the shared portrait "
                                              "of Archie and Ada", "100% 20%"),
}

# Proposed public copy (reviewed by the founder before it is final). ``short`` is the one-line
# role summary, ``bio`` the full biography, ``note`` what the profile does NOT establish.
COPY = {
    "charlie": (
        "Strategy and orchestration",
        "The team's thoughtful coordinator, Charlie brings enterprise technology, security and AI "
        "architecture into a coherent plan. He weighs competing evidence, explains trade-offs and "
        "synthesizes recommendations.",
        "Charlie can propose specialist collaboration, but the application, not Charlie, "
        "authorizes and executes it."),
    "nova": (
        "Research and knowledge",
        "Nova follows difficult questions across science, history and technology with curiosity "
        "and methodical reasoning. Her focus is organizing findings, distinguishing evidence from "
        "assumptions, and making complex subjects understandable.",
        "Research capability does not imply independent live-web retrieval."),
    "scout": (
        "Evidence and source checking",
        "Scout is the team's evidence detective, searching for gaps, contradictions and weak "
        "assumptions in available material. Scout focuses on source quality, fact checking and "
        "pattern recognition, while separating supplied evidence from unverified claims.",
        "Scout checks the evidence supplied to the team; it is not an independent external "
        "verification service."),
    "cipher": (
        "Security and privacy review",
        "Cipher examines systems with a disciplined, evidence-first security mindset. From "
        "identity and least privilege to DSPM and agentic AI risk, Cipher makes threats, "
        "assumptions and mitigations explicit.",
        "A profile is not proof of live scanning access."),
    "pixel": (
        "Slide decks on request",
        "Pixel translates ideas into stories and presentations that people can understand. A "
        "design-minded creative partner, Pixel emphasizes message, structure and visual clarity.",
        "Structured slide-deck generation is supported. Image generation and multimodal "
        "processing are not established."),
    "sage": (
        "Perspective and human stakes",
        "Sage asks what a decision means for people, not just whether it works technically. "
        "Drawing on ethics, philosophy and human behavior, Sage surfaces consequences and "
        "constructive alternatives.",
        "Sage's perspective is advisory, not an independent approval or veto."),
    "atlas": (
        "Exploration and logistics",
        "Atlas turns ambitious outdoor ideas into practical plans. From overlanding routes and "
        "navigation to equipment, preparedness and logistics, Atlas looks for the workable path "
        "and a sensible fallback.",
        "Atlas does not run in the chat today. Booking and purchasing are not connected "
        "capabilities."),
    "orbit": (
        "Automation and engineering",
        "Orbit approaches engineering as a governed system: clear interfaces, reproducible "
        "workflows and explicit failure handling. Its mission spans automation, cloud "
        "infrastructure and DevOps.",
        "Orbit does not run in the chat today. An AWS read-only v0 is specified in issue #269; it "
        "is not deployed and has no infrastructure privileges."),
    "kepler": (
        "First-contributor steward",
        "Kepler is a governed first-contributor steward deployed on AWS AgentCore. It "
        "monitors first-time contributor pull-request onboarding and status, checks CLA, "
        "third-party rights, commit identity, branch, CI and review evidence, prepares "
        "approved-template follow-up drafts, and alerts the founder.",
        "Kepler does not write code, push branches, merge, deploy, change permissions or post "
        "contributor-facing messages without approval, and it does not run in the chat. It runs "
        "in the AWS lab account on a six-hourly schedule, reads GitHub read-only and emails the "
        "founder. See issue #198."),
}


# Archie and Ada have no registry identity here, so their title, personality and skills live in
# this module as website copy. The applications themselves are developed in their own repositories.
EXTERNAL = {
    "archie": dict(
        name="Professor Archibald",
        title="Chief Knowledge & Learning Officer",
        personality="Scholarly but approachable, patient, methodical, intellectually curious, "
                    "and honest about uncertainty.",
        skills=("Computer science education", "Distributed systems", "AI systems",
                "Software architecture", "Engineering history", "Technical explanation"),
        short="Engineering history and knowledge",
        bio="Professor Archibald is the patient technical historian behind the Knowledge and "
            "Learning application. He turns verified engineering decisions into clear "
            "explanations and instructor material, connecting how systems work with why they "
            "were built that way.",
        note="Archie is a separate application in its own repository. It reads ViharBot's merged "
             "pull requests over GitHub as untrusted data; ViharBot does not run it."),
    "ada": dict(
        name="Dr. Ada Lovewell",
        title="Director of Curriculum & Learning Experience",
        personality="Energetic, highly organized, encouraging, practical, detail-oriented, and "
                    "creative.",
        skills=("Curriculum design", "Instructional sequencing", "Exercise and assessment design",
                "Learning progression", "Technical onboarding"),
        short="Training and instruction",
        bio="Dr. Ada Lovewell leads the Training and Instruction application. She turns approved "
            "technical knowledge into practical learning journeys, designing lessons, exercises "
            "and assessments that help learners demonstrate understanding by doing.",
        note="Ada is a separate application in its own repository. ViharBot does not run it and "
             "grants it no student-record or execution access."),
}


@dataclass(frozen=True)
class Application:
    id: str
    name: str
    tagline: str
    summary: str
    lead: str  # the identity that leads or anchors the application
    members: tuple[str, ...]
    status: str  # a plain statement of what exists today


# The ViharBot agentic-AI platform hosts three applications. Membership is website framing, not an
# authorization: an identity here does not gain any capability from belonging to an application.
APPLICATIONS = (
    Application(
        "super-consulting", "Super Consulting", "AI security consulting",
        "A consulting crew for AI security and the data and decisions around it. The team "
        "researches, checks evidence, reviews security and privacy, and synthesizes a "
        "recommendation.",
        "charlie",
        ("charlie", "nova", "scout", "cipher", "pixel", "sage", "atlas"),
        "Six roles run in the live chat today. Atlas is planned and does not run."),
    Application(
        "knowledge-learning", "Knowledge and Learning", "Engineering knowledge, kept and explained",
        "Keeps the history and reasoning behind the platform's engineering and turns it into "
        "instructor material.",
        "archie", ("archie",),
        "A separate application in its own repository, not run by ViharBot."),
    Application(
        "training-instruction", "Training and Instruction", "Learning journeys led by Ada",
        "Turns approved knowledge into lessons, exercises and assessments, led by Dr. Ada "
        "Lovewell.",
        "ada", ("ada",),
        "A separate application in its own repository, not run by ViharBot."),
)


# Identities that look after the platform itself rather than any one application.
PLATFORM = Application(
    "platform", "ViharBot Platform", "Shared agents that support every application",
    "Agents that look after the platform itself: Kepler stewards first-time contributors to the "
    "project, and Orbit covers automation and cloud engineering.",
    "kepler", ("kepler", "orbit"),
    "Kepler runs on a schedule in AWS and is not in the chat. Orbit is platform support and "
    "does not run.")


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    title: str
    short: str
    bio: str
    note: str
    personality: str
    skills: tuple[str, ...]
    status_kind: str  # live | support | planned | development
    status: str
    portrait: Portrait

    @property
    def live(self) -> bool:
        return self.status_kind == "live"

    @property
    def anchor(self) -> str:
        return f"team-{self.id}"

    @property
    def image_url(self) -> str:
        return "/images/" + quote(self.portrait.file, safe="")


def _live_roles() -> dict[str, list[str]]:
    """The chat roles each identity really has, from the enabled execution bindings."""
    roles: dict[str, list[str]] = {}
    for binding in default_bindings().bindings:
        if not binding.enabled or binding.capability in IN_PROCESS_CAPABILITIES:
            continue
        word = CAPABILITY_WORD.get(binding.capability.value)
        if word:
            roles.setdefault(binding.identity_id, []).append(word)
    return roles


def team_profiles() -> tuple[Profile, ...]:
    registry = default_registry()
    roles = _live_roles()
    profiles = []
    for identity_id in ORDER:
        identity = registry.get(identity_id)
        short, bio, note = COPY[identity_id]
        if identity_id in roles:
            kind, status = "live", "Live · " + " & ".join(w.lower() for w in roles[identity_id])
        else:
            kind, status = NOT_LIVE_STATUS[identity_id]
        profiles.append(Profile(
            id=identity_id, name=identity.name, title=identity.title, short=short, bio=bio,
            note=note, personality=identity.personality, skills=tuple(identity.expertise),
            status_kind=kind, status=status, portrait=PORTRAITS[identity_id]))
    # Every registered identity must have copy and a portrait, so a new one cannot be skipped.
    missing = {i.id for i in registry.list()} - set(ORDER)
    if missing:
        raise RuntimeError(f"identities without website profiles: {sorted(missing)}")
    for identity_id in EXTERNAL_IDS:
        data = EXTERNAL[identity_id]
        kind, status = EXTERNAL_STATUS
        profiles.append(Profile(
            id=identity_id, name=data["name"], title=data["title"], short=data["short"],
            bio=data["bio"], note=data["note"], personality=data["personality"],
            skills=data["skills"], status_kind=kind, status=status,
            portrait=PORTRAITS[identity_id]))
    return tuple(profiles)


def portrait_files() -> frozenset[str]:
    return frozenset(p.file for p in PORTRAITS.values())


def _img(profile: Profile, css: str, decorative: bool = False) -> str:
    alt = "" if decorative else profile.portrait.alt
    style = f' style="object-position: {escape(profile.portrait.position)}"' if profile.portrait.position else ""
    return (f'<img class="{css}" src="{escape(profile.image_url)}" '
            f'alt="{escape(alt)}"{style} />')


def render_sidebar(profiles: tuple[Profile, ...]) -> str:
    live = [p for p in profiles if p.live]
    external = [p for p in profiles if p.status_kind == "external"]
    others = [p for p in profiles if not p.live and p.status_kind != "external"]

    def agent(p: Profile) -> str:
        skills = " · ".join(p.skills)
        return (
            f'<div class="agent" id="agent-{escape(p.id)}">'
            f'<div class="agent-row">{_img(p, "agent-photo", True)}'
            f'<span class="agent-copy"><a class="agent-name" href="#team/{escape(p.id)}">'
            f'{escape(p.name)} <small class="status-{p.status_kind}">{escape(p.status)}</small></a>'
            f'<span class="agent-desc">{escape(p.short)}</span></span></div>'
            f'<details class="agent-more"><summary>More about {escape(p.name)}</summary>'
            f'<p class="agent-role">{escape(p.title)}</p>'
            f'<p>{escape(p.bio)}</p>'
            f'<p class="agent-skills"><strong>Skills:</strong> {escape(skills)}</p>'
            f'<p class="agent-traits"><strong>Personality:</strong> {escape(p.personality)}</p>'
            f'<p class="agent-note">{escape(p.note)}</p>'
            f'<a class="agent-profile-link" href="#team/{escape(p.id)}">Full profile '
            f'<span aria-hidden="true">↗</span></a></details></div>')

    def group(label: str, count_text: str, members: list[Profile]) -> str:
        return (f'<section class="team-group" aria-label="{escape(label)}">'
                f'<div class="team-group-title"><span>{escape(label)}</span>'
                f'<em>{escape(count_text)}</em></div><div class="agent-list">'
                + "".join(agent(p) for p in members) + "</div></section>")

    return (group("In the live chat", f"{len(live)} agents", live)
            + group("Established, not in the chat", f"{len(others)} identities", others)
            + group("Separate applications", f"{len(external)} leads", external))


def render_profiles(profiles: tuple[Profile, ...]) -> str:
    def card(p: Profile) -> str:
        skills = "".join(f"<li>{escape(s)}</li>" for s in p.skills)
        return (
            f'<article class="profile-card" id="{escape(p.anchor)}" tabindex="-1" '
            f'aria-labelledby="{escape(p.anchor)}-name">{_img(p, "profile-portrait")}'
            f'<div class="profile-info"><div class="profile-name-line">'
            f'<h3 id="{escape(p.anchor)}-name">{escape(p.name)}</h3>'
            f'<span class="profile-status {p.status_kind}">{escape(p.status)}</span></div>'
            f'<p class="profile-title">{escape(p.title)}</p>'
            f'<p class="profile-bio">{escape(p.bio)}</p>'
            f'<details class="profile-more"><summary>Skills, personality and limits</summary>'
            f'<p><strong>Personality:</strong> {escape(p.personality)}</p>'
            f'<ul class="profile-skills" aria-label="Skills">{skills}</ul>'
            f'<p class="profile-note"><strong>Worth knowing:</strong> {escape(p.note)}</p>'
            f'</details></div></article>')

    by_id = {p.id: p for p in profiles}
    sections = []
    for app in (*APPLICATIONS, PLATFORM):
        css = "app-group" if app in APPLICATIONS else "app-group platform-group"
        members = [by_id[i] for i in app.members if i in by_id]
        count = f"{len(members)} {'identity' if len(members) == 1 else 'identities'}"
        sections.append(
            f'<section class="{css}" id="app-{escape(app.id)}" '
            f'aria-labelledby="app-{escape(app.id)}-name">'
            f'<header class="app-head"><h2 id="app-{escape(app.id)}-name">{escape(app.name)}</h2>'
            f'<p class="app-tagline">{escape(app.tagline)} · {escape(count)}</p>'
            f'<p class="app-summary">{escape(app.summary)}</p>'
            f'<p class="app-status"><strong>Today:</strong> {escape(app.status)}</p></header>'
            f'<div class="app-members">' + "".join(card(p) for p in members) + "</div></section>")
    return "".join(sections)
