# 前端整合驗證報告

> 由 `esp-mask-test-59`（負責 C 前端軌 C01–C10、C15–C18）執行，2026-08-26。
> 目的：C05 之後從沒有人把五個模式一起載入、跑一輪，確認它們真的能共存。
> 測試方式：`ssi-backlog/tools/mock_device.py --mel 1 --scenario round`（proto v2，
> 有 `$F`）+ `vl53l7cx_test/monitor/bridge_server.py`（連到 mock 的 pty）+
> headless Chrome（CDP 自動化，`websockets` 直連 `9222` 系協定埠）。
> 全程 4 個真正註冊的模式（`monitor`/`record`/`quiz`/`replay`）+ 1 個尚未接上的
> 空白模式（`validate`，等 `C22`）。測完所有測試程序都已用明確 PID kill 掉，
> 沒有動到其他 agent 自己在跑的 `mock_device.py`/`bridge_server.py`。

## 結論摘要

| 項目 | 結果 |
|---|---|
| 五模式共存、資料真的流進來、console 乾淨 | ✅ 通過 |
| 切模式再切回，狀態保留（沒有被重新 `init`） | ✅ 通過 |
| 一個模式拋例外，其他模式仍收得到事件（故障隔離） | ✅ 通過（見下） |
| 已知降級路徑（404 端點）優雅降級 | ✅ 通過 |
| idle CPU < 15%（`C03` 驗收條件） | ⚠️ **視哪個模式可見而定** —— `monitor` 可見時 ~31%，其餘三個 11–13% |

---

## 1. 五模式共存 + 資料流 + console 乾淨

**跑法**：`mock_device.py --mel 1 --scenario round`（非 idle，逼真實資料變動）
接到 `bridge_server.py`，瀏覽器開 panel 首頁（預設 `monitor`）。

- 頁面載入完成，`console_errors_on_load` 為空陣列
- `monitor` 的 `[data-rate="A"]` / `[data-rate="B"]` 顯示 `30.0 Hz`，1 秒後
  再讀一次仍是即時數字（不是卡死的靜態文字）—— 證明 `$T` 真的在流動且
  即時渲染，不只是畫面存在
- 逐一切到 `record`/`quiz`/`replay`（用側欄按鈕，不是改 hash），
  **每次切換後 console 都是空的**，沒有任何 warning/error/exception
- `validate`：切過去後 `#mode-validate` 的 `innerHTML` 是空字串，
  **且沒有任何例外**。這是預期行為，不是 bug —— `main.js` 目前只
  `import` 了 `monitor.js`/`record.js`/`quiz.js`/`replay.js`
  （見檔案本身的註解 `(C22 validate 待補)`），`validate.js` 還沒被寫、
  `registerMode("validate", …)` 從未被呼叫，`shell.js` 對「沒有 hooks
  的模式」本來就只是留空白 section，不會報錯

## 2. 狀態保留（切走再切回）

用 `C05` 驗證時用過的手法：在每個模式的 root section 上蓋一個帶隨機值的
`data-integ-mark` 屬性（`el.setAttribute(...)`，只在我自己起的 Chrome tab
上操作，沒有動到任何原始碼），依序切過 `record → quiz → validate → replay`
之後再切回每一個，讀回這個屬性。

**四個真正模式的 mark 全部原封不動地存活**（`sentinel-monitor-xxx` 等，
逐一比對前後字串完全相同）—— 證明 `shell.js` 的 mode 生命週期真的是
「只切 `.active` class，不重新 `init()` DOM」，這正是 `README.md`「架構關鍵：
資料層與模式層分離」要保證的行為，在四個真正模式間確認過了。

## 3. 故障隔離（一個模式拋例外，其他模式仍收得到事件）

