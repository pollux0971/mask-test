// Shared, stateless drawing helpers for C19's per-trial thumbnails
// (quiz.js) and reused by monitor.js's live views where the underlying
// math is the same. Every export here is a pure function: no dataStore
// access, no module-level mutable state, nothing tied to a specific
// canvas's live scroll position. This is deliberate -- the *live*,
// incrementally-updated drawing functions (monitor.js's drawMelColumn,
// drawRmsColumn, renderChannel, renderDistanceChannel, drawTrail) stay in
// monitor.js because they're tightly coupled to their own mode's
// live-update state; forcing them into this shared module would complicate
// both call sites for no real gain. This module only holds the *static*
// (one-shot / batch, given a plain array of already-captured data)
// counterparts used to render a fixed snapshot after the fact.
//
// Any array/object passed in here must already be a capture-time COPY, not
// a live reference into dataStore's ring buffers -- dataStore keeps
// trimming/expiring old entries, so a live reference would silently go
// empty later (the C13 lesson: esp-mask-test-4f hit exactly this with
// thumbnails going blank over time). This module never reads dataStore
// itself, so that responsibility belongs entirely to the caller.

// --- Mel color ramp (moved from monitor.js's C08 work) --------------------
//
// -10/0 here must stay in sync with monitor.js's MEL_MIN/MEL_MAX -- both
// represent the same log_mel range (CONTRACTS.md §4.2's decoded float,
// -10 = reference_mel.py's LOG_FLOOR floor, 0 = a safe ceiling above the
// loudest values seen live, ~-1.2). Kept as separate constants (not a
// shared import) so this module stays dependency-free.
const MEL_MIN = -10, MEL_MAX = 0;

// Monotonic-luminance magma-ish ramp (C08.md: "viridis 或 magma").
const MEL_STOPS = [
  [10, 8, 20],     // near-black violet: silence / noise floor
  [82, 18, 92],
  [163, 33, 91],
  [230, 79, 58],
  [251, 159, 32],
  [252, 253, 191], // near-white yellow: loudest
];

export function melColorRgb(int16Value) {
  const t = Math.max(0, Math.min(1, (int16Value - MEL_MIN) / (MEL_MAX - MEL_MIN)));
  const segs = MEL_STOPS.length - 1;
  const scaled = t * segs;
  const i = Math.min(segs - 1, Math.floor(scaled));
  const localT = scaled - i;
  const a = MEL_STOPS[i], b = MEL_STOPS[i + 1];
  return [
    Math.round(a[0] + (b[0] - a[0]) * localT),
    Math.round(a[1] + (b[1] - a[1]) * localT),
    Math.round(a[2] + (b[2] - a[2]) * localT),
  ];
}

// One-shot batch draw of a whole historical set of Mel frames (not
// incremental scrolling like monitor.js's drawMelColumn/shiftMelCanvasLeft
// -- there's no "next column" here, the whole thumbnail is drawn at once
// from a fixed array). frames: array of band arrays, chronological order
// (oldest first), each the same length. Same low-band-at-bottom convention
// as the live waterfall so a viewer who has seen the monitor's live view
// reads this the same way.
export function drawMelWaterfallStatic(ctx, w, h, frames) {
  ctx.clearRect(0, 0, w, h);
  if (!frames || frames.length === 0) return;
  const cols = frames.length;
  const rows = frames[0].length;
  const colW = w / cols;
  const rowH = h / rows;
  for (let c = 0; c < cols; c++) {
    const bands = frames[c];
    for (let b = 0; b < rows; b++) {
      const rowFromTop = rows - 1 - b;
      const [r, g, bl] = melColorRgb(bands[b]);
      ctx.fillStyle = `rgb(${r},${g},${bl})`;
      // +1 covers the sub-pixel seam that shows up between adjacent cells
      // when w/cols or h/rows isn't a whole number.
      ctx.fillRect(c * colW, rowFromTop * rowH, colW + 1, rowH + 1);
    }
  }
}

