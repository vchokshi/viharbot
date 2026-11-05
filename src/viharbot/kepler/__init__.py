"""Kepler: governed coding agent, assignment and workspace workflow (issue #128).

This package is deliberately independent of the other agents and makes no model call; inference
for the agent loop that will drive it belongs to #127 and must go through the external LiteLLM
gateway. Nothing here is wired into compose, Dockerfiles or deploy files, and there is no
module-level app.
"""
