# BOOT_OUTPUT — 開機時序列埠上會出現什麼

**範圍**：純讀 `vl53l7cx_test/main/` 原始碼 + sdkconfig，沒有碰板子、沒有改韌體。
**目的**：給 `reports/FIRST_REAL_DATA.md`（第一筆真實資料會在哪裡噎到）當上游輸入 —
主機端解析器一直只餵過 `mock_device.py` 產生的乾淨 `$` 行，這份文件列出真板子在那之前/之間
會混進哪些格式。

---

## 1. 開機後序列埠上依序會出現什麼

板子的 console UART 是 **UART0，設定死在 sdkconfig 裡是 460800 baud**
（`CONFIG_ESP_CONSOLE_UART_BAUDRATE=460800`，同一個值也用在
`CONFIG_ESPTOOLPY_MONITOR_BAUD`，所以 `idf.py monitor` 會用 460800 開 port）。

依時間順序：

### (a) Mask ROM 開機訊息 —— 🔴 這一段大機率是亂碼
```
rst:0x1 (POWERON_RESET),boot:0x8 (SPI_FAST_FLASH_BOOT)
SPIWP:0xee
mode:DIO, clock div:1
load:0x3fce3810,len:...
...
entry 0x403...
```
這些是晶片 **Mask ROM**（燒錄在矽片裡，不是我們的程式，sdkconfig 完全管不到）印的，
用的是 ROM 自己寫死的固定 baud（ESP32 系列常見是 74880 或 115200，**不是** 460800）。
但 `idf.py monitor` 一開始就用 460800 去讀 port —— **baud 對不上，這幾行在終端機上會是亂碼**
（看起來像一串隨機符號，不是文字）。這是 ESP32 系列的通病，不是這塊板子或這份韌體特有的問題。

### (b) 第二階段 bootloader 的 log —— 應該正常，走 460800
```
I (27) boot: ESP-IDF v6.0.2 2nd stage bootloader
I (27) boot: compile time ...
I (30) boot: chip revision: ...
I (34) boot.esp32s3: Boot SPI Speed : ...
I (39) boot: Partition Table:
I (43) boot:  ## Label ... Usage
...
I (xx) boot: Loaded app from partition at offset 0x10000
```
`CONFIG_BOOTLOADER_LOG_LEVEL=3`（INFO），這段第二階段 bootloader **共用主專案的
`CONFIG_ESP_CONSOLE_UART_BAUDRATE=460800`**，所以理論上這裡開始 baud 就對得上，
文字應該可讀。(a)→(b) 交界處確切在哪一個 byte 沒辦法從原始碼推，只能實測。

### (c) ESP-IDF app 啟動的 log —— 正常，460800，`I (...) tag: ...` 格式
```
I (xx) cpu_start: Pro cpu up.
I (xx) cpu_start: Starting app cpu, entry point is ...
I (xx) cpu_start: Pro cpu start user code
I (xx) cpu_start: cache ...
I (xx) app_init: Application information:
I (xx) heap_init: Initializing. RAM available for dynamic allocation:
I (xx) spi_flash: detected chip: ...
I (xx) main_task: Started on CPU0
I (xx) main_task: Calling app_main()
```
這些全部**在我們的 `app_main()` 被呼叫之前**印出，`CONFIG_LOG_DEFAULT_LEVEL=3`(INFO)，
一行沒被關掉。也就是說：**在使用者自己的程式碼跑起來之前，序列埠已經吐了幾十行
`I (數字) tag: 文字` 格式的東西**，沒有一行是 `$` 開頭。

### (d) `app_main()` 開始跑之後
原始碼 `main/vl53l7cx_test.c:311-343`：

```c
void app_main(void)
{
    uart_out_init();       // 317 -- 只是建立一個 mutex，不碰 UART 硬體
    tof_print_status();    // 324 -- 第一個 $ 行：$STATUS,...
    vTaskDelay(100ms);     // 327
    for (每個感測器) init_bus_and_sensor(i);   // 336-339 -- 期間會印 ESP_LOGI/E
    bone_mic_init();        // 341
    bone_mic_start_monitor(); // 342 -- 生出 mic_task (priority 5)
    uart_cmd_start();       // 343 -- 生出 uart_cmd_task (priority 5)
    while (1) { ... }       // 之後才是穩定狀態：$T/$A/$H 循環
}
```

**確認回答「$STATUS 是感測器 init 之前還是之後印的」**：**之前**。
`uart_out_init()` 之後、感測器 init 迴圈之前就送出 `$STATUS`，這是刻意設計
（見程式碼註解：host 端要盡快拿到 proto/fw 版本做協商，不必等 I2C 韌體上傳跑完）。

**`$STATUS` 是全程第一行 `$` 開頭的輸出**，但它前面已經有 (a)(b)(c) 三段、
少說幾十行非 `$` 格式的東西。

