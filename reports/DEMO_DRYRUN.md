# Demo 四步：用模擬資料真的跑一遍

> ⚠️ **全部合成資料**（`mock_device.py --scenario round`），不是真人戴著
> 裝置念詞。目的不是驗證準確率，是驗證**管線接不接得起來**——`E06` 錄
> 真的樣板之前，先確認這條路現在不是空的。
>
> 用完的假樣板已經刪除（見「清理」一節），沒有留在 `templates/` 裡。

## 方法

重用專案自己的測試骨架（`vl53l7cx_test/monitor/test_bridge_sse.py` 的
`Rig`），真的起一個 mock device + `bridge_server.py`：

1. `POST /session/start` → `POST /session/baseline`
2. `POST /trial/hold/start`（帶 `label`）→ `hold/stop` → 需要時 `confirm`，
   錄 `wu`（五）× 4、`yi`（一）× 4、`_reject`（靜止）× 4，共 12 筆
3. `POST /session/end`，讀出這次的 session `.h5`
4. 用 `analysis/run_all.py` 的離線特徵組裝路徑（`tof_features`/
   `mel_features`/`assemble_feature_seq`，即 D01→D02→D03）把這 12 筆
   組成樣板，`analysis/similarity/enrollment.save_templates()` 存成
   **真的 `templates/s01_1.npz`**（bridge_server 預設讀的那個目錄）
5. `GET /templates`、`POST /recognize` 打**真的**在跑的 bridge（走
   `host/features/live_pipeline.py` 那條線）
6. 拿同一筆已錄好的 trial，分別餵給「離線路徑」與「`live_pipeline`
   的組裝函式」，直接比對輸出陣列

## 1. 錄樣板、存檔、載入——全部真的跑過

`GET /templates` 的回傳（截自實測 JSON）：

```json
{
  "loaded": true,
  "classes": {"wu": 4, "yi": 4},
  "n_reject_templates": 4,
  "theta_reject_tof": 0.812,
  "theta_reject_mel": 0.963
}
```

`_reject` 沒有出現在 `classes` 裡——這是**對的**，`RecognitionService`
把它獨立拉出來當拒識池，不是分類詞（`D08`/`enrollment.py` 的設計，
不是這次才發現）。

## 2. `POST /recognize`——`TriResult` 形狀完整，真的能跑

```json
{
  "classes": ["wu", "yi"],
  "d_tof": [2.0, 0.0], "d_mel": [0.0, 2.0],
  "d_tof_raw": [1.004, 0.916], "d_mel_raw": [0.425, 0.963],
  "reject_tof": true, "reject_mel": false,
  "tau": 0.5, "theta_reject_tof": 0.812, "theta_reject_mel": 0.963,
  "dist_method": "cosine",
  "latency_ms": {"feature": 0.0, "dist": 0.52, "total": 0.52}
}
```

`CONTRACTS.md` §4.3 要求的欄位全部在（`d_tof_raw`/`d_mel_raw`/兩個獨立
`theta_reject_*`），latency 在合理範圍（0.5ms，不是卡住或爆炸的數字）。
**這條路徑確實是通的，不是回一個假的 200。**

⚠️ **`d_tof`/`d_mel` 剛好卡在 `[2.0, 0.0]` 這種極端值，值得說明**：
這是合成資料的產物，不是 bug——`mock_device --scenario round` **對每個
`label` 都輸出同一套物理動作**，跟真人念不同詞完全不同；`wu`/`yi`/
`_reject` 三個類別的樣板事實上來自同一種訊號模式，只有雜訊不同，
cosine 距離在這種情況下容易飽和到 0 或 2（其中一類幾乎完美匹配、另一類
完全相反）。**這不是準確率的證據，這輪的目的只是驗證管線通不通。**

## 3. 🔴 問題 4：離線路徑 vs `live_pipeline` 路徑，比對結果

**`allclose=False`，最大絕對差 ≈ 0.009–0.013**（兩次獨立重跑，數字略有
浮動但同量級）。**但這個數字目前不能直接當成「兩條路徑不一致」的證據
——追出來的根因比這個更值得談：**

### 根因：兩條路徑對「ToF 幀數 ≠ Mel 幀數」的處理方式本來就不同

實測這次的 trial：`tof_A` 39 幀、`mel` 50 幀（`t_us` 對得上 `tof_A`）——
**`CONTRACTS.md` 自己早就警告過**「`mel` 的時間軸是 `F` 不是 `M`，
兩者幀數不相等，對齊一律靠 `t_us`」。

- **`analysis/run_all.py` 的 `build_feature_seqs()`**（離線建樣板用的
  參考路徑）：`n = min(len(tof_a_z), len(tof_b_z), len(mel_cmn), ...)`，
  直接**取前 n 筆**，是幀數對齊，不是時間對齊——第 39 個 ToF 幀配到的
  是第 39 個 Mel 幀，不是「時間上最接近的那個」。
- **`host/align/aligner.py`（`live_pipeline.py` 在真實系統裡吃的資料
  來源）**：讀過原始碼確認，`Aligner` 用 `bisect_left` + `nearest`/
  線性內插對 `t_us` 做**真正的時間對齊**（`_resolve()`/`bracket()`），
  不是幀數截斷。

**這代表：離線建樣板（`run_all.py`）跟即時推論（`live_pipeline.py` 走
真實 `Aligner`）用的是兩種不同的「怎麼把 ToF 跟 Mel 湊成同一組幀」的
邏輯。** 如果一次 trial 裡 ToF 跟 Mel 的取樣時間偏移夠大，離線樣板跟
即時查詢對到的 Mel 幀可能不是同一個時間點——**這才是問題 4 真正該
擔心的風險，比我這次量到的 0.01 這個數字本身更重要。**

