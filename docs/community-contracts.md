# Community interaction contracts

**Status: Phases C1, C2 and C3 of Epic #157 (issues #209, #210, #211).** The contracts (C1) are data only. C2 adds declared review edges and, behind `VIHARBOT_PEER_REVIEW_ENABLED` (off by default, set nowhere), has the iterative orchestrator **record** a validated review exchange for a delegation it already authorized. Only `orchestration_state.py` and `iterative_execution.py` import this package (tested). See [peer-review edges](#peer-review-edges-c2) below and the [persona and community specification](persona-community-specification.md).

## What they are for

Constitution [5.3 to 5.6](governance/agent-constitution-v1.md) require structured agent communication, a delegation contract, evidence-based disagreement with five named parts, and disclosure of unresolved disagreement. These models make a message that breaks those rules **unrepresentable**, so a later orchestrator can validate instead of trusting free text.

## Messages

All share an envelope: `message_id`, `engagement_id`, `task_id` (the parent task), `request_id` (the existing per-request id used in logs), `sender`, `recipient`, optional `in_reply_to`, and a `message_type` that selects the model.

| Type | Purpose | Key rules |
|---|---|---|
| `review_request` | Ask a reviewer to independently review an author's work | `author` is named and must differ from the reviewer (`recipient`); one or more `artifact_refs`; carries `objective`, `scope`, `completion_criteria` and `escalation_conditions` (delegation contract, 5.3) |
| `review_report` | The reviewer's answer | Must reply to a request. `issues_found` needs findings; `no_issues_found` and `unable_to_review` allow none; `unable_to_review` needs a reason; `limitations` are always required (5.8). Every finding must cite evidence |
| `challenge` | Constructive disagreement | All five parts of 5.5 are required: disputed conclusion, reason, evidence (at least one reference), potential consequence, proposed resolution. Only an agent can challenge, not the application |
| `challenge_resolution` | The outcome | Must reply to a challenge. A decided outcome (`accepted`, `partly_accepted`, `rejected`) must cite evidence. `unresolved` + `material` requires an `escalation` (5.6) |

`sender` is a registered identity, or `application`. The application can **send review requests and receive review reports, and nothing else** (it never disagrees), so a request the application originates has a representable, valid reply: the report goes back to `application`. Every other recipient, and `author`, must be a registered identity. Sender and recipient must differ.

`validate_reply(original, reply)` checks that a reply really answers its original: right reply type, `in_reply_to` equals the original's `message_id`, same engagement, task and request id, sent by the original recipient back to the original sender. This blocks forged or crossed replies.

## What a message cannot do

* **Grant authority.** Every model rejects unknown fields, so a message cannot carry tools, permissions, scopes, model aliases, approvals, trust scores or seniority. Tests cover each, including inside nested objects.
* **Set a budget.** Execution budgets belong to application policy. A message cannot request or raise one.
* **Prove a claim.** An evidence reference is a structural citation of material supplied in the engagement. Requiring one stops an unsupported challenge from being expressed; it does not make the cited claim true. Whether a challenge is sound is a judgment for the reviewer, the orchestrator and, if material, the user.
* **Instruct its reader.** Message contents are untrusted data to whoever receives them.

## Parsing

`parse_message(dict)` and `parse_message_json(str | bytes)` return the right model or raise `ContractError`. JSON over 32 KiB is rejected before decoding. Models are frozen. **Errors never contain sender-supplied text:** field names the contracts do not define become `<unknown>`, pydantic's own messages (which can quote an input such as a bad `message_type`) are replaced by fixed text, and only this module's own static validator messages pass through.

## Peer-review edges (C2)

A **review edge** says: a delegated run of a *review capability* on a fresh result of an *artifact capability* counts as an independent peer review of that result. It adds no engine and no execution path.

