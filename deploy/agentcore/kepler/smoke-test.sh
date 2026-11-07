#!/bin/bash
# Smoke test of the Kepler steward image. Builds it for linux/arm64, runs it the way AgentCore
# would (no credentials, read-only filesystem, no capabilities, loopback only), and checks the
# behaviours that matter. It never contacts GitHub or AWS: the container has no credentials, so a
# poll fails closed, which is itself one of the checks. Needs Docker. Run from the repository root:
#   bash deploy/agentcore/kepler/smoke-test.sh
set -u
IMAGE="viharbot-kepler:smoke"
NAME="kepler-smoke-$$"
PORT="${KEPLER_SMOKE_PORT:-18080}"  # loopback only; set another port if 18080 is taken
TOPIC="arn:aws:sns:us-west-2:000000000000:viharbot-kepler-alerts"
WORK=$(mktemp -d)
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT
pass=0; fail=0
ok()   { echo "PASS  $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1 :: $2"; fail=$((fail+1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$3', got '$2'"; fi; }
http() {  # method path [json-body]  -> prints "<code>" and leaves the body in $WORK/body
  if [ -n "${3:-}" ]; then
    curl -s -o "$WORK/body" -w "%{http_code}" -X "$1" "http://127.0.0.1:$PORT$2" -H 'content-type: application/json' -d "$3"
  else
    curl -s -o "$WORK/body" -w "%{http_code}" -X "$1" "http://127.0.0.1:$PORT$2"
  fi
}

echo "== build (linux/arm64)"
docker buildx build --platform linux/arm64 -f Dockerfile.kepler -t "$IMAGE" --load . >"$WORK/build.log" 2>&1 \
  || { echo "BUILD FAILED"; tail -20 "$WORK/build.log"; exit 1; }
ok "image builds for arm64"
check "image architecture" "$(docker image inspect "$IMAGE" --format '{{.Architecture}}')" "arm64"
check "image runs as a non-root user" "$(docker image inspect "$IMAGE" --format '{{.Config.User}}')" "1001:1001"
echo "     size: $(docker image inspect "$IMAGE" --format '{{.Size}}') bytes"

echo "== a bad configuration must not start"
docker run --rm --platform linux/arm64 "$IMAGE" >"$WORK/badstart.log" 2>&1; code=$?
[ "$code" -ne 0 ] && ok "no alert topic configured: the container exits ($code)" || bad "bad config" "container started"
grep -q "KEPLER_ALERT_TOPIC_ARN" "$WORK/badstart.log" && ok "the error names the missing setting" || bad "error message" "$(tail -3 "$WORK/badstart.log")"
docker run --rm --platform linux/arm64 -e KEPLER_ALERT_TOPIC_ARN="$TOPIC" -e KEPLER_REPOSITORY=vchokshi/viharbot-onboarding-instructor "$IMAGE" >"$WORK/forbidden.log" 2>&1; code=$?
[ "$code" -ne 0 ] && ok "the instructor repository can never be configured" || bad "forbidden repo" "container started"
docker run --rm --platform linux/arm64 -e KEPLER_ALERT_TOPIC_ARN="$TOPIC" -e KEPLER_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0 "$IMAGE" >"$WORK/claude.log" 2>&1; code=$?
[ "$code" -ne 0 ] && grep -q "approved allowlist" "$WORK/claude.log" && ok "an Anthropic model id is refused at startup" || bad "anthropic model" "container started or no clear message"
docker run --rm --platform linux/arm64 -e KEPLER_ALERT_TOPIC_ARN="$TOPIC" -e KEPLER_MODEL_ID=meta.llama3-3-70b-instruct-v1:0 "$IMAGE" >"$WORK/other.log" 2>&1; code=$?
[ "$code" -ne 0 ] && ok "any model id other than the one allowlisted is refused at startup" || bad "other model" "container started"

echo "== run as AgentCore would: no credentials, read-only, no capabilities"
docker run -d --name "$NAME" --platform linux/arm64 -p "127.0.0.1:$PORT:8080" --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -e KEPLER_ALERT_TOPIC_ARN="$TOPIC" -e AWS_REGION=us-west-2 -e AWS_EC2_METADATA_DISABLED=true \
  "$IMAGE" >/dev/null || { echo "could not start"; exit 1; }
for _ in $(seq 1 40); do [ "$(http GET /ping)" = "200" ] && break; sleep 0.5; done
check "/ping answers" "$(http GET /ping)" "200"
check "/ping body" "$(cat "$WORK/body")" '{"status":"Healthy"}'
check "container runs as non-root" "$(docker exec "$NAME" id -u)" "1001"
check "no docs or schema routes" "$(http GET /docs)" "404"

echo "== input is strict"
check "unknown action is refused"      "$(http POST /invocations '{"action":"delete"}')" "422"
check "an extra field is refused"      "$(http POST /invocations '{"action":"poll","repo":"x/y"}')" "422"
check "poll with a PR number is refused" "$(http POST /invocations '{"action":"poll","pr":5}')" "422"
check "check without a PR is refused"  "$(http POST /invocations '{"action":"check"}')" "422"
check "a non-object body is refused"   "$(http POST /invocations '[1,2]')" "422"
big=$(python3 -c "print('{\"action\":\"poll\",\"x\":\"' + 'a'*20000 + '\"}')")
check "an oversized body is refused"   "$(http POST /invocations "$big")" "413"

echo "== with no credentials a poll fails closed and leaks nothing"
code=$(http POST /invocations '{"input":{"action":"poll"},"request_id":"smoke-1"}')
check "the poll reports failure (502)" "$code" "502"
body=$(cat "$WORK/body")
echo "$body" | grep -q '"status":"failed"' && ok "the body says failed" || bad "body" "$body"
echo "$body" | grep -qE '"error_code":"(audit|auth)"' && ok "the cause is a credential problem, as expected" || bad "error_code" "$body"
echo "$body" | grep -qiE "token|secret|BEGIN|arn:aws" && bad "leak" "$body" || ok "the response contains no secret, key or ARN"

echo "== the container is minimal and credential-free"
env_dump=$(docker inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}')
echo "$env_dump" | grep -qiE "AWS_ACCESS_KEY|AWS_SECRET|AWS_SESSION|TOKEN|PASSWORD|PRIVATE" && bad "environment" "credentials present" || ok "no credential in the container environment"
packages=$(docker exec "$NAME" python -m pip list 2>/dev/null | tr 'A-Z' 'a-z')
echo "$packages" | grep -qE "^(litellm|openai|anthropic|strands)" && bad "packages" "a model SDK is installed" || ok "no model SDK is installed (LiteLLM, OpenAI, Anthropic, Strands)"
docker exec "$NAME" sh -c 'touch /app/x 2>/dev/null' && bad "filesystem" "the root filesystem is writable" || ok "the filesystem is read-only"
docker exec "$NAME" sh -c 'ls /app/gate' | grep -q cla_gate.py && ok "the CLA gate is present" || bad "gate" "missing"
docker exec "$NAME" sh -c 'ls /app /app/src/viharbot' | grep -qE "^(\.env|docs|tests|\.git)$" && bad "contents" "unexpected files in the image" || ok "no .env, docs, tests or .git in the image"
logs=$(docker logs "$NAME" 2>&1)
echo "$logs" | grep -qiE "BEGIN .*PRIVATE|ghs_|AWS_SECRET" && bad "logs" "a secret appears in the logs" || ok "no secret in the container logs"
echo "$logs" | grep -q '"agent": "kepler"' && ok "logs are structured JSON with the agent field" || bad "logs" "no structured log line"

echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
