import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import bot_core as infra


class PartialPostStateTests(unittest.TestCase):
    def _tx(self, txid, day, desc):
        return {"id": txid, "date": day, "description": desc}

    def test_post_maps_only_same_date_descriptions(self):
        same = "San Francisco Giants sent RHP Test Player on a rehab assignment to Sacramento River Cats."
        txns = [self._tx(1, "2026-04-14", same), self._tx(2, "2026-04-14", same), self._tx(3, "2026-05-01", same)]
        post = "2026-04-14\n- " + same
        self.assertEqual(infra.transaction_ids_represented_in_post(post, txns), {1, 2})

    def test_grouped_post_maps_multiple_transactions(self):
        first = self._tx(10, "2026-08-14", "San Francisco Giants activated C First Player.")
        second = self._tx(11, "2026-08-14", "San Francisco Giants activated C Second Player.")
        post = "2026-08-14\n- San Francisco Giants activated C First Player.\n- San Francisco Giants activated C Second Player."
        self.assertEqual(infra.transaction_ids_represented_in_post(post, [first, second]), {10, 11})

    def test_long_trimmed_description_uses_prefix_fallback(self):
        desc = "San Francisco Giants acquired " + ("Very Long Player Description " * 8)
        tx = self._tx(20, "2026-08-14", desc)
        post = "2026-08-14\n- " + desc[:100] + "…"
        self.assertEqual(infra.transaction_ids_represented_in_post(post, [tx]), {20})

    def test_successful_post_saves_seen_ids_immediately(self):
        tx = self._tx(30, "2026-08-14", "San Francisco Giants activated C Test Player.")
        seen = {5}
        with patch.object(bot.infra, "save_seen_ids") as save:
            covered = bot.record_successful_post("2026-08-14\n- San Francisco Giants activated C Test Player.", [tx], seen)
        self.assertEqual(covered, {30})
        self.assertEqual(seen, {5, 30})
        save.assert_called_once_with({5, 30})

    def test_production_workflow_commits_state_branch_even_after_failure(self):
        workflow = Path(".github/workflows/run.yml").read_text()
        self.assertIn("- name: Commit bot state\n        if: always()\n        working-directory: state", workflow)
        self.assertIn("ref: bot-state\n          path: state", workflow)
        self.assertIn("git push origin HEAD:bot-state", workflow)


if __name__ == "__main__":
    unittest.main()
