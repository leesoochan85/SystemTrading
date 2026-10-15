import { useState } from "react";
import MomentumPeriodComparison from "../components/MomentumPeriodComparison";
import { PERIOD_LABELS } from "../utils/momentumPeriods";
import StrategySourceLink from "../components/StrategySourceLink";
import usePolling from "../hooks/usePolling";
import { Link } from "react-router-dom";
import { getPaperAccount, getMomentumPeriods } from "../api/tradingApi";
import { momentumStatus } from "../utils/momentumStatus";

const MOMENTUM = "MomentumContractionStrategy";
const ACCOUNT_LABEL = "모멘텀 수축 돌파";
const ACCOUNT_COLOR = "#51c4a0";
const money = (v) => v == null ? "—" : `${Number(v).toLocaleString("ko-KR", { maximumFractionDigits: 0 })}원`;
const percent = (v) => v == null ? "—" : `${Number(v).toFixed(2)}%`;
const costPercent = (v) => `${(Number(v) * 100).toFixed(3)}%`;
const timeLabel = (v) => v ? `${v.slice(0, 4)}-${v.slice(4, 6)}-${v.slice(6, 8)} ${v.slice(8, 10)}:${v.slice(10, 12)}:${v.slice(12, 14)}` : "아직 기록 없음";
const REASONS = {
  MOMENTUM_RANK: "상승 순위 밖", ADR_TOO_LOW: "ADR 부족", ILLIQUID: "거래대금 부족",
  PRIOR_MOVE_TOO_SMALL: "선행 상승 부족", RANGE_NOT_TIGHT: "변동폭 미수축", LOW_NOT_RISING: "저점 상승 미충족",
  WAIT_OPENING_RANGE: "첫 구간 집계 중", OPENING_RANGE_MISSING: "첫 구간 누락 · 오늘 진입 보류",
  MISSING_TRADE_TIME: "체결시간 없음", INVALID_TRADE_TIME: "체결시간 오류",
  OPENING_VOLUME_MISSING: "체결량 없음 · 오늘 진입 보류",
  OPENING_START_UNVERIFIED: "첫 체결 확인 불가 · 오늘 진입 보류",
  OPENING_VOLUME_GAP: "첫 구간 체결 누락 / 수량 불일치 · 오늘 진입 보류",
  INVALID_TICK_PRICE: "체결가격 오류", DUPLICATE_TICK: "중복 틱 제외",
  STALE_OR_OUTSIDE_SESSION: "정규장 밖 / 지연 틱", OUT_OF_ORDER_TICK: "역순 틱",
  WAIT_BREAKOUT: "돌파 대기", INVALID_DAY_LOW: "당일 저가 확인 필요",
  STOP_TOO_WIDE: "손절 거리 > ATR", STOP_TOO_WIDE_AFTER_SLIPPAGE: "체결비용 적용 후 거리 초과",
  BUY_CUTOFF: "신규 진입 마감", PAPER_ENTRY_FILLED: "가상 진입 체결", SAME_DAY_REENTRY: "당일 재진입 차단",
  POSITION_LIMIT: "보유 한도 도달", INSUFFICIENT_CASH_OR_RISK: "현금 / 위험 한도 부족",
  ACCOUNT_CHANGED_RESTART_REQUIRED: "다른 프로세스에서 계좌 변경 · 재시작 필요",
  DAY_LOW_STOP: "초기 / 손익분기 손절", TIME_PARTIAL_EXIT: "거래일 경과 부분 매도", MA_CLOSE_EXIT: "확정 종가 MA 이탈",
};

function MomentumChart({ items, label = ACCOUNT_LABEL }) {
  const rows = items.filter((item) => item.strategy_name === MOMENTUM);
  const dates = [...new Set(rows.map((x) => x.snapshot_date))].sort();
  if (!dates.length) return <div className="chart-empty">장중 실행 후 가상 계좌 성과가 기록됩니다.</div>;
  const values = rows.map((x) => x.cumulative_return_pct);
  const max = Math.max(1, ...values), min = Math.min(-1, ...values);
  const width = 1000, height = 300;
  const x = (day) => 60 + (dates.length === 1 ? 440 : dates.indexOf(day) / (dates.length - 1) * 880);
  const y = (value) => 30 + (max - value) / (max - min) * 220;
  return <>
    <div className="paper-legend"><span style={{ color: ACCOUNT_COLOR }}>{label}</span></div>
    <svg className="daily-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="모멘텀 가상 계좌 누적 수익률">
      <line x1="60" x2="940" y1={y(0)} y2={y(0)} className="chart-zero-line" />
      {[min, 0, max].map((v) => <text key={v} x="50" y={y(v) + 4} textAnchor="end" className="chart-axis-label">{percent(v)}</text>)}
      <g>
        <polyline fill="none" stroke={ACCOUNT_COLOR} strokeWidth="2.5" points={rows.map((r) => `${x(r.snapshot_date)},${y(r.cumulative_return_pct)}`).join(" ")} />
        {rows.map((r) => <circle key={r.snapshot_date} cx={x(r.snapshot_date)} cy={y(r.cumulative_return_pct)} r="4" fill={ACCOUNT_COLOR}><title>{`${label} ${r.snapshot_date}: ${percent(r.cumulative_return_pct)}`}</title></circle>)}
      </g>
      {dates.filter((_, i) => i % Math.max(1, Math.ceil(dates.length / 6)) === 0 || i === dates.length - 1).map((d) => <text key={d} x={x(d)} y="280" textAnchor="middle" className="chart-axis-label">{`${d.slice(4, 6)}/${d.slice(6, 8)}`}</text>)}
    </svg>
  </>;
}

