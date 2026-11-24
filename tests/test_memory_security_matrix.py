"""Memory service security matrix, part 1 (issue #40, Part of #40): authentication, key registry,
replay, unauthorized principals, namespace isolation and privilege-escalation attempts.

Every test runs the REAL ADR-006 A2 verifier, the REAL policy table (``memory_policy.toml``) and the
REAL FastAPI app. Storage is an in-memory fake (``FakeRepo``) or a repository that fails the test
if it is reached, so a "refused" result is proof the request never got near storage. Offline: no
model, no gateway, no network, no database. Throwaway keys are generated per test; nothing secret
is written in this file.

The matrix (docs/memory-security-findings.md) cites these tests by name.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from charlie_memory_helpers import FakeRepo
from memory_helpers import (
    READ,
    WRITE,
    ExplodingRepository,
    agent_path,
    build_world,
    create_body,
    new_key,
    session_path,
)

from viharbot import service_auth as sa
from viharbot.memory_service.app import AUDIENCE, create_app
from viharbot.memory_service.policy import PolicyProvider, load_policy

MID = "01234567-89ab-7def-8123-456789abcdef"
GENERIC_403 = {"detail": "forbidden"}


def raw_token(world, sub, method, target, body=b"", cap=None):
    """A VALIDLY SIGNED token whose ``tgt`` is the target verbatim, skipping the issuer's own
    canonicalization (which refuses to mint these). It models an attacker who holds the signing
    key, or a buggy issuer, so the CALLEE's checks are what is under test."""
    import time
    from uuid import uuid4

    kid, key_file = world.keys[sub]
    header = {"alg": sa.ALGORITHM, "typ": sa.ASSERTION_TYP, "kid": kid}
    now = int(time.time())
    claims = {"iss": sub, "sub": sub, "aud": AUDIENCE, "exp": now + 30, "iat": now, "jti": uuid4().hex,
              "cap": cap or (READ if method == "GET" else WRITE), "eng": "unscoped", "cls": "unclassified",
              "mth": method, "tgt": target, "req_sha256": hashlib.sha256(body).hexdigest()}
    seg = lambda v: sa.b64url_encode(json.dumps(v, separators=(",", ":")).encode())  # noqa: E731
    signing_input = f"{seg(header)}.{seg(claims)}"
    signature = sa.load_signing_key(str(key_file)).sign(signing_input.encode("ascii"))
    return f"{signing_input}.{sa.b64url_encode(signature)}"


@pytest.fixture
def world(tmp_path):
    """Charlie, Nova, Scout, Cipher and a valid-key stranger (``mallory``), in-memory storage."""
    return build_world(tmp_path, FakeRepo(), extra_subs=("charlie", "mallory"))


@pytest.fixture
def sealed(tmp_path):
    """The same wiring over a repository that fails the test if it is touched at all."""
    return build_world(tmp_path, ExplodingRepository(), extra_subs=("charlie", "mallory"))


# --- Charlie's signed assertions ------------------------------------------------------------------


def test_positive_control_charlie_can_write_and_read_only_its_own_scope(world):
    created = world.call("charlie", "POST", agent_path("charlie"), create_body())
    assert created.status_code == 201
    listed = world.call("charlie", "GET", agent_path("charlie") + "?limit=5")
    assert listed.status_code == 200 and listed.json()["count"] == 1


def test_a_service_key_cannot_claim_another_subject(sealed):
    # Charlie's key, a token that says it is nova: iss/sub must equal the registry's sub for the kid.
    response = sealed.call("charlie", "POST", agent_path("nova"), create_body(), claim_sub="nova")
    assert response.status_code == 401


def test_a_missing_empty_or_garbage_assertion_is_401_and_never_reaches_storage(sealed):
    for token in (None, "", "a.b.c", "x" * 20, "a.b", "....", "e30.e30.e30"):
        headers = {"content-type": "application/json"}
        if token is not None:
            headers[sa.ASSERTION_HEADER] = token
        response = sealed.client.post(agent_path("charlie"), content=json.dumps(create_body()), headers=headers)
        assert response.status_code == 401, token
    assert sealed.client.get(agent_path("charlie")).status_code == 401


