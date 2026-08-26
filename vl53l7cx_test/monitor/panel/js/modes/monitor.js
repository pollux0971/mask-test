// Monitor mode. C05 migrated the distance heatmap from the old single-file
// panel.html. C07 adds the signal-rate channel: press S to cycle
// distance -> signal -> both (A-dist/A-sig/B-dist/B-sig, 2x2), each
// channel keeps its own color scale so they can't be misread for one
// another, and signal uses a log scale (its dynamic range is much wider
// than distance's).
//
// Wire notes (see monitor.js's original C05 comment / T04/C03/C05 reports
// for how these were confirmed against live traffic, not assumed):
// - CONTRACTS.md ch.4 (SSE) is still unfrozen and the live shape changed
//   again mid-C07 (B19 landed the v2 parser): a "tof" event is now
//   {dim, dist:[...], signal:[...], valid:[...], seq, t_us} -- separate
//   arrays plus an explicit per-zone valid[], not the old concatenated
//   `values`. This reads that shape; if ch.4 freezes differently, whoever
//   changes it needs to update this file too. `dim` is zone count (16|64),
//   grid side is round(sqrt(dim)).
// - Invalid zones report -1 for BOTH d and s together (CONTRACTS.md 1.1)
//   and now also valid[i]===false; one shared invalid check covers both
//   channels either way.
// - zone layout (row-major) is still an unverified assumption per D11; the
//   warning badge covers both channels since it's the same physical data,
//   and it's rendered unconditionally (not tied to a specific channel's
//   visibility) so it's on screen no matter which view is selected.

import { registerMode } from "../shell.js";
import { dataStore } from "../bus.js";
import { melColorRgb } from "../draw/thumbnails.js";

const DIST_MIN = 0, DIST_MAX = 1200; // mm, clamps the distance color scale
const DIST_NEAR = [23, 73, 90];      // rgb, close object
const DIST_FAR = [223, 231, 226];    // rgb, far / no object nearby

function distColor(mm) {
  const t = Math.max(0, Math.min(1, mm / DIST_MAX));
  const r = Math.round(DIST_NEAR[0] + (DIST_FAR[0] - DIST_NEAR[0]) * t);
  const g = Math.round(DIST_NEAR[1] + (DIST_FAR[1] - DIST_NEAR[1]) * t);
  const b = Math.round(DIST_NEAR[2] + (DIST_FAR[2] - DIST_NEAR[2]) * t);
  return { rgb: `rgb(${r},${g},${b})`, luminance: (0.299 * r + 0.587 * g + 0.114 * b) / 255 };
}

// signal_per_spad/100 has no theoretical upper bound (CONTRACTS.md 1.1) but
// "實務值多落在 0-200" -- clamp the color scale there, log-spaced so a
// near-zero (unaimed / absorbing) zone is visibly distinct from a merely
// "not maximal" one instead of the whole low end looking the same.
// Deliberately a different family (violet -> amber) from distance's
// teal -> pale gray, per C07.md "色系明顯不同，不會誤讀".
const SIG_MIN = 1, SIG_MAX = 200;
const SIG_LOW = [45, 24, 74];    // deep violet: little/no reflection
const SIG_HIGH = [255, 191, 64]; // amber: strong reflection

function signalColor(s) {
  const clamped = Math.max(SIG_MIN, Math.min(SIG_MAX, Math.max(s, SIG_MIN)));
  const t = Math.log(clamped / SIG_MIN) / Math.log(SIG_MAX / SIG_MIN);
  const r = Math.round(SIG_LOW[0] + (SIG_HIGH[0] - SIG_LOW[0]) * t);
  const g = Math.round(SIG_LOW[1] + (SIG_HIGH[1] - SIG_LOW[1]) * t);
  const b = Math.round(SIG_LOW[2] + (SIG_HIGH[2] - SIG_LOW[2]) * t);
  return { rgb: `rgb(${r},${g},${b})`, luminance: (0.299 * r + 0.587 * g + 0.114 * b) / 255 };
}

// --- C06: Δ / z-score diverging scale -------------------------------------
//
// Both "delta" and "zscore" display modes share this color function -- the
// story requires Δ's color to be scaled by ±3σ too (not raw mm), so a zone
// with tiny baseline noise shows strong color for a small mm change while a
// noisy zone doesn't light up for nothing. Only the *displayed number*
// differs between the two modes (raw mm vs. the z-score itself).
const ZSCORE_CLAMP = 3;
const ZSCORE_DEADZONE = 0.5; // C06.md: "中間 ±0.5σ 用接近背景的深灰"
const Z_NEG = [64, 140, 226];   // blue: closer than baseline
const Z_POS = [226, 87, 76];    // red (reuses --warn's rgb): farther than baseline
const Z_NEUTRAL = [43, 50, 45]; // near --surface-2, reads as "background, nothing happening"

function lerpColor(a, b, t) {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return { rgb: `rgb(${r},${g},${bl})`, luminance: (0.299 * r + 0.587 * g + 0.114 * bl) / 255 };
}

function zscoreColor(z) {
  const clamped = Math.max(-ZSCORE_CLAMP, Math.min(ZSCORE_CLAMP, z));
  const target = clamped < 0 ? Z_NEG : Z_POS;
  const abs = Math.abs(clamped);
  if (abs <= ZSCORE_DEADZONE) {
    return lerpColor(Z_NEUTRAL, target, (abs / ZSCORE_DEADZONE) * 0.3);
  }
  const t = 0.3 + 0.7 * ((abs - ZSCORE_DEADZONE) / (ZSCORE_CLAMP - ZSCORE_DEADZONE));
  return lerpColor(Z_NEUTRAL, target, t);
}

function fmtDelta(mm) {
  return (mm >= 0 ? "+" : "") + mm.toFixed(1);
}

// unstable/suspectZeroVariance/noSignal mirror B10's own flags (B10.md:
// sigma > 2mm = unstable, ~0 = suspect, no valid samples = no signal) so a
// zone flagged by a real B10 baseline and a zone flagged by this client
// capture mean the same thing on screen.
function computeZoneStats(frames, dim) {
  const mu = new Array(dim).fill(NaN);
  const sigma = new Array(dim).fill(NaN);
  const unstable = [], suspectZeroVariance = [], noSignal = [];
  for (let z = 0; z < dim; z++) {
    const samples = [];
    for (const f of frames) {
      const validZone = f.valid ? f.valid[z] !== false : true;
      const v = f.dist[z];
      if (validZone && v != null && v >= 0) samples.push(v);
    }
    if (samples.length === 0) {
      noSignal.push(z);
      continue;
    }
    const m = samples.reduce((a, b) => a + b, 0) / samples.length;
    const variance = samples.length > 1
      ? samples.reduce((a, b) => a + (b - m) * (b - m), 0) / (samples.length - 1)
      : 0;
    const s = Math.sqrt(variance);
    mu[z] = m;
    sigma[z] = s;
    if (s > 2.0) unstable.push(z);
    else if (s < 0.05) suspectZeroVariance.push(z);
  }
  return { mu, sigma, unstable, suspectZeroVariance, noSignal };
}

