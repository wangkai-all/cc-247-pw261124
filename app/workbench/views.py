"""Read models and shared page pieces for the workbench UI."""

from __future__ import annotations

import sqlite3
import urllib.parse

from ..common import field_diff, json_loads, normalize_fields
from ..web import badge, esc as h

STATUS_LABELS = {
    "pending": "待执行",
    "running": "执行中/待接管",
    "retry_wait": "待补交",
    "needs_reconcile": "待恢复核对",
    "applied": "已生效",
    "resolved": "人工已处理",
    "conflict": "冲突队列",
    "failed": "失败",
    "invalid": "导入无效/校验失败",
    "missing": "门户无此记录",
    "noop": "无需提交",
}

COMPARE_LABELS = {
    "unchecked": "未比对",
    "applied": "本次已生效",
    "partial": "部分生效",
    "others": "他人修改",
    "not_landed": "本次未生效",
    "same_as_target": "与目标一致",
    "checking": "核对中",
    "unreachable": "门户不可达",
    "interrupted": "执行中断",
    "invalid": "校验失败",
    "missing": "记录不存在",
    "error": "执行错误",
    "accepted": "已采纳门户值",
    "dismissed": "已关闭",
    "unknown": "无法判断",
}

REASON_LABELS = {
    "partial": "部分生效 + 他人修改",
    "others_changed": "他人修改（本次未生效）",
    "version_conflict": "提交时版本冲突",
    "preflight_foreign_edit": "提交前发现他人修改",
}


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT b.*, "
        " SUM(CASE WHEN r.status IN ('applied','resolved','noop') THEN 1 ELSE 0 END) done,"
        " SUM(CASE WHEN r.status IN ('conflict') THEN 1 ELSE 0 END) conflicts,"
        " SUM(CASE WHEN r.status IN ('pending','retry_wait') THEN 1 ELSE 0 END) remaining"
        " FROM wb_batches b LEFT JOIN wb_rows r ON r.batch_id=b.id"
        " GROUP BY b.id ORDER BY b.id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def batch_rows(conn: sqlite3.Connection, batch_id: int, status: str = "") -> list[dict]:
    sql = (
        "SELECT r.*, b.name AS batch_name FROM wb_rows r JOIN wb_batches b ON b.id=r.batch_id"
        " WHERE r.batch_id=?"
    )
    args: list = [batch_id]
    if status:
        sql += " AND r.status=?"
        args.append(status)
    sql += " ORDER BY r.row_number"
    rows = []
    for raw in conn.execute(sql, args).fetchall():
        item = dict(raw)
        item["target_fields"] = normalize_fields(json_loads(item["target"], None))
        item["base"] = normalize_fields(json_loads(item["base_snapshot"], None))
        item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
        item["compare_label"] = COMPARE_LABELS.get(item["compare_status"], item["compare_status"])
        rows.append(item)
    return rows


def row_detail(conn: sqlite3.Connection, row_id: int) -> dict | None:
    raw = conn.execute(
        "SELECT r.*, b.name AS batch_name FROM wb_rows r JOIN wb_batches b ON b.id=r.batch_id"
        " WHERE r.id=?",
        (row_id,),
    ).fetchone()
    if not raw:
        return None
    item = dict(raw)
    item["target_fields"] = normalize_fields(json_loads(item["target"], None))
    item["base"] = normalize_fields(json_loads(item["base_snapshot"], None))
    item["attempts"] = [
        dict(a)
        for a in conn.execute(
            "SELECT * FROM wb_attempts WHERE row_id=? ORDER BY attempt_no DESC", (row_id,)
        ).fetchall()
    ]
    item["conflict"] = None
    conflict = conn.execute(
        "SELECT * FROM wb_conflicts WHERE row_id=? ORDER BY id DESC LIMIT 1", (row_id,)
    ).fetchone()
    stored_fields = None
    if conflict:
        item["conflict"] = dict(conflict)
        item["current"] = normalize_fields(json_loads(conflict["current_snapshot"], None))
        detail = json_loads(conflict["verify_detail"], {}) or {}
        stored_fields = detail.get("fields")
    else:
        item["current"] = normalize_fields(json_loads(raw["current_snapshot"], None)) if "current_snapshot" in raw.keys() else None
    if item["current"] is None:
        item["current"] = {}
    # The stored comparison knows who wrote each field (from portal history);
    # recomputing with a plain diff would lose that attribution.
    if stored_fields:
        item["diff"] = stored_fields
    else:
        item["diff"] = field_diff(item["base"], item["target_fields"], item["current"]) if item["current"] else []
    item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    item["compare_label"] = COMPARE_LABELS.get(item["compare_status"], item["compare_status"])
    item["takeovers"] = [
        dict(t)
        for t in conn.execute(
            "SELECT * FROM wb_takeovers WHERE row_id=? ORDER BY id DESC", (row_id,)
        ).fetchall()
    ]
    item["lease"] = conn.execute(
        "SELECT * FROM wb_executors WHERE row_id=? AND status='active' ORDER BY started_at DESC LIMIT 1",
        (row_id,),
    ).fetchone()
    item["lease"] = dict(item["lease"]) if item["lease"] else None
    return item


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {key: 0 for key in STATUS_LABELS}
    for raw in conn.execute("SELECT status, COUNT(*) c FROM wb_rows GROUP BY status"):
        counts[raw["status"]] = raw["c"]
    counts["_total"] = sum(v for k, v in counts.items() if not k.startswith("_"))
    return counts