def _resign_header(world, sub, method, target, body, mutate):
    token = world.token(sub, method, target, body)
    head, payload, sig = token.split(".")
    header = json.loads(sa.b64url_decode(head))
    mutate(header)
    new_head = sa.b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    return f"{new_head}.{payload}.{sig}"


def test_alg_none_and_hmac_confusion_are_refused(sealed):
    target, body = agent_path("charlie"), json.dumps(create_body()).encode()
    for alg in ("none", "None", "HS256", "HS512", "RS256", "ES256", ""):
        token = _resign_header(sealed, "charlie", "POST", target, body, lambda h, a=alg: h.update(alg=a))
        response = sealed.call(None, "POST", target, raw_body=body, token=token)
        assert response.status_code == 401, alg
    # HS256 "signed" with the PUBLIC key bytes as the shared secret (classic confusion attack).
    _, public, _ = new_key()
    head = sa.b64url_encode(json.dumps({"alg": "HS256", "typ": sa.ASSERTION_TYP, "kid": "charlie-test"}).encode())
    payload = sealed.token("charlie", "POST", target, body).split(".")[1]
    mac = hmac.new(sa.b64url_decode(public), f"{head}.{payload}".encode(), hashlib.sha256).digest()
    forged = f"{head}.{payload}.{sa.b64url_encode(mac)}"
    assert sealed.call(None, "POST", target, raw_body=body, token=forged).status_code == 401


def test_embedded_key_headers_are_ignored_never_fetched_or_trusted(sealed):
    """jku, x5u, x5c and jwk never select a key: only the local registry's kid does."""
    attacker, public, _ = new_key()
    target, body = agent_path("charlie"), json.dumps(create_body()).encode()
    token = sealed.token("charlie", "POST", target, body)
    head, payload, _sig = token.split(".")
    header = json.loads(sa.b64url_decode(head))
    header.update(kid="attacker-kid", jwk={"kty": "OKP", "crv": "Ed25519", "x": public},
                  jku="http://127.0.0.1:1/keys", x5u="http://127.0.0.1:1/c", x5c=["AAAA"])
    new_head = sa.b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    signature = attacker.sign(f"{new_head}.{payload}".encode())
    forged = f"{new_head}.{payload}.{sa.b64url_encode(signature)}"
    assert sealed.call(None, "POST", target, raw_body=body, token=forged).status_code == 401
    # Same header junk on a known kid still needs a signature by the registered key.
    header["kid"] = "charlie-test"
    new_head = sa.b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    signature = attacker.sign(f"{new_head}.{payload}".encode())
    forged = f"{new_head}.{payload}.{sa.b64url_encode(signature)}"
    assert sealed.call(None, "POST", target, raw_body=body, token=forged).status_code == 401


def test_a_grant_type_token_and_a_foreign_audience_are_refused(sealed):
    target, body = agent_path("charlie"), json.dumps(create_body()).encode()
    wrong_typ = _resign_header(sealed, "charlie", "POST", target, body, lambda h: h.update(typ="viharbot-grant+jwt"))
    assert sealed.call(None, "POST", target, raw_body=body, token=wrong_typ).status_code == 401
    kid, key_file = sealed.keys["charlie"]
    for audience in ("research", "synthesis", "memory-dev", "", "MEMORY"):
        try:
            token = sa.mint_assertion(kid=kid, sub="charlie", audience=audience, capability=WRITE,
                                      engagement="unscoped", classification="unclassified", method="POST",
                                      target=target, body=body, key_path=str(key_file))
        except ValueError:
            continue
        assert sealed.call(None, "POST", target, raw_body=body, token=token).status_code == 401, audience