// --- ToF keyframe heatmap ---------------------------------------------
//
// Color math extracted from monitor.js's C05/C06 live grid (distColor /
// zscoreColor / lerpColor). Duplicated here rather than imported so this
// module has zero dependency on the monitor mode -- quiz.js shouldn't have
// to pull in the whole live-grid mode just for color math. Same
// keep-in-sync tradeoff as the Mel constants above if the color scales
// ever change.
const DIST_MAX = 1200;
const DIST_NEAR = [23, 73, 90];
const DIST_FAR = [223, 231, 226];
const ZSCORE_CLAMP = 3;
const ZSCORE_DEADZONE = 0.5;
const Z_NEG = [64, 140, 226];
const Z_POS = [226, 87, 76];
const Z_NEUTRAL = [43, 50, 45];
const CELL_INVALID = "#3a3f3c"; // approximates --cell-invalid; canvas can't read CSS vars

function lerpRgb(a, b, t) {
  return [
    Math.round(a[0] + (b[0] - a[0]) * t),
    Math.round(a[1] + (b[1] - a[1]) * t),
    Math.round(a[2] + (b[2] - a[2]) * t),
  ];
}

function distColorRgb(mm) {
  const t = Math.max(0, Math.min(1, mm / DIST_MAX));
  return lerpRgb(DIST_NEAR, DIST_FAR, t);
}

function zscoreColorRgb(z) {
  const clamped = Math.max(-ZSCORE_CLAMP, Math.min(ZSCORE_CLAMP, z));
  const target = clamped < 0 ? Z_NEG : Z_POS;
  const abs = Math.abs(clamped);
  if (abs <= ZSCORE_DEADZONE) return lerpRgb(Z_NEUTRAL, target, (abs / ZSCORE_DEADZONE) * 0.3);
  const t = 0.3 + 0.7 * ((abs - ZSCORE_DEADZONE) / (ZSCORE_CLAMP - ZSCORE_DEADZONE));
  return lerpRgb(Z_NEUTRAL, target, t);
}

