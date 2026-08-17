import { Link } from "react-router-dom";

function number(value, digits = 2) {
  return Number(value || 0).toFixed(digits);
}

export default function StrategyCard({ strategy }) {
  const p = strategy.virtual_performance || {};
  const events = strategy.today_virtual_events || {};

  return (
    <Link className="strategy-card" to={`/strategies/${strategy.strategy_name}`}>
      <div className="strategy-card__head">
        <div>
          <span className="eyebrow">STRATEGY</span>
          <h2>{strategy.display_name}</h2>
        </div>
        <span className="arrow">→</span>
      </div>

      <p className="muted">{strategy.description}</p>

      <div className="metric-grid">
        <div><span>가상 보유</span><strong>{p.open_position_count || 0}</strong></div>
        <div><span>완료 거래</span><strong>{p.completed_trade_count || 0}</strong></div>
        <div><span>승률</span><strong>{number(p.win_rate_pct)}%</strong></div>
        <div><span>평균 수익률</span><strong>{number(p.average_return_pct)}%</strong></div>
      </div>

      <div className="signal-row">
        <span className="virtual-buy">오늘 가상 BUY {events.buy || 0}</span>
        <span className="virtual-sell">오늘 가상 SELL {events.sell || 0}</span>
        <span>현재 가상 보유 {p.open_position_count || 0}</span>
      </div>
    </Link>
  );
}
