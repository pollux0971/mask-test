# E2E_PIPELINE — 第一次把整條鏈走完

## 結論（先講）

**mock device → bridge_server → session → baseline → trial → HDF5 → 讀回 → 回放，這條鏈今天走得通，而且大部分是真的、不是 skip。**

最初版本 12 個檢查點裡 10 個綠燈、2 個明確 skip（都附原因）。過程中在自己的檔案裡（`host/replay/session_replay.py`，B17）抓到並修掉兩個真的形狀不一致的 bug——都是「照著這支測試的要求真的重播一次自己產的檔案」才會冒出來的那種問題，光看程式碼看不出來。

**更新（同一天，後續）**：`speaking_mode`（`B21`）跟 `/replay/*` HTTP 路由（`esp-mask-test-ed`）都在這之後接上了。兩個原本的 skip 都已經換成真斷言，而且是**測試自己主動告知**要換——`test_replay_http_endpoint` 原本設計成「一旦收到非 404 就 `pytest.fail()`」，wiring 一落地它就紅了，不是我自己去翻文件才發現。現在 12 個檢查點全部是真斷言，沒有 skip。

**又加了第 7 節：HDF5 → 分析層的型別接縫（D15 標記過、沒人測過的假設）。結論是沒問題，但要看下面「怎麼比的」才算數，不是一句「沒問題」。**

測試檔：`vl53l7cx_test/monitor/test_e2e_pipeline.py`（19 個測試函式，共用一個 module-scoped fixture 跑一次完整流程；型別接縫那 6 個裡有 5 個吃同一份真檔，1 個是獨立構造的合成 trial）。

---

## 怎麼跑

```bash
.venv/bin/python -m pytest vl53l7cx_test/monitor/test_e2e_pipeline.py -v
```

不需要真板子、不需要額外設定。跟其他 `test_bridge_*.py` 一樣，重用 `test_bridge_sse.py` 的 `Rig`：真的起一個 `mock_device.py`（T04）子行程 + 真的起一個 `bridge_server.py`（`--source mock`）子行程，中間用 pty 接起來，session 檔寫到 `tmp_path` 級的隔離目錄，不會弄髒 repo。

連跑 3 次，結果一致：`19 passed`，13-16 秒。

---

## 12 個檢查點，逐一對照 story 的驗收條件

| # | 檢查點 | 測試函式 | 結果 |
|---|---|---|---|
| 1 | `mock_device.py --mel 1` 真的起、`$F` 有送出來 | fixture 本身（若沒收到會在下游 mel 檢查 skip） | ✅ |
| 2 | `POST /session/start` → `POST /session/baseline` | `test_baseline_response_has_the_numbers` | ✅ |
| 3 | `/meta` 有 `baseline_mu_*`/`noise_floor_*` | `test_meta_has_baseline_and_noise_floor_attrs` | ✅ |
| 4 | 錄幾筆 trial（`hold/start`+`hold/stop`） | fixture 本身，3 筆 | ✅ |
| 5 | `trial_000` 是 baseline，重開檔後還在 | `test_baseline_trial_survives_reopening_for_trials` | ✅（B07 append-mode 的 regression） |
| 6 | trial 的 idx 不跟 baseline 的 idx 0 撞 | `test_trial_idx_does_not_collide_with_baseline` | ✅ |
| 7 | `mel` 是自己的 `F` 軸，跟 `mel_t_us` 成對，不用等於 `mic_t_us` | `test_mel_has_its_own_axis_paired_with_mel_t_us` | ✅ |
| 8 | 沒偵測到時，4 個 VAD 時間 attr **完全不存在**（不是 0/-1/視窗邊界） | `test_vad_timing_attrs_are_entirely_absent` | ✅ |
| 9 | `speaking_mode` 有寫入 | `test_speaking_mode_is_written` | ✅（B21 落地後從 skip 換成真斷言，值固定是 `"normal"`——bridge_server.py 的 `_dispatch_trial()` 目前還沒把 request body 的 `speaking_mode` 傳給 `hold_start()`，所以每筆都拿到 `TrialStateMachine.__init__` 的預設值，不是真的有分辨 normal/whisper/silent） |
| 10 | 回放這個檔案，事件形狀跟 live 一致 | `test_replay_reproduces_live_event_shapes` | ✅（過程中修了 2 個 bug，見下方） |
| 11 | 回放事件都帶 `replay: true` | `test_replay_events_carry_the_replay_flag` | ✅ |
| 12 | `/events` 的 `status.source` 接 mock 時不是 `"live"` | `test_status_declares_mock_not_live` | ✅ |
| 13 | `/replay/*` HTTP 端點：開始回放、事件真的帶 `replay:true` 送到 `/events`、回放期間即時 tof/mic/mel 被擋下不會混進來、`status.source` 不受影響 | `test_replay_http_endpoint` | ✅（ed 落地後從 skip 換成真斷言） |

