# 跨報告數字一致性稽核

> 起因：調度員發現自己轉述過一個過時的數字（「錄製次數不對齊，誤拒率
> 從 7% 升到 15%」）。這份稽核逐一核對 `reports/` 底下十四份報告裡
> 重複出現的量，確認哪些一致、哪些不一致，以及不一致時是「兩種方法都對」
> 還是「其中一份過時了」——兩者處理方式不同，見下方各項。
>
> **第二輪（同一天，後續）**：同樣的問題又發生第三次，這次是使用者要
> 拿去口試的 `DEFENSE_QA.md`。範圍延伸到當天新產生、涉及九份報告的
> 一批數字，見文末「第二輪稽核」一節。

## 方法

1. 讀完 `reports/` 底下全部 14 份 `.md`（`A04_polling`、`A10_spike`、
   `A15_perf`、`B20_bridge_throughput`、`C_monitor_perf`、
   `D09_template_count_study`、`D13_silhouette_notes`、
   `D21_signal_ablation`、`D22_reject_calibration`、`D_extremum_audit`、
   `E01_bringup_checklist`、`E2E_PIPELINE`、`HANDOFF`、`PANEL_INTEGRATION`、
   `TEST_SUITE`），逐一列出每份報告裡的量化聲稱（百分比、耗時、樣板數、
   門檻值）。
2. 對每一類量，找出在兩份以上報告出現的，逐一比對數字，判斷「一致」／
   「方法不同但都對」／「其中一份過時」三種情況之一。
3. 對判定為「過時」或「文件手誤」的項目，實際去源頭（程式碼、原始量測
   報告）確認後才修改，不是只憑感覺調整數字。
4. 對三份不能碰的檔案（`HANDOFF.md`、`C_monitor_perf.md` §9、
   `E2E_PIPELINE.md`）裡發現的問題，只回報、不編輯。
5. 部分項目實際重新執行驗證（不是只讀文件）：`pytest --collect-only`
   重新拿測試總數；讀 `tools/fw_regression.py`/`ssi-backlog/CONTRACTS.md`
   源碼確認頻寬除數的正確值。

---

## 逐項比對

### ✅ 一致：ToF 幀率／幀間隔門檻

`A04_polling.md`、`A15_perf.md`、`E01_bringup_checklist.md` 三份都寫
「4×4 兩顆皆 ≥ 29 Hz、8×8 ≥ 9.8 Hz、幀間隔標準差 < 3 ms」，逐字一致。
（目前全部待上機量測，沒有實測數字可比，比對的是門檻本身。）

### ✅ 一致：理論頻寬使用率

- `A15_perf.md`（理論值，`CONTRACTS.md` §1.4 算出）：4×4@30Hz+Mel ≈ 54%，
  8×8@10Hz+Mel ≈ 70%。
- `D21_signal_ablation.md`（合成資料實測，含 signal 通道）：
  4×4@30Hz 53.9%，8×8@10Hz 69.8%。

四捨五入後完全對得上（53.9%≈54%、69.8%≈70%），是同一個計算在不同精度
下的呈現，不是矛盾。

### 🔧 已修正：頻寬使用率的除數，`A15_perf.md` 文件手誤

`A15_perf.md` 原本寫「bytes/秒 ÷ 46000」，但：
- `tools/fw_regression.py` 的實際程式碼：`LINK_BYTES_PER_S = 460800 / 10.0
  = 46080.0`（8N1，每 byte 佔 10 bit）。
- `ssi-backlog/CONTRACTS.md` §1.4：「460800 baud ≈ 46 KB/s」。
- `B20_bridge_throughput.md` 的量測方法：明講用 `460800 ÷ 10 bits/byte`。

三個獨立來源都是精確的 46080，只有 `A15_perf.md` 的文字描述寫成約整的
46000。**程式碼本身沒有錯，只有這份文件的文字手誤**——已改成 46080
並註明來源，避免有人照著文件手算複驗時對不起來。

