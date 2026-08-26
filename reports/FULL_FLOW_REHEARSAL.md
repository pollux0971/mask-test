# 端到端整合排練：錄音 → 體檢 → 建樣板 → 辨識 → 測驗模式

> ✅⚠️ **更新（2026-08-26 下午）：擋住整條路的換詞 bug 已修，
> 但本報告的狀態是「待重跑」，不是「已完成」。**
> 自己讀 `host/trial/state_machine.py` 確認：`hold_stop()`（line 494）
> 現在有 `self._order_pos += 1`，附了一段完整解釋這正是這裡踩到的那個
> bug（單元測試沒抓到，因為沒有斷言連續存兩筆之間詞真的換了）。
> **順帶多驗證到一個你訊息沒提的修復**：`quiz.js` 的 `/recognize`
> 錯誤處理（步驟 5 那個第 6 案例）也已經修好——現在會顯示
> `body.error`/真實狀態碼，不再一律顯示「還沒上線」。
> 🔴 **但這份報告記錄的「單一詞、16 筆」資料集本身，是換詞 bug 造成的
> 限制產物**——blocker 消失後，步驟 5/6（辨識、拒識）需要**用真的多詞
> 資料集重新走一次**才能算驗證過，不是把這份報告的結論直接當「已驗證
> 通過」。以下原文保留不刪，第 240 節表格已更新狀態欄並標註待重跑。

> 由 `esp-mask-test-ca`（本輪對外自稱 `59`，實際 session 名稱以此為準）執行，2026-08-26。
> 目標：假裝自己是使用者，**全程用面板**走一次 `錄音 → 體檢 → 建樣板 → 辨識 → 測驗模式看結果`，
> 因為這是這條路第一次全部接通、卻**沒有人從頭到尾走過一次**。
>
> **結論先講**：**第一步（錄音）就撞到一個會擋住後面所有步驟的真實 bug**——
> trial 提示的詞完全不會換，連「跳過這詞」都沒用。已第一時間回報給調度員，
> 已指派給 `4f`（`state_machine.py`/`record.js` 的擁有者）。**沒有繞過去**：
> 剩下的步驟照樣往下走，但用的是這個 bug 產生的「單一詞、16 筆」資料集，
> 每一步凡是因為這個資料集不完整而測不出東西的，都在下面明確標記，
> 不假裝驗證過。

## 結論摘要

| 步驟 | 結果 |
|---|---|
| 1. 錄音（開 session、baseline、錄 trial） | 🔴 **卡住**：trial 詞完全不會換，回報並指派給 `4f` |
| 2. `first_session_check.py` 體檢 | ✅ 腳本本身沒問題，輸出清楚；順帶印證了 bug（`四: 16/72`，只有一個詞） |
| 3. `build_templates_from_session.py` 建樣板 | ✅ 腳本行為正確、訊息清楚；因資料集缺 `_reject`，**明確拒絕**產出可用樣板 |
| 4. 重啟 bridge 指向新樣板 | ✅ 不需要重啟——`--templates-dir` 指到的目錄**熱載入**；`GET /templates` 給出精確原因 |
| 5. `quiz` 模式按「觸發辨識」 | 🔴 **後端正確拒絕（503 + 清楚原因），前端顯示錯**——第 6 個「前端沒跟上」案例 |
| 6. 用沒錄進樣板的詞測拒識 | ⛔ **無法測試**——沒有 `_reject` 樣板，`RecognitionService` 根本沒被建出來，第 5 步已經證實 |

---

## 步驟 1 🔴 錄音：trial 詞完全不會換（卡住的地方）

**環境**：mock_device + bridge_server（`--sessions-dir`/`--templates-dir` 都指到我自己的
scratchpad，`--http-port 8990`），headless Chrome 全程用 CDP 的
`Input.dispatchKeyEvent` 送**真的鍵盤事件**（不是 JS 合成事件）。

