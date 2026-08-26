// App entry point. Loads shell.js (mode switching, hash routing) and
// bus.js (dataStore + per-mode fan-out), opens the one SSE connection for
// the app's lifetime, and hands every message to the bus (C03). Mode
// switching never tears this connection down or rebuilds it -- see
// ssi-backlog/README.md, "架構關鍵：資料層與模式層分離" -- and
// EventSource auto-reconnects on its own after a drop, so a bridge
// restart just resumes the same pipeline without any code here noticing.
import { notifySseConnection } from "./shell.js";
import { handleEvent } from "./bus.js";
// 每個模式模組必須在這裡 import 一次，否則 registerMode() 永遠不會被呼叫，
// 該模式的 section 在瀏覽器裡會是空白的（看起來像模式自己的 bug，而不是
// 忘了在這裡加一行 -- C11 debug 這個坑花了一段時間才找到，見完成回報）。
// C05 monitor / C11 record / C15 quiz / C24 replay / (C22 validate 待補)
import "./modes/monitor.js";
import "./modes/record.js";
import "./modes/quiz.js";
import "./modes/replay.js";

const es = new EventSource("/events");

es.onopen = () => {
  console.log("[panel] SSE connected");
  notifySseConnection(true); // C04: browser<->bridge connection, distinct from bridge<->device {type:"link"}
};

es.onerror = () => {
  console.log("[panel] SSE connection error (bridge down or restarting)");
  notifySseConnection(false);
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
