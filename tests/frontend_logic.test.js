// Run with: node tests/frontend_logic.test.js  (pure functions from the dashboard, no browser needed)
const assert = require("assert");
const m = require("../app/static/app.js");

const text = [
  '# HELP payops_requests_total HTTP requests',
  'payops_requests_total{endpoint="/authorize",status="200"} 90',
  'payops_requests_total{endpoint="/authorize",status="429"} 4',
  'payops_requests_total{endpoint="/authorize",status="503"} 6',
  'payops_requests_total{endpoint="/ready",status="200"} 7',
  'payops_request_duration_seconds_bucket{endpoint="/authorize",le="0.1"} 50',
  'payops_request_duration_seconds_bucket{endpoint="/authorize",le="0.3"} 95',
  'payops_request_duration_seconds_bucket{endpoint="/authorize",le="+Inf"} 100',
].join("\n");

const snap = m.authorizeSnapshot(m.parseMetrics(text));
assert.deepStrictEqual([snap.ok, snap.limited, snap.failed, snap.total], [90, 4, 6, 100]);
assert.strictEqual(m.quantile(0.5, snap.buckets), 0.1);
assert.strictEqual(m.quantile(0.95, snap.buckets), 0.3);
assert.strictEqual(m.quantile(0.5, []), null);
assert.strictEqual(m.bucketDelta([[0.1, 1]], [[0.1, 5]]), null, "counter reset must not produce a delta");

const base = { reachable: true, api: { redis_up: true, ready: true }, errRate: 0, p95ms: 24, rps: 15 };
assert.strictEqual(m.statusFrom(base).kind, "ok");
assert.strictEqual(m.statusFrom({ ...base, p95ms: 812 }).kind, "slow");
assert.strictEqual(m.statusFrom({ ...base, errRate: 0.4 }).kind, "errors");
assert.strictEqual(m.statusFrom({ ...base, api: { redis_up: false, ready: false } }).kind, "redis");
assert.strictEqual(m.statusFrom({ ...base, api: { redis_up: true, ready: false } }).kind, "notready");
assert.strictEqual(m.statusFrom({ ...base, reachable: false }).kind, "down");
console.log("frontend logic: all checks passed");
