import math
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kcaloriebot.domain import (
    NutritionTotals,
    QuickAdd,
    ValidationError,
    normalize_food_name,
    normalize_search_query,
    parse_calories,
    parse_daily_goal,
    parse_entry_time,
    parse_grams,
    parse_macro,
    parse_quick_add,
    per_100_from_totals,
    scale_per_100,
    validate_macro_sum,
)


class NumericParsingTests(unittest.TestCase):
    def test_calories_accept_non_negative_finite_values(self) -> None:
        cases = {
            "0": 0.0,
            " 12.5 ": 12.5,
            "12,5": 12.5,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_calories(text), expected)

    def test_calories_reject_invalid_values(self) -> None:
        for text in (
            "",
            "not-a-number",
            "-0.01",
            "10000.01",
            "nan",
            "inf",
            "-inf",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_calories(text)

    def test_grams_must_be_strictly_positive_and_finite(self) -> None:
        self.assertEqual(parse_grams("0.01"), 0.01)
        for text in ("", "0", "-1", "100000.01", "nan", "inf", "-inf"):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_grams(text)

    def test_macro_accepts_closed_zero_to_one_hundred_range(self) -> None:
        for text, expected in (("0", 0.0), ("25,5", 25.5), ("100", 100.0)):
            with self.subTest(text=text):
                self.assertEqual(parse_macro(text, "Protein"), expected)

    def test_macro_rejects_out_of_range_and_non_finite_values(self) -> None:
        for text in ("-0.01", "100.01", "nan", "inf", "-inf", "food"):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_macro(text, "Protein")

    def test_food_name_is_normalized_and_bounded(self) -> None:
        self.assertEqual(normalize_food_name("  Greek   yogurt  "), "Greek yogurt")
        for text in ("   ", "x" * 201):
            with self.subTest(length=len(text)):
                with self.assertRaises(ValidationError):
                    normalize_food_name(text)

    def test_search_query_is_normalized_and_bounded(self) -> None:
        self.assertEqual(normalize_search_query("  greek   yogurt "), "greek yogurt")
        for text in ("\t\n", "x" * 201):
            with self.subTest(length=len(text)):
                with self.assertRaises(ValidationError):
                    normalize_search_query(text)


class NutritionTests(unittest.TestCase):
    def test_macro_sum_accepts_exactly_one_hundred_and_missing_values(self) -> None:
        validate_macro_sum(30.0, 20.0, 50.0)
        validate_macro_sum(60.0, None, 40.0)
        validate_macro_sum(None, None, None)

    def test_macro_sum_rejects_more_than_one_hundred(self) -> None:
        with self.assertRaises(ValidationError):
            validate_macro_sum(30.0, 20.0, 50.01)

    def test_macro_sum_rejects_invalid_individual_values(self) -> None:
        for value in (math.nan, math.inf, -math.inf, -1.0, 101.0):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_macro_sum(value, None, None)

    def test_scale_per_100_scales_each_value_exactly_once(self) -> None:
        totals = scale_per_100(250.0, 40.0, 10.0, 20.0, 30.0)

        self.assertEqual(totals.grams, 40.0)
        self.assertAlmostEqual(totals.calories, 100.0)
        self.assertAlmostEqual(totals.protein or 0.0, 4.0)
        self.assertAlmostEqual(totals.fat or 0.0, 8.0)
        self.assertAlmostEqual(totals.carbs or 0.0, 12.0)

    def test_scale_preserves_missing_optional_macros(self) -> None:
        totals = scale_per_100(100.0, 25.0, None, 4.0, None)

        self.assertIsNone(totals.protein)
        self.assertAlmostEqual(totals.fat or 0.0, 1.0)
        self.assertIsNone(totals.carbs)

    def test_macro_sum_is_checked_in_per_100_units_before_scaling(self) -> None:
        totals = scale_per_100(300.0, 250.0, 30.0, 20.0, 40.0)

        self.assertAlmostEqual(totals.protein or 0.0, 75.0)
        self.assertAlmostEqual(totals.fat or 0.0, 50.0)
        self.assertAlmostEqual(totals.carbs or 0.0, 100.0)

    def test_scale_rejects_invalid_calories_or_grams(self) -> None:
        invalid_cases = (
            (-1.0, 100.0),
            (math.nan, 100.0),
            (math.inf, 100.0),
            (100.0, 0.0),
            (100.0, -1.0),
            (100.0, math.nan),
            (100.0, math.inf),
        )
        for calories, grams in invalid_cases:
            with self.subTest(calories=calories, grams=grams):
                with self.assertRaises(ValidationError):
                    scale_per_100(calories, grams, None, None, None)

    def test_scale_rejects_finite_inputs_that_overflow_the_result(self) -> None:
        with self.assertRaises(ValidationError):
            scale_per_100(1e307, 1e307, None, None, None)

    def test_scale_rejects_invalid_macros_and_macro_sum(self) -> None:
        for macros in (
            (-1.0, None, None),
            (101.0, None, None),
            (math.nan, None, None),
            (math.inf, None, None),
            (50.0, 40.0, 20.0),
        ):
            with self.subTest(macros=macros):
                with self.assertRaises(ValidationError):
                    scale_per_100(100.0, 100.0, *macros)

    def test_per_100_recovers_original_values_from_totals(self) -> None:
        totals = NutritionTotals(
            calories=100.0, grams=40.0, protein=4.0, fat=None, carbs=12.0
        )

        calories, protein, fat, carbs = per_100_from_totals(totals)

        self.assertAlmostEqual(250.0, calories)
        self.assertAlmostEqual(10.0, protein)
        self.assertIsNone(fat)
        self.assertAlmostEqual(30.0, carbs)

    def test_per_100_clamps_float_rounding_at_the_macro_boundary(self) -> None:
        totals = NutritionTotals(
            calories=300.0, grams=0.3, protein=0.30000000000000004, fat=None, carbs=None
        )

        _, protein, _, _ = per_100_from_totals(totals)

        self.assertEqual(100.0, protein)


class QuickAddParsingTests(unittest.TestCase):
    def test_plain_name_calories_grams(self) -> None:
        self.assertEqual(
            parse_quick_add("oatmeal 370 60"),
            QuickAdd("oatmeal", 370.0, 60.0),
        )

    def test_multiword_names_and_decimal_commas(self) -> None:
        self.assertEqual(
            parse_quick_add("greek yogurt 59,5 150"),
            QuickAdd("greek yogurt", 59.5, 150.0),
        )

    def test_units_fix_value_assignment_in_any_order(self) -> None:
        for text in (
            "bread 250 kcal 150 g",
            "bread 150g 250kcal",
            "bread 150 g 250 kcal",
            "bread 250kcal 150",
            "bread 150g 250",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_quick_add(text), QuickAdd("bread", 250.0, 150.0))

    def test_russian_units_are_recognized(self) -> None:
        self.assertEqual(
            parse_quick_add("буханка 250 ккал 150 г"),
            QuickAdd("буханка", 250.0, 150.0),
        )

    def test_optional_macro_tokens(self) -> None:
        self.assertEqual(
            parse_quick_add("bread 250 150 p8 f3 c47"),
            QuickAdd("bread", 250.0, 150.0, 8.0, 3.0, 47.0),
        )
        self.assertEqual(
            parse_quick_add("хлеб 250 150 б8 ж3 у47"),
            QuickAdd("хлеб", 250.0, 150.0, 8.0, 3.0, 47.0),
        )

    def test_non_quick_add_text_returns_none(self) -> None:
        for text in (
            "Add Food",
            "hello there",
            "рис",
            "5 шт",
            "oatmeal 370",
            "1 2 3",
            "370 60",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_quick_add(text))

    def test_invalid_values_raise_validation_errors(self) -> None:
        for text in (
            "bread 20000 100",
            "bread 250 0",
            "bread 250 100 p60 f60",
            "bread 250kcal 100kcal",
            "bread 250 100 p101",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_quick_add(text)


class DailyGoalParsingTests(unittest.TestCase):
    def test_goal_accepts_positive_values_up_to_the_cap(self) -> None:
        self.assertEqual(2000.0, parse_daily_goal("2000"))
        self.assertEqual(50_000.0, parse_daily_goal("50000"))

    def test_goal_rejects_invalid_values(self) -> None:
        for text in ("0", "-1", "50001", "nan", "inf", "food"):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_daily_goal(text)


class EntryTimeParsingTests(unittest.TestCase):
    NOW = int(datetime(2024, 6, 15, 20, 0, tzinfo=timezone.utc).timestamp())

    def test_full_datetime_is_interpreted_in_the_user_timezone(self) -> None:
        expected = int(
            datetime(2024, 6, 14, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")).timestamp()
        )
        self.assertEqual(
            expected, parse_entry_time("2024-06-14 12:00", "Europe/Moscow", self.NOW)
        )
        self.assertEqual(
            expected, parse_entry_time("14.06.2024 12:00", "Europe/Moscow", self.NOW)
        )

    def test_clock_only_uses_the_local_today(self) -> None:
        expected = int(datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(expected, parse_entry_time("12:00", "UTC", self.NOW))

    def test_future_and_ancient_times_are_rejected(self) -> None:
        for text in ("23:59", "2023-01-01 12:00"):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_entry_time(text, "UTC", self.NOW)

    def test_unrecognized_format_is_rejected(self) -> None:
        for text in ("yesterday", "12", "2024-06-15", "12:00:30"):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    parse_entry_time(text, "UTC", self.NOW)


if __name__ == "__main__":
    unittest.main()
