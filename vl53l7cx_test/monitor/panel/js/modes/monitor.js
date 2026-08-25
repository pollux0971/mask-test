// Monitor mode (C05): dual ToF heatmap. Migration of the old single-file
// panel.html's buildGrid/renderTof/distColor -- behavior unchanged, only
// wired into the C03 mode lifecycle (registerMode/onEnter/onLeave/onData)
// instead of a page-global <script>.
//
// Two real adaptations beyond a straight copy-paste (per C05.md):
//
// 1. `$T`'s values are now [d0..d(dim-1), s0..s(dim-1)] -- 2*dim entries,
//    not dim entries. This mode only shows distance (signal rate display
//    is C07), so it slices off the first `dim` values and ignores the
//    rest. NOTE: C05.md's implementation note says "2 x dim^2", which
//    assumes `dim` means grid side. It doesn't: CONTRACTS.md 1.1 defines
//    the wire's `dim` field as *zone count* (16|64), and that's what's
//    empirically on the wire today -- captured live SSE during C01-C03
//    testing showed {"type":"tof",...,"dim":16,"values":[...32 entries]}.
//    So the real length is 2*dim, and grid side is round(sqrt(dim)). Also
//    worth knowing: the separate {"type":"status"} event's `dim` is grid
//    side (4|8, tied to bridge_server.py's /switch?res=4|8), NOT zone
//    count -- the same field name means two different things on two event
//    types from the *same*, unmodified bridge_server.py right now. This
//    mode never reads status's dim for grid sizing to avoid that trap; it
//    only ever trusts a tof event's own dim.
// 2. onLeave() cancels the rAF loop; onEnter() restarts it. onData() only
//    updates cheap local state (latest frame + rate-counter timestamps),
//    so a hidden monitor mode keeps its Hz readout and last-known frame
//    current without spending any paint time -- painting is what the rAF
//    loop does, and that's the part that actually stops.

import { registerMode } from "../shell.js";

const DIST_MIN = 0, DIST_MAX = 1200; // mm, clamps the color scale
const NEAR = [23, 73, 90];    // rgb, close object
const FAR = [223, 231, 226];  // rgb, far / no object nearby

function distColor(mm) {
  const t = Math.max(0, Math.min(1, mm / DIST_MAX));
  const r = Math.round(NEAR[0] + (FAR[0] - NEAR[0]) * t);
  const g = Math.round(NEAR[1] + (FAR[1] - NEAR[1]) * t);
  const b = Math.round(NEAR[2] + (FAR[2] - NEAR[2]) * t);
  return { rgb: `rgb(${r},${g},${b})`, luminance: (0.299 * r + 0.587 * g + 0.114 * b) / 255 };
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

function renderTof(cells, dValues) {
  if (!cells || cells.length !== dValues.length) return;
  for (let i = 0; i < dValues.length; i++) {
    const v = dValues[i];
    const c = cells[i];
    if (v == null || v < 0) {
      c.className = "cell invalid";
      c.textContent = "·";
      c.style.background = "var(--cell-invalid)";
      c.style.color = "";
    } else {
      const { rgb, luminance } = distColor(v);
      c.className = "cell";
      c.textContent = v;
      c.style.background = rgb;
      c.style.color = luminance > 0.55 ? "#10140f" : "#f3f6f2";
    }
  }
}

let warnedBadLength = false;

registerMode("monitor", (() => {
  let gridEls = { A: null, B: null };
  let rateEls = { A: null, B: null };
  let cells = { A: [], B: [] };
  let currentDim = { A: null, B: null }; // zone count (16|64), per-sensor
  let latestFrame = { A: null, B: null }; // { dim, dValues }
  let rateCounters = { A: [], B: [] }; // timestamps of recent frames, for a rough Hz readout -- unchanged from the original, per C05.md ("這個 story 先不動它")
  let rafId = null;

  function ensureGrid(sensor, dim) {
    if (currentDim[sensor] === dim) return;
    const side = Math.round(Math.sqrt(dim));
    cells[sensor] = buildGrid(gridEls[sensor], side);
    currentDim[sensor] = dim;
  }

  function paint() {
    for (const sensor of ["A", "B"]) {
      const frame = latestFrame[sensor];
      if (!frame) continue;
      ensureGrid(sensor, frame.dim);
      renderTof(cells[sensor], frame.dValues);
      const arr = rateCounters[sensor];
      const hz = arr.length / 2; // 2s sliding window, see onData
      rateEls[sensor].textContent = hz.toFixed(1) + " Hz";
    }
    rafId = requestAnimationFrame(paint);
  }

  return {
    init(root) {
      root.innerHTML = `
        <div class="section-label">ToF depth grids (mm)
          <span class="assumed-badge" title="zone 的實體排列方式（row-major）是未驗證的假設，見 D11">
            ⚠ zone 佈局 row-major — ASSUMED, unverified
          </span>
        </div>
        <div class="tof-grids">
          <div class="sensor-panel">
            <div class="sensor-head">
              <span class="sensor-name">Sensor A</span>
              <span class="sensor-hz mono" data-rate="A">--</span>
            </div>
            <div class="grid" data-grid="A"></div>
          </div>
          <div class="sensor-panel">
            <div class="sensor-head">
              <span class="sensor-name">Sensor B</span>
              <span class="sensor-hz mono" data-rate="B">--</span>
            </div>
            <div class="grid" data-grid="B"></div>
          </div>
        </div>
      `;
      gridEls = { A: root.querySelector('[data-grid="A"]'), B: root.querySelector('[data-grid="B"]') };
      rateEls = { A: root.querySelector('[data-rate="A"]'), B: root.querySelector('[data-rate="B"]') };
    },

    onEnter() {
      if (rafId == null) rafId = requestAnimationFrame(paint);
    },

    onLeave() {
      if (rafId != null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
    },

    onData(evt) {
      if (evt.type !== "tof") return;
      const sensor = evt.sensor === "B" ? "B" : "A";
      const dim = evt.dim;
      const values = evt.values;
      if (!Array.isArray(values) || values.length !== 2 * dim) {
        if (!warnedBadLength) {
          console.warn(
            `[monitor] tof event length mismatch: dim=${dim} expected ${2 * dim} values, got ${values && values.length}`
          );
          warnedBadLength = true;
        }
        return;
      }
      latestFrame[sensor] = { dim, dValues: values.slice(0, dim) };

      const now = performance.now();
      const arr = rateCounters[sensor];
      arr.push(now);
      while (arr.length && now - arr[0] > 2000) arr.shift();
    },
  };
})());
