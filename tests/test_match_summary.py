from __future__ import annotations

import unittest

from fonbet_mcp.summary import find_match_candidates, summarize_match_line


class MatchLineTests(unittest.TestCase):
    def setUp(self):
        self.events = [
            {"id": 10, "sportId": 1, "team1": "Брентфорд", "team2": "Эвертон",
             "startTime": 1795464000, "place": "line"},
            {"id": 11, "sportId": 1, "name": "угловые", "parentId": 10},
            {"id": 12, "sportId": 1, "name": "желтые карты", "parentId": 10},
            {"id": 13, "sportId": 1, "name": "1-й тайм угловые", "parentId": 10},
            {"id": 99, "sportId": 2, "team1": "Брентфорд", "team2": "Эвертон"},
        ]
        self.line = {
            "sports": [
                {"id": 1, "kind": "sport", "name": "Футбол"},
                {"id": 2, "kind": "sport", "name": "Теннис"},
            ],
            "events": self.events,
            "customFactors": [
                {"e": 10, "factors": [
                    {"f": 930, "pt": "2.5", "v": 1.82},
                    {"f": 931, "pt": "2.5", "v": 1.98},
                    {"f": 1809, "pt": "0.5", "v": 1.20},
                    {"f": 1810, "pt": "0.5", "v": 4.00},
                    {"f": 1854, "pt": "0.5", "v": 1.30},
                    {"f": 9009, "pt": "0.5", "v": 2.00},
                    {"f": 1888, "pt": "1:0", "v": 8.10},
                ]},
                {"e": 11, "factors": [
                    {"f": 1696, "pt": "8.5", "v": 1.32},
                    {"f": 1697, "pt": "8.5", "v": 3.10},
                    {"f": 1809, "pt": "4.5", "v": 1.37},
                    {"f": 1810, "pt": "4.5", "v": 2.85},
                    {"f": 1698, "pt": "5.5", "v": 1.5},
                ]},
                {"e": 12, "factors": [
                    {"f": 930, "pt": "3.5", "v": 1.90},
                    {"f": 1809, "pt": "1.5", "v": 1.84},
                    {"f": 1810, "pt": "1.5", "v": 1.86},
                    {"f": 1854, "pt": "1.5", "v": 1.65},
                    {"f": 1871, "pt": "1.5", "v": 2.10},
                    {"f": 1873, "pt": "2.5", "v": 3.05, "b": True},
                ]},
                {"e": 13, "factors": [
                    {"f": 930, "pt": "4.5", "v": 1.67},
                    {"f": 1809, "pt": "4.5", "v": 1.90},
                ]},
            ],
        }
        # Event view has no subevents; they must be merged from line payload.
        self.detail = {
            "sports": self.line["sports"],
            "events": [self.events[0]],
            "customFactors": [self.line["customFactors"][0]],
        }

    def test_match_query_exact_variants_and_single_name(self):
        for query in ("Брентфорд — Эвертон", "брентфорд-эвертон",
                      "БРЕНТФОРД ЭВЕРТОН", "Брентфорд"):
            result = find_match_candidates(self.line, query)
            self.assertEqual(result["status"], "found")
            self.assertEqual(result["event"]["event_id"], 10)

    def test_ambiguous_query_returns_candidates_not_random_event(self):
        line = dict(self.line)
        line["events"] = [*self.events, {
            "id": 20, "sportId": 1, "team1": "Брентфорд", "team2": "Ньюкасл"
        }]
        result = find_match_candidates(line, "Брентфорд")
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual({v["event_id"] for v in result["candidates"]}, {10, 20})
        # Exact two-team query must still prefer the correct match.
        self.assertEqual(find_match_candidates(line, "Брентфорд Эвертон")["event"]["event_id"], 10)

    def test_unknown_and_short_query(self):
        self.assertEqual(find_match_candidates(self.line, "неизвестная команда")["status"], "not_found")
        self.assertEqual(find_match_candidates(self.line, "-")["status"], "invalid_query")

    def test_summary_filters_verified_full_match_totals(self):
        result = summarize_match_line(10, self.detail, self.line)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["event"]["team1"], "Брентфорд")
        self.assertEqual(result["event"]["team2"], "Эвертон")
        sections = result["market_sections"]
        self.assertEqual(len(sections["goals"]["general_totals"]), 2)
        self.assertEqual(len(sections["goals"]["individual_totals"]), 3)
        self.assertEqual(len(sections["corners"]["general_totals"]), 2)
        self.assertEqual(len(sections["corners"]["individual_totals"]), 2)
        self.assertEqual(len(sections["yellow_cards"]["general_totals"]), 1)
        self.assertEqual(len(sections["yellow_cards"]["individual_totals"]), 4)
        self.assertEqual(result["selected_market_count"], 14)

        home_cards = next(x for x in sections["yellow_cards"]["individual_totals"]
                          if x["factor_id"] == 1809)
        self.assertEqual(home_cards["team"], "Брентфорд")
        self.assertEqual(home_cards["direction"], "over")
        self.assertEqual(home_cards["threshold"], "1.5")
        self.assertEqual(home_cards["break_even_percent"], 54.35)

        away_cards = next(x for x in sections["yellow_cards"]["individual_totals"]
                          if x["factor_id"] == 1871)
        self.assertEqual(away_cards["team"], "Эвертон")
        self.assertEqual(away_cards["direction"], "under")
        self.assertEqual(away_cards["odds"], 2.10)

        self.assertEqual(
            {x["factor_id"] for cat in sections.values() for values in cat.values() for x in values}
            & {9009, 1888, 1698},
            set(),
        )
        self.assertTrue(all("1-й тайм" not in x["label"]
                            for cat in sections.values() for values in cat.values() for x in values))
        self.assertIn("NOT an independent prediction", result["probability_note"])

    def test_blocked_missing_unknown_and_periods_are_not_exposed(self):
        self.line["customFactors"][2]["factors"].extend([
            {"f": 1873, "pt": "2.5", "v": 3.05, "b": True},
            {"f": 1874, "pt": "2.5", "v": 1},
            {"f": 1874, "pt": "2.5", "v": "1.33"},
        ])
        result = summarize_match_line(10, self.detail, self.line)
        cards = result["market_sections"]["yellow_cards"]["individual_totals"]
        self.assertTrue(all(x["factor_id"] not in {1873, 1874} for x in cards))
        self.assertEqual(result["selected_market_count"], 14)

    def test_empty_known_market_does_not_invent_odds(self):
        detail = dict(self.detail)
        detail["customFactors"] = [{"e": 10, "factors": []}]
        line = dict(self.line)
        line["customFactors"] = []
        result = summarize_match_line(10, detail, line)
        self.assertEqual(result["selected_market_count"], 0)
        self.assertTrue(all(not section["general_totals"] and not section["individual_totals"]
                            for section in result["market_sections"].values()))


if __name__ == "__main__":
    unittest.main()
