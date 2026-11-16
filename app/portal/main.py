"""Entry point for the mock external portal.

Usage:
    python3 -m app.portal.main --port 8801
    python3 -m app.portal.main --reset
"""

from __future__ import annotations

import argparse
import os

from .. import storage
from ..web import serve
from . import server as portal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模拟外部门户（本地虚构数据）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--db", default="")
    parser.add_argument("--reset", action="store_true", help="清空并重建门户数据后退出")
    parser.add_argument(
        "--faults",
        default="",
        help="内联故障规则，写入 cm-default 配置后退出（语法见 app/portal/faults.py）",
    )
    args = parser.parse_args(argv)

    if args.db:
        os.environ[storage.DB_ENV] = args.db
    conn = storage.connect()
    storage.init_db(conn, portal_seed=True)

    if args.reset:
        conn.execute("DELETE FROM portal_sessions")
        conn.execute("DELETE FROM portal_revisions")
        conn.execute("DELETE FROM portal_records")
        conn.execute("DELETE FROM wb_settings WHERE key LIKE 'fault_used:%'")
        storage.init_db(conn, portal_seed=True)
        print("门户已重置为初始虚构数据。")
        conn.close()
        return 0
    if args.faults:
        from . import faults

        text = args.faults.replace("\\n", "\n")
        errors = faults.save_rules(conn, faults.DEFAULT_PROFILE, text)
        conn.close()
        if errors:
            print("规则有误：" + "；".join(errors))
            return 2
        print("已写入 cm-default 故障配置。")
        return 0
    conn.close()

    portal.set_connection_factory(storage.connect)
    httpd = serve(
        portal.build_router(),
        args.host,
        args.port,
        storage.connect,
        "MockPortal/1.0",
    )
    print(f"模拟门户已启动：http://{args.host}:{args.port}/  （Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
