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
// Scope per C11.md: settings form + baseline capture screen only, ending at
// a "baseline done, ready" placeholder. C12 (below) replaces that
// placeholder with the actual trial prompt/countdown/capture screen.
// Progress list (C13), redo/discard UI (C14) remain out of scope here.
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
// CONFIRM's two endpoints are POST /trial/confirm (keep) and
// POST /trial/discard (skip this word) -- flat siblings of start/hold-
// start/hold-stop/abort/redo, matching TrialStateMachine's method names.
// (This file originally proposed nested /trial/confirm/keep|discard for
// C12; corrected per the dispatcher after C14 found the real wiring used
// the flat form -- discard isn't a kind of confirm.)
//
// C13 adds: progress bar + n/N + ETA, a per-label count bar chart, a
// recent-10 list with quality lights, and a click-to-preview overlay
// (ToF Δ heatmap + mic envelope) for one of those 10. Two gaps looked like
// they might need new backend surface; neither did:
//
//   1. Full vocab list (to show labels with a ZERO count, not just ones
//      already seen): originally this fetched the static copy at
//      panel/data/vocab.json (matching quiz.js's own C15-era stopgap,
//      before bridge_server.py had a vocab route at all). ed has since
//      added GET /config/vocab (reads config/vocab.json fresh on every
//      request, no caching/restart needed) and quiz.js switched to it --
//      this file now does too, making panel/data/vocab.json's only
//      remaining reader go away. Falls back to "labels seen so far" if
//      that fetch ever fails, same as before.
//
//   2. Per-trial preview data (Δ heatmap + waveform for one of the last
//      10 trials): no CONTRACTS endpoint exists for reading back a past
//      trial's raw frames (grepped -- confirmed absent). Rather than
//      propose one, this reuses bus.js's existing dataStore, which
//      already buffers 65s of raw tof/mic SSE events by browser receive
//      time for every mode (C03). At CAPTURE-entry this file records
//      performance.now(); at the matching SAVE it slices
//      dataStore.getRecent("tofA"/"tofB"/"mic") to that window and copies
//      the result into recentTrials[] (has to be a copy, not a live
//      reference -- the ring buffer keeps trimming, and 10 trials'
//      worth of session time can easily exceed the 65s retention
//      window). No backend change, no new wire shape.
//
// ⚠ Honesty note on "波形" (waveform): CONTRACTS.md's `mic` SSE event
// only carries `rms`/`peak` per sample window, never raw PCM -- that's
// true of the live stream everywhere in this app, not something this
// file chose to downsample. The preview renders an rms/peak envelope
// bar chart, not a literal waveform; labelled as such rather than
// implying more precision than the wire format has.
//
// "每詞目標次數" (target reps per label) defaults to 8, matching E06's
// own "8 詞 × 8 樣板 + 靜止 × 8 = 72 筆" convention -- editable in the UI
// since nothing in CONTRACTS freezes this number for every session.
//
// C14 adds: 棄用 (reject, keeps the HDF5 data but flags quality=rejected)
// and 重錄 (redo-a-saved-trial: reject + queue the same label for the very
// next real hold_start() press) on each of the recent-10 items, plus an
// R-key shortcut for "redo the last one" while IDLE/REST.
//
// Two things flagged in the C14 completion report, both since resolved by
// the dispatcher:
//   1. The confirm/discard path mismatch above -- CONTRACTS.md corrected to
//      match bridge_server.py's flat /trial/confirm|/trial/discard.
//   2. "棄用" needed an HTTP action for TrialStateMachine's already-existing
//      mark_current_trial_saved_quality() -- proposed as POST /trial/reject
//      {"trial_idx": N} (still degrades to "後端路由尚未接上" until ed adds
//      the route), accepted into CONTRACTS.md §4.2.
// A third item (host/storage/manifest.py excluding rejected by default) was
// outside this file's scope and is handled separately, not here.
//
// "重錄使用相同 label" is done with zero new wire shape beyond what ed
// already wired: /trial/hold/start already accepts an optional
// {"label": ...} body override (bridge_server.py: body.get("label")). This
// file can't queue a label ahead of time -- there's no such wire concept --
// so it remembers the intent locally (pendingRequeueLabel) and supplies it
// on the user's next real hold_start() press, one-shot.

import { registerMode } from "../shell.js";
import { dataStore } from "../bus.js";

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
// B21: normal/whisper/silent used to live in *this* datalist, conflated
// with `mode` (session/panel type) -- exactly the two-different-axes bug
// this story exists to fix (see SPEAKING_MODES below, CONTRACTS.md §2:
// mode and speaking_mode are separate). Left as free text per the story's
// explicit "mode 維持自由文字" -- only the suggestion list changes.
const MODE_SUGGESTIONS = ["quiz"];

// B21: matches quiz.js's SPEAKING_MODES verbatim (same key/label pairs,
// "照 C15 的做法" per the story) -- session_writer.VALID_SPEAKING_MODES is
// the actual frozen value domain this must stay a subset of.
const SPEAKING_MODES = [
  { key: "normal", label: "正常" },
  { key: "whisper", label: "氣音" },
  { key: "silent", label: "無聲" },
];

const BASELINE_DURATION_S = 30; // host/storage/baseline.py: BASELINE_DURATION_S
// ca's disconnect/keyboard audit caught this screen never advancing: it ran
// a local 30s countdown but never actually called POST /session/baseline,
// so there was nothing to advance it (see requestBaselineCapture() below).
// bridge_server.py's capture_session_baseline() reads the *device clock's*
// last BASELINE_DURATION_S of already-buffered frames -- calling it before
// that much has actually accumulated 409s with "let it run longer first",
// which isn't a hard failure, just early. Retry on a short interval rather
// than treating it as broken.
const BASELINE_POST_RETRY_MS = 2000;

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

// --- C13: progress dashboard constants -----------------------------------

const DEFAULT_TARGET_PER_LABEL = 8; // E06 convention: "8 詞 × 8 樣板 + 靜止 × 8"
const RECENT_TRIALS_MAX = 10;
const ETA_SAMPLES_MAX = 8; // rolling window for the "actual average" ETA estimate

// Disconnect-during-CAPTURE audit (ca): matches shell.js's own
// LINK_DATA_FRESHNESS_MS (not exported, so not imported -- see file-top
// note) -- same "how long is too long since real data last arrived"
// threshold, kept in sync by convention rather than by import.
const CAPTURE_STALL_MS = 3000;
const CAPTURE_STALL_POLL_MS = 750;

// ca's audit: recording a 4-hour E05 session against a baseline that's
// since drifted produces trials that *look* fine (no error, no crash) but
// have a wrong z-score reference the whole way down (baseline_mu/sigma,
// energy_mu -> lip VAD thresholds -> feature vectors). Matches monitor.js's
// own BASELINE_STALE_MS exactly (not exported, so not imported -- same
// "propose reuse via matching constant" situation as CAPTURE_STALL_MS
// above) -- this project has broken from a second, drifted definition of
// the same threshold before; reusing the number, not inventing a new one.
const BASELINE_STALE_MS = 10 * 60 * 1000;
const QUALITY_DOT_LABEL = { ok: "●", low: "●", rejected: "●" };

// Real-hardware audit (this session): every silent failure mode found today
// looked identical to normal on screen -- sensor A dropping mid-record,
// mic never actually connected, a stream stopping outright -- because
// nothing showed the raw numbers live. "有沒有資料在流" reuses
// CAPTURE_STALL_MS's own threshold/meaning (same question, not a new one).
// The user's own request was for numbers, not a pass/fail judgment --
// quiet is not broken (a bone-conduction mic reads ~4-6 RMS untouched,
// confirmed on real hardware), only "no data" or "always exactly zero"
// gets flagged.
const LIVE_SENSOR_HZ_WINDOW_MS = 2000;
const LIVE_MIC_BAR_MAX = 500; // generous scale for the bar's width, not a pass/fail threshold

// Small self-contained z-score color, deliberately not imported from
// monitor.js (nothing there is exported for reuse) -- same thresholds and
// meaning as monitor.js's zscoreColor() so a red/blue cell means the same
// thing in both places, just a smaller copy scoped to this file's preview.
const PREVIEW_Z_CLAMP = 3;
const PREVIEW_Z_DEADZONE = 0.5;
const PREVIEW_Z_NEG = [64, 140, 226];
const PREVIEW_Z_POS = [226, 87, 76];
const PREVIEW_Z_NEUTRAL = [43, 50, 45];

function previewLerp(a, b, t) {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return { rgb: `rgb(${r},${g},${bl})`, luminance: (0.299 * r + 0.587 * g + 0.114 * bl) / 255 };
}

