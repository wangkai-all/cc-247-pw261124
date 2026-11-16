"""Task-drill harness: start everything, inject faults, drive the executor.

Run:  python3 -m app.drill all
      python3 -m app.drill drop_ack
      python3 -m app.drill status

This is for demonstrating/validating the workbench; it is not required at
runtime. Everything uses fictional, local data in ``data/``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

from . import storage
from .workbench import engine, runner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_CSV = os.path.join(ROOT, "examples", "demo_batch.csv")
PORTAL_PORT = int(os.environ.get("DRILL_PORTAL_PORT", "8801"))


def _db() -> str:
    return os.environ.get(storage.DB_ENV) or str(storage.DEFAULT_DB)


def portal_running() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORTAL_PORT}/api/records", timeout=1
        ) as response:
            return response.status == 200
    except Exception:
        return False


def start_portal() -> subprocess.Popen:
    if portal_running():
        print(f"门户已在 127.0.0.1:{PORTAL_PORT} 运行。")
        return None
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.portal.main", "--port", str(PORTAL_PORT)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if portal_running():
            break
        time.sleep(0.1)
    print(f"门户已启动（pid {proc.pid}）。")
    return proc


def fresh_db() -> sqlite3.Connection:
    path = _db()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
    conn = storage.connect()
    storage.init_db(conn)
    engine.set_portal_url(conn, f"http://127.0.0.1:{PORTAL_PORT}")
    return conn


def do_import(conn, name: str = "演练批次") -> int:
    with open(DEMO_CSV, encoding="utf-8") as handle:
        result = engine.import_csv(conn, handle.read(), name)
    print(f"导入：{result}")
    return result["batch_id"]


def set_faults(conn, text: str, profile: str = "cm-default") -> None:
    from .portal import faults

    errors = faults.save_rules(conn, profile, text)
    if errors:
        raise SystemExit("故障规则有误：" + "；".join(errors))
    faults.FaultBook.reset(conn)
    print("故障规则：\n" + text)


def run_batch(conn, batch_id: int) -> dict:
    summary = runner.run_batch(conn, batch_id=batch_id)
    print(json.dumps(
        {k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False
    ))
    for row_id, key, status in summary["results"]:
        print(f"  行#{row_id} {key} → {status}")
    return summary


def print_state(conn) -> None:
    print("\n行状态：")
    for row in conn.execute(
        "SELECT r.*, b.name batch FROM wb_rows r JOIN wb_batches b ON b.id=r.batch_id ORDER BY r.id"
    ):
        print(
            f"  #{row['id']} {row['record_key']:<9} {row['status']:<16} "
            f"compare={row['compare_status']:<12} {row['error_summary'] or ''}"
        )
    print("\n冲突队列：")
    for conflict in engine.conflict_list(conn):
        print(
            f"  {conflict['record_key']} 原因={conflict['reason']} 当前={conflict['current']}"
        )
    print("\n最近事件：")
    for event in conn.execute("SELECT * FROM wb_events ORDER BY id DESC LIMIT 12"):
        print(f"  {event['at'][11:19]} {event['action']:<18} {event['detail']}")


def portal_dump(conn) -> dict:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{PORTAL_PORT}/api/records", timeout=3
    ) as response:
        return json.loads(response.read())


def scenario_drop_ack(conn, batch_id: int) -> None:
    set_faults(conn, "HK-1001:drop_ack:1\n*:ok")
    run_batch(conn, batch_id)
    print_state(conn)


def scenario_timeout(conn, batch_id: int) -> None:
    set_faults(conn, "HK-1003:timeout:1\n*:ok")
    run_batch(conn, batch_id)
    print_state(conn)


def scenario_manual_edit(conn, batch_id: int) -> None:
    """Human edits the portal after our first attempt's base was captured."""
    set_faults(conn, "*:ok")
    rows = engine.pending_rows(conn, batch_id)
    if not rows:
        print("没有待执行行。")
        return
    row = rows[0]
    client = engine.client_for(conn, row)
    opened = client.open_form(row["record_key"])
    # simulate a human saving a different address between our open and submit
    client.stage(opened["session_token"], {
        **opened["fields"],
        "address": opened["fields"]["address"] + "（人工更正）",
    })
    client.submit(opened["session_token"])
    print(f"已模拟他人在门户修改 {row['record_key']}。")
    run_batch(conn, batch_id)
    print_state(conn)


def scenario_crash(conn, batch_id: int, kill_after: float = 0.6) -> None:
    set_faults(conn, "HK-1001:timeout:1\n*:ok")
    env = dict(os.environ)
    cmd = [
        sys.executable, "-m", "app.workbench.executor", "run", "--batch", str(batch_id),
        "--kill-after", str(kill_after), "--timeout", "3",
    ]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env)
    proc.wait()
    print(f"执行器退出码：{proc.returncode}")
    print_state(conn)
    print("\n恢复：")
    engine.recover_interrupted(conn)
    engine.reconcile_pending(conn)
    print_state(conn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地演练脚本")
    parser.add_argument("scenario", choices=[
        "all", "drop_ack", "timeout", "manual_edit", "crash", "status", "reset",
    ])
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args(argv)

    if args.scenario == "reset":
        start_portal()
        conn = fresh_db()
        print("已重置。")
        return 0
    if args.scenario == "status":
        conn = storage.connect()
        storage.init_db(conn)
        print_state(conn)
        return 0

    start_portal()

    def prepare() -> tuple:
        conn = fresh_db() if not args.keep_db or args.scenario == "all" else storage.connect()
        if args.scenario == "crash":
            rows = conn.execute("SELECT MAX(batch_id) b FROM wb_rows").fetchone()
            batch_id = rows["b"] if rows and rows["b"] else do_import(conn)
        else:
            batch_id = do_import(conn)
        return conn, batch_id

    if args.scenario == "all":
        # Each scenario gets a clean database so its injected fault is real:
        # running them in sequence would leave nothing pending for the next.
        for name, runner_fn in (
            ("drop_ack", scenario_drop_ack),
            ("timeout", scenario_timeout),
            ("manual_edit", scenario_manual_edit),
            ("crash", scenario_crash),
        ):
            print(f"\n{'=' * 70}\n场景：{name}\n{'=' * 70}")
            conn, batch_id = prepare()
            runner_fn(conn, batch_id)
        return 0

    conn, batch_id = prepare()
    if args.scenario == "drop_ack":
        scenario_drop_ack(conn, batch_id)
    elif args.scenario == "timeout":
        scenario_timeout(conn, batch_id)
    elif args.scenario == "manual_edit":
        scenario_manual_edit(conn, batch_id)
    elif args.scenario == "crash":
        scenario_crash(conn, batch_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
