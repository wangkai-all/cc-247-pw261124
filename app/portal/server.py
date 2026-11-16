"""The external mock portal: step-by-step data entry + receipt query + executor API.

Behaviour mirrors a legacy web system:

  1. open a form for one record, which freezes a version (``expected_ver``)
  2. enter fields step by step (校验 → 暂存/确认 → 提交)
  3. submitting requires the form token and re-checks the version; if anyone
     else changed the record in the meantime the portal refuses with 409.

Fault injection only applies to requests sent by the automated executor
(User-Agent starts with ``collector-executor``). A human browser is never
affected by simulated faults.

Every successful write bumps the record ``revision`` and appends a revision
row holding the full snapshot, the changed fields, the operator and the form
session token. The workbench uses that history as evidence when it has to
decide whether a lost-receipt submission actually landed.
"""

from __future__ import annotations

import os
import sqlite3
import time
import urllib.parse
import uuid

from ..common import (
    FIELD_LABELS,
    FIELD_NAMES,
    SERVICE_LEVELS,
    iso,
    json_dumps,
    json_loads,
    normalize_fields,
    revision_diff,
    validate_fields,
    validate_record_key,
)
from ..web import Request, Response, Router, badge, esc as h, html_response, json_response, page, redirect
from . import faults
from .faults import DEFAULT_PROFILE, FaultBook

PORTAL_NAV = [
    ("记录列表", "/records"),
    ("提交回执查询", "/query"),
    ("演练/故障注入", "/drill"),
    ("日志", "/logs"),
]

EXECUTOR_UA = "collector-executor"
FAULT_DELAY_SECONDS = float(os.environ.get("PORTAL_FAULT_DELAY_SECONDS", "5"))

_connection_factory = None


def set_connection_factory(factory) -> None:
    global _connection_factory
    _connection_factory = factory


def _conn() -> sqlite3.Connection:
    if _connection_factory is None:
        raise RuntimeError("portal connection factory not set")
    return _connection_factory()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def fetch_record(conn: sqlite3.Connection, record_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM portal_records WHERE record_key=?", (record_key,)
    ).fetchone()
    return dict(row) if row else None


def record_fields(record: dict) -> dict[str, str]:
    return {name: record.get(name, "") or "" for name in FIELD_NAMES}


