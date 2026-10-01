import unittest

from sre_toolkit.cache import Cache
from sre_toolkit.errors import BudgetExceeded
from sre_toolkit.ledger import Ledger
from sre_toolkit.pricing import cost_of, token_cost


class Pricing(unittest.TestCase):
    def test_metric_pricing_is_per_thousand(self):
        # 1,000 metrics at $0.01/1,000 is one cent.
        self.assertAlmostEqual(cost_of("cloudwatch:GetMetricData", 1000), 0.01)
        self.assertAlmostEqual(cost_of("cloudwatch:GetMetricData", 19), 0.00019)

    def test_free_operations_are_free(self):
        for op in ("logs:FilterLogEvents", "ecs:DescribeServices", "cloudtrail:LookupEvents"):
            self.assertEqual(cost_of(op, 500), 0.0)

    def test_insights_is_priced_per_gb(self):
        self.assertAlmostEqual(cost_of("logs:StartQuery", 2.0), 0.01)

    def test_unknown_model_prices_as_most_expensive(self):
        known = token_cost("anthropic.claude-opus-5", 1000, 1000)
        unknown = token_cost("some-future-model", 1000, 1000)
        self.assertGreaterEqual(unknown, known)


class BudgetGuard(unittest.TestCase):
    def test_charge_raises_before_breaching_limit(self):
        ledger = Ledger(max_spend=0.001)
        ledger.charge("cloudwatch:GetMetricData", 50)  # $0.0005, fine
        with self.assertRaises(BudgetExceeded):
            ledger.charge("logs:StartQuery", 1.0)  # $0.005, over the limit
        # The refused call is not recorded as spend.
        self.assertLess(ledger.total, 0.001)

    def test_dry_run_records_but_does_not_bill(self):
        ledger = Ledger(dry_run=True)
        ledger.charge("cloudwatch:GetMetricData", 1000)
        self.assertEqual(ledger.total, 0.0)
        self.assertAlmostEqual(ledger.avoided, 0.01)

    def test_cached_entries_count_as_avoided_spend(self):
        ledger = Ledger()
        ledger.charge("cloudwatch:GetMetricData", 1000, billed=False)
        self.assertEqual(ledger.total, 0.0)
        self.assertAlmostEqual(ledger.avoided, 0.01)

    def test_token_charge_respects_limit(self):
        ledger = Ledger(max_spend=0.0001)
        with self.assertRaises(BudgetExceeded):
            ledger.charge_tokens("anthropic.claude-opus-5", 100_000, 10_000)


class CacheBehaviour(unittest.TestCase):
    def test_round_trip_and_ttl_expiry(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            import os

            os.environ["SRE_CACHE_DIR"] = tmp
            try:
                cache = Cache(ttl=300)
                cache.set("op", {"a": 1}, {"value": 42})
                self.assertEqual(cache.get("op", {"a": 1}), {"value": 42})
                self.assertIsNone(cache.get("op", {"a": 2}))

                expired = Cache(ttl=-1)
                expired.enabled = True
                self.assertIsNone(expired.get("op", {"a": 1}))
            finally:
                os.environ.pop("SRE_CACHE_DIR", None)

    def test_disabled_cache_never_returns_values(self):
        cache = Cache(ttl=300, enabled=False)
        cache.set("op", {}, {"value": 1})
        self.assertIsNone(cache.get("op", {}))


if __name__ == "__main__":
    unittest.main()
