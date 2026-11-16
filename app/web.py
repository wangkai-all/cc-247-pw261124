"""Tiny routing / request helpers used by both local web servers.

Only stdlib is used. Servers bind to 127.0.0.1 and serve fictional local data.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import traceback
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

MAX_BODY = 12 * 1024 * 1024
MAX_LOG_BODY = 500


@dataclass
class Response:
    status: int = 200
    body: str | bytes = ""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)
    # Simulated lost receipt: send headers promising a body, then slam the
    # socket shut. The client sees an incomplete read, not a clean 200.
    drop: bool = False


def redirect(location: str, status: int = 303) -> Response:
    return Response(status=status, body="", headers=[("Location", location)])


def html_response(body: str, status: int = 200) -> Response:
    return Response(status=status, body=body)


def json_response(data: dict, status: int = 200) -> Response:
    return Response(
        status=status,
        body=json.dumps(data, ensure_ascii=False),
        content_type="application/json; charset=utf-8",
    )


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def parse_multipart(body: bytes, content_type: str) -> dict[str, tuple[str, bytes]]:
    """Minimal multipart/form-data parser: name -> (filename, content)."""
    match = re.search(r"boundary=([^;]+)", content_type or "")
    if not match:
        return {}
    boundary = match.group(1).strip().strip('"').encode()
    parts: dict[str, tuple[str, bytes]] = {}
    for chunk in body.split(b"--" + boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        raw_headers, content = chunk.split(b"\r\n\r\n", 1)
        disposition = ""
        for line in raw_headers.decode("latin-1").split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                disposition = line
        name_match = re.search(r'name="([^"]*)"', disposition)
        if not name_match:
            continue
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        name = name_match.group(1)
        filename = filename_match.group(1) if filename_match else ""
        parts[name] = (filename, content.rstrip(b"\r\n"))
    return parts


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: object
    body: bytes
    client_address: str
    match: re.Match | None = None
    _form: dict[str, str] | None = None
    _json: dict | None = None
    _files: dict[str, tuple[str, bytes]] | None = None

    def q(self, name: str, default: str = "") -> str:
        values = self.query.get(name)
        return values[0] if values else default

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "")

    def form(self) -> dict[str, str]:
        if self._form is None:
            ctype = self.content_type.split(";")[0].strip().lower()
            body_text = self.body.decode("utf-8", errors="replace")
            if ctype == "application/x-www-form-urlencoded" or not ctype:
                parsed = urllib.parse.parse_qs(body_text, keep_blank_values=True)
                self._form = {k: v[0] for k, v in parsed.items()}
            elif ctype == "multipart/form-data":
                self._form = {}
                for name, (_filename, content) in self.files().items():
                    self._form[name] = content.decode("utf-8", errors="replace")
            else:
                self._form = {}
        return self._form

    def files(self) -> dict[str, tuple[str, bytes]]:
        if self._files is None:
            self._files = parse_multipart(self.body, self.content_type)
        return self._files

    def json(self) -> dict:
        if self._json is None:
            try:
                value = json.loads(self.body.decode("utf-8") or "{}")
                self._json = value if isinstance(value, dict) else {}
            except (ValueError, UnicodeDecodeError):
                self._json = {}
        return self._json


Handler = Callable[[Request], Response]


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        self.routes.append((method.upper(), re.compile("^" + pattern + "$"), handler))

    def get(self, pattern: str, handler: Handler) -> None:
        self.add("GET", pattern, handler)

    def post(self, pattern: str, handler: Handler) -> None:
        self.add("POST", pattern, handler)

    def route(self, request: Request) -> Response:
        for method, regex, handler in self.routes:
            if method != request.method:
                continue
            match = regex.match(request.path)
            if match:
                request.match = match
                return handler(request)
        return Response(status=404, body=page("未找到", "<p class='muted'>页面不存在。</p>"))


def make_handler(
    router: Router,
    connect_fn: Callable[[], sqlite3.Connection],
    server_name: str,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = server_name

        def log_message(self, fmt: str, *args) -> None:  # quiet
            return

        def _handle(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(Response(status=413, body="请求体过大"))
                return
            body = self.rfile.read(length) if length else b""
            request = Request(
                method=method,
                path=parsed.path,
                query=query,
                headers=self.headers,
                body=body,
                client_address=self.client_address[0],
            )
            conn = connect_fn()
            try:
                response = router.route(request)
            except Exception:
                traceback.print_exc()
                response = Response(
                    status=500,
                    body=page("内部错误", "<p class='bad'>处理请求时发生内部错误，已输出到终端日志。</p>"),
                )
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            self._send(response)

        def _send(self, response: Response) -> None:
            if isinstance(response.body, str):
                payload = response.body.encode("utf-8")
            else:
                payload = response.body
            try:
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                if response.drop:
                    # Promise a body we will never send, then close without it.
                    self.send_header("Content-Length", "64")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    self.wfile.flush()
                    self.connection.shutdown(2)
                    return
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                for key, value in response.headers:
                    self.send_header(key, value)
                self.end_headers()
                if payload:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

    return Handler


def serve(router: Router, host: str, port: int, connect_fn, server_name: str):
    httpd = ThreadingHTTPServer((host, port), make_handler(router, connect_fn, server_name))
    httpd.daemon_threads = True
    return httpd


# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f4f5f7;--card:#fff;--ink:#17212b;--muted:#667085;--line:#d9dde3;
--accent:#1f5f8b;--accent2:#eaf3f8;--ok:#1c7a4a;--warn:#a15c00;--bad:#b42318;--conflict:#9a3412}
*{box-sizing:border-box}
body{margin:0;font:14px/1.55 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
header{background:#12263a;color:#fff;padding:0 24px;display:flex;align-items:center;gap:24px;flex-wrap:wrap}
header .brand{font-weight:700;padding:14px 0;letter-spacing:.5px}
header nav a{color:#cbd8e5;text-decoration:none;padding:14px 8px;display:inline-block;border-bottom:3px solid transparent}
header nav a.active,header nav a:hover{color:#fff;border-bottom-color:#7fb3d5}
main{max-width:1180px;margin:24px auto 60px;padding:0 20px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}h3{font-size:15px;margin:18px 0 8px}
.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:18px;margin:14px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}
.stat .n{font-size:26px;font-weight:700}.stat .l{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;background:#fff}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;background:#fafbfc}
tr:last-child td{border-bottom:0}
a{color:var(--accent)}
.btn{display:inline-block;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:6px;padding:7px 13px;font-size:13px;cursor:pointer;text-decoration:none}
.btn.secondary{background:#fff;color:var(--accent)}
.btn.danger{background:var(--bad);border-color:var(--bad)}
.btn.small{padding:3px 9px;font-size:12px}
.btn:disabled{opacity:.5;cursor:not-allowed}
input[type=text],input[type=search],textarea,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:5px;font:inherit;background:#fff}
label{display:block;font-size:12px;color:var(--muted);margin:10px 0 3px}
.form-row{max-width:560px}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
.badge{display:inline-block;border-radius:999px;padding:1px 8px;font-size:12px;white-space:nowrap;border:1px solid transparent}
.b-pending{background:#fef3c7;color:#92400e}.b-running{background:#dbeafe;color:#1e40af}
.b-applied,.b-resolved{background:#dcfce7;color:#166534}.b-retry_wait,.b-needs_reconcile{background:#e0e7ff;color:#3730a3}
.b-conflict{background:#ffedd5;color:#9a3412}.b-failed,.b-invalid,.b-missing{background:#fee2e2;color:#991b1b}
.b-noop{background:#f1f5f9;color:#475569}.b-queued{background:#f3e8ff;color:#6b21a8}
.b-unchanged,.b-same_as_target{background:#f1f5f9;color:#475569}
.b-applied-field{background:#dcfce7;color:#166534}.b-pending-field{background:#e0e7ff;color:#3730a3}
.b-others{background:#fef3c7;color:#92400e}.b-conflict-field{background:#ffedd5;color:#9a3412}
.muted{color:var(--muted)}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
.code{background:#0f1720;color:#d7e2ee;padding:12px;border-radius:6px;overflow:auto;font:12px/1.45 ui-monospace,Consolas,monospace;white-space:pre-wrap}
.note{background:var(--accent2);border-left:3px solid var(--accent);padding:10px 14px;border-radius:0 6px 6px 0;margin:12px 0}
.note.warn{background:#fff7ed;border-left-color:var(--warn)}
.note.bad{background:#fef2f2;border-left-color:var(--bad)}
.kv{display:grid;grid-template-columns:150px 1fr;gap:6px 12px;font-size:13px}
.kv .k{color:var(--muted)}
.steps{display:flex;gap:0;margin:16px 0 22px}
.steps .s{flex:1;padding:8px;border-bottom:3px solid var(--line);color:var(--muted);font-size:13px}
.steps .s.done{border-color:#7fb3d5;color:var(--ink)}.steps .s.current{border-color:var(--accent);color:var(--ink);font-weight:700}
.field-table td{vertical-align:top}.field-table .old{color:var(--muted);text-decoration:line-through}
.footer{margin-top:36px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px}
.tabs a{margin-right:12px}
pre{white-space:pre-wrap;word-break:break-all}
"""