**流程**：填表單（subject=fullflow01, mode=quiz, distance=300, angle=0,
ambient=quiet）→ 送出 → **在同一個瀏覽器分頁裡耐心等 baseline 完成**（這是
之前 `PANEL_KEYBOARD.md` 抓到的 `BASELINE_WAIT_GRACE_MS` bug 卡住的那個
地方——**這次 32 秒內順利進到 trial 畫面，確認那個 bug 真的修好了**）。

**接著發生的事**：對 trial 畫面做了 10 次、又 20 次真實的 Space 長按
（每次按住 1 秒，符合 SAVE 的 0.3–5 秒視窗），**存了 16 筆，但題目全程
都是「四」**，一次都沒換過，遠遠超過表單設定的 `target-per-label=4`
（見下方附帶發現），甚至超過寫死在 `record.js` 裡的預設值 8。

**再測一次，用「跳過」而不是「存檔」**：讀畫面上的詞（「四」）→ 送出一次
真實的 Escape keydown/keyup（照 `record.js` 自己的說明跟 `CONTRACTS.md`，
這應該呼叫 `triggerAbort()`，把詞往下推一個）→ 等 1.5 秒 → 再讀一次畫面上
的詞——**還是「四」**。

**排查到這裡確認不是我測試腳本的問題**：
- `.recent-item` 清單真的從 5 筆長到 16+ 筆，代表 SAVE 真的有落盤成功，
  不是卡住沒反應
- 讀 `record.js` 原始碼：畫面上顯示的詞（`trialLabel`/`trialNextLabel`）
  直接來自後端 SSE 事件的 `evt.label`/`evt.next_label`（line 1086/1089）
  ——**選字邏輯根本不在 `record.js` 裡**
- 讀 `host/trial/state_machine.py`：`_order_pos`（決定下一個詞的位置）
  在 `_do_save()`（line 504）跟 abort（line 568）都有 `+= 1`，**而且這兩個
  行為都各自有專門的單元測試在 `test_state_machine.py`**（例如
  `test_abort_advances_next_label_redo_keeps_it`），單獨看程式碼跟測試，
  邏輯應該是對的
- 讀 `bridge_server.py`：`TrialStateMachine` 整個 session 只建一次
  （line 960），不是每個 request 重建一次，排除「狀態被重置」這個簡單
  原因

**卡在**：單獨看 `state_machine.py` 的邏輯是對的，但整合起來（透過
`bridge_server.py` 的 SAVE/abort handler → SSE → `record.js` 顯示）觀察到
的行為是「一直不換」。**沒有再往下挖**（`_do_save`/`hold_stop` handler
內部細節），因為這已經跨出這輪授權範圍（不能改 `record.js`，
`bridge_server.py`/`state_machine.py` 也不是這輪該動的檔案），繼續挖只是
在猜。**已經回報給調度員，調度員已指派給 `4f`。**

⚠️ **附帶發現，一併回報**：表單上的「每詞目標筆數」欄位（`data-target-
per-label`）填了 4，但錄音行為一直用預設值 8（甚至 16 之後還沒換，這欄位
看起來根本沒接上選字邏輯）。**調度員判斷這跟換詞 bug 可能同一個根因**
（表單設定沒真的傳到選字邏輯），已經一起交給 `4f` 優先查。

**這一步對真人使用者的意義**：如果這個 bug 不修，**使用者照著介面操作，
一整個小時只會錄到同一個詞**，完全不會發現（畫面上「已存 X 筆」的計數會
正常增加，`.recent-item` 清單會正常變長，唯一的破綻只有「怎麼題目一直
一樣」——**沒有任何錯誤訊息或警告會提醒使用者這是不正常的**，這是
最需要優先修的地方。

## 步驟 2 ✅ `first_session_check.py` 體檢

```
.venv/bin/python3 ssi-backlog/tools/first_session_check.py <session.h5>
```

