"""Offline coverage of the memory service's request validation and error mapping (issue #40).

The PostgreSQL contract tests already decide what the database enforces. These tests run with no
database at all: a recording repository stands in for storage and proves two things for each case,
that the service refuses (or maps) the request with a fixed, generic body, and that a refused
request never reached storage. They also pin the idempotency hash, the generated-id retry loop and
the list query grammar, none of which had an offline test.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from memory_helpers import (
    T0,
    ExplodingRepository,
    agent_path,
    build_world,
    create_body,
    only_policy,
    session_path,
)

from viharbot.memory_service.models import CreateRequest, UpdateRequest
from viharbot.memory_service.paths import parse_memory_path
from viharbot.memory_service.repository import (
    ConflictError,
    MemoryStoreError,
    MutationResult,
    NotFoundError,
    RefusedError,
    StorageUnavailableError,
    TombstonedError,
)
from viharbot.memory_service.service import _hash

MID = "0194f3a0-0000-7000-8000-000000000001"


class Recorder(ExplodingRepository):
    """Records every create; behaviour for the next calls is scripted through ``script``."""

    def __init__(self) -> None:
        self.creates: list = []
        self.lists: list = []
        self.script: list = []  # exceptions raised, in order, before a create succeeds
        self.ping_error: Exception | None = None

    def ping(self):
        if self.ping_error:
            raise self.ping_error

    def create(self, new, ctx):
        self.creates.append((new, ctx))
        if self.script:
            raise self.script.pop(0)
        return MutationResult(201, {"memory_id": new.memory_id, "state": "created"})

    def list(self, scope, owner, now, classes, tag, limit):
        self.lists.append((scope, owner, classes, tag, limit))
        return []

    def get(self, scope, owner, memory_id, now, classes):
        raise NotFoundError()

    def update(self, *a, **k):
        raise self.script.pop(0)

    classify = retire = erase = update


@pytest.fixture
def repo():
    return Recorder()


@pytest.fixture
def world(tmp_path, repo):
    return build_world(tmp_path, repo)


def refused(response, status, code=None):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) <= {"detail", "code"}
    if code:
        assert body.get("code") == code
    return body


# --- create validation: each refusal is generic and never reaches storage ---------------------


@pytest.mark.parametrize(
    "path_fn, change, status, code",
    [
        (agent_path, {"ttl_seconds": 60}, 422, "ttl_not_for_agent_records"),
        (session_path, {"retention_until": "2030-01-01T00:00:00+00:00"}, 422, "retention_not_for_session"),
        (agent_path, {"retention_until": "2026-12-01T00:00:00"}, 422, "retention_needs_timezone"),
        (agent_path, {"retention_until": "2026-11-10T11:59:59+00:00"}, 422, "retention_in_past"),
        (agent_path, {"retention_until": "2026-11-10T12:00:00+00:00"}, 422, "retention_in_past"),  # == now
        (agent_path, {"retention_until": "2028-01-01T00:00:00+00:00"}, 422, "retention_too_long"),
        (session_path, {"ttl_seconds": 86401}, 422, "ttl_too_long"),
        (agent_path, {"content": "x" * 16385}, 422, "content_too_large"),
        (agent_path, {"classification": "confidential"}, 403, "classification_above_ceiling"),
        (agent_path, {"personal_data": True, "consent": {"basis": "consent_recorded", "reference": "c-1"}},
         422, "personal_data_not_permitted"),
        (agent_path, {"consent": {"basis": "consent_recorded"}}, 422, "consent_reference_required"),
    ],
)
def test_create_refusals_are_fixed_codes_and_never_reach_storage(world, repo, path_fn, change, status, code):
    response = world.call("nova", "POST", path_fn(), create_body(**change))
    refused(response, status, code)
    assert repo.creates == []


def test_personal_data_without_consent_is_refused_even_when_the_policy_allows_it(tmp_path):
    repo = Recorder()
    world = build_world(tmp_path, repo, policies=only_policy(allow_personal_data=True))
    refused(world.call("nova", "POST", agent_path(), create_body(personal_data=True)), 422, "consent_required")
    assert repo.creates == []
    ok = world.call(
        "nova", "POST", agent_path(),
        create_body(personal_data=True, consent={"basis": "consent_recorded", "reference": "c-1"}),
    )
    assert ok.status_code == 201 and len(repo.creates) == 1


def test_content_exactly_at_the_policy_limit_is_accepted_and_one_byte_more_is_not(world, repo):
    assert world.call("nova", "POST", agent_path(), create_body("x" * 16384)).status_code == 201
    refused(world.call("nova", "POST", agent_path(), create_body("é" * 8193)), 422, "content_too_large")  # 16386 bytes
    assert len(repo.creates) == 1


def test_a_multibyte_limit_is_counted_in_bytes_not_characters(world, repo):
    assert world.call("nova", "POST", agent_path(), create_body("é" * 8192)).status_code == 201  # 16384 bytes
    assert len(repo.creates) == 1


# --- retention and expiry are derived from the service clock ----------------------------------


def test_default_retention_and_ttl_come_from_the_policy_and_the_service_clock(world, repo):
    world.call("nova", "POST", agent_path(), create_body())
    world.call("nova", "POST", session_path(), create_body())
    agent_new, agent_ctx = repo.creates[0]
    session_new, _ = repo.creates[1]
    assert agent_new.retention_until == T0 + timedelta(days=90) and agent_new.expires_at is None
    assert session_new.expires_at == T0 + timedelta(seconds=3600) and session_new.retention_until is None
    assert agent_ctx.now == T0


def test_a_requested_retention_at_the_maximum_is_accepted_and_stored_as_asked(world, repo):
    latest = T0 + timedelta(days=365)
    assert world.call(
        "nova", "POST", agent_path(), create_body(retention_until=latest.isoformat())
    ).status_code == 201
    assert repo.creates[0][0].retention_until == latest
    refused(world.call(
        "nova", "POST", agent_path(),
        create_body(retention_until=(latest + timedelta(seconds=1)).isoformat()),
    ), 422, "retention_too_long")


def test_an_explicit_ttl_is_honoured_up_to_the_maximum(world, repo):
    assert world.call("nova", "POST", session_path(), create_body(ttl_seconds=86400)).status_code == 201
    assert repo.creates[0][0].expires_at == T0 + timedelta(seconds=86400)


# --- derived fields are never taken from the request ------------------------------------------


def test_the_owner_author_and_scope_are_derived_not_requested(world, repo):
    world.call("nova", "POST", agent_path(), create_body(), request_id="req-abc-123")
    new, ctx = repo.creates[0]
    assert (ctx.owner, ctx.principal, ctx.scope.tier, ctx.scope.key) == ("nova", "nova", "agent", "nova")
    assert new.provenance.author == "nova" and new.provenance.request_id == "req-abc-123"
    assert ctx.allowed_classes == ["public", "internal"]


# --- the generated-id retry loop ---------------------------------------------------------------


def test_a_colliding_generated_id_is_redrawn_and_never_reused(world, repo):
    repo.script = [ConflictError("id_exists"), TombstonedError("id_tombstoned")]
    response = world.call("nova", "POST", agent_path(), create_body())
    assert response.status_code == 201
    ids = [new.memory_id for new, _ in repo.creates]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert response.json()["memory_id"] == ids[-1]


def test_three_colliding_ids_end_in_a_generic_503(world, repo):
    repo.script = [ConflictError("id_exists")] * 3
    refused(world.call("nova", "POST", agent_path(), create_body()), 503)
    assert len(repo.creates) == 3


def test_an_idempotency_conflict_is_a_409_and_is_not_retried_with_a_new_id(world, repo):
    repo.script = [ConflictError("idempotency_conflict")]
    refused(world.call("nova", "POST", agent_path(), create_body()), 409, "idempotency_conflict")
    assert len(repo.creates) == 1


@pytest.mark.parametrize(
    "error, status",
    [
        (NotFoundError(), 404),
        (ConflictError("stale_rev"), 409),
        (TombstonedError("id_tombstoned"), 409),
        (RefusedError("classification_lowered"), 422),
        (StorageUnavailableError(), 503),
        (MemoryStoreError(), 503),
    ],
)
def test_storage_errors_map_to_fixed_bodies_that_carry_no_detail(world, repo, error, status):
    repo.script = [error]
    response = world.call("nova", "POST", agent_path("nova", MID, "retire"), {})
    body = refused(response, status)
    assert "Traceback" not in response.text and MID not in response.text
    if status == 503:
        assert body == {"detail": "temporarily unavailable"}


# --- the idempotency hash ----------------------------------------------------------------------


def _create(**changes) -> CreateRequest:
    return CreateRequest.model_validate_json(json.dumps(create_body(**changes)))


def test_the_request_hash_ignores_only_the_idempotency_key():
    scope = type("S", (), {"tier": "agent", "key": "nova"})()
    base = _hash("create", scope, None, _create(idempotency_key="key-aaaaaaaa"))
    assert base == _hash("create", scope, None, _create(idempotency_key="key-bbbbbbbb"))
    for changed in (_create(idempotency_key="key-aaaaaaaa", content="other"),
                    _create(idempotency_key="key-aaaaaaaa", classification="public"),
                    _create(idempotency_key="key-aaaaaaaa", tags=["x"])):
        assert _hash("create", scope, None, changed) != base
    other_scope = type("S", (), {"tier": "session", "key": "nova"})()
    assert _hash("create", other_scope, None, _create(idempotency_key="key-aaaaaaaa")) != base
    assert _hash("update", scope, None, _create(idempotency_key="key-aaaaaaaa")) != base
    assert _hash("create", scope, MID, _create(idempotency_key="key-aaaaaaaa")) != base


def test_an_update_hash_binds_the_record_id_and_expected_rev():
    scope = type("S", (), {"tier": "agent", "key": "nova"})()

    def update(**c):
        return UpdateRequest.model_validate_json(json.dumps({
            "idempotency_key": "key-aaaaaaaa", "expected_rev": 1, "content": "c",
            "provenance": {"kind": "agent_memory", "source_id": "s"}, **c,
        }))

    assert _hash("update", scope, MID, update()) != _hash("update", scope, MID, update(expected_rev=2))
    assert _hash("update", scope, MID, update()) != _hash("update", scope, MID.replace("1", "2"), update())


# --- list and query grammar --------------------------------------------------------------------


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=101", "limit=-1", "limit=abc", "limit=1.0", "limit=", "limit=%EF%BC%91", "limit=1&limit=2", "tag=UPPER", "tag=", "tag=a&tag=b", "scope=agent/scout",
              "agent=scout", "owner=scout", "tag=ok&extra=1"],
)
def test_a_malformed_list_query_is_a_400_and_never_reaches_storage(world, repo, query):
    refused(world.call("nova", "GET", agent_path() + "?" + query), 400)
    assert repo.lists == []


def test_a_valid_list_query_reaches_storage_with_the_callers_own_scope_and_ceiling(world, repo):
    assert world.call("nova", "GET", agent_path() + "?limit=100&tag=synthesis-meta").status_code == 200
    scope, owner, classes, tag, limit = repo.lists[0]
    assert (scope.tier, scope.key, owner, tag, limit) == ("agent", "nova", "nova", "synthesis-meta", 100)
    assert classes == ["public", "internal"]
    world.call("nova", "GET", agent_path())
    assert repo.lists[1][4] == 50  # the default limit


def test_a_query_string_on_a_single_record_read_is_refused(world, repo):
    refused(world.call("nova", "GET", agent_path("nova", MID) + "?limit=1"), 400)


def test_a_body_on_a_read_is_refused(world, repo):
    refused(world.call("nova", "GET", agent_path("nova", MID), raw_body=b"{}"), 400)


# --- malformed bodies fail safely --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b"null", b'"text"', b"\xff\xfe\x00", b"{" * 5000, b'{"a":' * 3000,
     b'{"idempotency_key":"key-12345678","content":"x\\ud800","provenance":{"kind":"agent_memory","source_id":"s"}}',
     b'{"idempotency_key":"key-12345678","content":"a\\u0000b","provenance":{"kind":"agent_memory","source_id":"s"}}'],
)
def test_a_malformed_create_body_is_a_generic_422_that_never_reaches_storage(world, repo, raw):
    response = world.call("nova", "POST", agent_path(), raw_body=raw)
    body = refused(response, 422, "invalid_body")
    assert body == {"detail": "invalid request", "code": "invalid_body"}
    assert repo.creates == []


def test_duplicate_json_keys_cannot_smuggle_a_second_value(world, repo):
    raw = (b'{"idempotency_key":"key-12345678","content":"first","content":"second",'
           b'"provenance":{"kind":"agent_memory","source_id":"s"}}')
    response = world.call("nova", "POST", agent_path(), raw_body=raw)
    if response.status_code == 201:  # last-wins is acceptable only if it is deterministic
        assert repo.creates[0][0].content == "second"
    else:
        refused(response, 422, "invalid_body")


def test_put_patch_and_delete_are_no_routes_even_when_authenticated(world):
    # only GET and POST are routes; the verb is refused after authentication
    for method in ("PUT", "PATCH", "DELETE"):
        refused(world.call("nova", method, agent_path("nova", MID), raw_body=b"{}"), 404)


# --- health ------------------------------------------------------------------------------------


def test_health_reports_unavailable_without_detail_when_storage_is_down(world, repo):
    repo.ping_error = StorageUnavailableError()
    response = world.client.get("/health")
    assert response.status_code == 503 and response.json() == {"status": "unavailable", "agent": "memory"}
    repo.ping_error = None
    assert world.client.get("/health").json() == {"status": "ok", "agent": "memory"}


# --- path parsing edge cases the service relies on ---------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [b"/memory/agent/Nova/records", b"/memory/agent/nova/records/", b"/memory/agent/nova//records",
     b"/memory/agent/nova/records/../records", b"/memory/agent/nova/records%2F", b"/memory/agent/nova/records?x=1",
     b"/memory/session/s 1/records", b"/memory/agent/nova/records/" + MID.upper().encode(),
     b"/memory/agent/nova/records/" + MID.encode() + b"/update/extra", b"/memory/agent/" + b"a" * 33 + b"/records",
     b"/memory/session/" + b"a" * 65 + b"/records", b"/memory/agent/nova/records\x00", b"/memory/agent/n\xc3\xb6va/records"],
)
def test_odd_paths_are_refused_by_the_parser(raw):
    with pytest.raises(Exception):
        parse_memory_path(raw)


def test_a_json_dump_of_an_error_body_never_contains_request_content(world):
    secret = "SECRET-CONTENT-12345"
    response = world.call("nova", "POST", agent_path(), create_body(secret, classification="restricted"))
    assert response.status_code == 422 or response.status_code == 403
    assert secret not in json.dumps(response.json())
