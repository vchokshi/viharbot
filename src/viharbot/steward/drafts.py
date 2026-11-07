"""Draft a respectful follow-up for the founder to review. Nothing is posted by this module.

The wording is fixed templates filled only with values the code controls: the contributor's GitHub
login, the legal-terms version and the exact acceptance statement from the CLA gate. No model is
used and nothing a contributor wrote is copied in, so a hostile pull request or comment cannot
steer a draft.

It never repeats a request that is still fresh: if the founder already asked for the CLA, no
draft is produced until the reminder window has passed, and a reminder lists only what is still
outstanding. If the contributor has already replied, there is nothing to ask: a human reads it.

Every message says plainly that it comes from an AI agent, so a contributor is never left to wonder
whether they are talking to a person. The first message also introduces Kepler as the first of
ViharBot's agents, and says what it can and cannot do. It also says truthfully who posts it: today
the maintainer reads the draft and posts it, so it says "drafted by me and posted by @maintainer";
a later phase in which Kepler posts approved messages itself says that instead. It never implies a
human wrote what an agent wrote, or that an agent posted what a human posted.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Literal

from .assess import REMINDER_AFTER_HOURS, _hours_since
from .cla import ClaAssessment, load_gate
from .model import OUTSTANDING, PullAssessment, safe_login


@dataclass(frozen=True)
class DraftDecision:
    reason: str  # why there is, or is not, a draft
    kind: str | None = None  # welcome or reminder
    body: str | None = None
    items: tuple[str, ...] = ()


def _outstanding(assessment: PullAssessment, key: str) -> bool:
    return assessment.requirement(key).status == OUTSTANDING


def _who_posts(maintainer: str | None, posting: str) -> str:
    person = f"@{maintainer}" if maintainer else "the project maintainer"
    if posting == "agent":
        return f"This message was approved by {person} and posted by me."
    return f"This message was drafted by me and reviewed and posted by {person}."


def _introduction(maintainer: str | None, posting: str) -> str:
    return (
        "A quick introduction: I'm Kepler, an AI agent, not a person. I'm the first of ViharBot's "
        "agents you'll interact with, and there are more: ViharBot is built as a small team of AI "
        "agents, each with a narrow job, working under a human maintainer. My job is to welcome "
        "first-time contributors and help with the contribution paperwork. I can read this pull "
        "request and its checks, but I can't change code, merge anything, or decide whether your "
        "contribution is accepted; the maintainer does that. "
        + _who_posts(maintainer, posting)
        + " If you'd rather talk to a person, just say so here and the maintainer will reply."
    )


def draft_followup(
    assessment: PullAssessment, cla: ClaAssessment, now: datetime.datetime, repo: str,
    reminder_after_hours: int = REMINDER_AFTER_HOURS,
    maintainer_login: str | None = None,
    posting: Literal["maintainer", "agent"] = "maintainer",
) -> DraftDecision:
    if not cla.record_readable:
        return DraftDecision("record_unreadable")
    items: list[str] = []
    if _outstanding(assessment, "cla_recorded") and cla.claim != "valid":
        items.append("cla")
    if _outstanding(assessment, "third_party_disclosure"):
        items.append("disclosure")
    if _outstanding(assessment, "commit_identity"):
        items.append("identity")
    if assessment.requirement("branch_current").status == OUTSTANDING and (
            "conflict" in assessment.requirement("branch_current").detail):
        items.append("conflicts")
    if not items:
        return DraftDecision("nothing_to_ask")
    if cla.author_replied_after_request:
        return DraftDecision("contributor_replied_human_should_read")

    asked_hours = _hours_since(cla.maintainer_request_at, now)
    if cla.maintainer_request_at is None:
        kind = "welcome"
    elif asked_hours is not None and asked_hours < reminder_after_hours:
        return DraftDecision("already_requested_waiting")
    else:
        kind = "reminder"

    statement = load_gate().statement(cla.legal_version or "", repo)
    maintainer = safe_login(maintainer_login)
    greeting = f"Hi @{safe_login(assessment.author_login)}" if safe_login(
        assessment.author_login) else "Hi there"
    if kind == "welcome":
        lines = [f"{greeting}, welcome to ViharBot, and thank you for the contribution!",
                 _introduction(maintainer, posting),
                 "Before this can be considered for merge, a few things are needed:"]
    else:
        lines = [f"{greeting}, a gentle reminder about the open items on this pull request. "
                 f"(This is Kepler, ViharBot's AI agent. {_who_posts(maintainer, posting)})"]
    numbered: list[str] = []
    if "cla" in items:
        numbered.append(
            "Read the Contributor License Agreement (legal-terms version "
            f"{cla.legal_version}; note it has not been reviewed by a lawyer), then post this exact "
            "text as a new comment from your own account, and don't edit it afterwards:\n"
            f"   ```text\n   {statement}\n   ```")
    if "disclosure" in items:
        numbered.append(
            "Tell us here if any of these apply: your employer or another party may have rights "
            "in this work, the pull request includes code from somewhere else, or you are under "
            "18. If none apply, say so.")
    if "identity" in items:
        numbered.append(
            "Make sure your commits use an email address linked to your GitHub account (your "
            "`@users.noreply.github.com` address works), otherwise the automated check cannot "
            "identify you.")
    if "conflicts" in items:
        numbered.append("Update the branch from `main` and resolve the merge conflicts.")
    lines += [f"{index}. {text}" for index, text in enumerate(numbered, start=1)]
    lines.append(
        "Posting the acceptance comment is a request: you are covered only once the maintainer "
        "has verified it and the record is merged. This doesn't guarantee a merge.")
    return DraftDecision("draft_ready", kind, "\n\n".join(lines), tuple(items))
