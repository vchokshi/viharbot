"""Kepler's heartbeat alarm: the request, its timing behaviour and the script. Offline.

The request is checked against the AWS SDK's own CloudWatch model. The timing claim (it stays quiet
while polls run every 6 hours, and fires 1 to 2 hours after a missed run) is checked by simulating
the alarm under both readings of how CloudWatch lines up its hourly periods, because which one
CloudWatch uses was not confirmed in its documentation.
"""

from __future__ import annotations

import bisect
import importlib.util
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEPLER = ROOT / "deploy/agentcore/kepler"
SCRIPT = KEPLER / "create-heartbeat-alarm.sh"
pytestmark = pytest.mark.skipif(not (KEPLER / "build_alarm.py").exists(), reason="alarm files absent")

ACCT = "123456789012"
TOPIC = f"arn:aws:sns:us-west-2:{ACCT}:viharbot-kepler-alerts"


def load():
    spec = importlib.util.spec_from_file_location("build_alarm", KEPLER / "build_alarm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ba = load()


def test_the_alarm_watches_the_completed_poll_heartbeat_and_treats_silence_as_a_failure():
    request = ba.build(TOPIC)
    assert request["AlarmName"] == "viharbot-kepler-heartbeat-missing"
    assert (request["Namespace"], request["MetricName"]) == ("ViharBot/Kepler", "PollCompleted")
    assert request["Statistic"] == "Sum" and request["Period"] == 3600
    assert request["ComparisonOperator"] == "LessThanThreshold" and request["Threshold"] == 1
    assert request["TreatMissingData"] == "breaching"
    # every period in the window must breach, so one quiet hour between runs never alarms
    assert request["EvaluationPeriods"] == request["DatapointsToAlarm"] == 8
    assert request["AlarmActions"] == [TOPIC] and request["OKActions"] == [TOPIC]
    assert request["ActionsEnabled"] is False  # no data exists before the first poll
    assert ba.build(TOPIC, enable_actions=True)["ActionsEnabled"] is True
    assert {t["Key"]: t["Value"] for t in request["Tags"]} == {
        "project": "viharbot", "agent": "kepler", "environment": "lab", "owner": "vchokshi",
        "managed-by": "human-supervised"}


def test_the_request_is_valid_against_the_aws_sdk_cloudwatch_model():
    pytest.importorskip("botocore")
    import botocore.session
    from botocore.validate import validate_parameters
    model = botocore.session.get_session().get_service_model("cloudwatch")
    for enabled in (False, True):
        report = validate_parameters(ba.build(TOPIC, enable_actions=enabled),
                                     model.operation_model("PutMetricAlarm").input_shape)
        assert report is None  # raises ParamValidationError on any unknown or malformed field


@pytest.mark.parametrize("topic", [
    "", "*", f"arn:aws:sns:us-east-1:{ACCT}:viharbot-kepler-alerts", f"arn:aws:sns:us-west-2:{ACCT}:other-topic",
    "arn:aws:sns:us-west-2:1:viharbot-kepler-alerts", f"arn:aws:sqs:us-west-2:{ACCT}:viharbot-kepler-alerts",
    f"{TOPIC}; rm -rf /",
])
def test_only_a_kepler_topic_in_this_region_can_be_notified(topic):
    with pytest.raises(ValueError):
        ba.build(topic)


def test_the_metric_and_namespace_are_exactly_what_the_runtime_publishes():
    from viharbot.steward.metrics import NAMESPACE
    assert ba.NAMESPACE == NAMESPACE
    source = (ROOT / "src/viharbot/steward/poller.py").read_text()
    assert f'metrics.put("{ba.METRIC}"' in source  # the heartbeat metric exists in the code


# ---- timing: simulate the alarm under both readings of CloudWatch's period alignment ------------------


def alarming_aligned(success_minutes: list[int], now: int, periods: int) -> bool:
    """Hourly buckets aligned to the clock hour, the current partial hour counted as the newest."""
    buckets = {m // 60 for m in success_minutes if m <= now}
    newest = now // 60
    return not any(newest - i in buckets for i in range(periods))


def alarming_sliding(success_minutes: list[int], now: int, periods: int) -> bool:
    """The last `periods` hours counted back from now, exactly."""
    low = bisect.bisect_right(success_minutes, now - periods * 60)
    high = bisect.bisect_right(success_minutes, now)
    return high - low == 0


def schedule(runs: int, base: int, jitter: int, seed: int) -> list[int]:
    rng = random.Random(seed)  # a completed run's metric lands a little after its scheduled time
    return [base + 360 * k + rng.randint(0, jitter) for k in range(runs)]


@pytest.mark.parametrize("model", [alarming_aligned, alarming_sliding])
def test_a_healthy_six_hour_schedule_never_alarms_under_either_reading(model):
    for base in range(0, 60, 7):
        for seed in range(5):
            successes = schedule(60, base, 10, seed)  # run time and Scheduler jitter up to 10 minutes
            for now in range(successes[0] + 8 * 60, successes[-1] + 1):
                assert not model(successes, now, 8), (model.__name__, base, seed, now)


def test_seven_periods_would_raise_a_false_alarm_when_the_clock_hour_is_the_boundary():
    # A run lands at 59 minutes past the hour; the next, 6 hours and 5 minutes later. Under the
    # clock-hour reading a 7-period alarm briefly alarms before the second run; 8 periods does not.
    successes = [59, 59 + 365]
    alarms7 = [now for now in range(60, successes[1]) if alarming_aligned(successes, now, 7)]
    alarms8 = [now for now in range(60, successes[1]) if alarming_aligned(successes, now, 8)]
    assert alarms7, "this is why the alarm uses 8 periods, not 7"
    assert not alarms8


@pytest.mark.parametrize("model", [alarming_aligned, alarming_sliding])
def test_a_missed_run_alarms_one_to_two_hours_after_it_was_due(model):
    for base in range(0, 60, 11):
        successes = schedule(10, base, 10, 3)
        del successes[5]  # the run at index 5 never happens
        last_good = successes[4]
        due = last_good + 360
        first = next(now for now in range(last_good + 1, last_good + 24 * 60) if model(successes[:5], now, 8))
        assert 7 * 60 < first - last_good <= 8 * 60 + 1, (model.__name__, base, first - last_good)
        assert 60 <= first - due <= 2 * 60 + 1


@pytest.mark.parametrize("model", [alarming_aligned, alarming_sliding])
def test_runs_that_keep_failing_alarm_because_only_completed_polls_count(model):
    # a failed run publishes PollCompleted = 0, which is not a success, so it never resets the alarm
    completed = [0]
    assert model(completed, 8 * 60 + 61, 8)
    assert not model(completed, 8 * 60 - 1, 8)


# ---- the script ---------------------------------------------------------------------------------------


def run_script(tmp_path, stub_sum="None", **env):
    if not shutil.which("bash") or not shutil.which("python3"):
        pytest.skip("bash and python3 are needed to run the script")
    stub = tmp_path / "aws"
    calls = tmp_path / "calls.log"
    stub.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{calls}"\n'
        'if [ "$1 $2" = "sts get-caller-identity" ]; then echo 123456789012; exit 0; fi\n'
        f'if [ "$1 $2" = "cloudwatch get-metric-statistics" ]; then echo "{stub_sum}"; exit 0; fi\n'
        "exit 99\n")
    stub.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", **env}
    done = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=environment, capture_output=True,
                          text=True, timeout=60)
    return done, (calls.read_text().splitlines() if calls.exists() else [])


