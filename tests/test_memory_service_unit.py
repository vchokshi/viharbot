"""Memory service: path canonicalization, the policy table and deny-by-default HTTP behavior.

No database: every request below must be refused before storage, and the repository used here
fails the test if it is touched. Offline, no model, no gateway.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from memory_helpers import (
    AGENTS,
    T0,
    ExplodingRepository,
    agent_path,
    build_world,
    create_body,
    session_path,
    update_body,
)

from viharbot import service_auth as sa
from viharbot.execution_bindings import default_bindings
from viharbot.identity_registry import default_registry
from viharbot.memory_service import migrate as migrate_module
from viharbot.memory_service.models import CreateRequest
from viharbot.memory_service.paths import PathError, ReservedScope, parse_memory_path
from viharbot.memory_service.policy import (
    DEFAULT_POLICY_FILE,
    CLASSIFICATIONS,
    Denied,
    MemoryPolicy,
    PolicyConfigError,
    PolicyProvider,
    load_policy,
    parse_policy,
)
from viharbot.memory_service.repository import GuardRefusedError, MutationResult
from viharbot.memory_service.service import new_memory_id

MID = "01234567-89ab-7def-8123-456789abcdef"


# --- path canonicalization -------------------------------------------------------------------


@pytest.mark.parametrize("path, tier, scope, memory_id, action", [
    ("/memory/agent/nova/records", "agent", "nova", None, None),
    (f"/memory/agent/nova/records/{MID}", "agent", "nova", MID, None),
    (f"/memory/agent/nova/records/{MID}/update", "agent", "nova", MID, "update"),
    (f"/memory/agent/nova/records/{MID}/classify", "agent", "nova", MID, "classify"),
    (f"/memory/agent/nova/records/{MID}/retire", "agent", "nova", MID, "retire"),
    (f"/memory/agent/nova/records/{MID}/erase", "agent", "nova", MID, "erase"),
    ("/memory/session/Sess_1.a-b/records", "session", "Sess_1.a-b", None, None),
    (f"/memory/session/s1/records/{MID}/erase", "session", "s1", MID, "erase"),
])
def test_canonical_paths_parse(path, tier, scope, memory_id, action):
    parsed = parse_memory_path(path.encode())
    assert (parsed.tier, parsed.scope_key, parsed.memory_id, parsed.action) == (tier, scope, memory_id, action)
    assert parsed.canonical() == path


@pytest.mark.parametrize("path", [
    "", "/", "/memory", "/memory/", "/health", "memory/agent/nova/records",
    "/memory/agent/nova/records/",  # trailing slash is another resource
    "/memory//agent/nova/records", "/memory/agent//records",
    "/memory/agent/nova/../scout/records", "/memory/agent/./records", "/memory/agent/nova/records/..",
    "/memory/agent/nova%2Fx/records", "/memory/agent/nova%2fx/records", "/memory/agent/no%76a/records",
    "/memory/agent/nova%00/records", "/memory/agent/nova%3A/records",
    "/memory/agent/Nova/records", "/memory/agent/nova x/records", "/memory/agent/nova\\x/records",
    "/memory/agent/nova;x/records", "/memory/agent/nova?x=1/records", "/memory/agent/nova#x/records",
    "/memory/agent/növa/records", "/memory/agent/9nova/records", "/memory/agent/" + "a" * 33 + "/records",
    "/memory/agent/nova/record", "/memory/agent/nova/records/not-a-uuid",
    "/memory/agent/nova/records/01234567-89AB-7def-8123-456789abcdef",  # upper case is not canonical
    f"/memory/agent/nova/records/{MID}/delete", f"/memory/agent/nova/records/{MID}/update/x",
    f"/memory/agent/nova/records/{MID}/", "/memory/other/nova/records", "/memory/agent",
    "/memory/session/.hidden/records", "/memory/session/" + "s" * 65 + "/records",
])
def test_non_canonical_or_unknown_paths_are_refused(path):
    with pytest.raises(PathError) as error:
        parse_memory_path(path.encode("utf-8"))
    assert not isinstance(error.value, ReservedScope)
    assert error.value.reason and "nova" not in error.value.reason  # a fixed code, never an echo


@pytest.mark.parametrize("path", ["/memory/eng/e1/records", "/memory/inst/topic/records", "/memory/eng/x"])
def test_engagement_and_institutional_paths_are_reserved_not_served(path):
    with pytest.raises(ReservedScope):
        parse_memory_path(path.encode())


def test_a_path_is_never_decoded_or_normalized():
    # the same bytes the verifier signs are the bytes that are parsed; there is no second form
    assert parse_memory_path(b"/memory/agent/nova/records").canonical() == "/memory/agent/nova/records"
    with pytest.raises(PathError):
        parse_memory_path(b"/memory/agent/nova/records%2F")


def test_generated_memory_ids_are_uuidv7_unique_and_time_ordered():
    ids = [new_memory_id() for _ in range(2000)]
    assert len(set(ids)) == 2000
    assert all(parse_memory_path(f"/memory/agent/nova/records/{i}".encode()).memory_id == i for i in ids[:50])
    assert all(i[14] == "7" and i[19] in "89ab" for i in ids)


# --- the policy table ------------------------------------------------------------------------


def test_the_shipped_policy_names_only_real_operational_identities():
    policy = load_policy(DEFAULT_POLICY_FILE)
    registry, bound = default_registry(), {b.identity_id for b in default_bindings().bindings}
    assert policy.agents, "an empty policy would deny everyone"
    for agent in policy.agents:
        assert agent.agent_id in registry, agent.agent_id
        assert agent.agent_id in bound, f"{agent.agent_id} has no execution binding"
        assert agent.agent_id != "kepler"
        assert agent.classification_ceiling == "internal" and agent.allow_personal_data is False
        assert agent.max_content_bytes <= 65_536


def test_deny_by_default():
    policy = load_policy()
    assert MemoryPolicy.empty().for_sub("nova") is None
    for sub in ("", "mallory", "NOVA", "nova ", "web", "kepler", "*", "nova\x00"):
        with pytest.raises(Denied) as error:
            policy.authorize(sub, "agent", "nova", "read")
        assert error.value.reason == "sub_not_in_policy"


def test_an_agent_reaches_only_its_own_agent_scope_and_only_named_operations():
    policy = load_policy()
    assert policy.authorize("nova", "agent", "nova", "read").agent_id == "nova"
    with pytest.raises(Denied):
        policy.authorize("nova", "agent", "scout", "read")
    with pytest.raises(Denied):
        policy.authorize("nova", "eng", "e1", "read")
    narrowed = parse_policy(DEFAULT_POLICY_FILE.read_text().replace(
        'agent_private = ["read", "create", "update", "classify", "retire", "erase"]',
        'agent_private = ["read"]'))
    assert narrowed.authorize("nova", "agent", "nova", "read")
    with pytest.raises(Denied) as error:
        narrowed.authorize("nova", "agent", "nova", "erase")
    assert error.value.reason == "operation_not_permitted"


GOOD = DEFAULT_POLICY_FILE.read_text()


@pytest.mark.parametrize("mutation", [
    lambda t: t.replace("version = 1", "version = 2"),
    lambda t: t.replace('classification_ceiling = "internal"', 'classification_ceiling = "top"', 1),
    lambda t: t.replace('subs = ["nova"]', 'subs = []', 1),
    lambda t: t.replace('subs = ["scout"]', 'subs = ["nova"]', 1),  # one sub, two agents
    lambda t: t.replace('agent_id = "scout"', 'agent_id = "nova"', 1),
    lambda t: t.replace('agent_id = "nova"', 'agent_id = "Nova"', 1),
    lambda t: t.replace('allow_personal_data = false', 'allow_personal_data = "no"', 1),
    lambda t: t.replace('max_content_bytes = 16384', 'max_content_bytes = 70000', 1),
    lambda t: t.replace('default_retention_days = 90', 'default_retention_days = 9999', 1),
    lambda t: t.replace('session_default_ttl_seconds = 3600', 'session_default_ttl_seconds = 999999', 1),
    lambda t: t.replace('"read", "create"', '"read", "read"', 1),
    lambda t: t.replace('"read", "create"', '"read", "delete"', 1),
    lambda t: t.replace('allow_personal_data = false', 'allow_personal_data = false\nextra = 1', 1),
    lambda t: t.replace('max_content_bytes = 16384\n', '', 1),
    lambda t: t + '\n[other]\nx = 1\n',
    lambda t: "not toml [",
])
def test_a_malformed_policy_is_refused_whole(mutation):
    with pytest.raises(PolicyConfigError):
        parse_policy(mutation(GOOD))


def test_a_missing_policy_file_stops_startup_and_a_bad_reload_denies_everything(tmp_path):
    with pytest.raises(PolicyConfigError):
        PolicyProvider.from_file(tmp_path / "absent.toml")
    path = tmp_path / "policy.toml"
    path.write_text(GOOD)
    provider = PolicyProvider.from_file(path)
    assert provider.current().for_sub("nova")
    path.write_text("version = 99\n")  # broken on reload: fail closed, not keep-last-known
    assert provider.current().for_sub("nova") is None
    path.write_text(GOOD + "\n# edited\n")
    assert provider.current().for_sub("nova")


def test_classification_scale_is_ordered_lowest_to_highest():
    assert CLASSIFICATIONS == ("public", "internal", "confidential", "restricted")


# --- request bodies --------------------------------------------------------------------------


@pytest.mark.parametrize("field, value", [
    ("owner_agent_id", "scout"), ("agent_id", "scout"), ("agent", "scout"), ("scope", "agent/scout"),
    ("engagement", "e1"), ("eng", "e1"), ("memory_id", MID), ("author", "scout"), ("lifecycle", "active"),
    ("rev", 5), ("version", 9), ("memory_type", "session"), ("owner", "scout"), ("tenant", "t"),
])
def test_a_body_can_never_name_an_agent_scope_owner_engagement_or_id(field, value):
    with pytest.raises(Exception):
        CreateRequest.model_validate_json(json.dumps({**create_body(), field: value}))


def test_body_validation_rejects_bad_content_and_shapes():
    bad = [
        {"content": ""}, {"content": "  \n"}, {"content": "a\x00b"}, {"content": "\ud800"},
        {"idempotency_key": "short"}, {"classification": "top"}, {"tags": ["UPPER"]},
        {"tags": ["a", "a"]}, {"tags": [f"t{i}" for i in range(9)]}, {"ttl_seconds": 0},
        {"provenance": {"kind": "agent_memory", "source_id": "x\ny"}},
        {"provenance": {"kind": "made_up", "source_id": "x"}},
        {"provenance": {"kind": "agent_memory", "source_id": "x", "author": "scout"}},
        {"consent": {"basis": "yes"}}, {"personal_data": "false"}, {"retention_until": "tomorrow"},
    ]
    for change in bad:
        with pytest.raises(Exception):
            CreateRequest.model_validate_json(json.dumps({**create_body(), **change}, ensure_ascii=False).encode("utf-8", "surrogatepass"))


# --- deny by default over HTTP (the repository must never be reached) ------------------------


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path, ExplodingRepository())


def refused_generically(response, status):
    assert response.status_code == status
    body = response.json()
    assert set(body) <= {"detail", "code"}
    assert "nova" not in response.text and "scout" not in response.text and MID not in response.text
    return body


def test_a_missing_token_gets_nothing(world):
    for method, target in (("GET", agent_path()), ("POST", agent_path()),
                           ("GET", agent_path("nova", MID)), ("POST", agent_path("nova", MID, "erase")),
                           ("GET", session_path()), ("GET", "/memory/eng/e1/records"),
                           ("GET", "/memory/anything/else")):
        response = world.call(None, method, target, {"x": 1} if method == "POST" else None)
        assert refused_generically(response, 401) == {"detail": "authentication failed"}


def test_a_garbage_or_replayed_token_gets_nothing(world):
    assert world.call(None, "GET", agent_path(), token="not.a.token").status_code == 401
    # mallory's token verifies, so it is consumed by the 403; presenting it again is a replay
    token = world.token("mallory", "GET", agent_path("mallory"))
    assert world.call(None, "GET", agent_path("mallory"), token=token).status_code == 403
    assert world.call(None, "GET", agent_path("mallory"), token=token).status_code == 401


def test_a_forged_sub_is_refused(world):
    # signed with nova's key but claiming to be scout: the registry binds the kid to one sub
    response = world.call("nova", "GET", agent_path("scout"), claim_sub="scout")
    assert response.status_code == 401
    other = world.token("nova", "GET", agent_path("nova"))
    # a token minted for one route cannot be reused on another (request binding)
    assert world.call(None, "GET", agent_path("scout"), token=other).status_code == 401


def test_an_unknown_kid_is_refused(world):
    token = sa.mint_assertion(
        kid="nobody", sub="nova", audience="memory", capability="memory.read", engagement="unscoped",
        classification="unclassified", method="GET", target=agent_path(),
        key_path=str(world.keys["nova"][1]),
    )
    assert world.call(None, "GET", agent_path(), token=token).status_code == 401


def test_a_sub_not_named_in_the_policy_gets_nothing(world):
    # mallory holds a valid registered key and a valid token, but the policy table does not name it
    bodies = []
    for method, target, body in (
        ("GET", agent_path("mallory"), None), ("GET", agent_path("nova"), None),
        ("POST", agent_path("mallory"), create_body()), ("POST", session_path(), create_body()),
        ("GET", session_path("s1", MID), None), ("POST", agent_path("nova", MID, "erase"), {"reason": "other"}),
    ):
        bodies.append(refused_generically(world.call("mallory", method, target, body), 403))
    assert all(b == {"detail": "forbidden"} for b in bodies)


def test_the_wrong_agent_gets_the_same_generic_refusal(world):
    unnamed = refused_generically(world.call("mallory", "GET", agent_path("nova")), 403)
    wrong = refused_generically(world.call("nova", "GET", agent_path("scout")), 403)
    unreal = refused_generically(world.call("nova", "GET", agent_path("zed")), 403)
    assert unnamed == wrong == unreal == {"detail": "forbidden"}
    for agent in AGENTS:
        other = next(a for a in AGENTS if a != agent)
        assert world.call(agent, "POST", agent_path(other), create_body()).status_code == 403
        assert world.call(agent, "GET", agent_path(other, MID)).status_code == 403
        assert world.call(agent, "POST", agent_path(other, MID, "erase"), {"reason": "other"}).status_code == 403


def test_capability_is_enforced_per_method(world):
    assert world.call("nova", "GET", agent_path(), cap="memory.write").status_code == 403
    assert world.call("nova", "POST", agent_path(), create_body(), cap="memory.read").status_code == 403


def test_scope_in_a_query_parameter_is_refused_not_honored(world):
    for query in ("?agent=scout", "?agent_id=scout", "?scope=agent/scout", "?owner=scout",
                  "?engagement=e1", "?tag=ok&agent=scout", "?limit=5&limit=6"):
        response = world.call("nova", "GET", agent_path() + query)
        assert refused_generically(response, 400)["detail"] == "bad request"
    assert world.call("nova", "POST", agent_path() + "?agent=scout", create_body()).status_code == 400
    assert world.call("nova", "GET", agent_path("nova", MID) + "?agent=scout").status_code == 400
    assert world.call("nova", "GET", agent_path() + "?limit=0").status_code == 400
    assert world.call("nova", "GET", agent_path() + "?limit=101").status_code == 400
    assert world.call("nova", "GET", agent_path() + "?tag=BAD").status_code == 400


def test_scope_fields_in_a_body_are_refused_whole(world):
    for extra in ({"agent_id": "scout"}, {"owner_agent_id": "scout"}, {"scope": "agent/scout"},
                  {"engagement": "e1"}, {"agent_id": "nova"}):
        response = world.call("nova", "POST", agent_path(), {**create_body(), **extra})
        assert refused_generically(response, 422)["code"] == "invalid_body"


def test_error_bodies_never_echo_the_request(world):
    # Built at runtime so no `name = "value"` literal sits in source (secret scanners match those).
    canary = "-".join(("ECHO", "CANARY", "1f3a"))
    response = world.call("nova", "POST", agent_path(), {**create_body(canary), "unknown_field": canary})
    assert response.status_code == 422 and canary not in response.text


def test_non_canonical_paths_are_refused_after_authentication(world):
    for target in (
        "/memory/agent/nova%3A/records", "/memory/agent/nova/records/", "/memory/agent/Nova/records",
        "/memory/agent/nova/records/not-a-uuid", "/memory/session/%20x/records",
        f"/memory/agent/nova/records/{MID}/update/extra",
    ):
        response = world.call("nova", "GET", target)
        assert response.status_code == 400, target
        assert response.json() == {"detail": "bad request"}


def test_reserved_scopes_are_not_served(world):
    for target in ("/memory/eng/e1/records", "/memory/inst/topic/records", f"/memory/eng/e1/records/{MID}"):
        for method in ("GET", "POST"):
            response = world.call("nova", method, target, {} if method == "POST" else None)
            assert response.status_code == 501 and response.json() == {"detail": "not implemented"}


def test_unsupported_methods_and_routes_are_not_found(world):
    assert world.call("nova", "DELETE", agent_path("nova", MID)).status_code == 404
    assert world.call("nova", "PUT", agent_path("nova", MID), {}).status_code == 404
    assert world.call("nova", "GET", agent_path("nova", MID, "erase")).status_code == 404
    assert world.call("nova", "POST", agent_path("nova", MID), {}).status_code == 404
    assert world.call("nova", "GET", agent_path("nova", MID), raw_body=b"{}").status_code == 400


def test_the_docs_and_schema_routes_are_off(world):
    for target in ("/docs", "/redoc", "/openapi.json"):
        assert world.client.get(target).status_code == 404


def test_an_oversized_body_is_refused_before_authentication(tmp_path):
    world = build_world(tmp_path, ExplodingRepository(), max_body_bytes=1024)
    response = world.client.post(
        agent_path(), content=b"x" * 2048, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


def test_the_service_refuses_to_start_without_a_key_registry(tmp_path):
    from viharbot.memory_service.app import create_app_from_environment

    with pytest.raises(sa.ServiceAuthConfigError):
        create_app_from_environment({})
    with pytest.raises(sa.ServiceAuthConfigError):
        create_app_from_environment({"VIHARBOT_KEY_REGISTRY": str(tmp_path / "missing.toml")})
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    with pytest.raises(sa.ServiceAuthConfigError):
        create_app_from_environment({"VIHARBOT_KEY_REGISTRY": str(empty)})


def test_database_settings_have_no_defaults_for_credentials():
    from viharbot.memory_service.app import conninfo_from_environment

    full = {"MEMORY_DB_HOST": "h", "MEMORY_DB_NAME": "n", "MEMORY_DB_USER": "u", "MEMORY_DB_PASSWORD": "p"}
    assert "password=p" in conninfo_from_environment(full)
    for missing in full:
        with pytest.raises(sa.ServiceAuthConfigError):
            conninfo_from_environment({k: v for k, v in full.items() if k != missing})


# --- migrations (no database) ----------------------------------------------------------------


def test_migrations_are_numbered_plain_sql_without_gaps(tmp_path):
    found = migrate_module.discover()
    assert [v for v, _, _ in found] == list(range(1, len(found) + 1))
    for _, name, text in found:
        assert name.endswith(".sql") and text.strip()
    (tmp_path / "0001_a.sql").write_text("select 1;")
    (tmp_path / "0003_c.sql").write_text("select 1;")
    with pytest.raises(migrate_module.MigrationError):
        migrate_module.discover(tmp_path)
    (tmp_path / "0003_c.sql").rename(tmp_path / "bad name.sql")
    with pytest.raises(migrate_module.MigrationError):
        migrate_module.discover(tmp_path)


def test_the_first_migration_grants_the_service_role_no_ddl_or_truncate():
    text = migrate_module.discover()[0][2]
    grants = [line for line in text.splitlines() if line.startswith("GRANT")]
    assert grants and all("memory_service" in line for line in grants)
    for line in grants:
        assert not any(word in line for word in ("ALL", "TRUNCATE", "REFERENCES", "TRIGGER", "CREATE"))
    assert "memory.tombstones TO memory_service" in text and "DELETE ON memory.tombstones" not in text
    assert "REVOKE ALL ON SCHEMA memory FROM PUBLIC" in text
    assert "memory.schema_migrations" not in "\n".join(grants)  # the service cannot see migration state


def test_no_agent_module_calls_the_memory_service_yet():
    root = Path(__file__).resolve().parent.parent / "src" / "viharbot"
    offenders = []
    for path in root.rglob("*.py"):
        if "memory_service" in path.parts:
            continue
        if "memory_service" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_remember_and_its_jsonl_traces_are_unchanged(tmp_path, monkeypatch):
    from viharbot.memory import remember

    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    remember("nova", "req-1", {"ok": True})
    line = (tmp_path / "nova" / "memory.jsonl").read_text().strip()
    assert set(json.loads(line)) == {"timestamp", "request_id", "result"}


def test_replace_helper_is_available_for_policy_variants():
    changed = replace(load_policy().agents[0], classification_ceiling="confidential")
    assert changed.classification_ceiling == "confidential"


# --- the in-transaction guard is bound to the policy the request was validated against ---------


class GuardProbe(ExplodingRepository):
    """Stands in for the storage transaction.

    Before the guard runs it lets a policy change land (``race``), as if the policy file reloaded
    after authorization and before the record lock was taken. It then reports whether the guard
    let the write through, exactly as the PostgreSQL adapter does after locking the row.
    """

    def __init__(self) -> None:
        self.race = None
        self.landed: list[str] = []

    def _mutate(self, name: str, status: int, ctx) -> MutationResult:
        if self.race:
            self.race()
        if not ctx.guard():
            raise GuardRefusedError()
        self.landed.append(name)
        return MutationResult(status, {"memory_id": MID, "state": "ok"})

    def create(self, new, ctx):
        return self._mutate("create", 201, ctx)

    def update(self, memory_id, expected_rev, change, ctx):
        return self._mutate("update", 200, ctx)

    def classify(self, memory_id, expected_rev, classification, ctx):
        return self._mutate("classify", 200, ctx)

    def retire(self, memory_id, ctx):
        return self._mutate("retire", 200, ctx)

    def erase(self, memory_id, reason, ctx):
        return self._mutate("erase", 200, ctx)


def _nova(**changes) -> MemoryPolicy:
    """The shipped policy with Nova's fields changed."""
    agents = [replace(a, **changes) if a.agent_id == "nova" else a for a in load_policy().agents]
    return MemoryPolicy(agents)