// Per-zone mean/stdev across a set of frames (moved from monitor.js's C06
// baseline capture). unstable/suspectZeroVariance/noSignal mirror B10's own
// flags (B10.md: sigma > 2mm = unstable, ~0 = suspect, no valid samples =
// no signal) so a zone flagged here and a zone flagged by a real B10
// baseline mean the same thing on screen. frames: array of { dist, valid }
// (same shape monitor.js's B10 baseline capture uses).
export function computeZoneStats(frames, dim) {
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

// dValues: flat array (dim = side*side) of distance mm for one keyframe.
// validArr: matching valid[] (optional -- falls back to the v<0 sentinel).
// baseline: optional { mu, sigma, flags:{noSignal,suspectZeroVariance,unstable} }
//   matching monitor.js's per-sensor baseline slice. When present, draws
//   z-score colors with the same priority as monitor.js's
//   renderDistanceChannel (no_signal > suspect_zero_variance > plain z).
//   When absent, draws plain absolute-distance color -- same "no baseline
//   yet, don't fake one" honesty as the live grid.
export function drawTofKeyframeGrid(ctx, w, h, dValues, validArr, baseline) {
  ctx.clearRect(0, 0, w, h);
  const dim = dValues.length;
  const side = Math.round(Math.sqrt(dim));
  const cw = w / side, ch = h / side;
  for (let i = 0; i < dim; i++) {
    const row = Math.floor(i / side), col = i % side;
    const x = col * cw, y = row * ch;
    const v = dValues[i];
    const invalid = v == null || v < 0 || (validArr && validArr[i] === false);
    const noSignal = !!(baseline && baseline.flags && baseline.flags.noSignal &&
      baseline.flags.noSignal.includes(i));
    if (invalid || noSignal) {
      ctx.fillStyle = CELL_INVALID;
      ctx.fillRect(x, y, cw + 1, ch + 1);
      continue;
    }
    let rgb;
    if (baseline && baseline.mu && baseline.sigma) {
      const delta = v - baseline.mu[i];
      const isSuspectZero = !!(baseline.flags && baseline.flags.suspectZeroVariance &&
        baseline.flags.suspectZeroVariance.includes(i));
      const z = isSuspectZero ? (delta === 0 ? 0 : Math.sign(delta) * ZSCORE_CLAMP) : delta / baseline.sigma[i];
      rgb = zscoreColorRgb(z);
    } else {
      rgb = distColorRgb(v);
    }
    ctx.fillStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
    ctx.fillRect(x, y, cw + 1, ch + 1);
  }
}

// --- PCA trajectory thumbnail ------------------------------------------
//
// Static analogue of monitor.js's live drawTrail(): same autoscale-to-spread
// + fading-dot-trail + bright-current-point look, but a static snapshot has
// no "now" to fade against elapsed real time, so age is expressed by
// position in the array instead (oldest = dimmest, newest = brightest).
//
// trail: array of {x, y} already-projected PCA points, oldest first --
// must be a capture-time copy (see module comment above).
// ellipses: optional array of {cx, cy, rx, ry, rotation, label, color} in
// the same PCA (x,y) space, one per enrolled class. Pass null/omit when no
// enrollment data exists yet (C10's pre-existing gap) -- this function
// draws nothing extra in that case rather than fabricating a placeholder.
// --- PCA fitting/projection math (moved from monitor.js's C10 work) ------
//
// Pure numeric functions, no DOM/canvas/dataStore coupling -- monitor.js's
// live view uses these to periodically refit a stub model from a sliding
// window of recent frames (see monitor.js's pcaBookkeeping/FIT_WINDOW_MS);
// quiz.js's per-trial thumbnail (C19) does a single one-shot fit from
// whatever ToF frames were captured for that trial instead of a periodic
// refit, but needs the exact same fitting/projection math to be a genuine
// "復用 C10" rather than a second, possibly-diverging implementation.
export function vecSub(vec, mean) {
  const out = new Array(vec.length);
  for (let i = 0; i < vec.length; i++) out[i] = vec[i] - mean[i];
  return out;
}

export function dot(a, b) {
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
export function fitPCA2Stub(samples) {
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

export function projectPCA(model, vec) {
  const c = vecSub(vec, model.mean);
  return [dot(c, model.components[0]), dot(c, model.components[1])];
}

// source: "tof_only" | "enrollment". Returns null (not throw) both when the
// endpoint genuinely doesn't exist yet (confirmed live, 404/204-no-body as
// of C19) and on any network hiccup -- callers already have a stub-fit
// fallback for "no real model yet", so this stays a plain null rather than
// forcing every caller to add its own try/catch around a throwing fetch.
export async function tryFetchServerPcaModel(source) {
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
    return null;
  }
}

export function drawPcaTrailStatic(ctx, w, h, trail, ellipses) {
  ctx.clearRect(0, 0, w, h);
  if (!trail || trail.length < 2) return;

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  const consider = (x, y) => {
    if (x < minX) minX = x; if (x > maxX) maxX = x;
    if (y < minY) minY = y; if (y > maxY) maxY = y;
  };
  for (const p of trail) consider(p.x, p.y);
  if (ellipses) {
    for (const e of ellipses) {
      consider(e.cx - e.rx, e.cy - e.ry);
      consider(e.cx + e.rx, e.cy + e.ry);
    }
  }
  const spanX = Math.max(maxX - minX, 1e-6), spanY = Math.max(maxY - minY, 1e-6);
  const scale = Math.min((w * 0.7) / spanX, (h * 0.7) / spanY);
  const midX = (minX + maxX) / 2, midY = (minY + maxY) / 2;
  const cx = w / 2, cy = h / 2;
  const toScreen = (x, y) => [cx + (x - midX) * scale, cy - (y - midY) * scale];

  if (ellipses) {
    for (const e of ellipses) {
      const [sx, sy] = toScreen(e.cx, e.cy);
      ctx.save();
      ctx.translate(sx, sy);
      ctx.rotate(e.rotation || 0);
      ctx.beginPath();
      ctx.ellipse(0, 0, Math.max(e.rx * scale, 0.01), Math.max(e.ry * scale, 0.01), 0, 0, Math.PI * 2);
      ctx.strokeStyle = e.color || "rgba(232,163,61,0.6)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.restore();
    }
  }

  for (let i = 0; i < trail.length; i++) {
    const p = trail[i];
    const alpha = 0.25 + 0.75 * (i / (trail.length - 1));
    const [sx, sy] = toScreen(p.x, p.y);
    ctx.beginPath();
    ctx.arc(sx, sy, 3 + 2 * alpha, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(232, 163, 61, ${alpha.toFixed(3)})`;
    ctx.fill();
  }
  const last = trail[trail.length - 1];
  const [lx, ly] = toScreen(last.x, last.y);
  ctx.beginPath();
  ctx.arc(lx, ly, 6, 0, Math.PI * 2);
  ctx.strokeStyle = "#f3f6f2";
  ctx.lineWidth = 1.5;
  ctx.stroke();
}
