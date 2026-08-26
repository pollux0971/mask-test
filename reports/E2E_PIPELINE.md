# E2E_PIPELINE — 第一次把整條鏈走完

## 結論（先講）

**mock device → bridge_server → session → baseline → trial → HDF5 → 讀回 → 回放，這條鏈今天走得通，而且大部分是真的、不是 skip。**

12 個檢查點裡 10 個綠燈、2 個明確 skip（都附原因）。過程中在自己的檔案裡（`host/replay/session_replay.py`，B17）抓到並修掉兩個真的形狀不一致的 bug——都是「照著這支測試的要求真的重播一次自己產的檔案」才會冒出來的那種問題，光看程式碼看不出來。

測試檔：`vl53l7cx_test/monitor/test_e2e_pipeline.py`（12 個測試函式，共用一個 module-scoped fixture 跑一次完整流程）。

---

## 怎麼跑

```bash
.venv/bin/python -m pytest vl53l7cx_test/monitor/test_e2e_pipeline.py -v
```

不需要真板子、不需要額外設定。跟其他 `test_bridge_*.py` 一樣，重用 `test_bridge_sse.py` 的 `Rig`：真的起一個 `mock_device.py`（T04）子行程 + 真的起一個 `bridge_server.py`（`--source mock`）子行程，中間用 pty 接起來，session 檔寫到 `tmp_path` 級的隔離目錄，不會弄髒 repo。

連跑 3 次，結果一致：`10 passed, 2 skipped`，13-14 秒。

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
| 9 | `speaking_mode` 有寫入 | `test_speaking_mode_is_written` | ⏭️ SKIP（見下方「已知缺口」） |
| 10 | 回放這個檔案，事件形狀跟 live 一致 | `test_replay_reproduces_live_event_shapes` | ✅（過程中修了 2 個 bug，見下方） |
| 11 | 回放事件都帶 `replay: true` | `test_replay_events_carry_the_replay_flag` | ✅ |
| 12 | `/events` 的 `status.source` 接 mock 時不是 `"live"` | `test_status_declares_mock_not_live` | ✅ |

第 12 點原本以為要 skip（story 交下來時判斷 `source` 還沒接上），但重新讀最新的 `bridge_server.py` 發現 `--source`／`link_source["value"]` 已經是完整功能，`status` 事件的第一筆快照跟之後每一筆都帶著正確的值——ed 已經把這塊做完了，這裡直接斷言，不是 skip。

`/replay/*` 這條 HTTP 路由本身還沒接（見下方），所以第 10-11 點是在**函式庫層級**（直接呼叫 `host/replay/session_replay.py` 的 `read_session_events()`/`ReplayController`）驗證，不是透過 HTTP。這仍然是「真的重播這支測試自己產生的檔案」，只是還沒有 HTTP 這層外皮。

---

## 過程中在自己的檔案裡抓到、也修掉的 2 個 bug

`session_replay.py` 是我自己的檔案（B17），修這兩個都是「回放要重現 live 的事件形狀」這個目標本身要求的最小修正，不是新功能：

1. **回放的 `tof` 事件少了 `dim` 欄位。** live 的 `tof` 事件固定帶 `dim`（前端拿它跟 `len(dist)` 對帳），回放組出來的 dict 漏掉了這個 key。已加回去（`dim: n_zones`，本來就有算，只是沒放進 payload）。
2. **回放的 `mic.rms` 型別是 float，line 協定的 `rms` 是 int。** `SessionWriter` 把 `mic_rms` 存成 `float32`（儲存精度考量），但回放要重現的是**線協定**的形狀，那裡 `rms` 一律是整數（CONTRACTS #1.1，`test_bridge_sse.py` 也是這樣斷言的）。已在讀回時轉回 `int(round(...))`。

兩個都只影響 `_trial_events()` 組 payload 的那幾行，沒有動到儲存格式本身，也沒有改到既有測試依賴的任何行為——`host/replay/test_session_replay.py` 原本 21 個測試改完後仍然全過。

---

## 已知缺口（story 明講：這是進度，不是失敗）

### `speaking_mode` 沒有被寫入（skip）

`host/trial/state_machine.py` 呼叫 `write_trial()` 時完全沒有傳 `speaking_mode` 這個參數——不是傳了空值，是壓根沒接這條線。`SessionWriter`（B07）跟回放（B17）都已經支援這個欄位，只要 `/trial/*` 的 request body 開始把它傳進 `TrialStateMachine`，這個測試不用改就會變成真的檢查。

### `/replay/*` 沒有 HTTP 路由（skip）

`bridge_server.py` 的 `do_GET`/`do_POST` 目前完全沒有 `/replay` 開頭的分支，打下去是純 404（`send_error()` 的預設 HTML，不是 JSON——測試裡特地寫了一個容忍非 JSON body 的小 helper 來探這個狀態，不能直接重用 `_request()`）。這條測試已經把「library 層級驗證過同一個檔案」的結論寫在 skip 訊息裡；等 HTTP 端點接上，只要把 `test_replay_http_endpoint` 從 skip 換成真斷言（開始回放、poll `/events` 收到帶 `replay:true` 的事件、確認回放期間 `status.source` 不會被真實序列埠資料蓋掉）就是完整的端對端 regression。

---

## 修改的檔案

- `host/replay/session_replay.py`（我的檔案，B17）—— 上述兩個回放形狀修正
- `vl53l7cx_test/monitor/test_e2e_pipeline.py`（新檔案）—— 這支測試
- `reports/E2E_PIPELINE.md`（新檔案）—— 這份報告

沒有動任何不是自己負責的檔案（`bridge_server.py`、`host/trial/state_machine.py` 都只讀不改，兩個已知缺口都已回報，不是自己動手接線）。

---

## 需要人工驗證的項目

- 真機（不是 mock）跑一次同樣的流程，確認 `source` 會回報別的值（例如 `"live"`）而不是 `"mock"`——這支測試只證明「命令列給什麼 `--source`，`status` 就回報什麼」，沒有真的驗證真機那條路徑。
- `/replay/*` 接上後，麻煩重新看一次 `test_replay_http_endpoint`：目前它在「非 404 就 `pytest.fail()`」，設計上是故意在 wiring 完成的當下逼自己回來把它從 skip 換成真斷言，別讓它一直紅著被忽略。

## CONTRACTS.md 的疑問或建議變更

沒有新的。這輪主要是驗證既有 schema 決策在真實資料流過一遍之後仍然成立。

## 我注意到但沒有動的問題

- `host/trial/state_machine.py` 裡已經有清楚的註解說明 VAD（B15/B16）還沒接進 trial machine，而且明確標注「這是比補一個佔位值更大的介面變更，且 TrialStateMachine 的建構子正是 ed 現在在 wiring 的地方，貿然改簽章有衝突風險」——這是別人正在動的檔案，這裡完全沒有碰，只是把它反映在 `test_vad_timing_attrs_are_entirely_absent` 的文件字串裡，讓下一個接手的人知道「現在測試綠燈」跟「VAD 真的有在跑」是兩件事。