def test_an_expired_or_future_dated_token_is_refused(sealed):
    import time

    target, body = agent_path("charlie"), json.dumps(create_body()).encode()
    kid, key_file = sealed.keys["charlie"]
    for offset in (-3600, -120, 120, 3600):
        token = sa.mint_assertion(kid=kid, sub="charlie", audience=AUDIENCE, capability=WRITE,
                                  engagement="unscoped", classification="unclassified", method="POST",
                                  target=target, body=body, key_path=str(key_file),
                                  now=time.time() + offset, lifetime=30)
        assert sealed.call(None, "POST", target, raw_body=body, token=token).status_code == 401, offset


# --- replay resistance and request binding --------------------------------------------------------


def test_a_captured_token_works_once_and_a_replay_is_refused(world):
    target, body = agent_path("charlie"), json.dumps(create_body()).encode()
    token = world.token("charlie", "POST", target, body)
    first = world.call(None, "POST", target, raw_body=body, token=token)
    again = world.call(None, "POST", target, raw_body=body, token=token)
    assert first.status_code == 201 and again.status_code == 401
    assert len(world.repo.records) == 1  # the replayed write did not run


def test_a_replayed_read_token_is_refused_too(world):
    target = agent_path("charlie")
    token = world.token("charlie", "GET", target)
    assert world.call(None, "GET", target, token=token).status_code == 200
    assert world.call(None, "GET", target, token=token).status_code == 401


def test_the_replay_cache_is_per_process_so_a_restart_forgets_it(tmp_path):
    """Recorded limitation (LOW): ADR-006 item 7 and the design's single-instance rule. A second
    authenticator (a restarted or second instance) has an empty cache and accepts a token that the
    first already consumed, for the rest of its life (at most 60 s plus 10 s skew), and the
    request body hash and target binding still apply. TLS in Stage is what limits capture."""
    from fastapi.testclient import TestClient

    first = build_world(tmp_path, FakeRepo(), extra_subs=("charlie",))
    target = agent_path("charlie")
    token = first.token("charlie", "GET", target)
    assert first.call(None, "GET", target, token=token).status_code == 200
    assert first.call(None, "GET", target, token=token).status_code == 401
    registry = sa.KeyRegistry(first.directory / "registry.toml")
    registry.load_initial()
    restarted = TestClient(create_app(
        repository=FakeRepo(), authenticator=sa.ServiceAuthenticator(audience=AUDIENCE, registry=registry),
        policies=PolicyProvider(load_policy())))
    headers = {sa.ASSERTION_HEADER: token}
    assert restarted.get(target, headers=headers).status_code == 200  # accepted: empty cache
    assert restarted.get(target, headers=headers).status_code == 401  # but only once per process
    assert restarted.get(target + "?limit=1", headers=headers).status_code == 401  # binding still holds


def test_a_token_is_bound_to_method_path_query_and_body(sealed):
    body = json.dumps(create_body()).encode()
    # method: a POST token replayed as a GET, and a GET token used as a POST
    post_token = sealed.token("charlie", "POST", agent_path("charlie"), body)
    assert sealed.call(None, "GET", agent_path("charlie"), token=post_token).status_code in (401, 403)
    get_token = sealed.token("charlie", "GET", agent_path("charlie"))
    assert sealed.call(None, "POST", agent_path("charlie"), raw_body=body, token=get_token).status_code in (401, 403)
    # path: a token for charlie's scope presented on nova's
    token = sealed.token("charlie", "POST", agent_path("charlie"), body)
    assert sealed.call(None, "POST", agent_path("nova"), raw_body=body, token=token).status_code == 401
    # query: signed without a query, sent with one (and the reverse)
    token = sealed.token("charlie", "GET", agent_path("charlie"))
    assert sealed.call(None, "GET", agent_path("charlie") + "?limit=1", token=token).status_code == 401
    token = sealed.token("charlie", "GET", agent_path("charlie") + "?limit=1")
    assert sealed.call(None, "GET", agent_path("charlie"), token=token).status_code == 401
    # body: one byte changed after signing
    token = sealed.token("charlie", "POST", agent_path("charlie"), body)
    assert sealed.call(None, "POST", agent_path("charlie"), raw_body=body + b" ", token=token).status_code == 401


