import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  getStrategy,
  getVirtualEvents,
  getVirtualPerformance,
  getVirtualTrades,
} from "../api/tradingApi";

const pct = (v) => `${Number(v || 0).toFixed(2)}%`;
const price = (v) => Number(v || 0).toLocaleString("ko-KR");

function formatTime(value) {
  const text = String(value || "");
  if (text.length !== 14) return text || "-";
  return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)} ${text.slice(8, 10)}:${text.slice(10, 12)}:${text.slice(12, 14)}`;
}

export default function StrategyPage() {
  const { name } = useParams();
  const [detail, setDetail] = useState(null);
  const [virtual, setVirtual] = useState(null);
  const [events, setEvents] = useState([]);
  const [trades, setTrades] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const [d, v, e, t] = await Promise.all([
          getStrategy(name),
          getVirtualPerformance(name),
          getVirtualEvents(name, 80),
          getVirtualTrades(name, 80),
        ]);
        if (!active) return;
        setDetail(d);
        setVirtual(v);
        setEvents(e.items || []);
        setTrades(t.items || []);
        setError("");
      } catch (e) {
        if (active) setError(e.message);
      }
    };
    load();
    const timer = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [name]);

  const p = virtual?.performance || {};

  return (
    <main className="page-shell">
      <Link className="back" to="/">← 전체 전략</Link>
      <header className="detail-head">
        <div>
          <span className="eyebrow">STRATEGY DETAIL</span>
          <h1>{detail?.display_name || name}</h1>
          <p>실제 계좌 주문 가능 여부와 관계없이 전략 조건으로 생성된 가상 체결만 표시합니다.</p>
        </div>
      </header>
      {error && <div className="error-box">{error}</div>}

      <section className="summary-strip">
        <div><span>가상 보유</span><strong>{p.open_position_count || 0}</strong></div>
        <div><span>완료 거래</span><strong>{p.completed_trade_count || 0}</strong></div>
        <div><span>승률</span><strong>{pct(p.win_rate_pct)}</strong></div>
        <div><span>평균 수익률</span><strong>{pct(p.average_return_pct)}</strong></div>
        <div><span>최고</span><strong>{pct(p.best_return_pct)}</strong></div>
        <div><span>최저</span><strong>{pct(p.worst_return_pct)}</strong></div>
      </section>

      <div className="metric-note">
        완료 거래는 가상 BUY 이후 100% 청산된 거래만 계산합니다. 30% 부분매도만 진행된 포지션은 완료 거래에 포함하지 않습니다.
      </div>

      <section className="panel">
        <div className="panel-title">
          <h2>현재 가상 보유</h2>
          <span>{virtual?.positions?.length || 0}건</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>종목</th><th>진입가</th><th>현재가</th><th>수익률</th><th>잔여비중</th><th>진입시간</th></tr></thead>
            <tbody>
              {(virtual?.positions || []).map((x) => (
                <tr key={`${x.strategy_name}-${x.code}`}>
                  <td><b>{x.code_name}</b><small>{x.code}</small></td>
                  <td>{price(x.entry_price)}</td>
                  <td>{price(x.current_price)}</td>
                  <td className={Number(x.unrealized_return_pct) >= 0 ? "up" : "down"}>{pct(x.unrealized_return_pct)}</td>
                  <td>{pct((x.remaining_ratio || 0) * 100)}</td>
                  <td>{formatTime(x.entry_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <h2>가상 청산 내역</h2>
          <span>최근 {trades.length}건</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>종목</th><th>진입가</th><th>청산가</th><th>수익률</th><th>청산비중</th><th>매도사유</th><th>청산시간</th></tr></thead>
            <tbody>
              {trades.map((x) => (
                <tr key={x.trade_id}>
                  <td><b>{x.code_name}</b><small>{x.code}</small></td>
                  <td>{price(x.entry_price)}</td>
                  <td>{price(x.exit_price)}</td>
                  <td className={Number(x.return_pct) >= 0 ? "up" : "down"}>{pct(x.return_pct)}</td>
                  <td>{pct((x.exit_ratio || 0) * 100)}</td>
                  <td>{x.sell_reason}</td>
                  <td>{formatTime(x.exit_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <h2>최근 가상매매 이벤트</h2>
          <span>최근 {events.length}건</span>
        </div>
        <div className="event-list">
          {events.map((x) => (
            <div className="event" key={x.event_id}>
              <span className={`pill ${x.event_type?.toLowerCase()}`}>{x.event_type}</span>
              <div>
                <b>{x.code_name} <small>{x.code}</small></b>
                <p>{x.reason || "-"}</p>
              </div>
              <div className="event-meta">
                <span>
                  {price(x.price)}원
                  {x.event_type === "SELL" && ` · ${pct((x.ratio || 0) * 100)} 청산`}
                  {x.event_type === "SELL" && ` · ${pct(x.return_pct)}`}
                </span>
                <time>{formatTime(x.event_at)}</time>
              </div>
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}
