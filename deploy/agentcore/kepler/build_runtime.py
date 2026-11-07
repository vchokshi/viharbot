"""Builds the AgentCore `CreateAgentRuntime` request for Kepler. Standard library only.

It prints JSON for `aws bedrock-agentcore-control create-agent-runtime --cli-input-json`; it contacts
nothing, so the tests can check the request against the AWS SDK's own service model.

What the request says, and why:

* The container is the steward image in the one Kepler ECR repository, addressed by an IMMUTABLE
  tag (a commit id), never `latest`, so what runs is exactly what was reviewed and pushed.
* The execution role is `viharbot-kepler-runtime`: the role whose permissions were reviewed, capped
  by the permissions boundary, and simulated against the live account.
* No authorizer is configured, so callers are authenticated by AWS IAM (SigV4). The service has no
  authentication of its own and must never be reachable any other way.
* The network mode is PUBLIC, which for AgentCore means the container can reach the internet
  (GitHub, AWS APIs) and is not reachable except through InvokeAgentRuntime. No VPC is created.
* The only environment variable is the alert topic. Everything else is the code's default: the
  approved Bedrock model is on, polls are bounded, and no key or token is ever set here.
* Tags are applied at creation, with exactly the keys the deployer role's policy requires
  (`project` and `agent`) plus the rest of the project tag set.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# A runtime name allows letters, digits and underscores only (no hyphens), up to 48 characters.
NAME = "viharbot_kepler"
REGION = "us-west-2"
REPOSITORY = "viharbot/kepler"
ROLE_NAME = "viharbot-kepler-runtime"
TAGS = {"project": "viharbot", "agent": "kepler", "environment": "lab", "owner": "vchokshi",
        "managed-by": "human-supervised"}
_ACCOUNT = re.compile(r"^\d{12}$")
# An immutable, specific tag: a commit id (7 to 40 hex characters), never a moving label.
_TAG = re.compile(r"^[0-9a-f]{7,40}$")


def build(account: str, image_tag: str) -> dict:
    if not _ACCOUNT.fullmatch(account):
        raise ValueError("account must be 12 digits")
    if not _TAG.fullmatch(image_tag):
        raise ValueError("image tag must be a commit id (7 to 40 lowercase hex characters), not a label")
    return {
        "agentRuntimeName": NAME,
        "description": "Kepler first-contributor steward (read-only; drafts only; alerts the founder by email)",
        "agentRuntimeArtifact": {"containerConfiguration": {
            "containerUri": f"{account}.dkr.ecr.{REGION}.amazonaws.com/{REPOSITORY}:{image_tag}"}},
        "roleArn": f"arn:aws:iam::{account}:role/{ROLE_NAME}",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": {
            "KEPLER_ALERT_TOPIC_ARN": f"arn:aws:sns:{REGION}:{account}:viharbot-kepler-alerts"},
        "tags": dict(TAGS),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", required=True)
    parser.add_argument("--image-tag", required=True)
    args = parser.parse_args(argv)
    try:
        request = build(args.account, args.image_tag)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(request, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