def test_query_parameter_order_and_duplicates_are_part_of_the_signature(sealed):
    token = sealed.token("charlie", "GET", agent_path("charlie") + "?tag=a&limit=2")
    assert sealed.call(None, "GET", agent_path("charlie") + "?limit=2&tag=a", token=token).status_code == 401
    token = sealed.token("charlie", "GET", agent_path("charlie") + "?limit=2")
    assert sealed.call(None, "GET", agent_path("charlie") + "?limit=2&limit=3", token=token).status_code == 401


def test_the_route_capability_is_enforced_in_both_directions(sealed):
    body = json.dumps(create_body()).encode()
    assert sealed.call("charlie", "POST", agent_path("charlie"), raw_body=body, cap=READ).status_code == 403
    assert sealed.call("charlie", "GET", agent_path("charlie"), cap=WRITE).status_code == 403
    for cap in ("specialist.process", "memory.admin", "memory.*", "research.run"):
        assert sealed.call("charlie", "GET", agent_path("charlie"), cap=cap).status_code in (401, 403), cap


# --- key registry: unknown, wrong, rotated and retired key ids; stale registry fails closed -------


class FakeTime:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _registry_app(tmp_path: Path, clock: FakeTime, kids: dict[str, tuple[str, str, tuple[str, ...]]]):
    """``kids``: kid -> (sub, public key, capabilities). Returns (client, registry file, rewrite)."""
    from fastapi.testclient import TestClient

    path = tmp_path / "rotating.toml"

    def rewrite(entries: dict[str, tuple[str, str, tuple[str, ...]]]) -> None:
        path.write_text("\n".join(
            f'[[keys]]\nkid = "{kid}"\nsub = "{sub}"\npublic_key = "{pub}"\n'
            f'capabilities = [{", ".join(chr(34) + c + chr(34) for c in caps)}]\n'
            for kid, (sub, pub, caps) in entries.items()))

    rewrite(kids)
    registry = sa.KeyRegistry(path, clock=clock)
    registry.load_initial()
    authenticator = sa.ServiceAuthenticator(audience=AUDIENCE, registry=registry, clock=clock)
    app = create_app(repository=FakeRepo(), authenticator=authenticator, policies=PolicyProvider(load_policy()))
    return TestClient(app), path, rewrite


def _signed_get(tmp_path, clock, kid, seed_file, target):
    token = sa.mint_assertion(kid=kid, sub="charlie", audience=AUDIENCE, capability=READ,
                              engagement="unscoped", classification="unclassified", method="GET",
                              target=target, key_path=str(seed_file), now=clock.now)
    return {sa.ASSERTION_HEADER: token}


def test_rotation_overlap_retirement_and_unknown_kid(tmp_path):
    clock = FakeTime()
    _, pub_old, seed_old = new_key()
    _, pub_new, seed_new = new_key()
    old_file, new_file = tmp_path / "old.seed", tmp_path / "new.seed"
    old_file.write_text(seed_old)
    new_file.write_text(seed_new)
    caps = (READ, WRITE)
    client, _path, rewrite = _registry_app(tmp_path, clock, {"charlie-old": ("charlie", pub_old, caps)})
    target = agent_path("charlie")
    get = lambda kid, f: client.get(target, headers=_signed_get(tmp_path, clock, kid, f, target)).status_code  # noqa: E731
    assert get("charlie-old", old_file) == 200
    assert get("charlie-new", new_file) == 401  # not yet published: unknown kid, no fallback to old
    # overlap: both published (two public keys valid at a time, ADR-006 item 8)
    rewrite({"charlie-old": ("charlie", pub_old, caps), "charlie-new": ("charlie", pub_new, caps)})
    clock.now += 61
    assert get("charlie-old", old_file) == 200 and get("charlie-new", new_file) == 200
    # a token signed by the NEW key but labelled with the OLD kid is a bad signature, not a pass
    assert get("charlie-old", new_file) == 401
    # retire the old kid: it stops working once the callee re-reads (within 60 s)
    rewrite({"charlie-new": ("charlie", pub_new, caps)})
    clock.now += 61
    assert get("charlie-old", old_file) == 401
    assert get("charlie-new", new_file) == 200


