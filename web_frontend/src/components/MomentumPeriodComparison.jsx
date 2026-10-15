import { PERIOD_LABELS, PERIOD_COLORS, periodSeries } from "../utils/momentumPeriods";
import { momentumStatus } from "../utils/momentumStatus";

const pct = v => v == null ? "—" : `${Number(v).toFixed(2)}%`;
const dateLabel = s => s ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : "—";

function PeriodChart({ profiles }) {
  const { dates, series } = periodSeries(profiles);
  const values = series.flatMap(s => s.segments.flatMap(segment => segment.map(p => p.value)));
  if (!values.length) return <div className="chart-empty">세 가상계좌가 실행되면 기간별 수익률이 기록됩니다.</div>;
  const min = Math.min(-1, ...values), max = Math.max(1, ...values);
  const x = day => 60 + (dates.length === 1 ? 440 : dates.indexOf(day) / (dates.length - 1) * 880);
  const y = value => 30 + (max - value) / (max - min) * 220;
  return <>
    <div className="paper-legend">{series.map(s => <span key={s.id} style={{ color: PERIOD_COLORS[s.id] }}>{PERIOD_LABELS[s.id]}</span>)}</div>
    <svg className="daily-chart" viewBox="0 0 1000 300" role="img" aria-label="1개월 3개월 6개월 가상계좌 비용 차감 누적 수익률 비교">
      <line x1="60" x2="940" y1={y(0)} y2={y(0)} className="chart-zero-line" />
      {[min, 0, max].map(v => <text key={v} x="50" y={y(v) + 4} textAnchor="end" className="chart-axis-label">{pct(v)}</text>)}
      {series.map(s => <g key={s.id}>
        {s.segments.map((segment, i) => <polyline key={i} fill="none" stroke={PERIOD_COLORS[s.id]} strokeWidth="2.5" points={segment.map(p => `${x(p.day)},${y(p.value)}`).join(" ")} />)}
        {s.segments.flat().map(p => <circle key={p.day} cx={x(p.day)} cy={y(p.value)} r="3" fill={PERIOD_COLORS[s.id]}><title>{`${s.label} ${p.day}: ${pct(p.value)}`}</title></circle>)}
      </g>)}
      {dates.filter((_, i) => i % Math.max(1, Math.ceil(dates.length / 6)) === 0 || i === dates.length - 1).map(d => <text key={d} x={x(d)} y="280" textAnchor="middle" className="chart-axis-label">{`${d.slice(4, 6)}/${d.slice(6, 8)}`}</text>)}
    </svg>
  </>;
}

export default function MomentumPeriodComparison({ data, selected, onSelect }) {
  const profiles = data?.profiles || [];
  const checks = data?.comparison || {};
  const topPercent = profiles[0]?.account?.settings?.strategy?.top_percent ?? 2;
  const mismatch = data?.ready && Object.values(checks).some(v => !v);
  return <section className="panel">
    <div className="panel-title"><h2>1·3·6개월 동시 가상매매 비교</h2><span>기간별 독립 자금 · 공통 매매 조건</span></div>
    <p>완료된 일봉으로 (최근 종가 ÷ 21·63·126거래일 전 종가 − 1) × 100을 계산하고, 각 기간의 상위 {topPercent}%에 동일한 후속 조건을 적용합니다. 같은 종목도 계좌별로 독립 체결합니다.</p>
    <p className="metric-note">세 기간 모두 동일한 유효 일봉 종목 집합을 사용합니다. 기존 기록은 아래 ‘기존’ 계좌에서 확인할 수 있습니다. 비교 성과는 적용 후 관측한 틱부터 쌓이며, 과거 성과를 소급 생성하지 않습니다.</p>
    {mismatch && <div className="error-box">시작시각·자금/매매 조건·기준 일봉 중 일치하지 않는 항목이 있습니다. 동일한 비교 구간인지 확인하세요.</div>}
    <PeriodChart profiles={profiles} />
    <div className="table-wrap"><table><thead><tr><th>기간</th><th>시작일</th><th>최종 후보</th><th>수익률</th><th>MDD</th><th>완료 거래</th><th>승률</th><th>이익합 / 손실합</th><th>상태</th></tr></thead><tbody>
      {profiles.map(p => {
        const view = p.account, account = view?.accounts?.[0], perf = account?.performance;
        return <tr key={p.id}><td>{PERIOD_LABELS[p.id]}</td><td>{dateLabel(account?.created_at)}</td><td>{view?.stats?.qualified ?? "—"}</td><td>{pct(account?.cumulative_return_pct)}</td><td>{pct(account?.max_drawdown_pct)}</td><td>{perf?.completed_trade_count ?? "—"}</td><td>{perf?.completed_trade_count ? pct(perf.win_rate_pct) : "—"}</td><td>{perf?.profit_factor == null ? "—" : perf.profit_factor.toFixed(2)}</td><td>{p.error ? "데이터 조회 실패" : momentumStatus(view?.stats?.paper_execution).text}</td></tr>;
      })}
    </tbody></table></div>
    <p className="metric-note">초기자금·비용·위험 한도는 세 계좌에 똑같이 적용됩니다. 수익률은 비용 차감 후 평가액 기준, MDD는 수신 가격을 5초마다 관측한 기준입니다. 거래 수와 관측 기간이 충분히 쌓인 뒤 성과를 비교하세요.</p>
    <label>상세 계좌 선택　<select aria-label="모멘텀 상세 계좌" value={selected} onChange={e => onSelect(e.target.value)}>
      {Object.entries(PERIOD_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
    </select></label>
  </section>;
}
