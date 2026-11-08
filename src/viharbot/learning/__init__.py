"""Archie's learning pipeline and curriculum workflow (education agents, epic #139).

Reads merged ViharBot pull requests as untrusted data, extracts provenance-aware learning artifacts
with deterministic code, and, under an explicit founder assignment, turns that evidence into a lesson
draft that a human reviews, a founder publishes and Ada can retrieve as a scripted, trainee-safe
package. A separate path prepares student feedback after a verified Sage review.

Nothing here merges, deploys or is reachable from chat, orchestration or the web app. A model is called
only if a caller passes a client to `synthesis.synthesize`; the provided client uses the external
LiteLLM gateway and is off unless `ARCHIE_MODEL_ALIAS` is set. See docs/education-agents.md.
"""