function buildGrid(el, side) {
  el.style.gridTemplateColumns = `repeat(${side}, 1fr)`;
  el.style.gridTemplateRows = `repeat(${side}, 1fr)`;
  el.innerHTML = "";
  const cells = [];
  for (let i = 0; i < side * side; i++) {
    const c = document.createElement("div");
    c.className = "cell invalid";
    c.textContent = "·";
    el.appendChild(c);
    cells.push(c);
  }
  return cells;
}

// `validArr` is the frame's shared valid[] (one zone is invalid for both
// channels at once, CONTRACTS.md 1.1) -- falls back to "v < 0" if it's
// ever missing, since that sentinel still holds even when valid[] doesn't.
function renderChannel(cells, values, validArr, colorFn) {
  if (!cells || cells.length !== values.length) return;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    const invalid = v == null || v < 0 || (validArr && validArr[i] === false);
    const c = cells[i];
    if (invalid) {
      c.className = "cell invalid";
      c.textContent = "·";
      c.style.background = "var(--cell-invalid)";
      c.style.color = "";
    } else {
      const { rgb, luminance } = colorFn(v);
      c.className = "cell";
      c.textContent = v;
      c.style.background = rgb;
      c.style.color = luminance > 0.55 ? "#10140f" : "#f3f6f2";
    }
  }
}

// Distance-panel renderer for C06's absolute/delta/zscore modes (the
// signal panels keep using renderChannel/signalColor unchanged -- this
// story only applies to distance, see C06.md's own framing around the
// 0-1200mm scale hiding a ~17mm signal).
//
// Priority when a zone carries a B10-style baseline-quality flag: no_signal
// (no valid samples during capture -- NaN is NOT the same as "no change",
// per esp-mask-test-ad's explicit warning) beats suspect_zero_variance
// (baseline σ≈0, dividing by it would fabricate a huge/undefined z-score)
// beats unstable (σ>2mm, baseline itself was noisy). Only a zone with none
// of those three gets the plain |z|>2 "significant" outline -- a big
// z-score computed from an untrustworthy baseline isn't meaningful, so it
// doesn't get the same "trust me" treatment as one from a good baseline.
function renderDistanceChannel(cells, dValues, validArr, sensor, displayMode, baseline) {
  if (!cells || cells.length !== dValues.length) return;
  const dim = dValues.length;
  const hasBaseline = !!(baseline && baseline.dim === dim);
  const useBaseline = hasBaseline && displayMode !== "absolute";
  const mu = useBaseline ? (sensor === "A" ? baseline.muA : baseline.muB) : null;
  const sigma = useBaseline ? (sensor === "A" ? baseline.sigmaA : baseline.sigmaB) : null;
  const flags = useBaseline ? (sensor === "A" ? baseline.flagsA : baseline.flagsB) : null;

  for (let i = 0; i < dim; i++) {
    const v = dValues[i];
    const c = cells[i];
    const invalid = v == null || v < 0 || (validArr && validArr[i] === false);
    c.style.outline = "";

    if (invalid) {
      c.className = "cell invalid";
      c.textContent = "·";
      c.style.background = "var(--cell-invalid)";
      c.style.color = "";
      continue;
    }

    if (displayMode === "absolute") {
      const { rgb, luminance } = distColor(v);
      c.className = "cell";
      c.textContent = v;
      c.style.background = rgb;
      c.style.color = luminance > 0.55 ? "#10140f" : "#f3f6f2";
      continue;
    }

    if (!hasBaseline) {
      // Panel-level "尚無 baseline" note (toggled by the caller) already
      // says so; cells go blank rather than showing something that could
      // be mistaken for real Δ/z-score data.
      c.className = "cell invalid";
      c.textContent = "";
      c.style.background = "var(--cell-invalid)";
      c.style.color = "";
      continue;
    }

    if (flags.noSignal.includes(i)) {
      c.className = "cell no-signal";
      c.textContent = "N/A";
      c.style.background = "";
      c.style.color = "";
      continue;
    }

    const delta = v - mu[i];

    if (flags.suspectZeroVariance.includes(i)) {
      const sign = delta === 0 ? 0 : (delta > 0 ? 1 : -1);
      const { rgb, luminance } = zscoreColor(sign * ZSCORE_CLAMP);
      c.className = "cell suspect-zero-var";
      c.textContent = displayMode === "delta" ? fmtDelta(delta) : "σ≈0";
      c.style.background = rgb;
      c.style.color = luminance > 0.55 ? "#10140f" : "#f3f6f2";
      continue;
    }

    const z = delta / sigma[i];
    const { rgb, luminance } = zscoreColor(z);
    let cls = "cell";
    if (flags.unstable.includes(i)) cls += " unstable-baseline";
    else if (Math.abs(z) > 2) cls += " significant";
    c.className = cls;
    c.textContent = displayMode === "delta" ? fmtDelta(delta) : z.toFixed(1);
    c.style.background = rgb;
    c.style.color = luminance > 0.55 ? "#10140f" : "#f3f6f2";
  }
}

// --- C08: Mel spectrogram waterfall -----------------------------------
//
// Value range is FIXED, not auto-normalized: auto-normalizing to whatever's
// currently on screen would make "the sound stopped" look identical to
// "the sound is just quiet", which is exactly the contrast Demo step 3
// (氣音「四」→ Mel 崩潰、ToF 仍對) needs to be visible.
//
// ⚠️ The SSE `bands` values are NOT the wire's int16 = round(log_mel*100)
// scale -- confirmed by reading the live event stream directly (`/events`)
// rather than assuming: bridge_server.py's to_sse_event() forwards
// `event["log_mel"]`, and protocol.py's parser already divides the wire
// int16 back down to the float log_mel before that -- so a JS panel sees
// e.g. -6.06, not -606. First draft of this file used the wire's ×100
// scale directly and rendered a solid, uninformative color (everything
// clamped to one end); this is what live-testing this way is for. The
// fixed range below is log_mel itself: -10 matches reference_mel.py's own
// LOG_FLOOR (`log10(1e-10)`), 0 is `log10(power)` at power=1, comfortably
// above the loudest values seen live (~-1.2).
const MEL_MIN = -10, MEL_MAX = 0;
const MEL_BANDS = 40;
const MEL_HISTORY_COLUMNS = 320; // backing canvas width; ~5s at 62.5Hz, ~10s at 31.25Hz either way "至少幾秒"
const RMS_MAX = 32767; // CONTRACTS.md §1.1 $M's rms range (16-bit PCM amplitude)
const MEL_BG = [10, 8, 20];
const MEL_ENABLED_TIMEOUT_MS = 3000; // no $STATUS + no traffic this long -> treat mel as off

// melColorRgb (the magma-ish ramp, C08.md: "viridis 或 magma") moved to
// draw/thumbnails.js so C19's static thumbnails can reuse the exact same
// color math -- its MEL_MIN/MEL_MAX must stay -10/0 to match the constants
// above.

let warnedBadLength = false;

