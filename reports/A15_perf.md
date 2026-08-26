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

### 3. 收集 stack 餘裕（ToF task 與 mic_task，都已經在程式裡了）

`vl53l7cx_test.c` 的主迴圈每 10 次心跳（約每 10 秒）會印一行：
```
I (xxx) vl53l7cx: a15_perf: tof_task stack headroom = XXXX bytes
```
`bone_mic.c` 的 `mic_task` 每 10 秒也會印一行（拿到 `bone_mic.*` 的所有權後補的）：
```
I (xxx) bone_mic: a15_perf: mic_task stack headroom = XXXX bytes
```

### 4. 用 `tools/fw_regression.py` 跑，不要手動數

不用手動數行、算 σ、心算頻寬——直接跑：
```bash
python3 tools/fw_regression.py --port /dev/ttyUSB0 --duration 300 \
    --config "4x4+mel" --out reports/A15/
```
四種組態各跑一次，`--config` 換成對應標籤（`4x4+mel` / `4x4-mel` /
`8x8+mel` / `8x8-mel`，切法見上面第 1 步）。腳本會：
- 直接重用 `host/capture/protocol.py`／`host/capture/dropwatch.py`
  （B01/B03 的解析器與掉幀交叉驗證邏輯，沒有自己重刻一份）
- 算出 ToF 幀率、幀間隔標準差、掉幀率（靠 `seq` 缺口，跟裝置端 `drop_*`
  是兩個獨立算法，見 `dropwatch.py` 的 cross-check 設計）、Mel Hz、
  **實際頻寬使用率**（用序列埠層真實收到的 bytes/秒 ÷ 46080，不依賴
  `$H` 的欄位，見下方已知限制——46080 = 460800 baud ÷ 10 bits/byte，
  `tools/fw_regression.py` 的 `LINK_BYTES_PER_S` 就是這樣算的，跟
  `B20_bridge_throughput.md` 用的除數一致；本節先前寫的「÷46000」是
  文件本身的手誤，程式碼從頭到尾用的都是精確值）
- 掉幀率 > 1% 時**以結束碼 1 退出**（`echo $?` 檢查），符合 A15 驗收條件
- 把結果寫成 `reports/A15/<config>_<git-sha>_<時間>.md`（自動建立
  `reports/A15/` 目錄，檔名含 git sha——`git sha` 是韌體自己在 `$STATUS`
  的 `fw=` 回報的，不是主機這邊的 `git rev-parse`，確保報告對得上實際燒的韌體）

✅ **已知限制已解決**（`HANDOFF.md` dry-run 稽核時重新確認，2026-08-26）：
本節原本記錄「`host/capture/protocol.py` 的 `_parse_heartbeat()` 還在
檢查 `len(parts) == 7`，新加的頻寬欄位讓整行變成 8 段而被判成畸形行」——
**這個限制目前已經不成立**。實際重讀程式碼並實測：`_parse_heartbeat()`
現在是 `len(parts) < 7` 起接受（不是等號檢查），且明確讀 `parts[7]` 當
`bw_bytes_since_last`（缺席時是 `None`，不是 0——`0` 是合法的頻寬讀數，
用它當缺值會讓舊韌體看起來像「這段期間完全沒傳東西」，見程式碼註解）。
用一行合成的 8 欄 `$H`（`$H,123456,0,0,0,50000,42,7890`）餵給
`parse_line()` 實測，`bw_bytes_since_last` 正確回傳 `7890`，`heap`／
`temp_c` 也都正確解析，不再是 `N/A`。

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

這張表由 `tools/fw_regression.py` 四次輸出自動填（ToF Hz／幀間 σ／掉幀率／
Mel Hz／頻寬），只有 `heap Δ`（已知限制，見上）跟兩個 stack 餘裕（人工讀
`idf.py monitor` 裡的 `a15_perf:` log 行）要手動抄進來。

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

`tools/fw_regression.py --out reports/A15/` 會自動建立 `reports/A15/` 目錄
並把每次跑的結果寫成 `reports/A15/<config>_<git-sha>_<時間>.md`，滿足
驗收條件「結果存進 `reports/A15/`，含 git sha」，不需要人工另外建立或搬檔案。

## 待人工完成事項

- 實際上機跑 `tools/fw_regression.py` 四次（四種組態），把輸出結果的數字
  彙整進上面「四種組態對照表」
- 兩個 `a15_perf:` stack 餘裕 log（`idf.py monitor` 裡讀）手動抄進表格
- `heap Δ`：等 `host/capture/protocol.py` 的 `_parse_heartbeat()` 更新
  支援 8 欄 `$H` 後才能拿到（見上方已知限制），這條驗收條件本輪無法自動判定
- 依「判讀門檻」表逐項判定過/不過，不過的要照表裡的建議處理或回報給調度員
- FFT+Mel 單幀耗時仍照 `reports/A10_spike.md` 的步驟另外收集（`fw_regression.py`
  沒有處理這個，因為它要求拉高 `bone_mic` tag 的 log level 手動讀）
