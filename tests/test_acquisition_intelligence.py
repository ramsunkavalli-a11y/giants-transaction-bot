import unittest
from unittest.mock import patch

import acquisition_intelligence as acquisition


class AcquisitionIntelligenceTests(unittest.TestCase):
    def test_hitter_prefers_meaningful_oaa(self):
        with patch.object(acquisition, "fetch_oaa", return_value=({"attempts": 30, "totalOutsAboveAverage": 4}, True)), \
             patch.object(acquisition, "fetch_zips_ros") as zips:
            part = acquisition.best_acquisition_part(123, False, 2026, cache={})
        self.assertEqual(part, "2026 defense: +4 OAA")
        zips.assert_not_called()

    def test_hitter_falls_back_to_zips(self):
        with patch.object(acquisition, "fetch_oaa", return_value=({"attempts": 10, "totalOutsAboveAverage": 2}, True)), \
             patch.object(acquisition, "fetch_zips_ros", return_value=({"wRcPlus": 92.4}, True)):
            part = acquisition.best_acquisition_part(123, False, 2026, cache={})
        self.assertEqual(part, "ZiPS ROS: 92 wRC+")

    def test_pitcher_prefers_usable_arsenal(self):
        arsenal = [
            {"code": "FF", "percentage": 0.56, "averageSpeed": 96.6, "totalPitches": 220},
            {"code": "SL", "percentage": 0.31, "averageSpeed": 88.7, "totalPitches": 220},
            {"code": "CH", "percentage": 0.13, "averageSpeed": 86.8, "totalPitches": 220},
        ]
        with patch.object(acquisition, "fetch_pitch_arsenal", return_value=(arsenal, True)), \
             patch.object(acquisition, "fetch_zips_ros") as zips:
            part = acquisition.best_acquisition_part(123, True, 2026, cache={})
        self.assertEqual(part, "Arsenal: FF 97 (56%) | SL 89 (31%)")
        zips.assert_not_called()

    def test_pitcher_falls_back_to_zips_when_arsenal_small(self):
        arsenal = [
            {"code": "FF", "percentage": 1.0, "averageSpeed": 95.1, "totalPitches": 25},
        ]
        with patch.object(acquisition, "fetch_pitch_arsenal", return_value=(arsenal, True)), \
             patch.object(acquisition, "fetch_zips_ros", return_value=({"era": "3.81", "fip": 3.96}, True)):
            part = acquisition.best_acquisition_part(123, True, 2026, cache={})
        self.assertEqual(part, "ZiPS ROS: 3.81 ERA / 3.96 FIP")


if __name__ == "__main__":
    unittest.main()
