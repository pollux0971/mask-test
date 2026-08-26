// Sidebar mode switcher (C02): click, keyboard 1-5, active highlight,
// <=150ms fade-in, and sidebar collapse (\, persisted in sessionStorage).
//
// C03 additions: the mode registry now runs the Mode lifecycle from
// C03.md -- init(root) once, onEnter()/onLeave() per switch, and a
// fan-out point (forEachRegisteredMode) that bus.js uses to deliver
// onData(evt) to every mode regardless of visibility -- plus URL hash
// routing (#/monitor etc.) so a mode can be deep-linked or opened ahead
// of a Demo.
//
// Scope boundary: this only decides which .mode-section is visible, which
// mode's render loop should be running, and fans data out to every mode's
// onData -- it never touches the SSE connection itself (main.js opens
// that once and keeps it open across switches), and it doesn't own the
// data (dataStore lives in bus.js). Per-mode content is C05+.

const MODES = ["monitor", "record", "quiz", "validate", "replay"];
const KEY_TO_MODE = { "1": "monitor", "2": "record", "3": "quiz", "4": "validate", "5": "replay" };
const COLLAPSE_STORAGE_KEY = "panel.sidebar.collapsed";
const PROJECTOR_STORAGE_KEY = "panel.projector"; // C25, same sessionStorage pattern as collapse above

const sidebar = document.getElementById("sidebar");
const collapseToggle = document.getElementById("collapseToggle");
const projectorToggle = document.getElementById("projectorToggle");
const navItems = Array.from(document.querySelectorAll(".mode-nav-item"));
const sections = Object.fromEntries(
  MODES.map((mode) => [mode, document.getElementById(`mode-${mode}`)])
);

// Modes register their lifecycle here via registerMode(). init(root) runs
// once, immediately, at registration time -- so a mode's DOM exists (and
// it can receive onData) even if the user never clicks its nav item, which
// is what makes "open #/quiz ahead of the Demo" and "SSE broadcasts to
// every mode regardless of visibility" (C03.md) both work.
const modeHooks = Object.fromEntries(MODES.map((mode) => [mode, {}]));
const modeInitDone = Object.fromEntries(MODES.map((mode) => [mode, false]));
const modeEntered = Object.fromEntries(MODES.map((mode) => [mode, false]));

// enterMode/leaveMode (not calling hooks.onEnter/onLeave directly) exist so
// "already active, nobody switched to it" and "just switched into it" both
// funnel through one place with an idempotency guard -- see registerMode's
// comment below for why the first one is needed.
function enterMode(mode) {
  if (modeEntered[mode]) return;
  const onEnter = modeHooks[mode].onEnter;
  // If this mode hasn't registered yet (e.g. the initial #/quiz-style hash
  // activation runs before any mode module has imported), there's nothing
  // to call -- and importantly, don't mark it entered: leave the flag false
  // so registerMode()'s own "already current mode" check gets a real chance
  // to call the hook once it exists, instead of finding modeEntered already
  // (wrongly) true and skipping it forever.
  if (typeof onEnter !== "function") return;
  onEnter();
  modeEntered[mode] = true;
}

function leaveMode(mode) {
  if (!modeEntered[mode]) return;
  if (typeof modeHooks[mode].onLeave === "function") modeHooks[mode].onLeave();
  modeEntered[mode] = false;
}

export function registerMode(mode, hooks) {
  if (!MODES.includes(mode)) return;
  modeHooks[mode] = hooks || {};
  if (!modeInitDone[mode] && typeof modeHooks[mode].init === "function") {
    modeHooks[mode].init(sections[mode]);
    modeInitDone[mode] = true;
  }
  // The mode shown by default in the static HTML (or targeted by the
  // initial URL hash, handled further down) never goes through
  // activateMode()'s switch path -- nothing "switches" into the mode
  // that's already active on first paint. Without this, a mode whose
  // onEnter() starts a render loop would never actually start rendering
  // until the user left and came back once (found via real testing in
  // C05, monitor.js's heatmap never painted until switched away and back).
  if (mode === currentMode) enterMode(mode);
}

