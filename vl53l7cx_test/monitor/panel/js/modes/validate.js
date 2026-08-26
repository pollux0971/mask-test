// Validate mode (C22): guided physical-verification experiment runner.
//
// Five experiments (C0/A/B/C/E) as defined by D15's analysis.run_all --
// this file is UI orchestration only, it does NOT reimplement any analysis
// math (C22.md's own "不包含: 分析演算法 D10-D14" boundary). Every PASS/FAIL
// number shown here comes verbatim from the backend's report matrix.
//
// Backend wiring status (re-confirmed live during FULL_FLOW_REHEARSAL /
// validate-mode rework, 2026-08-26): `/verify/run`, `/verify/state`,
// `/verify/reports`, `/verify/reports/<id>/<path>` are all fully wired in
// bridge_server.py now. Every fetch below still keeps a network-failure
// fallback (real "can't reach the bridge" case), but the old blanket
// "尚未串接" text for any non-ok response was wrong once the backend went
// live -- it hid real rejections (e.g. bad session path, already-running)
// behind stale "not wired yet" copy. Fixed to show the backend's actual
// error message instead.
//
// C0 (串擾) is a full member of the five main experiments run by
// analysis/run_all.py's run_crosstalk() -- and one of the three must-pass
// keys (verification_report.py's MUST_PASS). It scores off whichever
// session file(s) you hand to /verify/run, same as A/B/C/E, PROVIDED that
// session's per-frame sensors_enabled actually varies over time. This
// file's C0 card does the part a human can't reliably do by hand --
// correctly-ordered, correctly-timed sensor toggling via B18's /sensor
// endpoint -- but it does not itself start or own a recording; the user
// still needs a record-mode session running alongside it to produce a file
// worth handing to /verify/run.

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
  let blockingEl = null;
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
    // CONTRACTS 沒明講但實測確認：`lastRun.matrix` 的每一列只有
    // {key,name,metric,measured,criterion,status,mark,must_pass} -- 沒有
    // `reason`/`diagnosis`。這兩個欄位只在平行的 `lastRun.outcomes`
    // （`ExperimentOutcome.to_dict()`）裡。原本這裡讀 `matrix`，
    // 導致下面這條「skipped/error 一定要有說明」的規矩從寫出來那天起
    // 就沒有真的生效過 -- reason 永遠是 undefined。
    const outcome = lastRun && Array.isArray(lastRun.outcomes)
      ? lastRun.outcomes.find((o) => o.key === key) : null;
    if (!outcome) {
      return `<span class="validate-status validate-status-none">尚無結果</span>`;
    }
    const cls = STATUS_CLASS[outcome.status] || "validate-status-none";
    const label = STATUS_LABEL[outcome.status] || outcome.status;
    const measured = outcome.measured && outcome.measured !== "—" ? ` · ${outcome.measured}` : "";
    return `<span class="validate-status ${cls}">${label}${measured}</span>` +
      // skipped/error 一定要有說明，不能是沒有解釋的灰格子 (esp-mask-test-ad
      // 的明確要求，見完成回報)
      (outcome.reason ? `<div class="validate-reason">${outcome.reason}</div>` : "") +
      diagnosisHtml(outcome);
  }

  // `diagnosis` 一直存在於後端回應裡，但先前完全沒有被顯示過。內容依
  // status 分兩種完全不同的東西（都讀 run_all.py 原始碼確認過）：FAIL
  // 時是給人看的建議句子（例如「先懷疑維度詛咒，不要先懷疑資料」），
  // ERROR 時是 `_errored()` 塞進去的完整 Python traceback。兩者混用同一種
  // 「💡 提示」外觀會誤導人（traceback 不是建議，是程式炸了的證據）—— 前者
  // 直接顯示，後者摺起來、換一個不暗示「這是貼心提醒」的標籤，避免口試
  // 現場沒摺好的 stack trace 佔滿投影幕。
  function diagnosisHtml(outcome) {
    if (!outcome.diagnosis) return "";
    if (outcome.status === "error") {
      return `<details class="validate-diagnosis validate-diagnosis-error">` +
        `<summary>⚠ 詳細錯誤（工程除錯用）</summary>${outcome.diagnosis}</details>`;
    }
    return `<div class="validate-diagnosis">💡 ${outcome.diagnosis}</div>`;
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
      // C25: synthetic-data caveat uses the shared .caveat-badge look
      // (tokens.css), same amber language as monitor.css's "ASSUMED,
      // unverified" -- innerHTML here (not textContent) only to wrap that
      // one span; every other value stays plain text.
      runStatusEl.innerHTML = `上次執行：${lastRun.finished_at || "?"}（耗時 ${
        lastRun.elapsed_s != null ? lastRun.elapsed_s.toFixed(1) : "?"} 秒）` +
        (lastRun.is_synthetic ? `　<span class="caveat-badge">⚠ 合成資料</span>` : "");
    } else {
      runStatusEl.textContent = "尚未執行過";
    }
  }

  function blockingKeyLabel(key) {
    const exp = EXPERIMENTS.find((e) => e.key === key);
    return exp ? `${key}（${exp.name}）` : key;
  }

  // 🔴 esp-mask-test-ad 明確要求提到最上面，不是藏在某張卡片裡：C0 是
  // must-pass，C0 沒過代表下游每個數字都可能被汙染 -- 這是「這份報告能不能
  // 信」的開關，跟其他補充資訊不是同一個重要度。must-pass 的清單直接從
  // 這輪結果自己的 outcomes[].is_must_pass 算，不寫死 key 列表，避免跟
  // verification_report.py 的 MUST_PASS 之後改了卻沒人發現兜不起來。
  function renderBlocking() {
    if (!blockingEl) return;
    if (!lastRun) {
      blockingEl.style.display = "none";
      return;
    }
    const blocking = lastRun.blocking || [];
    const mustPassKeys = Array.isArray(lastRun.outcomes)
      ? lastRun.outcomes.filter((o) => o.is_must_pass).map((o) => o.key)
      : [];
    blockingEl.style.display = "block";
    if (blocking.length) {
      blockingEl.className = "validate-blocking validate-blocking-blocked";
      blockingEl.innerHTML =
        `🔴 這份報告的結論暫不可信：` +
        blocking.map(blockingKeyLabel).join("、") +
        ` 是必須通過（must-pass）的項目但沒有通過 -- 下游其他實驗的數字` +
        `可能已被汙染，不能只看綠燈就採信。`;
    } else if (mustPassKeys.length) {
      blockingEl.className = "validate-blocking validate-blocking-clear";
      blockingEl.textContent =
        `✅ must-pass 項目（${mustPassKeys.map(blockingKeyLabel).join("、")}）都通過，` +
        `其餘實驗的數字沒有被結構性汙染的已知理由。`;
    } else {
      blockingEl.style.display = "none";
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
      renderBlocking();
      renderRunStatus();
      if (!running && pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      // A run just finished -- the reports list (C23) has a new entry now,
      // refresh it so it shows up without the user reloading the page.
      if (wasRunning && !running) refreshReports();
    } catch (err) {
      // /verify/state is documented to always return 200 ("沒有在跑" 不是
      // 錯誤) -- so landing here now means a real network failure, not
      // "endpoint doesn't exist yet".
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      running = false;
      runStatusEl.textContent = "連不上後端（" + err.message + "）";
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
    let res;
    try {
      res = await fetch("/verify/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sessions,
          fast: fastCheckboxEl.checked,
          real: realCheckboxEl.checked,
        }),
      });
    } catch (err) {
      // Real network failure -- can't reach the bridge at all.
      runStatusEl.textContent = "連不上後端（" + err.message + "）";
      console.warn("[validate] /verify/run network error:", err.message);
      return;
    }
    if (res.status === 409) {
      const body = await res.json().catch(() => ({}));
      runErrorEl.textContent = body.error || "已經有一輪驗證在執行中，請等它結束";
      runErrorEl.style.display = "block";
      return;
    }
    if (!res.ok) {
      // /verify/run is live -- a non-ok status here is a real rejection
      // (e.g. "sessions 必須是非空的陣列"), not "not wired yet".
      const body = await res.json().catch(() => ({}));
      runErrorEl.textContent = body.error || `送出失敗：HTTP ${res.status}`;
      runErrorEl.style.display = "block";
      return;
    }
    running = true;
    runStartedMs = performance.now();
    renderRunStatus();
    startPolling();
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
        // C0 其實已經是 /verify/run 五個主實驗之一（analysis/run_all.py
        // 的 run_crosstalk()），而且是三個 must-pass 之一 -- 不是「尚未
        // 串接」，是這個精靈本身不負責錄音。這個精靈只做「依序、正確地
        // 切換兩顆感測器開關」這個真人很難自己計時計準的動作；要讓
        // /verify/run 評得出 C0，切換期間要有一個 record 模式的 session
        // 正在錄音（讓 sensors_enabled 在同一個 session 檔案裡隨時間變化）。
        renderC0Progress("裝置狀態已依序切換完成（三種感測器組態，各 30 秒）。");
        c0ResultEl.className = "validate-c0-result validate-c0-result-pending";
        c0ResultEl.textContent =
          "這個精靈本身不會產生 session 檔案，只負責切換感測器開關。" +
          "若剛才切換期間有另開 record 模式錄音，把錄好的 session 路徑貼到" +
          "上面「Session 檔案」欄位、按「執行 A / B / C / E」，C0（串擾）" +
          "會跟其他四項一起被評出 PASS / FAIL（它是 must-pass 項目）。";
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
    // 實測確認 GET /verify/reports 回的是
    // {run_id, modified_at, has_summary, files}（bridge_server.py
    // list_verify_runs()），不是原本猜的 {id, created_at, is_synthetic,
    // elapsed_s, figures, sessions}。那個猜測形狀在這輪之前從沒被後端
    // 滿足過，所以下面每一處都直接對應成真正存在的欄位，不再假裝有
    // is_synthetic/elapsed_s/sessions/figures 這幾個這個端點沒給的東西
    // （已回報調度員：這是「圖表看得到」需求現在做不到的根本原因）。
    return `${report.modified_at || report.run_id}　(${
      report.has_summary ? "有摘要" : "⚠ 無 summary.md"})`;
  }

  function renderReportsList() {
    if (!allReports.length) {
      reportsListEl.innerHTML = `<div class="validate-reports-empty">尚無歷史報告</div>`;
      return;
    }
    reportsListEl.innerHTML = allReports.map((r) => `
      <label class="validate-report-row${selectedIds.includes(r.run_id) ? " selected" : ""}">
        <input type="checkbox" data-report-checkbox value="${r.run_id}"
          ${selectedIds.includes(r.run_id) ? "checked" : ""}>
        <span class="mono validate-report-id">${r.run_id}</span>
        <span class="validate-report-meta">${fmtReportLabel(r)}</span>
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

  // figureListHtml() 拿掉了：GET /verify/reports 目前只列出報告根目錄的
  // 檔案（見 bridge_server.py list_verify_runs() 的 entry.iterdir()，
  // 只掃一層），從來沒有列過 figures/ 子目錄底下的檔名，而 write_outputs()
  // 確認圖一律寫進 figures/ 子目錄 -- 前端沒有任何管道知道圖檔叫什麼名字。
  // 猜檔名（例如照實驗 slug 拼）是原本這裡的註解自己講過要避免的做法：
  // D15 之後改圖檔命名，這裡會靜靜地全部連不到。已回報調度員：這是
  // 「圖表看得到」需求現在做不到的根本原因，需要後端補一個 figures 清單。

  function renderCompare() {
    if (!selectedIds.length) {
      compareAreaEl.innerHTML = `<div class="validate-compare-empty">勾選上面的報告以檢視（最多 2 份並排比較）</div>`;
      return;
    }
    compareAreaEl.innerHTML = selectedIds.map((id) => {
      const report = allReports.find((r) => r.run_id === id);
      if (!report) return "";
      const htmlUrl = report.has_summary ? `/verify/reports/${report.run_id}/summary.html` : null;
      return `
        <div class="validate-compare-panel">
          <div class="validate-compare-head mono">${report.run_id}　${fmtReportLabel(report)}</div>
          ${htmlUrl
            ? `<iframe class="validate-compare-iframe" src="${htmlUrl}" title="report ${report.run_id}"></iframe>`
            : `<div class="pending-note">這一輪沒有 summary.html（可能執行到一半失敗）。</div>`}
          <div class="pending-note">圖表清單目前後端沒有提供（/verify/reports 只列出報告根目錄的檔案，不含 figures/ 子目錄），已回報調度員追加端點。</div>
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
      // 禦性地自己再排一次，不假設對方的順序永遠不會變。欄位是
      // modified_at，不是原本猜的 created_at。
      allReports = [...reports].sort((a, b) => (b.modified_at || "").localeCompare(a.modified_at || ""));
      selectedIds = selectedIds.filter((id) => allReports.some((r) => r.run_id === id));
      reportsErrorEl.style.display = "none";
      renderReportsList();
      renderCompare();
    } catch (err) {
      // /verify/reports is live -- landing here is a real network failure.
      reportsErrorEl.textContent = "連不上後端（" + err.message + "）";
      reportsErrorEl.style.display = "block";
      console.warn("[validate] /verify/reports unavailable:", err.message);
    }
  }

  return {
    init(root) {
      root.innerHTML = `
        <div class="section-label">驗證模式 · 五項物理驗證實驗</div>

        <div class="validate-blocking" data-blocking style="display:none"></div>

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

      blockingEl = root.querySelector("[data-blocking]");
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
      renderBlocking();
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
