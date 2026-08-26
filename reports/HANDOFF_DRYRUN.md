# `HANDOFF.md` 步驟實測——真的照著跑一遍

> 起因：`HANDOFF.md` 第 3 節（「你要做什麼才能拿到真實結果」）從沒有人
> 真的照著跑過。這份報告假裝自己是拿到交接文件的人，把每一條能跑的指令
> 真的貼進 shell 執行，需要硬體的步驟改成「確認語法/路徑對」，不是照抄
> 文件就打勾。

## 結論摘要

| 項目 | 結果 |
|---|---|
| `python -m analysis.run_all --session ... --real` 真的能跑完、產出 `summary.md` 且通過矩陣在最上面 | ✅ 實測通過（見下） |
| **在一個乾淨環境（只裝 `requirements.txt`）能不能跑**（調度員特別想知道的） | ✅ **能**——`--collect-only` 1105 個測試零 import 錯誤；`analysis/`+`ssi-backlog/tools/` 450 個、`host/` 536 個全部在乾淨環境裡真的執行過，全綠；`analysis.run_all` 的實際輸出在乾淨環境與 `.venv` 逐字相同 |
| `tools/fw_regression.py`／韌體相關檔案路徑、常數、CLI 參數 | ✅ 全部核對存在且與文件描述一致 |
| `config/session_targets.json`／`config/quality_thresholds.json` | ✅ 欄位與文件描述（4 個 `null`／3 個 `[暫定]`）逐一對得上 |
| 需要硬體的步驟 | ⏭️ 跳過，但語法/檔案存在性都核對過 |
| 發現的問題 | 4 個，全部是**文件描述落後於已修好的程式碼**，不是程式本身壞掉；已修 3 個（不在禁改清單），1 個回報 |

---

## 1. `HANDOFF.md` 第 2 節「步驟」逐條核對

### 步驟 1-4：讀清單、做未驗證假設確認、開始蒐錄——需要硬體，跳過

引用的檔案路徑核對過都存在：`E01_bringup_checklist.md`、
`config/session_targets.json`（4 個欄位確實全部是 `null`：
`target_distance_mm`/`target_angle_deg`/`tolerance_distance_mm`/
`tolerance_angle_deg`）、`config/quality_thresholds.json`（`noise_floor`
green=300/yellow=1000、`valid_zones` green=0.8/yellow=0.5、`symmetry`
green=0.15/yellow=0.3，三個都標 `[暫定]`，跟文件描述完全一致）。

### 步驟 5：跑驗證報告——**真的執行過，通過**

```bash
python -m analysis.run_all --session <檔案.h5> --real
```

`--help` 核對：`--session`、`--out`、`--real`、`--ablation-permutations`、
`--fast`、`--time-budget` 全部存在，用法跟 `HANDOFF.md`/`E01` 寫的一致。

**用專案自己的合成 session 產生器**（`analysis/reporting/test_run_all
.write_session()`，兩個不同 `wear_id`）造出兩個真的 `.h5` 檔案，實際跑：

```bash
python -m analysis.run_all --session wear1.h5 --session wear2.h5 \
    --real --ablation-permutations 1000 --out <目錄>
```

**結果**：9 個檔案、21.0 秒寫完，`summary.md` 開頭確實是通過矩陣：

```
🔴 必通過項目失敗：C Silhouette。
## 通過矩陣
| C0 串擾 🔒 | ... | — SKIPPED |
| A 逐 zone SNR 🔒 | 7.82 / 6.87 | ✓ PASS |
| B 跨次戴 CV | ... | ✓ PASS |
| C Silhouette 🔒 | 0.106 | ✗ FAIL |
| E Viseme 敏感度 | ... | ✓ PASS |
```

`HANDOFF.md` 描述的每一個細節都對得上：通過矩陣在最上面、失敗項目有紅色
警示、SKIPPED 有原因、後面還有「跨實驗一致性」的診斷建議段落。**這條
指令是真的能用的，不是只存在於文件裡。**（C Silhouette 這裡會 FAIL 是
因為用的是最小合成 fixture，不是為了可分性特別構造的資料——`D13_silhouette
_notes.md` 自己就解釋過這個現象，不是這次發現的新 bug。）

### 步驟 6：`summary.md` 最上面的通過矩陣——確認屬實

見上。

---

## 2. 🔴 調度員特別想知道的：requirements.txt 在乾淨環境夠不夠

### 方法

在 `.venv` 之外**另建一個全新、只裝 `requirements.txt` 的 venv**
（不是重用任何專案已裝好的環境），逐步驗證：

1. `pip install -r requirements.txt` 本身跑完沒有報錯
2. `pytest --collect-only` 對全部四個測試目錄（`host/`、`analysis/`、
   `ssi-backlog/tools/`、`vl53l7cx_test/monitor/`）——**只做靜態收集，
   不執行**（這台機器目前有其他 agent 在跑東西，`TEST_SUITE.md` 已經
   記錄過同時多個 pytest 行程會讓時間敏感測試假性失敗，所以完整跑
   `vl53l7cx_test/monitor/` 這輪跳過，只做零風險的收集）
3. `analysis/` + `ssi-backlog/tools/`（較快，較少時間敏感測試）與
   `host/` 兩個目錄**真的執行**（不只收集）

### 結果

| 步驟 | 結果 |
|---|---|
| `pip install -r requirements.txt` | 乾淨完成，無錯誤 |
| `pytest --collect-only`（四目錄全部） | **1105 個測試，零 import 錯誤** |
| `analysis/` + `ssi-backlog/tools/` 真的執行 | **450 passed** |
| `host/` 真的執行 | **536 passed**（151.71s——比 `TEST_SUITE.md` 記錄的基準慢一些，這台機器目前有其他 agent 在跑，符合已知的系統忙碌現象，不是失敗） |
| `python -m analysis.run_all --session ... --real` 在乾淨環境跑 | 與 `.venv` 逐字相同的輸出（9 個檔案、通過矩陣數字完全一致），只是耗時較短（8.7s vs 21.0s，浮動屬正常） |

