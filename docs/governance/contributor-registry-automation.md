# Contributor registry automation

This describes what is automated for [`CONTRIBUTORS.md`](../../CONTRIBUTORS.md), what is not, and the GitHub settings the founder must apply by hand. Read the last section first if you only want to know what is *enforced*.

## What the registry is, and is not

`CONTRIBUTORS.md` lists the currently approved trusted contributors, keyed by the stable **numeric GitHub account ID** (usernames can change). It is a policy source. It is not a GitHub permission: being listed grants no repository access, and removing a row revokes none. Four kinds of people exist:

| Who | In the registry on `main`? | What they can do |
|---|---|---|
| External contributor | No | Fork and open ordinary pull requests (Path A). No onboarding needed |
| Applicant | No (their PR proposes a row) | Open a registration PR. The PR does not make them trusted |
| Trusted contributor | Yes | Eligible for privileged workflows. Repository access is still granted separately |
| Revoked contributor | No (row removed) | Treated exactly as an external contributor. Merged commits stay |

A revoked contributor is simply absent from the registry; the registry does not keep a list of past members.

## What is automated

* **`.github/scripts/contributor_registry.py`** (standard library only) parses and validates the registry, classifies a pull request's effect on it, and answers eligibility questions.
* **`.github/workflows/contributor-registry.yml`** runs the script on every pull request to `main` and reports the check **Contributor registry**.

The check enforces, and reports with a line number and a fix:

