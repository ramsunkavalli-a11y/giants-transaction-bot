import unittest
from datetime import date
from unittest.mock import patch

import roster_intelligence as roster


class RosterIntelligenceTests(unittest.TestCase):
    def test_40man_members_excludes_60day_il(self):
        payload = {
            "roster": [
                {"person": {"id": 1, "fullName": "Active Player"}, "status": {"code": "A"}},
                {"person": {"id": 2, "fullName": "60 Day Player"}, "status": {"code": "D60"}},
            ]
        }
        with patch.object(roster.infra, "request_json_with_retry", return_value=payload):
            members, ok = roster.fetch_40man_members(137, date(2026, 8, 16), cache={})
        self.assertTrue(ok)
        self.assertEqual(members, {1: "Active Player"})

    def test_recent_incoming_trade_creates_reconciliation_window(self):
        trade = {
            "id": 100, "date": "2026-08-03", "typeCode": "TR",
            "description": "San Francisco Giants traded for RHP Test Player.",
            "person": {"id": 700280, "fullName": "Test Player"},
            "fromTeam": {"id": 135, "name": "San Diego Padres"},
            "toTeam": {"id": 137, "name": "San Francisco Giants"},
        }
        assignment = {
            "id": 101, "date": "2026-08-14", "typeCode": "ASG",
            "description": "San Jose Giants sent RHP Test Player on a rehab assignment.",
            "person": {"id": 700280},
        }
        with patch.object(roster, "fetch_team_transactions", return_value=([trade], True)), \
             patch.object(roster, "fetch_40man_members", return_value=({700280: "Test Player"}, True)), \
             patch.object(roster.domain, "fetch_player_transactions", return_value=([trade, assignment], True)):
            windows, ok = roster.build_trade_exception_windows(
                137, date(2026, 8, 1), date(2026, 8, 16), cache={}
            )
        self.assertTrue(ok)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["person_id"], 700280)
        self.assertIsNone(windows[0]["end_exclusive"])

    def test_dfa_ends_trade_reconciliation_window(self):
        trade = {
            "id": 200, "date": "2026-08-03", "typeCode": "TR",
            "description": "San Francisco Giants traded for RHP Test Player.",
            "person": {"id": 700280},
            "fromTeam": {"id": 135}, "toTeam": {"id": 137},
        }
        dfa = {
            "id": 210, "date": "2026-08-12", "typeCode": "DES",
            "description": "San Francisco Giants designated RHP Test Player for assignment.",
            "person": {"id": 700280},
            "fromTeam": {"id": 137},
        }
        with patch.object(roster, "fetch_team_transactions", return_value=([trade], True)), \
             patch.object(roster, "fetch_40man_members", return_value=({700280: "Test Player"}, True)), \
             patch.object(roster.domain, "fetch_player_transactions", return_value=([trade, dfa], True)):
            windows, ok = roster.build_trade_exception_windows(
                137, date(2026, 8, 1), date(2026, 8, 16), cache={}
            )
        self.assertTrue(ok)
        self.assertEqual(windows[0]["end_exclusive"], date(2026, 8, 12))

    def test_adjusted_members_add_missing_known_40man_trade(self):
        window = {
            "person_id": 700280,
            "name": "Test Player",
            "start": date(2026, 8, 3),
            "end_exclusive": None,
        }
        with patch.object(roster, "fetch_40man_members", return_value=({1: "Other Player"}, True)):
            members, additions, ok = roster.adjusted_40man_members(
                137,
                as_of_date=date(2026, 8, 16),
                cache={},
                trade_windows=[window],
            )
        self.assertTrue(ok)
        self.assertEqual(len(members), 2)
        self.assertIn(700280, members)
        self.assertEqual(additions[0]["name"], "Test Player")

    def test_window_does_not_apply_on_removal_date(self):
        window = {
            "person_id": 700280,
            "name": "Test Player",
            "start": date(2026, 8, 3),
            "end_exclusive": date(2026, 8, 12),
        }
        with patch.object(roster, "fetch_40man_members", return_value=({}, True)):
            members, additions, ok = roster.adjusted_40man_members(
                137,
                as_of_date=date(2026, 8, 12),
                cache={},
                trade_windows=[window],
            )
        self.assertTrue(ok)
        self.assertEqual(members, {})
        self.assertEqual(additions, [])


if __name__ == "__main__":
    unittest.main()