def test_a_key_registered_for_one_subject_cannot_speak_for_another(tmp_path):
    clock = FakeTime()
    _, pub, seed = new_key()
    seed_file = tmp_path / "s.seed"
    seed_file.write_text(seed)
    client, _p, _r = _registry_app(tmp_path, clock, {"scout-k": ("scout", pub, (READ, WRITE))})
    target = agent_path("charlie")
    token = sa.mint_assertion(kid="scout-k", sub="charlie", audience=AUDIENCE, capability=READ,
                              engagement="unscoped", classification="unclassified", method="GET",
                              target=target, key_path=str(seed_file), now=clock.now)
    assert client.get(target, headers={sa.ASSERTION_HEADER: token}).status_code == 401
    # and scout's own identity is real but the policy gives it no access to charlie's scope
    own = sa.mint_assertion(kid="scout-k", sub="scout", audience=AUDIENCE, capability=READ,
                            engagement="unscoped", classification="unclassified", method="GET",
                            target=target, key_path=str(seed_file), now=clock.now)
    assert client.get(target, headers={sa.ASSERTION_HEADER: own}).status_code == 403


def test_the_registry_entry_can_narrow_a_subject_below_the_route_capability(tmp_path):
    clock = FakeTime()
    _, pub, seed = new_key()
    seed_file = tmp_path / "s.seed"
    seed_file.write_text(seed)
    client, _p, _r = _registry_app(tmp_path, clock, {"charlie-ro": ("charlie", pub, (READ,))})
    target = agent_path("charlie")
    read = _signed_get(tmp_path, clock, "charlie-ro", seed_file, target)
    assert client.get(target, headers=read).status_code == 200
    body = json.dumps(create_body()).encode()
    write = sa.mint_assertion(kid="charlie-ro", sub="charlie", audience=AUDIENCE, capability=WRITE,
                              engagement="unscoped", classification="unclassified", method="POST",
                              target=target, body=body, key_path=str(seed_file), now=clock.now)
    assert client.post(target, content=body, headers={sa.ASSERTION_HEADER: write}).status_code == 403


def test_a_registry_that_cannot_be_refreshed_keeps_last_known_for_15_minutes_then_fails_closed(tmp_path):
    clock = FakeTime()
    _, pub, seed = new_key()
    seed_file = tmp_path / "s.seed"
    seed_file.write_text(seed)
    client, path, _r = _registry_app(tmp_path, clock, {"charlie-k": ("charlie", pub, (READ, WRITE))})
    target = agent_path("charlie")
    get = lambda: client.get(target, headers=_signed_get(tmp_path, clock, "charlie-k", seed_file, target)).status_code  # noqa: E731
    assert get() == 200
    path.unlink()  # the delivered registry vanishes
    clock.now += 5 * 60
    assert get() == 200  # last-known registry, inside the documented bound
    clock.now += 11 * 60  # 16 minutes since the last good read
    assert get() == 503  # every request refused: authentication did not fail open
    path.write_text(f'[[keys]]\nkid="charlie-k"\nsub="charlie"\npublic_key="{pub}"\ncapabilities=["{READ}"]\n')
    clock.now += 61
    assert get() == 200  # recovers once a valid registry is back


