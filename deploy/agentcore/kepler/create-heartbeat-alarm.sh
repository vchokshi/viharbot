#!/bin/bash
# Creates Kepler's heartbeat alarm in the Cyera AWS lab account: "no poll has completed in 8 hours".
#
#   bash deploy/agentcore/kepler/create-heartbeat-alarm.sh                  # a dry run: prints the request
#   APPLY=yes bash deploy/agentcore/kepler/create-heartbeat-alarm.sh        # creates it, notifications OFF
#   APPLY=yes ENABLE=yes bash deploy/agentcore/kepler/create-heartbeat-alarm.sh   # notifications ON
#
# A DRY RUN by default: it prints the exact request and changes nothing. Set APPLY=yes to create it.
# Run from the repository root, signed in as the account administrator, only after the founder has
# approved this step.
#
# Why two stages: until the first real poll has published PollCompleted there is no data, and the
# alarm treats missing data as breaching, so an enabled alarm would fire at once. It is therefore
# created with notifications OFF. After a poll has run (the schedule `test`, or a manual check),
# re-run with ENABLE=yes. ENABLE=yes refuses to proceed unless PollCompleted has at least one 1 in
# the last 24 hours, so it cannot be switched on into an immediate false alarm.
#
# What APPLY=yes does, as the deployer role (credentials stay inside this process): one
# put-metric-alarm call for viharbot-kepler-heartbeat-missing, tagged project=viharbot, agent=kepler,
# environment=lab, owner=vchokshi, managed-by=human-supervised, notifying the existing alerts topic.
# The deployer may put and describe alarms named viharbot-kepler-*, and nothing else is touched.
# Roll back with: aws cloudwatch delete-alarms --alarm-names viharbot-kepler-heartbeat-missing
# Record the outcome in docs/aws-lab-change-record.md.
set -u
export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 AWS_PAGER=""
HERE="deploy/agentcore/kepler"
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 2
TOPIC="arn:aws:sns:us-west-2:$ACCT:viharbot-kepler-alerts"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

ARGS=(--topic-arn "$TOPIC")
[ "${ENABLE:-no}" = "yes" ] && ARGS+=(--enable-actions)
python3 -I "$HERE/build_alarm.py" "${ARGS[@]}" > "$WORK/alarm.json" || { echo "ABORT: could not build the alarm"; exit 2; }

echo "account: $ACCT"
echo "topic:   $TOPIC"
echo "--- alarm request"; cat "$WORK/alarm.json"
if [ "${APPLY:-no}" != "yes" ]; then
  echo
  echo "DRY RUN: nothing was created. Review the above, then re-run with APPLY=yes."
  [ "${ENABLE:-no}" = "yes" ] && echo "(ENABLE=yes only has effect together with APPLY=yes.)"
  exit 0
fi

if [ "${ENABLE:-no}" = "yes" ]; then
  # Checked as the administrator, before anything is changed: a real poll must already have run.
  START=$(python3 -I -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)-d.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
  END=$(python3 -I -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')
  SUM=$(aws cloudwatch get-metric-statistics --namespace ViharBot/Kepler --metric-name PollCompleted \
    --start-time "$START" --end-time "$END" --period 86400 --statistics Sum \
    --query 'max(Datapoints[].Sum)' --output text 2>/dev/null)
  case "$SUM" in
    ""|None|null|0|0.0) echo "ABORT: no completed poll (PollCompleted = 1) in the last 24 hours."
                        echo "Run a poll first, or this alarm would fire immediately."; exit 3;;
  esac
  echo "PollCompleted in the last 24 hours: $SUM (a poll has run)"
fi

read -r AK SK ST < <(aws sts assume-role --role-arn "arn:aws:iam::$ACCT:role/viharbot-kepler-deployer" \
  --role-session-name create-heartbeat-alarm --duration-seconds 3600 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
[ -z "$AK" ] && { echo "ABORT: could not assume the deployer role"; exit 2; }
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST"
echo "running as: $(aws sts get-caller-identity --query Arn --output text)"

step() { local name="$1"; shift; echo "== $name"; if ! "$@"; then echo "STOPPED at: $name"; exit 1; fi; }
step "heartbeat alarm (notifications $([ "${ENABLE:-no}" = "yes" ] && echo ON || echo OFF))" \
  aws cloudwatch put-metric-alarm --cli-input-json "file://$WORK/alarm.json"
step "read it back" aws cloudwatch describe-alarms --alarm-names viharbot-kepler-heartbeat-missing \
  --query 'MetricAlarms[0].[AlarmName,StateValue,ActionsEnabled]' --output text
echo "Done. Record it in docs/aws-lab-change-record.md."
