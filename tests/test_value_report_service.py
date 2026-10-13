"""Report delivery behavior without HTTP, Telegram or broker orders."""
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from paper_fixture import qt_stubs
qt_stubs()
from strategy.StrategyManager import StrategyManager
from strategy.value_report_service import ValueReportService, format_report_message


class ValueReportServiceTest(unittest.TestCase):
    def setUp(self):
        self.manager = StrategyManager.__new__(StrategyManager)
        self.manager.kiwoom = SimpleNamespace(balance={
            '1': {'보유수량': 3}, '000002': {'보유수량': 0},
            '000003': {'보유수량': 2}}, send_order=Mock())
        self.manager.get_code_name = Mock(return_value='테스트종목')
        self.manager.last_value_report_check_at = 0
        self.manager.value_report_check_interval = 1800
        self.manager.last_value_report_error_log_at = 0
        self.manager.value_report_error_log_interval = 3600
        self.clock = Mock(return_value=4000)
        self.lookup = Mock(side_effect=lambda code: 'ValueQualityStrategy' if code in ('000001', '000002') else 'MomentumContractionStrategy')
        self.scan = Mock(return_value={'reports': [self.report(11), self.report(12)], 'last_scanned_report_id': 12})
        self.cursor = Mock(return_value=10)
        self.write = Mock()
        self.sent = Mock(return_value=False)
        self.record = Mock()
        self.notify = Mock(return_value={'ok': True})
        self.service = ValueReportService(self.manager, position_strategy_lookup=self.lookup,
            scan_reports=self.scan, read_cursor=self.cursor, write_cursor=self.write,
            is_sent=self.sent, record_sent=self.record, send_message=self.notify, clock=self.clock)
        self.manager._value_reports = Mock(return_value=self.service)

    @staticmethod
    def report(number):
        return {'report_key': str(number), 'code': '1', 'title': '성장 전망', 'broker': '기관',
                'analyst': '작성자', 'report_date': '2026-10-13', 'opinion': 'BUY',
                'target_price': 15000, 'url': 'https://example.test/report/' + str(number)}

    def test_filter_interval_one_report_and_record_after_success_are_preserved(self):
        self.assertEqual(self.manager.get_value_quality_holding_codes(), ['000001'])
        order = Mock()
        for name, callback in [('cursor', self.cursor), ('scan', self.scan), ('write', self.write),
                               ('notify', self.notify), ('record', self.record)]:
            order.attach_mock(callback, name)
        self.manager.send_one_value_quality_report_if_needed()
        self.scan.assert_called_once_with(target_codes=['000001'], last_scanned_report_id=10,
            max_scan_count=120, consecutive_miss_limit=15)
        self.write.assert_called_once_with(12)
        self.notify.assert_called_once_with('[저평가 우량주 리포트]\n\n테스트종목(000001)\n성장 전망\n발행기관: 기관\n작성자: 작성자\n발표일: 2026-10-13\n투자의견: BUY\n목표주가: 15000\n\nhttps://example.test/report/11')
        self.assertEqual(self.record.call_args.kwargs['report_key'], '11')
        self.assertLess([c[0] for c in order.mock_calls].index('notify'), [c[0] for c in order.mock_calls].index('record'))
        self.clock.return_value = 4100
        self.manager.send_one_value_quality_report_if_needed()
        self.assertEqual(self.scan.call_count, 1)
        self.manager.kiwoom.send_order.assert_not_called()

    def test_force_bypasses_interval_and_already_sent_report_is_skipped(self):
        self.manager.last_value_report_check_at = 3999
        self.sent.side_effect = lambda key: key == '11'
        self.manager.send_one_value_quality_report_if_needed(force=True)
        self.assertEqual(self.record.call_args.kwargs['report_key'], '12')
        self.assertEqual(self.notify.call_count, 1)

    def test_failed_notification_does_not_mark_report_sent(self):
        self.notify.return_value = None
        self.manager.send_one_value_quality_report_if_needed()
        self.notify.assert_called_once()
        self.record.assert_not_called()
        self.write.assert_called_once_with(12)

    def test_no_holdings_skip_network_and_no_target_still_advances_scan_cursor(self):
        self.manager.kiwoom.balance = {}
        self.manager.send_one_value_quality_report_if_needed()
        self.scan.assert_not_called()
        self.manager.kiwoom.balance = {'1': {'보유수량': 3}}
        self.scan.return_value = {'reports': [], 'last_scanned_report_id': 20}
        self.manager.send_one_value_quality_report_if_needed(force=True)
        self.write.assert_called_once_with(20)
        self.notify.assert_not_called()

    def test_scan_failure_does_not_escape_and_error_console_is_throttled(self):
        self.scan.side_effect = RuntimeError('fixture failure')
        console = StringIO()
        with redirect_stdout(console):
            self.manager.send_one_value_quality_report_if_needed(force=True)
            self.clock.return_value = 4100
            self.manager.send_one_value_quality_report_if_needed(force=True)
        self.assertEqual(console.getvalue().count('fixture failure'), 1)
        self.assertEqual(self.manager.last_value_report_error_log_at, 4000)
        self.record.assert_not_called()

    def test_message_defaults_match_existing_format(self):
        self.assertEqual(format_report_message({'code': '1', 'target_price': '0', 'url': 'link'}, '테스트종목'),
            '[저평가 우량주 리포트]\n\n테스트종목(000001)\n기업 리포트\n발행기관: -\n작성자: -\n발표일: -\n투자의견: -\n목표주가: -\n\nlink')
