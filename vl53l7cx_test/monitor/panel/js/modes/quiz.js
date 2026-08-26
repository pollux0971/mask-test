// Quiz mode (C15): 8-option closed-set layout.
//
// Data source note: words come from panel/data/vocab.json, a manually
// synced COPY of the canonical config/vocab.json -- bridge_server.py has
// no route serving config/*.json to the browser (same class of gap as
// C09's quality_thresholds.json / C10's /pca, flagged in the completion
// report; not fixable from here since bridge_server.py isn't in scope
// this round). Editing panel/data/vocab.json changes the displayed
// options with zero JS changes, which is what C15.md's "改 JSON 即改變
// 選項" and E08's "4 選項備援只需要改一個 JSON" actually need -- keeping
// it in sync with the real config/vocab.json is a manual step for now.
//
// Auto-VAD readiness: {type:"trial",...} (CONTRACTS.md 4.2) is the wire
// signal for real PROMPT/COUNTDOWN/CAPTURE/SAVE/REST state, but nothing
// publishes it yet (B09/B12/B13 aren't wired into bridge_server.py's SSE
// -- confirmed empty by listening live). This shows a static "ready"
// breathing indicator as the assumed steady state and listens for real
// trial events so it upgrades automatically once a producer exists,
// same auto-upgrade pattern as C10's PCA model check.
//
// Speaking mode (normal/whisper/silent) is UI-only local state here --
// wiring an actual trigger to B11/B12/B13 is out of scope ("不包含: 評分
// 顯示/結果卡", and this story is the layout, not the trigger machinery).
// B13.md: constructing an audio-trigger VAD with speaking_mode=silent is
// an error, so silent mode swaps the readiness indicator to "unavailable"
// and surfaces a tof/either trigger-source choice instead of pretending
// Auto-VAD still applies.

// C16 additions: three-track (ToF-only / Mel-only / Fused) score bars.
//
// TriResult (CONTRACTS.md 4.3) comes from POST /recognize (D09) -- which
// doesn't exist yet (confirmed live: 404), and neither does the trial/VAD
// trigger pipeline that would normally call it. There's a manual "觸發
// 辨識" button so the chart is exercisable now and auto-works once D09
// lands, same auto-upgrade pattern as C10's /pca check.
//
// D07.md gives the exact formula, and it's the SAME formula for all three
// columns at different w: fuse(w) = softmax(-(w*d_tof + (1-w)*d_mel)/tau).
// w=1 -> pure ToF, w=0 -> pure Mel. So "ToF only" and "Mel only" aren't
// separate formulas, they're fuse(1) and fuse(0) -- and per D07's own
// acceptance criteria, d_tof/d_mel from ONE fetched TriResult must never
// be re-fetched just because w changes; this file only recomputes softmax
// locally. C17's slider is expected to reuse fuseScores() at whatever w
// it picks for the "Fused" column, not invent a second formula.
//
// distance -> score conversion is this softmax; CONTRACTS/D07.md don't
// specify a display formula beyond "the frontend computes fuse(w)" itself,
// so this is D07.md's own fuse() applied verbatim, not a guess.

import { registerMode } from "../shell.js";

const VOCAB_URL = "data/vocab.json";
const FALLBACK_VOCAB = { words: [], reject: { id: "_reject", text: "靜止／其他" } };
const DEFAULT_FUSED_W = 0.5; // Demo script step 1; C17 makes this a live slider

function softmax(values) {
  const max = Math.max(...values);
  const exps = values.map((v) => Math.exp(v - max));
  const sum = exps.reduce((a, b) => a + b, 0);
  return exps.map((v) => v / sum);
}

function fuseScores(triResult, w) {
  const { d_tof, d_mel, tau } = triResult;
  const combined = d_tof.map((d, i) => w * d + (1 - w) * d_mel[i]);
  return softmax(combined.map((d) => -d / tau));
}

function sortedEntries(classes, scores) {
  return classes
    .map((cls, i) => ({ cls, score: scores[i] }))
    .sort((a, b) => b.score - a.score);
}

// FLIP: capture each bar's position (keyed by class, since sort order
// changes), let updateFn mutate the DOM, then animate from the old
// position to the new one instead of snapping -- C16.md's explicit
// "分數變動時動畫重排（0.3s ease-out）" requirement.
function flipAnimate(container, updateFn) {
  const before = new Map(
    Array.from(container.querySelectorAll(".quiz-bar")).map((el) => [el.dataset.classId, el.getBoundingClientRect()])
  );
  updateFn();
  Array.from(container.querySelectorAll(".quiz-bar")).forEach((el) => {
    const first = before.get(el.dataset.classId);
    if (!first) return;
    const last = el.getBoundingClientRect();
    const dy = first.top - last.top;
    if (!dy) return;
    el.style.transition = "none";
    el.style.transform = `translateY(${dy}px)`;
    requestAnimationFrame(() => {
      el.style.transition = "transform 0.3s ease-out";
      el.style.transform = "";
    });
  });
}

