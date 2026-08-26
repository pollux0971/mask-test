# 極值型統計量稽核

由來：`D14`（`esp-mask-test-7c`）發現 ToF 一個模態是 24 幀 × 32 通道 = 768
個值，即使完全沒有動作，其中最大的 `|z|` 期望就有 3.4，而 `medium` 門檻是
3.0——「什麼都沒發生」會被判成 `medium`。這種錯誤程式邏輯完全正確、任何測試
都抓不到，因為問題出在統計量本身：**跨很多樣本取極值（`max`/`min`/
`argmax`/`argmin`/接近 100 的 `percentile`）當判準時，樣本數越多，極值本身
就越容易偏離「正常」，如果門檻是固定常數、沒有考慮樣本數，就會系統性誤判。**

本稽核範圍：`analysis/similarity/`（`D04`–`D09`、`D22`）與
`analysis/experiments/` 裡我自己的檔案（`exp_a_snr.py`、
`exp_d05_dtw_vs_cosine.py`、`exp_d09_template_count_study.py`、
`exp_d10_crosstalk.py`、`exp_d12_wear_cv.py`、`exp_d22_reject_calibration.py`）；
另外掃了不屬於我的檔案供調度員參考（不編輯，只回報）。

## 方法

先用 `grep` 找出所有 `.max()`／`.min()`／`argmax`／`argmin`／`percentile(` 的
呼叫點，逐一判斷：**這是不是「拿極值去跟一個固定門檻比較，決定 pass/fail 或
reject/accept」**（真正有風險的用法），還是單純「找最大/最小值來顯示、排序、
或當分類預測的 argmax」（沒有機率地板問題，見下方「排除的用法」）。

對每個判定為有風險的用法，估計「樣本數是多少」「純雜訊下這個統計量的期望
落在哪裡」，跟現有門檻比較。

## 排除的用法（不是這次稽核的對象）

這些出現在 grep 結果裡，但不是「極值當判準」的模式，逐一說明為什麼不算：

| 位置 | 用法 | 為什麼安全 |
|---|---|---|
| `fusion.py` `argmax(scores)`、`enrollment.py` `argmin(d_class)`、`exp_d05...` `argmin(d_class)` | 分類預測（挑最像的類別） | argmax/argmin 一定會挑出**某一個**答案，沒有「跟固定門檻比較」這一步，不存在機率地板問題 |
| `scoring.py` `d_class.min()`（`normalize_distances` 裡）、`logits.max()`（softmax 數值穩定用） | 正規化/數值技巧 | 用來重新定尺度或避免溢位，不是拿來做 accept/reject 判斷 |
| `reject_calibration_roc.py` 的 `argmin`/`argmax`（`select_threshold`） | 從已經算好的 ROC 曲線挑操作點 | 曲線本身的 FRR/FAR 才是機率量，這裡只是挑點，機率地板問題在曲線的來源（見下方核心項目）已經處理 |
| `d21_signal_ablation.py` 的 `np.min(...)`（找最小類別的樣本**數**） | 整數計數，不是連續統計量 | 沒有連續分布的極值地板效應 |
| `d14_viseme_sensitivity.py` 的 `(finite.min()+finite.max())/2` | 熱力圖色階中點 | 純粹是畫圖用的色彩置中，不是判斷 |
| `d18_permutation_test.py`（`sklearn.permutation_test_score`） | 標準排列檢定 p 值 | 這是教科書式正確做法：p 值本來就是拿觀察統計量跟**用同一份資料重排出來的**虛無分布比較，天生就會隨樣本數調整，不會有這次稽核在講的那種偏差 |

## 核心項目：D06/D07/D08/D09/D22 的拒識判定（`d_class.min() > theta`）

**這是我自己的檔案裡風險最高、也是本來就在追蹤的項目**——`min` 有對稱的機率
地板問題：樣本數（樣板數、類別數）越多，「最近距離」的期望越小，如果門檻是
用另一組樣本數校準出來的常數，兩邊對不上就會系統性偏差。這正是 `D06` 發現
「樣板數量比例會影響誤拒率」、`D09` 發現「多錄樣板救不了」的根本原因。

**結論：舊方法（`enrollment.fit_reject_threshold`，單邊 LOO）有這個問題，
已知且已經被 `D22` 的雙邊 ROC 方法解決，但服務層還沒接上新方法**——見下方
「發現並修正」。

### 為什麼雙邊 ROC 能解決（不只是「我猜有」，附推導）

舊方法只用 `_reject` 類別自己的 leave-one-out 距離分布校準門檻——這組樣本的
「有幾個樣本在互相比」跟真正部署時「一個真詞 query 對自己類別樣板」的比較
結構完全不同（不同的樣本數、不同的母體），兩邊的極值地板天生就不一致。

雙邊 ROC 的兩個分布**各自都精確對應部署時真正會發生的統計量**：
- `compute_word_loocv_distances`：對每個詞類別自己做 LOOCV，量的就是「一個真詞
  query 對自己類別樣板」在部署時的距離分布——n_classes 不影響這個量（只要類別
  夠可分，`d_class.min()` 對真詞來說幾乎必然落在自己類別，其他類別的距離不會
  進來搶答案）。
- `compute_reject_distances`：每一筆 `_reject` 樣板對**全部類別**取 `class_distances(...).min()`，
  這就是部署時「一個靜止輸入」會發生的確切運算，天然包含了 n_classes 這個因子。

兩邊分布的樣本結構跟部署時一致，門檻直接從兩者的實際重疊算出來（ROC），
不需要假設某一邊的統計性質可以套用到另一邊。

