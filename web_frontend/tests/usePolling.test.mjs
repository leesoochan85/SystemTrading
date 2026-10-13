import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

// Drive the hook's effect and state setters without a browser or network.
async function harness(load) {
  const states = [];
  let effect, timer, cleanup;
  const context = vm.createContext({
    setInterval(fn) { timer = fn; return 1; },
    clearInterval() { timer = null; },
  });
  const react = new vm.SyntheticModule(["useState", "useEffect"], function () {
    this.setExport("useState", (initial) => {
      const index = states.length;
      states.push(initial);
      return [initial, (value) => {
        states[index] = typeof value === "function" ? value(states[index]) : value;
      }];
    });
    this.setExport("useEffect", (fn) => { effect = fn; });
  }, { context });
  const source = await readFile(new URL("../src/hooks/usePolling.js", import.meta.url), "utf8");
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    assert.equal(specifier, "react");
    return react;
  });
  await module.evaluate();
  const hook = module.namespace.default(load);
  cleanup = effect();
  return { states, tick: () => timer?.(), cleanup, setData: hook.setData,
    timer: () => timer };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("a slow request does not overlap the next polling interval", async () => {
  const request = deferred();
  let calls = 0;
  const h = await harness(() => { calls++; return request.promise; });
  h.tick(); h.tick();
  assert.equal(calls, 1);
  request.resolve({ sequence: 1 });
  await settle();
  assert.equal(h.states[0].sequence, 1);
  h.tick();
  assert.equal(calls, 2);
  h.cleanup();
});

test("cleanup ignores late results and clears future polling", async () => {
  const request = deferred();
  const h = await harness(() => request.promise);
  h.cleanup();
  assert.equal(h.timer(), null);
  request.resolve({ stale: true });
  await settle();
  assert.equal(h.states[0], null);
  assert.equal(h.states[1], "");
});

test("an API failure keeps previous data and recovery clears the error", async () => {
  let calls = 0;
  const h = await harness(async () => {
    if (++calls === 2) throw new Error("API unavailable");
    return { sequence: calls };
  });
  await settle();
  h.tick(); await settle();
  assert.equal(h.states[0].sequence, 1);
  assert.equal(h.states[1], "API unavailable");
  h.tick(); await settle();
  assert.equal(h.states[0].sequence, 3);
  assert.equal(h.states[1], "");
  h.setData((current) => ({ ...current, revision: 2 }));
  assert.equal(h.states[0].revision, 2);
  h.cleanup();
});

test("a rejected request after navigation cannot set an error", async () => {
  const old = deferred();
  const previous = await harness(() => old.promise);
  previous.cleanup();
  const current = await harness(async () => ({ name: "new strategy" }));
  old.reject(new Error("old strategy failed"));
  await settle();
  assert.equal(previous.states[1], "");
  assert.equal(current.states[0].name, "new strategy");
  current.cleanup();
});
