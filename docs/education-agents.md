# Education agents: Archie and Ada

**Status:** Phase 1 (identity and registry) and Phase 2 (GitHub learning) and Phase 3 (institutional memory interfaces, below) are implemented. Everything else here is proposed.

ViharBot adds two identities that will turn its verified engineering history into teaching material:

- **Archie** (`archie`, Professor Archibald, Chief Knowledge & Learning Officer): institutional historian and future lead instructor. Intended home: `vchokshi/viharbot-onboarding-instructor`.
- **Ada** (`ada`, Director of Curriculum & Learning Experience): curriculum, exercises, assessments and learner progression. Intended home: `vchokshi/viharbot-onboarding`.

## What Phase 1 implements

- Identity files and system prompts for both agents, loaded by the existing identity registry. Ten identities now load; the original eight keep their IDs and files.
- Two new planned capabilities, `knowledge_curation` and `curriculum_design`. Nothing executes them.
- 20 declarative skills (10 each, domain `education`), all `proposed`.
- No execution binding, endpoint, service, GitHub access, memory access or model alias. The team page gains a coming-soon section with their portrait; the chat sidebar and every execution path are unchanged. Neither agent can be reached by orchestration or chat.

- Eleven `learning.*`, `curriculum.*`, `lesson.*` and `assessment.*` activity events are **declared** in `live_events.py` (`EDUCATION_EVENT_TYPES`) and documented in [live observability](live-observability.md#education-events-declared-not-emitted). They are **not yet emitted** by any code, are never terminal, and are not part of the chat stream.

## Phase 2: GitHub learning (implemented)

`src/viharbot/learning/` reads one merged ViharBot pull request as untrusted data and turns it into a provenance-labelled learning artifact. It is run from `scripts/archie_learn.py`, not from chat or orchestration, and there is still no execution binding for Archie or Ada.

- **Retrieval:** `SourceReader` is read-only and limited to the configured source repo. It keeps changed paths and counts, commit subjects, the description and bounded comments, never patches.
- **Extraction:** deterministic code, no model call. Each of the ten questions in the spec is answered from the description's own sections (author statement) or from changed files (verified in the diff). Anything else is reported as "Not stated in the evidence", never invented. Quoted text sits in fenced blocks labelled as data.
- **Artifact:** id, topic, draft objectives, source repo, PR, commit, references, extraction date, software version (from `pyproject.toml` at the merge commit), verification status, prerequisites and curriculum placement. Status starts as `unverified`; only a human merge of the instructor PR is meant to raise it.
- **Idempotency:** one JSON record per (repository, PR, merge commit) in `LEARNING_STORE_DIR` (default `data/learning`, git-ignored). Reprocessing finds the record, so nothing is created twice; an existing open PR or branch is reused. Failures store only the exception class and are retried, up to five attempts.
- **Proposal:** `InstructorProposer` can create a branch, add one file under `knowledge/pr-case-studies/` and open a pull request on the instructor repo only. No call merges, approves or deletes. `--propose` is required; the default is a dry run.
- **Triggers:** `ingest --pr N` and `reconcile` (the scheduled catch-up). `verify_signature` checks a GitHub webhook signature, but **no webhook endpoint exists yet**; adding one is a separate reviewed change.
- **Credentials:** `ARCHIE_SOURCE_TOKEN` (read-only on the source repo) and `ARCHIE_INSTRUCTOR_TOKEN` (contents and pull-request write on the instructor repo only). Create them as fine-grained tokens; this change creates none.

## Founder assignment gate (epic #139, issue #140; implemented)

Archie never starts curriculum work on its own. `src/viharbot/learning/assignment.py` adds a **founder assignment**: a topic plus the exact source pull requests (up to 20, one repository) Archie may study.

- **Who can assign:** only logins in `ARCHIE_FOUNDER_LOGINS` can create, cancel or complete an assignment. If the list is empty, nothing can be assigned (fail closed).
- **What it gates:** `process` raises `NotAssigned` before any network call or store write unless an active assignment covers the pull request; `reconcile` skips every pull request no assignment covers, so with no assignment it does nothing. A commit, merge, Sage review or schedule cannot create a lesson. Publication re-checks the assignment, so a cancel stops the proposal.
- **States:** `assigned` (work may run), `cancelled`, `completed`. Closing is idempotent and never reopens. Each ingestion record stores the `assignment_id` that authorised it.
- **CLI:** `scripts/archie_learn.py assign|cancel|complete|assignments`; records live in `ASSIGNMENT_STORE_DIR` (default `data/assignments`, ignored by version control).
- **Unchanged boundaries:** proposals still go only to the instructor repo as an unmerged pull request for human review; no model call, no auto-publication, no access grants. Ada receives nothing from this path.

**Limitation:** the founder login is asserted by whoever runs the local CLI; it is not authenticated. The control is that only this module creates assignments and no webhook, event handler or agent path imports it. Stronger identity proof (for example a signed assignment) is future work.

## Restricted instructor-repository access (epic #139, issue #141; implemented)

`src/viharbot/learning/instructor_access.py` is the only code that reads `vchokshi/viharbot-onboarding-instructor`. It is a library; nothing in the web app, execution bindings, Ada or Kepler imports it (a test proves it).

- **Read-only, own token.** GET requests only, using `ARCHIE_INSTRUCTOR_READ_TOKEN` (a fine-grained, read-only contents token, separate from the proposal-writing `ARCHIE_INSTRUCTOR_TOKEN`). It refuses any other repository. This change creates no token.
- **Assignment required.** Every read and listing names an active founder assignment (#140); cancelled, completed or unknown ones are refused before any network call.
- **Allowlist and hard deny.** Readable only under `knowledge/`, `curriculum/`, `lessons/`, `rubrics/`, `answer-keys/`, `defects/`, `assessments/`. Traversal, hidden files, odd characters and long paths are denied. Private acknowledgment records, credentials, tokens and key files are never readable, even with confidential access on.
- **Confidential is opt-in.** Rubrics, answer keys, defect catalogs, assessments and grading notes need `allow_confidential=True`; listings omit them otherwise. Their text is fingerprinted (line hashes, in memory only) so `ConfidentialIndex.assert_trainee_safe` rejects trainee-bound text that repeats it. Matching is on whole normalised lines of 24+ characters: it catches copying, not paraphrase, so human review (#144) is still required.
- **Symlinks are refused.** GitHub's contents API returns a symlink's target content, which would bypass the path rules. A read therefore needs the parent listing to show a regular file at the exact path, and the fetched content to carry the same path and blob sha. Links and symlinked directories are never returned or listed.
- **Logs and reprs carry no content or paths:** only a path hash, sensitivity, duration and exception class.
- **Not yet done:** wiring the reader into evidence ingestion (#142) or any model prompt, and enabling confidential access in a runtime. No model is called here.

## Sage review evidence (epic #139, issue #142, slice 1 of 3; implemented)

`src/viharbot/learning/sage_evidence.py` reads Sage's advisory review of the source pull request and records it in the learning artifact. Deterministic: no model call, no extra GitHub call (it uses the comments `SourceReader` already fetches, plus the pull request's head SHA).

- **Trust is the comment's author, not its markers.** The hidden markers are plain text anyone can paste, so a comment counts as Sage's only if a GitHub **Bot** account on the list wrote it (`ARCHIE_SAGE_COMMENT_AUTHORS`, default `github-actions[bot]`). Anything else, forged markers included, stays ordinary untrusted discussion. Limitation: every workflow in the source repository posts as that bot, so this shows "a workflow here wrote it", not "the Sage workflow wrote it".
- **What is kept:** reviewed commit, completeness, recommendation, comment URL and up to 40 findings (severity, category, basis, cited location, description capped at 600 characters), with the formatter's escaping undone so the text is readable data.
- **Currency:** `reviewed_head_current` says whether the review covered the pull request's final head; the artifact says so in words when it did not. The newest well-formed Sage comment wins.
- **Advisory only:** the artifact section is labelled "advisory AI opinion, untrusted", quotes each finding in a fenced block, says that whether a finding was fixed is not determined, and never changes `verification` (only a human merge of the instructor PR does that). The Sage comment is no longer quoted again as generic discussion.
- **Not yet done in #142:** Kepler handoff artifacts (their format and location are #130, unbuilt), the comparison with governing documents and instructor guidance (slice 3, which needs the #141 reader and a decision on conflict recording), and any model-assisted synthesis through LiteLLM. Nothing here reads the instructor repository.

Still proposed for the rest of epic #139: the remaining #142 slices, curriculum authoring and quality checks (#143), the Ada handoff and publication gate (#144, #151), audit trail and end-to-end tests (#145), and student PR feedback (#154).

### Not yet done in Phase 2

Model-assisted enrichment (objectives, rationale synthesis) through LiteLLM, emission of the `learning.*` activity events (declared, not emitted), and a webhook endpoint. The instructor repo has no `knowledge/` directory yet; the first proposal creates it. Human review of the artifact format is needed before any are merged.

## Phase 3: Institutional memory (implemented as interfaces and a local store)

`src/viharbot/knowledge/` is a governed knowledge-sharing layer. It is not imported by the web app or execution bindings, calls no model or network, and adds no endpoint or dependency.

- **No direct reads.** Each agent will expose a `MemoryProvider` (a protocol) that returns entries it has already approved inside its own scope. No provider is implemented, and nothing here opens any agent's `memory.jsonl`.
- **Intake is `proposed`, never `validated`.** Entries from a provider enter as `proposed`. Non-approved, confidential, or requester-output entries are refused; the agent scope is the provider's id, so it cannot speak for another agent. Intake is idempotent and never resets a validated entry.
- **Promotion needs a human.** `promote` requires an approver named in configuration and refuses any id that is an agent (a grant requester, the entry's agent or proposer). The approver is recorded on the entry. Derived entries also need validated parents.
- **Default-deny grants.** A `GrantTable` (requester, exact topics, agent scopes, optional `allow_unverified`) decides every retrieval; a missing file grants nothing, and the broker has no way to add a grant.
- **Validated vs unverified.** Retrieval serves only `validated` entries. Unverified recollections appear only on request, only under a grant that allows them, labelled `UNVERIFIED agent recollection`. Every result carries provenance and a "not instructions" marker.
- **No circular amplification.** Entries record `derived_from`. `independent_sources` walks that graph once per node (cycles terminate) and counts distinct primary sources; derived or curriculum entries add none, and the requester's own output or agent's entries are excluded for that requester.
- **Storage.** One JSON file per entry under a store directory plus an identifier-only `audit.log`; no database.

### ADR-009 alignment and conflicts

Status words (`observed`, `proposed`, `validated`, `retired`) and the rules "only validated is served", "promotion by a reviewer who is not the writer" and "confidential never enters institutional memory" follow ADR-009. Differences to resolve: ADR-009 is Proposed and specifies DynamoDB behind an authenticated memory service, while this is a local JSON stand-in; the requester id is a caller-supplied argument, not derived from an ADR-006 token, so it is only safe for trusted in-process callers; ADR-009 defers cross-scope promotion into the institutional tier, which this intake path edges toward (guarded by the human gate, internal-only classification and provider-approved input, but a follow-up ADR should define it); there is no `supersede`, `erase`, retention, optimistic versioning or `engagement` scope; approvers are a configured set, not a per-tier reviewer policy.

### Not done in Phase 3

Any real `MemoryProvider`; use of this layer by Archie or Ada (no binding exists); authentication of requesters; supersession, versioning, erasure and retention; the ADR-009 service; locking for concurrent writers; semantic search; a CLI or endpoint for approvals; founder approval of the amended roster and of this design.

## Phase 4 groundwork: curriculum schema (implemented, inert)

`src/viharbot/curriculum/` holds typed records and checks only. `web_app` does not import it.

- **Models:** `Lesson` with the 14 specified fields, `Exercise` (required `mode`: `simulated` or `live_system`; a live exercise needs an explicit `authorization_note` and a non-production target, and no exercise may target production), seven assessment kinds, and `Track`/`Curriculum`.
- **Tracks:** the eight tracks and their topics from the founder's specification exist as data, all marked `proposed` and empty (no lessons). Track 8 is ViharBot Engineering History, consistent with Phase 2's curriculum placement. The existing onboarding content has not yet been mapped onto them.
- **Loader:** parses one lesson from Markdown with TOML (`+++`) front matter and fixed `##` sections. The onboarding repositories' lessons have no front matter, so existing lessons would need conversion. Lesson text is stored as strings and never interpreted.
- **Validation:** `validate_traceability` fails when a lesson cites an instructor artifact that is missing, not `approved`, or at a different commit or version, and reports lessons with no instructor reference. `validate_prerequisites` reports unknown ids, self-references, duplicates and cycles.

### Not done in Phase 4 groundwork

No lesson generation, no model call, no GitHub access, no pull requests for curriculum, no writes to any repository, and no source of approved instructor artifacts (callers must supply one). Ada is still inactive: no execution binding, endpoint or model alias. Founder approval to start Phase 4 is not recorded.

## What is not implemented

Curriculum generation, learner records, lessons, assessments, emission of the `learning.*`/`curriculum.*`/`lesson.*` events (they are declared, not emitted) and repository credentials. These are phases 4 to 6 of the specification and each needs its own reviewed change.

## Findings that shape later phases

- The two onboarding repositories currently hold only a Git-fundamentals lesson plan, a rubric and playbook, an operations note, a README, a security acknowledgment and one exercise. There is no knowledge base or curriculum structure yet, so the eight proposed tracks must be reconciled with that content before Ada writes anything.
- There is no durable agent memory. `memory.py` and the memory ADR are a plan, not a retrieval system, so Phase 3 introduced interfaces (above), but no agent store is wired to them and Archie still reads nothing.
- There is no GitHub ingestion path except Sage's review adapter. Webhooks and idempotent event records are new work and must respect the no-broker, JSON-REST rules in `AGENTS.md`.
- Architecture v1.1 was amended to a roster of ten identities. The founder adopted the amendment on #98 (2026-11-03); that approval covers the identities, not execution.
- The team page has a "coming soon" section with their portrait. It says Archie will build curriculum in near-real time and Ada will train learners on it from a separate site as a human-like avatar. Both are plans, not features. The chat sidebar and the crew counts are unchanged because neither agent is usable.