`init_bus_and_sensor()` 內部（`vl53l7cx_test.c:136-183`）在失敗/成功時會印
`ESP_LOGE`/`ESP_LOGI`（例如 `[A] loading firmware into sensor...`、
`[A] ranging started (8x8 @ 15Hz)`，或失敗時 `i2c_new_master_bus failed: ...`）。
這一段跑的時候**只有 app_main 這一個 task 在動**（`bone_mic_start_monitor()`/
`uart_cmd_start()` 還沒被呼叫），所以這幾行 log 不會跟任何 `$` 行搶著印
—— 這段本身是安全的。

---

## 2. 🔴 穩態之後：`$` 行有沒有被切開的風險

**有，而且是結構性的，不是罕見 edge case。**

### 機制
- `uart_out_lock()`/`uart_out_unlock()`（`uart_out.c`）只是一個 FreeRTOS mutex，
  只在**我們自己寫的 `$` 行輸出函式**裡使用（`print_tof_line`/`print_ambient_line`/
  `tof_print_status`/`tof_print_heartbeat`（vl53l7cx_test.c）、
  `bone_mic_record_and_dump`/mic_task 的 `$M`/`$F` 輸出（bone_mic.c)）。
- **`ESP_LOGI`/`ESP_LOGW`/`ESP_LOGE`/`ESP_LOGD` 完全不走這把鎖**——它們底層直接呼叫
  `vprintf` → console UART，跟 `uart_out_lock` 一點關係都沒有。
- 三個會印東西的 task 優先權不對等：
  - `app_main`（主 ToF 迴圈，$T/$A/$H/$STATUS 的來源）：**預設 main task 優先權**
    （`CONFIG_ESP_MAIN_TASK_PRIORITY`，通常是 1，比另外兩個低）
  - `mic_task`（`bone_mic.c:432`）：**priority 5**
  - `uart_cmd_task`（`uart_cmd.c:282`）：**priority 5**

  FreeRTOS 是搶佔式排程：**priority 5 的 task 只要就緒，隨時可以打斷 priority 1 的
  app_main**，不管 app_main 當下正不正在印 `$T` 行、有沒有拿著 `uart_out_lock`。

### 具體會被切開的組合
`print_tof_line()`（vl53l7cx_test.c:197-216）建一行 `$T,...` 要呼叫 **超過 130 次 `printf()`**
（8×8=64 個 zone 的距離、再 64 個訊號值，每個 zone 各一次 `printf`）——這中間有大量
排程器插手的機會。如果就在這 130 次 `printf` 之間，`mic_task` 或 `uart_cmd_task`
（priority 5，會馬上搶到 CPU）剛好呼叫了一行**不在鎖保護內**的 log，比如：

- `bone_mic.c:192` `ESP_LOGW(TAG, "i2s_channel_read failed during recording: %s", ...)`
- `bone_mic.c:422` `ESP_LOGI(TAG, "a15_perf: mic_task stack headroom = %u bytes", ...)`
  （每 10 秒固定會印一次，不是只有出錯才印，**發生機率不算低**）
- `uart_cmd.c:268` `ESP_LOGW(TAG, "unrecognised command: '%s'", cmd)`
- `vl53l7cx_test.c:366-367` 同一個 tof_task 自己的 `a15_perf` log（同 task 不會打斷自己，
  但如果剛好卡在两个 printf 之間被 mic_task/uart_cmd_task 搶走 CPU、那兩個 task 又剛好
  也要印東西，一樣會夾進去）

→ 結果就是 host 端會收到一行**看起來完整但其實是「半行 $T + 一整行 log + 剩下半行 $T」**
拼起來的東西，例如：

```
$T,A,42,1234567,64,120,130,...
I (98765) bone_mic: a15_perf: mic_task stack headroom = 3200 bytes
...,145,-1,138
```

單純用「找 `$` 開頭、找換行」的 parser 會把中間那行 log 當成一行雜訊丟掉沒關係，
但**如果 parser 假設『看到下一個換行就代表這個 `$T` 行結束了』，就會把 `$T` 行從中間
截斷，得到一行欄位數不對、CSV 解析會炸掉的殘缺 `$T`**——這是最危險的情況，
必須確認 host 端 parser 是「數欄位對不對」還是「看到換行就收工」。

### 什麼情況「不會」被切
- `app_main` 前段（感測器 init 期間）：只有一個 task 在跑，安全。
- 兩個 lock 保護的 `$` 行函式**彼此之間**互斥沒問題（這是鎖原本要解決的問題，做到了）。
- 問題只在「**鎖保護的 `$` 輸出**」vs「**沒被鎖保護的 ESP_LOG**」這一種組合。

---

## 3. 把 ESP-IDF log 導去別處，划不划算？（只評估，這一輪沒改程式碼）

`esp_log_set_vprintf()` 是 ESP-IDF 標準 API，可以把所有 `ESP_LOG*` 的輸出攔截到自訂函式
（丟掉、轉去 UART1、寫進一塊記憶體 buffer 都可以），一行 `esp_log_set_vprintf(my_func)`
在 `app_main()` 最前面呼叫即可，**改動量很小**。