### ✅ 已驗證：`B19` 的 0.7% 掉幀率已被 `B20` 推翻，且已正確更正

`B20_bridge_throughput.md` 證明 `B19` 回報的「本機 pty、18% 頻寬掉 0.7%」
是主機端兩個計數 bug 造成的，真實掉幀率是 0.0000%。**`E01_bringup_checklist.md`
§1.2 已經正確引用這個更正**，不需要再改。這是「發現→更正→寫進檔案」
的正面案例，記錄下來對照下面幾項「沒進檔案」的反例。

### ✅ 一致：`monitor` 模式 CPU

`PANEL_INTEGRATION.md`（30.65%，C03 預算 15%）、`C_monitor_perf.md`
（延伸出 `TaskDuration`/`LayoutDuration` 兩個新指標，明確說「沒有改
`PANEL_INTEGRATION.md` 的原始數字」）、`E01_bringup_checklist.md`、
`HANDOFF.md` 四處引用的 30.65%、55.9%→21.7%、28.8%→7.6% 全部逐字一致。
`C_monitor_perf.md` 自己就把「總 CPU」與「主執行緒忙碌比例」這兩個
容易混講的指標分開列表說明——這是已經做對的示範，不需要再處理。

### ⚠️ 方法不同，都對：D09 vs D22 的舊方法誤拒率

`D09_template_count_study.md`（自己的 100-trial 掃描）與
`D22_reject_calibration.md`（自己的「舊方法」欄，60-trial × 3 組獨立
幾何取平均）在同樣的 n 值下數字略有差異（例如 n=10：ToF 32.3% vs
31.7%；n=100：35.3% vs 37.2%）。

查證：兩份報告背後是**兩支獨立的合成資料掃描**（`exp_d09_template_count_study
.sweep_template_counts(n_trials=100)` vs `exp_d22_reject_calibration
.sweep_compare_methods(n_trials=60, geometry_seeds=...)`），trial 數與
幾何取樣次數都不同，數字上下差 1-2 個百分點是預期的蒙地卡羅抽樣雜訊，
不是其中一份算錯。**兩者的質性結論完全一致**（舊方法在 n=10~100 之間
誤拒率持平在 ToF 30~37%／Mel 42~53%，不隨 n 下降），這才是重點，不是
到小數點後一位的數字本身。**沒有修改**——照調度員的原則，方法不同不
應該硬併成一個數字。

### 🔧 已修正：`E01_bringup_checklist.md` 自相矛盾（樣板數比例建議）

`E01_bringup_checklist.md` §6 有兩段離得很近、卻互相矛盾的文字：

- 「✅ 已解決：`D22` 的雙邊 ROC 校準」一節（第 180-194 行）自己的表格
  顯示：word:reject 比例從 1:0.3 掃到 1:3，**新方法全程 < 1.1%**。
- 緊接著四行之後，「錄製次數的暫定結論」一節卻仍然寫「詞與靜止的次數
  **仍然要對齊**——`D06` 實測不對齊時誤拒率從 7.33% 升到 14.67%，
  那個效應是真的」——把一個用**已被取代的舊校準方法**量到的效應，
  當成現在（`D22` 已是系統預設）仍然成立的建議在講。

這正是這次稽核的起因（調度員自己轉述過這組數字）。查證後確認：
`HANDOFF.md` §3.2/§4.4 已經正確記錄這件事的來龍去脈（那組數字只在
對話裡量過、從未進檔案、且已被 `D22` 推翻）。**已修正
`E01_bringup_checklist.md` 的那條建議**，改成跟 `HANDOFF.md` 的框架
一致：說明數字是真的量過的、用的是舊方法、且已被同一節裡的 `D22` 表格
推翻，不再建議刻意對齊次數。

### ✅ 一致：σ 下限常數（`1e-3` → `1/√12`）

