# Archie and Ada decoupling: deletion and migration manifest

Status: inventory only. Nothing in this document has been deleted or moved yet.
Tracks issue [#293](https://github.com/vchokshi/viharbot/issues/293). Taken from `origin/main` at `227fc54` (after PR #271).

ViharBot is the consulting and development team. Professor Archibald (`archie`) and Dr. Ada Lovewell (`ada`) are separate applications in `vchokshi/professor-archibald` and `vchokshi/dr-ada-lovewell`. Kepler, Sage, Charlie and the other team agents stay. This is a staged cleanup: migrate, verify, then delete, in separate reviewable PRs.

## Findings that change the plan

1. **The issue's file list was written against an older tree.** `src/viharbot/learning/` now has 21 modules and `docs/archie-workflow-gap-analysis.md` exists. They are listed in full below. The issue's module list omits `ada`, `basis`, `confidentiality`, `description`, `fsutil`, `governance`, `github` and `store`.
2. **`src/viharbot/curriculum/` is Archie-owned.** Its only importers are `learning/` (`lesson.py`, `synthesis.py`) and `tests/test_curriculum_schema.py`. It migrates with Archie. The issue did not list it.
3. **`learning/` imports shared platform code that must stay:** `sage_review_github`, `sage_review_format` (Sage's review client and format, used elsewhere) and `identity_registry`. After migration Archie needs these as a published dependency or a vendored copy. This is an open interface decision.
4. **`src/viharbot/team_profiles.py` and `tests/test_team_page.py` carry the website cards.** `static/index.html` has no Archie or Ada reference on current main (the old "Learning" section is gone). The live website references are the team page data in `team_profiles.py` and the shared `Professor_and_Assistant.png` in `web_app.py:256`.
5. **The Kepler steward already forbids the instructor repo by design** (`deploy/agentcore/kepler/smoke-test.sh:39-40`). This is a cross-repo contract to preserve, not remove.
6. **The knowledge broker is a reusable abstraction.** `src/viharbot/knowledge/` uses `archie` only as an example requester in `tests/test_institutional_memory.py`. Keep the module and genericize the tests' requester name.
7. **`.gitleaksignore` entries (lines 12, 15, 18) reference `tests/test_archie_confidentiality.py` by commit SHA.** Moving the file orphans them; they must be retired in the same change that deletes the file, and `.gitleaksignore` is not a protected path but secret-scan behavior should be re-verified.
8. **Protected paths are involved.** `AGENTS.md`, `docs/governance/` and `.github/` need founder-reviewed merge. `docs/governance/*` references (below) cannot be edited by an agent PR without that review.

## A. Migrate to `vchokshi/professor-archibald`, then delete here

Source (~3.4k lines in `learning/` plus a 125-line CLI):

| Path | Notes |
|---|---|
| `src/viharbot/learning/` (all except `ada.py`) | assignment, audit, authn, basis, confidentiality, description, evidence, extract, feedback, fsutil, github, governance, ingest, instructor_access, lesson, models, publication, sage_evidence, store, synthesis, workflow, `__init__` |
| `src/viharbot/curriculum/` | loader, models, validate, `__init__` (finding 2) |
| `scripts/archie_learn.py` | CLI; imports 7 `learning` modules |
| `src/viharbot/agent_identities/archie.toml`, `prompts/archie.md` | identity and prompt |
| `src/viharbot/skills/definitions/archie.toml` | 10 skills |
| `tests/test_archie_*.py` (11 files), `tests/archie_helpers.py` | dedicated tests |
| `tests/test_learning_pipeline.py`, `tests/test_instructor_access.py`, `tests/test_curriculum_schema.py` | dedicated tests |
| `tests/test_education_events.py`, `tests/fixtures/live_events/education/education_events.json` | event contract fixtures; decide with section D |
| `docs/archie-workflow-gap-analysis.md` | Archie workflow analysis |

Config: `.env.example` lines 34-47 (`ARCHIE_FOUNDER_LOGINS`, `ARCHIE_SAGE_COMMENT_AUTHORS`, `ARCHIE_SOURCE_TOKEN`, `ARCHIE_INSTRUCTOR_TOKEN`, `ARCHIE_INSTRUCTOR_READ_TOKEN`). Move to Archie's `.env.example`.

## B. Migrate to `vchokshi/dr-ada-lovewell`, then delete here

| Path | Notes |
|---|---|
| `src/viharbot/learning/ada.py` | 201 lines, Ada's learner-facing module |
| `src/viharbot/agent_identities/ada.toml`, `prompts/ada.md` | identity and prompt |
| `src/viharbot/skills/definitions/ada.toml` | 10 skills |

No Ada-specific curriculum or learner UI exists in ViharBot beyond the above. Ownership of `viharbot-onboarding` materials (copied to Professor Archibald) is undecided; see Decisions needed.

## C. Edit in place (shared files; do not delete)

| Path | Change |
|---|---|
| `src/viharbot/team_profiles.py` | drop `archie`/`ada` from status map (l.42), roster order (l.48), portraits (l.59, 72-75) and profile text (l.142+) |
| `src/viharbot/web_app.py:256` | drop `Professor_and_Assistant.png` from the served image list |
| `tests/test_team_page.py` | remove Archie/Ada cards, status and portrait assertions (l.43, 70, 134) and fix card counts |
| `src/viharbot/identity.py:37` | `KNOWLEDGE_CURATION` capability is marked "Archie: planned". Remove only if nothing else uses it; check `tests/test_agent_identity.py:39` |
| `src/viharbot/dev_wiki.py:60, 799` | drop "Archie", "Ada" from the team list and `education-agents` from the doc filter |
| `src/viharbot/personas/registry.py:25` | comment only |
| `src/viharbot/knowledge/__init__.py:3` | docstring example only |
| `src/viharbot/agent_identities/kepler.toml:6`, `prompts/kepler.md:4,22` | replace "document the work for Archie" with an external, explicit handoff contract (section D) |
| `tests/test_agent_identity.py` | `EXPECTED_IDS`, capability map (l.39), model-family map (l.52), assertions (l.90-100) |
| `tests/test_skills_registry.py` | `EXPECTED_IDS`, skill-count map (l.27-29), comment (l.58) |
| `tests/test_persona_registry.py:89-90` | identity-list assertion |
| `tests/test_institutional_memory.py` | rename requester `archie` to a neutral name; no behavior change |
| README.md (l.72, 92), `pyproject.toml:8` | agent counts ("eleven" becomes nine) and skill count (98 becomes 78); the pyproject text is generic and can stay |
| `.gitleaksignore` | retire the three entries once the file is gone (finding 7) |

## D. Docs

| Path | Action |
|---|---|
| `docs/education-agents.md` | move to Archie, or rewrite as a short "external integration" page |
| `docs/live-observability.md` | edit the 5 references (education event contract) |
| `docs/architecture.md`, `docs/agent-identity-architecture.md`, `docs/agent-personas.md`, `docs/persona-community-specification.md`, `docs/kepler.md` | edit references |
| `docs/governance/agent-architecture-v1.1.md`, `docs/governance/agent-skills-registry-v1.md` | **protected path**: founder-reviewed edit |
| `docs/governance/private-onboarding-setup.md` | **protected path**: ownership decision first |

Retained on purpose: documentation of the external handoff (versioned artifact or event contract) between Kepler/Sage/Charlie and the two applications.

## E. Intentional retentions

`sage_review_github.py`, `sage_review_format.py`, `identity_registry.py`, `knowledge/`, the Kepler steward and its instructor-repo guard, and closed issues and PRs (#139-145, #154) stay. Issues #125, #130, #131, #146, #151, #155, #162 stay open and get a cross-repo ownership comment.

## Proposed PR sequence

1. **This manifest** (docs only).
2. **Migrate A and B** into the two repos (their PRs, adapting package paths). Verify their test suites pass independently. No deletion here yet.
3. **Define the external handoff contract** (versioned JSON artifact or event) and update Kepler, Sage and Charlie to emit it, with tests.
4. **Delete A and B from ViharBot, and apply C and D**, with the full offline suite, a static import search for `archie|ada|learning|curriculum`, gitleaks and the website checks.
5. **Update the cross-referenced issues.**

## Decisions needed

1. Which repo owns the trainee-facing `viharbot-onboarding` materials, and the `education_events.json` contract?
2. How should Archie consume `sage_review_*` and `identity_registry`: published package, git dependency, or vendored copy?
3. Handoff contract shape for Kepler to Archie: versioned JSON artifact in a repo, a GitHub event, or REST?
4. Founder approval for edits to `AGENTS.md`, `docs/governance/`, `.github/` (sections C and D).
