# 5.18 倍的 ToF/Mel 尺度失衡，汙染了哪裡？

**⚠️ 全部合成資料**（沿用 `reports/DISTANCE_COMPARISON.md` 的合成資料
產生器，見 `analysis/experiments/exp_distance_metric_comparison.py`）。
真實結論待 `E05`。這份報告只讀 `analysis/run_all.py`／`analysis/reporting/`
（`8f` 正在裡面做 VAD 裁切），**沒有修改任何一行**，發現的問題都回報在
這裡，不自己動手。

## TL;DR

`analysis/run_all.py` 五張卡（A/B/C0/C/E）+ 側邊指標（D16/D18/D19）裡，
**只有一個地方真的被汙染，而且問題不是「量出來的數字被扭曲」，是文件
本身宣稱了一件不成立的事**：實驗 E（viseme 敏感度熱力圖）。其餘大多數
被 `StandardScaler` 或 `cosine` 距離本身的有界性保護掉了。實驗 B
（wear CV 的距離比）有一個較輕微、但同一個機制（ToF 稀釋 Mel）已經在
`reports/ALIGNMENT_MISMATCH.md` 記錄過的風險。

## 逐項稽核

| 項目 | 有沒有融合 ToF+Mel | 用 raw 還是正規化過的距離 | 結論 |
|---|---|---|---|
| A（SNR） | 沒有——只比 ToF-A vs ToF-B | N/A | **不在範圍內**，不涉及 ToF/Mel 比較 |
| B（wear CV 距離比） | 有，`cosine_dist` 直接吃完整 104 維 `.data` | 沒有額外正規化，吃的是 D01/D02 產出的原始 z-score+CMN 向量 | **有風險**，見下 |
| C（Silhouette） | 有，`stack_modality()` 攤平後 | **先 `StandardScaler().fit_transform()`** 才進 PCA/silhouette_score | **受保護** |
| E（viseme 敏感度） | 有，`max\|z\|` 直接算在 `.data` 切片上 | **沒有任何正規化步驟**，直接用 D01/D02 的輸出 | 🔴 **被汙染，且文件宣稱錯誤** |
| D16（互資訊） | 有，`tof_vs_mel_ratio()` | 先 `StandardScaler().fit_transform()`；且 `mutual_info_classif` 本身是逐維估計，對跨模態尺度差不敏感 | **受保護（雙重）** |
| D18（置換檢定） | 有，`all_vs_mel_only`/`all_vs_tof_only` | `make_estimator() = StandardScaler + SVC`，**每一維獨立標準化** | **受保護** |
| D19（消融） | 底層呼叫 D18 的 `run_permutation_test` | 同上 | **受保護** |

### 為什麼多數項目沒事：StandardScaler 逐維正規化

`stack_modality()` 把 (T,104) 攤平成 T×104 個獨立欄位，`StandardScaler`
對**每一欄**各自減平均、除標準差——不分這一欄原本是 ToF 還是 Mel。
無論原始尺度差幾倍，過完 `StandardScaler` 每一欄都變成單位變異數，
下游的 PCA/silhouette/SVM 看到的是**已經抹平尺度差的資料**。C、D16、
D18、D19 全部走這條路，這是它們沒被汙染的共同原因，不是四個各自獨立
的巧合。

### 🔴 實驗 E：文件宣稱的「跨模態可比」不成立

`d14_viseme_sensitivity.py` 的模組文件寫：

> 特徵在 §3.2 就已經是 per-zone z-score（除以 `baseline_sigma`），
> 所以這個數字的單位是「偏離自己的靜止基線幾個標準差」，**跨模態可比**。

**這句話對 ToF 成立，對 Mel 不成立。** `mel_features(cvn=False)`
（D02，且 `run_all.py`／`build_templates_from_session.py`／`bridge_server.py`
的所有正式呼叫都用這個預設）**只做 CMN（減平均），沒有除以標準差**。
`max|z|` 這個敏感度指標直接吃 `.data` 裡 Mel 那一段，沒有任何額外正規化
——它算出來的不是「偏離幾個標準差」，是「偏離自己 trial 內平均值幾個
log10-mel 原始單位」，跟 ToF 那欄真正的 z-score 不是同一把尺，**不能
直接比大小**。

**這不是「可能有風險」，是文件寫錯了一句可驗證的事實**——`cvn=False`
是可以直接讀程式碼確認的。實務影響：熱力圖裡任何「Mel 比 ToF 強/弱」
的格子，數字本身的絕對值不能拿來下結論；目前唯一有驗收條件掛著的格子
（擦音 F：Mel 該明顯強過 ToF）方向性判斷可能還算穩（擦音的 Mel 訊號
量級通常遠大於這種尺度差），但**任何「差距多大」或「哪個模態贏得比較
多」的定量比較都站不住**。這一項建議 `esp-mask-test-8f`／story owner
決定要不要修（在 `d14_viseme_sensitivity.py` 內部對 Mel 額外做一次
`/ std` 而不動 D02 的預設，或修正文件的宣稱），**這份報告只負責指出
問題，不動 `analysis/experiments/d14_viseme_sensitivity.py`**（不確定
這算不算在 `8f` 的施工範圍內，保守起見不碰）。

