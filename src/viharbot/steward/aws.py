"""The only place the steward imports AWS. Everything else takes injected clients, so the core and
the orchestration stay testable offline and a reviewer can see exactly where AWS is touched."""

from __future__ import annotations


def client(service: str, region: str):
    import boto3  # imported lazily: tests and the pure core never need it

    return boto3.client(service, region_name=region)


def secrets_client(region: str):
    return client("secretsmanager", region)


def dynamodb_client(region: str):
    return client("dynamodb", region)


def sns_client(region: str):
    return client("sns", region)


def cloudwatch_client(region: str):
    return client("cloudwatch", region)


def bedrock_runtime_client(region: str):
    """A Bedrock Runtime client with short timeouts and at most one retry: a draft is optional,
    so a slow or failing model must never hold a poll up."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime", region_name=region,
        config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}))