⚠️ 但這一輪**不建議現在改**，理由：
1. 使用者現在燒的是**沒有這個改動**的版本，「最小改動優先」是他剛下的指示
   （[[minimal-change-first]]）。
2. 就算做了，也**只能解決「穩態之後的 ESP_LOG」**——(a)(b)(c) 三段開機噪音發生在
   `app_main()` 被呼叫**之前**，`esp_log_set_vprintf()` 再早呼叫也來不及攔到，
   ROM 那段（(a)）更是完全管不到（硬體限制，不是任何 sdkconfig/程式碼能改的）。
3. 治本的做法其實是「host 端 parser 已經預期會有雜訊行，只看 `$` 開頭 + 用欄位數
   驗證完整性」，而不是「板子這邊想辦法印乾淨」——後者永遠堵不完（ROM 那段就堵不了）。

如果之後真的要做，**低成本的部分**只有「把 `ESP_LOG_INFO` 等級的一般訊息關小聲，只留
`ESP_LOGE`」（`esp_log_level_set()` 或调低 `CONFIG_LOG_DEFAULT_LEVEL`），可以砍掉
`a15_perf` 這種週期性噪音，但**出錯時你最需要看到的 `ESP_LOGE` 反而還是會插進 `$` 行中間**
——所以這治標不治本，只是降低頻率，沒有解決結構性問題。

---

## 4. 給使用者的一句話

**燒完開機，你會先看到一小段亂碼（ROM 印的，大約幾行，正常，不用管），
接著一堆 `I (數字) 標籤: 文字` 格式的正常開機訊息，
然後才會看到第一行 `$STATUS,...`——這才是我們自己的程式開始跑。**

之後長時間跑的時候，**偶爾**會在 `$T`/`$M`/`$F` 這類長資料行中間**混進一行不是 `$`
開頭的普通文字**（通常是 `I (...) bone_mic: a15_perf: ...` 或 `W (...) uart_cmd: ...`）
——**這是已知、預期內的行為，不代表板子壞了**，是因為印 log 跟印 `$` 資料行是兩條不同
的路徑，沒有互相排隊。如果看到的是**一行 `$T` 莫名其妙欄位數不對、缺一截**，
才是需要回報的問題（代表被夾斷的那次，剛好夾在 `$T` 行正中間，不是夾在行尾）。

---

## 5. 給 FIRST_REAL_DATA.md 的重點摘要

- 第一份真實資料進 host 端 parser 之前，**前面已經有幾十行非 `$` 格式的開機噪音**
  （其中最前面幾行是**亂碼**，不是普通文字，parser 若對非 UTF-8 位元組沒做防禦
  可能直接噎住）。
- **第一行 `$` 開頭的東西是 `$STATUS`**，在感測器 init 之前送出——如果 parser
  預期「先看到感測器資料才看到 STATUS」，順序是反的。
- **`$T`/`$M`/`$F` 長行有機率被一行 log 從中間切斷**，不是理論風險、是目前程式碼結構
  必然存在的 race，機率取決於 mic_task/uart_cmd_task 的 log 呼叫頻率
  （`a15_perf` 每 10 秒至少一次，是最穩定會發生的來源）。
  ⚠️ **更正**：見 §6——這個機率比本文件先前估的高得多，不是「10 分鐘大概撞不到一次」。

---

## 6. 🔴 更正前面的機率估計 + 最小韌體修法（已驗證過，**沒有套用**）

### 6.1 先更正 §2／`reports/E01_bringup_checklist.md` §1.2 的機率估計

先前（包含我自己寫進 `E01_bringup_checklist.md` §1.2 的那句）把碰撞機率估成
「千分之幾量級，10 分鐘大概撞不到一次」——**這個估計錯了，錯在想像 UART 是
「印完就丟進緩衝區、CPU 幾乎瞬間跑完」**。去讀了 `uart_cmd.c:279` 實際呼叫的
`uart_driver_install(UART_NUM_0, 512, 0, 0, NULL, 0)`，第三個參數
**`tx_buffer_size = 0`**。查了 ESP-IDF 驅動原始碼
（`~/esp/esp-idf/components/esp_driver_uart/src/uart.c:1654-1676`，
`uart_tx_all()`）：**`tx_buffer_size == 0` 代表完全沒有 TX ring buffer，
`uart_write_bytes()`/`printf()` 是真的照 wire 速度**逐段等 `tx_fifo_sem`**阻塞
到送完為止**，不是丟進緩衝區就返回。

重新估：
- 460800 baud，8N1，等效 46080 bytes/s。
- 4×4（`dim=16`）一行 `$T` 約 140-150 bytes ≈ **3.2ms** 傳輸時間；
  8×8（`dim=64`）一行約 550-650 bytes（超過那個已經是 0 的緩衝區，
  更沒有任何緩衝空間可言）≈ **12-14ms**。
- `uart_out_lock()` 整段（`print_tof_line()`/`print_ambient_line()`）
  就是持鎖持續這整段實際傳輸時間，不是持鎖幾十微秒。
