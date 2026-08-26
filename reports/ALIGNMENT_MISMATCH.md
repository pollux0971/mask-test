# 訓練/分析路徑跟線上推論路徑，兩套跨模態對齊邏輯量出來的落差

> 由來：`esp-mask-test-59` 做 Demo 乾跑時發現同一筆 trial 的 `tof_A` 是
> 39 幀、`mel` 是 50 幀，讀程式碼確認全庫有兩套完全不同的對齊邏輯在處理
> 這個落差。這份報告量化這個落差有多大、會不會影響辨識結果，並回答
> 「哪一條是對的」。**只做量測，沒有修改 `analysis/run_all.py` 或
> `host/features/live_pipeline.py`**——兩者都在這輪的改動邊界之外。
>
> 目前沒有真實資料（見 `HANDOFF.md`），下面全部用合成資料量測；
> 方法與限制寫在最後一節，結論請對照限制一起看。

## 結論先講

1. **兩套邏輯真的不一樣，而且不是刻意的設計。** 讀 `D15.md`/`D03.md`
   兩份 story 規格、`build_feature_seqs()` 引入時的 commit 訊息，都沒有
   任何一處提到「離線分析刻意不用 `Aligner`」這個決定。反而
   `analysis/features/feature_assembly.py` 自己的模組文件明講輸入
   「必須已經由 B06（`Aligner`）對到同一組共用幀」——`build_feature_seqs()`
   完全沒有呼叫 `Aligner`，直接違反自己下游模組寫明的前提。**這是讀
   程式碼直接確認的事實，不是猜的。**
2. **只看合併後的 104 維向量會完全看不出問題**——合併向量的
   cosine 距離全程 < 0.0003，看起來兩條路徑幾乎一樣。**但這是假象**：
   ToF 通道（64/104 維）本來就不受這個 bug 影響（見下），在合併向量裡
   權重夠大，把 Mel 通道的災難稀釋到看不見。
3. **拆開來看，Mel 這一軌是真的壞了。** `analysis/similarity/fusion.py`
   的 `compute_tri_result()` 本來就是分開算 ToF/Mel 距離再融合——用同一套
   分軌方式量測，Mel-only 的 cosine 距離落在 **0.86–1.46**（cosine 距離
   值域 0–2，>1 代表方向已經接近相反），而 ToF-only 全程 < 0.0003。
4. **這個落差真的會讓分類結果翻轉**：同一筆合成錄音，樣板用線上路徑建、
   查詢分別用線上／離線兩條路徑處理，Mel-only 這一軌在兩個測試時長下
   **選錯詞**（見下方分類翻轉測試）。
5. **哪一條是對的**：`Aligner` 是線上推論唯一可能的做法（資料是串流進來
   的，沒有「先湊齊全部再算」這個選項），`build_feature_seqs()` 應該向它
   對齊，不是反過來。**建議的最小改法見最後一節，這輪沒有動手，需要核准。**

---

## 1. 兩套邏輯分別在幹嘛

| | 離線（`analysis/run_all.py::build_feature_seqs()`） | 線上（`host/features/live_pipeline.py` + `host/align/aligner.py`） |
|---|---|---|
| 誰在用 | `D01`/`D06`/`D08`/`D09`/`D22` 等歷史分析報告、`analysis.run_all --session` | `7c [4bedc9]` 正在寫的建樣板腳本；唯一可能的即時推論路徑 |
| 怎麼對齊 | **不對齊**：各模態各自用原生取樣率算完 `tof_features()`/`mel_features()`，再 `n = min(len(tof_a_z), len(tof_b_z), len(mel_cmn), len(trial.tof_t_us))` **按索引截斷** | 先把原始樣本連同真實 `t_us` 餵進 `Aligner`，在固定輸出頻率（預設用 ToF 的原生 rate）上對每個模態做最近鄰／內插，兩個模態的每一幀對應同一個真實時間點 |
| 對齊依據 | 索引位置（第 i 個 ToF 幀配第 i 個 Mel 幀） | 真實時間戳 `t_us` |

ToF 30 Hz、Mel 62.5 Hz（`A14` 之後），比例約 1:2.08。因為 ToF 比較慢，
`n = min(...)` 幾乎每次都被 ToF 的幀數卡住——Mel 只留下**前面**
`n_tof / mel_rate` 秒那一段，後面的 Mel 資料整段被丟掉，而 ToF 卻涵蓋
整個錄音的完整長度。兩個模態因此代表**不同的真實時間窗**，而且窗口
的錯位隨錄音長度線性放大（`n_tof` 越大，Mel 只覆蓋的比例反而不變，
但絕對的秒數落差越大）。

