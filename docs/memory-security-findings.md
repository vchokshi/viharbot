# Memory service security findings (issue #40)

Scope: the governed memory service (ADR-009), its client in Charlie, and the proposed Stage memory window (`docs/stage-memory-design.md`, `docs/stage-memory-runbook.md`). Authority: the code on `main` at `a84f203`; ADR-006, `docs/service-authentication.md` and `src/viharbot/execution_config/memory_policy.toml` were read as the design intent and checked against the code. This is a test-and-review report. It changes no production code, deploys nothing, and calls no model, gateway or cloud API.

How to read the matrix: **PASS** means the test or check was executed in this review and passed (offline suite: 4085 passed, 92 skipped; 123 of those are the new tests on main alone; a 124th covers the bootstrap job once it is merged). **NOT RUN** means it was not executed here, with the reason. Nothing is marked PASS on the strength of a document alone. Test names are in `tests/test_memory_security_matrix.py` (M), `tests/test_memory_security_transport.py` (T) and `tests/test_memory_security_logs.py` (L); "suite" means an existing test that also ran in the full offline run.

Run note for contributors: in a git worktree the editable install points at the main checkout, so run `PYTHONPATH=src python -m pytest` to test the worktree's own code.

## Recommendation

**GO, conditional, for a Stage memory window.** No critical or high finding. Authentication fails closed, namespaces are isolated, no secret or content reached any log on any exercised path, and memory unavailability does not weaken authentication. The conditions below are operational, none needs a code change:

