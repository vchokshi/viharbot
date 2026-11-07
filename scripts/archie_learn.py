"""Run Archie's learning pipeline by hand or from a schedule.

    python scripts/archie_learn.py assign --as LOGIN --topic "..." --pr 88 [--pr 91 ...]
    python scripts/archie_learn.py cancel --as LOGIN ASSIGNMENT_ID    # or: complete
    python scripts/archie_learn.py assignments
    python scripts/archie_learn.py ingest --pr 88            # extract only (dry run, default)
    python scripts/archie_learn.py ingest --pr 88 --propose  # also open an instructor-repo PR
    python scripts/archie_learn.py reconcile [--propose]

Nothing runs without a founder assignment: `ingest` and `reconcile` only touch pull requests that
an active assignment covers. Only logins in ARCHIE_FOUNDER_LOGINS can assign, cancel or complete.

Configuration (environment): ARCHIE_FOUNDER_LOGINS (comma-separated), ARCHIE_SOURCE_TOKEN (read-only on the source repo),
ARCHIE_INSTRUCTOR_TOKEN (needed only with --propose; contents and pull-requests write on the
instructor repo only), LEARNING_SOURCE_REPO, LEARNING_INSTRUCTOR_REPO, LEARNING_STORE_DIR, ASSIGNMENT_STORE_DIR.
No model is called, and nothing is merged.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from viharbot.learning.assignment import AssignmentStore, NotAssigned, NotFounder, founders_from_env
from viharbot.learning.github import InstructorProposer, SourceReader
from viharbot.learning.ingest import process, reconcile
from viharbot.learning.store import IngestionStore
from viharbot.observability import configure_logging
from viharbot.sage_review_github import GitHubClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    asg = sub.add_parser("assign")
    asg.add_argument("--as", dest="login", required=True, help="founder GitHub login")
    asg.add_argument("--topic", required=True)
    asg.add_argument("--pr", type=int, action="append", required=True)
    for name in ("cancel", "complete"):
        c = sub.add_parser(name)
        c.add_argument("--as", dest="login", required=True, help="founder GitHub login")
        c.add_argument("assignment_id")
    sub.add_parser("assignments")
    one = sub.add_parser("ingest")
    one.add_argument("--pr", type=int, required=True)
    rec = sub.add_parser("reconcile")
    rec.add_argument("--limit", type=int, default=30)
    for p in (one, rec):
        p.add_argument("--propose", action="store_true", help="open an instructor-repo pull request")
    args = parser.parse_args(argv)
    configure_logging()

    source = os.getenv("LEARNING_SOURCE_REPO", "vchokshi/viharbot")
    assignments = AssignmentStore(Path(os.getenv("ASSIGNMENT_STORE_DIR", "data/assignments")),
                                  founders_from_env())
    if args.command in {"assign", "cancel", "complete", "assignments"}:
        try:
            if args.command == "assign":
                a = assignments.assign(args.login, args.topic, source, tuple(args.pr))
                print(f"{a.assignment_id}: assigned PRs {list(a.pull_requests)} in {a.source_repository}")
            elif args.command in {"cancel", "complete"}:
                a = getattr(assignments, args.command)(args.login, args.assignment_id)
                print(f"{a.assignment_id}: {a.state.value}")
            else:
                for a in assignments.list():
                    print(f"{a.assignment_id} {a.state.value} PRs {list(a.pull_requests)} {a.topic}")
        except (NotFounder, KeyError, ValueError) as exc:
            print(f"refused: {type(exc).__name__}", file=sys.stderr)
            return 2
        return 0
    instructor = os.getenv("LEARNING_INSTRUCTOR_REPO", "vchokshi/viharbot-onboarding-instructor")
    token = os.getenv("ARCHIE_SOURCE_TOKEN", "")
    if not token:
        print("ARCHIE_SOURCE_TOKEN is not set", file=sys.stderr)
        return 2
    reader = SourceReader(GitHubClient(token), source)
    proposer = None
    if args.propose:
        write_token = os.getenv("ARCHIE_INSTRUCTOR_TOKEN", "")
        if not write_token:
            print("ARCHIE_INSTRUCTOR_TOKEN is required with --propose", file=sys.stderr)
            return 2
        proposer = InstructorProposer(GitHubClient(write_token), instructor)
    store = IngestionStore(Path(os.getenv("LEARNING_STORE_DIR", "data/learning")))
    try:
        events = ([process(args.pr, reader, store, assignments, proposer)] if args.command == "ingest"
                  else reconcile(reader, store, assignments, proposer, args.limit))
    except NotAssigned:
        print(f"refused: no active founder assignment covers PR #{args.pr}", file=sys.stderr)
        return 2
    for e in events:
        print(f"PR #{e.pull_request}: {e.state.value}" + (f" ({e.error})" if e.error else "")
              + (f" {e.proposal_url}" if e.proposal_url else ""))
    return 1 if any(e.state.value == "failed" for e in events) else 0


if __name__ == "__main__":
    raise SystemExit(main())