// Used by bus.js to deliver onData(evt) to every registered mode, visible
// or not -- dataStore is the thing that's "always running"; this is how a
// mode itself can also keep lightweight state alive while hidden without
// polling dataStore every frame.
//
// Each mode's callback is isolated in its own try/catch (a plain forEach
// would let one mode's thrown exception stop the loop, so every mode after
// it in MODES silently never sees that event -- found live via C24: a
// crash in record.js's onData was blocking replay.js, last in MODES, from
// getting anything, and the person debugging would start looking in the
// wrong mode entirely since the symptom shows up somewhere else). Console
// logging every prefix with the mode name for exactly that reason; the
// catch does not rethrow -- one broken mode isn't supposed to blind the
// rest, only itself.
export function forEachRegisteredMode(fn) {
  MODES.forEach((mode) => {
    try {
      fn(modeHooks[mode], mode);
    } catch (err) {
      console.error(`[bus] mode "${mode}" 的 handler 拋出例外，已隔離，其餘模式不受影響：`, err);
    }
  });
}

let currentMode = MODES.find((m) => sections[m] && sections[m].classList.contains("active")) || MODES[0];

function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

function setActiveNavItem(mode) {
  navItems.forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === mode));
}

function hashForMode(mode) {
  return `#/${mode}`;
}

