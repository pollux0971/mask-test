// Quiz mode (C15): 8-option closed-set layout.
//
// Data source note: words come from GET /config/vocab (esp-mask-test-ed's
// endpoint, reads config/vocab.json fresh on every request -- no caching,
// no restart needed). This used to be a manually-synced COPY at
// panel/data/vocab.json (C15's own stopgap, made before the endpoint
// existed) -- that file still exists for record.js, which hasn't switched
// yet, but quiz.js no longer reads it. Editing config/vocab.json now
// changes the displayed options with zero JS changes and zero restart,
// which is what C15.md's "改 JSON 即改變選項" and E08's "4 選項備援只需要
// 改一個 JSON" actually need -- and unlike the old copy, there's no second
// file that can silently drift out of sync.
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
import { dataStore } from "../bus.js";
import {
  drawMelWaterfallStatic, drawTofKeyframeGrid, computeZoneStats,
  fitPCA2Stub, projectPCA, tryFetchServerPcaModel, drawPcaTrailStatic,
} from "../draw/thumbnails.js";

const VOCAB_URL = "/config/vocab";
const FALLBACK_VOCAB = { words: [], reject: { id: "_reject", text: "靜止／其他" } };
const DEFAULT_FUSED_W = 0.5; // Demo script step 1; C17 makes this a live slider

// --- C19: staged progress + input-signal thumbnails ---
//
// "分階段進度是走 B14 路線時的必要設計" (C19.md) -- B14.md's own latency
// breakdown is 錄音 2.0s + base64 傳輸 1.9s + 解碼/MFCC 0.1s = ~4.0s total.
// Only two of those phases have a live SSE signal today (bridge_server.py's
// `{type:"record", state:"receiving"|"done"}`, confirmed live -- there's no
// "recording" state event at all, and nothing between "done" and
// POST /recognize's own response arriving covers "解碼/MFCC"). So:
//   recording  -- from the moment the request is sent until "receiving"
//                 arrives (or the response itself, if it beats that)
//   receiving  -- from "receiving" until "done"
//   analyzing  -- from "done" (or from request-sent, if "done" never
//                 arrives -- e.g. today, since /recognize is 404) until the
//                 fetch resolves. No live progress signal exists for this
//                 phase, so it's shown as elapsed time with an explicit
//                 indeterminate (breathing) treatment, never a fabricated
//                 percentage -- same "don't fake per-step progress" call
//                 esp-mask-test-ca made for C22's run_all.
const PROGRESS_STAGES = ["recording", "receiving", "analyzing"];
const PROGRESS_STAGE_LABEL = { recording: "錄音中", receiving: "傳輸中", analyzing: "分析中" };
const PROGRESS_TICK_MS = 250;

// Capture window for the thumbnails: dataStore.getRecent(streamKey, ms),
// copied into plain objects immediately (never a held live reference --
// dataStore's ring buffers keep trimming, esp-mask-test-4f's C13 finding:
// a live reference goes silently empty later, indistinguishable from
// "never captured"). Window length = actual elapsed request time + a fixed
// slack, not a guess -- it's exactly how far back the data that could have
// fed this recognition goes.
const CAPTURE_WINDOW_SLACK_MS = 500;
const TOF_THUMB_PX = 72; // small keyframe grid, per-cell backing pixels
const MEL_THUMB_COLS = 80; // fewer columns than monitor.js's live 320 -- this is a thumbnail, not a scrolling waterfall
// A one-shot per-trial PCA fit is inherently sample-starved compared to
// monitor.js's continuously-refit MIN_FIT_SAMPLES=40 (chosen there as
// "well above the 64-dim rank-deficiency floor" for a 15s sliding window).
// A single ~2-4s trial has far fewer frames to offer, so this floor is
// intentionally lower -- below it, the fit would just be interpolating
// through too few points to mean anything, so this shows "資料不足" instead
// of a misleadingly confident-looking trajectory.
const PCA_THUMB_MIN_SAMPLES = 16;

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
// MATRIX_CELL_SIZE is authored at MATRIX_CELL_BASE_ROOT_PX (the default,
// un-scaled root font-size) -- canvas text (ctx.font, drawMatrix() below)
// doesn't participate in CSS's rem scaling at all, so C25's projector mode
// (html[data-projector-mode] { font-size:130% }) enlarges every DOM label
// around the matrix but leaves the matrix's own cells and text exactly
// MATRIX_CELL_SIZE px forever unless something explicitly compensates.
// currentMatrixCellSize() below does that for the on-screen canvas;
// exportMatrixPNG() deliberately keeps using this fixed base value
// unscaled -- a 300dpi print export shouldn't depend on whatever font-size
// the viewer's screen happened to be at when they clicked "匯出 PNG".
const MATRIX_CELL_SIZE = 40;
const MATRIX_CELL_BASE_ROOT_PX = 16;
const MATRIX_LABEL_W_RATIO = 1.8; // room for two-character labels like 不要/靜止／其他
const MATRIX_LABEL_H_RATIO = 1.3;
const EXPORT_DPI = 300;
const CSS_DPI = 96; // browsers' baseline px-per-inch assumption; scale = EXPORT_DPI/CSS_DPI

