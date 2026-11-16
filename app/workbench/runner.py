"""Executor loop / job records.

The executor is deliberately a separate process from the workbench web server:
that is what lets us simulate it dying mid-submission and prove that recovery
does not assume success. It may also be run twice concurrently; the lease
table in the database is the mutex and the lease revocation check between
steps is the barrier against overwriting a human takeover.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from ..common import iso, json_dumps
from . import engine
from .portal_client import PortalError


@dataclass
class RunSummary:
    executor_id: str
    job_id: int
    processed: int = 0
    applied: int = 0
    noop: int = 0
    retry_wait: int = 0
    conflicts: int = 0
    invalid: int = 0
    failed: int = 0
    skipped: int = 0
    results: list[tuple[int, str, str]] = field(default_factory=list)

    def add(self, row_id: int, key: str, status: str) -> None:
        self.processed += 1
        mapping = {
            engine.ST_APPLIED: "applied",
            engine.ST_NOOP: "noop",
            engine.ST_RETRY_WAIT: "retry_wait",
            engine.ST_CONFLICT: "conflicts",
            engine.ST_INVALID: "invalid",
            engine.ST_FAILED: "failed",
            engine.ST_NEEDS_RECONCILE: "failed",
        }
        attr = mapping.get(status)
        if attr:
            setattr(self, attr, getattr(self, attr) + 1)
        self.results.append((row_id, key, status))


def new_executor_id() -> str:
    return f"exec_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def create_job(conn: sqlite3.Connection, kind: str, payload: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO wb_jobs(kind, status, payload, fault_profile, started_at, heartbeat_at)"
        " VALUES(?,?,?,?,?,?)",
        (
            kind,
            "running",
            json_dumps(payload or {}),
            engine.get_fault_profile(conn),
            iso(),
            iso(),
        ),
    )
    return cur.lastrowid


def finish_job(conn: sqlite3.Connection, job_id: int, status: str, result: dict | None = None) -> None:
    conn.execute(
        "UPDATE wb_jobs SET status=?, result=?, finished_at=? WHERE id=?",
        (status, json_dumps(result or {}), iso(), job_id),
    )


def run_batch(
    conn: sqlite3.Connection,
    batch_id: int | None = None,
    limit: int | None = None,
    timeout: float = 2.5,
    max_attempts: int = 3,
    max_rounds: int = 6,
) -> dict:
    """Run all actionable rows in a batch, retrying safe failures.

    Rows with an unknown outcome stop the loop for that row until recovery
    reconciles them; there is no blind retry.
    """
    executor_id = new_executor_id()
    job_id = create_job(
        conn, "run_batch",
        {"batch_id": batch_id, "limit": limit, "timeout": timeout, "max_attempts": max_attempts},
    )
    summary = RunSummary(executor_id=executor_id, job_id=job_id)
    attempted: dict[int, int] = {}
    try:
        for round_no in range(max_rounds):
            rows = engine.pending_rows(conn, batch_id)
            if not rows:
                break
            progressed = False
            for raw in rows:
                if limit is not None and summary.processed >= limit:
                    break
                row_id = raw["id"]
                count = attempted.get(row_id, 0)
                if count >= max_attempts:
                    continue
                if not engine.acquire_lease(conn, row_id, executor_id, job_id):
                    summary.skipped += 1
                    continue
                attempted[row_id] = count + 1
                try:
                    result = engine.execute_row(
                        conn, row_id, executor_id, job_id=job_id, timeout=timeout
                    )
                except engine.RowBusy:
                    summary.skipped += 1
                    continue
                except PortalError as exc:
                    summary.failed += 1
                    summary.results.append((row_id, raw["record_key"], f"error:{exc.kind}"))
                    continue
                finally:
                    engine.release_lease(conn, executor_id)
                status = result.get("status", "unknown")
                summary.add(row_id, raw["record_key"], status)
                if status == engine.ST_RETRY_WAIT:
                    progressed = True
                elif status in {engine.ST_PENDING, engine.ST_RUNNING}:
                    progressed = True
            if limit is not None and summary.processed >= limit:
                break
            if not progressed:
                break
        status = "done"
    except KeyboardInterrupt:
        status = "interrupted"
        finish_job(conn, job_id, status, {"processed": summary.processed})
        engine.release_lease(conn, executor_id, "offline")
        raise
    finish_job(conn, job_id, status, {
        "processed": summary.processed,
        "applied": summary.applied,
        "noop": summary.noop,
        "retry_wait": summary.retry_wait,
        "conflicts": summary.conflicts,
        "invalid": summary.invalid,
        "failed": summary.failed,
        "skipped": summary.skipped,
    })
    engine.release_lease(conn, executor_id)
    return {
        "ok": True,
        "job_id": job_id,
        "executor_id": executor_id,
        "processed": summary.processed,
        "applied": summary.applied,
        "noop": summary.noop,
        "retry_wait": summary.retry_wait,
        "conflicts": summary.conflicts,
        "invalid": summary.invalid,
        "failed": summary.failed,
        "skipped": summary.skipped,
        "results": summary.results,
    }


def run_one(conn: sqlite3.Connection, row_id: int, timeout: float = 2.5) -> dict:
    executor_id = new_executor_id()
    job_id = create_job(conn, "run_one", {"row_id": row_id, "timeout": timeout})
    if not engine.acquire_lease(conn, row_id, executor_id, job_id):
        finish_job(conn, job_id, "skipped", {"reason": "lease_held"})
        return {"ok": False, "error": "该行正被其他执行器处理"}
    try:
        result = engine.execute_row(conn, row_id, executor_id, job_id=job_id, timeout=timeout)
        finish_job(conn, job_id, "done", result)
        return {"ok": True, **result}
    except engine.RowBusy as exc:
        finish_job(conn, job_id, "abandoned", {"reason": str(exc)})
        return {"ok": False, "error": str(exc)}
    finally:
        engine.release_lease(conn, executor_id)
