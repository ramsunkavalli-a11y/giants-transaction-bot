import unittest
from datetime import datetime
from unittest.mock import patch

import transaction_context as context


class TransactionContextTests(unittest.TestCase):
    def test_second_option_is_called_out(self):
        prior = {
            "id": 10, "date": "2026-05-01", "typeCode": "OPT",
            "description": "San Francisco Giants optioned CF Test Player to Sacramento River Cats.",
            "person": {"id": 123},
        }
        current = {
            "id": 20, "date": "2026-08-14", "typeCode": "OPT",
            "description": "San Francisco Giants optioned CF Test Player to Sacramento River Cats.",
            "person": {"id": 123},
        }
        with patch.object(context.domain, "fetch_player_transactions", return_value=([prior, current], True)):
            parts = context.context_parts_for_transaction(current, cache={})
        self.assertEqual(parts, ["2nd option in 2026"])

    def test_first_callup_and_40man_context(self):
        current = {
            "id": 30, "date": "2026-08-10", "typeCode": "SE",
            "typeDesc": "Selected",
            "description": "San Francisco Giants selected the contract of C Test Player from Sacramento River Cats.",
            "person": {"id": 123},
        }
        with patch.object(context.domain, "fetch_player_transactions", return_value=([current], True)), \
             patch.object(context.domain, "fetch_player_details", return_value={"id": 123, "mlbDebutDate": "2026-08-10"}), \
             patch.object(context.roster, "adjusted_40man_count", return_value=(39, [], True)):
            parts = context.context_parts_for_transaction(current, cache={})
        self.assertEqual(
            parts,
            ["First MLB call-up", "Uses a 40-man spot", "40-man: 39/40"],
        )

    def test_prior_mlb_debut_suppresses_first_callup(self):
        current = {
            "id": 31, "date": "2026-08-10", "typeCode": "SE",
            "description": "San Francisco Giants selected the contract of C Test Player.",
            "person": {"id": 123},
        }
        with patch.object(context.domain, "fetch_player_transactions", return_value=([current], True)), \
             patch.object(context.domain, "fetch_player_details", return_value={"id": 123, "mlbDebutDate": "2024-06-01"}), \
             patch.object(context.roster, "adjusted_40man_count", return_value=(40, [], True)):
            parts = context.context_parts_for_transaction(current, cache={})
        self.assertNotIn("First MLB call-up", parts)

    def test_second_dfa_is_called_out(self):
        prior = {
            "id": 40, "date": "2026-04-01", "typeCode": "DES",
            "description": "Test Club designated RHP Test Player for assignment.",
            "person": {"id": 123},
        }
        current = {
            "id": 50, "date": "2026-08-11", "typeCode": "DES",
            "description": "San Francisco Giants designated RHP Test Player for assignment.",
            "person": {"id": 123},
        }
        with patch.object(context.domain, "fetch_player_transactions", return_value=([prior, current], True)), \
             patch.object(context.roster, "adjusted_40man_count", return_value=(39, [], True)):
            parts = context.context_parts_for_transaction(current, cache={})
        self.assertEqual(
            parts,
            ["2nd DFA in 2026", "Opens a 40-man spot", "40-man: 39/40"],
        )

    def test_60day_transfer_uses_original_il_start(self):
        placed = {
            "id": 60, "date": "2026-06-11", "typeCode": "SC",
            "description": "San Francisco Giants placed RHP Test Player on the 15-day injured list.",
            "person": {"id": 123},
        }
        current = {
            "id": 70, "date": "2026-07-01", "typeCode": "SC",
            "description": "San Francisco Giants transferred RHP Test Player to the 60-day injured list.",
            "person": {"id": 123},
        }
        with patch.object(context.domain, "fetch_player_transactions", return_value=([placed, current], True)), \
             patch.object(context.roster, "adjusted_40man_count", return_value=(39, [], True)):
            parts = context.context_parts_for_transaction(current, cache={})
        self.assertEqual(
            parts,
            ["Out since Jun 11", "Opens a 40-man spot", "40-man: 39/40"],
        )

    def test_affiliate_60day_transfer_gets_no_40man_context(self):
        placed = {
            "id": 71, "date": "2026-06-11", "typeCode": "SC",
            "description": "Sacramento River Cats placed RHP Test Player on the 7-day injured list.",
            "person": {"id": 123},
        }
        current = {
            "id": 72, "date": "2026-07-01", "typeCode": "SC",
            "description": "Sacramento River Cats transferred RHP Test Player to the 60-day injured list.",
            "person": {"id": 123},
        }
        with patch.object(context.domain, "fetch_player_transactions", return_value=([placed, current], True)):
            parts = context.context_parts_for_transaction(current, cache={})
        self.assertEqual(parts, [])

    def test_reinstatement_reports_il_duration(self):
        placed = {
            "id": 80, "date": "2026-06-01", "typeCode": "SC",
            "description": "San Francisco Giants placed RHP Test Player on the 15-day injured list.",
            "person": {"id": 123},
        }
        transfer = {
            "id": 81, "date": "2026-06-20", "typeCode": "SC",
            "description": "San Francisco Giants transferred RHP Test Player to the 60-day injured list.",
            "person": {"id": 123},
        }
        current = {
            "id": 90, "date": "2026-08-01", "typeCode": "RE",
            "description": "San Francisco Giants reinstated RHP Test Player from the 60-day injured list.",
            "person": {"id": 123},
        }
        with patch.object(context.domain, "fetch_player_transactions", return_value=([placed, transfer, current], True)), \
             patch.object(context.roster, "adjusted_40man_count", return_value=(40, [], True)):
            parts = context.context_parts_for_transaction(current, cache={})
        self.assertEqual(
            parts,
            ["Returns after 61 days on IL", "Uses a 40-man spot", "40-man: 40/40"],
        )

    def test_claim_reports_dfa_acquisition_and_40man_context(self):
        dfa = {
            "id": 100, "date": "2026-08-01", "typeCode": "DES",
            "description": "Seattle Mariners designated 1B Test Player for assignment.",
            "person": {"id": 123},
        }
        claim = {
            "id": 110, "date": "2026-08-04", "typeCode": "CLW",
            "description": "San Francisco Giants claimed 1B Test Player off waivers from Seattle Mariners.",
            "person": {"id": 123},
            "fromTeam": {"id": 136}, "toTeam": {"id": 137},
        }
        with patch.object(context.domain, "fetch_player_transactions", return_value=([dfa, claim], True)), \
             patch.object(context.domain, "fetch_player_details", return_value={"id": 123, "primaryPosition": {"abbreviation": "1B"}}), \
             patch.object(context.acquisition, "best_acquisition_part", return_value="ZiPS ROS: 92 wRC+"), \
             patch.object(context.roster, "adjusted_40man_count", return_value=(40, [], True)):
            parts = context.context_parts_for_transaction(claim, cache={})
        self.assertEqual(
            parts,
            [
                "Claimed 3 days after DFA",
                "ZiPS ROS: 92 wRC+",
                "Uses a 40-man spot",
                "40-man: 40/40",
            ],
        )

    def test_first_career_outright(self):
        current = {
            "id": 120, "date": "2026-08-14", "typeCode": "OUT",
            "description": "San Francisco Giants outrighted SS Test Player to Sacramento River Cats.",
            "person": {"id": 123},
        }
        with patch.object(context.domain, "fetch_player_transactions", return_value=([current], True)):
            parts = context.context_parts_for_transaction(current, cache={})
        self.assertEqual(parts, ["First career outright"])

    def test_enrichment_inserts_context_before_player_link(self):
        tx = {
            "id": 130, "date": "2026-08-14", "typeCode": "OPT",
            "description": "San Francisco Giants optioned CF Test Player to Sacramento River Cats.",
            "person": {"id": 123},
        }
        post = (
            "2026-08-14\n"
            "- San Francisco Giants optioned CF Test Player to Sacramento River Cats.\n"
            "MLB since Aug 1: 5 G, 20 PA\n"
            "https://www.mlb.com/player/123"
        )
        with patch.object(context, "context_parts_for_transaction", return_value=["2nd option in 2026"]):
            enriched = context.enrich_posts(
                [post], [tx], cache={}, now_la=datetime(2026, 8, 14)
            )
        self.assertEqual(len(enriched), 1)
        self.assertIn("MLB since Aug 1: 5 G, 20 PA\n2nd option in 2026\nhttps://", enriched[0])
        self.assertLessEqual(len(enriched[0]), 300)


if __name__ == "__main__":
    unittest.main()