def test_a_corrupt_registry_refresh_is_not_half_applied(tmp_path):
    clock = FakeTime()
    _, pub, seed = new_key()
    seed_file = tmp_path / "s.seed"
    seed_file.write_text(seed)
    client, path, _r = _registry_app(tmp_path, clock, {"charlie-k": ("charlie", pub, (READ,))})
    path.write_text("this is [not valid toml")
    clock.now += 61
    target = agent_path("charlie")
    ok = client.get(target, headers=_signed_get(tmp_path, clock, "charlie-k", seed_file, target))
    assert ok.status_code == 200  # last-known keys retained; garbage never replaces them
    path.write_text("")  # empty is also a failed refresh, never "no keys means everyone is allowed"
    clock.now += 61
    again = client.get(target, headers=_signed_get(tmp_path, clock, "charlie-k", seed_file, target))
    assert again.status_code == 200


# --- unauthorized principals ----------------------------------------------------------------------


@pytest.mark.parametrize("target", [
    agent_path("charlie"), agent_path("nova"), agent_path("mallory"), session_path("s1"),
    agent_path("charlie", MID),
])
def test_a_valid_key_that_the_policy_does_not_name_gets_one_generic_403(sealed, target):
    method = "GET"
    response = sealed.call("mallory", method, target)
    assert response.status_code == 403 and response.json() == GENERIC_403
    post = sealed.call("mallory", "POST", target if target.endswith("records") else target + "/retire",
                       create_body() if target.endswith("records") else {})
    assert post.status_code in (403, 404) and post.status_code != 200


def test_a_subject_that_differs_only_in_case_or_padding_is_not_a_policy_match(tmp_path):
    for index, sub in enumerate(("Charlie", "charlie ", "CHARLIE", "charlie\u200b")):
        directory = tmp_path / f"w{index}"
        directory.mkdir()
        world = build_world(directory, ExplodingRepository(), extra_subs=(sub,))
        assert world.call(sub, "GET", agent_path("charlie")).status_code == 403, repr(sub)


def test_health_is_the_only_open_route_and_reveals_nothing_sensitive(tmp_path):
    class Pingable(ExplodingRepository):
        def ping(self):
            return None

    sealed = build_world(tmp_path, Pingable())
    health = sealed.client.get("/health")
    assert health.status_code in (200, 503) and set(health.json()) == {"status", "agent"}
    for path in ("/docs", "/openapi.json", "/redoc", "/", "/memory", "/memory/", "/metrics", "/admin"):
        assert sealed.client.get(path).status_code in (401, 404), path


# --- namespace violations and cross-agent reads and writes -----------------------------------------


@pytest.mark.parametrize("caller, victim", [
    ("charlie", "nova"), ("charlie", "scout"), ("nova", "charlie"), ("nova", "scout"),
    ("scout", "nova"), ("cipher", "charlie"),
])
def test_no_agent_can_read_or_write_another_agents_scope(sealed, caller, victim):
    for method, target, body in (
        ("GET", agent_path(victim), None),
        ("GET", agent_path(victim, MID), None),
        ("POST", agent_path(victim), create_body()),
        ("POST", agent_path(victim, MID, "update"), {"idempotency_key": "k" * 12, "expected_rev": 1,
                                                    "content": "x", "provenance": {"kind": "agent_memory", "source_id": "s"}}),
        ("POST", agent_path(victim, MID, "classify"), {"idempotency_key": "k" * 12, "expected_rev": 1,
                                                      "classification": "internal"}),
        ("POST", agent_path(victim, MID, "retire"), {}),
        ("POST", agent_path(victim, MID, "erase"), {"reason": "other"}),
    ):
        response = sealed.call(caller, method, target, body)
        assert response.status_code == 403 and response.json() == GENERIC_403, (method, target)


def test_a_record_written_by_one_agent_is_invisible_to_another_through_every_door(world):
    sentinel = "ISOLATION-SENTINEL-4f1c"
    created = world.call("nova", "POST", agent_path("nova"), create_body(sentinel))
    assert created.status_code == 201
    memory_id = created.json()["memory_id"]
    # its owner reads it back
    assert sentinel in world.call("nova", "GET", agent_path("nova", memory_id)).text
    # charlie cannot get it by id from nova's path, nor list it from its own, nor smuggle the owner
    assert world.call("charlie", "GET", agent_path("nova", memory_id)).status_code == 403
    own = world.call("charlie", "GET", agent_path("charlie"))
    assert own.status_code == 200 and sentinel not in own.text and own.json()["count"] == 0
    assert world.call("charlie", "GET", agent_path("charlie", memory_id)).status_code == 404


