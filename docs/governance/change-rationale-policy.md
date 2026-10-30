# Change Rationale Policy

**Status:** Proposed governance standard

## Principle

Every issue must explain **why the work is requested**. Every pull request must explain **why the change was made and why the chosen implementation was selected**.

The rationale must be specific enough that a future contributor can reconstruct the decision without relying on private conversations.

## Issues

Record the motivating problem or opportunity, evidence, why now, impact of inaction, desired outcome, and acceptance criteria. Do not confuse a requested outcome with a mandated implementation.

## Pull requests

Link the motivating issue whenever practical. Explain the chosen approach, meaningful alternatives, trade-offs, validation results, and operational implications. If no alternatives were considered, say why. Distinguish tested behavior from assumptions.

## Exceptions

For urgent fixes, the rationale may be concise, but it must not be absent. For unplanned maintenance, a PR can explain why no preceding issue exists. Trivial changes can use brief explanations, not empty ones.

## Enforcement

GitHub templates prompt contributors but do not enforce completeness on their own. To make this a merge requirement, add a separate automated PR-body validation workflow and configure branch protection or a ruleset to require its successful check. Human review remains responsible for judging the quality of reasoning.

## Scope

Applies to all future issues and pull requests after adoption. Existing open items may be updated incrementally. Do not rewrite historical decisions or invent retrospective rationale.
