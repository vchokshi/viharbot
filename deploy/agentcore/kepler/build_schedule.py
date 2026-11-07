"""Builds the EventBridge Scheduler `CreateSchedule` request for Kepler's poll. Standard library only.

It prints JSON for `aws scheduler create-schedule --cli-input-json`; it contacts nothing. Keeping it
in Python (not shell) lets the tests check the request against the AWS SDK's own service model.

Two shapes:

* `recurring`: `rate(6 hours)`, created DISABLED unless you ask otherwise, so it is switched on
  deliberately after the one-time test has passed.
* `test`: one `at(...)` run a few minutes from now that deletes itself afterwards, to prove the
  whole path (role, universal target, payload, runtime) before anything recurring exists.

The target is Scheduler's universal target for Bedrock AgentCore's InvokeAgentRuntime. The pieces
below marked UNVERIFIED are inferred from the SDK model and could not be confirmed against AWS
documentation or a live call, which is exactly what the one-time test is for.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import re
import sys

# UNVERIFIED: service name inferred from the SDK service id ("Bedrock AgentCore" -> bedrockagentcore),
# the same rule that gives "bedrockagent" for Bedrock Agent.
TARGET_ARN = "arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime"
# The poll runs in the background and the call returns 202 at once. Scheduler waits synchronously
# and was observed giving up on slow calls at about 30 seconds (not documented), while a poll can
# take minutes, so a synchronous call would be recorded as a failure even when the poll succeeds.
PAYLOAD = {"action": "poll", "wait": False}
_RUNTIME_ARN = re.compile(r"^arn:aws:bedrock-agentcore:us-west-2:\d{12}:runtime/[A-Za-z][A-Za-z0-9_-]{0,100}$")
_ROLE_ARN = re.compile(r"^arn:aws:iam::\d{12}:role/viharbot-kepler-scheduler$")
NAMES = {"recurring": "viharbot-kepler-poll", "test": "viharbot-kepler-poll-test"}


def build(mode: str, runtime_arn: str, role_arn: str, *, payload_encoding: str = "plain",
          enabled: bool = False, now: datetime.datetime | None = None, minutes_ahead: int = 3) -> dict:
    if mode not in NAMES:
        raise ValueError("mode must be recurring or test")
    if not _RUNTIME_ARN.fullmatch(runtime_arn):
        raise ValueError("runtime ARN must be a bedrock-agentcore runtime in us-west-2")
    if not _ROLE_ARN.fullmatch(role_arn):
        raise ValueError("role must be viharbot-kepler-scheduler")
    if payload_encoding not in ("plain", "base64"):
        raise ValueError("payload encoding must be plain or base64")
    if not 2 <= minutes_ahead <= 60:
        raise ValueError("a test schedule must run 2 to 60 minutes from now")

    body = json.dumps(PAYLOAD, separators=(",", ":"))
    payload = body if payload_encoding == "plain" else base64.b64encode(body.encode()).decode()
    request = {
        "Name": NAMES[mode],
        "GroupName": "default",
        "Description": "Kepler first-contributor steward poll (read-only; alerts go to the founder by email)",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": TARGET_ARN,
            "RoleArn": role_arn,
            # Retries off: a retry could start a duplicate poll, and the next scheduled run is the
            # retry. Dedup would absorb a duplicate, but a failed run should be seen, not repeated.
            "RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 60},
            "Input": json.dumps({"AgentRuntimeArn": runtime_arn, "Payload": payload},
                                separators=(",", ":")),
        },
    }
    if mode == "recurring":
        request["ScheduleExpression"] = "rate(6 hours)"
        request["State"] = "ENABLED" if enabled else "DISABLED"
    else:
        when = (now or datetime.datetime.now(datetime.timezone.utc)) + datetime.timedelta(minutes=minutes_ahead)
        request["ScheduleExpression"] = f"at({when:%Y-%m-%dT%H:%M:%S})"
        request["ScheduleExpressionTimezone"] = "UTC"
        request["ActionAfterCompletion"] = "DELETE"
        request["State"] = "ENABLED"
    return request


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=sorted(NAMES))
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--payload-encoding", choices=("plain", "base64"), default="plain")
    parser.add_argument("--enabled", action="store_true", help="recurring only: create it ENABLED")
    args = parser.parse_args(argv)
    try:
        request = build(args.mode, args.runtime_arn, args.role_arn,
                        payload_encoding=args.payload_encoding, enabled=args.enabled)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(request, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
