import unittest
from datetime import datetime
from unittest.mock import patch

import post_builder


class TransactionPresentationTests(unittest.TestCase):
    def test_number_changes_are_suppressed(self):
        number = {
            "id": 1, "date": "2026-04-15", "typeCode": "NUM",
            "typeDesc": "Number Change",
            "description": "CF Test Player changed number to 42.",
        }
        self.assertEqual(
            post_builder.build_posts(
                [number], season_mode=True, now_la=datetime(2026, 4, 15)
            ),
            [],
        )

    def test_mixed_group_omits_number_change(self):
        number = {
            "id": 1, "date": "2026-04-15", "typeCode": "NUM",
            "description": "CF Test Player changed number to 42.",
        }
        status = {
            "id": 2, "date": "2026-04-15", "typeCode": "SC",
            "typeDesc": "Status Change",
            "description": "San Francisco Giants activated CF Test Player.",
        }
        posts = post_builder.build_posts(
            [number, status], season_mode=True, now_la=datetime(2026, 4, 15)
        )
        joined = "\n".join(posts)
        self.assertIn("activated CF Test Player", joined)
        self.assertNotIn("changed number", joined)

    def test_incoming_claim_uses_enriched_separate_post(self):
        claim = {
            "id": 3, "date": "2026-08-02", "typeCode": "CLW",
            "typeDesc": "Claimed Off Waivers",
            "description": "San Francisco Giants claimed 1B Test Player off waivers from Seattle Mariners.",
            "person": {"id": 123, "fullName": "Test Player"},
            "fromTeam": {"id": 136, "name": "Seattle Mariners"},
            "toTeam": {"id": 137, "name": "San Francisco Giants"},
        }
        enrichment = {
            "pitcher": False, "age": 27,
            "primary_stats": "218 PA, .321/.424/.543, 8 HR, 12% K, 12% BB",
            "primary_label": "2026 AAA", "secondary": "MLB: 19 PA",
            "fallback": [],
        }
        with patch.object(post_builder.domain, "fetch_player_details", return_value={"id": 123}), \
             patch.object(post_builder, "build_signing_enrichment", return_value=enrichment):
            posts = post_builder.build_posts(
                [claim], season_mode=True, now_la=datetime(2026, 8, 2)
            )
        self.assertEqual(len(posts), 1)
        self.assertIn("claimed 1B Test Player off waivers", posts[0])
        self.assertIn("2026 AAA: 218 PA, .321/.424/.543", posts[0])
        self.assertIn("MLB: 19 PA | Age 27", posts[0])

    def test_outgoing_claim_remains_plain_grouped(self):
        claim = {
            "id": 4, "date": "2026-06-23", "typeCode": "CLW",
            "typeDesc": "Claimed Off Waivers",
            "description": "New York Mets claimed CF Test Player off waivers from San Francisco Giants.",
            "person": {"id": 123, "fullName": "Test Player"},
            "fromTeam": {"id": 137, "name": "San Francisco Giants"},
            "toTeam": {"id": 121, "name": "New York Mets"},
        }
        with patch.object(post_builder.domain, "fetch_player_details") as details:
            posts = post_builder.build_posts(
                [claim], season_mode=True, now_la=datetime(2026, 6, 23)
            )
        details.assert_not_called()
        self.assertEqual(len(posts), 1)
        self.assertIn("New York Mets claimed CF Test Player off waivers", posts[0])
        self.assertNotIn("https://www.mlb.com/player/123", posts[0])


if __name__ == "__main__":
    unittest.main()
