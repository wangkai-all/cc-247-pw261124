"""Field-by-field attribution: who wrote what, and which write was last."""

from __future__ import annotations

import unittest

from app.common import attributed_diff, field_diff, normalize_fields


def fields(**changes: str) -> dict[str, str]:
    base = {
        "full_name": "林晓禾",
        "phone": "13800001001",
        "address": "旧地址",
        "service_level": "basic",
        "note": "旧备注",
    }
    base.update(changes)
    return normalize_fields(base)


def by_name(rows: list[dict]) -> dict[str, str]:
    return {row["field"]: row["status"] for row in rows}


class FieldDiffTests(unittest.TestCase):
    def test_plain_diff_without_history(self) -> None:
        base = fields()
        target = fields(address="新地址")
        self.assertEqual(by_name(field_diff(base, target, base))["address"], "pending")
        self.assertEqual(by_name(field_diff(base, target, target))["address"], "applied")
        other = fields(address="别人改的")
        self.assertEqual(by_name(field_diff(base, target, other))["address"], "conflict")

    def test_our_write_is_applied_even_if_value_differs(self) -> None:
        # History says we wrote address, but the portal now shows something
        # else: a later write by someone else must not be called ours.
        base = fields()
        target = fields(address="目标地址")
        current = fields(address="目标地址")
        rows = by_name(attributed_diff(base, target, current, {"address"}, set()))
        self.assertEqual(rows["address"], "applied")

        current = fields(address="他人地址")
        rows = by_name(attributed_diff(base, target, current, {"address"}, set()))
        self.assertEqual(rows["address"], "conflict")

    def test_same_value_from_someone_else_is_not_claimed(self) -> None:
        # Current equals our target, but the portal history proves we never
        # wrote it. This is the "human typed the same value" case.
        base = fields()
        target = fields(address="目标地址")
        rows = by_name(attributed_diff(base, target, target, set(), set()))
        self.assertEqual(rows["address"], "same_as_target")
        labels = {r["field"]: r["status_label"] for r in attributed_diff(base, target, target, set(), set())}
        self.assertIn("非本次写入", labels["address"])

    def test_later_human_write_wins_over_our_earlier_write(self) -> None:
        base = fields()
        target = fields(note="本次备注")
        current = fields(note="人工改的备注")
        rows = by_name(attributed_diff(base, target, current, {"note"}, {"note"}, set()))
        self.assertEqual(rows["note"], "others")

    def test_later_human_write_with_identical_value(self) -> None:
        base = fields()
        target = fields(note="同样的值")
        rows = attributed_diff(base, target, target, {"note"}, {"note"}, set())
        status = by_name(rows)["note"]
        self.assertEqual(status, "other_last")
        label = next(r["status_label"] for r in rows if r["field"] == "note")
        self.assertIn("他人后写入", label)

    def test_our_later_write_wins(self) -> None:
        base = fields()
        target = fields(note="本次备注")
        current = fields(note="本次备注")
        rows = by_name(attributed_diff(base, target, current, {"note"}, {"note"}, {"note"}))
        self.assertEqual(rows["note"], "applied")

    def test_unchanged_field_changed_by_other(self) -> None:
        base = fields()
        target = fields()  # we did not want to change anything
        current = fields(phone="13900000000")
        rows = by_name(attributed_diff(base, target, current, set(), {"phone"}))
        self.assertEqual(rows["phone"], "others")


if __name__ == "__main__":
    unittest.main()
