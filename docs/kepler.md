# Kepler: governed coding agent

Status: **proposed**. Epic [#125](https://github.com/vchokshi/viharbot/issues/125). This page covers [#126](https://github.com/vchokshi/viharbot/issues/126) (identity and governance loading) and [#128](https://github.com/vchokshi/viharbot/issues/128) (manual assignment, branch workspace and draft PR workflow). Kepler is not deployed, is not wired into compose, Dockerfiles or deploy files, and nothing in the running application executes the `software_engineering` capability. The container and LiteLLM agent loop (#127), the Sage cycle limit (#129), Archie handoff (#130) and the final PR gate (#131) are separate issues and are not implemented.

## Identity

`src/viharbot/agent_identities/kepler.toml` and `prompts/kepler.md` declare Kepler as "The Architect of Harmony", after Johannes Kepler's discovery of mathematical order in planetary motion. The identity names no model alias and no runtime: it is independent of both. Like every identity it grants no permissions; the prompt states the boundaries, but they are enforced in code, not by the prompt.

## Governance loading

`src/viharbot/kepler_governance.py` loads the charter, constitution, architecture, skills registry and change-rationale policy from an assignment's checkout.

- **Fails closed.** A missing, unreadable, empty, oversized, non-UTF-8, unversioned (where a version is expected) or checkout-escaping document raises `GovernanceLoadError` and no bundle is returned, so the assignment cannot start. Errors name the document and the problem, never its contents.
- **Records versions.** Each document's stated version and SHA-256 are recorded (`GovernanceBundle.versions()`) for the audit log and the pull request. The hash is the authoritative identity.
- **Checks citations.** `verify_citations` requires every cited section to exist as a heading in the named document and every citation to carry evidence. `render_governance_followed` produces the mandatory "Governance followed" PR section. This proves the section exists, not that the evidence is true: Sage verifies that independently (#134).
- **Proposes, never approves.** `GovernanceProposal` is always `proposed`; constructing one with any other status raises. `require_editable(path, root)` rejects direct edits under `docs/governance/`, `docs/adr/` and to `AGENTS.md`. Its contract: `path` is checkout-relative and `root` is the trusted checkout root. Absolute, escaping and NUL-containing paths raise (fail closed), symlinks are resolved inside `root` so an innocent-looking alias of a protected file is still protected, and matching is case-insensitive. The workflow that calls it is #128.
- **Bounded reads.** Each document is read with a single read of at most the size limit plus one byte, so an oversized file produces the controlled error instead of exhausting memory.
- **Real headings only.** Headings inside fenced code blocks are not sections and cannot be cited.

## Assignment workflow (#128)

Code: `src/viharbot/kepler/`. The flow for one issue is **authorize, prepare workspace, commit, check, push, draft PR**, with every step recorded.

**Authorization.** A run starts only through `POST /v1/assignments` with a signed assertion (`service_auth`, audience `kepler`, capability `kepler.assign`). The caller's registry subject must also be named in `KeplerPolicy.assigners`, so a valid key without that policy entry is refused. The body is `{"issue": N}` and nothing else: the repository, clone URL and branch come only from trusted configuration. The issue must exist, be open and not be a pull request. There is one active run per issue; a second request gets 409 until the first is cancelled or failed, and then a new explicit authorization is needed. Nothing in the service starts a run by itself. Read is `GET /v1/assignments/{id}` (`kepler.read`); cancel is `POST /v1/assignments/{id}:cancel`. There are no merge, push, deploy or policy routes, and no docs or schema routes. Bind the service to a loopback or private interface; the replay cache is per process.

**Workspace.** Each run gets its own fresh `git clone` under the workspaces root and a `kepler/issue-N` branch derived from the issue number. Governance is loaded from that clone with the #126 loader and the run **fails closed** if it cannot be loaded. The loaded versions are recorded on the run, and the PR is refused (`governance_changed`) if they differ later.

**Commits.** All changes are staged and every path is checked with `require_editable`. Any change under `docs/governance/`, `docs/adr/` or to `AGENTS.md`, including through symlink aliases, is refused and unstaged. Repository git hooks are disabled.

**Push.** Only `HEAD:refs/heads/kepler/issue-N`, never forced. The branch is re-derived from policy at push time, so a tampered stored branch such as `main` is refused before git runs. A push with no commits beyond the base branch is refused.

**Draft PR.** Always created as a draft against the base branch, with `Refs #N` and the mandatory **Governance followed** section from #126 (citations are verified against the loaded documents). A second call updates the same PR. A PR that already exists for the branch, for example after a crash between creating it and saving it, is adopted rather than duplicated. The GitHub client has no merge, close, delete, approve or force operation and sends only `title` and `body` on update.

**Checks.** `run_checks` runs only commands that start with a configured allowlist entry (default `python -m pytest` and `ruff check`), in the workspace, with a timeout and a minimal environment that contains no GitHub token or LiteLLM key. The real exit code, duration and an output hash are recorded, and a failure is recorded as a failure.

**Credentials.** The GitHub token is injected into `HttpGitHubClient` and `Runner`. It reaches git only through the process environment (never argv), only for clone and push, never reaches test commands, and is scrubbed from stored and raised output. HTTPS only, or HTTP to a loopback or private address. Use a fine-grained token limited to `vchokshi/viharbot` (contents and pull requests write, issues read); the code cannot widen what the token allows. `vchokshi/viharbot-onboarding-instructor` cannot be configured as the repository.

**Audit.** Each run and event is persisted to a JSON store with atomic, locked writes. Events are SHA-256 chained, so editing, removing or reordering one is detected by `verify_chain`; the chain proves record integrity, not authorship. Denials are recorded too. Only named fields are written: no tokens, prompts or file contents. A corrupt store stops the service instead of being treated as empty.

**Failure behavior.** Clone, governance-load and push failures move the run to `failed` and free the issue for a new authorization. A process that dies mid-operation leaves the run in an in-flight state and the founder must cancel it. Automatic recovery, resource and time budgets and health checks are #127. The three-cycle remediation counter is #129.

## Limitations

The governance loader is called by the assignment service, but nothing drives the service yet (the agent loop is #127). The protected-path list is policy in code rather than a GitHub branch or credential restriction, and the website profile is a "proposed" section, not a live team card.
