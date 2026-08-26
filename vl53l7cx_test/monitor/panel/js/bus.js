// Event bus + dataStore (C03): SSE -> dataStore (ring buffers, always
// running) -> every registered mode's onData (only the visible mode does
// real rendering work, but every mode gets the event -- see C03.md).
//
// Data-model note: CONTRACTS.md chapter 4 (HTTP/SSE interface) is still
// unfrozen, and the *live* bridge_server.py right now still parses the
// pre-v2 wire format (see its parse_line()) -- its SSE events carry no
// seq/t_us at all, only {type, sensor, dim, values} / {type, rms, peak} /
// {type, dim}. So this buffers by *browser receive time*
// (performance.now()), not device t_us: it's correct today, and once a
// bridge revision starts forwarding t_us this still works unchanged --
// per-stream alignment by t_us (instead of by seq, since $T/$M/$F run at
// different, changing rates) is then a concern for whichever mode
// correlates streams, not for the buffer itself.
//
// Retention is time-based, not count-based, on purpose: $T is 30Hz (10Hz
// at 8x8), $M is ~31.25Hz, and $F will move from 31.25Hz to 62.5Hz after
// A14 -- a fixed entry count would represent a different span of real time
// depending on current rate and resolution.
//
// C09 needs 60s of "quality" history for its sparklines, so retention is
// 65s (60 + a little slack) -- applies to every stream, not just quality;
// harmless for the higher-rate ones, just a bigger buffer.

import { forEachRegisteredMode, notifyGlobalStatus } from "./shell.js";

const RETENTION_MS = 65000;

function makeRing() {
  return [];
}

function trim(buf, nowMs) {
  while (buf.length && nowMs - buf[0]._recvMs > RETENTION_MS) buf.shift();
}

const streams = {
  tofA: makeRing(),
  tofB: makeRing(),
  mic: makeRing(),
  // $F is wired into bridge_server.py's SSE output and mel events do flow
  // (confirmed live) -- this ring was reserved ahead of that per C03.md's
  // "架構要為未來留位置", the "no producer yet" note is stale now that
  // there is one.
  mel: makeRing(),
  // B19's 1Hz quality event (CONTRACTS.md 4.2) -- six metrics per tick,
  // used by C09 for 60s sparklines.
  quality: makeRing(),
};

let latestStatus = null;
let linkState = "down";

export const dataStore = {
  // Returns entries from the last `sinceMs` (default: full retention
  // window), oldest first. A gap in here (e.g. ToF silent during a
  // recording dump, see CONTRACTS.md 1.4) is just a gap -- there's no
  // "assume disconnected if stream X has been quiet" logic; the only
  // signal for link health is the explicit {type:"link"} event below.
  getRecent(streamKey, sinceMs = RETENTION_MS) {
    const buf = streams[streamKey];
    if (!buf) return [];
    const now = performance.now();
    return buf.filter((e) => now - e._recvMs <= sinceMs);
  },
  getLatestStatus() {
    return latestStatus;
  },
  getLinkState() {
    return linkState;
  },
};

function ingest(evt) {
  const nowMs = performance.now();
  if (evt.type === "tof") {
    const key = evt.sensor === "B" ? "tofB" : "tofA";
    const buf = streams[key];
    buf.push({ ...evt, _recvMs: nowMs });
    trim(buf, nowMs);
  } else if (evt.type === "mic") {
    streams.mic.push({ ...evt, _recvMs: nowMs });
    trim(streams.mic, nowMs);
  } else if (evt.type === "mel") {
    streams.mel.push({ ...evt, _recvMs: nowMs });
    trim(streams.mel, nowMs);
  } else if (evt.type === "quality") {
    streams.quality.push({ ...evt, _recvMs: nowMs });
    trim(streams.quality, nowMs);
  } else if (evt.type === "status") {
    latestStatus = { ...evt, _recvMs: nowMs };
  } else if (evt.type === "link") {
    linkState = evt.state;
  }
  // record/flash/mfcc etc. aren't time-series data; a mode that cares
  // reads them straight off the evt passed to onData.
}

export function handleEvent(evt) {
  if (!evt || typeof evt.type !== "string") return;
  ingest(evt);
  forEachRegisteredMode((hooks) => {
    if (typeof hooks.onData === "function") hooks.onData(evt);
  });
  // C04's global status bar (shell.js) needs every event, not just the
  // ones a specific mode cares about -- it's visible across all five
  // modes, so it stays a separate hook from forEachRegisteredMode rather
  // than pretending to be a sixth "mode".
  notifyGlobalStatus(evt);
}
