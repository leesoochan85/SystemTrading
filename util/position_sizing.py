"""실주문용 ATR 위험 예산 기반 매수 수량 계산."""
import math


def stop_risk_quantity(total_assets, price, stop_price, risk_ratio=0.005,
                       buy_fee_rate=0.0, sell_cost_rate=0.0):
    """Quantity from an explicit stop; prices may already include fill slippage."""
    values = (total_assets, price, stop_price, risk_ratio, buy_fee_rate, sell_cost_rate)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("risk sizing requires finite numbers")
    if total_assets <= 0 or price <= stop_price or stop_price <= 0 or not 0 < risk_ratio <= 1:
        raise ValueError("invalid assets, entry, stop, or risk ratio")
    if not 0 <= buy_fee_rate < 1 or not 0 <= sell_cost_rate < 1:
        raise ValueError("invalid fee rate")
    unit_loss = price * (1 + buy_fee_rate) - stop_price * (1 - sell_cost_rate)
    return math.floor(total_assets * risk_ratio / unit_loss)


def atr_risk_quantity(total_assets, price, entry_atr, risk_ratio=0.005,
                      initial_multiple=2.0):
    """초기 ATR 청산선에 체결된다는 가정에서 계획 손실액을 제한한다.

    실제 시장가 청산의 갭/슬리피지 손실은 이 수량으로 보장되지 않는다.
    """
    values = (total_assets, price, entry_atr, risk_ratio, initial_multiple)
    if any(not math.isfinite(float(value)) or float(value) <= 0 for value in values):
        raise ValueError("ATR 위험 수량 계산에는 유한한 양수가 필요합니다")
    stop_distance = float(entry_atr) * float(initial_multiple)
    if float(price) <= stop_distance:
        raise ValueError("ATR 초기 청산선이 0원 이하입니다")
    return math.floor(float(total_assets) * float(risk_ratio) / stop_distance)