第 12 點原本以為要 skip（story 交下來時判斷 `source` 還沒接上），但重新讀最新的 `bridge_server.py` 發現 `--source`／`link_source["value"]` 已經是完整功能，`status` 事件的第一筆快照跟之後每一筆都帶著正確的值——ed 已經把這塊做完了，這裡直接斷言，不是 skip。

第 9、13 點原本設計成明確 skip 並在訊息裡寫「wiring 補上之後這裡會自動變成真斷言」；`/replay/*` 那個 skip 更進一步：一旦收到非 404 就主動 `pytest.fail()`，逼自己回來改，不會安靜地一直綠燈。兩個都在後續（`B21` 與 ed 的 `bridge_server.py` 更新）落地時被抓到、換成了真的斷言——**沒有回頭修改判斷邏輯，純粹是被動地從 skip 轉綠**，這正是最初設計這兩個 skip 時想要的效果。

---

## 第 7 節：HDF5 → 分析層的型別接縫

### 為什麼要測這個

`D15`（`analysis/reporting/session_loader.py`）的作者自己在文件字串裡標了一個沒驗證過的假設：`_as_scalar()`（負責把 h5py 讀回來的 attr 統一成 Python 型別）只對過**自己構造的合成 attr**，沒對過 `SessionWriter`（B07）真的產生的檔案。實際查證後這個缺口是真的：`analysis/reporting/test_run_all.py` 開頭就寫明「用 `h5py` 直接寫出符合 §2 的合成 session（不依賴 `host/storage`）」——`meta.attrs["subject"] = "s01"` 這種寫法跳過了 `SessionWriter` 整條寫入路徑，`_as_scalar()` 從沒真的處理過 `SessionWriter` 寫出來的 attr。這正是這個專案一直在踩的那種 bug：寫入端（`session_writer.py`）跟讀取端（`session_loader.py`）各自照著自己對 schema 的理解走，兩邊個別看都對，接縫上才會不對（`C08` 的 mel int16 vs 解碼浮點、`C05` 的 `dim` 是 zone 數不是邊長，是同一類問題）。

### 怎麼比的

用 `test_e2e_pipeline.py` 既有的 `pipeline` fixture（真的 mock device → bridge_server → baseline → 3 筆 trial 產生的檔案）餵給 `load_session()`，逐項比對：