// --- C17: fusion weight slider ---
//
// esp-mask-test-ad's ruling on reject_fused (CONTRACTS.md 4.3, corrected
// after C16 -- esp-mask-test-59 caught the first version being silently
// wrong): theta_reject_fused(w) = w*theta_reject_tof + (1-w)*theta_reject_mel,
// reject_fused(w) = (min fused distance > theta_reject_fused(w)) -- computed
// here live, never stored in TriResult, since it depends on w which only
// exists client-side.
//
// IMPORTANT, and easy to "helpfully" break: the reject check MUST use
// d_tof_raw/d_mel_raw (unnormalized), not d_tof/d_mel. normalize_distances()
// subtracts the min, so d_tof.min() is always exactly 0 and a raw-vs-
// normalized mixup makes reject_fused permanently False -- no error, it
// just silently never rejects. fuseScores() above is correct to use the
// normalized d_tof/d_mel (softmax needs comparable scale); the reject
// threshold needs the absolute (raw) scale instead. Two different distance
// arrays for two different purposes, on purpose -- not an inconsistency to
// "clean up".
//
// C17.md's own keyboard shortcuts are T/M/F, not the 0/1-endpoint keys
// esp-mask-test-ad's message mentioned as an example -- and T/M/F happen
// to dodge the collision problem entirely (shell.js's global shortcuts are
// 1-5 for modes and \ for collapse; T/M/F share none of those).
const SNAP_POINTS = [0, 0.5, 1];
const SNAP_THRESHOLD = 0.03;

function snapW(raw) {
  for (const p of SNAP_POINTS) {
    if (Math.abs(raw - p) <= SNAP_THRESHOLD) return p;
  }
  return raw;
}

function computeFusedReject(triResult, w) {
  const { d_tof_raw, d_mel_raw, theta_reject_tof, theta_reject_mel } = triResult;
  const combined = d_tof_raw.map((d, i) => w * d + (1 - w) * d_mel_raw[i]);
  const minDist = Math.min(...combined);
  const thetaFused = w * theta_reject_tof + (1 - w) * theta_reject_mel;
  return minDist > thetaFused;
}

// --- C18: result card + confidence ring ---
//
// C18.md: confidence is top1-vs-top2 MARGIN, not top1's raw score. A raw
// softmax score is dominated by tau (D06: tau=0.05 -> top1 > 0.95 almost
// regardless of how separable the classes actually are; tau=5.0 -> top1-top4
// spread < 0.3 even when one class is a clean winner), so the same "85%"
// means totally different things at different tau. Margin against the
// runner-up is tau-invariant in a way the raw score isn't -- it's asking
// "how much does the winner actually lead by", not "how peaked is softmax".
function computeConfidence(classes, scores) {
  const entries = sortedEntries(classes, scores);
  const sTop1 = entries[0].score;
  const sTop2 = entries[1] ? entries[1].score : 0;
  const confidence = sTop1 > 0 ? (sTop1 - sTop2) / sTop1 : 0;
  return { top1: entries[0], confidence };
}

const RING_R = 52;
const RING_CIRC = 2 * Math.PI * RING_R;

const SPEAKING_MODES = [
  { key: "normal", label: "正常" },
  { key: "whisper", label: "氣音" },
  { key: "silent", label: "無聲" },
];
const TRIGGER_SOURCES = [
  { key: "tof", label: "ToF" },
  { key: "either", label: "任一（ToF 或音訊）" },
];
const EXPECT_LABEL = { "ToF": "ToF", "音訊": "音訊", "雙模態": "雙模態" };

// --- C20: real-time confusion matrix ---
//
// C20.md's own three marking-mode options ("建議兩種都做，用一個切換"):
// - "posthoc" (Demo-friendly): a result renders, "✓ 正確" assumes the fused
//   prediction shown right now is what was actually said; the "其實是"
//   dropdown corrects it when the system got it wrong. One click either way.
// - "assigned" (research-grade, == record mode's own flow): the system
//   names the target BEFORE recognition, so ground truth is known in
//   advance instead of trusted after the fact. Includes the _reject label
//   as a possible assigned target ("保持安靜"), matching E03/E05's
//   baseline-silence trials -- reject needs its own row/column in the
//   matrix, not just its own card in the quiz above.
//
// Each entry stores the trueLabel + the RAW TriResult, not a fixed
// predicted label: esp-mask-test-ad's call is that dragging the fusion
// slider should re-answer "what would the system have said" for the WHOLE
// accumulated history, the same thing C17 already does for the score bars.
// So renderMatrix() recomputes every cell from matrixEntries + currentW
// from scratch on every call -- it hooks into renderFusedColumn(), the
// same recompute chain the bars/result card use, not a second one.
const MARKING_MODES = [
  { key: "posthoc", label: "事後標記" },
  { key: "assigned", label: "系統指定" },
];
const MATRIX_CELL_SIZE = 40;
const MATRIX_LABEL_W_RATIO = 1.8; // room for two-character labels like 不要/靜止／其他
const MATRIX_LABEL_H_RATIO = 1.3;
const EXPORT_DPI = 300;
const CSS_DPI = 96; // browsers' baseline px-per-inch assumption; scale = EXPORT_DPI/CSS_DPI

