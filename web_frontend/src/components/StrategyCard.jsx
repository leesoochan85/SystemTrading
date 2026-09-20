import { Link } from "react-router-dom";

const STRATEGY_VIDEO_URLS = {
  HighBreakoutStrategy: "https://www.youtube.com/watch?v=iNh7S3tAO-o",
  PullbackTrendStrategy: "https://www.youtube.com/watch?v=O3DTjKuIW_g",
  ValueQualityStrategy: "https://www.youtube.com/watch?v=SskCOZ0yi9g",
};

function number(value, digits = 2) {
  return Number(value || 0).toFixed(digits);
}

export default function StrategyCard({ strategy }) {
  const p = strategy.virtual_performance || {};
  const events = strategy.today_virtual_events || {};
  const videoUrl = STRATEGY_VIDEO_URLS[strategy.strategy_name];

  return (
    <article className="strategy-card">
      <Link
        className="strategy-card__detail-link"
        to={`/strategies/${strategy.strategy_name}`}
        aria-label={`${strategy.display_name} 전략 상세 보기`}
      />

      <div className="strategy-card__head">
        <div>
          <span className="eyebrow">STRATEGY</span>
          <div className="strategy-card__title-row">
            <h2>{strategy.display_name}</h2>
            {videoUrl && (
              <a
                className="strategy-video-link"
                href={videoUrl}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`${strategy.display_name} 전략 설명 영상 보기`}
                title="유튜브에서 전략 설명 영상 보기"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M21.6 7.2a2.9 2.9 0 0 0-2-2C17.8 4.7 12 4.7 12 4.7s-5.8 0-7.6.5a2.9 2.9 0 0 0-2 2A30 30 0 0 0 2 12a30 30 0 0 0 .4 4.8 2.9 2.9 0 0 0 2 2c1.8.5 7.6.5 7.6.5s5.8 0 7.6-.5a2.9 2.9 0 0 0 2-2A30 30 0 0 0 22 12a30 30 0 0 0-.4-4.8ZM10 15.2V8.8l5.5 3.2-5.5 3.2Z" />
                </svg>
                <span>전략 영상</span>
              </a>
            )}
          </div>
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
    </article>
  );
}
