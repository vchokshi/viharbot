"""Epic #139, issue #140: the authorization boundary for assignments and their audit trail. Offline."""

from __future__ import annotations

import threading

import pytest

from archie_helpers import (
    FOUNDER_TOKEN, OTHER_TOKEN, REVIEWER_TOKEN, SRC, authenticator, make_env,
)
from viharbot.learning.assignment import (
    AssignmentState, AssignmentStore, EvidenceScope, NotAssigned, NotFounder,
)
from viharbot.learning.audit import AuditEvent, AuditLog
from viharbot.learning.authn import (
    ARCHIE, AgentActor, AuthenticationFailed, CredentialAuthenticator, HumanActor, Role, Unauthorized,
    hash_token,
)


def test_only_a_proven_token_yields_an_actor():
    auth = authenticator()
    actor = auth.authenticate("VChokshi", FOUNDER_TOKEN)  # login is case-insensitive
    assert actor.role is Role.FOUNDER and actor.login == "vchokshi"
    for login, token in [("vchokshi", "x" * 40), ("nobody", FOUNDER_TOKEN), ("vchokshi", ""),
                         ("reviewer-one", FOUNDER_TOKEN)]:
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(login, token)


def test_a_short_token_is_refused_even_if_its_hash_matches():
    short = "short"
    auth = CredentialAuthenticator({"vchokshi": hash_token(short)})
    with pytest.raises(AuthenticationFailed):
        auth.authenticate("vchokshi", short)


def test_an_actor_cannot_be_constructed_directly():
    with pytest.raises(Unauthorized):
        HumanActor(login="vchokshi", role=Role.FOUNDER)
    with pytest.raises(Unauthorized):
        HumanActor(login="vchokshi", role=Role.FOUNDER, _mint=object())


def test_repeated_failures_lock_a_login_out_until_the_window_passes():
    now = [1000.0]
    auth = CredentialAuthenticator({"vchokshi": hash_token(FOUNDER_TOKEN)}, clock=lambda: now[0])
    for _ in range(5):
        with pytest.raises(AuthenticationFailed):
            auth.authenticate("vchokshi", "wrong" * 8)
    with pytest.raises(AuthenticationFailed, match="too many"):
        auth.authenticate("vchokshi", FOUNDER_TOKEN)  # even the right token is refused while locked
    now[0] += 301
    assert auth.authenticate("vchokshi", FOUNDER_TOKEN).role is Role.FOUNDER


def test_token_hashes_are_parsed_from_the_environment(monkeypatch):
    monkeypatch.setenv("ARCHIE_FOUNDER_TOKEN_HASHES", f"vchokshi:{hash_token(FOUNDER_TOKEN)}, bad entry")
    monkeypatch.setenv("ARCHIE_REVIEWER_TOKEN_HASHES", f"reviewer-one:{hash_token(REVIEWER_TOKEN)}")
    auth = CredentialAuthenticator.from_env()
    assert auth.authenticate("vchokshi", FOUNDER_TOKEN).role is Role.FOUNDER
    assert auth.authenticate("reviewer-one", REVIEWER_TOKEN).role is Role.REVIEWER
    monkeypatch.delenv("ARCHIE_FOUNDER_TOKEN_HASHES")
    monkeypatch.delenv("ARCHIE_REVIEWER_TOKEN_HASHES")
    assert not CredentialAuthenticator.from_env().configured


def test_authenticated_store_refuses_bare_logins_agents_and_reviewers(tmp_path):
    env = make_env(tmp_path)
    for who in ("vchokshi", ARCHIE, AgentActor("kepler"), env.reviewer):
        with pytest.raises(NotFounder):
            env.assignments.assign(who, "t", SRC, (7,))
    assert env.assignments.list() == []
    refusals = env.audit.records(event="assignment.refused")
    assert len(refusals) == 4 and all(r.outcome == "refused" for r in refusals)


def test_a_founder_actor_must_also_be_on_the_configured_list(tmp_path):
    env = make_env(tmp_path)
    only_alice = AssignmentStore(tmp_path / "other", frozenset({"alice"}), require_authenticated=True)
    with pytest.raises(NotFounder):
        only_alice.assign(env.founder, "t", SRC, (7,))
    with pytest.raises(NotFounder):
        AssignmentStore(tmp_path / "none", frozenset(), require_authenticated=True).assign(env.founder, "t", SRC, (7,))


def test_legacy_string_login_still_works_when_authentication_is_not_required(tmp_path):
    store = AssignmentStore(tmp_path / "legacy", frozenset({"vchokshi"}))
    assert store.assign("vchokshi", "t", SRC, (7,)).assigned_by == "vchokshi"


def test_assignment_scope_names_evidence_and_defaults_exclude_kepler_and_instructor(tmp_path):
    env = make_env(tmp_path)
    a = env.assign()
    assert a.permits(EvidenceScope.SAGE_REVIEW) and a.permits(EvidenceScope.GOVERNANCE)
    assert not a.permits(EvidenceScope.KEPLER_HANDOFF) and not a.permits(EvidenceScope.INSTRUCTOR_GUIDANCE)


