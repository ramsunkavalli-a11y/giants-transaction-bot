import unittest
from unittest.mock import patch

import pitching_domain as pitching
import post_builder


class OptionPitcherFormattingTests(unittest.TestCase):
    def test_meaningful_reliever_stint_uses_fip_xfip_and_rates(self):
        selected = {
            "splitStats": {
                "inningsPitched": "11.0",
                "outs": 33,
                "era": "9.00",
                "battersFaced": 52,
                "strikeOuts": 5,
                "baseOnBalls": 5,
            }
        }
        post = post_builder.build_option_pitcher_post(
            "2026-07-09\n- San Francisco Giants optioned RHP Ryan Walker to Sacramento River Cats.",
            "https://www.mlb.com/player/676254",
            "MLB since Jun 12",
            selected,
            usage={"games": 11, "starts": 0},
            saber={"fip": 5.43751, "xfip": 5.13357},
        )
        self.assertIn("MLB since Jun 12: 11 G, 11.0 IP, 9.00 ERA", post)
        self.assertIn("5.44 FIP / 5.13 xFIP | 10 K% | 10 BB%", post)
        self.assertLessEqual(len(post), 300)

    def test_small_starter_stint_suppresses_advanced_line(self):
        selected = {
            "splitStats": {
                "inningsPitched": "5.2",
                "outs": 17,
                "era": "3.18",
                "battersFaced": 23,
                "strikeOuts": 4,
                "baseOnBalls": 4,
            }
        }
        post = post_builder.build_option_pitcher_post(
            "2026-07-10\n- Optioned Test Pitcher",
            "https://www.mlb.com/player/1",
            "MLB since Jul 9",
            selected,
            usage={"games": 1, "starts": 1},
            saber={"fip": 5.40, "xfip": 5.61},
        )
        self.assertIn("MLB since Jul 9: 1 G (1 GS), 5.2 IP, 3.18 ERA", post)
        self.assertNotIn("FIP", post)
        self.assertNotIn("K%", post)
        self.assertNotIn("BB%", post)

    def test_exact_ten_innings_enables_advanced_line(self):
        selected = {
            "splitStats": {
                "inningsPitched": "10.0",
                "outs": 30,
                "era": "4.50",
                "battersFaced": 40,
                "strikeOuts": 10,
                "baseOnBalls": 4,
            }
        }
        post = post_builder.build_option_pitcher_post(
            "2026-08-01\n- Optioned Test Pitcher",
            "https://www.mlb.com/player/1",
            "MLB since Jul 15",
            selected,
            usage={"games": 4, "starts": 2},
            saber={"fip": 4.10, "xfip": 4.40},
        )
        self.assertIn("4.10 FIP / 4.40 xFIP | 25 K% | 10 BB%", post)


class PitcherSabermetricDomainTests(unittest.TestCase):
    def test_selects_requested_team_without_counting_stat_volume(self):
        payload = {
            "stats": [{
                "splits": [
                    {
                        "sport": {"id": 1},
                        "team": {"id": 999, "name": "Other"},
                        "stat": {"fip": 2.00, "xfip": 2.20},
                    },
                    {
                        "sport": {"id": 1},
                        "team": {"id": 137, "name": "San Francisco Giants"},
                        "stat": {"fip": 5.43751, "xfip": 5.13357},
                    },
                ]
            }]
        }
        with patch.object(pitching.infra, "request_json_with_retry", return_value=payload):
            stat, ok = pitching.fetch_pitcher_sabermetrics(
                676254, 2026, "2026-06-12", "2026-07-09",
                cache={}, team_id=137,
            )
        self.assertTrue(ok)
        self.assertEqual(stat["fip"], 5.43751)
        self.assertEqual(stat["xfip"], 5.13357)

    def test_ambiguous_multi_team_rates_are_not_combined(self):
        payload = {
            "stats": [{
                "splits": [
                    {"sport": {"id": 1}, "team": {"id": 1}, "stat": {"fip": 3.0}},
                    {"sport": {"id": 1}, "team": {"id": 2}, "stat": {"fip": 5.0}},
                ]
            }]
        }
        with patch.object(pitching.infra, "request_json_with_retry", return_value=payload):
            stat, ok = pitching.fetch_pitcher_sabermetrics(
                1, 2026, "2026-01-01", "2026-08-01", cache={}
            )
        self.assertTrue(ok)
        self.assertIsNone(stat)


if __name__ == "__main__":
    unittest.main()