function modeFromHash() {
  const m = location.hash.match(/^#\/(\w+)/);
  return m && MODES.includes(m[1]) ? m[1] : null;
}

// replaceState (not pushState): switching modes shouldn't spam browser
// back-history with one entry per click, but the hash still stays accurate
// for a refresh, a copied link, or opening a second tab pinned to a mode.
function syncHashTo(mode) {
  const target = hashForMode(mode);
  if (location.hash !== target) {
    history.replaceState(null, "", target);
  }
}

export function activateMode(mode, { instant = false } = {}) {
  if (!MODES.includes(mode) || mode === currentMode) return;
  const prevMode = currentMode;
  const prevSection = sections[prevMode];
  const nextSection = sections[mode];
  if (!prevSection || !nextSection) return;

  leaveMode(prevMode);

  // Hide the outgoing section and show the incoming one in the same tick --
  // there is never a frame where both are display:none (a blank flash) or
  // both are visible at once (double content). The incoming section starts
  // at opacity 0 (via .fade-in) so it fades in over 150ms instead of
  // snapping straight to visible -- except on the initial hash-driven
  // activation at load, where a fade would just be a flash-then-fade from
  // the HTML's hardcoded default mode.
  prevSection.classList.remove("active");
  if (instant) {
    nextSection.classList.add("active");
  } else {
    nextSection.classList.add("active", "fade-in");
    // Force a style flush so the browser registers opacity:0 before we
    // remove .fade-in -- otherwise both class changes coalesce into one
    // frame and there's nothing to transition from.
    void nextSection.offsetWidth;
    nextSection.classList.remove("fade-in");
  }

  setActiveNavItem(mode);
  currentMode = mode;
  syncHashTo(mode);

  enterMode(mode);

  document.dispatchEvent(new CustomEvent("panel:modechange", { detail: { mode, previous: prevMode } }));
}

navItems.forEach((btn) => {
  btn.addEventListener("click", () => activateMode(btn.dataset.mode));
});

// --- URL hash routing ---
// On load, a valid #/mode wins over the HTML's hardcoded default so a
// Demo tab opened as .../panel/#/quiz shows quiz immediately -- instant,
// not faded, since this is the first paint rather than a user switch.
const initialHashMode = modeFromHash();
if (initialHashMode && initialHashMode !== currentMode) {
  activateMode(initialHashMode, { instant: true });
} else {
  syncHashTo(currentMode);
}

window.addEventListener("hashchange", () => {
  const target = modeFromHash();
  if (target && target !== currentMode) activateMode(target);
});

// --- sidebar collapse ---
function setCollapsed(collapsed) {
  sidebar.classList.toggle("collapsed", collapsed);
  collapseToggle.setAttribute("aria-label", collapsed ? "展開側邊欄" : "摺疊側邊欄");
  try {
    sessionStorage.setItem(COLLAPSE_STORAGE_KEY, collapsed ? "1" : "0");
  } catch {
    // sessionStorage unavailable (e.g. sandboxed preview) -- collapse still
    // works for this session, it just won't survive a reload.
  }
}

function toggleCollapsed() {
  setCollapsed(!sidebar.classList.contains("collapsed"));
}

collapseToggle.addEventListener("click", toggleCollapsed);

try {
  if (sessionStorage.getItem(COLLAPSE_STORAGE_KEY) === "1") setCollapsed(true);
} catch {
  // default: expanded
}

// --- C25: projector mode ---
//
// One-click 130% text for Demo (C25.md). Sets a root attribute that
// tokens.css's `html[data-projector-mode="true"]` rule reads -- this file
// only flips the attribute and persists the choice, tokens.css owns what
// "projector mode" actually looks like. Same sessionStorage-persisted
// toggle pattern as setCollapsed() above, on purpose: Demo rehearsal means
// switching this on/off repeatedly, and losing it on every reload would be
// exactly the kind of friction this feature exists to remove.
function setProjectorMode(on) {
  document.documentElement.setAttribute("data-projector-mode", on ? "true" : "false");
  projectorToggle.setAttribute("aria-pressed", on ? "true" : "false");
  projectorToggle.classList.toggle("active", on);
  try {
    sessionStorage.setItem(PROJECTOR_STORAGE_KEY, on ? "1" : "0");
  } catch {
    // sessionStorage unavailable -- projector mode still works for this
    // session, it just won't survive a reload.
  }
}

function toggleProjectorMode() {
  setProjectorMode(document.documentElement.getAttribute("data-projector-mode") !== "true");
}

projectorToggle.addEventListener("click", toggleProjectorMode);

try {
  if (sessionStorage.getItem(PROJECTOR_STORAGE_KEY) === "1") setProjectorMode(true);
} catch {
  // default: off
}

document.addEventListener("keydown", (e) => {
  if (isTypingTarget(e.target)) return;
  if (e.altKey || e.ctrlKey || e.metaKey) return;

  if (e.key === "\\") {
    e.preventDefault();
    toggleCollapsed();
    return;
  }
  if (e.key.toLowerCase() === "p") {
    e.preventDefault();
    toggleProjectorMode();
    return;
  }
  const mode = KEY_TO_MODE[e.key];
  if (mode) {
    e.preventDefault();
    activateMode(mode);
  }
});

// --- C04: global status bar --------------------------------------------
//
// Visible in all five modes (it lives in the sidebar, outside any
// .mode-section), so it can't hook into a mode's onData -- bus.js calls
// notifyGlobalStatus() on every event instead (see bus.js's handleEvent).
// Deliberately does NOT rebuild C09's quality dashboard; "點擊展開完整
// 品質面板" (C04.md) just switches to monitor mode, where that dashboard
// already lives.
//
// Two independent notions of "connected", per CONTRACTS.md: the browser's
// own SSE connection to bridge_server.py (EventSource-level; main.js
// reports this via notifySseConnection since it owns the EventSource),
// and the bridge's serial link to the device itself ({type:"link"}, C03).
// If the SSE is down the bridge is unreachable and nothing else matters;
// if SSE is up but the link is down, the device itself is unplugged --
// two different problems, so they get two different messages.

const REPLAY_WINDOW_MS = 3000;   // how long a single replay:true event's effect lingers
const TOF_RATE_WINDOW_MS = 2000; // matches monitor.js's own Hz calc window
const DEVICE_STATE_POLL_MS = 2000;

const statusConnTextEl = document.querySelector("[data-status-conn-text]");
const statusWarningEl = document.querySelector("[data-status-warning]");
const statusDropEl = document.querySelector("[data-status-drop]");
const statusSymmetryEl = document.querySelector("[data-status-symmetry]");
const statusFpsEl = document.querySelector("[data-status-fps]");
const statusSummaryBtn = document.querySelector("[data-status-summary]");
const linkDotEl = document.getElementById("linkDot");

let sseUp = false;
// Distinguishes "never connected yet" (normal for the first ~instant after
// page load, before EventSource's first onopen) from "was connected, now
// isn't" (a real disconnect) -- the alert ring below should only fire for
// the latter, or every fresh page load would flash red for that first
// instant even when nothing is actually wrong.
let everConnectedSse = false;
// A page that (re)loads after the device link was already established never
// sees that one-time {type:"link",state:"up"} broadcast -- bridge_server.py
// only relays *transitions* to whoever's subscribed at the moment, it
// doesn't resend current link state to a newly-joined SSE client (found via
// real testing: a fresh tab against an already-running bridge showed real
// tof/quality data but this bar stuck on "裝置斷線" forever). So: trust an
// explicit link event once we've seen one (in either direction, and that's
// authoritative from then on); until we have, infer from whether payload
// data is actually arriving.
let explicitLinkState = null; // null | "up" | "down"
let lastDataAt = -Infinity;   // last time ANY payload-carrying event (tof/quality/status/mic) arrived
const LINK_DATA_FRESHNESS_MS = 3000;

function computeLinkUp(now) {
  if (explicitLinkState === "down") return false;
  if (explicitLinkState === "up") return true;
  return now - lastDataAt < LINK_DATA_FRESHNESS_MS;
}
let latestDeviceStatus = null; // last {type:"status", ...} event
let qualityLevels = { drop_rate: "unknown", symmetry: "unknown" };
let qualityValues = { drop_rate: null, symmetry: null };
let tofTimestamps = [];
let lastReplayAt = -Infinity;
let resolutionChangeInProgress = false;
let lastDeviceStatePollAt = -Infinity;

function fmtPercent(v) {
  return typeof v === "number" ? (v * 100).toFixed(1) + "%" : "--";
}

// §4.1.2: a resolution switch resets seq (expected, session-boundary
// behavior per CONTRACTS 1.3) -- poll the existing /device/state endpoint
// (B18) so a reflash doesn't read as "connection broken" on this bar. C09
// polls this same endpoint independently for its own reflash note; not
// shared on purpose -- these are two different files/features and the
// endpoint is cheap, static JSON.
function pollDeviceState(now) {
  if (now - lastDeviceStatePollAt < DEVICE_STATE_POLL_MS) return;
  lastDeviceStatePollAt = now;
  fetch("/device/state")
    .then((r) => (r.ok ? r.json() : null))
    .then((state) => {
      resolutionChangeInProgress = !!(state && state.resolution_change_in_progress);
      renderStatusBar();
    })
    .catch(() => {
      // transient fetch failure -- leave the last known value, don't spam
    });
}

function renderStatusBar() {
  const now = performance.now();
  const inReplay = now - lastReplayAt < REPLAY_WINDOW_MS;
  const linkUp = computeLinkUp(now);

  sidebar.classList.toggle("status-replay", inReplay);
  sidebar.classList.toggle("status-reflashing", resolutionChangeInProgress && !inReplay);

  if (inReplay) {
    // B17: every event may carry replay:true; this must be unmistakable so
    // nobody mistakes a replayed session for a live one (CONTRACTS.md 4.2).
    linkDotEl.classList.remove("up");
    statusConnTextEl.textContent = "▶ REPLAY";
    statusConnTextEl.className = "status-conn-text mono replay";
  } else if (!sseUp) {
    linkDotEl.classList.remove("up");
    statusConnTextEl.textContent = "斷線（主機）";
    statusConnTextEl.className = "status-conn-text mono down";
  } else if (!linkUp) {
    linkDotEl.classList.remove("up");
    statusConnTextEl.textContent = "裝置斷線";
    statusConnTextEl.className = "status-conn-text mono down";
  } else if (resolutionChangeInProgress) {
    linkDotEl.classList.add("up");
    statusConnTextEl.textContent = "重燒中…";
    statusConnTextEl.className = "status-conn-text mono reflashing";
  } else {
    linkDotEl.classList.add("up");
    const proto = latestDeviceStatus ? latestDeviceStatus.protocol_version : null;
    const fw = latestDeviceStatus && latestDeviceStatus.fw ? latestDeviceStatus.fw.slice(0, 7) : "--";
    const protoText = proto == null ? "proto?" : `proto${proto}`;
    statusConnTextEl.textContent = `已連線  ${protoText}  ${fw}`;
    // proto v1 (or an explicit version mismatch) uses the warn color --
    // CONTRACTS.md 1.1: version_mismatch means the bridge has stopped
    // parsing all $ lines entirely, so the user needs to know why nothing
    // on screen is moving.
    const protoWarn = proto === 1 || (latestDeviceStatus && latestDeviceStatus.version_mismatch);
    statusConnTextEl.className = "status-conn-text mono" + (protoWarn ? " proto-warn" : "");
  }

  if (!inReplay && latestDeviceStatus && latestDeviceStatus.warning) {
    // B02: e.g. "協定 v1 — 無時間戳，資料不可用於驗證分析", recording_allowed=false alongside it.
    statusWarningEl.textContent = "⚠ " + latestDeviceStatus.warning;
    statusWarningEl.style.display = "block";
  } else {
    statusWarningEl.style.display = "none";
  }

  statusDropEl.textContent = fmtPercent(qualityValues.drop_rate);
  statusSymmetryEl.textContent = fmtPercent(qualityValues.symmetry);
  while (tofTimestamps.length && now - tofTimestamps[0] > TOF_RATE_WINDOW_MS) tofTimestamps.shift();
  statusFpsEl.textContent = (tofTimestamps.length / (TOF_RATE_WINDOW_MS / 1000)).toFixed(1) + " Hz";

  // unknown must never look like it passed (same rule as C09's hollow-ring
  // dot) -- here that means "not counted as red", not "counted as green".
  //
  // !sseUp belongs in this same "loudest alert" tier, not a smaller one --
  // found live via the disconnect audit: a degraded quality metric (still
  // receiving data, just not great) already triggered this ring, but the
  // browser having NO connection to the bridge at all didn't. That's the
  // severity ordering backwards (a bad-but-present signal alarmed harder
  // than no signal at all), and it was easy to miss since the only other
  // cue was one line of small sidebar text -- exactly the failure mode this
  // ring exists to prevent. Clears itself automatically the moment sseUp
  // flips back to true (EventSource's native reconnect already calls
  // notifySseConnection(true) -> renderStatusBar() with no reload needed,
  // verified in that same audit), so no separate recovery logic is needed.
  // everConnectedSse guards against a false-alarm flash during the first
  // instant after page load, before the very first onopen -- that's normal,
  // not a disconnect, and shouldn't ring the loudest alert this app has.
  const anyRed = qualityLevels.drop_rate === "red" || qualityLevels.symmetry === "red" || (everConnectedSse && !sseUp);
  sidebar.classList.toggle("status-alert", anyRed && !inReplay);
}

export function notifySseConnection(isUp) {
  sseUp = isUp;
  if (isUp) everConnectedSse = true;
  renderStatusBar();
}

export function notifyGlobalStatus(evt) {
  const now = performance.now();
  if (evt.replay === true) lastReplayAt = now;

  if (evt.type === "link") {
    explicitLinkState = evt.state === "up" ? "up" : "down";
  } else if (evt.type === "status") {
    latestDeviceStatus = evt;
    lastDataAt = now;
  } else if (evt.type === "quality") {
    const m = evt.metrics || {};
    if (m.drop_rate) { qualityLevels.drop_rate = m.drop_rate.level; qualityValues.drop_rate = m.drop_rate.value; }
    if (m.symmetry) { qualityLevels.symmetry = m.symmetry.level; qualityValues.symmetry = m.symmetry.value; }
    lastDataAt = now;
  } else if (evt.type === "tof") {
    tofTimestamps.push(now);
    lastDataAt = now;
  } else if (evt.type === "mic") {
    lastDataAt = now;
  }

  pollDeviceState(now);
  renderStatusBar();
}

statusSummaryBtn.addEventListener("click", () => activateMode("monitor"));

renderStatusBar(); // paint the initial "connecting..." state before any event arrives

// Without this, the bar only updates when notifyGlobalStatus fires -- if
// data stops arriving entirely (the exact case this bar exists to catch),
// nothing would trigger a re-render and it'd freeze showing stale "still
// connected, still 15Hz" numbers forever. A real disconnect's explicit
// {type:"link",state:"down"} event (near-instant, see C03's testing)
// already re-renders on its own; this tick is what makes the FPS readout
// decay toward 0 and the data-freshness link inference actually expire
// when nothing else is happening.
setInterval(renderStatusBar, 500);