`D_extremum_audit.md` 記錄的變更（`analysis/features/tof_features.py`
與 `analysis/experiments/exp_a_snr.py` 兩處的 `SIGMA_FLOOR`）與程式碼
現狀一致（這次稽核期間重新讀了兩份原始碼確認，仍是 `1.0 / 12 ** 0.5`）。
沒有其他報告引用這個常數的具體數值，無需比對。

### ✅ 一致：crosstalk 門檻（2 mm 距離差／10% ambient 變化率）

`D_extremum_audit.md` 的安全邊際分析與 `analysis/experiments
/exp_d10_crosstalk.py` 的 `DISTANCE_PASS_THRESHOLD_MM = 2.0` 一致。

---

## 已被取代的方法與數字對照表

| 舊數字/方法 | 新數字/方法 | 出處 | 目前狀態 |
|---|---|---|---|
| 拒識門檻校準：單邊 LOO（`D06`/`D08`），真實規模下誤拒率 30~37%（ToF）/ 42~53%（Mel），不隨樣板數改善 | 雙邊 ROC（`D22`），同規模下 0~3.3%，對樣板數比例不敏感 | `D09_template_count_study.md`、`D22_reject_calibration.md` | `roc` 已是 `RecognitionService` 系統預設，`loo_single` 保留為對照（見 `D_extremum_audit.md`） |
| 「詞與靜止次數要對齊，否則誤拒率 7.33%→14.67%」（`D06`，只存在於對話裡） | `D22` 的樣板不平衡掃描：1:0.3~1:3 新方法全程 < 1.1% | `HANDOFF.md` §3.2/§4.4（已正確）；`E01_bringup_checklist.md`（這次稽核修正） | 已於本次稽核修正 `E01_bringup_checklist.md`；`HANDOFF.md` 本身已正確，不需再改 |
| σ 下限 `1e-3` | `1/√12 ≈ 0.28868`（量化雜訊的理論下限） | `D_extremum_audit.md` | 已在 `tof_features.py`/`exp_a_snr.py` 生效，一致 |
| `B19`：本機 pty、18% 頻寬掉幀 0.7% | `B20`：同條件下掉幀率 0.0000%，0.7% 是兩個主機端計數 bug | `B20_bridge_throughput.md`、`E01_bringup_checklist.md`（已正確引用） | 已正確更正，無需再處理 |
| `A15_perf.md` 文字：頻寬除數「46000」 | 實際除數 46080（460800 baud ÷ 10 bits/byte） | `tools/fw_regression.py`、`CONTRACTS.md` §1.4、`B20_bridge_throughput.md` | 已於本次稽核修正 `A15_perf.md` 的文字（程式碼本來就是對的） |

---

## 我改了哪些

- `reports/A15_perf.md`：頻寬使用率的除數說明從「46000」改成「46080」，
  並加上與 `fw_regression.py`/`B20`/`CONTRACTS.md` 一致的來源說明。
- `reports/E01_bringup_checklist.md`：§6「錄製次數的暫定結論」裡「詞與
  靜止次數仍要對齊」那條建議，改為說明該效應來自已被 `D22` 取代的舊
  校準方法，現行方法下不必刻意對齊，並指向 `HANDOFF.md` §4.4 的完整
  來龍去脈。

沒有修改任何被判定為「方法不同但都對」的數字（`D09` vs `D22` 舊方法
掃描的小數點差異）——那是兩個獨立實驗的正常抽樣雜訊，不是錯誤。

**第二輪（今天新增的數字）：這輪只編輯這份檔案本身**，`STATISTICAL_
RIGOR.md`／`DEFENSE_QA.md`／`ALIGNMENT_MISMATCH.md` 等九份報告即使
查到問題也只寫進上面「第二輪稽核」那節回報，沒有動手改——邊界這次
明確只開放 `reports/NUMBERS_AUDIT.md`。

---

## 回報給調度員的項目（三份不能碰的檔案裡發現的問題）