- 4×4 @30Hz：A+B 兩顆各鎖一次 ≈ 6.4ms 鎖持有時間 / 33ms 幀週期
  ≈ **占用率約 19%**。
- 8×8 @10Hz：A+B 兩顆 ≈ 24-28ms / 100ms 幀週期 ≈ **占用率約 25-28%**。
- `a15_perf` 每 10 秒觸發一次，時機跟 `$T` 的鎖週期沒有同步關係，
  可以當成落在鎖持有窗口內的機率 ≈ 占用率本身。

**修正後的估計：每次 `a15_perf` 觸發，大約 1/5 到 1/4 的機率撞上，不是千分之幾。**
10 分鐘內 `a15_perf` 觸發約 60 次，預期撞上（因而弄丟一行 `$T`）約
**12-17 次**——不是「大概率一次都不會撞上」。

這個更正也代表 §6.2 這個修法**比原先想的更值得做**。

### 6.2 修法：把 `a15_perf`（`mic_task` 那一行）包進 `uart_out_lock()`

**可行性檢查**：

| 疑慮 | 結論 |
|---|---|
| `uart_out_lock()` 是什麼鎖？`mic_task` context 拿得到嗎？ | 純 FreeRTOS mutex（`xSemaphoreCreateMutex`，`uart_out.c:11`），支援 priority inheritance，一般 task context 呼叫沒有限制。 |
| 死結風險？ | 沒有。掃過全部 `uart_out_lock()`/`unlock()` 呼叫點（`bone_mic.c`、`vl53l7cx_test.c`），鎖保護的範圍永遠只是一串 `printf()`，沒有巢狀鎖、沒有在鎖內呼叫任何會回頭等 `mic_task` 自己的東西（I2S、queue）。 |
| 會不會讓 `mic_task` 卡太久、掉音訊幀？ | **不會，有約 5-7 倍餘裕。** 最壞情況卡在 `tof_task` 剛好持鎖印一行 8×8 的 `$T`：≈13ms。`i2s_channel_read()` 讀的是 DMA 緩衝（`I2S_CHANNEL_DEFAULT_CONFIG`：`dma_desc_num=6`、`dma_frame_num=240`，總容量 1440 samples @16kHz ≈ **90ms**），`mic_task` 每次只吃 256 samples（16ms）份，也就是說在下一次 `i2s_channel_read()` 被叫之前，硬體最多能吸收約 90ms 的落後，而不是 mic_task 一卡住就馬上掉樣本。13ms 遠低於 90ms。 |

**結論：可行，代價可接受，值得做。**

**精確改法**（`main/bone_mic.c` 第 420-425 行，現在的原始碼）：

```c
        if (t_end - last_stack_log_us >= 10 * 1000000) {
            UBaseType_t words_free = uxTaskGetStackHighWaterMark(NULL);
            ESP_LOGI(TAG, "a15_perf: mic_task stack headroom = %u bytes",
                     (unsigned)(words_free * sizeof(StackType_t)));
            last_stack_log_us = t_end;
        }
```

改成（只加兩行 `uart_out_lock()`/`uart_out_unlock()`，包住原本那一行 `ESP_LOGI`）：

```c
        if (t_end - last_stack_log_us >= 10 * 1000000) {
            UBaseType_t words_free = uxTaskGetStackHighWaterMark(NULL);
            uart_out_lock();
            ESP_LOGI(TAG, "a15_perf: mic_task stack headroom = %u bytes",
                     (unsigned)(words_free * sizeof(StackType_t)));
            uart_out_unlock();
            last_stack_log_us = t_end;
        }
```

`bone_mic.c` 已經 `#include "uart_out.h"`（第 19 行），不用加 include。

**已實際套用測試**：`idf.py build` 過，**0 warning**。測完已改回去，
`git diff` 確認乾淨。

**這個修法沒動到 log 的文字內容**——`reports/A15_perf.md` §3 說
`tools/fw_regression.py` 靠人工從 `idf.py monitor` 抄 `a15_perf:` 這行
（含 `bone_mic.c`/`vl53l7cx_test.c` 兩邊各一行）填表，這個修法只是「印之前
先排隊」，印出來的文字、tag、格式一個字都沒變，`A15` 的量測方法不受影響。

**沒解決的部分（範圍內就是如此，不是這次沒做完）**：
- `vl53l7cx_test.c` 自己那行 `a15_perf`（同 task 印 `$T`，本來就不會撞自己，不用改）。
- 開機期間（ROM + IDF 元件初始化）的雜訊——`app_main()` 都還沒開始跑，
  `uart_out_lock` 這時候根本不存在，管不到，跟本文件 §1 是同一個結論。
- `uart_cmd.c`/`bone_mic.c` 其餘的錯誤/指令觸發 log（i2s 失敗、
  `SENS`/`AMB`/`MEL`/`REC` 回應、`unrecognised command`）——這些是事件觸發，
  不是穩態週期性噪音，這次沒有一併包進鎖裡。如果之後要做「徹底沒有殘留風險」，
  這些也要包，但那是更大範圍的改動，不是這次「最小修法」的範圍。