**結論：`requirements.txt` 目前是足夠的。** 沒有發現任何缺漏的套件
（`cycler` 沒有列在裡面，但它是 `matplotlib` 的必要相依套件，
`pip install matplotlib` 會自動帶進來，不算漏）。**使用者三個月後
重建環境，照 `requirements.txt` 裝、照 `HANDOFF.md` 的指令跑，
目前驗證是能動的。**

（`vl53l7cx_test/monitor/` 目錄本身沒有在乾淨環境裡真的執行——只做過
零風險的收集確認零 import 錯誤，執行留給之後系統不忙的時候再補一次；
這個目錄涉及真的起 pty 子行程＋SSE 時序，不是這次要驗證的
`requirements.txt` 夠不夠這件事的核心，不執行不影響上面的結論。）

---

## 3. 韌體與工具檔案路徑核對（跳過執行，只核對存在性/語法）

- `vl53l7cx_test/main/fft_probe.h`／`.c`：存在，`fft_probe_run()` 已定義，
  `FFT_PROBE_EXPECT_BIN=32` 與 `A10_spike.md` 算的「1000Hz×512/16000Hz=32」
  一致。
- `vl53l7cx_test/main/bone_mic.c`：`MIC_HOP_SAMPLES=256`，與 `A15_perf.md`
  「A14 已把 hop 從 512 改成 256」一致。
- `vl53l7cx_test/main/vl53l7cx_test.c`：`TOF_RESOLUTION_MODE` 巨集存在，
  預設 `4`，與文件描述一致。
- `host/clock/align.py`：`ClockAligner.to_host_us()` 存在，與 `E01` §5
  `B04` 拍手測試的指示一致。
- `tools/fw_regression.py --help`：`--port`/`--baud`/`--duration`/
  `--config`/`--out` 全部存在，用法範例與 `A15_perf.md` 逐字一致。

`idf.py`/`esp-idf` 本身無法在這台機器驗證（沒有 ESP-IDF 環境），語法
（`idf.py -p /dev/ttyUSB0 build flash monitor`）是標準 ESP-IDF 用法。

---

## 4. 發現的問題：全部是「文件描述落後於已修好的程式碼」

這次逐一核對時，**實際重讀程式碼並寫一行合成資料實測**（不是只讀文件），
發現 `host/capture/protocol.py` 的 `_parse_heartbeat()` 早就修好了
`A15_perf.md`／`tools/fw_regression.py` 自己記錄的「已知限制」（$H 新欄位
被判成畸形行），但兩份文件都還在講這個已經不成立的限制。

驗證方式：`parse_line("$H,123456,0,0,0,50000,42,7890")` 直接呼叫實測，
`bw_bytes_since_last` 正確回傳 `7890`，`heap`/`temp_c` 也正確解析，
不是 `N/A`。

**這 4 處都已修正**（都不在禁改清單內）：

1. `reports/A15_perf.md`：「已知限制」段落改成「已解決」，附上實測方法。
2. `tools/fw_regression.py`：模組文件字串、`heap_str` 訊息、
   `malformed_h_lines` 警告訊息、`heap Δ` stderr 訊息，四處都拿掉
   對舊限制的引用（`malformed_h_lines` 那個分支本身是防禦性程式碼，
   邏輯不用改，只是訊息文字不該再暗示這是已知的、預期會發生的情況）。
3. `tools/OWNER.md`：原本只記錄 `compare_mel.py` 的 B 軌例外，沒有把
   `fw_regression.py` 的 A15 例外（同一輪已經被授權）寫進去——兩個檔案
   的頭部註解都提到彼此，但 `OWNER.md` 這份「誰能碰什麼」的權威清單
   漏了一半。已補上。
4. `reports/PANEL_INTEGRATION.md`：引用「`README.md`『架構關鍵：資料層
   與模式層分離』」，但這個專案**沒有根目錄 `README.md`**——那句話實際
   在 `ssi-backlog/README.md`。已修正引用路徑（實際搜過整個 repo 確認
   `ssi-backlog/README.md:55` 才是正確出處）。

沒有發現任何**程式邏輯本身**是壞的——四項都是「代碼已經對了，描述代碼
現狀的文字沒跟上」，跟這次稽核（`NUMBERS_AUDIT.md`）抓到的模式是同一類。

---

## 修改的檔案

- `reports/A15_perf.md`
- `tools/fw_regression.py`
- `tools/OWNER.md`
- `reports/PANEL_INTEGRATION.md`

沒有修改 `HANDOFF.md`（這次沒有在它自己的文字裡找到新問題——第 2 節
「步驟」逐條核對後是準確的；§4.5 技術債表格的既有問題已經在上一輪
`NUMBERS_AUDIT.md` 回報過，這裡不重複）。

## 需要人工驗證的項目

- `vl53l7cx_test/monitor/` 測試目錄沒有在乾淨環境裡真的執行過（只做過
  零風險的 collect-only），建議找一個這台機器比較不忙的時段補跑一次。
- `idf.py`/ESP-IDF 相關指令的語法只做過靜態核對，沒有實際環境可以真的
  跑一次 `idf.py build`。
- 所有需要硬體的步驟（燒錄、戴上、錄音、PING burst 等）維持原樣待
  `E01` 上機驗證，這次沒有也不可能執行它們。
