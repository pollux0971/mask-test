# A15 — 韌體效能回歸測試

## 目的

A 軌動了 14 個 story，每個都可能動到時序。這份文件是「燒一次、跑一輪、把
所有懸而未決的數字一次收齊」的操作手冊，涵蓋：

- `A10` 的 GO/NO-GO 決策數字（FFT+Mel 單幀耗時）
- `A04` 的 ToF 幀率／幀間隔標準差
- `A05` 的掉幀率
- `A06` 的 heap 走勢
- `A15` 本身的 stack 餘裕、四種組態對照表、頻寬使用率

**這份報告本身不含任何實測數字**——全部標「待上機」，理由跟 `A04_polling.md`／
`A10_spike.md` 一樣：我沒有實體板子，寫假數字比沒有數字更危險。

## 前置：這份文件跟另外兩份報告的關係

上機那一次，建議照這個順序做，一次測完：

1. 先照 `reports/A10_spike.md`「怎麼跑這個探針」章節，跑一次 FFT 探針，
   拿到 GO/NO-GO 決策（如果 NO-GO，後面的 Mel 相關數字就不用測了，直接改走 B14）
2. 再照 `reports/A04_polling.md`「量測方法」章節，量 ToF 幀率與幀間隔標準差
3. 最後照本文件下面的步驟，跑四種組態的完整 5 分鐘回歸測試

三份報告的空白表格加起來才是完整的驗收證據。

## 量測方法與照抄步驟

### 0. 前置：確認目前韌體組態

```bash
grep -n "TOF_RESOLUTION_MODE\|MIC_HOP_SAMPLES" vl53l7cx_test/main/vl53l7cx_test.c vl53l7cx_test/main/bone_mic.c
```
記錄這次測試的 git sha（`git rev-parse --short HEAD`）——`$STATUS` 開機那行也會印，兩邊要對得起來。

### 1. 切換組態（每個組態都要重編＋重燒）

**解析度**（`vl53l7cx_test/main/vl53l7cx_test.c`）：
```c
#define TOF_RESOLUTION_MODE 4   /* 改成 4 或 8 */
```
**Mel 開關**不需要重燒——上機後用序列埠指令即時切：
```
MEL:0   # 關閉 Mel 串流（$F 不再輸出）
MEL:1   # 開啟
```
（`SENS`/`MEL` 指令格式見 `CONTRACTS.md` §1.2；`MEL` 由 A13 實作完成）

每改一次 `TOF_RESOLUTION_MODE` 就要：
```bash
idf.py build flash monitor
```

### 2. 拉高 Mel 計時的 log 等級（收集 FFT+Mel 耗時，不要重寫量測邏輯）

`A12` 已經在 `bone_mic.c` 用 `esp_timer_get_time()` 量好、印在
`ESP_LOGD(TAG, "mel frame fft+mel=%lld us", ...)`（tag 是 `"bone_mic"`），
預設 log 等級看不到。上機後在 `idf.py monitor` 的互動 shell 或開機前用
menuconfig 拉高這個 tag 的等級即可看到，例如執行期間下指令：
```
idf.py monitor
# 在 monitor 裡按 Ctrl+T 再按 Ctrl+L 打開 log level 選單（或改 sdkconfig 的
# CONFIG_LOG_DEFAULT_LEVEL 為 Debug 後重編）
```
或直接在程式碼裡開機時呼叫（**這行只在測試時人工暫時加，測完拿掉，不要留在
正式版**）：
```c
esp_log_level_set("bone_mic", ESP_LOG_DEBUG);
```

### 3. 收集 ToF stack 餘裕（本 story 新增，已經在程式裡了）

`vl53l7cx_test.c` 的主迴圈每 10 次心跳（約每 10 秒）會印一行：
```
I (xxx) vl53l7cx: a15_perf: tof_task stack headroom = XXXX bytes
```
`mic_task` 的 stack 餘裕不在我的檔案裡，需要另外請 `A12`/`A14` 的負責人
（`bone_mic.c`）補一行類似的 `uxTaskGetStackHighWaterMark()` log，
或人工用 `idf.py monitor` 搭配除錯工具查——**這是本報告唯一一個我這邊沒有
現成產出的欄位，見下方表格備註**。

### 4. 每個組態跑滿 5 分鐘，收集這些

- **ToF 幀率**：數 5 分鐘內收到的 `$T,A,...` / `$T,B,...` 行數 ÷ 300 秒
- **幀間隔標準差**：見 `reports/A04_polling.md`「量測方法」
- **掉幀率**：讀 `$H` 行的 `drop_A`/`drop_B`（本 story 之前 `A05`/`A06` 做的，
  是**自開機累積值**，測試開始與結束各記一次，相減再除以理論總幀數）
