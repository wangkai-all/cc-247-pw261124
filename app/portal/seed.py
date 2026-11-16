"""Fictional seed data for the external portal."""

from __future__ import annotations

import sqlite3

from ..common import json_dumps, normalize_fields

SEED = [
    {
        "record_key": "HK-1001",
        "fields": {
            "full_name": "林晓禾",
            "phone": "13800001001",
            "address": "青禾街道 1 号 3 栋 201",
            "service_level": "basic",
            "note": "首次登记",
        },
        "updated_by": "portal-user",
    },
    {
        "record_key": "HK-1002",
        "fields": {
            "full_name": "周远",
            "phone": "13800001002",
            "address": "青禾街道 8 号 2 栋 1103",
            "service_level": "standard",
            "note": "",
        },
        "updated_by": "portal-user",
    },
    {
        "record_key": "HK-1003",
        "fields": {
            "full_name": "陈行舟",
            "phone": "13800001003",
            "address": "望江路 27 号平房",
            "service_level": "priority",
            "note": "独居，需电话确认",
        },
        "updated_by": "portal-user",
    },
    {
        "record_key": "HK-1004",
        "fields": {
            "full_name": "吴岚",
            "phone": "13800001004",
            "address": "望江路 5 号 6 栋 502",
            "service_level": "standard",
            "note": "",
        },
        "updated_by": "portal-user",
    },
    {
        "record_key": "HK-1005",
        "fields": {
            "full_name": "赵启明",
            "phone": "13800001005",
            "address": "梧桐巷 12 号",
            "service_level": "basic",
            "note": "2025 年迁入",
        },
        "updated_by": "portal-user",
    },
]

SEED_CREATED_AT = "2026-10-01T02:00:00+00:00"


def ensure_seed(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) c FROM portal_records").fetchone()["c"]:
        return
    for item in SEED:
        fields = normalize_fields(item["fields"])
        conn.execute(
            "INSERT INTO portal_records(record_key, full_name, phone, address, service_level, note,"
            " revision, updated_at, updated_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                item["record_key"],
                fields["full_name"],
                fields["phone"],
                fields["address"],
                fields["service_level"],
                fields["note"],
                1,
                SEED_CREATED_AT,
                item["updated_by"],
            ),
        )
        conn.execute(
            "INSERT INTO portal_revisions(record_key, revision, source, operator, changed_fields,"
            " snapshot, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                item["record_key"],
                1,
                "seed",
                "system",
                json_dumps(fields),
                json_dumps(fields),
                SEED_CREATED_AT,
            ),
        )
    conn.execute(
        "INSERT INTO wb_events(at, actor, action, detail) VALUES(?,?,?,?)",
        (SEED_CREATED_AT, "portal", "portal_seed", "写入 5 条虚构门户记录"),
    )
