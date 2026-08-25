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

const sidebar = document.getElementById("sidebar");
const collapseToggle = document.getElementById("collapseToggle");
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
export function forEachRegisteredMode(fn) {
  MODES.forEach((mode) => fn(modeHooks[mode], mode));
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

document.addEventListener("keydown", (e) => {
  if (isTypingTarget(e.target)) return;
  if (e.altKey || e.ctrlKey || e.metaKey) return;

  if (e.key === "\\") {
    e.preventDefault();
    toggleCollapsed();
    return;
  }
  const mode = KEY_TO_MODE[e.key];
  if (mode) {
    e.preventDefault();
    activateMode(mode);
  }
});
