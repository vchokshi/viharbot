# Private onboarding infrastructure (Issue #32)

This document specifies the access-control setup for contributor onboarding. It intentionally contains **no exercises, answers, rubrics, defect seeds, credentials, or trainee records**.

## Repository separation

Create two repositories under the founder's account, both with GitHub visibility set to **Private**:

1. `vchokshi/viharbot-onboarding` — trainee-facing exercises, instructions, and practice submissions.
2. `vchokshi/viharbot-onboarding-instructor` — instructor-only preparation, answer keys, assessment rubrics, intentional-defect catalog, and founder-controlled assessment records.

A directory inside a shared clone is not an access boundary. Do not add trainees to the instructor repository. Do not place instructor-only material in the trainee repository, its Git history, pull requests, issues, Actions logs, or artifacts.

**Current status:** Both repositories have been created and GitHub reports their visibility as **Private**. The founder reports that the access-isolation, acknowledgment-gate and revocation verification checks are complete. Verification evidence and live acknowledgment records belong in founder-controlled private storage, not this public document.

## Prerequisites to invite a trainee

- Founder approves the trainee's identity and intended training access.
- Trainee receives and acknowledges the security terms below **before** a private invitation is sent.
- Founder records the acknowledgment in founder-controlled private storage, never the public application repository.
- Founder verifies that only the trainee-facing repository is being shared, at the lowest available permissions appropriate for completing exercises.

## Required security acknowledgment

Before receiving access, the trainee must affirm:

1. I will keep credentials, tokens, private keys and recovery codes out of repositories, issues, logs and messages; I will use approved secret-handling practices.
2. I will keep nonpublic training content confidential and will not publish, redistribute or upload it to unauthorized services.
3. I will not seek, copy, disclose or share instructor-only materials, answer keys, or defect catalogs.
4. I will promptly report suspected vulnerabilities, exposed secrets or accidental access to restricted content through the founder's designated private channel, without posting details publicly.
5. I understand access is limited to the authorized purpose and can be revoked at any time. I will cease using and remove retained private training copies when instructed, subject to applicable record-retention obligations.
6. I will not use AI assistance during designated manual assessments; AI use outside assessments follows the contribution policy.

Record the acknowledgment version, contributor GitHub handle, date/time, method, founder approval, and training repository access status in private founder-controlled storage. Do not put personal contact information or acknowledgment records in the public repository.

## Invitation procedure

1. Verify acknowledgment status and founder approval.
2. Confirm both repositories are private and independently permissioned.
3. Confirm the trainee has **no** instructor repository access, including through teams or inherited organization permissions.
4. Invite trainee only to `viharbot-onboarding` with minimum sufficient access; avoid granting administrative privileges.
5. Record invitation date, access level, approving founder, and expected review/removal date privately.
6. Verify the trainee can access the intended exercise materials and cannot access instructor-only materials. Use a dedicated non-privileged test account or GitHub permission inspection; do not rely on repository naming alone.
7. Instructor records completion and qualification separately from the founder's trusted-access decision. Qualification does not automatically grant access to `viharbot`.

## Revocation procedure

1. Founder removes the trainee's access to the training repository and any other training resources.
2. Verify removal, including inherited access, active invitations and related credentials.
3. Revoke or rotate any training credentials that might remain usable.
4. Record revocation date, reason category, verifier, and acknowledgment status in private founder-controlled storage.
5. Note that removing GitHub access does not remotely delete an already cloned repository; do not treat revocation as proof that local copies disappeared.

## Validation checklist

- [ ] Trainee repository exists and GitHub reports **Private**.
- [ ] Instructor repository exists and GitHub reports **Private**.
- [ ] Permissions are separate; trainee has no instructor access.
- [ ] No instructor material is present in trainee repository or Git history.
- [ ] Acknowledgment template is stored privately and acceptance is recorded before invitation.
- [ ] Least-privilege invitation and revocation tested.
- [ ] No trainee username is required for initial infrastructure setup.
- [ ] No defects were seeded into the production ViharBot application.

## Related work

- Issue #30: public contribution policy and two-part onboarding overview.
- Issue #33: manual Git proficiency exercise and rubric (instructor-only answer material).
- Issue #35: confidential intentional-defect program.
- Issue #36: first contributor pilot.

**Do not close Issue #32** until private repository creation, visibility, access isolation, and acknowledgment gate have been verified.
