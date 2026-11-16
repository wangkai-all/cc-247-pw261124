"""Shared helpers: field schema, validation, CSV parsing, diffs, time.

All data in this project is fictional and stays on the local machine.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Business record schema (fictional "社区住户服务登记" records)
# ---------------------------------------------------------------------------

FIELDS: list[tuple[str, str]] = [
    ("full_name", "姓名"),
    ("phone", "手机号"),
    ("address", "地址"),
    ("service_level", "服务档位"),
    ("note", "备注"),
]
FIELD_NAMES = [name for name, _ in FIELDS]
FIELD_LABELS = dict(FIELDS)

SERVICE_LEVELS = {
    "basic": "基础",
    "standard": "标准",
    "priority": "优先",
}

RECORD_KEY_RE = re.compile(r"^[A-Z]{2}-\d{4}$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

MAX_ATTEMPTS = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def in_seconds(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds))


def normalize_fields(raw: dict[str, Any] | None) -> dict[str, str]:
    raw = raw or {}
    return {name: str(raw.get(name, "") or "").strip() for name in FIELD_NAMES}


def validate_fields(fields: dict[str, str]) -> dict[str, str]:
    """Return {field: message}. Empty dict means valid."""
    errors: dict[str, str] = {}
    name = fields.get("full_name", "")
    if not name:
        errors["full_name"] = "姓名不能为空"
    elif len(name) > 30:
        errors["full_name"] = "姓名不能超过 30 个字符"

    phone = fields.get("phone", "")
    if not phone:
        errors["phone"] = "手机号不能为空"
    elif not PHONE_RE.match(phone):
        errors["phone"] = "手机号格式不正确（需为 11 位大陆手机号）"

    address = fields.get("address", "")
    if not address:
        errors["address"] = "地址不能为空"
    elif len(address) > 80:
        errors["address"] = "地址不能超过 80 个字符"

    level = fields.get("service_level", "")
    if level not in SERVICE_LEVELS:
        errors["service_level"] = "服务档位必须是基础/标准/优先"

    if len(fields.get("note", "")) > 120:
        errors["note"] = "备注不能超过 120 个字符"
    return errors


def validate_record_key(key: str) -> str | None:
    key = (key or "").strip()
    if not key:
        return "业务编号不能为空"
    if not RECORD_KEY_RE.match(key):
        return "业务编号格式应为两个大写字母、短横线、四位数字，例如 HK-1001"
    return None


def empty_fields() -> dict[str, str]:
    return {name: "" for name in FIELD_NAMES}


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["record_key", *FIELD_NAMES]
CSV_HEADER_ALIASES = {
    "业务编号": "record_key",
    "姓名": "full_name",
    "手机号": "phone",
    "地址": "address",
    "服务档位": "service_level",
    "备注": "note",
}


def decode_csv(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030", errors="replace")


def parse_csv_rows(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """Parse CSV text; returns (rows, fatal_errors).

    Rows are returned with normalized header names. Per-row validation is done
    by the importer so that one bad line does not discard the batch.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], ["CSV 为空或缺少表头"]
    header_map: dict[str, str] = {}
    for raw_name in reader.fieldnames:
        name = (raw_name or "").strip()
        canonical = CSV_HEADER_ALIASES.get(name, name)
        header_map[raw_name] = canonical
    missing = [c for c in CSV_COLUMNS if c not in header_map.values()]
    if missing:
        labels = [FIELD_LABELS.get(m, m) for m in missing]
        return [], ["缺少必需列：" + "、".join(labels)]
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {header_map[k]: (v or "").strip() for k, v in raw.items() if k is not None}
        rows.append(row)
    return rows, []


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Field-by-field comparison
# ---------------------------------------------------------------------------

DIFF_LABELS = {
    "unchanged": "未变更",
    "applied": "本次已生效",
    "pending": "本次未生效",
    "others": "他人修改",
    "conflict": "与人工修改冲突",
    "same_as_target": "与目标一致",
    "other_last": "他人后写入（值恰好相同）",
    "not_written": "与目标一致但非本次写入",
}


def field_diff(
    base: dict[str, str] | None,
    target: dict[str, str] | None,
    current: dict[str, str] | None,
) -> list[dict[str, str]]:
    """Compare pre-submission base, our target and the portal's current state."""
    base = normalize_fields(base)
    target = normalize_fields(target)
    current = normalize_fields(current)
    rows = []
    for name in FIELD_NAMES:
        b, t, c = base[name], target[name], current[name]
        if b == t:
            if c == b:
                status = "unchanged"
            else:
                status = "others"
        elif c == t:
            status = "applied"
        elif c == b:
            status = "pending"
        else:
            status = "conflict"
        rows.append(
            {
                "field": name,
                "label": FIELD_LABELS[name],
                "base": b,
                "target": t,
                "current": c,
                "status": status,
                "status_label": DIFF_LABELS[status],
            }
        )
    return rows


def attributed_diff(
    base: dict[str, str] | None,
    target: dict[str, str] | None,
    current: dict[str, str] | None,
    ours_written: set[str],
    others_written: set[str],
    our_last: set[str] | None = None,
) -> list[dict[str, str]]:
    """Field-by-field comparison with *who wrote it* taken from portal history.

    ``ours_written`` / ``others_written`` are field names that our submission
    (matched by form token) or someone else's revision touched after the
    pre-submission base. ``our_last`` is the subset whose most recent write was
    ours (relevant when both sides edited the same field). This prevents us
    from claiming "本次已生效" merely because a human happened to write the
    same value we wanted.
    """
    base = normalize_fields(base)
    target = normalize_fields(target)
    current = normalize_fields(current)
    our_last = ours_written if our_last is None else our_last
    rows = []
    for name in FIELD_NAMES:
        b, t, c = base[name], target[name], current[name]
        ours = name in ours_written
        others = name in others_written
        if ours and others:
            # Both sides wrote it after the base; the later write wins.
            if name in our_last:
                status = "applied" if c == t else "conflict"
            else:
                status = "others" if c != t else "other_last"
        elif ours:
            status = "applied" if c == t else "conflict"
        elif others:
            status = "others"
        elif b == t:
            status = "unchanged" if c == b else "others"
        elif c == b:
            status = "pending"
        elif c == t:
            # Target value present without a matching write in the portal
            # history: someone else typed the same value. Do not call it ours.
            status = "same_as_target"
        else:
            status = "conflict"
        rows.append(
            {
                "field": name,
                "label": FIELD_LABELS[name],
                "base": b,
                "target": t,
                "current": c,
                "status": status,
                "status_label": {
                    "same_as_target": "与目标一致但非本次写入",
                }.get(status, DIFF_LABELS[status]),
            }
        )
    return rows


def revision_diff(before: dict[str, str] | None, after: dict[str, str] | None) -> list[dict[str, str]]:
    before = normalize_fields(before)
    after = normalize_fields(after)
    rows = []
    for name in FIELD_NAMES:
        if before[name] != after[name]:
            rows.append(
                {
                    "field": name,
                    "label": FIELD_LABELS[name],
                    "before": before[name],
                    "after": after[name],
                }
            )
    return rows