def _nova_sub_now_belongs_to_scout() -> MemoryPolicy:
    """A remap: the sub that was Nova's now resolves to Scout, and Nova names no sub."""
    agents = []
    for agent in load_policy().agents:
        if agent.agent_id == "nova":
            agent = replace(agent, subs=frozenset())
        elif agent.agent_id == "scout":
            agent = replace(agent, subs=agent.subs | {"nova"})
        agents.append(agent)
    return MemoryPolicy(agents)


def _request(operation: str, tier: str = "agent") -> tuple[str, dict]:
    path = (lambda *a: agent_path("nova", *a)) if tier == "agent" else (lambda *a: session_path("s1", *a))
    if operation == "create":
        return path(), create_body("a plain note", classification="internal")
    if operation == "update":
        return path(MID, "update"), update_body(1, "a revised note", classification="internal")
    if operation == "classify":
        return path(MID, "classify"), {
            "idempotency_key": "ceiling-0001", "expected_rev": 1, "classification": "internal",
        }
    if operation == "retire":
        return path(MID, "retire"), {}
    return path(MID, "erase"), {"reason": "other"}


def _drive(tmp_path, operation, tier, start: MemoryPolicy, race: MemoryPolicy | None):
    probe = GuardProbe()
    world = build_world(tmp_path, probe, policies=PolicyProvider(start))
    if race is not None:
        probe.race = lambda: world.policies.replace(race)
    path, body = _request(operation, tier)
    response = world.call("nova", "POST", path, body)
    return response, probe


