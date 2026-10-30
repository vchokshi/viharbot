# Contributing to ViharBot

Thank you for your interest in improving ViharBot. This project welcomes proposals and pull requests while maintaining founder-led architectural governance and merge decisions.

Read the [Founding Mission Charter](docs/governance/founding-charter-v2.md) and [Agent Constitution](docs/governance/agent-constitution-v1.md) before proposing foundational changes. These documents describe project direction; their presence does not imply every proposed feature is implemented.

## Two contribution paths

**External contributors (including contributors not yet onboarded)**

1. Open or reference an issue describing the problem and intended scope.
2. Fork the repository; create a focused branch such as `fix/issue-123-timeout-handling`.
3. Implement and test the change in your fork.
4. Open a pull request against `vchokshi/viharbot:main`, linking the issue and explaining validation and risks.
5. Respond to review feedback. A PR may be submitted before onboarding, but submission does **not** guarantee acceptance or merge.

**Trusted internal contributors (after authorization)**

1. Confirm your access and approved work scope with the maintainer.
2. Branch from current `main` in this repository, e.g. `feat/issue-123-event-tracing`.
3. Push to that feature branch and open a PR targeting `main`.
4. Complete required checks and reviews; wait for founder merge approval.

**Never push directly to `main`**, regardless of contribution path. Use descriptive names such as `docs/issue-30-contributor-governance`, `fix/issue-123-description`, or `feat/issue-123-description`. Keep PRs narrowly scoped; avoid unrelated refactors.

## Onboarding and acceptance

External contributors may open issues and submit PRs before onboarding. Acceptance and merge of contributions are gated on successful completion of the maintainer-prescribed onboarding and eligibility review. Trusted internal repository access requires explicit authorization. Onboarding may include private manual exercises and intentionally seeded defects; details, evaluation plans, answers, and training materials are confidential and must not be linked, copied into public issues or PRs, or disclosed to unauthorized tools or people. Ask the maintainer privately about eligibility and onboarding; do not assume public PR submission confers trusted status.

## AI-assisted engineering and accountability

AI should accelerate engineering competence, **not substitute for it**. AI assistance is prohibited during the manual onboarding assessment. Once that assessment is completed and AI use is authorized, contributors may use AI for implementation, research, tests, and review, but remain fully accountable for all submitted changes, including generated code. Contributors must be able to understand and explain their design and code, inspect generated output, review its security and licensing implications, and run appropriate tests. Never submit unreviewed AI output. Do not send credentials, secrets, personal or sensitive data, private onboarding materials, or confidential project information to unauthorized AI services.

## Architecture and founder authority

The founder retains **final architectural decision authority**, separately from **final merge authority**. Before implementing a material change to the project's mission, constitution, agent identities, LiteLLM inference routing, security architecture, authorization boundaries, or other foundational design, submit an architectural proposal and obtain explicit founder approval. The proposal must describe the problem, alternatives, tradeoffs, security and compatibility implications, and expected impact, and explain alignment with the [Mission Charter](docs/governance/founding-charter-v2.md) and [Agent Constitution](docs/governance/agent-constitution-v1.md). Approval to explore a design is not permission to merge or deploy it. Normal implementation PRs remain subject to review and founder-controlled merge.

## Testing and pull request review

Before requesting review:

- Run `python -m pytest -q` in an environment with Node installed for the frontend tests. Avoid local `.env` settings that change feature flags; do not run opt-in billable LiteLLM tests unless explicitly authorized.
- Run `ruff check src tests scripts .github/scripts` after installing the project test dependencies and Ruff.
- Describe tests actually run, their results, any skipped checks, and relevant risks. Never claim unexecuted checks passed.
- Explain the change, linked issue, design rationale, compatibility impact, and any new dependencies.
- Do not bypass CI, security review, or approval gates. Respond to review comments before requesting merge.

The repository's CI also checks Python, frontend behavior, lint, container builds, and security-related workflows. Passing checks is necessary but not sufficient for acceptance. The founder decides whether and when to merge; no contributor is entitled to merge their own PR.

## Security reporting

Do **not** publish exploitable vulnerabilities, credentials, sensitive data, or attack instructions in public issues or PRs. Contact the repository owner privately through an established private channel to coordinate responsible disclosure. If GitHub private vulnerability reporting is enabled for this repository, use that channel. Do not assume a private channel exists without verifying it, and never include confidential assessment or seeded-defect details in public reports.

## Licensing and provenance

**Licensing status:** At the time this policy was drafted, the repository had no root `LICENSE` file. Public visibility does not itself grant an open-source license or broad rights to reuse, redistribute, or incorporate the code. Before submitting contributions that require a particular license grant, contact the founder to clarify terms. The founder must select and publish a repository license before this document can claim a specific open-source licensing model.

**Attestation:** No Contributor License Agreement (CLA) or Developer Certificate of Origin (DCO) sign-off requirement is adopted by this document. The founder must decide whether either mechanism is appropriate before it becomes mandatory; do not represent one as already in force.

Only contribute code and assets you have rights to submit. Identify third-party dependencies, their licenses and notices, and any compatibility concerns. Review AI-assisted output for potential copied code, attribution requirements, and licensing conflicts. Record provenance and material dependency changes in the PR. Do not assume AI-generated code is free of intellectual-property obligations.

## Maintainer decisions

The founder retains final decisions on contributor eligibility, architecture, acceptance, and merges. Maintainers may request revisions, reject changes, or defer proposals even when CI passes. No contribution policy overrides applicable security controls or confidentiality obligations.
