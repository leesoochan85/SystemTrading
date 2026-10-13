"""Synthetic financial statements; no API keys, network, or broker orders."""
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import copy
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import pandas as pd

from util import value_quality_data as db
from util import value_fundamental_collector as collector


TODAY = date(2026, 10, 13)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 13, 16)


def statements(year, report="11011", owner=True):
    receipt = {2025: "20260330000001", 2026: "20260814000001"}[year]
    if year == 2025 and report != "11011":
        receipt = "20250814000001"
    def row(name, section, value, prior="1000", ytd=None):
        return dict(account_id="ifrs-full_" + name, account_nm=name,
                    sj_div=section, thstrm_amount=str(value), frmtrm_amount=str(prior),
                    thstrm_add_amount=str(ytd) if ytd is not None else "",
                    currency="KRW", rcept_no=receipt,
                    bsns_year=str(year), reprt_code=report)
    return [row("Revenue", "IS", 1200), row("GrossProfit", "IS", 480),
            row("Assets", "BS", 1000),
            row("EquityAttributableToOwnersOfParent" if owner else "Equity", "BS", 2000),
            row("ProfitLossAttributableToOwnersOfParent" if owner else "ProfitLoss",
                "IS", 100 if report == "11011" else 999,
                ytd=60 if year == 2026 else 40)]


def share_rows(year, report="11011", preferred=0):
    ends = collector.REPORTS[report]
    receipt = "20260814000001" if year == 2026 else (
        "20260330000001" if report == "11011" else "20250814000001")
    return [dict(se=kind, istc_totqy=str(count),
                 stlm_dt=date(year, *ends).isoformat(), rcept_no=receipt)
            for kind, count in (("보통주", 100), ("우선주", preferred), ("합계", 100 + preferred))]


def snapshot():
    return collector.calculate_snapshot(statements(2025), statements(2026, "11012"),
        statements(2025, "11012"), year=2026, report_code="11012", fs_div="CFS",
        market_cap=1200, quote_date=TODAY)


class FinancialCalculationTest(unittest.TestCase):
    def test_ttm_uses_ytd_not_three_month_amount(self):
        result = snapshot()
        self.assertEqual(result["per_ttm"], 10)
        self.assertEqual(result["pbr"], .6)
        self.assertEqual(result["gross_margin_pct"], 40)
        self.assertEqual(result["asset_turnover"], 1.2)
        self.assertEqual(result["financial_period_end"], "20260630")

    def test_missing_half_year_ytd_is_rejected(self):
        current = statements(2026, "11012")
        current[-1]["thstrm_add_amount"] = ""
        with self.assertRaises(collector.DataUnavailable):
            collector.calculate_snapshot(statements(2025), current, statements(2025, "11012"),
                year=2026, report_code="11012", fs_div="CFS", market_cap=1200, quote_date=TODAY)

    def test_q1_can_use_three_month_when_ytd_missing(self):
        current, prior = statements(2026, "11013"), statements(2025, "11013")
        current[-1].update(thstrm_add_amount="", thstrm_amount="60")
        prior[-1].update(thstrm_add_amount="", thstrm_amount="40")
        with self.assertRaisesRegex(collector.DataUnavailable, "STALE_FINANCIAL"):
            collector.calculate_snapshot(statements(2025), current, prior, year=2026,
                report_code="11013", fs_div="CFS", market_cap=1200, quote_date=date(2026, 12, 1))
        result = collector.calculate_snapshot(statements(2025), current, prior, year=2026,
            report_code="11013", fs_div="CFS", market_cap=1200, quote_date=TODAY)
        self.assertEqual(result["per_ttm"], 10)

    def test_total_consolidated_profit_is_not_used_for_parent_profit(self):
        annual = statements(2025, owner=False)
        with self.assertRaisesRegex(collector.DataUnavailable, "owner_profit"):
            collector.calculate_snapshot(annual, statements(2026, "11012"), statements(2025, "11012"),
                year=2026, report_code="11012", fs_div="CFS", market_cap=1200, quote_date=TODAY)

    def test_annual_quality_uses_average_assets(self):
        annual = statements(2025)
        annual[2]["frmtrm_amount"] = "500"
        result = collector.calculate_snapshot(annual, statements(2026, "11012"), statements(2025, "11012"),
            year=2026, report_code="11012", fs_div="CFS", market_cap=1200, quote_date=TODAY)
        self.assertEqual(result["asset_turnover"], 1.6)

    def test_gross_profit_can_be_calculated_from_explicit_cost(self):
        annual = statements(2025)
        annual[1].update(account_id="ifrs-full_CostOfSales", account_nm="매출원가", thstrm_amount="720")
        result = collector.calculate_snapshot(annual, statements(2026, "11012"), statements(2025, "11012"),
            year=2026, report_code="11012", fs_div="CFS", market_cap=1200, quote_date=TODAY)
        self.assertEqual(result["gross_margin_pct"], 40)

    def test_ambiguous_account_missing_currency_nan_and_loss_are_rejected(self):
        for alteration in ("duplicate", "currency", "nan", "loss"):
            annual = statements(2025)
            if alteration == "duplicate":
                annual.append(dict(annual[-1], thstrm_amount="900"))
            elif alteration == "currency":
                annual[-1]["currency"] = "USD"
            elif alteration == "nan":
                annual[-1]["thstrm_amount"] = "NaN"
            else:
                annual[-1]["thstrm_amount"] = "-900"
            with self.subTest(alteration=alteration), self.assertRaises(collector.DataUnavailable):
                collector.calculate_snapshot(annual, statements(2026, "11012"), statements(2025, "11012"),
                    year=2026, report_code="11012", fs_div="CFS", market_cap=1200, quote_date=TODAY)

    def test_report_after_quote_date_or_wrong_comparable_period_rejected(self):
        prior = statements(2025, "11012")
        prior[0]["bsns_year"] = "2024"
        with self.assertRaises(collector.DataUnavailable):
            collector.calculate_snapshot(statements(2025), statements(2026, "11012"), prior,
                year=2026, report_code="11012", fs_div="CFS", market_cap=1200, quote_date=TODAY)
        rows = statements(2026, "11012")
        rows[0]["rcept_no"] = "20261014000001"
        with self.assertRaises(collector.DataUnavailable):
            collector.receipt_numbers(rows, TODAY)

    def test_preferred_shares_and_period_mismatch_rejected(self):
        self.assertEqual(collector.check_ordinary_only(share_rows(2026, "11012"), date(2026, 6, 30), TODAY), 100)
        with self.assertRaisesRegex(collector.DataUnavailable, "MULTIPLE_SHARE"):
            collector.check_ordinary_only(share_rows(2026, "11012", 10), date(2026, 6, 30), TODAY)
        with self.assertRaises(collector.DataUnavailable):
            collector.check_ordinary_only(share_rows(2026, "11012"), date(2026, 3, 31), TODAY)


class DatabaseValidationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "value.db"
        clock = patch.object(db, "datetime", FixedDatetime)
        clock.start()
        self.addCleanup(clock.stop)

    def save(self, **changes):
        values = snapshot()
        values.update(changes)
        db.upsert_fundamental_snapshot("000001", "Fixture", db_path=self.path, **values)

    def test_verified_snapshot_loads_and_upsert_does_not_duplicate(self):
        self.save()
        self.save()
        self.assertEqual(list(db.load_value_candidates(db_path=self.path)), ["000001"])
        self.assertEqual(db.get_fundamental_status(self.path)["total"], 1)

    def test_freshness_rejects_old_future_unverified_and_nonfinite_values(self):
        base = snapshot()
        for changes in ({"as_of_date": "20261001"}, {"as_of_date": "20261014"},
                        {"financial_period_end": "20250101"}, {"annual_period_end": "20230101"},
                        {"validated": 0}, {"per_ttm": float("inf")}, {"source_note": ""}):
            with self.subTest(changes=changes):
                self.assertFalse(db.fundamental_is_fresh(dict(base, **changes), TODAY))

    def test_failure_invalidates_previous_snapshot_but_preserves_values(self):
        self.save()
        db.mark_fundamental_invalid("000001", "MISSING_ACCOUNT", self.path)
        self.assertEqual(db.load_value_candidates(db_path=self.path), {})
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute("SELECT per_ttm,validated FROM value_fundamental_snapshot").fetchone(), (10, 0))

    def test_legacy_schema_migrates_without_losing_existing_data(self):
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE value_fundamental_snapshot(code TEXT PRIMARY KEY,code_name TEXT,as_of_date TEXT,per_ttm REAL,pbr REAL,gross_margin_pct REAL,asset_turnover REAL,latest_annual_year TEXT,source_note TEXT,updated_at TEXT)")
            con.execute("INSERT INTO value_fundamental_snapshot VALUES ('000001','Old','20261013',10,.6,40,1.2,'2025','manual','20261013000000')")
        db.init_value_quality_tables(self.path)
        self.assertEqual(db.get_fundamental_status(self.path)["total"], 1)
        self.assertEqual(db.load_value_candidates(db_path=self.path), {})

    def test_candidate_filters_are_preserved(self):
        self.save(pbr=1.5)
        self.assertEqual(db.get_fundamental_status(self.path)["fresh"], 1)
        self.assertEqual(db.load_value_candidates(db_path=self.path), {})

    def test_cached_strategy_candidate_is_rejected_when_quote_expires(self):
        import ast
        source = Path(__file__).resolve().parents[1] / "strategy/ValueQualityStrategy.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ValueQualityStrategy")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_is_qualified_candidate")
        ns = {"fundamental_is_fresh": db.fundamental_is_fresh}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "value_strategy", "exec"), ns)
        manager = SimpleNamespace(universe={"000001": snapshot()}, PER_MIN=0, PER_MAX=15,
            PBR_MIN=0, PBR_MAX=1, GROSS_MARGIN_MIN=30, GROSS_MARGIN_MAX=95,
            ASSET_TURNOVER_MIN=1, ASSET_TURNOVER_MAX=10)
        self.assertTrue(ns["_is_qualified_candidate"](manager, "000001"))
        manager.universe["000001"]["as_of_date"] = "20261001"
        self.assertFalse(ns["_is_qualified_candidate"](manager, "000001"))

    def test_invalid_validated_import_never_replaces_existing_good_snapshot(self):
        self.save()
        with self.assertRaises(ValueError):
            self.save(as_of_date="20260101")
        self.assertEqual(db.get_fundamental_status(self.path)["qualified"], 1)

    def test_collection_writes_values_and_records_failure_reason(self):
        caps = pd.DataFrame({"시가총액": [1200, 1200], "상장주식수": [100, 100]}, index=["000001", "000002"])
        companies = {c: {"code_name": c, "corp_code": c} for c in caps.index}
        with patch.object(collector, "collect_company", side_effect=[snapshot(), collector.DataUnavailable("MISSING_ACCOUNT")]), redirect_stdout(StringIO()):
            result = collector.run_collection(Mock(), self.path, TODAY, caps, companies, limit=0)
        self.assertEqual((result["saved"], result["skipped"]), (1, 1))
        self.assertEqual(db.get_fundamental_status(self.path)["qualified"], 1)


