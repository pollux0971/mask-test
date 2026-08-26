# Schema 供需對帳：誰繞過 session_loader，誰寫了沒人讀，誰讀了沒人寫

> 方法：`grep` 全 repo 找出每一個直接開 h5py 的地方，讀懂它在幹嘛；
> 再把 `CONTRACTS.md` §2 的每一個欄位，對照 `host/storage/session_writer.py`
> （唯一的正式寫入端）跟實際的呼叫端、`analysis/` 底下的每一個消費點，
> 逐一分類。**這輪只盤點，不修**——除了兩處落在自己檔案裡、明顯是文件
> 遺漏的小 Edit（見文末）。

## 結論先講

- 掃過的直接 h5py 存取有 **6 處**，其中 5 處是刻意、窄範圍、風險低；
  **1 處是死碼**（`host/storage/mel_writer.py`，寫出來的檔案會違反現在的
  schema）。
- 供需對帳表裡，🔴 **第三類（有人讀、沒人寫）目前是 0 筆**——但這不是
  一直都成立：`energy_mu`/`energy_sigma` **今天稍早**曾經正是這一類，
  `bridge_server.py` 已經在讀、寫入端卻還沒接住，直到這輪把它加進
  `session_writer.py` 才補上。細節見下方，這是這張表唯一「抓到現行進行式
  的那三種歷史 bug」的一筆，不是舉例。
- 第二類（有寫、沒人讀）有 **12 筆**，大多是「D14/D-track 還沒做完」的
  正常暫態，不是 bug；但其中 `mel_t_us`/`tof_ambient_t_us` 是**結構性**
  讀不到（`session_loader.py` 的 `Trial` dataclass 沒有對應欄位，不是
  「剛好沒人寫」），值得記一筆。

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
| `host/storage/mel_writer.py` | 寫 | dataset：`mel`（**沒有** `mel_t_us`） | 🔴 **不是**——見下方 | 🔴 **高，但目前無害**，因為死碼（見下） |
| `ssi-backlog/tools/schema_example.py` | 寫 | 透過 `SessionWriter`，不是直接開檔 | 不算繞過 | 無 |

### 🔴 死碼：`host/storage/mel_writer.py`

自己的文件字串就寫明：

> 「B07 完成後，這支函式應該被併進 `SessionWriter`……現在只是先讓 B14
> 能被驗收」

`SessionWriter.write_trial()` 早就已經吃 `mel`/`mel_t_us` 了（B07/B14 都已
落地），但這支模組沒有跟著退場。它唯一的呼叫點在
`bridge_server.py`（`_handle_mfcc_ready` 附近，`--h5-session` 這條**舊的、
B09/B11 之前的示範路徑**，程式裡自己的註解寫著「B09（session/trial 狀態
機）還沒做，這裡先用『每次錄音自動 +1』模擬 trial_idx」——現在 B09/B11
早就做完了，真正的 `/trial/*` 走 `TrialStateMachine.write_trial()`，兩條
路徑不會同時對同一個 trial 動作，所以**目前**不會互相覆寫打架。

但只要有人還在用 `--h5-session` 這個舊旗標，`write_mel_to_trial()` 會寫出
一個**違反現行 schema** 的檔案：`mel` 沒有搭配的 `mel_t_us`。
`session_loader.py` 讀取時只檢查 `"mel" in group` 就直接讀，不會報錯，
但會產生一個「有 `mel` 卻沒有 `mel_t_us`」的檔案——如果將來有任何程式碼
假設「有 `mel` 就一定有 `mel_t_us`」（`_validate_mel()` 在 `SessionWriter`
這一側保證了這件事，容易讓人誤以為所有 `mel` 都成對），會在這種檔案上
悄悄壞掉。