| 檢查項目 | 測試函式 | 比對方法 |
|---|---|---|
| 字串 attr（`subject`/`mode`/`session_date`/trial `label`/`mode`/`quality`）讀回是 `str` 不是 `bytes` | `test_session_loader_reads_real_string_attrs_as_str` | `isinstance(value, str)`；`label` 特意用詞彙集的中文字（非 ASCII utf-8），不是隨便一個英文字串 |
| bool attr（`clock_sync_confirmed`/`clock_cross_check_ok`）讀回是 Python `bool` 不是 `numpy.bool_` | `test_session_loader_reads_bool_attrs_as_python_bool` | `isinstance(value, bool)` 且 `not isinstance(value, np.bool_)` |
| 數值純量（`clock_slope`/`noise_floor_mu`/trial `wear_id`）拆包成 Python `float`/`int`，不是 `numpy.float64`/`numpy.integer` | `test_session_loader_unwraps_numeric_scalars` | `type(value) is float`；`not isinstance(wear_id, np.integer)` |
| baseline 陣列（`baseline_mu_A` 等）形狀跟型別 | `test_session_loader_reads_baseline_arrays_with_correct_shape_and_dtype` | `mu.shape == (32,)`、`mu.dtype == np.float64`（`SessionWriter` 存成 `float32`，`session_loader.baseline()` 明確轉型成 `float64`，這裡確認轉型真的發生） |
| 選填欄位缺席（VAD 四個時間戳、`speaking_mode`、`sensors_enabled`）不會讓 `load_session()` 拋例外或安靜給錯值 | `test_session_loader_missing_optional_attrs_are_absent_not_crashing` | 確認 `session_loader.py` 全程用 `dict.get()`（先把 `attrs.items()` 轉成一般 dict 再存取），缺席回 `None`／不在 dict 裡，不是 `KeyError` |
| 無效 ToF zone 讀回真的是 `NaN`，有效性完全靠獨立的 `tof_valid_A`/`B` 陣列，不是靠 `NaN != NaN` 這種自比較 | `test_session_loader_invalid_tof_zones_stay_nan_and_validity_is_independent` | 不依賴 live rig 這次跑出來的合成場景剛好有沒有無效 zone——直接用 `SessionWriter` 構造一個保證有一格無效值的 trial，讀回後同時斷言 `tof_valid_a[1,5] is False` **與** `np.isnan(tof_a[1,5])`，並反向確認有效格既不是 NaN、`tof_valid_a` 也確實是 `True` |

### 結論：**沒有找到問題**

`h5py 3.16.0`（本機安裝版本）加上 `_as_scalar()` 現有的三個分支（`bytes`→`decode`、`np.generic`→`.item()`、`np.ndarray`→原樣回傳），對 `SessionWriter` 真的產生的檔案，上面六項全部如預期。`session_loader.py` 沒有任何地方對無效 ToF 值做 `==`/`!=` 比較，有效性判斷完全來自獨立的 `tof_valid_*` 陣列，跟 §2.1 的設計意圖一致。

沒有需要修改 `session_writer.py` 或 `session_loader.py` 的地方。這 6 個測試本身就是交付——`_as_scalar()` 這個假設從今天起有真檔案的 regression 保護，不再只是「大家覺得應該沒問題」。

---

## 過程中在自己的檔案裡抓到、也修掉的 2 個 bug

`session_replay.py` 是我自己的檔案（B17），修這兩個都是「回放要重現 live 的事件形狀」這個目標本身要求的最小修正，不是新功能：

1. **回放的 `tof` 事件少了 `dim` 欄位。** live 的 `tof` 事件固定帶 `dim`（前端拿它跟 `len(dist)` 對帳），回放組出來的 dict 漏掉了這個 key。已加回去（`dim: n_zones`，本來就有算，只是沒放進 payload）。
2. **回放的 `mic.rms` 型別是 float，line 協定的 `rms` 是 int。** `SessionWriter` 把 `mic_rms` 存成 `float32`（儲存精度考量），但回放要重現的是**線協定**的形狀，那裡 `rms` 一律是整數（CONTRACTS #1.1，`test_bridge_sse.py` 也是這樣斷言的）。已在讀回時轉回 `int(round(...))`。

兩個都只影響 `_trial_events()` 組 payload 的那幾行，沒有動到儲存格式本身，也沒有改到既有測試依賴的任何行為——`host/replay/test_session_replay.py` 原本 21 個測試改完後仍然全過。

---

## 已解決的缺口（原本是 skip，wiring 落地後自動變成真斷言）

### `speaking_mode`（原 skip，B21 落地後轉綠）

