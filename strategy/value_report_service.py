"""ValueQuality holding report delivery, independent of Qt and order execution."""

def format_report_message(target, code_name):
    code = str(target.get("code", "")).zfill(6)
    title = (
        target.get("title")
        or "기업 리포트"
    )
    broker = (
        target.get("broker")
        or "-"
    )
    analyst = (
        target.get("analyst")
        or "-"
    )
    report_date = (
        target.get("report_date")
        or "-"
    )
    opinion = (
        target.get("opinion")
        or "-"
    )
    target_price = (
        target.get("target_price")
    )
    url = target["url"]

    if target_price not in (
        None,
        "",
        0,
        "0",
    ):
        target_price_text = str(
            target_price
        )
    else:
        target_price_text = "-"

    message = (
        "[저평가 우량주 리포트]\n\n"
        f"{code_name}({code})\n"
        f"{title}\n"
        f"발행기관: {broker}\n"
        f"작성자: {analyst}\n"
        f"발표일: {report_date}\n"
        f"투자의견: {opinion}\n"
        f"목표주가: {target_price_text}\n\n"
        f"{url}"
    )

    return message


class ValueReportService:
    """Coordinate one report at a time while retaining manager-owned timing state."""

    def __init__(self, manager, *, position_strategy_lookup, scan_reports, read_cursor,
                 write_cursor, is_sent, record_sent, send_message, clock):
        self.manager = manager
        self.position_strategy_lookup = position_strategy_lookup
        self.scan_reports = scan_reports
        self.read_cursor = read_cursor
        self.write_cursor = write_cursor
        self.is_sent = is_sent
        self.record_sent = record_sent
        self.send_message = send_message
        self.clock = clock

    def get_holding_codes(self):
        codes = []

        for raw_code, balance_info in self.manager.kiwoom.balance.items():
            code = str(raw_code).strip().zfill(6)
            quantity = int(
                balance_info.get("보유수량", 0)
                or 0
            )

            if quantity <= 0:
                continue

            if (
                self.position_strategy_lookup(code)
                != "ValueQualityStrategy"
            ):
                continue

            codes.append(code)

        return sorted(set(codes))


    def send_if_due(
        self,
        force=False,
    ):
        """
        기존 무관한 경제뉴스 대신,
        현재 ValueQualityStrategy로 실제 보유 중인 종목의
        한경 컨센서스 기업 리포트만 Telegram으로 전송한다.

        - 30분마다 확인
        - 마지막 확인 리포트 ID 이후의 공개 상세페이지만 검사
        - 한 번에 최대 1건 전송
        - value_quality.db에서 중복 전송 방지
        """
        now = self.clock()

        if (
            not force
            and now - self.manager.last_value_report_check_at
            < self.manager.value_report_check_interval
        ):
            return

        self.manager.last_value_report_check_at = now

        holding_codes = (
            self.manager.get_value_quality_holding_codes()
        )

        if not holding_codes:
            return

        try:
            last_scanned_report_id = (
                self.read_cursor()
            )

            scan_result = self.scan_reports(
                target_codes=holding_codes,
                last_scanned_report_id=last_scanned_report_id,
                max_scan_count=120,
                consecutive_miss_limit=15,
            )

            reports = scan_result["reports"]
            new_last_scanned_id = int(
                scan_result["last_scanned_report_id"]
            )

            # 새 리포트 상세페이지가 확인된 범위까지만 high-water mark 전진.
            # 전송할 보유종목 리포트가 없어도 다음 검사에서 같은 전체 시장
            # 리포트를 반복 조회하지 않는다.
            if (
                last_scanned_report_id is None
                or new_last_scanned_id
                > int(last_scanned_report_id)
            ):
                self.write_cursor(
                    new_last_scanned_id
                )

            target = None

            # 오래된 것부터 처리해야 새 리포트가 여러 개 있을 때
            # 30분마다 순서대로 하나씩 전달된다.
            for report in reports:
                report_key = report.get(
                    "report_key"
                )
                code = str(
                    report.get("code", "")
                ).zfill(6)

                if (
                    not report_key
                    or code not in holding_codes
                ):
                    continue

                if self.is_sent(
                    report_key
                ):
                    continue

                target = report
                break

            if target is None:
                return

            code = str(
                target.get("code", "")
            ).zfill(6)

            title = target.get("title") or "기업 리포트"
            broker = target.get("broker") or "-"
            report_date = target.get("report_date") or "-"
            url = target["url"]
            code_name = self.manager.get_code_name(code)
            message = format_report_message(target, code_name)

            result = self.send_message(message)

            if result is not None:
                self.record_sent(
                    report_key=target[
                        "report_key"
                    ],
                    code=code,
                    title=title,
                    broker=broker,
                    report_date=report_date,
                    url=url,
                )

                print(
                    "[StrategyManager] "
                    "저평가 우량주 리포트 전송 완료: "
                    f"{code_name} / {title}"
                )

        except Exception as exc:
            # 사이트/API 변경이나 일시적인 네트워크 오류가
            # 자동매매 자체를 멈추게 해서는 안 된다.
            if (
                now - self.manager.last_value_report_error_log_at
                >= self.manager.value_report_error_log_interval
            ):
                print(
                    "[StrategyManager] "
                    "한경 컨센서스 리포트 조회 실패 - "
                    "매매는 계속 진행합니다: "
                    f"{exc}"
                )
                self.manager.last_value_report_error_log_at = now
