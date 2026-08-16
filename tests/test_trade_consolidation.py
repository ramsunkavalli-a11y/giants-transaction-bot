import unittest

import bot_core as infra


class TradeConsolidationTests(unittest.TestCase):
    def _leg(self, person_id, name, from_id, from_name, to_id, to_name):
        return {
            "id": 100,
            "date": "2026-08-03",
            "typeCode": "TR",
            "description": "San Francisco Giants traded two players for two prospects.",
            "person": {"id": person_id, "fullName": name},
            "fromTeam": {"id": from_id, "name": from_name},
            "toTeam": {"id": to_id, "name": to_name},
        }

    def test_top_level_trade_leg_is_recognized_as_incoming(self):
        tx = self._leg(700248, "Marty Gair", 143, "Philadelphia Phillies", 137, "San Francisco Giants")
        self.assertEqual(
            infra.extract_trade_incoming_players(tx),
            [{"id": 700248, "name": "Marty Gair"}],
        )

    def test_same_trade_id_collapses_to_one_line_and_keeps_all_incoming_links(self):
        records = [
            self._leg(700248, "Marty Gair", 143, "Philadelphia Phillies", 137, "San Francisco Giants"),
            self._leg(827408, "Ramon Marquez", 143, "Philadelphia Phillies", 137, "San Francisco Giants"),
            self._leg(668873, "Caleb Kilian", 137, "San Francisco Giants", 143, "Philadelphia Phillies"),
            self._leg(650333, "Luis Arraez", 137, "San Francisco Giants", 143, "Philadelphia Phillies"),
        ]
        blocks = infra.build_date_group_blocks(records)
        joined = "\n".join(blocks)
        self.assertEqual(joined.count("- Trade:"), 1)
        self.assertEqual(joined.count("San Francisco Giants traded two players for two prospects."), 1)
        self.assertIn("Marty Gair https://www.mlb.com/player/700248", joined)
        self.assertIn("Ramon Marquez https://www.mlb.com/player/827408", joined)
        self.assertNotIn("Caleb Kilian https://www.mlb.com/player/668873", joined)
        self.assertNotIn("Luis Arraez https://www.mlb.com/player/650333", joined)

    def test_separate_trade_ids_on_same_date_stay_separate(self):
        first = self._leg(1, "Incoming One", 143, "Phillies", 137, "Giants")
        second = self._leg(2, "Incoming Two", 147, "Yankees", 137, "Giants")
        second["id"] = 101
        second["description"] = "San Francisco Giants made a second trade."
        joined = "\n".join(infra.build_date_group_blocks([first, second]))
        self.assertEqual(joined.count("- Trade:"), 2)
        self.assertIn("Incoming One", joined)
        self.assertIn("Incoming Two", joined)


if __name__ == "__main__":
    unittest.main()