### 6.3 一句話建議

**值得搭 FFT 那次燒錄一起做。** 兩行改動、`uart_out_lock()`/`uart_out_unlock()`
已經是現有機制、風險（音訊延遲 13ms vs 90ms 容量）遠低於現在確認會發生的
問題（穩態下每 10 分鐘約 12-17 次 `$T` 遺失，且會被主機端誤判成傳輸層故障，
見 `E01_bringup_checklist.md` §1.2）。唯一的理由不做是「使用者這次只想燒
FFT probe，不想在同一次燒錄夾帶任何其他改動」——如果是這樣，這是可以晚一次
燒錄再做的東西，不影響 FFT probe 那次量測本身。

---

## 7. 下次重燒要合併的三個改動（已驗證過，**沒有套用**——見文末事故說明）

三個改動：`a15_perf` log race（§6）、`check_data_ready()` 失敗要計數
（`E01_bringup_checklist.md` §0.7）、**加碼**：把 FFT probe 改成指令觸發，
這樣只要燒一次就能同時拿到 FFT 量測跟另外兩個永久修正，之後也能隨時重測
FFT，不用為了這件事再燒一次。

### A. 三個改動各自的內容

**A1. `main/bone_mic.c` 第 420-425 行**——`a15_perf` log 包進 `uart_out_lock()`：

```c
        if (t_end - last_stack_log_us >= 10 * 1000000) {
            UBaseType_t words_free = uxTaskGetStackHighWaterMark(NULL);
            uart_out_lock();
            ESP_LOGI(TAG, "a15_perf: mic_task stack headroom = %u bytes",
                     (unsigned)(words_free * sizeof(StackType_t)));
            uart_out_unlock();
            last_stack_log_us = t_end;
        }
```
（只加 `uart_out_lock();`/`uart_out_unlock();` 兩行）

**風險**：mic_task 最壞卡 ~13ms（等 8×8 的 `$T` 印完），I2S DMA 緩衝
（`dma_desc_num=6 × dma_frame_num=240` ≈ 90ms 容量）能吸收，5-7 倍餘裕，
不會掉音訊幀。

**A2. `main/vl53l7cx_test.c` 第 402-403 行之後**——`check_data_ready()`
失敗也累加計數器：

```c
            uint8_t ready = 0;
            uint8_t status = vl53l7cx_check_data_ready(&s_dev[i], &ready);
            if (status != 0) {
                /* A05: the I2C transaction itself failed (bus down, loose
                 * connection) -- distinct from get_ranging_data() failing
                 * below, but was previously silent: nothing incremented,
                 * $H's drop_A/B stayed frozen, indistinguishable from a
                 * sensor that never dropped a frame. */
                s_drop_error[i]++;
            } else if (ready) {
```
（原本是 `if (status == 0 && ready) {`，改成 `if (status != 0) { s_drop_error[i]++; } else if (ready) {`，後面整段大括號內容不變）

**風險**：無。純加一個計數分支，不影響任何既有邏輯路徑（`status==0 &&
!ready` 這個最常見的「還沒準備好」情況，兩種寫法行為完全一樣）。

**A3+A4. FFT probe 改指令觸發**（跟 E01 §1.1 現在寫的「暫時加兩行、測完拆掉」
是兩種不同做法，這個是**永久留著**、隨時能重測）：

🔴 **附帶發現，這是這輪意外查到的**：`fft_probe.c` 原本結尾呼叫
`dsps_fft2r_deinit_fc32()`——查過 esp-dsp 原始碼
（`managed_components/espressif__esp-dsp/modules/fft/float/dsps_fft2r_fc32_ansi.c`），
這個函式操作的是**整個函式庫共用的一份全域狀態**
（`dsps_fft_w_table_fc32`／`dsps_fft2r_ram_rev_table`），跟 `bone_mic.c`
的 mel pipeline 用的是**同一份**（都是 N=512）。**在開機當下呼叫**（現在
E01 §1.1 的用法）是安全的，因為那時候 `mic_task` 還沒啟動、還沒宣稱擁有
這份全域狀態。**但如果改成指令觸發，指令一定是在 `mic_task` 已經跑起來
之後才可能送達**（`uart_cmd_start()` 在 `app_main()` 裡排在
`bone_mic_start_monitor()` 後面）——這時候再呼叫 `deinit`，會把
`mic_task` 正在用的 `dsps_fft2r_ram_rev_table` 釋放掉，**下一次 mel
pipeline 的 FFT 呼叫就是 use-after-free**，輕則 `$F` 輸出開始亂掉，
重則直接當機。**這是先前程式碼裡沒人踩到的地雷，因為 fft_probe_run()
之前只在開機當下、mic_task 還沒起來時被呼叫過。**

