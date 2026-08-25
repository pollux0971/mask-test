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

let warnedBadLength = false;

const SENSORS = ["A", "B"];
const CHANNELS = ["dist", "sig"];
const VIEW_MODES = ["distance", "signal", "both"];
const PANEL_LABEL = { dist: "距離", sig: "訊號" };

function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
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

  function isMonitorModeActive() {
    const section = document.getElementById("mode-monitor");
    return !!section && section.classList.contains("active");
  }

  function onKeydown(e) {
    if (isTypingTarget(e.target) || e.altKey || e.ctrlKey || e.metaKey) return;
    if (!isMonitorModeActive()) return;
    if (e.key.toLowerCase() === "s") {
      e.preventDefault();
      cycleView();
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
      renderChannel(cells[`${sensor}-dist`], frame.dValues, frame.valid, distColor);
      renderChannel(cells[`${sensor}-sig`], frame.sValues, frame.valid, signalColor);
      const hz = (rateCounters[sensor].length / 2).toFixed(1) + " Hz"; // 2s sliding window, unchanged from C05
      rateEls[sensor].forEach((el) => { el.textContent = hz; });
    }
    rafId = requestAnimationFrame(paint);
  }

  return {
    init(root) {
      root.innerHTML = `
        <div class="section-label">ToF depth / signal grids
          <span class="assumed-badge" title="zone 的實體排列方式（row-major）是未驗證的假設，見 D11 -- 距離與訊號兩種畫面皆適用">
            ⚠ zone 佈局 row-major — ASSUMED, unverified（距離／訊號皆適用）
          </span>
          <span class="view-hint mono">按 S 切換：距離 / 訊號 / 並排</span>
        </div>
        <div class="tof-grids view-distance" data-grids>
          ${SENSORS.map((sensor) => CHANNELS.map((ch) => `
            <div class="sensor-panel" data-panel="${sensor}-${ch}">
              <div class="sensor-head">
                <span class="sensor-name">Sensor ${sensor} · ${PANEL_LABEL[ch]}</span>
                <span class="sensor-hz mono" data-rate="${sensor}">--</span>
              </div>
              <div class="grid" data-grid="${sensor}-${ch}"></div>
            </div>
          `).join("")).join("")}
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
    },
  };
})());
