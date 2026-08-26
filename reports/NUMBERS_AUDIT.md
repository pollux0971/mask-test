# 跨報告數字一致性稽核

> 起因：調度員發現自己轉述過一個過時的數字（「錄製次數不對齊，誤拒率
> 從 7% 升到 15%」）。這份稽核逐一核對 `reports/` 底下十四份報告裡
> 重複出現的量，確認哪些一致、哪些不一致，以及不一致時是「兩種方法都對」
> 還是「其中一份過時了」——兩者處理方式不同，見下方各項。

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
