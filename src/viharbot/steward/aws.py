"""The only place the steward imports AWS. Everything else takes injected clients, so the core and
the orchestration stay testable offline and a reviewer can see exactly where AWS is touched."""

from __future__ import annotations

# Short timeouts and few attempts so a hung AWS call cannot hold a poll past its deadline.
# TOTAL attempts, first try included: botocore's `max_attempts` counts retries AFTER the first try,
# so it is not used here. Each call is bounded to (connect + read) x TOTAL_ATTEMPTS, so 20 seconds
# at worst for the shared clients and 26 for Bedrock.
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 7
TOTAL_ATTEMPTS = 2
BEDROCK_CONNECT_TIMEOUT_SECONDS = 3
BEDROCK_READ_TIMEOUT_SECONDS = 10  # the reply is one word; this is generous


def _config(connect: int, read: int):
    from botocore.config import Config

    return Config(connect_timeout=connect, read_timeout=read,
                  retries={"total_max_attempts": TOTAL_ATTEMPTS, "mode": "standard"})


def client(service: str, region: str):
    import boto3  # imported lazily: tests and the pure core never need it

    return boto3.client(service, region_name=region,
                        config=_config(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS))


def secrets_client(region: str):
    return client("secretsmanager", region)


def dynamodb_client(region: str):
    return client("dynamodb", region)


def sns_client(region: str):
    return client("sns", region)


def cloudwatch_client(region: str):
    return client("cloudwatch", region)


def bedrock_runtime_client(region: str):
    """A Bedrock Runtime client with short timeouts and two attempts in total (one retry): a draft
    is optional, so a slow or failing model must never hold a poll up."""
    import boto3

    return boto3.client("bedrock-runtime", region_name=region,
                        config=_config(BEDROCK_CONNECT_TIMEOUT_SECONDS, BEDROCK_READ_TIMEOUT_SECONDS))
