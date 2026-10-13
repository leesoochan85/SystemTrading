import sys
import os


def main():
    """Create the Qt application and broker only when explicitly starting trading."""
    from PyQt5.QtWidgets import QApplication
    from api.Kiwoom import Kiwoom
    from strategy.ValueQualityStrategy import ValueQualityStrategy
    from strategy.StrategyManager import StrategyManager
    from strategy.LiveMomentumContractionStrategy import LiveMomentumContractionStrategy

    app = QApplication(sys.argv)
    kiwoom = Kiwoom()
    value_quality_strategy = ValueQualityStrategy(kiwoom, auto_init=False)
    # Existing holdings stay managed even when new momentum buys are disabled.
    momentum_contraction_strategy = LiveMomentumContractionStrategy(
        kiwoom, allow_new_buys=os.getenv("SYSTEMTRADING_MOMENTUM_LIVE") == "1")
    print("[Momentum] 증권사 신규매수 활성" if momentum_contraction_strategy.allow_new_buys else "[Momentum] 신규매수 중지 · 기존 실보유 매도관리 유지")
    manager = StrategyManager(kiwoom, [value_quality_strategy, momentum_contraction_strategy])
    manager.start()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