**參數好不好懂**：位置參數 + 一個可選的 `--target-count`，`--help` 正常
可用。**對使用者友善**，不需要事先知道任何隱藏規則。

**實際輸出**（節錄）：

```
✅ 資料看起來可用，可以繼續錄
- baseline 位置正常（trial_000），另有 16 筆真實錄音的 trial
- 感測器 A/B 平均無效 zone 比例：7.6%（僅供參考，不代表失敗）
- baseline 實測底噪 RMS ≈ 401.3（σ=229.2）——目前門檻是拿模擬器訂的，
  跳黃/紅燈不代表裝置有問題
- VAD 有值的 trial：14/16
- comparable=True 的 trial：5/16
- 錄製進度：四: 16/72
```

**做得好的地方**：每個數字後面都附一句「這個門檻是怎麼來的、能不能當真」
的說明（例如明講 `noise_floor` 門檻是拿模擬器訂的，不是量真麥克風），
**這對一個緊張、戴著裝置的使用者非常重要**——不會因為看到黃燈紅燈就
自己嚇自己。也**間接印證了步驟 1 的 bug**：進度那行清清楚楚只列出
「四: 16/72」，沒有其他詞，跟我觀察到的現象一致。

沒有發現這支腳本本身的問題。

## 步驟 3 ✅ `build_templates_from_session.py` 建樣板

```
python3 -m analysis.similarity.build_templates_from_session \
  --session <session.h5> --out <out.npz> --subject <subject> --wear-id <N>
```

⚠️ **不好猜的地方，寫下來給使用者提醒**：
- **必須用 `python3 -m analysis.similarity.build_templates_from_session`
  這個模組寫法**，直接 `python3 build_templates_from_session.py` 會報
  `ModuleNotFoundError: No module named 'analysis'`——這個坑本輪之前已經
  踩過並回報過，這裡再次確認同樣的坑還在，**建議在腳本的 usage 或
  README 裡明講「要用 `-m` 從 repo 根目錄執行」**，不然使用者第一次
  一定會撞到。
- `--subject`/`--wear-id` 要跟 session 檔案 `/meta` 裡的值一致，這點
  腳本沒有主動檢查提示，是我自己另外開 `h5py` 讀出來對的（`subject:
  fullflow01`, `wear_id: 2`）——**如果使用者記錯自己 session 錄音時填的
  subject/wear_id，這裡沒有防呆**。

**實際輸出**（用步驟 1 那個「單一詞、16 筆」資料集）：

```
[build_templates] 跳過 1 筆：trial_000: 沒有 mel/mel_t_us（選填欄位，
                   這筆錄音當下 Mel 未開啟），無法組樣板
[build_templates] 各類別樣板數：
  四: 16
  ⚠ 沒有任何 _reject（靜止／其他）樣板——RecognitionService 需要它校準
    拒識門檻（D22 雙邊 ROC），缺少的話這批樣板無法拿去建構
    RecognitionService。
[build_templates] 存好：<out.npz>
[build_templates] 樣板來源記錄：<out>.provenance.json
```

**這支腳本的行為是對的、訊息也很清楚**：它沒有因為缺 `_reject` 就整批
拒絕存檔（還是把「四」的 16 筆存下來、順便附一份 provenance json 記錄
來源），但**明確警告這批樣板不完整、無法拿去建 `RecognitionService`**
——這是一個寫得好的安全閘門，不是 bug。**這一步能测出的問題只有「資料
集本身缺一個類別」，這正是步驟 1 那個 bug 造成的**，不是這支腳本的錯。

## 步驟 4 ✅ 重啟 bridge 指向新樣板：不需要重啟，且錯誤原因清楚

按原計畫這一步要重啟 bridge，但因為這次的 bridge 從一開始就用
`--templates-dir` 指到我自己的 scratchpad templates 目錄，**建樣板完成
後直接查 `GET /templates`，不用重啟就拿到最新結果**：