// --- C10: live PCA trajectory --------------------------------------------
//
// C10.md: "模型參數由 D03 算好透過 API 給前端，前端不做擬合" -- the real
// model comes from a server endpoint (GET /pca?model=tof_only|enrollment,
// per esp-mask-test-ad's coordination with B19). That endpoint doesn't
// exist yet, so this ships with a client-side live-fit STUB for tof_only:
// a genuine (not fabricated) PCA(2) fit via power iteration over a sliding
// window of live ToF frames, clearly badged as a stub with drifting axes.
// enrollment has no legitimate stand-in (its numbers are supposed to mean
// something specific -- faking them would be worse than not showing them),
// so it stays unavailable until the real endpoint responds.
//
// Model shape is deliberately generic (not "hardcode 64 dims") so a real
// server model swaps in with no code change:
//   { mean: number[], components: [number[], number[]], dims: number,
//     source: "tof_only" | "enrollment", stub: boolean,
//     explainedVarianceRatio: [number, number] | null }
//
// Feature vector for tof_only, built from what's actually on the wire
// today (CONTRACTS.md 3.2/3.3 style: distance + signal per sensor):
//   [A.dist(dim), A.sig(dim), B.dist(dim), B.sig(dim)]  -- length 4*dim
// (4*16 = 64 at the project's usual 4x4 resolution -- 64 isn't hardcoded,
// it falls out of whatever `dim` actually is right now.)

const PCA_TRAIL_MS = 2000;        // "最近 2 秒" per C10.md
const PCA_TRAIL_MAX_POINTS = 60;  // per C10.md's "60 個點"
const FIT_WINDOW_MS = 15000;      // sliding window the stub fits from
const REFIT_INTERVAL_MS = 2000;
const MIN_FIT_SAMPLES = 40;       // well above the 64-dim rank-deficiency floor
const SERVER_MODEL_CHECK_MS = 10000; // auto-upgrade stub -> real once the endpoint exists

function vecSub(vec, mean) {
  const out = new Array(vec.length);
  for (let i = 0; i < vec.length; i++) out[i] = vec[i] - mean[i];
  return out;
}

function dot(a, b) {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

function normalize(v) {
  const n = Math.sqrt(dot(v, v)) || 1;
  return v.map((x) => x / n);
}

function matVec(flatMat, n, v) {
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    let s = 0;
    const base = i * n;
    for (let j = 0; j < n; j++) s += flatMat[base + j] * v[j];
    out[i] = s;
  }
  return out;
}

// Top eigenvector/eigenvalue of a symmetric matrix via power iteration --
// avoids needing a full eigendecomposition library for a 2-component PCA.
function powerIteration(flatMat, n, iterations = 60) {
  let v = normalize(Array.from({ length: n }, () => Math.random() - 0.5));
  for (let it = 0; it < iterations; it++) {
    v = normalize(Array.from(matVec(flatMat, n, v)));
  }
  const eigenvalue = dot(Array.from(matVec(flatMat, n, v)), v);
  return { vector: v, eigenvalue };
}

// samples: array of same-length plain arrays. Returns a tof_only stub model.
function fitPCA2Stub(samples) {
  const m = samples.length;
  const n = samples[0].length;
  const mean = new Array(n).fill(0);
  for (const s of samples) for (let i = 0; i < n; i++) mean[i] += s[i];
  for (let i = 0; i < n; i++) mean[i] /= m;

  const cov = new Float64Array(n * n);
  for (const s of samples) {
    const c = vecSub(s, mean);
    for (let i = 0; i < n; i++) {
      const ci = c[i];
      const base = i * n;
      for (let j = 0; j < n; j++) cov[base + j] += ci * c[j];
    }
  }
  for (let k = 0; k < cov.length; k++) cov[k] /= Math.max(1, m - 1);

  const pc1 = powerIteration(cov, n);
  const deflated = new Float64Array(n * n);
  for (let i = 0; i < n; i++) {
    const base = i * n;
    for (let j = 0; j < n; j++) {
      deflated[base + j] = cov[base + j] - pc1.eigenvalue * pc1.vector[i] * pc1.vector[j];
    }
  }
  const pc2 = powerIteration(deflated, n);

  let totalVariance = 0;
  for (let i = 0; i < n; i++) totalVariance += cov[i * n + i];
  totalVariance = totalVariance || 1e-9;

  return {
    mean,
    components: [pc1.vector, pc2.vector],
    dims: n,
    source: "tof_only",
    stub: true,
    explainedVarianceRatio: [pc1.eigenvalue / totalVariance, pc2.eigenvalue / totalVariance],
  };
}

function projectPCA(model, vec) {
  const c = vecSub(vec, model.mean);
  return [dot(c, model.components[0]), dot(c, model.components[1])];
}

async function tryFetchServerPcaModel(source) {
  try {
    const res = await fetch(`/pca?model=${encodeURIComponent(source)}`);
    if (!res.ok) return null;
    const json = await res.json();
    if (!json || !Array.isArray(json.mean) || !Array.isArray(json.components)) return null;
    return {
      mean: json.mean,
      components: json.components,
      dims: json.mean.length,
      source: json.source || source,
      stub: false,
      explainedVarianceRatio: json.explained_variance_ratio || json.explainedVarianceRatio || null,
    };
  } catch {
    return null; // endpoint doesn't exist yet (expected right now) or the network hiccuped
  }
}

const SENSORS = ["A", "B"];
const CHANNELS = ["dist", "sig"];
const VIEW_MODES = ["distance", "signal", "both"];
const DISPLAY_MODES = ["absolute", "delta", "zscore"];
const DISPLAY_MODE_LABEL = { absolute: "絕對距離", delta: "Δ 距離", zscore: "z-score" };
const PANEL_LABEL = { dist: "距離", sig: "訊號" };
const BASELINE_CAPTURE_MS = 2000;      // C06.md: "按 B 現場擷取 2 秒"
const BASELINE_STALE_MS = 10 * 60 * 1000; // C06.md: 超過 10 分鐘建議重新擷取

function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

// --- C09: quality dashboard -------------------------------------------
//
// "前端只負責顯示，所有判定邏輯在 B19" (C09.md) -- this never computes a
// green/yellow/red level itself, only formats and colors whatever level
// the quality event already carries. The six metrics below are what
// host/quality/metrics.py actually emits (confirmed live: CONTRACTS.md
// 4.2 + a real SSE capture), NOT the seven C09.md's own implementation
// table lists -- that table has "基線穩定度"/"即時SNR" which don't exist
// on the wire, and is missing "bandwidth" which does. Same class of stale
// story-table issue as C05's "2 x dim^2". See the completion report.
const QUALITY_METRICS = [
  { key: "drop_rate", label: "掉幀率", format: (v) => (v * 100).toFixed(2) + "%", provenance: "暫定" },
  { key: "valid_zones", label: "有效 zone", format: (v) => (v * 100).toFixed(0) + "%", provenance: "暫定" },
  { key: "symmetry", label: "左右對稱性", format: (v) => (v * 100).toFixed(1) + "%", provenance: "暫定" },
  { key: "clock_resid", label: "對齊殘差", format: (v) => (v * 1000).toFixed(2) + " ms", provenance: "契約" },
  { key: "noise_floor", label: "音訊底噪", format: (v) => Math.round(v).toString(), provenance: "暫定" },
  { key: "bandwidth", label: "鏈路使用率", format: (v) => (v * 100).toFixed(0) + "%", provenance: "契約" },
];
// [暫定]/[契約] above is a static snapshot of config/quality_thresholds.json's
// _source field at the time this was written (esp-mask-test-ad suggested
// this, "建議不是要求") -- there's no endpoint serving that file to the
// browser, so this doesn't auto-update if B19 revises it. A real fetch
// would need a new route, same category of gap as C10's /pca.

