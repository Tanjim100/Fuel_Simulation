"""A bounded dead-letter queue for payloads the backend could not use.

PDF §12 asks for "graceful crash recovery" alongside the retry and
degraded-mode behaviour. This module is that recovery path's landing place:
anything that *arrives* but cannot be processed is quarantined here rather
than dropped or raised.

**Why a queue and not an exception.** The classic failure mode of an event
subscriber is that one bad frame kills the subscription, and the operator
never learns why the console stopped updating. A single malformed SSE frame
must not abort the world's event feed. The subscription survives; the bad
frame is quarantined; the operator can see it and replay it.

**Two stores, one queue.**

* **SQLite** at ``/app/data/dlq.sqlite3`` is the durable store. It survives
  a Redis flush and a container restart, which is the whole point of a
  dead-letter queue -- a queue that evaporates on restart is a log, not a
  queue.
* **Redis** mirrors the *current size* and the recent entries, so the
  metrics and the API can answer without touching the disk on the hot path.

The SQLite file is the source of truth; Redis is the fast mirror. When the
two disagree (Redis was flushed), the SQLite count wins.

**Bounded.** The queue is capped at ``max_entries`` (1000 by default). An
unbounded dead-letter queue in a long-running service is a slow memory leak
and an unbounded disk hog -- and a dead-letter queue that grows forever is a
sign that something is systematically broken, which is a page, not a queue.
Oldest entries are evicted first.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .metrics import DLQ_MESSAGES, DLQ_SIZE

LOGGER = logging.getLogger(__name__)

__all__ = ["DeadLetter", "DeadLetterQueue", "REASONS"]

#: The reasons a payload can be dead-lettered. They are a closed set so the
#: metric's ``reason`` label has a bounded cardinality.
REASONS = (
    "malformed_frame",
    "unparseable_payload",
    "schema_mismatch",
    "unknown",
)


@dataclass(frozen=True)
class DeadLetter:
    """One quarantined payload."""

    id: str
    reason: str
    source: str
    payload: str
    received_at: float
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reason": self.reason,
            "source": self.source,
            "payload": self.payload,
            "received_at": self.received_at,
            "attempts": self.attempts,
        }


class DeadLetterQueue:
    """A bounded, on-disk dead-letter queue.

    Thread-safe: the broker's SSE loop, the journal's recorder and the API
    handlers can all touch it, and they may be on different threads under
    ``TestClient``.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_entries: int = 1000,
        redis: Any = None,
    ) -> None:
        """
        :param path: the SQLite file. Its parent directory must exist.
        :param max_entries: the cap. Oldest entries are evicted first.
        :param redis: an optional Redis client used as a fast mirror of the
            current size. Its absence is not an error -- the queue works
            without it.
        """
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self._redis = redis
        self._lock = threading.Lock()
        self._connection: Optional[sqlite3.Connection] = None
        self._count = 0

    # -- storage ------------------------------------------------------------ #
    def connect(self) -> None:
        """Open the database and create the table if it is not there."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dead_letters (
                id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                source TEXT NOT NULL,
                payload TEXT NOT NULL,
                received_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_dead_letters_received "
            "ON dead_letters (received_at)"
        )
        self._connection.commit()
        self._count = self._count_rows()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self.connect()
        assert self._connection is not None
        return self._connection

    def _count_rows(self) -> int:
        row = self._db().execute("SELECT COUNT(*) FROM dead_letters").fetchone()
        return int(row[0]) if row else 0

    # -- producing ---------------------------------------------------------- #
    def put(
        self,
        payload: Any,
        *,
        reason: str = "unknown",
        source: str = "unknown",
    ) -> DeadLetter:
        """Quarantine one payload.

        Never raises. A dead-letter queue that can itself fail is a very bad
        idea: the one thing it must do is accept the thing that broke
        everything else.
        """
        if reason not in REASONS:
            reason = "unknown"
        text = _as_text(payload)
        entry = DeadLetter(
            id=uuid.uuid4().hex,
            reason=reason,
            source=source,
            payload=text,
            received_at=time.time(),
            attempts=0,
        )
        try:
            with self._lock:
                self._db().execute(
                    "INSERT INTO dead_letters "
                    "(id, reason, source, payload, received_at, attempts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        entry.id,
                        entry.reason,
                        entry.source,
                        entry.payload,
                        entry.received_at,
                        entry.attempts,
                    ),
                )
                self._evict()
                self._connection.commit()  # type: ignore[union-attr]
                self._count = self._count_rows()
            DLQ_MESSAGES.inc(reason=entry.reason)
            DLQ_SIZE.set(float(self._count))
            self._mirror_size()
        except Exception as exc:  # pragma: no cover - defensive
            # The queue must never be the reason the service dies.
            LOGGER.exception("Could not dead-letter a payload: %s", exc)
        return entry

    def _evict(self) -> None:
        """Drop the oldest entries once the cap is exceeded."""
        excess = self._count_rows() - self.max_entries
        if excess <= 0:
            return
        self._db().execute(
            "DELETE FROM dead_letters WHERE id IN ("
            "  SELECT id FROM dead_letters ORDER BY received_at ASC LIMIT ?"
            ")",
            (excess,),
        )

    # -- consuming ---------------------------------------------------------- #
    def entries(self, *, limit: int = 100) -> list[DeadLetter]:
        """The most recent entries, newest first."""
        try:
            with self._lock:
                rows = self._db().execute(
                    "SELECT id, reason, source, payload, received_at, attempts "
                    "FROM dead_letters ORDER BY received_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Could not read the dead-letter queue: %s", exc)
            return []
        return [
            DeadLetter(
                id=row[0], reason=row[1], source=row[2], payload=row[3],
                received_at=float(row[4]), attempts=int(row[5]),
            )
            for row in rows
        ]

    def get(self, entry_id: str) -> Optional[DeadLetter]:
        try:
            with self._lock:
                row = self._db().execute(
                    "SELECT id, reason, source, payload, received_at, attempts "
                    "FROM dead_letters WHERE id = ?",
                    (entry_id,),
                ).fetchone()
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Could not read the dead-letter queue: %s", exc)
            return None
        if row is None:
            return None
        return DeadLetter(
            id=row[0], reason=row[1], source=row[2], payload=row[3],
            received_at=float(row[4]), attempts=int(row[5]),
        )

    def resolve(self, entry_id: str, *, success: bool) -> bool:
        """Record a replay attempt.

        A successful replay removes the entry -- it is no longer a dead
        letter, it is a handled payload. A failed replay increments the
        attempt counter and leaves the entry in place, because it still has
        not been handled.
        """
        try:
            with self._lock:
                if success:
                    cursor = self._db().execute(
                        "DELETE FROM dead_letters WHERE id = ?", (entry_id,)
                    )
                    changed = cursor.rowcount > 0
                else:
                    cursor = self._db().execute(
                        "UPDATE dead_letters SET attempts = attempts + 1 "
                        "WHERE id = ?",
                        (entry_id,),
                    )
                    changed = cursor.rowcount > 0
                self._connection.commit()  # type: ignore[union-attr]
                self._count = self._count_rows()
            DLQ_SIZE.set(float(self._count))
            self._mirror_size()
            return changed
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Could not resolve the dead-letter entry: %s", exc)
            return False

    def __len__(self) -> int:
        return self._count

    @property
    def count(self) -> int:
        return self._count

    # -- the Redis mirror --------------------------------------------------- #
    def _mirror_size(self) -> None:
        """Mirror the current size into Redis, if there is a Redis.

        The mirror is best-effort. The queue works without Redis, and a
        Redis outage must not stop the queue from accepting payloads.
        """
        if self._redis is None:
            return
        try:
            self._redis.set("backend:dlq:size", str(self._count))
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.debug("Could not mirror the DLQ size to Redis: %s", exc)


def _as_text(payload: Any) -> str:
    """Render a payload as text, without ever raising."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes):
        try:
            return payload.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            return repr(payload)
    try:
        return json.dumps(payload, default=str)
    except Exception:
        try:
            return str(payload)
        except Exception:  # pragma: no cover - defensive
            return "<unprintable>"
