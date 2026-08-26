// Validate mode (C22): guided physical-verification experiment runner.
//
// Five experiments (C0/A/B/C/E) as defined by D15's analysis.run_all --
// this file is UI orchestration only, it does NOT reimplement any analysis
// math (C22.md's own "不包含: 分析演算法 D10-D14" boundary). Every PASS/FAIL
// number shown here comes verbatim from the backend's report matrix.
//
// Backend wiring status (confirmed live, both currently 404): `POST
// /verify/run` and `GET /verify/state` don't exist yet -- proposed their
// shape to esp-mask-test-ad/esp-mask-test-ed while writing this file, not
// yet implemented. Same auto-upgrade pattern as C10's /pca and C16-C19's
// /recognize: build the full UI against the documented shape now, degrade
// to a "尚未串接" status line when the fetch 404s, and it starts working
// the moment the endpoint lands with zero JS changes here.
//
// C0 (串擾) needs its own note. `session_loader.availability()`
// (analysis/reporting/session_loader.py) says outright: C0 can never be
// satisfied through run_all/session files (schema has no per-session
// sensor on/off record) and points at "exp_d10_crosstalk 的專用流程" --
// but exp_d10_crosstalk.py (checked directly) is a pure function library
// (zone_distance_delta/crosstalk_verdict), not a runnable pipeline; there
// is no CLI or HTTP surface that turns a live capture into a verdict.
// So this file's C0 card does the part that's genuinely real today --
// automatic, correctly-ordered device state stepping via B18's existing
// /sensor endpoint, with real progress -- and is honest that the analysis
// step itself has no backend wiring yet, rather than reimplementing D10's
// math in JS to fake a number. See the completion report.

import { registerMode } from "../shell.js";

const EXPERIMENTS = [
  { key: "C0", name: "串擾（Crosstalk）", metric: "Δ_dist", criterion: "< 2 mm", guided: true,
    purpose: "確認兩顆 ToF 感測器同時開啟時不會互相干擾距離讀數。" },
  { key: "A", name: "逐 Zone SNR", metric: "SNR_L / SNR_R", criterion: "> 3",
    purpose: "圓唇／展唇對照下，每個 zone 的訊噪比是否足夠分辨唇形。" },
  { key: "B", name: "跨次戴 CV", metric: "CV_between", criterion: "< 30%",
    purpose: "同一個人重新戴上裝置後，讀數的變異程度是否在可接受範圍。" },
  { key: "C", name: "Silhouette 可分性", metric: "Silhouette", criterion: "> 0.15",
    purpose: "不同詞的特徵在降維後是否群聚分離，越高代表越好分類。" },
  { key: "E", name: "Viseme 敏感度", metric: "擦音 Mel > ToF", criterion: "有模式",
    purpose: "擦音（如「四」）是否確實在 Mel 特徵上比 ToF 更明顯，驗證雙模態互補的假設。" },
];

const STATUS_LABEL = {
  pass: "✓ PASS", fail: "✗ FAIL", skipped: "— SKIPPED", error: "⚠ ERROR",
};
const STATUS_CLASS = {
  pass: "validate-status-pass", fail: "validate-status-fail",
  skipped: "validate-status-skipped", error: "validate-status-error",
};

// C22.md's own guided-flow example for C0, verbatim step order.
const C0_STEPS = [
  { label: "關閉感測器 B，請保持靜止", sensA: 1, sensB: 0 },
  { label: "關閉感測器 A、開啟 B，保持靜止", sensA: 0, sensB: 1 },
  { label: "兩顆同時開啟，保持靜止", sensA: 1, sensB: 1 },
];
const C0_STEP_SECONDS = 30;

function parseSessionPaths(raw) {
  return raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
}

