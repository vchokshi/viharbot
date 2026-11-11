"""Service authentication for Research, Security, Sage and Pixel (ADR-006 A2, issue #235).

Offline and deterministic: throwaway keys, an injected clock, the model call stubbed out, no network.
These tests prove authentication only (who signed, that the request was bound and not replayed).
They do not imply delegation-grant authorization, which is A3 (issue #95).
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from test_service_auth import Clock, raw_seed, registry_text
from viharbot import demo
from viharbot import outbound_auth as oa
from viharbot import pixel_execution, security_client
from viharbot import service_auth as sa

ROOT = Path(__file__).resolve().parents[1]
REAL_TIME = time.time

FINDINGS = {"summary": "s", "findings": [], "sources": [], "uncertainties": []}
DECK_A, DECK_B = "deck-aaaaaaaaaaaa", "deck-bbbbbbbbbbbb"
NEW_CAPABILITIES = (
    sa.RESEARCH_CAPABILITY,
    sa.SECURITY_CAPABILITY,
    sa.SAGE_CAPABILITY,
    sa.PIXEL_PROCESS_CAPABILITY,
    sa.PIXEL_READ_DECK_CAPABILITY,
)
MODULES = {
    "research": "research_agent",
    "security": "security_agent",
    "sage": "sage_agent",
    "pixel": "pixel_agent",
}
AUTH_ENV = (sa.ENABLE_ENV, sa.REGISTRY_ENV, "VIHARBOT_MAX_BODY_BYTES", "VIHARBOT_PPT_GENERATION_ENABLED")


def body(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


class Route(NamedTuple):
    service: str  # also the audience
    method: str
    path: str
    payload: bytes
    capability: str
    reaches_handler: int  # the status once authenticated (the model call is stubbed to fail)

    @property
    def id(self) -> str:
        return f"{self.service}:{self.method}:{self.path}"


QUESTION = body({"question": "q"})
ROUTES = [
    Route("research", "POST", "/research", QUESTION, sa.RESEARCH_CAPABILITY, 502),
    Route("research", "POST", "/research-and-assess", QUESTION, sa.RESEARCH_CAPABILITY, 502),
    Route("research", "POST", "/invocations", body({"input": {"question": "q"}}),
          sa.RESEARCH_CAPABILITY, 502),
    Route("security", "POST", "/assess", body({"findings": FINDINGS}), sa.SECURITY_CAPABILITY, 502),
    Route("security", "POST", "/invocations", body({"input": {"findings": FINDINGS}}),
          sa.SECURITY_CAPABILITY, 502),
    Route("sage", "POST", "/process", QUESTION, sa.SAGE_CAPABILITY, 502),
    # Pixel answers 503 once authenticated because the PPT flag is off, before any model call.
    Route("pixel", "POST", "/process", QUESTION, sa.PIXEL_PROCESS_CAPABILITY, 503),
    Route("pixel", "GET", f"/decks/{DECK_A}.pptx", b"", sa.PIXEL_READ_DECK_CAPABILITY, 200),
    Route("pixel", "GET", f"/decks/{DECK_A}.html", b"", sa.PIXEL_READ_DECK_CAPABILITY, 200),
]
BY_ID = {route.id: route for route in ROUTES}
parametrize_routes = pytest.mark.parametrize("route", ROUTES, ids=[r.id for r in ROUTES])


class Fleet:
    """Throwaway keys, one registry, one injected clock, and loaders for the four services."""

    def __init__(self, monkeypatch, tmp_path: Path) -> None:
        self.monkeypatch, self.tmp = monkeypatch, tmp_path
        self.clock = Clock()
        self.web, self.research = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        self.web_key = tmp_path / "web.key"
        self.web_key.write_text(raw_seed(self.web))
        self.research_key = tmp_path / "research.key"
        self.research_key.write_text(raw_seed(self.research))
        self.registry_path = tmp_path / "registry.toml"
        self.write_registry()
        self.registry = sa.KeyRegistry(self.registry_path, self.clock)
        self.registry.load_initial()
        self.loaded: list[str] = []
        real = sa.authenticator_from_environment
        clock = self.clock
        monkeypatch.setattr(
            sa, "authenticator_from_environment",
            lambda audience, environ=None, _clock=None: real(audience, environ, clock=clock),
        )

    def write_registry(self, *, web: bool = True) -> None:
        entries = []
        if web:
            entries.append(("web-1", "web", self.web, [*NEW_CAPABILITIES, sa.SPECIALIST_CAPABILITY]))
        entries.append(("research-1", "research", self.research, [sa.SECURITY_CAPABILITY]))
        self.registry_path.write_text(registry_text(*entries))

    def load(self, service: str, *, auth: bool = True, **env: str):
        """(Re)import a service module with the given environment and the model call stubbed."""
        mp = self.monkeypatch
        mp.setenv("MEMORY_DIR", str(self.tmp))
        for name in AUTH_ENV:
            mp.delenv(name, raising=False)
        if auth:
            mp.setenv(sa.ENABLE_ENV, "on")
            mp.setenv(sa.REGISTRY_ENV, str(self.registry_path))
        for name, value in env.items():
            mp.setenv(name, value)
        name = f"viharbot.{MODULES[service]}"
        self.loaded.append(name)
        module = importlib.reload(importlib.import_module(name))

        def no_inference(**_kwargs):
            raise RuntimeError("inference is disabled in these tests")

        mp.setattr(module, "build_agent", no_inference)  # never reach a real model
        return module

    def client(self, service: str, **kwargs) -> TestClient:
        return TestClient(self.load(service, **kwargs).app)

    def decks(self) -> None:
        folder = self.tmp / "pixel" / "decks"
        folder.mkdir(parents=True, exist_ok=True)
        for deck in (DECK_A, DECK_B):
            (folder / f"{deck}.pptx").write_bytes(b"PK-pptx-" + deck.encode())
            (folder / f"{deck}.html").write_text(f"<p>{deck}</p>", encoding="utf-8")

    def token(self, route: Route, *, key=None, **overrides) -> str:
        args = dict(
            kid="web-1", sub="web", audience=route.service, capability=route.capability,
            engagement="e", classification="c", method=route.method, target=route.path,
            body=route.payload, key_path=str(key or self.web_key), now=self.clock.now,
        )
        args.update(overrides)
        return sa.mint_assertion(**args)

    def send(self, client, route: Route, token=None, *, payload=None, target=None, method=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers[sa.ASSERTION_HEADER] = token
        content = route.payload if payload is None else payload
        return client.request(
            method or route.method, target or route.path, content=content or None, headers=headers
        )


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    fleet = Fleet(monkeypatch, tmp_path)
    fleet.decks()
    yield fleet
    for name in AUTH_ENV:  # leave every module as the rest of the suite expects it: auth off
        monkeypatch.delenv(name, raising=False)
    for name in dict.fromkeys(fleet.loaded):
        importlib.reload(importlib.import_module(name))


@pytest.fixture
def clients(fleet):
    """One client per service, loaded once with authentication on."""
    return {service: fleet.client(service) for service in MODULES}


# --- every protected route -------------------------------------------------------------------


@parametrize_routes
def test_valid_assertion_reaches_the_handler(fleet, clients, route):
    response = fleet.send(clients[route.service], route, fleet.token(route))
    assert response.status_code == route.reaches_handler


@parametrize_routes
def test_unsigned_request_is_rejected(fleet, clients, route):
    assert fleet.send(clients[route.service], route).status_code == 401


@parametrize_routes
def test_malformed_tokens_are_rejected(fleet, clients, route):
    for bad in ("abc", "a.b.c", "!!!.###.$$$", "x" * (sa.MAX_TOKEN_BYTES + 1)):
        assert fleet.send(clients[route.service], route, bad).status_code == 401


@parametrize_routes
def test_expired_and_not_yet_valid_tokens_are_rejected(fleet, clients, route):
    token = fleet.token(route, lifetime=30)
    fleet.clock.now += 30 + sa.CLOCK_SKEW_SECONDS + 1  # past exp plus skew; under the 60 s refresh
    assert fleet.send(clients[route.service], route, token).status_code == 401
    early = fleet.token(route, now=fleet.clock.now + 100)
    assert fleet.send(clients[route.service], route, early).status_code == 401


@parametrize_routes
def test_wrong_audience_is_rejected(fleet, clients, route):
    token = fleet.token(route, audience="synthesis")
    assert fleet.send(clients[route.service], route, token).status_code == 401
    other = next(r for r in ROUTES if r.service != route.service)
    assert fleet.send(clients[route.service], route, fleet.token(route, audience=other.service)
                      ).status_code == 401


@parametrize_routes
def test_wrong_capability_is_forbidden(fleet, clients, route):
    other = next(r for r in ROUTES if r.capability != route.capability)
    for capability in (other.capability, sa.SPECIALIST_CAPABILITY, "not.a.capability"):
        token = fleet.token(route, capability=capability)
        assert fleet.send(clients[route.service], route, token).status_code == 403


@parametrize_routes
def test_unknown_and_retired_kid_are_rejected(fleet, clients, route):
    assert fleet.send(clients[route.service], route, fleet.token(route, kid="ghost-1")
                      ).status_code == 401
    # Retire web-1: once the registry refreshes, its still-unexpired tokens stop working.
    fleet.write_registry(web=False)
    fleet.clock.now += sa.REGISTRY_REFRESH_SECONDS + 1
    assert fleet.send(clients[route.service], route, fleet.token(route)).status_code == 401


@parametrize_routes
def test_replayed_jti_is_rejected(fleet, clients, route):
    token = fleet.token(route)
    assert fleet.send(clients[route.service], route, token).status_code == route.reaches_handler
    assert fleet.send(clients[route.service], route, token).status_code == 401


@parametrize_routes
def test_tampered_body_is_rejected(fleet, clients, route):
    token = fleet.token(route)
    # POST: one byte appended. GET: a body where the signed body was empty.
    assert fleet.send(clients[route.service], route, token, payload=route.payload + b" "
                      ).status_code == 401


@parametrize_routes
def test_token_is_bound_to_query_and_method(fleet, clients, route):
    client = clients[route.service]
    assert fleet.send(client, route, fleet.token(route), target=route.path + "?x=1"
                      ).status_code == 401
    swapped = "GET" if route.method == "POST" else "POST"
    token = fleet.token(route, method=swapped)
    assert fleet.send(client, route, token).status_code == 401


@parametrize_routes
def test_token_is_bound_to_its_route(fleet, clients, route):
    for other in (r for r in ROUTES if r.service == route.service and r.path != route.path):
        token = fleet.token(route)
        status = fleet.send(clients[route.service], other, token, payload=route.payload
                            ).status_code
        # Same capability: the binding refuses it (401). Another capability: refused earlier (403).
        assert status == (401 if other.capability == route.capability else 403), other.id


@parametrize_routes
def test_a_service_key_cannot_claim_another_subject(fleet, clients, route):
    client = clients[route.service]
    # web-1 is bound to "web"; asserting any other sub or iss is refused.
    assert fleet.send(client, route, fleet.token(route, sub="research")).status_code == 401
    # research-1 is bound to "research"; claiming "web" is refused too.
    claiming_web = fleet.token(route, kid="research-1", sub="web", key=fleet.research_key)
    assert fleet.send(client, route, claiming_web).status_code == 401


@parametrize_routes
def test_research_key_may_only_assert_the_security_capability(fleet, clients, route):
    token = fleet.token(
        route, kid="research-1", sub="research", key=fleet.research_key,
        capability=sa.SECURITY_CAPABILITY,
    )
    status = fleet.send(clients[route.service], route, token).status_code
    if route.capability == sa.SECURITY_CAPABILITY:
        assert status == route.reaches_handler  # Research may call Security's routes
    else:
        assert status == 403
    own = fleet.token(route, kid="research-1", sub="research", key=fleet.research_key)
    expected = route.reaches_handler if route.capability == sa.SECURITY_CAPABILITY else 403
    assert fleet.send(clients[route.service], route, own).status_code == expected


def test_a_failed_check_does_not_consume_the_token(fleet, clients):
    route = BY_ID["sage:POST:/process"]
    token = fleet.token(route)
    assert fleet.send(clients["sage"], route, token, payload=b'{"question":"x"}').status_code == 401
    assert fleet.send(clients["sage"], route, token).status_code == 502


# --- the deck routes (GET, empty body) ---------------------------------------------------------


def test_deck_get_binds_method_target_and_the_empty_body(fleet, clients):
    pptx = BY_ID[f"pixel:GET:/decks/{DECK_A}.pptx"]
    client = clients["pixel"]
    claims = sa._json_object(fleet.token(pptx).split(".")[1])
    assert claims["mth"] == "GET" and claims["tgt"] == pptx.path
    assert claims["req_sha256"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    response = fleet.send(client, pptx, fleet.token(pptx))
    assert (response.status_code, response.content) == (200, b"PK-pptx-" + DECK_A.encode())
    # A token for the empty body does not authorize a request that carries a body.
    assert fleet.send(client, pptx, fleet.token(pptx), payload=b"{}").status_code == 401


def test_a_token_for_one_deck_is_unusable_for_another(fleet, clients):
    pptx_a = BY_ID[f"pixel:GET:/decks/{DECK_A}.pptx"]
    html_a = BY_ID[f"pixel:GET:/decks/{DECK_A}.html"]
    client = clients["pixel"]
    token_a = fleet.token(pptx_a)
    other_deck = f"/decks/{DECK_B}.pptx"
    assert fleet.send(client, pptx_a, token_a, target=other_deck).status_code == 401
    assert fleet.send(client, pptx_a, fleet.token(pptx_a), target=html_a.path).status_code == 401
    assert fleet.send(client, html_a, fleet.token(html_a), target=other_deck).status_code == 401
    # Each deck still works with a token minted for it.
    assert fleet.send(client, pptx_a, fleet.token(pptx_a, target=other_deck),
                      target=other_deck).status_code == 200


def test_pixel_process_and_deck_capabilities_are_not_interchangeable(fleet, clients):
    process = BY_ID["pixel:POST:/process"]
    deck = BY_ID[f"pixel:GET:/decks/{DECK_A}.pptx"]
    client = clients["pixel"]
    assert fleet.send(client, deck, fleet.token(deck, capability=sa.PIXEL_PROCESS_CAPABILITY)
                      ).status_code == 403
    assert fleet.send(client, process, fleet.token(process, capability=sa.PIXEL_READ_DECK_CAPABILITY)
                      ).status_code == 403


# --- routes that stay open, startup, auth off, body cap ---------------------------------------


def test_health_ping_and_model_stay_open(fleet, clients):
    for service, client in clients.items():
        assert client.get("/health").status_code == 200, service
        assert client.get("/model").status_code == 200, service  # follow-up: protect /model
    for service in ("research", "security"):
        assert clients[service].get("/ping").status_code == 200


@pytest.mark.parametrize("service", MODULES)
def test_authentication_on_without_a_usable_registry_refuses_to_start(fleet, service, tmp_path):
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    broken = tmp_path / "broken.toml"
    broken.write_text("[[keys]]\nkid = ")
    bad = tmp_path / "bad-key.toml"
    bad.write_text('[[keys]]\nkid = "a"\nsub = "b"\npublic_key = "AAAA"\ncapabilities = []\n')
    for registry in (None, "", str(tmp_path / "missing.toml"), str(empty), str(broken), str(bad),
                     str(tmp_path)):  # a directory, as Docker creates for a missing bind mount
        env = {sa.REGISTRY_ENV: registry} if registry is not None else {}
        with pytest.raises(sa.ServiceAuthConfigError):
            if registry is None:
                fleet.monkeypatch.delenv(sa.REGISTRY_ENV, raising=False)
                fleet.monkeypatch.setenv(sa.ENABLE_ENV, "on")
                name = f"viharbot.{MODULES[service]}"
                fleet.loaded.append(name)
                importlib.reload(importlib.import_module(name))
            else:
                fleet.load(service, **env)


@pytest.mark.parametrize("service", MODULES)
def test_a_malformed_enable_flag_refuses_to_start(fleet, service):
    with pytest.raises(sa.ServiceAuthConfigError):
        fleet.load(service, **{sa.ENABLE_ENV: "maybe"})


@parametrize_routes
def test_behavior_is_unchanged_when_authentication_is_off(fleet, route):
    module = fleet.load(route.service, auth=False)
    assert module._authenticator is None
    client = TestClient(module.app)
    response = fleet.send(client, route)  # no token at all
    assert response.status_code not in (400, 401, 403, 413)
    big = fleet.send(client, route, payload=b" " * 2_000_000) if route.method == "POST" else None
    assert big is None or big.status_code != 413  # the size cap is installed only with auth on


@pytest.mark.parametrize("route", [r for r in ROUTES if r.method == "POST"],
                         ids=[r.id for r in ROUTES if r.method == "POST"])
def test_oversized_bodies_are_refused_before_authentication(fleet, route):
    client = fleet.client(route.service, VIHARBOT_MAX_BODY_BYTES="1024")
    assert fleet.send(client, route, payload=b" " * 2000).status_code == 413  # unsigned
    assert fleet.send(client, route, fleet.token(route)).status_code == route.reaches_handler


def test_denials_are_logged_without_tokens_or_bodies(fleet, clients, caplog):
    route = BY_ID["pixel:POST:/process"]
    token = fleet.token(route)
    with caplog.at_level(logging.INFO):
        fleet.send(clients["pixel"], route, token, payload=b'{"question":"secret-question"}')
        fleet.send(clients["pixel"], route, fleet.token(route))
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "deny" in text and "binding_mismatch" in text and "allow" in text
    for secret in (token, token.split(".")[2], "secret-question", str(fleet.web_key)):
        assert secret not in text


# --- outbound signing ---------------------------------------------------------------------------


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Wire:
    """Stands in for a urlopen: records requests, then answers from a canned body or a TestClient."""

    def __init__(self, client: TestClient | None = None, body: bytes = b"{}") -> None:
        self.client, self.body, self.requests = client, body, []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.client is None:
            return Reply(self.body)
        response = self.client.request(
            request.get_method(), request.selector, content=request.data,
            headers=dict(request.header_items()),
        )
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, "err", {}, io.BytesIO(b""))
        return Reply(response.content)


def outbound_on(fleet, *, kid="web-1", key=None, subject=None):
    mp = fleet.monkeypatch
    mp.setenv(oa.ENABLE_ENV, "on")
    mp.setenv(oa.KID_ENV, kid)
    mp.setenv(oa.KEY_FILE_ENV, str(key or fleet.web_key))
    if subject:
        mp.setenv(oa.SUBJECT_ENV, subject)
    else:
        mp.delenv(oa.SUBJECT_ENV, raising=False)
    mp.setattr(sa.time, "time", fleet.clock)  # tokens are issued at the injected clock


def verifier(fleet, audience):
    return sa.ServiceAuthenticator(audience=audience, registry=fleet.registry, clock=fleet.clock)


def verify_request(fleet, request, audience, capability):
    split = sa.split_target(request.selector)
    return verifier(fleet, audience).verify(
        request.get_header("X-viharbot-assertion"), method=request.get_method(),
        raw_path=split[0], query=split[1], body=request.data or b"", required_capability=capability,
    )


@pytest.mark.parametrize("service,capability", [
    ("research", sa.RESEARCH_CAPABILITY), ("security", sa.SECURITY_CAPABILITY),
    ("sage", sa.SAGE_CAPABILITY), ("pixel", sa.PIXEL_PROCESS_CAPABILITY),
])
def test_post_json_signs_calls_to_each_new_destination(fleet, service, capability):
    outbound_on(fleet)
    wire = Wire(body=b'{"ok":true}')
    fleet.monkeypatch.setattr(demo, "no_redirect_urlopen", wire)
    fleet.monkeypatch.setattr(demo, "urlopen", Wire())  # the unsigned opener must not be used
    demo.post_json(f"http://{service}:8000/process", {"question": "q"}, "rid", service)
    (request,) = wire.requests
    caller = verify_request(fleet, request, service, capability)
    assert (caller.sub, caller.kid, caller.capability) == ("web", "web-1", capability)


def test_destination_capabilities_cover_all_eight_services_and_the_memory_service():
    assert set(oa.DESTINATION_CAPABILITIES) == {
        "research", "security", "sage", "pixel", "fact-check", "source-quality",
        "privacy-review", "synthesis", "memory",
    }
    assert oa.DESTINATION_CAPABILITIES["memory"] == sa.MEMORY_WRITE_CAPABILITY
    assert oa.GET_CAPABILITIES == {
        "pixel": sa.PIXEL_READ_DECK_CAPABILITY, "memory": sa.MEMORY_READ_CAPABILITY,
    }


def _security_reply(request_id: str) -> bytes:
    assessment = {"overall_risk": "low", "implications": [], "recommendations": [], "uncertainties": []}
    return body({"request_id": request_id, "assessment": assessment})


def _findings():
    from viharbot.schemas import ResearchFindings

    return ResearchFindings.model_validate(FINDINGS)


def test_research_calls_security_with_its_own_key_and_subject(fleet):
    outbound_on(fleet, kid="research-1", key=fleet.research_key)  # no VIHARBOT_SERVICE_SUBJECT set
    wire = Wire(body=_security_reply("rid"))
    fleet.monkeypatch.setattr(security_client, "no_redirect_urlopen", wire)
    fleet.monkeypatch.setattr(security_client, "urlopen", Wire())
    fleet.monkeypatch.setenv("SECURITY_AGENT_URL", "http://security:8000/assess")
    result = security_client.send_findings_to_security(findings=_findings(), request_id="rid")
    assert result.assessment.overall_risk == "low"
    (request,) = wire.requests
    caller = verify_request(fleet, request, "security", sa.SECURITY_CAPABILITY)
    assert (caller.sub, caller.kid) == ("research", "research-1")  # not the web key, not "web"
    claims = sa._json_object(request.get_header("X-viharbot-assertion").split(".")[1])
    assert claims["aud"] == "security" and claims["tgt"] == "/assess"


def test_research_signing_with_the_web_key_is_refused_by_security(fleet):
    outbound_on(fleet)  # the web kid and key, but Research signs as "research"
    wire = Wire(body=_security_reply("rid"))
    fleet.monkeypatch.setattr(security_client, "no_redirect_urlopen", wire)
    security_client.send_findings_to_security(findings=_findings(), request_id="rid")
    (request,) = wire.requests
    with pytest.raises(sa.AuthError) as raised:
        verify_request(fleet, request, "security", sa.SECURITY_CAPABILITY)
    assert raised.value.reason == "subject_mismatch"


def test_research_to_security_end_to_end_against_the_verifying_security_service(fleet):
    security = fleet.client("security")  # the callee verifies on the injected clock
    fleet.monkeypatch.setattr(security_client, "no_redirect_urlopen", Wire(security))
    outbound_on(fleet, kid="research-1", key=fleet.research_key)
    # Authenticated: the request reaches the handler, which fails at the stubbed model (502).
    with pytest.raises(security_client.SecurityClientError) as raised:
        security_client.send_findings_to_security(findings=_findings(), request_id="rid")
    assert raised.value.error_type == "HTTP_502"
    outbound_on(fleet)  # the web key under Research's subject: refused
    with pytest.raises(security_client.SecurityClientError) as raised:
        security_client.send_findings_to_security(findings=_findings(), request_id="rid")
    assert raised.value.error_type == "HTTP_401"
    fleet.monkeypatch.setenv(oa.ENABLE_ENV, "off")  # unsigned: refused by the same callee
    fleet.monkeypatch.setattr(security_client, "urlopen", Wire(security))
    with pytest.raises(security_client.SecurityClientError) as raised:
        security_client.send_findings_to_security(findings=_findings(), request_id="rid")
    assert raised.value.error_type == "HTTP_401"


@pytest.mark.parametrize("case", ["missing_key", "no_kid", "bad_flag"])
def test_research_signing_failure_sends_nothing(fleet, case):
    outbound_on(fleet, kid="research-1", key=fleet.research_key)
    mp = fleet.monkeypatch
    if case == "missing_key":
        mp.setenv(oa.KEY_FILE_ENV, str(fleet.tmp / "missing.key"))
    elif case == "no_kid":
        mp.delenv(oa.KID_ENV)
    else:
        mp.setenv(oa.ENABLE_ENV, "maybe")
    signed, plain = Wire(body=_security_reply("rid")), Wire(body=_security_reply("rid"))
    mp.setattr(security_client, "no_redirect_urlopen", signed)
    mp.setattr(security_client, "urlopen", plain)
    with pytest.raises(security_client.SecurityClientError) as raised:
        security_client.send_findings_to_security(findings=_findings(), request_id="rid")
    assert raised.value.error_type == "OutboundAuthError"
    assert signed.requests == [] and plain.requests == []  # no unauthenticated fallback


def test_research_to_security_is_unchanged_when_outbound_is_off(fleet):
    for name in (oa.ENABLE_ENV, oa.KID_ENV, oa.KEY_FILE_ENV):
        fleet.monkeypatch.delenv(name, raising=False)
    plain, strict = Wire(body=_security_reply("rid")), Wire()
    fleet.monkeypatch.setattr(security_client, "urlopen", plain)
    fleet.monkeypatch.setattr(security_client, "no_redirect_urlopen", strict)
    security_client.send_findings_to_security(findings=_findings(), request_id="rid")
    (request,) = plain.requests
    assert strict.requests == []
    assert request.get_header("X-viharbot-assertion") is None
    assert dict(request.header_items()) == {"Content-type": "application/json", "X-request-id": "rid"}


def test_fetch_deck_signs_a_get_with_an_empty_body_for_the_pixel_service(fleet):
    pixel = fleet.client("pixel")
    outbound_on(fleet)
    fleet.monkeypatch.setenv("PIXEL_URL", "http://pixel:8000/process")
    wire = Wire(pixel)
    fleet.monkeypatch.setattr(pixel_execution, "no_redirect_urlopen", wire)
    assert pixel_execution.fetch_deck(DECK_A) == b"PK-pptx-" + DECK_A.encode()
    assert pixel_execution.fetch_deck(DECK_A, "html") == f"<p>{DECK_A}</p>".encode()
    first, second = wire.requests
    assert (first.get_method(), first.data) == ("GET", None)
    claims = sa._json_object(first.get_header("X-viharbot-assertion").split(".")[1])
    assert (claims["mth"], claims["tgt"], claims["aud"], claims["cap"]) == (
        "GET", f"/decks/{DECK_A}.pptx", "pixel", sa.PIXEL_READ_DECK_CAPABILITY)
    # Replaying deck A's token against deck B, or the same request again, is refused.
    replay = pixel.get(f"/decks/{DECK_B}.pptx",
                       headers={sa.ASSERTION_HEADER: first.get_header("X-viharbot-assertion")})
    assert replay.status_code == 401
    again = pixel.get(first.selector,
                      headers={sa.ASSERTION_HEADER: first.get_header("X-viharbot-assertion")})
    assert again.status_code == 401  # already used by the fetch above
    # Without signing the same callee refuses the download, and fetch_deck reports it as missing.
    fleet.monkeypatch.setenv(oa.ENABLE_ENV, "off")
    assert pixel_execution.fetch_deck(DECK_A) is None


def test_fetch_deck_signing_failure_sends_nothing(fleet):
    outbound_on(fleet)
    fleet.monkeypatch.setenv(oa.KEY_FILE_ENV, str(fleet.tmp / "missing.key"))
    wire = Wire(body=b"deck-bytes")
    fleet.monkeypatch.setattr(pixel_execution, "no_redirect_urlopen", wire)
    assert pixel_execution.fetch_deck(DECK_A) is None
    assert wire.requests == []


def test_fetch_deck_is_unchanged_when_outbound_is_off(fleet):
    for name in (oa.ENABLE_ENV, oa.KID_ENV, oa.KEY_FILE_ENV):
        fleet.monkeypatch.delenv(name, raising=False)
    wire = Wire(body=b"deck-bytes")
    fleet.monkeypatch.setattr(pixel_execution, "no_redirect_urlopen", wire)
    assert pixel_execution.fetch_deck(DECK_A) == b"deck-bytes"
    (request,) = wire.requests
    assert request.get_header("X-viharbot-assertion") is None and request.header_items() == []


def test_post_json_to_the_pixel_and_sage_services_reach_the_verifying_apps(fleet):
    sage, pixel = fleet.client("sage"), fleet.client("pixel")
    outbound_on(fleet)
    for service, client, expected in (("sage", sage, 502), ("pixel", pixel, 503)):
        wire = Wire(client)
        fleet.monkeypatch.setattr(demo, "no_redirect_urlopen", wire)
        fleet.monkeypatch.setattr(demo, "urlopen", Wire(client))
        with pytest.raises(HTTPError) as raised:  # authenticated, then the stubbed handler fails
            demo.post_json("http://testserver/process", {"question": "q"}, "rid", service)
        assert raised.value.code == expected, service
        fleet.monkeypatch.setenv(oa.ENABLE_ENV, "off")
        with pytest.raises(HTTPError) as raised:
            demo.post_json("http://testserver/process", {"question": "q"}, "rid", service)
        assert raised.value.code == 401, service
        outbound_on(fleet)


# --- documentation and shipped configuration ---------------------------------------------------


def test_new_capabilities_are_documented_and_distinct():
    docs = (ROOT / "docs" / "service-authentication.md").read_text()
    assert len(set(NEW_CAPABILITIES)) == len(NEW_CAPABILITIES)
    for capability in (*NEW_CAPABILITIES, sa.SPECIALIST_CAPABILITY):
        assert f"`{capability}`" in docs


def test_the_stage_spec_does_not_enable_service_authentication():
    spec = (ROOT / "deploy" / "digitalocean" / "app.yaml").read_text()
    for name in (sa.ENABLE_ENV, oa.ENABLE_ENV, sa.REGISTRY_ENV, oa.KID_ENV):
        assert name not in spec  # Stage needs the promote-to-Stage confirmation (AGENTS.md)


def test_compose_turns_authentication_on_and_carries_no_key_material():
    text = (ROOT / "compose.yaml").read_text()
    assert "${VIHARBOT_SERVICE_AUTH:-on}" in text and "${VIHARBOT_OUTBOUND_AUTH:-on}" in text
    assert not re.search(re.escape(sa.SIGNING_KEY_ENV) + r"\s*:", text)  # keys come from files
    blocks = {
        name: re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", text, re.S | re.M).group(1)
        for name in ("web", "research", "security", "fact-check", "source-quality",
                     "privacy-review", "synthesis", "pixel", "sage", "orchestrator")
    }
    for name in ("research", "security", "fact-check", "source-quality", "privacy-review",
                 "synthesis", "pixel", "sage"):
        assert "*auth-verify" in blocks[name] and "registry.toml:/run/viharbot-auth/registry.toml:ro" in blocks[name], name
    for name in ("web", "research", "orchestrator"):  # the services that call others sign
        assert "VIHARBOT_OUTBOUND_AUTH" in blocks[name] and "signing.key:ro" in blocks[name], name
    for name in ("security", "fact-check", "source-quality", "privacy-review", "pixel", "sage"):
        assert "VIHARBOT_SIGNING_KID" not in blocks[name], name  # callees hold no signing key
    # Synthesis (Charlie) is the one specialist that calls another service: the memory service, with
    # its own key and subject. It never mounts the web key. Memory itself is off by default.
    synthesis = blocks["synthesis"]
    assert "VIHARBOT_SIGNING_KID: charlie-dev-1" in synthesis and "VIHARBOT_SERVICE_SUBJECT: charlie" in synthesis
    assert "keys/web.key" not in synthesis and "VIHARBOT_MEMORY_ENABLED: ${VIHARBOT_MEMORY_ENABLED:-off}" in synthesis
    assert "keys/web.key" in blocks["web"] and "keys/web.key" not in blocks["research"]
    assert "data/dev-auth/keys" not in text.replace("./data/dev-auth/keys/", "")  # no directory mount


def test_dev_key_script_writes_a_registry_the_verifier_accepts(tmp_path):
    spec = importlib.util.spec_from_file_location("dev_service_auth", ROOT / "scripts" / "dev_service_auth.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    out = tmp_path / "dev-auth"
    script.generate(out)
    entries = sa.parse_registry((out / "registry.toml").read_text())
    assert {e.sub for e in entries.values()} == {"web", "research", "orchestrator"}
    assert entries["research-dev-1"].capabilities == frozenset({sa.SECURITY_CAPABILITY})
    assert set(NEW_CAPABILITIES) <= entries["web-dev-1"].capabilities
    # Each private seed signs an assertion its own registry entry verifies, and only that entry.
    clock = Clock()
    registry = tmp_path / "check-registry.toml"
    registry.write_text((out / "registry.toml").read_text())
    reg = sa.KeyRegistry(registry, clock)
    reg.load_initial()
    auth = sa.ServiceAuthenticator(audience="security", registry=reg, clock=clock)
    token = sa.mint_assertion(
        kid="research-dev-1", sub="research", audience="security", capability=sa.SECURITY_CAPABILITY,
        engagement="e", classification="c", method="POST", target="/assess", body=b"{}",
        key_path=str(out / "keys" / "research.key"), now=clock.now,
    )
    assert auth.verify(token, method="POST", raw_path=b"/assess", query=None, body=b"{}",
                       required_capability=sa.SECURITY_CAPABILITY).sub == "research"
    with pytest.raises(FileExistsError):  # a running stack's keys are never replaced silently
        script.generate(out)
    script.generate(out, force=True)
