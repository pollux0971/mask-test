# D21 獨立審查——快速閉集合相似度探針

> **審查方法**：唯讀 `analysis/similarity/closed_set_probe.py`、
> `analysis/experiments/exp_d21_closed_set_probe.py`、
> `analysis/similarity/test_closed_set_probe.py`、規格
> `ssi-backlog/stories/D-analysis/D21.md`，以及
> `analysis/experiments/output/d21/` 的兩份實際輸出（trimmed/untrimmed）。
> 跑過一次 `pytest analysis/similarity/test_closed_set_probe.py`
> 確認目前是綠的（15 passed），沒有改任何程式碼。

## 結論先講：**可以信，六項逐一查過，五項乾淨，一項是「證據存在但沒被展示出來」，不是 bug**

---

## 1. 🔴 VAD 裁切真的有生效嗎？——**查過了，確實有生效**

機制面：`host/features/live_pipeline.py:282-284`
`assemble_query_from_aligned_frames()` 在 `speech_window.window_us` 非
`None` 時，會先用 `window_start_us <= f.t_us <= window_end_us` 過濾
`usable` 幀，**過濾之後才做固定長度重採樣**——不是「算了個窗口但沒真的
拿去用」這種常見的坑，逐行核對過確實有接上。

**證據面（`analysis/experiments/output/d21/`）**：兩份 JSON 對照看：

| | trimmed | untrimmed |
|---|---|---|
| `speech_window` | `window_us=[1299986, 1999981]`（跟 `SPEECH_START_FRAME=42`≈1.4s、`SPEECH_END_FRAME=57`≈1.9s 各加減 100ms margin 完全對得上） | `null`（沒嘗試裁切） |
| `separability_ratio`（8 個詞） | 1.23 – 1.37 | 0.9995 – 1.09（**有一個詞的比值 < 1**，代表在未裁切空間裡它離別的詞比離自己還近） |
| ToF 軌 top-1 到第二名的**margin**（歐氏） | 59.59 − 44.26 = **15.33** | 47.77 − 46.44 = **1.33** |

**margin 從 15.33 掉到 1.33，縮小了約 11.5 倍**——這是裁切有沒有生效
最直接的量化證據，比看 rank 準確得多（下面第 1.1 點解釋為什麼）。

### 1.1 ⚠️ 一個真的問題，但不是 bug：demo 的**輸出格式**沒有把這個效果講清楚

**這是我自己查出來的，不在 `ed` 列的六點裡，但屬於同一個驗收條件
（「未裁切 vs 已裁切的對照實驗：證明距離確實會擠成一團」），所以放在
這裡一起報。**

`--demo` 產生的兩份 JSON、以及 `run_probe()` 印到 stdout 的
`format_probe_report()`，**兩者的 `rank_of_true_label` 在 trimmed 跟
untrimmed 都是 1**——因為這份 demo 用的合成訊號振幅（`TOF_SIGNAL_AMP_MM
=30`）是雜訊標準差（`TOF_NOISE_STD_MM=5`）的 6 倍，訊噪比刻意調得很高，
就算被 86% 靜音稀釋，殘留的訊號還是足夠讓正確答案**勉強**排第一。

**如果一個人只看 demo 印出來的報告或存下來的 JSON**（不去讀
`test_closed_set_probe.py` 的原始碼），會看到「trimmed 排名 1、
untrimmed 也排名 1」，**很容易誤讀成「裁不裁切結果一樣，沒差」**
——完全錯過驗收條件真正想證明的事（距離擠在一起、margin 消失、
只是這次雜訊還沒大到把排名也翻過去）。

**真正嚴謹證明這件事的，是 `test_closed_set_probe.py:320` 的
`test_untrimmed_probe_scores_collapse_near_uniform`**：它算的是**原始
（未正規化）距離的離散係數 `std/mean`**，斷言 `< 0.15`，並且在註解裡
明確講了**為什麼不能用 `fused` 軌的正規化距離來判斷**（`normalize_
distances()` 不管輸入擠不擠，輸出永遠被拉開到差不多的範圍，拿它當量尺
是錯的）——這段推理是對的，測試本身也真的在跑（`pytest` 綠燈）。