REFUSED = (403, {"detail": "forbidden"})


@pytest.mark.parametrize("operation", ["create", "update", "classify", "retire", "erase"])
@pytest.mark.parametrize("tier", ["agent", "session"])
def test_an_unchanged_policy_lets_every_write_through(tmp_path, operation, tier):
    response, probe = _drive(tmp_path, operation, tier, load_policy(), None)
    assert response.status_code in (200, 201) and probe.landed == [operation]


@pytest.mark.parametrize("operation", ["create", "update", "classify"])
def test_a_lowered_classification_ceiling_refuses_the_write(tmp_path, operation):
    response, probe = _drive(tmp_path, operation, "agent", load_policy(), _nova(classification_ceiling="public"))
    assert (response.status_code, response.json()) == REFUSED and probe.landed == []


def test_a_lowered_content_limit_refuses_the_write(tmp_path):
    response, probe = _drive(tmp_path, "create", "agent", load_policy(), _nova(max_content_bytes=4))
    assert (response.status_code, response.json()) == REFUSED and probe.landed == []
    response, probe = _drive(tmp_path, "update", "agent", load_policy(), _nova(max_content_bytes=4))
    assert (response.status_code, response.json()) == REFUSED and probe.landed == []


def test_a_revoked_personal_data_permission_refuses_the_write(tmp_path):
    permissive = _nova(allow_personal_data=True)
    probe = GuardProbe()
    world = build_world(tmp_path, probe, policies=PolicyProvider(permissive))
    probe.race = lambda: world.policies.replace(_nova(allow_personal_data=False))
    body = create_body(
        "a note about a named person", personal_data=True,
        consent={"basis": "consent_recorded", "reference": "consent-1"},
    )
    response = world.call("nova", "POST", agent_path(), body)
    assert (response.status_code, response.json()) == REFUSED and probe.landed == []