**修法**：拿掉 `fft_probe.c` 結尾那行 `dsps_fft2r_deinit_fc32();`。
不呼叫 deinit 對兩種用法都安全——開機時用，`mic_task` 之後自己
`dsps_fft2r_init_fc32()` 會看到已經初始化過，直接跳過（原本的行為就是
如此，只是不再自己拆掉）；指令觸發時用，本來就該共用 `mic_task` 已經
建好的表，不去動它。

`main/fft_probe.c` 結尾：
```c
    ESP_LOGI(TAG, "N=%d expect_bin=%d(+-%d) got_bin=%d [%s] fft+bitrev=%lld us",
             FFT_PROBE_N, FFT_PROBE_EXPECT_BIN, FFT_PROBE_BIN_TOLERANCE, peak_bin,
             bin_ok ? "OK" : "MISMATCH", (long long)dt_us);

    /* No dsps_fft2r_deinit_fc32() here: ... (完整理由見程式碼註解) */
}
```
（刪掉原本結尾的 `dsps_fft2r_deinit_fc32();` 這一行，換成一段解釋為什麼
不呼叫的註解）

`main/uart_cmd.c`——加 `#include "fft_probe.h"`，並在 `uart_cmd_task()`
的指令判斷鏈裡加一個新分支：

```c
        } else if (strcmp(cmd, "FFTPROBE") == 0) {
            /* A10 diagnostic, not part of CONTRACTS.md #1.2 -- one-shot,
             * no state change, safe to run anytime after boot (see
             * fft_probe.c for why it no longer deinits shared FFT state). */
            fft_probe_run();
        }
```
（接在既有的 `REC:%d` 分支之後、`else` 之前）

⚠️ **`FFTPROBE` 是新指令，不在 `CONTRACTS.md` §1.2 目前定義的指令集裡**
——雖然是純診斷、不改任何資料格式，但 §1 是 FROZEN 章節，**新增指令
照規矩要走你的審查流程，這裡只當提案，不算已核准**。

**風險**：`fft_probe_run()` 本身只跑一次 512 點 FFT，耗時 <2ms
（見 `A10_spike.md`），在 `uart_cmd_task`（priority 5）裡執行不會拖慢
任何東西；不呼叫 deinit 後也不會再干擾 `mic_task`。

### B. 🔴 三個一起改，真的編過了

**這次是把上面 A1-A4 全部一次套用**（不是分開驗證各自 OK 就假設加總沒事）：
`bone_mic.c`（A1）、`vl53l7cx_test.c`（A2）、`fft_probe.c`（A3）、
`uart_cmd.c`（A4）四個檔案一起改，`idf.py build`——**過了，0 warning**
（`grep -ic "warning:"` 明確確認是 0，不是沒看到就當沒有）。

**已經改回去**——四個檔案的內容都還原成套用前的樣子，`git diff` 應該乾淨
（見文末「這輪的事故」，還原過程中撞到一個 git 問題，已經另外回報，
跟這份技術內容本身無關）。

### C. 順序與相依：其實只要燒一次

**FFT probe 原本（E01 §1.1 現在寫的版本）是「暫時加兩行、測完拆掉」，
代表要燒兩次**——第一次測 FFT，第二次拆掉恢復正常版本。

**改成指令觸發之後，這個限制消失了**：
- **只要燒一次**：把 A1（a15_perf lock）、A2（drop 計數）、A3+A4
  （FFT probe 改指令觸發，永久留著，不用拆）三個一起燒進去
- 燒完之後，**任何時候**想測 FFT，直接對序列埠送 `FFTPROBE` 這個指令，
  不用重燒——`E01_bringup_checklist.md` §1.1 的「測完把兩行拆掉再重燒
  一次」這個步驟**整段可以刪掉**，改成「送一次 `FFTPROBE` 指令」
- **代價**：多了一個非契約定義的指令、`fft_probe.c` 從「開機用一次即丟」
  變成「永久留在正式版裡的診斷工具」——程式碼量沒有變小，但換來的是
  以後 FFT 效能任何時候都能重測（例如換了 Mel 設定、懷疑效能退化時），
  不用再走「改程式碼、重燒、測完再改回去」這一整套流程

### D. 給使用者照抄的操作（他會戴著裝置做這件事）

```
. ~/esp/esp-idf/export.sh
cd vl53l7cx_test
# 四個檔案的改動已經準備好（見上面 A1-A4），套用之後：
idf.py -p /dev/ttyUSB0 build flash monitor
```

燒完開機、看到 `$STATUS` 那一行之後，**在 monitor 裡直接打
`FFTPROBE` 按下 Enter**，看 log 裡 `fft_probe` 這個 tag 印出來的
`N=512 expect_bin=32(+-1) got_bin=... fft+bitrev=... us`，照
`A10_spike.md` 的判準表填 GO/NO-GO。**之後任何時候想再測一次，
同樣打 `FFTPROBE` 就好，不用再重燒。**

---

## 8. 這輪的事故：我的測試改動被意外掃進一個不相干的 commit

**跟上面 §6/§7 的技術內容無關，是這次執行過程中的流程問題，記錄下來
避免下次重演。**

