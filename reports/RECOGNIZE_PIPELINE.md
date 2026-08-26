# `/recognize` 與 `/templates` 上線 — Phase 2（即時特徵管線）+ Phase 1（端點）

> 由 `esp-mask-test-7c [4bedc9]` 執行，2026-08-26。起因：Demo 腳本第 4 步
> （純 ToF 念「四」→ 系統顯示「無法辨識」）需要 `/recognize` 真的存在。
> 調查發現這不是「接線」，是兩個從沒被串起來過的缺口：`templates/`
> 目錄完全是空的（從沒錄過真的 enrollment），以及 B06→D01→D02→D03
> 這條「裝置資料→104 維向量」的管線從沒有人接過（各自都有實作、各自
> 都有測試，但只在合成測試資料裡各自被獨立呼叫過）。

## 結論摘要

| 項目 | 狀態 |
|---|---|
| `host/features/live_pipeline.py`（新檔案，Phase 2 核心） | ✅ 完成，獨立測試（不需 `bridge_server.py`/HTTP/真裝置），含一條真的 enrollment→recognize 端到端測試 |
| `GET /templates` | ✅ 完成並實測——無樣板時回 `{"loaded": false, "reason": ...}`（200，不是 500）；有樣板時回 `list_templates()` 完整內容 |
| `POST /recognize`（即時擷取路徑） | ✅ 完成並用**真的 HTTP 請求**端到端實測過（mock_device → bridge_server → 真的 200 TriResult） |
| `POST /recognize`（`trial_id` 路徑） | ⚠️ 完成，在函式層級用手造的 HDF5 檔測過（`_frames_from_stored_trial` 正確讀回、對齊、組出向量），**沒有**透過完整的 trial 錄製狀態機走一次真的 HTTP 請求——理由見下 |
| ROC 雙邊校準（不是舊的單邊 LOO） | ✅ 確認——`RecognitionService` 建構子沒傳 `reject_calibration_method` 就是預設 `"roc"`，實測 `GET /templates` 回傳 `"reject_calibration_method": "roc"` |
| `d_tof_raw`/`d_mel_raw` 有沒有漏 | ✅ 確認在回應裡，且用真實 HTTP 回應驗證過非空 |
| Demo 第 4 步能不能跑 | ❌ **還是不能**——`templates/` 是空的，這是已知、預期中的狀態，不是這輪的缺陷 |

---

## 1. `host/features/live_pipeline.py`（Phase 2）

新檔案，一個純函式 `assemble_query_from_aligned_frames(frames, baseline_mu_A,
baseline_sigma_A, baseline_mu_B, baseline_sigma_B, t_fixed=24, cvn=False,
active_zones_A=None, active_zones_B=None) -> FeatureSeq`：

- **輸入**：`Aligner.frames(...)` 的輸出（或任何 `AlignedFrame` 序列）+ 明確
  傳入的 baseline mu/sigma（不自己猜、不自己讀檔案——呼叫端決定用哪份
  baseline，這正是你要我把「baseline 從哪來」變成參數而不是內部假設的
  原因）。
- **輸出**：`analysis.features.feature_assembly.FeatureSeq`——`.data`
  （固定 T=24，給 cosine 距離）跟 `.data_raw`（原始長度，給 DTW）都有，
  直接對應 `RecognitionService.recognize()` 要的形狀。
- **只用三個模態同時「有資料」的幀**，其餘直接丟棄，不補值——理由跟
  `Aligner` 自己的 `*_present` mask 精神一致：補一個假數值比丟掉一幀
  更危險。
- **不處理 ambient**：`ambient_per_spad` 不是 CONTRACTS §3.3 104 維向量
  的一部分（那是 D10 串擾偵測用的獨立資料），這個管線完全不碰它。
- **不處理 VAD**：`mel_features()` 的 `vad_start`/`vad_end` 留空，跟
  `analysis/run_all.py`（目前唯一的真實參考管線）的 `build_feature_seqs()`
  做法一致——沒有 VAD 起訖點的即時 producer，C08/C19 都各自碰過這個缺口。

**測試**（`host/features/test_live_pipeline.py`，4 個測試全過）：
- 形狀/切片正確性
- 幀數不足時丟 `InsufficientFramesError`，不猜測
- 只有 ToF、沒有 Mel 時，幀全部被正確丟棄（不是補假資料硬撐）
- **端到端**：用 `ssi-backlog/tools/mock_device.py` 自己的合成資料模型
  （`Scenario`/`MelModel`，當函式庫匯入，不需要真的開 pty 子行程）產生
  逼真訊號，走完整條 `Aligner → live_pipeline → RecognitionService`，
  三個類別（`round_word`／`spread_word`／靜止當 `_reject`）**互相正確
  區分，靜止的查詢正確被兩軌都拒識**：

  ```
  round query:  top1=round_word,  reject_fused(0.5)=False
  spread query: top1=spread_word, reject_fused(0.5)=False
  idle query:   reject_tof=True, reject_mel=True, reject_fused(0.5)=True
  ```

  ⚠️ 這是合成資料，不是真實量測——這裡驗證的是「管線本身沒有斷掉、
  拒識邏輯有反應」，不是「真的能辨識人說的話」。

## 2. `bridge_server.py` 新增的部分（Phase 1）

**只加了新的、獨立的區塊**，沒有重排或改動任何既有程式碼：