class ClientSafetyTest(unittest.TestCase):
    def test_missing_key_and_network_exception_never_expose_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(collector.DartFailure):
                collector.DartClient("", Path(tmp) / "cache")
            key = "A" * 40
            session = Mock()
            session.get.side_effect = collector.requests.ConnectionError("secret=" + key)
            client = collector.DartClient(key, tmp, sleep=0, session=session)
            with self.assertRaises(collector.DartFailure) as caught:
                client.get("company.json", corp_code="12345678")
            self.assertNotIn(key, str(caught.exception))

    def test_quota_stops_instead_of_retrying_every_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Mock()
            session.get.return_value.status_code = 200
            session.get.return_value.json.return_value = {"status": "020", "message": "PRIVATE"}
            client = collector.DartClient("A" * 40, tmp, sleep=0, session=session)
            with self.assertRaisesRegex(collector.DartFailure, "DART_STATUS:020"):
                client.get("company.json", corp_code="12345678")
            session.get.assert_called_once()

    def test_success_cache_has_no_key_and_prevents_duplicate_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Mock()
            session.get.return_value.status_code = 200
            session.get.return_value.json.return_value = {"status": "000", "acc_mt": "12"}
            key = "A" * 40
            client = collector.DartClient(key, tmp, sleep=0, session=session)
            for _ in range(2):
                self.assertEqual(client.get("company.json", corp_code="12345678")["acc_mt"], "12")
            session.get.assert_called_once()
            self.assertNotIn(key, next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))

    def test_company_pipeline_selects_half_year_and_preserves_same_basis(self):
        client = Mock()
        def get(endpoint, **params):
            if endpoint == "company.json":
                return {"acc_mt": "12"}
            return {"list": share_rows(int(params["bsns_year"]), params["reprt_code"])}
        def financial(corp, year, report, basis):
            if report == "11014":
                return None
            return statements(year, report)
        client.get.side_effect = get
        client.statements.side_effect = financial
        result = collector.collect_company(client, {"corp_code": "12345678"}, TODAY, 1200, 100)
        self.assertEqual(result["per_ttm"], 10)
        self.assertTrue(all(call.args[-1] == "CFS" for call in client.statements.call_args_list))
        with self.assertRaisesRegex(collector.DataUnavailable, "SHARE_COUNT_CHANGED"):
            collector.collect_company(client, {"corp_code": "12345678"}, TODAY, 1200, 110)


if __name__ == "__main__":
    unittest.main()