驗證 §7 的三個改動時，四個檔案（`bone_mic.c`／`vl53l7cx_test.c`／
`fft_probe.c`／`uart_cmd.c`）短暫處於「已套用、還沒改回去」的狀態
（用來跑 `idf.py build` 驗證）。**在改回去之前**，這 4 個檔案的改動被
另一個 commit（`4b4215a`，訊息是「per-trial sensors_seen：中途掉線才
看得出來的那一層」，內容其實是 `session_writer.py`，**完全沒提到這 4
個韌體檔案**）意外一起帶進去，而且**已經推上 `origin/main`**。

已經用 `git checkout <該 commit 的父提交> -- <4 個檔案>` 把工作目錄還原
成套用前的樣子（不是改寫 git history，只是把檔案內容還原），重新
build 確認正常，**現在工作目錄跟使用者板子上跑的版本一致**。但
`origin/main` 上那個 commit 裡仍然留著這次洩漏的內容，需要負責
commit/push 的人決定要不要另外補一個 revert commit。細節已經另外
訊息回報過，這裡只留一句話存證，避免這份報告看起來像「東西已經套用了」
——**沒有，§7 的三個改動目前的正確狀態是「已驗證、未套用」，跟原本
的意思一樣，只是中間繞了一圈。**

---

## 9. 主機中途重新連線（`POST /device/connect`），韌體那邊會怎樣

**背景**：新功能要做「重開序列埠連線，不重啟 bridge 行程」，代表主機會在
板子已經跑了一陣子之後中途接上去。純讀 `vl53l7cx_test/main/`（唯讀）
回答四個問題，**沒有動韌體、沒有碰 `/dev/ttyUSB0`**。

### 1. 🔴 板子知道主機斷開過嗎？——**不知道，而且這是對的，不用修**

確認：韌體完全沒有「host 有沒有在聽」這個概念。`app_main()` 的主迴圈、
`mic_task`、`uart_cmd_task` 全部無條件往 UART 印，`s_seq[i]`
（`vl53l7cx_test.c:437`）跟 `m_seq`/`f_seq`（`bone_mic.c`）**持續累加，
不管有沒有人在讀**——沒有任何程式碼路徑會偵測或反應「連線斷開」。

**`DropTracker` 會不會因此算出一個巨大的假掉幀數？——不會，機制已經擋住了**：
查過 `host/capture/dropwatch.py`（B 軌的東西，這裡只讀不改，用來回答這題）
——`DEFAULT_GAP_LIMIT=1000`：如果重連時 `seq` 一次跳超過 1000
（30Hz 下約 33 秒的斷線就會超過這個門檻，中途重連的斷線時間通常遠不止
33 秒），這個 gap 會被歸類成「implausible」，**不會直接當成 missing 算
進掉幀數**——而是累積 `consecutive_anomalies`，滿 3 次
（`DEFAULT_RESYNC_AFTER=3`）之後觸發 `_restart()` 重新校準基準點，
且 `_restart()` 裡 `missing = seq if seq < gap_limit else 0`——**新的
`seq` 這麼大，`missing` 會是 0，不會捏造一個天文數字的假掉幀**。
唯一的代價：重連後的前 1-3 幀會被歸進 `anomalies` 計數，不算 received
也不算 missing，是最多 3 幀等級的統計小空白，不影響資料本身（實際的
`$T` 距離值照樣送進 `quality.observe_tof()`、進面板、進 HDF5，
`DropTracker` 的分類只影響掉幀率統計，不影響資料本身有沒有被收下）。
**這件事韌體端不用改，host 端的機制已經設計成擋得住這個情境**，是
`ed` 可以直接放心用的結論。

### 2. 主機中途接上時，第一行會看到什麼？——**大機率是 `ignored`，不是 `malformed`**

**跟 `ed` 猜的方向不太一樣，這裡更正一下**：中途接上序列埠時，作業系統
會從 UART 位元組流「當下正好傳到哪」開始讀，那個位置**幾乎不可能剛好是
一行 `$T` 的開頭**——`$` 只出現在每行最前面，一行 `$T`（4×4）大約
140-150 bytes，中途接上落在「行首那個 `$` 字元」的機率大約只有
1/150 量級，**絕大多數情況第一個讀到的殘片根本不是以 `$` 開頭**，
會被 `protocol.py` 的 `_parse_with()` 歸類成 `ignored`（跟裝置 log/
base64 payload 同一類，不是 `$` 開頭本來就不會被當成資料行嘗試解析），
**不會累加 `malformed`**。

只有在極少數情況（重連瞬間剛好卡在 `$` 那個位元組上，值列缺前面幾個
必要欄位）才會真的以 `$` 開頭但解析失敗，才算一筆 `malformed`——這種
情況**最多發生一次**，接下來的下一個 `\n` 之後就是一行完整、乾淨的
`$T`（UART 位元組流本身沒有壞，只是主機接上的時間點卡在中間），**恢復
是立即的，不會持續污染 `malformed_rate`**。跟 §2 查過的「log 被 a15_perf
切開」是不同的機制——那個是持續性、規律發生的；這個重連只在**開始
連線的那一瞬間**發生最多一次，之後不會再發生，除非又斷線重連一次。

