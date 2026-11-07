#!/bin/bash
# Creates the Kepler AgentCore Runtime from an image already pushed with push-image.sh.
#
#   IMAGE_TAG=<commit id> bash deploy/agentcore/kepler/create-runtime.sh               # a dry run
#   IMAGE_TAG=<commit id> APPLY=yes bash deploy/agentcore/kepler/create-runtime.sh     # create it
#
# A DRY RUN by default: it prints the exact request and changes nothing. Run from the repository
# root, signed in as the account administrator, only after the founder has approved this step.
#
# What APPLY=yes does, as the deployer role (credentials stay inside this process):
#   1. refuses if a runtime named viharbot_kepler already exists, or if the image tag is not in ECR
#   2. calls CreateAgentRuntime (the request is built by build_runtime.py: the immutable image tag,
#      the reviewed execution role viharbot-kepler-runtime, IAM authentication, PUBLIC network mode,
#      one environment variable, and the project tags applied at creation)
#   3. waits until the runtime is READY (or reports why it failed) and prints its ARN
# It does not invoke the runtime, create a schedule, or change any IAM. The runtime makes no call
# until something invokes it, and the schedule is a separate, later step (create-schedule.sh).
#
# Not verified in advance, and settled by running this: whether the deployer's tag-at-create
# permission works, whether AgentCore needs a service-linked role it must create, and whether the
# runtime role needs logs:CreateLogGroup. A failure at any of these changes nothing that matters
# (CreateAgentRuntime either succeeds or leaves nothing behind); the message is printed as is.
# Roll back with: aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id <id>
# Record the outcome in docs/aws-lab-change-record.md.
set -u
export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 AWS_PAGER=""
: "${IMAGE_TAG:?set IMAGE_TAG to the commit id of the image pushed with push-image.sh}"
HERE="deploy/agentcore/kepler"
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 2
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
python3 -I "$HERE/build_runtime.py" --account "$ACCT" --image-tag "$IMAGE_TAG" > "$WORK/runtime.json" \
  || { echo "ABORT: could not build the request"; exit 2; }

echo "account: $ACCT"
echo "--- create-agent-runtime request"; cat "$WORK/runtime.json"
if [ "${APPLY:-no}" != "yes" ]; then
  echo
  echo "DRY RUN: nothing was created. Review the above, then re-run with APPLY=yes."
  exit 0
fi

read -r AK SK ST < <(aws sts assume-role --role-arn "arn:aws:iam::$ACCT:role/viharbot-kepler-deployer" \
  --role-session-name create-runtime --duration-seconds 3600 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
[ -z "$AK" ] && { echo "ABORT: could not assume the deployer role"; exit 2; }
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST"
echo "running as: $(aws sts get-caller-identity --query Arn --output text)"

EXISTING=$(aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?agentRuntimeName=='viharbot_kepler'].agentRuntimeId" --output text) \
  || { echo "ABORT: could not list runtimes"; exit 1; }
[ -n "$EXISTING" ] && { echo "ABORT: a runtime named viharbot_kepler already exists ($EXISTING)"; exit 1; }
aws ecr describe-images --repository-name viharbot/kepler --image-ids imageTag="$IMAGE_TAG" >/dev/null 2>&1 \
  || { echo "ABORT: image tag $IMAGE_TAG is not in the viharbot/kepler repository"; exit 1; }

echo "== create the runtime"
aws bedrock-agentcore-control create-agent-runtime --cli-input-json "file://$WORK/runtime.json" \
  --query '[agentRuntimeArn,agentRuntimeId,status]' --output text > "$WORK/created.txt" 2> "$WORK/create.err" \
  || { echo "STOPPED at: create-agent-runtime"; cat "$WORK/create.err"; exit 1; }
cat "$WORK/created.txt"
read -r RT_ARN RT_ID _ < "$WORK/created.txt"

echo "== waiting for READY (up to 5 minutes)"
for _ in $(seq 1 30); do
  STATUS=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RT_ID" --query 'status' --output text 2>/dev/null)
  echo "   status: $STATUS"
  case "$STATUS" in
    READY) break;;
    CREATE_FAILED|UPDATE_FAILED|DELETING)
      aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RT_ID" --query 'failureReason' --output text
      echo "STOPPED: the runtime did not become ready"; exit 1;;
  esac
  sleep 10
done
[ "$STATUS" = "READY" ] || { echo "STOPPED: still $STATUS after 5 minutes (it may yet finish; check get-agent-runtime)"; exit 1; }
echo "runtime ARN: $RT_ARN"
echo "Done. Record it in docs/aws-lab-change-record.md. Nothing has invoked it yet."
