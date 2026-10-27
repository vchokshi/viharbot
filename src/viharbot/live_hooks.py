"""The hooks the orchestration loop calls at real boundaries, and the cancellation signal.

This module depends on nothing else in the package, so the loop can import it without cycles.
LiveReporter's methods do nothing; the stream layer subclasses it to turn each call into a
validated event. A loop run without a reporter behaves exactly as it always has.
"""

from __future__ import annotations

import threading


class OrchestrationCancelled(Exception):
    """The client is gone or the stream was closed. No new work may start."""


class StreamClosed(OrchestrationCancelled):
    """An event could not be delivered, so the stream is treated as disconnected."""


class CancelToken:
    """A request-scoped, cooperative cancellation flag."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def raise_if_set(self) -> None:
        if self._event.is_set():
            raise OrchestrationCancelled("the request was cancelled")


class LiveReporter:
    """Receives facts about what the loop is actually doing. All methods are no-ops here."""

    def planner_started(self, *, decision_number: int, remaining_ms: int) -> None: ...

    def planner_completed(
        self, *, decision_number: int, duration_ms: float, action: str,
        capability: str | None, rationale: str | None, model_alias: str | None,
    ) -> None: ...

    def planner_failed(self, *, decision_number: int, error_code: str, duration_ms: float) -> None: ...

    def delegation_requested(
        self, *, decision_number: int, capability: str, target_identity: str
    ) -> None: ...

    def delegation_authorized(
        self, *, decision_number: int, execution_id: str, capability: str,
        target_identity: str, service: str, result_version: int,
    ) -> None: ...

    def delegation_rejected(
        self, *, decision_number: int, capability: str | None, rejection_code: str
    ) -> None: ...

    def agent_started(
        self, *, decision_number: int, execution_id: str, identity_id: str,
        capability: str, service: str, result_version: int,
    ) -> None: ...

    def agent_completed(
        self, *, decision_number: int, execution_id: str, identity_id: str,
        capability: str, result_version: int, duration_ms: float, model_alias: str | None,
    ) -> None: ...

    def agent_failed(
        self, *, decision_number: int, execution_id: str, identity_id: str,
        capability: str, error_code: str, duration_ms: float,
    ) -> None: ...

    def dependency_recorded(
        self, *, decision_number: int, execution_id: str, consumer: dict, dependencies: list[dict]
    ) -> None: ...

    def evidence_superseded(
        self, *, decision_number: int, execution_id: str, capability: str,
        superseded_version: int, replacement_version: int, affected_results: list[dict],
    ) -> None: ...


NO_REPORTER = LiveReporter()