function hexToRgb(hex) {
  const h = hex.trim().replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = parseInt(full, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// Blends from the surface colour up to the full hue as `t` (0..1) grows,
// floored at 15% so a single occurrence is still visible against an empty
// (zero-count) cell rather than reading as "no data".
function intensityColor(hex, surfaceHex, t) {
  const a = hexToRgb(hex);
  const b = hexToRgb(surfaceHex);
  const k = Math.max(0.15, Math.min(1, t));
  const mixed = a.map((v, i) => Math.round(b[i] + (v - b[i]) * k));
  return `rgb(${mixed[0]}, ${mixed[1]}, ${mixed[2]})`;
}

function matrixTheme() {
  const style = getComputedStyle(document.documentElement);
  const get = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
  return {
    diag: get("--good", "#5fbf7a"),
    offdiag: get("--warn", "#e2574c"),
    surface: get("--surface", "#141915"),
    empty: get("--surface-2", "#1b211c"),
    border: get("--border", "#262c27"),
    text: get("--text", "#e7ece8"),
    onFill: get("--bg", "#0b0e0c"),
  };
}

// A focused range slider isn't "typing" -- unlike C02/C06/C07/C09's own
// copies of this check, a range <input> must NOT block T/M/F, or dragging
// the slider then immediately reaching for a shortcut key would silently
// do nothing until the user clicks elsewhere first.
function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  if (tag === "INPUT") return el.type !== "range";
  return tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

function isQuizModeActive() {
  const section = document.getElementById("mode-quiz");
  return !!section && section.classList.contains("active");
}

registerMode("quiz", (() => {
  let cardsEl = null;
  let baselineEl = null;
  let vadStatusEl = null;
  let speakingModeEls = [];
  let triggerRowEl = null;
  let triggerEls = [];
  let speakingMode = "normal";
  let triggerSource = "tof";
  let vocab = FALLBACK_VOCAB;

  // --- C16/C17: three-track result bars + fusion weight slider ---
  let recognizeBtn = null, resultStatusEl = null, resultsAreaEl = null, disagreeBannerEl = null;
  const barsEl = { tof: null, mel: null, fused: null };
  const rejectBadgeEl = { tof: null, mel: null, fused: null };
  let lastTriResult = null;
  let tofTopCache = null, melTopCache = null;
  let currentW = DEFAULT_FUSED_W;
  let wSliderEl = null, wValueEl = null;

  // --- C18: result card ---
  let resultCardEl = null, resultWordEl = null, resultSubEl = null, resultPctEl = null, ringFillEl = null, retryBtn = null;

  // --- C20: confusion matrix ---
  let markingModeEls = [], markingMode = "posthoc";
  let assignedPromptEl = null, assignedWordEl = null;
  let posthocControlsEl = null, posthocCorrectBtn = null, posthocSelectEl = null;
  let matrixCanvasEl = null, matrixCountEl = null, matrixExportBtn = null, matrixClearBtn = null;
  let assignedTarget = null; // { id, text } | null
  let matrixEntries = []; // { trueLabel, triResult }[]
  let currentEntryMarked = true; // true = nothing pending, so buttons/dropdown are inert until a fresh result arrives

  function renderColumn(el, classes, scores, rejected) {
    const entries = sortedEntries(classes, scores);
    el.innerHTML = entries.map((e, i) => `
      <div class="quiz-bar${i === 0 && !rejected ? " top1" : ""}" data-class-id="${e.cls}">
        <span class="quiz-bar-label">${e.cls}</span>
        <div class="quiz-bar-track"><div class="quiz-bar-fill" style="width:${(e.score * 100).toFixed(1)}%"></div></div>
        <span class="quiz-bar-pct mono">${(e.score * 100).toFixed(0)}%</span>
      </div>
    `).join("");
    return entries[0];
  }

  // Disagreement compares only the tracks that actually have an opinion --
  // a rejected track saying "nothing" isn't disagreement, it's just
  // silence (esp-mask-test-ad, written into CONTRACTS.md after C16: "拒識
  // 不是分歧，是沉默"). Otherwise C15's Demo step 4 (ToF alone, correctly
  // rejecting) would falsely light up the disagreement banner too.
  function updateDisagreement(fusedTop, rejectFused) {
    const tops = [];
    if (!lastTriResult.reject_tof) tops.push(tofTopCache.cls);
    if (!lastTriResult.reject_mel) tops.push(melTopCache.cls);
    if (!rejectFused) tops.push(fusedTop.cls);
    const disagree = new Set(tops).size > 1;
    resultsAreaEl.classList.toggle("results-disagree", disagree);
    disagreeBannerEl.style.display = disagree ? "block" : "none";
  }

  function setRingProgress(fraction) {
    const clamped = Math.max(0, Math.min(1, fraction));
    ringFillEl.style.strokeDashoffset = String(RING_CIRC * (1 - clamped));
  }

  // C18.md: "未偵測到" 要跟正常結果同樣大方 -- same card, same font sizes,
  // just a different (dashed/rejected) visual state, never a blank card or
  // all-bars-dim with no headline. And per esp-mask-test-ad's ruling on C18
  // (disagreement is real): the card always shows the FUSED track's answer
  // (the system's actual output), never a silently-picked "looks more
  // right" track -- disagreement is surfaced by C16's existing red banner,
  // not by swapping which track the card quotes.
  function updateResultCard(triResult, w, fusedScores, rejectFused) {
    resultCardEl.style.display = "flex";
    resultCardEl.classList.toggle("rejected", rejectFused);
    if (rejectFused) {
      resultWordEl.textContent = "未偵測到";
      resultSubEl.textContent = "系統判定：不認得";
      resultPctEl.textContent = "--";
      setRingProgress(0);
      return;
    }
    const { top1, confidence } = computeConfidence(triResult.classes, fusedScores);
    resultWordEl.textContent = top1.cls;
    // tau is shown alongside confidence, not just the bare percentage --
    // esp-mask-test-ad flagged that the same confidence number means a
    // different thing at different tau (D06), so the number without its
    // tau is misleading on its own.
    resultSubEl.textContent = `信心度 ${(confidence * 100).toFixed(0)}%　τ=${triResult.tau}　w=${w.toFixed(2)}`;
    resultPctEl.textContent = `${(confidence * 100).toFixed(0)}%`;
    setRingProgress(confidence);
  }

  // --- C20: confusion matrix ---

  function matrixLabels() {
    const words = vocab.words || [];
    const reject = vocab.reject || FALLBACK_VOCAB.reject;
    return [...words.map((w) => ({ id: w.id, text: w.text })), { id: reject.id, text: reject.text }];
  }

  // The one place "what did the fused track decide" is computed from a
  // TriResult + w -- reused for every historical entry every time the
  // matrix recomputes, so a w change re-answers this for the whole history
  // exactly like it re-answers it for the live score bars above.
  function predictedLabelFor(triResult, w) {
    const reject = vocab.reject || FALLBACK_VOCAB.reject;
    if (computeFusedReject(triResult, w)) return reject.id;
    const scores = fuseScores(triResult, w);
    return sortedEntries(triResult.classes, scores)[0].cls;
  }

  function pickRandomTarget() {
    const labels = matrixLabels();
    return labels[Math.floor(Math.random() * labels.length)];
  }

  function showNextAssignedTarget() {
    assignedTarget = pickRandomTarget();
    const reject = vocab.reject || FALLBACK_VOCAB.reject;
    assignedWordEl.textContent =
      assignedTarget.id === reject.id ? "保持安靜（不要念任何詞）" : assignedTarget.text;
  }

  function populateMatrixSelect() {
    const labels = matrixLabels();
    posthocSelectEl.innerHTML =
      `<option value="">其實是…</option>` +
      labels.map((l) => `<option value="${l.id}">${l.text}</option>`).join("");
  }

  // One recorded observation = one matrix entry. `currentEntryMarked` stops
  // a second click (posthoc: "✓ 正確" then the dropdown, or the dropdown
  // twice) from double-counting the same recognition.
  function recordMatrixEntry(trueLabel) {
    if (!lastTriResult || currentEntryMarked || !trueLabel) return;
    matrixEntries.push({ trueLabel, triResult: lastTriResult });
    currentEntryMarked = true;
    matrixCountEl.textContent = `已記錄 ${matrixEntries.length} 筆`;
    posthocControlsEl.style.display = "none";
    renderMatrix();
  }

  // Shared by the on-screen canvas and the PNG export -- same drawing code
  // at two different cellSize values (see EXPORT_DPI below), so the export
  // is pixel-for-pixel the same layout, just rendered at print resolution
  // instead of screen resolution.
  // Pure size math, no canvas/context involved -- both renderMatrix() and
  // exportMatrixPNG() need the pixel dimensions *before* they can size the
  // canvas (setting .width/.height clears it), and neither the label
  // column width nor the number of rows/columns depends on anything a
  // context could tell us (label width is a fixed ratio of cellSize, not
  // measured text width).
  function matrixLayoutSize(cellSize) {
    const n = matrixLabels().length;
    const labelW = cellSize * MATRIX_LABEL_W_RATIO;
    const labelH = cellSize * MATRIX_LABEL_H_RATIO;
    return { width: labelW + n * cellSize, height: labelH + n * cellSize, labelW, labelH, n };
  }

  // Draws into an ALREADY-sized canvas context. Called at two different
  // cellSize values (on-screen vs. export, see EXPORT_DPI below) with
  // otherwise identical code, so the export is pixel-for-pixel the same
  // layout, just at print resolution instead of screen resolution.
  function drawMatrix(ctx, cellSize) {
    const theme = matrixTheme();
    const labels = matrixLabels();
    const { width, height, labelW, labelH, n } = matrixLayoutSize(cellSize);

    const counts = Array.from({ length: n }, () => new Array(n).fill(0));
    matrixEntries.forEach((entry) => {
      const trueIdx = labels.findIndex((l) => l.id === entry.trueLabel);
      const predIdx = labels.findIndex((l) => l.id === predictedLabelFor(entry.triResult, currentW));
      if (trueIdx >= 0 && predIdx >= 0) counts[trueIdx][predIdx] += 1;
    });
    const maxCount = Math.max(1, ...counts.map((row) => Math.max(...row)));

    ctx.fillStyle = theme.surface;
    ctx.fillRect(0, 0, width, height);
    ctx.font = `${Math.round(cellSize * 0.24)}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    // Column headers (predicted axis) and row headers (true axis).
    ctx.fillStyle = theme.text;
    labels.forEach((l, j) => ctx.fillText(l.text, labelW + j * cellSize + cellSize / 2, labelH / 2));
    labels.forEach((l, i) => ctx.fillText(l.text, labelW / 2, labelH + i * cellSize + cellSize / 2));

    // Cells: diagonal and off-diagonal get different hues (not just
    // different intensities of the same colour) so "the matrix is mostly
    // one colour" reads as "mostly correct" at a glance, per C20.md's
    // "對角線與非對角線視覺區隔明確".
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        const count = counts[i][j];
        const x = labelW + j * cellSize;
        const y = labelH + i * cellSize;
        const base = i === j ? theme.diag : theme.offdiag;
        ctx.fillStyle = count === 0 ? theme.empty : intensityColor(base, theme.surface, count / maxCount);
        ctx.fillRect(x, y, cellSize, cellSize);
        ctx.strokeStyle = theme.border;
        ctx.strokeRect(x + 0.5, y + 0.5, cellSize - 1, cellSize - 1);
        if (count > 0) {
          ctx.fillStyle = count / maxCount > 0.55 ? theme.onFill : theme.text;
          ctx.fillText(String(count), x + cellSize / 2, y + cellSize / 2);
        }
      }
    }
  }

  function renderMatrix() {
    if (!matrixCanvasEl) return;
    const { width, height } = matrixLayoutSize(MATRIX_CELL_SIZE);
    matrixCanvasEl.width = width;
    matrixCanvasEl.height = height;
    drawMatrix(matrixCanvasEl.getContext("2d"), MATRIX_CELL_SIZE);
  }

  // 300 dpi (C20.md's explicit acceptance criterion): browsers have no
  // native notion of canvas DPI, so this renders the identical layout at
  // EXPORT_DPI/CSS_DPI times the on-screen cell size onto an offscreen
  // canvas instead -- a PNG's only DPI metadata is its pixel dimensions
  // relative to an assumed physical size, and this is the standard way to
  // get a print-resolution export from an on-screen-resolution canvas API.
  function exportMatrixPNG() {
    const scale = EXPORT_DPI / CSS_DPI;
    const exportCellSize = MATRIX_CELL_SIZE * scale;
    const { width, height } = matrixLayoutSize(exportCellSize);
    const exportCanvas = document.createElement("canvas");
    exportCanvas.width = width;
    exportCanvas.height = height;
    drawMatrix(exportCanvas.getContext("2d"), exportCellSize);

    const a = document.createElement("a");
    a.href = exportCanvas.toDataURL("image/png");
    a.download = `confusion-matrix-${new Date().toISOString().replace(/[:.]/g, "-")}.png`;
    a.click();
  }

  function setMarkingMode(mode) {
    markingMode = mode;
    markingModeEls.forEach((el) => el.classList.toggle("active", el.dataset.markingMode === mode));
    assignedPromptEl.style.display = mode === "assigned" ? "flex" : "none";
    posthocControlsEl.style.display = mode === "posthoc" && !currentEntryMarked ? "flex" : "none";
    if (mode === "assigned" && !assignedTarget) showNextAssignedTarget();
  }

  // Recomputes and repaints ONLY the Fused column -- ToF-only/Mel-only
  // never change with w (they're fuse(1)/fuse(0) always), and this never
  // touches d_tof/d_mel or re-fetches: C17.md's "不需重念" and D07/D09's
  // pinned "11 種 w 全程距離沒被動過" property. Also always updates the
  // w-value readout, even before any result exists, so moving the slider
  // ahead of the first "觸發辨識" still shows a sane number.
  function renderFusedColumn() {
    wValueEl.textContent = `w = ${currentW.toFixed(2)}`;
    if (!lastTriResult) return;

    const t0 = performance.now();
    const fusedScores = fuseScores(lastTriResult, currentW);
    const rejectFused = computeFusedReject(lastTriResult, currentW);

    // Sanity check, not just a one-off manual test: theta_reject_fused(1)
    // = theta_reject_tof and combined(w=1) = d_tof exactly (0*d_mel term
    // vanishes), so reject_fused(1) must equal reject_tof bit-for-bit. A
    // mismatch here means this formula and whatever D09 actually does for
    // reject_tof have drifted apart -- worth knowing loudly, not silently.
    if (currentW === 1 && rejectFused !== lastTriResult.reject_tof) {
      console.error("[quiz] reject_fused(w=1) != reject_tof -- formula/backend mismatch", {
        rejectFused, reject_tof: lastTriResult.reject_tof,
      });
    }
    if (currentW === 0 && rejectFused !== lastTriResult.reject_mel) {
      console.error("[quiz] reject_fused(w=0) != reject_mel -- formula/backend mismatch", {
        rejectFused, reject_mel: lastTriResult.reject_mel,
      });
    }

    let fusedTop;
    flipAnimate(barsEl.fused, () => {
      fusedTop = renderColumn(barsEl.fused, lastTriResult.classes, fusedScores, rejectFused);
    });
    rejectBadgeEl.fused.style.display = rejectFused ? "inline-block" : "none";
    updateDisagreement(fusedTop, rejectFused);
    updateResultCard(lastTriResult, currentW, fusedScores, rejectFused);
    renderMatrix(); // every entry's predicted label depends on w -- recompute the whole matrix, not just repaint

    const elapsed = performance.now() - t0;
    if (elapsed > 50) {
      console.warn(`[quiz] fused recompute took ${elapsed.toFixed(1)}ms, over the 50ms budget`);
    }
  }

  function setW(w) {
    currentW = w;
    wSliderEl.value = String(w);
    renderFusedColumn();
  }

  function onSliderInput() {
    currentW = snapW(parseFloat(wSliderEl.value));
    wSliderEl.value = String(currentW); // reflect the snap visually, not just internally
    renderFusedColumn();
  }

  // T = 純 ToF (w=1), M = 純音訊 (w=0), F = 平衡 (w=0.5) -- C17.md's exact
  // three shortcuts. Enter = 重試 (C18.md) -- same handler as the "重試"
  // button and the original "觸發辨識" button, since a retry IS just
  // triggering recognition again. Global (document-level, like C05/C07's
  // S/D/B), so a presenter doesn't need any particular element focused.
  function onKeydown(e) {
    if (isTypingTarget(e.target) || e.altKey || e.ctrlKey || e.metaKey) return;
    if (!isQuizModeActive()) return;
    const key = e.key.toLowerCase();
    if (key === "t") { e.preventDefault(); setW(1); }
    else if (key === "m") { e.preventDefault(); setW(0); }
    else if (key === "f") { e.preventDefault(); setW(0.5); }
    else if (key === "enter") { e.preventDefault(); onRecognizeClick(); }
  }

  function renderResult(triResult) {
    lastTriResult = triResult;
    const classes = triResult.classes;
    const tofScores = fuseScores(triResult, 1);
    const melScores = fuseScores(triResult, 0);

    flipAnimate(barsEl.tof, () => { tofTopCache = renderColumn(barsEl.tof, classes, tofScores, triResult.reject_tof); });
    flipAnimate(barsEl.mel, () => { melTopCache = renderColumn(barsEl.mel, classes, melScores, triResult.reject_mel); });

    rejectBadgeEl.tof.style.display = triResult.reject_tof ? "inline-block" : "none";
    rejectBadgeEl.mel.style.display = triResult.reject_mel ? "inline-block" : "none";

    renderFusedColumn(); // uses currentW (whatever the slider is already at) and the caches just set above

    resultStatusEl.textContent = "已顯示辨識結果";

    // C20: every fresh result is one un-marked matrix observation. In
    // "assigned" mode the ground truth was already fixed before recognition
    // ran (the target shown in assignedPromptEl), so this records itself
    // and moves straight to the next target -- no "was this right?" step,
    // that's the whole point of assigning it in advance. "posthoc" instead
    // waits for the "✓ 正確" button or the "其實是" dropdown below.
    currentEntryMarked = false;
    if (markingMode === "assigned" && assignedTarget) {
      recordMatrixEntry(assignedTarget.id);
      showNextAssignedTarget();
    } else if (markingMode === "posthoc") {
      posthocSelectEl.value = "";
      posthocControlsEl.style.display = "flex";
    }
  }

  async function onRecognizeClick() {
    resultStatusEl.textContent = "辨識中…";
    try {
      const res = await fetch("/recognize", { method: "POST" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const triResult = await res.json();
      if (!triResult || !Array.isArray(triResult.classes) || !Array.isArray(triResult.d_tof) || !Array.isArray(triResult.d_mel)
          || !Array.isArray(triResult.d_tof_raw) || !Array.isArray(triResult.d_mel_raw)) {
        throw new Error("malformed TriResult");
      }
      renderResult(triResult);
    } catch (err) {
      // D09's /recognize doesn't exist yet (confirmed live, 404) -- this
      // is the expected state right now, not a real error to alarm over.
      resultStatusEl.textContent = "尚未串接（/recognize 還沒上線）";
      console.warn("[quiz] /recognize unavailable:", err.message);
    }
  }

  function renderCards() {
    const words = vocab.words || [];
    const reject = vocab.reject || FALLBACK_VOCAB.reject;

    cardsEl.innerHTML = words.map((w) => `
      <div class="quiz-card" data-word-id="${w.id}">
        <div class="quiz-card-text">${w.text}</div>
        <div class="quiz-card-viseme">${w.viseme || ""}</div>
        <div class="quiz-card-expect" data-expect="${w.expect || ""}">${EXPECT_LABEL[w.expect] || w.expect || ""}</div>
      </div>
    `).join("") + `
      <div class="quiz-card quiz-card-reject" data-word-id="${reject.id}">
        <div class="quiz-card-text">${reject.text}</div>
        <div class="quiz-card-viseme">系統判定：不認得</div>
      </div>
    `;

    // 12.5% only holds for exactly 8 words (C15.md); recomputed so E08's
    // "降到 4 選項" (which becomes 25%) or any other count stays honest.
    // reject isn't counted -- it's "no match", not a candidate guess.
    const n = words.length || 1;
    baselineEl.textContent = `隨機基準 ${(100 / n).toFixed(1)}%`;
  }

  async function loadVocab() {
    try {
      const res = await fetch(VOCAB_URL);
      if (!res.ok) throw new Error("HTTP " + res.status);
      const json = await res.json();
      if (!json || !Array.isArray(json.words)) throw new Error("malformed vocab.json");
      vocab = json;
    } catch (err) {
      console.error("[quiz] failed to load vocab.json, falling back to an empty set:", err);
      vocab = FALLBACK_VOCAB;
    }
    renderCards();
  }

  function updateVadStatus() {
    if (speakingMode === "silent") {
      // B13.md: an audio-trigger VAD literally cannot be constructed with
      // speaking_mode=silent -- there's no meaningful "listening" state to
      // show, so don't show one.
      vadStatusEl.className = "quiz-vad-status unavailable";
      vadStatusEl.innerHTML = `<span class="quiz-vad-icon">✕</span> Auto-VAD 不適用（無聲模式）—— 請改用下方觸發方式`;
      triggerRowEl.style.display = "flex";
    } else {
      vadStatusEl.className = "quiz-vad-status ready";
      vadStatusEl.innerHTML = `<span class="quiz-vad-icon breathing">●</span> Auto-VAD 就緒中`;
      triggerRowEl.style.display = "none";
    }
  }

  function setSpeakingMode(mode) {
    speakingMode = mode;
    speakingModeEls.forEach((el) => el.classList.toggle("active", el.dataset.speakingMode === mode));
    updateVadStatus();
  }

  function setTriggerSource(src) {
    triggerSource = src;
    triggerEls.forEach((el) => el.classList.toggle("active", el.dataset.trigger === src));
  }

  return {
    init(root) {
      root.innerHTML = `
        <div class="section-label">測驗模式 · 8 選項閉集合
          <span class="quiz-baseline mono" data-baseline>隨機基準 --</span>
        </div>
        <div class="quiz-prompt">請念出其中一個詞</div>
        <div class="quiz-vad-status" data-vad-status></div>
        <div class="quiz-speaking-mode" data-speaking-mode>
          <span class="quiz-control-label">說話模式</span>
          ${SPEAKING_MODES.map((m) => `<button class="quiz-mode-btn" data-speaking-mode-btn data-speaking-mode="${m.key}">${m.label}</button>`).join("")}
        </div>
        <div class="quiz-trigger-row" data-trigger-row style="display:none">
          <span class="quiz-control-label">觸發來源</span>
          ${TRIGGER_SOURCES.map((t) => `<button class="quiz-mode-btn" data-trigger-btn data-trigger="${t.key}">${t.label}</button>`).join("")}
        </div>
        <div class="quiz-cards" data-cards></div>

        <div class="quiz-result-controls">
          <button class="quiz-mode-btn" data-recognize-btn>觸發辨識</button>
          <span class="quiz-result-status mono" data-result-status>尚無辨識結果</span>
        </div>
        <div class="quiz-disagree-banner" data-disagree-banner style="display:none">
          ⚠ 三軌判斷不一致
        </div>
        <div class="quiz-result-card" data-result-card style="display:none">
          <div class="quiz-result-ring-wrap">
            <svg class="quiz-result-ring" viewBox="0 0 120 120" width="120" height="120">
              <circle class="quiz-result-ring-track" cx="60" cy="60" r="${RING_R}"></circle>
              <circle class="quiz-result-ring-fill" data-ring-fill cx="60" cy="60" r="${RING_R}"
                      style="stroke-dasharray:${RING_CIRC};stroke-dashoffset:${RING_CIRC}"></circle>
            </svg>
            <div class="quiz-result-ring-pct mono" data-result-pct>--</div>
          </div>
          <div class="quiz-result-card-main">
            <div class="quiz-result-card-word" data-result-word>—</div>
            <div class="quiz-result-card-sub mono" data-result-sub>尚無辨識結果</div>
          </div>
          <button class="quiz-mode-btn quiz-result-retry-btn" data-retry-btn>重試 (Enter)</button>
        </div>
        <div class="quiz-w-slider-row">
          <span class="quiz-w-end-label">ToF</span>
          <input type="range" class="quiz-w-slider" data-w-slider min="0" max="1" step="0.01" value="${DEFAULT_FUSED_W}">
          <span class="quiz-w-end-label">音訊</span>
          <span class="quiz-w-value mono" data-w-value>w = ${DEFAULT_FUSED_W.toFixed(2)}</span>
        </div>
        <div class="quiz-results" data-results>
          <div class="quiz-result-col">
            <div class="quiz-result-col-head">ToF only
              <span class="quiz-reject-badge" data-reject-badge="tof" style="display:none">拒識</span>
            </div>
            <div class="quiz-bars" data-bars="tof"></div>
          </div>
          <div class="quiz-result-col">
            <div class="quiz-result-col-head">Mel only
              <span class="quiz-reject-badge" data-reject-badge="mel" style="display:none">拒識</span>
            </div>
            <div class="quiz-bars" data-bars="mel"></div>
          </div>
          <div class="quiz-result-col fused">
            <div class="quiz-result-col-head">Fused ★
              <span class="quiz-reject-badge" data-reject-badge="fused" style="display:none">拒識</span>
            </div>
            <div class="quiz-bars" data-bars="fused"></div>
          </div>
        </div>
      `;

      cardsEl = root.querySelector("[data-cards]");
      baselineEl = root.querySelector("[data-baseline]");
      vadStatusEl = root.querySelector("[data-vad-status]");
      speakingModeEls = Array.from(root.querySelectorAll("[data-speaking-mode-btn]"));
      triggerRowEl = root.querySelector("[data-trigger-row]");
      triggerEls = Array.from(root.querySelectorAll("[data-trigger-btn]"));

      speakingModeEls.forEach((el) => el.addEventListener("click", () => setSpeakingMode(el.dataset.speakingMode)));
      triggerEls.forEach((el) => el.addEventListener("click", () => setTriggerSource(el.dataset.trigger)));

      setSpeakingMode("normal");
      setTriggerSource("tof");
      loadVocab();

      recognizeBtn = root.querySelector("[data-recognize-btn]");
      resultStatusEl = root.querySelector("[data-result-status]");
      resultsAreaEl = root.querySelector("[data-results]");
      disagreeBannerEl = root.querySelector("[data-disagree-banner]");
      barsEl.tof = root.querySelector('[data-bars="tof"]');
      barsEl.mel = root.querySelector('[data-bars="mel"]');
      barsEl.fused = root.querySelector('[data-bars="fused"]');
      rejectBadgeEl.tof = root.querySelector('[data-reject-badge="tof"]');
      rejectBadgeEl.mel = root.querySelector('[data-reject-badge="mel"]');
      rejectBadgeEl.fused = root.querySelector('[data-reject-badge="fused"]');
      recognizeBtn.addEventListener("click", onRecognizeClick);

      wSliderEl = root.querySelector("[data-w-slider]");
      wValueEl = root.querySelector("[data-w-value]");
      wSliderEl.addEventListener("input", onSliderInput);
      document.addEventListener("keydown", onKeydown);

      resultCardEl = root.querySelector("[data-result-card]");
      resultWordEl = root.querySelector("[data-result-word]");
      resultSubEl = root.querySelector("[data-result-sub]");
      resultPctEl = root.querySelector("[data-result-pct]");
      ringFillEl = root.querySelector("[data-ring-fill]");
      retryBtn = root.querySelector("[data-retry-btn]");
      retryBtn.addEventListener("click", onRecognizeClick);
    },

    onData(evt) {
      // Forward-compatible hook: once a real {type:"trial"} producer
      // exists, this is where PROMPT/COUNTDOWN/CAPTURE/SAVE/REST would
      // replace the static "ready" indicator above. Nothing publishes
      // this yet (verified live), so there's nothing to wire against.
      if (evt.type !== "trial") return;
    },
  };
})());