**所以結論是**：這個驗收條件**在測試層是被正確、嚴謹地驗證了**，
不是空話。**問題只在「留給未來的人的證據」這個 demo CLI 的輸出本身**
——不管是 stdout 報告還是存下來的 JSON，都沒有把這個 CV 數字算出來
顯示，讀 demo 輸出的人看不到真正的證明，只會看到兩個都排第一，容易
得出錯誤結論。**建議（只提案，沒有改程式碼）**：`run_probe()` 在
`payload` 裡順手加一個 `raw_distance_cv`（`tof_raw.std()/tof_raw.mean()`）
欄位，trimmed/untrimmed 兩份都存，這樣光看 JSON 就能看到那個關鍵數字，
不用去翻測試原始碼才知道。

---

## 2. 🔴 兩個 VAD 都偵測不到時真的會報錯嗎？——**查過了，會，而且這條路徑今天真的被走到過**

`require_speech_window()`（`closed_set_probe.py:90-108`）在
`trial_speech_window()` 回傳 `source == "none"` 時丟
`NoSpeechDetectedError`（`ValueError` 子類別），訊息明確講「唇動（A/B）
與語音 VAD 全部偵測不到起訖時間……必須重錄」。

追到底層：`compute_speech_window()`（`host/features/live_pipeline.py:127-132`）
只在**所有** segments 的 `start`/`end` 都是 `None`（`usable` 清單為空）
時才回 `source="none"`——只要唇動 A、唇動 B、語音三者**任一個**有值就不會
觸發，符合 story「取聯集」的設計，不會誤報。

`test_require_speech_window_raises_when_nothing_detected`
（`test_closed_set_probe.py:117`）直接測了這個情境，通過。

⚠️ **確認過跟今天 `8f` 遇到的「唇動偵測完全偵測不到」是不同情境**：
`8f` 那次是**單一**唇動來源（例如只有 lip_A）偵測不到，但語音或另一顆
唇動還有值——這種情況 `source` 不會是 `"none"`（聯集邏輯會用有值的那些），
`require_speech_window()` 不會報錯，這是對的（半數來源失效不代表整段
錄音沒法用）。**只有「唇動 A、唇動 B、語音三者全部同時失效」才會觸發
硬性報錯**——這條路徑存在、也測過，但**目前手上沒有一次「三個 VAD
來源全滅」的真實案例走過這條路**（`8f` 那次不是三個全滅），所以只能說
「程式碼邏輯正確、單元測試通過」，還沒有真實資料驗證過這個最極端的分支
——這不是程式碼的問題，是目前沒有這麼糟的真實資料可以拿來驗證，
記錄下來讓之後的人知道這條分支的驗證狀態。

---

## 3. `silent` 模式下只用唇動，真的能裁嗎？——**查過了，正確**

`trial_speech_window()` 把 `voice_onset_us` 一起塞進 segments 清單，
`compute_speech_window()` 內部用 `if s is not None and e is not None`
過濾——`silent` 模式下 `voice_onset_us` 是 `None`（`CONTRACTS.md` §2.2
約定），會自動被濾掉，不需要特判，聯集邏輯本身就會退化成「只用唇動」。

`test_trial_speech_window_silent_mode_uses_lips_only`
（`test_closed_set_probe.py:105`）直接測了這個案例（`lip_onset_us_B`
跟 `voice_onset_us` 都是 `None`，只有 `lip_onset_us_A` 有值），
確認 `source == "lip_A"`、視窗算對，通過。

---

## 4. 🔴 可分性比值的分母：同類只有 1 筆時會除以 0 或回 NaN 嗎？——**查過了，不會，明確回傳 `None`**

`separability_ratio()`（`closed_set_probe.py:154-185`）在
`len(templates) < 2` 時直接 `ratios[label] = None; continue`，
**在算中位數/除法之前就攔下來**，不會有機會產生除以零或 NaN。
文件字串（docstring）明確講了為什麼不用 `0` 或 `inf` 代替——那兩個都會
被誤讀成「非常可分」或「完全不可分」，`None` 才誠實。

