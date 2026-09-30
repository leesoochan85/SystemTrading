import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  getStrategy,
  updateMaSettings,
  getVirtualEvents,
  getVirtualPerformance,
  getVirtualDailyPerformance,
  getVirtualIndexHistory,
  getVirtualTrades,
} from "../api/tradingApi";

const pct = (v) => `${Number(v || 0).toFixed(2)}%`;
const price = (v) => Number(v || 0).toLocaleString("ko-KR");

function formatTime(value) {
  const text = String(value || "");
  if (text.length !== 14) return text || "-";
  return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)} ${text.slice(8, 10)}:${text.slice(10, 12)}:${text.slice(12, 14)}`;
}


function formatDate(value) {
  const text = String(value || "");
  if (text.length !== 8) return text || "-";
  return `${text.slice(4, 6)}/${text.slice(6, 8)}`;
}

function MaSettingsPanel({ name, status, onSaved }) {
  const [period, setPeriod] = useState("5");
  const [ratio, setRatio] = useState("0.97");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!dirty && status?.saved) {
      setPeriod(String(status.saved.ma_period));
      setRatio(String(status.saved.exit_ratio));
    }
  }, [status?.saved?.revision, dirty]);

  const save = async (event) => {
    event.preventDefault();
    setMessage("");
    const maPeriod = Number(period);
    const exitRatio = Number(ratio);
    if (!Number.isInteger(maPeriod) || maPeriod < 2 || maPeriod > 60 ||
        !Number.isFinite(exitRatio) || exitRatio < 0.8 || exitRatio >= 1) {
      setMessage("MA 기간은 2~60의 정수, 기준 비율은 0.80 이상 1.00 미만으로 입력하세요.");
      return;
    }
    setSaving(true);
    try {
      const result = await updateMaSettings(name, {
        ma_period: maPeriod,
        exit_ratio: exitRatio,
        expected_revision: status.saved.revision,
      });
      setDirty(false);
      onSaved(result);
      setMessage("저장했습니다. 저장일 이후 첫 거래일에 매매 프로그램이 적용합니다.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setSaving(false);
    }
  };

  const active = status?.applied;
  const saved = status?.saved;
  return (
    <section className="panel ma-settings-panel">
      <div className="panel-title"><h2>신고가 MA 수익 청산 설정</h2><span>{status?.pending ? "적용 대기" : "저장 / 적용 현황"}</span></div>
      <p>수익 중인 보유 종목은 15:19~15:20에 MA 기준선을 검사합니다. ATR 청산은 기존대로 계속 작동합니다.</p>
      <div className="ma-settings-status">
        <div><span>매매 프로그램 적용 확인</span><strong>{active ? `v${active.revision} · MA${active.ma_period} × ${Number(active.exit_ratio).toFixed(3)}` : "미확인 (기본값 MA5 × 0.970)"}</strong><small>{active ? `적용 확인: ${formatTime(active.applied_at)}` : "매매 프로그램 실행 후 확인됩니다"}</small></div>
        <div><span>저장한 설정</span><strong>{saved ? `v${saved.revision} · MA${saved.ma_period} × ${Number(saved.exit_ratio).toFixed(3)}` : "불러오는 중"}</strong><small>{saved?.saved_at ? `저장: ${formatTime(saved.saved_at)}` : "기본값"}</small></div>
      </div>
      {status?.pending && <p className="ma-settings-pending">저장된 새 설정은 저장일 이후 첫 거래일에 적용됩니다. 매매 프로그램이 적용하면 위 적용 확인 값이 바뀝니다.</p>}
      <form className="ma-settings-form" onSubmit={save}>
        <label>이동평균 기간 (거래일)<input type="number" min="2" max="60" step="1" value={period} onChange={(e) => { setPeriod(e.target.value); setDirty(true); }} /></label>
        <label>청산 기준 비율<input type="number" min="0.8" max="0.999" step="0.001" value={ratio} onChange={(e) => { setRatio(e.target.value); setDirty(true); }} /></label>
        <button type="submit" disabled={!saved || saving || !dirty}>{saving ? "저장 중…" : "다음 거래일 설정 저장"}</button>
      </form>
      <p className="panel-subtitle">예: MA10 × 0.95면 기준선 아래로 내려온 수익 포지션의 잔량을 시장가로 청산합니다.</p>
      {message && <p role="status" className="ma-settings-feedback">{message}</p>}
    </section>
  );
}

function VirtualIndexChart({ items }) {
  const data = Array.isArray(items) ? items : [];

  if (!data.length) {
    return (
      <div className="chart-empty">
        장마감 가상 전략 지수가 아직 저장되지 않았습니다.
        다음 장중 실행 후 장마감부터 데이터가 쌓입니다.
      </div>
    );
  }

  const width = 1000;
  const height = 340;
  const pad = { left: 62, right: 24, top: 28, bottom: 46 };
  const values = data.map((item) => Number(item.daily_return_pct || 0));
  const maxAbs = Math.max(1, ...values.map((value) => Math.abs(value)));
  const yMin = -maxAbs * 1.15;
  const yMax = maxAbs * 1.15;
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const x = (index) => pad.left + (
    data.length <= 1 ? plotWidth / 2 : (index / (data.length - 1)) * plotWidth
  );
  const y = (value) => pad.top + ((yMax - value) / (yMax - yMin)) * plotHeight;
  const points = data.map((item, index) =>
    `${x(index)},${y(Number(item.daily_return_pct || 0))}`
  ).join(" ");
  const zeroY = y(0);
  const labelStep = Math.max(1, Math.ceil(data.length / 6));

  return (
    <div className="daily-chart-wrap">
      <svg className="daily-chart" viewBox={`0 0 ${width} ${height}`} role="img">
        <line className="chart-zero-line" x1={pad.left} x2={width - pad.right} y1={zeroY} y2={zeroY} />
        <text className="chart-axis-label" x={pad.left - 12} y={y(maxAbs)} textAnchor="end">
          +{maxAbs.toFixed(1)}%
        </text>
        <text className="chart-axis-label" x={pad.left - 12} y={zeroY + 4} textAnchor="end">0%</text>
        <text className="chart-axis-label" x={pad.left - 12} y={y(-maxAbs) + 4} textAnchor="end">
          -{maxAbs.toFixed(1)}%
        </text>
        <polyline className="chart-return-line" fill="none" points={points} />
        {data.map((item, index) => {
          const value = Number(item.daily_return_pct || 0);
          const tooltip = `${item.snapshot_date} | 일간 ${value.toFixed(2)}% | INDEX ${Number(item.strategy_index || 100).toFixed(2)} | DD ${Number(item.drawdown_pct || 0).toFixed(2)}%`;
          return (
            <g key={`${item.snapshot_date}-${index}`}>
              <circle
                className={value >= 0 ? "chart-point chart-point-up" : "chart-point chart-point-down"}
                cx={x(index)} cy={y(value)} r="5"
              >
                <title>{tooltip}</title>
              </circle>
              {(index % labelStep === 0 || index === data.length - 1) && (
                <text className="chart-date-label" x={x(index)} y={height - 16} textAnchor="middle">
                  {formatDate(item.snapshot_date)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}


function DailyReturnChart({ items }) {
  const data = Array.isArray(items) ? items : [];

  if (!data.length) {
    return (
      <div className="chart-empty">
        아직 청산된 가상 거래가 없어 일별 수익률 데이터가 없습니다.
      </div>
    );
  }

  const width = 1000;
  const height = 320;
  const pad = { left: 62, right: 24, top: 28, bottom: 46 };

  const values = data.map((item) =>
    Number(item.daily_return_pct || 0)
  );

  const maxAbs = Math.max(
    1,
    ...values.map((value) => Math.abs(value))
  );

  const yMin = -maxAbs * 1.15;
  const yMax = maxAbs * 1.15;
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;

  const x = (index) => (
    pad.left
    + (
      data.length <= 1
        ? plotWidth / 2
        : (index / (data.length - 1)) * plotWidth
    )
  );

  const y = (value) => (
    pad.top
    + ((yMax - value) / (yMax - yMin)) * plotHeight
  );

  const points = data
    .map((item, index) => (
      `${x(index)},${y(Number(item.daily_return_pct || 0))}`
    ))
    .join(" ");

  const zeroY = y(0);
  const labelStep = Math.max(
    1,
    Math.ceil(data.length / 6)
  );

  return (
    <div className="daily-chart-wrap">
      <svg
        className="daily-chart"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label="가상전략 일별 실현 수익률 선그래프"
      >
        <line
          className="chart-zero-line"
          x1={pad.left}
          x2={width - pad.right}
          y1={zeroY}
          y2={zeroY}
        />

        <text
          className="chart-axis-label"
          x={pad.left - 12}
          y={y(maxAbs)}
          textAnchor="end"
        >
          +{maxAbs.toFixed(1)}%
        </text>

        <text
          className="chart-axis-label"
          x={pad.left - 12}
          y={zeroY + 4}
          textAnchor="end"
        >
          0%
        </text>

        <text
          className="chart-axis-label"
          x={pad.left - 12}
          y={y(-maxAbs) + 4}
          textAnchor="end"
        >
          -{maxAbs.toFixed(1)}%
        </text>

        <polyline
          className="chart-return-line"
          fill="none"
          points={points}
        />

        {data.map((item, index) => {
          const value = Number(item.daily_return_pct || 0);

          return (
            <g key={`${item.date}-${index}`}>
              <circle
                className={
                  value >= 0
                    ? "chart-point chart-point-up"
                    : "chart-point chart-point-down"
                }
                cx={x(index)}
                cy={y(value)}
                r="5"
              >
                <title>
                  {`${item.date} · ${value.toFixed(2)}% · SELL ${item.sell_event_count || 0}건`}
                </title>
              </circle>

              {(
                index % labelStep === 0
                || index === data.length - 1
              ) && (
                <text
                  className="chart-date-label"
                  x={x(index)}
                  y={height - 16}
                  textAnchor="middle"
                >
                  {formatDate(item.date)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}


export default function StrategyPage() {
  const { name } = useParams();
  const [detail, setDetail] = useState(null);
  const [virtual, setVirtual] = useState(null);
  const [dailyPerformance, setDailyPerformance] = useState(null);
  const [virtualIndex, setVirtualIndex] = useState(null);
  const [events, setEvents] = useState([]);
  const [trades, setTrades] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const [d, v, daily, indexHistory, e, t] = await Promise.all([
          getStrategy(name),
          getVirtualPerformance(name),
          getVirtualDailyPerformance(name),
          getVirtualIndexHistory(name),
          getVirtualEvents(name, 80),
          getVirtualTrades(name, 80),
        ]);
        if (!active) return;
        setDetail(d);
        setVirtual(v);
        setDailyPerformance(daily);
        setVirtualIndex(indexHistory);
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

      {name === "HighBreakoutStrategy" && detail?.ma_settings && (
        <MaSettingsPanel name={name} status={detail.ma_settings}
          onSaved={(status) => setDetail((old) => ({ ...old, ma_settings: status }))} />
      )}

      <section className="panel primary-performance-panel">
        <div className="panel-title">
          <div>
            <h2>가상 전략 일별 성과지수</h2>
            <p className="panel-subtitle">
              실현 + 현재 보유 미실현손익을 함께 반영한 동일투자금 기준
            </p>
          </div>
          <span>{virtualIndex?.items?.length || 0}거래일</span>
        </div>

        <div className="daily-stat-grid index-stat-grid">
          <div>
            <span>현재 누적 성과</span>
            <strong className={Number(virtualIndex?.latest?.cumulative_return_pct || 0) >= 0 ? "up" : "down"}>
              {pct(virtualIndex?.latest?.cumulative_return_pct)}
            </strong>
          </div>
          <div>
            <span>MDD</span>
            <strong className="down">{pct(virtualIndex?.max_drawdown_pct)}</strong>
          </div>
          <div>
            <span>최고의 날</span>
            <strong className="up">{pct(virtualIndex?.best_daily_return_pct)}</strong>
          </div>
          <div>
            <span>최악의 날</span>
            <strong className="down">{pct(virtualIndex?.worst_daily_return_pct)}</strong>
          </div>
        </div>

        <VirtualIndexChart items={virtualIndex?.items || []} />

        <div className="metric-note chart-note">
          각 가상 BUY를 동일한 투자금 1단위로 보고, 이미 청산된 비중은 실제 청산가격으로,
          아직 보유 중인 잔여비중은 장마감 현재가로 평가합니다. 따라서 매도하지 않은 상태의
          급락도 이 그래프에 반영됩니다. 실제 전략별 현금계좌 NAV와는 다른 지표입니다.
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <div>
            <h2>가상 일별 실현 수익률</h2>
            <p className="panel-subtitle">
              그날 청산된 가상 포지션의 청산비중 가중 평균 수익률
            </p>
          </div>
          <span>
            {dailyPerformance?.active_day_count || 0}거래일
          </span>
        </div>

        <div className="daily-stat-grid">
          <div>
            <span>평균</span>
            <strong
              className={
                Number(dailyPerformance?.average_daily_return_pct || 0) >= 0
                  ? "up"
                  : "down"
              }
            >
              {pct(dailyPerformance?.average_daily_return_pct)}
            </strong>
          </div>

          <div>
            <span>최고의 날</span>
            <strong className="up">
              {pct(dailyPerformance?.best_daily_return_pct)}
            </strong>
          </div>

          <div>
            <span>최악의 날</span>
            <strong className="down">
              {pct(dailyPerformance?.worst_daily_return_pct)}
            </strong>
          </div>
        </div>

        <DailyReturnChart
          items={dailyPerformance?.items || []}
        />

        <div className="metric-note chart-note">
          이 그래프는 독립 전략계좌 NAV 수익률이 아닙니다.
          현재 가상매매는 모든 전략 신호를 독립 포지션으로 기록하므로,
          날짜별 SELL의 청산비중 가중 평균 실현수익률을 표시합니다.
          아직 청산되지 않은 포지션의 미실현 손익은 포함하지 않습니다.
        </div>
      </section>

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
