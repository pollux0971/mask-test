# A10 — esp-dsp 整合探針（FFT 正確性）spike 報告

## 目的

在投入 A11／A12 的裝置端 Mel 實作前，先確認 `espressif/esp-dsp` 的 FFT
在這塊板子（ESP32-S3）上能不能跑、結果對不對、多快。這是一個 yes/no 決策，
不是功能。

## 決策門檻

| 實測單次 FFT 耗時（含 bit-reverse） | 決策 |
|---|---|
| < 500 µs | **GO** — 繼續 A11 / A12 裝置端 Mel |
| 500 µs – 2 ms | **勉強 GO** — 可行但 A15 效能回歸要密切盯著 |
| > 2 ms，或 bin 對不上 | **NO-GO** — 停止 A11–A14，改走 B14 主機端路線，A11/A12 從 backlog 移除 |

耗時基準：16 ms hop（Mel `hop=256 @ 16kHz`，見 `CONTRACTS.md` §3.1）。
2 ms 是 16 ms 的 12.5%，超過這個就會排擠 mic_task 的其他工作。

## 怎麼跑這個探針

`fft_probe_run()` 目前**沒有任何呼叫點**（刻意的，A10 範圍不含整合進
`app_main`／`mic_task`）。要上機測，照這幾步手動接一次：

1. 在 `vl53l7cx_test/main/vl53l7cx_test.c` 檔案開頭加一行 include：
   ```c
   #include "fft_probe.h"
   ```
2. 在 `app_main()` 裡，`uart_out_init()` 之後（越早跑越好，避免跟感測器
   輪詢搶 CPU 干擾計時），暫時加一行：
   ```c
   fft_probe_run();
   ```
3. `idf.py build flash monitor` 燒錄，看 log 輸出（`fft_probe` tag）
4. **量完之後把這行呼叫拿掉**（或留著加個 `#if 0`／menuconfig 開關，看團隊怎麼決定），
   這一行不是要長期留在 `app_main` 裡的功能碼，只是這次 spike 的觸發點

## 怎麼判讀 log

`fft_probe_run()` 會印一行類似：

```
I (xxx) fft_probe: N=512 expect_bin=32(+-1) got_bin=32 [OK] fft+bitrev=xxx us
```

- `got_bin=32 [OK]`：能量峰值落在 bin 32±1，FFT 數學結果正確
- `[MISMATCH]`：峰值落在別的 bin，FFT 結果不對 —— 直接判 NO-GO，不用等耗時數字
- `fft+bitrev=xxx us`：`dsps_fft2r_fc32` + `dsps_bit_rev_fc32` 兩步驟合計耗時
  （刻意把 bit-reverse 算進去，因為真實管線裡这一步跑不掉，只算 FFT 本體會低估）
- 若 `dsps_fft2r_init_fc32` 失敗，會印 `E (xxx) fft_probe: dsps_fft2r_init_fc32 failed: <code>`
  然後直接 return，不會有後面那行——這種情況也是 NO-GO（連初始化都失敗）

## 靜態檢查（我這邊已確認，無法上機的部分見下）

- 預期 bin 算法核對：`1000 Hz * 512 / 16000 Hz = 32`，與 `FFT_PROBE_EXPECT_BIN` 定義一致
- `dsps_fft2r_init_fc32` / `dsps_fft2r_fc32` / `dsps_bit_rev_fc32` / `dsps_fft2r_deinit_fc32`
  成對呼叫，init 失敗會提前 return（不會用未初始化的表跑 FFT）
- 編譯通過（`idf.py build`，scratch 目錄，無警告無錯誤），`espressif/esp-dsp` 依賴
  透過 Component Manager 正常解析

## 結論

**GO / NO-GO：待上機實測填寫。**

需要人工完成上面「怎麼跑這個探針」的步驟，把 log 裡的 `got_bin` 與
`fft+bitrev=xxx us` 填進下表，再依決策門檻表判定：

| 項目 | 數值 |
|---|---|
| got_bin | 待測 |
| 判定（OK / MISMATCH） | 待測 |
| fft+bitrev 耗時 | 待測 |
| GO / 勉強 GO / NO-GO | 待測 |
