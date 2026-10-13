from __future__ import annotations


STRATEGIES = {
    "MomentumContractionStrategy": {
        "strategy_name": "MomentumContractionStrategy",
        "display_name": "모멘텀 수축 돌파",
        "paper_only": False,
        "supports_paper_account": True,
        "live_execution": "SYSTEMTRADING_MOMENTUM_LIVE=1일 때 신규매수 활성; 기존 실보유 매도관리는 계속 수행",
        "description": "모멘텀 실주문은 매매 프로세스의 명시적 활성 설정으로 실행하며, 모멘텀 가상 계좌 하나로 성과를 기록합니다.",
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
