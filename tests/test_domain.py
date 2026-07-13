import math
import unittest

from kcaloriebot.domain import (
    ValidationError,
    normalize_food_name,
    normalize_search_query,
    parse_calories,
    parse_grams,
    parse_macro,
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


if __name__ == "__main__":
    unittest.main()
