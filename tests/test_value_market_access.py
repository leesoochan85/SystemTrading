"""Market response validation with mock pykrx, no real credentials/network."""
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import os
import unittest
from unittest.mock import Mock, patch
import pandas as pd

from util import value_fundamental_collector as collector


class MarketAccessTest(unittest.TestCase):
    def setUp(self):
        self.stock = SimpleNamespace(get_market_cap_by_ticker=Mock())
        self.stock.get_market_cap_by_ticker.return_value = pd.DataFrame(
            {"시가총액": [1200], "상장주식수": [100]}, index=["000001"])
        patches = [patch.dict(os.environ, {"KRX_ID": "FAKE_USER", "KRX_PW": "FAKE_PASSWORD"}),
                   patch("importlib.metadata.version", return_value="1.2.9"),
                   patch.dict("sys.modules", {"pykrx": SimpleNamespace(stock=self.stock)})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_supported_version_calls_explicit_ticker_api(self):
        day, frame = collector.market_caps(date(2026, 10, 12))
        self.stock.get_market_cap_by_ticker.assert_called_once_with("20261012", market="ALL")
        self.assertEqual(day, date(2026, 10, 12))
        self.assertEqual(len(frame), 1)

    def test_old_version_fails_before_request(self):
        with patch("importlib.metadata.version", return_value="1.0.51"):
            with self.assertRaisesRegex(collector.DartFailure, "PYKRX_UPGRADE_REQUIRED"):
                collector.market_caps(date(2026, 10, 12))
        self.stock.get_market_cap_by_ticker.assert_not_called()

    def test_missing_krx_credentials_fail_before_request(self):
        with patch.dict(os.environ, {"KRX_PW": ""}):
            with self.assertRaisesRegex(collector.DartFailure, "KRX_CREDENTIALS_REQUIRED"):
                collector.market_caps(date(2026, 10, 12))
        self.stock.get_market_cap_by_ticker.assert_not_called()

    def test_original_keyerror_becomes_actionable_safe_error(self):
        self.stock.get_market_cap_by_ticker.side_effect = KeyError("FAKE_PASSWORD")
        with self.assertRaisesRegex(collector.DartFailure, "KRX_RESPONSE_COLUMNS_MISSING") as caught:
            collector.market_caps(date(2026, 10, 12))
        self.assertNotIn("FAKE_PASSWORD", str(caught.exception))

    def test_nonempty_malformed_response_is_not_a_holiday(self):
        self.stock.get_market_cap_by_ticker.return_value = pd.DataFrame({"wrong": [1]})
        with self.assertRaisesRegex(collector.DartFailure, "COLUMNS_CHANGED"):
            collector.market_caps(date(2026, 10, 12))
        self.assertEqual(self.stock.get_market_cap_by_ticker.call_count, 1)

    def test_zero_response_uses_previous_weekday(self):
        self.stock.get_market_cap_by_ticker.side_effect = [
            pd.DataFrame({"시가총액": [0], "상장주식수": [100]}, index=["000001"]),
            pd.DataFrame({"시가총액": [1200], "상장주식수": [100]}, index=["000001"])]
        day, _ = collector.market_caps(date(2026, 10, 12))
        self.assertEqual(day, date(2026, 10, 9))
        self.assertEqual([call.args[0] for call in self.stock.get_market_cap_by_ticker.call_args_list],
                         ["20261012", "20261009"])

    def test_invalid_rows_do_not_poison_valid_numeric_stock(self):
        self.stock.get_market_cap_by_ticker.return_value = pd.DataFrame(
            {"시가총액": [1200, 1200, float("inf"), 1200], "상장주식수": [100, 100, 100, 10.5]},
            index=["000001", "0043Y0", "000002", "000003"])
        _, frame = collector.market_caps(date(2026, 10, 12))
        self.assertEqual(list(frame.index), ["000001"])


if __name__ == "__main__":
    unittest.main()
