"""Write-ahead dispatch journal.

Every write to the simulator is recorded **before** it is attempted, and the
result is recorded after. If the backend crashes between the two, the intent is
still on disk -- and because the intent carries the idempotency key, replaying
it verbatim produces the simulator's idempotent ``201``, never a duplicate
shipment.

Storage is SQLite on a named Docker volume. It is intentionally synchronous
(it is a tiny local file and the write must complete before the network call)
but every public method is exposed as a coroutine so callers stay uniform.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["JournalIntent", "DispatchPlan", "DispatchJournal"]

LOGGER = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_intents (
    idempotency_key TEXT PRIMARY KEY,
    payload_json    TEXT NOT NULL,
    created_at      REAL NOT NULL,
    status          TEXT NOT NULL,
    result_json     TEXT,
    allocation_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON dispatch_intents(status);

CREATE TABLE IF NOT EXISTS dispatch_plans (
    plan_id       TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    tick          INTEGER NOT NULL,
    horizon       INTEGER NOT NULL,
    solver_used   TEXT NOT NULL,
    status        TEXT NOT NULL,
    payload_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_created ON dispatch_plans(created_at DESC);
"""


@dataclass(frozen=True)
class JournalIntent:
    """A recorded dispatch intent."""

    idempotency_key: str
    payload: dict[str, Any]
    created_at: float
    status: str
    result: Optional[dict[str, Any]] = None
    allocation_id: Optional[int] = None


@dataclass(frozen=True)
class DispatchPlan:
    """A recorded optimiser plan.

    The plan is recorded **once, before any shipment in it is dispatched**.
    That gives an audit trail of what was *intended* -- the individual
    ``dispatch_intents`` rows only prove what was attempted, not what the
    planner had in mind when it decided.

    This is additive: ``dispatch_intents`` is untouched, so the Phase 1
    replay path is unaffected.
    """

    plan_id: str
    created_at: float
    tick: int
    horizon: int
    solver_used: str
    status: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "tick": self.tick,
            "horizon": self.horizon,
            "solver_used": self.solver_used,
            "status": self.status,
            "payload": self.payload,
        }


class DispatchJournal:
    """Crash-safe record of dispatch intents."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # -- lifecycle ---------------------------------------------------------- #
    def connect(self) -> None:
        """Open (and migrate) the database. Safe to call repeatedly."""
        if self._conn is not None:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        LOGGER.info("Dispatch journal opened at %s", self.path)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    # -- writes -------------------------------------------------------------- #
    async def record_intent(
        self, idempotency_key: str, payload: dict[str, Any]
    ) -> None:
        """Record the intent to dispatch, *before* the simulator is called."""
        await asyncio.to_thread(self._record_intent_sync, idempotency_key, payload)

    def _record_intent_sync(
        self, idempotency_key: str, payload: dict[str, Any]
    ) -> None:
        conn = self._require()
        with self._lock:
            conn.execute(
                "INSERT OR REPLACE INTO dispatch_intents "
                "(idempotency_key, payload_json, created_at, status) "
                "VALUES (?, ?, ?, ?)",
                (
                    idempotency_key,
                    json.dumps(payload, sort_keys=True),
                    time.time(),
                    "PENDING",
                ),
            )
            conn.commit()

    async def record_result(
        self,
        idempotency_key: str,
        *,
        status: str,
        result: Optional[dict[str, Any]] = None,
        allocation_id: Optional[int] = None,
    ) -> None:
        """Record the outcome of a dispatch attempt."""
        await asyncio.to_thread(
            self._record_result_sync,
            idempotency_key,
            status,
            result,
            allocation_id,
        )

    def _record_result_sync(
        self,
        idempotency_key: str,
        status: str,
        result: Optional[dict[str, Any]],
        allocation_id: Optional[int],
    ) -> None:
        conn = self._require()
        with self._lock:
            conn.execute(
                "UPDATE dispatch_intents "
                "SET status = ?, result_json = ?, allocation_id = ? "
                "WHERE idempotency_key = ?",
                (
                    status,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    allocation_id,
                    idempotency_key,
                ),
            )
            conn.commit()

    # -- plans --------------------------------------------------------------- #
    async def record_plan(
        self,
        plan_id: str,
        *,
        tick: int,
        horizon: int,
        solver_used: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        """Record an optimiser plan, before any of its shipments is dispatched."""
        await asyncio.to_thread(
            self._record_plan_sync,
            plan_id,
            tick,
            horizon,
            solver_used,
            status,
            payload,
        )

    def _record_plan_sync(
        self,
        plan_id: str,
        tick: int,
        horizon: int,
        solver_used: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        conn = self._require()
        with self._lock:
            conn.execute(
                "INSERT OR REPLACE INTO dispatch_plans "
                "(plan_id, created_at, tick, horizon, solver_used, status, "
                "payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    plan_id,
                    time.time(),
                    tick,
                    horizon,
                    solver_used,
                    status,
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )
            conn.commit()

    async def find_plan(self, plan_id: str) -> Optional[DispatchPlan]:
        """Fetch one recorded plan by id."""
        return await asyncio.to_thread(self._find_plan_sync, plan_id)

    def _find_plan_sync(self, plan_id: str) -> Optional[DispatchPlan]:
        conn = self._require()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM dispatch_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        return DispatchPlan(
            plan_id=row["plan_id"],
            created_at=row["created_at"],
            tick=row["tick"],
            horizon=row["horizon"],
            solver_used=row["solver_used"],
            status=row["status"],
            payload=json.loads(row["payload_json"]),
        )

    async def list_plans(self, limit: int = 50) -> list[DispatchPlan]:
        """The most recent plans, newest first."""
        return await asyncio.to_thread(self._list_plans_sync, limit)

    def _list_plans_sync(self, limit: int) -> list[DispatchPlan]:
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM dispatch_plans ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [
            DispatchPlan(
                plan_id=row["plan_id"],
                created_at=row["created_at"],
                tick=row["tick"],
                horizon=row["horizon"],
                solver_used=row["solver_used"],
                status=row["status"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    # -- reads --------------------------------------------------------------- #
    async def pending_intents(self) -> list[JournalIntent]:
        """Intents recorded but never resolved -- these must be replayed."""
        return await asyncio.to_thread(self._pending_intents_sync)

    def _pending_intents_sync(self) -> list[JournalIntent]:
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT * FROM dispatch_intents WHERE status = 'PENDING' "
                "ORDER BY created_at ASC"
            ).fetchall()
        return [
            JournalIntent(
                idempotency_key=row["idempotency_key"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
                status=row["status"],
                result=(
                    json.loads(row["result_json"]) if row["result_json"] else None
                ),
                allocation_id=row["allocation_id"],
            )
            for row in rows
        ]

    async def find(self, idempotency_key: str) -> Optional[JournalIntent]:
        return await asyncio.to_thread(self._find_sync, idempotency_key)

    def _find_sync(self, idempotency_key: str) -> Optional[JournalIntent]:
        conn = self._require()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM dispatch_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return JournalIntent(
            idempotency_key=row["idempotency_key"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            status=row["status"],
            result=(json.loads(row["result_json"]) if row["result_json"] else None),
            allocation_id=row["allocation_id"],
        )

    async def stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> dict[str, Any]:
        conn = self._require()
        with self._lock:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM dispatch_intents "
                "GROUP BY status"
            ).fetchall()
        by_status = {row["status"]: row["n"] for row in rows}
        return {
            "path": self.path,
            "total": sum(by_status.values()),
            "by_status": by_status,
        }