```json
{"loaded": false, "reason": "fullflow01_2.npz 沒有 _reject 樣板，RecognitionService 無法校準拒識門檻"}
```

**⚠️ 沒能確認的是**：這是「本來就會自動偵測目錄變化」還是「單純因為這個
端點是查詢時才即時載入，不是啟動時載一次快取住」——這兩種機制對使用者
的體感一樣（不用重啟），但對「如果樣板檔案中途被覆蓋會不會用到舊的」這
個問題答案不同，**我沒有動 `bridge_server.py`（不是這輪授權範圍），
這點留給後端 owner 判斷要不要在文件裡講清楚**。

**這個錯誤訊息本身寫得很好**：直接講了原因（缺 `_reject`），跟步驟 3
腳本的警告文字幾乎一樣，**前後一致，不會讓使用者看到兩種說法而困惑**。

## 步驟 5 🔴 `quiz` 模式按「觸發辨識」：後端拒絕得很清楚，前端顯示錯了（第 6 個案例）

**後端直接呼叫**：

```
POST /recognize → HTTP 503
{"error": "尚無 enrollment 樣板，無法辨識",
 "reason": "fullflow01_2.npz 沒有 _reject 樣板，RecognitionService 無法校準拒識門檻"}
```

**這是對的行為**——沒有可用樣板本來就該拒絕，而且原因講得很清楚。

**但在瀏覽器裡真的點下「觸發辨識」按鈕**（真實 DOM click，不是呼叫
API），畫面顯示的是：

```
尚未串接（/recognize 還沒上線）
```

**這句話是舊的、現在會誤導人**——`/recognize` 端點**已經接上了**，
只是這次請求因為樣板不完整被合理拒絕。讀 `quiz.js` 原始碼（line 1023–
1044）確認原因：

```js
try {
  const res = await fetch("/recognize", { method: "POST" });
  if (!res.ok) throw new Error("HTTP " + res.status);
  ...
} catch (err) {
  // D09's /recognize doesn't exist yet (confirmed live, 404) -- this
  // is the expected state right now, not a real error to alarm over.
  resultStatusEl.textContent = "尚未串接（/recognize 還沒上線）";
  console.warn("[quiz] /recognize unavailable:", err.message);
}
```

**任何非 200 的回應**（不管是真的連不上、還是像這次一樣「連上了、但
後端有充分理由拒絕」）**都被同一句話蓋掉**，真正的原因
（`"尚無 enrollment 樣板，無法辨識"`）**只寫進 `console.warn`，畫面上
使用者完全看不到**。console 截圖確認：

```
[quiz] /recognize unavailable: HTTP 503
```

**這是這個專案今天抓到的第 6 個「後端已經接好、前端沒跟上」案例，
跟 `REPLAY_WALKTHROUGH.md` 抓到的第 5 個是同一種模式**：解析／呼叫本身
沒錯（`res.ok` 判斷正確捕捉到了 503），只是把「連不上」跟「連上了但被
拒絕」這兩種完全不同的情境，用同一句過時文字蓋掉。

🔴 **`quiz.js` 是 `js/modes/*.js`，不在我這輪授權範圍內，只回報不動手**
——照文檔的邊界規定，交給調度員轉派給 `quiz.js` 的 owner。**修法建議跟
`record.js`（`session/start` 那段）、剛修好的 `replay.js` 一致**：`res.ok`
為 false 時讀 `body.error`/`body.reason` 顯示出來，`fetch` 本身丟例外
（真的連不上）才顯示「尚未串接」。

**對真人使用者的意義**：使用者建完樣板、進 quiz 模式按下「觸發辨識」，
畫面會告訴他「這功能還沒做」——**但其實功能做好了，是他自己的樣板不完整
（例如忘記錄 `_reject`）**。使用者會去找開發者反應「辨識功能壞了」，
而不是回頭去補錄 `_reject` 樣板，**方向完全錯誤**。

