"""The workbench UI: the pages and actions an operator actually uses."""

from __future__ import annotations

import unittest
import urllib.error
import urllib.parse
import urllib.request

from tests.support import StackTestCase


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url: str, data: dict | None = None) -> tuple[int, str]:
    """GET or form-POST, returning (status, body) with redirects disabled."""
    payload = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"} if payload else {},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class WorkbenchPagesTests(StackTestCase):
    def test_all_pages_render(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        row_id = stack.row("HK-1001")["id"]
        paths = [
            "/", "/queue", "/conflicts", "/executors", "/verify", "/settings",
            f"/batch/{batch_id}", f"/row/{row_id}", "/sample.csv",
        ]
        for path in paths:
            status, body = fetch(stack.workbench_url + path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body.strip(), path)

    def test_dashboard_lists_the_batch_and_queue(self) -> None:
        stack = self.stack
        stack.import_demo("社保补录 10 月")
        status, body = fetch(stack.workbench_url + "/queue")
        self.assertEqual(status, 200)
        self.assertIn("社保补录 10 月", body)
        self.assertIn("HK-1001", body)

    def test_import_through_form_and_run_batch(self) -> None:
        stack = self.stack
        stack.set_faults("*:ok")
        status, _ = fetch(
            stack.workbench_url + "/import",
            {"name": "页面导入", "csv": "record_key,full_name,phone,address,service_level,note\n"
                                      "HK-1001,林晓禾,13800001001,青禾街道 1 号 3 栋 202,standard,地址更正\n"},
        )
        self.assertEqual(status, 303)
        batch = dict(stack.conn.execute("SELECT * FROM wb_batches ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(batch["name"], "页面导入")

        status, _ = fetch(stack.workbench_url + f"/batch/{batch['id']}/run", {})
        self.assertEqual(status, 303)
        self.assertEqual(stack.row("HK-1001")["status"], "applied")

    def test_conflict_page_shows_attribution(self) -> None:
        stack = self.stack
        batch_id = stack.import_demo()
        stack.human_save("HK-1001", address="人工更正后的地址")
        from app.workbench import runner

        runner.run_batch(stack.conn, batch_id=batch_id)
        status, body = fetch(stack.workbench_url + "/conflicts")
        self.assertEqual(status, 200)
        self.assertIn("HK-1001", body)
        self.assertIn("他人修改", body)

    def test_takeover_button_page_and_manual_submit(self) -> None:
        stack = self.stack
        stack.import_demo()
        stack.human_save("HK-1002", note="人工备注")
        from app.workbench import engine

        engine.reconcile_row(stack.conn, stack.row("HK-1002"))
        row_id = stack.row("HK-1002")["id"]
        status, _ = fetch(stack.workbench_url + f"/row/{row_id}/takeover", {"operator": "王审"})
        self.assertEqual(status, 303)
        takeover = stack.conn.execute(
            "SELECT * FROM wb_takeovers WHERE row_id=? ORDER BY id DESC", (row_id,)
        ).fetchone()
        self.assertEqual(takeover["operator"], "王审")

        status, _ = fetch(
            stack.workbench_url + f"/row/{row_id}/manual-submit",
            {
                "operator": "王审",
                "full_name": "周远",
                "phone": "13800001002",
                "address": "青禾街道 8 号 2 栋 1201",
                "service_level": "standard",
                "note": "人工备注",
            },
        )
        self.assertEqual(status, 303)
        self.assertEqual(stack.row("HK-1002")["status"], "resolved")
        self.assertEqual(stack.portal_record("HK-1002")["fields"]["note"], "人工备注")

    def test_verify_page_reads_the_portal(self) -> None:
        stack = self.stack
        stack.import_demo()
        from app.workbench import runner

        runner.run_batch(stack.conn)
        status, _ = fetch(stack.workbench_url + "/verify/refresh", {})
        self.assertEqual(status, 303)
        status, body = fetch(stack.workbench_url + "/verify")
        self.assertEqual(status, 200)
        self.assertIn("全部一致", body)

    def test_api_rows_and_conflicts(self) -> None:
        stack = self.stack
        stack.import_demo()
        status, body = fetch(stack.workbench_url + "/api/rows")
        self.assertEqual(status, 200)
        self.assertIn("HK-1001", body)
        status, body = fetch(stack.workbench_url + "/api/conflicts")
        self.assertEqual(status, 200)


class PortalPageTests(StackTestCase):
    def test_step_by_step_form_and_query(self) -> None:
        stack = self.stack
        stack.set_faults("*:ok")
        # Open the form the way a person would.
        request = urllib.request.Request(
            stack.portal_url + "/record/HK-1001/open",
            data=b"",
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(request, timeout=10)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location", "")
        else:
            self.fail("open should redirect")
        self.assertIn("/record/HK-1001?session=", location)
        token = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["session"][0]

        status, body = fetch(stack.portal_url + "/record/HK-1001")
        self.assertEqual(status, 200)
        self.assertIn("分步录入", body)
        status, _ = fetch(
            stack.portal_url + "/record/HK-1001/stage",
            {
                "session_token": token,
                "action": "stage",
                "full_name": "林晓禾",
                "phone": "13800001001",
                "address": "青禾街道 1 号 3 栋 202",
                "service_level": "standard",
                "note": "地址更正",
            },
        )
        self.assertEqual(status, 303)
        status, _ = fetch(stack.portal_url + "/record/HK-1001/submit", {"session_token": token})
        self.assertEqual(status, 200)
        self.assertEqual(stack.portal_record("HK-1001")["revision"], 2)

        sub_token = stack.conn.execute(
            "SELECT sub_token FROM portal_sessions WHERE token=?", (token,)
        ).fetchone()["sub_token"]
        status, body = fetch(stack.portal_url + "/query?sub_token=" + sub_token)
        self.assertEqual(status, 200)
        self.assertIn("回执详情", body)

    def test_portal_never_injects_faults_for_browser(self) -> None:
        stack = self.stack
        stack.set_faults("HK-1001:reject:1:all\nHK-1001:drop_ack:1:all\n*:ok")
        status, _ = fetch(
            stack.portal_url + "/record/HK-1001",
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
