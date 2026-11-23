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

    def test_calibrated_individual_full_match_corners_both_teams(self):
        # Odds/threshold combinations from the Fonbet UI of Brentford–Everton.
        pairs = [
            (1809, 1810, "4.5", 1.37, 2.85, "Брентфорд"),
            (1812, 1813, "5.5", 1.67, 2.07, "Брентфорд"),
            (1815, 1816, "6.5", 2.17, 1.61, "Брентфорд"),
            (1854, 1871, "3.5", 1.44, 2.60, "Эвертон"),
            (1873, 1874, "4.5", 1.90, 1.80, "Эвертон"),
            (1880, 1881, "5.5", 2.75, 1.40, "Эвертон"),
        ]
        factors = []
        for over_id, under_id, threshold, over, under, _ in pairs:
            factors.extend([
                {"f": over_id, "pt": threshold, "v": over},
                {"f": under_id, "pt": threshold, "v": under},
            ])
        payload = {
            "sports": [{"id": 1, "kind": "sport", "name": "Футбол"}],
            "events": [
                {"id": 10, "sportId": 1, "team1": "Брентфорд", "team2": "Эвертон"},
                {"id": 11, "sportId": 1, "name": "угловые", "parentId": 10},
            ],
            "customFactors": [{"e": 11, "factors": factors}],
        }
        actual = extract_match_markets(payload, event_id=10, query="углов")
        by_id = {m["factor_id"]: m for m in actual["markets"]}
        self.assertEqual(len(by_id), 12)
        for over_id, under_id, threshold, over, under, team in pairs:
            self.assertEqual(by_id[over_id]["name"], f"{team} ИТБ угловых ({threshold})")
            self.assertEqual(by_id[under_id]["name"], f"{team} ИТМ угловых ({threshold})")
            self.assertEqual(by_id[over_id]["odds"], over)
            self.assertEqual(by_id[under_id]["odds"], under)
            self.assertEqual(by_id[under_id]["mapping_source"], "calibrated_fonbet_ui_bre_ev_corners")

        # Previously, the non-adjacent pair 1854 / 1871 was missed.
        self.assertIn(1854, by_id)
        self.assertIn(1871, by_id)
        candidates = extract_unlabeled_pairs(payload, event_id=10, section="углов")
        self.assertEqual(candidates["pair_count"], 0)

    def test_corner_mapping_never_leaks_into_halves_cards_or_other_sports(self):
        payload = {
            "sports": [{"id": 1, "kind": "sport", "name": "Футбол"}],
            "events": [
                {"id": 10, "sportId": 1, "team1": "Брентфорд", "team2": "Эвертон"},
                {"id": 11, "sportId": 1, "name": "1-й тайм угловые", "parentId": 10},
                {"id": 12, "sportId": 1, "name": "желтые карты", "parentId": 10},
                {"id": 13, "sportId": 1, "name": "угловые", "parentId": 11},
            ],
            "customFactors": [
                {"e": 11, "factors": [{"f": 1809, "pt": "2.5", "v": 1.5}]},
                {"e": 12, "factors": [{"f": 1809, "pt": "1.5", "v": 1.8}]},
                {"e": 13, "factors": [{"f": 1809, "pt": "1.5", "v": 1.8}]},
            ],
        }
        result = extract_match_markets(payload, event_id=10)
        self.assertTrue(all(m["name"] is None for m in result["markets"]))
        payload["sports"] = [{"id": 1, "kind": "sport", "name": "Баскетбол"}]
        payload["events"].append({"id": 14, "sportId": 1, "name": "угловые", "parentId": 10})
        payload["customFactors"].append({"e": 14, "factors": [{"f": 1809, "pt": "4.5", "v": 1.5}]})
        result = extract_match_markets(payload, event_id=10)
        self.assertTrue(all(m["name"] is None for m in result["markets"]))

    def test_calibrated_individual_full_match_goals_both_teams(self):
        # Both screenshots for Brentford-Everton, 0.5/1/1.5/2/2.5.
        pairs = [
            (1809, 1810, "0.5", 1.20, 4.00, "Брентфорд"),
            (1812, 1813, "1", 1.40, 2.70, "Брентфорд"),
            (1815, 1816, "1.5", 2.08, 1.70, "Брентфорд"),
            (1818, 1819, "2", 3.35, 1.27, "Брентфорд"),
            (1821, 1822, "2.5", 4.20, 1.17, "Брентфорд"),
            (1854, 1871, "0.5", 1.30, 3.20, "Эвертон"),
            (1873, 1874, "1", 1.62, 2.20, "Эвертон"),
            (1880, 1881, "1.5", 2.45, 1.52, "Эвертон"),
            (1883, 1884, "2", 4.30, 1.20, "Эвертон"),
            (1886, 1887, "2.5", 4.90, 1.12, "Эвертон"),
        ]
        factors = []
        for over_id, under_id, threshold, over, under, _ in pairs:
            factors.extend([
                {"f": over_id, "pt": threshold, "v": over},
                {"f": under_id, "pt": threshold, "v": under},
            ])
        payload = {
            "sports": [{"id": 1, "kind": "sport", "name": "Футбол"}],
            "events": [{"id": 10, "sportId": 1,
                        "team1": "Брентфорд", "team2": "Эвертон"}],
            "customFactors": [{"e": 10, "factors": factors}],
        }
        result = extract_match_markets(payload, event_id=10)
        by_id = {m["factor_id"]: m for m in result["markets"]}
        self.assertEqual(len(by_id), 20)
        for over_id, under_id, threshold, over, under, team in pairs:
            self.assertEqual(by_id[over_id]["name"], f"{team} ИТБ голов ({threshold})")
            self.assertEqual(by_id[under_id]["name"], f"{team} ИТМ голов ({threshold})")
            self.assertEqual(by_id[over_id]["odds"], over)
            self.assertEqual(by_id[under_id]["odds"], under)
            self.assertEqual(by_id[over_id]["mapping_source"], "calibrated_fonbet_ui_bre_ev_goals")
        # Known factors disappear from the *unlabeled* diagnostic correctly.
        self.assertEqual(extract_unlabeled_pairs(payload, event_id=10)["calibration_hypothesis_count"], 0)

    def test_verified_goals_require_full_game_sport_and_threshold(self):
        payload = {
            "sports": [{"id": 1, "kind": "sport", "name": "Футбол"}],
            "events": [
                {"id": 10, "sportId": 1, "team1": "Home", "team2": "Away"},
                {"id": 11, "sportId": 1, "name": "желтые карты", "parentId": 10},
                {"id": 12, "sportId": 1, "name": "1-й тайм", "parentId": 10},
            ],
            "customFactors": [
                {"e": 10, "factors": [{"f": 1809, "pt": "1.5", "v": 1.6}]},
                {"e": 11, "factors": [{"f": 1809, "pt": "0.5", "v": 1.5}]},
                {"e": 12, "factors": [{"f": 1809, "pt": "0.5", "v": 1.7}]},
            ],
        }
        result = extract_match_markets(payload, event_id=10)
        self.assertTrue(all(m["name"] is None for m in result["markets"]))
        payload["sports"] = [{"id": 1, "kind": "sport", "name": "Баскетбол"}]
        payload["customFactors"][0]["factors"][0]["pt"] = "0.5"
        result = extract_match_markets(payload, event_id=10)
        self.assertTrue(all(m["name"] is None for m in result["markets"]))

    def test_verified_individual_yellow_card_totals_both_teams(self):
        # All 6 quotes from two Fonbet UI screenshots (Brentford-Everton).
        pairs = [
            (1809, 1810, "1.5", 1.84, 1.86, "Брентфорд"),
            (1854, 1871, "1.5", 1.65, 2.10, "Эвертон"),
            (1873, 1874, "2.5", 3.05, 1.33, "Эвертон"),
        ]
        factors = []
        for over_id, under_id, threshold, over, under, _ in pairs:
            factors.extend([
                {"f": over_id, "pt": threshold, "v": over},
                {"f": under_id, "pt": threshold, "v": under},
            ])
        payload = {
            "sports": [{"id": 1, "kind": "sport", "name": "Футбол"}],
            "events": [
                {"id": 10, "sportId": 1, "team1": "Брентфорд", "team2": "Эвертон"},
                {"id": 11, "sportId": 1, "name": "желтые карты", "parentId": 10},
            ],
            "customFactors": [{"e": 11, "factors": factors}],
        }
        markets = extract_match_markets(payload, event_id=10, query="желт")["markets"]
        by_id = {market["factor_id"]: market for market in markets}
        self.assertEqual(len(by_id), 6)
        for over_id, under_id, threshold, over, under, team in pairs:
            self.assertEqual(by_id[over_id]["name"], f"{team} ИТБ ЖК ({threshold})")
            self.assertEqual(by_id[under_id]["name"], f"{team} ИТМ ЖК ({threshold})")
            self.assertEqual(by_id[over_id]["odds"], over)
            self.assertEqual(by_id[under_id]["odds"], under)
            self.assertEqual(
                by_id[under_id]["mapping_source"], "calibrated_fonbet_ui_bre_ev_cards"
            )
        self.assertEqual(
            extract_unlabeled_pairs(payload, event_id=10, section="желт")["calibration_hypothesis_count"],
            0,
        )

    def test_yellow_card_calibration_never_leaks_to_other_sections_or_thresholds(self):
        payload = {
            "sports": [{"id": 1, "kind": "sport", "name": "Футбол"}],
            "events": [
                {"id": 10, "sportId": 1, "team1": "Home", "team2": "Away"},
                {"id": 11, "sportId": 1, "name": "желтые карты", "parentId": 10},
                {"id": 12, "sportId": 1, "name": "1-й тайм желтые карты", "parentId": 10},
                {"id": 13, "sportId": 1, "name": "желтые карты", "parentId": 12},
                {"id": 14, "sportId": 1, "name": "угловые", "parentId": 10},
            ],
            "customFactors": [
                {"e": 11, "factors": [
                    {"f": 1809, "pt": "0.5", "v": 1.8},  # threshold not verified
                    {"f": 1854, "pt": "2.5", "v": 1.8},  # threshold not verified
                ]},
                {"e": 12, "factors": [{"f": 1809, "pt": "1.5", "v": 1.8}]},
                {"e": 13, "factors": [{"f": 1854, "pt": "1.5", "v": 1.8}]},
                {"e": 14, "factors": [{"f": 1809, "pt": "4.5", "v": 1.5}]},
                {"e": 10, "factors": [{"f": 1809, "pt": "0.5", "v": 1.2}]},
            ],
        }
        markets = extract_match_markets(payload, event_id=10)["markets"]
        for m in markets:
            if m["event_id"] == 14:
                self.assertEqual(m["name"], "Home ИТБ угловых (4.5)")
            elif m["event_id"] == 10:
                self.assertEqual(m["name"], "Home ИТБ голов (0.5)")
            else:
                self.assertIsNone(m["name"])
        payload["sports"] = [{"id": 1, "kind": "sport", "name": "Баскетбол"}]
        markets = extract_match_markets(payload, event_id=10)["markets"]
        self.assertTrue(all(m["name"] is None for m in markets))

    def test_unverified_goal_card_hypotheses_only_for_full_match(self):
        payload = {
            "sports": [{"id": 1, "kind": "sport", "name": "Футбол"}],
            "events": [
                {"id": 10, "sportId": 1, "team1": "Брентфорд", "team2": "Эвертон"},
                {"id": 11, "sportId": 1, "name": "желтые карты", "parentId": 10},
                {"id": 12, "sportId": 1, "name": "1-й тайм желтые карты", "parentId": 10},
                {"id": 13, "sportId": 1, "name": "угловые", "parentId": 10},
            ],
            "customFactors": [
                {"e": 10, "factors": [
                    {"f": 1809, "pt": "0.5", "v": 1.20},
                    {"f": 1810, "pt": "0.5", "v": 4.00},
                    {"f": 1854, "pt": "0.5", "v": 1.30},
                    {"f": 1871, "pt": "0.5", "v": 3.20},
                    {"f": 1812, "pt": "1", "v": 1.40},  # missing under
                ]},
                {"e": 11, "factors": [
                    {"f": 1809, "pt": "1.5", "v": 1.84},
                    {"f": 1810, "pt": "1.5", "v": 1.86},
                    {"f": 1854, "pt": "1.5", "v": 1.65},
                    {"f": 1871, "pt": "1.5", "v": 2.10},
                ]},
                {"e": 12, "factors": [
                    {"f": 1809, "pt": "0.5", "v": 1.50},
                    {"f": 1810, "pt": "0.5", "v": 2.50},
                ]},
                {"e": 13, "factors": [
                    {"f": 1809, "pt": "4.5", "v": 1.37},
                    {"f": 1810, "pt": "4.5", "v": 2.85},
                ]},
            ],
        }
        result = extract_unlabeled_pairs(payload, event_id=10)
        candidates = result["calibration_hypotheses"]
        # Goals and these three card pairs have now been verified against
        # the user-supplied Fonbet UI. They no longer appear as hypotheses.
        self.assertEqual(len(candidates), 0)
        underlying = extract_match_markets(payload, event_id=10)
        self.assertTrue(all(m["name"] is not None for m in underlying["markets"]
                            if m["subevent_name"] == "желтые карты"))
        self.assertTrue(all(m["name"] is not None for m in underlying["markets"]
                            if m["event_id"] == 10))
        self.assertTrue(all(m["name"] is None for m in underlying["markets"]
                            if m["subevent_name"] == "1-й тайм желтые карты"))

    def test_team_hypotheses_are_filtered_by_section_and_reject_other_sports(self):
        payload = {
            "sports": [{"id": 1, "kind": "sport", "name": "Футбол"}],
            "events": [
                {"id": 10, "sportId": 1, "team1": "A", "team2": "B"},
                {"id": 11, "sportId": 1, "name": "желтые карты", "parentId": 10},
            ],
            "customFactors": [
                {"e": 10, "factors": [{"f": 1809, "pt": "1.5", "v": 1.5},
                                     {"f": 1810, "pt": "1.5", "v": 2.3}]},
                {"e": 11, "factors": [{"f": 1854, "pt": "1.5", "v": 1.7},
                                     {"f": 1871, "pt": "1.5", "v": 2.1}]},
            ],
        }
        cards = extract_unlabeled_pairs(payload, 10, section="желт")
        goals = extract_unlabeled_pairs(payload, 10, section="Матч")
        # Away factor 1854/1871 at 1.5 cards is now verified.
        self.assertEqual(cards["calibration_hypothesis_count"], 0)
        # The test uses the wrong full-match goal threshold for factor 1809
        # (1.5 instead of screenshot-confirmed 0.5), so it stays unverified.
        self.assertEqual(goals["calibration_hypothesis_count"], 1)
        self.assertEqual(goals["calibration_hypotheses"][0]["market_section"], "голы")
        payload["sports"] = [{"id": 1, "kind": "sport", "name": "Баскетбол"}]
        self.assertEqual(extract_unlabeled_pairs(payload, 10)["calibration_hypothesis_count"], 0)

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
