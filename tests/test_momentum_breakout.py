from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from paper_fixture import config, history, market_db
from util.momentum_breakout import analyze_bars, load_momentum_metrics, rank_and_filter, OpeningRange, momentum_exit


class MomentumRulesTest(unittest.TestCase):
    def setUp(self):
        self.settings = config()
        self.rules = self.settings["strategy"]

    def test_author_adr_and_pattern_use_completed_bars(self):
        rows = history()
        metric = analyze_bars(rows, self.rules)
        # 15 wide days plus 5 tight days, each H/L ratio is averaged.
        expected = 100 * ((15 * 16500 / 15500 + 5 * 16300 / 15800) / 20 - 1)
        self.assertAlmostEqual(metric["adr20_pct"], expected)
        self.assertAlmostEqual(metric["tr_ratio"], 0.5)
        self.assertEqual(metric["base_high"], 16500)
        self.assertGreater(metric["prior_move_pct"], 30)
        self.assertGreater(metric["low_recent"], metric["low_previous"])

    def test_rank_is_calculated_before_adr_filter(self):
        metric = analyze_bars(history(), self.rules)
        universe = {f"{i:06d}": dict(metric, low126_score=200-i) for i in range(1, 101)}
        universe["000001"]["adr20_pct"] = 0
        ranked, counts = rank_and_filter(universe, self.rules)
        self.assertFalse(ranked["000001"]["eligible"])
        self.assertTrue(ranked["000002"]["eligible"])
        self.assertEqual(ranked["000003"]["rejection_reason"], "MOMENTUM_RANK")
        self.assertEqual(counts["top_ranked"], 2)
        self.assertEqual(counts["qualified"], 1)

    def test_pdf_mode_uses_union_of_actual_return_ranks(self):
        metric = analyze_bars(history(), self.rules)
        universe = {f"{i:06d}": dict(metric, low126_score=200-i,
            return21_pct=200-i, return63_pct=i, return126_pct=i) for i in range(1, 101)}
        video, _ = rank_and_filter(universe, self.rules)
        pdf, counts = rank_and_filter(universe, dict(self.rules, momentum_mode="pdf_returns"))
        self.assertFalse(video["000100"]["eligible"])
        self.assertTrue(pdf["000100"]["eligible"])
        self.assertEqual(counts["top_ranked"], 4)

    def test_loader_excludes_same_day_stale_and_short_histories(self):
        rows = history()
        today = dict(rows[-1], date="20260930", high=999999, close=999000)
        with tempfile.TemporaryDirectory() as directory:
            path = market_db(Path(directory) / "market.db", {
                "000001": rows + [today], "000002": rows[:-1], "000003": rows[-60:]})
            metrics, stats = load_momentum_metrics(["000001", "000002", "000003"], path, "20260930", self.rules)
        self.assertEqual(set(metrics), {"000001"})
        self.assertEqual(metrics["000001"]["base_high"], 16500)
        self.assertEqual(metrics["000001"]["atr_date"], "20260929")
        self.assertEqual(stats["excluded_history"], 2)

    def test_invalid_price_and_duplicate_dates_are_rejected(self):
        rows = history()
        bad = deepcopy(rows)
        bad[-1]["low"] = 0
        self.assertIsNone(analyze_bars(bad, self.rules))
        bad = deepcopy(rows)
        bad[-1]["date"] = bad[-2]["date"]
        self.assertIsNone(analyze_bars(bad, self.rules))
        for field, value in (("volume", None), ("volume", "invalid"), ("date", "20260999")):
            bad = deepcopy(rows)
            bad[-1][field] = value
            self.assertIsNone(analyze_bars(bad, self.rules))

    def test_opening_range_freezes_and_refuses_missing_or_reordered_ticks(self):
        opening = OpeningRange(5)
        opening.registered(datetime(2026, 9, 30, 8, 59))
        cumulative = 0
        def observe(time, price):
            nonlocal cumulative
            cumulative += 100
            now = datetime.strptime("20260930" + time, "%Y%m%d%H%M%S")
            return opening.observe("000001", {"체결시간": time, "현재가": price,
                "누적거래량": cumulative, "거래량": 100}, now)
        self.assertEqual(observe("090000", 16200)[1], "WAIT_OPENING_RANGE")
        observe("090400", 16400)
        self.assertEqual(observe("090500", 16600), (16400, None))
        self.assertEqual(observe("090600", 16800), (16400, None))
        self.assertEqual(observe("090550", 20000)[1], "OUT_OF_ORDER_TICK")
        self.assertEqual(opening.observe("000002", {"現在가": 17000}, datetime(2026, 9, 30, 9, 6))[1], "MISSING_TRADE_TIME")
        self.assertEqual(opening.observe("000003", {"체결시간": "090600", "현재가": 17000,
            "누적거래량": 100, "거래량": 100}, datetime(2026, 9, 30, 9, 6))[1], "OPENING_RANGE_MISSING")

    def test_missing_volume_or_unverified_first_trade_blocks_the_day(self):
        for tick, reason in (({"체결시간": "090000", "현재가": 16200}, "OPENING_VOLUME_MISSING"),
            ({"체결시간": "090000", "현재가": 16200, "누적거래량": 500, "거래량": 100}, "OPENING_START_UNVERIFIED")):
            with self.subTest(reason=reason):
                opening = OpeningRange()
                opening.registered(datetime(2026, 9, 30, 8, 59))
                self.assertEqual(opening.observe("000001", tick, datetime(2026, 9, 30, 9))[1], reason)
                self.assertEqual(opening.observe("000001", {"체결시간": "090500", "현재가": 16600,
                    "누적거래량": 600, "거래량": 100}, datetime(2026, 9, 30, 9, 5))[1], reason)

    def test_duplicate_same_second_and_boundary_gap_are_distinguished(self):
        opening = OpeningRange()
        opening.registered(datetime(2026, 9, 30, 8, 59))
        now = datetime(2026, 9, 30, 9)
        tick = {"체결시간": "090000", "현재가": 16200, "누적거래량": 100, "거래량": -100}
        self.assertEqual(opening.observe("000001", tick, now)[1], "WAIT_OPENING_RANGE")
        self.assertEqual(opening.observe("000001", tick, now)[1], "DUPLICATE_TICK")
        # Multiple distinct executions in the same second are valid.
        self.assertEqual(opening.observe("000001", dict(tick, 현재가=16400, 누적거래량=200), now)[1], "WAIT_OPENING_RANGE")
        self.assertEqual(opening.observe("000001", {"체결시간": "090500", "현재가": 16600,
            "누적거래량": 400, "거래량": 100}, datetime(2026, 9, 30, 9, 5))[1], "OPENING_VOLUME_GAP")

    def test_reregistration_discards_range_and_next_day_can_recover(self):
        opening = OpeningRange()
        opening.registered(datetime(2026, 9, 30, 8, 59))
        tick = {"체결시간": "090000", "현재가": 16200, "누적거래량": 100, "거래량": 100}
        opening.observe("000001", tick, datetime(2026, 9, 30, 9))
        opening.registered(datetime(2026, 9, 30, 9, 1))
        self.assertEqual(opening.observe("000001", dict(tick, 체결시간="090500"), datetime(2026, 9, 30, 9, 5))[1], "OPENING_RANGE_MISSING")
        self.assertEqual(opening.observe("000001", tick, datetime(2026, 10, 1, 9))[1], "WAIT_OPENING_RANGE")

    def test_late_registration_and_stale_tick_do_not_reconstruct_range(self):
        opening = OpeningRange()
        opening.registered(datetime(2026, 9, 30, 9, 1))
        self.assertEqual(opening.observe("000001", {"체결시간": "090200", "현재가": 16500}, datetime(2026, 9, 30, 9, 2))[1], "OPENING_RANGE_MISSING")
        self.assertEqual(opening.observe("000001", {"체결시간": "090200", "현재가": 16500}, datetime(2026, 9, 30, 9, 5))[1], "STALE_OR_OUTSIDE_SESSION")

    def test_partial_waits_for_sessions_and_net_profit_then_ma_exits(self):
        position = dict(state={"stop_price": 9500, "partial_done": False}, entry_price=10005,
            quantity=99, initial_quantity=99, entry_at="20260930100000")
        costs = self.settings["account"]
        self.assertIsNone(momentum_exit(position, 11000, {"elapsed_sessions": 3}, self.rules, costs))
        self.assertIsNone(momentum_exit(position, 10010, {"elapsed_sessions": 4}, self.rules, costs))
        signal = momentum_exit(position, 11000, {"elapsed_sessions": 4}, self.rules, costs)
        self.assertEqual(signal, {"reason_code": "TIME_PARTIAL_EXIT", "quantity": 33, "partial": True})
        self.assertFalse(position["state"]["partial_done"])
        position.update(quantity=66, state={"stop_price": 10038, "partial_done": True})
        context = {"elapsed_sessions": 5, "ma_date": "20261006", "close": 10700, "ma": 10800}
        self.assertEqual(momentum_exit(position, 10500, context, self.rules, costs)["reason_code"], "MA_CLOSE_EXIT")
        self.assertEqual(momentum_exit(position, 9000, context, self.rules, costs)["reason_code"], "DAY_LOW_STOP")

    def test_one_share_can_exit_on_ma_after_partial_day(self):
        position = dict(state={"stop_price": 9000, "partial_done": False}, entry_price=10000,
            quantity=1, initial_quantity=1, entry_at="20260930100000")
        context = {"elapsed_sessions": 4, "ma_date": "20261006", "close": 11000, "ma": 11100}
        self.assertEqual(momentum_exit(position, 11000, context, self.rules, self.settings["account"]),
            {"reason_code": "MA_CLOSE_EXIT", "quantity": 1})
