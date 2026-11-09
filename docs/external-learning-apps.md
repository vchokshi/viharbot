# External learning applications

Professor Archibald (Archie) and Dr. Ada Lovewell (Ada) are **separate applications**, developed in
[vchokshi/professor-archibald](https://github.com/vchokshi/professor-archibald) and
[vchokshi/dr-ada-lovewell](https://github.com/vchokshi/dr-ada-lovewell). They were moved out of ViharBot
under issue #293. ViharBot hosts none of their code, identities, curricula or UI.

## How they relate to ViharBot

- **Archie reads ViharBot over GitHub.** It reads merged pull requests as untrusted data through the GitHub
  API, with its own tokens. ViharBot does not import Archie and does not know when it runs.
- **Sage's review comment is an external contract.** Archie parses Sage's pull-request comment from GitHub,
  only from an allowlisted bot author. It recognises the hidden markers (`<!-- sage-code-review:v1 -->`,
  reviewed-sha and status) and the section headings written by `sage_review_format.py`. **If you change
  that comment format, update `sage_markers.py`, `learning/sage_evidence.py` and `tests/sage_fixture.py` in
  Archie.** Neither repository has a test that catches the other's drift automatically.
- **Archie to Ada** uses a versioned delivery package (`PACKAGE_SCHEMA = "1.0"`) owned by Archie.
- **Kepler.** Kepler stays in ViharBot. The earlier plan for Kepler to hand material to Archie was removed;
  a future handoff will be designed as an explicit external contract.

## Intentional retentions

- `.gitleaksignore` entries that name `tests/test_archie_confidentiality.py` stay: they are fingerprints for
  commits that remain in history.
- Closed issues and pull requests, including #139 to #145 and #154, remain as historical records.
- `src/viharbot/knowledge/` is a generic institutional-memory interface and stays.