def recent_events(conn: sqlite3.Connection, limit: int = 80) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM wb_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    ]


def jobs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM wb_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    ]


def executors(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT e.*, r.record_key FROM wb_executors e LEFT JOIN wb_rows r ON r.id=e.row_id"
            " ORDER BY e.started_at DESC LIMIT 50"
        ).fetchall()
    ]


def q(value: object) -> str:
    return urllib.parse.quote("" if value is None else str(value))


def status_badge(status: str) -> str:
    if status in ("applied", "resolved"):
        return badge("applied", STATUS_LABELS.get(status, status))
    if status in ("failed", "invalid", "missing"):
        return badge("failed", STATUS_LABELS.get(status, status))
    if status == "noop":
        return badge("noop", STATUS_LABELS.get(status, status))
    return badge(status, STATUS_LABELS.get(status, status))


def compare_badge(compare: str) -> str:
    mapping = {
        "applied": "applied-field",
        "partial": "conflict-field",
        "others": "others",
        "not_landed": "pending-field",
        "same_as_target": "unchanged",
        "unchecked": "unchanged",
        "checking": "needs_reconcile",
        "unreachable": "needs_reconcile",
        "interrupted": "needs_reconcile",
        "invalid": "failed",
        "missing": "failed",
        "error": "failed",
        "accepted": "applied-field",
        "dismissed": "unchanged",
        "unknown": "needs_reconcile",
    }
    return f'<span class="badge b-{mapping.get(compare, "unchanged")}">{h(COMPARE_LABELS.get(compare, compare))}</span>'


def diff_table(rows: list[dict], show_status: bool = True) -> str:
    if not rows:
        return '<p class="muted">暂无可比对的字段快照。</p>'
    head = (
        '<table class="field-table"><thead><tr><th>字段</th><th>提交前（基准）</th>'
        "<th>本次目标</th><th>门户当前</th>"
        + ("<th>判定</th>" if show_status else "")
        + "</tr></thead><tbody>"
    )
    body = []
    classes = {
        "unchanged": "",
        "applied": "applied-field",
        "pending": "pending-field",
        "others": "others",
        "conflict": "conflict-field",
        "same_as_target": "unchanged",
        "other_last": "others",
    }
    for row in rows:
        if "current" in row:
            status = row["status"]
            badge_html = (
                f'<span class="badge b-{classes.get(status, "unchanged")}">{h(row["status_label"])}</span>'
            )
            base_html = h(row["base"]) if row["base"] else '<span class="muted">（空）</span>'
            target_html = h(row["target"]) if row["target"] else '<span class="muted">（空）</span>'
            current_html = h(row["current"]) if row["current"] else '<span class="muted">（空）</span>'
            body.append(
                f"<tr><td>{h(row['label'])}</td><td>{base_html}</td><td>{target_html}</td>"
                f"<td>{current_html}</td>{'<td>' + badge_html + '</td>' if show_status else ''}</tr>"
            )
        else:
            body.append(
                f"<tr><td>{h(row['label'])}</td><td>{h(row['before'])}</td>"
                f"<td>{h(row['after'])}</td><td></td></tr>"
            )
    return head + "".join(body) + "</tbody></table>"