def list_records(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM portal_records ORDER BY record_key").fetchall()
    return [dict(r) for r in rows]


def list_revisions(conn: sqlite3.Connection, record_key: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM portal_revisions WHERE record_key=? ORDER BY revision DESC LIMIT ?",
        (record_key, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def active_session(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM portal_sessions WHERE token=? AND archived_at IS NULL", (token,)
    ).fetchone()
    return dict(row) if row else None


def is_executor(request: Request) -> bool:
    return (request.headers.get("User-Agent") or "").startswith(EXECUTOR_UA)


def row_number_of(request: Request) -> int | None:
    value = request.headers.get("X-Row-Number")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Faults
# ---------------------------------------------------------------------------


def fault_decision(conn, request: Request, record_key: str, stage: str):
    if not is_executor(request):
        return None
    profile = request.headers.get("X-Fault-Profile") or DEFAULT_PROFILE
    _, rules = faults.load_rules(conn, profile)
    book = FaultBook(conn, profile)
    return book.decide(rules, record_key, row_number_of(request), stage)


def fault_response(decision: faults.Decision) -> Response | None:
    if decision.action == "ok":
        return None
    if decision.action == "drop_ack":
        return Response(
            status=200,
            body=json_dumps({"ok": True, "summary": "已提交", "receipt_lost": True}),
            content_type="application/json; charset=utf-8",
            headers=[("X-Simulated-Fault", "drop_ack")],
        )
    if decision.action == "timeout":
        return Response(
            status=200,
            body=json_dumps({"ok": True, "summary": "已提交", "slow": True}),
            content_type="application/json; charset=utf-8",
            headers=[("X-Simulated-Fault", "timeout")],
        )
    if decision.action == "reject":
        return Response(
            status=422,
            body=json_dumps(
                {
                    "ok": False,
                    "error": "validation_failed",
                    "message": "模拟校验失败：地址与电话组合不在允许范围内",
                    "fields": {"address": "模拟校验失败，请核对后重试"},
                }
            ),
            content_type="application/json; charset=utf-8",
            headers=[("X-Simulated-Fault", "reject")],
        )
    if decision.action == "lock":
        return Response(
            status=409,
            body=json_dumps(
                {"ok": False, "error": "locked", "message": "模拟记录被业务锁定，请稍后重试"}
            ),
            content_type="application/json; charset=utf-8",
            headers=[("X-Simulated-Fault", "lock")],
        )
    if decision.action == "5xx":
        return Response(
            status=503,
            body=json_dumps(
                {"ok": False, "error": "unavailable", "message": "模拟服务暂不可用"}
            ),
            content_type="application/json; charset=utf-8",
            headers=[("X-Simulated-Fault", "5xx")],
        )
    return None


# ---------------------------------------------------------------------------
# Submission core
# ---------------------------------------------------------------------------


class SubmitError(Exception):
    def __init__(self, code: str, message: str, fields: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields or {}


def open_session(conn: sqlite3.Connection, record_key: str) -> dict:
    record = fetch_record(conn, record_key)
    if record is None:
        raise SubmitError("record_missing", "门户中不存在该业务编号")
    token = "frm_" + uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO portal_sessions(token, record_key, expected_ver, sub_token, status, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (token, record_key, record["revision"], "sub_" + uuid.uuid4().hex[:16], "draft", iso()),
    )
    return active_session(conn, token)


def apply_submission(
    conn: sqlite3.Connection,
    session: dict,
    fields: dict[str, str],
    source: str,
    operator: str,
) -> tuple[int, dict[str, str] | None]:
    """Write a validated submission atomically. Returns (new_revision, before).

    ``before`` is None when the session was already submitted (idempotent
    replay) or when nothing changed.
    Raises SubmitError for stale version / invalid fields / missing record.
    """
    record = fetch_record(conn, session["record_key"])
    if record is None:
        raise SubmitError("record_missing", "记录已不存在")
    current = record_fields(record)
    errors = validate_fields(fields)
    if errors:
        raise SubmitError("validation_failed", "字段校验失败", errors)
    if session["status"] == "submitted":
        return record["revision"], None
    if record["revision"] != session["expected_ver"]:
        raise SubmitError(
            "version_conflict",
            f"记录已被修改（表单基于版本 {session['expected_ver']}，当前 {record['revision']}）",
        )
    changed = revision_diff(current, fields)
    if not changed:
        conn.execute(
            "UPDATE portal_sessions SET status='submitted', submitted_at=?, result=?, draft=?"
            " WHERE token=?",
            (iso(), "noop", json_dumps(fields), session["token"]),
        )
        return record["revision"], None
    new_rev = record["revision"] + 1
    conn.execute(
        "UPDATE portal_records SET full_name=?, phone=?, address=?, service_level=?, note=?,"
        " revision=?, updated_at=?, updated_by=? WHERE record_key=?",
        (
            fields["full_name"],
            fields["phone"],
            fields["address"],
            fields["service_level"],
            fields["note"],
            new_rev,
            iso(),
            operator,
            session["record_key"],
        ),
    )
    conn.execute(
        "INSERT INTO portal_revisions(record_key, revision, source, operator, session_token,"
        " changed_fields, snapshot, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            session["record_key"],
            new_rev,
            source,
            operator,
            session["token"],
            json_dumps(
                {c["field"]: {"before": c["before"], "after": c["after"]} for c in changed}
            ),
            json_dumps(fields),
            iso(),
        ),
    )
    result = "rcpt_" + session["sub_token"].split("_")[-1][:10]
    conn.execute(
        "UPDATE portal_sessions SET status='submitted', submitted_at=?, result=?, draft=?"
        " WHERE token=?",
        (iso(), result, json_dumps(fields), session["token"]),
    )
    return new_rev, current


def create_record(conn: sqlite3.Connection, record_key: str, fields: dict[str, str] | None = None) -> None:
    err = validate_record_key(record_key)
    if err:
        raise SubmitError("invalid_key", err)
    if fetch_record(conn, record_key):
        raise SubmitError("duplicate_key", "业务编号已存在")
    fields = normalize_fields(fields)
    has_content = bool(fields["full_name"])
    conn.execute(
        "INSERT INTO portal_records(record_key, full_name, phone, address, service_level, note,"
        " revision, updated_at, updated_by) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            record_key,
            fields["full_name"],
            fields["phone"],
            fields["address"],
            fields["service_level"],
            fields["note"],
            1 if has_content else 0,
            iso(),
            "portal-user",
        ),
    )
    if has_content:
        conn.execute(
            "INSERT INTO portal_revisions(record_key, revision, source, operator, session_token,"
            " changed_fields, snapshot, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (record_key, 1, "manual", "portal-user", "", json_dumps(fields), json_dumps(fields), iso()),
        )


# ---------------------------------------------------------------------------
# Human-facing pages
# ---------------------------------------------------------------------------


def page_records(conn: sqlite3.Connection, message: str = "", error: str = "") -> str:
    rows = list_records(conn)
    body = []
    if message:
        body.append(f'<div class="note">{h(message)}</div>')
    if error:
        body.append(f'<div class="note bad">{h(error)}</div>')
    body.append(
        '<div class="card"><table><thead><tr><th>业务编号</th><th>姓名</th><th>电话</th>'
        "<th>地址</th><th>档位</th><th>版本</th><th>更新时间</th><th></th></tr></thead><tbody>"
    )
    for row in rows:
        body.append(
            "<tr>"
            f'<td class="mono"><a href="/record/{h(row["record_key"])}">{h(row["record_key"])}</a></td>'
            f'<td>{h(row["full_name"])}</td><td class="mono">{h(row["phone"])}</td>'
            f'<td>{h(row["address"])}</td>'
            f'<td>{h(SERVICE_LEVELS.get(row["service_level"], row["service_level"]))}</td>'
            f'<td>{row["revision"]}</td><td class="muted">{h(row["updated_at"])}</td>'
            f'<td><a class="btn small secondary" href="/record/{h(row["record_key"])}">分步录入</a></td>'
            "</tr>"
        )
    body.append("</tbody></table></div>")
    body.append(
        '<div class="note">这是模拟的外部门户，只能逐条分步录入；你也可以直接修改一条记录，'
        "模拟“他人手工修改”。<a href=\"/reset\">重置门户数据</a>（仅本地虚构数据）。</div>"
    )
    body.append(
        '<div class="card"><h3>新增虚构记录</h3>'
        '<form method="post" action="/record/create"><div class="actions">'
        '<input style="max-width:220px" type="text" name="record_key" placeholder="如 HK-1006">'
        '<button class="btn secondary" type="submit">新增空记录</button></div></form></div>'
    )
    return page(
        "门户 · 记录列表",
        "".join(body),
        nav=PORTAL_NAV,
        active="记录列表",
        subtitle=f"共 {len(rows)} 条虚构记录；所有改动都会生成版本历史。",
    )


def _steps(session: dict | None) -> str:
    opened = session is not None
    staged = bool(session and session["draft"])
    submitted = bool(session and session["status"] == "submitted")
    classes = [
        "current" if not opened else "done",
        "current" if opened and not staged and not submitted else ("done" if opened else ""),
        "current" if staged and not submitted else ("done" if submitted else ""),
        "current" if staged and not submitted else ("done" if submitted else ""),
    ]
    labels = ["1. 打开表单", "2. 逐字段校验", "3. 确认", "4. 提交"]
    return '<div class="steps">' + "".join(
        f'<div class="s {cls}">{h(label)}</div>' for label, cls in zip(labels, classes)
    ) + "</div>"


def page_record(
    conn: sqlite3.Connection,
    record_key: str,
    token: str | None,
    staged: dict | None,
    errors: dict[str, str],
    message: str = "",
    error: str = "",
) -> str:
    record = fetch_record(conn, record_key)
    if record is None:
        return page(
            "门户 · 记录不存在",
            f'<div class="note bad">门户中不存在业务编号 {h(record_key)}。</div>'
            '<p><a class="btn secondary" href="/records">返回列表</a></p>',
            nav=PORTAL_NAV,
            active="记录列表",
        )
    session = active_session(conn, token) if token else None
    current = record_fields(record)
    expected_ver = session["expected_ver"] if session else record["revision"]
    stale = bool(session) and expected_ver != record["revision"]
    if staged is None:
        staged = normalize_fields(json_loads(session["draft"], None)) if session and session["draft"] else current

    body = [_steps(session)]
    if message:
        body.append(f'<div class="note">{h(message)}</div>')
    if error:
        body.append(f'<div class="note bad">{h(error)}</div>')
    if stale:
        body.append(
            '<div class="note bad">此表单打开后记录已被他人修改（当前版本 '
            f'{record["revision"]}，表单基于版本 {expected_ver}）。请关闭并重新打开表单，'
            "否则提交会被门户拒绝。</div>"
        )
    if session and session["status"] == "submitted":
        body.append(
            '<div class="note">本次会话已提交。'
            '<div class="kv">'
            f'<div class="k">回执编号</div><div class="mono">{h(session["result"])}</div>'
            f'<div class="k">回执令牌</div><div class="mono">{h(session["sub_token"] or "")}</div>'
            f'<div class="k">提交时间</div><div>{h(session["submitted_at"])}</div>'
            "</div></div>"
        )

    body.append('<div class="card"><div class="kv">')
    body.append(f'<div class="k">业务编号</div><div class="mono">{h(record_key)}</div>')
    body.append(
        f'<div class="k">门户当前版本</div><div>{record["revision"]}'
        f'（{h(record["updated_at"])}，{h(record["updated_by"])}）</div>'
    )
    if session:
        body.append(f'<div class="k">表单令牌</div><div class="mono">{h(session["token"])}</div>')
        body.append(f'<div class="k">表单基于版本</div><div>{expected_ver}</div>')
    body.append("</div></div>")

    if not session:
        body.append(
            f'<form method="post" action="/record/{h(record_key)}/open"><div class="actions">'
            '<button class="btn" type="submit">打开分步录入表单</button></div></form>'
        )
    else:
        body.append(f'<form method="post" action="/record/{h(record_key)}/stage">')
        body.append(f'<input type="hidden" name="session_token" value="{h(session["token"])}">')
        body.append('<div class="card"><h3>逐字段录入</h3>')
        for name in FIELD_NAMES:
            value = staged.get(name, "")
            err = errors.get(name, "")
            label = FIELD_LABELS[name]
            if name == "service_level":
                options = "".join(
                    f'<option value="{h(k)}" {"selected" if value == k else ""}>{h(v)}</option>'
                    for k, v in SERVICE_LEVELS.items()
                )
                control = f'<select name="{name}">{options}</select>'
            elif name == "note":
                control = f'<textarea name="{name}" rows="2">{h(value)}</textarea>'
            else:
                control = f'<input type="text" name="{name}" value="{h(value)}">'
            err_html = f'<div class="bad" style="font-size:12px">{h(err)}</div>' if err else ""
            body.append(f'<div class="form-row"><label>{h(label)}</label>{control}{err_html}</div>')
        body.append(
            '<div class="actions"><button class="btn secondary" type="submit" name="action" value="validate">校验</button>'
            '<button class="btn secondary" type="submit" name="action" value="stage">暂存并进入确认</button>'
            "</div></div></form>"
        )
        if session["draft"]:
            body.append('<div class="card"><h3>确认后提交</h3>')
            body.append(
                '<table class="field-table"><thead><tr><th>字段</th><th>门户当前</th>'
                "<th>将写入</th></tr></thead><tbody>"
            )
            for name in FIELD_NAMES:
                old, new = current[name], staged.get(name, "")
                changed = old != new
                old_html = f'<span class="old">{h(old)}</span>' if changed else h(old)
                body.append(
                    f"<tr><td>{h(FIELD_LABELS[name])}</td><td>{old_html}</td>"
                    f"<td>{('<b>' + h(new) + '</b>') if changed else h(new)}</td></tr>"
                )
            body.append("</tbody></table>")
            body.append(f'<form method="post" action="/record/{h(record_key)}/submit">')
            body.append(f'<input type="hidden" name="session_token" value="{h(session["token"])}">')
            body.append(
                '<div class="actions"><button class="btn" type="submit">确认提交</button>'
                f'<a class="btn secondary" href="/record/{h(record_key)}">刷新/关闭表单</a></div>'
                "</form></div>"
            )

    revisions = list_revisions(conn, record_key, 10)
    body.append(
        '<h2>版本历史（最近 10 条）</h2><div class="card"><table><thead><tr>'
        "<th>版本</th><th>来源</th><th>操作人</th><th>变更</th><th>时间</th></tr></thead><tbody>"
    )
    for rev in revisions:
        changed = json_loads(rev["changed_fields"], {}) or {}
        if rev["source"] == "seed":
            summary = "初始数据"
        else:
            summary = "；".join(
                f"{FIELD_LABELS.get(k, k)}: {v.get('after') if isinstance(v, dict) else v}"
                for k, v in changed.items()
            ) or "（无字段变化）"
        body.append(
            f'<tr><td>{rev["revision"]}</td><td>{h(rev["source"])}</td>'
            f'<td>{h(rev["operator"])}</td><td class="muted">{h(summary)}</td>'
            f'<td class="muted">{h(rev["created_at"])}</td></tr>'
        )
    body.append("</tbody></table></div>")
    body.append('<p><a class="btn secondary" href="/records">返回列表</a></p>')
    return page(
        f"门户 · {record_key} 分步录入",
        "".join(body),
        nav=PORTAL_NAV,
        active="记录列表",
        subtitle="必须按顺序打开表单 → 校验 → 确认 → 提交；提交时校验版本。",
    )


def page_query(conn: sqlite3.Connection, sub_token: str = "", error: str = "") -> str:
    body = []
    if error:
        body.append(f'<div class="note bad">{h(error)}</div>')
    body.append(
        '<div class="card"><form method="post" action="/query">'
        '<div class="form-row"><label>回执令牌（提交成功后门户返回）</label>'
        f'<input type="text" name="sub_token" value="{h(sub_token)}" placeholder="sub_..."></div>'
        '<div class="actions"><button class="btn" type="submit">查询</button></div></form></div>'
    )
    if sub_token:
        row = conn.execute(
            "SELECT * FROM portal_sessions WHERE sub_token=?", (sub_token,)
        ).fetchone()
        if row is None:
            body.append(
                '<div class="note bad">没有找到该回执令牌对应的提交记录。'
                "被模拟故障吞掉回执的提交也会保留回执，因此“查不到”意味着请求从未到达门户。</div>"
            )
        else:
            session = dict(row)
            record = fetch_record(conn, session["record_key"]) or {}
            body.append('<div class="card"><h3>回执详情</h3><div class="kv">')
            body.append(f'<div class="k">回执令牌</div><div class="mono">{h(session["sub_token"])}</div>')
            body.append(f'<div class="k">业务编号</div><div class="mono">{h(session["record_key"])}</div>')
            body.append(
                f'<div class="k">会话状态</div><div>'
                f'{badge("applied" if session["status"] == "submitted" else "pending", session["status"])}</div>'
            )
            body.append(f'<div class="k">回执编号</div><div class="mono">{h(session["result"])}</div>')
            body.append(f'<div class="k">提交时间</div><div>{h(session["submitted_at"])}</div>')
            body.append(f'<div class="k">记录当前版本</div><div>{record.get("revision", "?")}</div>')
            body.append("</div></div>")
            target = json_loads(session["draft"], None)
            if target and record:
                body.append('<div class="card"><h3>提交内容与门户当前值</h3>')
                body.append(
                    '<table class="field-table"><thead><tr><th>字段</th><th>提交内容</th>'
                    "<th>门户当前</th><th></th></tr></thead><tbody>"
                )
                for name in FIELD_NAMES:
                    cur = record.get(name, "")
                    want = target.get(name, "")
                    mark = '<span class="ok">一致</span>' if cur == want else '<span class="warn">不一致</span>'
                    body.append(
                        f"<tr><td>{h(FIELD_LABELS[name])}</td><td>{h(want)}</td>"
                        f"<td>{h(cur)}</td><td>{mark}</td></tr>"
                    )
                body.append("</tbody></table></div>")
    return page(
        "门户 · 提交回执查询",
        "".join(body),
        nav=PORTAL_NAV,
        active="提交回执查询",
        subtitle="用回执令牌确认一次提交是否真的在门户生效。",
    )


def page_drill(conn: sqlite3.Connection, profile: str, message: str = "", error: str = "") -> str:
    text, rules = faults.load_rules(conn, profile)
    recent = conn.execute(
        "SELECT record_key, revision, source, operator, created_at FROM portal_revisions"
        " ORDER BY id DESC LIMIT 12"
    ).fetchall()
    body = []
    if message:
        body.append(f'<div class="note">{h(message)}</div>')
    if error:
        body.append(f'<div class="note bad">{h(error)}</div>')
    body.append(
        '<div class="note">故障规则只作用于自动执行器（User-Agent 以 '
        f'<span class="mono">{h(EXECUTOR_UA)}</span> 开头的请求）；浏览器里的手工录入永远不会遇到模拟故障。'
        ' 规则可在执行器命令行用 <span class="mono">--faults</span> 临时覆盖。</div>'
    )
    body.append('<div class="card"><form method="post" action="/drill/save">')
    body.append(
        f'<div class="form-row"><label>配置名</label>'
        f'<input type="text" name="profile" value="{h(profile)}"></div>'
    )
    body.append(f'<label>规则</label><textarea name="rules" rows="10" class="mono">{h(text)}</textarea>')
    body.append(
        '<div class="actions"><button class="btn" type="submit">保存规则</button>'
        '<button class="btn secondary" type="submit" formaction="/drill/reset">重置故障计数</button></div>'
        "</form></div>"
    )
    book = FaultBook(conn, profile)
    body.append(
        '<div class="card"><h3>当前已解析规则</h3><table><thead><tr>'
        "<th>匹配</th><th>动作</th><th>次数</th><th>阶段</th><th>已触发</th></tr></thead><tbody>"
    )
    for rule in rules:
        used = book.used(rule)
        body.append(
            f'<tr><td class="mono">{h(rule.pattern)}</td><td>{h(rule.action)}</td>'
            f"<td>{'不限' if rule.count >= 10**9 else rule.count}</td>"
            f"<td>{h(rule.stage)}</td><td>{used}</td></tr>"
        )
    body.append("</tbody></table>")
    body.append(
        '<p class="muted">动作：ok 正常；timeout 处理已生效但响应超时；drop_ack 写入成功但连接被断开'
        "（回执丢失）；reject 422 校验失败；lock 409 记录被锁；5xx 服务不可用。</p></div>"
    )
    body.append(
        "<h2>门户最近版本</h2><div class=\"card\"><table><thead><tr>"
        "<th>记录</th><th>版本</th><th>来源</th><th>操作人</th><th>时间</th></tr></thead><tbody>"
    )
    for row in recent:
        body.append(
            f'<tr><td class="mono">{h(row["record_key"])}</td><td>{row["revision"]}</td>'
            f'<td>{h(row["source"])}</td><td>{h(row["operator"])}</td>'
            f'<td class="muted">{h(row["created_at"])}</td></tr>'
        )
    body.append("</tbody></table></div>")
    return page(
        "门户 · 演练/故障注入",
        "".join(body),
        nav=PORTAL_NAV,
        active="演练/故障注入",
        subtitle="模拟响应超时、校验失败、回执丢失；全部为本地虚构数据。",
    )


def page_logs(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT * FROM wb_events ORDER BY id DESC LIMIT 200").fetchall()
    body = [
        '<div class="card"><table><thead><tr><th>时间</th><th>来源</th><th>动作</th>'
        "<th>详情</th></tr></thead><tbody>"
    ]
    for row in rows:
        body.append(
            f'<tr><td class="muted mono">{h(row["at"])}</td><td>{h(row["actor"])}</td>'
            f'<td class="mono">{h(row["action"])}</td><td>{h(row["detail"])}</td></tr>'
        )
    body.append("</tbody></table></div>")
    return page(
        "门户/工作台 · 事件日志",
        "".join(body),
        nav=PORTAL_NAV,
        active="日志",
        subtitle="门户与工作台共写的审计日志（最近 200 条）。",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def build_router() -> Router:
    router = Router()

    def records(req: Request) -> Response:
        conn = _conn()
        try:
            return html_response(page_records(conn, req.q("message"), req.q("error")))
        finally:
            conn.close()

    def record_page(req: Request) -> Response:
        conn = _conn()
        try:
            key = urllib.parse.unquote(req.match.group("key"))
            return html_response(page_record(conn, key, req.q("session") or None, None, {}))
        finally:
            conn.close()

    def open_form(req: Request) -> Response:
        conn = _conn()
        try:
            key = urllib.parse.unquote(req.match.group("key"))
            decision = fault_decision(conn, req, key, "open")
            if decision and decision.action in {"reject", "lock", "5xx"}:
                return fault_response(decision)
            session = open_session(conn, key)
            if decision and decision.action == "timeout":
                time.sleep(FAULT_DELAY_SECONDS)
            return redirect(f"/record/{urllib.parse.quote(key)}?session={session['token']}")
        except SubmitError as exc:
            return redirect("/records?error=" + urllib.parse.quote(exc.message))
        finally:
            conn.close()

    def new_record(req: Request) -> Response:
        conn = _conn()
        try:
            form = req.form()
            key = form.get("record_key", "").strip().upper()
            fields = normalize_fields(form)
            create_record(conn, key, fields if fields["full_name"] else None)
            return redirect(f"/record/{urllib.parse.quote(key)}")
        except SubmitError as exc:
            return redirect("/records?error=" + urllib.parse.quote(exc.message))
        finally:
            conn.close()

    def stage(req: Request) -> Response:
        conn = _conn()
        try:
            key = urllib.parse.unquote(req.match.group("key"))
            form = req.form()
            session = active_session(conn, form.get("session_token", ""))
            if not session or session["record_key"] != key:
                return redirect("/records?error=" + urllib.parse.quote("表单会话已失效，请重新打开"))
            fields = normalize_fields(form)
            errors = validate_fields(fields)
            if form.get("action") == "validate":
                message = "校验通过，可以进入确认。" if not errors else ""
                return html_response(page_record(conn, key, session["token"], fields, errors, message=message))
            if errors:
                return html_response(
                    page_record(
                        conn, key, session["token"], fields, errors,
                        error="存在校验错误，无法进入确认。",
                    )
                )
            conn.execute(
                "UPDATE portal_sessions SET draft=? WHERE token=?",
                (json_dumps(fields), session["token"]),
            )
            return redirect(f"/record/{urllib.parse.quote(key)}?session={session['token']}")
        finally:
            conn.close()

    def submit_human(req: Request) -> Response:
        conn = _conn()
        try:
            key = urllib.parse.unquote(req.match.group("key"))
            form = req.form()
            session = active_session(conn, form.get("session_token", ""))
            if not session or session["record_key"] != key:
                return redirect("/records?error=" + urllib.parse.quote("表单会话已失效，请重新打开"))
            draft = normalize_fields(json_loads(session["draft"], None))
            if not draft["full_name"]:
                return redirect(
                    f"/record/{urllib.parse.quote(key)}?session={session['token']}"
                    "&error=" + urllib.parse.quote("尚未暂存任何字段")
                )
            try:
                new_rev, _before = apply_submission(conn, session, draft, "manual", "portal-user")
            except SubmitError as exc:
                if exc.code == "version_conflict":
                    return html_response(
                        page_record(
                            conn, key, None, None, {},
                            error="提交被拒绝：记录已被他人修改（版本冲突）。请重新打开表单后再提交。",
                        ),
                        status=409,
                    )
                return html_response(
                    page_record(conn, key, session["token"], draft, exc.fields, error=exc.message)
                )
            fresh = active_session(conn, session["token"])
            return html_response(
                page_record(
                    conn, key, fresh["token"], draft, {},
                    message=f"提交成功，门户版本 {new_rev}。回执令牌：{fresh['sub_token']}",
                )
            )
        finally:
            conn.close()

    def query(req: Request) -> Response:
        conn = _conn()
        try:
            return html_response(page_query(conn, req.q("sub_token")))
        finally:
            conn.close()

    def query_post(req: Request) -> Response:
        token = req.form().get("sub_token", "")
        return redirect("/query?sub_token=" + urllib.parse.quote(token))

    def drill(req: Request) -> Response:
        conn = _conn()
        try:
            return html_response(
                page_drill(conn, req.q("profile") or DEFAULT_PROFILE, req.q("message"), req.q("error"))
            )
        finally:
            conn.close()

    def drill_save(req: Request) -> Response:
        conn = _conn()
        try:
            form = req.form()
            profile = (form.get("profile") or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
            errors = faults.save_rules(conn, profile, form.get("rules", ""))
            if errors:
                return redirect(
                    f"/drill?profile={urllib.parse.quote(profile)}&error="
                    + urllib.parse.quote("；".join(errors))
                )
            return redirect(f"/drill?profile={urllib.parse.quote(profile)}&message=规则已保存")
        finally:
            conn.close()

    def drill_reset(req: Request) -> Response:
        conn = _conn()
        try:
            FaultBook.reset(conn)
            return redirect("/drill?message=故障计数已清零")
        finally:
            conn.close()

    def logs(req: Request) -> Response:
        conn = _conn()
        try:
            return html_response(page_logs(conn))
        finally:
            conn.close()

    def reset(req: Request) -> Response:
        conn = _conn()
        try:
            from ..storage import init_db

            conn.execute("DELETE FROM portal_sessions")
            conn.execute("DELETE FROM portal_revisions")
            conn.execute("DELETE FROM portal_records")
            conn.execute("DELETE FROM wb_settings WHERE key LIKE 'fault_used:%'")
            init_db(conn, portal_seed=True)
            return redirect("/records?message=" + urllib.parse.quote("门户已重置为初始虚构数据"))
        finally:
            conn.close()

    # -- executor JSON API ------------------------------------------------
    def api_records(req: Request) -> Response:
        conn = _conn()
        try:
            return json_response({"records": list_records(conn)})
        finally:
            conn.close()

    def api_record(req: Request) -> Response:
        conn = _conn()
        try:
            key = urllib.parse.unquote(req.match.group("key"))
            record = fetch_record(conn, key)
            if not record:
                return json_response({"ok": False, "error": "not_found"}, status=404)
            history = [
                {
                    "revision": rev["revision"],
                    "source": rev["source"],
                    "operator": rev["operator"],
                    "session_token": rev["session_token"],
                    "changed_fields": json_loads(rev["changed_fields"], {}),
                    "created_at": rev["created_at"],
                }
                for rev in list_revisions(conn, key, 20)
            ]
            return json_response(
                {
                    "ok": True,
                    "record_key": key,
                    "revision": record["revision"],
                    "fields": record_fields(record),
                    "updated_at": record["updated_at"],
                    "updated_by": record["updated_by"],
                    "history": history,
                }
            )
        finally:
            conn.close()

    def api_open(req: Request) -> Response:
        conn = _conn()
        try:
            body = req.json()
            key = (body.get("record_key") or "").strip().upper()
            decision = fault_decision(conn, req, key, "open")
            if decision and decision.action in {"reject", "lock", "5xx"}:
                return fault_response(decision)
            # open never writes the record, but it does create the form session.
            # A simulated timeout must delay the *real* response, not replace it
            # with a body the executor would misread as an empty record.
            session = open_session(conn, key)
            record = fetch_record(conn, key)
            payload = {
                "ok": True,
                "session_token": session["token"],
                "sub_token": session["sub_token"],
                "expected_version": session["expected_ver"],
                "fields": record_fields(record),
            }
            if decision and decision.action == "drop_ack":
                return Response(status=200, body=b"", drop=True)
            if decision and decision.action == "timeout":
                time.sleep(FAULT_DELAY_SECONDS)
            return json_response(payload)
        except SubmitError as exc:
            status = 404 if exc.code == "record_missing" else 400
            return json_response({"ok": False, "error": exc.code, "message": exc.message}, status=status)
        finally:
            conn.close()

    def api_stage(req: Request) -> Response:
        conn = _conn()
        try:
            body = req.json()
            session = active_session(conn, body.get("session_token", ""))
            if not session:
                return json_response(
                    {"ok": False, "error": "session_invalid", "message": "表单会话无效或已关闭"}, status=409
                )
            if session["status"] == "submitted":
                return json_response(
                    {"ok": False, "error": "already_submitted", "message": "该表单会话已提交"}, status=409
                )
            fields = normalize_fields(body.get("fields"))
            errors = validate_fields(fields)
            if errors:
                return json_response(
                    {"ok": False, "error": "validation_failed", "message": "字段校验失败", "fields": errors},
                    status=422,
                )
            conn.execute(
                "UPDATE portal_sessions SET draft=? WHERE token=?",
                (json_dumps(fields), session["token"]),
            )
            return json_response({"ok": True})
        finally:
            conn.close()

    def api_submit(req: Request) -> Response:
        conn = _conn()
        try:
            body = req.json()
            token = body.get("session_token", "")
            session = active_session(conn, token)
            if not session:
                return json_response(
                    {"ok": False, "error": "session_invalid", "message": "表单会话无效或已关闭"}, status=409
                )
            draft = normalize_fields(json_loads(session["draft"], None))
            key = session["record_key"]
            decision = fault_decision(conn, req, key, "submit")
            if decision and decision.action in {"reject", "lock", "5xx"}:
                return fault_response(decision)
            if not draft["full_name"]:
                return json_response(
                    {"ok": False, "error": "not_staged", "message": "尚未暂存任何字段"}, status=409
                )
            try:
                new_rev, before = apply_submission(conn, session, draft, "collector", "collector-executor")
            except SubmitError as exc:
                status = 409 if exc.code in {"version_conflict", "already_submitted"} else 422
                payload = {
                    "ok": False,
                    "error": exc.code,
                    "message": exc.message,
                    "fields": exc.fields,
                }
                payload.update(_state_payload(conn, key))
                return json_response(payload, status=status)
            payload = {
                "ok": True,
                "receipt": "rcpt_" + session["sub_token"].split("_")[-1][:10],
                "sub_token": session["sub_token"],
                "revision_before": session["expected_ver"],
                "revision_after": new_rev,
                "changed": before is not None and before != draft,
            }
            payload.update(_state_payload(conn, key))
            if decision and decision.action == "drop_ack":
                # Write is committed; the response is never delivered.
                return Response(status=200, body=b"", drop=True)
            if decision and decision.action == "timeout":
                time.sleep(FAULT_DELAY_SECONDS)
            return json_response(payload)
        finally:
            conn.close()

    def api_receipt(req: Request) -> Response:
        conn = _conn()
        try:
            token = urllib.parse.unquote(req.match.group("token"))
            row = conn.execute(
                "SELECT * FROM portal_sessions WHERE sub_token=?", (token,)
            ).fetchone()
            if not row:
                return json_response({"ok": False, "error": "no_such_receipt"}, status=404)
            session = dict(row)
            record = fetch_record(conn, session["record_key"])
            return json_response(
                {
                    "ok": True,
                    "receipt": {
                        "sub_token": session["sub_token"],
                        "record_key": session["record_key"],
                        "status": session["status"],
                        "result": session["result"],
                        "submitted_at": session["submitted_at"],
                        "target": json_loads(session["draft"], {}),
                    },
                    "record": record_fields(record) if record else None,
                    "revision": record["revision"] if record else None,
                }
            )
        finally:
            conn.close()

    router.get(r"/", records)
    router.get(r"/records", records)
    router.get(r"/record/(?P<key>[^/]+)", record_page)
    router.post(r"/record/create", new_record)
    router.post(r"/record/(?P<key>[^/]+)/open", open_form)
    router.post(r"/record/(?P<key>[^/]+)/stage", stage)
    router.post(r"/record/(?P<key>[^/]+)/submit", submit_human)
    router.get(r"/query", query)
    router.post(r"/query", query_post)
    router.get(r"/drill", drill)
    router.post(r"/drill/save", drill_save)
    router.post(r"/drill/reset", drill_reset)
    router.get(r"/logs", logs)
    router.get(r"/reset", reset)
    router.get(r"/api/records", api_records)
    router.get(r"/api/record/(?P<key>[^/]+)", api_record)
    router.get(r"/api/receipt/(?P<token>[^/]+)", api_receipt)
    router.post(r"/api/executor/open", api_open)
    router.post(r"/api/executor/stage", api_stage)
    router.post(r"/api/executor/submit", api_submit)
    return router


def _state_payload(conn: sqlite3.Connection, key: str) -> dict:
    record = fetch_record(conn, key)
    if not record:
        return {"current_record": None, "current_revision": None}
    return {
        "current_record": record_fields(record),
        "current_revision": record["revision"],
    }