def test_a_lowered_retention_limit_refuses_an_agent_record_write(tmp_path):
    response, probe = _drive(tmp_path, "create", "agent", load_policy(), _nova(max_retention_days=1))
    assert (response.status_code, response.json()) == REFUSED and probe.landed == []


def test_a_lowered_session_ttl_limit_refuses_a_session_record_write(tmp_path):
    response, probe = _drive(tmp_path, "create", "session", load_policy(), _nova(session_max_ttl_seconds=1))
    assert (response.status_code, response.json()) == REFUSED and probe.landed == []


@pytest.mark.parametrize("operation", ["create", "update", "classify", "retire", "erase"])
def test_a_sub_remapped_to_another_agent_is_refused_on_a_session_path(tmp_path, operation):
    """A session id only partitions the caller's own records, so authorization alone would pass
    for the new agent while the write still targets the former owner's partition."""
    response, probe = _drive(tmp_path, operation, "session", load_policy(), _nova_sub_now_belongs_to_scout())
    assert (response.status_code, response.json()) == REFUSED and probe.landed == []


@pytest.mark.parametrize("operation", ["retire", "erase"])
@pytest.mark.parametrize("tier", ["agent", "session"])
def test_an_unrelated_limit_change_does_not_stop_a_retire_or_an_erase(tmp_path, operation, tier):
    """These carry no payload validated against limits, so a tighter limit must not block them."""
    response, probe = _drive(
        tmp_path, operation, tier, load_policy(), _nova(max_content_bytes=4, classification_ceiling="public"),
    )
    assert response.status_code == 200 and probe.landed == [operation]


