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
  // three shortcuts. Global (document-level, like C05/C07's S/D/B), so a
  // presenter doesn't need the slider focused or even visible on screen.
  function onKeydown(e) {
    if (isTypingTarget(e.target) || e.altKey || e.ctrlKey || e.metaKey) return;
    if (!isQuizModeActive()) return;
    const key = e.key.toLowerCase();
    if (key === "t") { e.preventDefault(); setW(1); }
    else if (key === "m") { e.preventDefault(); setW(0); }
    else if (key === "f") { e.preventDefault(); setW(0.5); }
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
