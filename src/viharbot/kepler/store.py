"""A small JSON-file store with atomic, durable writes. No database.

The same technique as the deployment controller's store, copied rather than imported so the two
services stay independently deployable. Assumes a POSIX filesystem where fsync of the containing
directory persists a rename.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path


class StoreError(Exception):
    """A durable read or write failed. Callers must not proceed with the guarded action."""


class RunStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._lockfile = self._path.with_name(self._path.name + ".lock")
        self._depth = 0
        self._fd: int | None = None
        self.data: dict = {"runs": {}, "events": []}
        self._reload()

    def _reload(self) -> None:
        fresh = {"runs": {}, "events": []}
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.data = fresh  # first start: no runs exist
            return
        except OSError as exc:
            raise StoreError("store read failed") from exc
        try:
            loaded = json.loads(text)
        except ValueError as exc:
            # Unlike a lost approval, a lost run could re-authorize work or reset a counter, so a
            # corrupt store stops the service rather than being treated as empty.
            raise StoreError("store is corrupt") from exc
        if not (isinstance(loaded, dict) and all(k in loaded for k in fresh)):
            raise StoreError("store is corrupt")
        self.data = loaded

    @contextmanager
    def locked(self):
        """Exclusive access across threads, store instances and processes; reentrant.

        An advisory file lock is taken and the file re-read, so a read-modify-write always starts
        from the latest committed state.
        """
        with self._lock:
            if self._depth == 0:
                try:
                    self._fd = os.open(self._lockfile, os.O_RDWR | os.O_CREAT, 0o600)
                    fcntl.flock(self._fd, fcntl.LOCK_EX)
                except OSError as exc:
                    if self._fd is not None:
                        os.close(self._fd)
                        self._fd = None
                    raise StoreError("store lock failed") from exc
                try:
                    self._reload()
                except BaseException:
                    os.close(self._fd)
                    self._fd = None
                    raise
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    os.close(self._fd)  # closing the descriptor releases the lock
                    self._fd = None

    def get_run(self, run_id: str) -> dict | None:
        with self.locked():
            record = self.data["runs"].get(run_id)
        return copy.deepcopy(record) if record is not None else None

    def runs(self) -> list[dict]:
        with self.locked():
            return [copy.deepcopy(r) for r in self.data["runs"].values()]

    def events(self) -> list[dict]:
        with self.locked():
            return copy.deepcopy(self.data["events"])

    def commit(self, *, run: dict | None = None, events: list[dict] | None = None) -> None:
        """Apply a run update and its audit events in one atomic write; on failure nothing changes."""
        with self.locked():
            new = copy.deepcopy(self.data)
            if run is not None:
                new["runs"][run["run_id"]] = copy.deepcopy(run)
            new["events"].extend(copy.deepcopy(events or []))
            self._write(new)
            self.data = new

    def _write(self, data: dict) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise StoreError("store write failed") from exc