def test_a_revoked_operation_still_refuses_every_write(tmp_path):
    revoked = _nova(agent_private=frozenset(), session=frozenset())
    for operation in ("create", "update", "classify", "retire", "erase"):
        response, probe = _drive(tmp_path, operation, "agent", load_policy(), revoked)
        assert (response.status_code, response.json()) == REFUSED and probe.landed == [], operation


def test_an_unexpected_error_is_a_generic_500_that_never_echoes_its_message(tmp_path):
    class Broken(ExplodingRepository):
        def get(self, *args, **kwargs):
            raise RuntimeError("LEAK-CANARY content and password")

    world = build_world(tmp_path, Broken())
    response = world.call("nova", "GET", agent_path("nova", MID))
    assert response.status_code == 500 and response.json() == {"detail": "internal error"}
    assert "LEAK-CANARY" not in response.text


# --- expiry is judged on a fresh service-clock read after the row lock (Sage review, PR 1) -------


def test_the_write_context_carries_the_service_clock_and_a_fresh_read_is_not_the_captured_now(tmp_path):
    from memory_helpers import Clock

    seen = []

    class Capture(ExplodingRepository):
        def update(self, memory_id, expected_rev, change, ctx):
            seen.append(ctx)
            return MutationResult(200, {"memory_id": memory_id, "state": "updated"})

    world = build_world(tmp_path, Capture(), clock=Clock())
    world.call("nova", "POST", agent_path("nova", MID, "update"), update_body(1))
    ctx = seen[0]
    started = ctx.now
    world.clock.advance(seconds=45)
    assert ctx.now == started  # the stamp for creation and audit does not move
    assert ctx.clock() == started + timedelta(seconds=45)  # the carried clock is the live one


