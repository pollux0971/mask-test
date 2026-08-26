# Schema 供需對帳：誰繞過 session_loader，誰寫了沒人讀，誰讀了沒人寫

> 方法：`grep` 全 repo 找出每一個直接開 h5py 的地方，讀懂它在幹嘛；
> 再把 `CONTRACTS.md` §2 的每一個欄位，對照 `host/storage/session_writer.py`
> （唯一的正式寫入端）跟實際的呼叫端、`analysis/` 底下的每一個消費點，
> 逐一分類。這份報告分兩輪寫：第一輪只盤點；第二輪把調度員指名的三筆
> 結清（`mic_peak`/`mel_t_us`/`tof_ambient_t_us` 的結構性缺口、`source`
> 的落差、`mel_writer.py` 的精確偵察清單），邊界也跟著放寬到
> `session_loader.py`/`session_writer.py` 本身。

## 結論先講（第二輪更新後）

- 掃過的直接 h5py 存取有 **6 處**，其中 5 處是刻意、窄範圍、風險低；
  **1 處原本是死碼**（`host/storage/mel_writer.py`）——這輪把精確的偵察
  清單（誰引用、哪一行呼叫、`--h5-session` 還有沒有人用、刪掉會不會連累
  無關的 `.npy` 路徑）交出去，沒有動手刪，因為呼叫點在 `bridge_server.py`，
  當時有三個人同時在裡面。**第三輪更新：`ed` 已經清掉了**——
  `host/storage/mel_writer.py`／`test_mel_writer.py` 兩個檔案都已刪除，
  `--h5-session` 旗標也一併移除，`bridge_server.py` 裡確認過偵察清單標的
  那個風險（`wav_to_log_mel_timed` 跟 `write_mel_to_trial` 綁在同一個
  `try/except ImportError`）有被處理，不是單純刪檔留下地雷——這一項現在
  結案，不再是待辦。
- 🔴 第三類（有人讀、沒人寫）目前是 0 筆，`energy_mu`/`energy_sigma`
  當時的那個案例已經修掉（細節見下，留著是證明這張表抓得到這一類 bug）。
- 情況四（契約定義、兩邊都沒實作）**這輪也清空了**：`source` 原本卡在
  這一類（呼叫端在傳、寫入端沒接住、沒人讀），這輪把 `session_writer.py`
  接住了，現在改列進情況二（有寫、沒人讀）。
- `mic_peak`/`mel_t_us`/`tof_ambient_t_us` 這三筆原本「結構性讀不到」
  （`Trial` dataclass 根本沒有對應欄位）的技術缺口這輪也解掉了——但**沒有
  升級成情況一**：介面通了不等於有人在用，`analysis/` 目前還沒有實際消費
  這三個欄位的程式碼，仍然歸在情況二。
- `59` 同一輪把 `d14_viseme_sensitivity.py` 接上 `lip_onset_us`/
  `comparable` 等 7 個欄位，從情況二移到情況一，是這份報告目前唯一真正
  升級到「有寫有讀」的一批。

---

## 第一部分：誰繞過了 `session_loader.py`

`analysis/reporting/session_loader.py` 是**分析層**唯一的正式讀取端。
以下是全 repo 找到的**其他**直接開 `h5py.File(...)` 的地方（測試檔案跟
`session_writer.py`/`session_loader.py` 自己不算「繞過」，略過）：