registerMode("validate", (() => {
  let cardsEl = null;
  let sessionsInputEl = null, fastCheckboxEl = null, realCheckboxEl = null;
  let runBtn = null, runStatusEl = null, runErrorEl = null;
  let c0Btn = null, c0AbortBtn = null, c0ProgressEl = null, c0ResultEl = null;

  let lastRun = null;      // {matrix, finished_at, elapsed_s, ...} from /verify/state
  let running = false;
  let runStartedMs = null;
  let pollTimer = null;

  // ---------------------------------------------------------------- C23
  let reportsListEl = null, compareAreaEl = null, reportsErrorEl = null;
  let allReports = [];     // from GET /verify/reports, newest first
  let selectedIds = [];    // up to 2, for side-by-side compare

  let c0Running = false;
  let c0AbortRequested = false;

  function statusCellHtml(key) {
    const outcome = lastRun && lastRun.matrix ? lastRun.matrix.find((o) => o.key === key) : null;
    if (!outcome) {
      return `<span class="validate-status validate-status-none">尚無結果</span>`;
    }
    const cls = STATUS_CLASS[outcome.status] || "validate-status-none";
    const label = STATUS_LABEL[outcome.status] || outcome.status;
    const measured = outcome.measured && outcome.measured !== "—" ? ` · ${outcome.measured}` : "";
    return `<span class="validate-status ${cls}">${label}${measured}</span>` +
      // skipped/error 一定要有說明，不能是沒有解釋的灰格子 (esp-mask-test-ad
      // 的明確要求，見完成回報)
      (outcome.reason ? `<div class="validate-reason">${outcome.reason}</div>` : "");
  }

  function renderCards() {
    cardsEl.innerHTML = EXPERIMENTS.map((exp) => `
      <div class="validate-card" data-exp-card="${exp.key}">
        <div class="validate-card-head">
          <span class="validate-card-key mono">${exp.key}</span>
          <span class="validate-card-name">${exp.name}</span>
        </div>
        <div class="validate-card-purpose">${exp.purpose}</div>
        <div class="validate-card-criterion mono">${exp.metric}　${exp.criterion}</div>
        <div class="validate-card-result" data-exp-result="${exp.key}">${statusCellHtml(exp.key)}</div>
        ${exp.guided ? `
          <div class="validate-c0-wizard" data-c0-wizard>
            <div class="validate-c0-controls">
              <button class="validate-btn" data-c0-start>▶ 開始引導流程</button>
              <button class="validate-btn validate-btn-abort" data-c0-abort style="display:none">■ 中止</button>
            </div>
            <div class="validate-c0-progress" data-c0-progress></div>
            <div class="validate-c0-result" data-c0-result></div>
          </div>
        ` : ""}
      </div>
    `).join("");

    if (EXPERIMENTS.some((e) => e.guided)) {
      c0Btn = cardsEl.querySelector("[data-c0-start]");
      c0AbortBtn = cardsEl.querySelector("[data-c0-abort]");
      c0ProgressEl = cardsEl.querySelector("[data-c0-progress]");
      c0ResultEl = cardsEl.querySelector("[data-c0-result]");
      c0Btn.addEventListener("click", runC0Wizard);
      c0AbortBtn.addEventListener("click", () => { c0AbortRequested = true; });
    }
  }

  // ---------------------------------------------------------------- A/B/C/E
  // These four all go through the same "point run_all at session files"
  // path -- C0 is structurally different (no session file involved at all)
  // so it gets its own wizard below instead of sharing this button.

  function renderRunStatus() {
    if (running) {
      const elapsedS = ((performance.now() - runStartedMs) / 1000).toFixed(0);
      // D15's run_experiments() has no per-experiment progress callback
      // (it's a sequential try/except loop, not instrumented) -- an honest
      // "still running, Ns elapsed" beats faking a 5-step percentage that
      // doesn't correspond to anything real.
      runStatusEl.textContent = `執行中… 已過 ${elapsedS} 秒（目標 < 120 秒）`;
    } else if (lastRun) {
      runStatusEl.textContent = `上次執行：${lastRun.finished_at || "?"}（耗時 ${
        lastRun.elapsed_s != null ? lastRun.elapsed_s.toFixed(1) : "?"} 秒）` +
        (lastRun.is_synthetic ? "　⚠ 合成資料" : "");
    } else {
      runStatusEl.textContent = "尚未執行過";
    }
  }

  async function refreshState() {
    try {
      const res = await fetch("/verify/state");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const state = await res.json();
      const wasRunning = running;
      running = !!state.running;
      if (state.last_run) lastRun = state.last_run;
      runErrorEl.style.display = "none";
      renderCards();
      renderRunStatus();
      if (!running && pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      // A run just finished -- the reports list (C23) has a new entry now,
      // refresh it so it shows up without the user reloading the page.
      if (wasRunning && !running) refreshReports();
    } catch (err) {
      // /verify/state not wired yet (D09/C16-C19's exact same shape of gap)
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      running = false;
      runStatusEl.textContent = "尚未串接（/verify/state 還沒上線）";
      console.warn("[validate] /verify/state unavailable:", err.message);
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(refreshState, 1000);
  }

  async function onRunClick() {
    const sessions = parseSessionPaths(sessionsInputEl.value);
    if (!sessions.length) {
      runErrorEl.textContent = "請至少填一個 session .h5 檔案路徑";
      runErrorEl.style.display = "block";
      return;
    }
    runErrorEl.style.display = "none";
    try {
      const res = await fetch("/verify/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sessions,
          fast: fastCheckboxEl.checked,
          real: realCheckboxEl.checked,
        }),
      });
      if (res.status === 409) {
        runErrorEl.textContent = "已經有一輪驗證在執行中，請等它結束";
        runErrorEl.style.display = "block";
        return;
      }
      if (!res.ok) throw new Error("HTTP " + res.status);
      running = true;
      runStartedMs = performance.now();
      renderRunStatus();
      startPolling();
    } catch (err) {
      runStatusEl.textContent = "尚未串接（/verify/run 還沒上線）";
      console.warn("[validate] /verify/run unavailable:", err.message);
    }
  }

  // ------------------------------------------------------------------- C0

  async function setSensor(id, on) {
    const res = await fetch(`/sensor?id=${id}&on=${on ? 1 : 0}`, { method: "POST" });
    if (!res.ok) throw new Error("HTTP " + res.status);
  }

  function countdown(seconds, onTick) {
    return new Promise((resolve) => {
      let remaining = seconds;
      onTick(remaining);
      const id = setInterval(() => {
        if (c0AbortRequested) { clearInterval(id); resolve(); return; }
        remaining -= 1;
        onTick(remaining);
        if (remaining <= 0) { clearInterval(id); resolve(); }
      }, 1000);
    });
  }

  function renderC0Progress(text) {
    c0ProgressEl.textContent = text;
  }

  async function runC0Wizard() {
    if (c0Running) return;
    c0Running = true;
    c0AbortRequested = false;
    c0Btn.style.display = "none";
    c0AbortBtn.style.display = "inline-block";
    c0ResultEl.textContent = "";
    c0ResultEl.className = "validate-c0-result";

    try {
      for (let i = 0; i < C0_STEPS.length && !c0AbortRequested; i++) {
        const step = C0_STEPS[i];
        renderC0Progress(`步驟 ${i + 1}/4　${step.label}　[自動: SENS:A=${step.sensA},B=${step.sensB}]`);
        try {
          await setSensor("A", step.sensA);
          await setSensor("B", step.sensB);
        } catch (err) {
          renderC0Progress(
            `裝置控制端點呼叫失敗（${err.message}）—— 引導流程中止。` +
            `確認 bridge_server.py 有跑、B18 的 /sensor 端點已上線。`
          );
          c0Running = false;
          c0Btn.style.display = "inline-block";
          c0AbortBtn.style.display = "none";
          return;
        }
        await countdown(C0_STEP_SECONDS, (remaining) => {
          renderC0Progress(`步驟 ${i + 1}/4　${step.label}　${remaining}s`);
        });
      }

      if (c0AbortRequested) {
        renderC0Progress("已中止");
      } else {
        renderC0Progress("步驟 4/4　分析中…");
        // No backend wiring for this step yet -- exp_d10_crosstalk.py
        // (analysis/experiments/) is only a pure function library
        // (zone_distance_delta/crosstalk_verdict), there's no CLI or HTTP
        // endpoint that turns this wizard's three captures into a verdict.
        // Not reimplementing that math here (out of C22's scope per
        // C22.md's "不包含: 分析演算法"), so this is honest about the gap
        // instead of faking a PASS/FAIL. See completion report.
        renderC0Progress("裝置狀態已依序切換完成（三種感測器組態，各 30 秒）。");
        c0ResultEl.className = "validate-c0-result validate-c0-result-pending";
        c0ResultEl.textContent =
          "分析步驟尚未串接：exp_d10_crosstalk.py 目前只有純函式，" +
          "還沒有對應的 HTTP 端點能把這三段擷取變成 Δ_dist 判定。" +
          "已回報給調度員，等後端補上端點後這裡會自動可用。";
      }
    } finally {
      // Always leave both sensors on afterwards -- a solo-sensor state left
      // over from an aborted/finished wizard would silently break every
      // OTHER experiment card's dual-sensor assumption for whoever uses
      // this panel next.
      try { await setSensor("A", 1); await setSensor("B", 1); } catch { /* best-effort */ }
      c0Running = false;
      c0Btn.style.display = "inline-block";
      c0AbortBtn.style.display = "none";
    }
  }

  // ------------------------------------------------------------- C23 report viewer
  //
  // Design note: D15's summary.html is the primary view here (iframed
  // as-is) rather than this file writing a second Markdown-to-HTML
  // renderer to reproduce it -- exactly the "don't re-derive a second copy
  // of something that already exists" discipline esp-mask-test-ad flagged
  // after C22 (reference_mel.py / dtw_compare / D15's three-state split).
  // Two renderers of the same report are two things that can silently
  // drift apart; one iframe of D15's own HTML can't.
  //
  // Known gap, not fixed here (not this file's code): render_summary_html()
  // in verification_report.py has no "## 診斷建議" section -- summary.md
  // does, summary.html doesn't. Flagged to esp-mask-test-ad in the C23
  // proposal message; iframing summary.html as-is means that section is
  // currently invisible in this viewer until D15's owner adds it.
  //
  // Figures: neither summary.md nor summary.html embed any <img> at all
  // (checked verification_report.py directly -- figures are sibling files
  // under figures/, associated with an experiment only by the private
  // filename convention inside each run_*() function in run_all.py, not by
  // anything the report itself exposes). Rather than guess/hardcode that
  // mapping here (fragile -- breaks silently the day D15 adds or renames a
  // figure), this just lists whatever's in a report's `figures` array
  // generically as "圖表" for that run, with a PDF link alongside every
  // PNG for the D20 dual-format output.

  function fmtReportLabel(report) {
    const synth = report.is_synthetic ? "　⚠ 合成" : "";
    return `${report.created_at || report.id}${synth}　(${
      report.elapsed_s != null ? report.elapsed_s.toFixed(1) : "?"}s)`;
  }

  function renderReportsList() {
    if (!allReports.length) {
      reportsListEl.innerHTML = `<div class="validate-reports-empty">尚無歷史報告</div>`;
      return;
    }
    reportsListEl.innerHTML = allReports.map((r) => `
      <label class="validate-report-row${selectedIds.includes(r.id) ? " selected" : ""}">
        <input type="checkbox" data-report-checkbox value="${r.id}"
          ${selectedIds.includes(r.id) ? "checked" : ""}>
        <span class="mono validate-report-id">${r.id}</span>
        <span class="validate-report-meta">${fmtReportLabel(r)}</span>
        <span class="validate-report-sessions">${(r.sessions || []).join("、")}</span>
      </label>
    `).join("");
    Array.from(reportsListEl.querySelectorAll("[data-report-checkbox]")).forEach((el) => {
      el.addEventListener("change", () => onToggleSelect(el.value, el.checked));
    });
  }

  function onToggleSelect(id, checked) {
    if (checked) {
      // 「並排比較兩份」是這個 story 的核心價值 (C23.md) -- 上限 2，
      // 選第三個時把最舊的選取換掉，而不是無限疊加成一排看不完的 iframe。
      if (!selectedIds.includes(id)) {
        selectedIds.push(id);
        if (selectedIds.length > 2) selectedIds.shift();
      }
    } else {
      selectedIds = selectedIds.filter((x) => x !== id);
    }
    renderReportsList();
    renderCompare();
  }

  function figureListHtml(report) {
    const figures = report.figures || [];
    const pngs = figures.filter((f) => f.endsWith(".png"));
    if (!pngs.length) return `<div class="validate-report-no-figures">（這次執行沒有產生圖表）</div>`;
    return `<div class="validate-report-figures">` + pngs.map((png) => {
      const pdf = figures.find((f) => f.endsWith(".pdf") && f.slice(0, -4) === png.slice(0, -4));
      const pngUrl = `/verify/reports/${report.id}/${png}`;
      return `
        <figure class="validate-figure">
          <img src="${pngUrl}" alt="${png}" loading="lazy">
          <figcaption>
            ${png}
            ${pdf ? `<a href="/verify/reports/${report.id}/${pdf}" download>下載 PDF（向量圖，供排版用）</a>` : ""}
          </figcaption>
        </figure>
      `;
    }).join("") + `</div>`;
  }

  function renderCompare() {
    if (!selectedIds.length) {
      compareAreaEl.innerHTML = `<div class="validate-compare-empty">勾選上面的報告以檢視（最多 2 份並排比較）</div>`;
      return;
    }
    compareAreaEl.innerHTML = selectedIds.map((id) => {
      const report = allReports.find((r) => r.id === id);
      if (!report) return "";
      return `
        <div class="validate-compare-panel">
          <div class="validate-compare-head mono">${id}　${fmtReportLabel(report)}</div>
          <iframe class="validate-compare-iframe" src="${report.html_url}" title="report ${id}"></iframe>
          ${figureListHtml(report)}
        </div>
      `;
    }).join("");
  }

  async function refreshReports() {
    try {
      const res = await fetch("/verify/reports");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const reports = await res.json();
      // 依日期新到舊排序 (C23.md 驗收條件) -- 後端理論上已經排好，這裡防
      // 禦性地自己再排一次，不假設對方的順序永遠不會變。
      allReports = [...reports].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
      selectedIds = selectedIds.filter((id) => allReports.some((r) => r.id === id));
      reportsErrorEl.style.display = "none";
      renderReportsList();
      renderCompare();
    } catch (err) {
      reportsErrorEl.textContent = "尚未串接（/verify/reports 還沒上線）";
      reportsErrorEl.style.display = "block";
      console.warn("[validate] /verify/reports unavailable:", err.message);
    }
  }

  return {
    init(root) {
      root.innerHTML = `
        <div class="section-label">驗證模式 · 五項物理驗證實驗</div>

        <div class="validate-run-controls">
          <label class="validate-control-label">Session 檔案（一行一個，或逗號分隔）
            <textarea class="validate-sessions-input mono" data-sessions-input rows="2"
              placeholder="sessions/2026-09-01_S01.h5"></textarea>
          </label>
          <div class="validate-run-row">
            <label><input type="checkbox" data-fast-checkbox> --fast（降低 PCA 維度，換取速度）</label>
            <label><input type="checkbox" data-real-checkbox> --real（標示為真實資料，非合成）</label>
            <button class="validate-btn validate-run-btn" data-run-btn>▶ 執行 A / B / C / E</button>
          </div>
          <div class="validate-run-status mono" data-run-status>尚未執行過</div>
          <div class="validate-run-error" data-run-error style="display:none"></div>
        </div>

        <div class="validate-cards" data-cards></div>

        <div class="section-label">報告檢視器</div>
        <div class="validate-reports-error" data-reports-error style="display:none"></div>
        <div class="validate-reports-list" data-reports-list></div>
        <div class="validate-compare-area" data-compare-area></div>
      `;

      cardsEl = root.querySelector("[data-cards]");
      sessionsInputEl = root.querySelector("[data-sessions-input]");
      fastCheckboxEl = root.querySelector("[data-fast-checkbox]");
      realCheckboxEl = root.querySelector("[data-real-checkbox]");
      runBtn = root.querySelector("[data-run-btn]");
      runStatusEl = root.querySelector("[data-run-status]");
      runErrorEl = root.querySelector("[data-run-error]");

      runBtn.addEventListener("click", onRunClick);

      reportsListEl = root.querySelector("[data-reports-list]");
      compareAreaEl = root.querySelector("[data-compare-area]");
      reportsErrorEl = root.querySelector("[data-reports-error]");

      renderCards();
      refreshState();
      renderReportsList();
      renderCompare();
      refreshReports();
    },

    onData() {
      // No SSE event type is relevant here -- /verify/state is polled
      // explicitly while a run is in flight (see startPolling()), same
      // reasoning as C16-C19's manual-trigger pattern: nothing publishes a
      // "verification progress" SSE event today, so there's nothing to
      // subscribe to.
    },
  };
})());
