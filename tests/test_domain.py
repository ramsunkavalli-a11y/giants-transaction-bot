import unittest
from datetime import date
from unittest.mock import patch

import bot_core as infra
import mlb_domain as domain
import post_builder


class TransactionSemanticsTests(unittest.TestCase):
    def test_selected_contract_is_not_signing(self):
        tx = {
            "typeCode": "SE",
            "typeDesc": "Selected",
            "description": "San Francisco Giants selected the contract of CF Test Player.",
        }
        self.assertTrue(domain.is_contract_selected_transaction(tx))
        self.assertFalse(domain.is_signing_transaction(tx))
        self.assertEqual(domain.classify_transaction(tx, True), "contract_selected")

    def test_status_change_is_not_signing(self):
        tx = {
            "typeCode": "SC",
            "typeDesc": "Status Change",
            "description": "San Francisco Giants activated CF Test Player from the 10-day injured list.",
        }
        self.assertFalse(domain.is_signing_transaction(tx))
        self.assertIsNone(domain.classify_transaction(tx, True))

    def test_real_recall_and_dfa_codes(self):
        recall = {
            "typeCode": "CU",
            "typeDesc": "Recalled",
            "description": "San Francisco Giants recalled CF Test Player from Sacramento River Cats.",
        }
        dfa = {
            "typeCode": "DES",
            "typeDesc": "Designated",
            "description": "San Francisco Giants designated SS Test Player for assignment.",
        }
        self.assertTrue(domain.is_recalled_transaction(recall))
        self.assertEqual(domain.classify_transaction(recall, True), "recalled")
        self.assertTrue(domain.is_dfa_transaction(dfa))
        self.assertEqual(domain.classify_transaction(dfa, True), "dfa")

    def test_offseason_only_signings_are_special(self):
        signing = {
            "typeCode": "SFA",
            "typeDesc": "Signed Free Agent",
            "description": "San Francisco Giants signed free agent C Test Player.",
        }
        optioned = {
            "typeCode": "OPT",
            "typeDesc": "Optioned",
            "description": "San Francisco Giants optioned C Test Player to Sacramento River Cats.",
        }
        self.assertEqual(domain.classify_transaction(signing, False), "signing")
        self.assertIsNone(domain.classify_transaction(optioned, False))


class AggregationTests(unittest.TestCase):
    def test_combines_hitting_splits_and_recomputes_rates(self):
        splits = [
            {
                "stat": {
                    "plateAppearances": 10,
                    "atBats": 8,
                    "hits": 4,
                    "doubles": 1,
                    "triples": 0,
                    "homeRuns": 1,
                    "baseOnBalls": 1,
                    "strikeOuts": 2,
                    "hitByPitch": 1,
                    "sacFlies": 0,
                    "gamesPlayed": 3,
                }
            },
            {
                "stat": {
                    "plateAppearances": 12,
                    "atBats": 10,
                    "hits": 3,
                    "doubles": 1,
                    "triples": 1,
                    "homeRuns": 0,
                    "baseOnBalls": 2,
                    "strikeOuts": 3,
                    "hitByPitch": 0,
                    "sacFlies": 0,
                    "gamesPlayed": 4,
                }
            },
        ]
        stat = domain._combine_split_stats(splits, pitcher=False)
        self.assertEqual(stat["plateAppearances"], 22)
        self.assertEqual(stat["hits"], 7)
        self.assertEqual(stat["homeRuns"], 1)
        self.assertEqual(stat["strikeOuts"], 5)
        self.assertEqual(stat["baseOnBalls"], 3)
        self.assertEqual(stat["avg"], "0.389")
        self.assertEqual(stat["obp"], "0.500")
        self.assertEqual(stat["slg"], "0.778")

    def test_combines_pitching_splits_using_outs(self):
        splits = [
            {"stat": {
                "inningsPitched": "2.2", "hits": 2, "earnedRuns": 1,
                "strikeOuts": 4, "baseOnBalls": 1, "gamesPitched": 1,
            }},
            {"stat": {
                "inningsPitched": "1.1", "hits": 1, "earnedRuns": 0,
                "strikeOuts": 2, "baseOnBalls": 0, "gamesPitched": 1,
            }},
        ]
        stat = domain._combine_split_stats(splits, pitcher=True)
        self.assertEqual(stat["inningsPitched"], "4.0")
        self.assertEqual(stat["hits"], 3)
        self.assertEqual(stat["strikeOuts"], 6)
        self.assertEqual(stat["baseOnBalls"], 1)
        self.assertEqual(stat["era"], "2.25")