| 檔案 | 讀/寫 | 讀/寫了哪些欄位 | 刻意嗎 | 靜默失效風險 |
|---|---|---|---|---|
| `host/replay/session_replay.py`（B17，我的檔案） | 讀 | datasets 全部（`tof_A/B`、`mic_*`、`mel`/`mel_t_us`）+ attrs `label`/`quality`/`speaking_mode` | ✅ 刻意——回放要重現**線協定**的事件，不是做分析，本來就不該經過 `session_loader.py` 那套面向統計分析的資料模型 | 低。讀到的 attrs 只有字串（`label`/`quality`/`speaking_mode`），h5py 3.x 字串已經驗過會正確解回 `str`（`E2E_PIPELINE.md` 第 7 節）；沒有讀任何 bool attr，不會踩 `comparable` 那類陷阱 |
| `host/storage/manifest.py` | 讀 | attrs：`session_date`、`trial_idx`、`label`、`wear_id`、`mode`、`quality`、`valid_zone_ratio`、`drop_count`；dataset：`tof_A.shape[0]` | ✅ 刻意——manifest 是輕量索引表，載入整個 `Trial`（含所有陣列）太浪費 | 低。讀的全部是 `REQUIRED_META_KEYS`/`REQUIRED_TRIAL_ATTRS`，用 `[...]` 直接索引（不是 `.get()`）在邏輯上是對的——這些欄位本來就保證存在。都是 str/int/float，沒有 bool |
| `vl53l7cx_test/monitor/bridge_server.py`（`read_baseline_thresholds()`） | 讀 | `/meta` 的 `baseline_mu_*`/`sigma_*`、`noise_floor_mu`/`sigma`、`energy_mu`/`sigma` | ✅ 刻意——這是**擷取當下**要餵給 VAD 偵測器的門檻值，跟`analysis/`的離線分析是不同的消費者、不同的時機，本來就不該依賴 D-track 的 `session_loader.py` | 低，而且這處寫法本身就是示範：全部用 `.get()`、缺席印警告訊息，**不是**直接索引——見下方 `energy_mu` 那筆歷史 |
| `host/trial/state_machine.py`（`_mark_trial_quality()`） | 寫 | attrs：`quality`（單一欄位，事後訂正用，`reject` 動作） | ✅ 刻意——訂正一個已落盤 trial 的品質標記，不需要重跑整個 `write_trial()` | 低，只寫一個保證存在的必填字串欄位 |
| `host/storage/mel_writer.py` | ~~寫~~ **已刪除** | ~~dataset：`mel`（沒有 `mel_t_us`）~~ | ~~🔴 不是~~ | ✅ **第三輪更新：`ed` 已經刪掉這個檔案跟 `--h5-session` 旗標，風險已解除，見下方** |
| `ssi-backlog/tools/schema_example.py` | 寫 | 透過 `SessionWriter`，不是直接開檔 | 不算繞過 | 無 |

### 🔴 死碼：`host/storage/mel_writer.py`（偵察清單，沒有動手）

自己的文件字串就寫明：

> 「B07 完成後，這支函式應該被併進 `SessionWriter`……現在只是先讓 B14
> 能被驗收」

`SessionWriter.write_trial()` 早就已經吃 `mel`/`mel_t_us` 了（B07/B14 都已
落地），但這支模組沒有跟著退場。

#### 1. 誰引用它（全 repo）

| 檔案 | 怎麼用 |
|---|---|
| `vl53l7cx_test/monitor/bridge_server.py:97` | 模組層級佔位：`write_mel_to_trial = None` |
| `vl53l7cx_test/monitor/bridge_server.py:109` | `_load_mel_backend()` 裡：`from host.storage.mel_writer import write_mel_to_trial as _t`——**跟 `host/features/mel_pipeline.py` 的 `wav_to_log_mel_timed` 綁在同一個 `try/except ImportError` 區塊裡**（見下方風險） |
| `vl53l7cx_test/monitor/bridge_server.py:1173` | 唯一的**真正呼叫點**：`_process_mfcc()` 函式裡，`write_mel_to_trial(h5_path, trial_idx, log_mel)` |
| `host/storage/test_mel_writer.py` | 4 個測試，全部針對 `write_mel_to_trial()` 本身（正常寫入、覆蓋既有 placeholder、band 數錯誤時報錯、trial 不存在時報 `KeyError`）——**這 4 個測試會在刪除時直接壞掉** |

#### 2. `bridge_server.py` 裡精確到哪個函式、哪條路徑

- 觸發點：`_process_mfcc()`（`bridge_server.py:1140`），自己的文件字串寫著
  「**B14 備援路線**」——由 `/record` 這個 HTTP 端點驅動（不是
  `/trial/*`），流程是「錄一段 WAV → 用 `wav_to_log_mel_timed()` 算
  log-mel → 存一份 `.npy` → **若命令列給了 `--h5-session`**，再多寫一份
  進指定 session 檔的下一個 trial（`mfcc_target["next_trial_idx"]` 每次
  `_process_mfcc()` 呼叫自動 +1，跟 `TrialStateMachine`/
  `SessionRegistry` 完全無關，也不檢查 baseline 是否已完成）。