def test_the_dry_run_prints_the_request_and_makes_no_aws_call_but_the_account_lookup(tmp_path):
    done, calls = run_script(tmp_path)
    assert done.returncode == 0, done.stderr
    assert calls == ["sts get-caller-identity --query Account --output text"]
    assert "DRY RUN: nothing was created" in done.stdout
    request = json.loads(done.stdout[done.stdout.index("{"):done.stdout.rindex("}") + 1])
    assert request["AlarmActions"] == [TOPIC] and request["ActionsEnabled"] is False


def test_enable_without_apply_is_still_a_dry_run_and_says_so(tmp_path):
    done, calls = run_script(tmp_path, ENABLE="yes")
    assert done.returncode == 0 and "only has effect together with APPLY=yes" in done.stdout
    assert len(calls) == 1  # no metric lookup, no role assumption


@pytest.mark.parametrize("stub_sum", ["None", "", "0", "0.0"])
def test_notifications_are_refused_until_a_poll_has_actually_completed(tmp_path, stub_sum):
    done, calls = run_script(tmp_path, stub_sum=stub_sum, APPLY="yes", ENABLE="yes")
    assert done.returncode == 3 and "fire immediately" in done.stdout
    assert not any(c.startswith("sts assume-role") or "put-metric-alarm" in c for c in calls)


def test_with_a_completed_poll_it_goes_on_to_assume_the_deployer_role(tmp_path):
    done, calls = run_script(tmp_path, stub_sum="3.0", APPLY="yes", ENABLE="yes")
    assert "a poll has run" in done.stdout
    assert any(c.startswith("sts assume-role") and "viharbot-kepler-deployer" in c for c in calls)
    assert not any("put-metric-alarm" in c for c in calls)  # the stub refuses the role, so it stops there


def test_the_script_changes_nothing_before_the_apply_guard_and_holds_no_credential():
    text = SCRIPT.read_text()
    guard = text.index('"${APPLY:-no}" != "yes"')
    for risky in ("sts assume-role", "put-metric-alarm", "delete-alarms"):
        for line in text.splitlines():
            if risky in line and not line.lstrip().startswith(("#", "echo")):
                assert text.index(line) > guard, (risky, line)
    assert "python3 -I" in text
    for needle in ("AKIA", "ASIA", "aws_secret", "ghs_", "ghp_", "012079073600", "123456789012"):
        assert needle not in text
