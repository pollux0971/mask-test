// App entry point (C01): loads the shell/bus module stubs so the ES module
// graph is exercised end to end, and opens the SSE connection to the
// bridge. Parsing/dispatching those events into the bus (C03) and rendering
// per-mode content (C05+) are out of scope here — this only proves the
// connection itself works with no build step.
import "./shell.js";
import "./bus.js";

const es = new EventSource("/events");

es.onopen = () => {
  console.log("[panel] SSE connected");
};

es.onerror = () => {
  console.log("[panel] SSE connection error (bridge down or restarting)");
};