- 跟現行 `/trial/*` 流程（`TrialStateMachine.write_trial()`）是**兩條完全
  獨立、不互相知道對方存在**的路徑；只要沒有人同時對同一個 session 檔
  又走 `/record --h5-session` 又走 `/trial/*`，目前不會互相覆寫打架，
  但也沒有任何機制**阻止**兩者同時對著同一個檔案動作。

#### 3. `--h5-session` 這條舊路徑現在還有人用嗎

- `--h5-session` 的 argparse `help=` 文字自己承認是「B14 備援路線」。
- 全 repo 搜尋 `h5-session`/`h5_session`：**只有 `bridge_server.py` 自己
  的定義/讀取**，沒有任何 README、`HANDOFF.md`、`HANDOFF_DRYRUN.md`、
  `DEMO_RUNBOOK.md` 提到這個旗標。
- `DEMO_RUNBOOK.md` 明講「`E01`-`E08` 全部跳過」——這條路徑唯一存在的
  理由（`E08` 的「真板子壞掉時的示範備援」）**目前一次都沒被實際跑過**。
- 結論：目前找不到任何文件或使用紀錄顯示這個旗標還在被誰依賴，看起來
  是純粹的遺跡，但沒有查到 100% 排除的辦法（沒有人能替沒發生過的使用
  紀錄背書）。

#### 4. 刪掉會不會有東西壞——⚠️ 有一個不明顯的風險

直接刪 `host/storage/mel_writer.py` 這個檔案：

- `host/storage/test_mel_writer.py` 的 4 個測試會直接壞掉（`ImportError`），
  這個很好預期。
- **不明顯的風險**：`bridge_server.py:108-109` 把
  `wav_to_log_mel_timed`（來自 `host/features/mel_pipeline.py`，這支
  才是真正在算 mel 的邏輯）跟 `write_mel_to_trial`（`mel_writer.py`）
  **放在同一個 `try/except ImportError` 區塊裡一起 import**。如果直接
  刪掉 `mel_writer.py` 而不同時修改 `bridge_server.py:108-109` 的 import
  結構，`from host.storage.mel_writer import write_mel_to_trial as _t`
  會拋 `ModuleNotFoundError`（`ImportError` 的子類），被同一個
  `except ImportError` 接住，`_load_mel_backend()` 從此對每次呼叫都回
  `False`——**結果是整個 mel backend（含 `.npy` 這條完全正常、沒有
  schema 問題的路徑）被一起關掉**，不只是 `--h5-session` 那段有問題的
  程式碼停用。這是「只刪 `mel_writer.py`」這個最直覺的修法會踩到的坑，
  修的時候要**同時**把 `bridge_server.py` 的 import 拆開（或者把
  `write_mel_to_trial` 的 import 移到只在 `--h5-session` 真的有給值時
  才嘗試），這正是為什麼這輪只回報、不動手——這個坑本身就落在
  `bridge_server.py`，屬於 ed／`7c [c32fd9]` 現在正在動的檔案範圍。

**兩種修法都要有人排進 story**：(a) 讓 `_process_mfcc()` 改走
`TrialStateMachine`/`SessionWriter` 的正式路徑，`mel_writer.py` 整個退場；
或 (b) 保留 `--h5-session` 這條備援路線，但至少讓它一起寫 `mel_t_us`
（目前完全沒有算真正的時間戳，用 dataset index 當 timestamp 也不對，
`_process_mfcc()` 現有的資訊裡沒有裝置時鐘可以推），並且修 import 順序
避免上面那個風險。**這輪按邊界規定不動手，只回報。**

---

## 第二部分：schema 供需對帳表

