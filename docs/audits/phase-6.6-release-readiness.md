# Phase 6.6 release readiness (DRAFT)

**Status: OPEN / NOT SIGNED OFF.** Draft for founder review. It records what has been verified, on which commit, and what has not. Nothing here approves a release or a deployment. The live smoke test has now been run (section 3a); a real-browser run, gateway confirmation of the cost, and the owner items in section 6 remain.

* Release tracker: issue #23. Draft written 2026-11-06 against `main` at `57b64c7`; live smoke results added the same day.
* Evidence is kept in three separate categories, as the tracker asks: **implemented** (code and docs merged), **tested** (offline tests and CI, plus the local live smoke test), **deployed** (a running environment). **Nothing from Phase 6.6 has been deployed.** The live smoke test ran on a local, isolated Docker Compose stack, which is a development environment, not staging.

## 1. Verdict

The offline baseline is in place and green on one commit: the blocking defects (#19, #20, #21, #22) and the streaming-contract conformance work (#53) are merged, evidenced and closed, and the offline suite, Docker suite, security scans and CodeQL all pass on `main`. The owner approved a live smoke test (3 requests, $5 ceiling), and it ran on 2026-11-06 (section 3a): 2 successful full runs and 1 deliberate failure, each ending in exactly one terminal event in strict sequence, with a latency baseline for all of them. What keeps this from being signed off:

1. **Cost is estimated, not measured.** The supplied key can call only LLM routes, so its spend could not be read. The estimate is far below the ceiling, but the owner should confirm actual spend in the gateway.
2. **The browser SSE timeline was driven by a scripted client, not a real browser** (none was available). The page is tested only in a Node fake DOM.
3. **Open findings from the live run** (section 5): an alias that does not exist for this key, a planner that repeated a call three times, and a thin timeout margin on Sage.
4. **Owner decisions** in section 6, and closing #23 and the milestone, which is the owner's call. The milestone assignment is done.

## 2. Candidate commit and evidence

Candidate: `57b64c785bf22f993477afb86951392aab5750c7` (`main` after PR #208). All four workflows for this exact commit completed successfully.

| Check | Result | Evidence |
|---|---|---|
| CI (Python 3.12, Python 3.13, Frontend Node 24, Lint, Docker build and smoke test, `CI result` gate) | success | https://github.com/vchokshi/viharbot/actions/runs/42284376429 |
| Offline suite | **2849 passed, 3 skipped** on 3.12, on 3.13, and inside the Docker image (0 failed, 0 errors) | same run, job logs |
| Docker profile (`docker compose --profile tests run`) | executed in CI with no network and no secrets; only the opt-in live checks skipped | same run, "Offline suite in Docker" |
| Security Scan (Gitleaks, Bandit, pip-audit, Trivy) | success | https://github.com/vchokshi/viharbot/actions/runs/42284376383 |
| CodeQL | success | https://github.com/vchokshi/viharbot/actions/runs/42284376499 |
| OpenSSF Scorecard | success | https://github.com/vchokshi/viharbot/actions/runs/42284376474 |
| Coverage badge data | published by the `main` run | same CI run |

The 3 skipped tests are the opt-in live LiteLLM checks (`RUN_LITELLM_INTEGRATION`, `RUN_LITELLM_INFERENCE`). They are off by design because they make billable calls.

**Branch protection.** The active `protect-main` ruleset forbids deletion and non-fast-forward updates, requires a pull request, and requires the checks `CI result`, `Contributor registry`, `Dependency review` and `CLA acceptance`. Checked from the ruleset definition; badges were not separately verified beyond the coverage-badge job succeeding.

## 3. Tracker gate, item by item

| Gate item (from #23) | Status | Notes |
|---|---|---|
| Fix #19 to #22 (Docker image, Node guard, `.env` isolation, SSE terminal outcome) | **Done** | Section 4 |
| Canonical SSE contract fixtures and conformance (#53) | **Done** | Section 4 |
| Clean checkout tests on Python 3.12/3.13 and Node 24 | **Done on CI** | Run above. Local runs used Python 3.13 only |
| Docker offline suite executed, not only built | **Done** | CI executes it; no network, no secrets |
| CI, security scans, required checks | **Done** for the candidate commit | Section 2 |
| Badges | Partially verified | Coverage badge data job succeeded; badge rendering not checked |
| SSE fault-injection traces | **Done** as tests | `tests/test_live_terminal_faults.py`; no live traces captured |
| Browser distinguishes completed, failed, interrupted | **Done** in the Node fake DOM | **No real browser has been run** |
| `POST /api/chat` backward compatibility | **Done** | Unchanged; covered by the suite |
| Live six-service workflow and browser SSE timeline (opt-in, billable) | **Done, with caveats** | 3 runs on 2026-11-06 (section 3a). Driven by a scripted SSE client; **no real browser** |
| Latency, timeout, error and inference-cost baseline | **Latency, timeout and error: done. Cost: estimated only** | Section 3a. The key could not read its own spend |
| Record tested commit, limitations, rollback | This document | Needs owner review |
| Milestone `6.6` assignment and closure | **Assigned; not closed** | #19 to #23 and #53 are on milestone `6.6` (5 closed, #23 open). The milestone closes last, by the owner |
| Staging verification | **Not applicable** | Nothing was deployed. `deploy_on_push` stays disabled |

## 3a. Live smoke test, 2026-11-06

**Approval and limits.** Approved by the owner: 3 requests, a $5 ceiling, one supplied LiteLLM key. The key was used only as an environment variable on individual commands; it was never written to a file, log, commit or pull request. It appeared in the working session, so **rotate it**.

**Setup.** The candidate code (`main`, no local `.env`) on a separate Compose project (`smoke66`) with its own port (127.0.0.1:18080) and network, because Compose fixes the agent network name and a second stack on the default name would share service names with any other running stack. All nine containers were healthy (web, Research, Security, Fact Check, Source Quality, Privacy Review, Synthesis, Sage, Pixel). Slide generation was disabled. The stack was torn down afterwards (containers, volumes, network and local images removed); no other stack was touched.

**One deviation from defaults.** `PRIVACY_MODEL` and `SYNTHESIS_MODEL` were set to `openal/gpt-5.4-mini`, because the Compose default `gpt-5.4-mini` is not one of the five aliases this key can reach. Every other alias was the default (`gpt-6-luna` planner, `gpt-6.1-sol` Research and Sage, `openai/gpt-5.4` Security, `gemini/gemini-3.5-flash` Fact Check and Source Quality).

| Run | Request | Outcome | Total | First byte | Events | Planner calls (median) | Agent calls |
|---|---|---|---|---|---|---|---|
| 1 | full-flow question about AI reading employee email | `orchestration_completed` (Charlie finished) | 136.9 s | 44 ms | 38 | 6 (3.2 s) | 5: Research 26.4 s, Security 17.9 s, Privacy Review 3.1 s, **Sage 61.0 s**, Synthesis 4.9 s |
| 2 | one-line question about SQL injection | `orchestration_completed` | 42.6 s | 13 ms | 29 | 5 (2.7 s) | 4: Research 8.5 s, 6.8 s and 2.8 s (three times), Security 10.9 s |
| 3 | run 1's question with the Security container stopped | `orchestration_failed` (`agent_execution_failed`) | 29.7 s | 15 ms | 13 | 2 (2.3 s) | Research 25.0 s, Security failed in 0.1 s |

**What the live run confirms.**
* Every stream had **exactly one terminal event, in strict sequence 1..N with nothing after it**; the failure run had **no `final_response`**.
* Run 1's `final_response` carried `privacy_review`, `sage` and `synthesis`; each `agent_activity` entry reported its real model alias.
* Keep-alive comments arrive during long silences (6 during run 1, whose longest gap was Sage's 61 s) and are not events.
* The failure path behaves as designed against real infrastructure: `agent_failed` then one sanitized `orchestration_failed`.
* **Logs are clean.** Across all nine services: the question text appears in 0 log lines, the key in 0, `Bearer` header values in 0. There were 2 error-level records, both from the deliberate failure in run 3, each carrying only the destination agent, duration and error type (`URLError`).

**Latency baseline (3 requests; not statistically meaningful).** Time to first byte 13 to 44 ms. Planner call 2.3 to 6.5 s (median 2.7 s over 13 calls). Agent call 0.1 to 61 s, the longest being Sage. A full multi-agent run took 137 s, about 57 percent of the default 240 s request budget. No timeout, deadline expiry, planner failure or rejected delegation occurred.

**Cost estimate (not measured).** The key can call only LLM routes, so the gateway would not report its spend. Only Sage logs token counts: 3,403 input and 2,624 output tokens, about $0.03 at its listed price. If every one of the 11 agent calls had used three times that volume at the most expensive listed price, the total would still be under $2; the 13 planner calls use the cheapest model and add a few cents at most. The realistic figure is probably well under $1 for all three runs, against the $5 ceiling. **Confirm in the gateway's usage view.**

## 4. Child issues

| Issue | Pull request | Merge commit | Post-merge CI | State |
|---|---|---|---|---|
| #19 Docker test image | #192 | `65d0cd0` | https://github.com/vchokshi/viharbot/actions/runs/41936449622 | closed |
| #20 Node guard | #192 | `65d0cd0` | same | closed |
| #21 `.env` isolation | #194 | `75909e0` | https://github.com/vchokshi/viharbot/actions/runs/41938530530 | closed |
| #22 SSE terminal outcome | #196 | `c771137` | https://github.com/vchokshi/viharbot/actions/runs/41941592119 | closed |
| #53 SSE contract fixtures | #199 | `950d968` | https://github.com/vchokshi/viharbot/actions/runs/42112279136 | closed |

What each changed, in one line: #19 the test image now runs the whole offline suite (Node 24, `git`, an allowlist build context, no secrets, no runtime network); #20 restored `@needs_node` and added a regression check for every Node-reaching test; #21 stops LiteLLM's import-time `.env` load during tests; #22 makes "answer sent but completion could not be" an explicit, logged, tested protocol-failure outcome and reserves `cancelled`; #53 regenerates and compares every documented stream against the real loop, and adds a complete full-team fixture. No production Dockerfile, schema or wire format changed in any of them.

## 5. Known limitations

* **Fixtures are not live captures.** The documented streams are regenerated from the real orchestration loop with mocked inference. They show what the code emits, not what a live deployment with live models emits.
* **The browser path is tested in a Node fake DOM, not a real browser.**
* **Default execution budget (6) cannot cover all seven specialists.** A run that delegates to every specialist ends under `policy_budget` with the last delegation rejected. Whether six is the right default is an open product decision.
* **No `403`.** Authentication is HTTP Basic: `401` for bad or missing credentials, `503` when web authentication is not configured.
* **Disconnects do not guarantee terminal delivery.** A cancelled or disconnected run emits no terminal event, and in-flight remote inference is not interrupted. After the answer is sent, a failure to send the completion ends the stream without a terminal event (the page shows it as interrupted and the server logs it at error level). The page ignores frames after the first valid terminal event.
* **The `cancelled` failure code is declared but reserved**, and the emitter refuses to send it. Deleting it would change the wire contract and belongs with contract work.
* **Test-image size.** The Docker test image grew about 314 MB (measured on arm64; CI is amd64). It is never deployed.
* **Docker job scope.** On a pull request the Docker job runs only when a container definition or `src/` changes, so a tests-only change gets its Docker run on the next push to `main`.
* **Test image runs as root** (a known Trivy `DS-0002` finding, documented earlier and not part of this phase).
* **The `.env` fix is test-only.** In Compose, the `web` service still takes `.env` values by design. That is a production-facing question, not addressed.
* **Test fragility.** Four tests in `tests/test_pipeline_state.py` failed under full-suite ordering when a real `.env` leaked in. The leak is closed, but the order sensitivity itself was not investigated.
* **Alias mismatch found live (needs a decision).** The Compose default `gpt-5.4-mini` for `PRIVACY_MODEL` and `SYNTHESIS_MODEL` is not among the five aliases the smoke-test key can reach, which lists `openal/gpt-5.4-mini` (note the spelling) instead. With this key the defaults would fail. It is not known whether the alias exists on the gateway for the application's own key, or whether `openal` is a typo in the gateway's configuration; either way the Compose default is unverified against live inference.
* **The planner repeated Research three times for a one-line question (run 2).** This is within the repeat limit and ended correctly, but it tripled the Research cost and latency for a trivial prompt. A planner-behaviour observation, not a contract defect.
* **Sage is the long pole and the margin is thin.** Sage took 61 s in run 1; its default timeout is 80 s (`SAGE_MODEL_TIMEOUT_SECONDS`), so a response about a third slower would fail the agent and the run. Three runs cannot say how often that happens.
* **Concurrent streams, deadline expiry, planner failure and timeouts were not exercised live.** Only a normal full run, a short run and one agent failure were.
* **Two local stacks share service names by default.** Compose gives the agent network the fixed name `viharbot-agent-network`, so a second stack started from another checkout joins it, and names such as `research` can resolve to either stack's containers. The smoke test avoided this with an override; the default is a hazard for anyone running two stacks.
* **PR #208 and other merges after #199 are in the candidate commit** and are outside Phase 6.6's scope; this draft makes no claim about them beyond CI passing.

## 6. Items needing the owner

1. **Possible past billable call (verify).** Before #21 was fixed, host test runs on a machine with a local `.env` loaded a real planner key, and the live planner path logged `orchestration planner call failed`. It could not be determined whether a request actually reached the gateway. Checking gateway usage for the affected period would settle it.
2. **Confirm the smoke-test spend.** The live test ran (section 3a) but its cost is an estimate; read the actual spend for the key in the gateway's usage view and record it here.
3. **Rotate the smoke-test key.** It was supplied in the working session. It was never written to a file, log, commit or pull request (checked), but it is in the session transcript.
4. **Alias decision.** Settle whether `gpt-5.4-mini` (the Compose default) or `openal/gpt-5.4-mini` is correct for Privacy Review and Synthesis, and fix whichever is wrong.
5. **A real-browser run.** The SSE timeline was driven by a scripted client. A person (or a browser tool) watching the live page for one run would close that gap.
6. **Milestone.** Done: #19 to #23 and #53 are on milestone `6.6`. Close the milestone, and #23, when you judge the gate met.
7. **Exceptions.** None are approved. If any gate item above is deferred past this release (for example the real-browser run), record it here as an explicit, owner-approved exception.
8. **Decisions on the budget default** (finding above) and on whether to retire the reserved `cancelled` code.

## 7. Rollback

Every Phase 6.6 change is a merged pull request and can be reverted independently with `git revert -m 1 <merge-commit>`: `65d0cd0`, `75909e0`, `c771137`, `950d968`. They touch only tests, fixtures, docs, CI configuration and, for #22 only, the error handling and logging in `src/viharbot/live_stream.py` (no schema, no page, no wire change). Nothing was deployed, so rollback needs no redeploy, no data step and no infrastructure change. A push to `main` does not deploy.

## 8. Not verified

Actual inference spend (estimated only); a real browser; a proxy in front of the stream; concurrent live streams; deadline expiry, planner failure and timeouts under live inference; slide generation (disabled for the smoke test); DigitalOcean staging or AWS (neither was deployed or touched); Python 3.12 on a developer machine (covered by CI only); badge rendering; the effect of the merges after #199.

*Sources: pull requests #192, #194, #196, #199 and their descriptions; the CI runs linked above; `docs/live-observability.md`; `docs/continuous-integration.md`; `docs/development-status.md`.*