def test_instructor_scope_needs_explicit_paths_and_paths_need_the_scope(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(ValueError):
        env.assign(permitted_evidence=(EvidenceScope.SOURCE_REVISION, EvidenceScope.INSTRUCTOR_GUIDANCE))
    with pytest.raises(ValueError):
        env.assign(instructor_paths=("lessons/a.md",))
    a = env.assign(permitted_evidence=(EvidenceScope.SOURCE_REVISION, EvidenceScope.INSTRUCTOR_GUIDANCE),
                   instructor_paths=("lessons/a.md",))
    assert a.instructor_paths == ("lessons/a.md",)


def test_a_replayed_assignment_request_returns_the_original_and_never_a_second(tmp_path):
    env = make_env(tmp_path)
    first = env.assign(idempotency_key="req-1")
    again = env.assign(idempotency_key="req-1")
    assert first.assignment_id == again.assignment_id and len(env.assignments.list()) == 1
    with pytest.raises(ValueError, match="different assignment"):
        env.assign(prs=(7, 9), idempotency_key="req-1")
    env.assignments.cancel(env.founder, first.assignment_id)
    replay = env.assign(idempotency_key="req-1")  # a replay cannot reopen a cancelled assignment
    assert replay.state is AssignmentState.CANCELLED and len(env.assignments.list()) == 1


def test_without_a_key_each_request_is_its_own_assignment(tmp_path):
    env = make_env(tmp_path)
    assert env.assign().assignment_id != env.assign().assignment_id


def test_cancel_and_complete_are_founder_only_idempotent_and_terminal(tmp_path):
    env = make_env(tmp_path)
    a = env.assign()
    with pytest.raises(NotFounder):
        env.assignments.cancel(env.reviewer, a.assignment_id)
    assert env.assignments.cancel(env.founder, a.assignment_id).state is AssignmentState.CANCELLED
    assert env.assignments.complete(env.founder, a.assignment_id).state is AssignmentState.CANCELLED  # no reopen
    assert env.assignments.authorizing(SRC, 7) is None
    with pytest.raises(NotAssigned):
        env.assignments.require_active(a.assignment_id)


def test_assignment_changes_are_audited_and_the_chain_verifies(tmp_path):
    env = make_env(tmp_path)
    a = env.assign()
    env.assignments.cancel(env.founder, a.assignment_id)
    env.assignments.cancel(env.founder, a.assignment_id)  # repeat: no second record
    events = [r.event for r in env.audit.records(assignment_id=a.assignment_id)]
    assert events == [AuditEvent.ASSIGNMENT_CREATED, AuditEvent.ASSIGNMENT_CANCELLED]
    assert env.audit.verify()


def test_concurrent_cancel_and_complete_leave_one_consistent_terminal_state(tmp_path):
    env = make_env(tmp_path)
    a = env.assign()
    outcomes = []

    def run(op):
        outcomes.append(getattr(env.assignments, op)(env.founder, a.assignment_id).state)

    threads = [threading.Thread(target=run, args=(op,)) for op in ("cancel", "complete") * 4]
    [t.start() for t in threads]
    [t.join() for t in threads]
    final = env.assignments.get(a.assignment_id).state
    assert final in (AssignmentState.CANCELLED, AssignmentState.COMPLETED)
    assert set(outcomes) == {final}  # every caller saw the single winner
    assert len(env.audit.records(assignment_id=a.assignment_id)) == 2  # created + exactly one close
    assert env.audit.verify()


def test_audit_log_never_stores_secretish_values_and_drops_unlisted_keys(tmp_path):
    log = AuditLog(tmp_path / "a")
    rec = log.append(AuditEvent.REJECTED, "x", detail={
        "token": "ghp_" + "a" * 30, "ok": "fine", "Bad Key": "dropped", "n": 3,
        "auth": "Bearer " + "z" * 30})
    assert rec.detail == {"token": "[redacted]", "ok": "fine", "n": 3, "auth": "[redacted]"}
    assert "ghp_" not in (tmp_path / "a" / "audit.jsonl").read_text()


def test_audit_chain_detects_edits_and_deletions(tmp_path):
    log = AuditLog(tmp_path / "a")
    for i in range(3):
        log.append(AuditEvent.DRAFT_CREATED, "x", artifact_id=f"L{i}")
    assert log.verify()
    path = tmp_path / "a" / "audit.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # delete the middle record
    assert not log.verify()
    path.write_text("\n".join([lines[0].replace("L0", "LX"), lines[1], lines[2]]) + "\n")  # edit one
    assert not log.verify()


def test_audit_dedupe_key_makes_retries_idempotent(tmp_path):
    log = AuditLog(tmp_path / "a")
    a = log.append(AuditEvent.PUBLISHED, "x", key="k1")
    b = log.append(AuditEvent.PUBLISHED, "x", key="k1")
    assert a.seq == b.seq == 1 and len(log.records()) == 1


def test_concurrent_audit_appends_keep_the_chain_intact(tmp_path):
    log = AuditLog(tmp_path / "a")
    threads = [threading.Thread(target=lambda i=i: log.append(AuditEvent.DRAFT_CREATED, "x", artifact_id=f"L{i}"))
               for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(log.records()) == 12 and log.verify()
    assert OTHER_TOKEN  # fixture import used elsewhere
