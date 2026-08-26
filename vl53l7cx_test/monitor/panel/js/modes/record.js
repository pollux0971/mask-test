// Record mode (C11): session setup form -> forced 30s baseline capture.
//
// ⚠ Backend wiring status (read this before touching the fetch calls below):
// `host/storage/session_registry.py` (B09) and `host/storage/baseline.py`
// (B10) are real, tested, finished pure-logic modules -- but as of this
// story `bridge_server.py`'s do_GET/do_POST have NO /session/* or baseline
// routes at all (grepped, not assumed) and never import either module. The
// request/response shapes below are copied verbatim from CONTRACTS.md
// §4.1.1 (already written by B09) for /session/start|end|current|prefill,
// which do exist as a spec. There is, however, no documented wire format
// yet for *live baseline progress* -- CONTRACTS.md §4.2 only has the
// placeholder `{"type":"session","state":"started|baseline|ended","progress":{..}}`
// with `progress` left as `{..}`. This file defines and uses a concrete
// shape for it (see BASELINE_PROGRESS_SHAPE comment below) as a proposal;
// the completion report flags this for esp-mask-test-ad/B19 to ratify or
// change. Until B19 wires the routes, every fetch() below will 404 --
// this file handles that (shows a clear "等待後端接上" state) rather than
// pretending a request succeeded.
//
// BASELINE_PROGRESS_SHAPE (proposed):
//   {"type":"session","state":"baseline","progress":
//     {"elapsed_s":.., "remaining_s":.., "duration_s":30,
//      "live_sigma_A":[16 floats]|null, "live_sigma_B":[16 floats]|null}}
//   ... and on completion:
//   {"type":"session","state":"baseline","progress":
//     {"done":true, "outcome": <BaselineOutcome.to_dict() from host/storage/baseline.py>}}
//   BaselineOutcome.to_dict() shape (verbatim from baseline.py):
//     {"ok":bool, "reason":str|null, "quality":{"A":ZoneQualityReport,"B":ZoneQualityReport},
//      "baseline_mu_A":[32]|null, "baseline_sigma_A":[32]|null, ... , "valid_zone_ratio":float|null}
//   ZoneQualityReport: {"ok":bool,"unstable_zones":[int],"no_signal_zones":[int],
//                        "suspect_zero_variance_zones":[int],"valid_zone_ratio":float}
//
// Scope per C11.md: settings form + baseline capture screen only. Trial
// prompting/countdown (C12), progress list (C13), redo/discard UI (C14) are
// explicitly out of scope -- this mode stops at "baseline done, ready".
//
// C12 adds: the actual trial prompt/countdown/capture screen, driven by
// `{"type":"trial", "state":"PROMPT|COUNTDOWN|CAPTURE|CONFIRM|SAVE|REST|IDLE",
//  "label":.., "idx":.., "seed":..}` SSE events (CONTRACTS.md §4.2, defined
// by B11/B12). Three trigger mechanisms share this one wire shape:
//   - fixed-duration (B11 sm.start_trial(), no UI trigger in this story)
//   - Hold-to-Record (B12, spacebar -- this is what C12.md actually asks for)
//   - Auto-VAD (B13, esp-mask-test-18, still in progress as of this story)
// This file only *renders* whatever state arrives; it doesn't care which
// mechanism produced it, except for the spacebar handler, which explicitly
// drives hold_start()/hold_stop() (POST /trial/hold/start|stop, B12).
//
// ⚠ Known gap (flagged to esp-mask-test-ad, not resolved as of writing):
// nothing in the wire protocol exposes the *next* word before the user
// presses anything -- TrialStateMachine.hold_start()/start_trial() only
// decide the label at call time. So the prompt card can't show "what to
// say" before a Hold-to-Record press; it shows a generic "press to begin"
// hint until the first `trial` event actually carries a label. See
// completion report for the proposed fix (peek_next_label()).
//
// ⚠ Known deviation from C12.md's state/visual table: it describes
// COUNTDOWN as a literal "3-2-1" count, but B11's actual COUNTDOWN_S is
// 0.5s (not 3s) -- there is no real backend timing that a 3-2-1 count could
// honestly track. Rendered instead as a single proportional pulse over the
// real 0.5s window; flagged in the completion report rather than
// fabricating a 3-second countdown the backend doesn't actually run.
//
// ⚠ No documented HTTP endpoint exists yet for CONFIRM's confirm_keep()/
// discard_pending() (only /trial/hold/start|stop and /trial/abort|redo are
// in CONTRACTS.md's table). This file calls proposed endpoints
// POST /trial/confirm/keep and POST /trial/confirm/discard -- same
// propose-now-ratify-later pattern as C11's baseline progress shape.