| Piece | What it does |
|---|---|
| `execution_config/review_edges.toml` | The only place edges are declared. Shipped: Nova's research reviewed by Scout (`fact_check`, `source_quality`) and by Cipher (`security_assessment`). Edges name capabilities, never agents or services |
| Edge validation (load time) | Both capabilities implemented, orchestrated and **bound and enabled**; the reviewer's capability actually *consumes* the artifact; a report can be derived for it; and **the reviewer's identity differs from the author's**, looked up from the approved bindings so changing a binding cannot create a self-review |
| `authorize_review` | Rejects a request unless the application sent it, it matches this request, task and engagement, it names exactly one **fresh, current** artifact (`<capability>:v<n>`), the declared author and reviewer match trusted state and bindings, and the edge exists. It then calls the **existing `authorize_action`**, so binding, dependencies, freshness, the repeat limit and the execution, decision and time budgets apply unchanged |
| `derive_report` | Builds the `ReviewReport` deterministically from the reviewer's already-validated structured result. **No model call, nothing generated:** each finding maps to a field the reviewing service returned (a contradicted claim, a weak or unknown source, a security implication), cites the reviewed artifact, and clips hostile or oversized text to the contract. A quote is attached only when the application itself verified the citation. A report carries at most 20 findings, **most severe first**; anything omitted is counted in `omitted_findings` and `omitted_material_findings` and noted in the limitations, so a high or critical finding is never the one dropped and the material total stays accurate |
| Loop integration | With the flag on, after the existing authorization succeeds the loop plans a request, and after the delegation completes it records a `ReviewExchange` (request plus the report that provably answers it) in per-request state. Planning or recording failures are logged and ignored: they never fail, delay or change a delegation |

Because a review *is* a delegation, cancellation, the request deadline, budgets and repeat limits behave exactly as for any other step. Tests show the flag changes nothing about which services are called, their payloads, the returned result or the decision history.

**Disclosure (Constitution 5.6).** The completion log carries `peer_reviews` and `peer_review_material_findings` (high or critical). The user-facing `/api/chat` response and SSE events are unchanged, so material findings reach the user only through what already reaches them (the fact check and security results already feed synthesis). Disclosing them in the response or stream needs a contract change and belongs to E1.

## Relationship records (C3)

`community/relationships.py` records **collaboration history**: counts of what happened between two agents in an engagement. It is deliberately not part of the package `__init__` and is imported by no runtime module.

* **The pairing is configured, not invented.** A relationship exists only where a persona's `collaboration` entry declares it. The ledger records a review as the reviewer's `reviews` relationship with the author (for example Scout reviews Nova), and refuses (`RelationshipError`) a pairing the personas do not declare.
* **Counts only.** A `RelationshipRecord` is `schema_version`, `engagement_id`, `from_agent`, `to_agent`, `pattern` and `OutcomeCounts` (reviews completed, no issues, issues found, unable to review, findings, material findings, with omitted findings included). Counts are validated to be consistent. There is no free text, so nothing in it is an opinion.
* **Not a trust level or an access right.** No score, rank, seniority, weight, permission, scope, approval or tool field exists, and unknown fields are rejected. Tests check the field names, and check structurally that no authorization, delegation, binding, budget or review-policy module mentions or imports relationships, that no authorization function can be given one, and that the ledger does no I/O. Past outcomes, personality and perceived seniority therefore cannot bypass authorization.
* **In memory only.** The ledger is bounded (256 records) and lives as long as the caller holds it. Durable storage, retention, correction and deletion are Phase D and ADR-009. The engagement id is the `unscoped` placeholder until real engagement scoping exists.
* **Not wired in.** Nothing records into the ledger during a request yet. It is the history model that Phase D's memory interfaces will persist.

## Deliberately not here

* **Challenges and resolutions in the loop.** No current service produces a challenge, so generating one would need a new model call. `Challenge` and `ChallengeResolution` remain validated contracts only. Tracked as a follow-up.
* **Application-initiated reviews.** C2 records reviews of delegations Charlie recommends. Having the application schedule a review on its own (so Charlie does not have to relay each one) is a follow-up, because it changes what the loop runs.
* **Sage review edge.** Sage's analysis is not mapped to findings yet; only the three edges above exist. Atlas and Orbit have no service, so no edge involves them.
* Durable storage of exchanges and relationship history (Phase D, ADR-009); community SSE events and a response-level disclosure (E1); validation that evidence is relevant or true (a behavioral concern for evaluation, not a schema).
* **Authentication.** `validate_reply` and `authorize_review` check declared fields against trusted in-process state. They are not authentication of a remote sender; any future agent-to-agent transport must bind identity to a trusted execution context.
