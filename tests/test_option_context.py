import unittest
from unittest.mock import Mock, patch

import post_builder
import statcast_domain as statcast


class OptionHitterFormattingTests(unittest.TestCase):
    def setUp(self):
        self.selected = {
            "splitStats": {
                "plateAppearances": 47,
                "avg": ".081",
                "obp": ".227",
                "slg": ".162",
                "strikeOuts": 13,
                "baseOnBalls": 7,
            }
        }

    def test_full_option_format_uses_usage_ops_and_unlabeled_process_line(self):
        post = post_builder.build_option_hitter_post(
            "2026-08-14\n- San Francisco Giants optioned CF Grant McCray to Sacramento River Cats.",
            "https://www.mlb.com/player/687529",
            "MLB since Jul 10",
            self.selected,
            usage={"games": 23, "starts": 13},
            woba=0.197954,
            xwoba=0.295618,
        )
        self.assertIn(
            "MLB since Jul 10: 23 G (13 GS), 47 PA, .081/.227/.162 (.389 OPS)",
            post,
        )
        self.assertIn(".198 wOBA / .296 xwOBA | 28 K% | 15 BB%", post)
        self.assertNotIn("28% K%", post)
        self.assertLessEqual(len(post), 300)

    def test_small_sample_omits_second_line(self):
        selected = {
            "splitStats": {
                "plateAppearances": 8,
                "avg": ".000",
                "obp": ".250",
                "slg": ".000",
                "strikeOuts": 1,
                "baseOnBalls": 2,
            }
        }
        post = post_builder.build_option_hitter_post(
            "2026-08-14\n- Optioned Test Player",
            "https://www.mlb.com/player/1",
            "MLB since Aug 1",
            selected,
            usage={"games": 6, "starts": 1},
            woba=0.250,
            xwoba=0.500,
        )
        self.assertIn(
            "MLB since Aug 1: 6 G (1 GS), 8 PA, .000/.250/.000 (.250 OPS)",
            post,
        )
        self.assertNotIn("wOBA", post)
        self.assertNotIn("xwOBA", post)
        self.assertNotIn("K%", post)
        self.assertNotIn("BB%", post)

    def test_long_description_drops_usage_before_process_context(self):
        base = "2026-08-14\n- " + ("Very long transaction description " * 4).strip()
        post = post_builder.build_option_hitter_post(
            base,
            "https://www.mlb.com/player/687529",
            "MLB since Jul 10",
            self.selected,
            usage={"games": 23, "starts": 13},
            woba=0.197954,
            xwoba=0.295618,
            max_len=300,
        )
        self.assertLessEqual(len(post), 300)
        # If everything cannot fit, expected/process context outranks G/GS.
        if "23 G (13 GS)" not in post:
            self.assertIn(".198 wOBA / .296 xwOBA | 28 K% | 15 BB%", post)


class StatcastContextTests(unittest.TestCase):
    def test_date_bounded_xwoba_reconstructs_terminal_pa_values(self):
        csv_text = (
            'pitch_type,game_date,batter,pitcher,woba_value,woba_denom,estimated_woba_using_speedangle\n'
            'FF,2026-08-01,123,9,0,1,0.2\n'
            'SL,2026-08-02,123,8,0.7,1,0.5\n'
            'CH,2026-08-03,999,7,2,1,1.8\n'
        )
        response = Mock()
        response.text = csv_text
        response.raise_for_status.return_value = None
        with patch.object(statcast.requests, "get", return_value=response) as request:
            expected, actual, denom, ok = statcast.fetch_date_bounded_xwoba(
                123, False, 2026, "2026-08-01", "2026-08-03", cache={}
            )
        self.assertTrue(ok)
        self.assertEqual(denom, 2)
        self.assertAlmostEqual(expected, 0.35)
        self.assertAlmostEqual(actual, 0.35)
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["batters_lookup[]"], "123")
        self.assertNotIn("batter_lookup[]", params)
        self.assertEqual(request.call_args.kwargs["timeout"], statcast.OPTIONAL_TIMEOUT)

    def test_usage_counts_unique_games_and_hitter_starts(self):
        hitting = {
            "stats": [{"splits": [
                {"game": {"gamePk": 1}, "stat": {"plateAppearances": 4}},
                {"game": {"gamePk": 2}, "stat": {"plateAppearances": 0}},
            ]}]
        }
        fielding = {
            "stats": [{"splits": [
                {"game": {"gamePk": 1}, "stat": {"gamesStarted": 1}},
                {"game": {"gamePk": 2}, "stat": {"gamesStarted": 0}},
            ]}]
        }

        def fake_request(url):
            if "group=fielding" in url:
                return fielding
            return hitting

        with patch.object(statcast.infra, "request_json_with_retry", side_effect=fake_request):
            usage, ok = statcast.fetch_mlb_usage(
                123, False, 2026, "2026-08-01", "2026-08-10", cache={}
            )
        self.assertTrue(ok)
        self.assertEqual(usage, {"games": 2, "starts": 1})


if __name__ == "__main__":
    unittest.main()
