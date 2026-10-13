"""Telegram status presentation, independent of Qt and broker execution."""
import time
from datetime import datetime

from util.db_helper import (
    get_strategy_position_counts, get_today_order_count, get_today_realized_pnl,
)
from util.value_quality_data import get_last_sent_value_report_at


class StatusMessageBuilder:
    """Read account state and format /status without sending messages or orders."""

    def __init__(self, manager):
        self.manager = manager

    def build(self):
        now_text = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        if self.manager.last_order_sync_at:
            balance_sync_text = datetime.fromtimestamp(
                self.manager.last_order_sync_at
            ).strftime("%Y-%m-%d %H:%M:%S")
        else:
            balance_sync_text = "아직 동기화되지 않음"

        total_assets = self.manager.calculate_total_assets()
        reserved = self.manager.get_reserved_buy_amount()
        available_cash = self.manager.get_available_buy_cash()
        max_position_amount = int(
            total_assets * self.manager.MAX_POSITION_RATIO
        )

        lines = [
            "[자동매매 상태]",
            f"조회 시각: {now_text}",
            f"잔고/미체결 기준: {balance_sync_text}",
            f"총자산: {total_assets:,}원",
            f"예수금: {self.manager.cash_deposit:,}원",
            f"D+2 추정예수금: {self.manager.d2_estimated_deposit:,}원",
            f"키움 주문가능금액: {self.manager.deposit:,}원",
            f"Manager 매수 예약금: {reserved:,}원",
            f"실제 신규매수 가용현금: {available_cash:,}원",
            (
                f"종목당 최대 비중: "
                f"{self.manager.MAX_POSITION_RATIO * 100:.0f}% "
                f"({max_position_amount:,}원)"
            ),
            (
                f"계좌 보유/매수대기: "
                f"{self.manager.get_account_position_count()}/"
                f"{self.manager.MAX_ACCOUNT_POSITIONS}종목"
            ),
            "",
        ]

        self._append_balance_status(lines)
        self._append_strategy_status(lines)
        self._append_pending_order_status(lines)
        self._append_today_summary(lines)

        return "\n".join(lines)

    def _append_balance_status(self, lines):
        holdings = []

        for code, info in self.manager.kiwoom.balance.items():
            quantity = int(info.get("보유수량", 0) or 0)
            if quantity > 0:
                holdings.append((code, info))

        lines.append(
            f"[현재 보유 종목] {len(holdings)}개"
        )

        if not holdings:
            lines.append("- 없음")
            lines.append("")
            return

        for code, info in holdings:
            code = str(code).strip().zfill(6)
            code_name = (
                info.get("종목명")
                or self.manager.get_code_name(code)
            )
            quantity = int(
                info.get("보유수량", 0) or 0
            )
            buy_price = int(
                info.get("매입가", 0)
                or info.get("매입단가", 0)
                or 0
            )

            rt = self.manager.kiwoom.universe_realtime_transaction_info.get(
                code,
                {},
            )
            current_price = int(
                rt.get("현재가")
                or info.get("현재가", 0)
                or 0
            )

            if buy_price > 0 and current_price > 0:
                return_rate = (
                    (current_price - buy_price)
                    / buy_price
                    * 100
                )
            else:
                return_rate = float(
                    info.get("수익률", 0) or 0
                )

            lines.append(
                f"- {code_name}({code}) {quantity}주 | "
                f"매입 {buy_price:,} / 현재 {current_price:,} | "
                f"{return_rate:+.2f}%"
            )

        lines.append("")

    def _append_strategy_status(self, lines):
        counts = get_strategy_position_counts()

        lines.append("[전략별 보유 현황]")
        for strategy in self.manager.strategies:
            count = counts.get(
                strategy.strategy_name,
                0,
            )
            lines.append(
                f"- {strategy.strategy_name}: {count}개"
            )
        lines.append("")

    def _append_pending_order_status(self, lines):
        pending = []

        for code, info in self.manager.kiwoom.order.items():
            remain = int(
                info.get("미체결수량", 0) or 0
            )
            if remain > 0:
                pending.append(
                    (str(code).zfill(6), info)
                )

        lines.append(f"[미체결 주문] {len(pending)}건")

        if not pending:
            lines.append("- 없음")
            lines.append("")
            return

        for code, info in pending:
            code_name = (
                info.get("종목명")
                or self.manager.get_code_name(code)
            )
            order_type = info.get("주문구분", "-")
            remain = int(
                info.get("미체결수량", 0) or 0
            )
            order_price = int(
                info.get("주문가격", 0) or 0
            )
            elapsed = self._get_order_elapsed_text(info)

            lines.append(
                f"- {code_name}({code}) {order_type} "
                f"{remain}주 {order_price:,}원 | "
                f"경과 {elapsed}"
            )

        lines.append("")

    def _get_order_elapsed_text(self, order_info):
        order_time = order_info.get("order_time")

        if order_time:
            try:
                elapsed_seconds = max(
                    0,
                    int(
                        time.time()
                        - float(order_time)
                    ),
                )
                minutes, seconds = divmod(
                    elapsed_seconds,
                    60,
                )
                return f"{minutes}분 {seconds}초"
            except Exception:
                pass

        broker_time = str(
            order_info.get("시간", "") or ""
        ).strip()

        if len(broker_time) == 6 and broker_time.isdigit():
            try:
                today = datetime.now().strftime(
                    "%Y%m%d"
                )
                ordered_at = datetime.strptime(
                    today + broker_time,
                    "%Y%m%d%H%M%S",
                )
                elapsed_seconds = max(
                    0,
                    int(
                        (
                            datetime.now()
                            - ordered_at
                        ).total_seconds()
                    ),
                )
                minutes, seconds = divmod(
                    elapsed_seconds,
                    60,
                )
                return f"{minutes}분 {seconds}초"

            except ValueError:
                pass

        return "확인 불가"

    def _append_today_summary(self, lines):
        order_count = get_today_order_count()

        (
            gross_realized_pnl,
            net_realized_pnl,
            missing_buy_price_count,
            missing_cost_count,
        ) = get_today_realized_pnl()

        last_report_sent_at = (
            get_last_sent_value_report_at()
        )

        if last_report_sent_at:
            last_report_text = (
                f"{last_report_sent_at[0:4]}-"
                f"{last_report_sent_at[4:6]}-"
                f"{last_report_sent_at[6:8]} "
                f"{last_report_sent_at[8:10]}:"
                f"{last_report_sent_at[10:12]}:"
                f"{last_report_sent_at[12:14]}"
            )
        else:
            last_report_text = "전송 기록 없음"

        lines.append("[오늘 요약]")
        lines.append(
            "- 매도 체결 총손익(수수료·세금 전): "
            f"{gross_realized_pnl:+,}원"
        )
        lines.append(
            "- 매도 체결 순손익(비용 확인 거래 합산): "
            f"{net_realized_pnl:+,}원"
        )
        lines.append(f"- 주문 횟수: {order_count}건")
        lines.append(
            "- 마지막 저평가 리포트 전송: "
            f"{last_report_text}"
        )

        if missing_buy_price_count > 0:
            lines.append(
                "- 총손익 계산 제외 매도 거래: "
                f"{missing_buy_price_count}건 "
                "(매입가 기록 없음)"
            )

        if missing_cost_count > 0:
            lines.append(
                "- 순손익 계산 보류 거래: "
                f"{missing_cost_count}건 "
                "(수수료·세금 수신 대기 또는 미확인)"
            )
