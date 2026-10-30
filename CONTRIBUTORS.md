# Approved Trusted Contributors

This file is the **current trusted-contributor eligibility registry** for ViharBot. It is not a list of everyone who has contributed code or documentation. Historical attribution remains in Git history when a contributor is offboarded.

## Registry

| GitHub username | GitHub numeric account ID | Role | Approved on (UTC) |
| --- | ---: | --- | --- |
| [@vchokshi](https://github.com/vchokshi) | 8943966 | Founder | N/A |

The founder is listed as the project owner; this is not an assessment sign-off. The founder's approval date is **N/A** because founder status is not a contributor onboarding approval. For all future approved contributors, use the role **Trusted contributor** and an actual approval date in ISO 8601 format (`YYYY-MM-DD`); never invent a date. Other rows are added only after the required security acknowledgment, Git qualification, and explicit founder authorization.

## How registration works

1. A candidate forks the public repository, creates an `onboarding/<github-username>` branch, and proposes **one new row** through a pull request.
2. The candidate must supply their actual GitHub username and stable **numeric account ID**, and must not alter any existing row. The instructor independently verifies identity and assesses the Git workflow without AI assistance.
3. The founder confirms the acknowledgment and qualification, explicitly approves the candidate, and approves merging the registration PR.
4. **Until the approved PR is merged into protected `main`, the candidate is not listed or trusted.** Submitting or passing checks on a PR does not authorize its author.
5. Repository collaborator/team permissions, if needed, are provisioned separately by the founder.

A GitHub username can change. The numeric account ID is the stable identity key; automated checks must validate it against GitHub and use the registry on the trusted base branch, **never a PR-supplied version**, for authorization decisions. Do not interpret the presence of a row as proof of GitHub collaborator access.

## Offboarding

The founder approves a PR removing the person's current registry row and separately revokes their GitHub collaborator/team access and any relevant credentials. Confirm the change is merged and access has been revoked; record evidence in founder-controlled private storage. Previous commits and attribution remain in Git history. Registry removal alone does not revoke GitHub permissions, and permission revocation alone does not update this registry.

## Important boundaries

- **External contributors** can still submit and discuss ordinary pull requests without appearing here. Their contributions remain subject to the applicable contribution-rights requirements.
- Nobody may self-approve a registry change, merge their own onboarding PR, or use a proposed registry row to bypass review.
- The founder controls approvals and merges. CI results do not constitute authorization.
- This Markdown registry is a policy source, **not a GitHub permission enforcement mechanism**. Automated checks and repository rulesets must be separately implemented and verified.
- Do not publish security acknowledgments, assessment results, private instructor material, or personal access records here.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution policy.
