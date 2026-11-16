"""Command-line executor, a separate process from the workbench UI.

Examples (all local, fictional data):

    python3 -m app.workbench.executor run --batch 1 --faults HK-1001:drop_ack:1
    python3 -m app.workbench.executor run --batch 1 --kill-after 0.6
    python3 -m app.workbench.executor recover
    python3 -m app.workbench.executor run --batch 1          # resume after recovery

``--kill-after`` simulates the executor process dying mid-attempt: it exits
abruptly at that point without touching the row again, leaving the lease to
expire and the row for recovery.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

from .. import storage
from ..portal import faults as portal_faults
from . import engine, runner


def _banner(text: str) -> None:
    print(f"\n=== {text} ===")


def cmd_run(args) -> int:
    conn = storage.connect()
    storage.init_db(conn, portal_seed=True)
    engine.set_portal_url(conn, args.portal)
    if args.faults is not None:
        # Inline rules apply to this executor's fault profile for the run.
        text = args.faults.replace("\\n", "\n")
        errors = portal_faults.save_rules(conn, engine.get_fault_profile(conn), text)
        if errors:
            print("故障规则有误：" + "；".join(errors), file=sys.stderr)
            return 2
        portal_faults.FaultBook.reset(conn)
        _banner("已应用内联故障规则")
        for line in text.splitlines():
            if line.strip():
                print("  " + line)

    if args.kill_after is not None:
        return _run_with_selfkill(conn, args)

    summary = runner.run_batch(
        conn,
        batch_id=args.batch,
        limit=args.limit,
        timeout=args.timeout,
        max_attempts=args.max_attempts,
    )
    _banner("执行结果")
    for row_id, key, status in summary["results"]:
        print(f"  行#{row_id:<4} {key:<10} → {status}")
    print(
        f"\n共处理 {summary['processed']} 行：生效 {summary['applied']}，"
        f"无需变更 {summary['noop']}，待补交 {summary['retry_wait']}，"
        f"冲突 {summary['conflicts']}，无效 {summary['invalid']}，失败 {summary['failed']}，"
        f"跳过(被占用) {summary['skipped']}"
    )
    conn.close()
    return 0


def _run_with_selfkill(conn: sqlite3.Connection, args) -> int:
    """Start one row, then die at a chosen moment to simulate a crash."""
    rows = engine.pending_rows(conn, args.batch)
    if not rows:
        print("没有待执行行。")
        return 0
    row = rows[0]
    executor_id = runner.new_executor_id()
    job_id = runner.create_job(conn, "run_one", {"row_id": row["id"], "simulated_crash": True})
    if not engine.acquire_lease(conn, row["id"], executor_id, job_id):
        print("无法取得执行租约。")
        return 0
    print(
        f"执行器 {executor_id} 开始处理 {row['record_key']}（行#{row['id']}），"
        f"进程将在 {args.kill_after}s 后被强制终止……"
    )
    # The worker owns its own SQLite connection (sqlite3 connections are bound
    # to their creating thread). The main thread kills the whole process at an
    # arbitrary moment, mid-HTTP, exactly like a real crash.
    import threading

    def work() -> None:
        worker_conn = storage.connect()
        try:
            engine.execute_row(
                worker_conn, row["id"], executor_id, job_id=job_id, timeout=args.timeout
            )
        except BaseException:
            pass
        finally:
            worker_conn.close()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    time.sleep(args.kill_after)
    print("模拟执行器进程崩溃：现在退出（未做任何确认或清理）。")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(137)


def cmd_recover(args) -> int:
    conn = storage.connect()
    storage.init_db(conn, portal_seed=True)
    engine.set_portal_url(conn, args.portal)
    _banner("恢复中断的执行")
    result = engine.recover_interrupted(conn, reconcile=not args.no_reconcile)
    print(f"发现 {result['recovered']} 行待恢复" + ("：" + "、".join(result["rows"]) if result["rows"] else ""))
    pending = engine.reconcile_pending(conn)
    print(f"核对完成 {pending['processed']}/{pending['total']} 行，门户不可达 {pending['errors']} 行")
    for row in conn.execute(
        "SELECT record_key, status, compare_status, error_summary FROM wb_rows"
        " WHERE status IN ('needs_reconcile','conflict','retry_wait') ORDER BY id"
    ):
        print(f"  {row['record_key']:<10} {row['status']:<16} {row['compare_status']:<12} {row['error_summary']}")
    conn.close()
    return 0


def cmd_list(args) -> int:
    conn = storage.connect()
    storage.init_db(conn, portal_seed=True)
    _banner("行状态")
    for row in conn.execute(
        "SELECT r.*, b.name AS batch_name FROM wb_rows r JOIN wb_batches b ON b.id=r.batch_id"
        " ORDER BY r.id"
    ):
        print(
            f"  行#{row['id']:<3} {row['record_key']:<10} 批次#{row['batch_id']} "
            f"{row['status']:<16} {row['compare_status']:<12} {row['error_summary'] or ''}"
        )
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集工作台自动执行器（本地虚构数据）")
    parser.add_argument("--db", default="")
    parser.add_argument("--portal", default=engine.DEFAULT_PORTAL_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行一个批次（或全部待执行行）")
    run.add_argument("--batch", type=int, default=None)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--timeout", type=float, default=2.5)
    run.add_argument("--max-attempts", type=int, default=3)
    run.add_argument("--faults", default=None, help="内联故障规则（覆盖默认配置，停止后仍生效）")
    run.add_argument("--kill-after", type=float, default=None, help="模拟进程在 N 秒后崩溃")
    run.set_defaults(func=cmd_run)

    rec = sub.add_parser("recover", help="把中断/结果未知的行与门户核对")
    rec.add_argument("--no-reconcile", action="store_true")
    rec.set_defaults(func=cmd_recover)

    ls = sub.add_parser("list", help="列出所有行状态")
    ls.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    if args.db:
        os.environ[storage.DB_ENV] = args.db
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
