import unittest
from datetime import datetime

import mlb_domain as domain
import post_builder


class AssignmentFilteringTests(unittest.TestCase):
    def test_generic_org_assignment_is_noise_but_rehab_is_not(self):
        generic = {
            "id": 1, "date": "2026-03-02", "typeCode": "ASG",
            "description": "CF Jonah Cox assigned to San Francisco Giants.",
        }
        rehab = {
            "id": 2, "date": "2026-07-29", "typeCode": "ASG",
            "description": "San Francisco Giants sent CF Jonah Cox on a rehab assignment to Sacramento River Cats.",
        }
        self.assertTrue(domain.is_generic_org_assignment_transaction(generic))
        self.assertFalse(domain.is_rehab_assignment_transaction(generic))
        self.assertTrue(domain.is_rehab_assignment_transaction(rehab))
        self.assertFalse(domain.is_generic_org_assignment_transaction(rehab))

    def test_generic_org_assignment_is_suppressed(self):
        generic = {
            "id": 1, "date": "2026-03-02", "typeCode": "ASG",
            "description": "CF Jonah Cox assigned to San Francisco Giants.",
        }
        posts = post_builder.build_posts(
            [generic], season_mode=True, now_la=datetime(2026, 3, 2)
        )
        self.assertEqual(posts, [])

    def test_exact_same_day_rehab_duplicates_are_deduped(self):
        description = "San Francisco Giants sent LHP Sam Hentges on a rehab assignment to Sacramento River Cats."
        first = {"id": 1, "date": "2026-04-14", "typeCode": "ASG", "description": description}
        second = {"id": 2, "date": "2026-04-14", "typeCode": "ASG", "description": description}
        posts = post_builder.build_posts(
            [first, second], season_mode=True, now_la=datetime(2026, 4, 14)
        )
        joined = "\n".join(posts)
        self.assertEqual(joined.count(description), 1)

    def test_distinct_rehab_assignments_are_preserved(self):
        san_jose = {
            "id": 1, "date": "2026-05-10", "typeCode": "ASG",
            "description": "San Francisco Giants sent RHP Test Player on a rehab assignment to San Jose Giants.",
        }
        sacramento = {
            "id": 2, "date": "2026-05-12", "typeCode": "ASG",
            "description": "San Francisco Giants sent RHP Test Player on a rehab assignment to Sacramento River Cats.",
        }
        posts = post_builder.build_posts(
            [san_jose, sacramento], season_mode=True, now_la=datetime(2026, 5, 12)
        )
        joined = "\n".join(posts)
        self.assertIn("San Jose Giants", joined)
        self.assertIn("Sacramento River Cats", joined)


if __name__ == "__main__":
    unittest.main()