依 `CONTRACTS.md` §2（含這輪新增的 `energy_mu`/`sigma`、`comparable`）逐欄位。
「寫」看 `session_writer.py` 的 `REQUIRED_META_KEYS`/`OPTIONAL_META_KEYS`/
`REQUIRED_TRIAL_ATTRS`/`write_trial()` 簽章，加上真正呼叫它的產線
（`host/storage/baseline.py`、`host/trial/state_machine.py`）；
「讀」看 `analysis/` 底下（含 `session_loader.py` 自己）跟
`vl53l7cx_test/monitor/bridge_server.py`。

### 情況一：有寫、有讀（正常，列出來只是完整性）

| 欄位 | 寫 | 讀 |
|---|---|---|
| `subject`, `session_date`, `wear_id`, `mode`, `distance_mm`, `angle_deg`, `ambient`, `notes`, `fw_sha`, `proto_version`, `tof_dim`, `schema_version` | `SessionWriter._write_meta()`（必填） | `session_loader.py` 泛用 `meta` dict 曝露；`wear_id` 被 `SessionData.wear_ids`/`crosstalk_pairs()` 實際用到 |
| `baseline_mu_A/B`, `baseline_sigma_A/B` | `SessionWriter._write_meta()`（必填） | `SessionData.baseline()`；`bridge_server.read_baseline_thresholds()` |
| `sensors_enabled`, `sensors_enabled_confirmed` | `SessionWriter._write_meta()`（選填，成對）；`bridge_server.py` 的 `sensors_enabled_string()` 供值 | `SessionData.sensors_enabled`/`sensors_confirmed`；`crosstalk_pairs()` 拿它配對 C0 |
| `label`, `trial_idx`, `wear_id`, `mode`, `valid_zone_ratio`, `drop_count`, `quality` | `write_trial()`（必填） | `Trial` dataclass 明確欄位；`usable_trials()` 用 `quality` 過濾 |
| `tof_A/B`, `tof_t_us`, `tof_valid_A/B`, `mic_rms` | `write_trial()`（必填 dataset） | `Trial` dataclass 明確欄位；`stacked_tof()` |
| `mel` | `write_trial()`（選填） | `Trial.mel`；`run_all.py` 的 `mel_features()` |
| `tof_ambient_A/B` | `write_trial()`（選填，全有或全無） | `Trial.ambient_a/b`；`stacked_ambient()`、crosstalk 報告 |
| `speaking_mode` | `write_trial()`（選填，B21 已接線，目前固定 `"normal"`——見 `E2E_PIPELINE.md`） | `Trial.speaking_mode`（曝露了，但下面歸類在情況二：D-track 還沒有任何程式碼真的讀它） |
| `vad_start_us`, `vad_end_us`, `lip_onset_us`, `voice_onset_us`, `lip_onset_us_A`, `lip_onset_us_B`, `comparable` | `write_trial()`（選填；`vad_*`/`lip_onset_us`/`voice_onset_us` 見上方已知缺口，`lip_onset_us_A/B`/`comparable` 是這輪剛加、`4f` 剛接的） | **這輪剛接上**：`analysis/experiments/d14_viseme_sensitivity.py` 新增 `lip_lead_samples()`/`compare_lip_lead_versions()`，走 `Trial.attrs`（不直接開 h5py，`comparable` 的 `numpy.bool_` 陷阱由 `session_loader._as_scalar()` 擋掉）。只納入 `comparable is True` 且 `voice_onset_us` 存在的 trial；`lip_onset_us_B` 缺席（`union_min` 設計本身允許）只影響 `single_b` 這個版本，不影響 `fused`/`single_a`。**目前只用合成資料測過篩選邏輯本身正確，真實資料到手前不產出任何「哪個版本比較好」的結論**（`reports/VAD_FUSION_OPTIONS.md` 問題 4） |

### 情況二：有寫、沒人讀（浪費但無害）——9 筆

