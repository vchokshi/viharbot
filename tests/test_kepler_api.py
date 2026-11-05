"""Signed local HTTP assignment path and the GitHub REST client (issue #128). Offline: throwaway
Ed25519 keys through the real service_auth dependency, fake GitHub, local bare repo."""

from __future__ import annotations

import io
import json
import shutil
import urllib.error
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from kepler_helpers import ROOT, TOKEN, make_world

from viharbot import service_auth as sa
from viharbot.kepler.api import ASSIGN_CAPABILITY, AUDIENCE, READ_CAPABILITY, create_app
from viharbot.kepler.github import GitHubError, HttpGitHubClient
from viharbot.kepler.policy import KeplerPolicy

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    pytest.mark.skipif(not (ROOT / "docs" / "governance").exists(), reason="docs/ is not here"),
]
NOW = 1_800_000_000.0
IDENTITIES = {  # kid -> (registry subject, capabilities)
    "founder-1": ("founder", [ASSIGN_CAPABILITY, READ_CAPABILITY]),
    "reader-only": ("founder", [READ_CAPABILITY]),  # a founder key without the assign capability
    "stranger-1": ("stranger", [ASSIGN_CAPABILITY, READ_CAPABILITY]),  # authenticates, not an assigner
}


def b64(key, private):
    fmt = serialization.PrivateFormat.Raw if private else serialization.PublicFormat.Raw
    enc = serialization.Encoding.Raw
    if private:
        return sa.b64url_encode(key.private_bytes(enc, fmt, serialization.NoEncryption()))
    return sa.b64url_encode(key.public_key().public_bytes(enc, fmt))


@pytest.fixture
def api(tmp_path):
    world = make_world(tmp_path)
    keys, blocks = {}, []
    for kid, (sub, caps) in IDENTITIES.items():
        key = Ed25519PrivateKey.generate()
        seed = tmp_path / f"{kid}.key"
        seed.write_text(b64(key, True))
        keys[kid] = str(seed)
        listed = ", ".join(f'"{c}"' for c in caps)
        blocks.append(f'[[keys]]\nkid="{kid}"\nsub="{sub}"\npublic_key="{b64(key, False)}"\n'
                      f"capabilities=[{listed}]\n")
    registry_path = tmp_path / "registry.toml"
    registry_path.write_text("\n".join(blocks))
    registry = sa.KeyRegistry(registry_path, lambda: NOW)
    registry.load_initial()
    auth = sa.ServiceAuthenticator(audience=AUDIENCE, registry=registry, clock=lambda: NOW)
    app = create_app(world.service, auth)
    return SimpleNamespace(world=world, keys=keys, app=app, client=TestClient(app))


def call(api, who, method, target, body=None, capability=ASSIGN_CAPABILITY, **mint):
    raw = b"" if body is None else json.dumps(body).encode()
    headers = {"content-type": "application/json"} if body is not None else {}
    if who is not None:
        headers[sa.ASSERTION_HEADER] = sa.mint_assertion(
            kid=who, sub=IDENTITIES[who][0], audience=mint.pop("audience", AUDIENCE),
            capability=capability, engagement="kepler", classification="internal",
            method=method, target=target, body=raw, key_path=api.keys[who], now=NOW)
    return api.client.request(method, target, content=raw, headers=headers)


def test_health_needs_no_assertion(api):
    assert api.client.get("/health").json() == {"status": "ok", "agent": "kepler"}


def test_assignment_requires_a_signed_assertion(api):
    assert call(api, None, "POST", "/v1/assignments", {"issue": 128}).status_code == 401
    response = api.client.post("/v1/assignments", json={"issue": 128},
                               headers={sa.ASSERTION_HEADER: "garbage"})
    assert response.status_code == 401 and api.world.store.runs() == []


def test_a_signed_founder_call_authorizes_a_run(api):
    response = call(api, "founder-1", "POST", "/v1/assignments", {"issue": 128})
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["state"] == "authorized" and view["issue"] == 128
    assert view["branch"] == "kepler/issue-128" and view["created_by"] == "founder"
    assert response.headers["x-request-id"]
    again = call(api, "founder-1", "GET", f"/v1/assignments/{view['run_id']}",
                 capability=READ_CAPABILITY)
    assert again.status_code == 200 and again.json()["run_id"] == view["run_id"]