### ⚠️ 實驗 B：跟 `ALIGNMENT_MISMATCH.md` 同一個機制，不同起因

`_wear_distance_ratio()` 直接用 `cosine_dist(a, b)` 比較完整 104 維
`.data` 向量的組內／組間距離比，沒有 `StandardScaler`。`cosine_dist`
本身的最終輸出雖然有界 `[0,2]`，但**組成它的內積跟範數計算，仍然是
量級大的子區塊主導方向**——這正是 `reports/ALIGNMENT_MISMATCH.md`
已經記錄過的現象（「只看合併後的 104 維向量幾乎量不出差異，ToF 通道
把 Mel 通道的落差稀釋掉了」），當時的起因是對齊方式不同，**現在確認
即使對齊方式已經修好，尺度失衡本身也會造成同一種稀釋**：如果 Mel
在戴法重複性上的表現跟 ToF 不一樣（例如 Mel 更穩定或更不穩定），
這張卡的單一比值目前主要反映 ToF 的戴法重複性，Mel 的貢獻被稀釋。
**這是既有機制的延伸確認，不是新發現的獨立 bug。**

## 量測：把 Mel 也做變異數正規化（`cvn=True`）會怎樣

`analysis/experiments/exp_cvn_comparison.py`（新腳本，沒有改任何預設值
——只是用 `assemble_query_from_aligned_frames(..., cvn=True)` 這個既有的
可選參數多跑一次，3 組獨立幾何取平均）：

| | ToF/Mel 歐式距離量級比 | cosine top1 | cosine 誤拒 | cosine 正確拒識 | euclidean top1 | euclidean 誤拒 | euclidean 正確拒識 |
|---|---|---|---|---|---|---|---|
| `cvn=False`（現行預設） | 5.18x | 76.7% | 38.3% | 66.7% | 76.7% | 38.3% | 33.3% |
| `cvn=True` | **1.28x** | 80.0% | 33.3% | 73.3% | 71.7% | 33.3% | **46.7%** |

- **量級比從 5.18x 降到 1.28x**（三組幾何都很一致，標準差 <0.01x）——
  `cvn=True` 確實大幅拉近兩個模態的尺度，符合預期（兩者都變成單位變異數
  正規化後的量）。
- **分類準確率沒有變差，cosine 甚至略升**（76.7%→80.0%）；**euclidean
  top1 略降**（76.7%→71.7%）——樣本數小（每條件僅 60 筆測試查詢），
  這個降幅不足以下「cvn=True 讓 euclidean 分類變差」的結論，只能說
  **沒有看到 cvn=True 讓分類明顯變好**。
- 🔴 **拒識沒有完全救回來**：euclidean 的正確拒識率從 33.3% 回升到
  46.7%，**還是明顯低於 cosine 的 73.3%**。`cvn=True` 縮小了尺度差，
  但沒有消除 `reject_fused` 對量級敏感這個結構性問題本身——`w=0.5`
  加權原始距離這件事，只要兩個模態的距離「有效範圍」不完全相等，
  就永遠會有一定程度的不對等，`cvn=True` 只是把不對等的倍數從 5.18
  壓到 1.28，不是壓到 1.0。
- **cosine 本身也小幅受益**（正確拒識 66.7%→73.3%）——這可能是 `cvn=True`
  順便修正了 Mel 內部各 band 之間的變異數不均（不是跨模態問題，是
  Mel 自己 40 個 band 彼此量級不同），跟這次稽核的「跨模態」主題是
  兩件相關但不同的事，值得記一筆但不誇大。

## 一句話講清楚「一直都在」是什麼意思，什麼不是

**失衡本身確實一直都在**（`cvn=False` 是現行預設，`D01`/`D02` 的正規化
程度本來就不對等）。但它**不是「一路汙染到所有報告」**——`StandardScaler`
保護了 C/D16/D18/D19，`cosine` 距離的有界性讓現行 `reject_fused`（預設
`dist_method="cosine"`）的兩個 theta 實際上很接近（`0.94` vs `0.89`，
比例僅 ~1.05x，遠不是 5.18x——這是用 cosine 距離算 raw 距離時的副作用：
cosine 的公式本身會對每一對向量各自除以各自的範數，即使輸入的絕對尺度
差很多，算出來的單一距離值仍然被壓進 `[0,2]`）。**真正被汙染、且目前
還沒人處理的只有實驗 E 的文件宣稱**，跟「如果之後把 `dist_method`
換成 euclidean，`reject_fused` 會出事」這件事（已經在
`reports/DISTANCE_COMPARISON.md` 裡建議先不要換）。

## 沒有做的事（按邊界）

- 沒有改 `analysis/features/` 任何預設值。
- 沒有碰 `analysis/run_all.py`／`analysis/reporting/`／`panel/**`。
- 沒有改 `d14_viseme_sensitivity.py`（發現問題但不是這次授權範圍去改）。
- 沒有 commit/push。