| 欄位 | 誰在寫 | 為什麼現在沒人讀 |
|---|---|---|
| `speaking_mode` | `write_trial()`，B21 已接線 | D-track 還沒有 normal/whisper/silent 的分軌分析——`Trial.speaking_mode` 已曝露，是**準備好了、等 D 軌來用**，不是缺陷 |
| `vad_confidence` | `write_trial()`，選填 | 同上，B15 有算，`analysis/` 沒有消費點 |
| `noise_floor_mu`, `noise_floor_sigma` | `SessionWriter._write_meta()`，必填 | `bridge_server.read_baseline_thresholds()` 有讀（給 VAD 用），但 `analysis/` 沒有任何實驗直接用它算東西 |
| `clock_slope`, `clock_offset`, `clock_residual_p95`, `clock_drift_us`, `clock_drift_ppm`, `clock_sync_span_us`, `clock_sync_confirmed`, `session_start_*`, `session_end_*`, `clock_cross_check_ppm_diff`, `clock_cross_check_ok` | `SessionWriter._write_meta()`，必填/`finalize_session_end()` | 純校時診斷欄位，`analysis/` 沒有任何程式碼檢查它們——**這點比單純的「還沒輪到」更值得注意**：如果一個 session 的 `clock_sync_confirmed=False` 或 `clock_cross_check_ok=False`，代表這個檔案的時間戳可能不可信，但目前**沒有任何分析流程會因此警告或排除它**。不算這次的三種歷史 bug模式，但值得另開一個 story 評估要不要在 `availability()`／`verification_report.py` 加一道檢查 |
| `mic_peak` | `write_trial()`，必填 dataset | **這輪已把「結構性讀不到」的部分解掉**：`Trial` dataclass 現在有 `mic_peak` 欄位了（原本 `mic_rms` 有、`mic_peak` 沒有）。但介面通了不等於有人在用——`analysis/` 目前還沒有任何削波偵測一類的實驗需要它，仍然歸在「有寫、沒人讀」，只是不再是「連讀都讀不到」 |
| `mel_t_us` | `write_trial()`，跟 `mel` 成對必寫 | 同上，這輪加進 `Trial.mel_t_us`，結構性缺口解掉了；`analysis/` 還沒有任何要對齊 mel 幀與其他串流時間戳的分析會用到它 |
| `tof_ambient_t_us` | `write_trial()`，跟 `tof_ambient_*` 成對必寫 | 同上，這輪加進 `Trial.ambient_t_us` |
| `audio`, `audio_t0_us` | `write_trial()`，選填 | `Trial` dataclass完全沒有 `audio` 欄位；目前沒有任何錄音管線真的餵 `audio` 給 `write_trial()`（`schema_example.py` 的範例檔是唯一寫過的地方），純示範用途 |
| `source` | `SessionWriter._write_meta()`，選填——**這輪加了**（原本是情況四，見下方「已解決」） | 呼叫端（`bridge_server.py`）一直都在傳，現在寫入端接住了；`analysis/` 目前還沒有人讀，跟上面幾筆一樣屬於「接口通了、還沒被用」 |

`mic_peak`/`mel_t_us`/`tof_ambient_t_us`/`source` 這四筆**這輪把「讀不到」
的技術缺口解掉了，但沒有升級成情況一**——情況一要求真的有消費者，這四筆
目前都還沒有，跟上面 `speaking_mode`/`vad_confidence` 是同一種「準備好、
等人來用」的暫態，不是已經被驗證過的資料流。

**這輪移出這張表的 7 筆**（`vad_start_us`、`vad_end_us`、`lip_onset_us`、
`voice_onset_us`、`lip_onset_us_A`、`lip_onset_us_B`、`comparable`）——
`d14_viseme_sensitivity.py` 剛接上讀取端，移到上面「情況一：有寫、有讀」，
見下方「三道關卡」小節的更新。

### 一個欄位的三道關卡：`lip_onset_us_A`/`lip_onset_us_B`

這筆值得單獨記錄，因為它示範了調度員點名的重點——**一個 schema 欄位要
真的「有寫有讀」，中間有好幾道關卡，任何一道卡住都不會變紅**：

1. **契約有定義、兩邊都沒實作**（今天寫這份報告的當下之前）——調度員
   剛裁定用 `union_min` 融合策略，但 schema 裡完全沒有存放「融合前」的
   欄位。這個階段如果有人去讀，`session_loader.py` 連 key 都不知道要找。
