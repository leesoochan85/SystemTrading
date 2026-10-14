"""Quarterly TTM math, request counts, corrections, retries and recovery."""
from contextlib import closing, redirect_stdout
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from util import value_fundamental_collector as c
from util import value_quality_data as db
from test_value_fundamentals import statements, share_rows, snapshot, TODAY, FixedDatetime


class QuarterlyQualityTest(unittest.TestCase):
    def calculate(self, report="11012", owner=True):
        annual = statements(2025, owner=owner)
        current = statements(2026, report, owner=owner)
        previous = statements(2025, report, owner=owner)
        # Revenue TTM=1200+900-600=1500; gross TTM=480+450-240=690.
        current[0].update(thstrm_amount="1", thstrm_add_amount="900")
        current[1].update(thstrm_amount="2", thstrm_add_amount="450")
        current[2]["thstrm_amount"] = "1400"
        previous[2]["thstrm_amount"] = "600"
        return c.calculate_snapshot(annual, current, previous, year=2026,
            report_code=report, fs_div="CFS" if owner else "OFS", market_cap=1200,
            quote_date=TODAY)

    def test_half_year_quality_uses_ttm_ytd_and_ttm_asset_endpoints(self):
        value = self.calculate()
        self.assertEqual(value["gross_margin_pct"], 46)
        self.assertEqual(value["asset_turnover"], 1.5)
        self.assertEqual(value["per_ttm"], 10)
        self.assertEqual(value["pbr"], .6)
        self.assertEqual(json.loads(value["source_note"])["quality_basis"], c.QUALITY_BASIS)

    def test_q3_and_separate_basis_use_same_quality_math(self):
        for report, owner in (("11014", True), ("11012", False)):
            with self.subTest(report=report, owner=owner):
                self.assertEqual(self.calculate(report, owner)["gross_margin_pct"], 46)

    def test_q1_three_month_fallback_applies_to_revenue_gross_and_profit(self):
        current, prior = statements(2026, "11013"), statements(2025, "11013")
        for rows, values in ((current, (300, 150, 60)), (prior, (200, 80, 40))):
            for index, value in zip((0, 1, -1), values):
                rows[index].update(thstrm_amount=str(value), thstrm_add_amount="")
        result = c.calculate_snapshot(statements(2025), current, prior, year=2026,
            report_code="11013", fs_div="CFS", market_cap=1200, quote_date=TODAY)
        self.assertAlmostEqual(result["gross_margin_pct"], 550 / 1300 * 100)
        self.assertEqual(result["asset_turnover"], 1.3)

    def test_annual_report_is_four_quarters_and_uses_year_start_assets(self):
        annual = statements(2025)
        annual[2]["frmtrm_amount"] = "500"
        result = c.calculate_snapshot(annual, annual, None, year=2025,
            report_code="11011", fs_div="CFS", market_cap=1200, quote_date=date(2026, 4, 1))
        self.assertEqual(result["gross_margin_pct"], 40)
        self.assertEqual(result["asset_turnover"], 1.6)

    def test_missing_ytd_revenue_and_zero_ttm_start_assets_are_rejected(self):
        for missing in (True, False):
            current, prior = statements(2026, "11012"), statements(2025, "11012")
            if missing:
                current[0]["thstrm_add_amount"] = ""
            else:
                prior[2]["thstrm_amount"] = "0"
            with self.subTest(missing=missing), self.assertRaises(c.DataUnavailable):
                c.calculate_snapshot(statements(2025), current, prior, year=2026,
                    report_code="11012", fs_div="CFS", market_cap=1200, quote_date=TODAY)

    def test_gross_fallback_uses_cost_ytd_in_all_three_periods(self):
        annual, current, previous = statements(2025), statements(2026, "11012"), statements(2025, "11012")
        for rows, total, ytd in ((annual, 720, ""), (current, 999, 300), (previous, 999, 360)):
            rows[1].update(account_id="ifrs-full_CostOfSales", account_nm="매출원가",
                           thstrm_amount=str(total), thstrm_add_amount=str(ytd))
        result = c.calculate_snapshot(annual, current, previous, year=2026,
            report_code="11012", fs_div="CFS", market_cap=1200, quote_date=TODAY)
        self.assertEqual(result["gross_margin_pct"], 45)

    def test_annual_basis_snapshot_is_excluded_until_recalculated(self):
        result = snapshot()
        source = json.loads(result["source_note"])
        source["quality_basis"] = "annual; revenue/mean(beginning,end assets)"
        result["source_note"] = json.dumps(source)
        self.assertFalse(db.fundamental_is_fresh(result, TODAY))

    def test_quarter_quality_does_not_require_annual_beginning_assets(self):
        annual = statements(2025)
        annual[2]["frmtrm_amount"] = ""
        result = c.calculate_snapshot(annual, statements(2026, "11012"), statements(2025, "11012"),
            year=2026, report_code="11012", fs_div="CFS", market_cap=1200, quote_date=TODAY)
        self.assertEqual(result["asset_turnover"], 1.2)


class CollectorClientTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Mock()
        self.client = c.DartClient("A" * 40, self.temp.name, sleep=0, session=self.session)
        self.output = redirect_stdout(StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        p = patch.object(c.time, "sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def response(self, payload, status=200):
        return Mock(status_code=status, json=Mock(return_value=payload))

    def filings(self, receipt="20261013000001", page=1, pages=1, corp="12345678"):
        return self.response(dict(status="000", total_page=pages,
            list=[dict(corp_code=corp, rcept_no=receipt)]))

    def test_success_report_cache_survives_24_hours_but_expires_in_seven_days(self):
        self.session.get.return_value = self.response(dict(status="000", list=[1]))
        for stamp in (1000000, 1000000 + 2 * 86400, 1000000 + 8 * 86400):
            with patch.object(c.time, "time", return_value=stamp):
                self.client.get("fnlttSinglAcntAll.json", corp_code="12345678")
            # Filesystem mtime is set explicitly for the mocked clock.
            if stamp == 1000000:
                path = next(Path(self.temp.name).glob("*.json"))
                os.utime(path, (stamp, stamp))
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(self.client.cache_hits, 1)

    def test_no_data_cached_for_six_hours_and_force_bypasses_it(self):
        self.session.get.return_value = self.response(dict(status="013"))
        self.assertIsNone(self.client.get("company.json", corp_code="12345678"))
        self.assertIsNone(self.client.get("company.json", corp_code="12345678"))
        self.session.get.assert_called_once()
        path = next(Path(self.temp.name).glob("*.json"))
        os.utime(path, (0, 0))
        self.client.get("company.json", corp_code="12345678")
        self.client.force_refresh = True
        self.client.get("company.json", corp_code="12345678")
        self.assertEqual(self.session.get.call_count, 3)

    def test_network_recovers_on_third_attempt_and_never_prints_secret(self):
        self.session.get.side_effect = [c.requests.ConnectionError("SECRET"),
            c.requests.Timeout("SECRET"), self.response(dict(status="000"))]
        self.client.get("company.json")
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.client.api_requests, 3)

    def test_exhausted_network_and_quota_fail_safely_without_error_cache(self):
        self.session.get.side_effect = c.requests.ConnectionError("SECRET")
        with self.assertRaisesRegex(c.DartFailure, "DART_NETWORK:ConnectionError") as caught:
            self.client.get("company.json")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(self.session.get.call_count, 3)
        self.session.get.reset_mock(side_effect=True)
        self.session.get.return_value = self.response(dict(status="020"))
        with self.assertRaisesRegex(c.DartFailure, "DART_STATUS:020"):
            self.client.get("company.json")
        self.session.get.assert_called_once()
        self.assertFalse(list(Path(self.temp.name).glob("*.json")))

    def test_http_503_retried_but_http_429_stops(self):
        self.session.get.side_effect = [self.response({}, 503), self.response(dict(status="000"))]
        self.client.get("company.json")
        self.assertEqual(self.session.get.call_count, 2)
        self.session.get.reset_mock(side_effect=True)
        self.session.get.return_value = self.response({}, 429)
        with self.assertRaisesRegex(c.DartFailure, "DART_HTTP:429"):
            self.client.get("stockTotqySttus.json")
        self.session.get.assert_called_once()

    def test_new_correction_invalidates_only_changed_company_cache(self):
        self.session.get.return_value = self.response(dict(status="000", list=["old"]))
        self.client.get("fnlttSinglAcntAll.json", corp_code="12345678")
        self.client.get("fnlttSinglAcntAll.json", corp_code="87654321")
        self.session.get.return_value = self.filings()
        self.client.prepare_filings(TODAY)
        self.session.get.return_value = self.response(dict(status="000", list=["new"]))
        self.assertEqual(self.client.get("fnlttSinglAcntAll.json", corp_code="12345678")["list"], ["new"])
        self.assertEqual(self.client.get("fnlttSinglAcntAll.json", corp_code="87654321")["list"], ["old"])
        self.assertEqual(self.session.get.call_count, 4)

    def test_paginated_filings_persist_only_after_all_pages_succeed(self):
        self.session.get.side_effect = [self.filings(pages=2), c.requests.ConnectionError()]
        self.client.retries = 0
        with self.assertRaises(c.DartFailure):
            self.client.prepare_filings(TODAY)
        self.assertFalse((Path(self.temp.name) / "filing_state.json").exists())
        self.session.get.side_effect = [self.filings(corp="87654321", pages=2)]
        self.client.prepare_filings(TODAY)
        self.assertEqual(set(self.client.generations), {"12345678", "87654321"})

    def test_old_receipt_does_not_undo_newer_correction_and_watermark_overlaps(self):
        self.session.get.return_value = self.filings()
        self.client.prepare_filings(TODAY)
        self.session.get.return_value = self.filings(receipt="20261012000001")
        self.client.prepare_filings(TODAY + timedelta(days=1))
        params = self.session.get.call_args.kwargs["params"]
        self.assertEqual(params["bgn_de"], "20261012")
        self.assertEqual(params["last_reprt_at"], "N")
        self.assertEqual(self.client.generation("12345678"), "20261013000001")


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "value.db"
        self.caps = pd.DataFrame({"시가총액": [1200, 1200], "상장주식수": [100, 100]},
                                 index=["000001", "000002"])
        self.companies = {code: dict(corp_code=code.zfill(8), code_name=code) for code in self.caps.index}
        self.client = c.DartClient("A" * 40, Path(self.temp.name) / "cache", sleep=0)
        p = patch.object(db, "datetime", FixedDatetime)
        p.start()
        self.addCleanup(p.stop)
        self.output = redirect_stdout(StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def run_collection(self, **kwargs):
        return c.run_collection(self.client, self.path, TODAY, self.caps, self.companies, limit=0, **kwargs)

    def test_resume_retries_failed_company_skips_success_and_does_not_duplicate(self):
        with patch.object(c, "collect_company", side_effect=[snapshot(), c.DartFailure("DART_NETWORK:ConnectionError")]):
            first = self.run_collection()
        self.assertIn("stopped", first)
        with patch.object(c, "collect_company", return_value=snapshot()) as collect:
            second = self.run_collection()
        self.assertEqual((second["saved"], second["resumed"]), (1, 1))
        self.assertEqual(collect.call_args.args[1]["code_name"], "000002")
        self.assertEqual(db.get_fundamental_status(self.path)["total"], 2)

    def test_transport_failure_preserves_previous_good_snapshot(self):
        db.upsert_fundamental_snapshot("000001", "old", db_path=self.path, **snapshot())
        with patch.object(c, "collect_company", side_effect=c.DartFailure("DART_NETWORK:ConnectionError")):
            self.run_collection()
        self.assertEqual(db.get_fundamental_status(self.path)["fresh"], 1)

    def test_market_change_correction_force_and_new_quote_invalidate_checkpoint(self):
        with patch.object(c, "collect_company", return_value=snapshot()) as collect:
            self.run_collection()
            self.run_collection()
            self.assertEqual(collect.call_count, 2)
            self.caps.loc["000001", "시가총액"] = 1000
            self.run_collection()
            self.assertEqual(collect.call_count, 3)
            self.client.generations["00000001"] = "20261013000001"
            self.run_collection()
            self.assertEqual(collect.call_count, 4)
            self.run_collection(resume=False)
            self.assertEqual(collect.call_count, 6)
            with patch.object(db, "fundamental_is_fresh", return_value=True), patch.object(c, "fundamental_is_fresh", return_value=True):
                c.run_collection(self.client, self.path, TODAY + timedelta(days=1), self.caps, self.companies, limit=0)
            self.assertEqual(collect.call_count, 8)

    def test_data_exclusion_is_checkpointed_and_retried_after_six_hours(self):
        with patch.object(c, "collect_company", side_effect=c.DataUnavailable("MISSING_AMOUNT")) as collect:
            self.run_collection()
            self.assertEqual(self.run_collection()["resumed"], 2)
            with closing(sqlite3.connect(self.path)) as con, con:
                con.execute("UPDATE value_collection_checkpoint SET completed_at=0")
            self.run_collection()
            self.assertEqual(collect.call_count, 4)

    def test_next_day_market_repricing_uses_cached_financials_and_changes_candidates(self):
        self.caps = self.caps.iloc[:1].copy()
        session = Mock()
        def reply(url, params, **kwargs):
            endpoint = url.rsplit("/", 1)[-1]
            if endpoint == "company.json":
                payload = dict(status="000", acc_mt="12")
            elif endpoint == "list.json":
                payload = dict(status="013")
            else:
                year, report = int(params["bsns_year"]), params["reprt_code"]
                if endpoint == "fnlttSinglAcntAll.json":
                    payload = (dict(status="013") if report == "11014" else
                               dict(status="000", list=statements(year, report)))
                else:
                    payload = dict(status="000", list=share_rows(year, report))
            return Mock(status_code=200, json=Mock(return_value=payload))
        session.get.side_effect = reply
        self.client.session = session
        self.client.prepare_filings(TODAY)
        self.run_collection()
        self.assertEqual(session.get.call_count, 9)
        self.assertEqual(db.get_fundamental_status(self.path)["qualified"], 1)
        # Price change alone pushes PER above 15 and PBR above 1.
        self.caps.loc["000001", "시가총액"] = 2400
        self.client.prepare_filings(TODAY + timedelta(days=1))
        class NextDay(FixedDatetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 14, 8)
        with patch.object(db, "datetime", NextDay):
            c.run_collection(self.client, self.path, TODAY + timedelta(days=1),
                             self.caps, self.companies, limit=0)
            self.assertEqual(db.get_fundamental_status(self.path)["fresh"], 1)
        self.assertEqual(session.get.call_count, 10)  # One market-wide filings request; no financial requests.
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT per_ttm,pbr FROM value_fundamental_snapshot").fetchone(), (20, 1.2))
        self.assertEqual(db.get_fundamental_status(self.path)["qualified"], 0)


class FinancialConnectionLifetimeTest(unittest.TestCase):
    def test_commit_rollback_and_explicit_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "value.db"
            with db._connect(path) as con:
                con.execute("CREATE TABLE lifetime(value INTEGER)")
                con.execute("INSERT INTO lifetime VALUES (1)")
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with db._connect(path) as failed:
                    failed.execute("INSERT INTO lifetime VALUES (2)")
                    raise RuntimeError("rollback")
            with self.assertRaises(sqlite3.ProgrammingError):
                failed.execute("SELECT 1")
            with db._connect(path) as read:
                self.assertEqual(read.execute("SELECT value FROM lifetime").fetchall()[0][0], 1)

    def test_reported_resume_test_closes_every_handle_without_garbage_collection(self):
        # Strong references prevent GC from hiding unclosed connections on Linux.
        # Windows cannot delete the temporary file while any such handle is open.
        connect = sqlite3.connect
        connections = []
        def tracked(*args, **kwargs):
            con = connect(*args, **kwargs)
            connections.append(con)
            return con
        outcome = unittest.TestResult()
        with patch.object(sqlite3, "connect", side_effect=tracked):
            ResumeTest("test_data_exclusion_is_checkpointed_and_retried_after_six_hours").run(outcome)
        try:
            self.assertTrue(outcome.wasSuccessful(), outcome.errors + outcome.failures)
            self.assertGreater(len(connections), 0)
            for con in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    con.execute("SELECT 1")
        finally:
            for con in connections:
                con.close()


if __name__ == "__main__":
    unittest.main()
