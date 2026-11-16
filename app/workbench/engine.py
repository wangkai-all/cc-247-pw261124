"""Workbench core: CSV import, attempt execution, reconciliation and recovery.

Safety invariants implemented here:

* A retry never reuses a previous portal form session; the executor opens a
  fresh form. But a fresh form alone would silently overwrite a human edit, so
  every attempt first compares the portal's current values with the stored
  pre-submission base:
      no field is "他人修改" / "冲突"  → safe to submit
      any such field                  → conflict queue, never auto-submit
* A submit whose outcome is unknown (timeout, dropped receipt, 5xx, crash) is
  reconciled against the portal *before* any retry. It is never retried blind
  and never marked successful on assumption.
* Before the first submit the workbench stores a full pre-submission snapshot
  (base) and the form token. Recovery compares field by field and uses the
  portal revision's session token to prove whether *this* submission landed.
* Taking over a row revokes all executor leases; the executor re-checks its
  lease between every network step and refuses to submit once revoked. A
  manual submission also revokes leases before it starts. Two executors (or an
  executor and a human) therefore cannot overwrite each other.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from typing import Any

from .. import storage
from ..common import (
    attributed_diff,
    field_diff,
    iso,
    json_dumps,
    json_loads,
    normalize_fields,
    parse_csv_rows,
    utcnow,
    validate_fields,
    validate_record_key,
)
from ..portal import faults as portal_faults
from .portal_client import PortalClient, PortalError, PortalHTTP

# Row statuses
ST_PENDING = "pending"
ST_RUNNING = "running"
ST_RETRY_WAIT = "retry_wait"
ST_NEEDS_RECONCILE = "needs_reconcile"
ST_APPLIED = "applied"
ST_RESOLVED = "resolved"
ST_CONFLICT = "conflict"
ST_FAILED = "failed"
ST_INVALID = "invalid"
ST_MISSING = "missing"
ST_NOOP = "noop"

AUTOMATIC = {ST_PENDING, ST_RETRY_WAIT}
FINAL = {ST_APPLIED, ST_RESOLVED, ST_NOOP, ST_INVALID, ST_MISSING}
LEASE_SECONDS = 20

DEFAULT_PORTAL_URL = "http://127.0.0.1:8801"


# ---------------------------------------------------------------------------
# Settings / clients
# ---------------------------------------------------------------------------


def get_portal_url(conn: sqlite3.Connection) -> str:
    return storage.get_setting(conn, "portal_url", DEFAULT_PORTAL_URL)


def set_portal_url(conn: sqlite3.Connection, url: str) -> None:
    storage.set_setting(conn, "portal_url", url.rstrip("/"))


def get_fault_profile(conn: sqlite3.Connection) -> str:
    return storage.get_setting(conn, "fault_profile", portal_faults.DEFAULT_PROFILE)


def client_for(conn: sqlite3.Connection, row: dict | None = None, timeout: float = 2.5) -> PortalClient:
    return PortalClient(
        get_portal_url(conn),
        fault_profile=get_fault_profile(conn),
        timeout=timeout,
        row_number=row["row_number"] if row else None,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_csv(
    conn: sqlite3.Connection, text: str, batch_name: str = "", precheck: bool = True
) -> dict:
    rows, fatal = parse_csv_rows(text)
    if fatal:
        return {"ok": False, "errors": fatal, "batch_id": None}
    if not rows:
        return {"ok": False, "errors": ["CSV 没有数据行"], "batch_id": None}
    now = iso()
    cur = conn.execute(
        "INSERT INTO wb_batches(name, created_at, row_count) VALUES(?,?,?)",
        (batch_name or ("导入 " + now[:19]), now, len(rows)),
    )
    batch_id = cur.lastrowid
    seen: dict[str, int] = {}
    invalid = 0
    conn.execute("BEGIN")
    for index, raw in enumerate(rows, 1):
        key = (raw.get("record_key") or "").strip().upper()
        fields = normalize_fields(raw)
        errors: list[str] = []
        key_error = validate_record_key(key)
        if key_error:
            errors.append(key_error)
        errors.extend(f"{v}" for v in validate_fields(fields).values())
        if key and key in seen:
            errors.append(f"与本批次第 {seen[key]} 行业务编号重复")
        elif key:
            seen[key] = index
        if key and not key_error:
            duplicate = conn.execute(
                "SELECT b.name, r.row_number FROM wb_rows r JOIN wb_batches b ON b.id=r.batch_id"
                " WHERE r.record_key=? AND r.status NOT IN ('applied','resolved','noop','invalid',"
                "'missing','failed') ORDER BY r.id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if duplicate:
                errors.append(
                    f"该业务编号在批次「{duplicate['name']}」第 {duplicate['row_number']} 行"
                    " 仍有未完成记录，请先处理"
                )
        status = ST_INVALID if errors else ST_PENDING
        if errors:
            invalid += 1
        conn.execute(
            "INSERT INTO wb_rows(batch_id, row_number, record_key, target, status, compare_status,"
            " error_summary, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                index,
                key or f"<空>#{index}",
                json_dumps(fields),
                status,
                "unchecked",
                "；".join(dict.fromkeys(errors)),
                now,
                now,
            ),
        )
    conn.execute("COMMIT")
    storage.event(
        conn, "workbench", "import_batch",
        f"批次 #{batch_id}「{batch_name or now[:19]}」导入 {len(rows)} 行，{invalid} 行无效",
    )
    prechecked = precheck_batch(conn, batch_id, only_pending=True) if precheck else None
    return {
        "ok": True,
        "batch_id": batch_id,
        "rows": len(rows),
        "invalid": invalid,
        "errors": [],
        "precheck": prechecked,
    }


def precheck_row(conn: sqlite3.Connection, row: dict) -> dict:
    """Snapshot the portal's current values as the row's pre-submission base.

    This is the baseline every later comparison uses. If the portal is
    unreachable the row keeps ``base_snapshot IS NULL`` and the first attempt
    falls back to the values it sees when it opens the form (still safe against
    edits between open and submit thanks to the portal's version check).
    """
    client = client_for(conn, row)
    try:
        data = client.get_record(row["record_key"])
    except PortalHTTP as exc:
        if exc.status == 404:
            set_row_state(conn, row["id"], ST_MISSING, compare="missing",
                          error="门户中不存在该业务编号，未提交")
            return {"ok": False, "status": ST_MISSING, "message": "门户无此记录"}
        return {"ok": False, "status": None, "message": exc.message}
    except PortalError as exc:
        return {"ok": False, "status": None, "message": exc.message}
    current = normalize_fields(data.get("fields"))
    target = normalize_fields(json_loads(row["target"], {}))
    if current == target:
        status, compare, message = ST_NOOP, "same_as_target", "门户当前值已与目标一致"
    else:
        status, compare, message = None, "unchecked", ""
    conn.execute(
        "UPDATE wb_rows SET base_snapshot=?, base_revision=?,"
        " status=COALESCE(?, status), compare_status=?, error_summary=?, updated_at=? WHERE id=?",
        (
            json_dumps(current),
            data.get("revision"),
            status,
            compare,
            message,
            iso(),
            row["id"],
        ),
    )
    if status == ST_NOOP:
        storage.event(conn, "workbench", "precheck_noop",
                      f"{row['record_key']} 预检：门户已与目标一致")
    return {"ok": True, "status": status, "revision": data.get("revision")}


def precheck_batch(
    conn: sqlite3.Connection, batch_id: int, only_pending: bool = True
) -> dict:
    sql = "SELECT * FROM wb_rows WHERE batch_id=?"
    if only_pending:
        sql += " AND status IN ('pending','retry_wait')"
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", (batch_id,))]
    ok = failed = noop = 0
    for row in rows:
        result = precheck_row(conn, row)
        if result.get("ok"):
            ok += 1
            if result.get("status") == ST_NOOP:
                noop += 1
        else:
            failed += 1
    storage.event(
        conn, "workbench", "precheck_batch",
        f"批次 #{batch_id} 预检：成功 {ok}，无需提交 {noop}，门户不可达/缺记录 {failed}",
    )
    return {"checked": len(rows), "ok": ok, "noop": noop, "failed": failed}


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


def _stale_cutoff() -> str:
    return iso(utcnow() - timedelta(seconds=LEASE_SECONDS))


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lease_is_dead(conn: sqlite3.Connection, lease: dict) -> bool:
    """A lease is dead when its heartbeat is stale or its process is gone."""
    if lease["heartbeat_at"] < _stale_cutoff():
        return True
    # Only trust the PID check on this machine; the executor is local.
    return lease["pid"] is not None and not _pid_alive(lease["pid"])


def reclaim_dead_leases(conn: sqlite3.Connection) -> list[dict]:
    leases = conn.execute("SELECT * FROM wb_executors WHERE status='active'").fetchall()
    dead = []
    for lease in leases:
        if _lease_is_dead(conn, dict(lease)):
            conn.execute("UPDATE wb_executors SET status='offline' WHERE id=?", (lease["id"],))
            dead.append(dict(lease))
    return dead


def acquire_lease(conn: sqlite3.Connection, row_id: int, executor_id: str, job_id: int | None) -> bool:
    """Atomically take the execution lease for a row.

    Expired leases are reclaimed (the previous process is presumed dead; its
    outcome is left for recovery). Returns False if a live lease exists.
    """
    now = iso()
    reclaim_dead_leases(conn)
    live = conn.execute(
        "SELECT id FROM wb_executors WHERE row_id=? AND status='active'", (row_id,)
    ).fetchone()
    if live:
        return False
    conn.execute(
        "INSERT INTO wb_executors(id, job_id, row_id, pid, status, started_at, heartbeat_at)"
        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status='active',"
        " job_id=excluded.job_id, row_id=excluded.row_id, started_at=excluded.started_at,"
        " heartbeat_at=excluded.heartbeat_at",
        (executor_id, job_id, row_id, os.getpid(), "active", now, now),
    )
    return True


def release_lease(conn: sqlite3.Connection, executor_id: str, status: str = "done") -> None:
    conn.execute(
        "UPDATE wb_executors SET status=?, heartbeat_at=? WHERE id=?",
        (status, iso(), executor_id),
    )


def heartbeat(conn: sqlite3.Connection, executor_id: str, job_id: int | None = None) -> None:
    conn.execute("UPDATE wb_executors SET heartbeat_at=? WHERE id=?", (iso(), executor_id))
    if job_id is not None:
        conn.execute("UPDATE wb_jobs SET heartbeat_at=? WHERE id=?", (iso(), job_id))


def revoke_leases(conn: sqlite3.Connection, row_id: int, status: str = "revoked") -> int:
    cur = conn.execute(
        "UPDATE wb_executors SET status=?, heartbeat_at=? WHERE row_id=? AND status='active'",
        (status, iso(), row_id),
    )
    return cur.rowcount


def still_held(conn: sqlite3.Connection, row_id: int, executor_id: str) -> bool:
    row = conn.execute(
        "SELECT status FROM wb_executors WHERE id=? AND row_id=? AND status='active'",
        (executor_id, row_id),
    ).fetchone()
    return row is not None


def row_lease_active(conn: sqlite3.Connection, row_id: int) -> bool:
    row = conn.execute(
        "SELECT id FROM wb_executors WHERE row_id=? AND status='active' AND heartbeat_at>=?",
        (row_id, _stale_cutoff()),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Attempt / row state helpers
# ---------------------------------------------------------------------------


def next_attempt_no(conn: sqlite3.Connection, row_id: int) -> int:
    row = conn.execute(
        "SELECT MAX(attempt_no) m FROM wb_attempts WHERE row_id=?", (row_id,)
    ).fetchone()
    return (row["m"] or 0) + 1


def start_attempt(conn: sqlite3.Connection, row: dict) -> dict:
    no = next_attempt_no(conn, row["id"])
    cur = conn.execute(
        "INSERT INTO wb_attempts(row_id, attempt_no, status, stage, target, started_at)"
        " VALUES(?,?,?,?,?,?)",
        (row["id"], no, "running", "open", row["target"], iso()),
    )
    return {"id": cur.lastrowid, "attempt_no": no}


def finish_attempt(conn: sqlite3.Connection, attempt_id: int, **fields: Any) -> None:
    allowed = {
        "status", "stage", "http_status", "error_kind", "error_message", "form_token",
        "sub_token", "base_snapshot", "target", "verify_verdict", "verify_detail",
        "needs_manual", "next_retry_at", "finished_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE wb_attempts SET {assignments} WHERE id=?", (*updates.values(), attempt_id))


def set_row_state(
    conn: sqlite3.Connection,
    row_id: int,
    status: str,
    *,
    compare: str | None = None,
    error: str | None = None,
    last_error: str | None = None,
) -> None:
    now = iso()
    conn.execute(
        "UPDATE wb_rows SET status=?, compare_status=COALESCE(?, compare_status),"
        " error_summary=COALESCE(?, error_summary), last_error=COALESCE(?, last_error),"
        " last_attempt_at=?, updated_at=? WHERE id=?",
        (status, compare, error, last_error, now, now, row_id),
    )


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------


def _verdict_from_fields(fields: list[dict[str, str]], landed: bool) -> str:
    statuses = {f["status"] for f in fields}
    if not landed:
        if all(f["current"] == f["target"] for f in fields):
            # The final value is right but the portal history proves this
            # submission never wrote it (a human typed it, possibly the same
            # value we wanted). Never claim it, never resubmit.
            return "matched_elsewhere"
        if statuses & {"others", "conflict"}:
            return "others_changed"
        return "not_landed"
    if statuses & {"conflict", "pending", "others"}:
        return "partial"
    if "applied" not in statuses:
        # Everything already matches the target but none of it was written by
        # this submission (e.g. a human typed the same values, or the CSV was
        # a no-op). Final state is right, but this attempt must not claim it.
        return "matched_elsewhere"
    return "applied"


def safe_to_submit(base: dict[str, str] | None, target: dict[str, str], current: dict[str, str]) -> bool:
    """True when no field shows a foreign edit that a submit would clobber."""
    rows = field_diff(base, target, current)
    return not any(r["status"] in {"others", "conflict"} for r in rows)


def _attempt_form_tokens(conn: sqlite3.Connection, row_id: int) -> set[str]:
    return {
        r["form_token"]
        for r in conn.execute(
            "SELECT form_token FROM wb_attempts WHERE row_id=? AND form_token IS NOT NULL",
            (row_id,),
        )
    }


def written_fields(
    history: list[dict], tokens: set[str]
) -> tuple[set[str], set[str], set[str], bool]:
    """Split fields changed since the base into ours vs other people's.

    A field is attributed to us only when a portal revision whose form token
    belongs to one of this row's attempts changed it. Everything else that
    changed is someone else's edit — the only honest way to tell "we landed it"
    from "a human happened to type the same value".

    Returns (ours, others, our_last, landed): ``our_last`` is the subset whose
    most recent write in the history was ours, so a subsequent human edit is
    never reported as our work.
    """
    ours: set[str] = set()
    others: set[str] = set()
    last_writer: dict[str, bool] = {}
    ours_revisions = 0
    for item in sorted(history, key=lambda h: h.get("revision", 0)):
        if item.get("revision", 0) <= (item.get("_base_revision") or 0):
            continue
        is_ours = item.get("source") == "collector" and item.get("session_token") in tokens
        for name in item.get("changed_fields") or {}:
            last_writer[name] = is_ours
            (ours if is_ours else others).add(name)
        if is_ours:
            ours_revisions += 1
    our_last = {name for name, was_ours in last_writer.items() if was_ours}
    return ours, others, our_last, ours_revisions > 0


def reconcile_row(conn: sqlite3.Connection, row: dict, *, source: str = "reconcile") -> dict:
    """Fetch the portal record and classify a previously unknown outcome."""
    target = normalize_fields(json_loads(row["target"], {}))
    base = normalize_fields(json_loads(row["base_snapshot"], None)) if row["base_snapshot"] else None
    client = client_for(conn, row)
    try:
        data = client.get_record(row["record_key"])
    except PortalHTTP as exc:
        if exc.status == 404:
            set_row_state(conn, row["id"], ST_MISSING, compare="missing",
                          error="门户中已不存在该记录")
            return {"verdict": "missing", "fields": [], "current": None, "revision": None}
        raise
    current = normalize_fields(data.get("fields"))
    history = [
        {**h, "_base_revision": row.get("base_revision") or 0} for h in (data.get("history") or [])
    ]
    tokens = _attempt_form_tokens(conn, row["id"])
    ours, others, our_last, landed = written_fields(history, tokens)
    fields = attributed_diff(base, target, current, ours, others, our_last)
    verdict = _verdict_from_fields(fields, landed)
    detail = {
        "verdict": verdict,
        "fields": fields,
        "current": current,
        "revision": data.get("revision"),
        "base_revision": row.get("base_revision"),
        "history": history,
        "ours_written": sorted(ours),
        "others_written": sorted(others),
        "our_last": sorted(our_last),
        "at": iso(),
    }
    _apply_reconcile(conn, row, detail, source)
    return detail


def _apply_reconcile(conn: sqlite3.Connection, row: dict, detail: dict, source: str) -> None:
    verdict = detail["verdict"]
    if verdict == "applied":
        set_row_state(conn, row["id"], ST_APPLIED, compare="applied",
                      error=f"恢复核对：本次提交已在门户生效（版本 {detail['revision']}）")
        _resolve_conflict(conn, row["id"], "system", "恢复核对确认已生效")
    elif verdict == "matched_elsewhere":
        set_row_state(conn, row["id"], ST_RESOLVED, compare="accepted",
                      error="恢复核对：门户当前值已与目标一致，但并非本次提交写入，未重复提交")
        _resolve_conflict(conn, row["id"], "system", "门户已与目标一致（非本次写入）")
    elif verdict == "partial":
        set_row_state(conn, row["id"], ST_CONFLICT, compare="partial",
                      error="恢复核对：部分字段已生效，部分字段为他人修改，需人工处理")
        _open_conflict(conn, row, detail, reason="partial")
    elif verdict == "others_changed":
        set_row_state(conn, row["id"], ST_CONFLICT, compare="others",
                      error="恢复核对：本次提交未生效，记录已被他人修改，需人工处理")
        _open_conflict(conn, row, detail, reason="others_changed")
    elif verdict == "not_landed":
        set_row_state(conn, row["id"], ST_RETRY_WAIT, compare="not_landed",
                      error="恢复核对：本次提交未生效，可安全补交")
    elif verdict == "missing":
        set_row_state(conn, row["id"], ST_MISSING, compare="missing",
                      error="门户中已不存在该记录")
    else:
        set_row_state(conn, row["id"], ST_NEEDS_RECONCILE, compare="unknown",
                      error="恢复核对无法判断，需人工处理")
    conn.execute(
        "UPDATE wb_attempts SET verify_verdict=?, verify_detail=? WHERE id=("
        " SELECT id FROM wb_attempts WHERE row_id=? AND verify_verdict IS NULL"
        " ORDER BY id DESC LIMIT 1)",
        (verdict, json_dumps(detail), row["id"]),
    )
    verdict_notes = {
        "applied": "本次提交已生效",
        "partial": "部分生效且与他人修改混合",
        "others_changed": "本次未生效，他人修改",
        "not_landed": "本次未生效，可补交",
        "missing": "门户记录不存在",
        "matched_elsewhere": "门户已与目标一致但非本次写入",
        "unknown": "无法判断",
    }
    conn.execute(
        "INSERT INTO wb_events(at, actor, action, detail) VALUES(?,?,?,?)",
        (iso(), "workbench", "reconcile",
         f"{row['record_key']} 行#{row.get('row_number','?')} 恢复核对结论：{verdict_notes[verdict]}（{source}）"),
    )


def _open_conflict(conn: sqlite3.Connection, row: dict, detail: dict, reason: str) -> None:
    current = detail.get("current")
    existing = conn.execute(
        "SELECT id FROM wb_conflicts WHERE row_id=? AND status='open'", (row["id"],)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE wb_conflicts SET reason=?, verify_detail=?, current_snapshot=?, opened_at=?"
            " WHERE id=?",
            (reason, json_dumps(detail), json_dumps(current), iso(), existing["id"]),
        )
        return
    conn.execute(
        "INSERT INTO wb_conflicts(row_id, reason, status, verify_detail, current_snapshot,"
        " opened_at) VALUES(?,?,?,?,?,?)",
        (row["id"], reason, "open", json_dumps(detail), json_dumps(current), iso()),
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class RowBusy(Exception):
    pass


def execute_row(
    conn: sqlite3.Connection,
    row_id: int,
    executor_id: str,
    *,
    job_id: int | None = None,
    timeout: float = 2.5,
) -> dict:
    """Run one full submit attempt. The caller holds the lease.

    Returns a summary with ``status`` set to the row's resulting status.
    """
    raw = conn.execute("SELECT * FROM wb_rows WHERE id=?", (row_id,)).fetchone()
    if raw is None:
        raise ValueError("row_not_found")
    row = dict(raw)
    if not still_held(conn, row_id, executor_id):
        raise RowBusy("执行权已被收回（接管或超时），放弃本次执行")
    if row["status"] not in AUTOMATIC | {ST_CONFLICT, ST_NEEDS_RECONCILE, ST_RUNNING}:
        raise RowBusy(f"状态 {row['status']} 不允许执行")

    target = normalize_fields(json_loads(row["target"], {}))
    base = normalize_fields(json_loads(row["base_snapshot"], None)) if row["base_snapshot"] else None
    client = client_for(conn, row, timeout=timeout)
    attempt = start_attempt(conn, row)
    attempt_id = attempt["id"]
    conn.execute("UPDATE wb_rows SET status=?, updated_at=? WHERE id=?", (ST_RUNNING, iso(), row_id))

    def abandon(stage: str) -> None:
        finish_attempt(conn, attempt_id, status="abandoned", stage=stage, finished_at=iso(),
                       error_kind="lease_revoked", error_message="执行权在执行中被收回，未提交")
        set_row_state(conn, row_id, ST_CONFLICT, compare="unchecked",
                      error="执行权被接管/收回，本次未提交，请人工核对")

    opened = None
    try:
        # Step 1: open a fresh form (portal freezes current version).
        try:
            opened = client.open_form(row["record_key"])
        except PortalHTTP as exc:
            return _fail_definite(conn, row, attempt_id, exc, stage="open")
        except PortalError as exc:
            return _fail_unknown(conn, row, attempt_id, exc, None, None, None,
                                 stage="open", can_have_written=False)
        base_open = normalize_fields(opened.get("fields"))
        form_token = opened.get("session_token")
        sub_token = opened.get("sub_token")
        base_revision = opened.get("expected_version")
        try:
            history_open = (client.get_record(row["record_key"]).get("history") or [])
        except PortalError:
            history_open = []
        finish_attempt(conn, attempt_id, form_token=form_token, sub_token=sub_token, stage="opened")
        conn.execute(
            "UPDATE wb_attempts SET base_snapshot=? WHERE id=?", (json_dumps(base_open), attempt_id)
        )
        heartbeat(conn, executor_id, job_id)
        if not still_held(conn, row_id, executor_id):
            abandon("opened")
            raise RowBusy("执行权已被收回，放弃本次执行")

        # Step 2: preflight. Never overwrite a foreign edit.
        if base is None:
            # First attempt: the pre-submission base is what we just read.
            base = base_open
            conn.execute(
                "UPDATE wb_rows SET base_snapshot=?, base_revision=? WHERE id=? AND base_snapshot IS NULL",
                (json_dumps(base), base_revision, row_id),
            )
            row["base_snapshot"] = json_dumps(base)
            row["base_revision"] = base_revision
        preflight = field_diff(base, target, base_open)
        ours_before, others_before, last_before, landed_before = written_fields(
            [{**h, "_base_revision": row.get("base_revision") or 0} for h in history_open],
            _attempt_form_tokens(conn, row_id),
        )
        if ours_before or others_before:
            preflight = attributed_diff(base, target, base_open, ours_before, others_before, last_before)
        if not safe_to_submit(base, target, base_open):
            # Distinguish "we partly landed" from "we never landed and someone
            # else edited" using the portal's own revision history.
            verdict = "partial" if landed_before else "others_changed"
            if verdict == "partial" and not any(
                f["status"] in {"applied", "conflict"} for f in preflight
            ):
                verdict = "others_changed"
            set_row_state(
                conn, row_id, ST_CONFLICT,
                compare="partial" if verdict == "partial" else "others",
                error=(
                    "提交前发现门户记录被他人修改：部分字段已是本次目标值，另有字段为他人修改，"
                    "已转入冲突队列，未再次提交"
                    if verdict == "partial"
                    else "提交前发现门户记录被他人修改，已自动转入冲突队列，未提交"
                ),
            )
            _open_conflict(conn, row, {"verdict": verdict, "fields": preflight,
                                       "current": base_open, "revision": base_revision,
                                       "ours_written": sorted(ours_before),
                                       "others_written": sorted(others_before),
                                       "our_last": sorted(last_before)},
                           reason="preflight_foreign_edit")
            finish_attempt(conn, attempt_id, status="blocked", stage="preflight",
                           error_kind="foreign_edit", finished_at=iso(),
                           error_message="提交前发现他人修改，未提交",
                           verify_verdict=verdict, verify_detail=json_dumps(preflight))
            storage.event(conn, "executor", "preflight_blocked",
                          f"{row['record_key']} 提交前发现他人修改，转冲突队列")
            return {"status": ST_CONFLICT, "verdict": verdict, "kind": "foreign_edit"}
        if base_open == target:
            finish_attempt(conn, attempt_id, status="noop", stage="preflight",
                           finished_at=iso(), error_kind="already_matches")
            set_row_state(conn, row_id, ST_NOOP, compare="same_as_target",
                          error="门户当前值与目标一致，无需提交")
            return {"status": ST_NOOP, "changed": False}

        # Step 3: stage.
        try:
            client.stage(form_token, target)
        except PortalHTTP as exc:
            return _fail_definite(conn, row, attempt_id, exc, stage="stage")
        except PortalError as exc:
            # Staging writes only a draft in the portal session; the record
            # itself cannot have changed.
            return _fail_unknown(conn, row, attempt_id, exc, base_open, target, form_token,
                                 stage="stage", can_have_written=False)
        heartbeat(conn, executor_id, job_id)
        finish_attempt(conn, attempt_id, stage="staged")
        if not still_held(conn, row_id, executor_id):
            abandon("staged")
            raise RowBusy("执行权已被收回，放弃本次执行")

        # Step 4: submit. This is the only step that can write.
        try:
            response = client.submit(form_token)
        except PortalHTTP as exc:
            if not exc.outcome_unknown:
                return _fail_definite(conn, row, attempt_id, exc, stage="submit",
                                      base=base_open, target=target)
            return _fail_unknown(conn, row, attempt_id, exc, base_open, target, form_token,
                                 stage="submit", can_have_written=True)
        except PortalError as exc:
            return _fail_unknown(conn, row, attempt_id, exc, base_open, target, form_token,
                                 stage="submit", can_have_written=True)

        heartbeat(conn, executor_id, job_id)
        if not still_held(conn, row_id, executor_id):
            # Rare: revoked between the submit landing and this check. The
            # write did happen; record it and let the taker reconcile.
            finish_attempt(conn, attempt_id, status="applied", stage="submitted",
                           http_status=200, sub_token=response.get("sub_token") or sub_token,
                           finished_at=iso())
            set_row_state(conn, row_id, ST_APPLIED, compare="applied",
                          error=f"提交已生效（门户版本 {response.get('revision_after')}），执行权随后被收回")
            return {"status": ST_APPLIED, "revision": response.get("revision_after")}

        finish_attempt(conn, attempt_id, status="applied", stage="submitted", http_status=200,
                       sub_token=response.get("sub_token") or sub_token, finished_at=iso())
        if response.get("changed") is False:
            set_row_state(conn, row_id, ST_NOOP, compare="same_as_target",
                          error="门户记录已与目标一致，无需变更")
            storage.event(conn, "executor", "row_noop", f"{row['record_key']} 无需变更")
            return {"status": ST_NOOP, "revision": response.get("revision_after"), "changed": False}
        set_row_state(conn, row_id, ST_APPLIED, compare="applied",
                      error=f"已提交，门户版本 {response.get('revision_after')}")
        storage.event(conn, "executor", "row_applied",
                      f"{row['record_key']} 提交成功，门户版本 {response.get('revision_after')}")
        return {"status": ST_APPLIED, "revision": response.get("revision_after"), "changed": True}
    finally:
        pass


def _fail_definite(
    conn: sqlite3.Connection,
    row: dict,
    attempt_id: int,
    exc: PortalHTTP,
    *,
    stage: str,
    base: dict[str, str] | None = None,
    target: dict[str, str] | None = None,
) -> dict:
    """Portal definitely refused; nothing was written by this attempt."""
    finish_attempt(conn, attempt_id, status="failed", stage=stage, http_status=exc.status,
                   error_kind=exc.kind, error_message=exc.message, finished_at=iso())
    if exc.kind == "http_404" or (exc.status == 404):
        set_row_state(conn, row["id"], ST_MISSING, compare="missing",
                      error="门户中不存在该业务编号，未提交", last_error=exc.message)
        storage.event(conn, "executor", "row_missing", f"{row['record_key']} 门户无此记录")
        return {"status": ST_MISSING, "error": exc.message, "kind": "missing"}
    if exc.kind == "validation_failed":
        set_row_state(conn, row["id"], ST_INVALID, compare="invalid",
                      error="门户校验失败：" + exc.message, last_error=exc.message)
        conn.execute("UPDATE wb_attempts SET needs_manual=1 WHERE id=?", (attempt_id,))
        storage.event(conn, "executor", "row_invalid",
                      f"{row['record_key']} 门户校验失败：{exc.message}")
        return {"status": ST_INVALID, "error": exc.message, "kind": exc.kind}
    if exc.kind == "version_conflict":
        current = normalize_fields((exc.body or {}).get("current_record"))
        fields = field_diff(base, target, current)
        set_row_state(conn, row["id"], ST_CONFLICT, compare="others",
                      error="提交时版本冲突：记录已被他人修改", last_error=exc.message)
        _open_conflict(conn, row,
                       {"verdict": "others_changed", "fields": fields, "current": current,
                        "revision": (exc.body or {}).get("current_revision")},
                       reason="version_conflict")
        return {"status": ST_CONFLICT, "error": exc.message, "kind": exc.kind}
    set_row_state(conn, row["id"], ST_FAILED, compare="error",
                  error=exc.message, last_error=exc.message)
    return {"status": ST_FAILED, "error": exc.message, "kind": exc.kind}


def _fail_unknown(
    conn: sqlite3.Connection,
    row: dict,
    attempt_id: int,
    exc: PortalError,
    base: dict[str, str] | None,
    target: dict[str, str] | None,
    form_token: str | None,
    *,
    stage: str,
    can_have_written: bool,
) -> dict:
    """Network outcome unknown. Only the submit step can have written data."""
    if not can_have_written:
        finish_attempt(conn, attempt_id, status="failed", stage=stage,
                       error_kind=exc.kind, error_message=exc.message,
                       finished_at=iso())
        set_row_state(conn, row["id"], ST_RETRY_WAIT, compare="not_landed",
                      error=f"门户未响应（{exc.kind}），尚未提交任何数据，可安全补交",
                      last_error=exc.message)
        storage.event(conn, "executor", "retryable_failure",
                      f"{row['record_key']} {stage} 阶段失败（{exc.kind}），未写入，可补交")
        return {"status": ST_RETRY_WAIT, "error": exc.message, "kind": exc.kind}
    finish_attempt(conn, attempt_id, status="unknown", stage=stage,
                   error_kind=exc.kind, error_message=exc.message,
                   form_token=form_token, needs_manual=1, finished_at=iso())
    set_row_state(conn, row["id"], ST_NEEDS_RECONCILE, compare="checking",
                  error=f"提交结果未知（{exc.kind}）：{exc.message}；正在与门户核对",
                  last_error=exc.message)
    storage.event(conn, "executor", "unknown_outcome",
                  f"{row['record_key']} 提交结果未知（{exc.kind}），进入恢复核对")
    try:
        result = reconcile_row(conn, row, source="executor")
    except PortalError as exc2:
        set_row_state(conn, row["id"], ST_NEEDS_RECONCILE, compare="unreachable",
                      error=f"结果未知且门户不可达，需稍后恢复核对：{exc2.message}")
        return {"status": ST_NEEDS_RECONCILE, "verdict": "unknown", "error": exc.message}
    fresh = conn.execute("SELECT status FROM wb_rows WHERE id=?", (row["id"],)).fetchone()
    return {
        "status": fresh["status"] if fresh else ST_NEEDS_RECONCILE,
        "verdict": result.get("verdict"),
        "error": exc.message,
        "kind": exc.kind,
    }


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def recover_interrupted(conn: sqlite3.Connection, reconcile: bool = True) -> dict:
    """Mark rows left 'running' by dead executors, then reconcile them."""
    recovered: list[dict] = []
    reclaim_dead_leases(conn)
    # Any row still "running" without a live lease was interrupted.
    rows = conn.execute(
        "SELECT * FROM wb_rows WHERE status='running' OR"
        " (status='needs_reconcile' AND compare_status IN ('checking','interrupted'))"
    ).fetchall()
    for raw in rows:
        if row_lease_active(conn, raw["id"]):
            continue
        conn.execute(
            "UPDATE wb_attempts SET status='abandoned', error_kind=COALESCE(error_kind,'interrupted'),"
            " error_message=COALESCE(error_message,'执行器进程中断'), finished_at=COALESCE(finished_at,?)"
            " WHERE row_id=? AND status='running'",
            (iso(), raw["id"]),
        )
        if raw["status"] != ST_NEEDS_RECONCILE:
            set_row_state(conn, raw["id"], ST_NEEDS_RECONCILE, compare="interrupted",
                          error="执行器中断，提交结果未知，需恢复核对")
        fresh = conn.execute("SELECT * FROM wb_rows WHERE id=?", (raw["id"],)).fetchone()
        recovered.append(dict(fresh))
    for row in recovered:
        if not reconcile:
            break
        try:
            reconcile_row(conn, row, source="recovery")
        except PortalError as exc:
            set_row_state(conn, row["id"], ST_NEEDS_RECONCILE, compare="unreachable",
                          error=f"门户不可达，稍后可再次恢复核对：{exc.message}")
    if recovered:
        storage.event(conn, "workbench", "recover_interrupted",
                      f"处理 {len(recovered)} 行中断执行：{', '.join(r['record_key'] for r in recovered)}")
    return {"recovered": len(recovered), "rows": [r["record_key"] for r in recovered]}


def reconcile_pending(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT * FROM wb_rows WHERE status='needs_reconcile' ORDER BY id"
    ).fetchall()
    done = 0
    errors = 0
    for raw in rows:
        try:
            reconcile_row(conn, dict(raw), source="manual_reconcile")
            done += 1
        except PortalError:
            errors += 1
    return {"processed": done, "errors": errors, "total": len(rows)}


# ---------------------------------------------------------------------------
# Conflict queue / manual takeover
# ---------------------------------------------------------------------------


def conflict_list(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, r.record_key, r.row_number, r.target, r.base_snapshot, r.base_revision,"
        " r.batch_id, r.status AS row_status, b.name AS batch_name"
        " FROM wb_conflicts c JOIN wb_rows r ON r.id=c.row_id"
        " JOIN wb_batches b ON b.id=r.batch_id"
        " WHERE c.status='open' ORDER BY c.id"
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["current"] = normalize_fields(json_loads(item["current_snapshot"], None))
        item["target_fields"] = normalize_fields(json_loads(item["target"], None))
        item["base"] = normalize_fields(json_loads(item["base_snapshot"], None))
        detail = json_loads(item["verify_detail"], {}) or {}
        stored = detail.get("fields")
        if stored:
            item["diff"] = stored
        else:
            item["diff"] = field_diff(item["base"], item["target_fields"], item["current"])
        item["reason_label"] = {
            "partial": "部分生效",
            "others_changed": "他人修改",
            "version_conflict": "提交时版本冲突",
            "preflight_foreign_edit": "提交前发现他人修改",
        }.get(item["reason"], item["reason"])
        result.append(item)
    return result


def take_over(conn: sqlite3.Connection, row_id: int, operator: str = "reviewer") -> dict:
    """Human takes ownership of a row; all executor leases are revoked.

    Every executor checks its lease between steps, and a submit is refused
    once the lease is gone. This is the barrier against overwriting manual
    corrections.
    """
    row = conn.execute("SELECT * FROM wb_rows WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise ValueError("行不存在")
    revoked = revoke_leases(conn, row_id, status="taken_over")
    conn.execute(
        "UPDATE wb_rows SET status=?, updated_at=? WHERE id=?", (ST_RUNNING, iso(), row_id)
    )
    conn.execute(
        "INSERT INTO wb_takeovers(row_id, operator, status, revoked_leases, created_at)"
        " VALUES(?,?,?,?,?)",
        (row_id, operator, "active", revoked, iso()),
    )
    storage.event(conn, operator, "take_over",
                  f"{row['record_key']} 行#{row['row_number']} 接管，撤销 {revoked} 个执行器租约")
    return {"row_id": row_id, "revoked": revoked}


def manual_submit(
    conn: sqlite3.Connection,
    row_id: int,
    fields: dict[str, str],
    operator: str = "reviewer",
    timeout: float = 4.0,
) -> dict:
    """Submit one row by hand from the workbench UI.

    Opens a fresh portal form (latest version), stages the reviewer's values,
    and relies on the portal's version check as the final guard.
    """
    raw = conn.execute("SELECT * FROM wb_rows WHERE id=?", (row_id,)).fetchone()
    if raw is None:
        raise ValueError("行不存在")
    row = dict(raw)
    fields = normalize_fields(fields)
    errors = validate_fields(fields)
    if errors:
        return {"ok": False, "error": "字段校验失败：" + "；".join(errors.values()), "fields": errors}
    revoke_leases(conn, row_id, status="taken_over")
    client = client_for(conn, row, timeout=timeout)
    attempt = start_attempt(conn, row)
    attempt_id = attempt["id"]
    session_token = None
    try:
        opened = client.open_form(row["record_key"])
        base_open = normalize_fields(opened.get("fields"))
        session_token = opened.get("session_token")
        sub_token = opened.get("sub_token")
        finish_attempt(conn, attempt_id, form_token=session_token, sub_token=sub_token,
                       base_snapshot=json_dumps(base_open), stage="opened")
        client.stage(session_token, fields)
        response = client.submit(session_token)
    except PortalHTTP as exc:
        finish_attempt(conn, attempt_id, status="failed", stage="submit",
                       http_status=exc.status, error_kind=exc.kind, error_message=exc.message,
                       finished_at=iso())
        if exc.kind == "version_conflict":
            current = normalize_fields((exc.body or {}).get("current_record"))
            _open_conflict(conn, row,
                           {"verdict": "others_changed",
                            "fields": field_diff(base_open, fields, current),
                            "current": current,
                            "revision": (exc.body or {}).get("current_revision")},
                           reason="version_conflict")
            set_row_state(conn, row_id, ST_CONFLICT, compare="others",
                          error="手工提交时版本冲突：记录刚被他人修改")
            return {"ok": False, "error": "版本冲突：记录刚被他人修改，请刷新后重新核对",
                    "kind": exc.kind}
        set_row_state(conn, row_id, ST_FAILED, compare="error", error=exc.message)
        return {"ok": False, "error": exc.message, "kind": exc.kind}
    except PortalError as exc:
        finish_attempt(conn, attempt_id, status="unknown", stage="submit",
                       error_kind=exc.kind, error_message=exc.message,
                       needs_manual=1, finished_at=iso())
        set_row_state(conn, row_id, ST_NEEDS_RECONCILE, compare="checking",
                      error=f"手工提交结果未知：{exc.message}，需恢复核对")
        return {"ok": False, "error": exc.message, "kind": exc.kind, "unknown": True}
    finish_attempt(conn, attempt_id, status="applied", stage="submitted", http_status=200,
                   sub_token=response.get("sub_token") or sub_token, finished_at=iso())
    if response.get("changed") is False:
        set_row_state(conn, row_id, ST_NOOP, compare="same_as_target",
                      error=f"{operator} 手工提交：门户记录已与输入一致")
        _resolve_conflict(conn, row_id, operator, "人工提交后与目标一致（无需变更）")
        return {"ok": True, "status": ST_NOOP, "revision": response.get("revision_after")}
    set_row_state(conn, row_id, ST_RESOLVED, compare="applied",
                  error=f"{operator} 手工提交成功，门户版本 {response.get('revision_after')}")
    conn.execute(
        "UPDATE wb_rows SET target=?, base_snapshot=?, base_revision=? WHERE id=?",
        (json_dumps(fields), json_dumps(fields), response.get("revision_after"), row_id),
    )
    _resolve_conflict(conn, row_id, operator, "人工接管并提交成功")
    storage.event(conn, operator, "manual_submit",
                  f"{row['record_key']} 手工提交成功，门户版本 {response.get('revision_after')}")
    return {"ok": True, "status": ST_RESOLVED, "revision": response.get("revision_after")}


def accept_portal_values(conn: sqlite3.Connection, row_id: int, operator: str = "reviewer") -> dict:
    raw = conn.execute("SELECT * FROM wb_rows WHERE id=?", (row_id,)).fetchone()
    if raw is None:
        raise ValueError("行不存在")
    row = dict(raw)
    data = client_for(conn, row).get_record(row["record_key"])
    current = normalize_fields(data.get("fields"))
    conn.execute(
        "UPDATE wb_rows SET target=?, base_snapshot=?, base_revision=?, status=?,"
        " compare_status='accepted', error_summary=?, updated_at=? WHERE id=?",
        (json_dumps(current), json_dumps(current), data.get("revision"), ST_RESOLVED,
         f"{operator} 采纳门户当前值，不再覆盖", iso(), row_id),
    )
    _resolve_conflict(conn, row_id, operator, "采纳门户当前值")
    storage.event(conn, operator, "accept_portal",
                  f"{row['record_key']} 采纳门户当前值（版本 {data.get('revision')}）")
    return {"ok": True, "revision": data.get("revision")}


def retry_conflict(
    conn: sqlite3.Connection, row_id: int, operator: str = "reviewer",
    target: dict[str, str] | None = None,
) -> dict:
    """Send a conflicted row back to the automatic queue — deliberately.

    The operator is choosing to overwrite the portal's current values, so the
    pre-submission base is moved to the portal's current state *at this
    moment*. Without that rebase the very next executor run would see the same
    "someone else edited this" difference and bounce straight back here.
    """
    raw = conn.execute("SELECT * FROM wb_rows WHERE id=?", (row_id,)).fetchone()
    if raw is None:
        raise ValueError("行不存在")
    row = dict(raw)
    client = client_for(conn, row)
    data = client.get_record(row["record_key"])
    current = normalize_fields(data.get("fields"))
    fields = normalize_fields(target) if target is not None else normalize_fields(
        json_loads(row["target"], {})
    )
    errors = validate_fields(fields)
    if errors:
        return {"ok": False, "error": "字段校验失败：" + "；".join(errors.values())}
    conn.execute(
        "UPDATE wb_rows SET target=?, base_snapshot=?, base_revision=?, status=?,"
        " compare_status='unchecked', error_summary=?, updated_at=? WHERE id=?",
        (
            json_dumps(fields),
            json_dumps(current),
            data.get("revision"),
            ST_RETRY_WAIT,
            f"{operator} 决定按当前目标重新提交（基准已更新为门户版本 {data.get('revision')}）",
            iso(),
            row_id,
        ),
    )
    _resolve_conflict(conn, row_id, operator, "人工确认后按新基准重试")
    storage.event(
        conn, operator, "retry_conflict",
        f"{row['record_key']} 转回待补交，基准更新为门户版本 {data.get('revision')}",
    )
    return {"ok": True, "revision": data.get("revision")}


def dismiss(conn: sqlite3.Connection, row_id: int, operator: str = "reviewer", note: str = "") -> None:
    conn.execute(
        "UPDATE wb_conflicts SET status='dismissed', resolved_by=?, resolved_at=?,"
        " resolution_note=? WHERE row_id=? AND status='open'",
        (operator, iso(), note or "人工关闭，不再提交", row_id),
    )
    set_row_state(conn, row_id, ST_RESOLVED, compare="dismissed",
                  error=f"{operator} 关闭：{note or '不再提交'}")
    storage.event(conn, operator, "dismiss_conflict", f"行#{row_id} 人工关闭")


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------


def pending_rows(conn: sqlite3.Connection, batch_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT * FROM wb_rows WHERE status IN ('pending','retry_wait')"
        + (" AND batch_id=?" if batch_id else "")
        + " ORDER BY id"
    )
    args = (batch_id,) if batch_id else ()
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _resolve_conflict(conn: sqlite3.Connection, row_id: int, operator: str, note: str) -> None:
    conn.execute(
        "UPDATE wb_conflicts SET status='resolved', resolved_by=?, resolved_at=?,"
        " resolution_note=? WHERE row_id=? AND status='open'",
        (operator, iso(), note, row_id),
    )