背景：`esp-mask-test-18` 在做 `C24` 時，用真實 trial 事件測出
`bus.js`/`shell.js` 舊版 `forEachRegisteredMode` 用一般 `forEach`，一個模式的
`onData` 拋例外會讓陣列裡排在後面的模式完全收不到那個事件（`record` 排第二，
炸了會讓 `quiz`/`validate`/`replay` 三個全部失明）。已修好，並留了一頁
專用回歸測試：`vl53l7cx_test/monitor/panel/js/test_fault_isolation.html`。

**驗證方式一：跑那頁回歸測試**（`http://<bridge>/panel/js/test_fault_isolation.html`）：

```
result: PASS — 'monitor' 拋例外沒有擋住其他模式收到事件
received by: ["record","quiz","validate","replay"]
console.error mentioned the failing mode by name: true
```

`monitor` 排 `MODES` 第一個，刻意讓它的 `onData` 拋例外，其餘四個（含
`validate`，即使沒有真實內容也在 hooks 陣列裡）**全部收到事件**，且
`console.error` 有明確指名是哪個 mode 壞的。**這是最權威的一份證據**，
因為它不依賴任何真實模式檔案的內部邏輯，只測 `forEachRegisteredMode`
這個五行迴圈本身。

**驗證方式二：對正在跑的真實 app 注入事件**，嘗試重現 `esp-mask-test-18`
原本抓到的那個 `record.js` SAVE 事件真 bug（用 `import('/panel/js/bus.js')`
拿到跟 `main.js` 共用的同一個模組實例，呼叫其匯出的 `handleEvent()` —— 這是
`C16`/`C17` 就用過的既有測試手法，不是新發明的）：

- 送出 `PROMPT → COUNTDOWN → CAPTURE → SAVE` 完整序列（`type:"trial"`，
  帶 `idx`/`label`/`seed`/`quality`/`n_frames`/`valid_zone_ratio`/`drop_count`）
- **沒有拋出例外**（`sequence_result: "no_throw"`），console 也沒有任何
  `[bus] mode "record" 的 handler 拋出例外` 訊息
- `monitor` 的 Hz 讀數在注入前後持續正常更新，`quiz` 的卡片數維持 9 個不變

**這代表 `record.js` 那個 SAVE 事件的真 bug 目前看起來已經不會炸了**
（可能 `esp-mask-test-4f` 在做 `C13`/`C14` 時順便修掉了，也可能我這個
最小化的合成事件沒踩到真正的觸發條件——我只補了 `onTrialEvent`/
`finalizeSavedTrial` 讀到的欄位，沒有驅動完整的 session/baseline 流程）。
**沒有轉派給誰**，因為沒有觀察到失敗；如果之後真的又炸了，方式一的
回歸測試頁會抓到。

## 4. 降級路徑（404 端點）

現在還是 404 的端點：`/recognize`（`D09`，`quiz.js` 已在 `C16`/`C17`/`C18`
測過會顯示「尚未串接」+ `console.warn`，不拋例外）、
`/replay/sessions`（`replay.js` 顯示 `console.warn: [replay] /replay/sessions
unavailable: HTTP 404`，不拋例外）。

**其餘幾個原本以為是 404 的端點，這輪測到已經不是了**（值得記一筆，免得
`E01` 當天照舊清單去查會撲空）：`/pca` → `204`、`/baseline` → `204`、
`/session/prefill` → `200`。這幾個 `monitor.js`/`record.js` 拿到之後
完全沒有任何 console 輸出——降級是靜默且正確的，不是沒處理。

## 5. CPU 實測（`C03` 驗收條件：idle CPU < 15%）

**方法**：headless Chrome（`--headless=new --disable-gpu`），單一分頁，
對該 Chrome instance 底下所有 process（renderer/gpu/zygote，排除
crashpad-handler）加總 `/proc/[pid]/stat` 的 `utime+stime`，取 8 秒視窗算
`%CPU`（100% = 佔滿一個核心，跟 `top`/`ps` 同一種算法，`C03`/`C10` 之前
應該也是這樣量的，所以可以直接對照那兩個舊數字）。

