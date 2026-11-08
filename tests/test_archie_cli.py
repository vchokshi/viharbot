"""Epic #139, issue #140: the assignment CLI uses the same authorization boundary. Offline."""

from __future__ import annotations

import importlib.util

from archie_helpers import FOUNDER_TOKEN
from viharbot.learning.assignment import AssignmentState, AssignmentStore
from viharbot.learning.audit import AuditLog
from viharbot.learning.authn import hash_token


def _cli():
    spec = importlib.util.spec_from_file_location("archie_learn", "scripts/archie_learn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store(tmp_path):
    return AssignmentStore(tmp_path / "a", frozenset({"vchokshi"}))


def _env(monkeypatch, tmp_path, hashes=True):
    monkeypatch.setenv("ASSIGNMENT_STORE_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("ARCHIE_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("ARCHIE_FOUNDER_LOGINS", "vchokshi")
    monkeypatch.delenv("ARCHIE_FOUNDER_TOKEN", raising=False)
    if hashes:
        monkeypatch.setenv("ARCHIE_FOUNDER_TOKEN_HASHES", f"vchokshi:{hash_token(FOUNDER_TOKEN)}")
    else:
        monkeypatch.delenv("ARCHIE_FOUNDER_TOKEN_HASHES", raising=False)


def test_cli_requires_the_founder_token_once_hashes_are_configured(monkeypatch, tmp_path, capsys):
    cli = _cli()
    _env(monkeypatch, tmp_path)
    args = ["assign", "--as", "vchokshi", "--topic", "x", "--pr", "7"]
    assert cli.main(args) == 2  # a bare login is no longer enough
    monkeypatch.setenv("ARCHIE_FOUNDER_TOKEN", "wrong-token-" + "x" * 20)
    assert cli.main(args) == 2
    assert _store(tmp_path).list() == []
    monkeypatch.setenv("ARCHIE_FOUNDER_TOKEN", FOUNDER_TOKEN)
    assert cli.main([*args, "--idempotency-key", "k"]) == 0
    assert cli.main([*args, "--idempotency-key", "k"]) == 0  # a replay returns the same assignment
    assert len(_store(tmp_path).list()) == 1
    assert FOUNDER_TOKEN not in capsys.readouterr().out + (tmp_path / "audit" / "audit.jsonl").read_text()
    assert AuditLog(tmp_path / "audit").verify()


def test_cli_cancel_needs_the_token_and_scope_flags_are_validated(monkeypatch, tmp_path):
    cli = _cli()
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ARCHIE_FOUNDER_TOKEN", FOUNDER_TOKEN)
    assert cli.main(["assign", "--as", "vchokshi", "--topic", "x", "--pr", "7"]) == 0
    aid = _store(tmp_path).list()[0].assignment_id
    monkeypatch.delenv("ARCHIE_FOUNDER_TOKEN")
    assert cli.main(["cancel", "--as", "vchokshi", aid]) == 2
    monkeypatch.setenv("ARCHIE_FOUNDER_TOKEN", FOUNDER_TOKEN)
    # instructor guidance without exact paths is refused, and so are paths without the scope
    assert cli.main(["assign", "--as", "vchokshi", "--topic", "x", "--pr", "8",
                     "--allow", "instructor_guidance"]) == 2
    assert cli.main(["assign", "--as", "vchokshi", "--topic", "x", "--pr", "8",
                     "--instructor-path", "lessons/a.md"]) == 2
    assert cli.main(["cancel", "--as", "vchokshi", aid]) == 0
    assert _store(tmp_path).get(aid).state is AssignmentState.CANCELLED


def test_cli_legacy_mode_still_works_without_hashes_but_warns(monkeypatch, tmp_path, capsys):
    cli = _cli()
    _env(monkeypatch, tmp_path, hashes=False)
    assert cli.main(["assign", "--as", "vchokshi", "--topic", "x", "--pr", "7"]) == 0
    assert "asserted, not proven" in capsys.readouterr().err