def test_the_assertion_is_bound_to_the_request_body(api):
    token = sa.mint_assertion(
        kid="founder-1", sub="founder", audience=AUDIENCE, capability=ASSIGN_CAPABILITY,
        engagement="kepler", classification="internal", method="POST",
        target="/v1/assignments", body=json.dumps({"issue": 128}).encode(),
        key_path=api.keys["founder-1"], now=NOW)
    response = api.client.post("/v1/assignments", json={"issue": 129},  # a different issue
                               headers={sa.ASSERTION_HEADER: token})
    assert response.status_code == 401 and api.world.store.runs() == []


def test_replayed_assertions_are_rejected(api):
    body = json.dumps({"issue": 128}).encode()
    token = sa.mint_assertion(
        kid="founder-1", sub="founder", audience=AUDIENCE, capability=ASSIGN_CAPABILITY,
        engagement="kepler", classification="internal", method="POST",
        target="/v1/assignments", body=body, key_path=api.keys["founder-1"], now=NOW, jti="same")
    send = lambda: api.client.post(  # noqa: E731
        "/v1/assignments", content=body,
        headers={sa.ASSERTION_HEADER: token, "content-type": "application/json"})
    assert send().status_code == 200
    assert send().status_code == 401


def test_wrong_audience_and_wrong_capability_are_rejected(api):
    assert call(api, "founder-1", "POST", "/v1/assignments", {"issue": 128},
                audience="deployment-controller").status_code == 401
    assert call(api, "reader-only", "POST", "/v1/assignments", {"issue": 128}).status_code == 403
    assert call(api, "founder-1", "POST", "/v1/assignments", {"issue": 128},
                capability=READ_CAPABILITY).status_code in (401, 403)
    assert api.world.store.runs() == []


def test_an_authenticated_stranger_who_is_not_an_assigner_is_refused(api):
    assert call(api, "stranger-1", "POST", "/v1/assignments", {"issue": 128}).status_code == 403
    assert call(api, "stranger-1", "GET", "/v1/assignments/abc",
                capability=READ_CAPABILITY).status_code == 403
    assert api.world.store.runs() == []


@pytest.mark.parametrize("body", [{"issue": "128"}, {"issue": 0}, {"issue": -3}, {"issue": 1.5},
                                  {"issue": True}, {}, {"issue": 128, "branch": "main"},
                                  {"issue": 128, "clone_url": "https://evil.example/x.git"},
                                  {"issue": 10**12}])
def test_request_bodies_are_strict_and_name_only_an_issue(api, body):
    assert call(api, "founder-1", "POST", "/v1/assignments", body).status_code == 422
    assert api.world.store.runs() == []


def test_policy_errors_map_to_http_statuses(api):
    assert call(api, "founder-1", "POST", "/v1/assignments", {"issue": 51}).status_code == 422
    assert call(api, "founder-1", "POST", "/v1/assignments", {"issue": 128}).status_code == 200
    duplicate = call(api, "founder-1", "POST", "/v1/assignments", {"issue": 128})
    assert duplicate.status_code == 409 and duplicate.json()["detail"] == "active_run_exists"
    missing = call(api, "founder-1", "GET", "/v1/assignments/nope", capability=READ_CAPABILITY)
    assert missing.status_code == 404


def test_cancel_over_http(api):
    run = call(api, "founder-1", "POST", "/v1/assignments", {"issue": 128}).json()
    done = call(api, "founder-1", "POST", f"/v1/assignments/{run['run_id']}:cancel")
    assert done.status_code == 200 and done.json()["state"] == "cancelled"
    assert call(api, "founder-1", "POST",
                f"/v1/assignments/{run['run_id']}:cancel").status_code == 409


def test_there_is_no_route_to_merge_push_deploy_or_change_policy(api):
    paths = {(m, r.path) for r in api.app.routes if hasattr(r, "methods") for m in r.methods}
    assert paths == {("GET", "/health"), ("POST", "/v1/assignments"),
                     ("GET", "/v1/assignments/{run_id}"), ("POST", "/v1/assignments/{run_id}:cancel")}
    for bad in ("/v1/merge", "/v1/push", "/v1/deploy", "/v1/policy", "/docs", "/openapi.json"):
        assert api.client.post(bad).status_code in (404, 405)