### `HANDOFF.md` §4.5 技術債表格已經過時

該表格目前仍寫：

> 「五張圖還沒套用統一樣式」……「`exp_a_snr` 的熱力圖可能被灰階檢查
> 擋下……那正是該檢查要做的事，不是 bug」

但**這兩件事這次稽核前一項任務已經做完**：`exp_a_snr`/`exp_d10_crosstalk`
/`exp_d12_wear_cv`/`exp_d09_template_count_study` 四份檔案已套用
`SEQUENTIAL_CMAP`＋`save_figure()`（`exp_d05_dtw_vs_cosine.py` 沒有圖表，
不算在五張圖裡）。而且**實測確認 `exp_a_snr` 的熱力圖沒有被灰階檢查
擋下**——SNR 定義本身是非負量值（`|Δ|/σ`），`cividis` 語意正確，不需要
數值標註或 `diverging_opt_out`。這兩行需要更新，但 `HANDOFF.md` 不在
我這次的編輯範圍內，請轉知它的作者。

### `HANDOFF.md` 的「924 個測試」與 `TEST_SUITE.md` 的快照數字都已落後

`TEST_SUITE.md` 自己的基準快照是 843（完稿前已重新 collect 到 890，
文件裡明講「這是移動目標，不要當常數比對」）。`HANDOFF.md` 的「三十秒
版本」寫「測試全綠（924 個測試）」，沒有附上同樣的「移動目標」警語。

**這次稽核實際重新跑了一次 `pytest --collect-only`**（不是用舊數字），
目前真實總數是 **1063**——比兩份報告寫的都多，符合「D/C 軌還在開發，
數字會持續成長」的預期，不是矛盾，但代表 `HANDOFF.md` 的「924」現在
已經是一個過時的快照，讀者可能誤以為那是目前的精確值。建議 `HANDOFF.md`
的作者要嘛在定稿前重新 collect 一次，要嘛比照 `TEST_SUITE.md` 加一句
「這是快照，會持續成長」的警語。（**沒有重新跑過整套測試本身**——這台
機器目前有其他 agent 在跑東西，`TEST_SUITE.md` 自己就警告過同時多個
pytest 行程會讓時間敏感測試假性失敗，所以只做了不執行測試、純粹清點
數量的 `--collect-only`，不會被那個問題影響。）

### `HANDOFF.md` §3.1「n≥30 時誤拒率是 0」略有誇大

`D22_reject_calibration.md` 的新方法欄裡，n=40 是 ToF 1.1%／Mel 0.6%，
不是 0——`HANDOFF.md` 說「n≥30 時誤拒率是 0」在 n=30/50/75/100 都成立，
唯獨 n=40 不是精確的 0（雖然仍然很低）。這是小地方，不影響任何決策，
但嚴格來說不是「n≥30 全部是 0」，是「n≥30 大致落在 0~1.1%」。一併回報，
是否需要改由你判斷。

---

## 第二輪稽核：今天新增的數字（`0.917`/`0.625`、時長洩漏、模態尺度、
`D21` 可分性、`11` 個檢定、真板子 RMS）

> 這輪範圍是今天新產生、散在 `STATISTICAL_RIGOR.md`、`DEFENSE_QA.md`、
> `ALIGNMENT_MISMATCH.md`、`MODALITY_SCALE_AUDIT.md`、
> `DISTANCE_COMPARISON.md`、`D21_signal_ablation.md`、
> `THRESHOLD_DIRECTIONS.md`、`FIRST_REAL_DATA.md`、`E01_bringup_checklist.md`
> 九份報告裡的數字。**這輪只有 `reports/NUMBERS_AUDIT.md` 可以編輯**——
> 其他八份即使查到問題也只回報、不動手，跟這份稽核的上一輪處理
> `HANDOFF.md`/`C_monitor_perf.md`/`E2E_PIPELINE.md` 是同一個原則，
> 只是這輪禁改清單更長。

