"""Regression tests for context-aware extended Fonbet football markets.

Run with: python -m unittest discover -s tests -p 'test_expanded_summary.py'
"""
import unittest

from fonbet_mcp.summary import _expanded_identified_sections, _identified_market_context


EVENTS = {
    10: {"id": 10, "team1": "Сантос", "team2": "Гремио"},
    20: {"id": 20, "name": "фолы", "parentId": 10},
    21: {"id": 21, "name": "1-й тайм фолы", "parentId": 20},
    22: {"id": 22, "name": "удары в створ", "parentId": 10},
    23: {"id": 23, "name": "2-й тайм удары в створ", "parentId": 22},
    30: {"id": 30, "name": "угловые", "parentId": 10},
    31: {"id": 31, "name": "1-й тайм угловые", "parentId": 30},
    40: {"id": 40, "name": "1-й тайм", "parentId": 10},
    41: {"id": 41, "name": "2-й тайм", "parentId": 10},
    50: {"id": 50, "name": "штанги или перекладины", "parentId": 10},
    60: {"id": 60, "name": "1-й тайм фолы", "parentId": 10},
    70: {"id": 70, "name": "1-й тайм фолы", "parentId": 999},
}


def market(eid, factor, name, param=None, odds=1.8, blocked=None):
    return {
        "event_id": eid,
        "factor_id": factor,
        "name": name,
        "param_text": param,
        "odds": odds,
        "blocked": blocked,
        "subevent_name": None if eid == 10 else EVENTS[eid]["name"],
    }


class ExpandedMarketTests(unittest.TestCase):
    def test_full_time_fouls(self):
        x = market(20, 930, "Тотал больше (27.5)", "27.5", 1.73)
        result, n = _expanded_identified_sections([x], EVENTS, 10)
        self.assertEqual(n, 1)
        entry = result["fouls"]["full_time"]["total"][0]
        self.assertEqual(entry["threshold"], "27.5")
        self.assertEqual(entry["odds"], 1.73)
        self.assertEqual(entry["identification_basis"], "known_factor_id_and_explicit_event_path")

    def test_half_market_is_separate(self):
        first = market(21, 931, "Тотал меньше (13.5)", "13.5", 1.77)
        second = market(23, 927, "Фора 1 (-1.5)", "-1.5", 1.85)
        result, n = _expanded_identified_sections([first, second], EVENTS, 10)
        self.assertEqual(n, 2)
        self.assertEqual(result["fouls"]["first_half"]["total"][0]["threshold"], "13.5")
        self.assertEqual(result["shots_on_target"]["second_half"]["handicap"][0]["team"], "Сантос")

    def test_existing_verified_full_time_totals_not_duplicated(self):
        old = market(30, 930, "Тотал больше (9.5)", "9.5")
        half = market(31, 930, "Тотал больше (4.5)", "4.5")
        result, n = _expanded_identified_sections([old, half], EVENTS, 10)
        self.assertEqual(n, 1)
        self.assertNotIn("full_time", result["corners"])
        self.assertIn("first_half", result["corners"])

    def test_result_types_and_both_score(self):
        items = [
            market(10, 921, "П1"),
            market(10, 924, "1X"),
            market(10, 709, "Обе забьют - да"),
            market(50, 922, "Ничья"),
            market(50, 709, "Обе забьют - да"),
        ]
        result, n = _expanded_identified_sections(items, EVENTS, 10)
        self.assertEqual(n, 4)
        self.assertIn("outcome", result["goals"]["full_time"])
        self.assertIn("both_teams_score", result["goals"]["full_time"])
        self.assertEqual(result["woodwork"]["full_time"]["outcome"][0]["label"], "Ничья")

    def test_strict_path_prevents_false_labels(self):
        bad_direct = market(60, 930, "Тотал больше (12.5)", "12.5")
        bad_parent = market(70, 930, "Тотал больше (12.5)", "12.5")
        result, n = _expanded_identified_sections([bad_direct, bad_parent], EVENTS, 10)
        self.assertEqual((result, n), ({}, 0))

    def test_unlabeled_and_inconsistent_factors_excluded(self):
        unknown = market(20, 1809, None, "12.5")
        wrong_name = market(20, 930, "Тотал меньше (27.5)", "27.5")
        missing_threshold = market(20, 930, "Тотал больше", None)
        blocked = market(20, 930, "Тотал больше (27.5)", "27.5", blocked=True)
        no_price = market(20, 930, "Тотал больше (27.5)", "27.5", odds=1.0)
        result, n = _expanded_identified_sections(
            [unknown, wrong_name, missing_threshold, blocked, no_price], EVENTS, 10
        )
        self.assertEqual((result, n), ({}, 0))

    def test_duplicate_market_only_once(self):
        m = market(20, 930, "Тотал больше (27.5)", "27.5", 1.73)
        result, n = _expanded_identified_sections([m, dict(m)], EVENTS, 10)
        self.assertEqual(n, 1)

    def test_goals_first_half_and_second_half(self):
        items = [
            market(40, 930, "Тотал больше (1.5)", "1.5", 1.6),
            market(41, 931, "Тотал меньше (1.5)", "1.5", 1.7),
        ]
        result, n = _expanded_identified_sections(items, EVENTS, 10)
        self.assertEqual(n, 2)
        self.assertIn("first_half", result["goals"])
        self.assertIn("second_half", result["goals"])


if __name__ == "__main__":
    unittest.main()
