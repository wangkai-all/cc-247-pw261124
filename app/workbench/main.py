"""Entry point for the collection workbench web UI.

Usage:
    python3 -m app.workbench.main --port 8802
    python3 -m app.workbench.main --port 8802 --db data/portal.db
"""

from __future__ import annotations

import argparse
import os

from .. import storage
from ..web import serve
from . import engine, server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集工作台（本地虚构数据）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8802)
    parser.add_argument("--db", default="")
    parser.add_argument("--portal", default="")
    parser.add_argument("--reset", action="store_true", help="清空工作台演练数据后退出")
    args = parser.parse_args(argv)

    if args.db:
        os.environ[storage.DB_ENV] = args.db
    conn = storage.connect()
    storage.init_db(conn, portal_seed=True)
    if args.portal:
        engine.set_portal_url(conn, args.portal)
    if args.reset:
        storage.wb_reset(conn)
        print("工作台演练数据已清空。")
        conn.close()
        return 0
    conn.close()

    server.set_connection_factory(storage.connect)
    httpd = serve(
        server.build_router(), args.host, args.port, storage.connect, "CollectWorkbench/1.0"
    )
    print(f"采集工作台已启动：http://{args.host}:{args.port}/  （Ctrl+C 退出）")
    print(f"门户地址：{engine.get_portal_url(storage.connect())}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
