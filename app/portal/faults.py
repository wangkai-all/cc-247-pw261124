"""Deterministic fault injection for the mock portal.

The executor always identifies itself with a dedicated User-Agent so that a
human using the browser is *never* subjected to injected faults. Fault rules
are stored in workbench settings and can be edited on the "演练/故障注入"
page or via ``--faults`` on the executor CLI.

Rule syntax (one per line):
    PATTERN:ACTION[:COUNT[:STAGE]]

  PATTERN  record key (HK-1001), ``*``, or ``row:<n>``
  ACTION   ok | timeout | drop_ack | reject | lock | 5xx
  COUNT    number of matching requests to affect (default 1, ``inf`` = all)
  STAGE    submit (default) | open | all

Stages are consumed per pattern+action, so a rule only fires COUNT times in
total per stage. Example::

    HK-1001:drop_ack:1      # first submit applies but the receipt is lost
    HK-1002:reject:1        # first submit rejected with a 422
    HK-1003:timeout:1       # submit is processed but response is dropped
    *:ok                    # everything else succeeds
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field

from ..common import iso, json_dumps, json_loads

ACTIONS = {"ok", "timeout", "drop_ack", "reject", "lock", "5xx"}
STAGES = {"open", "submit", "precheck", "all"}
# stage "all" covers every stage below
STAGE_ALIASES = {"open": "open", "submit": "submit", "precheck": "precheck", "all": "all"}

DEFAULT_PROFILE = "cm-default"
DEFAULT_RULES = "\n".join(
    [
        "# 默认演练：超时、校验失败、回执丢失各一次",
        "HK-1001:drop_ack:1",
        "HK-1002:reject:1",
        "HK-1003:timeout:1",
        "*:ok",
    ]
)

# Fault kinds that leave the request in limbo (outcome unknown).
UNKNOWN_OUTCOMES = {"timeout", "drop_ack"}
# Fault kinds that guarantee nothing was written.
NOT_WRITTEN = {"reject", "lock", "5xx"}


@dataclass
class Decision:
    action: str
    stage: str
    pattern: str
    rule_index: int
    detail: str = ""

    def applies_to_unknown(self) -> bool:
        return self.action in UNKNOWN_OUTCOMES


@dataclass
class FaultState:
    """Per-request state carrying what the fault rule decided."""

    rules: list["Rule"] = field(default_factory=list)
    fired: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class Rule:
    pattern: str
    action: str
    count: int
    stage: str
    consumed: int = 0
    raw: str = ""

    def matches(self, record_key: str, row_number: int | None, stage: str) -> bool:
        if self.stage != "all" and self.stage != stage:
            return False
        if self.consumed >= self.count:
            return False
        pattern = self.pattern.strip()
        if pattern == "*":
            return True
        if pattern.startswith("row:") and row_number is not None:
            return pattern[4:].strip() == str(row_number)
        return pattern.upper() == record_key.upper()


def parse_rules(text: str) -> tuple[list[Rule], list[str]]:
    rules: list[Rule] = []
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 2:
            errors.append(f"第 {lineno} 行缺少动作：{line}")
            continue
        pattern, action = parts[0].strip(), parts[1].strip().lower()
        if not pattern:
            errors.append(f"第 {lineno} 行缺少匹配对象")
            continue
        if action not in ACTIONS:
            errors.append(f"第 {lineno} 行动作无效：{action}")
            continue
        count = 1
        stage = "submit"
        if len(parts) >= 3 and parts[2].strip():
            raw_count = parts[2].strip().lower()
            if raw_count in {"inf", "all", "*"}:
                count = 10**9
            else:
                try:
                    count = int(raw_count)
                except ValueError:
                    errors.append(f"第 {lineno} 行次数无效：{parts[2]}")
                    continue
                if count < 1:
                    errors.append(f"第 {lineno} 行次数必须为正整数")
                    continue
        if len(parts) >= 4 and parts[3].strip():
            stage = parts[3].strip().lower()
            if stage not in STAGES:
                errors.append(f"第 {lineno} 行阶段无效：{stage}")
                continue
        rules.append(
            Rule(pattern=pattern, action=action, count=count, stage=stage, raw=line)
        )
    return rules, errors


def load_rules(conn: sqlite3.Connection, profile: str | None = None) -> tuple[str, list[Rule]]:
    profile = profile or DEFAULT_PROFILE
    raw = conn.execute(
        "SELECT value FROM wb_settings WHERE key=?", (f"fault_profile:{profile}",)
    ).fetchone()
    text = raw["value"] if raw else (DEFAULT_RULES if profile == DEFAULT_PROFILE else "")
    rules, _ = parse_rules(text)
    return text, rules


def save_rules(conn: sqlite3.Connection, profile: str, text: str) -> list[str]:
    rules, errors = parse_rules(text)
    if errors:
        return errors
    conn.execute(
        "INSERT INTO wb_settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"fault_profile:{profile}", text),
    )
    return []


class FaultBook:
    """Tracks how many times each rule has fired per stage.

    A small table is used instead of process memory so that the mock portal
    behaves consistently across restarts (the CLI executor runs while the
    portal may restart).
    """

    def __init__(self, conn: sqlite3.Connection, profile: str) -> None:
        self.conn = conn
        self.profile = profile

    def _key(self, rule: Rule, stage: str) -> str:
        # The rule text is part of the key so that editing rules resets them.
        return f"fault:{self.profile}:{rule.stage}:{rule.raw}"

    def used(self, rule: Rule) -> int:
        return self._used(self._key(rule, rule.stage))

    def decide(self, rules: list[Rule], record_key: str, row_number: int | None, stage: str) -> Decision | None:
        for index, rule in enumerate(rules):
            if not rule.matches(record_key, row_number, stage):
                continue
            key = self._key(rule, stage)
            used = self._used(key)
            if used >= rule.count:
                continue
            self._use(key)
            return Decision(
                action=rule.action,
                stage=stage,
                pattern=rule.pattern,
                rule_index=index,
                detail=rule.raw,
            )
        return None

    def _used(self, key: str) -> int:
        row = self.conn.execute(
            "SELECT value FROM wb_settings WHERE key=?", ("fault_used:" + key,)
        ).fetchone()
        return int(json_loads(row["value"], 0)) if row else 0

    def _use(self, key: str) -> None:
        used = self._used(key) + 1
        self.conn.execute(
            "INSERT INTO wb_settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("fault_used:" + key, json_dumps(used)),
        )

    @staticmethod
    def reset(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM wb_settings WHERE key LIKE 'fault_used:%'")


def new_sub_token() -> str:
    return "sub_" + uuid.uuid4().hex[:16]


def receipt(summary: str, record_key: str = "", revision: int | None = None) -> dict:
    token = new_sub_token()
    payload = {
        "receipt_id": "rcpt_" + uuid.uuid4().hex[:12],
        "submitted_at": iso(),
        "summary": summary,
        "record_key": record_key,
        "revision": revision,
        "sub_token": token,
    }
    return payload


def late_body(record_key: str, sub_token: str) -> str:
    """What the lost response would have contained (for the drill page)."""
    return (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        'X-Simulated-Fault: drop_ack\r\n\r\n'
        + json_dumps(
            {
                "ok": True,
                "record_key": record_key,
                "receipt": {"sub_token": sub_token, "summary": "已提交"},
            }
        )
    )
