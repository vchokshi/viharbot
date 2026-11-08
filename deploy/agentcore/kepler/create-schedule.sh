#!/bin/bash
# Creates Kepler's scheduler role and its poll schedule in the Cyera AWS lab account.
#
#   RUNTIME_ARN=arn:aws:bedrock-agentcore:us-west-2:<acct>:runtime/<name> \
#     bash deploy/agentcore/kepler/create-schedule.sh test        # a one-time run in a few minutes
#   ... then, once the test has passed:
#   RUNTIME_ARN=... bash deploy/agentcore/kepler/create-schedule.sh recurring
#   RUNTIME_ARN=... ENABLE=yes bash deploy/agentcore/kepler/create-schedule.sh recurring
#
# A DRY RUN by default: it prints the role, the policies and the exact schedule request and changes
# nothing. Set APPLY=yes to create them. Run from the repository root, signed in as the account
# administrator, only after the founder has approved this step. It needs a deployed Kepler runtime
# (RUNTIME_ARN), which does not exist until that step is done.
#
# What APPLY=yes does, as the deployer role (credentials stay inside this process):
#   1. creates the IAM role viharbot-kepler-scheduler with the permissions boundary attached, if it
#      does not exist, trusted only by EventBridge Scheduler for viharbot-kepler-* schedules
#   2. gives it one permission: bedrock-agentcore:InvokeAgentRuntime on that one runtime
#   3. creates the schedule (`test`: one run in 3 minutes that deletes itself; `recurring`:
#      rate(6 hours), created DISABLED unless ENABLE=yes). If the schedule already exists, it is
#      UPDATED to the printed request instead, which is how a disabled `recurring` schedule is
#      switched on: re-run with ENABLE=yes and the live schedule becomes exactly what was reviewed.
# Nothing here runs a model, holds a secret or reads GitHub. The run it triggers does that, with the
# runtime's own role. Record the outcome in docs/aws-lab-change-record.md.
#
# Known gaps, to record in the change record, not hide:
#   * Schedules cannot carry tags (CreateSchedule has no Tags field; TagResource is for schedule
#     groups). The role is tagged; the schedule is identified by its viharbot-kepler- name. Cost is
#     per invocation, about 4 a day, so it is far inside the free tier.
#   * The universal target's name and payload shape are inferred from the SDK model, not confirmed in
#     AWS documentation. The `test` mode exists to confirm them. If the payload is rejected, retry
#     with PAYLOAD_ENCODING=base64; if the path still fails, use a small Lambda instead.
set -u
export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 AWS_PAGER=""
MODE="${1:-}"
case "$MODE" in test|recurring) ;; *) echo "usage: RUNTIME_ARN=... bash $0 test|recurring"; exit 2;; esac
: "${RUNTIME_ARN:?set RUNTIME_ARN to the deployed Kepler runtime ARN}"
IAM_DIR="deploy/agentcore/kepler/iam"
HERE="deploy/agentcore/kepler"
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 2
ROLE_NAME="viharbot-kepler-scheduler"
ROLE_ARN="arn:aws:iam::$ACCT:role/$ROLE_NAME"
BOUNDARY="arn:aws:iam::$ACCT:policy/viharbot-kepler-boundary"
TAGS_KV="Key=project,Value=viharbot Key=agent,Value=kepler Key=environment,Value=lab Key=owner,Value=vchokshi Key=managed-by,Value=human-supervised"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

sed "s/ACCOUNT_ID/$ACCT/g" "$IAM_DIR/scheduler-trust.json" > "$WORK/trust.json"
sed -e "s|RUNTIME_ARN|$RUNTIME_ARN|g" "$IAM_DIR/scheduler-policy.json" > "$WORK/policy.json"
ARGS=("$MODE" --runtime-arn "$RUNTIME_ARN" --role-arn "$ROLE_ARN" --payload-encoding "${PAYLOAD_ENCODING:-plain}")
[ "${ENABLE:-no}" = "yes" ] && ARGS+=(--enabled)
python3 -I "$HERE/build_schedule.py" "${ARGS[@]}" > "$WORK/schedule.json" || { echo "ABORT: could not build the schedule"; exit 2; }

echo "account:  $ACCT"
echo "role:     $ROLE_ARN (boundary $BOUNDARY)"
echo "runtime:  $RUNTIME_ARN"
echo "--- role trust policy";       cat "$WORK/trust.json"
echo "--- role permissions policy"; cat "$WORK/policy.json"
echo "--- schedule request";        cat "$WORK/schedule.json"
if [ "${APPLY:-no}" != "yes" ]; then
  echo
  echo "DRY RUN: nothing was created. Review the above, then re-run with APPLY=yes."
  exit 0
fi

read -r AK SK ST < <(aws sts assume-role --role-arn "arn:aws:iam::$ACCT:role/viharbot-kepler-deployer" \
  --role-session-name create-schedule --duration-seconds 3600 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
[ -z "$AK" ] && { echo "ABORT: could not assume the deployer role"; exit 2; }
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST"
echo "running as: $(aws sts get-caller-identity --query Arn --output text)"

step() { local name="$1"; shift; echo "== $name"; if ! "$@"; then echo "STOPPED at: $name"; exit 1; fi; }

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "== role exists: keeping it (its policy is replaced below)"
else
  step "1. IAM role $ROLE_NAME" aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$WORK/trust.json" --permissions-boundary "$BOUNDARY" \
    --tags $TAGS_KV --query 'Role.Arn' --output text
fi
step "2. permissions: invoke this one runtime" aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name invoke-kepler-runtime --policy-document "file://$WORK/policy.json"
echo "== waiting 15 s for IAM to propagate"; sleep 15
SCHEDULE_NAME=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["Name"])' "$WORK/schedule.json")
if aws scheduler get-schedule --name "$SCHEDULE_NAME" --group-name default >/dev/null 2>&1; then
  # It already exists (for example the recurring schedule created DISABLED): this is how it is switched
  # on. UpdateSchedule replaces the whole definition with the one printed above, so the live schedule
  # ends up exactly as reviewed, with only the State changed when ENABLE=yes.
  step "3. schedule $SCHEDULE_NAME exists: update it to the request above" \
    aws scheduler update-schedule --cli-input-json "file://$WORK/schedule.json" --query 'ScheduleArn' --output text
else
  step "3. schedule ($MODE)" aws scheduler create-schedule --cli-input-json "file://$WORK/schedule.json" \
    --query 'ScheduleArn' --output text
fi
echo "Done. Record it in docs/aws-lab-change-record.md. For 'test', watch the runtime logs and the"
echo "ViharBot/Kepler PollCompleted metric a few minutes after the scheduled time."
