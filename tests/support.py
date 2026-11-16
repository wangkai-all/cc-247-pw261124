"""Shared test harness: two in-process servers, one temporary database.

Everything is stdlib (``unittest``); no pytest is required. Each test gets a
fresh temporary SQLite file, a mock portal and a workbench UI, all bound to
127.0.0.1 on ephemeral ports. All data is fictional and disappears with the
temporary directory.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest

from app import storage
from app.portal import faults
from app.portal import server as portal_server
from app.web import serve
from app.workbench import engine, server as wb_server

DEMO_CSV = (
    "record_key,full_name,phone,address,service_level,note\n"
    "HK-1001,林晓禾,13800001001,青禾街道 1 号 3 栋 202,standard,地址更正\n"
    "HK-1002,周远,13800001002,青禾街道 8 号 2 栋 1201,standard,电话回访\n"
    "HK-1003,陈行舟,13800001003,望江路 27 号平房,priority,独居，需每日电话确认\n"
)


class Stack:
    """A running portal + workbench pair over one temporary database."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="wb-test-")
        self.db_path = os.path.join(self.dir, "portal.db")
        self._previous_db = os.environ.get(storage.DB_ENV)
        os.environ[storage.DB_ENV] = self.db_path
        self.conn = storage.connect()
        storage.init_db(self.conn, portal_seed=True)

        portal_server.set_connection_factory(storage.connect)
        self.portal = serve(
            portal_server.build_router(), "127.0.0.1", 0, storage.connect, "MockPortal/1.0"
        )
        self.portal_url = f"http://127.0.0.1:{self.portal.server_address[1]}"
        engine.set_portal_url(self.conn, self.portal_url)

        wb_server.set_connection_factory(storage.connect)
        self.workbench = serve(
            wb_server.build_router(), "127.0.0.1", 0, storage.connect, "CollectWorkbench/1.0"
        )
        self.workbench_url = f"http://127.0.0.1:{self.workbench.server_address[1]}"

        self._threads = [
            threading.Thread(target=srv.serve_forever, daemon=True)
            for srv in (self.portal, self.workbench)
        ]
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        for srv in (self.portal, self.workbench):
            srv.shutdown()
            srv.server_close()
        self.conn.close()
        if self._previous_db is None:
            os.environ.pop(storage.DB_ENV, None)
        else:
            os.environ[storage.DB_ENV] = self._previous_db
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- convenience -----------------------------------------------------

    def set_faults(self, text: str) -> None:
        errors = faults.save_rules(self.conn, faults.DEFAULT_PROFILE, text)
        if errors:
            raise AssertionError("bad fault rules: %s" % errors)
        faults.FaultBook.reset(self.conn)

    def import_demo(self, name: str = "测试批次") -> int:
        result = engine.import_csv(self.conn, DEMO_CSV, name)
        assert result["ok"], result
        return result["batch_id"]

    def row(self, record_key: str) -> dict:
        raw = self.conn.execute(
            "SELECT * FROM wb_rows WHERE record_key=? ORDER BY id DESC LIMIT 1", (record_key,)
        ).fetchone()
        assert raw is not None, record_key
        return dict(raw)

    def attempts(self, row_id: int) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM wb_attempts WHERE row_id=? ORDER BY id", (row_id,)
            ).fetchall()
        ]

    def portal_record(self, record_key: str) -> dict:
        from app.workbench.portal_client import PortalClient

        return PortalClient(self.portal_url).get_record(record_key)

    def human_save(self, record_key: str, **changes: str) -> dict:
        """Simulate a person editing the record in the browser portal."""
        from app.workbench.portal_client import PortalClient

        self.set_faults("*:ok")  # the browser is never faulted
        client = PortalClient(self.portal_url, source="human")
        opened = client.open_form(record_key)
        fields = dict(opened["fields"])
        fields.update(changes)
        client.stage(opened["session_token"], fields)
        return client.submit(opened["session_token"])


class StackTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = Stack()
        self.addCleanup(self.stack.close)
        # Make timeout faults cheap: the portal still commits the write first.
        self._old_delay = portal_server.FAULT_DELAY_SECONDS
        portal_server.FAULT_DELAY_SECONDS = 0.4
        self.addCleanup(self._restore_delay)

    def _restore_delay(self) -> None:
        portal_server.FAULT_DELAY_SECONDS = self._old_delay
