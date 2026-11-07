#!/bin/bash
# Builds the Kepler steward image for linux/arm64 and pushes it to the Kepler ECR repository.
#
#   IMAGE_TAG=<commit id> bash deploy/agentcore/kepler/push-image.sh                 # a dry run
#   IMAGE_TAG=<commit id> APPLY=yes bash deploy/agentcore/kepler/push-image.sh       # build and push
#
# A DRY RUN by default: it prints the plan and changes nothing (it does not even build). Run from the
# repository root, signed in as the account administrator, with Docker running, only after the
# founder has approved this step.
#
# IMAGE_TAG must be the commit id of the code being built (7 to 40 lowercase hex characters), not a
# label: the repository's tags are immutable, so a tag names exactly one image forever, and the
# runtime is created from that exact tag. The script refuses to build unless the code that goes into
# the image (everything outside deploy/, docs/ and tests/) is identical to that commit, with no
# uncommitted or untracked changes, so the image is exactly the reviewed commit.
#
# What APPLY=yes does, as the deployer role (credentials stay inside this process):
#   1. builds Dockerfile.kepler for linux/arm64 (the build fails on any other target)
#   2. logs Docker in to the account's ECR registry (a token valid for 12 hours), pushes the tag, and
#      logs Docker out again
#   3. reads back the pushed image's digest and size
# It never runs the image, never contacts GitHub, and never touches a secret. ECR scans the image on
# push (scan-on-push is set on the repository); the scan result needs ecr:DescribeImageScanFindings,
# which the deployer does not have, so read it as the administrator afterwards.
# Record the outcome in docs/aws-lab-change-record.md.
set -u
export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2 AWS_PAGER=""
: "${IMAGE_TAG:?set IMAGE_TAG to the commit id being built}"
case "$IMAGE_TAG" in *[!0-9a-f]*|"") echo "ABORT: IMAGE_TAG must be a commit id (lowercase hex)"; exit 2;; esac
[ "${#IMAGE_TAG}" -ge 7 ] && [ "${#IMAGE_TAG}" -le 40 ] || { echo "ABORT: IMAGE_TAG must be 7 to 40 characters"; exit 2; }
REPO="viharbot/kepler"
ACCT=$(aws sts get-caller-identity --query Account --output text) || exit 2
REGISTRY="$ACCT.dkr.ecr.us-west-2.amazonaws.com"
IMAGE="$REGISTRY/$REPO:$IMAGE_TAG"

echo "account:  $ACCT"
echo "image:    $IMAGE"
echo "platform: linux/arm64 (Dockerfile.kepler)"
# Provenance: the code that goes into the image must be exactly the code at IMAGE_TAG. Everything the
# build can copy lives outside deploy/, docs/ and tests/ (the allowlist is Dockerfile.kepler.dockerignore),
# so those three directories may differ (for example, the scripts in deploy/ themselves) and nothing
# else may: no commits since IMAGE_TAG that change it, no uncommitted or untracked changes.
FULL_ID=$(git rev-parse --verify "$IMAGE_TAG^{commit}" 2>/dev/null) || { echo "ABORT: $IMAGE_TAG is not a commit in this repository"; exit 2; }
git merge-base --is-ancestor "$FULL_ID" HEAD || { echo "ABORT: $IMAGE_TAG is not an ancestor of HEAD"; exit 2; }
IMAGE_PATHS=(. ':(exclude)deploy' ':(exclude)docs' ':(exclude)tests')
git diff --quiet "$FULL_ID" -- "${IMAGE_PATHS[@]}" || { echo "ABORT: the code differs from $IMAGE_TAG outside deploy/, docs/ and tests/"; exit 2; }
if [ -n "$(git status --porcelain -- "${IMAGE_PATHS[@]}" 2>/dev/null)" ]; then
  echo "ABORT: uncommitted or untracked changes outside deploy/, docs/ and tests/"; exit 2
fi
echo "source:   commit $FULL_ID (image inputs identical to HEAD)"
if [ "${APPLY:-no}" != "yes" ]; then
  echo
  echo "DRY RUN: nothing was built or pushed. Re-run with APPLY=yes to build and push."
  exit 0
fi

WORK=$(mktemp -d); trap 'docker logout "$REGISTRY" >/dev/null 2>&1; rm -rf "$WORK"' EXIT
read -r AK SK ST < <(aws sts assume-role --role-arn "arn:aws:iam::$ACCT:role/viharbot-kepler-deployer" \
  --role-session-name push-image --duration-seconds 3600 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
[ -z "$AK" ] && { echo "ABORT: could not assume the deployer role"; exit 2; }
unset AWS_PROFILE
export AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST"
echo "running as: $(aws sts get-caller-identity --query Arn --output text)"

step() { local name="$1"; shift; echo "== $name"; if ! "$@"; then echo "STOPPED at: $name"; exit 1; fi; }

if aws ecr describe-images --repository-name "$REPO" --image-ids imageTag="$IMAGE_TAG" >/dev/null 2>&1; then
  echo "ABORT: $IMAGE_TAG already exists in $REPO (tags are immutable); nothing to push"; exit 1
fi
step "1. build (linux/arm64)" docker buildx build --platform linux/arm64 -f Dockerfile.kepler -t "$IMAGE" --load .
step "   confirm the architecture" test "$(docker image inspect "$IMAGE" --format '{{.Architecture}}')" = "arm64"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >"$WORK/login.log" 2>&1 \
  || { echo "STOPPED at: docker login"; tail -3 "$WORK/login.log"; exit 1; }
echo "== 2. docker login ok"
step "   push $IMAGE" docker push "$IMAGE"
step "3. read back" aws ecr describe-images --repository-name "$REPO" --image-ids imageTag="$IMAGE_TAG" \
  --query 'imageDetails[0].[imageDigest,imageSizeInBytes,imagePushedAt]' --output text
echo "Done. Read the scan result as the administrator, then record it in docs/aws-lab-change-record.md."
