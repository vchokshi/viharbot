"""Kepler's assignment workflow: authorize, prepare a workspace, commit, check, push, draft PR.

Authority is enforced here in code. Only an actor named in the policy can authorize a run, and a
run is for exactly one open issue. There is no operation that merges, pushes to the base branch,
deploys, edits governance or starts work on its own. The agent loop that will drive the
in-process methods (#127) is trusted code; the HTTP surface (api.py) exposes only authorize, read
and cancel.

Known limitations: a process that dies mid-operation leaves its run in an in-flight state and the
founder must cancel it (the issue stays blocked until then); recovery belongs to #127. The review
cycle counter and Sage integration are #129. Repository text, issue text and tool output are never
used as instructions here: the branch name derives from the issue number only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ..kepler_governance import (
    GovernanceCitation,
    GovernanceCitationError,
    GovernanceLoadError,
    GovernanceModificationError,
    load_governance,
    render_governance_followed,
    require_editable,
)
from .audit import build_event, log_event
from .github import GitHubClient, GitHubError
from .policy import AssignmentDenied, KeplerPolicy
from .store import RunStore
from .workspace import GitError, Runner, Workspace

TERMINAL = frozenset({"cancelled", "failed"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "authorized": frozenset({"preparing", "cancelled", "failed"}),
    "preparing": frozenset({"workspace_ready", "cancelled", "failed"}),
    "workspace_ready": frozenset({"branch_pushed", "cancelled", "failed"}),
    "branch_pushed": frozenset({"branch_pushed", "draft_pr_open", "cancelled", "failed"}),
    "draft_pr_open": frozenset({"draft_pr_open", "cancelled", "failed"}),
    "cancelled": frozenset(),
    "failed": frozenset(),
}
WORKING = frozenset({"workspace_ready", "branch_pushed", "draft_pr_open"})
PUBLIC_FIELDS = (
    "run_id", "issue", "state", "branch", "created_by", "created_at", "updated_at", "reason",
)
MAX_TITLE = 200
MAX_SUMMARY = 20_000
MAX_MESSAGE = 5_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssignmentService:
    def __init__(
        self,
        *,
        policy: KeplerPolicy,
        store: RunStore,
        github: GitHubClient,
        workspaces_root: Path,
        runner: Runner,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.policy = policy
        self.store = store
        self.github = github
        self.root = Path(workspaces_root)
        self.runner = runner
        self._clock = clock

    # --- audit and state -----------------------------------------------------------------

    def _event(self, event: str, decision: str, **fields: object) -> dict:
        """Append one chained event (no run change). Denials use this."""
        with self.store.locked():
            events = self.store.events()
            built = build_event(
                events[-1] if events else None,
                timestamp=self._clock(), event=event, decision=decision, fields=fields,
            )
            self.store.commit(events=[built])
        log_event(built)
        return built

    def _deny(self, reason: str, status: int, **fields: object) -> AssignmentDenied:
        self._event("action_denied", "deny", reason=reason, **fields)
        return AssignmentDenied(reason, status)

    def _update(
        self,
        run_id: str,
        allowed_from: frozenset[str] | set[str],
        new_state: str | None,
        event: str,
        *,
        decision: str = "allow",
        changes: dict | None = None,
        **fields: object,
    ) -> dict:
        """Compare-and-set a run's state and append the event in one atomic write."""
        with self.store.locked():
            run = self.store.get_run(run_id)
            if run is None:
                raise AssignmentDenied("unknown_run", 404)
            if run["state"] not in allowed_from:
                raise AssignmentDenied("invalid_state", 409)
            target = new_state or run["state"]
            if new_state is not None and new_state not in TRANSITIONS[run["state"]]:
                raise AssignmentDenied("invalid_transition", 409)
            before = run["state"]
            run.update(changes or {})
            run["state"] = target
            run["updated_at"] = self._clock()
            events = self.store.events()
            built = build_event(
                events[-1] if events else None, timestamp=self._clock(), event=event,
                decision=decision,
                fields={"run_id": run_id, "issue": run["issue"], "branch": run["branch"],
                        "state_from": before, "state_to": target, **fields},
            )
            self.store.commit(run=run, events=[built])
        log_event(built)
        return run

    @staticmethod
    def public(run: dict) -> dict:
        view = {name: run.get(name) for name in PUBLIC_FIELDS}
        pr = run.get("pr")
        view["pr_number"] = pr["number"] if pr else None
        view["pr_url"] = pr["url"] if pr else None
        return view

    def _run(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise AssignmentDenied("unknown_run", 404)
        return run

    def _workspace(self, run: dict) -> Workspace:
        return Workspace(self.root, Path(run["workspace"]).name, self.policy, self.runner)

    def _require_working(self, run_id: str) -> dict:
        run = self._run(run_id)
        if run["state"] not in WORKING:
            raise self._deny("invalid_state", 409, run_id=run_id, issue=run["issue"])
        # The branch is re-derived from policy, never trusted from the stored record alone.
        try:
            self.policy.check_branch(run["branch"], run["issue"])
        except AssignmentDenied:
            raise self._deny("branch_not_allowed", 403, run_id=run_id, issue=run["issue"]) from None
        return run

    # --- the workflow --------------------------------------------------------------------

    def authorize(self, issue: int, *, actor: str, request_id: str | None = None) -> dict:
        """Start a run for one open issue. Only a policy assigner can do this."""
        if actor not in self.policy.assigners:
            raise self._deny("not_authorized", 403, actor=actor, request_id=request_id)
        if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
            raise self._deny("invalid_issue", 422, actor=actor, request_id=request_id)
        try:
            info = self.github.get_issue(issue)
        except GitHubError as exc:
            raise self._deny("github_unavailable", 502, actor=actor, issue=issue,
                             request_id=request_id, error=str(exc)) from None
        if info is None or info.is_pull_request or info.state != "open":
            raise self._deny("issue_not_assignable", 422, actor=actor, issue=issue,
                             request_id=request_id)
        run_id = uuid4().hex[:12]
        with self.store.locked():
            for existing in self.store.runs():
                if existing["issue"] == issue and existing["state"] not in TERMINAL:
                    raise self._deny("active_run_exists", 409, actor=actor, issue=issue,
                                     request_id=request_id, run_id=existing["run_id"])
            now = self._clock()
            run = {
                "run_id": run_id, "issue": issue, "repository": self.policy.repository,
                "branch": self.policy.branch_for(issue), "state": "authorized",
                "created_by": actor, "created_at": now, "updated_at": now,
                "request_id": request_id, "workspace": str(self.root / f"issue-{issue}-{run_id}"),
                "governance": None, "pr": None, "commits": [], "reason": None,
            }
            events = self.store.events()
            built = build_event(
                events[-1] if events else None, timestamp=now, event="assignment_authorized",
                decision="allow",
                fields={"run_id": run_id, "issue": issue, "actor": actor, "branch": run["branch"],
                        "request_id": request_id, "state_to": "authorized"},
            )
            self.store.commit(run=run, events=[built])
        log_event(built)
        return self.public(run)

    def prepare_workspace(self, run_id: str) -> dict:
        """Clone, create the feature branch and load governance. Fails closed on any problem."""
        run = self._update(run_id, {"authorized"}, "preparing", "workspace_started")
        workspace = self._workspace(run)
        try:
            workspace.clone(run["branch"])
            bundle = load_governance(workspace.path)
        except (GitError, GovernanceLoadError, AssignmentDenied) as exc:
            reason = exc.reason if isinstance(exc, AssignmentDenied) else "workspace_failed"
            self._update(run_id, {"preparing"}, "failed", "assignment_failed", decision="fail",
                         changes={"reason": reason}, reason=reason, error=str(exc)[:300])
            raise AssignmentDenied(reason, 500) from None
        run = self._update(
            run_id, {"preparing"}, "workspace_ready", "workspace_ready",
            changes={"governance": bundle.versions()}, governance=bundle.versions(),
        )
        return self.public(run)

    def commit(self, run_id: str, message: str) -> str:
        """Commit the workspace's changes, refusing any that touch protected governance paths."""
        run = self._require_working(run_id)
        message = self._clean(message, MAX_MESSAGE, "invalid_message", run)
        workspace = self._workspace(run)
        paths = workspace.stage_all()
        if not paths:
            raise self._deny("nothing_to_commit", 409, run_id=run_id, issue=run["issue"])
        for path in paths:
            try:
                require_editable(path, workspace.path)
            except GovernanceModificationError:
                workspace.unstage_all()
                raise self._deny("protected_path", 403, run_id=run_id, issue=run["issue"]) from None
        sha = workspace.commit(message)
        self._update(run_id, WORKING, None, "commit_created", commit=sha,
                     changes={"commits": [*run["commits"], sha]})
        return sha

    def run_checks(self, run_id: str, argv: list[str]) -> dict:
        """Run an allowlisted test/lint command in the workspace and record the real result."""
        run = self._require_working(run_id)
        if not argv or not all(isinstance(a, str) and a for a in argv) or \
                not self.policy.command_allowed(argv):
            raise self._deny("command_not_allowed", 403, run_id=run_id, issue=run["issue"])
        label = " ".join(argv[:3])
        self._update(run_id, WORKING, None, "tests_started", command=label)
        result = self.runner.run_command(argv, self._workspace(run).path)
        self._update(run_id, WORKING, None, "tests_completed", command=label,
                     exit_code=result.exit_code, duration_ms=result.duration_ms,
                     output_sha256=result.output_sha256,
                     decision="allow" if result.exit_code == 0 else "fail")
        return {"exit_code": result.exit_code, "output_tail": result.tail,
                "output_sha256": result.output_sha256}

    def push(self, run_id: str) -> dict:
        """Push the issue's feature branch. Refuses if there is nothing beyond the base branch."""
        run = self._require_working(run_id)
        workspace = self._workspace(run)
        if workspace.commits_ahead() < 1:
            raise self._deny("no_commits", 409, run_id=run_id, issue=run["issue"])
        try:
            workspace.push(run["branch"], run["issue"])
        except GitError as exc:
            self._update(run_id, WORKING, "failed", "assignment_failed", decision="fail",
                         changes={"reason": "push_failed"}, reason="push_failed",
                         error=str(exc)[:300])
            raise AssignmentDenied("push_failed", 502) from None
        target = "draft_pr_open" if run["state"] == "draft_pr_open" else "branch_pushed"
        run = self._update(run_id, WORKING, target, "branch_pushed")
        return self.public(run)

    def open_draft_pr(
        self, run_id: str, *, title: str, summary: str,
        citations: tuple[GovernanceCitation, ...],
    ) -> dict:
        """Create or update the run's draft PR, with the mandatory Governance followed section."""
        run = self._require_working(run_id)
        if run["state"] == "workspace_ready":
            raise self._deny("branch_not_pushed", 409, run_id=run_id, issue=run["issue"])
        title = self._clean(title, MAX_TITLE, "invalid_title", run)
        summary = self._clean(summary, MAX_SUMMARY, "invalid_summary", run)
        workspace = self._workspace(run)
        try:
            bundle = load_governance(workspace.path)
            if bundle.versions() != run["governance"]:
                raise self._deny("governance_changed", 409, run_id=run_id, issue=run["issue"])
            governance = render_governance_followed(bundle, tuple(citations))
        except GovernanceLoadError:
            raise self._deny("governance_unavailable", 409, run_id=run_id,
                             issue=run["issue"]) from None
        except GovernanceCitationError:
            raise self._deny("invalid_governance_citations", 422, run_id=run_id,
                             issue=run["issue"]) from None
        body = f"Refs #{run['issue']}\n\n{summary}\n\n{governance}\n"
        try:
            pr = run["pr"]
            if pr is None:
                adopted = self.github.find_pull_request(run["branch"])  # recover after a crash
                if adopted is not None:
                    pr = {"number": adopted.number, "url": adopted.url}
            if pr is None:
                created = self.github.create_draft_pull_request(run["branch"], title, body)
                pr, event = {"number": created.number, "url": created.url}, "draft_pr_created"
            else:
                updated = self.github.update_pull_request(pr["number"], title, body)
                pr, event = {"number": updated.number, "url": updated.url}, "draft_pr_updated"
        except GitHubError as exc:
            raise self._deny("github_unavailable", 502, run_id=run_id, issue=run["issue"],
                             error=str(exc)) from None
        run = self._update(run_id, WORKING, "draft_pr_open", event, changes={"pr": pr},
                           pr_number=pr["number"])
        return self.public(run)

    def cancel(self, run_id: str, *, actor: str, request_id: str | None = None) -> dict:
        if actor not in self.policy.assigners:
            raise self._deny("not_authorized", 403, actor=actor, run_id=run_id,
                             request_id=request_id)
        run = self._update(
            run_id, set(TRANSITIONS) - TERMINAL, "cancelled", "assignment_cancelled",
            changes={"reason": "cancelled"}, actor=actor, request_id=request_id,
            reason="cancelled",
        )
        return self.public(run)

    def get(self, run_id: str) -> dict:
        return self.public(self._run(run_id))

    def _clean(self, text: object, limit: int, reason: str, run: dict) -> str:
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            raise self._deny(reason, 422, run_id=run["run_id"], issue=run["issue"])
        return text.strip()