寫這支測試的當下，`host/trial/state_machine.py` 呼叫 `write_trial()` 完全沒有傳 `speaking_mode`。`B21` 把它接上之後（`TrialStateMachine` 建構時預設 `"normal"`，`start_trial()`/`hold_start()` 都能覆寫），現在每筆 trial 都會寫入這個欄位。**還留著一個小缺口**：`bridge_server.py` 的 `_dispatch_trial()` 目前沒有把 `/trial/hold/start` request body 的 `speaking_mode` 傳給 `machine.hold_start()`，所以線上錄到的每一筆現在都固定是 `"normal"`——欄位有寫入，但還沒有人能透過 API 真的錄到 `whisper`/`silent`。這不影響這支測試的斷言（只驗證「有寫入、值域合法」），但值得記錄成下一步。

### `/replay/*` HTTP 路由（原 skip，ed 落地後轉綠）

寫這支測試的當下，`bridge_server.py` 完全沒有 `/replay` 開頭的路由。`esp-mask-test-ed` 接上之後（`/replay/start`、`/replay/sessions`、`/replay/state`、`/replay/control`、`/replay/speed` 等），`test_replay_http_endpoint` 從「一旦收到非 404 就主動 `pytest.fail()`」的自我提醒機制被觸發，換成了真斷言：開始回放、確認 `/events` 收到帶 `replay:true` 的事件、確認回放期間即時 `tof`/`mic`/`mel` 真的被 `bridge_server.py` 的 `handle_parsed_event()` 擋下（沒有任何未標記的即時資料混進來）、確認 `status.source` 不受影響。

---

## 修改的檔案

- `host/replay/session_replay.py`（我的檔案，B17）—— 兩個回放形狀修正（`dim`、`mic.rms` 型別）
- `host/storage/session_writer.py`（我的檔案，B07）—— 新增選填的 `sensors_enabled`/`sensors_enabled_confirmed`
- `host/storage/test_session_writer.py` —— 對應的 6 個新測試
- `ssi-backlog/tools/schema_example.py`（我的檔案，T02）—— 補 `sensors_enabled`，順便修掉 VAD `-1` 佔位值的舊 bug
- `vl53l7cx_test/monitor/test_e2e_pipeline.py`（新檔案）—— 這支測試，19 個測試函式
- `reports/E2E_PIPELINE.md`（新檔案）—— 這份報告

沒有動任何不是自己負責的檔案——`bridge_server.py`、`host/trial/state_machine.py` 全程只讀不改；兩個原本的已知缺口（`speaking_mode`、`/replay/*`）都是其他 agent 自己接上、這裡的測試被動偵測到才轉綠，不是這裡主動去接線。

---

## 需要人工驗證的項目

- 真機（不是 mock）跑一次同樣的流程，確認 `source` 會回報別的值（例如 `"live"`）而不是 `"mock"`——這支測試只證明「命令列給什麼 `--source`，`status` 就回報什麼」，沒有真的驗證真機那條路徑。
- `/replay/*` 接上後，麻煩重新看一次 `test_replay_http_endpoint`：目前它在「非 404 就 `pytest.fail()`」，設計上是故意在 wiring 完成的當下逼自己回來把它從 skip 換成真斷言，別讓它一直紅著被忽略。

## CONTRACTS.md 的疑問或建議變更

沒有新的。這輪主要是驗證既有 schema 決策在真實資料流過一遍之後仍然成立。

## 我注意到但沒有動的問題

- `host/trial/state_machine.py` 裡已經有清楚的註解說明 VAD（B15/B16）還沒接進 trial machine，而且明確標注「這是比補一個佔位值更大的介面變更，且 TrialStateMachine 的建構子正是 ed 現在在 wiring 的地方，貿然改簽章有衝突風險」——這是別人正在動的檔案，這裡完全沒有碰，只是把它反映在 `test_vad_timing_attrs_are_entirely_absent` 的文件字串裡，讓下一個接手的人知道「現在測試綠燈」跟「VAD 真的有在跑」是兩件事。
