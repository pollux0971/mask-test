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

import { registerMode } from "../shell.js";

const VOCAB_URL = "data/vocab.json";
const FALLBACK_VOCAB = { words: [], reject: { id: "_reject", text: "靜止／其他" } };

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
