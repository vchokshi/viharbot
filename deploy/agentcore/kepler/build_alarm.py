"""Builds the CloudWatch `PutMetricAlarm` request for Kepler's heartbeat. Standard library only.

It prints JSON for `aws cloudwatch put-metric-alarm --cli-input-json`; it contacts nothing, so the
tests can check the request against the AWS SDK's own service model.

The alarm answers one question: "has Kepler completed a poll recently?" The runtime publishes the
`PollCompleted` metric at the end of every run: 1 for a run that finished (ok or partial), 0 for a
run that failed. The schedule runs every 6 hours. The alarm fires when each of the last
`EVALUATION_PERIODS` hourly periods has a sum below 1, which includes periods with no data at all
(missing data is treated as breaching). That is one missed 6-hour run plus margin for how
CloudWatch lines up its periods, and it also fires after eight hours of failed runs. It is the catch-all for everything the
schedule cannot report on its own: a dropped invocation, a dead runtime, a crash before any metric.

It is created with actions DISABLED. Before the first poll has ever run there is no data, so an
enabled alarm would fire at once. Enable the actions (`--enable-actions`) only after a real poll
has published `PollCompleted`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

NAME = "viharbot-kepler-heartbeat-missing"
NAMESPACE = "ViharBot/Kepler"  # the only namespace the runtime role may publish to
METRIC = "PollCompleted"
PERIOD_SECONDS = 3600
# 8 hourly periods. If CloudWatch aligns period boundaries to the clock hour and counts the current
# partial hour, an alarm over N hourly periods looks back only N-1 to N hours, so 7 could raise a
# brief false alarm (and a recovery) when a run lands just after the 6-hour mark. 8 cannot, under
# either reading (see the tests). It still catches a missed run: the alarm fires 1 to 2 hours after
# the missed run was due. Whether CloudWatch aligns that way was not confirmed in its documentation.
EVALUATION_PERIODS = 8
TAGS = {"project": "viharbot", "agent": "kepler", "environment": "lab", "owner": "vchokshi",
        "managed-by": "human-supervised"}
_TOPIC = re.compile(r"^arn:aws:sns:us-west-2:\d{12}:viharbot-kepler-[a-z0-9-]{1,40}$")


def build(topic_arn: str, *, enable_actions: bool = False) -> dict:
    if not _TOPIC.fullmatch(topic_arn):
        raise ValueError("topic must be an SNS topic named viharbot-kepler-<name> in us-west-2")
    return {
        "AlarmName": NAME,
        "AlarmDescription": (
            "Kepler has not completed a poll in 8 hours: a scheduled run was missed, the runtime is "
            "down, or every run is failing. Check the schedule, the runtime logs and PollFailed."),
        "Namespace": NAMESPACE,
        "MetricName": METRIC,
        "Statistic": "Sum",
        "Period": PERIOD_SECONDS,
        "EvaluationPeriods": EVALUATION_PERIODS,
        "DatapointsToAlarm": EVALUATION_PERIODS,
        "Threshold": 1,
        "ComparisonOperator": "LessThanThreshold",
        "TreatMissingData": "breaching",
        "ActionsEnabled": bool(enable_actions),
        "AlarmActions": [topic_arn],
        "OKActions": [topic_arn],  # tell the founder when it recovers, so silence means all is well
        "Tags": [{"Key": key, "Value": value} for key, value in TAGS.items()],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topic-arn", required=True)
    parser.add_argument("--enable-actions", action="store_true",
                        help="create it with notifications ON (only after a real poll has run)")
    args = parser.parse_args(argv)
    try:
        request = build(args.topic_arn, enable_actions=args.enable_actions)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(request, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