- `runtime_paths["templates"]`（預設 `<repo>/templates`）+ `--templates-dir` CLI 參數，跟既有的 `--verification-dir` 同一種寫法
- `_load_recognition_service()` / `_frames_from_live_session()` /
  `_frames_from_stored_trial()`：模組層級的新函式，`RecognitionService`/
  `load_templates` 用**延遲 import**（跟這個檔案既有的 `h5py`/`librosa`
  延遲 import 慣例一致，沒有在檔頭加新的 eager import）
- `_handle_templates()` / `_handle_recognize()`：新的 request-handler 方法
- `do_GET`/`do_POST` 各加一行 `elif`，**確認過不在 `/verify/reports` 那個
  順序敏感區塊附近**

**沒有樣板時的行為**（`templates/` 現在就是這個狀態）：`GET /templates`
回 `200 {"loaded": false, "reason": "..."}`，`POST /recognize` 回
`503 {"error": ..., "reason": ...}`——都不是 500，`RecognitionService`
的建構子不會被拿沒驗證過的資料硬呼叫。

## 3. 實測（真的 HTTP 請求，不是函式呼叫）

mock_device + bridge_server（自己的空 port `8899`，`--templates-dir`
指到自己的 scratchpad，從頭到尾沒碰過 repo 裡真的（空的）`templates/`）：

```
GET /templates（空目錄）         → 200 {"loaded": false, "reason": "...尚未錄過 enrollment"}
POST /recognize（空目錄）        → 503 {"error": "尚無 enrollment 樣板，無法辨識", ...}
```

放一份自己造的合成樣板（用 `live_pipeline.py` 生的向量 + `enrollment.
save_templates()` 存的 `.npz`，**沒有寫進 repo 的 `templates/`**）進去：

```
GET /templates → 200 {"loaded": true, "dist_method": "cosine",
  "reject_calibration_method": "roc",
  "classes": {"round_word": 3, "spread_word": 3}, "n_reject_templates": 5,
  "theta_reject_tof": 0.00087..., "theta_reject_mel": 0.00918..., ...}
```

`POST /session/start` → `POST /session/baseline`（baseline `ok: true`）→
`POST /recognize`（不帶 body，走即時擷取路徑）：

```json
{"classes": ["round_word", "spread_word"],
 "d_tof": [1.999..., 0.0], "d_mel": [1.999..., 0.0],
 "d_tof_raw": [1.021..., 1.020...], "d_mel_raw": [1.183..., 1.085...],
 "reject_tof": true, "reject_mel": true,
 "tau": 0.5, "theta_reject_tof": 0.00087..., "theta_reject_mel": 0.00918...,
 "dist_method": "cosine",
 "latency_ms": {"feature": 0.0, "dist": 0.4, "total": 0.4}}
```

`reject_tof`/`reject_mel` 都是 `true`——mock_device 這次隨機產生的訊號
剛好沒有落進校準時用的任一個類別窄門檻裡（校準樣板數只有 3-5 筆，門檻
本來就很緊），**這是真實跑出來的結果，不是我挑過的**，剛好也是拒識路徑
在真正運作的證據。

**`trial_id` 路徑**：用手造的、符合 CONTRACTS.md §2 schema 的 HDF5 檔（`h5py`
直接寫，`/meta` 帶 baseline 屬性、`trial_001` 帶 `tof_A`/`tof_B`/
`tof_valid_A/B`/`tof_t_us`/`mel`/`mel_t_us`）在函式層級測過：
`_frames_from_stored_trial()` 正確讀回、重新對齊（ToF 30Hz 跟 Mel 62.5Hz
是分開的時間軸，不能直接拼接，這點在函式的 docstring 裡也寫了），
`read_baseline_thresholds()`（既有函式，重用而非重寫）正確讀回 baseline，
組出的 `query.data` 形狀正確、全部有限值。

⚠️ **沒有透過真的 `/trial/*` 錄製流程走一次完整的 HTTP 請求**——
`TrialStateMachine` 的 hold-to-record 計時在這台機器目前的負載下容易卡在
非預期狀態（跟另一個 agent 現在正在查的 `/verify/*` hold_duration_s
問題是同一類環境敏感度），評估過風險後決定不硬闖，改用函式層級的真實
HDF5 檔驗證核心邏輯。**這部分比即時擷取路徑驗證得淺**，如果要更有信心，
之後可以在機器負載低的時候補一次完整 HTTP 流程的測試。

## 4. 順便驗到的既有測試現象（不是我造成的迴歸）

跑 `test_bridge_session_api.py` 時看到 3 個 PING burst 計時測試失敗
（`session_start`/`session_end` 觸發的 burst 沒在測試等待的視窗內看到）。
**這些測試碰的是 clock_sync/PING 邏輯，我這輪完全沒有動過那部分程式碼**，
而且失敗型態（真實時間視窗、機器忙時偏移）跟 `reports/TEST_SUITE.md`
已經記錄過的「多個 agent 的 pytest 行程同時搶 CPU 導致時間敏感測試假性
失敗」完全一致——這台機器這次測試時同時有 10 組其他 agent 的行程在跑。
**沒有進一步排查**（不是我的檔案，且已有既定解釋），如實記錄在這裡。

## 5. 測試環境清理

mock_device/bridge_server 全部用自己的空 port + scratchpad 目錄（sessions、
templates 都不是 repo 裡的真實目錄），測完精確 PID kill。這輪機器上同時
有 10 組其他 agent 的行程，過程中沒有動到任何一個。**沒有寫入 repo 的
`templates/` 目錄**，那個目錄目前依然是空的，Demo 還是拿不到真實辨識結果
——這是誠實、預期中的狀態，不是這輪要解決的問題。
