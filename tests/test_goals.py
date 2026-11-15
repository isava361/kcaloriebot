import math
import unittest

from kcaloriebot.domain import (
    Goals,
    Stats,
    ValidationError,
    check_goals,
    parse_goals,
    parse_reminder_time,
)
from kcaloriebot.render import (
    goals_summary,
    macro_goal_progress,
    stats_totals_text,
    today_progress_text,
)


class GoalParsingTests(unittest.TestCase):
    def test_bare_number_changes_only_the_calorie_goal(self) -> None:
        self.assertEqual({"calories": 2000.0}, parse_goals("2000"))
        self.assertEqual({"calories": 1850.5}, parse_goals("1850,5"))

    def test_bare_number_keeps_the_original_messages(self) -> None:
        for text, message in (
            ("0", "greater than zero"),
            ("-5", "greater than zero"),
            ("abc", "must be a number"),
            ("50001", "no more than 50000"),
            ("nan", "must be finite"),
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValidationError, message):
                    parse_goals(text)

    def test_calories_and_macros_in_one_message(self) -> None:
        expected = {"calories": 2000.0, "protein": 150.0, "fat": 70.0, "carbs": 200.0}
        for text in (
            "2000 p150 f70 c200",
            "p150 f70 c200 2000",
            "2000 kcal p150 f70 c200",
            "2000ккал б150 ж70 у200",
            "2000 P150 F70 C200",
            "2000 p 150 f 70 c 200",
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, parse_goals(text))

    def test_macros_without_calories_and_zero_removes_one(self) -> None:
        self.assertEqual({"protein": 120.0}, parse_goals("p120"))
        self.assertEqual({"protein": None, "fat": 60.5}, parse_goals("p0 f60,5"))

    def test_invalid_goal_messages_are_rejected(self) -> None:
        for text in (
            "2000 x5",
            "2000 1800",
            "p100 p120",
            "p2001",
            "2000 p",
            "kcal 2000",
            "0 p100",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_goals(text)

    def test_merge_keeps_unmentioned_goals(self) -> None:
        goals = Goals(2000.0, 150.0, 70.0, None)
        self.assertEqual(
            Goals(1800.0, 150.0, 70.0, None), goals.merged(parse_goals("1800"))
        )
        self.assertEqual(
            Goals(2000.0, None, 70.0, 250.0), goals.merged(parse_goals("p0 c250"))
        )

    def test_check_goals_enforces_limits(self) -> None:
        check_goals(Goals(2000.0, 2000.0, None, 1.0))
        for goals in (
            Goals(calories=0.0),
            Goals(protein=0.0),
            Goals(fat=2000.5),
            Goals(carbs=math.inf),
        ):
            with self.subTest(goals=goals):
                with self.assertRaises(ValidationError):
                    check_goals(goals)

    def test_reminder_time_parsing(self) -> None:
        self.assertEqual(21 * 60, parse_reminder_time("21:00"))
        self.assertEqual(9 * 60 + 30, parse_reminder_time(" 9.30 "))
        self.assertEqual(21 * 60, parse_reminder_time("21"))
        self.assertEqual(0, parse_reminder_time("00:00"))
        for text in ("24:00", "21:60", "9pm", "", "21:5"):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_reminder_time(text)


def stats(
    entries: int,
    calories: float,
    protein=None,
    protein_known=0,
    fat=None,
    fat_known=0,
    carbs=None,
    carbs_known=0,
) -> Stats:
    return Stats(
        entry_count=entries,
        calories=calories,
        protein=protein,
        fat=fat,
        carbs=carbs,
        coverage_total=entries,
        protein_coverage=protein_known,
        fat_coverage=fat_known,
        carbs_coverage=carbs_known,
    )


class GoalProgressRenderingTests(unittest.TestCase):
    def test_calorie_line_is_unchanged_without_macro_goals(self) -> None:
        self.assertEqual(
            "Today: 100 kcal.", today_progress_text(stats(1, 100.0), Goals())
        )
        self.assertEqual(
            "Today: 100 / 2000 kcal (1900 left).",
            today_progress_text(stats(1, 100.0), Goals(calories=2000.0)),
        )
        self.assertEqual(
            "Today: 2100 / 2000 kcal (100 over).",
            today_progress_text(stats(1, 2100.0), Goals(calories=2000.0)),
        )

    def test_complete_macro_coverage_shows_remaining(self) -> None:
        text = today_progress_text(
            stats(2, 900.0, protein=95.0, protein_known=2, fat=80.0, fat_known=2),
            Goals(2000.0, 150.0, 70.0, None),
        )
        self.assertIn("Today: 900 / 2000 kcal (1100 left).", text)
        self.assertIn("protein 95 / 150 g (55 left)", text)
        self.assertIn("fat 80 / 70 g (10 over)", text)
        self.assertNotIn("carbs", text)

    def test_partial_macro_coverage_is_a_lower_bound(self) -> None:
        text = today_progress_text(
            stats(3, 900.0, protein=60.0, protein_known=2),
            Goals(protein=150.0, carbs=200.0),
        )
        self.assertIn("protein at least 60 / 150 g (known for 2/3 entries)", text)
        self.assertNotIn("left", text.split("\n")[1])
        self.assertIn("carbs not recorded / 200 g", text)

    def test_no_entries_yet_counts_from_zero(self) -> None:
        text = today_progress_text(stats(0, 0.0), Goals(protein=150.0))
        self.assertIn("protein 0 / 150 g (150 left)", text)

    def test_diary_totals_show_macro_goals_honestly(self) -> None:
        text = stats_totals_text(
            "Today's totals",
            stats(3, 900.0, protein=60.0, protein_known=2, fat=30.0, fat_known=3),
            2000.0,
            Goals(2000.0, 150.0, 70.0, None),
        )
        self.assertIn("Calories: 900 kcal / 2000 goal (1100 left)", text)
        self.assertIn(
            "Protein: 60 g (partial: 2/3 entries) / 150 g goal (remaining unknown)",
            text,
        )
        self.assertIn("Fat: 30 g / 70 g goal (40 left)", text)
        self.assertIn("Carbs: not recorded", text)
        self.assertNotIn("Carbs: not recorded (partial: 0/3 entries) /", text)

    def test_diary_totals_with_goals_but_no_macros_recorded(self) -> None:
        text = stats_totals_text(
            "Today's totals", stats(1, 100.0), None, Goals(protein=150.0)
        )
        self.assertIn(
            "Protein: not recorded (partial: 0/1 entries) / 150 g goal "
            "(remaining unknown)",
            text,
        )
        self.assertEqual(
            "Today's totals:\nCalories: 100 kcal\nMacros not recorded",
            stats_totals_text("Today's totals", stats(1, 100.0), None, Goals()),
        )

    def test_partial_coverage_already_over_the_goal(self) -> None:
        self.assertEqual(
            "150 g goal (over by at least 10)", macro_goal_progress(160.0, 1, 2, 150.0)
        )

    def test_goals_summary_uses_the_input_format(self) -> None:
        self.assertEqual(
            "2000 kcal p150 f70.5 c200",
            goals_summary(Goals(2000.0, 150.0, 70.5, 200.0)),
        )
        self.assertEqual("p120", goals_summary(Goals(protein=120.0)))
        self.assertEqual("", goals_summary(Goals()))
        self.assertEqual(
            {"calories": 2000.0, "protein": 150.0, "fat": 70.5, "carbs": 200.0},
            parse_goals(goals_summary(Goals(2000.0, 150.0, 70.5, 200.0))),
        )


if __name__ == "__main__":
    unittest.main()
