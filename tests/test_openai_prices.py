import unittest

from collector import openai_prices as p


class PriceKeyTests(unittest.TestCase):
    def test_exact_prefix_and_date_forms(self):
        self.assertEqual(p.price_key("gpt-5.4"), "gpt-5.4")
        self.assertEqual(p.price_key("openai/gpt-5.4"), "gpt-5.4")
        self.assertEqual(p.price_key("gpt-5.4-2026-03-05"), "gpt-5.4")
        self.assertEqual(p.price_key("GPT-5.4-mini"), "gpt-5.4-mini")      # longest key wins
        self.assertEqual(p.price_key("gpt-5.4-mini-20260305"), "gpt-5.4-mini")

    def test_unknown(self):
        self.assertIsNone(p.price_key("mystery"))
        self.assertIsNone(p.price_key(None))
        self.assertIsNone(p.price_key("gpt-9"))


class RowCostTests(unittest.TestCase):
    def test_gpt6_astra(self):
        # 1M input at $10, 1M cached at $1, 1M output at $50
        self.assertAlmostEqual(p.row_cost("gpt-6-astra", 1_000_000, 1_000_000, 1_000_000, 0), 61.0)

    def test_cache_write_gpt6_is_1_25x_input(self):
        self.assertAlmostEqual(p.row_cost("gpt-6-astra", 0, 0, 0, 1_000_000), 12.5)
        self.assertAlmostEqual(p.row_cost("gpt-5.5", 0, 0, 0, 1_000_000), 5.0)

    def test_unpublished_cached_rate_uses_input_rate(self):
        self.assertAlmostEqual(p.row_cost("gpt-5.2", 0, 0, 1_000_000, 0), 1.75)

    def test_unlisted_model_is_none(self):
        self.assertIsNone(p.row_cost("mystery", 1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