1. The Stage app spec (Agent B's change) gives `memory` no public route (no `http_port`, no ingress rule), one instance, `deploy_on_push: false`. NOT RUN here: the spec on `main` has no memory component yet.
2. Before pasting the registry variable, decode it and confirm it holds exactly one subject, `charlie`, with only `memory.read` and `memory.write`. The policy file also names nova, scout, cipher, pixel and sage with full rights (erase included); the only thing that keeps them out on Stage is that no registry entry exists for them (M: `test_a_valid_key_that_the_policy_does_not_name_gets_one_generic_403` shows the second gate works; the first gate is operational).
3. Run the trusted-source denial check from the design (an untrusted client is refused) in the window. It is UNTESTED on DigitalOcean (finding F-5). If it fails, treat the database credentials as exposed to anything inside the VPC and stop.
4. Fresh cluster, fresh certificate, fresh Charlie key and registry per window; none reused anywhere else; certificate validity at least the window plus a margin (finding F-3).
5. Console access to the Stage app limited to the founder for the window (finding F-1).
6. The AGENTS.md promotion question is asked and answered for that specific build before anything is deployed.

**NO-GO triggers:** the registry names any subject but `charlie`; the memory component is reachable from outside the app; the trusted-source denial check fails and the founder does not accept that; a log paste (checked by the founder first) shows a token, key, CA text, database URL or memory content; the full offline suite is not green at the commit being promoted.

## Matrix

### Charlie's signed assertions and the key registry

| # | Check | Evidence | Status |
|---|---|---|---|
| 1 | Positive control: Charlie writes and reads only its own scope | M `test_positive_control_charlie_can_write_and_read_only_its_own_scope` | PASS |
| 2 | A key cannot claim another subject | M `test_a_service_key_cannot_claim_another_subject`, `test_a_key_registered_for_one_subject_cannot_speak_for_another`; suite `test_a_forged_sub_is_refused` | PASS |
| 3 | Missing, empty, malformed token is 401 and never reaches storage | M `test_a_missing_empty_or_garbage_assertion_is_401_and_never_reaches_storage` | PASS |
| 4 | `alg=none`, HS*, RS/ES, HMAC-with-public-key confusion refused | M `test_alg_none_and_hmac_confusion_are_refused` | PASS |
| 5 | `jwk`, `jku`, `x5u`, `x5c` never select a key | M `test_embedded_key_headers_are_ignored_never_fetched_or_trusted` | PASS |
| 6 | Wrong `typ` (grant token) and wrong audience refused | M `test_a_grant_type_token_and_a_foreign_audience_are_refused` | PASS |
| 7 | Expired and future-dated tokens refused | M `test_an_expired_or_future_dated_token_is_refused` | PASS |
| 8 | Unknown `kid` refused; no fallback to another key | M `test_rotation_overlap_retirement_and_unknown_kid`; suite `test_an_unknown_kid_is_refused` | PASS |
| 9 | Rotation overlap works; a token signed by the new key under the old `kid` fails | M `test_rotation_overlap_retirement_and_unknown_kid` | PASS |
| 10 | Retired `kid` stops working within the 60 s refresh | M `test_rotation_overlap_retirement_and_unknown_kid` | PASS |
| 11 | Registry entry can narrow a subject below the route capability | M `test_the_registry_entry_can_narrow_a_subject_below_the_route_capability` | PASS |
| 12 | Unreadable registry: last-known for 15 min, then 503 for everyone, then recovers | M `test_a_registry_that_cannot_be_refreshed_keeps_last_known_for_15_minutes_then_fails_closed` | PASS |
| 13 | A corrupt or empty refresh never replaces good keys and never means "allow all" | M `test_a_corrupt_registry_refresh_is_not_half_applied` | PASS |
| 14 | Route capability enforced both ways; foreign capabilities refused | M `test_the_route_capability_is_enforced_in_both_directions` | PASS |

### Replay and request binding

| # | Check | Evidence | Status |
|---|---|---|---|
| 15 | A captured write token works once; the replayed write does not run | M `test_a_captured_token_works_once_and_a_replay_is_refused` | PASS |
| 16 | Same for a read token | M `test_a_replayed_read_token_is_refused_too` | PASS |
| 17 | Token bound to method, path, query, body | M `test_a_token_is_bound_to_method_path_query_and_body` | PASS |
| 18 | Query order and duplicates are part of the signature | M `test_query_parameter_order_and_duplicates_are_part_of_the_signature` | PASS |
| 19 | Replay cache is per process (restart forgets it), binding still holds | M `test_the_replay_cache_is_per_process_so_a_restart_forgets_it` (records limitation F-4) | PASS |
| 20 | Replay across several replicas | Single instance by design; shared cache is ADR-006 open question 5 | NOT RUN |

### Unauthorized principals

| # | Check | Evidence | Status |
|---|---|---|---|
| 21 | A valid key the policy does not name gets one generic 403 everywhere | M `test_a_valid_key_that_the_policy_does_not_name_gets_one_generic_403` | PASS |
| 22 | Look-alike subjects (case, padding, zero-width) do not match the policy | M `test_a_subject_that_differs_only_in_case_or_padding_is_not_a_policy_match` | PASS |
| 23 | `/health` is the only open route; docs and schema routes are off | M `test_health_is_the_only_open_route_and_reveals_nothing_sensitive` | PASS |
| 24 | Refusal bodies are short, fixed, never echo input | M `test_every_refusal_has_a_short_fixed_body_that_never_echoes_input`; suite `test_error_bodies_never_echo_the_request` | PASS |

### Namespaces, cross-agent access, escalation

| # | Check | Evidence | Status |
|---|---|---|---|
| 25 | No agent reads or writes another agent's scope (list, get, create, update, classify, retire, erase) | M `test_no_agent_can_read_or_write_another_agents_scope` (6 pairs, 7 operations) | PASS |
| 26 | A record is invisible to other agents through every door | M `test_a_record_written_by_one_agent_is_invisible_to_another_through_every_door` | PASS |
| 27 | Charlie has no session tier; `eng` and `inst` tiers are reserved and not probeable without a token | M `test_session_tier_is_not_reachable_by_charlie_and_reserved_tiers_are_never_served` | PASS |
| 28 | Path tricks (dot segments, encodings, empty segments, case, NUL, backslash) never change the acted-on scope, even when validly signed | M `test_path_tricks_never_change_which_scope_is_acted_on` (11 cases) | PASS |
| 29 | Scope or identity in a query refused | M `test_scope_or_identity_in_a_query_never_changes_the_outcome` (20 cases) | PASS |
| 30 | Identity, scope, lifecycle fields in a body (top level and in provenance) refused whole | M `test_identity_scope_and_lifecycle_fields_in_a_body_are_refused_whole` (15 fields) | PASS |
| 31 | Identity headers and method overrides ignored | M `test_identity_headers_and_method_overrides_are_ignored` | PASS |
| 32 | PUT, PATCH, DELETE and action spoofing have no route | M `test_unsupported_methods_and_action_spoofing_are_refused_after_authentication` | PASS |
| 33 | A body on a read or a query on a write refused | M `test_a_body_on_a_read_or_a_query_on_a_write_is_refused` | PASS |
| 34 | Policy limits (ceiling, personal data, size, retention, ttl) cannot be exceeded by the caller; nothing stored | M `test_policy_limits_cannot_be_exceeded_by_the_caller`, `test_classification_cannot_be_lowered_or_raised_past_the_ceiling_through_classify` | PASS |

### Transport, pinned certificate, Stage launcher

| # | Check | Evidence | Status |
|---|---|---|---|
| 35 | Expired pinned certificate fails closed, nothing sent | T `test_an_expired_pinned_certificate_fails_closed_and_nothing_is_sent` | PASS |
| 36 | Not-yet-valid certificate fails closed | T `test_a_not_yet_valid_pinned_certificate_fails_closed` | PASS |
| 37 | Stale pin after server rotation fails closed; new pin works | T `test_rotation_requires_both_sides_to_change_and_a_stale_pin_fails_closed` | PASS |
| 38 | No rotation overlap: two certificates in the pin are refused | T `test_there_is_no_overlap_window_for_rotation_one_pin_only` (records F-3) | PASS |
| 39 | An already-expired pin is accepted at start, fails only on use | T `test_a_pin_that_is_already_expired_is_accepted_at_start_and_only_fails_when_used` (records F-3) | PASS |
| 40 | Pinned cert is the only trust anchor; hostname check and TLS 1.2 floor on | T `test_the_system_trust_store_is_never_consulted_when_a_certificate_is_pinned`; suite pinned-cert tests | PASS |
| 41 | Plain HTTP and URL credentials never start the client | T `test_plain_http_or_credentials_in_the_url_never_start_the_client` (8 URLs) | PASS |
| 42 | Redirects not followed | T `test_redirects_are_never_followed_and_carry_no_token_elsewhere` | PASS |
| 43 | Stage launcher warns, does not refuse, on an expired certificate | T `test_the_stage_launcher_starts_with_an_expired_certificate_but_says_so_loudly` (records F-3) | PASS |
| 44 | Launcher refuses mismatched key, missing certificate, key or registry | T `test_the_stage_launcher_refuses_a_key_that_does_not_match_the_certificate`, `test_the_stage_launcher_never_serves_plaintext_or_without_a_registry` | PASS |
| 45 | Stage database connection forced to `verify-full`; remote host without CA refused | T `test_the_database_connection_is_forced_to_verify_full_in_stage_mode` | PASS |
| 46 | Real TLS handshake against DigitalOcean's internal network and the cluster CA | Needs a live cluster; no provider contact allowed | NOT RUN |

### Fail-open availability, fail-closed authentication

| # | Check | Evidence | Status |
|---|---|---|---|
| 47 | Memory unreachable: Charlie neither reads nor writes, and does not fail | T `test_an_unreachable_memory_service_never_stops_charlie_reading_or_writing_around_it` | PASS |
| 48 | A 401 or 403 is swallowed for availability, not retried, not downgraded | T `test_a_denied_call_is_swallowed_for_availability_but_is_not_retried_or_downgraded` | PASS |
| 49 | Signing off: no request is made, no unsigned fallback | T `test_with_signing_off_no_request_is_ever_made_there_is_no_unsigned_fallback` | PASS |
| 50 | Client refuses any subject but `charlie` | T `test_the_client_refuses_to_run_as_any_subject_but_charlie` | PASS |
| 51 | Database outage is 503 for the authenticated and never opens authentication | T `test_a_database_outage_is_a_503_for_the_authenticated_and_never_opens_authentication` | PASS |
| 52 | Memory service has no unauthenticated mode | T `test_the_memory_service_has_no_unauthenticated_mode`; suite `test_the_service_refuses_to_start_without_a_key_registry` | PASS |
| 53 | End to end over loopback: unregistered Charlie key, Charlie answers 200, nothing stored | T `test_charlie_answers_normally_when_memory_rejects_its_key_end_to_end` | PASS |
| 54 | End to end control: registered key stores exactly one record owned by `charlie` | T `test_charlie_with_a_registered_key_does_store_and_the_control_works` | PASS |

### Logs: content, credentials, keys, CA text, database URLs

| # | Check | Evidence | Status |
|---|---|---|---|
| 55 | Service success and 11 error paths (422, oversize, ceiling, cross-agent, unnamed, bad query, bad JSON, bad id, garbage token, forged sub, replay): no content, token, signature or signing seed in any record, attribute or header dump | L `test_service_logs_carry_no_content_token_or_signing_key_on_any_path` | PASS |
| 56 | Repository exception whose message holds content and a database URL: 500, class name only | L `test_a_repository_that_raises_with_content_in_its_message_is_never_echoed_or_logged` | PASS |
| 57 | Store error whose code (wrongly) carries content: not echoed, not logged | L `test_a_store_error_whose_code_carries_content_is_not_logged_or_echoed` | PASS |
| 58 | Client and Charlie, 6 server behaviours (ok, bad shape, not JSON, 500, 403, 307) and a URL with credentials: nothing remote, no sent token, no URL secret logged | L `test_client_and_charlie_logs_carry_no_content_token_or_url_secret` | PASS |
| 59 | What Charlie stores holds only the five enumerated keys, never the question or response text | L `test_what_charlie_stores_never_contains_the_question_or_any_response_text` | PASS |
| 60 | Launcher output, including refusals: no private key, certificate, CA text, registry, password, `postgresql://`, `sslrootcert`, host | L `test_launcher_output_has_no_private_key_ca_text_registry_or_database_url` | PASS |
| 61 | A key pasted into the CA variable, and the reverse, is not echoed | L `test_a_ca_or_key_pasted_into_the_wrong_variable_is_not_echoed` | PASS |
| 62 | Static scan of every logging call on the memory path: constant messages, no `exc_info` or traceback, no content-like `extra` keys; `print` calls limited to fixed text, class names or JSON | L `test_logging_calls_use_constant_messages_and_never_exception_text_or_tracebacks`, `test_print_calls_in_the_memory_path_are_fixed_text_class_names_or_json_only`, `test_the_only_exception_text_that_is_logged_is_policy_config_text_built_from_code_constants` | PASS |
| 63 | Authentication log reflects a caller-chosen path (bounded, query cut) | L `test_the_authentication_log_reflects_a_caller_chosen_path_but_bounded_and_without_the_query` (records F-6) | PASS |
| 64 | Stage bootstrap job output (only step, outcome, SQLSTATE, fixed flags, exception class; no password, host or driver text, four runs including a driver-style exception) | L `test_the_stage_bootstrap_job_prints_only_step_outcome_sqlstate_and_fixed_fields` (skips until `stage_bootstrap.py` exists; run and passing with it merged) | PASS |
| 64b | uvicorn's own access log on the Stage launcher's server | Not exercised: tests use the in-process client, not uvicorn's access logger | NOT RUN |

### Database and deployment (review from documents only)

| # | Check | Evidence | Status |
|---|---|---|---|
| 65 | PostgreSQL contract suite (roles, grants, isolation) against a real database | 82 of 83 tests skip without `VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN`; the runbook runs them on the window's cluster | NOT RUN |
| 66 | Trusted-source rule denies an untrusted client | Documented UNTESTED (`docs/stage-memory-design.md`, section 2.4 and the drill) | NOT RUN |
| 67 | Memory component has no public route, one instance, no deploy on push | `deploy/digitalocean/app.yaml` on `main` has no memory component yet (Agent B's change) | NOT RUN |
| 68 | Stage registry holds only `charlie` | The generator (`scripts/stage_memory_materials.py`) hard-codes subject `charlie`; the window's actual variable must be decoded and checked (condition 2) | NOT RUN |
| 69 | Stage key rejected in production | No production registry exists; unknown-`kid` refusal (row 8) is the mechanism | NOT RUN |

## Findings, by severity

No critical or high findings. No defect was found in code that needed a patch, so none was made. Nothing here expands IAM scope.

**MEDIUM**

- **F-1. Stage secrets are readable by anyone with console access.** Encrypted App Platform variables are visible to console users (ADR-006 accepts this for a lab; design section 3). Whoever reads Charlie's signing seed can read and write Charlie's agent-private scope (five metadata fields) until the `kid` is removed; the TLS key allows impersonating `memory` to Charlie; the database passwords reach the cluster. Mitigation: window-scoped keys, restricted console access, regenerate per window (conditions 4 and 5).
- **F-2. Emergency key retirement on Stage means a redeploy.** The registry reaches the container from one variable, written to a file once at launch. The 60 second refresh re-reads that same file, so ADR-006's propagation bound is not delivered on Stage: removing a `kid` takes effect only when `memory` is redeployed. Acceptable for a short window with one subject; do not rely on the 60 second figure in the runbook.

**LOW**

- **F-3. Certificate expiry and rotation are blunt.** One pinned self-signed certificate, no overlap (rows 38, 39, 43), the launcher warns but starts with an expired certificate, and Charlie accepts an already-expired pin at start and logs `enabled`. The failure is safe (Charlie fails closed on TLS, nothing is sent, row 35) but quiet: memory is dead while `/health` is green. Mitigation: window shorter than certificate validity; check Charlie's `memory client` log lines at the start of the window.
- **F-4. Replay cache is per process.** A restart or a second instance during a rolling deploy forgets consumed tokens for at most 70 seconds (60 lifetime plus 10 skew); binding to method, target and body still applies (row 19). TLS limits capture. The design's `instance_count: 1` is therefore a security control, not only a cost choice. Do not scale `memory` without a shared cache (ADR-006 open question 5).
- **F-5. Database trusted-source denial is unverified.** The design records it as UNTESTED. Authentication to the database still needs the password and TLS `verify-full` (row 45), but if the rule does not deny, the password is the only barrier inside the VPC. The window's drill covers it (condition 3).
- **F-6. The authentication log reflects the caller's raw path.** Up to 200 characters, query removed, JSON-escaped (row 63). It is not memory content (content never travels in a path), but it is attacker-chosen text in a log. A fix would log only the parsed scope label after path validation; not made, because `service_auth.py` is shared by four other services.

**INFO**

- **F-7. Authorization is by subject only (A2).** `eng` and `cls` are required but not enforced; ADR-006 A3 (#38) is not implemented. Isolation rests on the policy mapping each `sub` to one agent and on the registry listing only `charlie` on Stage.
- **F-8. IAM issues #338 to #350.** None is implemented and none is a dependency of a memory window. Memory has no human-facing endpoint and no `web` subject in policy or registry, so the public web tier cannot reach it (M `test_a_valid_key_that_the_policy_does_not_name_gets_one_generic_403`). This work gives early evidence for #344 (cross-agent and escalation tests: rows 25 to 34) and #347 (redaction tests: rows 55 to 63), and nothing in it changes IAM scope. Per-agent workload identity (#345) would remove F-1 and F-2; it is not needed for a lab window.
- **F-9. Policy file is broader than Stage.** It grants nova, scout, cipher, pixel and sage full operations. Harmless while no registry names them; the registry is the live control (condition 2).

## Reproducing

`PYTHONPATH=src python -m pytest -q tests/test_memory_security_matrix.py tests/test_memory_security_transport.py tests/test_memory_security_logs.py` runs all 123 new tests in about 9 seconds with no network beyond loopback. The full offline suite: `PYTHONPATH=src python -m pytest -q`.
