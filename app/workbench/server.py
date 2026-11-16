"""Collection workbench web UI and API.

Pages: import queue, batch detail, row detail (takeover / reconcile / manual
submit), conflict queue, executor view, settings/drill, and the final check
against the original portal.
"""

from __future__ import annotations

import urllib.parse

from .. import storage
from ..common import (
    FIELD_LABELS,
    FIELD_NAMES,
    SERVICE_LEVELS,
    field_diff,
    iso,
    json_dumps,
    json_loads,
    normalize_fields,
)
from ..portal import faults as portal_faults
from ..web import Request, Response, Router, badge, esc as h, html_response, json_response, page, redirect
from . import engine, runner, views
from .portal_client import PortalError
from .views import (
    COMPARE_LABELS,
    REASON_LABELS,
    STATUS_LABELS,
    compare_badge,
    diff_table,
    q,
    status_badge,
)

NAV = [
    ("总览", "/"),
    ("导入与队列", "/queue"),
    ("冲突队列", "/conflicts"),
    ("执行器", "/executors"),
    ("最终核对", "/verify"),
    ("设置/演练", "/settings"),
]

_connection_factory = None


def set_connection_factory(factory) -> None:
    global _connection_factory
    _connection_factory = factory


def _conn():
    if _connection_factory is None:
        raise RuntimeError("workbench connection factory not set")
    return _connection_factory()


def _portal_link(conn, path: str) -> str:
    base = engine.get_portal_url(conn)
    return base + path


def _flash(req: Request) -> str:
    message = req.q("message")
    error = req.q("error")
    html = ""
    if message:
        html += f'<div class="note">{h(message)}</div>'
    if error:
        html += f'<div class="note bad">{h(error)}</div>'
    return html


def _actor(req: Request) -> str:
    return req.form().get("operator", "reviewer").strip() or "reviewer"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def page_dashboard(conn, flash: str = "") -> str:
    counts = views.status_counts(conn)
    batches = views.list_batches(conn)
    conflicts = engine.conflict_list(conn)
    body = [flash]
    body.append(
        '<div class="grid">'
        + "".join(
            f'<div class="stat"><div class="n">{counts.get(key, 0)}</div>'
            f'<div class="l">{h(label)}</div></div>'
            for key, label in [
                ("pending", "待执行"),
                ("retry_wait", "待补交"),
                ("needs_reconcile", "待恢复核对"),
                ("conflict", "冲突队列"),
                ("applied", "已生效"),
                ("resolved", "人工已处理"),
                ("noop", "无需提交"),
                ("invalid", "导入无效/校验失败"),
            ]
        )
        + "</div>"
    )
    if conflicts:
        body.append(
            f'<div class="note warn">有 <b>{len(conflicts)}</b> 条记录需要人工决定：'
            f'<a href="/conflicts">进入冲突队列</a>。自动执行器不会覆盖这些记录。</div>'
        )
    body.append(
        '<div class="note">工作流：导入 CSV → 预检（记录提交前版本）→ 执行器逐条提交 → '
        "结果未知的自动与门户核对 → 冲突进队列人工接管 → 在原门户查最终记录。"
        "所有姓名/电话/地址均为本地虚构数据。</div>"
    )
    body.append('<h2>批次</h2>')
    if not batches:
        body.append('<div class="card"><p class="muted">还没有批次。先到 <a href="/queue">导入与队列</a> 上传 CSV。</p></div>')
    else:
        body.append(
            '<div class="card"><table><thead><tr><th>批次</th><th>名称</th><th>总行数</th>'
            "<th>完成</th><th>冲突</th><th>剩余</th><th>创建时间</th></tr></thead><tbody>"
        )
        for b in batches:
            body.append(
                f'<tr><td>#{b["id"]}</td><td><a href="/batch/{b["id"]}">{h(b["name"])}</a></td>'
                f'<td>{b["row_count"]}</td><td>{b["done"] or 0}</td>'
                f'<td>{b["conflicts"] or 0}</td><td>{b["remaining"] or 0}</td>'
                f'<td class="muted">{h(b["created_at"])}</td></tr>'
            )
        body.append("</tbody></table></div>")
    body.append('<h2>最近事件</h2>')
    body.append(_events_table(views.recent_events(conn, 25)))
    return page("采集工作台 · 总览", "".join(body), nav=NAV, active="总览",
                subtitle="CSV 采集、提交回执核对、冲突接管与最终门户核对。")


def _events_table(events: list[dict]) -> str:
    body = [
        '<div class="card"><table><thead><tr><th>时间</th><th>来源</th><th>动作</th>'
        "<th>详情</th></tr></thead><tbody>"
    ]
    for e in events:
        body.append(
            f'<tr><td class="muted mono">{h(e["at"])}</td><td>{h(e["actor"])}</td>'
            f'<td class="mono">{h(e["action"])}</td><td>{h(e["detail"])}</td></tr>'
        )
    body.append("</tbody></table></div>")
    return "".join(body)


