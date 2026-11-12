"""The PostgreSQL fixture's safety logic, without a database (issue #40, work item 9).

Covers loopback detection, the remote opt-in rule, the empty-cluster decision, the admin privilege
check, DSN inheritance of the TLS settings, and the migration role rewrite. No network: name
resolution is stubbed.
"""

from __future__ import annotations

import re
import secrets

import pytest

pytest.importorskip("psycopg")

import memory_helpers as mh  # noqa: E402

from viharbot.memory_service.migrate import discover  # noqa: E402

PW = secrets.token_hex(8)  # built at runtime: no credential literal in source
NEW_ROLE = "vbtest_s_0123456789ab"


def kv(host: str, **extra: str) -> str:
    parts = {"host": host, "user": "admin", "password": PW, "dbname": "postgres", **extra}
    return " ".join(f"{k}={v}" for k, v in parts.items())


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    table = {
        "localhost": ["127.0.0.1", "::1"],
        "db.example.test": ["203.0.113.7"],
        "mixed.test": ["127.0.0.1", "203.0.113.7"],
    }

    def fake(host, *args, **kwargs):
        if host not in table:
            raise OSError("no such host")
        return [(0, 0, 0, "", (a, 0)) for a in table[host]]

    monkeypatch.setattr(mh.socket, "getaddrinfo", fake)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.5.0.1", "::1", "[::1]", "localhost", "", "/var/run/postgresql"])
def test_loopback_hosts_are_local(host):
    assert mh.is_loopback_host(host)


@pytest.mark.parametrize("host", ["10.0.0.5", "203.0.113.7", "db.example.test", "mixed.test", "unresolvable.test"])
def test_everything_else_including_unresolvable_names_is_remote(host):
    assert not mh.is_loopback_host(host)


def test_a_dsn_is_remote_unless_every_host_is_loopback_and_hostaddr_wins():
    assert not mh.dsn_is_remote(kv("127.0.0.1"), {})
    assert not mh.dsn_is_remote("postgresql://u@localhost:5432/postgres", {})
    assert mh.dsn_is_remote(kv("db.example.test"), {})
    assert mh.dsn_is_remote(kv("127.0.0.1,db.example.test"), {})
    assert mh.dsn_is_remote(kv("localhost", hostaddr="203.0.113.7"), {})  # libpq connects to hostaddr
    assert not mh.dsn_is_remote(kv("db.example.test", hostaddr="127.0.0.1"), {})


def test_the_force_switch_can_only_make_a_loopback_dsn_stricter():
    assert mh.dsn_is_remote(kv("127.0.0.1"), {mh.FORCE_GUARD_ENV: "1"})
    assert not mh.dsn_is_remote(kv("127.0.0.1"), {mh.FORCE_GUARD_ENV: "0"})
    assert mh.dsn_is_remote(kv("db.example.test"), {mh.FORCE_GUARD_ENV: "0"})


def test_a_remote_cluster_needs_both_explicit_opt_ins_exactly_equal_to_1():
    both = {mh.ALLOW_REMOTE_ENV: "1", mh.CONFIRM_EMPTY_ENV: "1"}
    assert mh.remote_opt_in_problems(both) == []
    assert len(mh.remote_opt_in_problems({})) == 2
    only_allow = mh.remote_opt_in_problems({mh.ALLOW_REMOTE_ENV: "1"})
    assert len(only_allow) == 1 and mh.CONFIRM_EMPTY_ENV in only_allow[0]
    only_confirm = mh.remote_opt_in_problems({mh.CONFIRM_EMPTY_ENV: "1"})
    assert len(only_confirm) == 1 and mh.ALLOW_REMOTE_ENV in only_confirm[0]
    for value in ("true", "yes", "0", ""):
        assert len(mh.remote_opt_in_problems({mh.ALLOW_REMOTE_ENV: value, mh.CONFIRM_EMPTY_ENV: "1"})) == 1


def test_an_empty_cluster_holds_only_system_databases_and_none_of_the_real_roles():
    assert mh.cluster_problems(["postgres", "template0", "template1", "defaultdb", "_dodb"], ["doadmin"]) == []
    assert mh.cluster_problems(["postgres", "vbtest_abc123"], []) == []  # leftovers of this suite are fine


def test_a_live_database_or_a_real_role_makes_the_cluster_not_empty():
    live = mh.cluster_problems(["postgres", "viharbot_memory"], [])
    assert len(live) == 1 and "viharbot_memory" in live[0]
    roles = mh.cluster_problems(["postgres"], ["memory_service"])
    assert len(roles) == 1 and "memory_service" in roles[0]
    assert len(mh.cluster_problems(["postgres", "app"], ["memory_migrator", "memory_service"])) == 2
    assert mh.cluster_problems(["vbtestx"], [])  # a name that merely resembles the prefix is not it


def test_the_admin_needs_createrole_and_createdb_unless_superuser():
    assert mh.admin_privilege_problems(True, False, False) == []
    assert mh.admin_privilege_problems(False, True, True) == []
    assert mh.admin_privilege_problems(False, False, True) == ["CREATEROLE"]
    assert mh.admin_privilege_problems(False, True, False) == ["CREATEDB"]
    assert mh.admin_privilege_problems(False, False, False) == ["CREATEROLE", "CREATEDB"]


def test_derived_dsns_inherit_sslmode_and_sslrootcert_in_both_dsn_forms():
    extra = {"sslmode": "verify-full", "sslrootcert": "/tmp/ca.crt"}
    uri = f"postgresql://admin:{PW}@db.example.test:25060/postgres?sslmode=verify-full&sslrootcert=/tmp/ca.crt"
    for admin in (kv("db.example.test", **extra), uri):
        derived = mh.derive_dsn(admin, dbname="vbtest_x", user="vbtest_m_x", password=PW + "2")
        parts = mh._dsn_dict(derived)
        assert (parts["sslmode"], parts["sslrootcert"]) == ("verify-full", "/tmp/ca.crt")
        assert (parts["dbname"], parts["user"], parts["password"]) == ("vbtest_x", "vbtest_m_x", PW + "2")
        assert parts["host"] == "db.example.test"
    plain = mh._dsn_dict(mh.derive_dsn(kv("127.0.0.1"), dbname="vbtest_x"))
    assert "sslmode" not in plain and plain["user"] == "admin"  # nothing is invented either


def test_the_role_rewrite_changes_only_the_service_role_identifier():
    for _, name, text in discover():
        assert mh.rewrite_role_name(text, "memory_service") == text
        rewritten = mh.rewrite_role_name(text, NEW_ROLE)
        assert not re.search(r"\bmemory_service\b", rewritten)
        old, new = text.splitlines(), rewritten.splitlines()
        assert len(old) == len(new)
        changed = [(a, b) for a, b in zip(old, new) if a != b]
        for before, after in changed:
            assert re.search(r"\bmemory_service\b", before)
            assert after == before.replace("memory_service", NEW_ROLE)
        if name.startswith("0001"):
            assert changed, "migration 0001 names the service role and must be rewritten"


def test_the_rewrite_refuses_an_unsafe_role_name():
    with pytest.raises(AssertionError):
        mh.rewrite_role_name("GRANT USAGE ON SCHEMA memory TO memory_service;", 'x"; DROP ROLE y; --')


def test_the_fixture_refuses_to_drop_what_it_did_not_create():
    with pytest.raises(ValueError):
        mh.drop_database("host=127.0.0.1", "viharbot_memory")
    with pytest.raises(ValueError):
        mh.drop_database("host=127.0.0.1", "vbtest_neverprovisioned")
