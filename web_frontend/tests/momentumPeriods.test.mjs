import test from "node:test";
import assert from "node:assert/strict";
import { periodSeries } from "../src/utils/momentumPeriods.js";

test("period chart keeps independent returns and splits missing observations", () => {
  const row = (day, value) => ({ snapshot_date: day, cumulative_return_pct: value });
  const chart = periodSeries([
    { id: "1m", account: { equity_history: [row("20261015", 1), row("20261019", 3)] } },
    { id: "3m", account: { equity_history: [row("20261015", -2), row("20261016", -1), row("20261019", 0)] } },
    { id: "6m" },
  ]);
  assert.deepEqual(chart.dates, ["20261015", "20261016", "20261019"]);
  assert.deepEqual(chart.series[0].segments.map(s => s.map(p => p.value)), [[1], [3]]);
  assert.deepEqual(chart.series[1].segments.map(s => s.map(p => p.value)), [[-2, -1, 0]]);
  assert.deepEqual(chart.series[2].segments, []);
});

test("uninitialized comparison has no synthetic history", () => {
  assert.deepEqual(periodSeries(), { dates: [], series: [] });
});