### 3. `$STATUS`/`PING` 中途接上一樣可靠嗎？——**確認過，跟開機時完全一樣**

重新核對過 `uart_cmd.c:243-257` 的 `PING` 分支，在目前 HEAD 逐字比對
過，跟之前查過的結論一樣：`tof_print_heartbeat(); tof_print_status();`
兩行**無條件依序執行**，**沒有任何分支、計數器、或計時器**會依「開機
多久了」改變這條路徑的行為——這段程式碼本身**沒有狀態**，跟裝置跑了
10 秒還是 10 分鐘無關，`ed` 送 `PING` 過去，行為保證跟開機時一模一樣。

`$STATUS` 確實只在開機、`SENS`/`AMB`/`MEL` 改變、跟 `PING` 時送
（`vl53l7cx_test.c`/`uart_cmd.c` 全文搜過，沒有其他觸發點）——中途接上
的主機如果不主動 `PING`，理論上要等到使用者剛好送一次 `SENS:`/`AMB:`/
`MEL:` 才會等到下一次 `$STATUS`，**`ed` 的重連流程必須主動送一次
`PING`，不能假設 `$STATUS` 會自己跑出來**，這點你們應該已經知道，
這裡只是確認韌體那端沒有任何意外會讓 `PING` 中途變得不可靠。

### 4. 🔴 板子開機了但主機一直沒連上，`printf()` 會塞住嗎？——**不會，可以放心**

這是最重要的一題，答案是**不會卡住**：

`uart_cmd.c:279` 的 `uart_driver_install(UART_NUM_0, 512, 0, 0, NULL, 0)`
——`tx_buffer_size=0`，§6.1 已經查過 ESP-IDF 驅動原始碼確認這代表
`printf()` 會阻塞到資料真的送進硬體 TX FIFO 為止（`uart_tx_all()`
的 `tx_fifo_sem`）。**但這個阻塞等的是「硬體 128-byte FIFO 有沒有空位」，
不是「有沒有人在收」**——UART 這個電氣層本身沒有交握機制（不像 I2C
需要對方 ACK），TX 腳位就是照設定的 baud rate（460800）持續把位元
移出去，**跟另一端有沒有東西在聽完全無關**。硬體 FIFO 會不會清空、
騰出空位讓下一批資料進去，只取決於晶片內部的 baud-rate 時鐘，不取決於
主機端。

查過 `sdkconfig`：這個 UART **沒有設定 CTS/RTS 硬體流控**
（`i2c_dev_cfg`/`uart_driver_install` 呼叫都只給了 TX/RX 兩支腳，沒有
流控腳位），代表**沒有任何機制能讓「沒人收」這件事反向卡住 TX 端**。
沒接 USB 線、USB 橋接晶片沒被驅動、或主機軟體沒在讀，這三種情況在
ESP32 這端看起來完全一樣：**TX 照樣持續送出，FIFO 照樣以固定速率清空，
`printf()` 該多快返回就多快返回，`app_main`/`mic_task`/`uart_cmd_task`
全部正常運作，不會被「等主機」卡住**。

⚠️ **這是 ESP32 這一端能確認的範圍**——板子上實際的 USB-序列埠橋接晶片
（`/dev/ttyUSB0` 這個命名代表是外接橋接晶片而非 ESP32-S3 原生 USB，
兩者是分開的硬體）自己內部有沒有做任何緩衝或流控，屬於那顆晶片的行為，
不是這份韌體的原始碼管得到的範圍，**這裡只能保證 ESP32 那一側絕對不會
卡，橋接晶片那一側沒有原始碼可查，如果要 100% 排除，需要實測**（開著
板子、不接主機軟體，放個幾分鐘後再連，看 ToF/mic 的資料時間戳
`t_us` 有沒有連續、有沒有卡頓的痕跡）。

### 給 `ed` 的結論摘要

1. **`seq` 大跳躍不會產生假掉幀**——`DropTracker` 的 `gap_limit`/
   `resync_after` 機制已經處理好，最多丟失 1-3 幀的統計精度，不影響
   實際資料
2. **重連的第一個殘片大機率是 `ignored`，不是 `malformed`**——不會
   系統性污染 `malformed_rate`，就算最壞情況也只發生一次就恢復
3. **`PING`/`$STATUS` 中途一樣可靠**——這段程式碼沒有狀態，不受開機
   時長影響，重連流程主動送 `PING` 就一定拿得到 `$STATUS`
4. 🔴 **板子不會因為沒人連線而卡住**——ESP32 這端的 UART TX 沒有流控、
   沒有交握，"沒人聽" 不會讓 `printf()` 塞住，可以放心讓使用者先開板子
   再開軟體，順序不影響韌體

**這次沒有動 `vl53l7cx_test/main/` 底下任何東西，也沒有碰 `/dev/ttyUSB0`，
也沒有碰 `vl53l7cx_test/monitor/`。**