* The table header, one row per contributor, valid username syntax, an account ID that is a positive whole number with no leading zeros, and an `Approved on` value that is a real `YYYY-MM-DD` date or `Founder`.
* No duplicate account IDs and no duplicate usernames (case-insensitive).
* **Registration requests.** A pull request that adds exactly one row, for its own author (matching the author's account ID and username), changes only `CONTRIBUTORS.md`, and does not claim `Founder`, is classified as a registration. Anything else that adds rows is rejected.
* **Unauthorized modification.** Changing an existing row (username, date or founder marker), or adding and removing rows together, is rejected. Existing rows may only be changed by the founder, identified as the `Founder` row **on the base branch**, never in the pull request's own copy.
* **Removal.** Removing rows is accepted as a well-formed removal request and flagged as needing founder approval.
* **Identity.** For rows a pull request adds or changes, the script asks GitHub for the account with that ID and confirms its current login matches the username. If GitHub cannot be reached, is rate limited or errors, the check **fails** and says the identity is unverified. It never passes silently.
* A pull request that does not touch the registry passes without contacting GitHub, so ordinary external pull requests are unaffected.

A passing check means the change is well formed. **It is not approval and grants no trust.** The report says so every time.

## How privileged workflows must decide eligibility

No workflow in this repository is privileged by contributor status today. When one is added, it must decide eligibility from the protected default branch only:

```bash
git fetch origin main
python3 .github/scripts/contributor_registry.py eligible --git-ref origin/main --account-id "$ACTOR_ID"
# prints "trusted" (exit 0) or "external" (exit 1); exit 2 if the registry is invalid
```

The command reads the file with `git show <ref>:CONTRIBUTORS.md`, never from the working tree, so a pull request's modified registry cannot authorize anyone. Use the numeric ID from the event payload (`github.actor_id`), not the username. An invalid registry fails closed. Eligibility is not a substitute for GitHub permissions or for review.

## Why the workflow is designed this way

The workflow uses `pull_request_target` deliberately, and safely:

* The workflow file and the script come from the **base branch**, so a pull request cannot edit the check that judges it. With a plain `pull_request` trigger, the pull request's own copy of both would run, and an attacker could weaken them.
* The only checkout is the base commit. The pull request's commit is fetched as plain data (`git fetch refs/pull/N/head`) and only its `CONTRIBUTORS.md` is read and parsed. No code from the pull request is checked out, installed, imported or run.
* Permissions are `contents: read` and `pull-requests: read`. No secret is used. The token is read-only, and the pull request cannot reach it.
* Event data (author, number) is passed through `env:`, never interpolated into shell. Untrusted text is stripped of newlines and backticks before it is printed, so a username cannot inject workflow commands.
* There is no path filter, so the check reports on every pull request and can be made required without leaving unrelated pull requests waiting.

Tests in `tests/test_contributor_registry.py` assert these properties of the workflow file, so weakening them fails CI. Those tests protect against accidents, not against the founder; see the next section.

## What Actions cannot enforce

* **Founder approval of a registration or removal.** A workflow cannot require a human review. Only a branch ruleset can. Until the settings below are applied, founder approval is policy, not enforcement.
* **Preventing someone from editing the workflow or script.** With the design above a pull request cannot change the check that judges it, but a pull request that edits `.github/` is only stopped from merging by **required review of those paths**. That also needs the ruleset below.
* **Revoking access.** Removing a row does nothing on GitHub (see Offboarding).
* **That the person is who they say.** The check confirms the ID and username belong together. It does not confirm the person; that is the founder's and the instructor's job.

## Settings the founder must apply (manual, not applied by this change)

Verified when this was written: one active ruleset applies to the default branch with **no bypass actors**. It blocks deletion and non-fast-forward pushes and requires a pull request, but requires **0 approvals**, no code-owner review and no status checks. Actions default workflow permissions are `read`, Actions cannot approve pull requests, and first-time fork contributors need approval to run workflows. The founder is the only collaborator. This is coordinated with issue #31 and does not replace it.

Settings > Rules > Rulesets, edit the existing ruleset (target: default branch, enforcement: active):

| Setting | Value | Why |
|---|---|---|
| Require a pull request before merging | on | Already on |
| Required approvals | `1` | Without it nothing forces a human review |
| Require review from Code Owners | on | Makes `.github/CODEOWNERS` binding for `CONTRIBUTORS.md`, `.github/`, `CONTRIBUTING.md`, `docs/governance/` |
| Dismiss stale approvals on push; require approval of the most recent push | on | A later push cannot ride on an earlier approval |
| Require status checks | `CI result` and `Contributor registry` (add others per [continuous integration](../continuous-integration.md)) | Offered only after each has run once. Do not require "Contributor registry" before this change is merged, or every pull request will wait for it |
| Require branches to be up to date | on | The check compares against current `main` |
| Restrict deletions; block force pushes | on | Already on |
| **Bypass list** | The repository **admin** role, mode **"For pull requests only"** | See below |

**Self-approval problem.** GitHub does not let an author approve their own pull request, and the founder is the only collaborator. With one required approval and code-owner review, the founder's own pull requests could not merge. Bypass in "pull requests only" mode lets the founder merge their own pull request, still through a pull request and not by pushing, but it also lets the founder skip required checks. The alternative is a second reviewer account, which gives stronger separation and costs a second person. Choose deliberately; this is the founder's decision.

Other settings: leave Actions default workflow permissions on **Read** and "Allow GitHub Actions to create and approve pull requests" **off**; keep fork-PR approval at least at "first-time contributors" (consider "all outside collaborators"); do not grant write access to anyone who is merely registered.

Not verifiable from the repository: whether the ruleset was applied as described. After applying it, run a negative test: open a pull request that edits `CONTRIBUTORS.md` from an account without bypass and confirm it cannot merge without the founder's review.

## Registration, step by step

1. The applicant forks, creates `onboarding/<their-username>`, adds **one row for themselves** and opens a pull request. The **Contributor registry** check validates it and confirms their username matches their account ID. An unlisted author can do this; there is no bootstrap loop.
2. The founder confirms the security acknowledgment, the instructor's qualification and contribution rights, then reviews and merges, setting the real approval date.
3. Trust begins when the merge lands on `main`. Repository access, if any, is granted separately.

## Offboarding

Removing someone from the registry ends **registry eligibility** for privileged workflows from the moment the removal merges to `main`. It does not touch GitHub. Complete all of these:

1. Open a pull request removing the person's row. The check accepts it as a removal; the founder approves and merges it.
2. Remove them as a repository collaborator and from every team (Settings > Collaborators and teams).
3. Revoke their credentials: personal access tokens and SSH or deploy keys the founder issued or approved, and any access to hosting, LiteLLM keys or other project systems.
4. Remove their access to the private onboarding repositories.
5. Rotate any shared secret they could see.
6. Check the repository and organization audit log, and record the evidence in founder-controlled private storage, not in this repository.
7. Merged commits stay in Git history with their attribution. Open pull requests by the removed person become ordinary external pull requests.

If steps 2 to 5 are skipped the person keeps access even though the registry no longer lists them. Permission revocation alone also does not update the registry.

## Known limitations

* Nothing here has run on GitHub yet. The workflow's behavior is covered by unit tests of the script and of the workflow file's text, not by a live run; the first pull request after merging is the real test.
* The GitHub lookup uses the read-only workflow token and is subject to API rate limits and outages. In that case the check fails and must be re-run.
* The check cannot tell a new contributor's row from a real approval; that is a human decision.
* `CODEOWNERS` and the ruleset govern review only on `main`.