export default function MomentumPage() {
  const { data: baseline, error: baselineError } = usePolling(getPaperAccount);
  const { data: comparison, error: comparisonError } = usePolling(getMomentumPeriods);
  const [selected, setSelected] = useState("1m");
  const data = selected === "baseline" ? baseline : comparison?.profiles?.find(p => p.id === selected)?.account;
  const error = comparisonError || (selected === "baseline" ? baselineError : "");
  const label = PERIOD_LABELS[selected];
  const account = data?.accounts?.find((a) => a.strategy_name === MOMENTUM);
  const rules = data?.settings?.strategy;
  const costs = account?.costs;
  const execution = data?.stats?.paper_execution;
  const status = momentumStatus(execution);
  return <main className="page-shell">
    <Link className="back" to="/">← 전체 전략</Link>
    <header className="detail-head"><div><span className="eyebrow">모멘텀 가상 계좌</span><h1>모멘텀 수축 돌파</h1><StrategySourceLink name="MomentumContractionStrategy" /><p>가상 현금과 거래 비용을 반영해 모멘텀 수축 돌파 전략의 성과를 확인합니다.</p></div></header>
    {error && <div className="error-box">{error}</div>}
    <MomentumPeriodComparison data={comparison} selected={selected} onSelect={setSelected} />
    <h2>{label} 상세</h2>
    {!data?.ready && <div className="metric-note">매매 프로그램을 실행하면 가상 계좌와 후보가 준비됩니다.</div>}
    <section className="panel">
      <div className="panel-title"><h2>모멘텀 웹 가상매매</h2><span>{status.text}</span></div>
      <p>우량주 증권사 주문과 함께 동작하며, 가상 현금과 보유 한도는 실계좌와 별도로 적용됩니다.</p>
      <p className="metric-note">상태 기록 {timeLabel(execution?.heartbeat_at)} · 최근 유효 틱 {timeLabel(execution?.last_tick_at)} · 모멘텀 증권사 신규매수 설정 {execution ? (execution.live_new_buys_enabled ? "켜짐" : "꺼짐") : "미수신"}</p>
      <p className="metric-note">평가액은 마지막 수신 가격을 사용합니다. 상태 기록 시각만으로 모든 보유 종목의 현재가가 갱신됐다고 판단하지 않습니다.</p>
      {execution?.phase === "NO_CANDIDATES" && <p className="metric-note">일봉 수집은 완료했어도 아래 조건을 모두 통과한 종목이 없으면 신규 가상매수는 발생하지 않습니다. 기존 가상 보유는 계속 관리합니다.</p>}
      {execution?.phase === "OPENING_RANGE_MISSING" && <p className="metric-note">다음 거래일에는 장 시작 전에 매매 프로그램을 실행해 첫 구간의 체결을 관측해야 합니다.</p>}
    </section>
    <section className="summary-strip">
      <div><span>가상 계좌 평가액</span><strong>{money(account?.equity)}</strong></div>
      <div><span>사용 가능 현금</span><strong>{money(account?.cash)}</strong></div>
      <div><span>비용 차감 수익률</span><strong>{percent(account?.cumulative_return_pct)}</strong></div>
      <div><span>관측 MDD</span><strong>{percent(account?.max_drawdown_pct)}</strong></div>
      <div><span>누적 수수료·세금</span><strong>{money(account?.total_fees)}</strong></div>
      <div><span>현재 보유</span><strong>{account?.open_position_count ?? "—"}종목</strong></div>
    </section>
    <section className="panel"><div className="panel-title"><h2>가상 계좌 성과</h2><span>{timeLabel(data?.as_of)}</span></div>
      <MomentumChart items={data?.equity_history || []} label={label} />
      <div className="table-wrap"><table><thead><tr><th>계좌</th><th>평가액</th><th>수익률</th><th>MDD</th><th>완료 거래</th><th>승률</th><th>이익합 / 손실합</th></tr></thead><tbody>
        <tr><td>{label}</td><td>{money(account?.equity)}</td><td>{percent(account?.cumulative_return_pct)}</td><td>{percent(account?.max_drawdown_pct)}</td><td>{account?.performance?.completed_trade_count ?? "—"}</td><td>{percent(account?.performance?.win_rate_pct)}</td><td>{account?.performance?.profit_factor == null ? "—" : account.performance.profit_factor.toFixed(2)}</td></tr>
      </tbody></table></div>
      <p className="metric-note">모멘텀 가상 계좌는 당일 저가 손절·부분 매도·확정 종가 MA 청산을 사용합니다. 완료 거래는 잔량까지 전부 청산된 진입 건입니다. MDD는 5초 간격으로 기록한 평가액 기준입니다.</p>
    </section>
    <section className="panel"><div className="panel-title"><h2>적용 조건</h2><span>모멘텀 가상 계좌 · 실제 주문 모드는 매매 프로세스에서 설정</span></div>
      {rules && <p>{rules.momentum_mode === "video_low126" ? "126거래일 최저가 대비 상승" : rules.momentum_mode === "pdf_returns" ? "21·63·126거래일 수익률 중 하나 이상" : `${rules.momentum_mode.split("_")[1]}거래일 종가 수익률`} 상위 {rules.top_percent}% · ADR(20) &gt; {rules.min_adr_pct}% · 첫 {rules.opening_range_minutes}분 고가 돌파 · ATR {rules.stop_atr_multiple}배 이내 손절 · {rules.partial_after_sessions}거래일 후 약 {(rules.partial_ratio * 100).toFixed(1)}% 부분 매도 · MA{rules.trailing_ma_period} 확정 종가 청산</p>}
      {rules && <p className="metric-note">횡보 {rules.consolidation_days}일, 최근 {rules.recent_range_days}일 TR 비율 {rules.max_tr_ratio}, 선행 상승 {rules.min_prior_move_pct}%와 저점 블록 비교는 자동 판정을 위한 실험값입니다.</p>}
      {costs && <p className="metric-note">비용 가정: 매수 {costPercent(costs.buy_fee_rate)} · 매도 수수료 {costPercent(costs.sell_fee_rate)} · 매도 세금 {costPercent(costs.sell_tax_rate)} · 체결 슬리피지 {costs.slippage_bps}bp. 실제 계좌 비용·현행 세율을 보장하는 값이 아닙니다.</p>}
    </section>
    <section className="panel"><div className="panel-title"><h2>후보와 진입 상태</h2><span>기준 일봉 {data?.stats?.reference_date || "—"}</span></div>
      <p className="metric-note">유효 이력 {data?.stats?.valid_history ?? 0} → 상위 순위 {data?.stats?.top_ranked ?? 0} → ADR {data?.stats?.adr ?? 0} → 유동성 {data?.stats?.liquid ?? 0} → 선행 상승 {data?.stats?.prior_move ?? 0} → 수축 {data?.stats?.contraction ?? 0} → 최종 {data?.stats?.qualified ?? 0}</p>
      <div className="table-wrap"><table><thead><tr><th>종목</th><th>순위</th><th>ADR</th><th>TR 비율</th><th>횡보 고가</th><th>첫 구간 고가</th><th>현재가</th><th>상태</th></tr></thead><tbody>
        {(data?.watchlist || []).map((r) => <tr key={r.code}><td><b>{r.code_name}</b><small>{r.code}</small></td><td>{r.momentum_rank}</td><td>{percent(r.metric?.adr20_pct)}</td><td>{r.metric?.tr_ratio?.toFixed(2) ?? "—"}</td><td>{money(r.metric?.base_high)}</td><td>{money(r.opening_high)}</td><td>{money(r.current_price)}</td><td>{REASONS[r.last_reason] || r.last_reason || "대기"}</td></tr>)}
      </tbody></table></div>
    </section>
    <section className="panel"><div className="panel-title"><h2>가상 계좌 현재 보유</h2><span>{data?.positions?.length || 0}종목</span></div>
      <div className="table-wrap"><table><thead><tr><th>종목</th><th>진입가</th><th>현재가</th><th>잔량</th><th>손절선</th><th>부분 매도</th><th>진입 시간</th></tr></thead><tbody>
        {(data?.positions || []).map((p) => <tr key={p.position_id}><td><b>{p.code_name}</b><small>{p.code}</small></td><td>{money(p.entry_price)}</td><td>{money(p.current_price)}</td><td>{p.quantity}주</td><td>{money(p.state?.stop_price)}</td><td>{p.state?.partial_done ? "체결 완료" : "미체결"}</td><td>{timeLabel(p.entry_at)}</td></tr>)}
      </tbody></table></div>
    </section>
    <section className="panel"><div className="panel-title"><h2>가상 체결</h2><span>최근 {data?.trades?.length || 0}건</span></div>
      <div className="table-wrap"><table><thead><tr><th>종목</th><th>구분</th><th>수량</th><th>체결가</th><th>수수료·세금</th><th>순손익</th><th>사유</th><th>시간</th></tr></thead><tbody>
        {(data?.trades || []).map((t) => <tr key={t.trade_id}><td>{t.code_name}</td><td>{t.event_type}</td><td>{t.quantity}주</td><td>{money(t.price)}</td><td>{money(t.fee)}</td><td>{t.event_type === "BUY" ? "—" : money(t.net_pnl)}</td><td>{REASONS[t.reason_code] || t.reason_code}</td><td>{timeLabel(t.event_at)}</td></tr>)}
      </tbody></table></div>
    </section>
  </main>;
}