### ✅ 一致：`0.917`/`0.625`（分組驗證灌水）

`STATISTICAL_RIGOR.md`（"同一批資料，未分組準確率 0.917，按 wear_id
分組後 0.625"）與 `DEFENSE_QA.md` Q6（"灌水幅度是 29 個百分點
（0.917 → 0.625）"）逐字一致，同一組合成資料實測。

### ✅ 一致：`5.18x`/`1.28x`（ToF/Mel 模態尺度）與拒識率 `66.7%`/`33.3%`/`46.7%`

`MODALITY_SCALE_AUDIT.md` 的 `cvn=False`（現行預設）那一列——`5.18x`、
cosine 正確拒識率 `66.7%`、euclidean 正確拒識率 `33.3%`——跟
`DISTANCE_COMPARISON.md`「發現 2」表格的對應欄位逐字對得上（同一組
合成資料的同一次量測，`MODALITY_SCALE_AUDIT.md` 只是把它跟 `cvn=True`
的新實驗並排列出）。`cvn=True` 那一列（`1.28x`、euclidean 拒識率回升到
`46.7%`）是 `MODALITY_SCALE_AUDIT.md` 這次新做的實驗，`DISTANCE_
COMPARISON.md` 目前沒有這個組態不是矛盾，是後者還沒收錄這個新結果——
不需要回填，`MODALITY_SCALE_AUDIT.md` 自己已經清楚標示這是延伸實驗。
`DEFENSE_QA.md` 引用的 `5.18` 倍也跟這兩份一致。

### ✅ 一致：`11` 個檢定（`D18` 2 + `D19` 6 + `D21` 3）與 p 下限 `0.005`

`STATISTICAL_RIGOR.md` 第 1 節與 `DEFENSE_QA.md` Q6 對「11 個檢定」的
拆解逐字一致（`D18` 的 `all`/`tof_only` 2 個、`D19` 的
`all`/`mel`/`tof_combined`/`tof_l`/`tof_r`/`random_channel` 6 個、`D21`
3 個），跟 `analysis/experiments/d19_ablation_suite.py`（今天稍早親自
確認過）的 6 個 `_run()` 呼叫也對得上，不是憑印象數的。置換次數 200
時 p 下限 `1/201≈0.005`，兩份報告數字一致。

### ✅ 一致：真板子 RMS `4-6` 與門檻 `300`

`E01_bringup_checklist.md`、`FIRST_REAL_DATA.md`、`THRESHOLD_DIRECTIONS.md`
（我自己這輪寫的，今天稍早）三份對真板子實測 RMS 4-6、
`config/quality_thresholds.json` 的 `noise_floor.green=300` 描述一致。
**這是一個知識正確傳遞的正面案例**：`THRESHOLD_DIRECTIONS.md` 設計
「只抓恆為 0、不訂 >0 的下限」這個判準，直接引用了 `E01`/
`FIRST_REAL_DATA` 記錄的這個真實案例，沒有自己重新假設一個門檻。

### 🔴 已過時：`DEFENSE_QA.md` 附錄問答表「時長洩漏還沒修」

附錄表格（文末，上台前快速掃一眼用的那張）第 223 行寫：

> 「樣板品質怎麼確保？ | ⚠️ 對齊邏輯已修好，時長洩漏還沒修，靠人工控制」

但 `ALIGNMENT_MISMATCH.md` 開頭明白寫著「✅ 已修（調度員核准後）：VAD
裁切」，並附上實測：同一詞不同長度的最大距離從 0.9985 降到 0.3069，
「長度效應／詞義效應」比例從 99.7%（幾乎打平）降到 30.7%，`464` 個
測試通過。**這不是「完全沒做」，是「做了、量過、有真的改善，但沒有
完全消除」**——`ALIGNMENT_MISMATCH.md` 自己也誠實寫「30.7% 仍然不是
0，現階段仍建議人工控制時長」，所以「靠人工控制」這個**建議本身**
沒有錯，錯的是「時長洩漏還沒修」這句話本身，讓人以為連
`speech_window` 這個裁切都還沒做。