def test_session_tier_is_not_reachable_by_charlie_and_reserved_tiers_are_never_served(sealed):
    # Charlie's policy has session = []; nova's has session operations on a session path.
    assert sealed.call("charlie", "GET", session_path("s1")).status_code == 403
    assert sealed.call("charlie", "POST", session_path("s1"), create_body()).status_code == 403
    for tier in ("eng", "inst"):
        authed = sealed.call("charlie", "GET", f"/memory/{tier}/x/records")
        assert authed.status_code == 501
        # without authentication a reserved path is indistinguishable from any other: 401
        assert sealed.client.get(f"/memory/{tier}/x/records").status_code == 401


@pytest.mark.parametrize("path", [
    "/memory/agent/charlie/../nova/records", "/memory/agent/charlie/%2e%2e/nova/records",
    "/memory/agent/nova%2Fcharlie/records", "/memory/agent//records", "/memory/agent/charlie/records/",
    "/memory/agent/charlie/records//", "/memory/agent/CHARLIE/records", "/memory/agent/charlie%00/records",
    "/memory/agent/charlie\\..\\nova/records", "/memory/agent/charlie/records;x=1",
    "/memory/agent/char%6cie/records",
])
def test_path_tricks_never_change_which_scope_is_acted_on(sealed, path):
    # signed verbatim, so the callee (not the issuer) is what refuses
    response = sealed.call(None, "GET", path, token=raw_token(sealed, "charlie", "GET", path))
    assert response.status_code in (400, 401, 403, 404), path


# --- privilege escalation via body, path, query and headers ---------------------------------------


@pytest.mark.parametrize("query", [
    "?agent=nova", "?agent_id=nova", "?scope=nova", "?scope_key=nova", "?owner=nova",
    "?owner_agent_id=nova", "?engagement=eng-1", "?tier=session", "?memory_type=session",
    "?classification=restricted", "?limit=5&limit=6", "?tag=a&tag=b", "?limit=0", "?limit=-1",
    "?limit=1e3", "?limit=%31", "?tag=UPPER", "?sub=nova", "?as=nova", "?admin=1",
])
def test_scope_or_identity_in_a_query_never_changes_the_outcome(sealed, query):
    target = agent_path("charlie") + query
    response = sealed.call(None, "GET", target, token=raw_token(sealed, "charlie", "GET", target))
    assert response.status_code in (400, 401), query


@pytest.mark.parametrize("field, value", [
    ("owner_agent_id", "nova"), ("agent_id", "nova"), ("scope_key", "nova"), ("memory_type", "session"),
    ("tier", "session"), ("engagement", "eng-1"), ("memory_id", MID), ("author", "nova"),
    ("lifecycle", "active"), ("version", 99), ("rev", 99), ("sub", "nova"), ("caller", "nova"),
    ("classification_ceiling", "restricted"), ("is_admin", True),
])
def test_identity_scope_and_lifecycle_fields_in_a_body_are_refused_whole(sealed, field, value):
    body = create_body()
    body[field] = value
    assert sealed.call("charlie", "POST", agent_path("charlie"), body).status_code == 422, field
    nested = create_body()
    nested["provenance"][field] = value
    assert sealed.call("charlie", "POST", agent_path("charlie"), nested).status_code == 422, field