class ApiShapeTests(unittest.TestCase):
    def test_fetch_player_stat_uses_singular_sport_id_and_combines_teams(self):
        payload = {
            "stats": [{
                "group": {"displayName": "hitting"},
                "splits": [
                    {
                        "season": "2026",
                        "sport": {"id": 11},
                        "team": {"id": 1, "name": "AAA Team One"},
                        "stat": {
                            "plateAppearances": 10, "atBats": 8, "hits": 4,
                            "doubles": 1, "triples": 0, "homeRuns": 1,
                            "baseOnBalls": 1, "strikeOuts": 2,
                            "hitByPitch": 1, "sacFlies": 0, "gamesPlayed": 3,
                        },
                    },
                    {
                        "season": "2026",
                        "sport": {"id": 11},
                        "team": {"id": 2, "name": "AAA Team Two"},
                        "stat": {
                            "plateAppearances": 12, "atBats": 10, "hits": 3,
                            "doubles": 1, "triples": 1, "homeRuns": 0,
                            "baseOnBalls": 2, "strikeOuts": 3,
                            "hitByPitch": 0, "sacFlies": 0, "gamesPlayed": 4,
                        },
                    },
                ],
            }]
        }

        with patch.object(infra, "request_json_with_retry", return_value=payload) as request:
            selected, ok = domain.fetch_player_stat(
                123, False, 11, "byDateRange", 2026,
                cache={}, start_date="2026-01-01", end_date="2026-08-11",
            )

        self.assertTrue(ok)
        self.assertEqual(selected["levelToken"], "AAA")
        self.assertEqual(selected["splitStats"]["plateAppearances"], 22)
        url = request.call_args.args[0]
        self.assertIn("sportId=11", url)
        self.assertNotIn("sportIds=", url)
        self.assertIn("startDate=2026-01-01", url)
        self.assertIn("endDate=2026-08-11", url)

    def test_prior_transaction_history_uses_player_endpoint_and_same_day_order(self):
        current = {
            "id": 30,
            "effectiveDate": "2026-08-14",
            "typeCode": "OPT",
            "description": "San Francisco Giants optioned CF Test Player.",
        }
        payload = {
            "transactions": [
                {
                    "id": 20,
                    "effectiveDate": "2026-08-14",
                    "typeCode": "CU",
                    "description": "San Francisco Giants recalled CF Test Player.",
                },
                {
                    "id": 40,
                    "effectiveDate": "2026-08-14",
                    "typeCode": "CU",
                    "description": "Later same-day event that must not count.",
                },
            ]
        }
        with patch.object(infra, "request_json_with_retry", return_value=payload) as request:
            found = domain.latest_prior_transaction_date(
                123, current, domain.is_recalled_transaction, cache={}
            )
        self.assertEqual(found, date(2026, 8, 14))
        url = request.call_args.args[0]
        self.assertIn("playerId=123", url)
        self.assertIn("endDate=2026-08-14", url)


class BuildIsolationTests(unittest.TestCase):
    def test_core_contains_no_legacy_domain_builder_or_classifiers(self):
        self.assertFalse(hasattr(infra, "build_posts"))
        self.assertFalse(hasattr(infra, "is_signing_transaction"))
        self.assertFalse(hasattr(infra, "is_contract_selected_transaction"))

    def test_hitter_formatter_derives_missing_rates_from_pa(self):
        text = post_builder.format_stat_clause({
            "plateAppearances": 47,
            "avg": ".081",
            "obp": ".227",
            "slg": ".162",
            "homeRuns": 1,
            "strikeOuts": 13,
            "baseOnBalls": 7,
        }, pitcher=False)
        self.assertEqual(
            text,
            "47 PA, .081/.227/.162, 1 HR, 28% K, 15% BB",
        )


if __name__ == "__main__":
    unittest.main()