**這是 `bridge_server.py` 裡的呼叫點，屬於 ed／`7c [c32fd9]` 正在動的檔案
範圍，這輪按邊界規定不動，只回報。**`host/storage/mel_writer.py` 本身
沒有主人明確認領，兩種修法都值得考慮：退場（呼叫點改用
`TrialStateMachine`／`SessionWriter` 的正式路徑）或至少讓它一起寫
`mel_t_us`（用 dataset 的 index 當 timestamp 顯然不對，真正的時間戳
`--h5-session` 這條路徑目前也沒有在算）。

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
| `mic_peak` | `write_trial()`，必填 dataset | `Trial` dataclass 沒有 `mic_peak` 欄位（`mic_rms` 有，`mic_peak` 沒有）——**結構性讀不到**：不是「沒人剛好去讀」，是 loader 從沒把它接進 `Trial`。目前沒有實驗需要峰值（跟削波偵測相關，`D` 軌尚未有這類實驗），先記錄 |
| `mel_t_us` | `write_trial()`，跟 `mel` 成對必寫 | **結構性讀不到**：`Trial.mel` 有暴露，但沒有對應的 `Trial.mel_t_us`——任何要對齊 mel 幀與其他串流時間戳的分析（例如驗證 D14 的 `lip_onset_us` 落在哪個 mel 幀）目前做不到，要先加欄位 |
| `tof_ambient_t_us` | `write_trial()`，跟 `tof_ambient_*` 成對必寫 | 同上，`Trial.ambient_a/b` 有暴露，沒有 `Trial.ambient_t_us` |
| `audio`, `audio_t0_us` | `write_trial()`，選填 | `Trial` dataclass完全沒有 `audio` 欄位；目前沒有任何錄音管線真的餵 `audio` 給 `write_trial()`（`schema_example.py` 的範例檔是唯一寫過的地方），純示範用途 |
| `source` | **半供應**——見情況三前的特別說明 | 見下 |

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

**目前的欄位裡沒有找到第二個活生生的例子**——但 `source` 接近：見下方。

### 情況四：契約有定義、兩邊都沒真的實作（半路的例外：`source`）

`source` 是這四種情況裡最特殊的一個，嚴格來說不完全屬於任何一類：

- **契約定義了**（`CONTRACTS.md` §2 明確列出 `source`）
- **呼叫端努力要寫**：`bridge_server.py` 組 baseline 的 `meta` dict 時，
  確實把 `"source": link_source["value"]` 放進去了，程式裡自己的註解寫
  「Passed, but currently dropped: `SessionWriter._write_meta` only writes
  `REQUIRED_META_KEYS`……the moment `source` joins the schema this starts
  working with no change on this side」
- **但寫入端真的沒接住**：`session_writer.py` 的 `REQUIRED_META_KEYS`/
  `OPTIONAL_META_KEYS` 都沒有 `source`，這個值進了 `_write_meta()` 就被
  忽略，`/meta` 裡完全沒有這個 attr
- **也沒有人在讀**：`grep` 過 `analysis/`，沒有任何地方讀 `meta.get("source")`

跟 `energy_mu` 那個案例的差別：**還沒有人開始讀**，所以現在拿到的不是
「安靜的錯誤答案」，是單純的「這個欄位目前不存在」——**還不是活的 bug，
是下一個 `energy_mu`**。一旦哪天有人（例如 `E05` 要驗證四小時錄製真的是
拿真板子錄的，不是不小心接到 mock）開始寫 `meta.get("source")`，會拿到
`None`，而 `bridge_server.py` 那句註解已經先講好「這裡不用改」，所以
**修法非常小**：把 `source` 加進 `session_writer.py` 的
`OPTIONAL_META_KEYS`，跟 `sensors_enabled` 同一個模式。

這處落在自己的檔案（`session_writer.py`），但**這輪先不修**——照邊界規定
先盤點，等調度員決定要不要排進這輪或另開一個小 story（改動本身預期
不到 10 行，跟 `sensors_enabled` 那次一樣小）。

---

## 這輪順手修的兩處文件遺漏（都是自己先前編輯留下的缺）

`CONTRACTS.md` §2 的 schema 列表原本漏列 `sensors_enabled_confirmed`
（`sensors_enabled` 的確認旗標，程式碼裡一直都有、`session_loader.py`
的 `sensors_confirmed` 也真的在讀，只是列表忘了寫）——已補上一行，
不影響任何邏輯，純粹讓這份供需對帳表本身站得住腳。

---

## 沒有動的檔案

`host/storage/baseline.py`、`host/trial/state_machine.py`（4f 正在改）、
`vl53l7cx_test/monitor/bridge_server.py`（ed／`7c [c32fd9]` 同時在改）——
全程只讀不改，上面每一筆涉及這些檔案的發現都只回報，沒有動手修。
