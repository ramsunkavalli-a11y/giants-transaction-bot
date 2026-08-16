import unittest
from datetime import date, datetime
from unittest.mock import patch

import roster_daily


class RosterDailyTests(unittest.TestCase):
    def test_due_only_during_11pm_la_hour(self):
        self.assertTrue(roster_daily.is_due_time(datetime(2026, 8, 16, 23, 15)))
        self.assertFalse(roster_daily.is_due_time(datetime(2026, 8, 16, 22, 15)))
        self.assertFalse(roster_daily.is_due_time(datetime(2026, 8, 17, 0, 15)))

    def test_summary_counts_current_streak_and_cumulative_days(self):
        counts = {
            date(2026, 8, 10): 39,
            date(2026, 8, 11): 40,
            date(2026, 8, 12): 39,
            date(2026, 8, 13): 38,
            date(2026, 8, 14): 39,
        }
        summary = roster_daily.summarize_counts(counts, date(2026, 8, 14))
        self.assertEqual(summary["count"], 39)
        self.assertEqual(summary["streak"], 3)
        self.assertEqual(summary["cumulative"], 4)

    def test_full_roster_builds_no_post(self):
        summary = {"count": 40, "open": False, "streak": 0, "cumulative": 8}
        self.assertIsNone(roster_daily.build_post_text(summary, 2026))

    def test_open_roster_post_format(self):
        summary = {"count": 39, "open": True, "streak": 6, "cumulative": 18}
        self.assertEqual(
            roster_daily.build_post_text(summary, 2026),
            "40-man roster: 39/40\nOpen spot streak: Day 6\n2026 total: 18 days",
        )

    def test_isolated_d60_create_and_fill_dip_is_normalized(self):
        counts = {
            date(2026, 7, 1): 40,
            date(2026, 7, 2): 39,
            date(2026, 7, 3): 40,
        }
        txs = [
            {
                "id": 1, "date": "2026-07-02", "typeCode": "SC",
                "description": "San Francisco Giants transferred RHP A to the 60-day injured list.",
                "person": {"id": 1},
            },
            {
                "id": 2, "date": "2026-07-02", "typeCode": "SE",
                "description": "San Francisco Giants selected the contract of RHP B.",
                "person": {"id": 2},
            },
        ]
        fixed = roster_daily.normalize_isolated_transaction_dips(counts, txs, 137)
        self.assertEqual(fixed[date(2026, 7, 2)], 40)

    def test_real_open_dip_is_not_normalized_without_40man_add(self):
        counts = {
            date(2026, 7, 1): 40,
            date(2026, 7, 2): 39,
            date(2026, 7, 3): 40,
        }
        txs = [
            {
                "id": 1, "date": "2026-07-02", "typeCode": "SC",
                "description": "San Francisco Giants transferred RHP A to the 60-day injured list.",
                "person": {"id": 1},
            },
        ]
        fixed = roster_daily.normalize_isolated_transaction_dips(counts, txs, 137)
        self.assertEqual(fixed[date(2026, 7, 2)], 39)

    def test_current_count_applies_verified_trade_window(self):
        window = {
            "person_id": 700280,
            "name": "Test Player",
            "start": date(2026, 8, 3),
            "end_exclusive": None,
        }
        base = {i: f"Player {i}" for i in range(1, 40)}
        with patch.object(roster_daily.roster, "fetch_40man_members", return_value=(base, True)):
            count, additions, ok = roster_daily.current_adjusted_count(
                137, date(2026, 8, 16), [window], cache={}
            )
        self.assertTrue(ok)
        self.assertEqual(count, 40)
        self.assertEqual(len(additions), 1)


if __name__ == "__main__":
    unittest.main()
