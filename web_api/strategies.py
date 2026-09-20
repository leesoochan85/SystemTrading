from __future__ import annotations


STRATEGIES = {
    "HighBreakoutStrategy": {
        "strategy_name": "HighBreakoutStrategy",
        "display_name": "신고가 돌파",
        "description": "60일 최고가 대비 -5% 이상에서 전일 고가를 넘으면 진입하고 ATR로 청산하는 전략",
        "buy_conditions": [
            "현재가 >= 직전 60거래일 최고가의 95% (상한 없음, 오늘 제외)",
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
        ],
        "reason_codes": {
            "BUY": ["HIGH_ZONE_OR_BREAKOUT_ENTRY"],
            "SELL": ["ATR_INITIAL_STOP", "ATR_TRAILING_STOP"],
        },
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