## 步驟 6 ⛔ 用沒錄進樣板的詞測拒識：無法測試

因為步驟 5 已經證實 `RecognitionService` 根本沒有被建出來（`loaded:
false`），**沒有任何辨識邏輯在跑，自然也沒有「拒識」這回事可以測**。
這不是我沒去測，是這一步在目前的資料集下**structurally 不可能測到**
——必須先解掉步驟 1 的換詞 bug、錄到至少一個 `_reject` 樣板，才有辦法
真的走到這一步。**如實回報「無法驗證」，不假裝測過。**

---

## 修了什麼 / 誰的 / 狀態

| 發現 | 嚴重度 | 歸誰 | 狀態 |
|---|---|---|---|
| trial 提示詞永遠不換（存檔、abort 都無效） | 🔴 擋住整條路 | `state_machine.py`／`record.js`（`4f`） | ✅ **已於 2026-08-26 修復**（`hold_stop()` 補上 `_order_pos += 1`）——⚠️ 但步驟 5/6 需要用真的多詞資料集**待重跑**才算驗證過 |
| 表單「每詞目標筆數」欄位沒接上選字邏輯 | 🔴 可能同根因 | 同上 | 待確認是否隨上面一起修好（未重新驗證） |
| `quiz.js` 把 `/recognize` 的任何非 200 回應都顯示成「還沒上線」 | 🔴 第 6 個「前端沒跟上」案例 | `quiz.js`（owner 待調度員指派） | ✅ **已於 2026-08-26 修復**（顯示 `body.error`/真實狀態碼） |
| `first_session_check.py` 輸出清楚、有陪伴式的門檻說明 | ✅ 做得好 | — | 不需要動 |
| `build_templates_from_session.py` 缺 `_reject` 時明確拒絕產出可用樣板 | ✅ 做得好 | — | 不需要動 |
| `-m` 模組執行方式容易踩坑（直接跑會 `ModuleNotFoundError`） | ⚠️ 建議補文件 | 該腳本 owner | 回報，未修 |
| `GET /templates` 不用重啟就反映新樣板目錄內容 | ✅ 對使用者體感好 | — | 不需要動；⚠️ 快取機制細節未確認 |

## 這輪順便修的（`replay.js`，另一個調度員指派的插隊任務）

排練途中，調度員把稽核完但被 `ed` 依 `CONTRACTS.md` 檔案所有權規則拒絕
編輯的三處 `replay.js` 問題轉派給我（`panel/**` 是 C 軌獨佔）。三處都已
修好並**在瀏覽器裡實測過**，不是只看程式碼：

1. `GET /replay/sessions` 回應解析從 `{files:[...]}` 改成直接讀陣列，
   `value` 用 `path`、顯示用 `file`（照 `CONTRACTS.md` §4.1.3 最新版）
2. `/replay/start`／`postControl` 的錯誤處理改成先分「連不上後端」跟
   「後端回了真的錯誤」，後者把 `body.error` 顯示出來
3. 加了 `onLeave()` 呼叫 `POST /replay/stop`

🔴 **過程中多抓到一個更深的結構性 bug**：`shell.js` 的 `leaveMode()`
只有在 `modeEntered[mode]` 為 `true` 時才會呼叫 `onLeave`，而這個旗標
只有 `enterMode()` 在該模式定義了 `onEnter` 時才會設成 `true`。
`replay.js` 原本從未定義過 `onEnter`，導致**它的 `onLeave` 從一開始就是
死的掛點**，不只是這次加的 `/replay/stop` 沒生效。補了一個 `onEnter()`
解決（順便重抓 session 清單）。三處修改 + 這個額外發現都已經
`SendMessage` 詳細回報給調度員。

沒有 commit/push。測試 session／樣板全程存在自己的 scratchpad，沒有
碰 `/dev/ttyUSB0`，全程 `pgrep -af` 核對 PID，沒有 pattern kill。