## 2. 量到的落差（合成資料）

方法：合成一筆已知真實時間軸的 trial（ToF/Mel 各自原生取樣率、內容是
一個已知頻率的正弦波，模擬「嘴型隨時間變化」），分別餵給兩條路徑算出
的 (T=24, 104) 特徵向量，用 `analysis/similarity/cosine_baseline.py` 的
`cosine_dist`/`modality_cosine_dist`（跟 `compute_tri_result()` 用的
同一套函式）比較。

| 情境 | ToF 原生幀數 | Mel 原生幀數 | cosine(offline, online) 合併 | ToF-only | **Mel-only** |
|---|---|---|---|---|---|
| 4×4@30Hz / Mel@62.5Hz, 0.5s | 15 | 32 | 0.0001 | 0.0001 | **0.86** |
| 4×4@30Hz / Mel@62.5Hz, 1.3s | 39 | 82 | 0.0002 | 0.0001 | **1.41** |
| 4×4@30Hz / Mel@62.5Hz, 3.0s | 90 | 188 | 0.0002 | 0.0001 | **1.12** |
| 4×4@30Hz / Mel@62.5Hz, 1.3s + 20% ToF 掉幀（模擬 REC dump） | 31 | 82 | 0.0003 | 0.0002 | **1.46** |
| 8×8@10Hz / Mel@62.5Hz, 1.3s（CONTRACTS §1.4 頻寬表組態） | 13 | 82 | 0.0000 | 0.0000 | **1.05** |

**觀察**：
- ToF-only 全程幾乎是 0——合理，ToF 通道的索引排列本來就沒被 Mel 干擾，
  只要模態內部不動就沒事。
- Mel-only 全程 ≥ 0.86，遠超過「同一份錄音的兩種處理方式應該長得很像」
  的合理範圍。**掉幀（模擬 REC dump）讓它更糟，不是更好**；
  8×8@10Hz（ToF/Mel 比例落差更大，1:6.25）也一樣嚴重。
- 落差沒有隨情境單調變化到某個「安全區」——**在所有測過的情境下都是
  嚴重的**，不是只有極端情境才會發生。

## 3. 分類翻轉測試

方法：兩個可分辨的合成詞（`word_round` 頻率 1.2 Hz、`word_spread`
頻率 2.6 Hz），樣板一律用線上路徑建（對應 `7c` 正在寫的建樣板腳本），
查詢用**同一筆**錄音分別跑線上／離線兩條路徑，看 cosine 最近鄰
（`compute_tri_result()` 實際使用的判定方式）會不會選錯類別。

| 錄音長度 | 真實詞 | Mel-only／online 判定 | Mel-only／offline 判定 |
|---|---|---|---|
| 0.5 s | word_round | word_round（對，但 offline 距離已經 0.86 vs 1.12，逼近決策邊界） | word_round（對，僥倖） |
| **1.3 s** | word_round | word_round（對，距離 0.0013 vs 1.10，差距懸殊） | **word_spread（🔴 錯）** |
| **3.0 s** | word_round | word_round（對） | **word_spread（🔴 錯）** |
| 6.0 s | word_round | word_round（對） | word_round（對，僥倖，距離 1.02 vs 1.03 幾乎打平） |

**combined 與 ToF-only 兩軌在全部情境下都判斷正確**——這正是「只看
合併向量會覺得沒事」的地方。**Mel-only 這一軌在兩個時長下真的選錯詞**，
其餘兩個時長雖然剛好還是選對，但 offline 算出的距離已經跟正確/錯誤
類別非常接近，換一組合成參數或換一個真實的詞就可能翻過去——**不是
「這個 bug 不會影響結果」，是「這次剛好沒有跨過門檻」**。

## 4. 這對已經在跑的東西有什麼影響

- **如果部署系統的樣板建置與線上查詢都走線上路徑**（`7c` 正在寫的建樣板
  腳本用 `live_pipeline.py`，而真正的即時推論本來就只能走這條路），
  **系統內部可能是自洽的**——這份報告的分類翻轉測試刻意混用兩條路徑，
  不代表部署系統一定會踩到。
