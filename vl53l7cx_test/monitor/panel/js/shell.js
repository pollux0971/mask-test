// Sidebar mode switcher (C02): click, keyboard 1-5, active highlight,
// <=150ms fade-in, and sidebar collapse (\, persisted in sessionStorage).
//
// Scope boundary: this only decides which .mode-section is visible and
// which mode's render loop should be running (via onEnter/onExit hooks on
// each mode module) -- it never touches the SSE connection, which main.js
// opens once and keeps open across switches. Cross-mode state persistence
// (what a mode remembers when hidden) and the data bus/dataStore are C03.
// Per-mode content is C05+.

const MODES = ["monitor", "record", "quiz", "validate", "replay"];
const KEY_TO_MODE = { "1": "monitor", "2": "record", "3": "quiz", "4": "validate", "5": "replay" };
const COLLAPSE_STORAGE_KEY = "panel.sidebar.collapsed";
const FADE_MS = 150;

const sidebar = document.getElementById("sidebar");
const collapseToggle = document.getElementById("collapseToggle");
const navItems = Array.from(document.querySelectorAll(".mode-nav-item"));
const sections = Object.fromEntries(
  MODES.map((mode) => [mode, document.getElementById(`mode-${mode}`)])
);

// Modes register onEnter/onExit here (C03+ fills these in per mode module);
// shell.js just guarantees they're called at the right time so the "stop
// rAF, keep accumulating state" contract from README.md has somewhere to
// live. Absence of a hook is fine -- C01/C02 ship with empty mode modules.
const modeHooks = Object.fromEntries(MODES.map((mode) => [mode, {}]));

export function registerMode(mode, hooks) {
  if (!MODES.includes(mode)) return;
  modeHooks[mode] = hooks || {};
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

export function activateMode(mode) {
  if (!MODES.includes(mode) || mode === currentMode) return;
  const prevMode = currentMode;
  const prevSection = sections[prevMode];
  const nextSection = sections[mode];
  if (!prevSection || !nextSection) return;

  if (typeof modeHooks[prevMode].onExit === "function") modeHooks[prevMode].onExit();

  // Hide the outgoing section and show the incoming one in the same tick --
  // there is never a frame where both are display:none (a blank flash) or
  // both are visible at once (double content). The incoming section starts
  // at opacity 0 (via .fade-in) so it fades in over FADE_MS instead of
  // snapping straight to visible.
  prevSection.classList.remove("active");
  nextSection.classList.add("active", "fade-in");
  // Force a style flush so the browser registers opacity:0 before we
  // remove .fade-in -- otherwise both class changes coalesce into one
  // frame and there's nothing to transition from.
  void nextSection.offsetWidth;
  nextSection.classList.remove("fade-in");

  setActiveNavItem(mode);
  currentMode = mode;

  if (typeof modeHooks[mode].onEnter === "function") modeHooks[mode].onEnter();

  document.dispatchEvent(new CustomEvent("panel:modechange", { detail: { mode, previous: prevMode } }));
}

navItems.forEach((btn) => {
  btn.addEventListener("click", () => activateMode(btn.dataset.mode));
});

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
