import json
import os
import tempfile
import unittest
from unittest import mock

from tokenpanel import pricing
from tokenpanel.model import Usage


class PricingTest(unittest.TestCase):
    def setUp(self):
        # No user price file from the machine running the tests.
        patcher = mock.patch.object(pricing, "_user", {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_normalize(self):
        for raw in (
            "claude-sonnet-4-5",
            "claude-sonnet-4-5-20250929",
            "anthropic/claude-sonnet-4-5",
            "claude-sonnet-4-5[1m]",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "claude-sonnet-4-5@20250929",
            "Claude-Sonnet-4-5",
        ):
            self.assertEqual(pricing.normalize(raw), "claude-sonnet-4-5", raw)

    def test_claude_cost_with_cache_tiers_and_fast_mode(self):
        # Opus 5.5: $4 in, $20 out, $0.20 cache read, $5 5m write, $8 1h write.
        u = Usage(input=1_000_000, cache_read=2_000_000, cache_write=3_000_000, output=1_000_000)
        pricing.price_usage(u, "claude-opus-5-5", write_1h=1_000_000)
        self.assertAlmostEqual(u.cost, 4 + 0.40 + 2 * 5 + 8 + 20)
        self.assertEqual(u.unpriced, 0)
        pricing.price_usage(u, "claude-opus-5-5", write_1h=1_000_000, fast=True)
        self.assertAlmostEqual(u.cost, 2 * (4 + 0.40 + 2 * 5 + 8 + 20))

    def test_openai_cached_input_and_long_context(self):
        # gpt-5.5: $5 in, $0.50 cached, $30 out; over 272K input in one request: 2x input, 1.5x output.
        u = Usage(input=100_000, cache_read=100_000, output=10_000)
        pricing.price_usage(u, "gpt-5.5")
        self.assertAlmostEqual(u.cost, (100_000 * 5 + 100_000 * 0.5 + 10_000 * 30) / 1e6)
        u = Usage(input=200_000, cache_read=100_000, output=10_000)
        pricing.price_usage(u, "gpt-5.5")
        self.assertAlmostEqual(u.cost, (2 * (200_000 * 5 + 100_000 * 0.5) + 1.5 * 10_000 * 30) / 1e6)
        # Older models have no long-context tier.
        u = Usage(input=300_000, output=0)
        pricing.price_usage(u, "gpt-5.1-codex")
        self.assertAlmostEqual(u.cost, 300_000 * 1.25 / 1e6)
        # No official price for a bare "gpt-6".
        pricing.price_usage(u, "gpt-6")
        self.assertEqual(u.cost, 0)

    def test_unknown_model_is_counted_as_unpriced(self):
        u = Usage(input=100, cache_read=900, cache_write=10, output=50)
        pricing.price_usage(u, "some-new-model")
        self.assertEqual(u.cost, 0)
        self.assertEqual(u.unpriced, 160)

    def test_user_price_file_adds_and_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "prices.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"my-model": {"input": 1, "output": 2}, "claude-haiku-4-5": {"input": 9, "output": 9},
                           "broken": {"input": "x"}}, fh)
            prices = pricing.load_user_prices(path)
        self.assertEqual(set(prices), {"my-model", "claude-haiku-4-5"})
        with mock.patch.object(pricing, "_user", prices):
            u = Usage(input=1_000_000, cache_read=1_000_000, output=1_000_000)
            pricing.price_usage(u, "my-model")
            self.assertAlmostEqual(u.cost, 1 + 1 + 2)  # cache reads default to the input price
            pricing.price_usage(u, "claude-haiku-4-5-20251001")
            self.assertAlmostEqual(u.cost, 9 + 9 + 9)

    def test_missing_user_file(self):
        self.assertEqual(pricing.load_user_prices("/nonexistent/prices.json"), {})




class FormatTest(unittest.TestCase):
    def test_short_and_money(self):
        from tokenpanel import fmt

        self.assertEqual(
            [fmt.short(x) for x in (999, 999_499, 999_950, 1_000_000, 15_317_124, 310_000, 99_950)],
            ["999", "999K", "1M", "1M", "15.3M", "310K", "100K"],
        )
        self.assertEqual([fmt.money(x) for x in (0, 0.004, 12.4, 1234.6)], ["$0", "<$0.01", "$12.40", "$1,235"])


if __name__ == "__main__":
    unittest.main()
