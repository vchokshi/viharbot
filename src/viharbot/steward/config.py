"""Steward configuration from the environment. No credential ever lives here.

Names are validated so a typo cannot point the steward at the wrong table, topic or repository.
The reader app's credentials are read from AWS Secrets Manager at run time by secret *name*; the
secret value is never configuration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_REPO = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_TABLE = re.compile(r"^viharbot-kepler-[a-z0-9-]{1,40}$")
_SECRET = re.compile(r"^viharbot/kepler/[a-z0-9-]{1,60}$")
_TOPIC = re.compile(r"^arn:aws:sns:[a-z0-9-]+:\d{12}:viharbot-kepler-[a-z0-9-]{1,40}$")
_REGION = re.compile(r"^[a-z]{2}-[a-z]+-\d$")
# ADR-011: an explicit allowlist of model IDs, failing closed. Exactly one model today, in-region and
# on-demand. A cross-region profile ("us.", "global."), another Llama, another provider, and in
# particular any Anthropic model, are all refused, so a typo cannot route contributor text through
# other regions or to an unapproved model. The runtime role's IAM allow names the same single ARN
# (deploy/agentcore/kepler/iam/runtime-policy.json); the two must stay in sync, and a test checks it.
DEFAULT_MODEL_ID = "meta.llama3-1-8b-instruct-v1:0"
APPROVED_MODEL_IDS = frozenset({DEFAULT_MODEL_ID})
# The only values of KEPLER_MODEL_ID that turn the model off. Anything else must be approved.
MODEL_OFF_VALUES = frozenset({"off", "none", "disabled"})


class ConfigError(ValueError):
    """The configuration is missing or unsafe; the steward must not start."""


@dataclass(frozen=True)
class StewardConfig:
    repo: str
    region: str
    reader_secret_id: str
    state_table: str
    audit_table: str
    alert_topic_arn: str
    base_branch: str = "main"
    deadline_seconds: int = 240  # one poll never runs longer than this
    max_pulls: int = 25
    reminder_after_hours: int = 72
    dedup_ttl_days: int = 30
    pending_retry_minutes: int = 30
    max_alert_attempts: int = 2  # claims per alert in total: the first, plus one retry
    # On by default: first-contact drafts use the one approved model. None (an explicit
    # KEPLER_MODEL_ID=off) means no model is called and drafts are the plain fixed template.
    model_id: str | None = DEFAULT_MODEL_ID
    model_max_tokens: int = 150

    def __post_init__(self) -> None:
        if not 1 <= self.max_alert_attempts <= 5:
            raise ConfigError("max_alert_attempts must be between 1 and 5")
        if self.model_id is not None and self.model_id not in APPROVED_MODEL_IDS:
            raise ConfigError("model is not on the approved allowlist (ADR-011)")
        if not 20 <= self.model_max_tokens <= 400:
            raise ConfigError("model_max_tokens must be between 20 and 400")
        if not _REPO.fullmatch(self.repo):
            raise ConfigError("repository must be owner/name")
        if not _REGION.fullmatch(self.region):
            raise ConfigError("region looks wrong")
        if not _SECRET.fullmatch(self.reader_secret_id):
            raise ConfigError("reader secret must be named viharbot/kepler/<name>")
        for name in (self.state_table, self.audit_table):
            if not _TABLE.fullmatch(name):
                raise ConfigError("tables must be named viharbot-kepler-<name>")
        if self.state_table == self.audit_table:
            raise ConfigError("state and audit must be separate tables")
        if not _TOPIC.fullmatch(self.alert_topic_arn):
            raise ConfigError("alert topic must be an SNS topic named viharbot-kepler-<name>")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", self.base_branch):
            raise ConfigError("base branch looks wrong")
        if not 10 <= self.deadline_seconds <= 840:
            raise ConfigError("deadline must be between 10 and 840 seconds")
        if not 1 <= self.max_pulls <= 100:
            raise ConfigError("max_pulls must be between 1 and 100")
        if not 1 <= self.reminder_after_hours <= 24 * 30:
            raise ConfigError("reminder window must be between 1 hour and 30 days")
        if not 1 <= self.dedup_ttl_days <= 365 or not 1 <= self.pending_retry_minutes <= 1440:
            raise ConfigError("dedup ttl or retry window out of range")

    @property
    def repo_name(self) -> str:
        return self.repo.split("/", 1)[1]


def model_id_from(raw: str) -> str | None:
    """Unset or empty is the default model; off/none/disabled is the kill switch; anything else is
    passed through and refused by the config check unless it is on the allowlist."""
    value = raw.strip()
    if not value:
        return DEFAULT_MODEL_ID
    return None if value.casefold() in MODEL_OFF_VALUES else value


def from_env(environ: Mapping[str, str]) -> StewardConfig:
    def need(key: str) -> str:
        value = environ.get(key, "").strip()
        if not value:
            raise ConfigError(f"{key} is required")
        return value

    def number(key: str, default: int) -> int:
        raw = environ.get(key, "").strip()
        try:
            return int(raw) if raw else default
        except ValueError:
            raise ConfigError(f"{key} must be a whole number") from None

    return StewardConfig(
        repo=environ.get("KEPLER_REPOSITORY", "vchokshi/viharbot").strip(),
        region=environ.get("AWS_REGION", "us-west-2").strip(),
        reader_secret_id=environ.get("KEPLER_READER_SECRET", "viharbot/kepler/github-app-reader"),
        state_table=environ.get("KEPLER_STATE_TABLE", "viharbot-kepler-state"),
        audit_table=environ.get("KEPLER_AUDIT_TABLE", "viharbot-kepler-audit"),
        alert_topic_arn=need("KEPLER_ALERT_TOPIC_ARN"),
        base_branch=environ.get("KEPLER_BASE_BRANCH", "main").strip(),
        deadline_seconds=number("KEPLER_DEADLINE_SECONDS", 240),
        max_pulls=number("KEPLER_MAX_PULLS", 25),
        reminder_after_hours=number("KEPLER_REMINDER_HOURS", 72),
        dedup_ttl_days=number("KEPLER_DEDUP_TTL_DAYS", 30),
        pending_retry_minutes=number("KEPLER_PENDING_RETRY_MINUTES", 30),
        max_alert_attempts=number("KEPLER_MAX_ALERT_ATTEMPTS", 2),
        model_id=model_id_from(environ.get("KEPLER_MODEL_ID", "")),
        model_max_tokens=number("KEPLER_MODEL_MAX_TOKENS", 150),
    )