2. **有寫、沒人讀**（這輪剛完成的狀態，寫這節的當下）——`session_writer.py`
   已經加了 `lip_onset_us_A`/`lip_onset_us_B`（選填、各自獨立、沒偵測到
   就整個省略），`4f` 會接 `state_machine.py` 去真的填值，但 `analysis/`
   還沒有任何程式碼讀它。
3. **有寫有讀**（這輪完成）——`d14_viseme_sensitivity.py` 的
   `lip_lead_samples()`/`compare_lip_lead_versions()` 已經接上，走
   `Trial.attrs`（不直接開 h5py），篩選規則（`comparable is True`、
   `voice_onset_us` 必須存在、`lip_onset_us_B` 缺席只影響 `single_b`）
   跟 `CONTRACTS.md` §2 的語意逐條對齊。**但「有寫有讀」不等於「已經能
   回答當初選 `union_min` 對不對」**——現在讀到的還是合成資料，這一關
   只證明「程式邏輯正確、篩選不會靜默納入不該納入的樣本」，`E05` 的
   真實資料到手之後才能真的回答那個問題。

**中間任何一關卡住，程式都不會報錯、測試都不會變紅**——這正是這份報告
存在的理由：不是等 bug 出現才回頭查，是先把「這個欄位現在卡在哪一關」
寫下來，讓下一個接手的人一眼看到，而不是要靠 `grep` 才發現。

### 情況三：有人讀、但沒人寫（目前 0 筆，但有一筆「今天稍早才不是 0」）

**目前掃過去沒有找到活生生的這一類**——這輪最重要的發現是下面這個
**已經自己解決、但值得記錄的案例**，因為它精準對上調度員說的三個歷史模式：

> #### 案例：`energy_mu`/`energy_sigma`（今天生效前，曾經是這一類）
>
> `vl53l7cx_test/monitor/bridge_server.py` 的 `read_baseline_thresholds()`
> 早就已經在讀 `/meta` 的 `energy_mu`/`energy_sigma`（程式碼裡的註解寫著
> 「esp-mask-test-18's writer change for energy_* has not landed yet」——
> ed 明確知道寫入端還沒接，用 `.get()` 防禦性地寫，缺席就印警告、繼續用
> 估的門檻）。
>
> 在這輪之前，`session_writer.py` 的 `_write_meta()` **只寫
> `REQUIRED_META_KEYS`**，`energy_mu`/`energy_sigma` 就算 `baseline.py`
> 算好、放進 `meta` dict 傳進來，也會被**整個丟掉**——`bridge_server.py`
> 每次都會印出 `⚠ VAD thresholds missing from /meta`，`detect_lip_activity()`
> 永遠退回自己估的門檻，而 `B16` 量到那個估法**系統性偏嚴約 23%**，
> 讓 `lip_onset_us` 系統性偏晚，**而 `D14` 唯一要量的東西
> （`measure_lip_lead()` 的唇動領先量）會被系統性低估——沒有任何錯誤
> 訊息，兩邊的測試各自都是綠的**。
>
> 這正好符合調度員點名的第三個歷史案例（`recognition_service.py` 呼叫舊
> 校準、兩邊測試都綠、100% 誤拒）的形狀：**寫入端沒有欄位可以接住，
> 讀取端已經在等，而且會安靜地退化，不會報錯。**
>
> **這輪已經修了**（`session_writer.py` 加 `OPTIONAL_META_KEYS` 的
> `energy_mu`/`energy_sigma`），`bridge_server.py`/`host/trial/
> state_machine.py` 的呼叫鏈已經追過一次確認 key 名稱完全對得上
> （`baseline_mu_A` 等到 `energy_sigma`，`**vad` 展開餵進
> `TrialStateMachine.__init__` 的每個關鍵字參數都一一核對過）。列在這裡
> 是為了證明這張表**真的能抓到這一類 bug**，不是憑空舉例。

**目前的欄位裡沒有找到第二個活生生的例子。**

### 情況四：契約有定義、兩邊都沒真的實作（目前 0 筆——`source` 這輪已解決）

`source` 原本就落在這一類，寫上一版報告時的狀態是：