function previewZscoreColor(z) {
  const clamped = Math.max(-PREVIEW_Z_CLAMP, Math.min(PREVIEW_Z_CLAMP, z));
  const target = clamped < 0 ? PREVIEW_Z_NEG : PREVIEW_Z_POS;
  const abs = Math.abs(clamped);
  if (abs <= PREVIEW_Z_DEADZONE) {
    return previewLerp(PREVIEW_Z_NEUTRAL, target, (abs / PREVIEW_Z_DEADZONE) * 0.3);
  }
  const t = 0.3 + 0.7 * ((abs - PREVIEW_Z_DEADZONE) / (PREVIEW_Z_CLAMP - PREVIEW_Z_DEADZONE));
  return previewLerp(PREVIEW_Z_NEUTRAL, target, t);
}

function fmtEtaSeconds(s) {
  if (s == null || !isFinite(s)) return "—";
  if (s < 60) return `約 ${Math.ceil(s)} 秒`;
  const m = Math.floor(s / 60);
  const rem = Math.ceil(s % 60);
  return `約 ${m} 分 ${rem} 秒`;
}

registerMode("record", (() => {
  let root = null;
  let screen = "form"; // "form" | "baseline" | "trial"
  let els = {};
  let prefill = {};
  let lastWearId = null; // derived from prefill.wear_id - 1, null if no history
  let wearMode = "new"; // "new" (🔄 +1) | "same" (➡ unchanged)
  let speakingMode = "normal"; // B21: separate axis from `mode` -- one of SPEAKING_MODES
  let currentSession = null;
  let baselineStartedAt = null; // performance.now(), for the local pacing countdown
  let baselineCaptureTimer = null; // fires requestBaselineCapture() once the local countdown ends
  let baselineOutcome = null; // last BaselineOutcome.to_dict(), or null
  let baselineCapturedAtMs = null; // performance.now() when baseline last succeeded, for staleness

  // --- C12: trial screen state ---
  let trialState = "IDLE"; // mirrors the last {"type":"trial",...} event's `state`
  let trialLabel = null;
  let trialNextLabel = null;
  let trialIdx = null;
  let trialSeed = null;
  let holdKeyDown = false; // debounce: OS key-repeat fires keydown many times per real press
  let beepEnabled = true;
  let audioCtx = null; // created lazily on first real user gesture (browser autoplay policy)

  // --- C13: progress dashboard state ---
  let vocabWords = null; // [{id,text,..}] from GET /config/vocab, or null if unavailable
  let vocabReject = null; // {id:"_reject", text:"..."} from the same file
  let targetPerLabel = DEFAULT_TARGET_PER_LABEL;
  let labelCounts = {}; // label id -> saved-trial count (any quality), dynamic-discovery fallback too
  let savedTrialTotal = 0;
  let recentTrials = []; // newest first, capped at RECENT_TRIALS_MAX
  let captureWindowStartMs = null; // performance.now() at this trial's CAPTURE-entry
  let captureStallTimer = null; // setInterval id, running only while trialState === "CAPTURE"
  let captureStalled = false; // true once CAPTURE has gone quiet for CAPTURE_STALL_MS
  let etaSamplesMs = []; // recent inter-SAVE wall-clock gaps, rolling window
  let lastSaveAtMs = null;
  let previewTrialIdx = null; // idx of the recentTrials entry open in the preview overlay

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

  function updateSpeakingModeUI() {
    els.speakingModeBtns.forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.speakingMode === speakingMode);
    });
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
      // B21: speaking_mode is deliberately NOT sent here. CONTRACTS.md §2
      // puts it on /trial_NNN's attrs, not /meta -- it's a trial property,
      // not a session property (the README demo script switches it mid-
      // session: normal -> whisper for one word -> back to normal, which a
      // session-level field couldn't represent without restarting the
      // session). Sent per-trial instead, alongside `label`, at the actual
      // hold_start() call -- see triggerHoldStart().
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
      // This try block also covers renderTargetCheck()/enterBaselineScreen()
      // (run right after a *successful* fetch), so a real JS bug in either
      // of those landed here too and got mislabeled as "backend not
      // connected" -- a fetch() network failure is specifically a
      // TypeError; anything else is a real code bug, not a connectivity
      // issue, and saying so (plus logging the full error/stack) matters --
      // ca's audit lost real time to exactly this ambiguity ("後端路由尚未接上"
      // reads as one of this project's many known-unwired routes, so a real
      // bug hiding behind it goes uninvestigated). Not a full
      // error-handling rewrite, just telling the two cases apart.
      console.error("[record] submitForm failed:", err);
      if (err instanceof TypeError) {
        setFormError("無法連上 /session/start（後端路由尚未接上，見完成回報「需要人工驗證的項目」）：" + err.message);
      } else {
        setFormError("送出後處理失敗（程式錯誤，不是連線問題，已印出完整訊息到 console）：" + err.message);
      }
    } finally {
      els.submitBtn.disabled = false;
      els.submitBtn.textContent = "開始 Session";
    }
  }

  // --- baseline screen ---------------------------------------------------

  const SCREEN_ELS = () => ({ form: els.formScreen, baseline: els.baselineScreen, trial: els.trialScreen });
  const SCREEN_DISPLAY = { form: "block", baseline: "block", trial: "flex" };

  function showScreen(name) {
    const screens = SCREEN_ELS();
    // A screen element missing here means `init()` hasn't finished building
    // `els` yet (e.g. this got called while `root.innerHTML` was still
    // being parsed/queried) -- ca hit this as an intermittent "Cannot read
    // properties of undefined" with no indication of *which* element was
    // missing. Failing loudly and specifically beats a silent skip: a
    // silent skip here would leave every screen at its default `display`
    // (i.e. the previous screen keeps showing, or nothing does), which is
    // strictly more confusing to debug than a clear error naming the gap.
    for (const [key, el] of Object.entries(screens)) {
      if (!el) {
        throw new Error(`[record] showScreen(${name}): els.${key}Screen is not ready yet (DOM not built?)`);
      }
    }
    screen = name;
    for (const [key, el] of Object.entries(screens)) {
      el.style.display = key === name ? SCREEN_DISPLAY[key] : "none";
    }
  }

  function enterBaselineScreen() {
    baselineStartedAt = performance.now();
    baselineOutcome = null;
    els.baselineWaitingNote.style.display = "none";
    els.baselineUnstableBox.style.display = "none";
    updateBaselineCountdown();
    showScreen("baseline");

    // The 30s is a local pacing countdown for the person ("hold still for
    // this long"); the actual capture is a single POST once that's done --
    // bridge_server.py's capture_session_baseline() looks at the device
    // clock's already-buffered last BASELINE_DURATION_S, it doesn't stream
    // progress. Missing this call entirely (this screen used to just show
    // the countdown UI and never call anything) is exactly the "stuck on
    // baseline forever, no error, looks like it's still counting down" bug
    // ca's audit caught.
    clearTimeout(baselineCaptureTimer);
    baselineCaptureTimer = setTimeout(requestBaselineCapture, BASELINE_DURATION_S * 1000);
  }

  async function requestBaselineCapture() {
    if (screen !== "baseline") return; // navigated away before the countdown finished
    try {
      const res = await fetch("/session/baseline", { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (res.status === 200 || res.status === 422) {
        // Both carry a real BaselineOutcome -- renderBaselineOutcome()
        // itself branches on body.ok (422 = quality gate failed, still a
        // real, fully-computed outcome, not an error to retry).
        renderBaselineOutcome(body);
        return;
      }
      if (res.status === 409) {
        // "not enough buffered data yet" -- device clock hasn't caught up
        // to a full BASELINE_DURATION_S, or a brief link hiccup. Not a hard
        // failure: retry shortly instead of stranding the person here with
        // no explanation (see file-top note on this exact bug).
        showBaselineWaiting(body.error || "尚未收集到足夠的裝置資料，重試中…");
        baselineCaptureTimer = setTimeout(requestBaselineCapture, BASELINE_POST_RETRY_MS);
        return;
      }
      showBaselineWaiting(body.error || `送出失敗：HTTP ${res.status}`);
    } catch (err) {
      showBaselineWaiting("無法連上 /session/baseline：" + err.message);
    }
  }

  function showBaselineWaiting(text) {
    els.baselineWaitingNote.textContent = text;
    els.baselineWaitingNote.style.display = "block";
  }

  function updateBaselineCountdown() {
    if (screen !== "baseline" || baselineStartedAt == null) return;
    const elapsedS = (performance.now() - baselineStartedAt) / 1000;
    const remaining = Math.max(0, BASELINE_DURATION_S - elapsedS);
    els.baselineCountdown.textContent = remaining.toFixed(0) + " s";
    els.baselineElapsedBar.style.width = `${Math.min(100, (elapsedS / BASELINE_DURATION_S) * 100)}%`;
  }

  function checkBaselineStaleness() {
    if (baselineCapturedAtMs == null || screen !== "trial") {
      els.baselineStaleNote.style.display = "none";
      return;
    }
    const elapsedMs = performance.now() - baselineCapturedAtMs;
    const stale = elapsedMs > BASELINE_STALE_MS;
    els.baselineStaleNote.style.display = stale ? "flex" : "none";
    if (stale) els.baselineStaleMinutes.textContent = (elapsedMs / 60000).toFixed(1);
  }

  function renderLiveStability(progress) {
    if (!progress) return;
    if (typeof progress.elapsed_s === "number") {
      baselineStartedAt = performance.now() - progress.elapsed_s * 1000; // resync to server's clock
    }
    els.baselineWaitingNote.style.display = "none";

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
      baselineCapturedAtMs = performance.now(); // starts the 10-minute staleness clock (checkBaselineStaleness())
      els.baselineUnstableBox.style.display = "none";
      showScreen("trial");
      renderTrialCard(); // no `trial` event has arrived yet -- render the "press to begin" placeholder
      checkBaselineStaleness();
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

  function retryBaseline() {
    // An unstable outcome means the *previous* 30s window had real motion/
    // noise in it -- re-POSTing against that same buffered window would
    // just fail again for the same reason. There's no separate "retry"
    // endpoint (grepped: only POST /session/baseline exists); re-running
    // the normal 30s countdown gives the person a fresh still window to
    // capture, then requestBaselineCapture() calls the same real endpoint
    // again once it's elapsed.
    enterBaselineScreen();
  }

  // --- C12: trial prompt card / countdown / capture -----------------------

  function ensureAudioCtx() {
    // Must be created/resumed from inside a real user gesture (keydown is
    // one) -- browsers block audio autoplay otherwise. Reused across beeps
    // rather than recreated per-beep to avoid hitting any per-page context
    // limits over a 4-hour E05 session.
    if (!audioCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      audioCtx = new Ctx();
    }
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
  }

  function playBeep(freqHz) {
    if (!beepEnabled) return;
    try {
      const ctx = ensureAudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = freqHz;
      osc.type = "sine";
      // Short linear fade-out instead of a hard stop -- a click at the cut
      // point is exactly the kind of "recording quiz sound" artifact that
      // could contaminate a Hold-to-Record capture if the mic ever hears
      // the panel's own speaker (e.g. testing on a laptop instead of
      // headphones). Cheap to avoid, so avoid it.
      gain.gain.setValueAtTime(0.15, ctx.currentTime);
      gain.gain.linearRampToValueAtTime(0, ctx.currentTime + BEEP_DURATION_S);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + BEEP_DURATION_S);
    } catch (err) {
      console.warn("[record] beep failed (non-fatal):", err);
    }
  }

  // --- C13: progress dashboard --------------------------------------------

  async function loadVocab() {
    try {
      const res = await fetch("/config/vocab");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      vocabWords = Array.isArray(data.words) ? data.words : null;
      vocabReject = data.reject || null;
    } catch (err) {
      // Falls back to dynamic discovery (labelCounts grows as SAVE events
      // arrive) -- see file-top note. Not fatal, just less complete until
      // every label has appeared at least once.
      console.warn("[record] /config/vocab unavailable, falling back to dynamic label discovery:", err);
      vocabWords = null;
      vocabReject = null;
    }
    renderLabelBars();
    renderProgressDash();
  }

  function allLabelEntries() {
    if (vocabWords) {
      const entries = vocabWords.map((w) => ({ id: w.id, text: w.text || w.id }));
      if (vocabReject) entries.push({ id: vocabReject.id, text: vocabReject.text || vocabReject.id });
      return entries;
    }
    // Fallback: whatever labels have actually been saved so far, in first-
    // seen order -- can't show a zero-count label that hasn't appeared yet
    // without the real vocab list, so the bar chart is incomplete until it
    // has (flagged in the completion report, not silently pretended away).
    return Object.keys(labelCounts).map((id) => ({ id, text: id }));
  }

  function computeTargetTotal() {
    const n = allLabelEntries().length;
    return n > 0 && vocabWords ? n * targetPerLabel : null;
  }

  function computeEtaText(targetTotal) {
    if (targetTotal == null) return "剩餘時間：—（字彙表未知，無法估計總數）";
    const remaining = targetTotal - savedTrialTotal;
    if (remaining <= 0) return "已達目標筆數";
    if (etaSamplesMs.length === 0) return "剩餘時間：—（尚無足夠資料估計）";
    const avgMs = etaSamplesMs.reduce((a, b) => a + b, 0) / etaSamplesMs.length;
    return `剩餘時間：${fmtEtaSeconds((avgMs * remaining) / 1000)}（依實際平均耗時估計）`;
  }

  function renderProgressDash() {
    const targetTotal = computeTargetTotal();
    els.progressSummary.textContent = targetTotal != null
      ? `第 ${savedTrialTotal} / ${targetTotal} 筆`
      : `已錄 ${savedTrialTotal} 筆`;
    const pct = targetTotal ? Math.min(100, (savedTrialTotal / targetTotal) * 100) : 0;
    els.progressBarFill.style.width = `${pct}%`;
    els.progressEta.textContent = computeEtaText(targetTotal);
  }

  function renderLabelBars() {
    const entries = allLabelEntries();
    const maxCount = Math.max(targetPerLabel, ...entries.map((e) => labelCounts[e.id] || 0), 1);
    els.labelBars.innerHTML = entries.map((e) => {
      const count = labelCounts[e.id] || 0;
      const pct = Math.min(100, (count / maxCount) * 100);
      const done = count >= targetPerLabel;
      return `
        <div class="label-bar ${done ? "done" : ""}">
          <span class="label-bar-name">${e.text}</span>
          <div class="label-bar-track"><div class="label-bar-fill" style="width:${pct}%"></div></div>
          <span class="label-bar-count mono">${count}/${targetPerLabel}</span>
        </div>`;
    }).join("");
  }

  function renderRecentList() {
    if (!recentTrials.length) {
      els.recentList.innerHTML = `<div class="recent-empty">尚無已存檔的 trial</div>`;
      return;
    }
    els.recentList.innerHTML = recentTrials.map((t) => `
      <div class="recent-item ${t.quality === "rejected" ? "is-rejected" : ""}" data-recent-idx="${t.idx}">
        <span class="quality-dot quality-${t.quality}" title="${t.quality}">${QUALITY_DOT_LABEL[t.quality] || "●"}</span>
        <span class="recent-label" data-recent-open="${t.idx}" tabindex="0" role="button">
          ${t.label}${t.quality === "rejected" ? ' <span class="rejected-tag">已棄用</span>' : ""}
        </span>
        <span class="recent-meta mono">#${t.idx} · ${t.n_frames}幀</span>
        <div class="recent-actions">
          <button type="button" class="recent-action-btn" data-reject-idx="${t.idx}"
                  title="標記這筆為壞樣本：資料保留在 HDF5，但 manifest 預設排除">🗑 棄用</button>
          <button type="button" class="recent-action-btn recent-redo-btn" data-redo-idx="${t.idx}"
                  title="棄用這筆，下一次按住空白鍵會錄同一個詞（${t.label}）">↺ 重錄</button>
        </div>
      </div>`).join("");
  }

  // --- C14: 棄用（reject）與重錄（redo a saved trial） ---------------------
  //
  // ⚠ Proposed, not yet wired (grepped bridge_server.py -- no HTTP action
  // marks an already-*saved* trial's quality; the state machine's own
  // mark_current_trial_saved_quality() exists precisely for this, per its
  // own docstring, but nothing calls it from an HTTP handler yet). This
  // file calls the proposed POST /trial/reject {"trial_idx": N} and
  // degrades to the standard "後端路由尚未接上" message on 404 -- same
  // propose-now-ratify-later pattern as every other gap in this file.
  // Flagged in the completion report, including that adding the route
  // itself is outside this story's authorized path (bridge_server.py).

  async function rejectTrial(idx) {
    const t = recentTrials.find((x) => x.idx === idx);
    if (!t || t.quality === "rejected") return t; // already marked, no-op
    const ok = await postTrialAction("/trial/reject", { trial_idx: idx });
    if (!ok) return null;
    labelCounts[t.label] = Math.max(0, (labelCounts[t.label] || 0) - 1);
    savedTrialTotal = Math.max(0, savedTrialTotal - 1);
    t.quality = "rejected";
    renderProgressDash();
    renderLabelBars();
    renderRecentList();
    return t;
  }

  async function redoSavedTrial(idx) {
    const t = recentTrials.find((x) => x.idx === idx);
    if (!t) return;
    const label = t.label;
    if (t.quality !== "rejected") {
      const rejected = await rejectTrial(idx);
      if (!rejected) return; // reject failed -- don't queue a requeue on top of an unknown state
    }
    // "立刻" 用同一個 label 錄一筆新的：wire protocol 沒有「預先排隊一個
    // 詞」的機制，只有 hold_start() 呼叫當下的 label 覆寫（body.get("label")，
    // bridge_server.py 已經支援）。所以這裡只能記住意圖，等使用者真的按下
    // 空白鍵那一刻才送出 -- 見 triggerHoldStart() 與 pendingRequeueLabel。
    if (trialState !== "IDLE" && trialState !== "REST") {
      setTrialError(`目前狀態是 ${trialState}，無法排入重錄——等這個 trial 結束（回到 IDLE/REST）再試`);
      return;
    }
    pendingRequeueLabel = label;
    renderTrialCard(); // reflect "下一個：<label>（重錄）" immediately, not just on the next real event
  }

  function snapshotPreviewFrames(startMs, endMs) {
    // Copy out of the shared ring buffer now -- it keeps trimming by age,
    // so a live reference would silently go stale/empty by the time this
    // trial scrolls out of the recent-10 list (see file-top note).
    const within = (arr) => arr.filter((e) => e._recvMs >= startMs && e._recvMs <= endMs);
    return {
      tofA: within(dataStore.getRecent("tofA")),
      tofB: within(dataStore.getRecent("tofB")),
      mic: within(dataStore.getRecent("mic")),
    };
  }

  // --- build templates from this session's recordings ---------------------
  //
  // The user's own request: recording templates needs a panel button, not a
  // terminal command someone wearing the device can't reach. Backend is
  // 7c's POST /templates/build + GET /templates/build/state (202+poll, no
  // completion SSE event -- confirmed by reading run_templates_build(), the
  // finally block updates templates_build_state but never calls
  // broadcaster.publish() again after the initial kickoff, so this has to
  // poll rather than wait for an event).
  //
  // 7c found (real BlockingIOError, not a guess) that building against a
  // session whose SessionWriter is still open 409s -- "結束 session 再建
  // 樣板". One button that does both, not two the person has to sequence
  // themselves, since they're wearing the device: end the session, then
  // build against it (no body -- /templates/build defaults to
  // session_runtime["h5_path"], which /session/end leaves set even after
  // clearing the writer).

  let templatesPollTimer = null;

  function showTemplatesError(text) {
    els.templatesStatusBox.style.display = "none";
    els.templatesError.textContent = text;
    els.templatesError.style.display = "block";
  }

  function showTemplatesStatus(title) {
    els.templatesError.style.display = "none";
    els.templatesStatusBox.style.display = "block";
    els.templatesStatusTitle.textContent = title;
    els.templatesSummary.textContent = "";
    els.templatesWarningsList.innerHTML = "";
  }

  function renderTemplatesResult(result) {
    els.templatesError.style.display = "none";
    els.templatesStatusBox.style.display = "block";
    els.templatesStatusTitle.textContent = "✅ 樣板已建立，可以切到「測驗」模式試試看";
    const counts = Object.entries(result.counts || {}).map(([label, n]) => `${label}=${n}`).join("、");
    const skipped = (result.skipped || []).length
      ? `　跳過 ${result.skipped.length} 筆（${result.skipped.map((s) => `${s.trial}: ${s.reason}`).join("；")}）`
      : "";
    els.templatesSummary.textContent = `${result.out_path}　各類別：${counts}${skipped}`;
    // warnings 原樣顯示，不要吞掉或改寫成別的話——7c 的腳本寫這些是給操作
    // 者看的（例如 n=1 時「準確率是 nan 不是 0%」這種特定訊息）。
    els.templatesWarningsList.innerHTML = (result.warnings || [])
      .map((w) => `<li>⚠ ${w}</li>`).join("");
  }

  async function pollTemplatesBuildState() {
    clearTimeout(templatesPollTimer);
    try {
      const res = await fetch("/templates/build/state");
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        showTemplatesError(body.error || `查詢建樣板狀態失敗：HTTP ${res.status}`);
        return;
      }
      if (body.running) {
        showTemplatesStatus(`建樣板中…（已經 ${body.elapsed_s ?? "?"} 秒）`);
        templatesPollTimer = setTimeout(pollTemplatesBuildState, 1000);
        return;
      }
      if (body.last_error) {
        showTemplatesError(body.last_error);
        return;
      }
      if (body.last_result) {
        renderTemplatesResult(body.last_result);
        return;
      }
      showTemplatesError("建樣板結束了，但既沒有結果也沒有錯誤訊息（這不應該發生，回報給調度員）");
    } catch (err) {
      console.error("[record] poll /templates/build/state failed:", err);
      if (err instanceof TypeError) {
        showTemplatesError("無法連上 /templates/build/state：" + err.message);
      } else {
        showTemplatesError("輪詢建樣板狀態時發生程式錯誤（不是連線問題）：" + err.message);
      }
    }
  }

  async function buildTemplatesFromThisSession() {
    els.buildTemplatesBtn.disabled = true;
    showTemplatesStatus("結束 session 中…");
    try {
      const endRes = await fetch("/session/end", { method: "POST" });
      // 409 here just means "already ended" (e.g. a second click, or the
      // person already ended it some other way) -- not a reason to stop,
      // /templates/build below will use whatever session is already on
      // disk. Any other non-2xx is a real problem worth stopping for.
      if (!endRes.ok && endRes.status !== 409) {
        const body = await endRes.json().catch(() => ({}));
        showTemplatesError(body.error || `結束 session 失敗：HTTP ${endRes.status}`);
        return;
      }

      showTemplatesStatus("啟動建樣板中…");
      const buildRes = await fetch("/templates/build", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const buildBody = await buildRes.json().catch(() => ({}));
      if (buildRes.status !== 202) {
        showTemplatesError(buildBody.error || `啟動建樣板失敗：HTTP ${buildRes.status}`);
        return;
      }
      pollTemplatesBuildState();
    } catch (err) {
      console.error("[record] buildTemplatesFromThisSession failed:", err);
      // Same distinction as submitForm()'s catch (C11/ca's audit): a fetch()
      // network failure is a TypeError, anything else is a real bug in this
      // function and saying so (plus logging it) matters more than folding
      // every failure into one "後端沒接上"-shaped message.
      if (err instanceof TypeError) {
        showTemplatesError("無法連上後端：" + err.message);
      } else {
        showTemplatesError("建樣板流程出錯（程式錯誤，不是連線問題，已印出完整訊息到 console）：" + err.message);
      }
    } finally {
      els.buildTemplatesBtn.disabled = false;
    }
  }

  // --- live sensor readout (real-hardware audit) --------------------------

  function renderLiveSensorReadout() {
    const tofA = dataStore.getRecent("tofA", CAPTURE_STALL_MS);
    const tofB = dataStore.getRecent("tofB", CAPTURE_STALL_MS);
    const mic = dataStore.getRecent("mic", CAPTURE_STALL_MS);
    const tofAHz = dataStore.getRecent("tofA", LIVE_SENSOR_HZ_WINDOW_MS).length / (LIVE_SENSOR_HZ_WINDOW_MS / 1000);
    const tofBHz = dataStore.getRecent("tofB", LIVE_SENSOR_HZ_WINDOW_MS).length / (LIVE_SENSOR_HZ_WINDOW_MS / 1000);
    const micHz = dataStore.getRecent("mic", LIVE_SENSOR_HZ_WINDOW_MS).length / (LIVE_SENSOR_HZ_WINDOW_MS / 1000);

    const renderTof = (frames, hz, dotEl, textEl) => {
      const fresh = frames.length > 0;
      dotEl.className = "quality-dot " + (fresh ? "quality-ok" : "quality-rejected");
      if (!fresh) {
        textEl.textContent = "無資料（斷線或串流已停止）";
        return;
      }
      const latest = frames[frames.length - 1];
      const validText = Array.isArray(latest.valid)
        ? `　${latest.valid.filter((v) => v !== false).length}/${latest.valid.length} 有效`
        : "";
      textEl.textContent = `${hz.toFixed(0)} Hz${validText}`;
    };
    renderTof(tofA, tofAHz, els.liveTofADot, els.liveTofAText);
    renderTof(tofB, tofBHz, els.liveTofBDot, els.liveTofBText);

    // 麥克風：不用門檻判斷「好不好」（接觸式骨傳導麥克風安靜時 RMS 4-6
    // 是真板子上量到的正常值，不是壞掉）——只標兩種真正算損壞的狀況：
    // 完全沒有資料、或有資料但 RMS 恆為 0（後者是「沒接上」，不是「很安
    // 靜」，兩者對使用者的意義完全不同，不能混在一起變成同一個「安靜」）。
    const micFresh = mic.length > 0;
    const micAllZero = micFresh && mic.every((m) => (m.rms || 0) === 0);
    if (!micFresh) {
      els.liveMicDot.className = "quality-dot quality-rejected";
      els.liveMicText.textContent = "無資料（麥克風串流已停止）";
      els.liveMicBar.style.width = "0%";
    } else if (micAllZero) {
      els.liveMicDot.className = "quality-dot quality-rejected";
      els.liveMicText.textContent = "RMS 恆為 0 —— 麥克風可能沒接上（不是「很安靜」）";
      els.liveMicBar.style.width = "0%";
    } else {
      const latest = mic[mic.length - 1];
      els.liveMicDot.className = "quality-dot quality-ok";
      els.liveMicText.textContent =
        `RMS ${(latest.rms ?? 0).toFixed(1)}　peak ${latest.peak ?? "—"}　${micHz.toFixed(0)} Hz`;
      els.liveMicBar.style.width = `${Math.min(100, ((latest.rms || 0) / LIVE_MIC_BAR_MAX) * 100)}%`;
    }
  }

  // --- disconnect-during-CAPTURE watch (ca's audit) -----------------------
  //
  // "斷線這件事前端已經知道了，只是 record 模式沒有用這個資訊" -- shell.js
  // (C04) already computes a correct up/down signal (explicit {type:"link"}
  // events plus a data-freshness fallback for when the bridge process itself
  // dies before it can even send a down event), but doesn't export it
  // (grepped: only registerMode/forEachRegisteredMode/activateMode/
  // notifySseConnection/notifyGlobalStatus are exported, nothing exposes the
  // computed link state) -- and shell.js isn't this file's to add an export
  // to right now. Rather than build a second, separate disconnect detector,
  // this reuses bus.js's dataStore -- already the single source of truth for
  // "has real tof data actually arrived recently" (it's a public ring
  // buffer, not new logic) -- and asks the exact question CAPTURE cares
  // about: is data for *this* trial still flowing, regardless of which
  // layer stopped it. Flagged in the completion report as a case where a
  // small shell.js export would let this reuse the richer signal directly.

  function isCaptureDataFresh() {
    return dataStore.getRecent("tofA", CAPTURE_STALL_MS).length > 0
      || dataStore.getRecent("tofB", CAPTURE_STALL_MS).length > 0;
  }

  function checkCaptureFreshness() {
    if (trialState !== "CAPTURE") return; // stale timer tick racing a state change; ignore
    const stalled = !isCaptureDataFresh();
    if (stalled !== captureStalled) {
      captureStalled = stalled;
      renderTrialCard();
    }
  }

  function startCaptureStallWatch() {
    captureStalled = false;
    clearInterval(captureStallTimer);
    captureStallTimer = setInterval(checkCaptureFreshness, CAPTURE_STALL_POLL_MS);
  }

  function stopCaptureStallWatch() {
    clearInterval(captureStallTimer);
    captureStallTimer = null;
    captureStalled = false;
  }

  function finalizeSavedTrial(evt) {
    const nowMs = performance.now();
    savedTrialTotal += 1;
    labelCounts[evt.label] = (labelCounts[evt.label] || 0) + 1;

    if (lastSaveAtMs != null) {
      etaSamplesMs.push(nowMs - lastSaveAtMs);
      if (etaSamplesMs.length > ETA_SAMPLES_MAX) etaSamplesMs.shift();
    }
    lastSaveAtMs = nowMs;

    const preview = captureWindowStartMs != null
      ? snapshotPreviewFrames(captureWindowStartMs, nowMs)
      : { tofA: [], tofB: [], mic: [] };
    captureWindowStartMs = null;

    recentTrials.unshift({
      idx: evt.idx, label: evt.label, quality: evt.quality, n_frames: evt.n_frames,
      valid_zone_ratio: evt.valid_zone_ratio, drop_count: evt.drop_count, seed: evt.seed,
      preview,
    });
    if (recentTrials.length > RECENT_TRIALS_MAX) recentTrials.length = RECENT_TRIALS_MAX;

    renderProgressDash();
    renderLabelBars();
    renderRecentList();
  }

  function buildPreviewGrid(container, dim) {
    const side = Math.round(Math.sqrt(dim));
    container.innerHTML = "";
    container.style.gridTemplateColumns = `repeat(${side}, 1fr)`;
    const cells = [];
    for (let i = 0; i < dim; i++) {
      const c = document.createElement("div");
      c.className = "preview-cell";
      container.appendChild(c);
      cells.push(c);
    }
    return cells;
  }

  function renderPreviewHeatmap(container, label, frames, mu, sigma) {
    const wrap = document.createElement("div");
    wrap.className = "preview-heatmap";
    const title = document.createElement("div");
    title.className = "preview-heatmap-title";
    const frame = frames.length ? frames[frames.length - 1] : null; // last frame in the capture window
    if (!frame) {
      title.textContent = `${label}：擷取視窗內沒有收到資料（可能是 65s 保留窗已過期，見完成回報）`;
      wrap.appendChild(title);
      container.appendChild(wrap);
      return;
    }
    const dim = frame.dist.length;
    const hasBaseline = Array.isArray(mu) && mu.length === dim;
    title.textContent = hasBaseline
      ? `${label}：Δ（相對 baseline，最後一幀）`
      : `${label}：原始距離（baseline zone 數不符，${dim} vs ${mu ? mu.length : "—"}）`;
    wrap.appendChild(title);
    const grid = document.createElement("div");
    grid.className = "preview-grid";
    wrap.appendChild(grid);
    const cells = buildPreviewGrid(grid, dim);
    for (let z = 0; z < dim; z++) {
      const v = frame.dist[z];
      const invalid = v == null || v < 0 || (frame.valid && frame.valid[z] === false);
      const c = cells[z];
      if (invalid) {
        c.classList.add("invalid");
        c.textContent = "·";
        continue;
      }
      if (hasBaseline && sigma && sigma[z] > 0) {
        const z_score = (v - mu[z]) / sigma[z];
        const { rgb, luminance } = previewZscoreColor(z_score);
        c.style.background = rgb;
        c.style.color = luminance > 0.55 ? "#10140f" : "#f3f6f2";
        c.textContent = (v - mu[z]).toFixed(0);
      } else {
        c.textContent = v.toFixed(0);
      }
    }
    container.appendChild(wrap);
  }

  function renderPreviewWaveform(container, micFrames) {
    container.innerHTML = "";
    const title = document.createElement("div");
    title.className = "preview-waveform-title";
    title.textContent = "麥克風 RMS/峰值包絡（不是原始波形，見完成回報）";
    container.appendChild(title);
    if (!micFrames.length) {
      const empty = document.createElement("div");
      empty.className = "preview-empty";
      empty.textContent = "擷取視窗內沒有收到 mic 資料";
      container.appendChild(empty);
      return;
    }
    const bars = document.createElement("div");
    bars.className = "preview-waveform-bars";
    const maxRms = Math.max(...micFrames.map((f) => f.rms || 0), 1);
    micFrames.forEach((f) => {
      const bar = document.createElement("div");
      bar.className = "preview-waveform-bar";
      bar.style.height = `${Math.max(2, ((f.rms || 0) / maxRms) * 100)}%`;
      bars.appendChild(bar);
    });
    container.appendChild(bars);
  }

  function renderPreview(trial) {
    els.previewTitle.textContent = `#${trial.idx} ${trial.label} — ${trial.quality} · ${trial.n_frames} 幀`;
    els.previewHeatmaps.innerHTML = "";
    // bridge_server.py's real POST /session/baseline response (_baseline_payload())
    // uses mu_A/sigma_A/mu_B/sigma_B, not baseline_mu_A/etc -- this file's
    // earlier C13 work assumed the latter (BaselineOutcome.to_dict()'s own
    // naming) since baselineOutcome was only ever populated by hand-built
    // test events before this story's fix actually wired the real endpoint.
    const mu = baselineOutcome ? baselineOutcome.mu_A : null;
    const sigma = baselineOutcome ? baselineOutcome.sigma_A : null;
    const muB = baselineOutcome ? baselineOutcome.mu_B : null;
    const sigmaB = baselineOutcome ? baselineOutcome.sigma_B : null;
    renderPreviewHeatmap(els.previewHeatmaps, "Sensor A", trial.preview.tofA, mu, sigma);
    renderPreviewHeatmap(els.previewHeatmaps, "Sensor B", trial.preview.tofB, muB, sigmaB);
    renderPreviewWaveform(els.previewWaveform, trial.preview.mic);
  }

  function openPreview(idx) {
    const trial = recentTrials.find((t) => t.idx === idx);
    if (!trial) return;
    previewTrialIdx = idx;
    renderPreview(trial);
    els.previewOverlay.style.display = "flex";
  }

  function closePreview() {
    previewTrialIdx = null;
    els.previewOverlay.style.display = "none";
  }

  function setTrialError(text) {
    els.trialError.textContent = text || "";
    els.trialError.style.display = text ? "block" : "none";
  }

  function renderTrialCard() {
    const card = els.trialCard;
    // C-track disconnect audit (ca): a real capture that stalls (device
    // unplugged, bridge killed) leaves this card frozen on the CSS pulse
    // forever -- the animation is pure CSS, it doesn't need data to keep
    // running, so it kept looking like a normal in-progress recording with
    // no timeout and no message. Reusing .state-confirm's existing
    // dashed-warn look here (not inventing a new class -- css/modes/record.css
    // isn't this file's to edit right now, see completion report) since it
    // already means "something needs your attention" on this same card.
    card.className = "trial-card " + (captureStalled ? "state-confirm" : "state-" + trialState.toLowerCase());

    if (trialState === "CONFIRM") {
      els.trialWord.textContent = trialLabel || "—";
    } else if (trialState === "IDLE" || trialState === "REST") {
      // "顯示下一個詞的預告" (C12.md's REST row) -- IDLE gets the same
      // treatment so Hold-to-Record's very first press also has something
      // to read beforehand, not just after REST once already.
      //
      // C14: a pending "重錄" overrides this -- peek_next_label() reflects
      // the *normal* cyclic order, which is no longer what the next real
      // hold_start() call is actually going to send (see
      // pendingRequeueLabel/triggerHoldStart()). Showing the stale cyclic
      // word here would make the person say the wrong thing next.
      els.trialWord.textContent = pendingRequeueLabel || trialNextLabel || "—";
    } else {
      els.trialWord.textContent = trialLabel || "—";
    }

    els.trialRequeueNote.style.display = pendingRequeueLabel && (trialState === "IDLE" || trialState === "REST")
      ? "block" : "none";

    els.trialStateLabel.textContent = captureStalled
      ? "⚠ 連線中斷 — 這筆沒錄到"
      : (TRIAL_STATE_LABEL[trialState] || trialState);
    els.trialIdx.textContent = trialIdx != null ? `#${trialIdx}` : "—";
    els.trialSeed.textContent = trialSeed != null ? `seed=${trialSeed}` : "";

    if (captureStalled) {
      setTrialError("這一筆沒錄到（收不到感測器資料），請放開重念一次");
    } else if (trialState === "CAPTURE") {
      setTrialError(""); // clears a stale "沒錄到" message once data resumes flowing
    }

    const showPrefix = trialState === "REST" && trialLabel;
    els.trialPrevLabel.style.display = showPrefix ? "block" : "none";
    if (showPrefix) els.trialPrevLabel.textContent = `剛才：${trialLabel}`;

    els.trialConfirmBox.style.display = trialState === "CONFIRM" ? "flex" : "none";
    if (trialState === "CONFIRM") {
      els.trialConfirmReason.textContent =
        (lastTrialEvent && lastTrialEvent.warning === "too_short")
          ? "按住時間太短（< 0.3s），可能是誤觸"
          : (lastTrialEvent && lastTrialEvent.warning === "too_long")
          ? "按住時間太長（> 4s），可能忘了放開"
          : "時長超出正常範圍";
    }
  }

  let lastTrialEvent = null;

  function onTrialEvent(evt) {
    lastTrialEvent = evt;
    // C13: mark the start of the capture window (browser receive time) the
    // moment CAPTURE is entered, before trialState gets overwritten below --
    // this is what lets finalizeSavedTrial() slice the right span out of
    // bus.js's dataStore once SAVE arrives.
    const wasCapturing = trialState === "CAPTURE";
    if (evt.state === "CAPTURE" && trialState !== "CAPTURE") {
      captureWindowStartMs = performance.now();
    }
    trialState = evt.state;
    trialLabel = evt.label != null ? evt.label : trialLabel;
    trialIdx = evt.idx != null ? evt.idx : trialIdx;
    trialSeed = evt.seed != null ? evt.seed : trialSeed;
    if (evt.next_label !== undefined) trialNextLabel = evt.next_label;

    if (trialState === "COUNTDOWN") playBeep(BEEP_GET_READY_HZ);
    if (trialState === "CAPTURE") playBeep(BEEP_GO_HZ);
    if (trialState === "SAVE") finalizeSavedTrial(evt);

    // A real trial event arriving at all is itself proof the link is up
    // (SAVE/CONFIRM/etc. only exist if the backend is alive and talking to
    // us) -- so any state change away from CAPTURE ends the stall watch,
    // even if it never actually detected a stall.
    if (!wasCapturing && trialState === "CAPTURE") startCaptureStallWatch();
    else if (wasCapturing && trialState !== "CAPTURE") stopCaptureStallWatch();

    renderTrialCard();
  }

  async function postTrialAction(path, jsonBody) {
    try {
      const opts = { method: "POST" };
      if (jsonBody !== undefined) {
        opts.headers = { "Content-Type": "application/json" };
        opts.body = JSON.stringify(jsonBody);
      }
      const res = await fetch(path, opts);
      if (!res.ok && res.status !== 404) {
        const body = await res.json().catch(() => ({}));
        setTrialError(body.error || `HTTP ${res.status}`);
        return false;
      }
      if (res.status === 404) {
        // Expected right now -- /trial/* wiring (B19/ed) isn't live yet
        // for every one of these endpoints. Say so instead of pretending
        // the button did nothing for no reason.
        setTrialError(`後端路由 ${path} 尚未接上（見完成回報）`);
        return false;
      }
      setTrialError("");
      return true;
    } catch (err) {
      setTrialError(`無法連上 ${path}：${err.message}`);
      return false;
    }
  }

  // C14: set by "重錄"/R-key -- the *next* real hold-to-record press should
  // use this label instead of whatever the state machine's own cyclic order
  // would give it. One-shot (cleared the moment it's actually used) --
  // there is no wire-protocol way to "queue" a label ahead of time, only to
  // override the label a hold_start() call is making *right now* (see
  // bridge_server.py's body.get("label") passthrough), so this file has to
  // hold the intent locally until the user's next real keypress supplies it.
  let pendingRequeueLabel = null;

  function triggerHoldStart() {
    if (holdKeyDown) return; // OS key-repeat guard
    holdKeyDown = true;
    ensureAudioCtx(); // arm audio on the same user gesture, before any beep is due
    const label = pendingRequeueLabel;
    pendingRequeueLabel = null;
    // B21: speaking_mode travels with the trial, not the session (see
    // readFormMetadata()'s note) -- sent on every hold_start(), same as the
    // one-shot label override above. ⚠ Backend wiring status: ed is adding
    // the body.get("speaking_mode") passthrough on bridge_server.py's side
    // (this file's authorized path doesn't include that file); until that
    // lands, the value is sent but silently ignored server-side -- normal
    // for this project's propose-now-wire-later pattern, not a bug here.
    const body = { speaking_mode: speakingMode };
    if (label != null) body.label = label;
    // Proposed (not yet consumed backend-side, see completion report): how
    // stale the current baseline was at the exact moment this trial's
    // capture started. A screen warning only protects whoever's watching
    // it right now -- E05 runs 4 hours unattended-ish, and a number
    // written to the trial itself is the only way D14 can flag/exclude
    // trials recorded against a drifted baseline after the fact.
    if (baselineCapturedAtMs != null) {
      body.baseline_age_s = (performance.now() - baselineCapturedAtMs) / 1000;
    }
    postTrialAction("/trial/hold/start", body);
  }

  function triggerHoldStop() {
    if (!holdKeyDown) return;
    holdKeyDown = false;
    postTrialAction("/trial/hold/stop");
  }

  function triggerAbort() {
    postTrialAction("/trial/abort");
  }

  function triggerRedo() {
    postTrialAction("/trial/redo");
  }

  function triggerConfirmKeep() {
    // CONTRACTS.md §4.2 corrected: bridge_server.py's real _dispatch_trial()
    // uses flat sibling actions matching the state machine's method names
    // (confirm/discard, alongside start/hold-start/hold-stop/abort/redo) --
    // not the nested /trial/confirm/keep|discard this file originally
    // proposed for C12 (dispatcher's call: discard isn't a kind of confirm,
    // nesting it under confirm/ didn't make sense).
    postTrialAction("/trial/confirm");
  }

  function triggerConfirmDiscard() {
    postTrialAction("/trial/discard");
  }

  function isRecordTrialScreenActive() {
    const section = document.getElementById("mode-record");
    return !!section && section.classList.contains("active") && screen === "trial";
  }

  function onTrialKeydown(e) {
    if (!isRecordTrialScreenActive()) return;
    if (isTypingTarget(e.target)) return; // no text inputs on this screen, but stay consistent with C02's rule
    if (e.altKey || e.ctrlKey || e.metaKey) return;

    if (previewTrialIdx != null) {
      // C13 preview overlay is open -- ESC closes *that*, not the current
      // trial. Every other key is ignored so a stray Space/Enter behind the
      // overlay can't start a hold or confirm a decision the user can't see.
      if (e.key === "Escape") {
        e.preventDefault();
        closePreview();
      }
      return;
    }

    if (e.code === "Space" || e.key === " ") {
      if (trialState === "CONFIRM") return; // space shouldn't start a new hold while a decision is pending
      e.preventDefault(); // C12.md: "否則會捲動頁面"
      triggerHoldStart();
    } else if (e.key === "Enter" && trialState === "CONFIRM") {
      e.preventDefault();
      triggerConfirmKeep();
    } else if (e.key === "Escape") {
      e.preventDefault();
      // During CONFIRM, ESC's "throw this away, don't save" meaning lines
      // up exactly with discard_pending() -- reusing it here is consistent
      // with what ESC means everywhere else on this screen, not a new rule.
      if (trialState === "CONFIRM") triggerConfirmDiscard();
      else triggerAbort();
    } else if (e.key.toLowerCase() === "r" && (trialState === "IDLE" || trialState === "REST")) {
      // C14: "R 鍵重錄上一筆" -- targets the most recently *saved* trial
      // (recentTrials[0]), not the in-flight one (there isn't one; IDLE/REST
      // is exactly the state where the previous trial already finished).
      // Distinct from the PROMPT/COUNTDOWN/CAPTURE branch below: the old
      // in-flight redo() would 409 in these two states anyway (B12's
      // abort/redo explicitly excludes REST/IDLE, C12's own fix), so this
      // doesn't take away any working behaviour -- it replaces a dead key
      // combo with a real one.
      if (recentTrials.length) redoSavedTrial(recentTrials[0].idx);
      else setTrialError("還沒有已存檔的 trial 可以重錄");
    } else if (e.key.toLowerCase() === "r" && trialState !== "CONFIRM") {
      // Not in C12.md's literal "包含" list (only ESC/abort is) -- added
      // because esp-mask-test-ad's dispatch explicitly called out that
      // abort vs redo must both be reachable and distinguishable. No key
      // was specified for redo anywhere, so this is this story's own
      // choice; flagged in the completion report for confirmation.
      // C14: only reaches here for PROMPT/COUNTDOWN/CAPTURE now (IDLE/REST
      // are handled above) -- retry the current, not-yet-saved trial.
      triggerRedo();
    }
  }

  function onTrialKeyup(e) {
    if (!isRecordTrialScreenActive()) return;
    if (e.code === "Space" || e.key === " ") {
      e.preventDefault();
      triggerHoldStop();
    }
  }

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
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
                <span>說話模式（speaking_mode）</span>
                <div class="speaking-mode-toggle" data-speaking-mode-toggle>
                  ${SPEAKING_MODES.map((m) => `<button type="button" class="speaking-mode-btn" data-speaking-mode-btn data-speaking-mode="${m.key}">${m.label}</button>`).join("")}
                </div>
              </div>

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
            <div class="baseline-waiting-note" data-baseline-waiting style="display:none"></div>
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

          <section class="record-screen trial-screen" data-screen="trial" style="display:none">
            <div class="trial-main">
              <div class="trial-topbar">
                <label class="beep-toggle">
                  <input type="checkbox" data-beep-toggle checked /> 🔊 嗶聲
                </label>
                <div class="trial-meta mono">
                  <span data-trial-idx>—</span>
                  <span data-trial-seed></span>
                </div>
              </div>

              <div class="baseline-stale-note mono" data-baseline-stale-note
                   style="display:none; align-items:center; gap:10px; color:var(--accent);">
                <span>⚠ baseline 已經 <span data-baseline-stale-minutes></span> 分鐘前擷取，z-score 基準可能已經漂掉，建議重新擷取</span>
                <button type="button" class="retry-btn" data-baseline-restale-btn
                        style="margin-top:0; padding:4px 10px; font-size:12px;">重新擷取 baseline</button>
              </div>

              <div class="trial-card state-idle" data-trial-card>
                <div class="trial-prev-label" data-trial-prev-label style="display:none"></div>
                <div class="trial-word" data-trial-word>—</div>
                <div class="trial-requeue-note" data-trial-requeue-note style="display:none">🔁 重錄模式：下一次按住空白鍵會錄這個詞</div>
                <div class="trial-state-label" data-trial-state-label>準備中</div>
              </div>

              <div class="trial-confirm-box" data-trial-confirm style="display:none">
                <div class="trial-confirm-title">⚠ 尚未存檔 —— 請選擇</div>
                <div class="trial-confirm-reason" data-trial-confirm-reason></div>
                <div class="trial-confirm-buttons">
                  <button type="button" class="confirm-keep-btn" data-confirm-keep>✅ 保留（Enter）</button>
                  <button type="button" class="confirm-discard-btn" data-confirm-discard>⏭ 跳過此詞（Esc）</button>
                </div>
              </div>

              <div class="trial-error" data-trial-error style="display:none"></div>

              <div class="trial-hint mono">
                按住空白鍵開始錄音、放開結束　｜　Esc 放棄（跳過此詞）　｜　R 重來（錄音中：保留此詞／錄完後：重錄剛存的上一筆）
              </div>
            </div>

            <aside class="progress-dash" data-progress-dash>
              <div class="section-label">即時感測器</div>
              <div class="mono live-sensor-panel" data-live-sensor-panel style="font-size:12px; display:flex; flex-direction:column; gap:4px; margin-bottom:14px;">
                <div><span class="quality-dot quality-ok" data-live-tofa-dot>●</span> ToF A：<span data-live-tofa-text>—</span></div>
                <div><span class="quality-dot quality-ok" data-live-tofb-dot>●</span> ToF B：<span data-live-tofb-text>—</span></div>
                <div><span class="quality-dot quality-ok" data-live-mic-dot>●</span> 麥克風：<span data-live-mic-text>—</span></div>
                <div class="label-bar-track"><div class="label-bar-fill" data-live-mic-bar style="width:0%"></div></div>
              </div>

              <div class="section-label">進度（C13）</div>
              <label class="target-per-label-field mono">
                每詞目標 <input type="number" min="1" step="1" data-target-per-label value="${DEFAULT_TARGET_PER_LABEL}" /> 次
              </label>
              <div class="progress-summary mono" data-progress-summary>已錄 0 筆</div>
              <div class="progress-bar-track">
                <div class="progress-bar-fill" data-progress-bar-fill style="width:0%"></div>
              </div>
              <div class="progress-eta" data-progress-eta></div>

              <div class="section-label dash-subsection">各詞數量</div>
              <div class="label-bars" data-label-bars></div>

              <div class="section-label dash-subsection">最近 10 筆（點擊可預覽）</div>
              <div class="recent-list" data-recent-list></div>

              <div class="section-label dash-subsection">建立樣板</div>
              <button type="button" class="retry-btn" data-build-templates-btn style="width:100%;">
                🏗 結束 Session 並用這批建樣板
              </button>
              <div class="warnings-box" data-templates-status-box style="display:none; margin-top:8px;">
                <div class="warnings-title" data-templates-status-title></div>
                <div class="mono" data-templates-summary style="font-size:12px; margin:6px 0;"></div>
                <ul data-templates-warnings-list></ul>
              </div>
              <div class="form-error" data-templates-error style="display:none; margin-top:8px;"></div>
            </aside>
          </section>

          <div class="trial-preview-overlay" data-preview-overlay style="display:none">
            <div class="trial-preview-box">
              <div class="trial-preview-header">
                <span data-preview-title class="mono"></span>
                <button type="button" class="preview-close-btn" data-preview-close>✕ 關閉</button>
              </div>
              <div class="trial-preview-body">
                <div class="preview-heatmaps" data-preview-heatmaps></div>
                <div class="preview-waveform" data-preview-waveform></div>
              </div>
            </div>
          </div>
        </div>
      `;

      els = {
        formScreen: root.querySelector('[data-screen="form"]'),
        baselineScreen: root.querySelector('[data-screen="baseline"]'),
        trialScreen: root.querySelector('[data-screen="trial"]'),
        form: root.querySelector("[data-form]"),
        subject: root.querySelector("[data-subject]"),
        mode: root.querySelector("[data-mode]"),
        distance: root.querySelector("[data-distance]"),
        angle: root.querySelector("[data-angle]"),
        ambient: root.querySelector("[data-ambient]"),
        notes: root.querySelector("[data-notes]"),
        formError: root.querySelector("[data-form-error]"),
        submitBtn: root.querySelector("[data-submit]"),
        speakingModeBtns: Array.from(root.querySelectorAll("[data-speaking-mode-btn]")),
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
        beepToggle: root.querySelector("[data-beep-toggle]"),
        baselineStaleNote: root.querySelector("[data-baseline-stale-note]"),
        baselineStaleMinutes: root.querySelector("[data-baseline-stale-minutes]"),
        baselineRestaleBtn: root.querySelector("[data-baseline-restale-btn]"),
        trialIdx: root.querySelector("[data-trial-idx]"),
        trialSeed: root.querySelector("[data-trial-seed]"),
        trialCard: root.querySelector("[data-trial-card]"),
        trialPrevLabel: root.querySelector("[data-trial-prev-label]"),
        trialWord: root.querySelector("[data-trial-word]"),
        trialRequeueNote: root.querySelector("[data-trial-requeue-note]"),
        trialStateLabel: root.querySelector("[data-trial-state-label]"),
        trialConfirmBox: root.querySelector("[data-trial-confirm]"),
        trialConfirmReason: root.querySelector("[data-trial-confirm-reason]"),
        confirmKeepBtn: root.querySelector("[data-confirm-keep]"),
        confirmDiscardBtn: root.querySelector("[data-confirm-discard]"),
        trialError: root.querySelector("[data-trial-error]"),
        targetPerLabelInput: root.querySelector("[data-target-per-label]"),
        progressSummary: root.querySelector("[data-progress-summary]"),
        progressBarFill: root.querySelector("[data-progress-bar-fill]"),
        progressEta: root.querySelector("[data-progress-eta]"),
        labelBars: root.querySelector("[data-label-bars]"),
        recentList: root.querySelector("[data-recent-list]"),
        liveTofADot: root.querySelector("[data-live-tofa-dot]"),
        liveTofAText: root.querySelector("[data-live-tofa-text]"),
        liveTofBDot: root.querySelector("[data-live-tofb-dot]"),
        liveTofBText: root.querySelector("[data-live-tofb-text]"),
        liveMicDot: root.querySelector("[data-live-mic-dot]"),
        liveMicText: root.querySelector("[data-live-mic-text]"),
        liveMicBar: root.querySelector("[data-live-mic-bar]"),
        buildTemplatesBtn: root.querySelector("[data-build-templates-btn]"),
        templatesStatusBox: root.querySelector("[data-templates-status-box]"),
        templatesStatusTitle: root.querySelector("[data-templates-status-title]"),
        templatesSummary: root.querySelector("[data-templates-summary]"),
        templatesWarningsList: root.querySelector("[data-templates-warnings-list]"),
        templatesError: root.querySelector("[data-templates-error]"),
        previewOverlay: root.querySelector("[data-preview-overlay]"),
        previewTitle: root.querySelector("[data-preview-title]"),
        previewClose: root.querySelector("[data-preview-close]"),
        previewHeatmaps: root.querySelector("[data-preview-heatmaps]"),
        previewWaveform: root.querySelector("[data-preview-waveform]"),
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
      els.speakingModeBtns.forEach((btn) => {
        btn.addEventListener("click", () => {
          speakingMode = btn.dataset.speakingMode;
          updateSpeakingModeUI();
        });
      });
      els.retryBtn.addEventListener("click", retryBaseline);
      // Same recapture flow as the unstable-baseline retry button above,
      // just reachable from the trial screen once the existing baseline
      // has gone stale (see BASELINE_STALE_MS) -- reuses enterBaselineScreen()
      // rather than a second "recapture" code path.
      els.baselineRestaleBtn.addEventListener("click", enterBaselineScreen);
      els.buildTemplatesBtn.addEventListener("click", buildTemplatesFromThisSession);

      els.beepToggle.addEventListener("change", () => {
        beepEnabled = els.beepToggle.checked;
      });
      els.confirmKeepBtn.addEventListener("click", triggerConfirmKeep);
      els.confirmDiscardBtn.addEventListener("click", triggerConfirmDiscard);
      document.addEventListener("keydown", onTrialKeydown);
      document.addEventListener("keyup", onTrialKeyup);

      els.targetPerLabelInput.addEventListener("change", () => {
        const v = parseInt(els.targetPerLabelInput.value, 10);
        targetPerLabel = v > 0 ? v : DEFAULT_TARGET_PER_LABEL;
        renderProgressDash();
        renderLabelBars();
      });
      // Event delegation -- recent-list items are re-rendered wholesale on
      // every SAVE, so binding once here (instead of per-item) means new
      // items are clickable without re-attaching anything.
      els.recentList.addEventListener("click", (e) => {
        // Reject/redo buttons live inside the same clickable row as the
        // preview trigger -- check them first so clicking a button doesn't
        // also pop the preview overlay open behind it.
        const rejectBtn = e.target.closest("[data-reject-idx]");
        if (rejectBtn) { rejectTrial(Number(rejectBtn.dataset.rejectIdx)); return; }
        const redoBtn = e.target.closest("[data-redo-idx]");
        if (redoBtn) { redoSavedTrial(Number(redoBtn.dataset.redoIdx)); return; }
        const item = e.target.closest("[data-recent-idx]");
        if (item) openPreview(Number(item.dataset.recentIdx));
      });
      els.previewClose.addEventListener("click", closePreview);
      els.previewOverlay.addEventListener("click", (e) => {
        if (e.target === els.previewOverlay) closePreview(); // click on the dim backdrop
      });

      updateWearIdUI();
      updateSpeakingModeUI();
      showScreen("form");
      loadPrefill();
      loadVocab();
      renderProgressDash();
      renderLabelBars();
      renderRecentList();
    },

    onEnter() {
      if (countdownTimer == null) {
        // Same interval drives both -- updateBaselineCountdown() is a
        // no-op off the baseline screen, checkBaselineStaleness() is a
        // no-op off the trial screen, so sharing one timer instead of
        // running two is just avoiding a redundant setInterval, not
        // coupling two unrelated concerns.
        countdownTimer = setInterval(() => {
          updateBaselineCountdown();
          checkBaselineStaleness();
          // 4Hz -- monitor.js has its own CPU history from updating on every
          // frame (drawTrail() forcing layout via clientWidth reads after
          // style writes, 916 times in 8s); sharing this same throttled
          // timer instead of a new one keeps this well under that.
          renderLiveSensorReadout();
        }, 250);
      }
    },

    onLeave() {
      if (countdownTimer != null) {
        clearInterval(countdownTimer);
        countdownTimer = null;
      }
      // Mode switched away mid-hold: release the key so a stray
      // hold_start() on this device never gets stuck open server-side with
      // nothing left to send its matching hold_stop().
      if (holdKeyDown) triggerHoldStop();
      closePreview(); // don't leave the overlay stuck open over another mode
      pendingRequeueLabel = null; // C14: don't let a stale requeue intent survive a mode switch
      stopCaptureStallWatch(); // don't leave this interval running while another mode is visible
      clearTimeout(templatesPollTimer); // don't keep polling for a build result nobody's looking at
    },

    onData(evt) {
      if (evt.type === "session") {
        if (evt.state === "baseline" && evt.progress) {
          if (evt.progress.done && evt.progress.outcome) {
            renderBaselineOutcome(evt.progress.outcome);
          } else {
            renderLiveStability(evt.progress);
          }
        }
      } else if (evt.type === "trial") {
        onTrialEvent(evt);
      }
    },
  };
})());
