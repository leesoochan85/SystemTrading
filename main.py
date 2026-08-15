from api.Kiwoom import *
from strategy.HighBreakoutStrategy import HighBreakoutStrategy
from strategy.PullbackTrendStrategy import PullbackTrendStrategy
from strategy.ValueQualityStrategy import ValueQualityStrategy
from strategy.StrategyManager import StrategyManager
import sys


app = QApplication(sys.argv)
kiwoom = Kiwoom()

print("HighBreakout 객체 생성 시작")
high_breakout_strategy = HighBreakoutStrategy(
    kiwoom,
    auto_init=False,
)
print("HighBreakout 객체 생성 완료")

print("PullbackTrend 객체 생성 시작")
pullback_trend_strategy = PullbackTrendStrategy(
    kiwoom,
    auto_init=False,
)
print("PullbackTrend 객체 생성 완료")

print("ValueQuality 객체 생성 시작")
value_quality_strategy = ValueQualityStrategy(
    kiwoom,
    auto_init=False,
)
print("ValueQuality 객체 생성 완료")

print("전략 매니저 시작")
manager = StrategyManager(
    kiwoom,
    [
        high_breakout_strategy,
        pullback_trend_strategy,
        value_quality_strategy,
    ],
)
manager.start()
print("전략 매니저 시작 완료")

app.exec_()
