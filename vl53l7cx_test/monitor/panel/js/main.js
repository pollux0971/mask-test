// App entry point. Loads shell.js (mode switching, hash routing) and
// bus.js (dataStore + per-mode fan-out), opens the one SSE connection for
// the app's lifetime, and hands every message to the bus (C03). Mode
// switching never tears this connection down or rebuilds it -- see
// ssi-backlog/README.md, "架構關鍵：資料層與模式層分離" -- and
// EventSource auto-reconnects on its own after a drop, so a bridge
// restart just resumes the same pipeline without any code here noticing.
import "./shell.js";
import { handleEvent } from "./bus.js";
import "./modes/monitor.js"; // C05; other modes import themselves in as they're built

const es = new EventSource("/events");

es.onopen = () => {
  console.log("[panel] SSE connected");
};

es.onerror = () => {
  console.log("[panel] SSE connection error (bridge down or restarting)");
};

es.onmessage = (e) => {
  let evt;
  try {
    evt = JSON.parse(e.data);
  } catch {
    return;
  }
  handleEvent(evt);
};
