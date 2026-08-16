import unittest
from datetime import date
from unittest.mock import patch

import mlb_domain as domain
import post_builder


class TransactionDateTests(unittest.TestCase):
    def test_signing_context_is_bounded_to_transaction_date(self):
        milb = {
            "seasonYear": 2026,
            "levelToken": "AAA",
            "splitStats": {"plateAppearances": 200},
        }

        with (
            patch.object(domain, "fetch_player_stat", return_value=(None, True)) as mlb_fetch,
            patch.object(domain, "fetch_highest_milb_stat", return_value=(milb, True)) as milb_fetch,
        ):
            mlb, selected_milb, ok = post_builder._signing_context(
                123, False, date(2026, 8, 11), cache={}
            )

        self.assertIsNone(mlb)
        self.assertIs(selected_milb, milb)
        self.assertTrue(ok)

        mlb_fetch.assert_called_once_with(
            123, False, 1, "byDateRange", 2026,
            cache={}, start_date="2026-01-01", end_date=date(2026, 8, 11),
        )
        milb_fetch.assert_called_once_with(
            123, False, "byDateRange", 2026,
            cache={}, start_date="2026-01-01", end_date=date(2026, 8, 11),
        )


if __name__ == "__main__":
    unittest.main()
