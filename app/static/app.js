"use strict";
/* PayOps control room.
 * Everything shown here comes from the running API: /metrics (Prometheus text), /api/status,
 * /api/drills, and the authorization requests this page sends itself. */

const SLO_P95_MS = 300;
const ERR_THRESHOLD = 0.05;
const BUDGET_FRACTION = 0.005; // 99.5% objective
const TAPE_WINDOW_MS = 60000;
const TAPE_MAX_MS = 2000;
const HISTORY = 120; // seconds of chart history

/* ---------- pure helpers (unit-tested with node) ---------- */

function parseMetrics(text) {
  const out = [];
  const lineRe = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+0-9.eE]+|NaN|[+-]Inf)\s*$/;
  for (const line of text.split("\n")) {
    if (!line || line[0] === "#") continue;
    const m = lineRe.exec(line);
    if (!m) continue;
    const labels = {};
    if (m[2]) {
      const re = /([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"/g;
      let l;
      while ((l = re.exec(m[2]))) labels[l[1]] = l[2];
    }
    out.push({ name: m[1], labels, value: Number(m[3].replace("+Inf", "Infinity")) });
  }
  return out;
}

function authorizeSnapshot(samples) {
  const snap = { ok: 0, limited: 0, failed: 0, total: 0, buckets: [] };
  for (const s of samples) {
    if (s.labels.endpoint !== "/authorize") continue;
    if (s.name === "payops_requests_total") {
      const code = s.labels.status || "";
      if (code[0] === "5") snap.failed += s.value;
      else if (code[0] === "4") snap.limited += s.value;
      else snap.ok += s.value;
      snap.total += s.value;
    } else if (s.name === "payops_request_duration_seconds_bucket") {
      const le = s.labels.le === "+Inf" ? Infinity : Number(s.labels.le);
      snap.buckets.push([le, s.value]);
    }
  }
  snap.buckets.sort((a, b) => a[0] - b[0]);
  return snap;
}

function bucketDelta(cur, prev) {
  if (!prev || prev.length !== cur.length) return null;
  const d = cur.map(([le, c], i) => [le, c - prev[i][1]]);
  return d.some(([, c]) => c < 0) ? null : d;
}

function quantile(q, buckets) {
  if (!buckets || !buckets.length) return null;
  const total = buckets[buckets.length - 1][1];
  if (!(total > 0)) return null;
  const target = q * total;
  let prevLe = 0, prevCount = 0;
  for (const [le, count] of buckets) {
    if (count >= target) {
      if (le === Infinity) return prevLe;
      if (count === prevCount) return le;
      return prevLe + (le - prevLe) * ((target - prevCount) / (count - prevCount));
    }
    prevLe = le === Infinity ? prevLe : le;
    prevCount = count;
  }
  return prevLe;
}

/* Decide the one-sentence state of the service. m: {reachable, api, errRate, p95ms, rps} */
function statusFrom(m) {
  const fmt = (n) => Math.round(n);
  if (!m.reachable) {
    return { kind: "down", level: "bad", headline: "Can't reach the API.",
      sub: "This page gets no response from the server. Check that payops-api is running." };
  }
  if (m.api && m.api.redis_up === false) {
    return { kind: "redis", level: "bad", headline: "Redis is unreachable.",
      sub: "Authorizations return 503 until it comes back. The service fails closed on purpose." };
  }
  if (m.api && m.api.ready === false) {
    return { kind: "notready", level: "bad", headline: "The service says it is not ready.",
      sub: "The readiness check is failing. After a deploy, the script rolls back on its own." };
  }
  if (m.errRate != null && m.errRate > ERR_THRESHOLD) {
    return { kind: "errors", level: "bad", headline: `${fmt(m.errRate * 100)}% of authorizations are failing.`,
      sub: "The alert threshold is 5%. Open the runbook section for service down." };
  }
  if (m.p95ms != null && m.p95ms > SLO_P95_MS) {
    return { kind: "slow", level: "warn", headline: `Authorizations are slow: ${fmt(m.p95ms)} ms for the slowest 5%.`,
      sub: "The target is 300 ms. Requests still succeed, so error alerts stay quiet." };
  }
  const rate = m.rps != null ? m.rps : 0;
  return { kind: "ok", level: "ok", headline: "Everything is running normally.",
    sub: m.p95ms != null
      ? `The slowest 5% take ${fmt(m.p95ms)} ms against a 300 ms target, at ${rate.toFixed(1)} requests per second.`
      : "No requests in the last few seconds. Start test traffic to see live figures." };
}

if (typeof module !== "undefined") {
  module.exports = { parseMetrics, authorizeSnapshot, bucketDelta, quantile, statusFrom };
}

/* ---------- browser ---------- */

if (typeof document !== "undefined") {
  const $ = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const state = {
    snaps: [],            // [{t, snap}] last ~6 s
    hist: { p95: [], ok: [], limited: [], failed: [], err: [] },
    api: null,
    reachable: false,
    lastKind: null,
    pendingKind: null,
    pendingCount: 0,
    tape: [],             // [{t, cls, ms}]
    trafficTimer: null,
    inflight: 0,
    drillsLoaded: false,
  };

  /* --- clock --- */
  function tickClock() {
    $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false });
  }
  tickClock();
  setInterval(tickClock, 1000);

  /* --- event log --- */
  function logEvent(kind, text) {
    const list = $("events");
    const empty = list.querySelector(".empty");
    if (empty) empty.remove();
    const li = document.createElement("li");
    li.dataset.kind = kind;
    const t = document.createElement("time");
    t.textContent = new Date().toLocaleTimeString([], { hour12: false });
    const p = document.createElement("span");
    p.textContent = text;
    li.append(t, p);
    list.prepend(li);
    while (list.children.length > 40) list.lastChild.remove();
  }

  const EVENT_TEXT = {
    down: "Lost contact with the API.",
    redis: "Redis became unreachable. Authorizations are failing closed.",
    notready: "Readiness started failing.",
    errors: "More than 5% of authorizations are failing.",
    slow: "Slowest-5% response time went above the 300 ms target.",
    ok: "Back to normal.",
  };
  const EVENT_LEVEL = { down: "bad", redis: "bad", notready: "bad", errors: "bad", slow: "warn", ok: "ok" };

  /* --- polling --- */
  async function poll(withStatus) {
    try {
      const res = await fetch("/metrics", { cache: "no-store" });
      if (!res.ok) throw new Error("metrics " + res.status);
      const text = await res.text();
      state.reachable = true;
      ingest(authorizeSnapshot(parseMetrics(text)));
    } catch (e) {
      state.reachable = false;
    }
    if (withStatus) {
      try {
        const res = await fetch("/api/status", { cache: "no-store" });
        if (res.ok) state.api = await res.json();
      } catch (e) { /* reachability already reflected above */ }
    }
    render();
  }

  let lastIngestMs = 0;
  function ingest(snap) {
    const now = Date.now();
    const prev = state.snaps[state.snaps.length - 1];
    state.snaps.push({ t: now, snap });
    while (state.snaps.length > 7) state.snaps.shift();
    const cur = { ok: null, limited: null, failed: null, p95: null, err: null };

    if (prev && snap.total >= prev.snap.total) {
      const dt = Math.max((now - prev.t) / 1000, 0.001);
      cur.ok = (snap.ok - prev.snap.ok) / dt;
      cur.limited = (snap.limited - prev.snap.limited) / dt;
      cur.failed = (snap.failed - prev.snap.failed) / dt;
    } else if (prev) {
      state.snaps = [{ t: now, snap }]; // API restarted: counters reset
    }

    // windowed figures (about 5 s) for p95 and failing share
    const oldest = state.snaps[0];
    if (oldest && oldest !== state.snaps[state.snaps.length - 1]) {
      const d = bucketDelta(snap.buckets, oldest.snap.buckets);
      const q = d ? quantile(0.95, d) : null;
      cur.p95 = q != null ? q * 1000 : null;
      const reqs = snap.total - oldest.snap.total;
      const failed = snap.failed - oldest.snap.failed;
      cur.err = reqs > 0 && failed >= 0 ? failed / reqs : null;
    }
    state.cur = cur;
    state.latest = snap;
    const h = state.hist;
    for (const k of ["p95", "ok", "limited", "failed", "err"]) {
      h[k].push(cur[k]);
      if (h[k].length > HISTORY) h[k].shift();
    }
    lastIngestMs = now;
  }

  /* --- render --- */
  function fmtPct(n, d) { return n.toFixed(d) + "%"; }

  function render() {
    const cur = state.cur || {};
    const rps = cur.ok != null ? cur.ok + cur.limited + cur.failed : null;
    const st = statusFrom({ reachable: state.reachable, api: state.api, errRate: cur.err, p95ms: cur.p95, rps });
    $("status-line").textContent = st.headline;
    $("status-sub").textContent = st.sub;
    $("mark").dataset.state = st.level;

    if (state.lastKind !== st.kind) {
      if (state.pendingKind === st.kind) state.pendingCount += 1;
      else { state.pendingKind = st.kind; state.pendingCount = 1; }
      if (state.pendingCount >= 2 || st.kind === "down") {
        if (state.lastKind !== null && !(state.lastKind === "ok" && st.kind === "ok")) {
          logEvent(EVENT_LEVEL[st.kind], EVENT_TEXT[st.kind]);
        }
        state.lastKind = st.kind;
      }
    } else {
      state.pendingKind = null; state.pendingCount = 0;
    }

    // header chips
    const redisUp = state.api ? state.api.redis_up : null;
    $("redis-dot").dataset.state = redisUp == null ? "idle" : redisUp ? "ok" : "bad";
    $("redis-text").textContent = redisUp == null ? "Redis unknown" : redisUp ? "Redis connected" : "Redis unreachable";
    $("release-chip").textContent = state.api ? "Release " + state.api.version : "Release unknown";

    // figures
    const snap = state.latest;
    if (snap && snap.total > 0) {
      const okShare = 100 * (1 - snap.failed / snap.total);
      $("fig-avail").textContent = fmtPct(okShare, okShare >= 99.95 ? 2 : 1);
      const used = snap.failed / (BUDGET_FRACTION * snap.total);
      $("fig-budget").textContent = fmtPct(Math.max(0, 100 * (1 - used)), 0);
    } else {
      $("fig-avail").textContent = "–";
      $("fig-budget").textContent = "–";
    }
    $("fig-p95").textContent = cur.p95 != null ? Math.round(cur.p95) + " ms" : "–";
    $("fig-rps").textContent = rps != null ? rps.toFixed(1) : "–";
    $("fig-err").textContent = cur.err != null ? fmtPct(100 * cur.err, 1) : "–";

    drawCharts();
  }

  /* --- charts (hand-drawn SVG) --- */
  const NS = "http://www.w3.org/2000/svg";
  function el(name, attrs, text) {
    const e = document.createElementNS(NS, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  const W = 360, H = 130, PAD_T = 14, PAD_B = 18, PAD_L = 2, PAD_R = 2;
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  function frame(svg, max, unit, threshold, thresholdLabel) {
    svg.replaceChildren();
    const y = (v) => PAD_T + (H - PAD_T - PAD_B) * (1 - Math.min(v, max) / max);
    svg.append(el("line", { x1: PAD_L, x2: W - PAD_R, y1: H - PAD_B, y2: H - PAD_B, stroke: css("--rule"), "stroke-width": 1 }));
    svg.append(el("text", { x: PAD_L, y: 10 }, max.toFixed(max < 10 ? 1 : 0) + unit));
    svg.append(el("text", { x: PAD_L, y: H - 4 }, "2 min ago"));
    svg.append(el("text", { x: W - PAD_R, y: H - 4, "text-anchor": "end" }, "now"));
    if (threshold != null && threshold <= max) {
      svg.append(el("line", { x1: PAD_L, x2: W - PAD_R, y1: y(threshold), y2: y(threshold), stroke: css("--err"), "stroke-width": 1, "stroke-dasharray": "4 3" }));
      svg.append(el("text", { x: W - PAD_R, y: y(threshold) - 3, "text-anchor": "end" }, thresholdLabel));
    }
    return y;
  }
  const xAt = (i, n) => PAD_L + (W - PAD_L - PAD_R) * (HISTORY - n + i) / (HISTORY - 1);

  function drawLine(svg, series, max, unit, threshold, thresholdLabel, color) {
    const y = frame(svg, max, unit, threshold, thresholdLabel);
    let d = "", pen = false;
    series.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      d += (pen ? "L" : "M") + xAt(i, series.length).toFixed(1) + " " + y(v).toFixed(1) + " ";
      pen = true;
    });
    if (d) svg.append(el("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round" }));
  }

  function drawStack(svg) {
    const h = state.hist;
    const n = h.ok.length;
    let max = 1;
    for (let i = 0; i < n; i++) max = Math.max(max, (h.ok[i] || 0) + (h.limited[i] || 0) + (h.failed[i] || 0));
    max = Math.ceil(max * 1.1);
    const y = frame(svg, max, "/s");
    const bw = Math.max(1, (W - PAD_L - PAD_R) / HISTORY - 0.5);
    const layers = [["ok", css("--ok")], ["limited", css("--decline")], ["failed", css("--err")]];
    for (let i = 0; i < n; i++) {
      let base = 0;
      for (const [k, color] of layers) {
        const v = h[k][i] || 0;
        if (v <= 0) continue;
        const top = y(base + v), bottom = y(base);
        svg.append(el("rect", { x: xAt(i, n) - bw / 2, y: top, width: bw, height: Math.max(0, bottom - top), fill: color }));
        base += v;
      }
    }
  }

  function drawCharts() {
    const h = state.hist;
    const p95 = h.p95.filter((v) => v != null);
    const pmax = Math.max(600, Math.ceil((p95.length ? Math.max(...p95) : 0) * 1.15 / 100) * 100);
    drawLine($("ch-p95"), h.p95, pmax, " ms", SLO_P95_MS, "300 ms target", css("--ink"));
    drawStack($("ch-rps"));
    const errPct = h.err.map((v) => (v == null ? null : v * 100));
    const emax = Math.min(100, Math.max(10, Math.ceil(Math.max(0, ...errPct.filter((v) => v != null)) * 1.15 / 5) * 5));
    drawLine($("ch-err"), errPct, emax, "%", ERR_THRESHOLD * 100, "5% alert", css("--err"));
  }

  /* --- authorization tape (canvas) --- */
  const canvas = $("tape");
  const ctx = canvas.getContext("2d");
  const CLS_COLOR = { approved: "--ok", declined: "--decline", failed: "--err" };

  function yFor(ms, h, top, bottom) {
    const f = Math.log(1 + Math.min(ms, TAPE_MAX_MS)) / Math.log(1 + TAPE_MAX_MS);
    return bottom - (bottom - top) * f;
  }

  function drawTape() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width * dpr)), h = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const cw = rect.width, ch = rect.height;
    ctx.clearRect(0, 0, cw, ch);
    const top = 30, bottom = ch - 26;
    const now = Date.now();
    state.tape = state.tape.filter((e) => now - e.t <= TAPE_WINDOW_MS);

    // baseline and latency target
    ctx.strokeStyle = css("--rule");
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, bottom + 0.5); ctx.lineTo(cw, bottom + 0.5); ctx.stroke();
    const sy = yFor(SLO_P95_MS, ch, top, bottom);
    ctx.save();
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = css("--err");
    ctx.beginPath(); ctx.moveTo(0, sy + 0.5); ctx.lineTo(cw, sy + 0.5); ctx.stroke();
    ctx.restore();
    ctx.font = "12px " + css("--ui");
    ctx.fillStyle = css("--muted");
    ctx.textAlign = "left";
    ctx.fillText("60 s ago", 10, ch - 8);
    ctx.textAlign = "right";
    ctx.fillText("now", cw - 10, ch - 8);

    // ticks
    const tickW = 3;
    for (const e of state.tape) {
      const x = cw - ((now - e.t) / TAPE_WINDOW_MS) * cw;
      const y = yFor(e.ms, ch, top, bottom);
      ctx.fillStyle = css(CLS_COLOR[e.cls]);
      ctx.fillRect(x - tickW / 2, y, tickW, bottom - y);
    }

    // Target label is drawn after the ticks, on a backing box, so tall ticks never garble it.
    const label = "300 ms target";
    const lw = ctx.measureText(label).width;
    ctx.fillStyle = css("--panel");
    ctx.fillRect(cw - 10 - lw - 6, sy - 22, lw + 12, 17);
    ctx.fillStyle = css("--muted");
    ctx.textAlign = "right";
    ctx.fillText(label, cw - 10, sy - 8);

    // summary
    let ap = 0, dec = 0, fl = 0;
    for (const e of state.tape) { if (e.cls === "approved") ap++; else if (e.cls === "declined") dec++; else fl++; }
    const total = ap + dec + fl;
    const txt = total
      ? `Last 60 seconds: ${total} requests, ${ap} approved, ${dec} declined or limited, ${fl} failed`
      : "Last 60 seconds: no requests";
    $("tape-summary").textContent = txt;
    canvas.setAttribute("aria-label", "Live tape of authorization requests. " + txt);
    $("tape-empty").hidden = total > 0 || state.trafficTimer != null;
  }

  function loop() {
    drawTape();
    if (reduceMotion) setTimeout(loop, 1000);
    else requestAnimationFrame(loop);
  }

  /* --- test traffic --- */
  function randomBody() {
    return JSON.stringify({
      card_token: "card-" + (1 + Math.floor(Math.random() * 5000)),
      amount: Math.round((1 + Math.random() * 400) * 100) / 100,
      merchant_id: "m-" + (1 + Math.floor(Math.random() * 20)),
    });
  }

  // Browsers allow about 6 parallel connections per host. Staying below that means a slow API
  // slows the send rate instead of queueing requests in the browser, where they would time out
  // and show up as "failed" even though the service never saw them.
  const MAX_INFLIGHT = 5;

  async function sendOne() {
    if (state.inflight >= MAX_INFLIGHT) return;
    state.inflight++;
    const t0 = performance.now();
    let cls = "failed";
    try {
      const res = await fetch("/authorize", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: randomBody(),
        signal: AbortSignal.timeout(TAPE_MAX_MS + 500),
      });
      if (res.status === 200) {
        const j = await res.json();
        cls = String(j.decision || "").startsWith("declined") ? "declined" : "approved";
      } else if (res.status < 500) {
        cls = "declined";
      }
    } catch (e) { cls = "failed"; }
    finally { state.inflight--; }
    const ms = cls === "failed" && performance.now() - t0 > TAPE_MAX_MS ? TAPE_MAX_MS : performance.now() - t0;
    state.tape.push({ t: Date.now(), cls, ms });
  }

  function setTraffic(on) {
    if (state.trafficTimer) { clearInterval(state.trafficTimer); state.trafficTimer = null; }
    if (on) {
      const rps = Number($("rps").value);
      state.trafficTimer = setInterval(sendOne, 1000 / rps);
    }
    $("traffic-toggle").textContent = on ? "Stop test traffic" : "Start test traffic";
    $("tape-empty").hidden = on || state.tape.length > 0;
  }

  $("rps").addEventListener("input", () => {
    $("rps-out").textContent = $("rps").value;
    if (state.trafficTimer) setTraffic(true);
  });
  $("traffic-toggle").addEventListener("click", () => setTraffic(!state.trafficTimer));
  $("tape-start").addEventListener("click", () => setTraffic(true));

  /* --- fault controls --- */
  $("delay").addEventListener("input", () => { $("delay-out").textContent = $("delay").value; });

  async function sendFault(delay, fail) {
    const note = $("fault-note");
    try {
      const res = await fetch("/admin/fault", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ downstream_delay_ms: delay, downstream_fail: fail }),
      });
      if (!res.ok) throw new Error(String(res.status));
      note.textContent = delay === 0 && !fail
        ? "Risk scorer restored to normal."
        : `Risk scorer ${fail ? "set to fail" : "slowed by " + delay + " ms"}. Watch the tape.`;
    } catch (e) {
      note.textContent = "That did not work. Start the API with ENABLE_FAULT_INJECTION=1 to use these controls.";
    }
  }
  $("fault-apply").addEventListener("click", () => sendFault(Number($("delay").value), $("fail").checked));
  $("fault-clear").addEventListener("click", () => {
    $("delay").value = 0; $("delay-out").textContent = "0"; $("fail").checked = false;
    sendFault(0, false);
  });

  function syncFaultAvailability() {
    if (!state.api) return;
    const on = state.api.fault_injection_enabled;
    for (const id of ["delay", "fail", "fault-apply", "fault-clear"]) $(id).disabled = !on;
    $("fault-hint").textContent = on
      ? "Slow down or fail the risk scorer, then watch the tape, the figures and the event log react."
      : "Fault injection is off. Start the API with ENABLE_FAULT_INJECTION=1 to use these controls.";
  }

  /* --- drill results --- */
  const DRILL_NAMES = {
    redis_outage: "Redis stops",
    port_blocked: "Port blocked by a firewall rule",
    disk_full: "Disk fills up",
    high_latency: "Risk scorer slows down",
    bad_deploy: "Bad release is deployed",
  };

  async function loadDrills() {
    const box = $("drills");
    let rows = [];
    try {
      const res = await fetch("/api/drills", { cache: "no-store" });
      if (res.ok) rows = await res.json();
    } catch (e) { /* leave empty */ }
    box.replaceChildren();
    if (!rows.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No drill results yet. Run python drills/run_drill.py all to record them.";
      box.append(p);
      return;
    }
    const maxDetect = Math.max(...rows.map((r) => r.detect_s || 0), 0.001);
    const maxRecover = Math.max(...rows.map((r) => r.recover_s || 0), 0.001);
    const table = document.createElement("table");
    table.className = "drills";
    const head = table.createTHead().insertRow();
    for (const t of ["What broke", "Noticed after", "Back to normal after", "Failed requests"]) {
      const th = document.createElement("th"); th.scope = "col"; th.textContent = t; head.append(th);
    }
    const body = table.createTBody();
    for (const r of rows) {
      if (r.status !== "OK") continue;
      const tr = body.insertRow();
      tr.insertCell().textContent = DRILL_NAMES[r.drill] || r.drill;
      const a = tr.insertCell(); a.className = "num";
      a.textContent = r.detect_s + " s";
      const b1 = document.createElement("span"); b1.className = "bar"; b1.style.width = Math.max(2, 100 * r.detect_s / maxDetect) + "%"; a.append(b1);
      const c = tr.insertCell(); c.className = "num";
      c.textContent = r.recover_s + " s";
      const b2 = document.createElement("span"); b2.className = "bar rec"; b2.style.width = Math.max(2, 100 * r.recover_s / maxRecover) + "%"; c.append(b2);
      const d = tr.insertCell(); d.className = "num";
      d.textContent = `${r.failed_req} of ${r.total_req}`;
    }
    box.append(table);
    const note = document.createElement("p");
    note.className = "drill-note";
    note.textContent = "Measured on one host. A script applied each fix right after detection, so recovery times are a lower bound.";
    box.append(note);
  }

  /* --- start --- */
  let n = 0;
  async function pollLoop() {
    await poll(n % 2 === 0);
    if (n % 2 === 0) syncFaultAvailability();
    n++;
    setTimeout(pollLoop, 1000);
  }
  pollLoop();
  loadDrills();
  loop();

  const auto = Number(new URLSearchParams(location.search).get("traffic"));
  if (auto >= 1 && auto <= 40) { $("rps").value = auto; $("rps-out").textContent = String(auto); setTraffic(true); }
}
