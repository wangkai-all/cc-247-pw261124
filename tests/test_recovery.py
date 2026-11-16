"""Crash mid-submit, then recovery.

The executor runs in a real separate process that is killed while its HTTP
submit is in flight. Recovery must not assume the write failed (it may have
landed) and must not assume it succeeded (it may not have).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

from tests.support import StackTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CrashRecoveryTests(StackTestCase):
    def _crash_run(self, batch_id: int, faults: str) -> subprocess.CompletedProcess:
        stack = self.stack
        stack.set_faults(faults)
        from app.portal import server as portal_server

        portal_server.FAULT_DELAY_SECONDS = 1.5
        env = dict(os.environ)
        env["RURAL_PORTAL_DB"] = stack.db_path
        env["PORTAL_FAULT_DELAY_SECONDS"] = "1.5"
        return subprocess.run(
            [
                sys.executable, "-m", "app.workbench.executor",
                "--portal", stack.portal_url,
                "run", "--batch", str(batch_id),
                "--kill-after", "0.5",
                "--timeout", "5",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _assert_no_success_recorded(self, row_id: int) -> None:
        row = dict(self.stack.conn.execute("SELECT * FROM wb_rows WHERE id=?", (row_id,)).fetchone())
        self.assertNotEqual(row["status"], "applied")
        self.assertNotEqual(row["status"], "resolved")

    def test_crash_before_submit_lands_is_recovered_as_not_landed(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        # kill happens during the 1.5s delayed *open*: nothing can be written
        proc = self._crash_run(batch_id, "HK-1001:timeout:1:open\n*:ok")
        self.assertEqual(proc.returncode, 137, proc.stderr)
        row = stack.row("HK-1001")
        self._assert_no_success_recorded(row["id"])
        self.assertEqual(row["status"], "running")

        from app.workbench import engine

        result = engine.recover_interrupted(stack.conn)
        self.assertIn("HK-1001", result["rows"])
        row = stack.row("HK-1001")
        self.assertEqual(row["status"], "retry_wait")
        self.assertEqual(row["compare_status"], "not_landed")
        self.assertEqual(stack.portal_record("HK-1001")["revision"], 1)

        attempts = stack.attempts(row["id"])
        self.assertTrue(any(a["status"] == "abandoned" for a in attempts))

    def test_crash_during_delayed_submit_is_recovered_from_history(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        # The submit is committed, then the portlet delays its response 1.5s;
        # the executor dies in that window with the receipt unread.
        proc = self._crash_run(batch_id, "HK-1001:timeout:1\n*:ok")
        self.assertEqual(proc.returncode, 137, proc.stderr)
        row = stack.row("HK-1001")
        self._assert_no_success_recorded(row["id"])
        self.assertEqual(row["status"], "running")
        self.assertEqual(stack.portal_record("HK-1001")["revision"], 2)

        from app.workbench import engine

        engine.recover_interrupted(stack.conn)
        row = stack.row("HK-1001")
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["compare_status"], "applied")
        # Recovery proved it via the form token, not by guessing.
        attempts = stack.attempts(row["id"])
        self.assertEqual(
            [a["verify_verdict"] for a in attempts if a["verify_verdict"]], ["applied"]
        )
        # And it did not resubmit: revision is still 2.
        self.assertEqual(stack.portal_record("HK-1001")["revision"], 2)

    def test_rerun_after_recovery_does_not_duplicate(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        self._crash_run(batch_id, "HK-1001:timeout:1\n*:ok")
        from app.workbench import engine, runner

        engine.recover_interrupted(stack.conn)
        runner.run_batch(stack.conn, batch_id=batch_id)
        self.assertEqual(stack.portal_record("HK-1001")["revision"], 2)
        self.assertEqual(stack.row("HK-1001")["status"], "applied")


if __name__ == "__main__":
    unittest.main()