def page(
    title: str,
    body: str,
    nav: list[tuple[str, str]] | None = None,
    active: str = "",
    subtitle: str = "",
) -> str:
    nav_html = ""
    if nav:
        items = "".join(
            f'<a href="{esc(href)}" class="{"active" if label == active else ""}">{esc(label)}</a>'
            for label, href in nav
        )
        nav_html = f"<nav>{items}</nav>"
    sub_html = f'<p class="sub">{subtitle}</p>' if subtitle else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head>
<body><header><div class="brand">本地虚构数据 · 采集演练台</div>{nav_html}</header>
<main><h1>{esc(title)}</h1>{sub_html}{body}
<div class="footer">所有姓名、电话、地址均为虚构测试数据；服务仅监听本机。</div></main>
</body></html>"""


def badge(status: str, label: str | None = None) -> str:
    labels = {
        "pending": "待执行",
        "queued": "已排队",
        "running": "执行中",
        "retry_wait": "等待补交",
        "needs_reconcile": "待恢复核对",
        "applied": "已生效",
        "resolved": "人工已处理",
        "conflict": "冲突队列",
        "failed": "失败",
        "invalid": "导入无效",
        "missing": "门户无此记录",
        "noop": "无需提交",
        "done": "完成",
        "interrupted": "已中断",
        "offline": "离线",
    }
    return f'<span class="badge b-{esc(status)}">{esc(label or labels.get(status, status))}</span>'
