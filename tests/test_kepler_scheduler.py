"""Kepler's polling schedule: the request, the role files and the script. Offline; nothing here
contacts AWS. The request is checked against the AWS SDK's own service model, and against the
runtime's own input schema, so a schedule that the SDK or the runtime would refuse fails here.
"""

from __future__ import annotations

import base64
import datetime
import fnmatch
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEPLER = ROOT / "deploy/agentcore/kepler"
IAM = KEPLER / "iam"
SCRIPT = KEPLER / "create-schedule.sh"
pytestmark = pytest.mark.skipif(not (KEPLER / "build_schedule.py").exists(), reason="scheduler files absent")

ACCT = "123456789012"
RUNTIME = f"arn:aws:bedrock-agentcore:us-west-2:{ACCT}:runtime/kepler_AbCdEfGhIj"
ROLE = f"arn:aws:iam::{ACCT}:role/viharbot-kepler-scheduler"
NOW = datetime.datetime(2026, 11, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)


def load():
    spec = importlib.util.spec_from_file_location("build_schedule", KEPLER / "build_schedule.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bs = load()


def test_the_recurring_schedule_polls_every_six_hours_and_starts_disabled_without_retries():
    request = bs.build("recurring", RUNTIME, ROLE)
    assert request["Name"] == "viharbot-kepler-poll" and request["ScheduleExpression"] == "rate(6 hours)"
    assert request["State"] == "DISABLED"  # switched on deliberately, after the one-time test passes
    assert bs.build("recurring", RUNTIME, ROLE, enabled=True)["State"] == "ENABLED"
    target = request["Target"]
    assert target["RoleArn"] == ROLE and target["RetryPolicy"]["MaximumRetryAttempts"] == 0
    assert target["Arn"] == "arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime"
    assert request["FlexibleTimeWindow"] == {"Mode": "OFF"} and request["GroupName"] == "default"


def test_the_test_schedule_runs_once_soon_in_utc_and_deletes_itself():
    request = bs.build("test", RUNTIME, ROLE, now=NOW)
    assert request["Name"] == "viharbot-kepler-poll-test"
    assert request["ScheduleExpression"] == "at(2026-11-07T12:03:00)"
    assert request["ScheduleExpressionTimezone"] == "UTC" and request["ActionAfterCompletion"] == "DELETE"
    assert request["State"] == "ENABLED"
    for minutes in (0, 1, 61):
        with pytest.raises(ValueError):
            bs.build("test", RUNTIME, ROLE, now=NOW, minutes_ahead=minutes)


@pytest.mark.parametrize("mode", ["recurring", "test"])
def test_the_request_is_valid_against_the_aws_sdk_service_model(mode):
    pytest.importorskip("botocore")
    import botocore.session
    from botocore.validate import validate_parameters
    model = botocore.session.get_session().get_service_model("scheduler")
    request = bs.build(mode, RUNTIME, ROLE, now=NOW)
    report = validate_parameters(request, model.operation_model("CreateSchedule").input_shape)
    assert report is None  # raises ParamValidationError on any unknown or malformed field


@pytest.mark.parametrize("encoding", ["plain", "base64"])
def test_the_runtime_accepts_exactly_the_payload_the_schedule_sends(encoding):
    from viharbot.steward.runtime import Invocation
    sent = json.loads(bs.build("recurring", RUNTIME, ROLE, payload_encoding=encoding)["Target"]["Input"])
    assert sent["AgentRuntimeArn"] == RUNTIME and set(sent) == {"AgentRuntimeArn", "Payload"}
    raw = sent["Payload"] if encoding == "plain" else base64.b64decode(sent["Payload"]).decode()
    invocation = Invocation.model_validate(json.loads(raw))
    # a scheduled poll must return at once: Scheduler waits synchronously and gives up on slow calls
    assert invocation.action == "poll" and invocation.wait is False and invocation.pr is None


@pytest.mark.parametrize("runtime", [
    f"arn:aws:bedrock-agentcore:us-east-1:{ACCT}:runtime/kepler_x",
    "arn:aws:bedrock-agentcore:us-west-2:999:runtime/kepler_x",
    f"arn:aws:bedrock-agentcore:us-west-2:{ACCT}:runtime/x; rm -rf /",
    f"arn:aws:bedrock-agentcore:us-west-2:{ACCT}:memory/kepler_x", "", "*",
])
def test_a_runtime_that_is_not_one_in_this_region_is_refused(runtime):
    with pytest.raises(ValueError):
        bs.build("recurring", runtime, ROLE)


@pytest.mark.parametrize("role", [
    f"arn:aws:iam::{ACCT}:role/viharbot-kepler-runtime", f"arn:aws:iam::{ACCT}:role/admin", "", "*",
])
def test_only_the_scheduler_role_can_be_the_target_role(role):
    with pytest.raises(ValueError):
        bs.build("recurring", RUNTIME, role)


def test_the_role_can_only_be_assumed_by_scheduler_for_kepler_schedules_in_this_account():
    trust = json.loads((IAM / "scheduler-trust.json").read_text())
    (statement,) = trust["Statement"]
    assert statement["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole" and statement["Effect"] == "Allow"
    conditions = statement["Condition"]
    assert conditions["StringEquals"] == {"aws:SourceAccount": "ACCOUNT_ID"}
    assert conditions["ArnLike"]["aws:SourceArn"] == \
        "arn:aws:scheduler:us-west-2:ACCOUNT_ID:schedule/default/viharbot-kepler-*"


def test_the_role_can_do_one_thing_invoke_the_one_runtime_and_stays_inside_the_boundary():
    policy = json.loads((IAM / "scheduler-policy.json").read_text())
    (statement,) = policy["Statement"]
    assert statement["Effect"] == "Allow" and statement["Action"] == "bedrock-agentcore:InvokeAgentRuntime"
    resources = statement["Resource"]
    assert resources[0] == "RUNTIME_ARN" and all(r.startswith("RUNTIME_ARN") for r in resources)
    assert "*" not in resources and not any(r.endswith(":*") for r in resources)
    # inside the permissions boundary: the boundary allows this action only on runtimes here
    boundary = {s["Sid"]: s for s in json.loads((IAM / "boundary-policy.json").read_text())["Statement"]}
    allowed = boundary["InvokeAgentRuntimesInThisAccountOnly"]
    assert allowed["Action"] == statement["Action"]
    pattern = allowed["Resource"].replace("ACCOUNT_ID", ACCT)
    for resource in resources:
        assert fnmatch.fnmatchcase(resource.replace("RUNTIME_ARN", RUNTIME), pattern)


def test_the_deployer_may_create_the_schedule_and_pass_this_role_to_scheduler():
    deployer = json.loads((IAM / "deployer-policy.json").read_text())["Statement"]
    schedules = [s for s in deployer if s.get("Sid") == "SchedulerKeplerOnly"]
    assert schedules and "scheduler:CreateSchedule" in schedules[0]["Action"]
    for name in ("viharbot-kepler-poll", "viharbot-kepler-poll-test"):
        arn = f"arn:aws:scheduler:us-west-2:ACCOUNT_ID:schedule/default/{name}"
        assert fnmatch.fnmatchcase(arn, schedules[0]["Resource"])
    passes = [s for s in deployer if s.get("Sid") == "PassOnlyKeplerRolesToTheirServices"][0]
    assert "arn:aws:iam::ACCOUNT_ID:role/viharbot-kepler-scheduler" in passes["Resource"]
    services = set(passes["Condition"]["StringEquals"]["iam:PassedToService"])
    assert {"scheduler.amazonaws.com"} <= services


def test_the_script_is_a_dry_run_unless_apply_is_set_and_changes_nothing_before_that():
    text = SCRIPT.read_text()
    guard = text.index('"${APPLY:-no}" != "yes"')
    for risky in ("sts assume-role", "iam create-role", "iam put-role-policy", "scheduler create-schedule"):
        for line_no, line in enumerate(text.splitlines()):
            if risky in line and not line.lstrip().startswith(("#", "echo")):
                assert text.index(line) > guard, (risky, line_no)
    assert "DRY RUN" in text and 'exit 0' in text[guard:guard + 400]
    assert text.index("viharbot-kepler-boundary") < guard  # the boundary is always attached on create
    assert "--permissions-boundary" in text and "viharbot-kepler-deployer" in text
    assert "python3 -I" in text  # isolated mode: no stray module can be picked up from the cwd


def run_script(tmp_path, mode, **env):
    """Run the real script with a stub `aws` that answers only the account lookup."""
    import os
    import shutil
    import subprocess
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
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "RUNTIME_ARN": RUNTIME, **env}
    done = subprocess.run(["bash", str(SCRIPT), mode], cwd=ROOT, env=environment, capture_output=True,
                          text=True, timeout=60)
    return done, (calls.read_text().splitlines() if calls.exists() else [])


@pytest.mark.parametrize("mode", ["test", "recurring"])
def test_the_dry_run_prints_the_whole_plan_and_makes_no_aws_call_but_the_account_lookup(tmp_path, mode):
    done, calls = run_script(tmp_path, mode)
    assert done.returncode == 0, done.stderr
    assert calls == ["sts get-caller-identity --query Account --output text"]
    out = done.stdout
    assert "DRY RUN: nothing was created" in out
    # the printed trust policy names Scheduler as the one principal (checked as a parsed field)
    trust_start = out.index("--- role trust policy") + len("--- role trust policy")
    trust = json.loads(out[trust_start:out.index("--- role permissions policy")])
    assert trust["Statement"][0]["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert "123456789012" in out and "ACCOUNT_ID" not in out
    assert "bedrock-agentcore:InvokeAgentRuntime" in out and RUNTIME in out and "RUNTIME_ARN" not in out
    assert ('"Name": "viharbot-kepler-poll-test"' if mode == "test" else '"rate(6 hours)"') in out


def test_a_bad_runtime_arn_or_mode_is_refused_before_anything_else(tmp_path):
    done, calls = run_script(tmp_path, "recurring", RUNTIME_ARN="arn:aws:bedrock-agentcore:eu-west-1:1:runtime/x")
    assert done.returncode == 2 and "DRY RUN" not in done.stdout
    done, _ = run_script(tmp_path, "delete")
    assert done.returncode == 2 and "usage" in done.stdout


def test_the_script_never_contains_a_credential_or_a_hardcoded_account():
    text = SCRIPT.read_text()
    for needle in ("AKIA", "ASIA", "aws_secret", "ghs_", "ghp_"):
        assert needle not in text
    assert "012079073600" not in text and "123456789012" not in text