const QUALITY_LEVEL_COLOR = {
  green: "#5fbf7a",  // var(--good)
  yellow: "#e8a33d", // var(--accent)
  red: "#e2574c",    // var(--warn)
  unknown: "#8b968e", // var(--text-dim) -- deliberately NOT green; see below
};
const QUALITY_SPARKLINE_MS = 60000; // C09.md: "60 秒趨勢迷你圖"
const DEVICE_STATE_CHECK_MS = 2000;

function drawSparkline(canvas, ctx, points) {
  if (!canvas || !ctx) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (w === 0 || h === 0) return;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  ctx.clearRect(0, 0, w, h);
  if (points.length < 2) return;
  const values = points.map((p) => p.value);
  const minV = Math.min(...values), maxV = Math.max(...values);
  const span = Math.max(maxV - minV, 1e-9);
  const lastLevel = points[points.length - 1].level;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = (i / (points.length - 1)) * w;
    const y = h - ((p.value - minV) / span) * (h - 4) - 2;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = QUALITY_LEVEL_COLOR[lastLevel] || QUALITY_LEVEL_COLOR.unknown;
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

registerMode("monitor", (() => {
  const gridEls = {};   // "A-dist" -> element
  const rateEls = {};   // "A" -> element (shared by both channel panels for that sensor)
  const cells = {};     // "A-dist" -> cell array
  const currentDim = { A: null, B: null };
  const latestFrame = { A: null, B: null }; // { dim, dValues, sValues }
  const rateCounters = { A: [], B: [] };
  let rafId = null;
  let viewMode = "distance";
  let gridsContainer = null;

  // --- C06: Δ / z-score baseline state ---
  let displayMode = "absolute";
  let baseline = null; // { dim, muA, sigmaA, muB, sigmaB, flagsA, flagsB, capturedAt, source }
  let baselineStatusEl = null;
  const baselineNoteEls = { A: null, B: null };
  let displayModeTagEls = [];

  function updateDisplayModeTag() {
    const text = `[${DISPLAY_MODE_LABEL[displayMode]}]`;
    displayModeTagEls.forEach((el) => { el.textContent = text; });
  }

  // --- C09: quality dashboard state ---
  const qualityEls = { value: {}, dot: {}, hint: {}, sparkCanvas: {}, sparkCtx: {} };
  const lastLevels = {}; // key -> "green"|"yellow"|"red"|"unknown", for the record-confirm gate
  let alarmBannerEl = null;
  let reflashNoteEl = null;
  let recBtn = null, recSecondsEl = null, recStatusEl = null;
  let lastDeviceStateCheckAt = 0;

  function handleQualityEvent(evt) {
    const metrics = evt.metrics || {};
    for (const m of QUALITY_METRICS) {
      const info = metrics[m.key];
      // Missing metric entirely (shouldn't happen given the six are always
      // sent, but don't assume) is treated the same as "unknown": still
      // not green.
      const level = info ? info.level : "unknown";
      lastLevels[m.key] = level;

      qualityEls.value[m.key].textContent = (info && typeof info.value === "number") ? m.format(info.value) : "--";

      const dot = qualityEls.dot[m.key];
      dot.className = "quality-level-dot level-" + level;
      dot.title = level === "unknown" ? "unknown（尚無門檻或尚無資料，不是綠燈）" : level;

      const hintEl = qualityEls.hint[m.key];
      if (level !== "green" && info && info.hint) {
        hintEl.textContent = info.hint;
        hintEl.style.display = "block";
      } else {
        hintEl.textContent = "";
        hintEl.style.display = "none";
      }
    }

    // Sparklines read straight from dataStore's "quality" stream (C09
    // extended bus.js for this) rather than keeping a second, redundant
    // history buffer here.
    const history = dataStore.getRecent("quality", QUALITY_SPARKLINE_MS);
    for (const m of QUALITY_METRICS) {
      const points = history
        .map((e) => {
          const info = e.metrics && e.metrics[m.key];
          return info && typeof info.value === "number" ? { value: info.value, level: info.level } : null;
        })
        .filter(Boolean);
      drawSparkline(qualityEls.sparkCanvas[m.key], qualityEls.sparkCtx[m.key], points);
    }

    // alarms: seq-gap-vs-$H mismatch, a transport fault signal -- kept
    // visually separate from the drop_rate card per B19's explicit
    // instruction, not folded into it.
    const alarms = Array.isArray(evt.alarms) ? evt.alarms : [];
    if (alarmBannerEl) {
      if (alarms.length) {
        alarmBannerEl.textContent = "⚠ 傳輸層警報：" + alarms.join("；");
        alarmBannerEl.style.display = "block";
      } else {
        alarmBannerEl.style.display = "none";
      }
    }

    maybeCheckDeviceState(performance.now());
  }

  // §4.1.2: a resolution switch resets seq (expected -- $STATUS is the
  // session boundary, CONTRACTS.md 1.3), which would otherwise look like a
  // fault on this dashboard. Poll /device/state (a real, existing B18
  // endpoint) so a note can say "this is a reflash" instead of the metrics
  // just looking alarming for no visible reason.
  function maybeCheckDeviceState(now) {
    if (now - lastDeviceStateCheckAt < DEVICE_STATE_CHECK_MS) return;
    lastDeviceStateCheckAt = now;
    fetch("/device/state")
      .then((r) => (r.ok ? r.json() : null))
      .then((state) => {
        if (!reflashNoteEl) return;
        reflashNoteEl.style.display = state && state.resolution_change_in_progress ? "block" : "none";
      })
      .catch(() => {
        // transient fetch failure -- leave the note as it was, don't spam
      });
  }

  function anyRedLevel() {
    return QUALITY_METRICS.some((m) => lastLevels[m.key] === "red");
  }

  function onRecordClick() {
    if (anyRedLevel()) {
      const redLabels = QUALITY_METRICS.filter((m) => lastLevels[m.key] === "red").map((m) => m.label).join("、");
      const proceed = window.confirm(`目前有紅燈指標（${redLabels}），確定要開始錄製嗎？`);
      if (!proceed) return;
    }
    const seconds = recSecondsEl.value;
    recStatusEl.textContent = "啟動中…";
    fetch(`/record?seconds=${seconds}`, { method: "POST" })
      .then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        recStatusEl.textContent = "已送出錄製指令";
      })
      .catch((err) => {
        recStatusEl.textContent = "失敗：" + err.message;
      });
  }

  function captureBaseline() {
    const now = performance.now();
    const recentA = dataStore.getRecent("tofA", BASELINE_CAPTURE_MS);
    const recentB = dataStore.getRecent("tofB", BASELINE_CAPTURE_MS);
    if (!recentA.length || !recentB.length) return; // nothing buffered yet -- can't capture from nothing
    const dim = recentA[recentA.length - 1].dim;
    const framesA = recentA.filter((f) => f.dim === dim);
    const framesB = recentB.filter((f) => f.dim === dim);
    if (!framesA.length || !framesB.length) return;
    const statsA = computeZoneStats(framesA, dim);
    const statsB = computeZoneStats(framesB, dim);
    baseline = {
      dim,
      muA: statsA.mu, sigmaA: statsA.sigma,
      muB: statsB.mu, sigmaB: statsB.sigma,
      flagsA: { unstable: statsA.unstable, suspectZeroVariance: statsA.suspectZeroVariance, noSignal: statsA.noSignal },
      flagsB: { unstable: statsB.unstable, suspectZeroVariance: statsB.suspectZeroVariance, noSignal: statsB.noSignal },
      capturedAt: now,
      source: "manual", // B10's own session baseline has no live delivery endpoint yet -- see completion report
    };
    updateBaselineStatus();
  }

  function updateBaselineStatus() {
    for (const sensor of SENSORS) {
      if (baselineNoteEls[sensor]) {
        const show = displayMode !== "absolute" && !(baseline && baseline.dim === currentDim[sensor]);
        baselineNoteEls[sensor].style.display = show ? "flex" : "none";
      }
    }
    if (!baselineStatusEl) return;
    if (!baseline) {
      baselineStatusEl.textContent = "尚無 baseline —— 按 B 現場擷取 2 秒";
      baselineStatusEl.className = "baseline-status mono";
      return;
    }
    const elapsedMs = performance.now() - baseline.capturedAt;
    const elapsedMin = elapsedMs / 60000;
    const clock = new Date(Date.now() - elapsedMs).toLocaleTimeString("zh-Hant-TW", { hour12: false });
    const stale = elapsedMs > BASELINE_STALE_MS;
    baselineStatusEl.textContent =
      `baseline：${clock} 擷取（${elapsedMin.toFixed(1)} 分鐘前）${stale ? " —— 已超過 10 分鐘，建議按 B 重新擷取" : ""}`;
    baselineStatusEl.className = "baseline-status mono" + (stale ? " stale" : "");
  }

  // --- C10: PCA trajectory state ---
  let pcaCanvas = null, pcaCtx = null, pcaBadgeEl = null, pcaVarianceEl = null;
  let pcaModel = null;
  let fitWindow = []; // { t, vec }
  let trail = [];      // { t, x, y }
  let lastFitAt = 0;
  let lastServerCheckAt = 0;

  // --- C08: Mel spectrogram waterfall state ---
  let melCanvas = null, melCtx = null, melBadgeEl = null, melHzEl = null;
  let pendingMelColumns = []; // Int16Array/number[40][], queued in onData, drained in paint()
  let pendingRmsColumns = []; // number[] (rms), same queue discipline for the degraded view
  let lastMelEventAt = 0;
  let melActive = null; // null = not painted yet; true/false = which view was last drawn
  let melCanvasReady = false;

  function combinedVectorNow() {
    const a = latestFrame.A, b = latestFrame.B;
    if (!a || !b || a.dim !== b.dim) return null;
    return a.dValues.concat(a.sValues, b.dValues, b.sValues);
  }

  function setModel(model) {
    const axesChanged = !pcaModel || pcaModel.source !== model.source || pcaModel.dims !== model.dims;
    pcaModel = model;
    if (axesChanged) trail = []; // different model = different axes; old points would mislead (esp-mask-test-ad's instruction
    updatePcaBadge();
  }

  function updatePcaBadge() {
    if (!pcaBadgeEl) return;
    if (!pcaModel) {
      pcaBadgeEl.textContent = "PCA 模型：累積資料中…";
      pcaBadgeEl.className = "pca-model-badge mono";
    } else {
      const label = pcaModel.source === "enrollment" ? "enrollment（104 維）" : "ToF-only（64 維）";
      const stubTag = pcaModel.stub ? "，即時擬合、座標軸會漂移" : "";
      pcaBadgeEl.textContent = `PCA 模型：${label}${stubTag}`;
      pcaBadgeEl.className = "pca-model-badge mono " + (pcaModel.stub ? "stub" : "live");
    }
    if (pcaVarianceEl) {
      const evr = pcaModel && pcaModel.explainedVarianceRatio;
      pcaVarianceEl.textContent = Array.isArray(evr) && evr.length >= 2
        ? `PC1 ${(evr[0] * 100).toFixed(1)}% + PC2 ${(evr[1] * 100).toFixed(1)}% = ${((evr[0] + evr[1]) * 100).toFixed(1)}%`
        : "解釋變異比例：N/A";
    }
  }

  function pcaBookkeeping(now) {
    const vec = combinedVectorNow();
    if (vec) {
      fitWindow.push({ t: now, vec });
      while (fitWindow.length && now - fitWindow[0].t > FIT_WINDOW_MS) fitWindow.shift();
    }

    if ((!pcaModel || pcaModel.stub) && now - lastFitAt >= REFIT_INTERVAL_MS) {
      const n = vec ? vec.length : null;
      const samples = n ? fitWindow.filter((s) => s.vec.length === n).map((s) => s.vec) : [];
      if (samples.length >= MIN_FIT_SAMPLES) {
        setModel(fitPCA2Stub(samples));
        lastFitAt = now;
      }
    }

    if (now - lastServerCheckAt >= SERVER_MODEL_CHECK_MS) {
      lastServerCheckAt = now;
      const wanted = pcaModel && pcaModel.source === "enrollment" ? "enrollment" : "tof_only";
      tryFetchServerPcaModel(wanted).then((model) => {
        if (model) setModel(model); // upgrades stub -> real automatically once the endpoint exists
      });
    }

    if (pcaModel && vec && vec.length === pcaModel.dims) {
      const [x, y] = projectPCA(pcaModel, vec);
      trail.push({ t: now, x, y });
    }
    while (trail.length && now - trail[0].t > PCA_TRAIL_MS) trail.shift();
    while (trail.length > PCA_TRAIL_MAX_POINTS) trail.shift();
  }

  function resizePcaCanvas() {
    if (!pcaCanvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = pcaCanvas.clientWidth, h = pcaCanvas.clientHeight;
    if (w === 0 || h === 0) return;
    pcaCanvas.width = w * dpr;
    pcaCanvas.height = h * dpr;
    pcaCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function drawTrail() {
    if (!pcaCtx) return;
    const w = pcaCanvas.clientWidth, h = pcaCanvas.clientHeight;
    // Redraw from scratch every frame -- C10.md explicitly warns that
    // canvas globalAlpha stacking accumulates ghosting instead of a clean
    // fade, same lesson as the old mic waveform's per-frame clearRect.
    pcaCtx.clearRect(0, 0, w, h);
    if (trail.length < 2) return;

    // Autoscale to the trail's own spread: a stub's axes drift over time
    // (new fits rotate/rescale the components), so a fixed scale would
    // eventually push the trajectory off-canvas.
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const p of trail) {
      if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y;
    }
    const spanX = Math.max(maxX - minX, 1e-6), spanY = Math.max(maxY - minY, 1e-6);
    const scale = Math.min((w * 0.7) / spanX, (h * 0.7) / spanY);
    const midX = (minX + maxX) / 2, midY = (minY + maxY) / 2;
    const cx = w / 2, cy = h / 2;

    const now = performance.now();
    for (let i = 0; i < trail.length; i++) {
      const p = trail[i];
      const alpha = Math.max(0, 1 - (now - p.t) / PCA_TRAIL_MS);
      const sx = cx + (p.x - midX) * scale;
      const sy = cy - (p.y - midY) * scale; // flip: screen y grows down, PCA space doesn't
      pcaCtx.beginPath();
      pcaCtx.arc(sx, sy, 3 + 2 * alpha, 0, Math.PI * 2);
      pcaCtx.fillStyle = `rgba(232, 163, 61, ${alpha.toFixed(3)})`;
      pcaCtx.fill();
    }
    const last = trail[trail.length - 1];
    pcaCtx.beginPath();
    pcaCtx.arc(cx + (last.x - midX) * scale, cy - (last.y - midY) * scale, 6, 0, Math.PI * 2);
    pcaCtx.strokeStyle = "#f3f6f2";
    pcaCtx.lineWidth = 1.5;
    pcaCtx.stroke();
  }

  // --- C08: Mel spectrogram waterfall --------------------------------
  //
  // Backing canvas is MEL_HISTORY_COLUMNS x MEL_BANDS pixels (one pixel
  // per band per frame), scaled up with CSS + image-rendering:pixelated
  // (monitor.css) so band boundaries stay crisp instead of blurring --
  // cheap because the actual pixel-pushing work is 40 pixels per new
  // frame via putImageData, not a redraw of the visible (much larger)
  // element size.
  function resizeMelCanvas() {
    if (!melCanvas) return;
    melCanvas.width = MEL_HISTORY_COLUMNS;
    melCanvas.height = MEL_BANDS;
    melCtx.fillStyle = `rgb(${MEL_BG[0]},${MEL_BG[1]},${MEL_BG[2]})`;
    melCtx.fillRect(0, 0, MEL_HISTORY_COLUMNS, MEL_BANDS);
    melCanvasReady = true;
  }

  // C08.md's own implementation hint, applied here:
  //   ctx.putImageData(ctx.getImageData(1, 0, w-1, h), 0, 0);
  //   drawColumn(w - 1, melFrame);
  // Shifts existing pixels one column left, then paints only the new
  // rightmost column -- never a full-canvas redraw.
  function shiftMelCanvasLeft() {
    const w = MEL_HISTORY_COLUMNS, h = MEL_BANDS;
    melCtx.putImageData(melCtx.getImageData(1, 0, w - 1, h), 0, 0);
  }

  // bands[0] is the lowest mel band (fmin=80Hz, CONTRACTS.md §3.1); drawn
  // at the bottom row so the image reads like a conventional spectrogram
  // (frequency increases upward).
  function drawMelColumn(bands) {
    shiftMelCanvasLeft();
    const h = MEL_BANDS;
    const col = melCtx.createImageData(1, h);
    for (let b = 0; b < h; b++) {
      const rowFromTop = h - 1 - b;
      const [r, g, bl] = melColorRgb(bands[b]);
      const idx = rowFromTop * 4;
      col.data[idx] = r; col.data[idx + 1] = g; col.data[idx + 2] = bl; col.data[idx + 3] = 255;
    }
    melCtx.putImageData(col, MEL_HISTORY_COLUMNS - 1, 0);
  }

  // Degraded view (no $F): a solid-color column per $M frame, brightness =
  // loudness. Deliberately a different texture (flat bar vs. banded
  // spectrogram) from the real waterfall so the degraded state is visibly,
  // not just textually, distinct -- on top of the badge text below.
  function drawRmsColumn(rms) {
    shiftMelCanvasLeft();
    const h = MEL_BANDS;
    const t = Math.max(0, Math.min(1, rms / RMS_MAX));
    const [r, g, bl] = melColorRgb(MEL_MIN + t * (MEL_MAX - MEL_MIN));
    const col = melCtx.createImageData(1, h);
    for (let row = 0; row < h; row++) {
      const idx = row * 4;
      col.data[idx] = r; col.data[idx + 1] = g; col.data[idx + 2] = bl; col.data[idx + 3] = 255;
    }
    melCtx.putImageData(col, MEL_HISTORY_COLUMNS - 1, 0);
  }

  // $STATUS's `mel` field (CONTRACTS.md §1.1.2) is the authoritative source
  // once it has arrived. Before the first $STATUS (or on firmware too old
  // to send the field -- it's optional per §1.1.2), fall back to "have we
  // actually seen a $F frame recently" -- real data outranks absent
  // metadata. A12 may never ship at all (depends on A10's spike result,
  // per A12.md), so this must default to the degraded view, not to "on".
  function isMelEnabled() {
    const status = dataStore.getLatestStatus();
    if (status && typeof status.mel === "boolean") return status.mel;
    return lastMelEventAt > 0 && (performance.now() - lastMelEventAt) < MEL_ENABLED_TIMEOUT_MS;
  }

  // §1.1.2 exists precisely so this doesn't have to guess: A14 changed $F
  // from 31.25 to 62.5 Hz without changing the wire shape, so the rate is
  // read from sr/mel_hop every time, never assumed.
  function melRateLabel() {
    const status = dataStore.getLatestStatus();
    if (status && typeof status.sr === "number" && typeof status.mel_hop === "number" && status.mel_hop > 0) {
      return (status.sr / status.mel_hop).toFixed(1) + " Hz";
    }
    return "幀率未知（等待 $STATUS）";
  }

  function paintMelWaterfall() {
    if (!melCanvas || !melCanvasReady) return;
    const enabled = isMelEnabled();
    if (enabled !== melActive) {
      // View switch: clear so the outgoing view's pixels don't linger
      // underneath the incoming one, and drop any queued backlog from the
      // view we're leaving -- it belongs to a different visualization.
      melCtx.fillStyle = `rgb(${MEL_BG[0]},${MEL_BG[1]},${MEL_BG[2]})`;
      melCtx.fillRect(0, 0, MEL_HISTORY_COLUMNS, MEL_BANDS);
      pendingMelColumns = [];
      pendingRmsColumns = [];
      melActive = enabled;
    }

    if (enabled) {
      for (const bands of pendingMelColumns) drawMelColumn(bands);
      pendingMelColumns = [];
    } else {
      for (const rms of pendingRmsColumns) drawRmsColumn(rms);
      pendingRmsColumns = [];
    }

    if (melBadgeEl) {
      melBadgeEl.textContent = enabled ? "Mel 頻譜（62.5 Hz 前為 31.25 Hz，見右側幀率）" : "⚠ Mel 未啟用 — 顯示 RMS 包絡";
      melBadgeEl.className = "mel-waterfall-badge mono" + (enabled ? "" : " degraded");
    }
    if (melHzEl) melHzEl.textContent = enabled ? melRateLabel() : "";

    drawPendingVadMarks();
  }

  // --- C08: VAD start/end overlay (B15/B16) ---------------------------
  //
  // No live producer exists for this yet: grepped bridge_server.py and
  // protocol.py, and listened to a live mock session -- neither publishes
  // vad_start_us/vad_end_us today. CONTRACTS.md §4.2 only documents
  // {type:"trial", state, label, idx, seed, next_label}, no VAD timestamps
  // on it or on any other event. quiz.js hit the exact same gap for this
  // same "trial" event ("confirmed empty by listening live") and shipped
  // the listener anyway so it activates automatically once a producer
  // exists -- same approach here: noteVadMark() below is wired to fire on
  // vad_start_us/vad_end_us appearing on ANY event (the eventual shape
  // isn't decided), but with today's system it's simply never called.
  // "起訖線正確對齊" is therefore unverifiable end-to-end right now -- see
  // completion report.
  //
  // Marks are drawn onto the canvas's current rightmost column at paint
  // time and then scroll left for free along with everything else via the
  // normal putImageData shift -- no separate overlay layer or per-frame
  // redraw needed. Simplification: a mark queued mid-way through a
  // multi-column catch-up batch (mode was hidden when it happened) lands
  // on the newest column of that batch rather than its exact historical
  // one -- accepted because there's no live data to make that distinction
  // observable, and it's a one-column error at most 60 times/sec.
  let pendingVadMarks = []; // "start" | "end", queued in noteVadMark, drawn in paint()

  function noteVadMark(kind) {
    pendingVadMarks.push(kind);
  }

  function drawPendingVadMarks() {
    if (!pendingVadMarks.length || !melCanvasReady) return;
    const x = MEL_HISTORY_COLUMNS - 1;
    for (const kind of pendingVadMarks) {
      melCtx.strokeStyle = kind === "start" ? "#5fbf7a" : "#e2574c"; // --good / --warn
      melCtx.lineWidth = 1;
      melCtx.beginPath();
      melCtx.moveTo(x + 0.5, 0);
      melCtx.lineTo(x + 0.5, MEL_BANDS);
      melCtx.stroke();
    }
    pendingVadMarks = [];
  }

  function ensureGrids(sensor, dim) {
    if (currentDim[sensor] === dim) return;
    const side = Math.round(Math.sqrt(dim));
    for (const ch of CHANNELS) {
      cells[`${sensor}-${ch}`] = buildGrid(gridEls[`${sensor}-${ch}`], side);
    }
    currentDim[sensor] = dim;
  }

  function applyViewMode() {
    gridsContainer.classList.remove("view-distance", "view-signal", "view-both");
    gridsContainer.classList.add(`view-${viewMode}`);
  }

  function cycleView() {
    const idx = VIEW_MODES.indexOf(viewMode);
    viewMode = VIEW_MODES[(idx + 1) % VIEW_MODES.length];
    applyViewMode();
  }

  function cycleDisplayMode() {
    const idx = DISPLAY_MODES.indexOf(displayMode);
    displayMode = DISPLAY_MODES[(idx + 1) % DISPLAY_MODES.length];
    updateDisplayModeTag();
    updateBaselineStatus(); // toggles the per-panel "尚無 baseline" note for the new mode
  }

  function isMonitorModeActive() {
    const section = document.getElementById("mode-monitor");
    return !!section && section.classList.contains("active");
  }

  function onKeydown(e) {
    if (isTypingTarget(e.target) || e.altKey || e.ctrlKey || e.metaKey) return;
    if (!isMonitorModeActive()) return;
    const key = e.key.toLowerCase();
    if (key === "s") {
      e.preventDefault();
      cycleView();
    } else if (key === "d") {
      e.preventDefault();
      cycleDisplayMode();
    } else if (key === "b") {
      e.preventDefault();
      captureBaseline();
    }
  }

  function paint() {
    // All four panels are kept current every frame regardless of which are
    // visible -- cheap (four small grids) and means switching views (or
    // side-by-side) never shows stale data, satisfying "四張圖同步更新"
    // trivially instead of tracking per-view dirty state.
    for (const sensor of SENSORS) {
      const frame = latestFrame[sensor];
      if (!frame) continue;
      ensureGrids(sensor, frame.dim);
      renderDistanceChannel(cells[`${sensor}-dist`], frame.dValues, frame.valid, sensor, displayMode, baseline);
      renderChannel(cells[`${sensor}-sig`], frame.sValues, frame.valid, signalColor);
      const hz = (rateCounters[sensor].length / 2).toFixed(1) + " Hz"; // 2s sliding window, unchanged from C05
      rateEls[sensor].forEach((el) => { el.textContent = hz; });
    }
    updateBaselineStatus(); // elapsed-time text needs to tick even without a new capture
    drawTrail();
    paintMelWaterfall();
    rafId = requestAnimationFrame(paint);
  }

  return {
    init(root) {
      root.innerHTML = `
        <div class="section-label">ToF depth / signal grids
          <span class="assumed-badge" title="zone 的實體排列方式（row-major）是未驗證的假設，見 D11 -- 距離與訊號兩種畫面皆適用">
            ⚠ zone 佈局 row-major — ASSUMED, unverified（距離／訊號皆適用）
          </span>
          <span class="view-hint mono">按 S：距離／訊號／並排　按 D：絕對／Δ／z-score　按 B：擷取基線</span>
        </div>
        <div class="baseline-status mono" data-baseline-status></div>
        <div class="tof-grids view-distance" data-grids>
          ${SENSORS.map((sensor) => CHANNELS.map((ch) => `
            <div class="sensor-panel" data-panel="${sensor}-${ch}">
              <div class="sensor-head">
                <span class="sensor-name">Sensor ${sensor} · ${PANEL_LABEL[ch]}${ch === "dist" ? ' <span class="display-mode-tag mono" data-display-mode-tag></span>' : ""}</span>
                <span class="sensor-hz mono" data-rate="${sensor}">--</span>
              </div>
              <div class="grid" data-grid="${sensor}-${ch}"></div>
              ${ch === "dist" ? `<div class="baseline-note" data-baseline-note="${sensor}">尚無 baseline —— 按 B 現場擷取 2 秒</div>` : ""}
            </div>
          `).join("")).join("")}
        </div>
        <div class="pca-panel">
          <div class="section-label">PCA 即時軌跡
            <span class="pca-model-badge mono" data-pca-badge></span>
            <span class="pca-variance mono" data-pca-variance></span>
          </div>
          <div class="pca-canvas-wrap">
            <canvas data-pca-canvas></canvas>
            <div class="pca-empty-note">尚無 enrollment 樣板可顯示信賴橢圓（等 D08）</div>
          </div>
        </div>
        <div class="mel-waterfall-panel">
          <div class="section-label">Mel 頻譜瀑布圖
            <span class="mel-waterfall-badge mono" data-mel-badge></span>
            <span class="mel-waterfall-hz mono" data-mel-hz></span>
          </div>
          <div class="mel-waterfall-canvas-wrap">
            <canvas data-mel-canvas></canvas>
          </div>
        </div>
        <div class="quality-panel">
          <div class="section-label">訊號品質
            <span class="quality-alarm-banner" data-alarm-banner style="display:none"></span>
          </div>
          <div class="reflash-note" data-reflash-note style="display:none">
            ⚠ 解析度切換中（重燒進行中）——以下指標可能暫時異常，這是預期行為
          </div>
          <div class="quality-grid">
            ${QUALITY_METRICS.map((m) => `
              <div class="quality-card">
                <div class="quality-card-head">
                  <span class="quality-label">${m.label}
                    <span class="prov-badge mono" title="門檻出處：config/quality_thresholds.json 的 _source（靜態快照）">[${m.provenance}]</span>
                  </span>
                  <span class="quality-level-dot level-unknown" data-quality-dot="${m.key}" title="unknown"></span>
                </div>
                <div class="quality-value mono" data-quality-value="${m.key}">--</div>
                <canvas class="quality-sparkline" data-quality-spark="${m.key}"></canvas>
                <div class="quality-hint" data-quality-hint="${m.key}" style="display:none"></div>
              </div>
            `).join("")}
          </div>
          <div class="quality-record-row">
            <select class="quality-rec-select" data-rec-seconds>
              <option value="3">3s</option>
              <option value="5" selected>5s</option>
              <option value="10">10s</option>
            </select>
            <button class="quality-rec-btn" data-rec-btn>開始錄製</button>
            <span class="quality-rec-status mono" data-rec-status></span>
          </div>
        </div>
      `;
      gridsContainer = root.querySelector("[data-grids]");
      rateEls.A = [];
      rateEls.B = [];
      for (const sensor of SENSORS) {
        for (const ch of CHANNELS) {
          const key = `${sensor}-${ch}`;
          gridEls[key] = root.querySelector(`[data-grid="${key}"]`);
          rateEls[sensor].push(root.querySelector(`[data-panel="${key}"] [data-rate="${sensor}"]`));
        }
      }
      applyViewMode();
      document.addEventListener("keydown", onKeydown);

      baselineStatusEl = root.querySelector("[data-baseline-status]");
      baselineNoteEls.A = root.querySelector('[data-baseline-note="A"]');
      baselineNoteEls.B = root.querySelector('[data-baseline-note="B"]');
      displayModeTagEls = Array.from(root.querySelectorAll("[data-display-mode-tag]"));
      updateDisplayModeTag();
      updateBaselineStatus();

      pcaCanvas = root.querySelector("[data-pca-canvas]");
      pcaCtx = pcaCanvas.getContext("2d");
      pcaBadgeEl = root.querySelector("[data-pca-badge]");
      pcaVarianceEl = root.querySelector("[data-pca-variance]");
      updatePcaBadge();
      resizePcaCanvas();
      window.addEventListener("resize", resizePcaCanvas);
      // Try once immediately at startup too, not just the periodic check --
      // no reason to wait 10s if the endpoint already happens to exist.
      tryFetchServerPcaModel("tof_only").then((model) => { if (model) setModel(model); });

      melCanvas = root.querySelector("[data-mel-canvas]");
      // willReadFrequently: every drawn column calls getImageData (the
      // putImageData-shift technique C08.md specifies), up to 62.5x/sec --
      // without this hint the browser optimizes for write-heavy canvases
      // and getImageData falls back to a slow path (confirmed live: Chrome
      // logs exactly this warning without the hint).
      melCtx = melCanvas.getContext("2d", { willReadFrequently: true });
      melBadgeEl = root.querySelector("[data-mel-badge]");
      melHzEl = root.querySelector("[data-mel-hz]");
      // Fixed logical pixel grid (MEL_HISTORY_COLUMNS x MEL_BANDS), stretched
      // by CSS -- unlike the PCA canvas, this doesn't depend on
      // devicePixelRatio or container size, so it's set up once here, not
      // re-run on window resize or onEnter.
      resizeMelCanvas();

      for (const m of QUALITY_METRICS) {
        qualityEls.value[m.key] = root.querySelector(`[data-quality-value="${m.key}"]`);
        qualityEls.dot[m.key] = root.querySelector(`[data-quality-dot="${m.key}"]`);
        qualityEls.hint[m.key] = root.querySelector(`[data-quality-hint="${m.key}"]`);
        const sparkCanvas = root.querySelector(`[data-quality-spark="${m.key}"]`);
        qualityEls.sparkCanvas[m.key] = sparkCanvas;
        qualityEls.sparkCtx[m.key] = sparkCanvas.getContext("2d");
      }
      alarmBannerEl = root.querySelector("[data-alarm-banner]");
      reflashNoteEl = root.querySelector("[data-reflash-note]");
      recBtn = root.querySelector("[data-rec-btn]");
      recSecondsEl = root.querySelector("[data-rec-seconds]");
      recStatusEl = root.querySelector("[data-rec-status]");
      recBtn.addEventListener("click", onRecordClick);
    },

    onEnter() {
      resizePcaCanvas(); // canvas may have had 0 size while this section was display:none
      if (rafId == null) rafId = requestAnimationFrame(paint);
    },

    onLeave() {
      if (rafId != null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
    },

    onData(evt) {
      // C08: VAD marks, checked on every event type (not tied to one
      // specific type -- the eventual producer/shape isn't decided, see
      // the noteVadMark() comment). Doesn't return; falls through to the
      // type-specific handling below same as any other event.
      if (typeof evt.vad_start_us === "number") noteVadMark("start");
      if (typeof evt.vad_end_us === "number") noteVadMark("end");

      if (evt.type === "quality") {
        // 1Hz, cheap DOM text/canvas updates -- runs directly here rather
        // than being deferred to the rAF paint loop (unlike the heatmap/
        // PCA canvases) because there's nothing to gain from tying a
        // once-a-second update to a 60fps loop, and it means the panel
        // stays current even while this mode is hidden.
        handleQualityEvent(evt);
        return;
      }
      if (evt.type === "mel") {
        // Queued here, drawn in paint() -- accumulates regardless of
        // visibility (C10's established pattern for this file), only the
        // actual canvas painting stops while the mode is hidden.
        const bands = evt.bands;
        if (Array.isArray(bands) && bands.length === MEL_BANDS) {
          lastMelEventAt = performance.now();
          pendingMelColumns.push(bands);
          if (pendingMelColumns.length > MEL_HISTORY_COLUMNS) pendingMelColumns.shift();
        }
        return;
      }
      if (evt.type === "mic") {
        if (typeof evt.rms === "number") {
          pendingRmsColumns.push(evt.rms);
          if (pendingRmsColumns.length > MEL_HISTORY_COLUMNS) pendingRmsColumns.shift();
        }
        return;
      }
      if (evt.type !== "tof") return;
      const sensor = evt.sensor === "B" ? "B" : "A";
      const { dim, dist, signal, valid } = evt;
      if (!Array.isArray(dist) || !Array.isArray(signal) || dist.length !== dim || signal.length !== dim) {
        if (!warnedBadLength) {
          console.warn(
            `[monitor] tof event shape mismatch: dim=${dim}, dist.length=${dist && dist.length}, signal.length=${signal && signal.length}`
          );
          warnedBadLength = true;
        }
        return;
      }
      latestFrame[sensor] = { dim, dValues: dist, sValues: signal, valid: Array.isArray(valid) ? valid : null };

      const now = performance.now();
      const arr = rateCounters[sensor];
      arr.push(now);
      while (arr.length && now - arr[0] > 2000) arr.shift();

      // Runs every onData call regardless of visibility -- per C05/C07's
      // established pattern and C10.md's explicit requirement, trajectory
      // history and the live-fit model both keep accumulating while
      // hidden; only drawTrail() (in the rAF loop) actually stops.
      pcaBookkeeping(now);
    },
  };
})());