- **heap 走勢**：`$H` 的 `heap` 欄位，測試開始與結束各記一次，算 Δ
- **頻寬使用率**：見下方「理論頻寬對照」，實測用序列埠吞吐量工具（例如
  `pv` 接在 socat／cat 到 `/dev/ttyUSB0`）或用行長 × 實測幀率反推
- **stack 餘裕**：見上方第 3 點

## 理論頻寬對照（可先核對，不必等上機）

`CONTRACTS.md` §1.4 已經算過（`460800 baud ≈ 46 KB/s`）：

| 組態 | 理論使用率 |
|---|---|
| 4×4 @30Hz, Mel hop 256 | 54% |
| 8×8 @10Hz, Mel hop 256 | 70% |

`A14` 已把 hop 從 512 改成 256（`bone_mic.c:27` 現在是
`#define MIC_HOP_SAMPLES 256`），**目前程式碼裡已經沒有 hop=512 的路徑了**。
如果要對照「hop 512 撐不撐得住」，得手動把這個常數暫時改回 512 重編測一輪，
測完記得改回來——不是本 story 該做的重構，只是提供這個選項給上機測試的人。

## 四種組態對照表（照 A15.md 原表）

| 組態 | ToF Hz | 幀間 σ | 掉幀率 | Mel Hz | 頻寬 | heap Δ | ToF task stack 餘裕 | mic_task stack 餘裕 |
|---|---|---|---|---|---|---|---|---|
| 4×4 +Mel | 待測 | 待測 | 待測 | 待測 | 待測 | 待測 | 待測 | 待測* |
| 4×4 −Mel | 待測 | 待測 | 待測 | — | 待測 | 待測 | 待測 | — |
| 8×8 +Mel | 待測 | 待測 | 待測 | 待測 | 待測 | 待測 | 待測 | 待測* |
| 8×8 −Mel | 待測 | 待測 | 待測 | — | 待測 | 待測 | 待測 | — |

\* `mic_task` stack 餘裕：目前沒有現成的 log 輸出這個數字（見上方第 3 點），
需要 `bone_mic.c` 的負責人補一行 `uxTaskGetStackHighWaterMark()` log，或人工
用除錯工具查。

## 判讀門檻

| 指標 | 過 | 不過 | 不過時怎麼辦 |
|---|---|---|---|
| 4×4 ToF 幀率 | 兩顆皆 ≥ 29 Hz | 任一 < 29 Hz | 回頭檢查 `A04` 的量測是否重現；懷疑輪詢週期又被改壞 |
| 8×8 ToF 幀率 | ≥ 9.8 Hz | < 9.8 Hz | 同上 |
| 幀間隔標準差 | < 3 ms | ≥ 3 ms | 依 `A04.md` 升級方案 B（`FREERTOS_HZ`→1000）或方案 C（中斷） |
| 掉幀率 | ≤ 1% | > 1% | **驗收條件明定：腳本／流程要以非零狀態結束**，不能算過；先查 `drop_notready` 還是 `drop_error` 比較高，前者查 CPU/輪詢，後者查 I2C 接線 |
| heap 5 分鐘變化 | 下降 < 2 KB | 下降 ≥ 2 KB | 懷疑有記憶體洩漏，用 `heap_caps_get_info` 或逐一停用模組二分法定位 |
| FFT+Mel 單幀耗時（A10 門檻） | < 500 µs 全速 GO；500µs–2ms 勉強 GO | > 2ms 或 bin 對不上 | 停止 A11–A14 後續強化，改走 B14 主機端路線（`A10.md` 已寫的決策） |
| ToF task stack 餘裕 | > 1 KB | ≤ 1 KB | 調高該 task 的 stack 配置 |
| mic_task stack 餘裕 | > 1 KB | ≤ 1 KB | 同上，找 `bone_mic.c` 負責人 |

## 结果存放

依驗收條件，結果要存進 `reports/A15/`（本檔案是操作手冊放在 `reports/`，
實測產出的原始 log／CSV／截圖建議另外放 `reports/A15/`，檔名帶上 git sha
與日期，例如 `reports/A15/4x4_mel_<sha>_<date>.log`）。**這個子目錄本身還沒
建立，上機測試時再建即可**，不在這輪程式碼變更範圍內。

## 待人工完成事項

- 上面「四種組態對照表」全部欄位
- `mic_task` stack 餘裕的 log 輸出（需要 `bone_mic.c` 負責人補，或人工查）
- `reports/A15/` 目錄與原始量測檔案
- 依「判讀門檻」表逐項判定過/不過，不過的要照表裡的建議處理或回報給調度員