- 🔴 **但這不代表 D01–D22 的歷史結論可以直接套用到部署系統**：`D06`/
  `D08`/`D09`/`D22` 的準確率、Silhouette、拒識門檻（`theta_reject_mel`
  等）全部是拿 `build_feature_seqs()`（離線截斷）算出來的特徵訓練/驗證
  出來的。這條管線在 Mel 通道上跟部署系統實際會用的線上管線**量表
  完全不同**（上面測到的是同一份錄音、同一類詞之間的距離就相差
  100–1000 倍尺度）。也就是說：即使部署系統本身自洽，過去半年 D-track
  報告裡「拒識門檻應該設多少」「樣板數要多少才夠」這類數字，是拿一條
  **跟部署系統實際运行方式不同**的管線量出來的，`E05` 真實資料到手後
  用 `run_all.py` 重新分析出的結論，**不能假設可以直接套用在線上服務**
  （`RecognitionService`）身上——兩者用的不是同一套特徵萃取邏輯。
  這條建議轉交負責 `E05`/`E06` 分析規劃與 `D09` 服務串接的人一起看。
- 這份報告**沒有**去確認 `7c` 正在寫的建樣板腳本是否真的完全走線上路徑
  （那個檔案這輪按邊界規定沒有讀）——如果它為了方便重用了
  `run_all.py`/`build_feature_seqs()` 來處理錄好的 enrollment session
  檔，就會直接踩進第 3 節那個分類翻轉的情境，建議跟 `7c` 核對一下
  它的建樣板腳本實際呼叫的是哪一條路徑。

## 5. 建議的最小改法（這輪沒有動手，需要核准）

`analysis/run_all.py::build_feature_seqs()` 目前（約第 129–141 行）：

```python
tof_a_z = tof_features(trial.tof_a, trial.tof_valid_a, mu_a, sigma_a)
tof_b_z = tof_features(trial.tof_b, trial.tof_valid_b, mu_b, sigma_b)
mel_cmn = mel_features(trial.mel)
n = min(len(tof_a_z), len(tof_b_z), len(mel_cmn), len(trial.tof_t_us))
...
seq = assemble_feature_seq(tof_a_z[:n], tof_b_z[:n], mel_cmn[:n], trial.tof_t_us[:n])
```

改法方向：把 ToF/Mel 原始資料（含各自的 `tof_t_us`/`mel_t_us`）餵進
`Aligner`，呼叫 `.frames()`，再呼叫 `live_pipeline
.assemble_query_from_aligned_frames()`——**跟線上路徑呼叫同一份程式碼**，
而不是離線自己重寫一套。這樣兩條路徑**不可能再走岔**，因為它們本來
就是同一段程式碼。

**這個改法直到最近才可行**：`assemble_query_from_aligned_frames()`
需要每個樣本各自的真實 `t_us`，而 `mel_t_us` 是 `esp-mask-test-18`
剛剛才加進 `analysis/reporting/session_loader.py` 的 `Trial` dataclass
（見 `SCHEMA_SUPPLY_DEMAND.md`）——`build_feature_seqs()` 目前用的
`trial.mel`/`trial.tof_a` 都有對應的 `_t_us`，接口已經通了，換掉截斷
邏輯不再有「拿不到時間戳」這個技術障礙。

⚠️ **這條路徑跑著整套驗證報告**（`D01`–`D22` 全部經過
`build_feature_seqs()`），改動需要重新確認一次現有測試與已發布的門檻
數字會不會跟著變，這正是這輪先只回報、不動手的原因。

## 6. 方法與限制（誠實列出，結論請對照著看）

- **全部是合成資料**，目前沒有真實資料（見 `HANDOFF.md`）。
- 合成訊號是**單一頻率的正弦波**（乘上每個 zone/band 各自的固定權重），
  刻意做成「時間對不對得上」一目了然的形狀；真實語音/嘴型訊號更複雜，
  實際的距離扭曲程度可能更大也可能更小，這份報告量的是**機制本身
  真實存在、量級不可忽略**，不是「真實資料上誤差正好是多少」。
- 只測了 `cosine` 距離（系統目前預設，`analysis/similarity
  /recognition_service.py` 的 `DEFAULT_DIST_METHOD`）；沒有測 DTW
  （`dist_method="dtw"`）——DTW 允許時間扭曲比對，理論上**可能**天生
  對這種索引錯位更不敏感，但也可能反而放大它，這份報告沒有量，
  是一個誠實的缺口，不是隱藏的假設。
- 分類翻轉測試只用了 2 個合成詞、固定的頻率/相位參數；沒有掃過大量
  隨機種子做統計顯著性檢定——第 3 節「有些時長剛好沒翻轉」已經如實
  寫出來，不代表那些時長是安全的。
- 沒有驗證 `7c` 正在寫的建樣板腳本實際呼叫哪一條路徑（那個檔案這輪
  在改動邊界之外）。