def test_responses_never_expose_paths_or_secrets(api):
    run = call(api, "founder-1", "POST", "/v1/assignments", {"issue": 128}).json()
    text = json.dumps(run)
    assert "workspace" not in text and str(api.world.root) not in text and TOKEN not in text


# ---- the GitHub REST client ---------------------------------------------------------------


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def client_with(responses):
    sent = []

    def opener(request, timeout=None):
        sent.append(request)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return Response(json.dumps(item).encode())

    return HttpGitHubClient(KeplerPolicy(), TOKEN, opener=opener), sent


def pull_json(number=7, draft=True, head="kepler/issue-128"):
    return {"number": number, "html_url": f"https://github.com/vchokshi/viharbot/pull/{number}",
            "draft": draft, "head": {"ref": head}}


def test_draft_pr_request_is_always_a_draft_against_main_with_a_bearer_header():
    client, sent = client_with([pull_json()])
    pull = client.create_draft_pull_request("kepler/issue-128", "T", "B")
    request = sent[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.github.com/repos/vchokshi/viharbot/pulls"
    assert json.loads(request.data) == {"title": "T", "body": "B", "head": "kepler/issue-128",
                                        "base": "main", "draft": True}
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in request.full_url
    assert pull.draft and pull.number == 7


def test_a_non_draft_response_is_treated_as_a_failure():
    client, _ = client_with([pull_json(draft=False)])
    with pytest.raises(GitHubError, match="draft"):
        client.create_draft_pull_request("kepler/issue-128", "T", "B")


def test_update_sends_only_title_and_body():
    client, sent = client_with([pull_json()])
    client.update_pull_request(7, "T2", "B2")
    assert sent[0].get_method() == "PATCH"
    assert json.loads(sent[0].data) == {"title": "T2", "body": "B2"}  # no state, base or draft


def test_get_issue_and_find_pull_request_are_scoped_to_the_repository():
    client, sent = client_with([
        {"number": 128, "state": "open", "title": "t"},
        {"number": 5, "state": "open", "title": "pr", "pull_request": {}},
        urllib.error.HTTPError("u", 404, "nf", {}, None),
        [pull_json(head="someone-else")],
        [pull_json(head="kepler/issue-128")],
    ])
    assert client.get_issue(128).is_pull_request is False
    assert client.get_issue(5).is_pull_request is True
    assert client.get_issue(99999) is None
    assert client.find_pull_request("kepler/issue-128") is None
    assert client.find_pull_request("kepler/issue-128").number == 7
    assert all(r.full_url.startswith("https://api.github.com/repos/vchokshi/viharbot/")
               for r in sent)
    assert "head=vchokshi%3Akepler%2Fissue-128" in sent[3].full_url


def test_errors_never_contain_the_token_or_response_body():
    secret_body = io.BytesIO(f"leaked {TOKEN}".encode())
    client, _ = client_with([
        urllib.error.HTTPError("u", 500, "boom", {}, secret_body),
        urllib.error.URLError(f"connect failed for {TOKEN}"),
    ])
    for _ in range(2):
        with pytest.raises(GitHubError) as info:
            client.get_issue(1)
        assert TOKEN not in str(info.value) and "leaked" not in str(info.value)


def test_requests_outside_the_repository_are_impossible():
    client, _ = client_with([])
    with pytest.raises(GitHubError):
        client._request("GET", "/repos/vchokshi/viharbot-onboarding-instructor/issues/1")
    with pytest.raises(GitHubError):
        client._request("GET", "/user")


@pytest.mark.parametrize("base", ["http://api.github.com", "ftp://x", "https://", "http://example.com",
                                  "file:///etc", "api.github.com"])
def test_credentials_go_only_over_https_or_a_private_address(base):
    with pytest.raises(ValueError):
        HttpGitHubClient(KeplerPolicy(), TOKEN, api_base=base)


@pytest.mark.parametrize("base", ["https://api.github.com", "http://127.0.0.1:9", "http://10.0.0.5"])
def test_permitted_api_bases(base):
    HttpGitHubClient(KeplerPolicy(), TOKEN, api_base=base)


@pytest.mark.parametrize("token", ["", "   ", None])
def test_a_token_is_required(token):
    with pytest.raises((ValueError, AttributeError)):
        HttpGitHubClient(KeplerPolicy(), token)  # type: ignore[arg-type]
