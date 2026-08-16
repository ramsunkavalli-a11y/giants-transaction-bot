import unittest
from datetime import datetime
from unittest.mock import patch

import post_builder


class PostOrderingTests(unittest.TestCase):
    def _tx(self, txid, code, description):
        return {"id": txid, "date": "2026-08-14", "typeCode": code, "description": description}

    def test_grouped_move_before_special_stays_before_special(self):
        activation = self._tx(10, "SC", "San Francisco Giants activated C Test Player from the 10-day injured list.")
        option = self._tx(11, "OPT", "San Francisco Giants optioned C Other Player to Sacramento River Cats.")
        with patch.object(post_builder, "build_special_transaction_post", return_value="SPECIAL OPTION"):
            posts = post_builder.build_posts([option, activation], season_mode=True, now_la=datetime(2026, 8, 14))
        self.assertEqual(len(posts), 2)
        self.assertIn("activated C Test Player", posts[0])
        self.assertEqual(posts[1], "SPECIAL OPTION")

    def test_special_before_grouped_move_stays_before_grouped(self):
        option = self._tx(10, "OPT", "San Francisco Giants optioned C Other Player to Sacramento River Cats.")
        activation = self._tx(11, "SC", "San Francisco Giants activated C Test Player from the 10-day injured list.")
        with patch.object(post_builder, "build_special_transaction_post", return_value="SPECIAL OPTION"):
            posts = post_builder.build_posts([activation, option], season_mode=True, now_la=datetime(2026, 8, 14))
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], "SPECIAL OPTION")
        self.assertIn("activated C Test Player", posts[1])

    def test_adjacent_plain_moves_still_pack_together(self):
        first = self._tx(10, "SC", "San Francisco Giants activated C First Player.")
        second = self._tx(11, "SC", "San Francisco Giants activated C Second Player.")
        posts = post_builder.build_posts([second, first], season_mode=True, now_la=datetime(2026, 8, 14))
        self.assertEqual(len(posts), 1)
        self.assertLess(posts[0].index("First Player"), posts[0].index("Second Player"))

if __name__ == "__main__":
    unittest.main()
