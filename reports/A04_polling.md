# A04 — ToF 輪詢週期量測報告

## 改前 / 改後設定

| | 改前 | 改後 |
|---|---|---|
| 程式碼 | `vTaskDelay(pdMS_TO_TICKS(50))` | `vTaskDelay(pdMS_TO_TICKS(10))` |
| `CONFIG_FREERTOS_HZ` | 100（tick = 10 ms，未改） | 100（未改） |
| 實際 tick 數 | `pdMS_TO_TICKS(50) = 5 ticks` | `pdMS_TO_TICKS(10) = 1 tick` |
| 理論輪詢頻率 | 20 Hz | 100 Hz |
| 對 30 Hz 感測器的過取樣倍率 | 0.67×（**欠取樣**，會不規則丟幀） | 3.3×（足夠） |

## 這次審查發現的 0-tick 陷阱（專案資產，務必留存）

第一版修正把延遲寫成 `vTaskDelay(pdMS_TO_TICKS(5))`，原始推理是「`pdMS_TO_TICKS()`
會無條件進位，5 ms 在 10 ms tick 解析度下會進位成 1 tick」。**這個推理是錯的。**

`pdMS_TO_TICKS()` 的定義（`FreeRTOS-Kernel/include/freertos/projdefs.h`）：

```c
#define pdMS_TO_TICKS(xTimeInMs) \
    ((TickType_t)(((TickType_t)(xTimeInMs) * (TickType_t)configTICK_RATE_HZ) / (TickType_t)1000U))
```

是整數除法，**無條件捨去，不會進位**：`pdMS_TO_TICKS(5) = (5*100)/1000 = 500/1000 = 0`
（整數除法捨去餘數）。

`vTaskDelay(0)` 不會讓任務真正 block ——`tasks.c` 裡只有 `xTicksToDelay > 0` 才會把
任務放進 delayed list；等於 0 時只做 `portYIELD_WITHIN_API()`，任務幾乎立刻又被排回來。
`app_main` 這個 loop 因此變成 busy-loop，吃滿一顆核心的 CPU，把優先權更低的 IDLE task
餓死。本專案的 `sdkconfig` 開了 `CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0=y`，逾時
`CONFIG_ESP_TASK_WDT_TIMEOUT_S=5`，後果是開機約 5 秒後 task watchdog 觸發並重置
——比原本「20 Hz 欠取樣丟幀」的問題嚴重得多。

**教訓**：`CONFIG_FREERTOS_HZ` 低（本專案 100 Hz，tick=10ms）時，任何小於一個 tick
週期的 `pdMS_TO_TICKS()` 參數都會靜默變成 0，而 0 不是「幾乎不延遲」，是「完全不
block」。改 FreeRTOS 延遲參數前，先算一下 `所需毫秒數 × configTICK_RATE_HZ ÷ 1000`
是否 ≥ 1。

## 量測方法（給上機驗證的人）

**幀率**：在指定時間窗（建議 10 秒）內，數主機收到的 `$TOF,<A|B>,...` 行數，
除以時間窗秒數，A、B 兩顆分別算。4×4 模式驗收門檻兩顆皆 ≥ 29 Hz；8×8 模式 ≥ 9.8 Hz。

**幀間隔標準差**：目前 `$TOF` 輸出格式沒有 `t_us`／`seq` 欄位（見下方 CONTRACTS 相關
說明），無法用裝置端時間戳做精確差分。標準方法（待 T01 序列埠協定 v2 上線後可用）：
1. 主機依序記錄每一行 `$T,<A|B>,<seq>,<t_us>,...` 的 `t_us`
2. 對同一顆感測器的連續 `t_us` 做一階差分，得到逐幀間隔
3. 算這組間隔的標準差，驗收門檻 < 3 ms

在 T01 上線前的暫時替代方案：改用主機收到每行的**到達時間戳**（而非裝置端 t_us）
做同樣的差分與標準差計算，準度較差（多了序列傳輸與 host 排程的抖動），僅供粗略參考。

## 實測數字

**待上機填寫（阻擋於 T01 協定 v2 + E01 冒煙測試）。**

| 指標 | 改前（50ms/20Hz） | 改後（10ms/100Hz） |
|---|---|---|
| 4×4 Sensor A 實測幀率 | 待測 | 待測 |
| 4×4 Sensor B 實測幀率 | 待測 | 待測 |
| 8×8 Sensor A 實測幀率 | 待測 | 待測 |
| 8×8 Sensor B 實測幀率 | 待測 | 待測 |
| 幀間隔標準差 | 待測 | 待測 |
