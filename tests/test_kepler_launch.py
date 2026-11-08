"""Kepler's launch scripts: the runtime request, the image push and the permission they need. Offline.

The request is checked against the AWS SDK's own AgentCore model. The scripts are dry runs unless
APPLY=yes, and these tests prove that, with a stub `aws` that answers only the account lookup.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEPLER = ROOT / "deploy/agentcore/kepler"
IAM = KEPLER / "iam"
pytestmark = pytest.mark.skipif(not (KEPLER / "build_runtime.py").exists(), reason="launch files absent")

ACCT = "123456789012"
TAG = "5c34adb940ed"


def load(name):
    spec = importlib.util.spec_from_file_location(name, KEPLER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


br = load("build_runtime")


def test_the_request_names_the_immutable_image_the_reviewed_role_and_nothing_secret():
    request = br.build(ACCT, TAG)
    assert request["agentRuntimeName"] == "viharbot_kepler"
    assert request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == \
        f"{ACCT}.dkr.ecr.us-west-2.amazonaws.com/viharbot/kepler:{TAG}"
    assert request["roleArn"] == f"arn:aws:iam::{ACCT}:role/viharbot-kepler-runtime"
    assert request["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert request["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert "authorizerConfiguration" not in request  # callers are authenticated by AWS IAM (SigV4)
    # the only setting is the alert topic: the model is on by default and no key or token exists here
    assert request["environmentVariables"] == {
        "KEPLER_ALERT_TOPIC_ARN": f"arn:aws:sns:us-west-2:{ACCT}:viharbot-kepler-alerts"}
    blob = json.dumps(request).lower()
    for needle in ("secret", "token", "password", "api_key", "litellm", "kepler_model_id"):
        assert needle not in blob, needle


def test_the_request_is_valid_against_the_aws_sdk_agentcore_model():
    pytest.importorskip("botocore")
    import botocore.session
    from botocore.validate import validate_parameters
    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    shape = model.operation_model("CreateAgentRuntime").input_shape
    assert validate_parameters(br.build(ACCT, TAG), shape) is None
    # the runtime name pattern forbids hyphens: a hyphenated name would be refused by the service
    assert re.fullmatch(shape.members["agentRuntimeName"].metadata["pattern"], br.NAME)
    uri = br.build(ACCT, TAG)["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
    assert re.fullmatch(shape.members["agentRuntimeArtifact"].members["containerConfiguration"]
                        .members["containerUri"].metadata["pattern"], uri)


@pytest.mark.parametrize("tag", ["latest", "v1", "main", "", "5C34ADB", "abc", "5c34adb;rm", "g" * 12, "a" * 41])
def test_only_a_commit_id_can_be_an_image_tag(tag):
    with pytest.raises(ValueError):
        br.build(ACCT, tag)


@pytest.mark.parametrize("account", ["", "123", "abcdefghijkl", "1234567890123"])
def test_the_account_must_be_twelve_digits(account):
    with pytest.raises(ValueError):
        br.build(account, TAG)


def test_the_tags_satisfy_the_deployer_condition_and_match_the_project_tag_set():
    request = br.build(ACCT, TAG)
    deployer = json.loads((IAM / "deployer-policy.json").read_text())["Statement"]
    (create,) = [s for s in deployer if s.get("Sid") == "AgentCoreCreateOnlyTaggedKepler"]
    for key, value in create["Condition"]["StringEquals"].items():
        assert request["tags"][key.rsplit("/", 1)[1]] == value, key  # aws:RequestTag/<key>
    # the one tag set every Kepler resource carries (the other builders and scripts pin the same values)
    assert request["tags"] == {"project": "viharbot", "agent": "kepler", "environment": "lab",
                               "owner": "vchokshi", "managed-by": "human-supervised"}


def test_the_schedule_accepts_the_runtime_arn_this_request_will_produce():
    schedule = load("build_schedule")
    arn = f"arn:aws:bedrock-agentcore:us-west-2:{ACCT}:runtime/viharbot_kepler-HMKTl44pwd"
    assert schedule.build("recurring", arn, f"arn:aws:iam::{ACCT}:role/viharbot-kepler-scheduler")


def test_the_deployer_may_manage_the_workload_identity_of_the_kepler_runtime_and_nothing_wider():
    # CreateAgentRuntime also creates a workload identity; the live API refused without this permission
    deployer = json.loads((IAM / "deployer-policy.json").read_text())["Statement"]
    (statement,) = [s for s in deployer if s.get("Sid") == "AgentCoreWorkloadIdentityForTheKeplerRuntimeOnly"]
    assert statement["Effect"] == "Allow"
    assert sorted(statement["Action"]) == ["bedrock-agentcore:CreateWorkloadIdentity",
                                           "bedrock-agentcore:DeleteWorkloadIdentity",
                                           "bedrock-agentcore:GetWorkloadIdentity"]
    resources = statement["Resource"]
    assert all("ACCOUNT_ID" in r and r.startswith("arn:aws:bedrock-agentcore:us-west-2:") for r in resources)
    assert not any(r == "*" or r.endswith(":*") for r in resources)
    # the identity named in the live refusal is covered, and another agent's identity is not
    mine = f"arn:aws:bedrock-agentcore:us-west-2:{ACCT}:workload-identity-directory/default/workload-identity/viharbot_kepler-HMKTl44pwd"
    other = f"arn:aws:bedrock-agentcore:us-west-2:{ACCT}:workload-identity-directory/default/workload-identity/other_agent-AbCdEfGhIj"
    patterns = [r.replace("ACCOUNT_ID", ACCT) for r in resources]
    assert any(fnmatch.fnmatchcase(mine, p) for p in patterns)
    assert not any(fnmatch.fnmatchcase(other, p) for p in patterns)


# ---- the scripts ----------------------------------------------------------------------------------


def run(script, tmp_path, **env):
    if not shutil.which("bash") or not shutil.which("python3"):
        pytest.skip("bash and python3 are needed to run the script")
    stub = tmp_path / "aws"
    calls = tmp_path / "calls.log"
    stub.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{calls}"\n'
        'if [ "$1 $2" = "sts get-caller-identity" ]; then echo 123456789012; exit 0; fi\n'
        "exit 99\n")
    stub.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", **env}
    done = subprocess.run(["bash", str(KEPLER / script)], cwd=ROOT, env=environment, capture_output=True,
                          text=True, timeout=60)
    return done, (calls.read_text().splitlines() if calls.exists() else [])


def test_the_runtime_script_is_a_dry_run_that_makes_no_aws_call_but_the_account_lookup(tmp_path):
    done, calls = run("create-runtime.sh", tmp_path, IMAGE_TAG=TAG)
    assert done.returncode == 0, done.stderr
    assert calls == ["sts get-caller-identity --query Account --output text"]
    assert "DRY RUN: nothing was created" in done.stdout
    request = json.loads(done.stdout[done.stdout.index("{"):done.stdout.rindex("}") + 1])
    assert request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"].endswith(f"/viharbot/kepler:{TAG}")


@pytest.mark.parametrize("tag", ["latest", "v1", "main"])
def test_the_runtime_script_refuses_a_label_instead_of_a_commit_id(tmp_path, tag):
    done, calls = run("create-runtime.sh", tmp_path, IMAGE_TAG=tag)
    assert done.returncode == 2 and "DRY RUN" not in done.stdout
    assert not any(c.startswith(("sts assume-role", "bedrock-agentcore-control")) for c in calls)


@pytest.mark.parametrize("script", ["create-runtime.sh", "push-image.sh"])
def test_nothing_changes_before_the_apply_guard_and_no_credential_is_held(script):
    text = (KEPLER / script).read_text()
    guard = text.index('"${APPLY:-no}" != "yes"')
    for risky in ("sts assume-role", "create-agent-runtime", "docker push", "docker buildx build",
                  "docker login", "delete-agent-runtime"):
        for line in text.splitlines():
            if risky in line and not line.lstrip().startswith(("#", "echo")):
                assert text.index(line) > guard, (script, risky, line)
    for needle in ("AKIA", "ASIA", "aws_secret", "ghs_", "ghp_", "012079073600", "123456789012"):
        assert needle not in text, (script, needle)
    assert "viharbot-kepler-deployer" in text  # both run as the deployer, never as the administrator


def test_the_push_script_requires_a_commit_id_checks_provenance_and_logs_out():
    text = (KEPLER / "push-image.sh").read_text()
    assert 'IMAGE_TAG:?' in text and "must be a commit id" in text
    assert "merge-base --is-ancestor" in text and "git diff --quiet" in text and "git status --porcelain" in text
    assert "--platform linux/arm64" in text and "already exists" in text  # immutable tags: never overwrite
    assert 'docker logout "$REGISTRY"' in text and "trap" in text  # the registry login does not linger
    assert "buildx build" in text and ":latest" not in text