import { registerMode } from "../shell.js";

const TRIAL_STATE_LABEL = {
  IDLE: "準備下一個",
  PROMPT: "請準備",
  COUNTDOWN: "即將開始",
  CAPTURE: "錄製中",
  CONFIRM: "尚未存檔 — 請選擇",
  SAVE: "已存檔",
  REST: "休息中",
};

// Web Audio beep tones (OscillatorNode, no audio file -- C12.md "維持零建置").
const BEEP_GET_READY_HZ = 880; // COUNTDOWN entry: "準備"
const BEEP_GO_HZ = 1320; // CAPTURE entry: "開始錄了"
const BEEP_DURATION_S = 0.15;

const REQUIRED_FIELDS = [
  { key: "subject", label: "受試者代號" },
  { key: "mode", label: "模式" },
  { key: "distance_mm", label: "距離 (mm)" },
  { key: "angle_deg", label: "角度 (°)" },
  { key: "ambient", label: "環境描述" },
];
const MODE_SUGGESTIONS = ["normal", "whisper", "silent", "quiz"];

const BASELINE_DURATION_S = 30; // host/storage/baseline.py: BASELINE_DURATION_S
const BASELINE_WAIT_GRACE_MS = 4000; // how long to wait for a real SSE progress event before saying so

const TARGET_CHECK_LABEL = {
  not_configured: "未設定",
  ok: "正常",
  warning: "警告",
};

function isBlank(v) {
  // CONTRACTS.md §4.1.1: distance_mm/angle_deg of 0 are legal; only
  // "not given at all" or an empty string count as missing (matches
  // SessionRegistry's own `in (None, "")` check) -- must NOT use `!value`,
  // that would treat 0 as missing too.
  return v === undefined || v === null || String(v).trim() === "";
}

function zoneListText(zones) {
  return zones.length ? zones.join(", ") : "無";
}