def test_identity_headers_and_method_overrides_are_ignored(sealed):
    """Identity is only the verified assertion: no header substitutes for it or widens it."""
    headers = {"X-Agent": "nova", "X-Forwarded-User": "nova", "X-Viharbot-Agent": "nova",
               "X-Original-Path": agent_path("nova"), "X-Rewrite-URL": agent_path("nova"),
               "X-HTTP-Method-Override": "DELETE", "X-Method-Override": "PUT",
               "Authorization": "Bearer anything", "X-Real-IP": "127.0.0.1"}
    token = sealed.token("charlie", "GET", agent_path("nova"))
    # even with a perfectly valid token for charlie, a request for nova's scope is refused
    response = sealed.client.get(agent_path("nova"), headers={**headers, sa.ASSERTION_HEADER: token})
    assert response.status_code == 403
    # and the headers alone, with no assertion, buy nothing
    assert sealed.client.get(agent_path("charlie"), headers=headers).status_code == 401


def test_unsupported_methods_and_action_spoofing_are_refused_after_authentication(sealed):
    for method in ("PUT", "PATCH", "DELETE"):
        # authenticated with the write capability, still no route
        response = sealed.call("charlie", method, agent_path("charlie", MID), {"x": 1})
        assert response.status_code == 404, method
    # an action on the collection (no record id), or an unknown action, is no route either
    assert sealed.call("charlie", "POST", agent_path("charlie") + "/erase", {"reason": "other"}).status_code == 400
    assert sealed.call("charlie", "POST", agent_path("charlie", MID, "promote"), {}).status_code == 400
    assert sealed.call("charlie", "GET", agent_path("charlie", MID, "erase")).status_code == 404


def test_a_body_on_a_read_or_a_query_on_a_write_is_refused(sealed):
    response = sealed.call("charlie", "GET", agent_path("charlie", MID), raw_body=b'{"owner":"nova"}')
    assert response.status_code in (400, 404)
    response = sealed.call("charlie", "POST", agent_path("charlie") + "?agent=nova", create_body())
    assert response.status_code == 400


def test_policy_limits_cannot_be_exceeded_by_the_caller(world):
    """The ceiling, personal-data ban, size and retention limits come from policy, not the body."""
    path = agent_path("charlie")
    over = {
        "classification above the ceiling": create_body(classification="confidential"),
        "restricted": create_body(classification="restricted"),
        "personal data": create_body(personal_data=True),
        "oversized content": create_body("x" * 16385),
        "retention beyond the maximum": create_body(retention_until="2099-01-01T00:00:00+00:00"),
        "ttl on an agent record": create_body(ttl_seconds=60),
    }
    for label, body in over.items():
        response = world.call("charlie", "POST", path, body)
        assert response.status_code in (400, 403, 409, 422), label
    assert world.repo.records == []  # none of them was stored


def test_classification_cannot_be_lowered_or_raised_past_the_ceiling_through_classify(world):
    created = world.call("charlie", "POST", agent_path("charlie"), create_body())
    memory_id = created.json()["memory_id"]
    response = world.call("charlie", "POST", agent_path("charlie", memory_id, "classify"),
                          {"idempotency_key": "classify-key-1", "expected_rev": 1, "classification": "restricted"})
    assert response.status_code in (400, 403, 404, 409, 422)  # above charlie's ceiling: never 200


def test_every_refusal_has_a_short_fixed_body_that_never_echoes_input(sealed):
    probe = "ECHO-PROBE-9e2b"
    responses = [
        sealed.call("charlie", "GET", agent_path("charlie") + f"?agent={probe}"),
        sealed.call("charlie", "POST", agent_path("charlie"), {"content": probe, "owner": probe}),
        sealed.call("charlie", "GET", f"/memory/agent/{probe.lower()}/records"),
        sealed.call("charlie", "GET", f"/memory/agent/charlie/records/{probe}"),
        sealed.call(None, "GET", agent_path("charlie"), token=probe),
        sealed.call("mallory", "GET", agent_path("charlie") + f"?tag={probe}"),
    ]
    for response in responses:
        assert response.status_code >= 400
        assert probe not in response.text and probe.lower() not in response.text
        assert len(response.text) < 200