**這不是空口推導**——`D22` 的完成報告已經對 `n=10..100`（8 類、104 維、T=24）
掃過一輪：新方法的誤拒率全程壓在 0%~1.1%，沒有隨 `n` 上升的趨勢；舊方法全程
30%~37%，也沒有隨 `n` 下降的趨勢（這正是「地板問題」的訊號：如果只是樣板不夠，
增加 `n` 應該要改善，但沒有）。**這次稽核額外確認**：新方法的兩個分布結構
（見上）從設計上就避開了「用一種樣本結構的統計量去校準另一種樣本結構」的
錯誤，不是巧合。

## 🔴 發現並修正：`recognition_service.py`（`D09`）還在用舊方法

**這是這次稽核唯一需要動手改的地方。** `D09` 完成時 `D22` 還不存在，
`RecognitionService._recalibrate_thresholds()` 原本硬呼叫
`enrollment.calibrate_tri_reject_thresholds`（單邊 LOO）——調度員後來把
`D22` 的雙邊 ROC 定為系統預設、寫進 CONTRACTS §4.3，但**實際部署會呼叫到的
即時辨識服務沒有跟著換**，等於系統其餘部分（文件、決議）已經前進，
真正跑起來的程式碼還停在舊方法上。

**已修正**：`RecognitionService` 新增 `reject_calibration_method` 參數，
預設 `"roc"`（呼叫 `reject_calibration_roc.calibrate_tri_threshold_roc`），
`"loo_single"` 保留可選（`D22` 的原則：舊方法留著當對照，不刪除）。
新增 4 個測試釘住：預設值確實是 `"roc"`、`"loo_single"` 仍可選、未知方法
報錯、在真實規模下 `roc` 的誤拒率明顯低於 `loo_single`（實測案例：同一組
合成資料，`loo_single` 100% 誤拒、`roc` 0% 誤拒）。

## 其他項目：D10 的 crosstalk / ambient 門檻（`max` 跨 16 zone）

`crosstalk_verdict()` 用 `max(zone_delta_mm) < 2mm` 判 PASS/FAIL；
`format_report()` 用 `max(|ambient_rate|) > 10%` 判斷「邊際訊號」。
兩者都是「跨 16 個 zone 取極值」，理論上有同樣風險，**但用合成資料模擬純雜訊
（無真實 crosstalk）之後確認安全**：

| 統計量 | 樣本數（每 zone 的時間幀數 T） | 純雜訊下的期望值（模擬 2000 次） | 門檻 | 安全邊際 |
|---|---|---|---|---|
| `max(zone_delta_mm)` | T=200（典型錄製長度） | 均值 0.10mm，p99 0.17mm | 2mm | ~12–20 倍 |
| `max(zone_delta_mm)` | T=10（極短錄製） | 均值 0.46mm，p99 0.75mm | 2mm | ~2.7–4 倍 |
| `max(\|ambient_rate\|)` | T=10~60 | p99 0.016~0.039 | 0.10 (10%) | ~2.5–6 倍 |

安全的原因：這兩個統計量都是「先對很多時間幀取平均（`mean`），再跨 16 個
zone 取 `max`」——平均值的標準誤差隨 `1/√T`縮小，就算 T 很小（10 幀）安全
邊際依然足夠。**這個結論的前提是實際 `E02` 錄製的每個 zone 至少有這個數量級
的幀數**——如果真實錄製比 T=10 短很多（例如個位數幀），需要重新驗證。
沒有修改任何程式碼，這兩處判定維持原樣。

## D11（`exp_a_snr.py`）：確認不受影響

`three_way_verdict()` 用的是 `overall_snr = snr_zone.mean()`（平均值），不是
`max`。逐一檢查過 `exp_a_snr.py` 的所有比較/判斷邏輯，沒有其他 `max`/`min`
判準。維持原樣，不需要修改。

## D12（`exp_d12_wear_cv.py`）：確認不受影響

`grep` 完全沒有 `max`/`min`/`argmax`/`argmin`/`percentile` 的判斷用法——CV
公式全部用 `mean`/`std`，這一類統計量的性質跟極值不同（不會隨樣本數系統性
偏移向某個方向），不在這次稽核的範圍內。

## 不屬於我的檔案：只回報，未編輯

| 檔案 | 發現 | 狀態 |
|---|---|---|
| `d14_viseme_sensitivity.py` | `mean max\|z\|` 的機率地板問題，已由 `esp-mask-test-7c` 自己發現並在追蹤 | 不重複回報，只在此記錄我確認過 |
| `d21_signal_ablation.py` | 唯一的 `min` 用法是整數樣本計數，不是連續統計量的極值判準 | 確認安全，無需轉派 |
| `exp_c_silhouette.py`、`d16_mutual_information.py`、`d17_tsne_visualization.py`、`d18_permutation_test.py`、`d19_ablation_suite.py` | 沒有極值型判準（`d18` 用標準排列檢定 p 值，方法本身正確） | 確認安全，無需轉派 |

## 修改的檔案

- `analysis/similarity/recognition_service.py`：新增 `reject_calibration_method`
  參數（預設 `"roc"`，可選 `"loo_single"`）、`roc_strategy`/`roc_target` 參數；
  `_recalibrate_thresholds()` 依方法分派；`list_templates()` 回報使用的方法
- `analysis/similarity/test_recognition_service.py`：新增 4 個測試

其餘檔案（`exp_a_snr.py`、`exp_d05_dtw_vs_cosine.py`、`exp_d09_template_count_study.py`、
`exp_d10_crosstalk.py`、`exp_d12_wear_cv.py`）**逐一檢查過，判斷安全，沒有修改**。
