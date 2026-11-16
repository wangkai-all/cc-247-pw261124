"""Engine behaviour against a live in-process portal.

Covers the cases the task names explicitly:
  * lost receipt  → never retried blind, never recorded as success on faith
  * timeout       → write landed, recovery proves it via the form token
  * validation    → definite failure, not retried
  * human edits   → conflict queue with per-field attribution
  * takeovers     → executor cannot overwrite a human correction
  * crash         → recovery reconciles instead of assuming failure/success
"""

from __future__ import annotations

import unittest

from tests.support import StackTestCase


def fields(**changes: str) -> dict[str, str]:
    base = {
        "full_name": "林晓禾",
        "phone": "13800001001",
        "address": "青禾街道 1 号 3 栋 202",
        "service_level": "standard",
        "note": "地址更正",
    }
    base.update(changes)
    return base


class ImportTests(StackTestCase):
    def test_import_snapshots_base_and_marks_noop(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        rows = {
            r["record_key"]: r
            for r in stack.conn.execute(
                "SELECT * FROM wb_rows WHERE batch_id=? ORDER BY row_number", (batch_id,)
            ).fetchall()
        }
        self.assertEqual(set(rows), {"HK-1001", "HK-1002", "HK-1003"})
        self.assertEqual(rows["HK-1001"]["status"], "pending")
        self.assertIsNotNone(rows["HK-1001"]["base_snapshot"])
        self.assertEqual(rows["HK-1001"]["base_revision"], 1)

    def test_invalid_rows_are_kept_but_not_submitted(self) -> None:
        stack = self.stack
        csv_text = (
            "record_key,full_name,phone,address,service_level,note\n"
            "HK-2001,张三,12345,某街道,basic,\n"
            "BADKEY,李四,13800009999,某街道,basic,\n"
        )
        from app.workbench import engine

        result = engine.import_csv(stack.conn, csv_text, "坏数据")
        self.assertTrue(result["ok"])
        self.assertEqual(result["invalid"], 2)
        self.assertEqual(engine.pending_rows(stack.conn, result["batch_id"]), [])

    def test_duplicate_open_key_is_rejected(self) -> None:
        stack = self.stack
        from app.workbench import engine

        stack.import_demo("第一批")
        csv_text = (
            "record_key,full_name,phone,address,service_level,note\n"
            "HK-1001,林晓禾,13800001001,新地址,basic,\n"
        )
        result = engine.import_csv(stack.conn, csv_text, "第二批")
        self.assertTrue(result["ok"])
        self.assertEqual(result["invalid"], 1)


class LostReceiptTests(StackTestCase):
    def test_drop_ack_reconciles_to_applied_without_duplicate(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("HK-1001:drop_ack:1\n*:ok")

        from app.workbench import runner

        summary = runner.run_batch(stack.conn, batch_id=batch_id)
        row = stack.row("HK-1001")
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["compare_status"], "applied")

        record = stack.portal_record("HK-1001")
        self.assertEqual(record["revision"], 2, "exactly one write landed")
        self.assertEqual(record["fields"]["address"], fields()["address"])

        attempts = stack.attempts(row["id"])
        self.assertGreaterEqual(len(attempts), 1)
        self.assertTrue(any(a["error_kind"] == "receipt_lost" for a in attempts))
        self.assertIn("applied", [a["verify_verdict"] for a in attempts])
        self.assertGreaterEqual(summary["applied"], 1)

    def test_drop_ack_does_not_reuse_the_form_token(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("HK-1001:drop_ack:1\n*:ok")
        from app.workbench import runner

        runner.run_batch(stack.conn, batch_id=batch_id, limit=1)
        tokens = {rev["session_token"] for rev in stack.portal_record("HK-1001")["history"]}
        attempts = stack.attempts(stack.row("HK-1001")["id"])
        self.assertEqual(len([a for a in attempts if a["form_token"]]), 1)
        self.assertIn(attempts[0]["form_token"], tokens)


class TimeoutTests(StackTestCase):
    def test_timeout_that_landed_is_classified_applied(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("HK-1002:timeout:1\n*:ok")
        from app.portal import server as portal_server
        from app.workbench import runner

        portal_server.FAULT_DELAY_SECONDS = 2.0
        runner.run_batch(stack.conn, batch_id=batch_id, timeout=0.6)
        row = stack.row("HK-1002")
        self.assertEqual(row["status"], "applied")
        self.assertEqual(stack.portal_record("HK-1002")["revision"], 2)

    def test_timeout_at_open_is_retryable_and_never_mistaken_for_edit(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        # A timeout at "open" cannot have written anything, so the row must be
        # safe to retry — and the executor must not read the missing response
        # as an empty record (which would look like a human wiped every field).
        stack.set_faults("HK-1003:timeout:1:open\n*:ok")
        from app.portal import server as portal_server
        from app.workbench import runner

        portal_server.FAULT_DELAY_SECONDS = 2.0
        runner.run_batch(stack.conn, batch_id=batch_id, timeout=0.6)
        row = stack.row("HK-1003")
        attempts = stack.attempts(row["id"])
        first = attempts[0]
        self.assertEqual(first["error_kind"], "timeout")
        self.assertIn(row["status"], {"applied", "retry_wait"})
        self.assertNotEqual(row["status"], "conflict")
        self.assertEqual(stack.portal_record("HK-1003")["revision"], 2)


class ValidationTests(StackTestCase):
    def test_reject_marks_row_invalid_and_never_retries(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("HK-1002:reject:1\n*:ok")
        from app.workbench import runner

        runner.run_batch(stack.conn, batch_id=batch_id)
        row = stack.row("HK-1002")
        self.assertEqual(row["status"], "invalid")
        self.assertEqual(stack.portal_record("HK-1002")["revision"], 1)
        self.assertEqual(len([a for a in stack.attempts(row["id"]) if a["stage"] == "submit"]), 1)

    def test_real_validation_error_from_portal(self) -> None:
        stack = self.stack
        from app.workbench import engine

        result = engine.import_csv(
            stack.conn,
            "record_key,full_name,phone,address,service_level,note\n"
            "HK-1001,林晓禾,123,青禾街道,basic,\n",
            "校验",
        )
        self.assertEqual(result["invalid"], 1)


class HumanEditTests(StackTestCase):
    def test_edit_before_submit_goes_to_conflict_queue(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("*:ok")
        stack.human_save("HK-1001", address="人工更正后的地址")

        from app.workbench import runner

        runner.run_batch(stack.conn, batch_id=batch_id)
        row = stack.row("HK-1001")
        self.assertEqual(row["status"], "conflict")
        self.assertEqual(row["compare_status"], "others")
        # Nobody overwrote the human value.
        self.assertEqual(
            stack.portal_record("HK-1001")["fields"]["address"], "人工更正后的地址"
        )
        conflicts = [
            dict(c)
            for c in stack.conn.execute(
                "SELECT * FROM wb_conflicts WHERE row_id=? AND status='open'", (row["id"],)
            )
        ]
        self.assertEqual(len(conflicts), 1)

    def test_partial_landing_then_human_edit_attributes_latest_writer(self) -> None:
        """Our write lands, then a human changes the same field.

        The field must be reported as the human's, not "本次已生效".
        """
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("*:ok")
        from app.workbench import engine
        from app.workbench.portal_client import PortalClient

        row = stack.row("HK-1001")
        target = engine.normalize_fields(engine.json_loads(row["target"], {}))

        client = PortalClient(stack.portal_url, row_number=row["row_number"])
        opened = client.open_form("HK-1001")
        client.stage(opened["session_token"], target)
        client.submit(opened["session_token"])

        # The attempt whose receipt was "lost" and whose token must be proven.
        attempt = engine.start_attempt(stack.conn, row)
        engine.finish_attempt(
            stack.conn, attempt["id"], status="unknown", stage="submit",
            form_token=opened["session_token"], finished_at="2026-01-01T00:00:00+00:00",
        )
        stack.human_save("HK-1001", note="人工后来改写")

        detail = engine.reconcile_row(stack.conn, row)
        self.assertEqual(detail["verdict"], "partial")
        statuses = {f["field"]: f["status"] for f in detail["fields"]}
        self.assertEqual(statuses["address"], "applied")
        self.assertEqual(statuses["note"], "others")
        self.assertEqual(stack.row("HK-1001")["status"], "conflict")

    def test_human_types_same_value_we_wanted(self) -> None:
        """Current == target but the write was not ours: never claim it."""
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("*:ok")
        from app.workbench import engine

        row = stack.row("HK-1003")
        target = engine.normalize_fields(engine.json_loads(row["target"], {}))
        stack.human_save(
            "HK-1003",
            address=target["address"],
            note=target["note"],
        )
        # Pretend our submit timed out even though it never reached the portal.
        attempt = engine.start_attempt(stack.conn, row)
        engine.finish_attempt(
            stack.conn, attempt["id"], status="unknown", stage="submit",
            form_token="frm_never_sent", finished_at="2026-01-01T00:00:00+00:00",
        )
        detail = engine.reconcile_row(stack.conn, row)
        self.assertEqual(detail["verdict"], "matched_elsewhere")
        self.assertEqual(stack.row("HK-1003")["status"], "resolved")
        self.assertEqual(stack.portal_record("HK-1003")["revision"], 2)


class TakeoverTests(StackTestCase):
    def test_second_executor_cannot_acquire_lease(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("*:ok")
        from app.workbench import engine, runner

        row = stack.row("HK-1002")
        first = runner.new_executor_id()
        self.assertTrue(engine.acquire_lease(stack.conn, row["id"], first, None))
        second = runner.new_executor_id()
        result = runner.run_one(stack.conn, row["id"])
        self.assertFalse(result["ok"])
        self.assertIn("其他执行器", result["error"])
        engine.release_lease(stack.conn, first)

    def test_takeover_revokes_lease_and_blocks_submit(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("*:ok")
        from app.workbench import engine, runner

        row = stack.row("HK-1001")
        executor_id = runner.new_executor_id()
        self.assertTrue(engine.acquire_lease(stack.conn, row["id"], executor_id, None))
        engine.take_over(stack.conn, row["id"], "reviewer")
        with self.assertRaises(engine.RowBusy):
            engine.execute_row(stack.conn, row["id"], executor_id)
        self.assertEqual(stack.portal_record("HK-1001")["revision"], 1)

    def test_retry_rebases_so_the_next_run_does_not_bounce_back(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("*:ok")
        stack.human_save("HK-1001", address="人工先改的地址")
        from app.workbench import engine, runner

        runner.run_batch(stack.conn, batch_id=batch_id)
        row = stack.row("HK-1001")
        self.assertEqual(row["status"], "conflict")

        # The reviewer decides to overwrite the human value with the CSV target.
        result = engine.retry_conflict(stack.conn, row["id"], "reviewer")
        self.assertTrue(result["ok"])
        self.assertEqual(result["revision"], 2)
        # Base was rebased to the portal's current state.
        self.assertEqual(
            engine.json_loads(stack.row("HK-1001")["base_snapshot"], {})["address"],
            "人工先改的地址",
        )
        runner.run_batch(stack.conn, batch_id=batch_id)
        self.assertEqual(stack.row("HK-1001")["status"], "applied")
        self.assertEqual(
            stack.portal_record("HK-1001")["fields"]["address"], "青禾街道 1 号 3 栋 202"
        )

    def test_manual_submit_after_takeover_wins(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.set_faults("*:ok")
        from app.workbench import engine

        row = stack.row("HK-1002")
        stack.human_save("HK-1002", note="人工备注")
        engine.reconcile_row(stack.conn, stack.row("HK-1002"))
        engine.take_over(stack.conn, row["id"], "reviewer")
        merged = dict(fields(**{"address": "青禾街道 8 号 2 栋 1201", "note": "人工备注"}))
        result = engine.manual_submit(stack.conn, row["id"], merged, "reviewer")
        self.assertTrue(result["ok"], result)
        record = stack.portal_record("HK-1002")
        self.assertEqual(record["fields"]["note"], "人工备注")
        self.assertEqual(record["fields"]["address"], merged["address"])
        self.assertEqual(stack.row("HK-1002")["status"], "resolved")

    def test_manual_submit_detects_version_conflict(self) -> None:
        stack = self.stack
        stack.import_demo()
        stack.set_faults("*:ok")
        from app.workbench import engine
        from app.workbench.portal_client import PortalClient

        row = stack.row("HK-1001")
        engine.take_over(stack.conn, row["id"], "reviewer")
        client = PortalClient(stack.portal_url, source="human")
        opened = client.open_form("HK-1001")
        path = {"session_token": opened["session_token"]}
        # Monkeypatch the client used inside manual_submit so it opens, then
        # let a human write before the submit reaches the portal.
        original = engine.client_for

        def racing_client(conn, row=None, timeout=2.5):
            inner = original(conn, row, timeout=timeout)

            class Racing:
                def open_form(self, key):
                    return opened

                def stage(self, token, values):
                    stack.human_save("HK-1001", note="抢先修改")
                    return inner.stage(token, values)

                def submit(self, token):
                    return inner.submit(token)

            return Racing()

        engine.client_for = racing_client
        self.addCleanup(setattr, engine, "client_for", original)
        result = engine.manual_submit(stack.conn, row["id"], fields(), "reviewer")
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "version_conflict")
        self.assertEqual(stack.row("HK-1001")["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