`test_separability_ratio_known_case`（`test_closed_set_probe.py:45`）
用手算過的小案例驗證：class "a" 兩筆（自距離中位數=2）、class "b" 一筆
——斷言 `ratios["a"] == 8.0/2.0`（正確算出來）、`ratios["b"] is None`
（1 筆的類別正確回 `None`）。**這正是使用者第一次錄音很可能發生的情況
（每個詞只錄 1-2 次）**，已經有測試覆蓋，不是空想的 edge case。

順帶查了分母恰好是 0 的情況（兩筆完全相同的樣板）：`median_self >
NORM_EPS` 這個判斷式擋住了（`NORM_EPS=1e-9`），退化成 `inf` 而不是
`ZeroDivisionError`，也不會是誤導性的「非常小的正數除出一個天文數字」。

---

## 5. 隨機基準：`(N+1)/2`，N=8 時是 4.5——**對，而且有測試釘住 story 原文的例子**

`expected_random_rank()`（`closed_set_probe.py:243-245`）就是
`(n_candidates + 1) / 2.0`。`test_expected_random_rank_matches_story_example`
直接斷言 `expected_random_rank(8) == 4.5`（跟 story 原文的例子逐字對
上），還多測了 `N=1 → 1.0`、`N=3 → 2.0` 兩個邊界，數學上就是「1 到 N
均勻分布的期望值」，沒有問題。

---

## 6. ⚠️ `7c` 誠實回報「可分性比值沒過 1.5 門檻」——**查過了，是隨機單位向量的自然結果，不是 bug**

`_class_signature(rng, n_dims)`（`test_closed_set_probe.py:215-217`）：
`v = rng.normal(size=n_dims); return v / np.linalg.norm(v)`——**純粹的
隨機高斯向量再正規化成單位向量，沒有任何刻意拉開類別間距的邏輯**（不是
正交基底、不是均勻分散在球面上、沒有最小角度保證）。這是刻意的老實選擇：
如果為了讓 demo 好看而人工保證類別分開，demo 本身就失去意義了（會變成
「證明我自己設計的東西可以分開」，不是「證明這個 pipeline 對隨機訊號
的行為正確」）。

`separability_ratio()` 的計算本身（第 4 點已驗證）是對的，`demo` 用的
`class_sigs` 也是同一套隨機生成邏輯（`exp_d21_closed_set_probe.py:159`
呼叫的正是 `test_closed_set_probe._class_signature`，兩邊共用同一個
生成器，沒有各自維護一份可能不一致的邏輯）——**trimmed 情況下比值落在
1.23-1.37（沒過 1.5），是「8 個隨機單位向量剛好抽到這麼近」的自然結果，
換一個隨機種子完全可能超過 1.5，也完全可能更低**。`7c` 誠實回報「沒過
1.5」是對的態度，這個數字本身不代表 ToF 硬體不行，只代表**這次demo用的
隨機種子抽出來的合成詞剛好不夠分散**——`demo` 本來就不是拿來證明「ToF
有沒有用」，story 自己也講清楚了 `--demo` 只是驗證 pipeline 機制本身
（不需要硬體、不需要真的錄音），真正回答「ToF 有沒有攜帶詞彙資訊」要
等真的錄了才有意義。

---

## 總結：有沒有任何情況會讓它給出「看起來合理但錯誤」的答案？

**六項逐一查過，五項（2/3/4/5/6）程式碼與測試都乾淨，沒有找到會安靜給
錯答案的路徑。唯一的問題是第 1 點：demo CLI 的輸出格式沒有把「距離确实
擠成一團」這個關鍵證據（原始距離的離散係數）算出來顯示——這不會讓
`probe_three_track()`/`separability_ratio()` 本身算錯任何東西，程式碼
邏輯是對的，`pytest` 也驗證過**，但如果有人只看 `--demo` 印出來的報告
或存的 JSON、不去讀測試原始碼，**有可能誤以為「裁不裁切結果一樣」，
低估 VAD 裁切這個前提有多重要**——這是一個溝通/呈現層的缺口，不是
邏輯錯誤，建議是加一個 `raw_distance_cv` 欄位到 demo 輸出（只提案，
沒有改程式碼）。

**可以信這份工具給出的排名跟可分性比值數字，等接頭修好、真的錄了資料
之後，`--session` 模式的結果是可以直接拿來做「值不值得投入 `E05`」
這個決定的。**
