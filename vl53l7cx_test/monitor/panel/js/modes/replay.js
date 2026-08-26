// Replay mode (C24): load a saved HDF5 session and play it back through the
// SAME SSE pipeline live data uses (B17's ReplayController publishes onto
// the existing /events stream via broadcaster.publish() -- confirmed by
// B17's completion report), so switching to any other mode during playback
// "just works" with zero changes to bus.js/monitor.js/shell.js: those
// files already branch on evt.type, not on whether evt.replay is set.
//
// Backend endpoints this calls do NOT exist yet (confirmed live: every one
// 404s) -- B17 (host/replay/session_replay.py) is done, but B19's HTTP
// wiring for it hasn't landed. Proposed shape (from B17's completion
// report, relayed to ed):
//   GET  /replay/sessions                      -> {"files": ["<name>", ...]}
//   POST /replay/start?file=<name>&start_trial=<n>
//                                               -> {"trials": [{"idx","label","quality"}, ...]} (best-effort)
//   POST /replay/control?action=pause|resume|step
//   POST /replay/speed?value=0.25|1|4
//   POST /replay/seek?trial=<n>
// Same auto-upgrade pattern as quiz.js's /recognize and C10's /pca check:
// build the UI against the agreed shape, degrade to a visible "尚未串接"
// message on 404 instead of failing silently, and it starts working the
// moment ed wires bridge_server.py -- no changes needed here.
//
// Playback control semantics map directly onto ReplayController's three
// distinct anchor operations (session_replay.py's own hard-won lesson: one
// generic "rebase" for all four control ops caused resume() to instantly
// dump the whole backlog). This file doesn't re-derive that logic -- it
// only ever sends one action per button and lets the backend own state.
//
// REPLAY marks itself in two places, per C24.md ("不能只在回放模式裡標"):
// the sidebar text (already done by C04, listening for evt.replay -- this
// file doesn't touch shell.js) and a small fixed corner ribbon over the
// main content area, injected here (not into shell.js/index.html) so it
// survives switching away from replay mode. pointer-events:none so it
// never blocks clicks on whatever mode is actually showing underneath.

import { registerMode } from "../shell.js";

const REPLAY_WATERMARK_LINGER_MS = 3000; // matches shell.js's own REPLAY_WINDOW_MS
const SPEEDS = [0.25, 1, 4];

function fmtSpeed(v) {
  return (v === 1 ? "1" : v < 1 ? v.toFixed(2).replace(/0+$/, "").replace(/\.$/, "") : String(v)) + "×";
}