- **契約定義了**（`CONTRACTS.md` §2 明確列出 `source`）
- **呼叫端努力要寫**：`bridge_server.py` 組 baseline 的 `meta` dict 時，
  確實把 `"source": link_source["value"]` 放進去了，程式裡自己的註解寫
  「Passed, but currently dropped: `SessionWriter._write_meta` only writes
  `REQUIRED_META_KEYS`……the moment `source` joins the schema this starts
  working with no change on this side」
- **但寫入端真的沒接住**：`session_writer.py` 完全沒有欄位可以接住它
- **也沒有人在讀**：`analysis/` 沒有任何地方讀 `meta.get("source")`

跟 `energy_mu` 那個案例的差別是**還沒有人開始讀**，所以當時拿到的不是
「安靜的錯誤答案」，是單純的「這個欄位不存在」——**是還沒發作的
`energy_mu`**。修法正是預告過的那樣：把 `source` 加進
`session_writer.py` 的 `OPTIONAL_META_KEYS`，值域跟 `bridge_server.py`
的 `VALID_SOURCES` 同步（`live`/`mock`/`replay-log`/`replay-session`），
**這輪已經做完**（`host/storage/test_session_writer.py` 新增 4 個測試，
含四個合法值各自都能收）。`bridge_server.py` 那端沒有動——它本來就
已經在傳，不需要改。

`analysis/` 目前仍然沒有人讀 `source`，所以它現在的正確歸類是「情況
二：有寫、沒人讀」（已經搬過去），不是情況一——**接口通了不等於已經
有人在用**，跟 `mic_peak`/`mel_t_us`/`tof_ambient_t_us` 是同一種暫態。

---

## 這輪順手修的文件遺漏

`CONTRACTS.md` §2 的 schema 列表原本漏列 `sensors_enabled_confirmed`
（`sensors_enabled` 的確認旗標，程式碼裡一直都有、`session_loader.py`
的 `sensors_confirmed` 也真的在讀，只是列表忘了寫）——已補上一行，
不影響任何邏輯，純粹讓這份供需對帳表本身站得住腳。

## 第二輪：結清 `mic_peak`/`mel_t_us`/`tof_ambient_t_us`/`source`

- `analysis/reporting/session_loader.py`：`Trial` dataclass 新增
  `mic_peak`/`mel_t_us`/`ambient_t_us` 三個欄位，`load_session()` 對應
  讀出來（`mic_peak` 必填直接讀，`mel_t_us`/`ambient_t_us` 跟著
  `mel`/`tof_ambient_A` 是否存在來決定讀不讀，選填時缺席回 `None` 不報錯）。
  `analysis/reporting/test_run_all.py` 新增 2 個測試：有 mel/ambient 時
  三個新欄位都讀得到且長度跟資料對得上，沒有 mel/ambient 時新欄位是
  `None` 不是空陣列或報錯。
- `host/storage/session_writer.py`：`OPTIONAL_META_KEYS` 加入 `source`，
  值域跟 `bridge_server.py` 的 `VALID_SOURCES` 同步
  （`live`/`mock`/`replay-log`/`replay-session`），沒給就整個 attr 不寫入。
  `host/storage/test_session_writer.py` 新增 4 個測試（省略、給值、四個
  合法值各自都能收、非法值拒絕）。
- 都跑過對應測試套件確認全線通過，見文末。

## `mel_writer.py`：偵察清單交出去，沒有動手刪

見上方第一部分的完整偵察小節（誰引用、`bridge_server.py` 精確到哪個
函式與哪一行、`--h5-session` 有沒有人在用、刪掉會不會連累
`wav_to_log_mel_timed` 這條完全正常的 `.npy` 路徑）。**這輪沒有刪除或
修改 `mel_writer.py`**，也沒有動 `bridge_server.py` 的 import 結構。

---

## 沒有動的檔案

`host/storage/baseline.py`、`host/trial/state_machine.py`（4f 正在改）、
`vl53l7cx_test/monitor/bridge_server.py`（ed／`7c [c32fd9]`／`7c [4bedc9]`
三個人同時在改）——全程只讀不改，上面每一筆涉及這些檔案的發現都只回報，
沒有動手修，也沒有刪除 `host/storage/mel_writer.py`（只列清單）。
