from __future__ import annotations


STRATEGIES = {
    "HighBreakoutStrategy": {
        "strategy_name": "HighBreakoutStrategy",
        "display_name": "신고가 돌파",
        "description": "직전 180거래일 고가 돌파에 진입하고 ATR 및 장 마감 전 MA 수익 조건으로 청산하는 전략",
        "buy_conditions": [
            "현재가 >= 직전 180거래일 최고가 (오늘 제외)",
            "현재가 > 전일 완결 일봉 고가",
            "당일 누적거래량이 최근 20거래일 평균거래량 이상",
            "최근 20거래일 평균거래대금이 20억원 이상",
            "유효한 Wilder ATR(14)와 0원 초과 초기 청산선 확보",
        ],
        "sell_conditions": [
            "초기 청산선 = 실제 매입가 - 2 × 진입 직전 Wilder ATR(14)",
            "추적 청산선 = max(기존 청산선, 진입 이후 최고 종가 - 3 × 완결 일봉 ATR(14))",
            "완결 일봉으로 갱신한 청산선은 다음 거래일부터 적용 (낮추지 않음)",
            "현재가 <= 청산선이면 잔량 시장가 매도 (고정 -5% / 동적 MA20 매도 없음)",
            "15:19~15:20 수익 상태에서 첫 틱으로 고정한 예상 MA × 설정 비율 이하이면 잔량 시장가 매도",
        ],
        "reason_codes": {
            "BUY": ["BREAKOUT_ENTRY"],
            "SELL": ["ATR_INITIAL_STOP", "ATR_TRAILING_STOP", "MA_PROFIT_EXIT"],
        },
    },
    "MomentumContractionStrategy": {
        "strategy_name": "MomentumContractionStrategy",
        "display_name": "모멘텀 수축 돌파",
        "paper_only": True,
        "description": "ADR·상승 순위·횡보 수축을 확인한 돌파 전략. 동일 조건의 A/B/C 가상 계좌로 비교합니다.",
        "buy_conditions": [
            "기본 설정: 전일 종가 / 최근 126거래일 최저가 순위 상위 2%",
            "저자 공식 ADR(20) > 3.5%, 20일 평균 거래대금 20억원 이상",
            "선행 상승·저점 상승·변동폭 수축 확인 (정량 기준은 실험 설정)",
            "횡보 고가와 정규장 첫 5분 고가를 모두 초과",
            "진입 당시 당일 저가까지의 손절 거리 <= 전일 ATR(14)",
        ],
        "sell_conditions": [
            "진입 당시 정한 당일 저가 이하에서 잔량 청산 (손절선 하향 없음)",
            "4거래일 경과 후 수익 상태에서 최초 수량 약 1/3을 한 번 청산",
            "부분 매도 체결 후 비용을 반영한 손익분기점으로 손절선 상향",
            "잔량: 확정 종가의 MA10 이탈 확인 후 다음 거래일 관측 틱으로 청산",
        ],
        "reason_codes": {"BUY": ["CONTRACTION_BREAKOUT"], "SELL": ["DAY_LOW_STOP", "TIME_PARTIAL_EXIT", "MA_CLOSE_EXIT"]},
    },
    "PullbackTrendStrategy": {
        "strategy_name": "PullbackTrendStrategy",
        "display_name": "상승추세 눌림목",
        "description": "이동평균선 정배열 상태에서 20일선 부근 조정을 매수하는 추세 눌림목 전략",
        "buy_conditions": [
            "MA5 > MA20 > MA60 정배열",
            "현재가/MA20 이격도 97~103%",
            "현재가가 MA20 이상",
        ],
        "sell_conditions": [
            "수익률 -5% 이하 고정 손절",
            "현재가가 MA20 아래로 하락",
            "당일 누적거래량이 전일 대비 15% 이상 증가하면서 음봉",
            "매수 전 직전 60거래일 최고가 도달 시 최초 1회 30% 부분매도",
            "전고점 도달 다음 거래일 15:20 이후 거래량 감소 시 잔량 전량매도",
        ],
        "reason_codes": {
            "BUY": ["PULLBACK_ENTRY"],
            "SELL": [
                "STOP_LOSS",
                "MA20_BREAKDOWN",
                "BEARISH_VOLUME",
                "PREVIOUS_HIGH_PARTIAL_EXIT",
                "NEXT_DAY_VOLUME_FADE",
            ],
        },
    },
    "ValueQualityStrategy": {
        "strategy_name": "ValueQualityStrategy",
        "display_name": "저평가 우량주",
        "description": "PER·PBR 저평가와 수익성·자산효율 조건을 함께 사용하는 가치/퀄리티 전략",
        "buy_conditions": [
            "0 < PER(TTM) <= 15",
            "0 < PBR <= 1",
            "30% <= 매출총이익률 <= 95%",
            "1 <= 총자산회전율 <= 10",
        ],
        "sell_conditions": [
            "수익률 -10% 이하 전량매도",
            "수익률 +30% 이상 전량매도",
        ],
        "reason_codes": {
            "BUY": ["VALUE_QUALITY_ENTRY"],
            "SELL": ["STOP_LOSS", "TAKE_PROFIT"],
        },
    },
}


def get_strategy_config(strategy_name: str):
    return STRATEGIES.get(strategy_name)


def get_all_strategy_configs():
    return list(STRATEGIES.values())
