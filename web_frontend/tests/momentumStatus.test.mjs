import test from "node:test";
import assert from "node:assert/strict";
import { momentumStatus } from "../src/utils/momentumStatus.js";

const now = Date.parse("2026-10-15T10:00:10+09:00");
const execution = { paper_enabled: true, live_new_buys_enabled: false, phase: "NO_CANDIDATES", heartbeat_at: "20261015100000" };

test("paper remains running with real new buys off and no candidates", () => {
  assert.equal(momentumStatus(execution, now).stale, false);
  assert.match(momentumStatus(execution, now).text, /후보 0개/);
});
test("Korean timestamps detect stale heartbeat without claiming live execution", () => {
  assert.equal(momentumStatus(execution, now + 21000).stale, true);
  assert.match(momentumStatus(undefined, now).text, /미수신/);
});
test("outside-session and late-start statuses stay distinct", () => {
  assert.equal(momentumStatus({ ...execution, phase: "MARKET_CLOSED" }, now+3600000).stale, false);
  assert.match(momentumStatus({ ...execution, phase: "OPENING_RANGE_MISSING" }, now).text, /미관측/);
});