// The cells themselves grow, not just the text -- ctx.font is already
// cellSize * 0.24 (see drawMatrix() below), so scaling cellSize alone keeps
// the existing font-to-cell ratio and the text automatically follows,
// instead of enlarging text into cells that stayed the old size.
function currentMatrixCellSize() {
  const rootPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || MATRIX_CELL_BASE_ROOT_PX;
  return Math.round(MATRIX_CELL_SIZE * (rootPx / MATRIX_CELL_BASE_ROOT_PX));
}

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

  // --- D08 (partial): build templates from the current recording ---
  let buildTemplatesBtn = null, buildTemplatesStatusEl = null, buildTemplatesWarningsEl = null;
  let buildTemplatesPollId = null;

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
  let modeSwitchWarningEl = null;

  // --- C21: session stats row ---
  const statsBodyEl = { tof: null, mel: null, fused: null };
  let statsBaselineEl = null;

  // --- C19: staged progress + input-signal thumbnails ---
  let progressEl = null, progressNoteEl = null;
  const progressStageEls = {};
  let progressStage = null; // null | "recording" | "receiving" | "analyzing" | "done"
  let progressTimerId = null;
  let recognizeRequestSentAt = null;
  let thumbnailsEl = null;
  let thumbTofGridEl = null, thumbTofNoteEl = null;
  let thumbMelCanvas = null, thumbMelCtx = null, thumbMelNoteEl = null;
  let thumbPcaCanvas = null, thumbPcaCtx = null, thumbPcaNoteEl = null;

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
    renderStats();
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
    const cellSize = currentMatrixCellSize();
    const { width, height } = matrixLayoutSize(cellSize);
    matrixCanvasEl.width = width;
    matrixCanvasEl.height = height;
    drawMatrix(matrixCanvasEl.getContext("2d"), cellSize);
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
    // esp-mask-test-ad's C20-review followup: switching modes while a
    // result is still unmarked silently drops it (never counted in the
    // matrix or in C21's stats below) -- "靜默" was the actual complaint,
    // not the drop itself (E05 recording hundreds of trials, nobody would
    // notice one missing). Not blocking the switch, just saying so once.
    if (mode !== markingMode && !currentEntryMarked && modeSwitchWarningEl) {
      modeSwitchWarningEl.textContent = "⚠ 上一筆尚未標記的紀錄已略過（不計入矩陣／統計）";
      modeSwitchWarningEl.style.display = "block";
    }
    markingMode = mode;
    markingModeEls.forEach((el) => el.classList.toggle("active", el.dataset.markingMode === mode));
    assignedPromptEl.style.display = mode === "assigned" ? "flex" : "none";
    posthocControlsEl.style.display = mode === "posthoc" && !currentEntryMarked ? "flex" : "none";
    if (mode === "assigned" && !assignedTarget) showNextAssignedTarget();
  }

  // --- C21: session stats row ---
  //
  // Wilson score interval, not the normal approximation (C21.md: normal
  // approx gives nonsense (>100%/<0%) CIs at n<30). Verified against the
  // story's own worked example: n=12, k=10 -> 83.3%, 95% CI [55.2%, 95.3%]
  // reproduces exactly with this formula (checked by hand before wiring
  // this in, since there's no JS test runner in this repo to pin it with).
  const WILSON_Z95 = 1.959963985;

  function wilsonInterval(k, n) {
    if (n === 0) return { lower: 0, upper: 1 };
    const z = WILSON_Z95;
    const phat = k / n;
    const z2 = z * z;
    const denom = 1 + z2 / n;
    const center = phat + z2 / (2 * n);
    const margin = z * Math.sqrt((phat * (1 - phat)) / n + z2 / (4 * n * n));
    return { lower: Math.max(0, (center - margin) / denom), upper: Math.min(1, (center + margin) / denom) };
  }

  // top-3: is trueLabel among the top 3 ranked classes at this w? Mirrors
  // predictedLabelFor()'s own reject-first logic for k=1 consistency: if
  // the track itself rejected, only a true _reject target counts as a hit
  // (an AAC user reaching for their 2nd/3rd choice assumes the system at
  // least attempted a word, per C21.md's "top-1 70% 但 top-3 95%" framing).
  function topKHit(triResult, w, trueLabel, k) {
    const reject = vocab.reject || FALLBACK_VOCAB.reject;
    const rejectedNow = computeFusedReject(triResult, w);
    if (trueLabel === reject.id) return rejectedNow;
    if (rejectedNow) return false;
    const scores = fuseScores(triResult, w);
    const ranked = sortedEntries(triResult.classes, scores).slice(0, k).map((e) => e.cls);
    return ranked.includes(trueLabel);
  }

  // esp-mask-test-ad's explicit split: 正確/答錯/拒識 are three different
  // things, reject must never get folded into "wrong" -- D22 just pushed
  // false-reject down to ~0%, and that number only reads as a result if
  // "拒識" has its own bucket instead of being buried inside "答錯".
  // Classified by PREDICTED label (not by trueLabel), so a false-accept
  // during an assigned-silence trial (trueLabel=_reject, predicted=some
  // word) correctly lands in 答錯, not 拒識 -- the system didn't say
  // "unrecognized", it confidently picked the wrong thing.
  function computeTrackStats(w) {
    const reject = vocab.reject || FALLBACK_VOCAB.reject;
    let correct = 0, wrong = 0, rejected = 0, top3 = 0;
    matrixEntries.forEach(({ trueLabel, triResult }) => {
      const predicted = predictedLabelFor(triResult, w);
      if (predicted === trueLabel) correct++;
      else if (predicted === reject.id) rejected++;
      else wrong++;
      if (topKHit(triResult, w, trueLabel, 3)) top3++;
    });
    const n = matrixEntries.length;
    const ci = wilsonInterval(correct, n);
    return {
      n, correct, wrong, rejected,
      accuracy: n ? correct / n : 0,
      ciLower: ci.lower, ciUpper: ci.upper,
      top3Accuracy: n ? top3 / n : 0,
    };
  }

  // Same "reject isn't a candidate guess" convention as renderCards()'s
  // baselineEl -- kept as its own function so C21's baseline number can
  // never silently drift from C15's original one.
  function randomBaselineFraction() {
    const n = (vocab.words || []).length || 1;
    return 1 / n;
  }

  function renderStatBlock(s) {
    const pct = (x) => (x * 100).toFixed(1);
    if (!s.n) return `<div class="quiz-stat-acc">--</div><div class="quiz-stat-sub mono">尚無資料</div>`;
    return `
      <div class="quiz-stat-acc">${pct(s.accuracy)}%<span class="quiz-stat-ci mono"> [${pct(s.ciLower)}–${pct(s.ciUpper)}%]</span></div>
      <div class="quiz-stat-sub mono">top-3 ${pct(s.top3Accuracy)}%　n=${s.n}</div>
      <div class="quiz-stat-breakdown mono">✓ ${s.correct}　✕ ${s.wrong}　⦸ ${s.rejected}</div>
    `;
  }

  // Same recompute-the-whole-history idea as C20's renderMatrix(): a w
  // change re-answers "what would the system have said" for every past
  // trial, so accuracy/CI/top-3 all have to be recomputed from scratch
  // too, not just the newest entry.
  function renderStats() {
    if (!statsBodyEl.tof) return;
    if (statsBaselineEl) statsBaselineEl.textContent = `隨機基準 ${(randomBaselineFraction() * 100).toFixed(1)}%`;
    [["tof", 1], ["mel", 0], ["fused", currentW]].forEach(([key, w]) => {
      statsBodyEl[key].innerHTML = renderStatBlock(computeTrackStats(w));
    });
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
    renderStats(); // C21: same reason -- accuracy/CI/top-3 are all a function of w too

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

    // C19: input-signal thumbnails. Deliberately NOT called from inside
    // renderFusedColumn() alongside C17/C20/C21 -- unlike those three,
    // thumbnails are a function of the captured INPUT (what the sensors
    // saw), not of the fusion weight w, so recomputing them every time the
    // slider moves would just redraw the identical three images repeatedly
    // for no reason. This still runs exactly once per fresh result, right
    // next to the renderFusedColumn() call that starts the rest of the
    // "a result just arrived" chain.
    const elapsedMs = recognizeRequestSentAt != null
      ? performance.now() - recognizeRequestSentAt
      : CAPTURE_WINDOW_SLACK_MS;
    captureAndRenderThumbnails(elapsedMs);

    resultStatusEl.textContent = "已顯示辨識結果";

    // C20: every fresh result is one un-marked matrix observation. In
    // "assigned" mode the ground truth was already fixed before recognition
    // ran (the target shown in assignedPromptEl), so this records itself
    // and moves straight to the next target -- no "was this right?" step,
    // that's the whole point of assigning it in advance. "posthoc" instead
    // waits for the "✓ 正確" button or the "其實是" dropdown below.
    currentEntryMarked = false;
    if (modeSwitchWarningEl) modeSwitchWarningEl.style.display = "none";
    if (markingMode === "assigned" && assignedTarget) {
      recordMatrixEntry(assignedTarget.id);
      showNextAssignedTarget();
    } else if (markingMode === "posthoc") {
      posthocSelectEl.value = "";
      posthocControlsEl.style.display = "flex";
    }
  }

  // --- C19: staged progress ---

  function startProgress() {
    progressStage = "recording";
    if (thumbnailsEl) thumbnailsEl.style.display = "none";
    if (progressEl) progressEl.style.display = "flex";
    renderProgress();
    if (progressTimerId != null) clearInterval(progressTimerId);
    progressTimerId = setInterval(renderProgress, PROGRESS_TICK_MS);
  }

  // Stages only ever move forward. A stray "receiving"/"done" arriving
  // after this trial already reached "analyzing" (e.g. a near-simultaneous
  // manual /record click from record.js firing the same SSE states) must
  // not rewind the bar back to an earlier stage.
  function setProgressStage(stage) {
    const from = progressStage ? PROGRESS_STAGES.indexOf(progressStage) : -1;
    const to = PROGRESS_STAGES.indexOf(stage);
    if (to <= from) return;
    progressStage = stage;
    renderProgress();
  }

  function renderProgress() {
    if (!progressEl || progressStage == null) return;
    const finished = progressStage === "done";
    const currentIdx = finished ? PROGRESS_STAGES.length : PROGRESS_STAGES.indexOf(progressStage);
    PROGRESS_STAGES.forEach((s, i) => {
      const el = progressStageEls[s];
      if (!el) return;
      el.classList.toggle("done", i < currentIdx);
      el.classList.toggle("active", i === currentIdx && !finished);
      // "analyzing" has no live server progress signal -- indeterminate
      // (breathing) rather than a fabricated percentage. See the C19
      // module comment above for why.
      el.classList.toggle("indeterminate", i === currentIdx && !finished && s === "analyzing");
    });
    if (progressNoteEl) {
      if (finished) {
        progressNoteEl.textContent = "完成";
      } else {
        const elapsedS = recognizeRequestSentAt != null ? (performance.now() - recognizeRequestSentAt) / 1000 : 0;
        progressNoteEl.textContent = `已等待 ${elapsedS.toFixed(1)} 秒`;
      }
    }
  }

  function finishProgress() {
    progressStage = "done";
    renderProgress();
    if (progressTimerId != null) { clearInterval(progressTimerId); progressTimerId = null; }
    // Leave the "完成" state on screen briefly instead of yanking it away
    // the instant the fetch resolves -- the stage labels finishing is part
    // of what tells the audience "this just happened", not just the
    // thumbnails appearing underneath.
    setTimeout(hideProgress, 600);
  }

  function hideProgress() {
    if (progressTimerId != null) { clearInterval(progressTimerId); progressTimerId = null; }
    if (progressEl) progressEl.style.display = "none";
    progressStage = null;
  }

  // --- C19: input-signal thumbnails ---

  function pickNearestFrame(frames, targetMs) {
    let best = frames[0], bestDiff = Infinity;
    for (const f of frames) {
      const diff = Math.abs(f.t - targetMs);
      if (diff < bestDiff) { bestDiff = diff; best = f; }
    }
    return best;
  }

  // 3 keyframes per C19.md's suggestion (VAD 起點／能量峰值／VAD 終點).
  // There's no VAD start/end producer (confirmed live -- same gap C08's
  // noteVadMark comment documents, nothing publishes vad_start_us/
  // vad_end_us today), so start/end honestly fall back to the captured
  // window's own first/last frame rather than a real speech boundary.
  // "能量峰值" is NOT a fallback, though -- it's computed for real from the
  // captured mic RMS history, correlated by timestamp against the ToF
  // frames (every dataStore stream shares the same performance.now() clock
  // per bus.js, so this correlation is exact, not approximate).
  function pickKeyframeTimes(tofFrames, micFrames) {
    if (!tofFrames.length) return [];
    const startT = tofFrames[0].t;
    const endT = tofFrames[tofFrames.length - 1].t;
    let peakT = tofFrames[Math.floor(tofFrames.length / 2)].t; // fallback: middle by index
    if (micFrames.length) {
      let peak = micFrames[0];
      for (const m of micFrames) if (m.rms > peak.rms) peak = m;
      peakT = peak.t;
    }
    return [startT, peakT, endT];
  }

  function renderTofThumb(tofA, tofB, micFrames) {
    thumbTofGridEl.innerHTML = "";
    if (!tofA.length && !tofB.length) {
      thumbTofNoteEl.textContent = "尚無資料";
      thumbTofGridEl.innerHTML = `<div class="quiz-thumb-empty">尚無資料</div>`;
      return;
    }
    const times = pickKeyframeTimes(tofA.length ? tofA : tofB, micFrames);
    thumbTofNoteEl.textContent = micFrames.length ? "起點／能量峰值／終點" : "起點／中點／終點（無音訊資料，退回取中點）";
    for (const [label, frames] of [["A", tofA], ["B", tofB]]) {
      if (!frames.length) continue;
      const rowEl = document.createElement("div");
      rowEl.className = "quiz-thumb-tof-row";
      const labelEl = document.createElement("span");
      labelEl.className = "quiz-thumb-tof-row-label";
      labelEl.textContent = label;
      rowEl.appendChild(labelEl);
      // Zone-stat baseline computed from THIS trial's own captured window
      // (there's no cross-mode access to monitor.js's B10 baseline state),
      // same computeZoneStats() B10 baseline capture uses -- so the Δ
      // heatmap colors show "how far this keyframe is from this trial's
      // own average", which is exactly the contrast that makes a mouth
      // movement visible.
      const dim = frames[frames.length - 1].dim;
      const sameDim = frames.filter((f) => f.dim === dim);
      const stats = computeZoneStats(sameDim, dim);
      const baseline = {
        mu: stats.mu, sigma: stats.sigma,
        flags: { unstable: stats.unstable, suspectZeroVariance: stats.suspectZeroVariance, noSignal: stats.noSignal },
      };
      for (const t of times) {
        const frame = pickNearestFrame(sameDim, t);
        const canvas = document.createElement("canvas");
        canvas.width = TOF_THUMB_PX;
        canvas.height = TOF_THUMB_PX;
        rowEl.appendChild(canvas);
        drawTofKeyframeGrid(canvas.getContext("2d"), TOF_THUMB_PX, TOF_THUMB_PX, frame.dist, frame.valid, baseline);
      }
      thumbTofGridEl.appendChild(rowEl);
    }
  }

  function renderMelThumb(melFrames) {
    const w = thumbMelCanvas.clientWidth || MEL_THUMB_COLS, h = thumbMelCanvas.clientHeight || 28;
    thumbMelCanvas.width = w;
    thumbMelCanvas.height = h;
    const ctx = thumbMelCtx;
    if (!melFrames.length) {
      thumbMelNoteEl.textContent = "尚無 Mel 資料（未啟用，或本次沒有擷取到）";
      ctx.clearRect(0, 0, w, h);
      return;
    }
    thumbMelNoteEl.textContent = `${melFrames.length} 幀`;
    // Downsample to MEL_THUMB_COLS columns when there are more frames than
    // that -- this is a thumbnail, not a full-resolution scroll, per
    // C19.md's "縮圖不拖慢主要結果的顯示".
    const step = Math.max(1, Math.floor(melFrames.length / MEL_THUMB_COLS));
    const sampled = [];
    for (let i = 0; i < melFrames.length; i += step) sampled.push(melFrames[i].bands);
    drawMelWaterfallStatic(ctx, w, h, sampled);
  }

  // Same 64-dim [A.dist, A.sig, B.dist, B.sig] feature vector monitor.js's
  // combinedVectorNow() builds, just paired by nearest timestamp instead of
  // "whatever's currently latest" (there's no live rAF loop here re-pairing
  // every frame, so this reconstructs the pairing once from the capture).
  function pairTofSamples(tofA, tofB) {
    if (!tofA.length || !tofB.length) return [];
    const out = [];
    for (const a of tofA) {
      const b = pickNearestFrame(tofB, a.t);
      if (a.dim === b.dim) out.push({ t: a.t, vec: a.dist.concat(a.signal, b.dist, b.signal) });
    }
    return out;
  }

  async function renderPcaThumb(tofA, tofB) {
    const samples = pairTofSamples(tofA, tofB);
    const w = thumbPcaCanvas.clientWidth || 120, h = thumbPcaCanvas.clientHeight || 80;
    thumbPcaCanvas.width = w;
    thumbPcaCanvas.height = h;
    const ctx = thumbPcaCtx;
    if (samples.length < PCA_THUMB_MIN_SAMPLES) {
      thumbPcaNoteEl.textContent = `資料不足（${samples.length} 筆，需要至少 ${PCA_THUMB_MIN_SAMPLES} 筆）`;
      ctx.clearRect(0, 0, w, h);
      return;
    }
    // Same auto-upgrade pattern as C10: try the real server model first,
    // fall back to a one-shot client-side stub fit from just this trial's
    // samples (labeled as such below, never presented as the real thing).
    let model = await tryFetchServerPcaModel("tof_only");
    let stub = !model;
    if (!model) model = fitPCA2Stub(samples.map((s) => s.vec));
    const trail = samples
      .filter((s) => s.vec.length === model.dims)
      .map((s) => { const [x, y] = projectPCA(model, s.vec); return { x, y }; });
    // No confidence ellipses -- there's no enrollment data (D08 gap, same
    // one C10's own PCA panel already flags), so this omits them honestly
    // rather than fabricating a placeholder.
    drawPcaTrailStatic(ctx, w, h, trail, null);
    thumbPcaNoteEl.textContent = stub
      ? `本次試驗即時擬合（${samples.length} 筆樣本，座標軸僅供參考）`
      : `伺服器模型（${model.source}）`;
  }

  function captureAndRenderThumbnails(elapsedMs) {
    if (!thumbnailsEl) return;
    const windowMs = Math.max(500, elapsedMs) + CAPTURE_WINDOW_SLACK_MS;
    // Copy into plain objects immediately -- never hold a live dataStore
    // reference (see the C19 module comment above / esp-mask-test-4f's C13
    // finding: a live reference goes silently empty later as the ring
    // buffer trims, indistinguishable from "never captured").
    const tofA = dataStore.getRecent("tofA", windowMs).map((e) => (
      { t: e._recvMs, dim: e.dim, dist: e.dist.slice(), signal: e.signal.slice(), valid: e.valid ? e.valid.slice() : null }));
    const tofB = dataStore.getRecent("tofB", windowMs).map((e) => (
      { t: e._recvMs, dim: e.dim, dist: e.dist.slice(), signal: e.signal.slice(), valid: e.valid ? e.valid.slice() : null }));
    const mic = dataStore.getRecent("mic", windowMs).map((e) => ({ t: e._recvMs, rms: e.rms }));
    const mel = dataStore.getRecent("mel", windowMs).map((e) => ({ t: e._recvMs, bands: e.bands.slice() }));

    thumbnailsEl.style.display = "grid";
    renderTofThumb(tofA, tofB, mic);
    renderMelThumb(mel);
    renderPcaThumb(tofA, tofB); // async (may fetch /pca) -- fine, draws in place once it resolves without blocking the rest
  }

  // Same split replay.js/record.js already use: a thrown fetch (can't
  // reach the bridge at all) is genuinely "尚未串接"/連不上後端; a response
  // that came back with !res.ok is the backend reachable and answering
  // with a real reason (e.g. 503 "尚無 enrollment 樣板，無法辨識" -- see
  // reports/RECOGNIZE_PIPELINE.md) -- that reason belongs on screen, not
  // flattened into the same generic "not wired yet" text and left to rot
  // in console.warn where only a developer would ever see it.
  async function onRecognizeClick() {
    resultStatusEl.textContent = "辨識中…";
    recognizeRequestSentAt = performance.now();
    startProgress();

    let res;
    try {
      res = await fetch("/recognize", { method: "POST" });
    } catch (err) {
      hideProgress();
      resultStatusEl.textContent = "連不上後端（" + err.message + "）";
      console.warn("[quiz] /recognize network error:", err.message);
      return;
    }

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      hideProgress();
      resultStatusEl.textContent = body.error || `辨識失敗：HTTP ${res.status}`;
      console.warn("[quiz] /recognize error:", res.status, body.error);
      return;
    }

    const triResult = await res.json().catch(() => null);
    if (!triResult || !Array.isArray(triResult.classes) || !Array.isArray(triResult.d_tof) || !Array.isArray(triResult.d_mel)
        || !Array.isArray(triResult.d_tof_raw) || !Array.isArray(triResult.d_mel_raw)) {
      hideProgress();
      resultStatusEl.textContent = "辨識結果格式不對（不是合法的 TriResult）";
      console.warn("[quiz] /recognize returned malformed TriResult:", triResult);
      return;
    }

    finishProgress();
    renderResult(triResult);
  }

  // --- D08 (partial): build templates from the current recording ---
  //
  // POST /templates/build defaults to the current session on the backend
  // side (reports/RECOGNIZE_PIPELINE.md), so this UI doesn't pick one --
  // just trigger it and show whatever comes back. Same fetch-throws vs
  // !res.ok split as onRecognizeClick() above, plus a bounded poll loop
  // for the 202-then-poll pattern C22's /verify/run already established.
  const BUILD_TEMPLATES_POLL_MS = 1000;
  const BUILD_TEMPLATES_POLL_MAX_ATTEMPTS = 300; // 5 min safety cap -- the normal case self-clears on completion well before this

  function renderBuildTemplatesWarnings(warnings) {
    if (!warnings || !warnings.length) {
      buildTemplatesWarningsEl.style.display = "none";
      buildTemplatesWarningsEl.innerHTML = "";
      return;
    }
    // Shown verbatim -- written to be read by the person who just
    // recorded (e.g. "n=1 沒有東西可以留一筆出來測，準確率是 nan"), not
    // developer-facing text that belongs buried in console.warn.
    buildTemplatesWarningsEl.innerHTML = warnings.map((w) => `<li>⚠ ${w}</li>`).join("");
    buildTemplatesWarningsEl.style.display = "block";
  }

  function stopBuildTemplatesPoll() {
    if (buildTemplatesPollId != null) {
      clearInterval(buildTemplatesPollId);
      buildTemplatesPollId = null;
    }
  }

  async function pollBuildTemplatesState(attempt) {
    let res;
    try {
      res = await fetch("/templates/build/state");
    } catch (err) {
      stopBuildTemplatesPoll();
      buildTemplatesStatusEl.textContent = "連不上後端（" + err.message + "）";
      console.warn("[quiz] /templates/build/state network error:", err.message);
      return;
    }
    if (!res.ok) {
      stopBuildTemplatesPoll();
      const body = await res.json().catch(() => ({}));
      buildTemplatesStatusEl.textContent = body.error || `查詢建樣板狀態失敗：HTTP ${res.status}`;
      return;
    }
    const state = await res.json().catch(() => null);
    if (!state) return;

    if (state.running) {
      buildTemplatesStatusEl.textContent = `建樣板中…（${(state.elapsed_s ?? 0).toFixed(1)} 秒）`;
      if (attempt >= BUILD_TEMPLATES_POLL_MAX_ATTEMPTS) {
        stopBuildTemplatesPoll();
        buildTemplatesStatusEl.textContent = "建樣板時間過長，停止等待（背景可能仍在跑，稍後重新整理再看一次）";
      }
      return;
    }

    stopBuildTemplatesPoll();
    if (state.last_error) {
      buildTemplatesStatusEl.textContent = state.last_error;
      renderBuildTemplatesWarnings(null);
      return;
    }
    const result = state.last_result;
    if (!result) {
      buildTemplatesStatusEl.textContent = "沒有結果（未曾建過）";
      return;
    }
    const counts = Object.entries(result.counts || {}).map(([label, n]) => `${label}=${n}`).join("，");
    buildTemplatesStatusEl.textContent = `建好了：${counts}`;
    renderBuildTemplatesWarnings(result.warnings);
  }

  async function onBuildTemplatesClick() {
    buildTemplatesStatusEl.textContent = "建樣板中…";
    renderBuildTemplatesWarnings(null);
    stopBuildTemplatesPoll();

    let res;
    try {
      res = await fetch("/templates/build", { method: "POST" });
    } catch (err) {
      buildTemplatesStatusEl.textContent = "連不上後端（" + err.message + "）";
      console.warn("[quiz] /templates/build network error:", err.message);
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      // Includes the 409 "這個 session 還在錄製中……請先按「結束 session」
      // 再建樣板" case (confirmed against a real file lock, see
      // reports/RECOGNIZE_PIPELINE.md) -- shown verbatim, not "發生錯誤".
      buildTemplatesStatusEl.textContent = body.error || `啟動建樣板失敗：HTTP ${res.status}`;
      console.warn("[quiz] /templates/build error:", res.status, body.error);
      return;
    }

    let attempt = 0;
    buildTemplatesPollId = setInterval(() => {
      attempt++;
      pollBuildTemplatesState(attempt);
    }, BUILD_TEMPLATES_POLL_MS);
    pollBuildTemplatesState(attempt); // don't wait a full interval for the first check
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

    // C20: the matrix's axes and the posthoc dropdown both come from the
    // same vocab, so they have to refresh together whenever it (re)loads --
    // guarded because init()'s synchronous wiring runs before this async
    // load resolves in the normal case, but not guaranteed to.
    if (posthocSelectEl) populateMatrixSelect();
    if (matrixCanvasEl) renderMatrix();
    if (statsBodyEl.tof) renderStats();
  }

  async function loadVocab() {
    try {
      const res = await fetch(VOCAB_URL);
      if (!res.ok) throw new Error("HTTP " + res.status);
      const json = await res.json();
      if (!json || !Array.isArray(json.words)) throw new Error("malformed vocab response");
      vocab = json;
    } catch (err) {
      // Expected when the bridge isn't running yet -- GET /config/vocab
      // 404s/fails to connect exactly like every other endpoint this file
      // already treats as a graceful-degradation case, not a fatal error.
      console.error("[quiz] failed to load /config/vocab, falling back to an empty set:", err);
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
        <div class="quiz-result-controls">
          <button class="quiz-mode-btn" data-build-templates-btn>用目前的錄音建樣板</button>
          <span class="quiz-result-status mono" data-build-templates-status></span>
        </div>
        <ul class="mono" data-build-templates-warnings style="display:none; margin:0; padding-left:20px;"></ul>
        <div class="quiz-progress" data-quiz-progress style="display:none">
          <div class="quiz-progress-stages">
            <span class="quiz-progress-stage" data-progress-stage="recording">錄音中</span>
            <span class="quiz-progress-stage" data-progress-stage="receiving">傳輸中</span>
            <span class="quiz-progress-stage" data-progress-stage="analyzing">分析中</span>
          </div>
          <span class="quiz-progress-note mono" data-progress-note></span>
        </div>
        <div class="quiz-thumbnails" data-thumbnails style="display:none">
          <div class="quiz-thumb-block">
            <div class="quiz-thumb-head">ToF Δ 熱力圖 <span class="quiz-thumb-sub mono" data-thumb-tof-note></span></div>
            <div class="quiz-thumb-tof-grid" data-thumb-tof-grid></div>
          </div>
          <div class="quiz-thumb-block">
            <div class="quiz-thumb-head">Mel 頻譜</div>
            <div class="quiz-thumb-canvas-wrap"><canvas data-thumb-mel></canvas></div>
            <div class="quiz-thumb-sub mono" data-thumb-mel-note></div>
          </div>
          <div class="quiz-thumb-block">
            <div class="quiz-thumb-head">PCA 軌跡</div>
            <div class="quiz-thumb-canvas-wrap"><canvas data-thumb-pca></canvas></div>
            <div class="quiz-thumb-sub mono" data-thumb-pca-note></div>
          </div>
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

        <div class="quiz-matrix-section">
          <div class="quiz-matrix-controls">
            <span class="quiz-control-label">標記模式</span>
            ${MARKING_MODES.map((m) => `<button class="quiz-mode-btn" data-marking-mode-btn data-marking-mode="${m.key}">${m.label}</button>`).join("")}
            <span class="quiz-matrix-count mono" data-matrix-count>已記錄 0 筆</span>
            <button class="quiz-mode-btn" data-matrix-export-btn>匯出 PNG</button>
            <button class="quiz-mode-btn" data-matrix-clear-btn>清除矩陣</button>
          </div>
          <div class="quiz-mode-switch-warning" data-mode-switch-warning style="display:none"></div>
          <div class="quiz-assigned-prompt" data-assigned-prompt style="display:none">
            系統指定：請念 <span class="quiz-assigned-word mono" data-assigned-word>—</span>
          </div>
          <div class="quiz-posthoc-controls" data-posthoc-controls style="display:none">
            <span class="quiz-control-label">這次實際上是</span>
            <button class="quiz-mode-btn quiz-posthoc-correct" data-posthoc-correct-btn>✓ 正確</button>
            <select class="quiz-posthoc-select" data-posthoc-select></select>
          </div>
          <div class="quiz-matrix-wrap">
            <canvas class="quiz-matrix-canvas" data-matrix-canvas></canvas>
          </div>
        </div>

        <div class="quiz-stats-section">
          <div class="section-label">Session 統計
            <span class="quiz-stats-baseline mono" data-stats-baseline>隨機基準 --</span>
          </div>
          <div class="quiz-stats-grid">
            <div class="quiz-stat-col" data-stats-col="tof">
              <div class="quiz-stat-head">ToF only</div>
              <div class="quiz-stat-body" data-stats-body="tof"></div>
            </div>
            <div class="quiz-stat-col" data-stats-col="mel">
              <div class="quiz-stat-head">Mel only</div>
              <div class="quiz-stat-body" data-stats-body="mel"></div>
            </div>
            <div class="quiz-stat-col fused" data-stats-col="fused">
              <div class="quiz-stat-head">Fused ★</div>
              <div class="quiz-stat-body" data-stats-body="fused"></div>
            </div>
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

      // --- D08 (partial): build templates from the current recording ---
      buildTemplatesBtn = root.querySelector("[data-build-templates-btn]");
      buildTemplatesStatusEl = root.querySelector("[data-build-templates-status]");
      buildTemplatesWarningsEl = root.querySelector("[data-build-templates-warnings]");
      buildTemplatesBtn.addEventListener("click", onBuildTemplatesClick);

      // --- C19: staged progress + thumbnail wiring ---
      progressEl = root.querySelector("[data-quiz-progress]");
      progressNoteEl = root.querySelector("[data-progress-note]");
      PROGRESS_STAGES.forEach((s) => {
        progressStageEls[s] = root.querySelector(`[data-progress-stage="${s}"]`);
      });
      thumbnailsEl = root.querySelector("[data-thumbnails]");
      thumbTofGridEl = root.querySelector("[data-thumb-tof-grid]");
      thumbTofNoteEl = root.querySelector("[data-thumb-tof-note]");
      thumbMelCanvas = root.querySelector("[data-thumb-mel]");
      thumbMelCtx = thumbMelCanvas.getContext("2d");
      thumbMelNoteEl = root.querySelector("[data-thumb-mel-note]");
      thumbPcaCanvas = root.querySelector("[data-thumb-pca]");
      thumbPcaCtx = thumbPcaCanvas.getContext("2d");
      thumbPcaNoteEl = root.querySelector("[data-thumb-pca-note]");

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

      // --- C20: confusion matrix wiring ---
      markingModeEls = Array.from(root.querySelectorAll("[data-marking-mode-btn]"));
      assignedPromptEl = root.querySelector("[data-assigned-prompt]");
      assignedWordEl = root.querySelector("[data-assigned-word]");
      posthocControlsEl = root.querySelector("[data-posthoc-controls]");
      posthocCorrectBtn = root.querySelector("[data-posthoc-correct-btn]");
      posthocSelectEl = root.querySelector("[data-posthoc-select]");
      matrixCanvasEl = root.querySelector("[data-matrix-canvas]");
      matrixCountEl = root.querySelector("[data-matrix-count]");
      matrixExportBtn = root.querySelector("[data-matrix-export-btn]");
      matrixClearBtn = root.querySelector("[data-matrix-clear-btn]");

      markingModeEls.forEach((el) => el.addEventListener("click", () => setMarkingMode(el.dataset.markingMode)));
      posthocCorrectBtn.addEventListener("click", () => recordMatrixEntry(predictedLabelFor(lastTriResult, currentW)));
      posthocSelectEl.addEventListener("change", () => recordMatrixEntry(posthocSelectEl.value));
      matrixExportBtn.addEventListener("click", exportMatrixPNG);
      matrixClearBtn.addEventListener("click", () => {
        matrixEntries = [];
        matrixCountEl.textContent = "已記錄 0 筆";
        renderMatrix();
        renderStats();
      });

      // --- C21: session stats wiring ---
      statsBodyEl.tof = root.querySelector('[data-stats-body="tof"]');
      statsBodyEl.mel = root.querySelector('[data-stats-body="mel"]');
      statsBodyEl.fused = root.querySelector('[data-stats-body="fused"]');
      statsBaselineEl = root.querySelector("[data-stats-baseline]");
      modeSwitchWarningEl = root.querySelector("[data-mode-switch-warning]");

      setMarkingMode("posthoc");
      renderMatrix();
      renderStats();

      // Projector mode (C25, shell.js) flips html[data-projector-mode] and
      // scales every DOM font-size via CSS -- canvas text/cell sizing
      // doesn't participate in that at all (see currentMatrixCellSize()'s
      // comment above), so without this the matrix would silently keep
      // its old size until the next unrelated redraw (a new recognize
      // result), which reads as "the toggle didn't work" rather than "it
      // worked, just hasn't repainted yet" -- worse than not scaling at
      // all. Watching the attribute directly (not shell.js's toggle
      // function, which isn't ours to touch) keeps this decoupled from
      // shell.js's own code.
      new MutationObserver(renderMatrix).observe(document.documentElement, {
        attributes: true, attributeFilter: ["data-projector-mode"],
      });
    },

    onData(evt) {
      // C19: staged progress for the B14 route. Only acts while a
      // recognize request from THIS mode is actually in flight
      // (progressStage is null otherwise) -- so a manual /record click
      // from record.js firing these same SSE states doesn't hijack quiz's
      // progress bar for an unrelated recording.
      if (evt.type === "record" && progressStage != null && progressStage !== "done") {
        if (evt.state === "receiving") setProgressStage("receiving");
        else if (evt.state === "done") setProgressStage("analyzing");
        // "error" is left alone here -- POST /recognize's own response is
        // the authoritative signal for whether THIS request failed, not a
        // same-shaped event that might belong to an unrelated recording.
      }

      // Forward-compatible hook: once a real {type:"trial"} producer
      // exists, this is where PROMPT/COUNTDOWN/CAPTURE/SAVE/REST would
      // replace the static "ready" indicator above. Nothing publishes
      // this yet (verified live), so there's nothing to wire against.
      if (evt.type !== "trial") return;
    },
  };
})());