registerMode("replay", (() => {
  let filesSelectEl, fileInputEl, loadBtn, statusEl;
  let playPauseBtn, stepBtn, speedEls;
  let timelineEl, seekInputEl;
  let watermarkEl;

  let sessionLoaded = false;
  let paused = false;
  let speed = 1;
  let currentTrialIdx = null;
  // Keyed by idx so out-of-order SAVE-after-CAPTURE updates (quality can
  // change between the two) both land on the same row instead of the
  // timeline growing a duplicate entry per state transition.
  const trialsByIdx = new Map();
  let lastReplayEventAt = -Infinity;

  function setStatus(text, isError) {
    statusEl.textContent = text;
    statusEl.classList.toggle("replay-status-error", !!isError);
  }

  async function fetchSessionList() {
    try {
      const res = await fetch("/replay/sessions");
      if (!res.ok) throw new Error("HTTP " + res.status);
      // CONTRACTS.md §4.1.3: 回應是「陣列」，每個元素是
      // {file, path, bytes, modified_at}，不是 {files:[...]}。value 要用
      // path（/replay/start?file= 吃的是這個），label 用 file 給人看。
      const files = await res.json();
      filesSelectEl.innerHTML =
        `<option value="">— 選擇 session（或下方手動輸入路徑）—</option>` +
        files.map((f) => `<option value="${f.path}">${f.file}</option>`).join("");
      filesSelectEl.disabled = files.length === 0;
    } catch (err) {
      filesSelectEl.innerHTML = `<option value="">（session 清單讀取失敗，請用下方輸入路徑）</option>`;
      filesSelectEl.disabled = true;
      console.warn("[replay] /replay/sessions unavailable:", err.message);
    }
  }

  function selectedFile() {
    return (fileInputEl.value || filesSelectEl.value || "").trim();
  }

  function renderTimeline() {
    const entries = Array.from(trialsByIdx.values()).sort((a, b) => a.idx - b.idx);
    if (!entries.length) {
      timelineEl.innerHTML = `<span class="replay-timeline-empty">尚無 trial（播放後這裡會即時填入）</span>`;
      return;
    }
    timelineEl.innerHTML = entries.map((t) => `
      <button class="replay-trial-marker${t.idx === currentTrialIdx ? " current" : ""}"
              data-quality="${t.quality || ""}" data-trial-idx="${t.idx}" title="quality: ${t.quality || "?"}">
        <span class="replay-trial-idx mono">${t.idx}</span>
        <span class="replay-trial-label">${t.label || ""}</span>
      </button>
    `).join("");
    Array.from(timelineEl.querySelectorAll("[data-trial-idx]")).forEach((el) => {
      el.addEventListener("click", () => seekToTrial(Number(el.dataset.trialIdx)));
    });
  }

  function upsertTrial(idx, label, quality) {
    const prev = trialsByIdx.get(idx) || {};
    trialsByIdx.set(idx, { idx, label: label ?? prev.label, quality: quality ?? prev.quality });
  }

  function setPlaybackControlsEnabled(enabled) {
    playPauseBtn.disabled = !enabled;
    stepBtn.disabled = !enabled;
    seekInputEl.disabled = !enabled;
    speedEls.forEach((el) => (el.disabled = !enabled));
  }

  async function postControl(path) {
    let res;
    try {
      res = await fetch(path, { method: "POST" });
    } catch (err) {
      // Real network failure -- can't reach the bridge at all.
      setStatus("連不上後端（" + err.message + "）", true);
      console.warn("[replay] control endpoint network error:", path, err.message);
      return false;
    }
    if (!res.ok) {
      // Endpoint is live (B19 wired it); a non-ok status is a real backend
      // error (e.g. bad seek target), not "not wired yet" -- show it.
      const body = await res.json().catch(() => ({}));
      setStatus(body.error || `操作失敗：HTTP ${res.status}（${path}）`, true);
      console.warn("[replay] control endpoint error:", path, res.status, body.error);
      return false;
    }
    return true;
  }

  async function onLoadClick() {
    const file = selectedFile();
    if (!file) {
      setStatus("請先選擇或輸入 session 檔案路徑", true);
      return;
    }
    setStatus("載入中…");
    trialsByIdx.clear();
    currentTrialIdx = null;
    renderTimeline();

    let res;
    try {
      res = await fetch(`/replay/start?file=${encodeURIComponent(file)}`, { method: "POST" });
    } catch (err) {
      // Real network failure -- can't reach the bridge at all.
      sessionLoaded = false;
      setPlaybackControlsEnabled(false);
      setStatus("連不上後端（" + err.message + "），請確認 bridge_server.py 是否還在跑", true);
      console.warn("[replay] /replay/start network error:", err.message);
      return;
    }

    if (!res.ok) {
      // CONTRACTS.md §4.1.3: file 不在 sessions 目錄內、或根本不存在，
      // 都回同一個 404 -- 這是給使用者看的真實錯誤，不是「還沒上線」。
      const body = await res.json().catch(() => ({}));
      sessionLoaded = false;
      setPlaybackControlsEnabled(false);
      setStatus(body.error || `載入失敗：HTTP ${res.status}`, true);
      console.warn("[replay] /replay/start error:", res.status, body.error);
      return;
    }

    const data = await res.json().catch(() => null);
    // Best-effort: a backend that already knows every trial up front can
    // hand back the full list so the timeline doesn't start empty and
    // fill in only as playback reaches each one.
    if (data && Array.isArray(data.trials)) {
      data.trials.forEach((t) => upsertTrial(t.idx, t.label, t.quality));
    }
    sessionLoaded = true;
    paused = false;
    playPauseBtn.textContent = "⏸ 暫停";
    setPlaybackControlsEnabled(true);
    setStatus(`回放中：${file}`);
    renderTimeline();
  }

  async function onPlayPauseClick() {
    const action = paused ? "resume" : "pause";
    const ok = await postControl(`/replay/control?action=${action}`);
    if (!ok) return;
    paused = !paused;
    playPauseBtn.textContent = paused ? "▶ 播放" : "⏸ 暫停";
  }

  async function onStepClick() {
    await postControl("/replay/control?action=step");
  }

  async function onSpeedClick(value) {
    const ok = await postControl(`/replay/speed?value=${value}`);
    if (!ok) return;
    speed = value;
    speedEls.forEach((el) => el.classList.toggle("active", Number(el.dataset.speed) === value));
  }

  async function seekToTrial(idx) {
    if (!Number.isInteger(idx)) return;
    await postControl(`/replay/seek?trial=${idx}`);
  }

  function onSeekInputSubmit() {
    const idx = Number(seekInputEl.value);
    if (Number.isInteger(idx)) seekToTrial(idx);
  }

  function updateWatermark() {
    const active = performance.now() - lastReplayEventAt < REPLAY_WATERMARK_LINGER_MS;
    watermarkEl.classList.toggle("visible", active);
  }

  function ensureWatermark() {
    // Lives outside every .mode-section (appended straight to #mainContent,
    // a sibling of all five sections) so it's still on screen after
    // switching to monitor/quiz/etc -- CONTRACTS.md 4.2's whole point
    // ("否則會拿回放資料當即時資料") only holds if the mark survives the
    // mode switch a Demo presenter would actually make.
    if (document.getElementById("replayWatermark")) return document.getElementById("replayWatermark");
    const mainContent = document.getElementById("mainContent");
    const el = document.createElement("div");
    el.id = "replayWatermark";
    el.className = "replay-watermark";
    el.innerHTML = `<span>▶ REPLAY</span>`;
    mainContent.appendChild(el);
    return el;
  }

  // Mirrors shell.js's own setInterval(renderStatusBar, 500): without a
  // tick, the watermark would only ever turn OFF the instant a new evt
  // happens to arrive, i.e. never, once the replay actually stops (no more
  // events => no more onData calls => nothing left to notice the linger
  // window has expired).
  setInterval(updateWatermark, 500);

  return {
    init(root) {
      watermarkEl = ensureWatermark();

      root.innerHTML = `
        <div class="section-label" data-replay-label>回放模式 · HDF5 Session 重播</div>

        <div class="replay-picker">
          <select class="replay-select" data-files-select disabled>
            <option value="">載入中…</option>
          </select>
          <input class="replay-file-input" data-file-input type="text"
                 placeholder="或手動輸入 data/sessions/ 下的檔名">
          <button class="replay-btn replay-btn-primary" data-load-btn>▶ 載入並播放</button>
        </div>
        <div class="replay-status mono" data-status>尚未載入 session</div>

        <div class="replay-controls" data-controls>
          <button class="replay-btn" data-play-pause disabled>⏸ 暫停</button>
          <button class="replay-btn" data-step disabled title="單步：不管排程，立刻送出下一個事件">⏭ 單步</button>
          <div class="replay-speed-group" data-speed-group>
            ${SPEEDS.map((s) => `<button class="replay-btn replay-speed-btn${s === 1 ? " active" : ""}"
                     data-speed="${s}" disabled>${fmtSpeed(s)}</button>`).join("")}
          </div>
          <div class="replay-seek">
            <span class="replay-control-label">跳到 trial</span>
            <input class="replay-seek-input mono" data-seek-input type="number" min="0" step="1" disabled>
            <button class="replay-btn" data-seek-go disabled>跳轉</button>
          </div>
        </div>

        <div class="section-label">Trial 時間軸</div>
        <div class="replay-timeline" data-timeline></div>
      `;

      filesSelectEl = root.querySelector("[data-files-select]");
      fileInputEl = root.querySelector("[data-file-input]");
      loadBtn = root.querySelector("[data-load-btn]");
      statusEl = root.querySelector("[data-status]");
      playPauseBtn = root.querySelector("[data-play-pause]");
      stepBtn = root.querySelector("[data-step]");
      speedEls = Array.from(root.querySelectorAll("[data-speed]"));
      timelineEl = root.querySelector("[data-timeline]");
      seekInputEl = root.querySelector("[data-seek-input]");
      const seekGoBtn = root.querySelector("[data-seek-go]");

      loadBtn.addEventListener("click", onLoadClick);
      playPauseBtn.addEventListener("click", onPlayPauseClick);
      stepBtn.addEventListener("click", onStepClick);
      speedEls.forEach((el) => el.addEventListener("click", () => onSpeedClick(Number(el.dataset.speed))));
      seekGoBtn.addEventListener("click", onSeekInputSubmit);
      seekInputEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter") onSeekInputSubmit();
      });

      setPlaybackControlsEnabled(false);
      renderTimeline();
      fetchSessionList();
    },

    onEnter() {
      // shell.js's leaveMode() only fires onLeave when modeEntered[mode] was
      // set true by enterMode() -- which requires onEnter to exist at all
      // (shell.js line ~52). Without this, onLeave (and its /replay/stop
      // call, CONTRACTS.md §4.1.3) silently never runs on any mode switch.
      fetchSessionList(); // pick up a session recorded since we last looked
    },

    onData(evt) {
      if (evt.replay === true) {
        lastReplayEventAt = performance.now();
        updateWatermark();
      }
      if (evt.type === "trial" && (evt.state === "CAPTURE" || evt.state === "SAVE")) {
        if (evt.state === "CAPTURE") currentTrialIdx = evt.idx;
        upsertTrial(evt.idx, evt.label, evt.quality);
        renderTimeline();
      }
    },

    onLeave() {
      // CONTRACTS.md §4.1.3: 前端「必須」在離開回放模式時呼叫 /replay/stop，
      // 否則後端會持續處於回放狀態、繼續擋掉真實資料 -- 浮水印會照常在
      // REPLAY_WATERMARK_LINGER_MS 後淡出，但送過來的其實還是舊錄音，
      // 使用者會以為自己在看即時資料。
      if (!sessionLoaded) return;
      sessionLoaded = false;
      paused = false;
      setPlaybackControlsEnabled(false);
      fetch("/replay/stop", { method: "POST" }).catch((err) => {
        console.warn("[replay] /replay/stop failed on leave:", err.message);
      });
    },
  };
})());
