from __future__ import annotations

import unittest

from fonbet_mcp.fonbet import extract_match_markets, extract_unlabeled_pairs, _factor_type_catalog


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

    def test_sports_name_never_overrides_bet_factor_name(self):
        payload = dict(self.payload)
        payload["sports"] = [
            *self.payload["sports"],
            {"id": 921, "name": "Unrelated sports league"},
            {"id": 90001, "name": "Unrelated other sport"},
        ]
        result = extract_match_markets(payload, event_id=10)
        by_id = {m["factor_id"]: m for m in result["markets"]}
        self.assertEqual(by_id[921]["name"], "П1")
        self.assertIsNone(by_id[90001]["name"])

    def test_only_explicit_factor_metadata_is_read(self):
        payload = {
            "sports": [{"id": 123, "name": "Wrong sport name"}],
            "factorTypes": [{"factorId": 123, "name": "Verified market label"}],
        }
        self.assertEqual(_factor_type_catalog(payload)[123]["name"], "Verified market label")

    def test_historical_double_chance_ids(self):
        payload = {
            "events": [{"id": 10, "team1": "Home", "team2": "Away"}],
            "customFactors": [{"e": 10, "factors": [
                {"f": 924, "v": 1.1}, {"f": 925, "v": 2.45},
                {"f": 1571, "v": 1.18},
            ]}],
        }
        markets = extract_match_markets(payload, event_id=10)["markets"]
        by_id = {m["factor_id"]: m["name"] for m in markets}
        self.assertEqual(by_id, {924: "1X", 925: "X2", 1571: "12"})

    def test_verified_alternative_totals_across_markets(self):
        pairs = ((1696, 1697), (1727, 1728), (1730, 1731),
                 (1733, 1734), (1736, 1737))
        factors = [
            {"f": factor_id, "v": 1.8, "p": 350, "pt": "3.5"}
            for pair in pairs for factor_id in pair
        ]
        payload = {
            "events": [
                {"id": 10, "team1": "Home", "team2": "Away"},
                {"id": 11, "name": "угловые", "parentId": 10},
            ],
            "customFactors": [{"e": 11, "factors": factors}],
        }
        result = extract_match_markets(payload, event_id=10, query="углов")
        self.assertEqual(len(result["markets"]), len(factors))
        by_id = {m["factor_id"]: m for m in result["markets"]}
        for over_id, under_id in pairs:
            self.assertEqual(by_id[over_id]["name"], "Тотал больше (3.5)")
            self.assertEqual(by_id[under_id]["name"], "Тотал меньше (3.5)")
            self.assertEqual(by_id[over_id]["group"], "угловые")
        # Individual totals are still unknown: avoid false specificity.
        self.assertNotIn(1809, by_id)

    def test_unlabeled_pairs_within_same_section_and_threshold(self):
        payload = {
            "events": [
                {"id": 10, "team1": "Home", "team2": "Away"},
                {"id": 11, "name": "угловые", "parentId": 10},
                {"id": 12, "name": "желтые карты", "parentId": 10},
            ],
            "customFactors": [
                {"e": 10, "factors": [
                    {"f": 1809, "v": 1.2, "pt": "0.5"},
                    {"f": 1810, "v": 4.5, "pt": "0.5"},
                    {"f": 930, "v": 1.6, "pt": "2.5"},
                ]},
                {"e": 11, "factors": [
                    {"f": 1809, "v": 1.5, "pt": "4.5"},
                    {"f": 1810, "v": 2.5, "pt": "4.5"},
                    {"f": 1812, "v": 1.6, "pt": "5.5"},
                    {"f": 1813, "v": 2.3, "pt": "5.5"},
                ]},
                {"e": 12, "factors": [
                    {"f": 1809, "v": 1.2, "pt": "1.5"},
                    {"f": 1810, "v": 4.0, "pt": "1.5"},
                ]},
            ],
        }
        found = extract_unlabeled_pairs(payload, event_id=10, section="углов")
        self.assertEqual(found["pair_count"], 2)
        self.assertEqual(
            {tuple(sel["factor_id"] for sel in p["selections"]) for p in found["pairs"]},
            {(1809, 1810), (1812, 1813)},
        )
        self.assertTrue(all(p["verified_team"] is False for p in found["pairs"]))
        self.assertTrue(all(p["verified_direction"] is False for p in found["pairs"]))
        self.assertTrue(all(p["subevent_name"] == "угловые" for p in found["pairs"]))

    def test_unlabeled_pair_pagination_and_skip_mismatched_params(self):
        payload = {
            "events": [{"id": 10, "team1": "A", "team2": "B"}],
            "customFactors": [{"e": 10, "factors": [
                {"f": 1809, "v": 1.2, "pt": "0.5"},
                {"f": 1810, "v": 2.4, "pt": "0.5"},
                {"f": 1812, "v": 1.5, "pt": "1.5"},
                {"f": 1813, "v": 2.0, "pt": "1.5"},
                {"f": 1900, "v": 1.6, "pt": "2.5"},
                {"f": 1901, "v": 2.1, "pt": "3.5"},
            ]}],
        }
        first = extract_unlabeled_pairs(payload, event_id=10, limit=1)
        self.assertEqual(first["pair_count"], 2)
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual(len(first["pairs"]), 1)
        second = extract_unlabeled_pairs(payload, event_id=10, offset=1, limit=1)
        self.assertIsNone(second["next_offset"])
        self.assertEqual(second["pairs"][0]["param_text"], "1.5")

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
