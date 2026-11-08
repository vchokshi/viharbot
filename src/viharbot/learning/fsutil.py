"""Small file helpers shared by the learning stores: an exclusive lock and an atomic write.

The lock is an advisory `flock` on a sidecar file, so it serialises read-modify-write cycles between
threads and between processes on one host. It is not a distributed lock (no database by design,
see AGENTS.md). Where `fcntl` is unavailable the lock degrades to a process-local one.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_LOCAL = threading.RLock()
_HELD = threading.local()


@contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """Exclusive, re-entrant lock for the given sidecar path."""
    held = getattr(_HELD, "paths", None)
    if held is None:
        held = _HELD.paths = set()
    key = str(lock_path)
    if key in held:  # re-entrant within one thread
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCAL if fcntl is None else _noop():
        handle = open(lock_path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            held.add(key)
            yield
        finally:
            held.discard(key)
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


@contextmanager
def _noop() -> Iterator[None]:
    yield


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