def page_queue(conn, flash: str = "") -> str:
    batches = views.list_batches(conn)
    body = [flash]
    body.append(
        '<div class="card"><h2 style="margin-top:0">导入 CSV</h2>'
        '<p class="muted">必需列：record_key（业务编号）、full_name、phone、address、'
        "service_level（basic/standard/priority）、note。也接受中文表头。"
        "导入后会立即做一次预检，记录每行的“提交前版本”。</p>"
        '<form method="post" action="/import" enctype="multipart/form-data">'
        '<div class="form-row"><label>批次名称</label>'
        '<input type="text" name="name" placeholder="例如：2026-11 梧桐巷入户采集"></div>'
        '<div class="form-row"><label>CSV 文件</label><input type="file" name="file" accept=".csv,text/csv"></div>'
        '<div class="actions"><button class="btn" type="submit">导入并预检</button></div></form>'
        '<p class="muted">也可以直接粘贴 CSV：</p>'
        '<form method="post" action="/import">'
        '<div class="form-row"><label>批次名称</label><input type="text" name="name" placeholder="粘贴导入"></div>'
        '<label>CSV 内容</label><textarea name="csv" rows="6" class="mono" '
        'placeholder="record_key,full_name,phone,address,service_level,note"></textarea>'
        '<div class="actions"><button class="btn secondary" type="submit">从文本导入</button></div></form>'
        '<p class="muted">示例文件：<a href="/sample.csv">examples/demo_batch.csv</a></p></div>'
    )
    body.append("<h2>批次队列</h2>")
    if not batches:
        body.append('<div class="card"><p class="muted">暂无批次。</p></div>')
    for b in batches:
        rows = views.batch_rows(conn, b["id"])
        if any(r["status"] in ("pending", "retry_wait") for r in rows):
            action = (
                f'<form method="post" action="/batch/{b["id"]}/run" style="display:inline">'
                '<button class="btn small" type="submit">运行本批次</button></form> '
            )
        else:
            action = ""
        body.append(
            f'<div class="card"><div class="actions"><b>批次 #{b["id"]} · {h(b["name"])}</b>'
            f'<span class="muted">共 {b["row_count"]} 行</span>{action}'
            f'<a class="btn secondary small" href="/batch/{b["id"]}">查看明细</a></div>'
        )
        body.append(_rows_table(rows, show_batch=False))
        body.append("</div>")
    return page("采集工作台 · 导入与队列", "".join(body), nav=NAV, active="导入与队列",
                subtitle="按业务键导入；重复的未完成业务键会被拒绝，避免同一条记录被两个批次同时提交。")


def _rows_table(rows: list[dict], show_batch: bool = True) -> str:
    if not rows:
        return '<p class="muted">没有行。</p>'
    head = "<table><thead><tr>"
    if show_batch:
        head += "<th>批次</th>"
    head += (
        "<th>业务编号</th><th>姓名</th><th>状态</th><th>比对</th><th>最近说明</th>"
        "<th>尝试</th><th></th></tr></thead><tbody>"
    )
    body = []
    for row in rows:
        target = row["target_fields"]
        action = f'<a class="btn small secondary" href="/row/{row["id"]}">查看</a>'
        name_cell = h(target.get("full_name", ""))
        batch_cell = f'<td><a href="/batch/{row["batch_id"]}">#{row["batch_id"]}</a></td>' if show_batch else ""
        body.append(
            f'<tr>{batch_cell}'
            f'<td class="mono">{h(row["record_key"])}</td><td>{name_cell}</td>'
            f"<td>{status_badge(row['status'])}</td><td>{compare_badge(row['compare_status'])}</td>"
            f'<td class="muted">{h(row["error_summary"] or "")}</td>'
            f'<td>{row["_attempts"] if "_attempts" in row else ""}</td>'
            f"<td>{action}</td></tr>"
        )
    return head + "".join(body) + "</tbody></table>"