registerMode("record", (() => {
  let root = null;
  let screen = "form"; // "form" | "baseline" | "ready"
  let els = {};
  let prefill = {};
  let lastWearId = null; // derived from prefill.wear_id - 1, null if no history
  let wearMode = "new"; // "new" (🔄 +1) | "same" (➡ unchanged)
  let currentSession = null;
  let baselineStartedAt = null; // performance.now(), for the local pacing countdown
  let baselineWaitTimer = null;
  let baselineOutcome = null; // last BaselineOutcome.to_dict(), or null

  function fmtMissingFieldsMessage(fields) {
    const labels = fields.map((f) => {
      const found = REQUIRED_FIELDS.find((r) => r.key === f);
      return found ? `${found.label}（${f}）` : f;
    });
    return `缺少必填欄位：${labels.join("、")}`;
  }

  function setFormError(text) {
    els.formError.textContent = text || "";
    els.formError.style.display = text ? "block" : "none";
  }

  function computeWearIdValue() {
    if (lastWearId == null) return 1; // no history at all -- nothing to toggle between
    return wearMode === "same" ? lastWearId : lastWearId + 1;
  }

  function updateWearIdUI() {
    els.wearIdValue.textContent = String(computeWearIdValue());
    const hasHistory = lastWearId != null;
    els.wearToggle.style.display = hasHistory ? "flex" : "none";
    els.wearNoHistoryNote.style.display = hasHistory ? "none" : "block";
    els.wearNewBtn.classList.toggle("active", wearMode === "new");
    els.wearSameBtn.classList.toggle("active", wearMode === "same");
  }

  async function loadPrefill() {
    try {
      const res = await fetch("/session/prefill");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      prefill = await res.json();
    } catch (err) {
      // Expected right now (route doesn't exist yet, see file-top note) --
      // fall back to an empty form rather than blocking on it.
      console.warn("[record] /session/prefill unavailable, starting blank:", err);
      prefill = {};
    }

    lastWearId = typeof prefill.wear_id === "number" ? prefill.wear_id - 1 : null;
    wearMode = "new";
    updateWearIdUI();

    if (!isBlank(prefill.subject)) els.subject.value = prefill.subject;
    if (!isBlank(prefill.mode)) els.mode.value = prefill.mode;
    if (typeof prefill.distance_mm === "number") els.distance.value = prefill.distance_mm;
    if (typeof prefill.angle_deg === "number") els.angle.value = prefill.angle_deg;
    if (!isBlank(prefill.ambient)) els.ambient.value = prefill.ambient;
    if (!isBlank(prefill.notes)) els.notes.value = prefill.notes;
  }

  function readFormMetadata() {
    return {
      subject: els.subject.value.trim(),
      wear_id: computeWearIdValue(),
      mode: els.mode.value.trim(),
      distance_mm: els.distance.value.trim() === "" ? "" : Number(els.distance.value),
      angle_deg: els.angle.value.trim() === "" ? "" : Number(els.angle.value),
      ambient: els.ambient.value.trim(),
      notes: els.notes.value.trim(),
    };
  }

  function clientSideMissingFields(metadata) {
    // Mirrors SessionRegistry.REQUIRED_FIELDS exactly (wear_id excluded --
    // it auto-fills, never "missing" from the user's point of view) so the
    // "缺欄位無法送出" acceptance condition doesn't need a round trip to
    // discover something the backend contract already tells us up front.
    return REQUIRED_FIELDS.filter((f) => isBlank(metadata[f.key])).map((f) => f.key);
  }

  function renderTargetCheck(info) {
    const check = info.target_check || "not_configured";
    els.targetCheck.textContent = `配戴幾何檢查：${TARGET_CHECK_LABEL[check] || check}`;
    els.targetCheck.className = "target-check " + check;
    if (check === "not_configured") {
      // Explicit instruction (esp-mask-test-ad, A15/C11 dispatch): never
      // show a fake green light when there's no target geometry to compare
      // against -- an untrue "正常" is worse than no check at all, because
      // it lets someone record a whole batch in the wrong position.
      els.targetCheckNote.textContent = info.note || "目標幾何未設定，待 E01 上機量測後才會有檢查依據";
    } else {
      els.targetCheckNote.textContent = info.note || "";
    }
    els.warningsList.innerHTML = "";
    (info.warnings || []).forEach((w) => {
      const li = document.createElement("li");
      li.textContent = w;
      els.warningsList.appendChild(li);
    });
    els.warningsBox.style.display = (info.warnings || []).length ? "block" : "none";
  }

  async function submitForm() {
    const metadata = readFormMetadata();
    const missing = clientSideMissingFields(metadata);
    if (missing.length) {
      setFormError(fmtMissingFieldsMessage(missing));
      return;
    }
    setFormError("");
    els.submitBtn.disabled = true;
    els.submitBtn.textContent = "送出中…";

    try {
      const res = await fetch("/session/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(metadata),
      });

      if (res.status === 400) {
        const body = await res.json().catch(() => ({}));
        // CONTRACTS.md §4.1.1: {"error": "缺少必填欄位: distance_mm"} --
        // the message already names the field(s); show it directly rather
        // than re-deriving from a `fields` array that may not be there.
        setFormError(body.error || "缺少必填欄位");
        return;
      }
      if (res.status === 409) {
        setFormError("已經有一個進行中的 session，無法重複開始（若這是意外，請先呼叫 /session/end）");
        return;
      }
      if (!res.ok) {
        setFormError(`送出失敗：HTTP ${res.status}`);
        return;
      }

      currentSession = await res.json();
      renderTargetCheck(currentSession);
      enterBaselineScreen();
    } catch (err) {
      // Expected right now (route not wired yet, see file-top note).
      setFormError(
        "無法連上 /session/start（後端路由尚未接上，見完成回報「需要人工驗證的項目」）：" + err.message
      );
    } finally {
      els.submitBtn.disabled = false;
      els.submitBtn.textContent = "開始 Session";
    }
  }

  // --- baseline screen ---------------------------------------------------

  function showScreen(name) {
    screen = name;
    els.formScreen.style.display = name === "form" ? "block" : "none";
    els.baselineScreen.style.display = name === "baseline" ? "block" : "none";
    els.readyScreen.style.display = name === "ready" ? "block" : "none";
  }

  function enterBaselineScreen() {
    baselineStartedAt = performance.now();
    baselineOutcome = null;
    els.baselineWaitingNote.style.display = "none";
    els.baselineUnstableBox.style.display = "none";
    updateBaselineCountdown();
    showScreen("baseline");

    clearTimeout(baselineWaitTimer);
    baselineWaitTimer = setTimeout(() => {
      // No real {"type":"session","state":"baseline",...} SSE event showed
      // up -- almost certainly because bridge_server.py doesn't emit it yet
      // (see file-top note). Say so plainly instead of leaving a countdown
      // frozen with no explanation.
      if (screen === "baseline" && baselineOutcome === null) {
        els.baselineWaitingNote.style.display = "block";
      }
    }, BASELINE_WAIT_GRACE_MS);
  }

  function updateBaselineCountdown() {
    if (screen !== "baseline" || baselineStartedAt == null) return;
    const elapsedS = (performance.now() - baselineStartedAt) / 1000;
    const remaining = Math.max(0, BASELINE_DURATION_S - elapsedS);
    els.baselineCountdown.textContent = remaining.toFixed(0) + " s";
    els.baselineElapsedBar.style.width = `${Math.min(100, (elapsedS / BASELINE_DURATION_S) * 100)}%`;
  }

  function renderLiveStability(progress) {
    if (!progress) return;
    if (typeof progress.elapsed_s === "number") {
      baselineStartedAt = performance.now() - progress.elapsed_s * 1000; // resync to server's clock
    }
    els.baselineWaitingNote.style.display = "none";
    clearTimeout(baselineWaitTimer);

    const renderSigma = (arr, el) => {
      if (!Array.isArray(arr)) {
        el.textContent = "—";
        return;
      }
      const maxSigma = Math.max(...arr);
      el.textContent = arr.map((s) => s.toFixed(1)).join(" ");
      el.classList.toggle("hot", maxSigma > 2.0); // matches baseline.py's SIGMA_INSTABILITY_THRESHOLD_MM
    };
    renderSigma(progress.live_sigma_A, els.liveSigmaA);
    renderSigma(progress.live_sigma_B, els.liveSigmaB);
  }

  function renderBaselineOutcome(outcome) {
    baselineOutcome = outcome;
    if (outcome.ok) {
      els.baselineUnstableBox.style.display = "none";
      showScreen("ready");
      els.readySummary.textContent =
        `baseline 完成（valid_zone_ratio=${(outcome.valid_zone_ratio ?? 0).toFixed(2)}）—— ` +
        "可以開始錄製 trial（C12 尚未接上，這裡先停在這一步）";
      return;
    }

    // "不穩時列出問題 zone 並可重錄" -- surface all three flags per sensor,
    // not just "unstable": no_signal is the one esp-mask-test-ad called out
    // by name (NaN mu/sigma silently corrupts every downstream number), so
    // it's listed first and styled as an error rather than a warning.
    const qa = outcome.quality || {};
    const rows = ["A", "B"].map((sensor) => {
      const q = qa[sensor] || {};
      return `
        <div class="baseline-sensor-report">
          <div class="baseline-sensor-name">感測器 ${sensor}</div>
          <div class="zone-flag zone-flag-error">
            無訊號 zone（NaN，baseline 無效）：${zoneListText(q.no_signal_zones || [])}
          </div>
          <div class="zone-flag zone-flag-warn">
            不穩定 zone（σ > 2mm，可能在動）：${zoneListText(q.unstable_zones || [])}
          </div>
          <div class="zone-flag zone-flag-note">
            疑似零變異 zone（可能沒對準目標）：${zoneListText(q.suspect_zero_variance_zones || [])}
          </div>
        </div>`;
    }).join("");

    els.baselineUnstableReason.textContent = outcome.reason || "baseline 品質不過";
    els.baselineUnstableZones.innerHTML = rows;
    els.baselineUnstableBox.style.display = "block";
  }

  async function retryBaseline() {
    // No documented retry endpoint exists yet either (same gap as the rest
    // of the baseline wire format) -- proposed as POST /session/baseline/retry.
    // Until it's wired, this just re-arms the local countdown/waiting UI so
    // the "可重錄" affordance is visibly real even though the network call
    // will 404; see completion report.
    try {
      await fetch("/session/baseline/retry", { method: "POST" });
    } catch {
      // expected for now
    }
    enterBaselineScreen();
  }

  // --- lifecycle -----------------------------------------------------

  let countdownTimer = null;

  return {
    init(rootEl) {
      root = rootEl;
      root.innerHTML = `
        <div class="record-mode">
          <section class="record-screen" data-screen="form">
            <div class="section-label">Session 設定</div>
            <form data-form>
              <label class="field">
                <span>受試者代號 <span class="required">*</span></span>
                <input type="text" name="subject" data-subject autocomplete="off" />
              </label>

              <label class="field">
                <span>模式 <span class="required">*</span></span>
                <input type="text" name="mode" data-mode list="record-mode-suggestions" autocomplete="off" />
                <datalist id="record-mode-suggestions">
                  ${MODE_SUGGESTIONS.map((m) => `<option value="${m}"></option>`).join("")}
                </datalist>
              </label>

              <div class="field">
                <span>戴法（wear_id）</span>
                <div class="wear-toggle" data-wear-toggle>
                  <button type="button" data-wear-new class="wear-btn">🔄 重新戴上</button>
                  <button type="button" data-wear-same class="wear-btn">➡ 同次繼續</button>
                </div>
                <div class="wear-id-value mono">wear_id = <span data-wear-id-value>1</span></div>
                <div class="wear-no-history-note" data-wear-no-history style="display:none">
                  沒有上次紀錄，第一次使用，wear_id 從 1 開始
                </div>
              </div>

              <label class="field">
                <span>距離 (mm) <span class="required">*</span></span>
                <input type="number" step="any" name="distance_mm" data-distance autocomplete="off" />
              </label>

              <label class="field">
                <span>角度 (°) <span class="required">*</span></span>
                <input type="number" step="any" name="angle_deg" data-angle autocomplete="off" />
              </label>

              <label class="field">
                <span>環境描述 <span class="required">*</span></span>
                <input type="text" name="ambient" data-ambient placeholder="例：安靜房間" autocomplete="off" />
              </label>

              <label class="field">
                <span>備註（選填）</span>
                <input type="text" name="notes" data-notes autocomplete="off" />
              </label>

              <div class="form-error" data-form-error style="display:none"></div>

              <button type="submit" class="submit-btn" data-submit>開始 Session</button>
            </form>

            <div class="target-check" data-target-check></div>
            <div class="target-check-note" data-target-check-note></div>
            <div class="warnings-box" data-warnings-box style="display:none">
              <div class="warnings-title">⚠ 警告</div>
              <ul data-warnings-list></ul>
            </div>
          </section>

          <section class="record-screen" data-screen="baseline" style="display:none">
            <div class="section-label">Baseline 擷取中（保持不動、不要出聲）</div>
            <div class="baseline-countdown mono" data-baseline-countdown>30 s</div>
            <div class="baseline-bar-track">
              <div class="baseline-bar-fill" data-baseline-elapsed-bar></div>
            </div>
            <div class="baseline-waiting-note" data-baseline-waiting style="display:none">
              尚未收到伺服器的即時基線進度事件（後端路由/SSE 尚未接上，見完成回報）——
              倒數是本地估計，不是真實擷取進度。
            </div>
            <div class="live-sigma">
              <div>Sensor A 即時 σ (mm)：<span class="mono" data-live-sigma-a>—</span></div>
              <div>Sensor B 即時 σ (mm)：<span class="mono" data-live-sigma-b>—</span></div>
            </div>

            <div class="baseline-unstable-box" data-baseline-unstable style="display:none">
              <div class="baseline-unstable-title">⚠ Baseline 不穩定，需要重錄</div>
              <div class="baseline-unstable-reason" data-baseline-unstable-reason></div>
              <div data-baseline-unstable-zones></div>
              <button type="button" class="retry-btn" data-retry-baseline>🔁 重新擷取 Baseline</button>
            </div>
          </section>

          <section class="record-screen" data-screen="ready" style="display:none">
            <div class="section-label">Session 就绪</div>
            <div data-ready-summary></div>
          </section>
        </div>
      `;

      els = {
        formScreen: root.querySelector('[data-screen="form"]'),
        baselineScreen: root.querySelector('[data-screen="baseline"]'),
        readyScreen: root.querySelector('[data-screen="ready"]'),
        form: root.querySelector("[data-form]"),
        subject: root.querySelector("[data-subject]"),
        mode: root.querySelector("[data-mode]"),
        distance: root.querySelector("[data-distance]"),
        angle: root.querySelector("[data-angle]"),
        ambient: root.querySelector("[data-ambient]"),
        notes: root.querySelector("[data-notes]"),
        formError: root.querySelector("[data-form-error]"),
        submitBtn: root.querySelector("[data-submit]"),
        wearToggle: root.querySelector("[data-wear-toggle]"),
        wearNewBtn: root.querySelector("[data-wear-new]"),
        wearSameBtn: root.querySelector("[data-wear-same]"),
        wearIdValue: root.querySelector("[data-wear-id-value]"),
        wearNoHistoryNote: root.querySelector("[data-wear-no-history]"),
        targetCheck: root.querySelector("[data-target-check]"),
        targetCheckNote: root.querySelector("[data-target-check-note]"),
        warningsBox: root.querySelector("[data-warnings-box]"),
        warningsList: root.querySelector("[data-warnings-list]"),
        baselineCountdown: root.querySelector("[data-baseline-countdown]"),
        baselineElapsedBar: root.querySelector("[data-baseline-elapsed-bar]"),
        baselineWaitingNote: root.querySelector("[data-baseline-waiting]"),
        liveSigmaA: root.querySelector("[data-live-sigma-a]"),
        liveSigmaB: root.querySelector("[data-live-sigma-b]"),
        baselineUnstableBox: root.querySelector("[data-baseline-unstable]"),
        baselineUnstableReason: root.querySelector("[data-baseline-unstable-reason]"),
        baselineUnstableZones: root.querySelector("[data-baseline-unstable-zones]"),
        retryBtn: root.querySelector("[data-retry-baseline]"),
        readySummary: root.querySelector("[data-ready-summary]"),
      };

      els.form.addEventListener("submit", (e) => {
        e.preventDefault();
        submitForm();
      });
      els.wearNewBtn.addEventListener("click", () => {
        wearMode = "new";
        updateWearIdUI();
      });
      els.wearSameBtn.addEventListener("click", () => {
        wearMode = "same";
        updateWearIdUI();
      });
      els.retryBtn.addEventListener("click", retryBaseline);

      updateWearIdUI();
      showScreen("form");
      loadPrefill();
    },

    onEnter() {
      if (countdownTimer == null) {
        countdownTimer = setInterval(updateBaselineCountdown, 250);
      }
    },

    onLeave() {
      if (countdownTimer != null) {
        clearInterval(countdownTimer);
        countdownTimer = null;
      }
    },

    onData(evt) {
      if (evt.type !== "session") return;
      if (evt.state === "baseline" && evt.progress) {
        if (evt.progress.done && evt.progress.outcome) {
          renderBaselineOutcome(evt.progress.outcome);
        } else {
          renderLiveStability(evt.progress);
        }
      }
    },
  };
})());
