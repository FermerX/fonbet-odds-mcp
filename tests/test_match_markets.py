from __future__ import annotations

import unittest

from fonbet_mcp.fonbet import extract_match_markets


class MatchMarketTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "packetVersion": 123,
            "events": [
                {"id": 10, "team1": "Home", "team2": "Away", "sportId": 1},
                {"id": 11, "name": "Угловые", "parentId": 10, "sportId": 1},
                {"id": 12, "name": "Жёлтые карточки", "parentId": 10, "sportId": 1},
                {"id": 13, "name": "Угловые 1-й тайм", "parentId": 11, "sportId": 1},
                {"id": 99, "name": "Unrelated", "sportId": 1},
            ],
            "sports": [{"id": 1, "name": "Футбол", "kind": "sport"}],
            "customFactors": [
                {"e": 10, "factors": [{"f": 921, "v": 1.52}]},
                {"e": 11, "factors": [{"f": 90001, "v": 1.45, "p": 550, "pt": "5.5"}]},
                {"e": 12, "factors": [{"f": 90002, "v": 1.6}]},
                {"e": 13, "factors": [{"f": 90003, "v": 1.8}]},
                {"e": 99, "factors": [{"f": 90004, "v": 1.1}]},
            ],
        }

    def test_includes_children_and_grandchildren_but_not_unrelated_events(self):
        result = extract_match_markets(self.payload, event_id=10)
        self.assertEqual(result["total_market_count"], 4)
        self.assertEqual({m["event_id"] for m in result["markets"]}, {10, 11, 12, 13})
        self.assertEqual({s["event_id"] for s in result["subevents"]}, {11, 12, 13})

    def test_search_child_section_and_preserve_unknown_factor_id(self):
        result = extract_match_markets(self.payload, event_id=10, query="углов")
        self.assertEqual(result["market_count"], 2)
        self.assertEqual({m["factor_id"] for m in result["markets"]}, {90001, 90003})
        self.assertTrue(all(m["name"] is None for m in result["markets"]))
        self.assertEqual(result["markets"][0]["group"], "Угловые")

    def test_main_event_details_with_child_events_only_in_line(self):
        detail = {
            "events": [self.payload["events"][0]],
            "sports": self.payload["sports"],
            "customFactors": [self.payload["customFactors"][0]],
        }
        line = {
            "events": self.payload["events"],
            "sports": self.payload["sports"],
            "customFactors": self.payload["customFactors"],
        }
        result = extract_match_markets(detail, event_id=10, query="углов", line_payload=line)
        self.assertEqual(result["market_count"], 2)
        self.assertEqual(result["source_counts"]["event_view_events"], 1)
        self.assertEqual(result["source_counts"]["linked_events"], 4)
        self.assertEqual({m["event_id"] for m in result["markets"]}, {11, 13})

    def test_pagination(self):
        a = extract_match_markets(self.payload, event_id=10, offset=0, limit=2)
        b = extract_match_markets(self.payload, event_id=10, offset=2, limit=2)
        self.assertEqual(a["next_offset"], 2)
        self.assertIsNone(b["next_offset"])
        self.assertEqual(len(a["markets"]) + len(b["markets"]), 4)

    def test_unknown_root(self):
        with self.assertRaises(ValueError):
            extract_match_markets(self.payload, event_id=999)


if __name__ == "__main__":
    unittest.main()