def page_batch(conn, batch_id: int, flash: str = "") -> str:
    batch = conn.execute("SELECT * FROM wb_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        return page("批次不存在", '<div class="note bad">批次不存在。</div>', nav=NAV)
    rows = views.batch_rows(conn, batch_id)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    body = [flash]
    body.append('<div class="card"><div class="kv">')
    body.append(f'<div class="k">批次</div><div>#{batch["id"]} · {h(batch["name"])}</div>')
    body.append(f'<div class="k">导入时间</div><div>{h(batch["created_at"])}</div>')
    body.append(f'<div class="k">行数</div><div>{len(rows)}</div>')
    body.append(
        '<div class="k">状态分布</div><div>'
        + " ".join(
            f"{status_badge(key)} {value}" for key, value in sorted(counts.items())
        )
        + "</div>"
    )
    body.append("</div>")
    body.append('<div class="actions">')
    body.append(
        f'<form method="post" action="/batch/{batch_id}/precheck" style="display:inline">'
        '<button class="btn secondary" type="submit">重新预检</button></form>'
    )
    body.append(
        f'<form method="post" action="/batch/{batch_id}/run" style="display:inline">'
        '<button class="btn" type="submit">运行批次（自动执行器）</button></form>'
    )
    body.append(
        f'<form method="post" action="/batch/{batch_id}/recover" style="display:inline">'
        '<button class="btn secondary" type="submit">恢复中断/结果未知</button></form>'
    )
    body.append("</div></div>")
    body.append("<h2>明细</h2><div class=\"card\">")
    body.append(_rows_table(rows, show_batch=False))
    body.append("</div>")
    return page(f"采集工作台 · 批次 #{batch_id}", "".join(body), nav=NAV, active="导入与队列",
                subtitle=h(batch["name"]))


def page_row(conn, row_id: int, flash: str = "") -> str:
    row = views.row_detail(conn, row_id)
    if not row:
        return page("行不存在", '<div class="note bad">行不存在。</div>', nav=NAV)
    body = [flash]
    conflict = row["conflict"]
    body.append('<div class="card"><div class="kv">')
    body.append(f'<div class="k">业务编号</div><div class="mono">{h(row["record_key"])}</div>')
    body.append(f'<div class="k">批次</div><div><a href="/batch/{row["batch_id"]}">#{row["batch_id"]} · {h(row["batch_name"])}</a>，第 {row["row_number"]} 行</div>')
    body.append(f'<div class="k">行状态</div><div>{status_badge(row["status"])} {compare_badge(row["compare_status"])}</div>')
    body.append(f'<div class="k">最近说明</div><div>{h(row["error_summary"] or "")}</div>')
    body.append(
        f'<div class="k">提交前版本</div><div>'
        f'{h(row["base_revision"]) if row["base_revision"] is not None else "未记录"}</div>'
    )
    if row["lease"]:
        body.append(
            f'<div class="k">当前执行租约</div><div class="mono">'
            f'{h(row["lease"]["id"])}（心跳 {h(row["lease"]["heartbeat_at"])}）</div>'
        )
    body.append(
        f'<div class="k">原门户</div><div><a target="_blank" rel="noopener" '
        f'href="{h(_portal_link(conn, "/record/" + urllib.parse.quote(row["record_key"])))}">'
        "在门户打开此记录</a></div>"
    )
    body.append("</div></div>")

    if conflict and conflict["status"] == "open":
        body.append(
            f'<div class="note warn"><b>冲突队列：{h(REASON_LABELS.get(conflict["reason"], conflict["reason"]))}</b>'
            f'（打开于 {h(conflict["opened_at"])}）。自动执行器不会提交此行，'
            "直到你接管并处理。</div>"
        )
    if row["diff"]:
        body.append('<div class="card"><h3>逐字段比对</h3>'
                    '<p class="muted">基准 = 提交前版本；目标 = CSV 本次要写入；当前 = 门户最新值。</p>')
        body.append(diff_table(row["diff"]))
        body.append("</div>")

    # Merge suggestion used by both the retry button and the manual form:
    # where a human changed a field, default to their value; keep ours elsewhere.
    fields = dict(row["target_fields"])
    if conflict and conflict["status"] == "open":
        current = row["current"]
        for name in FIELD_NAMES:
            status = next((d["status"] for d in row["diff"] if d["field"] == name), "")
            if status in ("others", "conflict", "other_last"):
                fields[name] = current.get(name, fields[name])

    # Actions
    body.append('<div class="card"><h3>人工接管与处理</h3>')
    body.append(
        '<form method="post" action="/row/%d/takeover" class="actions">'
        '<label style="margin:0;color:inherit">接管人</label>'
        '<input style="max-width:180px" type="text" name="operator" value="reviewer">'
        '<button class="btn" type="submit">接管此行（撤销所有执行器租约）</button></form>' % row_id
    )
    if conflict and conflict["status"] == "open":
        merge_inputs = "".join(
            f'<input type="hidden" name="{h(name)}" value="{h(fields[name])}">'
            for name in FIELD_NAMES
        )
        body.append(
            '<div class="actions">'
            f'<form method="post" action="/row/{row_id}/accept" style="display:inline">'
            '<input type="hidden" name="operator" value="reviewer">'
            '<button class="btn secondary" type="submit">采纳门户当前值（不覆盖人工更正）</button></form>'
            f'<form method="post" action="/row/{row_id}/retry">'
            '<input type="hidden" name="operator" value="reviewer">'
            f'{merge_inputs}'
            '<button class="btn secondary" type="submit">按当前值放回待提交队列</button></form>'
            f'<form method="post" action="/row/{row_id}/dismiss" style="display:inline">'
            '<input type="hidden" name="operator" value="reviewer">'
            '<button class="btn danger" type="submit">关闭，不再提交</button></form>'
            "</div>"
        )
    body.append("</div>")

    if row["status"] in ("needs_reconcile", "running", "conflict", "retry_wait"):
        body.append(
            f'<div class="card"><h3>与门户核对/恢复</h3>'
            '<p class="muted">会重新读取门户当前值，判断本次是否已生效、他人是否修改、'
            "以及未提交的部分，并按结果自动归类。</p>"
            f'<form method="post" action="/row/{row_id}/reconcile" class="actions">'
            '<button class="btn secondary" type="submit">立即核对门户</button></form>'
            f'<form method="post" action="/row/{row_id}/run" class="actions">'
            '<button class="btn" type="submit">由此行重新提交（自动执行器）</button></form></div>'
        )

    # Manual submit form, prefilled with a merge suggestion.
    body.append(
        f'<div class="card"><h3>手工提交（接管后）</h3>'
        '<p class="muted">以新的门户版本打开表单并逐字段提交；若提交瞬间记录又被改动，'
        "门户会拒绝版本冲突，工作台会重新转入冲突队列。</p>"
        f'<form method="post" action="/row/{row_id}/manual-submit">'
    )
    body.append(
        '<div class="form-row"><label>接管人</label>'
        f'<input type="text" name="operator" value="reviewer"></div>'
    )
    for name in FIELD_NAMES:
        value = fields.get(name, "")
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
        body.append(f'<div class="form-row"><label>{h(label)}</label>{control}</div>')
    body.append('<div class="actions"><button class="btn" type="submit">提交到门户</button></div></form></div>')

    # Attempts
    body.append("<h2>提交尝试</h2><div class=\"card\">")
    if not row["attempts"]:
        body.append('<p class="muted">尚无尝试。</p>')
    else:
        body.append(
            '<table><thead><tr><th>#</th><th>结果</th><th>阶段</th><th>错误</th>'
            "<th>核对结论</th><th>表单令牌</th><th>开始</th><th>结束</th></tr></thead><tbody>"
        )
        for a in row["attempts"]:
            verdict = a["verify_verdict"] or ""
            body.append(
                f'<tr><td>{a["attempt_no"]}</td><td>{h(a["status"])}</td><td>{h(a["stage"])}</td>'
                f'<td class="muted">{h(a["error_message"] or "")}</td>'
                f'<td>{h(COMPARE_LABELS.get(verdict, verdict))}</td>'
                f'<td class="mono">{h(a["form_token"] or "")}</td>'
                f'<td class="muted mono">{h(a["started_at"])}</td>'
                f'<td class="muted mono">{h(a["finished_at"] or "")}</td></tr>'
            )
        body.append("</tbody></table>")
    body.append("</div>")

    body.append('<h2>接管记录</h2><div class="card">')
    if not row["takeovers"]:
        body.append('<p class="muted">无接管记录。</p>')
    else:
        body.append('<table><thead><tr><th>时间</th><th>接管人</th><th>撤销租约</th><th>状态</th></tr></thead><tbody>')
        for t in row["takeovers"]:
            body.append(
                f'<tr><td class="muted mono">{h(t["created_at"])}</td><td>{h(t["operator"])}</td>'
                f'<td>{t["revoked_leases"]}</td><td>{h(t["status"])}</td></tr>'
            )
        body.append("</tbody></table>")
    body.append("</div>")
    return page(f"采集工作台 · {row['record_key']}", "".join(body), nav=NAV, active="导入与队列",
                subtitle="逐字段比对、接管与手工提交。")


def page_conflicts(conn, flash: str = "") -> str:
    conflicts = engine.conflict_list(conn)
    body = [flash]
    body.append(
        '<div class="note">这里聚集“需要人来做决定”的记录：提交结果未知且与他人修改混合、'
        "提交前发现他人修改、或提交时版本冲突。自动执行器不会自动覆盖这些行。</div>"
    )
    if not conflicts:
        body.append('<div class="card"><p class="ok">当前没有待处理冲突。</p></div>')
        return page("采集工作台 · 冲突队列", "".join(body), nav=NAV, active="冲突队列")
    for c in conflicts:
        body.append('<div class="card">')
        body.append(
            f'<div class="actions"><b class="mono">{h(c["record_key"])}</b>'
            f'<span class="badge b-conflict">{h(REASON_LABELS.get(c["reason"], c["reason"]))}</span>'
            f'<span class="muted">批次 #{c["batch_id"]} 第 {c["row_number"]} 行 · 打开于 {h(c["opened_at"])}</span>'
            f'<a class="btn small secondary" href="/row/{c["row_id"]}">处理</a></div>'
        )
        body.append(diff_table(c["diff"]))
        body.append("</div>")
    return page("采集工作台 · 冲突队列", "".join(body), nav=NAV, active="冲突队列",
                subtitle=f"共 {len(conflicts)} 条待人工处理。")


def page_executors(conn, flash: str = "") -> str:
    body = [flash]
    body.append(
        '<div class="note">执行器是独立进程（也可同时开两个并发运行）。同一行同一时间只允许一个租约；'
        "人工接管会撤销租约，执行器在每次网络步骤前都会确认租约仍在，否则不提交。</div>"
    )
    body.append(
        '<div class="card"><h3>运行一次执行器</h3>'
        '<p class="muted">在终端里运行（与网页服务分离，才能模拟进程崩溃）：</p>'
        '<pre class="code">python3 -m app.workbench.executor run --batch 1 --timeout 2.5\n'
        "python3 -m app.workbench.executor recover</pre>"
        f'<p class="muted">执行器数据库：<span class="mono">{h(str(storage.db_path()))}</span>，'
        f'门户地址：<span class="mono">{h(engine.get_portal_url(conn))}</span></p></div>'
    )
    body.append("<h2>执行器租约</h2><div class=\"card\">")
    execs = views.executors(conn)
    if not execs:
        body.append('<p class="muted">暂无租约记录。</p>')
    else:
        body.append(
            '<table><thead><tr><th>执行器</th><th>行</th><th>状态</th><th>心跳</th>'
            "<th>开始</th><th>PID</th></tr></thead><tbody>"
        )
        for e in execs:
            body.append(
                f'<tr><td class="mono">{h(e["id"])}</td>'
                f'<td class="mono">{h(e["record_key"] or "-")}</td>'
                f'<td>{h(e["status"])}</td><td class="muted mono">{h(e["heartbeat_at"])}</td>'
                f'<td class="muted mono">{h(e["started_at"])}</td><td>{e["pid"] or ""}</td></tr>'
            )
        body.append("</tbody></table>")
    body.append("</div>")
    body.append("<h2>任务</h2><div class=\"card\">")
    jobs = views.jobs(conn)
    if not jobs:
        body.append('<p class="muted">暂无任务。</p>')
    else:
        body.append(
            '<table><thead><tr><th>#</th><th>类型</th><th>状态</th><th>结果</th>'
            "<th>开始</th><th>结束</th></tr></thead><tbody>"
        )
        for job in jobs:
            body.append(
                f'<tr><td>{job["id"]}</td><td>{h(job["kind"])}</td><td>{h(job["status"])}</td>'
                f'<td class="mono muted">{h(job["result"])}</td>'
                f'<td class="muted mono">{h(job["started_at"])}</td>'
                f'<td class="muted mono">{h(job["finished_at"] or "")}</td></tr>'
            )
        body.append("</tbody></table>")
    body.append("</div>")
    body.append('<h2>事件日志</h2>' + _events_table(views.recent_events(conn, 80)))
    return page("采集工作台 · 执行器", "".join(body), nav=NAV, active="执行器")


def page_verify(conn, flash: str = "", only: str = "") -> str:
    body = [flash]
    body.append(
        '<div class="note">最终核对：读取门户当前记录，与本次任务目标逐字段比较，'
        "并给出门户最终版本。可点“在门户打开”到原门户人工确认。</div>"
    )
    sql = (
        "SELECT r.*, b.name AS batch_name FROM wb_rows r JOIN wb_batches b ON b.id=r.batch_id"
        " WHERE r.status NOT IN ('invalid')"
    )
    args: list = []
    if only:
        sql += " AND r.record_key=?"
        args.append(only)
    sql += " ORDER BY r.id DESC"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    body.append('<div class="card"><form method="get" action="/verify" class="actions">'
                '<input style="max-width:240px" type="text" name="only" placeholder="只看业务编号，如 HK-1001" '
                f'value="{h(only)}"><button class="btn secondary" type="submit">筛选</button>'
                '<button class="btn" type="submit" formaction="/verify/refresh">刷新全部</button></form></div>')
    if not rows:
        body.append('<div class="card"><p class="muted">没有可核对的行。</p></div>')
        return page("采集工作台 · 最终核对", "".join(body), nav=NAV, active="最终核对")
    body.append('<div class="card"><table><thead><tr><th>业务编号</th><th>批次</th>'
                "<th>行状态</th><th>门户版本</th><th>逐字段结果</th><th></th></tr></thead><tbody>")
    for row in rows:
        target = normalize_fields(json_loads(row["target"], None))
        verification = json_loads(row["last_verification"], {}) if "last_verification" in row.keys() else {}
        current = normalize_fields(verification.get("current")) if verification else {}
        revision = verification.get("revision")
        if current and target:
            diff = field_diff(normalize_fields(json_loads(row["base_snapshot"], None)), target, current)
            mismatched = [d for d in diff if d["current"] != d["target"]]
            if not mismatched:
                summary = '<span class="ok">全部一致</span>'
            else:
                summary = "".join(
                    f'<span class="badge b-{ "others" if d["status"]=="others" else "pending-field" }">'
                    f'{h(d["label"])}</span> ' for d in mismatched
                )
        else:
            summary = '<span class="muted">尚未核对</span>'
        body.append(
            f'<tr><td class="mono">{h(row["record_key"])}</td>'
            f'<td><a href="/batch/{row["batch_id"]}">#{row["batch_id"]}</a></td>'
            f"<td>{status_badge(row['status'])}</td>"
            f'<td>{h(revision) if revision is not None else "—"}</td>'
            f"<td>{summary}</td>"
            f'<td><a class="btn small secondary" href="/row/{row["id"]}">明细</a> '
            f'<a class="btn small secondary" target="_blank" rel="noopener" '
            f'href="{h(_portal_link(conn, "/record/" + urllib.parse.quote(row["record_key"])))}">在门户打开</a></td></tr>'
        )
    body.append("</tbody></table></div>")
    return page("采集工作台 · 最终核对", "".join(body), nav=NAV, active="最终核对",
                subtitle="在原门户确认每一条的最终记录。")


def page_settings(conn, flash: str = "") -> str:
    profile = engine.get_fault_profile(conn)
    text = storage.get_setting(conn, f"fault_profile:{profile}", portal_faults.DEFAULT_RULES)
    body = [flash]
    body.append('<div class="card"><h2 style="margin-top:0">工作台设置</h2>')
    body.append(
        '<form method="post" action="/settings">'
        f'<div class="form-row"><label>门户地址</label>'
        f'<input type="text" name="portal_url" value="{h(engine.get_portal_url(conn))}"></div>'
        f'<div class="form-row"><label>故障配置名（执行器用）</label>'
        f'<input type="text" name="fault_profile" value="{h(profile)}"></div>'
        '<div class="actions"><button class="btn" type="submit">保存设置</button></div></form>'
        '<p class="muted">数据库：<span class="mono">' + h(str(storage.db_path())) + "</span></p></div>"
    )
    body.append('<div class="card"><h2 style="margin-top:0">故障注入（作用于自动执行器）</h2>')
    body.append(
        '<p class="muted">语法：<span class="mono">匹配:动作:次数[:阶段]</span>。'
        "动作 = ok / timeout / drop_ack / reject / lock / 5xx；阶段 = submit（默认）/ open / all。"
        "示例：<span class=\"mono\">HK-1001:drop_ack:1</span>。"
        "浏览器手工录入永远不会被注入故障。</p>"
    )
    body.append('<form method="post" action="/settings/faults">')
    body.append(f'<input type="hidden" name="profile" value="{h(profile)}">')
    body.append(f'<label>规则</label><textarea name="rules" rows="9" class="mono">{h(text)}</textarea>')
    body.append('<div class="actions"><button class="btn" type="submit">保存规则并清零计数</button></div></form></div>')
    body.append(
        '<div class="card"><h3>重置</h3><p class="muted">清空所有批次、尝试与冲突，并重置门户虚构数据。'
        '（演练用，不会触及其他文件）</p>'
        '<form method="post" action="/settings/reset">'
        '<div class="actions"><button class="btn danger" type="submit">清空并重置演练数据</button></div>'
        "</form></div>"
    )
    body.append("<h2>演练脚本</h2><div class=\"card\"><pre class=\"code\">"
                "python3 -m app.drill drop_ack      # 回执丢失\n"
                "python3 -m app.drill timeout      # 响应超时\n"
                "python3 -m app.drill manual_edit  # 他人修改 → 冲突队列\n"
                "python3 -m app.drill crash        # 执行器崩溃 → 恢复核对\n"
                "</pre></div>")
    return page("采集工作台 · 设置/演练", "".join(body), nav=NAV, active="设置/演练")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _verify_row(conn, row: dict) -> dict:
    client = engine.client_for(conn, row)
    data = client.get_record(row["record_key"])
    current = normalize_fields(data.get("fields"))
    target = normalize_fields(json_loads(row["target"], None))
    base = normalize_fields(json_loads(row["base_snapshot"], None))
    diff = field_diff(base, target, current)
    conn.execute(
        "UPDATE wb_rows SET last_verification=?, last_verified_at=?, updated_at=? WHERE id=?",
        (
            json_dumps({"current": current, "revision": data.get("revision"), "diff": diff}),
            iso(),
            iso(),
            row["id"],
        ),
    )
    return {"record_key": row["record_key"], "revision": data.get("revision"),
            "current": current, "diff": diff,
            "all_match": all(d["current"] == d["target"] for d in diff)}


def build_router() -> Router:
    router = Router()

    def dashboard(req):
        conn = _conn()
        try:
            return html_response(page_dashboard(conn, _flash(req)))
        finally:
            conn.close()

    def queue(req):
        conn = _conn()
        try:
            return html_response(page_queue(conn, _flash(req)))
        finally:
            conn.close()

    def do_import(req):
        conn = _conn()
        try:
            form = req.form()
            name = form.get("name", "").strip()
            filename, data = req.files().get("file", ("", b""))
            if data:
                from ..common import decode_csv

                text = decode_csv(data)
                name = name or filename
            else:
                text = form.get("csv", "")
            if not text.strip():
                return redirect("/queue?error=" + q("没有收到 CSV 内容"))
            engine.set_portal_url(conn, form["portal_url"]) if form.get("portal_url") else None
            result = engine.import_csv(conn, text, name)
            if not result["ok"]:
                return redirect("/queue?error=" + q("；".join(result["errors"])))
            pre = result.get("precheck") or {}
            msg = (
                f"批次 #{result['batch_id']} 导入 {result['rows']} 行"
                f"（无效 {result['invalid']}），预检：成功 {pre.get('ok', 0)}，"
                f"无需提交 {pre.get('noop', 0)}，失败 {pre.get('failed', 0)}"
            )
            return redirect(f"/batch/{result['batch_id']}?message=" + q(msg))
        finally:
            conn.close()

    def batch_page(req):
        conn = _conn()
        try:
            batch_id = int(req.match.group("id"))
            return html_response(page_batch(conn, batch_id, _flash(req)))
        finally:
            conn.close()

    def batch_precheck(req):
        conn = _conn()
        try:
            batch_id = int(req.match.group("id"))
            result = engine.precheck_batch(conn, batch_id)
            return redirect(
                f"/batch/{batch_id}?message="
                + q(f"预检完成：{result['checked']} 行，成功 {result['ok']}，"
                    f"无需提交 {result['noop']}，失败 {result['failed']}")
            )
        finally:
            conn.close()

    def batch_run(req):
        conn = _conn()
        try:
            batch_id = int(req.match.group("id"))
            summary = runner.run_batch(conn, batch_id=batch_id)
            msg = (
                f"执行完成：处理 {summary['processed']}，生效 {summary['applied']}，"
                f"无需变更 {summary['noop']}，待补交 {summary['retry_wait']}，"
                f"冲突 {summary['conflicts']}，无效 {summary['invalid']}，"
                f"失败 {summary['failed']}，跳过 {summary['skipped']}"
            )
            return redirect(f"/batch/{batch_id}?message=" + q(msg))
        finally:
            conn.close()

    def batch_recover(req):
        conn = _conn()
        try:
            batch_id = int(req.match.group("id"))
            recovered = engine.recover_interrupted(conn)
            pending = engine.reconcile_pending(conn)
            msg = (
                f"恢复 {recovered['recovered']} 行；核对 {pending['processed']}/{pending['total']} 行"
                f"（门户不可达 {pending['errors']}）"
            )
            return redirect(f"/batch/{batch_id}?message=" + q(msg))
        finally:
            conn.close()

    def row_page(req):
        conn = _conn()
        try:
            row_id = int(req.match.group("id"))
            return html_response(page_row(conn, row_id, _flash(req)))
        finally:
            conn.close()

    def row_takeover(req):
        conn = _conn()
        try:
            row_id = int(req.match.group("id"))
            operator = _actor(req)
            result = engine.take_over(conn, row_id, operator)
            return redirect(f"/row/{row_id}?message=" + q(
                f"已接管，撤销 {result['revoked']} 个执行器租约"))
        finally:
            conn.close()

    def row_reconcile(req):
        conn = _conn()
        try:
            row_id = int(req.match.group("id"))
            row = dict(conn.execute("SELECT * FROM wb_rows WHERE id=?", (row_id,)).fetchone())
            try:
                result = engine.reconcile_row(conn, row, source="manual")
                return redirect(f"/row/{row_id}?message=" + q(
                    f"核对结论：{COMPARE_LABELS.get(result['verdict'], result['verdict'])}"))
            except PortalError as exc:
                return redirect(f"/row/{row_id}?error=" + q(f"无法核对：{exc.message}"))
        finally:
            conn.close()

    def row_run(req):
        conn = _conn()
        try:
            row_id = int(req.match.group("id"))
            result = runner.run_one(conn, row_id)
            if not result.get("ok"):
                return redirect(f"/row/{row_id}?error=" + q(result.get("error", "执行失败")))
            return redirect(f"/row/{row_id}?message=" + q(
                f"执行结果：{STATUS_LABELS.get(result.get('status'), result.get('status'))}"))
        finally:
            conn.close()

    def row_manual_submit(req):
        conn = _conn()
        try:
            row_id = int(req.match.group("id"))
            form = req.form()
            operator = form.get("operator", "reviewer").strip() or "reviewer"
            fields = normalize_fields(form)
            result = engine.manual_submit(conn, row_id, fields, operator)
            if result.get("ok"):
                return redirect(f"/row/{row_id}?message=" + q(
                    f"手工提交成功，门户版本 {result.get('revision')}"))
            return redirect(f"/row/{row_id}?error=" + q(result.get("error", "手工提交失败")))
        finally:
            conn.close()

    def row_accept(req):
        conn = _conn()
        try:
            row_id = int(req.match.group("id"))
            operator = _actor(req)
            try:
                engine.accept_portal_values(conn, row_id, operator)
                return redirect(f"/row/{row_id}?message=" + q("已采纳门户当前值"))
            except PortalError as exc:
                return redirect(f"/row/{row_id}?error=" + q(f"无法读取门户：{exc.message}"))
        finally:
            conn.close()

    def row_retry(req):
        conn = _conn()
        try:
            row_id = int(req.match.group("id"))
            operator = _actor(req)
            # The reviewer may have edited the values in the manual-submit form;
            # honour them so "retry" means "submit this corrected target".
            form = req.form()
            target = normalize_fields(form) if form.get("full_name") else None
            try:
                result = engine.retry_conflict(conn, row_id, operator, target=target)
            except PortalError as exc:
                return redirect(f"/row/{row_id}?error=" + q(f"无法读取门户当前版本：{exc.message}"))
            if not result.get("ok"):
                return redirect(f"/row/{row_id}?error=" + q(result.get("error", "无法重试")))
            return redirect(f"/row/{row_id}?message=" + q(
                f"已放回待提交队列，基准更新为门户版本 {result.get('revision')}"))
        finally:
            conn.close()

    def row_dismiss(req):
        conn = _conn()
        try:
            row_id = int(req.match.group("id"))
            engine.dismiss(conn, row_id, _actor(req), "人工确认不提交")
            return redirect(f"/row/{row_id}?message=" + q("已关闭此行"))
        finally:
            conn.close()

    def conflicts_page(req):
        conn = _conn()
        try:
            return html_response(page_conflicts(conn, _flash(req)))
        finally:
            conn.close()

    def executors_page(req):
        conn = _conn()
        try:
            return html_response(page_executors(conn, _flash(req)))
        finally:
            conn.close()

    def verify_page(req):
        conn = _conn()
        try:
            return html_response(page_verify(conn, _flash(req), req.q("only")))
        finally:
            conn.close()

    def verify_refresh(req):
        conn = _conn()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM wb_rows WHERE status NOT IN ('invalid') ORDER BY id"
            ).fetchall()]
            ok = errors = 0
            mismatch = []
            for row in rows:
                try:
                    result = _verify_row(conn, row)
                    ok += 1
                    if not result["all_match"]:
                        mismatch.append(result["record_key"])
                except PortalError:
                    errors += 1
            msg = f"核对 {ok} 行（门户不可达 {errors}）"
            if mismatch:
                msg += "；与目标不一致：" + "、".join(mismatch)
            return redirect("/verify?message=" + q(msg))
        finally:
            conn.close()

    def verify_api(req):
        conn = _conn()
        try:
            only = req.q("only")
            sql = "SELECT * FROM wb_rows WHERE status NOT IN ('invalid')"
            args: list = []
            if only:
                sql += " AND record_key=?"
                args.append(only.upper())
            results = []
            errors = []
            for row in [dict(r) for r in conn.execute(sql, args).fetchall()]:
                try:
                    results.append(_verify_row(conn, row))
                except PortalError as exc:
                    errors.append({"record_key": row["record_key"], "error": exc.message})
            return json_response({"ok": True, "results": results, "errors": errors})
        finally:
            conn.close()

    def settings_page(req):
        conn = _conn()
        try:
            return html_response(page_settings(conn, _flash(req)))
        finally:
            conn.close()

    def settings_save(req):
        conn = _conn()
        try:
            form = req.form()
            if form.get("portal_url"):
                engine.set_portal_url(conn, form["portal_url"].strip())
            if form.get("fault_profile"):
                storage.set_setting(conn, "fault_profile", form["fault_profile"].strip())
            return redirect("/settings?message=" + q("设置已保存"))
        finally:
            conn.close()

    def settings_faults(req):
        conn = _conn()
        try:
            form = req.form()
            profile = form.get("profile", portal_faults.DEFAULT_PROFILE).strip() or portal_faults.DEFAULT_PROFILE
            errors = portal_faults.save_rules(conn, profile, form.get("rules", ""))
            if errors:
                return redirect("/settings?error=" + q("；".join(errors)))
            portal_faults.FaultBook.reset(conn)
            return redirect("/settings?message=" + q("故障规则已保存，计数已清零"))
        finally:
            conn.close()

    def settings_reset(req):
        conn = _conn()
        try:
            storage.wb_reset(conn)
            portal_faults.FaultBook.reset(conn)
            engine.set_portal_url(conn, engine.DEFAULT_PORTAL_URL)
            return redirect("/?message=" + q("演练数据已重置"))
        finally:
            conn.close()

    def sample_csv(req):
        from ..web import Response

        text = (
            "record_key,full_name,phone,address,service_level,note\n"
            "HK-1001,林晓禾,13800001001,青禾街道 1 号 3 栋 202,standard,地址更正\n"
            "HK-1002,周远,13800001002,青禾街道 8 号 2 栋 1201,standard,电话回访\n"
        )
        return Response(status=200, body=text, content_type="text/csv; charset=utf-8",
                        headers=[("Content-Disposition", 'attachment; filename="demo_batch.csv"')])

    # -- JSON API ---------------------------------------------------------
    def api_rows(req):
        conn = _conn()
        try:
            batch = req.q("batch")
            rows = []
            sql = ("SELECT r.*, b.name AS batch_name FROM wb_rows r JOIN wb_batches b"
                   " ON b.id=r.batch_id")
            args: list = []
            if batch:
                sql += " WHERE r.batch_id=?"
                args.append(int(batch))
            sql += " ORDER BY r.id"
            for raw in conn.execute(sql, args).fetchall():
                item = dict(raw)
                item["target"] = json_loads(item["target"], {})
                item["base_snapshot"] = json_loads(item["base_snapshot"], None)
                rows.append(item)
            return json_response({"ok": True, "rows": rows})
        finally:
            conn.close()

    def api_row(req):
        conn = _conn()
        try:
            row = views.row_detail(conn, int(req.match.group("id")))
            if not row:
                return json_response({"ok": False, "error": "not_found"}, status=404)
            row.pop("attempts", None)
            row["attempts"] = views.row_detail(conn, row["id"])["attempts"]
            return json_response({"ok": True, "row": row})
        finally:
            conn.close()

    def api_conflicts(req):
        conn = _conn()
        try:
            return json_response({"ok": True, "conflicts": engine.conflict_list(conn)})
        finally:
            conn.close()

    def api_import(req):
        conn = _conn()
        try:
            body = req.json()
            result = engine.import_csv(conn, body.get("csv", ""), body.get("name", ""))
            return json_response(result, status=200 if result["ok"] else 400)
        finally:
            conn.close()

    router.get(r"/", dashboard)
    router.get(r"/queue", queue)
    router.post(r"/import", do_import)
    router.get(r"/sample.csv", sample_csv)
    router.get(r"/batch/(?P<id>\d+)", batch_page)
    router.post(r"/batch/(?P<id>\d+)/precheck", batch_precheck)
    router.post(r"/batch/(?P<id>\d+)/run", batch_run)
    router.post(r"/batch/(?P<id>\d+)/recover", batch_recover)
    router.get(r"/row/(?P<id>\d+)", row_page)
    router.post(r"/row/(?P<id>\d+)/takeover", row_takeover)
    router.post(r"/row/(?P<id>\d+)/reconcile", row_reconcile)
    router.post(r"/row/(?P<id>\d+)/run", row_run)
    router.post(r"/row/(?P<id>\d+)/manual-submit", row_manual_submit)
    router.post(r"/row/(?P<id>\d+)/accept", row_accept)
    router.post(r"/row/(?P<id>\d+)/retry", row_retry)
    router.post(r"/row/(?P<id>\d+)/dismiss", row_dismiss)
    router.get(r"/conflicts", conflicts_page)
    router.get(r"/executors", executors_page)
    router.get(r"/verify", verify_page)
    router.post(r"/verify/refresh", verify_refresh)
    router.get(r"/api/verify", verify_api)
    router.get(r"/settings", settings_page)
    router.post(r"/settings", settings_save)
    router.post(r"/settings/faults", settings_faults)
    router.post(r"/settings/reset", settings_reset)
    router.get(r"/api/rows", api_rows)
    router.get(r"/api/row/(?P<id>\d+)", api_row)
    router.get(r"/api/conflicts", api_conflicts)
    router.post(r"/api/import", api_import)
    return router
