"""Drive Archie's curriculum and student-feedback workflows by hand (epic #139).

Curriculum (needs an active founder assignment, made with scripts/archie_learn.py):

    python scripts/archie_curriculum.py draft --assignment AS-... --pr 88 [--model]
    python scripts/archie_curriculum.py submit WF-...                       # Archie submits for review
    python scripts/archie_curriculum.py status WF-...
    python scripts/archie_curriculum.py request-changes --as LOGIN WF-... [--reason code]
    python scripts/archie_curriculum.py approve --as LOGIN --quality-reviewed --confidentiality-reviewed WF-...
    python scripts/archie_curriculum.py publish --as LOGIN WF-...           # founder only
    python scripts/archie_curriculum.py revoke --as LOGIN WF-...            # founder only
    python scripts/archie_curriculum.py ada-get LESSON_ID [--version N]     # what Ada would receive

Student feedback (no assignment; the student must be on the roster):

    python scripts/archie_curriculum.py feedback-prepare --pr 91            # also submits for review
    python scripts/archie_curriculum.py feedback-approve --as LOGIN --quality-reviewed --confidentiality-reviewed FB-...
    python scripts/archie_curriculum.py feedback-release --as LOGIN FB-...  # founder only; checks the live head
    python scripts/archie_curriculum.py feedback-revoke --as LOGIN FB-...   # founder only
    python scripts/archie_curriculum.py feedback-status FB-...
    python scripts/archie_curriculum.py ada-feedback FB-... --student LOGIN
    python scripts/archie_curriculum.py audit-verify

Authority. `draft`, `submit` and `feedback-prepare` run as the Archie agent: they can only draft and
submit, and `draft` still needs an active assignment. Every other write needs a human who proves a
token: set ARCHIE_TOKEN to your secret and pass `--as` your login; it is checked against
ARCHIE_FOUNDER_TOKEN_HASHES or ARCHIE_REVIEWER_TOKEN_HASHES. With no hashes configured, privileged
commands are refused (there is no legacy mode here). The authorization rules live in the library, not
in this script, so it cannot grant anything the library would refuse.

Instructor guidance: if the assignment grants it, set ARCHIE_INSTRUCTOR_READ_TOKEN and
ARCHIE_ALLOW_CONFIDENTIAL=1. The confidential-text index is never saved, so `approve` and `publish`
rebuild it by re-reading the assignment's instructor paths, and refuse if they cannot.

Configuration (environment): ARCHIE_SOURCE_TOKEN (read-only on the source repo), LEARNING_SOURCE_REPO,
LEARNING_INSTRUCTOR_REPO, ASSIGNMENT_STORE_DIR, ARCHIE_AUDIT_DIR, ARCHIE_DRAFT_DIR, ARCHIE_PUBLISHED_DIR,
ARCHIE_WORKFLOW_DIR, ARCHIE_FEEDBACK_DIR, ARCHIE_STUDENT_LOGINS, ARCHIE_MODEL_ALIAS (with --model).
Nothing here merges, deploys or calls a model unless --model is given and an alias is configured.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from viharbot.learning.ada import AdaGateway, NotAvailable, PublishedStore
from viharbot.learning.assignment import AssignmentStore, EvidenceScope, founders_from_env
from viharbot.learning.audit import AuditLog
from viharbot.learning.authn import ARCHIE, CredentialAuthenticator, HumanActor
from viharbot.learning.confidentiality import ContentGuard
from viharbot.learning.feedback import Attestation as FeedbackAttestation
from viharbot.learning.feedback import FeedbackGateway, FeedbackService, roster_from_env
from viharbot.learning.github import SourceReader
from viharbot.learning.instructor_access import InstructorReader
from viharbot.learning.publication import Attestation, DraftStore, PublicationService, ValidationFailed
from viharbot.learning.sage_evidence import DEFAULT_SAGE_AUTHORS
from viharbot.learning.synthesis import LiteLLMModelClient
from viharbot.learning.workflow import CurriculumWorkflow
from viharbot.observability import configure_logging
from viharbot.sage_review_github import GitHubClient, GitHubError

PRIVILEGED = {"request-changes", "approve", "publish", "revoke", "feedback-approve", "feedback-release",
              "feedback-revoke"}


class Refused(Exception):
    """A usage or configuration problem; the message is safe to print."""


def _client(token: str) -> GitHubClient:  # a seam for tests
    return GitHubClient(token)


def _dir(name: str, default: str) -> Path:
    return Path(os.getenv(name, default))


def _env_flag(name: str) -> bool:
    return os.getenv(name, "") == "1"


def _reader() -> SourceReader:
    token = os.getenv("ARCHIE_SOURCE_TOKEN", "")
    if not token:
        raise Refused("ARCHIE_SOURCE_TOKEN is not set")
    authors = frozenset(a.strip() for a in os.getenv("ARCHIE_SAGE_COMMENT_AUTHORS", "").split(",") if a.strip())
    return SourceReader(_client(token), os.getenv("LEARNING_SOURCE_REPO", "vchokshi/viharbot"),
                        authors or DEFAULT_SAGE_AUTHORS)


def _human(args: argparse.Namespace) -> HumanActor:
    auth = CredentialAuthenticator.from_env()
    if not auth.configured:
        raise Refused("no token hashes are configured (ARCHIE_FOUNDER_TOKEN_HASHES / ARCHIE_REVIEWER_TOKEN_HASHES)")
    if not args.login:
        raise Refused("--as LOGIN is required")
    return auth.authenticate(args.login, os.getenv("ARCHIE_TOKEN", ""))


class Context:
    """The stores and services, built from the environment on demand."""

    def __init__(self) -> None:
        self.audit = AuditLog(_dir("ARCHIE_AUDIT_DIR", "data/audit"))
        self.assignments = AssignmentStore(_dir("ASSIGNMENT_STORE_DIR", "data/assignments"), founders_from_env(),
                                           require_authenticated=True, audit=self.audit)
        self.guard = ContentGuard()
        self.drafts = DraftStore(_dir("ARCHIE_DRAFT_DIR", "data/curriculum/drafts"))
        self.published = PublishedStore(_dir("ARCHIE_PUBLISHED_DIR", "data/curriculum/published"))
        self.service = PublicationService(self.drafts, self.published, self.assignments, self.audit, self.guard)

    def instructor_reader(self) -> InstructorReader | None:
        token = os.getenv("ARCHIE_INSTRUCTOR_READ_TOKEN", "")
        if not token:
            return None
        return InstructorReader(_client(token), os.getenv("LEARNING_INSTRUCTOR_REPO",
                                                          "vchokshi/viharbot-onboarding-instructor"),
                                self.assignments, allow_confidential=_env_flag("ARCHIE_ALLOW_CONFIDENTIAL"),
                                index=self.guard)

    def workflow(self, reader: SourceReader | None, model: bool = False) -> CurriculumWorkflow:
        return CurriculumWorkflow(
            _dir("ARCHIE_WORKFLOW_DIR", "data/curriculum/workflows"), assignments=self.assignments,
            service=self.service, audit=self.audit, reader=reader, guard=self.guard,  # type: ignore[arg-type]
            instructor=self.instructor_reader(), model=LiteLLMModelClient() if model else None)

    def rebuild_guard(self, workflow_id: str, workflow: CurriculumWorkflow) -> None:
        """Reload confidential fingerprints before a gate that scans content; refuse if impossible."""
        record = workflow.get(workflow_id)
        assignment = self.assignments.get(record.assignment_id) if record else None
        if assignment is None or not assignment.permits(EvidenceScope.INSTRUCTOR_GUIDANCE):
            return
        reader = self.instructor_reader()
        if reader is None or not _env_flag("ARCHIE_ALLOW_CONFIDENTIAL"):
            raise Refused("this assignment used instructor guidance; set ARCHIE_INSTRUCTOR_READ_TOKEN and "
                          "ARCHIE_ALLOW_CONFIDENTIAL=1 so the leak check can be rebuilt")
        for path in assignment.instructor_paths:
            reader.read(assignment.assignment_id, path, request_id=workflow_id)


def _attestation(args: argparse.Namespace, cls=Attestation):
    return cls(instructional_quality_reviewed=args.quality_reviewed,
               confidentiality_reviewed=args.confidentiality_reviewed)


def run(args: argparse.Namespace) -> int:
    cmd = args.command
    ctx = Context()
    if cmd == "audit-verify":
        ok = ctx.audit.verify()
        print("audit chain: " + ("intact" if ok else "BROKEN"))
        return 0 if ok else 1
    if cmd == "ada-get":
        package = AdaGateway(ctx.published, ctx.audit).get(args.lesson_id, args.version)
        print(json.dumps({"lesson_id": package.content.lesson_id, "version": package.content.version,
                          "segments": len(package.content.segments), "prompts": len(package.content.prompts),
                          "delivery_mode": package.content.delivery_mode, "integrity_ok": package.verify(),
                          "approved_by": package.provenance.approved_by,
                          "published_by": package.provenance.published_by}))
        return 0
    if cmd.startswith("feedback-") or cmd == "ada-feedback":
        return run_feedback(args, ctx)

    if cmd == "draft":
        wf = ctx.workflow(_reader(), args.model)
        record = wf.run_to_draft(args.assignment, args.pr, ARCHIE)
        print(f"{record.workflow_id}: {record.step}" + (f" ({record.last_error})" if record.last_error else ""))
        return 1 if record.last_error else 0
    wf = ctx.workflow(None)
    if cmd == "status":
        print(json.dumps(wf.status(args.workflow_id), indent=2))
        return 0
    if cmd == "submit":
        print(f"{args.workflow_id}: {wf.submit(args.workflow_id, ARCHIE).step}")
        return 0
    actor = _human(args)
    if cmd == "request-changes":
        print(f"{args.workflow_id}: {wf.request_changes(args.workflow_id, actor, args.reason).step}")
    elif cmd == "approve":
        ctx.rebuild_guard(args.workflow_id, wf)
        print(f"{args.workflow_id}: {wf.approve(args.workflow_id, actor, _attestation(args)).step}")
    elif cmd == "publish":
        ctx.rebuild_guard(args.workflow_id, wf)
        print(f"{args.workflow_id}: {wf.publish(args.workflow_id, actor).step}")
    elif cmd == "revoke":
        print(f"{args.workflow_id}: {wf.revoke(args.workflow_id, actor).step}")
    return 0


def run_feedback(args: argparse.Namespace, ctx: Context) -> int:
    service = FeedbackService(_dir("ARCHIE_FEEDBACK_DIR", "data/feedback"), ctx.audit, roster_from_env(), ctx.guard)
    cmd = args.command
    if cmd == "ada-feedback":
        package = FeedbackGateway(service.released, ctx.audit).get(args.feedback_id, args.student)
        print(json.dumps({"feedback_id": package.feedback_id, "reviewed_sha": package.reviewed_sha,
                          "segments": len(package.segments), "delivery_mode": package.delivery_mode,
                          "integrity_ok": package.verify()}))
        return 0
    if cmd == "feedback-status":
        print(json.dumps(service.status(args.feedback_id), indent=2))
        return 0
    if cmd == "feedback-prepare":
        record = service.prepare(_reader().review_snapshot(args.pr), ARCHIE)
        record = service.submit(record.feedback_id, ARCHIE)
        print(f"{record.feedback_id}: {record.state.value} (reviewed {record.reviewed_sha[:12]})")
        return 0
    actor = _human(args)
    if cmd == "feedback-approve":
        print(f"{args.feedback_id}: {service.approve(args.feedback_id, actor, _attestation(args, FeedbackAttestation)).state.value}")
    elif cmd == "feedback-release":
        record = service._need(args.feedback_id)
        head = _reader().review_snapshot(record.pull_request).head_sha  # the live head, fetched now
        print(f"{args.feedback_id}: {service.release(args.feedback_id, actor, head).state.value}")
    elif cmd == "feedback-revoke":
        print(f"{args.feedback_id}: {service.revoke(args.feedback_id, actor).state.value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, *, wf: str | None = None, human: bool = False, review: bool = False):
        p = sub.add_parser(name)
        if human:
            p.add_argument("--as", dest="login", required=True, help="your GitHub login (token in ARCHIE_TOKEN)")
        if review:
            p.add_argument("--quality-reviewed", action="store_true")
            p.add_argument("--confidentiality-reviewed", action="store_true")
        if wf:
            p.add_argument(wf)
        return p

    d = sub.add_parser("draft")
    d.add_argument("--assignment", required=True)
    d.add_argument("--pr", type=int, required=True)
    d.add_argument("--model", action="store_true", help="try the LiteLLM model draft (needs ARCHIE_MODEL_ALIAS)")
    add("status", wf="workflow_id")
    add("submit", wf="workflow_id")
    add("request-changes", wf="workflow_id", human=True).add_argument("--reason", default="unspecified")
    add("approve", wf="workflow_id", human=True, review=True)
    add("publish", wf="workflow_id", human=True)
    add("revoke", wf="workflow_id", human=True)
    g = sub.add_parser("ada-get")
    g.add_argument("lesson_id")
    g.add_argument("--version", type=int, default=None)
    fp = sub.add_parser("feedback-prepare")
    fp.add_argument("--pr", type=int, required=True)
    add("feedback-approve", wf="feedback_id", human=True, review=True)
    add("feedback-release", wf="feedback_id", human=True)
    add("feedback-revoke", wf="feedback_id", human=True)
    add("feedback-status", wf="feedback_id")
    af = add("ada-feedback", wf="feedback_id")
    af.add_argument("--student", required=True)
    sub.add_parser("audit-verify")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    try:
        return run(args)
    except ValidationFailed as exc:
        print("refused: validation failed: " + ", ".join(exc.codes), file=sys.stderr)
    except NotAvailable:
        print("refused: not available", file=sys.stderr)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
    except (PermissionError, ValueError, KeyError, LookupError, GitHubError) as exc:
        print(f"refused: {type(exc).__name__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
