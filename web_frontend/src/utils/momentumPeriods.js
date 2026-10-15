export const PERIOD_LABELS = { "1m": "1개월 · 21거래일", "3m": "3개월 · 63거래일", "6m": "6개월 · 126거래일", baseline: "기존 · 126일 최저가 대비" };
export const PERIOD_COLORS = { "1m": "#51c4a0", "3m": "#65a8ff", "6m": "#f3bb67" };

// Missing observations split a line; they are never fabricated as zero returns.
export function periodSeries(profiles = []) {
  const dates = [...new Set(profiles.flatMap(p => (p.account?.equity_history || []).map(r => r.snapshot_date)))].sort();
  const series = profiles.map(p => {
    const byDate = new Map((p.account?.equity_history || []).map(r => [r.snapshot_date, r.cumulative_return_pct]));
    const segments = [];
    let current = [];
    for (const day of dates) {
      const value = byDate.get(day);
      if (Number.isFinite(value)) current.push({ day, value });
      else if (current.length) { segments.push(current); current = []; }
    }
    if (current.length) segments.push(current);
    return { id: p.id, label: p.label, segments };
  });
  return { dates, series };
}
