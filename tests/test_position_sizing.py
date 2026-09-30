import unittest

from util.position_sizing import atr_risk_quantity


class AtrPositionSizingTest(unittest.TestCase):
    def test_atr_risk_budget_changes_with_stop_distance(self):
        assets = 10_000_000
        self.assertEqual(atr_risk_quantity(assets, 10_000, 250), 100)
        self.assertEqual(atr_risk_quantity(assets, 10_000, 500), 50)
        self.assertEqual(atr_risk_quantity(assets, 10_000, 1000), 25)

    def test_invalid_stop_or_unaffordable_one_share_is_rejected(self):
        with self.assertRaises(ValueError):
            atr_risk_quantity(10_000_000, 10_000, 5_000)
        self.assertEqual(atr_risk_quantity(10_000, 10_000, 100), 0)


if __name__ == "__main__":
    unittest.main()