比較細看的話，Q10 完整版的回答（附錄表格上方那一節，第 189-207 行）
其實已經比附錄表格精確一些——它有提到「`live_pipeline.py` 已經加了
一個可選的 `speech_window` VAD 裁切參數作為長期解法，但還沒有用真實
資料驗證過」，只是**沒有引用 `ALIGNMENT_MISMATCH.md` 現在已經有的
量化結果**（99.7%→30.7% 那組數字）——如果委員追問「修了多少」，
照現在 Q10 的答案會答不出來，但證據其實已經在報告裡。

**建議**（只回報，沒有動手）：附錄表格那一格改成類似「⚠️ 對齊邏輯已修好，
時長洩漏用 VAD 裁切改善了 69 個百分點（99.7%→30.7%），但沒有完全消除，
仍靠人工控制輔助」；Q10 的完整答案加一句引用 `ALIGNMENT_MISMATCH.md`
的量化結果。

### 🔴 已過時（部分）：`STATISTICAL_RIGOR.md` 第 157 行「`run_all` 還沒有把
`wear_id` 傳下去」

原文（第 3 節「已修」的說明框裡）：

> 「⚠️ `run_all` 還沒有把 `wear_id` 傳下去（那個檔案由別人在改）。
> 介面已經備好，接上去只要多傳一個參數。」

實際去讀 `analysis/run_all.py` 源碼確認：**`D18` 這條線已經接上了**
——`run_d18_permutation()`（第 668 行）被真的呼叫時傳了
`wear_ids_for_features`（第 843 行），不是只是「介面備好、還沒接」。
**但 `D19`（消融套件）確實還沒接**——`run_d19_ablation()` 呼叫
`mod.run_ablation_suite()` 時沒有傳 `groups`（今天稍早我讀源碼確認過
這件事，也是這輪順帶把 `d19_ablation_suite.py` 的六個檢定＋時間反轉
測試都補上 `groups=` 支援的原因——介面現在真的備好了，包含比原本
更完整的時間反轉測試分組）。

**這行陳述現在只對一半**：對 `D19` 還成立，對 `D18` 已經不成立。
`8f` 正在 `analysis/run_all.py`／`analysis/reporting/` 裡持續接
`effect_size`，這條線很可能很快也會接上 `D19`，屆時這句話要整段改成
「已修」——先回報現況，不猜測接下來會不會改。

### ⚠️ 沒找到出處：`D21` 可分性 `1.0–1.09`／`1.23–1.37`

逐一讀了 `D21_signal_ablation.md`（signal 通道消融，CV 準確率
0.625–0.850，跟這組數字對不上）、`DEFENSE_QA.md`、`STATISTICAL_RIGOR.md`
——三份都只提到「`D21` 3 個檢定」這個**數量**，沒有找到 `1.0–1.09`／
`1.23–1.37` 這組具體數值，逐字比對跟近似範圍比對都沒有命中。

`git status` 顯示 `analysis/similarity/closed_set_probe.py`、
`analysis/experiments/exp_d21_closed_set_probe.py`、
`analysis/similarity/test_closed_set_probe.py` 目前都是這輪新增／修改
的檔案（`7c [4bedc9]` 正在裡面），**這組數字很可能是還沒寫進任何報告
的即時結果**，或者程式碼變動後已經跟目前版本對不上——邊界規定
`analysis/similarity/` 這輪只能讀不能動，我沒有進一步深入那支還在變動
的程式碼去反推數字，怕讀到的是中間狀態。**如果這組數字要拿去對委員
講，需要先確認它現在寫在哪一份報告裡（可能還沒寫）**，不能假設它已經
有一個穩定的出處。