### 這次量到的 0.01 為什麼不能直接當證據

我這次的「path B」**沒有真的走 `Aligner`**——`session_loader.Trial` 目前
沒有暴露 `mel_t_us`（`reports/SCHEMA_SUPPLY_DEMAND.md` 已經記錄這個
缺口），所以無法從已存檔的 trial 精確重建「原始逐模態時間戳」餵給真的
`Aligner`。我用**同索引配對**（ToF 第 i 幀配 Mel 第 i 幀）湊了
`AlignedFrame` 去呼叫 `assemble_query_from_aligned_frames()`——這**不是
真實系統裡 `Aligner` 真正的行為**，只是離 `run_all.py` 的「前 n 筆截斷」
更接近的一種簡化。**兩個都不是「真的即時系統會看到的樣子」，所以這 0.01
的差異多半只反映了兩種簡化本身的差異，不能拿來說「live_pipeline 本身
壞了」，也不能拿來說「兩條路徑其實一致」。**

### 老實的結論

**目前沒有辦法從已存檔的 session 精確重現「離線樣板 vs 真實即時推論」
的逐位元組比較**——缺的是 `mel_t_us` 的讀取端（結構性缺口，不是這次
才發現）。要做到真的可信的比對，需要：
1. `session_loader.Trial` 加 `mel_t_us` 欄位（`SCHEMA_SUPPLY_DEMAND.md`
   已經記錄，不在這次範圍內修）；或
2. 直接在真實系統上（`E05`/`E06` 錄音時）同時存一份「query 當下
   `live_pipeline` 實際組出來的向量」，事後跟同一段錄音离線重跑
   `run_all.py` 的結果比對——**這需要 `bridge_server.py` 或
   `live_pipeline.py` 加一個 debug 輸出點，不是我這次能做的**（邊界
   明講不能改這兩個檔案）。

**回報給你，不自己修**：`run_all.py` 的 `build_feature_seqs()` 用幀數
截斷、不是 `t_us` 對齊這件事本身，可能值得另開一個 story 讓
`run_all.py` 的擁有者評估要不要改成跟 `Aligner` 一致的時間對齊邏輯——
如果兩邊差距在真實資料上夠大，會是「樣板訓練用一種對齊、線上推論用
另一種對齊」的系統性偏差來源。

## 4. 走 Demo 四步——能走到哪一步

用這批合成樣板（`wu`/`yi`/`_reject`），對照 `DEMO_RUNBOOK.md`：

- **第 1 步（w=0.5 辨識）**：`POST /recognize` 真的回傳結果，`classes`/
  `d_tof`/`d_mel` 都在，機制上等同「三軌都給出答案」。
- **第 2/3 步（拖 `w`，靜音模式）**：這次沒有實地在瀏覽器裡拖滑桿/切
  說話模式——那部分邏輯是純前端重算（`fuseScores`/`computeFusedReject`），
  已經在稍早的 `C17`/`C20` 工作裡用合成 `TriResult` 驗證過，這次的重點
  是**後端這條線通不通**，沒有重複做前端那部分。
- **第 4 步（拒識）**：**這次第一次 `POST /recognize` 剛好回傳
  `reject_tof: true`**（見上方 JSON）——拒識機制在真實請求下確實會
  觸發，不是只在合成 pytest 裡成立。**但這不是「刻意用一個未錄的詞
  測拒識」**：`mock_device --scenario round` 對所有 label 產生同一種
  訊號，我沒有辦法用它模擬「這個詞真的沒錄過」的情境，只能說「拒識
  觸發的程式路徑被真實請求走過一次」，不是「驗證了對未知詞會正確拒識」
  ——這兩者不一樣，真實驗證要等 `E06`/`E07` 拿真的未錄詞試。

## 5. 哪一步在真實資料上可能表現不同

- **距離飽和到 0/2 這個現象在真實資料上大機率不會這麼極端**——真人講
  不同詞的訊號真的不同，不會像這次的合成資料一樣三個類別共用同一套
  物理動作。
- **問題 4 的對齊落差在真實資料上可能更明顯**——`mock_device` 產生的
  ToF/Mel 幀率是穩定、可預期的合成節奏，真實裝置的兩顆感測器與麥克風
  各自的時脈漂移、韌體排程抖動會讓幀數落差更不規則，`run_all.py` 的
  截斷式對齊跟 `Aligner` 的時間對齊之間的差距可能比這次量到的大。
- **`reject_tof`/`reject_mel` 的觸發率**：這次是合成資料的巧合結果
  （樣板彼此高度相似導致 cosine 距離兩極化），真實資料下的觸發率完全
  取決於 `E06` 錄的樣板品質與詞彙之間的真實可分性，這次的數字沒有
  任何預測力。

## 清理

- `templates/s01_1.npz`：**已刪除**（測試腳本 `finally` 區塊自動處理，
  兩次執行都確認刪除訊息與 `ls templates/` 只剩 `.gitkeep`）。
- 測試用的 mock device / bridge_server：透過 `Rig.close()`（記精確 PID
  `terminate()`，不是 pattern kill）清理，且用 `ps aux` 確認過沒有
  遺留、沒有動到其他 agent 自己的行程。
- 沒有碰 `/dev/ttyUSB0`、沒有改 `host/features/live_pipeline.py`、
  `bridge_server.py`、`analysis/run_all.py`。

## 修改的檔案

無（本輪只新增這份報告 + 暫時性的測試腳本，腳本在 `/tmp` 的 scratchpad
裡，不在 repo 裡，測完自動清理樣板檔案）。