def test_the_adapter_reads_the_clock_after_the_lock_never_before(monkeypatch):
    from viharbot.memory_service.postgres import PostgresMemoryRepository

    order = []

    class Tx:
        def execute(self, name, params=()):
            order.append(name)
            return self

        def fetchone(self):
            # memory_id, lifecycle, rev, version, classification, retention_until, expires_at
            return ("m", "active", 1, 1, "internal", None, None) if order[-1] == "record_lock" else (1,)

        rowcount = 1

    class Boundary:
        def __enter__(self):
            return Tx()

        def __exit__(self, *exc):
            return False

    repo = PostgresMemoryRepository("postgresql://unused")
    monkeypatch.setattr(repo, "_transaction", lambda operation: Boundary())

    def clock():
        order.append("CLOCK")
        return T0

    from viharbot.memory_service.repository import NewVersion, Provenance, Scope, WriteContext

    ctx = WriteContext(
        principal="nova", owner="nova", scope=Scope("agent", "nova"), request_id="r", now=T0,
        clock=clock, guard=lambda: True, allowed_classes=None, idempotency_key="k" * 12, request_hash="h",
    )
    prov = Provenance("agent_memory", "s", "nova", "r")
    repo.update("m", 1, NewVersion("c", prov, None, None), ctx)
    assert order.index("CLOCK") > order.index("record_lock")
    assert order.count("CLOCK") == 1
    order.clear()
    repo.classify("m", 1, "confidential", ctx)
    assert order.index("CLOCK") > order.index("record_lock")