| 狀態 | CPU % |
|---|---|
| headless Chrome 本身開銷（空白分頁，無 panel） | 0.25% |
| **`monitor` 可見**（idle，真實資料持續流入） | **30.65%** |
| `quiz` 可見 | 12.97% |
| `record` 可見 | 11.48% |
| `replay` 可見 | 11.97% |

⚠️ **`monitor` 可見時超過 `C03` 的 15% 預算，大約是兩倍。**
其餘三個模式都在預算內。

**架構上一個好消息，順便驗到了**：`quiz`/`record`/`replay` 可見時的數字
彼此相近（11–13%），代表 `monitor` 隱藏起來以後**沒有**繼續在背景跑
canvas 動畫迴圈燒 CPU——如果它有這個問題，其他三個模式的數字應該會
被拉高到接近 30%，但沒有。真正燒 CPU 的是 `monitor` **本身可見時**的
熱力圖格子 + PCA 散點圖 + Mel 瀑布圖三個 canvas 同時重繪，不是背景洩漏。

**這個數字要轉給 `monitor.js` 的負責人**（目前 `esp-mask-test-7c` 正在
那個檔案抽 `C08` 的共用繪製函式到 `js/draw/thumbnails.js`，抽完之後
`C03`/`C10` 的舊數字（1.92% 框架 / 12.25% 有真內容）跟這次的 30.65% 混在
一起看，成長主要來自 `C08`（Mel 瀑布圖）+ `C10`（PCA 散點圖）都是持續重繪
的 canvas，兩個疊加。**不是我的檔案，我沒有動手改**，只回報數字。

⚠️ **方法論警告**：這是 headless（`--disable-gpu`，軟體合成）量出來的
數字，跟真的 Demo 筆電（有 GPU 加速）的實際負載未必是同一個絕對值——
但 `blank-tab baseline` 只有 0.25%，代表 headless 本身開銷可以忽略，
量出來的 30.65% 幾乎全部是 panel 自己的工作量，**相對比較（哪個模式重、
重多少）應該是可信的**，只是這個絕對數字上 Demo 現場的真筆電前務必
**再量一次**（`chrome://version` 開 Task Manager 看真實 GPU 加速下的數字）。

## 6. 沒有動到的檔案

只讀取、沒有修改：`record.js`、`quiz.js`、`replay.js`、`monitor.js`、
`shell.js`、`bus.js`。所有 DOM 操作（`data-integ-mark` 標記、`handleEvent()`
注入）都是透過瀏覽器 console/CDP 對執行中的頁面做的，沒有寫回任何原始碼檔。

## 7. `E01` 當天前端檢查步驟（給 `reports/E01_bringup_checklist.md` 參考合併）

上機測試那天，除了硬體相關項目，前端這邊建議照下面順序走一遍：

- [ ] 五個 nav 項目全部點過一輪（含 `validate`——目前應該是空白，若 `C22`
      屆時已完成則應該有內容，不再是空白）
- [ ] 每個模式停留至少 5 秒，開瀏覽器 DevTools console，確認**沒有任何
      未預期的 error/exception**（`[replay] /replay/sessions unavailable`
      這類 `console.warn` 是預期中的降級訊息，不算異常）
- [ ] 切走 `monitor` 到別的模式，等 30 秒，切回來確認 `baseline`/PCA/Mel
      瀑布圖等狀態還在，沒有被重置
- [ ] 開瀏覽器工作管理員（`Shift+Esc`）或 `chrome://version` 頁面附近的
      Task Manager，量一次 `monitor` 可見時的真實 CPU/GPU 使用率，
      對照本報告第 5 節的 headless 數字，確認真筆電上沒有更糟
- [ ] 真的斷開 `/recognize`（`D09` 若當天沒上線）、確認 `quiz` 模式的
      「觸發辨識」按鈕點下去只顯示「尚未串接」，UI 不卡死
- [ ] 開 `vl53l7cx_test/monitor/panel/js/test_fault_isolation.html`，
      確認還是 PASS（這頁不需要真的裝置或 bridge，隨時可以單獨開）
